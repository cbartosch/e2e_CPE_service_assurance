"""PON plant adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.pon.simulator import SimulatedPONAdapter

__all__ = ["SimulatedPONAdapter"]
