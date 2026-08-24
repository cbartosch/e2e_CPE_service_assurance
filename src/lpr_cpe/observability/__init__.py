"""Structured logging, tracing and KPI instrumentation -- what makes a run auditable afterwards.

One owner per concern, and each designed so that the *easy* path is the correct one:

* **`logging`** -- structlog, with `security.redaction` installed as a processor. A caller cannot
  emit an unmasked MAC through this logger even by accident, because the masking is in the pipeline
  rather than in a rule people are asked to remember.
* **`tracing`** -- the specification's 17 trace attributes, enumerated once in `TraceAttr`.
  OpenTelemetry is an optional extra: when it is absent or disabled, `configure_tracing` reports why
  and `span` degrades to a no-op that still runs the body. Nothing in the graph may depend on a span
  existing.
* **`kpi`** -- every KPI *derived* from `IncidentState`. Values are never hard-coded, rates always
  carry their numerator and denominator so aggregation is `sum(num)/sum(den)`, and the KPIs an
  incident's state genuinely cannot produce are named in `NOT_DERIVABLE_FROM_STATE` rather than
  faked.

The three are deliberately not coupled: a node that logs does not have to trace, and KPI emission
works with tracing off. An observability layer that has to be fully wired to be useful is an
observability layer that gets switched off in the environment where it matters most.
"""

from lpr_cpe.observability.kpi import (
    NOT_DERIVABLE_FROM_STATE,
    SPEC_KPIS_WITHOUT_ENUM_MEMBER,
    UNIT_COUNT,
    UNIT_RATE,
    UNIT_SECONDS,
    KPICalculator,
    KPINotDerivableError,
    KPIValue,
    MetricTimestamp,
    mark,
    stamp,
)
from lpr_cpe.observability.logging import (
    bind_context,
    bind_incident,
    clear_context,
    configure_logging,
    get_logger,
    is_configured,
    reset_logging_for_tests,
)
from lpr_cpe.observability.tracing import (
    REQUIRED_TRACE_ATTRS,
    TraceAttr,
    approval_state,
    attempt_counts,
    attributes_from_state,
    configure_langsmith,
    configure_tracing,
    reset_tracing_for_tests,
    selected_lane,
    span,
    sync_span,
    tracing_enabled,
    tracing_status,
)

__all__ = [
    "NOT_DERIVABLE_FROM_STATE",
    "REQUIRED_TRACE_ATTRS",
    "SPEC_KPIS_WITHOUT_ENUM_MEMBER",
    "UNIT_COUNT",
    "UNIT_RATE",
    "UNIT_SECONDS",
    "KPICalculator",
    "KPINotDerivableError",
    "KPIValue",
    "MetricTimestamp",
    "TraceAttr",
    "approval_state",
    "attempt_counts",
    "attributes_from_state",
    "bind_context",
    "bind_incident",
    "clear_context",
    "configure_langsmith",
    "configure_logging",
    "configure_tracing",
    "get_logger",
    "is_configured",
    "mark",
    "reset_logging_for_tests",
    "reset_tracing_for_tests",
    "selected_lane",
    "span",
    "stamp",
    "sync_span",
    "tracing_enabled",
    "tracing_status",
]
