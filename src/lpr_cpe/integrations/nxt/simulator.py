"""Fixture-backed stand-in for CommScope ServAssure NXT.

**No ServAssure NXT documentation was supplied** (IMPLEMENTATION_PLAN.md A1/A2). Every key below is
our invention: `rf_verdict`, `service_group_health`, the alarm shape, the PNM capture envelope. None
of it is a confirmed CommScope payload and none of it should be quoted at a vendor as though it
were. See `docs/vendor-integration-gaps.md`, gaps NXT-1 to NXT-5.

The one design decision worth defending here: `fetch_pnm_capture` returns an **object reference**
plus summary statistics, not a spectrum array. A real PNM capture is thousands of bins; putting one
in graph state means writing it to the checkpointer on every super-step for the life of the
incident. `domain.base.object_reference` exists for exactly this, so the simulator models the shape
the real integration must use rather than the shape that would be convenient at fixture size.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lpr_cpe.domain.base import object_reference
from lpr_cpe.integrations.base import AdapterUnavailableError
from lpr_cpe.simulation.fixtures.determinism import jitter, unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase


class SimulatedNXTAdapter(SimulatedAdapterBase):
    """Alarms, RF/PNM measurements and service-group views, from fixtures.

    Readings are seeded from the service reference (`determinism.jitter`), so the same service
    always measures the same and its tap-mates measure differently. That is what makes neighbour
    comparison testable: a 5-of-8 cluster has to be distinguishable from one outlier, and it cannot
    be if every read redraws.
    """

    system_name = "nxt"
    external_ref_prefix = "NXT"

    # No write methods, deliberately. The specification permits alarm acknowledge and clear "when
    # supported by verified integration specifications", and none was supplied, so `NXTAdapter` in
    # `integrations/base.py` declares none. An `acknowledge_alarm` here would be a guess at a
    # state-changing vendor call -- the one category of guess with a real blast radius. Gap NXT-5.

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_alarms(
        self, *, since: datetime, service_ref: str | None = None
    ) -> list[dict[str, Any]]:
        """Alarms raised after `since`, optionally for one service.

        A **collection query**: an unknown `service_ref` yields `[]` rather than raising. "No alarms
        for that service" and "that service has none in this window" are the same answer, and a
        quiet window must not read as an outage.

        Alarms are derived from the fixtures' health states rather than stored, so an alarm always
        agrees with the measurement that justifies it -- a stored alarm list drifts from the
        telemetry it is supposed to describe the first time a fixture is edited.
        """
        self._ensure_available()
        out: list[dict[str, Any]] = []
        candidates = (
            [self._fixtures.services[service_ref]]
            if service_ref in self._fixtures.services
            else ([] if service_ref is not None else list(self._fixtures.services.values()))
        )
        for service in candidates:
            for alarm in self._alarms_for(service):
                if datetime.fromisoformat(str(alarm["raised_at"])) >= since:
                    out.append(alarm)
        # Newest first, which is the order a triage view reads them in.
        out.sort(key=lambda a: str(a["raised_at"]), reverse=True)
        return out

    def _alarms_for(self, service: dict[str, Any]) -> list[dict[str, Any]]:
        health = str(service["health"])
        ref = str(service["service_ref"])
        if health in {"hfc_healthy", "pon_healthy"}:
            return []
        # Ages are offsets against the injected clock, so an alarm is "90 minutes old" in every run.
        specs: dict[str, tuple[str, str, str, float]] = {
            "hfc_degraded_upstream": (
                "US_SNR_DEGRADED",
                "high",
                "Upstream MER below threshold with rising uncorrectables",
                -1.5,
            ),
            "hfc_marginal": (
                "DS_POWER_LOW",
                "low",
                "Downstream receive power at lower operating limit",
                -6.0,
            ),
            "pon_degraded_optical": (
                "ONT_RX_LOW",
                "high",
                "ONT receive power 9 dB below ODP peers",
                -2.25,
            ),
            "pon_power_affected": (
                "ONT_DYING_GASP",
                "critical",
                "ONT reported dying gasp then went unreachable",
                -3.2,
            ),
        }
        alarm_type, severity, summary, age_hours = specs[health]
        return [
            {
                "alarm_id": f"NXT-ALM-{str(unit(ref, 'alarm'))[2:10]}",
                "alarm_type": alarm_type,
                "severity": severity,
                "summary": summary,
                "service_ref": ref,
                "network_element_ref": service["node_ref"] or service["pon_port_ref"],
                "delimiter_ref": service["delimiter_ref"],
                "technology": service["technology"],
                "raised_at": self._offset_hours(age_hours),
                "cleared_at": None,
                "acknowledged": False,
                "occurrences_24h": 1 + int(unit(ref, "occurrences") * 6),
                **self._provenance(ref),
            }
        ]

    async def fetch_rf_measurements(self, service_ref: str) -> dict[str, Any]:
        """DOCSIS RF for one service.

        **Subject read**: an unknown ref raises `AdapterUnavailableError`.

        Raises rather than returning empty for a PON service too. Asking NXT for RF on a fibre
        service is a caller bug -- the technology fork belongs upstream, in
        `SimulatedAdapters.plant_adapter_for` -- and silently returning nulls would let a detector
        conclude "no RF impairment" about a service that has no RF at all.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        if service["technology"] != "hfc":
            raise AdapterUnavailableError(
                self.system_name,
                f"{service_ref} is {service['technology']}, which has no DOCSIS RF measurements",
            )
        profile = self._fixtures.telemetry(service)
        return {
            "service_ref": service_ref,
            "cmts_ref": service["cmts_ref"],
            "service_group_ref": service["service_group_ref"],
            "downstream_power_dbmv": round(
                float(profile["downstream_power_dbmv"]) + jitter(service_ref, "ds_power", 0.8), 2
            ),
            "upstream_power_dbmv": round(
                float(profile["upstream_power_dbmv"]) + jitter(service_ref, "us_power", 1.1), 2
            ),
            "downstream_snr_db": round(
                float(profile["downstream_snr_db"]) + jitter(service_ref, "ds_snr", 0.6), 2
            ),
            "upstream_snr_db": round(
                float(profile["upstream_snr_db"]) + jitter(service_ref, "us_snr", 0.9), 2
            ),
            "uncorrectable_codewords": int(profile["uncorrectable_codewords"]),
            "corrected_codewords": int(profile["corrected_codewords"]),
            "t3_timeouts": int(profile["t3_timeouts"]),
            "t4_timeouts": int(profile["t4_timeouts"]),
            "flap_count_24h": int(profile["flap_count_24h"]),
            "rf_verdict": profile["rf_verdict"],
            "measured_window_hours": 1,
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_pnm_capture(self, service_ref: str) -> dict[str, Any]:
        """PNM sweep summary plus a pointer to the capture. **Subject read**: unknown ref raises.

        The spectrum itself is an `object_reference`, never inline -- see the module docstring.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        if service["technology"] != "hfc":
            raise AdapterUnavailableError(
                self.system_name, f"{service_ref} is not HFC; PNM does not apply"
            )
        impaired = str(service["health"]) == "hfc_degraded_upstream"
        return {
            "service_ref": service_ref,
            "capture_kind": "upstream_pre_equalisation",
            # Summary statistics only. A detector thresholds on these; nothing thresholds on bins.
            "group_delay_ns": round(18.0 + jitter(service_ref, "delay", 4.0), 1),
            "main_tap_energy_ratio_db": round((11.2 if impaired else 26.4), 1),
            "in_channel_ripple_db": round((6.8 if impaired else 1.1), 2),
            "suspected_impairment": "impedance_mismatch_resonance" if impaired else "none",
            "distance_to_fault_m": round(41.0 + jitter(service_ref, "dtf", 9.0), 1)
            if impaired
            else None,
            "confidence": 0.78 if impaired else 0.94,
            "capture_object": object_reference("pnm-captures", f"{service_ref}/latest.bin"),
            "bin_count": 4096,
            "data_available": True,
            "data_quality_notes": [
                "simulated summary; bin-level spectrum is a reference, not inline"
            ],
            **self._provenance(service_ref),
        }

    async def fetch_service_group_health(self, service_group_ref: str) -> dict[str, Any]:
        """Aggregate health for one CMTS service group. **Subject read**: unknown ref raises.

        The counts are computed from the member services' health states rather than stored, so the
        group view cannot disagree with the per-service views the detectors also read. Two sources
        for one fact is how a common-cause cluster gets confirmed by one and denied by the other.
        """
        self._ensure_available()
        node_ref = next(
            (
                ref
                for ref, node in self._fixtures.hfc_nodes.items()
                if node["service_group_ref"] == service_group_ref
            ),
            None,
        )
        if node_ref is None:
            raise AdapterUnavailableError(
                self.system_name, f"unknown service_group_ref {service_group_ref!r}"
            )
        members = [
            s
            for s in self._fixtures.services.values()
            if s["service_group_ref"] == service_group_ref
        ]
        degraded = [s for s in members if str(s["health"]) not in {"hfc_healthy", "pon_healthy"}]
        by_delimiter: dict[str, int] = {}
        for service in degraded:
            key = str(service["delimiter_ref"])
            by_delimiter[key] = by_delimiter.get(key, 0) + 1
        return {
            "service_group_ref": service_group_ref,
            "node_ref": node_ref,
            "cmts_ref": self._fixtures.hfc_nodes[node_ref]["cmts_ref"],
            "subscribers_total": len(members),
            "subscribers_degraded": len(degraded),
            "degraded_refs": tuple(str(s["service_ref"]) for s in degraded),
            # The number the common-cause detector needs: degraded homes grouped by delimiter.
            "degraded_by_delimiter": by_delimiter,
            "return_path_state": self._fixtures.hfc_nodes[node_ref]["return_path_state"],
            "utilisation_pct": round(52.0 + jitter(service_group_ref, "util", 14.0), 1),
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_group_ref),
        }
