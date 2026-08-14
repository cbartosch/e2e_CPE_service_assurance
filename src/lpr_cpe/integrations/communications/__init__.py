"""Customer contact adapter. Protocol in `integrations.base`; only a simulator exists (A2).

The catalogues are exported because the stage that chooses a message must be able to check that its
choice exists before it builds an `ActionRequest`, and because a customer-facing string should be
reviewable in one place rather than discovered by reading the adapter.
"""

from lpr_cpe.integrations.communications.simulator import (
    SELF_HELP_SCRIPTS,
    SUPPORTED_CHANNELS,
    SUPPORTED_LANGUAGES,
    TEMPLATES,
    SimulatedCommunicationsAdapter,
)

__all__ = [
    "SELF_HELP_SCRIPTS",
    "SUPPORTED_CHANNELS",
    "SUPPORTED_LANGUAGES",
    "TEMPLATES",
    "SimulatedCommunicationsAdapter",
]
