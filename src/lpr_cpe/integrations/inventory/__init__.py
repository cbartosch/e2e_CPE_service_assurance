"""Plant inventory adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.inventory.simulator import SimulatedInventoryAdapter

__all__ = ["SimulatedInventoryAdapter"]
