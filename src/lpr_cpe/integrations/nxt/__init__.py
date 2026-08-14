"""ServAssure NXT adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.nxt.simulator import SimulatedNXTAdapter

__all__ = ["SimulatedNXTAdapter"]
