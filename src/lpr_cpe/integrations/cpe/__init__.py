"""CPE / ACS adapter. Protocol in `integrations.base`; only a simulator exists (A2).

`STALE_AFTER_HOURS` is exported because the scan pipeline and this adapter must agree on what
"stale" means, and one constant is how they do.
"""

from lpr_cpe.integrations.cpe.simulator import (
    STALE_AFTER_HOURS,
    SUPPORTED_ACTIONS,
    SUPPORTED_DIAGNOSTICS,
    SimulatedCPEAdapter,
)

__all__ = [
    "STALE_AFTER_HOURS",
    "SUPPORTED_ACTIONS",
    "SUPPORTED_DIAGNOSTICS",
    "SimulatedCPEAdapter",
]
