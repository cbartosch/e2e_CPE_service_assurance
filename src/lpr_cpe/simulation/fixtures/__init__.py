"""Fixture data for the simulators.

Python modules rather than JSON files, for one reason: the topology has to satisfy
`TopologyContext`'s validators (a PON service may not sit behind a TAP, an HFC service may not sit
behind an ODP, `homes_behind_delimiter <= homes_behind_node_or_port`), and expressing the network as
data *plus a builder* means the nesting counts are computed from the structure instead of typed in
twice and drifting. A JSON file would have the same numbers written by hand in two places.

Nothing here is a vendor payload. Every key is our invention -- see
`docs/vendor-integration-gaps.md`.
"""

from lpr_cpe.simulation.fixtures.determinism import jitter, pick, unit
from lpr_cpe.simulation.fixtures.network import (
    AREAS,
    CPE_DEVICES,
    CREWS,
    HFC_NODES,
    ODPS,
    OLTS,
    PLANT_CHANGES,
    POWER_OUTAGES,
    SERVICES,
    TAPS,
    TELEMETRY_PROFILES,
    WEATHER_BY_AREA,
    WIFI_PROFILES,
)

__all__ = [
    "AREAS",
    "CPE_DEVICES",
    "CREWS",
    "HFC_NODES",
    "ODPS",
    "OLTS",
    "PLANT_CHANGES",
    "POWER_OUTAGES",
    "SERVICES",
    "TAPS",
    "TELEMETRY_PROFILES",
    "WEATHER_BY_AREA",
    "WIFI_PROFILES",
    "jitter",
    "pick",
    "unit",
]
