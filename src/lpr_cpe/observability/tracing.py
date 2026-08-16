"""OpenTelemetry-compatible tracing, with the otel packages treated as a genuinely optional extra.

Two properties this module is built for:

**A missing extra degrades, it does not crash.** `opentelemetry-*` lives in the `otel` extra
(`pyproject.toml`) and is not installed in the default development environment. Every import of it
is therefore inside a function, inside a `try`, and a failure sets up a no-op tracer instead of
propagating. The alternative -- a module-scope `import opentelemetry` -- would mean `import
lpr_cpe.api` fails on a machine that never asked for tracing, which is how an optional dependency
becomes a required one in practice.

**No unredacted PII reaches a tracing backend.** Trace attributes are strings that get shipped to a
third-party system and retained there, usually with looser access control than our own database. So
`span()` routes every attribute through `security.redaction.redact` and refuses any value that still
looks like an identifier afterwards. That refusal is a backstop against a regression in the masker,
not the primary control.

The 17 attributes the specification's Observability section requires are enumerated once, in
`TraceAttr`. Nothing in this package writes a trace attribute as a string literal: a typo'd
`incident.id` instead of `incident_id` produces a span that looks fine and cannot be joined to
anything.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.security.redaction import MASK, looks_like_pii, redact

# --------------------------------------------------------------------------------------------
# The 17 attributes
# --------------------------------------------------------------------------------------------


class TraceAttr(StrEnum):
    """The trace attributes the specification requires, named once.

    The list is the specification's, in its order, one member each -- 17 in total. Two shapes are
    plural because the underlying fact is (`work_order_ids`, `detector_versions`) and collapsing
    them to a single value would lose the second visit or the second detector, which is usually the
    interesting one.

    `dotted.names` rather than bare words: OpenTelemetry's convention is a namespaced key, and
    `incident.id` cannot collide with an attribute some auto-instrumentation library also calls
    `id`.
    """

    INCIDENT_ID = "lpr.incident.id"
    CORRELATION_ID = "lpr.correlation.id"
    TECHNOLOGY = "lpr.technology"
    SOURCE = "lpr.source"
    AREA_ARCHETYPE = "lpr.pr.archetype"
    WORKFLOW_STAGE = "lpr.workflow.stage"
    NODE = "lpr.workflow.node"
    FAULT_DOMAIN = "lpr.fault.domain"
    SELECTED_LANE = "lpr.resolution.lane"
    ATTEMPT_COUNTS = "lpr.attempts"
    APPROVAL_STATE = "lpr.approval.state"
    WORK_ORDER_IDS = "lpr.work_order.ids"
    MR_IDS = "lpr.mr.ids"
    POLICY_VERSION = "lpr.policy.version"
    DETECTOR_VERSIONS = "lpr.detector.versions"
    MODEL_VERSION = "lpr.model.version"
    OUTCOME = "lpr.outcome"


REQUIRED_TRACE_ATTRS: Final[tuple[TraceAttr, ...]] = tuple(TraceAttr)
"""All 17, as a tuple. A test asserts `len(...) == 17` against the specification's list."""

_MAX_ATTR_CHARS: Final = 1024
"""Per-attribute cap. Tracing backends truncate silently at their own limits; truncating here means
the truncation is ours, marked, and the same in every backend."""

_REFUSED: Final = "[REFUSED_UNREDACTED_PII]"
_TRUNCATED: Final = "...[TRUNCATED]"

# --------------------------------------------------------------------------------------------
# Module state
# --------------------------------------------------------------------------------------------

_tracer: Any = None
_enabled: bool = False
_unavailable_reason: str = "tracing not configured"


class _NoopSpan:
    """A span that records nothing, with the same surface as a real one.

    Exists so a caller never has to write `if span is not None`. A conditional at every call site is
    how half of them end up missing the attribute that mattered.
    """

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def set_status_error(self, description: str) -> None:
        return None


class _OtelSpan:
    """Thin wrapper over an otel span, so callers never touch the otel API directly.

    The wrapper is not decoration: it is where redaction is enforced. A caller holding a raw otel
    span could call `set_attribute` with anything, and the masking in `span()` would be bypassed.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        for k, v in _safe_attributes({key: value}).items():
            self._span.set_attribute(k, v)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        for k, v in _safe_attributes(attributes).items():
            self._span.set_attribute(k, v)

    def record_exception(self, exc: BaseException) -> None:
        """Record the failure with the message masked.

        Deliberately does **not** call otel's own `record_exception`. That helper derives
        `exception.message` and `exception.stacktrace` straight from the exception object, with no
        hook to mask what it extracts -- and an adapter error routinely quotes the device it could
        not reach ("no response from CPE AA:BB:CC:DD:EE:FF"). The specification's rule is that no
        unredacted PII reaches a tracing backend, so the event is built here instead and the message
        goes through `_safe_attributes` like any other value.

        The stack trace is omitted rather than masked. It is the one field that is a verbatim dump
        of source lines, and masking it would mean trusting the shape-based masker over a body of
        text it was not designed for; the exception type plus the masked message is what an operator
        actually reads off a span, and the full trace is in the log stream, where the structlog
        processor has already masked it.
        """
        self._span.add_event(
            "exception",
            attributes=_safe_attributes(
                {"exception.type": type(exc).__name__, "exception.message": str(exc)}
            ),
        )
        self.set_status_error(str(exc))

    def set_status_error(self, description: str) -> None:
        try:
            from opentelemetry.trace import Status, StatusCode
        except ImportError:  # pragma: no cover - only when the extra vanishes mid-process
            return
        self._span.set_status(Status(StatusCode.ERROR, str(redact(description))))


SpanHandle = _NoopSpan | _OtelSpan
"""What `span()` yields. Both members share the surface a node needs, so no call site branches."""

_NOOP_SPAN: Final = _NoopSpan()


# --------------------------------------------------------------------------------------------
# Attribute coercion
# --------------------------------------------------------------------------------------------


#: What a tracing backend will accept for one attribute. Named once because the union appears in
#: `_coerce`, in `_safe_attributes` and in the span wrappers, and three copies of it would drift.
type AttrValue = str | bool | int | float | tuple[str, ...]


def _coerce(value: Any) -> AttrValue | None:
    """Reduce a Python value to something a tracing backend accepts.

    OpenTelemetry attributes may be a primitive or a homogeneous sequence of primitives. A dict is
    JSON-encoded rather than dropped, because `attempt_counts` is naturally a mapping and flattening
    it into five separate attributes would put five near-identical keys in every span.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return json.dumps({str(k): _json_safe(v) for k, v in value.items()}, sort_keys=True)
    if isinstance(value, Sequence | set | frozenset):
        return tuple(str(v) for v in value)
    return str(value)


def _json_safe(value: Any) -> Any:
    """Best-effort JSON-encodable rendering, for the mapping branch of `_coerce`."""
    if isinstance(value, bool | int | float | str | type(None)):
        return value
    return str(value)


def _safe_attributes(attrs: Mapping[str, Any]) -> dict[str, AttrValue]:
    """Redact, coerce, cap and (if necessary) refuse a set of attributes.

    A string value that still satisfies `looks_like_pii` after redaction is replaced with
    `[REFUSED_UNREDACTED_PII]` rather than raising. Raising would mean a masker regression takes the
    incident down instead of degrading one span, and an operator staring at a stalled graph is worse
    off than one staring at a span with a refused attribute -- the refusal is loud enough to find.
    Anything dropped this way is a bug in `security.redaction`, and the marker is what makes it
    findable.
    """
    out: dict[str, AttrValue] = {}
    for key, raw in attrs.items():
        coerced = _coerce(redact(raw))
        if coerced is None:
            # A missing value is omitted, not sent as "None". An attribute present with the string
            # "None" is indistinguishable from one whose value really is the word None.
            continue
        if isinstance(coerced, str):
            if _still_looks_unmasked(coerced):
                coerced = _REFUSED
            elif len(coerced) > _MAX_ATTR_CHARS:
                coerced = coerced[: _MAX_ATTR_CHARS - len(_TRUNCATED)] + _TRUNCATED
        elif isinstance(coerced, tuple):
            coerced = tuple(_REFUSED if _still_looks_unmasked(v) else v for v in coerced)
        out[str(key)] = coerced
    return out


def _still_looks_unmasked(value: str) -> bool:
    """Does this string look like PII that the masker did not touch?

    `looks_like_pii` alone is not the right test, because `mask_email` deliberately keeps the domain
    and the outer characters of the local part: `m****a@example.com` is correctly masked and still
    matches the e-mail shape. Refusing it would throw away the one thing the masked form is for --
    correlating two spans about the same customer -- on a value that is already safe.

    So the refusal needs both conditions: PII-shaped *and* carrying no mask marker. Every masker in
    `security.redaction` leaves `**` behind (`AA:BB:CC:**:**:**`, `192.168.**.**`, `**-**-**42`,
    `m****a@example.com`), so the marker's absence is what distinguishes a value the masker missed
    from one it handled. A genuinely unmasked address has no `**` in it, and that is the case this
    guard exists to catch.
    """
    return looks_like_pii(value) and MASK not in value


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


def configure_tracing(settings: Settings | None = None) -> bool:
    """Set up OTLP tracing if it is both switched on and installed. Returns whether it is live.

    Both conditions are required, and the second is checked by *attempting the import*, not by
    inspecting a version list. Returns `False` -- and leaves a no-op tracer in place -- when the
    extra is absent, when the endpoint is empty, or when the SDK raises during setup. Never raises:
    a telemetry dependency that can prevent the service from starting is a telemetry dependency that
    will prevent the service from starting, at 03:00, for a reason nobody expects.

    Idempotent: a second call with tracing already live is a no-op, for the same uvicorn-reloader
    reason documented in `observability.logging`.
    """
    global _tracer, _enabled, _unavailable_reason

    resolved = settings if settings is not None else get_settings()
    if not resolved.otel_enabled:
        _tracer, _enabled = None, False
        _unavailable_reason = "otel_enabled is False"
        return False
    if _enabled and _tracer is not None:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # The documented degradation. `pip install '.[otel]'` is the fix, and the message says so
        # rather than leaving an operator to infer it from an ImportError in a log.
        _tracer, _enabled = None, False
        _unavailable_reason = (
            f"opentelemetry is not installed ({exc}); install the 'otel' extra to enable tracing"
        )
        return False

    try:
        resource = Resource.create(
            {
                "service.name": "lpr-cpe-assurance",
                "deployment.environment": resolved.environment.value,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = _build_exporter(resolved.otel_endpoint)
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("lpr_cpe")
        _enabled = True
        _unavailable_reason = ""
        return True
    except Exception as exc:  # noqa: BLE001 -- telemetry setup must never take the process down
        # An unreachable collector, a malformed endpoint or a version-skewed SDK are all reasons to
        # run without tracing, not reasons to refuse to diagnose incidents. The reason is recorded
        # so `/health` can report degraded telemetry rather than the operator discovering empty
        # traces.
        _tracer, _enabled = None, False
        _unavailable_reason = f"tracing setup failed: {type(exc).__name__}: {exc}"
        return False


def _build_exporter(endpoint: str) -> Any:
    """The OTLP exporter, or `None` to keep spans in-process.

    An empty endpoint is a legitimate configuration: it produces spans that instrumentation can read
    in-process (and that a test can assert on) without shipping anything anywhere.
    """
    if not endpoint.strip():
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        return None
    return OTLPSpanExporter(endpoint=endpoint)


def tracing_enabled() -> bool:
    """Whether spans are actually being recorded. Read by `GET /ready`."""
    return _enabled


def tracing_status() -> str:
    """`""` when live, otherwise why not. So the readiness endpoint can say something useful."""
    return "" if _enabled else _unavailable_reason


def reset_tracing_for_tests() -> None:
    """Drop the tracer. Leaves otel's global provider, which otel itself refuses to replace."""
    global _tracer, _enabled, _unavailable_reason
    _tracer, _enabled = None, False
    _unavailable_reason = "tracing not configured"


def configure_langsmith(settings: Settings | None = None) -> bool:
    """Switch LangSmith tracing on through its environment variables. Returns whether it is on.

    LangSmith is configured by environment variable and not by an API call, so this function's whole
    job is to set `LANGSMITH_TRACING` -- and, importantly, to *not* set the API key. A key belongs
    in the deployment's secret store and reaches the process as
    `LANGCHAIN_API_KEY`/`LANGSMITH_API_KEY` already; a function that took one as an argument would
    invite it into a config file.

    A no-op when `settings.langsmith_enabled` is False, including explicitly setting the variable to
    `"false"`: a stale `LANGSMITH_TRACING=true` in a shell profile would otherwise silently ship
    prompts to a third party from a deployment whose settings say tracing is off.
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.langsmith_enabled:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault("LANGSMITH_PROJECT", f"lpr-cpe-{resolved.environment.value}")
    return True


# --------------------------------------------------------------------------------------------
# Spans
# --------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def span(name: str, **attrs: Any) -> AsyncIterator[SpanHandle]:
    """An async span. A no-op with the same surface when tracing is off.

    Async because the things worth tracing here are async: an adapter call, a node, a model call.
    The body is synchronous -- there is no `await` inside -- but an async context manager is what an
    `async def` node can use with `async with`, and offering a sync one would force half the call
    sites to use the wrong idiom.

    Every attribute passes through `redact` (see `_safe_attributes`). An exception escaping the body
    is recorded and re-raised: swallowing it here would turn a failure into a successful span, which
    is the one thing worse than no span at all.
    """
    if not _enabled or _tracer is None:
        yield _NOOP_SPAN
        return
    with _tracer.start_as_current_span(name) as raw:
        handle = _OtelSpan(raw)
        handle.set_attributes(attrs)
        try:
            yield handle
        except Exception as exc:
            handle.record_exception(exc)
            handle.set_status_error(f"{type(exc).__name__}: {exc}")
            raise


@contextlib.contextmanager
def sync_span(name: str, **attrs: Any) -> Iterator[SpanHandle]:
    """The synchronous twin, for the deterministic layers (detectors, scoring, policy engine).

    Those are pure synchronous functions and wrapping them in an event loop to get a span would be
    absurd. Same redaction, same no-op behaviour.
    """
    if not _enabled or _tracer is None:
        yield _NOOP_SPAN
        return
    with _tracer.start_as_current_span(name) as raw:
        handle = _OtelSpan(raw)
        handle.set_attributes(attrs)
        try:
            yield handle
        except Exception as exc:
            handle.record_exception(exc)
            handle.set_status_error(f"{type(exc).__name__}: {exc}")
            raise


# --------------------------------------------------------------------------------------------
# Building the 17 from state
# --------------------------------------------------------------------------------------------


def attributes_from_state(
    state: Mapping[str, Any],
    *,
    node: str = "",
    workflow_stage: str = "",
    outcome: str = "",
) -> dict[str, Any]:
    """Assemble the 17 required attributes from `IncidentState`.

    One function so that every span carries the same attribute set derived the same way. A node that
    built its own dict would omit whichever attribute it happened not to need, and the omission
    would only be noticed by whoever later tried to filter on it.

    `node`, `workflow_stage` and `outcome` are arguments rather than state reads because they are
    properties of the *span*, not of the incident: the same state produces a `diagnose` span and a
    `dispatch` span, and only the caller knows which one it is in.

    Absent facts are omitted rather than defaulted. `_safe_attributes` drops `None`, so a span for
    an incident with no work orders has no `work_order.ids` attribute at all -- which is a queryable
    fact, whereas `""` is not.
    """
    attrs: dict[str, Any] = {
        TraceAttr.INCIDENT_ID.value: state.get("incident_id"),
        TraceAttr.CORRELATION_ID.value: state.get("correlation_id"),
        TraceAttr.TECHNOLOGY.value: state.get("technology"),
        TraceAttr.SOURCE.value: state.get("source"),
        TraceAttr.AREA_ARCHETYPE.value: _area_archetype(state),
        TraceAttr.WORKFLOW_STAGE.value: workflow_stage or None,
        TraceAttr.NODE.value: node or None,
        TraceAttr.FAULT_DOMAIN.value: state.get("fault_domain"),
        TraceAttr.SELECTED_LANE.value: selected_lane(state),
        TraceAttr.ATTEMPT_COUNTS.value: attempt_counts(state),
        TraceAttr.APPROVAL_STATE.value: approval_state(state),
        TraceAttr.WORK_ORDER_IDS.value: sorted(
            {wo.work_order_id for wo in state.get("work_orders", [])}
        )
        or None,
        TraceAttr.MR_IDS.value: sorted({mr.mr_id for mr in state.get("mr_records", [])}) or None,
        TraceAttr.POLICY_VERSION.value: _policy_version(state),
        TraceAttr.DETECTOR_VERSIONS.value: _detector_versions(state),
        TraceAttr.MODEL_VERSION.value: _model_version(state),
        TraceAttr.OUTCOME.value: outcome or _outcome(state),
    }
    return attrs


def _area_archetype(state: Mapping[str, Any]) -> str | None:
    topology = state.get("topology")
    archetype = getattr(topology, "area_archetype", None)
    return str(archetype) if archetype is not None else None


def selected_lane(state: Mapping[str, Any]) -> str | None:
    """Which resolution lane this incident is in.

    Derived, because the state contract has no `selected_lane` field. The derivation is ordered from
    most to least specific: an incident that went to plant repair is in the plant lane even though
    it also had a remote attempt and a field visit on the way.

    If a `selected_lane` field is ever added to `IncidentState`, this function should read it and
    stop deriving -- the derivation is an inference about history and the field would be a statement
    of intent, and where they disagree the intent is right.
    """
    if state.get("mr_records"):
        return "plant"
    if state.get("work_orders"):
        return "field"
    if state.get("self_help_session") is not None:
        return "self_help"
    if state.get("remote_actions") or state.get("remote_attempt_count", 0):
        return "remote"
    if state.get("rca") is not None:
        return "diagnosis"
    return None


def attempt_counts(state: Mapping[str, Any]) -> dict[str, int] | None:
    """Every attempt counter, as one attribute.

    One JSON attribute rather than a key each: they are read together, and a span with seven
    near-identical attribute names is harder to scan, not easier. The last two are the pair the
    bounded-loop guard bounds, and both are here for that reason -- an operator reading a
    `resolution_cycles` escalation needs to see the number the guard saw.
    """
    counts = {
        "remote": int(state.get("remote_attempt_count", 0) or 0),
        "self_help": int(state.get("self_help_attempt_count", 0) or 0),
        "field": int(state.get("field_visit_count", 0) or 0),
        "mr": int(state.get("mr_attempt_count", 0) or 0),
        "plant": int(state.get("plant_attempt_count", 0) or 0),
        "diagnostic_cycles": int(state.get("diagnostic_cycles", 0) or 0),
        "resolution_cycles": int(state.get("resolution_cycles", 0) or 0),
    }
    return counts if any(counts.values()) else None


def approval_state(state: Mapping[str, Any]) -> str | None:
    """`pending:<kind>` while an interrupt is open, otherwise the last decision, otherwise `None`.

    The pending case takes precedence over the history: an incident with three granted approvals and
    a fourth outstanding is *waiting*, and a span reporting `approved` for it would be actively
    misleading to whoever is trying to work out why nothing is moving.
    """
    pending = state.get("pending_approval")
    if pending is not None:
        kind = getattr(pending, "kind", None)
        return f"pending:{kind}" if kind is not None else "pending"
    approvals = state.get("approvals", [])
    if approvals:
        last = approvals[-1]
        return f"{last.status}:{last.kind}"
    return None


def _policy_version(state: Mapping[str, Any]) -> str | None:
    decisions = state.get("policy_decisions", [])
    if not decisions:
        return None
    # The most recent decision's pack version. Distinct versions across one incident are possible (a
    # pack can be reloaded mid-incident) and the audit trail on each `PolicyDecision` is where that
    # is reconstructed; a span carries the version in force now.
    return str(decisions[-1].policy_version)


def _detector_versions(state: Mapping[str, Any]) -> tuple[str, ...] | None:
    findings = state.get("anomaly_findings", [])
    versions = sorted({f"{f.detector_name}@{f.detector_version}" for f in findings})
    return tuple(versions) or None


def _model_version(state: Mapping[str, Any]) -> str | None:
    prediction = state.get("prediction")
    if prediction is None:
        return None
    name = getattr(prediction, "model_name", "")
    version = getattr(prediction, "model_version", "")
    return f"{name}@{version}" if name or version else None


def _outcome(state: Mapping[str, Any]) -> str | None:
    """The incident's outcome so far: the closure code if it closed, else the status.

    Not `""` for an open incident. An open incident has no outcome, and reporting one is how a
    dashboard comes to believe that an incident in `diagnosing` was resolved.
    """
    closure = state.get("closure")
    if closure is not None:
        return str(closure.closure_code)
    if state.get("escalated"):
        return "escalated"
    status = state.get("status")
    return str(status) if status is not None else None
