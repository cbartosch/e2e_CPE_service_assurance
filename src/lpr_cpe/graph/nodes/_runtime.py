"""What every node does the same way, written once.

A node in this system is not just "an async function that returns a dict". It has four obligations
that are identical everywhere and easy to get subtly wrong in each new file:

1. **Check the loop guard before doing any work.** After, and the incident has already made the
   external call the bound existed to prevent.
2. **Record its own visit.** The guard reads `node_visits`; a node that forgets to bump it is a node
   the guard cannot see, and the bound silently stops applying to it.
3. **Reach dependencies through the runtime, not a closure.** `get_runtime(GraphContext).context` is
   what lets two graphs with different adapters coexist in one process.
4. **Mint every id from its inputs.** An interrupted node re-runs from its start on resume, so a
   `uuid4` id means the same fact recorded twice under two natural keys, and `append_unique` keeps
   both.

`@node` discharges 1-3. `derive_id` and `audit` discharge 4. Nothing else in `graph/nodes/` should
re-implement any of them.

Why the decorator does not catch exceptions
-------------------------------------------
The obvious fifth obligation -- "turn a failure into an error record instead of crashing" -- is
deliberately absent, and its absence is the reason `Gathered` exists.

An adapter failure is *expected*, and it is expected at a known place: the fetch. `Gathered` catches
it there, where the source's name is still in scope, and turns it into a `DataQualityFlag` and a
note that says which source and why. That is a fact about the incident, and D01 and D05 route on it.

Anything else escaping a node body is a bug in this codebase. If the decorator swallowed it, the
node would return a state update claiming nothing happened, the graph would advance, and the
checkpoint would record a plausible lie. Letting it propagate leaves the checkpoint un-advanced at
the last node that did complete, which is both truthful and resumable.

So: adapter errors are caught narrowly, at the seam that knows what they mean. Everything else is
allowed to stop the graph.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from functools import wraps
from typing import Any, Protocol, cast, get_type_hints

from langgraph.runtime import get_runtime

from lpr_cpe.domain.enums import DataQualityFlag, EvidenceKind, KPIName, ReasonCode
from lpr_cpe.domain.governance import AuditEvent, KPIEvent
from lpr_cpe.domain.records import DataQualityAssessment, EvidenceItem
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import check_budgets, escalation_update
from lpr_cpe.graph.state import IncidentState, bump_visit
from lpr_cpe.integrations.base import AdapterError, AdapterUnavailableError
from lpr_cpe.observability.kpi import KPICalculator, KPINotDerivableError
from lpr_cpe.policies.models import EvidencePolicy

#: What a node returns. Deliberately loose: a `TypedDict` of every field a node *might* write would
#: have to be `total=False` over the whole of `IncidentState`, which is what `IncidentState` already
#: is. The reducers, not the return type, are what enforce the contract.
NodeUpdate = dict[str, Any]

NodeBody = Callable[[IncidentState, GraphContext], Awaitable[NodeUpdate]]


class NodeCallable(Protocol):
    """A wrapped node. `node_name` is carried so `graph.builder` names the node from one source."""

    node_name: str

    def __call__(self, state: IncidentState) -> Awaitable[NodeUpdate]: ...


def _state_reducers() -> dict[str, Callable[[Any, Any], Any]]:
    """The reducer attached to each `IncidentState` field, read off the annotations.

    Read rather than restated. A hand-written copy of this mapping would be correct on the day it
    was written and wrong the first time somebody adds a field, and the way it would be wrong --
    `preview` silently using last-write-wins for a field that appends -- produces a plausible number
    rather than an error.
    """
    hints = get_type_hints(IncidentState, include_extras=True)
    out: dict[str, Callable[[Any, Any], Any]] = {}
    for name, hint in hints.items():
        for meta in getattr(hint, "__metadata__", ()):
            if callable(meta):
                out[name] = meta
    return out


_STATE_REDUCERS = _state_reducers()


def preview(state: IncidentState, update: NodeUpdate) -> IncidentState:
    """State as the reducers will leave it, for a node that must read its own writes.

    A node returns a partial mapping and LangGraph reduces it *after* the node finishes -- so a node
    computing something from a value it is in the middle of writing cannot simply read `state`. P06
    is the case that forces this: it stamps `TRIAGED_AT` and then emits `time_to_triage_seconds`,
    which is derived from that stamp.

    The obvious `{**state, **update}` is wrong here, and quietly. `metrics_timestamps` reduces with
    `merge_dict`, so a plain spread would replace the whole mapping with the single stamp just
    written and every earlier timestamp would vanish from the KPI's view. Applying the declared
    reducers is the only version of this that stays correct as the state contract grows.

    This does not touch the checkpoint. It is a local view for one computation; the update returned
    to LangGraph is still the partial mapping.
    """
    merged: dict[str, Any] = dict(state)
    for key, value in update.items():
        reducer = _STATE_REDUCERS.get(key)
        merged[key] = reducer(merged.get(key), value) if reducer is not None else value
    return cast(IncidentState, merged)


# ----------------------------------------------------------------------------------------------
# Deterministic identity
# ----------------------------------------------------------------------------------------------


def derive_id(prefix: str, *parts: object, length: int = 20) -> str:
    """A stable id for a fact, derived from what makes the fact distinct.

    Same construction as `guards._escalation_event_id`, generalised because every node needs it. The
    unit separator is not decorative: joining on `-` would let `("a-b", "c")` and `("a", "b-c")`
    collide, and those are different facts.

    Callers must include whatever distinguishes a *genuine* second occurrence from a replay of the
    first -- usually `diagnostic_cycles` or an attempt counter. Omitting it makes a real second
    observation invisible; including a clock reading makes every replay look like a new one. Both
    failures are silent, which is why this function takes the parts rather than guessing them.
    """
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:length]}"


def audit(
    state: IncidentState,
    ctx: GraphContext,
    *,
    node: str,
    action: str,
    outcome: str,
    subject_ref: str | None = None,
    reason_code: ReasonCode | None = None,
    detail: Mapping[str, Any] | None = None,
    discriminator: object = "",
) -> AuditEvent:
    """One audit event, keyed so a replay collapses into it rather than beside it.

    `discriminator` is the escape hatch for a node that legitimately records the same action twice
    in one incident -- a second diagnostic cycle, a second attempt. Pass the counter that separates
    them. Leaving it empty is correct for a node that acts once.
    """
    incident_id = state.get("incident_id") or ""
    return AuditEvent(
        event_id=derive_id("AUD", incident_id, node, action, discriminator),
        incident_id=incident_id,
        occurred_at=ctx.clock.now(),
        actor=ctx.automation_actor,
        action=action,
        node=node,
        subject_ref=subject_ref,
        reason_code=reason_code,
        outcome=outcome,
        detail=dict(detail or {}),
        policy_version=ctx.policy.policy_version,
        correlation_id=state.get("correlation_id", ""),
    )


def make_evidence(
    state: IncidentState,
    ctx: GraphContext,
    *,
    node: str,
    kind: EvidenceKind,
    subject_ref: str,
    summary: str,
    source_system: str,
    observed_at: datetime | None = None,
    payload: Mapping[str, Any] | None = None,
    object_ref: Mapping[str, Any] | None = None,
    trustworthiness: float = 1.0,
    discriminator: object = "",
) -> EvidenceItem:
    """One evidence item with a ref that survives replay.

    `EvidenceItem` derives its own ref from `(kind, subject_ref, observed_at)` when none is given,
    and that default is right for a caller holding a source's timestamp -- but a node that stamped
    `observed_at` from the clock would derive a *different* ref on every replay, and `evidence`
    reduces with `append_unique`. The same observation would appear twice, and RCA counts distinct
    evidence refs when it decides whether a hypothesis is corroborated. Two copies of one reading
    would look like two independent readings agreeing.

    So the ref is supplied here instead, from what actually distinguishes one observation from
    another: which incident, which node recorded it, of what, and a caller-supplied discriminator
    for when one node records several of a kind -- the peer alarm's id, the diagnostic cycle.
    `observed_at` is then free to be the honest instant without affecting identity.

    When `observed_at` is not given it is read from the payload, because every adapter here stamps
    one (measured) and the source's own reading of when it looked is better than ours of when we
    asked. The clock is the last resort, not the first.
    """
    incident_id = state.get("incident_id") or ""
    resolved_at = observed_at or _observed_at(payload) or ctx.clock.now()
    return EvidenceItem(
        ref=derive_id("EV", incident_id, node, kind.value, subject_ref, discriminator),
        kind=kind,
        subject_ref=subject_ref,
        observed_at=resolved_at,
        recorded_at=ctx.clock.now(),
        source_system=source_system,
        summary=summary,
        payload=dict(payload or {}),
        object_ref=dict(object_ref) if object_ref is not None else None,
        trustworthiness=trustworthiness,
    )


def emit_kpi(
    state: IncidentState,
    ctx: GraphContext,
    kpi_name: KPIName,
    *,
    node: str,
    dimensions: Mapping[str, str] | None = None,
    discriminator: object = "",
) -> list[KPIEvent]:
    """Emit one KPI, re-keyed to be replay-safe, or nothing if state cannot yet derive it.

    Two adjustments to `KPICalculator.emit`, both of which exist because a node is not the same
    caller as a reporting job:

    * **The id is replaced.** `emit` mints `new_id("KPI")`, which is `uuid4`, and `kpi_events` is
      de-duplicated on `event_id` -- so a node that interrupted and replayed would record the same
      measurement twice with two ids and double the numerator of every rate built from it. The
      derived key is the incident, the KPI and the node, which is exactly the granularity at which
      one node measuring one thing should appear once.
    * **`KPINotDerivableError` returns empty rather than propagating.** A KPI that state cannot yet
      support is a normal condition at a stage boundary, not a fault. The alternative -- every call
      site wrapping this in a `try` -- is how one forgotten `try` turns a reporting gap into a
      failed incident.

    `discriminator` is the same escape hatch `audit` carries, and for the same reason: a node the
    graph legitimately re-enters measures a *different* value the second time. `policy_block_rate`
    after the second diagnostic cycle's evaluation is not the same number as after the first's, and
    without a discriminator `append_unique` -- which is first-write-wins -- would keep the first and
    discard every later one. A node that runs once should leave this empty; one inside a loop should
    pass the counter that separates the passes.

    Returns a list so the caller can splice it into an update unconditionally: `"kpi_events":
    emit_kpi(...)` is correct whether or not anything was derivable.
    """
    calculator = KPICalculator(ctx.clock)
    try:
        event = calculator.emit(state, kpi_name, dimensions=dict(dimensions or {}))
    except KPINotDerivableError:
        return []
    incident_id = state.get("incident_id") or ""
    return [
        event.model_copy(
            update={"event_id": derive_id("KPI", incident_id, kpi_name, node, discriminator)}
        )
    ]


# ----------------------------------------------------------------------------------------------
# The decorator
# ----------------------------------------------------------------------------------------------


def node(name: str) -> Callable[[NodeBody], NodeCallable]:
    """Wrap a node body with the guard check, the visit record and the timestamp.

    The body is handed `(state, ctx)` and returns only what it changed. It never sees the runtime,
    never bumps its own counter and never needs to remember the guard -- which is the point: those
    three are the obligations most easily forgotten, and forgetting any of them fails silently.

    Ordering inside the returned mapping matters. `node_visits` and `updated_at` are written *first*
    so a body may override `updated_at` with the instant it actually observed something, rather than
    the instant it finished. `node_visits` is not overridable, because a node that could opt out of
    being counted could evade the loop guard.
    """

    def decorate(body: NodeBody) -> NodeCallable:
        @wraps(body)
        async def run(state: IncidentState) -> NodeUpdate:
            ctx = get_runtime(GraphContext).context
            verdict = check_budgets(state, ctx, node=name)
            if not verdict.within_budget:
                return {
                    **escalation_update(state, ctx, verdict, node=name),
                    "node_visits": bump_visit(state, name),
                }
            update = await body(state, ctx)
            return {
                "updated_at": ctx.clock.now(),
                **update,
                "node_visits": bump_visit(state, name),
            }

        run.node_name = name  # type: ignore[attr-defined]
        return cast(NodeCallable, run)

    return decorate


def check_node_registry(registry: Sequence[tuple[str, Any]], label: str) -> None:
    """Each registry key equals the name the `@node` decorator recorded on the callable.

    Called at import from every module that owns a registry -- the parent's `graph.nodes` and each
    subgraph -- rather than from a test, so that the mismatch cannot reach a running graph. The
    decorator stores the name it stamps on audit events as `node_name`; a node registered under a
    different string would produce a graph whose topology and whose audit trail disagree, and
    nothing at runtime would notice until somebody tried to trace an incident.

    This check has been seen to go red: written against `__node_name__`, which is not what the
    decorator calls the attribute, it reported all eleven parent nodes as mismatched on import.

    It is a function taking the registry rather than a copy per module because the *second* copy is
    where the drift starts. `label` is what makes the error name the registry that is wrong.
    """
    wrong = {
        name: getattr(fn, "node_name", None)
        for name, fn in registry
        if getattr(fn, "node_name", None) != name
    }
    if wrong:
        raise RuntimeError(
            f"{label} disagrees with the @node decorators: {wrong}. The key is the LangGraph node "
            "name and the decorator's name is what appears in the audit trail; they must be the "
            "same string."
        )
    counts = Counter(name for name, _ in registry)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        # `add_node` raises on a duplicate anyway, but only once a builder is run against a real
        # graph. A stage tuple pasted twice is a plausible edit and this says so on import.
        raise RuntimeError(f"{label} has duplicate names: {duplicates}")


# ----------------------------------------------------------------------------------------------
# Gathering from adapters
# ----------------------------------------------------------------------------------------------


class Freshness(StrEnum):
    """Which of the pack's age limits governs a source.

    The limits themselves stay in `policy.evidence`, where they are versioned; this enum is only the
    name a call site uses to select one. A node passing a raw number here would be a node with its
    own opinion about how old a measurement may be.
    """

    TELEMETRY = "telemetry"
    TOPOLOGY = "topology"
    SLA = "sla"
    DISPATCH = "dispatch"


def _age_limit(evidence: EvidencePolicy, freshness: Freshness) -> int:
    match freshness:
        case Freshness.TELEMETRY:
            return evidence.max_telemetry_age_minutes
        case Freshness.TOPOLOGY:
            return evidence.max_topology_age_minutes
        case Freshness.SLA:
            return evidence.max_sla_age_minutes
        case Freshness.DISPATCH:
            return evidence.max_age_for_dispatch_minutes


def _observed_at(payload: object) -> datetime | None:
    """The instant a payload says it was observed, if it says.

    Every simulated adapter stamps `observed_at` as an ISO string (measured). A payload without one
    is not treated as stale -- it is treated as not making a claim, which is different. Inventing a
    staleness verdict for it would put a flag on the incident that no measurement supports.
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("observed_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class Gathered:
    """Several adapter reads, and the reason each one is or is not usable.

    This is the only place in `graph/nodes/` that catches an adapter exception, and it is the reason
    `@node` does not. It knows the source's name, so it can turn a failure into a fact that names
    it; a `try` further out could only say "something failed".

    An adapter answer falls into exactly three states, and the distinction is load-bearing because
    the detectors and D01/D05 route on it:

    | State | How it arrives | Recorded as | Returned |
    | --- | --- | --- | --- |
    | usable | payload, `data_available` not False | nothing | the payload |
    | answered, unusable | payload, `data_available is False` | `MISSING_FIELD` | the payload |
    | could not answer | `AdapterUnavailableError` | `ADAPTER_UNAVAILABLE`, blocking | `None` |

    The middle row is why an unusable payload is still handed back rather than nulled. The adapter
    said *why* in `data_quality_notes`, `detectors.physical` reads `data_available` itself, and
    `DetectionContext` reserves `None` for "never fetched". Replacing an honest "the CPE is offline,
    here is the last cached value" with `None` would destroy both the value and the reason.

    A plain `AdapterError` -- retryable, one specific call that went wrong -- is *not* the system
    being unavailable, so it does not raise a blocking flag. Conflating them would let one bad
    reference quarantine an incident that has every other source it needs.
    """

    def __init__(self, ctx: GraphContext, *, assessed_at: datetime) -> None:
        self._evidence = ctx.policy.pack.evidence
        self._assessed_at = assessed_at
        self._attempted: list[str] = []
        self._usable: list[str] = []
        self._missing: list[str] = []
        self._stale: list[str] = []
        self._flags: list[DataQualityFlag] = []
        self._notes: list[str] = []

    # -- recording -----------------------------------------------------------------------------

    def _flag(self, flag: DataQualityFlag) -> None:
        if flag not in self._flags:
            self._flags.append(flag)

    def _note(self, text: str) -> None:
        if text and text not in self._notes:
            self._notes.append(text)

    def _record_success(self, name: str, payload: object, freshness: Freshness | None) -> None:
        usable = True
        if isinstance(payload, Mapping):
            if payload.get("data_available") is False:
                usable = False
                self._flag(DataQualityFlag.MISSING_FIELD)
                self._missing.append(name)
                reasons = payload.get("data_quality_notes") or []
                if isinstance(reasons, Iterable) and not isinstance(reasons, str | bytes):
                    for reason in reasons:
                        self._note(f"{name}: {reason}")
                else:
                    self._note(f"{name}: reported data_available=False without a stated reason")
            self._check_staleness(name, payload, freshness)
        if usable:
            self._usable.append(name)

    def _check_staleness(self, name: str, payload: object, freshness: Freshness | None) -> None:
        if freshness is None:
            return
        observed = _observed_at(payload)
        if observed is None:
            return
        limit = _age_limit(self._evidence, freshness)
        age = self._assessed_at - observed
        if age > timedelta(minutes=limit):
            self._flag(DataQualityFlag.STALE_DATA)
            self._stale.append(name)
            minutes = age.total_seconds() / 60.0
            self._note(
                f"{name}: observed {minutes:.1f} min ago, "
                f"limit {limit} min (policy.evidence.{freshness.value})"
            )

    def _record_failure(self, name: str, error: BaseException) -> None:
        self._missing.append(name)
        if isinstance(error, AdapterUnavailableError):
            self._flag(DataQualityFlag.ADAPTER_UNAVAILABLE)
            self._note(f"{name}: unavailable -- {error}")
        elif isinstance(error, AdapterError):
            self._flag(DataQualityFlag.MISSING_FIELD)
            self._note(f"{name}: call failed -- {error}")
        else:  # pragma: no cover -- re-raised by the callers, never stored
            raise error

    # -- fetching ------------------------------------------------------------------------------

    async def fetch[T](
        self, name: str, call: Awaitable[T], *, freshness: Freshness | None = None
    ) -> T | None:
        """Await one adapter call, recording what it did. `None` means it could not answer."""
        self._attempted.append(name)
        try:
            payload = await call
        except AdapterError as exc:
            self._record_failure(name, exc)
            return None
        self._record_success(name, payload, freshness)
        return payload

    async def gather(
        self,
        calls: Mapping[str, Awaitable[Any]],
        *,
        freshness: Freshness | None = None,
    ) -> dict[str, Any]:
        """Await several calls concurrently, recording each. Missing keys are absent, not `None`.

        Concurrency here is not a micro-optimisation. P07 assembles a case from nine or so sources
        and every one of them is a network round trip; run in sequence they compose into a
        multi-second node, and the loop guard's step budget is spent on wall-clock time rather than
        on work.

        A source that could not answer is **omitted from the result** rather than mapped to `None`,
        so `payloads.get(name)` and `payloads[name] is None` stay distinguishable at the call site.
        `return_exceptions=True` is what makes one failure not cancel the other eight -- the default
        would let a single unavailable adapter erase the evidence that *was* obtainable.
        """
        names = list(calls)
        self._attempted.extend(names)
        results = await asyncio.gather(*(calls[name] for name in names), return_exceptions=True)
        out: dict[str, Any] = {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, AdapterError):
                self._record_failure(name, result)
            elif isinstance(result, BaseException):
                raise result
            else:
                self._record_success(name, result, freshness)
                out[name] = result
        return out

    # -- reporting -----------------------------------------------------------------------------

    def add_flag(self, flag: DataQualityFlag, note: str = "") -> None:
        """Record a defect the fetches could not see -- a topology that contradicts itself, say."""
        self._flag(flag)
        self._note(note)

    def add_note(self, note: str) -> None:
        self._note(note)

    @property
    def attempted(self) -> int:
        return len(self._attempted)

    @property
    def usable(self) -> int:
        return len(self._usable)

    @property
    def usable_sources(self) -> list[str]:
        """The names that answered usably -- what `policy.evidence.min_sources_*` counts."""
        return list(self._usable)

    @property
    def completeness_score(self) -> float:
        """Usable answers over attempts. `1.0` when nothing was attempted.

        An empty gather scoring 1.0 rather than 0.0 is deliberate: a node that needed nothing has
        not failed to obtain anything. The *blocking flags* are what stop an incident with no
        evidence, and they are raised by failures, not by arithmetic. Scoring 0.0 here would make
        `sufficient_for_action` false for a node that asked no questions.
        """
        if not self._attempted:
            return 1.0
        return len(self._usable) / len(self._attempted)

    def assessment(
        self,
        *,
        previous: DataQualityAssessment | None = None,
    ) -> DataQualityAssessment:
        """Fold this gather into an assessment, carrying forward anything already known.

        `previous` exists because P03, P05 and P07 each gather, and the second one must not erase
        the first one's findings by writing a fresh assessment over the top -- `data_quality` is a
        plain field with last-write-wins, not an append-only list. Flags and notes union; the
        completeness score is the **lower** of the two, because a stage that found half of what it
        needed does not become complete by a later stage finding all of what *it* needed.
        """
        flags = list(self._flags)
        missing = list(self._missing)
        stale = list(self._stale)
        notes = list(self._notes)
        score = self.completeness_score
        if previous is not None:
            flags = [f for f in previous.flags if f not in flags] + flags
            missing = [m for m in previous.missing_sources if m not in missing] + missing
            stale = [s for s in previous.stale_sources if s not in stale] + stale
            notes = [n for n in previous.notes if n not in notes] + notes
            score = min(score, previous.completeness_score)
        return DataQualityAssessment(
            assessed_at=self._assessed_at,
            flags=flags,
            missing_sources=missing,
            stale_sources=stale,
            notes=notes,
            completeness_score=score,
        )
