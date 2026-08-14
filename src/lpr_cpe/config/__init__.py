"""Settings, the operating mode gate, and the one clock.

Three things live here because each is a fact that must have exactly one owner:

* **the mode** -- `simulation` or `production`, and the separate `allow_production_writes` switch.
  Restating "are we allowed to write?" at each adapter is how a system ends up allowed to write from
  eleven places and forbidden from ten of them.
* **the clock** -- `now()` returns a timezone-aware UTC instant, and `local_now()` renders it in
  `America/Puerto_Rico`. Nothing in this package calls `datetime.now()` without a tzinfo; Ruff's DTZ
  rules are switched on to keep that true rather than merely intended.
* **the access-network defaults** -- tap size, ODP size, split ratio. The specification says these
  are configurable, so they are read from here and never written as literals in a detector.
"""

from lpr_cpe.config.clock import Clock, FrozenClock, SystemClock
from lpr_cpe.config.settings import (
    AppMode,
    Environment,
    LogFormat,
    ModelProvider,
    Settings,
    get_settings,
    reset_settings_cache,
)

__all__ = [
    "AppMode",
    "Clock",
    "Environment",
    "FrozenClock",
    "LogFormat",
    "ModelProvider",
    "Settings",
    "SystemClock",
    "get_settings",
    "reset_settings_cache",
]
