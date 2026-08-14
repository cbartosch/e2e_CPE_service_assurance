"""Fixture-backed HFC plant view: node and amplifier chain, per-tap neighbour sets.

No CMTS, CCAP or plant-records API was supplied (A1/A2). `tap_value_db`, `return_path_state`,
`housing` and the tap-view shape are ours. Gaps HFC-1 to HFC-4.

`fetch_tap_view` is the reason this adapter exists separately from NXT. The common-cause detector
needs "how many of the homes behind this tap are degraded, and which", and that is a *plant*
question answered from plant records plus per-home telemetry -- not an alarm-system question.
Keeping it here means the blast-radius denominator has one owner.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.integrations.base import AdapterUnavailableError
from lpr_cpe.simulation.fixtures.determinism import jitter
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase


class SimulatedHFCAdapter(SimulatedAdapterBase):
    """Read-only. There is no HFC write in `HFCAdapter`, because a plant change is an MR in jTrack
    and a work order in WFM -- not a direct edit to plant records from an assurance workflow."""

    system_name = "hfc"
    external_ref_prefix = "HFC"

    async def fetch_topology(self, service_ref: str) -> dict[str, Any]:
        """The full chain from CPE to headend. **Subject read**: unknown ref raises.

        Returns `homes_behind_delimiter` and `homes_behind_node_or_port` as separate fields and
        never substitutes one for the other. `TopologyContext` treats a null denominator as unknown
        on purpose -- a guessed 8 produces a confident blast radius that no record supports -- so
        this adapter reports what the plant records say and nothing more.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        if service["technology"] != "hfc":
            raise AdapterUnavailableError(
                self.system_name,
                f"{service_ref} is {service['technology']}; ask the PON adapter for its topology",
            )
        tap = self._fixtures.taps[str(service["delimiter_ref"])]
        node = self._fixtures.hfc_nodes[str(service["node_ref"])]
        return {
            "service_ref": service_ref,
            "technology": "hfc",
            "delimiter_kind": "tap",
            "delimiter_ref": service["delimiter_ref"],
            "delimiter_port": service["delimiter_port"],
            "tap_value_db": tap["tap_value_db"],
            "node_ref": service["node_ref"],
            "amplifier_refs": list(service["amplifier_refs"]),
            "cmts_ref": service["cmts_ref"],
            "service_group_ref": service["service_group_ref"],
            "headend_ref": service["headend_ref"],
            "mdu_ref": service["mdu_ref"],
            "homes_behind_delimiter": service["homes_behind_delimiter"],
            "homes_behind_node_or_port": service["homes_behind_node_or_port"],
            "area_archetype": service["archetype"],
            "latitude": service["latitude"],
            "longitude": service["longitude"],
            "docsis_version": node["docsis_version"],
            "topology_source": f"{self.system_name}:plant_records(simulated)",
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_tap_view(self, tap_ref: str) -> dict[str, Any]:
        """Every home behind one tap, with its own reading. **Subject read**: unknown ref raises.

        `homes_passed` and `services_in_service` are both reported, and they differ. A tap passes 8
        homes and may have 4 subscribers; using the port count as the impact denominator overstates
        blast radius on a lightly built tap, and using the subscriber count understates the size of
        the physical fault. The caller picks, knowingly.
        """
        self._ensure_available()
        tap = self._fixtures.delimiter(tap_ref, system=self.system_name)
        if tap_ref not in self._fixtures.taps:
            raise AdapterUnavailableError(
                self.system_name, f"{tap_ref} is an ODP, not a tap; ask the PON adapter"
            )
        peers = self._fixtures.peers_behind_delimiter(tap_ref)
        homes: list[dict[str, Any]] = []
        for peer in peers:
            ref = str(peer["service_ref"])
            profile = self._fixtures.telemetry(peer)
            degraded = str(peer["health"]) != "hfc_healthy"
            homes.append(
                {
                    # No customer identifiers: this is a neighbour set, and a detector needs the
                    # readings, not who lives there. `DetectionContext.peers` is documented as
                    # already-masked summaries and this is where that starts being true.
                    "service_ref": ref,
                    "delimiter_port": peer["delimiter_port"],
                    "upstream_power_dbmv": round(
                        float(profile["upstream_power_dbmv"]) + jitter(ref, "us_power", 1.1), 2
                    ),
                    "downstream_snr_db": round(
                        float(profile["downstream_snr_db"]) + jitter(ref, "ds_snr", 0.6), 2
                    ),
                    "uncorrectable_codewords": int(profile["uncorrectable_codewords"]),
                    "degraded": degraded,
                    "rf_verdict": profile["rf_verdict"],
                }
            )
        degraded_count = sum(1 for h in homes if h["degraded"])
        return {
            "delimiter_ref": tap_ref,
            "delimiter_kind": "tap",
            "node_ref": tap["node_ref"],
            "housing": tap["housing"],
            "tap_value_db": tap["tap_value_db"],
            "last_audit_year": tap["last_audit_year"],
            "homes_passed": tap["ports"],
            "services_in_service": len(homes),
            "degraded_count": degraded_count,
            # The ratio the common-cause detector thresholds on, over services actually in service.
            "degraded_fraction": round(degraded_count / len(homes), 3) if homes else None,
            "homes": homes,
            "latitude": tap["latitude"],
            "longitude": tap["longitude"],
            "data_available": True,
            "data_quality_notes": [] if homes else ["no services in service behind this tap"],
            **self._provenance(tap_ref),
        }

    async def fetch_node_health(self, node_ref: str) -> dict[str, Any]:
        """Node and amplifier state. **Subject read**: unknown ref raises.

        `degraded_by_delimiter` is computed from the member services, the same way the NXT
        service-group view computes it, and both read the one fixture -- so the plant view and the
        alarm view cannot disagree about how many homes are affected.
        """
        self._ensure_available()
        node = self._fixtures.hfc_nodes.get(node_ref)
        if node is None:
            raise AdapterUnavailableError(self.system_name, f"unknown node_ref {node_ref!r}")
        members = [s for s in self._fixtures.services.values() if s["node_ref"] == node_ref]
        degraded = [s for s in members if str(s["health"]) != "hfc_healthy"]
        by_delimiter: dict[str, int] = {}
        for service in degraded:
            key = str(service["delimiter_ref"])
            by_delimiter[key] = by_delimiter.get(key, 0) + 1
        taps = {ref: t for ref, t in self._fixtures.taps.items() if t["node_ref"] == node_ref}
        return {
            "node_ref": node_ref,
            "cmts_ref": node["cmts_ref"],
            "service_group_ref": node["service_group_ref"],
            "headend_ref": node["headend_ref"],
            "amplifiers": [
                {
                    "amplifier_ref": ref,
                    "output_level_dbmv": round(46.0 + jitter(str(ref), "amp_out", 1.4), 2),
                    "return_gain_db": round(18.0 + jitter(str(ref), "amp_ret", 1.0), 2),
                    "powered": True,
                }
                for ref in node["amplifier_refs"]
            ],
            "taps": sorted(taps),
            "homes_passed": sum(int(t["ports"]) for t in taps.values()),
            "services_in_service": len(members),
            "degraded_count": len(degraded),
            "degraded_by_delimiter": by_delimiter,
            "return_path_state": node["return_path_state"],
            "commissioned_year": node["commissioned_year"],
            "area_archetype": node["archetype"],
            "latitude": node["latitude"],
            "longitude": node["longitude"],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(node_ref),
        }
