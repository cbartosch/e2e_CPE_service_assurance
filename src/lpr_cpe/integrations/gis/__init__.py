"""Geography / weather / power adapter. Protocol in `integrations.base`; only a simulator (A2).

`FERRY_WIND_LIMIT_KPH` and `FORECAST_HORIZON_HOURS` are exported because the dispatch stage has to
reason about both -- "can the ferry sail" and "is this reading a reading" -- and neither should be a
number typed twice.
"""

from lpr_cpe.integrations.gis.simulator import (
    FERRY_WIND_LIMIT_KPH,
    FORECAST_HORIZON_HOURS,
    SimulatedGISAdapter,
)

__all__ = ["FERRY_WIND_LIMIT_KPH", "FORECAST_HORIZON_HOURS", "SimulatedGISAdapter"]
