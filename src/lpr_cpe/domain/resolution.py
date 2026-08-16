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
    ApprovalKind,
    CommunicationChannel,
    FaultDomain,
    ReasonCode,
)


class ResolutionOption(FrozenDomainModel):
    """One way this might be fixed, with its cost to the customer stated.

    `risk` and `required_approval` are **copied from the policy pack when the plan is built**, by
    `decision_services.resolution.plan_resolution`, which already reads the pack's `ActionRule` to
    decide whether the option may be offered at all and until now discarded everything but
    `allowed`. Two reasons to carry them rather than make every reader look them up again:

    * A plan is checkpointed into graph state and read back during an audit. An option that names
      only its action type tells an auditor what *today's* pack says about it, not what was in
      force when the plan was made. The copy is a snapshot of the rule that actually applied.
    * The API and the operator UI show options. "Needs a dispatch approval" is part of choosing,
      and a surface that has to re-open the pack to say so will eventually not bother.

    **The copy must never authorise anything.** `PolicyEngine` remains the only thing that decides
    whether an action may run, and it re-reads the pack at execution time; these two fields are for
    display and for the record. Using `required_approval` here as the approval gate would move the
    authorisation decision to whenever the plan happened to be built, which for an incident that
    sits pending overnight is a stale answer.
    """

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
    #: The pack's risk band for this action, verbatim. A `str` and not an enum because
    #: `policies.models.ActionRule.risk` is a `str`: the pack owns that vocabulary, and mirroring it
    #: as an enum here would make a pack that adds a band fail to load against a model that has not
    #: been redeployed. Empty means the plan was built without a pack rule, which cannot happen on
    #: the `plan_resolution` path -- an action with no rule is not offered at all.
    risk: str = ""
    #: The approval the pack demands before this action may run, or `None` for none. Display and
    #: audit only; see the class docstring.
    required_approval: ApprovalKind | None = None
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


#: Outcomes that mean the action ran, for `RemoteAction.fixed_it`.
#:
#: `SIMULATED` sits beside `SUCCEEDED` because `simulate_write` never reports `SUCCEEDED`: it
#: refuses to claim an effect it did not have, which is right. But simulation is the only mode with
#: a working adapter today, so requiring `SUCCEEDED` here would make `fixed_it` a constant `False`,
#: D10's `verify` branch dead code, and the specification's Scenario 2 inexpressible.
#:
#: No information is lost by admitting it. Whether the write was real is already recorded, once, on
#: `RemoteAction.simulated`; encoding it a second time inside `fixed_it` would make that property
#: mean two things at once, and it is the *verified* half that D10 needs.
#:
#: `PARTIAL` is deliberately absent. A partly-applied action that then verified clean is a real
#: situation and a genuinely harder judgement than this constant should be making silently.
_RAN: frozenset[ActionOutcome] = frozenset({ActionOutcome.SUCCEEDED, ActionOutcome.SIMULATED})


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
        """Only true when the action ran AND verification confirmed it.

        `verification_passed is True` rather than truthiness: `None` means not yet verified, and
        treating that as success is exactly the failure this property exists to prevent. That is
        the half this property exists for; see `_RAN` for why the outcome half admits `SIMULATED`.
        """
        return self.outcome in _RAN and self.verification_passed is True


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
    #: Device telemetry as it stood when the instructions went out, and as it stands once the
    #: customer has answered. Named to match `RemoteAction`, because they are the same two readings
    #: taken for the same reason and a reader should not have to learn two vocabularies.
    #:
    #: The pair is what makes P13's "resulting telemetry" a *comparison* rather than a snapshot. A
    #: post-reading on its own cannot distinguish "the customer fixed it" from "it was never broken
    #: in a way this adapter can see", and those two lead to opposite places: closure and a truck.
    pre_state: dict[str, Any] = Field(default_factory=dict)
    post_state: dict[str, Any] = Field(default_factory=dict)
    #: `in_progress | resolved | not_resolved | declined | timed_out`.
    #:
    #: Four terminal words for five situations: "the customer complied and the telemetry cannot say
    #: whether it worked" has no word of its own and is recorded as `not_resolved`. That is the
    #: conservative direction and the deliberate one -- D12 routes `resolved` to validation and
    #: everything else back round, and an unconfirmable step is not a restoration. The distinction
    #: is not lost, it is just not in this field: `notes` carries the summary that says which of the
    #: two it was.
    outcome: str = "in_progress"
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
