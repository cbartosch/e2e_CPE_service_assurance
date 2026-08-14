"""External-system boundary: ten adapter Protocols, one write gate, and simulators behind both.

Import the *Protocols* from here. Nothing outside `lpr_cpe.simulation` should import a
`Simulated*Adapter` by name -- a stage that annotates against `CPEAdapter` works unchanged the day a
real ACS client appears, and one that annotates against `SimulatedCPEAdapter` has to be edited. That
is the entire benefit of the Protocol layer and it is lost by one convenient import.

`build_simulated_adapters` is re-exported through a module-level `__getattr__` (PEP 562) rather than
imported at the top. It has to be, and the reason is a real cycle rather than a stylistic worry:
`lpr_cpe.simulation.loader` imports the ten simulators, each simulator imports
`lpr_cpe.simulation.simulated_base`, and that module imports `WriteGate` from
`lpr_cpe.integrations.base`. Importing `loader` eagerly here would mean that a process whose first
import is `lpr_cpe.simulation.loader` re-enters this module while `loader` is still half-initialised
and fails on a name that does not exist yet. Deferring it to first attribute access breaks the loop
without hiding the symbol: `from lpr_cpe.integrations import build_simulated_adapters` works, and
the `TYPE_CHECKING` import below means mypy still sees the real signature rather than `Any`.
"""

from typing import TYPE_CHECKING, Any

from lpr_cpe.integrations.base import (
    Adapter,
    AdapterError,
    AdapterUnavailableError,
    CircuitBreaker,
    CircuitOpenError,
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
    WriteVerdict,
    with_retry,
)

if TYPE_CHECKING:
    from lpr_cpe.simulation.loader import SimulatedAdapters, build_simulated_adapters

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterUnavailableError",
    "CPEAdapter",
    "CircuitBreaker",
    "CircuitOpenError",
    "CommunicationsAdapter",
    "GISAdapter",
    "HFCAdapter",
    "InventoryAdapter",
    "JTrackAdapter",
    "NXTAdapter",
    "PONAdapter",
    "SimulatedAdapters",
    "TMFAdapter",
    "WFMAdapter",
    "WriteGate",
    "WriteVerdict",
    "build_simulated_adapters",
    "with_retry",
]

#: Names served lazily from `lpr_cpe.simulation.loader`. Listed explicitly rather than resolved by a
#: catch-all `getattr`, so a typo raises `AttributeError` here instead of triggering an import of a
#: module that will not have the name either.
_LAZY: frozenset[str] = frozenset({"SimulatedAdapters", "build_simulated_adapters"})


def __getattr__(name: str) -> Any:
    """Resolve the simulation-side names on first use. See the module docstring for why."""
    if name in _LAZY:
        from lpr_cpe.simulation import loader

        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Keep `dir()` and tab-completion honest about the lazy names."""
    return sorted(__all__)
