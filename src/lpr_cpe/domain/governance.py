"""Policy decisions, approvals, the typed action envelope, and audit events.

`ActionRequest` is the choke point. Every effect the system has on the outside world is one of
these, and the model requires the six fields the specification demands -- incident id, idempotency
key, actor, reason code, approval ref, correlation id -- so an adapter *cannot* be written that
forgets one. The approval ref is conditionally required: a model validator refuses an action whose
policy decision said `requires_approval` unless an approval ref is present. That is the difference
between a rule and a convention.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from lpr_cpe.domain.base import FrozenDomainModel
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    PolicyOutcome,
    ReasonCode,
)


class PolicyDecision(FrozenDomainModel):
    """The engine's verdict on one requested action.

    `policy_version` is not decoration. An action taken under version 3 of the pack and reviewed
    after version 4 shipped is otherwise impossible to explain, and "the rules changed" is the most
    common true explanation for a decision that looks wrong in hindsight.
    """

    decision_id: str
    decided_at: datetime
    action_type: ActionType
    outcome: PolicyOutcome
    reason_codes: tuple[ReasonCode, ...] = ()
    explanation: str = ""
    policy_version: str
    matched_rule: str = ""
    required_approval_kind: ApprovalKind | None = None
    required_role: str = ""
    constraints: dict[str, Any] = Field(default_factory=dict)
    evaluated_inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _outcome_is_justified(self) -> Self:
        # Every non-allow must say why, in machine-readable form. A block with no reason code is a
        # block nobody can appeal and nobody can audit.
        if self.outcome is not PolicyOutcome.ALLOWED and not self.reason_codes:
            raise ValueError(f"outcome={self.outcome} requires at least one reason code")
        if self.outcome is PolicyOutcome.REQUIRES_APPROVAL and self.required_approval_kind is None:
            raise ValueError(
                "REQUIRES_APPROVAL without required_approval_kind: the graph would not know which "
                "interrupt to raise"
            )
        return self

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOWED

    @property
    def blocked(self) -> bool:
        return self.outcome is PolicyOutcome.BLOCKED


class ApprovalRequest(FrozenDomainModel):
    """What a human is being asked, and what they need in order to answer.

    `context` carries the evidence summary rather than a pointer to it, because the operator sees
    this payload at the interrupt and a pointer they have to go and dereference is a pointer they
    will approve without reading. It is a *summary*: redaction has already run over it
    (`security.redaction`), so it holds no customer identifiers.
    """

    approval_id: str
    incident_id: str
    kind: ApprovalKind
    requested_at: datetime
    expires_at: datetime | None = None
    action_type: ActionType | None = None
    target_ref: str | None = None
    required_role: str = "noc_operator"

    question: str = Field(min_length=1)
    recommendation: str = ""
    risk_summary: str = ""
    blast_radius: int | None = Field(default=None, ge=0)
    reversible: bool = True
    policy_decision_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requested_at", "expires_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return v

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now > self.expires_at

    def to_interrupt_payload(self) -> dict[str, Any]:
        """The exact value handed to `langgraph.types.interrupt()`.

        One owner for this shape: the API's pending-approval view reads it back out of
        `snapshot.interrupts`, so if the node built the payload ad hoc the two would drift.
        """
        return {
            "approval_id": self.approval_id,
            "incident_id": self.incident_id,
            "kind": self.kind.value,
            "question": self.question,
            "recommendation": self.recommendation,
            "risk_summary": self.risk_summary,
            "action_type": self.action_type.value if self.action_type else None,
            "target_ref": self.target_ref,
            "blast_radius": self.blast_radius,
            "reversible": self.reversible,
            "required_role": self.required_role,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "context": self.context,
        }


class ApprovalDecision(FrozenDomainModel):
    """A human's answer. Append-only in state; this is the audit record of who allowed what."""

    approval_id: str
    incident_id: str
    kind: ApprovalKind
    status: ApprovalStatus
    decided_at: datetime
    decided_by: str = Field(min_length=1)
    decided_by_role: str = ""
    rationale: str = ""
    reason_code: ReasonCode | None = None
    conditions: tuple[str, ...] = ()
    modified_action: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _rejection_is_explained(self) -> Self:
        if self.status is ApprovalStatus.REJECTED and not self.rationale:
            raise ValueError(
                "a rejected approval must carry a rationale: the graph routes on it and the "
                "technician or NOC operator downstream needs to know what to do instead"
            )
        return self

    @property
    def granted(self) -> bool:
        return self.status is ApprovalStatus.APPROVED

    @property
    def approval_ref(self) -> str:
        """The value that goes on the `ActionRequest`. Derived, so it cannot disagree."""
        return f"{self.approval_id}:{self.decided_by}"


class ActionRequest(FrozenDomainModel):
    """The typed envelope for every effect on an external system.

    The six mandatory fields are `incident_id`, `idempotency_key`, `actor`, `reason_code`,
    `approval_ref` and `correlation_id`. Five are unconditionally required by the type; the sixth,
    `approval_ref`, is required exactly when `policy_outcome is REQUIRES_APPROVAL`, which the
    validator below enforces. Without that validator the field would be present-but-empty on
    precisely the actions where it matters.
    """

    action_id: str
    incident_id: str
    action_type: ActionType
    target_ref: str
    requested_at: datetime

    idempotency_key: str = Field(min_length=8)
    actor: str = Field(min_length=1)
    reason_code: ReasonCode
    correlation_id: str = Field(min_length=1)
    approval_ref: str | None = None

    policy_decision_id: str | None = None
    policy_outcome: PolicyOutcome = PolicyOutcome.ALLOWED
    attempt: int = Field(default=1, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reversible: bool = True
    expected_blast_radius: int | None = Field(default=None, ge=0)
    simulated: bool = False

    @model_validator(mode="after")
    def _approval_present_when_required(self) -> Self:
        if self.policy_outcome is PolicyOutcome.REQUIRES_APPROVAL and not self.approval_ref:
            raise ValueError(
                f"{self.action_type} needs approval per policy but carries no approval_ref; an "
                "action that reaches an adapter in this state is an unapproved production write"
            )
        if self.policy_outcome is PolicyOutcome.BLOCKED:
            raise ValueError(
                f"{self.action_type} was blocked by policy and must not be built into an "
                "ActionRequest at all"
            )
        return self


class ActionRecord(FrozenDomainModel):
    """What actually happened when an `ActionRequest` was executed. Append-only in state.

    Separate from the request because the request is an intention and this is an outcome, and
    collapsing the two loses the case that matters most: an action that was requested, sent, and
    whose result we never learned.
    """

    action_id: str
    incident_id: str
    action_type: ActionType
    target_ref: str
    idempotency_key: str
    outcome: ActionOutcome
    started_at: datetime
    completed_at: datetime | None = None
    actor: str = ""
    reason_code: ReasonCode | None = None
    approval_ref: str | None = None
    correlation_id: str = ""
    attempt: int = Field(default=1, ge=1)
    simulated: bool = False
    external_ref: str | None = None
    detail: str = ""
    error: str = ""
    evidence_refs: tuple[str, ...] = ()

    @property
    def duration(self) -> timedelta | None:
        if self.completed_at is None:
            return None
        return max(self.completed_at - self.started_at, timedelta(0))

    @property
    def changed_something(self) -> bool:
        """Whether this action is believed to have altered external state.

        `PARTIAL` counts. A partially applied bulk config push has changed something, and treating
        it as a no-op is how a rollback gets skipped.
        """
        return self.outcome in (ActionOutcome.SUCCEEDED, ActionOutcome.PARTIAL)


class AuditEvent(FrozenDomainModel):
    """One line of the audit trail. Append-only, never edited, no PII.

    `actor` is a principal, not a customer. Where an event concerns a customer, the customer appears
    as a reference (`subject_ref`), never as a name, address or MAC.
    """

    event_id: str
    incident_id: str
    occurred_at: datetime
    actor: str
    action: str = Field(min_length=1)
    node: str = ""
    subject_ref: str | None = None
    reason_code: ReasonCode | None = None
    outcome: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    policy_version: str | None = None
    correlation_id: str = ""


class KPIEvent(FrozenDomainModel):
    """A measured KPI observation.

    `value` is always computed from state at emission time; nothing here is a target or a
    hard-coded rate. `dimensions` carries the slicing (technology, area archetype, crew type) so a
    single KPI name does not fork into a dozen near-duplicates.
    """

    event_id: str
    kpi_name: str
    emitted_at: datetime
    value: float
    unit: str = ""
    incident_id: str | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    numerator: float | None = None
    denominator: float | None = None

    @model_validator(mode="after")
    def _rate_shows_its_working(self) -> Self:
        # A rate with no numerator/denominator cannot be re-aggregated: averaging averages is wrong,
        # and a dashboard that does it silently reports a number nobody can reproduce.
        if self.unit == "rate" and (self.numerator is None or self.denominator is None):
            raise ValueError(
                "a rate KPI must carry numerator and denominator so it can be re-aggregated "
                "without averaging averages"
            )
        return self
