"""Shared fixtures: the simulated adapter set, and the snapshot assembly the graph will use.

`build_context` here is deliberately the long-hand version of what the diagnosis stage does rather
than a call into it. The detectors are supposed to be runnable from a snapshot with no graph and no
network, and a test that reached through the stage to prove it would not be proving it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lpr_cpe.detectors import DetectionContext
from lpr_cpe.domain.enums import Technology
from lpr_cpe.simulation.loader import build_simulated_adapters, load_fixtures


@pytest.fixture(scope="session")
def fixtures() -> Any:
    return load_fixtures()


@pytest.fixture
def adapters(fixtures: Any) -> Any:
    return build_simulated_adapters(fixtures=fixtures)


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="session")
def make_context() -> Any:
    """`build_context` handed over as a value rather than imported from this module.

    Importing a helper out of a `conftest` works by accident of `sys.path` and stops working the
    moment a nearer `conftest.py` appears. Session scope so that a module- or session-scoped fixture
    can depend on it -- the expensive sweeps below build one snapshot per fixture service and would
    otherwise be forced down to function scope by a function-scoped dependency.
    """
    return build_context


@pytest.fixture(scope="session")
def state_of() -> Any:
    """`detector_state` handed over as a value, for the same reason as `make_context`."""
    return detector_state


async def _try(coro: Any) -> Any:
    """One adapter call, its failure turned into `None` rather than propagated.

    This is the diagnosis stage's contract and the reason the context fields are Optional. An
    offline CPE makes `run_diagnostic` raise `AdapterUnavailableError`; if that propagated, one dark
    gateway would cost all thirteen detectors their run. `None` means "the fetch failed", which is
    what the `requires` gate reports as unavailable -- distinct from `{}`, which means the fetch
    succeeded and there was nothing there.
    """
    try:
        return await coro
    except Exception:  # noqa: BLE001 -- the stage absorbs adapter failures by design
        return None


async def build_context(
    adapters: Any,
    service: dict[str, Any],
    *,
    now: datetime,
    history: dict[str, Any] | None = None,
    thresholds: dict[str, float] | None = None,
) -> DetectionContext:
    """Assemble one snapshot: fetch once, run thirteen detectors over it."""
    tech = Technology(service["technology"])
    service_ref = service["service_ref"]
    cpe_ref = service["cpe_ref"]

    nxt: dict[str, Any] | None = None
    if tech is Technology.HFC:
        nxt = {
            "rf": await _try(adapters.nxt.fetch_rf_measurements(service_ref)),
            "pnm": await _try(adapters.nxt.fetch_pnm_capture(service_ref)),
            "service_group": await _try(
                adapters.nxt.fetch_service_group_health(service["service_group_ref"])
            ),
        }
        plant = {
            "port": await _try(adapters.hfc.fetch_node_health(service["node_ref"])),
            "delimiter": await _try(adapters.hfc.fetch_tap_view(service["delimiter_ref"])),
        }
    else:
        plant = {
            "optical": await _try(adapters.pon.fetch_optical_levels(service_ref)),
            "port": await _try(adapters.pon.fetch_pon_port_health(service["pon_port_ref"])),
            "delimiter": await _try(adapters.pon.fetch_odp_view(service["delimiter_ref"])),
        }

    diag = await _try(adapters.cpe.run_diagnostic(cpe_ref, "download_speed"))
    return DetectionContext(
        incident_id=f"INC-TEST-{service_ref}",
        now=now,
        technology=tech,
        nxt=nxt,
        plant=plant,
        cpe_raw=await _try(adapters.cpe.read_status(cpe_ref)),
        wifi=await _try(adapters.cpe.read_wifi_status(cpe_ref)),
        service_platform=(
            {"service_ref": service_ref, "download_speed": diag.get("result") or {}}
            if diag is not None
            else None
        ),
        recent_changes=await _try(
            adapters.inventory.fetch_recent_changes(
                object_refs=[service["delimiter_ref"], service.get("node_ref") or ""],
                since=now - timedelta(days=14),
            )
        ),
        power_outages=await _try(
            adapters.gis.fetch_power_outages(
                latitude=service["latitude"], longitude=service["longitude"], radius_km=5.0
            )
        ),
        weather=await _try(
            adapters.gis.fetch_weather(
                latitude=service["latitude"], longitude=service["longitude"], at=now
            )
        ),
        # Incident state, not adapter data -- the graph carries it.
        history=history if history is not None else {},
        thresholds=thresholds or {},
    )


def detector_state(result: Any) -> str:
    """The three outcomes, kept distinct. Collapsing them is what `ran` exists to prevent."""
    if not result.ran:
        return "n/a" if not result.data_quality_warnings else "unavailable"
    return "fired" if result.findings else "clean"
