"""CRM / ITSM adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.tmf.simulator import SimulatedTMFAdapter

__all__ = ["SimulatedTMFAdapter"]
