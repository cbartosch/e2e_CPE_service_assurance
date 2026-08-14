"""jTrack MR adapter. Protocol in `integrations.base`; only a simulator exists (A2).

`REQUIRED_MR_FIELDS` is exported because the handover stage must be able to tell an operator what an
MR is missing before it builds the `ActionRequest`, rather than by catching the adapter's rejection.
"""

from lpr_cpe.integrations.jtrack.simulator import REQUIRED_MR_FIELDS, SimulatedJTrackAdapter

__all__ = ["REQUIRED_MR_FIELDS", "SimulatedJTrackAdapter"]
