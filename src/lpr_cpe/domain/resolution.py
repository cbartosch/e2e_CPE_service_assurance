"""Resolution options, the chosen plan, remote actions and guided self-help.

`ResolutionOption` deliberately carries `estimated_success_probability` **and**
`customer_disruption`. Ranking on success alone sends a factory reset ahead of a channel change
whenever the reset is marginally more likely to work, which is right for the incident and wrong for
the customer. The ranking function combines both, and it lives here rather than in whichever node
happens to sort the list.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import Field, model_validator

from lpr_cpe.domain.base import DomainModel, FrozenDomainModel
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    CommunicationChannel,
    FaultDomain,
    ReasonCode,
)


class ResolutionOption(FrozenDomainModel):
    """One way this might be fixed, with its cost to the customer stated."""

    option_id: str
    action_type: ActionType
    target_ref: str
    label: str = Field(min_length=1)
    addresses_domain: FaultDomain
    estimated_success_probability: float = Field(ge=0.0, le=1.0)
    estimated_duration: timedelta = timedelta(minutes=5)
    customer_disruption: float = Field(default=0.0, ge=0.0, le=1.0)
    reversible: bool = True
    requires_customer_present: bool = False
    requires_truck_roll: bool = False
    blast_radius: int = Field(default=1, ge=0)
    prerequisites: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""

    @property
    def rank_key(self) -> tuple[float, float, float]:
        """Sort key: best first when used with `reverse=True`.

        Success probability discounted by disruption, then a preference for reversible actions, then
        a preference against truck rolls. Written as one expression so two nodes cannot rank the
        same options differently.
        """
        adjusted = self.estimated_success_probability * (1.0 - 0.5 * self.customer_disruption)
        return (adjusted, 1.0 if self.reversible else 0.0, 0.0 if self.requires_truck_roll else 1.0)


class ResolutionPlan(DomainModel):
    """The ordered attempt sequence, and the one option currently selected.

    `attempted_option_ids` is what makes "re-diagnose before repeating work" enforceable: the plan
    knows what has already been tried, so a loop that comes back around cannot silently re-run the
    same reboot and count it as progress.
    """

    plan_id: str
    created_at: datetime
    fault_domain: FaultDomain
    options: list[ResolutionOption] = Field(default_factory=list)
    selected_option_id: str | None = None
    attempted_option_ids: list[str] = Field(default_factory=list)
    escalation_path: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _selection_exists(self) -> Self:
        if self.selected_option_id is not None:
            known = {o.option_id for o in self.options}
            if self.selected_option_id not in known:
                raise ValueError(
                    f"selected_option_id {self.selected_option_id!r} is not among the plan's "
                    f"options {sorted(known)}"
                )
        return self

    def ranked(self) -> list[ResolutionOption]:
        return sorted(self.options, key=lambda o: o.rank_key, reverse=True)

    def untried(self) -> list[ResolutionOption]:
        tried = set(self.attempted_option_ids)
        return [o for o in self.ranked() if o.option_id not in tried]

    @property
    def selected(self) -> ResolutionOption | None:
        if self.selected_option_id is None:
            return None
        return next((o for o in self.options if o.option_id == self.selected_option_id), None)

    @property
    def exhausted(self) -> bool:
        return not self.untried()


class RemoteAction(DomainModel):
    """A remote fix attempt and its verification.

    `verified_at` and `verification_summary` are separate from the outcome on purpose. A reboot that
    the ACS acknowledged is not a reboot that fixed anything, and "proof before closure" means the
    verification is a distinct observation with its own timestamp.
    """

    action_id: str
    action_type: ActionType
    target_ref: str
    idempotency_key: str
    requested_at: datetime
    completed_at: datetime | None = None
    outcome: ActionOutcome = ActionOutcome.SKIPPED
    attempt: int = Field(default=1, ge=1)
    simulated: bool = False
    reason_code: ReasonCode | None = None
    approval_ref: str | None = None
    pre_state: dict[str, Any] = Field(default_factory=dict)
    post_state: dict[str, Any] = Field(default_factory=dict)
    verified_at: datetime | None = None
    verification_summary: str = ""
    verification_passed: bool | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    error: str = ""

    @model_validator(mode="after")
    def _verification_is_dated(self) -> Self:
        if self.verification_passed is not None and self.verified_at is None:
            raise ValueError(
                "verification_passed was set without verified_at: an undated verification cannot "
                "be shown to have happened after the action"
            )
        return self

    @property
    def fixed_it(self) -> bool:
        """Only true when the action succeeded AND verification confirmed it.

        `verification_passed is True` rather than truthiness: `None` means not yet verified, and
        treating that as success is exactly the failure this property exists to prevent.
        """
        return self.outcome is ActionOutcome.SUCCEEDED and self.verification_passed is True


class SelfHelpSession(DomainModel):
    """A guided self-help interaction with the customer.

    Modelled with an explicit `awaiting_response_since` rather than a sleep: the graph interrupts
    and is resumed by a customer response or a timer event, which is what the specification
    requires and what makes a 24-hour customer wait cost nothing to hold.
    """

    session_id: str
    incident_id: str
    channel: CommunicationChannel
    started_at: datetime
    steps_sent: list[str] = Field(default_factory=list)
    step_index: int = Field(default=0, ge=0)
    customer_responses: list[str] = Field(default_factory=list)
    awaiting_response_since: datetime | None = None
    response_deadline: datetime | None = None
    completed_at: datetime | None = None
    outcome: str = "in_progress"  # in_progress | resolved | not_resolved | declined | timed_out
    reason_code: ReasonCode | None = None
    accessibility_accommodations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def awaiting_customer(self) -> bool:
        return self.awaiting_response_since is not None and self.completed_at is None

    def timed_out(self, now: datetime) -> bool:
        return (
            self.awaiting_customer
            and self.response_deadline is not None
            and now > self.response_deadline
        )

    @property
    def attempts(self) -> int:
        return len(self.steps_sent)

    def wait_duration(self, now: datetime) -> timedelta | None:
        if self.awaiting_response_since is None:
            return None
        end = self.completed_at or now
        return max(end - self.awaiting_response_since, timedelta(0))
