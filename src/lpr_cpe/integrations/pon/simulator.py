"""Fixture-backed PON plant view: OLT, PON port, splitter, ODP, and optical levels.

No OLT northbound, OMCI or fibre-records API was supplied (A1/A2). `optical_verdict`,
`omci_state`, `secondary_split` and the ODP-view shape are ours. Gaps PON-1 to PON-5.

The optical numbers carry the distinction the drop-versus-plant decision turns on: a single ONT
reading 9 dB below its ODP-mates is a drop or connector fault (one crew, one visit, Clean Boots),
while all sixteen reading low together is a feeder or splitter fault (Dirty Boots and an MR). Those
are different verdicts from the same measurement, and only the peer set separates them -- which is
why `fetch_odp_view` returns every position's reading rather than an average.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.integrations.base import AdapterUnavailableError
from lpr_cpe.simulation.fixtures.determinism import jitter
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase


class SimulatedPONAdapter(SimulatedAdapterBase):
    """Read-only, for the same reason as the HFC adapter: plant changes go via jTrack and WFM."""

    system_name = "pon"
    external_ref_prefix = "PON"

    async def fetch_topology(self, service_ref: str) -> dict[str, Any]:
        """ONT to CO chain. **Subject read**: unknown ref raises.

        `delimiter_kind` is always `odp` here and never `tap`. `TopologyContext` rejects a PON
        service behind a TAP, so an adapter that guessed would produce a validation error at the
        boundary instead of a wrong crew at a wrong cabinet -- but only because that validator
        exists. This adapter does not rely on it: it reads the kind from the plant record.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        if service["technology"] != "pon":
            raise AdapterUnavailableError(
                self.system_name,
                f"{service_ref} is {service['technology']}; ask the HFC adapter for its topology",
            )
        odp = self._fixtures.odps[str(service["odp_ref"])]
        olt = self._fixtures.olts[str(service["olt_ref"])]
        return {
            "service_ref": service_ref,
            "technology": "pon",
            "delimiter_kind": "odp",
            "delimiter_ref": service["delimiter_ref"],
            "delimiter_port": service["delimiter_port"],
            "odp_ref": service["odp_ref"],
            "secondary_split": odp["secondary_split"],
            "primary_splitter_ref": service["primary_splitter_ref"],
            "pon_port_ref": service["pon_port_ref"],
            "olt_ref": service["olt_ref"],
            "headend_ref": service["headend_ref"],
            "split_ratio": service["split_ratio"],
            "feeder_km": olt["feeder_km"],
            "pon_standard": olt["pon_standard"],
            "mdu_ref": service["mdu_ref"],
            "homes_behind_delimiter": service["homes_behind_delimiter"],
            "homes_behind_node_or_port": service["homes_behind_node_or_port"],
            "area_archetype": service["archetype"],
            "latitude": service["latitude"],
            "longitude": service["longitude"],
            "topology_source": f"{self.system_name}:fibre_records(simulated)",
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_optical_levels(self, service_ref: str) -> dict[str, Any]:
        """Per-ONT optical power and OMCI state. **Subject read**: unknown ref raises.

        A powered-off ONT returns `None` for every optical value with `data_available: False` and a
        note, rather than 0.0 or -40.0. A sentinel number would be thresholded against and would
        read as the worst optical fault in the network; the honest answer is that there is no
        measurement, and the power-correlation detector is the thing that should explain why.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        if service["technology"] != "pon":
            raise AdapterUnavailableError(
                self.system_name, f"{service_ref} is not PON; it has no optical levels"
            )
        profile = self._fixtures.telemetry(service)
        powered = profile["rx_optical_power_dbm"] is not None
        if not powered:
            return {
                "service_ref": service_ref,
                "olt_ref": service["olt_ref"],
                "pon_port_ref": service["pon_port_ref"],
                "odp_ref": service["odp_ref"],
                "rx_optical_power_dbm": None,
                "tx_optical_power_dbm": None,
                "olt_rx_from_ont_dbm": None,
                "ont_operational_state": profile["ont_operational_state"],
                "omci_state": profile["omci_state"],
                "ber_estimate": None,
                "los_events_24h": int(profile["los_events_24h"]),
                "dying_gasp_events_24h": int(profile["dying_gasp_events_24h"]),
                "optical_verdict": profile["optical_verdict"],
                "data_available": False,
                "data_quality_notes": [
                    "ONT unreachable: no optical measurement exists for this window",
                    "dying gasp received; check utility power before treating as a fibre fault",
                ],
                **self._provenance(service_ref),
            }
        return {
            "service_ref": service_ref,
            "olt_ref": service["olt_ref"],
            "pon_port_ref": service["pon_port_ref"],
            "odp_ref": service["odp_ref"],
            "rx_optical_power_dbm": round(
                float(profile["rx_optical_power_dbm"]) + jitter(service_ref, "rx", 0.7), 2
            ),
            "tx_optical_power_dbm": round(
                float(profile["tx_optical_power_dbm"]) + jitter(service_ref, "tx", 0.4), 2
            ),
            "olt_rx_from_ont_dbm": round(
                float(profile["olt_rx_from_ont_dbm"]) + jitter(service_ref, "oltrx", 0.5), 2
            ),
            "ont_operational_state": profile["ont_operational_state"],
            "omci_state": profile["omci_state"],
            "ber_estimate": profile["ber_estimate"],
            "los_events_24h": int(profile["los_events_24h"]),
            "dying_gasp_events_24h": int(profile["dying_gasp_events_24h"]),
            "optical_verdict": profile["optical_verdict"],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_pon_port_health(self, pon_port_ref: str) -> dict[str, Any]:
        """Aggregate state for one PON port. **Subject read**: unknown ref raises."""
        self._ensure_available()
        olt_ref = next(
            (
                ref
                for ref, olt in self._fixtures.olts.items()
                if olt["pon_port_ref"] == pon_port_ref
            ),
            None,
        )
        if olt_ref is None:
            raise AdapterUnavailableError(
                self.system_name, f"unknown pon_port_ref {pon_port_ref!r}"
            )
        olt = self._fixtures.olts[olt_ref]
        members = [s for s in self._fixtures.services.values() if s["pon_port_ref"] == pon_port_ref]
        degraded = [s for s in members if str(s["health"]) != "pon_healthy"]
        odps = {ref: o for ref, o in self._fixtures.odps.items() if o["olt_ref"] == olt_ref}
        by_delimiter: dict[str, int] = {}
        for service in degraded:
            key = str(service["delimiter_ref"])
            by_delimiter[key] = by_delimiter.get(key, 0) + 1
        return {
            "pon_port_ref": pon_port_ref,
            "olt_ref": olt_ref,
            "primary_splitter_ref": olt["primary_splitter_ref"],
            "headend_ref": olt["headend_ref"],
            "split_ratio": olt["split_ratio"],
            "pon_standard": olt["pon_standard"],
            "feeder_km": olt["feeder_km"],
            "odps": sorted(odps),
            "homes_passed": sum(int(o["ports"]) for o in odps.values()),
            "services_in_service": len(members),
            "degraded_count": len(degraded),
            "degraded_by_delimiter": by_delimiter,
            # A port-level transmit level that is nominal while one ONT reads low is the evidence
            # that separates a drop fault from a feeder fault. It is reported even when nothing is
            # wrong, because "the port is fine" is the load-bearing half of that comparison.
            "port_tx_power_dbm": round(3.2 + jitter(pon_port_ref, "porttx", 0.3), 2),
            "port_state": "up",
            "area_archetype": olt["archetype"],
            "latitude": olt["latitude"],
            "longitude": olt["longitude"],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(pon_port_ref),
        }

    async def fetch_odp_view(self, odp_ref: str) -> dict[str, Any]:
        """Every position on one ODP, with its own optical reading. **Subject read**: raises.

        The PON counterpart to `fetch_tap_view`, and the same `homes_passed` versus
        `services_in_service` distinction applies: a 16-port ODP with 8 subscribers has two
        different denominators and the caller has to choose one on purpose.
        """
        self._ensure_available()
        odp = self._fixtures.delimiter(odp_ref, system=self.system_name)
        if odp_ref not in self._fixtures.odps:
            raise AdapterUnavailableError(
                self.system_name, f"{odp_ref} is a tap, not an ODP; ask the HFC adapter"
            )
        peers = self._fixtures.peers_behind_delimiter(odp_ref)
        positions: list[dict[str, Any]] = []
        for peer in peers:
            ref = str(peer["service_ref"])
            profile = self._fixtures.telemetry(peer)
            rx = profile["rx_optical_power_dbm"]
            positions.append(
                {
                    # Readings only, no customer identifiers -- see the HFC tap view.
                    "service_ref": ref,
                    "delimiter_port": peer["delimiter_port"],
                    "rx_optical_power_dbm": (
                        round(float(rx) + jitter(ref, "rx", 0.7), 2) if rx is not None else None
                    ),
                    "ont_operational_state": profile["ont_operational_state"],
                    "omci_state": profile["omci_state"],
                    "degraded": str(peer["health"]) != "pon_healthy",
                    "optical_verdict": profile["optical_verdict"],
                }
            )
        measured = [
            float(p["rx_optical_power_dbm"])
            for p in positions
            if p["rx_optical_power_dbm"] is not None
        ]
        degraded_count = sum(1 for p in positions if p["degraded"])
        unmeasured = len(positions) - len(measured)
        return {
            "delimiter_ref": odp_ref,
            "delimiter_kind": "odp",
            "olt_ref": odp["olt_ref"],
            "housing": odp["housing"],
            "secondary_split": odp["secondary_split"],
            "last_audit_year": odp["last_audit_year"],
            "homes_passed": odp["ports"],
            "services_in_service": len(positions),
            "degraded_count": degraded_count,
            "degraded_fraction": round(degraded_count / len(positions), 3) if positions else None,
            # Median, not mean: one ONT at -28.6 dBm drags a mean of eight far enough to make the
            # outlier look less like an outlier, which is the opposite of what the comparison is
            # for.
            "median_rx_optical_power_dbm": (
                round(sorted(measured)[len(measured) // 2], 2) if measured else None
            ),
            "worst_rx_optical_power_dbm": round(min(measured), 2) if measured else None,
            "positions": positions,
            "latitude": odp["latitude"],
            "longitude": odp["longitude"],
            "data_available": bool(measured),
            "data_quality_notes": (
                [f"{unmeasured} of {len(positions)} positions have no optical reading"]
                if unmeasured
                else []
            ),
            **self._provenance(odp_ref),
        }
