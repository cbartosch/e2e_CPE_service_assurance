"""The adapter contract, and the one gate every external write passes through.

Every adapter in this package is a `Protocol` with a fixture-backed simulator behind it. None of
them names a real vendor endpoint, because none was supplied (IMPLEMENTATION_PLAN.md A1/A2), and
inventing one would produce code that looks integrated and is not.

**`WriteGate` is the single owner of "are we allowed to change the outside world?"** Adapters call
`gate.authorize(request)` and act on the answer. They do not read `settings.app_mode`, they do not
check `allow_production_writes`, and they do not decide for themselves what counts as a write. Two
reasons that matters:

1. A rule restated in eleven adapters is a rule that is wrong in one of them, and the one it is
   wrong in is the one nobody tests.
2. The gate is the place a *test* can assert on. `test_no_write_escapes_in_simulation_mode` walks
   every adapter's write methods and asserts each one went through the gate; that test is only
   possible because there is exactly one gate to go through.

The gate does not evaluate policy. Policy asks "should we?" and lives in `lpr_cpe.policies`; the
gate asks "is this deployment permitted to at all?" and answers from configuration. An action must
pass both, in that order -- policy first, because a blocked action should never reach an adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from lpr_cpe.config import Settings, get_settings
from lpr_cpe.domain.enums import ActionOutcome, ReasonCode
from lpr_cpe.domain.governance import ActionRequest

T = TypeVar("T")


class AdapterError(RuntimeError):
    """An external system failed in a way the caller may retry."""

    def __init__(self, system: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__(f"{system}: {detail}")
        self.system = system
        self.detail = detail
        self.retryable = retryable


class AdapterUnavailableError(AdapterError):
    """The system could not be reached at all.

    Distinct from `AdapterError` because the routing differs: an unavailable adapter is a
    *data-quality* fact that must reach `DataQualityAssessment`, whereas a failed call that returned
    an error is a result.
    """

    def __init__(self, system: str, detail: str) -> None:
        super().__init__(system, detail, retryable=True)


class CircuitOpenError(AdapterUnavailableError):
    """The local breaker is open, so we did not even try."""


@dataclass(frozen=True, slots=True)
class WriteVerdict:
    """The gate's answer. `simulated=True` means "proceed, but do not call out"."""

    permitted: bool
    simulated: bool
    reason_code: ReasonCode
    explanation: str

    @property
    def outcome_if_refused(self) -> ActionOutcome:
        return ActionOutcome.SIMULATED if self.simulated else ActionOutcome.BLOCKED_BY_POLICY


class WriteGate:
    """Answers "may this write leave the process?" from configuration alone.

    Both switches, never one: `settings.writes_permitted` is
    `app_mode is PRODUCTION and allow_production_writes`. A deployment in production mode with
    writes disabled simulates; a deployment in simulation mode simulates regardless of the write
    switch. There is no combination that writes without both being deliberately set.
    """

    __slots__ = ("_recorded", "_settings")

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        # Every authorize() call, for the test that asserts nothing bypassed the gate and for the
        # simulation-mode audit trail: in simulation the intent IS the record.
        self._recorded: list[dict[str, Any]] = []

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def recorded(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._recorded)

    def authorize(self, request: ActionRequest) -> WriteVerdict:
        self._recorded.append(
            {
                "action_id": request.action_id,
                "action_type": request.action_type.value,
                "target_ref": request.target_ref,
                "idempotency_key": request.idempotency_key,
                "incident_id": request.incident_id,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        if self._settings.writes_permitted:
            return WriteVerdict(
                permitted=True,
                simulated=False,
                reason_code=ReasonCode.POLICY_ALLOWED,
                explanation="mode=production and allow_production_writes=true",
            )
        return WriteVerdict(
            permitted=False,
            simulated=True,
            reason_code=ReasonCode.POLICY_WRITES_DISABLED,
            explanation=(
                f"simulated: app_mode={self._settings.app_mode.value}, "
                f"allow_production_writes={self._settings.allow_production_writes}. "
                "Both must be set for a write to leave the process."
            ),
        )


@dataclass
class CircuitBreaker:
    """A per-adapter breaker. Deliberately small: three counters and a clock.

    Half-open is implicit -- once the cooldown has passed the next call is simply attempted, and a
    success resets the count. A separate half-open state would add a mode with no behaviour of its
    own here, since there is only ever one probe.
    """

    name: str
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def before_call(self, monotonic: float) -> None:
        if self._opened_at is None:
            return
        if monotonic - self._opened_at < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (monotonic - self._opened_at)
            raise CircuitOpenError(
                self.name, f"breaker open after {self._failures} failures, {remaining:.1f}s left"
            )
        # Cooldown elapsed: allow one probe through with the breaker still armed.
        self._opened_at = None

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self, monotonic: float) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = monotonic

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    system: str,
    attempts: int = 3,
    base_delay: float = 0.05,
    breaker: CircuitBreaker | None = None,
    monotonic: Callable[[], float] | None = None,
) -> T:
    """Retry with exponential backoff, honouring a breaker.

    `base_delay` defaults to 50 ms rather than the conventional second because these retries happen
    inside a graph super-step: a three-attempt retry at one second is three seconds of an incident's
    latency budget spent waiting, and the simulator this runs against fails instantly or not at all.
    A real HTTP adapter should raise it.

    Non-retryable `AdapterError`s propagate on the first attempt. Retrying a 400 is just a
    slower 400.
    """
    clock = monotonic or (lambda: asyncio.get_running_loop().time())
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if breaker is not None:
            breaker.before_call(clock())
        try:
            result = await operation()
        except AdapterError as exc:
            last = exc
            if breaker is not None:
                breaker.on_failure(clock())
            if not exc.retryable or attempt == attempts:
                raise
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
        else:
            if breaker is not None:
                breaker.on_success()
            return result
    raise AdapterUnavailableError(system, f"exhausted {attempts} attempts: {last}")


# ------------------------------------------------------------------------------------------------
# Adapter protocols
# ------------------------------------------------------------------------------------------------
#
# Read operations return plain dicts or domain models; write operations take an `ActionRequest` and
# return an `ActionRecord`-shaped dict. Every write signature takes the request whole rather than
# loose arguments, so the six mandatory fields travel with it and cannot be dropped at a call site.


@runtime_checkable
class Adapter(Protocol):
    """Common surface. `system_name` appears in evidence provenance and audit events."""

    system_name: str

    async def health(self) -> bool: ...


@runtime_checkable
class NXTAdapter(Adapter, Protocol):
    """CommScope ServAssure NXT. Alarms, RF/PNM measurements, service-group views.

    No endpoint here is confirmed -- see docs/vendor-integration-gaps.md, gap NXT-1.
    """

    async def fetch_alarms(
        self, *, since: datetime, service_ref: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def fetch_rf_measurements(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_pnm_capture(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_service_group_health(self, service_group_ref: str) -> dict[str, Any]: ...


@runtime_checkable
class HFCAdapter(Adapter, Protocol):
    """HFC plant: node/amplifier topology and per-tap views."""

    async def fetch_topology(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_tap_view(self, tap_ref: str) -> dict[str, Any]: ...

    async def fetch_node_health(self, node_ref: str) -> dict[str, Any]: ...


@runtime_checkable
class PONAdapter(Adapter, Protocol):
    """PON plant: OLT/port/splitter/ODP topology and optical readings."""

    async def fetch_topology(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_optical_levels(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_pon_port_health(self, pon_port_ref: str) -> dict[str, Any]: ...

    async def fetch_odp_view(self, odp_ref: str) -> dict[str, Any]: ...


@runtime_checkable
class CPEAdapter(Adapter, Protocol):
    """ACS / TR-069 northbound. The only adapter that reads TR-181 `Device.WiFi.*`.

    `read_wifi_status` masks client MAC addresses *before returning*, which is why the masking lives
    in the adapter and not in the detector: the specification requires it at the collection
    boundary, and a detector-side masker would mean the unmasked payload had already been logged.
    """

    async def read_status(self, cpe_ref: str) -> dict[str, Any]: ...

    async def read_wifi_status(self, cpe_ref: str) -> dict[str, Any]: ...

    async def run_diagnostic(self, cpe_ref: str, diagnostic: str) -> dict[str, Any]: ...

    async def apply_action(self, request: ActionRequest) -> dict[str, Any]: ...


@runtime_checkable
class TMFAdapter(Adapter, Protocol):
    """CRM / ITSM / TMF-aligned record store. Field names are ours (A2)."""

    async def fetch_customer(self, customer_ref: str) -> dict[str, Any]: ...

    async def fetch_service(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_sla(self, service_ref: str) -> dict[str, Any]: ...

    async def upsert_service_problem(self, request: ActionRequest) -> dict[str, Any]: ...


@runtime_checkable
class WFMAdapter(Adapter, Protocol):
    """Workforce management: crew availability and work orders."""

    async def fetch_crew_availability(
        self, *, area: str, crew_type: str, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]: ...

    async def create_work_order(self, request: ActionRequest) -> dict[str, Any]: ...

    async def cancel_work_order(self, request: ActionRequest) -> dict[str, Any]: ...

    async def fetch_work_order(self, work_order_ref: str) -> dict[str, Any]: ...


@runtime_checkable
class InventoryAdapter(Adapter, Protocol):
    async def fetch_plant_object(self, object_ref: str) -> dict[str, Any]: ...

    async def fetch_recent_changes(
        self, *, object_refs: list[str], since: datetime
    ) -> list[dict[str, Any]]: ...

    async def update_plant_object(self, request: ActionRequest) -> dict[str, Any]: ...


@runtime_checkable
class JTrackAdapter(Adapter, Protocol):
    """LPR's MR system of record. Gap JTRACK-1: no schema was supplied."""

    async def create_mr(self, request: ActionRequest) -> dict[str, Any]: ...

    async def update_mr(self, request: ActionRequest) -> dict[str, Any]: ...

    async def fetch_mr(self, mr_ref: str) -> dict[str, Any]: ...

    async def fetch_open_mrs(self, plant_object_ref: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class GISAdapter(Adapter, Protocol):
    """Geography, weather and utility power. Read-only by design."""

    async def fetch_location(self, service_ref: str) -> dict[str, Any]: ...

    async def fetch_weather(
        self, *, latitude: float, longitude: float, at: datetime
    ) -> dict[str, Any]: ...

    async def fetch_power_outages(
        self, *, latitude: float, longitude: float, radius_km: float
    ) -> list[dict[str, Any]]: ...

    async def travel_minutes(
        self, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float, archetype: str
    ) -> float: ...


@runtime_checkable
class CommunicationsAdapter(Adapter, Protocol):
    """Outbound customer contact.

    Quiet hours and contact caps are policy, not this adapter's job.
    """

    async def send_notification(self, request: ActionRequest) -> dict[str, Any]: ...

    async def send_self_help(self, request: ActionRequest) -> dict[str, Any]: ...

    async def fetch_customer_responses(self, incident_id: str) -> list[dict[str, Any]]: ...
