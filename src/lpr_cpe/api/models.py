"""Request and response bodies. Thin, and deliberately not the domain models.

The domain models are validated records with derived properties and refusal rules --
`ClosureRecord` refuses to construct without proof, `ActionRequest` refuses an approval-requiring
outcome with no `approval_ref`. Those are the right shapes *inside* the graph and the wrong ones on
the wire: a caller posting an event should not have to satisfy `AssuranceEvent`'s validators to be
told its id is malformed, and a response that serialised a domain model would publish every field
the model happens to carry, including the ones redaction has to remove.

So the bodies here are the smallest thing each endpoint needs, and `api.app` maps them onto the
domain. The one place that rule bends is `ResumePayload.value`, which is `Any` on purpose: the six
gates take six different answer shapes, and `graph.subgraphs` owns each parser. A model here would
be a seventh opinion about what a crew report looks like.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lpr_cpe.domain.enums import ApprovalStatus, CaseType, EventSource, Severity, Technology


class _Body(BaseModel):
    """Reject unknown fields rather than ignoring them.

    A typo in a request body is a caller bug, and accepting it silently means the caller believes
    they set something they did not. `extra="forbid"` turns that into a 422 naming the field.
    """

    model_config = ConfigDict(extra="forbid")


class EventIn(_Body):
    """An assurance event arriving from a monitoring system. What `POST /events` takes."""

    event_id: str = Field(min_length=1, max_length=128)
    source: EventSource
    case_type: CaseType
    technology: Technology
    severity: Severity
    service_ref: str = Field(min_length=1, max_length=128)
    customer_ref: str | None = None
    cpe_ref: str | None = None
    summary: str = Field(default="", max_length=2000)

    #: Optional so a caller may set the thread id, which `D1` makes the incident id. Absent, the
    #: service reference derives it -- one incident per service in flight, which is what
    #: `deduplicate_and_correlate` assumes when it looks for a parent.
    incident_id: str | None = Field(default=None, max_length=128)


class ApprovalIn(_Body):
    """A human's answer to a gate. The shape `graph.interrupts` parses on resume.

    `decided_by_role` is required and is not decoration: `security.rbac.can_approve` refuses a role
    that may not approve this kind, and an approval endpoint that let the caller omit the role would
    be an approval endpoint with no authorisation in it.
    """

    status: ApprovalStatus
    decided_by: str = Field(min_length=1, max_length=128)
    decided_by_role: str = Field(min_length=1, max_length=64)
    rationale: str = Field(default="", max_length=2000)

    @field_validator("status")
    @classmethod
    def _must_be_decided(cls, value: ApprovalStatus) -> ApprovalStatus:
        """`pending` is not an answer, and posting one would resume the gate with a non-decision."""
        if value is ApprovalStatus.PENDING:
            raise ValueError(
                "an approval must be decided: post `approved` or `rejected`. Posting `pending` "
                "would resume the gate with a value that answers nothing."
            )
        return value


class CustomerResponseIn(_Body):
    """A customer's reply to a self-help instruction. Parsed by `self_help.customer_reply`."""

    response: Literal["completed", "declined"]
    customer_completed_step: bool | None = None


class ResumePayload(_Body):
    """The generic resume. `value` is whatever the paused gate's own parser expects.

    Typed `Any` deliberately -- see the module docstring. What this model *does* enforce is that
    `value` is present at all, which is the one thing no downstream parser can check for itself:
    `Command(resume={})` is read by LangGraph as a map that resumes nothing, and the graph re-pauses
    silently. See `api.app.resume_incident`.
    """

    value: Any

    @field_validator("value")
    @classmethod
    def _not_an_empty_mapping(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value:
            raise ValueError(
                "an empty mapping resumes nothing. LangGraph reads `{}` as an interrupt-id map "
                "with no entries -- `all()` over an empty dict is True -- so the graph re-pauses "
                "having run no node and written no audit event. Send the gate's own answer shape, "
                "or a non-empty value."
            )
        return value


class WebhookIn(_Body):
    """One inbound notification from an external system.

    `delivery_id` is what makes processing idempotent, and it is required rather than derived: the
    specification asks for duplicate suppression, and a hash of the body would suppress two
    genuinely distinct notifications that happened to be identical -- two alarms of the same
    kind on the same object, which is exactly what a flapping fault produces.
    """

    delivery_id: str = Field(min_length=1, max_length=128)
    incident_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentAccepted(BaseModel):
    """What a thread-starting call returns. The id is the thread id; D1 makes them the same."""

    incident_id: str
    status: str
    awaiting_human: bool


class ResumeResult(BaseModel):
    """What a resume returns: whether the graph moved, and where it is now."""

    incident_id: str
    status: str
    awaiting_human: bool
    resumed: bool


class WebhookResult(BaseModel):
    """Whether this delivery did anything, or was a duplicate of one already seen."""

    delivery_id: str
    accepted: bool
    duplicate: bool
    detail: str = ""


class TimelineEntry(BaseModel):
    """One audit event, flattened. Redacted before it leaves the process."""

    event_id: str
    occurred_at: str
    node: str | None
    action: str
    outcome: str
    actor: str
    reason_code: str | None


__all__ = [
    "ApprovalIn",
    "CustomerResponseIn",
    "EventIn",
    "IncidentAccepted",
    "ResumePayload",
    "ResumeResult",
    "TimelineEntry",
    "WebhookIn",
    "WebhookResult",
]
