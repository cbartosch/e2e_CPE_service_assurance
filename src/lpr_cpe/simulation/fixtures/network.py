"""A small, internally consistent synthetic Puerto Rico access network.

Two HFC nodes, two PON OLTs, eight delimiters (four taps of 8 and four ODPs of 16), 41 services
spread across the four area archetypes, one CPE per service, and nine crews.

**Every field name here is our invention.** No LPR, CommScope or jTrack schema was supplied
(IMPLEMENTATION_PLAN.md A1/A2), so nothing in this file should be read as a confirmed payload shape;
`docs/vendor-integration-gaps.md` records what was guessed and what a real integration would have to
answer.

Three authoring rules, each of which exists because breaking it produced a bug once:

1. **Nesting counts are computed, not typed.** `homes_behind_delimiter` and
   `homes_behind_node_or_port` come from the structure below (a tap's port count; the sum of the
   ports on a node's taps), because `TopologyContext` validates that the first does not exceed the
   second and hand-written numbers drift.
2. **Times are stored as offsets, never as instants.** A stale CPE is `last_inform_offset_hours =
   -96`, not a literal 2026-08-10 timestamp. The simulators resolve offsets against the injected
   `Clock`, so the same fixture is "four days stale" under a `FrozenClock` in a test and under a
   `SystemClock` in the demo. A literal timestamp would silently become three years stale.
3. **Homes passed is not services in service.** A tap passes 8 homes and may have 4 subscribers.
   The blast-radius denominator is homes passed (`ports`); the occupancy is the number of services
   generated against it. Conflating them overstates blast radius on lightly built taps.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.simulation.fixtures.determinism import unit

# ---------------------------------------------------------------------------------------------
# Areas. One per archetype, which is why the archetype is the key: the GIS simulator is handed an
# archetype string by `travel_minutes()` and has to find the travel model without a second lookup.
# ---------------------------------------------------------------------------------------------
AREAS: dict[str, dict[str, Any]] = {
    "metro_mdu": {
        "area_ref": "AREA-SJ-SANTURCE",
        "name": "San Juan - Santurce",
        "archetype": "metro_mdu",
        "latitude": 18.4500,
        "longitude": -66.0700,
        # Travel model: dense grid, short distances, but parking and building access dominate, so
        # the fixed overhead is the larger term. A per-km rate alone would predict 4-minute visits.
        "travel_minutes_per_km": 3.4,
        "fixed_overhead_minutes": 22.0,
        "ferry_required": False,
        "access_constraints": ["building_concierge_hours", "roof_access_permit", "no_van_parking"],
        "municipality": "San Juan",
    },
    "coastal_city_suburb": {
        "area_ref": "AREA-PO-COASTAL",
        "name": "Ponce - coastal suburb",
        "archetype": "coastal_city_suburb",
        "latitude": 18.0110,
        "longitude": -66.6140,
        "travel_minutes_per_km": 2.1,
        "fixed_overhead_minutes": 12.0,
        "ferry_required": False,
        "access_constraints": ["salt_air_corrosion", "afternoon_squalls"],
        "municipality": "Ponce",
    },
    "central_mountain_rural": {
        "area_ref": "AREA-UT-CORDILLERA",
        "name": "Utuado - central cordillera",
        "archetype": "central_mountain_rural",
        "latitude": 18.2680,
        "longitude": -66.7000,
        # Mountain roads: a 12 km job is a 45-minute drive, and that is the whole reason the
        # dispatch optimizer weights archetype rather than straight-line distance.
        "travel_minutes_per_km": 4.8,
        "fixed_overhead_minutes": 18.0,
        "ferry_required": False,
        "access_constraints": ["unpaved_access_road", "landslide_risk", "no_cell_coverage"],
        "municipality": "Utuado",
    },
    "remote_island": {
        "area_ref": "AREA-VQ-ISABEL",
        "name": "Vieques - Isabel Segunda",
        "archetype": "remote_island",
        "latitude": 18.1500,
        "longitude": -65.4400,
        "travel_minutes_per_km": 3.0,
        # Island-side overhead only: parking, the yard, finding the site. It is deliberately in
        # line with the other archetypes, because the crossing is NOT counted here. The ferry has
        # one owner -- `gis.simulator._FERRY_MINUTES`, added when `ferry_required` -- and folding
        # it in here as well made a zero-kilometre trip on Vieques cost 260 minutes.
        "fixed_overhead_minutes": 20.0,
        # A same-day second visit to Vieques does not exist, which is why joint dispatch matters
        # most here; that follows from the crossing, not from this number.
        "ferry_required": True,
        "ferry_windows_local": ["06:30", "11:00", "16:30"],
        "access_constraints": ["ferry_dependent", "limited_van_stock", "generator_dependent_sites"],
        "municipality": "Vieques",
    },
}

# ---------------------------------------------------------------------------------------------
# HFC plant
# ---------------------------------------------------------------------------------------------
HFC_NODES: dict[str, dict[str, Any]] = {
    "HFC-NODE-SJ-011": {
        "node_ref": "HFC-NODE-SJ-011",
        "archetype": "metro_mdu",
        "headend_ref": "HE-SANJUAN",
        "cmts_ref": "CCAP-SJ-01",
        "service_group_ref": "SG-SJ-011-1",
        "amplifier_refs": ["AMP-SJ-011-1", "AMP-SJ-011-2"],
        "latitude": 18.4512,
        "longitude": -66.0688,
        "docsis_version": "3.1",
        "return_path_state": "impaired",
        "commissioned_year": 2016,
    },
    "HFC-NODE-PO-042": {
        "node_ref": "HFC-NODE-PO-042",
        "archetype": "coastal_city_suburb",
        "headend_ref": "HE-PONCE",
        "cmts_ref": "CCAP-PO-02",
        "service_group_ref": "SG-PO-042-1",
        "amplifier_refs": ["AMP-PO-042-1", "AMP-PO-042-2"],
        "latitude": 18.0126,
        "longitude": -66.6119,
        "docsis_version": "3.1",
        "return_path_state": "nominal",
        "commissioned_year": 2019,
    },
}

TAPS: dict[str, dict[str, Any]] = {
    "TAP-SJ-011-A": {
        "delimiter_ref": "TAP-SJ-011-A",
        "node_ref": "HFC-NODE-SJ-011",
        "ports": 8,
        "tap_value_db": 14,
        "latitude": 18.4515,
        "longitude": -66.0691,
        "mdu_ref": "MDU-SJ-CONDADO-14",
        "housing": "mdu_riser",
        "last_audit_year": 2021,
    },
    "TAP-SJ-011-B": {
        "delimiter_ref": "TAP-SJ-011-B",
        "node_ref": "HFC-NODE-SJ-011",
        "ports": 8,
        "tap_value_db": 11,
        "latitude": 18.4498,
        "longitude": -66.0712,
        "mdu_ref": "MDU-SJ-CONDADO-22",
        "housing": "mdu_riser",
        "last_audit_year": 2023,
    },
    "TAP-PO-042-A": {
        "delimiter_ref": "TAP-PO-042-A",
        "node_ref": "HFC-NODE-PO-042",
        "ports": 8,
        "tap_value_db": 17,
        "latitude": 18.0131,
        "longitude": -66.6108,
        "mdu_ref": None,
        "housing": "aerial_strand",
        "last_audit_year": 2024,
    },
    "TAP-PO-042-B": {
        "delimiter_ref": "TAP-PO-042-B",
        "node_ref": "HFC-NODE-PO-042",
        "ports": 8,
        "tap_value_db": 20,
        "latitude": 18.0104,
        "longitude": -66.6152,
        "mdu_ref": None,
        "housing": "pedestal",
        "last_audit_year": 2022,
    },
}

# ---------------------------------------------------------------------------------------------
# PON plant
# ---------------------------------------------------------------------------------------------
OLTS: dict[str, dict[str, Any]] = {
    "OLT-UT-001": {
        "olt_ref": "OLT-UT-001",
        "archetype": "central_mountain_rural",
        "headend_ref": "CO-UTUADO",
        "pon_port_ref": "PON-UT-001-1-1-1",
        "primary_splitter_ref": "SPL-UT-001-P1",
        "split_ratio": 32,
        "latitude": 18.2691,
        "longitude": -66.7014,
        "pon_standard": "xgs-pon",
        "feeder_km": 6.4,
        "commissioned_year": 2022,
    },
    "OLT-VQ-002": {
        "olt_ref": "OLT-VQ-002",
        "archetype": "remote_island",
        "headend_ref": "CO-VIEQUES",
        "pon_port_ref": "PON-VQ-002-1-1-1",
        "primary_splitter_ref": "SPL-VQ-002-P1",
        "split_ratio": 32,
        "latitude": 18.1489,
        "longitude": -65.4412,
        "pon_standard": "gpon",
        "feeder_km": 3.1,
        "commissioned_year": 2023,
    },
}

ODPS: dict[str, dict[str, Any]] = {
    "ODP-UT-001-A": {
        "delimiter_ref": "ODP-UT-001-A",
        "olt_ref": "OLT-UT-001",
        "ports": 16,
        "latitude": 18.2702,
        "longitude": -66.7031,
        "housing": "aerial_closure",
        "secondary_split": "1:8",
        "last_audit_year": 2023,
    },
    "ODP-UT-001-B": {
        "delimiter_ref": "ODP-UT-001-B",
        "olt_ref": "OLT-UT-001",
        "ports": 16,
        "latitude": 18.2664,
        "longitude": -66.6978,
        "housing": "pedestal_closure",
        "secondary_split": "1:8",
        "last_audit_year": 2024,
    },
    "ODP-VQ-002-A": {
        "delimiter_ref": "ODP-VQ-002-A",
        "olt_ref": "OLT-VQ-002",
        "ports": 16,
        "latitude": 18.1511,
        "longitude": -65.4389,
        "housing": "aerial_closure",
        "secondary_split": "1:8",
        "last_audit_year": 2023,
    },
    "ODP-VQ-002-B": {
        "delimiter_ref": "ODP-VQ-002-B",
        "olt_ref": "OLT-VQ-002",
        "ports": 16,
        "latitude": 18.1472,
        "longitude": -65.4437,
        "housing": "wall_closure",
        "secondary_split": "1:8",
        "last_audit_year": 2021,
    },
}

# ---------------------------------------------------------------------------------------------
# Telemetry profiles. A service's `health` selects one of these; the simulators spread the nominal
# values across homes with seeded jitter (see `determinism.jitter`) so neighbour comparison has
# something to compare while any one home's reading stays reproducible.
#
# The numbers are conventional DOCSIS/GPON operating values, not measurements from LPR plant:
# upstream 45 dBmV nominal with >51 dBmV concerning, downstream MER 38 dB good and <30 dB poor,
# GPON ONT receive -19 dBm nominal with -28 dBm at the edge of class B+ sensitivity.
# ---------------------------------------------------------------------------------------------
TELEMETRY_PROFILES: dict[str, dict[str, Any]] = {
    "hfc_healthy": {
        "downstream_power_dbmv": 0.6,
        "upstream_power_dbmv": 44.5,
        "downstream_snr_db": 38.4,
        "upstream_snr_db": 36.9,
        "uncorrectable_codewords": 0,
        "corrected_codewords": 18,
        "t3_timeouts": 0,
        "t4_timeouts": 0,
        "flap_count_24h": 0,
        "rf_verdict": "nominal",
    },
    "hfc_degraded_upstream": {
        # The common-cause signature: upstream drive pushed to the top of range, return MER down,
        # uncorrectables climbing. Shared by 5 of the 8 homes on TAP-SJ-011-A.
        "downstream_power_dbmv": -1.8,
        "upstream_power_dbmv": 53.6,
        "downstream_snr_db": 31.2,
        "upstream_snr_db": 24.8,
        "uncorrectable_codewords": 4180,
        "corrected_codewords": 61240,
        "t3_timeouts": 9,
        "t4_timeouts": 1,
        "flap_count_24h": 6,
        "rf_verdict": "upstream_impairment",
    },
    "hfc_marginal": {
        "downstream_power_dbmv": -6.4,
        "upstream_power_dbmv": 48.9,
        "downstream_snr_db": 34.1,
        "upstream_snr_db": 32.0,
        "uncorrectable_codewords": 140,
        "corrected_codewords": 2210,
        "t3_timeouts": 1,
        "t4_timeouts": 0,
        "flap_count_24h": 1,
        "rf_verdict": "marginal_downstream",
    },
    "pon_healthy": {
        "rx_optical_power_dbm": -19.2,
        "tx_optical_power_dbm": 2.1,
        "olt_rx_from_ont_dbm": -21.4,
        "ont_operational_state": "operational",
        "omci_state": "in_service",
        "ber_estimate": 1e-11,
        "los_events_24h": 0,
        "dying_gasp_events_24h": 0,
        "optical_verdict": "nominal",
    },
    "pon_degraded_optical": {
        # One ONT far below its ODP-mates: a drop/connector fault, not a feeder or splitter fault.
        # The whole point of the fixture is that its 15 ODP-mates read nominal.
        "rx_optical_power_dbm": -28.6,
        "tx_optical_power_dbm": 3.4,
        "olt_rx_from_ont_dbm": -29.9,
        "ont_operational_state": "operational",
        "omci_state": "in_service",
        "ber_estimate": 4.2e-6,
        "los_events_24h": 3,
        "dying_gasp_events_24h": 0,
        "optical_verdict": "high_attenuation_drop",
    },
    "pon_power_affected": {
        # Utility power gone: the ONT sent a dying gasp and stopped. Reading this as an optical
        # fault is the mistake the power-correlation detector exists to prevent.
        "rx_optical_power_dbm": None,
        "tx_optical_power_dbm": None,
        "olt_rx_from_ont_dbm": None,
        "ont_operational_state": "power_off",
        "omci_state": "not_present",
        "ber_estimate": None,
        "los_events_24h": 1,
        "dying_gasp_events_24h": 1,
        "optical_verdict": "no_signal_dying_gasp",
    },
}

# ---------------------------------------------------------------------------------------------
# Wi-Fi profiles for the predictive scan. `client_count`, `worst_rssi_dbm` and the channel options
# are what the CPE simulator turns into a TR-181 tree.
# ---------------------------------------------------------------------------------------------
WIFI_PROFILES: dict[str, dict[str, Any]] = {
    "clean": {
        "client_count_2g": 2,
        "client_count_5g": 4,
        "utilization_2g_pct": 21.0,
        "utilization_5g_pct": 17.0,
        "noise_floor_2g_dbm": -94.0,
        "noise_floor_5g_dbm": -96.0,
        "error_rate_pct": 0.2,
        "worst_rssi_dbm": -63.0,
        "best_rssi_dbm": -41.0,
        "throughput_mbps": 412.0,
        "channels_2g": (1, 6, 11),
        "channels_5g": (36, 44, 149, 157),
    },
    "congested_2g": {
        # Metro MDU: 2.4 GHz saturated by neighbouring APs. A Wi-Fi problem that no plant repair
        # fixes, which is why the detector has to be able to reach this verdict.
        "client_count_2g": 9,
        "client_count_5g": 3,
        "utilization_2g_pct": 88.0,
        "utilization_5g_pct": 34.0,
        "noise_floor_2g_dbm": -78.0,
        "noise_floor_5g_dbm": -93.0,
        "error_rate_pct": 6.8,
        "worst_rssi_dbm": -76.0,
        "best_rssi_dbm": -48.0,
        "throughput_mbps": 96.0,
        "channels_2g": (1, 6, 11),
        "channels_5g": (36, 40),
    },
    "weak_coverage": {
        "client_count_2g": 4,
        "client_count_5g": 1,
        "utilization_2g_pct": 44.0,
        "utilization_5g_pct": 12.0,
        "noise_floor_2g_dbm": -90.0,
        "noise_floor_5g_dbm": -95.0,
        "error_rate_pct": 3.1,
        "worst_rssi_dbm": -84.0,
        "best_rssi_dbm": -57.0,
        "throughput_mbps": 58.0,
        "channels_2g": (6,),
        "channels_5g": (149,),
    },
    "no_radio_data": {
        # The device answers but the Wi-Fi subtree is empty -- a firmware build that does not
        # populate `Device.WiFi.AccessPoint.*.AssociatedDevice.*`. Real, and it must surface as a
        # data-quality note rather than as "0 clients, healthy".
        "client_count_2g": 0,
        "client_count_5g": 0,
        "utilization_2g_pct": None,
        "utilization_5g_pct": None,
        "noise_floor_2g_dbm": None,
        "noise_floor_5g_dbm": None,
        "error_rate_pct": None,
        "worst_rssi_dbm": None,
        "best_rssi_dbm": None,
        "throughput_mbps": None,
        "channels_2g": (11,),
        "channels_5g": (44,),
    },
}


# ---------------------------------------------------------------------------------------------
# Service and CPE generation
# ---------------------------------------------------------------------------------------------
# (delimiter_ref, service count, {port: health}, {port: wifi_profile}) -- occupancy is deliberately
# below the port count on most delimiters, per authoring rule 3.
_SERVICE_PLAN: tuple[tuple[str, int, dict[int, str], dict[int, str]], ...] = (
    (
        "TAP-SJ-011-A",
        8,
        # The common-cause cluster: 5 of 8 homes behind one tap share an upstream impairment.
        {
            1: "hfc_degraded_upstream",
            2: "hfc_degraded_upstream",
            3: "hfc_degraded_upstream",
            5: "hfc_degraded_upstream",
            7: "hfc_degraded_upstream",
        },
        {2: "congested_2g", 4: "congested_2g"},
    ),
    ("TAP-SJ-011-B", 4, {}, {1: "congested_2g", 3: "no_radio_data"}),
    ("TAP-PO-042-A", 6, {4: "hfc_marginal"}, {2: "weak_coverage"}),
    ("TAP-PO-042-B", 3, {}, {}),
    # The drop fault: one ONT of 16 ODP positions reads 9 dB below its neighbours.
    ("ODP-UT-001-A", 8, {3: "pon_degraded_optical"}, {5: "weak_coverage"}),
    ("ODP-UT-001-B", 4, {}, {1: "weak_coverage"}),
    # Vieques sits inside the power outage below, so position 1 is a dying-gasp ONT.
    ("ODP-VQ-002-A", 5, {1: "pon_power_affected"}, {}),
    ("ODP-VQ-002-B", 3, {}, {3: "congested_2g"}),
)

# Deliberate data-quality cases, keyed by service_ref. Written out rather than derived so a reader
# can see exactly which services the detectors' stale/offline paths are exercised by.
_CPE_OVERRIDES: dict[str, dict[str, Any]] = {
    # STALE: answered four days ago and has not informed since. Any Wi-Fi verdict computed from it
    # is a verdict about four-day-old conditions.
    "SVC-VQ-002-A-02": {
        "last_inform_offset_hours": -96.0,
        "online": True,
        "stale": True,
        "data_quality_notes": [
            "last inform 96h before read; TR-181 values are the last cached values, not current"
        ],
    },
    # OFFLINE: no contact at all. Distinct from stale -- there is nothing cached to reason about.
    "SVC-UT-001-B-01": {
        "last_inform_offset_hours": -31.0,
        "online": False,
        "offline": True,
        "data_quality_notes": [
            "device offline at read time; no current TR-181 tree available",
            "uptime and Wi-Fi counters withheld rather than reported as zero",
        ],
    },
    # The dying-gasp ONT in the outage area is also unreachable, for the same physical reason.
    "SVC-VQ-002-A-01": {
        "last_inform_offset_hours": -3.2,
        "online": False,
        "offline": True,
        "data_quality_notes": ["device offline; utility power outage open in this area"],
    },
}

#: (product tier, downstream speed in Mbps). The product *name* is composed from the tier and the
#: technology at build time, so an HFC service never ends up labelled "Fibre".
_TIERS: tuple[tuple[str, int], ...] = (
    ("residential", 500),
    ("residential", 300),
    ("business", 1000),
    ("residential", 100),
)


def _archetype_for(delimiter_ref: str) -> str:
    if delimiter_ref in TAPS:
        node = HFC_NODES[str(TAPS[delimiter_ref]["node_ref"])]
        return str(node["archetype"])
    olt = OLTS[str(ODPS[delimiter_ref]["olt_ref"])]
    return str(olt["archetype"])


def _homes_behind_node_or_port(parent_ref: str) -> int:
    """Sum of the ports on every delimiter hanging off this node or PON port.

    Computed, per authoring rule 1: `TopologyContext` rejects a delimiter that serves more homes
    than its parent, and this is the only place the parent's number is produced.
    """
    total = 0
    for tap in TAPS.values():
        if tap["node_ref"] == parent_ref:
            total += int(tap["ports"])
    for odp in ODPS.values():
        if odp["olt_ref"] == parent_ref:
            total += int(odp["ports"])
    return total


def _build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    services: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    for delimiter_ref, occupancy, health_by_port, wifi_by_port in _SERVICE_PLAN:
        is_hfc = delimiter_ref in TAPS
        delimiter = TAPS[delimiter_ref] if is_hfc else ODPS[delimiter_ref]
        archetype = _archetype_for(delimiter_ref)
        area = AREAS[archetype]
        ports = int(delimiter["ports"])
        # Exactly one of these is bound; the per-service update below branches on which, so neither
        # name is ever possibly-undefined and neither needs a cast.
        parent_ref = str(delimiter["node_ref"] if is_hfc else delimiter["olt_ref"])
        node = HFC_NODES[parent_ref] if is_hfc else None
        olt = None if is_hfc else OLTS[parent_ref]
        suffix = delimiter_ref.split("-", 1)[1]  # "SJ-011-A" / "UT-001-A"

        for port in range(1, occupancy + 1):
            service_ref = f"SVC-{suffix}-{port:02d}"
            cpe_ref = f"CPE-{suffix}-{port:02d}"
            default_health = "hfc_healthy" if is_hfc else "pon_healthy"
            health = health_by_port.get(port, default_health)
            tier, speed_mbps = _TIERS[(port - 1) % len(_TIERS)]
            product = f"{tier.title()} {'Cable' if is_hfc else 'Fibre'} {speed_mbps}"
            # Position offsets keep every home at its own coordinates without a second table; ~11 m
            # per 0.0001 degree, so a tap's homes land within a block of it.
            latitude = round(float(delimiter["latitude"]) + 0.00018 * port, 6)
            longitude = round(float(delimiter["longitude"]) - 0.00015 * port, 6)

            service: dict[str, Any] = {
                "service_ref": service_ref,
                "customer_ref": f"CUS-{suffix}-{port:02d}",
                "cpe_ref": cpe_ref,
                "technology": "hfc" if is_hfc else "pon",
                "archetype": archetype,
                "area_ref": area["area_ref"],
                "delimiter_kind": "tap" if is_hfc else "odp",
                "delimiter_ref": delimiter_ref,
                "delimiter_port": port,
                "homes_behind_delimiter": ports,
                "homes_behind_node_or_port": _homes_behind_node_or_port(parent_ref),
                "latitude": latitude,
                "longitude": longitude,
                "health": health,
                "product_tier": tier,
                "product_name": product,
                "downstream_speed_mbps": speed_mbps,
                # Two protected customers, both on the degraded tap, because that is the case where
                # the vulnerable-customer policy has to actually change the outcome.
                "vulnerable_customer": service_ref in {"SVC-SJ-011-A-03", "SVC-UT-001-A-03"},
                "priority_customer": tier == "business",
                "language": "es" if port % 3 else "en",
                "activated_days_ago": 40 + port * 11,
                "mdu_ref": delimiter.get("mdu_ref"),
            }
            if node is not None:
                service.update(
                    {
                        "node_ref": parent_ref,
                        "amplifier_refs": list(node["amplifier_refs"]),
                        "cmts_ref": node["cmts_ref"],
                        "service_group_ref": node["service_group_ref"],
                        "headend_ref": node["headend_ref"],
                        "olt_ref": None,
                        "pon_port_ref": None,
                        "primary_splitter_ref": None,
                        "odp_ref": None,
                        "split_ratio": None,
                    }
                )
            elif olt is not None:
                service.update(
                    {
                        "node_ref": None,
                        "amplifier_refs": [],
                        "cmts_ref": None,
                        "service_group_ref": None,
                        "headend_ref": olt["headend_ref"],
                        "olt_ref": parent_ref,
                        "pon_port_ref": olt["pon_port_ref"],
                        "primary_splitter_ref": olt["primary_splitter_ref"],
                        "odp_ref": delimiter_ref,
                        "split_ratio": olt["split_ratio"],
                    }
                )
            services.append(service)

            override = _CPE_OVERRIDES.get(service_ref, {})
            wifi_profile = wifi_by_port.get(port, "clean")
            device: dict[str, Any] = {
                "cpe_ref": cpe_ref,
                "service_ref": service_ref,
                "customer_ref": service["customer_ref"],
                # `hash()` would be wrong here: str hashing is salted per process, so the same
                # fixture would carry a different serial on every run.
                "serial_number": f"SN{int(unit(cpe_ref, 'serial') * 10**8):08d}",
                "vendor": "SimVendor-A" if is_hfc else "SimVendor-B",
                "model": "SGW-2400-DOCSIS" if is_hfc else "OGW-1200-XGS",
                "firmware_version": "4.12.3" if port % 2 else "4.11.9",
                "technology": "hfc" if is_hfc else "pon",
                "management_protocol": "tr-069",
                "online": bool(override.get("online", True)),
                # Offsets, not instants -- authoring rule 2.
                "last_inform_offset_hours": float(override.get("last_inform_offset_hours", -0.15)),
                "uptime_seconds": 3600 * (72 + port * 13),
                "wifi_profile": wifi_profile,
                "telemetry_profile": health,
                "stale": bool(override.get("stale", False)),
                "offline": bool(override.get("offline", False)),
                "data_quality_notes": list(override.get("data_quality_notes", [])),
                "ssid_2g": f"LPR-{suffix}-{port:02d}",
                "ssid_5g": f"LPR-{suffix}-{port:02d}-5G",
            }
            devices.append(device)
    return services, devices


_SERVICES, _DEVICES = _build()

#: 41 services, keyed by `service_ref`.
SERVICES: dict[str, dict[str, Any]] = {str(s["service_ref"]): s for s in _SERVICES}
#: One CPE per service, keyed by `cpe_ref`.
CPE_DEVICES: dict[str, dict[str, Any]] = {str(d["cpe_ref"]): d for d in _DEVICES}

# Two devices carry an explicit client list, so the MAC-masking path is exercised against
# fixture-supplied addresses as well as against the ones the simulator synthesises. These are
# locally-administered example addresses, not captured traffic.
CPE_DEVICES["CPE-SJ-011-A-02"]["clients"] = [
    {"mac": "02:1A:2B:3C:4D:5E", "band": "2.4GHz", "rssi_dbm": -78, "hostname": "living-room-tv"},
    {"mac": "02:1A:2B:3C:4D:5F", "band": "2.4GHz", "rssi_dbm": -71, "hostname": "thermostat"},
    {"mac": "02:1A:2B:3C:4D:60", "band": "5GHz", "rssi_dbm": -49, "hostname": "laptop"},
]
CPE_DEVICES["CPE-UT-001-A-03"]["clients"] = [
    {"mac": "02:44:55:66:77:88", "band": "5GHz", "rssi_dbm": -52, "hostname": "phone"},
    {"mac": "02:44:55:66:77:89", "band": "2.4GHz", "rssi_dbm": -69, "hostname": "camera"},
]


# ---------------------------------------------------------------------------------------------
# Recent plant changes, power, weather, crews
# ---------------------------------------------------------------------------------------------
#: Offsets again: `changed_offset_hours = -26` is "26 hours before whatever now is".
PLANT_CHANGES: tuple[dict[str, Any], ...] = (
    {
        "change_ref": "CHG-PO-042-0091",
        "object_refs": ["AMP-PO-042-2", "HFC-NODE-PO-042", "TAP-PO-042-A", "SVC-PO-042-A-04"],
        "change_type": "amplifier_gain_realignment",
        "changed_offset_hours": -26.0,
        "changed_by": "osp_maintenance",
        "work_order_ref": "WO-PLANT-77413",
        "description": "Return path realigned after amplifier replacement; downstream tilt reset.",
        "risk_note": "Marginal downstream on SVC-PO-042-A-04 postdates this change by ~4h.",
    },
    {
        "change_ref": "CHG-UT-001-0043",
        "object_refs": ["OLT-UT-001", "PON-UT-001-1-1-1"],
        "change_type": "olt_firmware_upgrade",
        "changed_offset_hours": -430.0,
        "changed_by": "core_engineering",
        "work_order_ref": "WO-PLANT-76108",
        "description": "OLT line-card firmware upgrade.",
        "risk_note": "Older than the correlation window; present so the window can be seen to bite.",
    },
)

#: One open outage, over the remote island. Vieques services read as power-affected, and the
#: correlation detector should reach POWER_OR_WEATHER_CORRELATED rather than an optical fault.
POWER_OUTAGES: tuple[dict[str, Any], ...] = (
    {
        "outage_ref": "PWR-VQ-2026-0814-3",
        "utility": "utility_operator",
        "archetype": "remote_island",
        "latitude": 18.1500,
        "longitude": -65.4400,
        "radius_km": 4.5,
        "started_offset_hours": -3.2,
        "estimated_restore_offset_hours": 5.0,
        "customers_affected": 1840,
        "cause": "feeder_breaker_trip_after_storm",
        "status": "open",
    },
    {
        "outage_ref": "PWR-UT-2026-0812-1",
        "utility": "utility_operator",
        "archetype": "central_mountain_rural",
        "latitude": 18.2680,
        "longitude": -66.7000,
        "radius_km": 2.0,
        "started_offset_hours": -62.0,
        "estimated_restore_offset_hours": -55.0,
        "customers_affected": 210,
        "cause": "vegetation_contact",
        "status": "restored",
    },
)

WEATHER_BY_AREA: dict[str, dict[str, Any]] = {
    "metro_mdu": {
        "condition": "partly_cloudy",
        "temperature_c": 31.0,
        "wind_kph": 14.0,
        "rain_mm_1h": 0.0,
        "lightning_within_10km": False,
        "field_work_safe": True,
        "advisory": "",
    },
    "coastal_city_suburb": {
        "condition": "showers",
        "temperature_c": 29.5,
        "wind_kph": 26.0,
        "rain_mm_1h": 4.2,
        "lightning_within_10km": False,
        "field_work_safe": True,
        "advisory": "Afternoon squalls; aerial work may pause.",
    },
    "central_mountain_rural": {
        "condition": "thunderstorm",
        "temperature_c": 24.0,
        "wind_kph": 33.0,
        "rain_mm_1h": 11.8,
        "lightning_within_10km": True,
        # The one archetype where field work is unsafe right now, so WEATHER_STOOD_DOWN has a case.
        "field_work_safe": False,
        "advisory": "Lightning within 10 km; no aerial or ladder work.",
    },
    "remote_island": {
        "condition": "tropical_wave",
        "temperature_c": 28.0,
        "wind_kph": 48.0,
        "rain_mm_1h": 8.4,
        "lightning_within_10km": False,
        "field_work_safe": True,
        "advisory": "Ferry service subject to cancellation above 55 kph.",
    },
}

#: Nine crews: four Clean, three Dirty, two Joint. Shifts are local hours resolved against the
#: injected clock by the WFM simulator, not stored instants.
CREWS: tuple[dict[str, Any], ...] = (
    {
        "crew_id": "CREW-CLEAN-SJ-01",
        "crew_type": "clean",
        "skills": ["docsis_rf", "in_home_wiring", "wifi_optimisation", "mdu_riser"],
        "shift_start_hour_local": 7,
        "shift_end_hour_local": 17,
        "base_latitude": 18.4482,
        "base_longitude": -66.0721,
        "area_archetypes": ["metro_mdu", "coastal_city_suburb"],
        "max_jobs": 7,
        "carried_parts": ["PART-COAX-DROP-50M", "PART-F-CONNECTOR-KIT", "PART-CPE-SGW-2400"],
        "on_call": False,
    },
    {
        "crew_id": "CREW-CLEAN-SJ-02",
        "crew_type": "clean",
        "skills": ["docsis_rf", "in_home_wiring", "ont_replacement"],
        "shift_start_hour_local": 12,
        "shift_end_hour_local": 22,
        "base_latitude": 18.4531,
        "base_longitude": -66.0654,
        "area_archetypes": ["metro_mdu"],
        "max_jobs": 6,
        "carried_parts": ["PART-COAX-DROP-50M", "PART-CPE-SGW-2400"],
        "on_call": True,
    },
    {
        "crew_id": "CREW-CLEAN-PO-01",
        "crew_type": "clean",
        "skills": ["docsis_rf", "in_home_wiring", "wifi_optimisation"],
        "shift_start_hour_local": 7,
        "shift_end_hour_local": 16,
        "base_latitude": 18.0098,
        "base_longitude": -66.6171,
        "area_archetypes": ["coastal_city_suburb", "central_mountain_rural"],
        "max_jobs": 6,
        "carried_parts": ["PART-COAX-DROP-50M", "PART-F-CONNECTOR-KIT"],
        "on_call": False,
    },
    {
        "crew_id": "CREW-CLEAN-UT-01",
        "crew_type": "clean",
        "skills": ["fibre_drop", "ont_replacement", "in_home_wiring", "otdr_basic"],
        "shift_start_hour_local": 7,
        "shift_end_hour_local": 16,
        "base_latitude": 18.2655,
        "base_longitude": -66.7042,
        "area_archetypes": ["central_mountain_rural"],
        "max_jobs": 5,
        "carried_parts": ["PART-FIBRE-DROP-80M", "PART-SC-APC-PIGTAIL", "PART-CPE-OGW-1200"],
        "on_call": False,
    },
    {
        "crew_id": "CREW-DIRTY-SJ-01",
        "crew_type": "dirty",
        "skills": ["hfc_plant", "amplifier_alignment", "tap_replacement", "aerial_strand"],
        "shift_start_hour_local": 6,
        "shift_end_hour_local": 15,
        "base_latitude": 18.4467,
        "base_longitude": -66.0803,
        "area_archetypes": ["metro_mdu", "coastal_city_suburb"],
        "max_jobs": 4,
        "carried_parts": ["PART-TAP-8WAY-14DB", "PART-AMP-MODULE", "PART-HARDLINE-CONNECTOR"],
        "on_call": False,
    },
    {
        "crew_id": "CREW-DIRTY-PO-01",
        "crew_type": "dirty",
        "skills": ["hfc_plant", "amplifier_alignment", "pedestal_work", "pnm_sweep"],
        "shift_start_hour_local": 6,
        "shift_end_hour_local": 15,
        "base_latitude": 18.0142,
        "base_longitude": -66.6088,
        "area_archetypes": ["coastal_city_suburb"],
        "max_jobs": 4,
        "carried_parts": ["PART-TAP-8WAY-17DB", "PART-HARDLINE-CONNECTOR"],
        "on_call": True,
    },
    {
        "crew_id": "CREW-DIRTY-UT-01",
        "crew_type": "dirty",
        "skills": ["fibre_splicing", "otdr_advanced", "odp_replacement", "feeder_work"],
        "shift_start_hour_local": 7,
        "shift_end_hour_local": 17,
        "base_latitude": 18.2712,
        "base_longitude": -66.6961,
        "area_archetypes": ["central_mountain_rural", "coastal_city_suburb"],
        "max_jobs": 3,
        "carried_parts": ["PART-SPLICE-CLOSURE", "PART-FIBRE-96F-100M", "PART-ODP-16PORT"],
        "on_call": False,
    },
    {
        "crew_id": "CREW-JOINT-SJ-01",
        "crew_type": "joint",
        "skills": ["docsis_rf", "hfc_plant", "in_home_wiring", "tap_replacement"],
        "shift_start_hour_local": 8,
        "shift_end_hour_local": 18,
        "base_latitude": 18.4495,
        "base_longitude": -66.0739,
        "area_archetypes": ["metro_mdu", "coastal_city_suburb"],
        "max_jobs": 3,
        "carried_parts": ["PART-TAP-8WAY-14DB", "PART-COAX-DROP-50M", "PART-F-CONNECTOR-KIT"],
        "on_call": False,
    },
    {
        # The only crew that can work Vieques, and it is on the island rather than ferried in --
        # which is exactly the constraint that makes remote_island dispatch different.
        "crew_id": "CREW-JOINT-VQ-01",
        "crew_type": "joint",
        "skills": [
            "fibre_drop",
            "fibre_splicing",
            "ont_replacement",
            "in_home_wiring",
            "generator",
        ],
        "shift_start_hour_local": 7,
        "shift_end_hour_local": 16,
        "base_latitude": 18.1483,
        "base_longitude": -65.4425,
        "area_archetypes": ["remote_island"],
        "max_jobs": 4,
        "carried_parts": ["PART-FIBRE-DROP-80M", "PART-SC-APC-PIGTAIL", "PART-CPE-OGW-1200"],
        "on_call": True,
    },
)
