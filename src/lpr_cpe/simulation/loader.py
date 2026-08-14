"""The typed fixture container, and the one place simulated adapters are constructed.

`build_simulated_adapters()` is the factory the graph, the demo and the scenario tests all call. It
exists so that "which ten adapters does this system have, and how are they wired" is answered once.
Ten `SimulatedXAdapter(...)` constructor calls scattered across a graph builder, a CLI and a test
fixture is ten places to forget to pass the shared `WriteGate` -- and an adapter holding its own
gate is an adapter whose writes never appear in the audit the gate exists to produce.

`load_fixtures()` is cached because the fixture graph is immutable and building the index maps costs
more than it should to repeat per incident. `Fixtures` is deliberately read-only: the simulators
keep their own mutable idempotency ledgers, so nothing needs to write back here, and a mutable
shared fixture set would let one scenario's write leak into the next scenario's read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from lpr_cpe.config.clock import Clock, SystemClock
from lpr_cpe.integrations.base import (
    AdapterUnavailableError,
    CommunicationsAdapter,
    CPEAdapter,
    GISAdapter,
    HFCAdapter,
    InventoryAdapter,
    JTrackAdapter,
    NXTAdapter,
    PONAdapter,
    TMFAdapter,
    WFMAdapter,
    WriteGate,
)
from lpr_cpe.integrations.communications.simulator import SimulatedCommunicationsAdapter
from lpr_cpe.integrations.cpe.simulator import SimulatedCPEAdapter
from lpr_cpe.integrations.gis.simulator import SimulatedGISAdapter
from lpr_cpe.integrations.hfc.simulator import SimulatedHFCAdapter
from lpr_cpe.integrations.inventory.simulator import SimulatedInventoryAdapter
from lpr_cpe.integrations.jtrack.simulator import SimulatedJTrackAdapter
from lpr_cpe.integrations.nxt.simulator import SimulatedNXTAdapter
from lpr_cpe.integrations.pon.simulator import SimulatedPONAdapter
from lpr_cpe.integrations.tmf.simulator import SimulatedTMFAdapter
from lpr_cpe.integrations.wfm.simulator import SimulatedWFMAdapter
from lpr_cpe.simulation import fixtures as fixture_data


@dataclass(frozen=True, slots=True)
class Fixtures:
    """The synthetic network, with the lookups the simulators actually perform.

    The `by_*` indexes are built once here rather than scanned per call. That is not premature
    optimization: `fetch_tap_view` needs every service behind a delimiter, and the common-cause
    detector calls it for each of eight tap-mates, so a linear scan over 41 services would run 328
    times for one incident.
    """

    services: dict[str, dict[str, Any]]
    cpe_devices: dict[str, dict[str, Any]]
    hfc_nodes: dict[str, dict[str, Any]]
    olts: dict[str, dict[str, Any]]
    taps: dict[str, dict[str, Any]]
    odps: dict[str, dict[str, Any]]
    areas: dict[str, dict[str, Any]]
    crews: tuple[dict[str, Any], ...]
    plant_changes: tuple[dict[str, Any], ...]
    power_outages: tuple[dict[str, Any], ...]
    weather_by_area: dict[str, dict[str, Any]]
    telemetry_profiles: dict[str, dict[str, Any]]
    wifi_profiles: dict[str, dict[str, Any]]

    services_by_delimiter: dict[str, tuple[str, ...]] = field(default_factory=dict)
    services_by_parent: dict[str, tuple[str, ...]] = field(default_factory=dict)
    service_by_cpe: dict[str, str] = field(default_factory=dict)
    service_by_customer: dict[str, str] = field(default_factory=dict)

    # -- lookups ---------------------------------------------------------------------------------
    #
    # Each raises `AdapterUnavailableError` on an unknown ref rather than returning None. The
    # reasoning is in the adapters' own docstrings: a missing subject is a data-quality fact that
    # must reach `DataQualityAssessment`, and a `None` that a caller forgets to check becomes a
    # confident verdict computed from nothing.

    def service(self, service_ref: str, *, system: str) -> dict[str, Any]:
        try:
            return self.services[service_ref]
        except KeyError:
            raise AdapterUnavailableError(system, f"unknown service_ref {service_ref!r}") from None

    def cpe(self, cpe_ref: str, *, system: str) -> dict[str, Any]:
        try:
            return self.cpe_devices[cpe_ref]
        except KeyError:
            raise AdapterUnavailableError(system, f"unknown cpe_ref {cpe_ref!r}") from None

    def delimiter(self, delimiter_ref: str, *, system: str) -> dict[str, Any]:
        found = self.taps.get(delimiter_ref) or self.odps.get(delimiter_ref)
        if found is None:
            raise AdapterUnavailableError(system, f"unknown delimiter_ref {delimiter_ref!r}")
        return found

    def plant_object(self, object_ref: str) -> dict[str, Any] | None:
        """Any addressable plant object, or None.

        The only lookup that returns None, because `InventoryAdapter.fetch_plant_object` answers
        "does this object exist" for objects the workflow has only heard named in an alarm -- an
        exception there would make a routine miss look like an outage.
        """
        for collection in (self.hfc_nodes, self.olts, self.taps, self.odps, self.services):
            if object_ref in collection:
                return collection[object_ref]
        return None

    def cpe_for_service(self, service_ref: str, *, system: str) -> dict[str, Any]:
        return self.cpe(str(self.service(service_ref, system=system)["cpe_ref"]), system=system)

    def peers_behind_delimiter(self, delimiter_ref: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            self.services[ref] for ref in self.services_by_delimiter.get(delimiter_ref, ())
        )

    def telemetry(self, service: dict[str, Any]) -> dict[str, Any]:
        """The nominal reading profile for a service's health state."""
        return self.telemetry_profiles[str(service["health"])]

    def area_for(self, archetype: str, *, system: str) -> dict[str, Any]:
        try:
            return self.areas[archetype]
        except KeyError:
            raise AdapterUnavailableError(system, f"unknown area archetype {archetype!r}") from None


def _build_fixtures() -> Fixtures:
    by_delimiter: dict[str, list[str]] = {}
    by_parent: dict[str, list[str]] = {}
    by_cpe: dict[str, str] = {}
    by_customer: dict[str, str] = {}
    for ref, service in fixture_data.SERVICES.items():
        by_delimiter.setdefault(str(service["delimiter_ref"]), []).append(ref)
        parent = service["node_ref"] or service["pon_port_ref"]
        by_parent.setdefault(str(parent), []).append(ref)
        by_cpe[str(service["cpe_ref"])] = ref
        by_customer[str(service["customer_ref"])] = ref
    return Fixtures(
        services=dict(fixture_data.SERVICES),
        cpe_devices=dict(fixture_data.CPE_DEVICES),
        hfc_nodes=dict(fixture_data.HFC_NODES),
        olts=dict(fixture_data.OLTS),
        taps=dict(fixture_data.TAPS),
        odps=dict(fixture_data.ODPS),
        areas=dict(fixture_data.AREAS),
        crews=tuple(fixture_data.CREWS),
        plant_changes=tuple(fixture_data.PLANT_CHANGES),
        power_outages=tuple(fixture_data.POWER_OUTAGES),
        weather_by_area=dict(fixture_data.WEATHER_BY_AREA),
        telemetry_profiles=dict(fixture_data.TELEMETRY_PROFILES),
        wifi_profiles=dict(fixture_data.WIFI_PROFILES),
        services_by_delimiter={k: tuple(v) for k, v in by_delimiter.items()},
        services_by_parent={k: tuple(v) for k, v in by_parent.items()},
        service_by_cpe=by_cpe,
        service_by_customer=by_customer,
    )


@lru_cache(maxsize=1)
def load_fixtures() -> Fixtures:
    """The synthetic network, built once per process."""
    return _build_fixtures()


def reset_fixture_cache() -> None:
    """Forget the cached fixtures. For a test that needs a pristine set."""
    load_fixtures.cache_clear()


@dataclass(frozen=True, slots=True)
class SimulatedAdapters:
    """All ten adapters, typed as their Protocols rather than their implementations.

    Annotating these as `NXTAdapter` and not `SimulatedNXTAdapter` is what makes the container
    usable as the production wiring point too: a caller that reaches for a simulator-only attribute
    fails type-checking here, where it is one line to see, rather than at the swap.
    """

    nxt: NXTAdapter
    hfc: HFCAdapter
    pon: PONAdapter
    cpe: CPEAdapter
    tmf: TMFAdapter
    wfm: WFMAdapter
    inventory: InventoryAdapter
    jtrack: JTrackAdapter
    gis: GISAdapter
    communications: CommunicationsAdapter

    gate: WriteGate
    clock: Clock
    fixtures: Fixtures

    def all_adapters(self) -> dict[str, Any]:
        """Name -> adapter, for the health sweep and for the test that walks every write method."""
        return {
            "nxt": self.nxt,
            "hfc": self.hfc,
            "pon": self.pon,
            "cpe": self.cpe,
            "tmf": self.tmf,
            "wfm": self.wfm,
            "inventory": self.inventory,
            "jtrack": self.jtrack,
            "gis": self.gis,
            "communications": self.communications,
        }

    def plant_adapter_for(self, technology: str) -> HFCAdapter | PONAdapter:
        """The plant adapter that owns this technology.

        One owner for the HFC/PON fork. Every node that needs plant topology would otherwise carry
        its own `if technology == "hfc"`, and the eleventh copy is the one that forgets `unknown`.
        """
        if technology == "hfc":
            return self.hfc
        if technology == "pon":
            return self.pon
        raise AdapterUnavailableError(
            "plant", f"no plant adapter for technology {technology!r}; resolve inventory first"
        )


def build_simulated_adapters(
    clock: Clock | None = None,
    gate: WriteGate | None = None,
    fixtures: Fixtures | None = None,
) -> SimulatedAdapters:
    """Construct all ten simulated adapters over one clock, one gate and one fixture set.

    The shared `gate` is the point. `gate.recorded` is then the complete list of every write the
    process intended, across all ten systems, which is what
    `test_no_write_escapes_in_simulation_mode` asserts over.
    """
    resolved_clock = clock or SystemClock()
    resolved_gate = gate or WriteGate()
    data = fixtures or load_fixtures()
    return SimulatedAdapters(
        nxt=SimulatedNXTAdapter(data, resolved_clock, resolved_gate),
        hfc=SimulatedHFCAdapter(data, resolved_clock, resolved_gate),
        pon=SimulatedPONAdapter(data, resolved_clock, resolved_gate),
        cpe=SimulatedCPEAdapter(data, resolved_clock, resolved_gate),
        tmf=SimulatedTMFAdapter(data, resolved_clock, resolved_gate),
        wfm=SimulatedWFMAdapter(data, resolved_clock, resolved_gate),
        inventory=SimulatedInventoryAdapter(data, resolved_clock, resolved_gate),
        jtrack=SimulatedJTrackAdapter(data, resolved_clock, resolved_gate),
        gis=SimulatedGISAdapter(data, resolved_clock, resolved_gate),
        communications=SimulatedCommunicationsAdapter(data, resolved_clock, resolved_gate),
        gate=resolved_gate,
        clock=resolved_clock,
        fixtures=data,
    )
