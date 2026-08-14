"""Workforce management adapter. Protocol in `integrations.base`; only a simulator exists (A2)."""

from lpr_cpe.integrations.wfm.simulator import SimulatedWFMAdapter

__all__ = ["SimulatedWFMAdapter"]
