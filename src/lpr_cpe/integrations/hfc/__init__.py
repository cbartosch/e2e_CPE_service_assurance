"""HFC plant adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.hfc.simulator import SimulatedHFCAdapter

__all__ = ["SimulatedHFCAdapter"]
