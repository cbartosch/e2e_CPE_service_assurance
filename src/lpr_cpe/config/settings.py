"""Settings, read once from the environment.

The two safety switches are deliberately awkward to satisfy: `writes_permitted` requires
`mode is PRODUCTION` **and** `allow_production_writes is True`. Either alone is not enough. A single
`ENABLE_WRITES=true` would be one typo away from a live write from a laptop, and the mode is
something a developer sets casually.

`get_settings()` is cached so the environment is read once per process. `reset_settings_cache()`
exists for tests and is the only supported way to change settings mid-process -- mutating a
`Settings` instance is refused because the model is frozen.
"""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lpr_cpe.config.clock import parse_scan_windows


class AppMode(StrEnum):
    SIMULATION = "simulation"
    PRODUCTION = "production"


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class ModelProvider(StrEnum):
    FAKE = "fake"
    ANTHROPIC = "anthropic"


class Settings(BaseSettings):
    """Frozen configuration. See `.env.example` for the same list with commentary."""

    model_config = SettingsConfigDict(
        env_prefix="LPR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- safety --------------------------------------------------------------------------------
    app_mode: AppMode = AppMode.SIMULATION
    allow_production_writes: bool = False
    environment: Environment = Environment.LOCAL

    # -- logging -------------------------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- persistence ---------------------------------------------------------------------------
    postgres_dsn: str = ""
    postgres_pool_min: int = Field(default=1, ge=1)
    postgres_pool_max: int = Field(default=10, ge=1)

    # -- clock ---------------------------------------------------------------------------------
    timezone: str = "America/Puerto_Rico"
    scan_windows: str = "07:00,21:00"

    # -- model ---------------------------------------------------------------------------------
    model_provider: ModelProvider = ModelProvider.FAKE
    model_name: str = "claude-sonnet-4-5"
    model_max_tokens: int = Field(default=1024, ge=1)
    model_timeout_seconds: float = Field(default=30.0, gt=0)

    # -- access network defaults ---------------------------------------------------------------
    default_tap_size: int = Field(default=8, ge=1)
    default_odp_size: int = Field(default=16, ge=1)
    default_split_ratio: int = Field(default=32, ge=1)

    # -- external systems ----------------------------------------------------------------------
    nxt_base_url: str = ""
    cpe_acs_base_url: str = ""
    tmf_base_url: str = ""
    wfm_base_url: str = ""
    inventory_base_url: str = ""
    jtrack_base_url: str = ""
    gis_base_url: str = ""
    comms_base_url: str = ""
    external_timeout_seconds: float = Field(default=15.0, gt=0)
    external_max_retries: int = Field(default=3, ge=0)

    # -- observability -------------------------------------------------------------------------
    otel_enabled: bool = False
    otel_endpoint: str = ""
    langsmith_enabled: bool = False

    # -- webhooks ------------------------------------------------------------------------------
    webhook_secret: str = ""

    # -- graph limits --------------------------------------------------------------------------
    # Bounds the number of super-steps a single incident may take. The specification asks for
    # max-loop protection; putting the number here rather than in the graph means the scenario
    # tests can lower it to prove the protection fires instead of waiting for 60 real iterations.
    max_graph_steps: int = Field(default=60, ge=4)
    # One pass through P07 is one cycle, counted on entry, and the guard refuses the pass that
    # would *reach* the limit -- so N permits N-1 complete cycles. That arithmetic became
    # load-bearing when the resolution fork was wired, because D10 and D12 spend a whole cycle to
    # reach the next resolution option: a plan of k options needs k complete passes to be worked
    # through. At 3 nothing could be tried twice, and measured over all 41 simulation services the
    # self-help branch was unreachable end to end -- the one service that offers a script spends
    # two cycles on the Wi-Fi settings options that are offered ahead of it. The largest plan any
    # fixture produces holds five options, so 6 is the first value that lets a plan be exhausted
    # rather than truncated. Higher buys nothing and costs the bound.
    max_diagnostic_cycles: int = Field(default=6, ge=1)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        out = v.upper()
        if out not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {v!r}")
        return out

    @model_validator(mode="after")
    def _pool_bounds(self) -> Self:
        if self.postgres_pool_max < self.postgres_pool_min:
            raise ValueError(
                f"postgres_pool_max ({self.postgres_pool_max}) is below postgres_pool_min "
                f"({self.postgres_pool_min})"
            )
        return self

    @model_validator(mode="after")
    def _windows_parse_at_startup(self) -> Self:
        # Parse eagerly so a malformed window is a startup error, not a 07:00 surprise.
        parse_scan_windows(self.scan_windows)
        return self

    @model_validator(mode="after")
    def _prod_needs_webhook_secret(self) -> Self:
        # An unauthenticated webhook in production is an open door for forged NXT alarms, and the
        # graph acts on those. Refuse to start rather than log a warning nobody reads.
        if self.environment is Environment.PROD and not self.webhook_secret:
            raise ValueError(
                "LPR_WEBHOOK_SECRET must be set when LPR_ENVIRONMENT=prod: the inbound webhooks "
                "create and advance incidents, so an unauthenticated one is a way to forge them"
            )
        return self

    # -- derived -------------------------------------------------------------------------------
    @property
    def writes_permitted(self) -> bool:
        """Both switches, never one.

        The only place this question is answered. `integrations.base` consults it; adapters do not
        re-derive it from `app_mode`.
        """
        return self.app_mode is AppMode.PRODUCTION and self.allow_production_writes

    @property
    def scan_window_times(self) -> tuple[time, ...]:
        return parse_scan_windows(self.scan_windows)

    @property
    def postgres_enabled(self) -> bool:
        return bool(self.postgres_dsn.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Forget the cached settings. For tests that monkeypatch the environment."""
    get_settings.cache_clear()
