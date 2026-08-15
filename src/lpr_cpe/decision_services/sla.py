"""Which clock this incident runs against, and how much of it is left.

`SLAContext` is D1's one clock: `clock_started_at` is written once and every deadline is derived
from it on read. This module is the two things around that record which the record cannot do for
itself -- deciding what its targets should be at intake, and reporting where the clock stands now.

The targets come from two places that both have standing. The customer's contract, read through
`integrations.tmf.fetch_sla`, is what was sold. The policy pack's `sla` section is the internal
target by severity, which exists because a critical fault on a residential line is not something the
organisation is willing to sit on for the residential contract's twenty-four hours. `resolve_sla`
takes the **tighter of the two** and records which one bound, because the two answers are not
interchangeable afterwards: missing a contractual restore target is a credit, a dispute and a
regulatory line item, while missing an internal target is a conversation. An incident that reports
only "breached" cannot tell an operations review which of those happened.

The pack also tightens by band for a vulnerable customer -- `SLAPolicy._effective` does that, and it
is not re-done here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from lpr_cpe.decision_services._payload import read_float, read_text
from lpr_cpe.domain.enums import DataQualityFlag, Severity
from lpr_cpe.domain.records import SLAContext
from lpr_cpe.policies.models import EscalationPolicy, SLAPolicy

#: Which source set a target. `both` means they agreed to the minute, which is worth distinguishing
#: from either winning: it usually means the contract was written from the pack, and a later change
#: to one of them will show up here as a change of binding source.
SLABound = Literal["contract", "policy", "both"]


@dataclass(frozen=True, slots=True)
class ResolvedSLA:
    """The context to file, plus what was decided and what could not be read.

    `flags` and `notes` mirror `decision_services.delimiter.ResolvedTopology` deliberately: both are
    intake resolvers, both fold their flags into the same `DataQualityAssessment`, and a caller that
    handles one should not have to learn a second shape to handle the other.
    """

    context: SLAContext
    response_bound_by: SLABound
    restore_bound_by: SLABound
    flags: tuple[DataQualityFlag, ...] = field(default=())
    notes: tuple[str, ...] = field(default=())


def _contract_minutes(payload: dict[str, Any], key: str) -> float | None:
    """Hours from the contract read, as minutes. Zero and negative are refused, not clamped.

    A zero-minute restore target is breached at the instant the incident opens, so every incident
    under that contract would escalate on arrival and the escalation queue would stop meaning
    anything. Refusing it falls back to the pack, which is validated to be positive.
    """
    hours = read_float(payload, key)
    if hours is None or hours <= 0:
        return None
    return hours * 60.0


def resolve_sla(
    payload: dict[str, Any] | None,
    *,
    severity: Severity,
    clock_started_at: datetime,
    policy: SLAPolicy,
) -> ResolvedSLA:
    """Build the incident's clock from the contract and the pack, taking whichever is tighter.

    A `None` payload -- the SLA read failed -- still produces a clock, from the pack alone, plus
    `ADAPTER_UNAVAILABLE`. This is the one place in the package where an unavailable adapter does
    not leave the field unknown, and the reason is that the alternative is worse in a specific way:
    an incident with no clock is an incident that can never be late, so a CRM outage would silently
    suspend the SLA on every incident opened during it. The pack's severity target is a defensible
    stand-in; no target at all is not.

    `vulnerable_customer` and `priority_customer` are read from the contract and carried onto the
    context, and `vulnerable_customer` is also passed to the pack so its band tightening applies. A
    failed read means both are `False`, which is the wrong way round for safety -- so the flag is
    raised and `notes` says which protection was not applied, rather than pretending it was.
    """
    flags: list[DataQualityFlag] = []
    notes: list[str] = []

    if payload is None:
        flags.append(DataQualityFlag.ADAPTER_UNAVAILABLE)
        notes.append(
            "the SLA contract could not be read; targets are the policy pack's for a "
            f"{severity.value} incident, and vulnerable-customer tightening was not applied "
            "because that flag lives in the record we could not read"
        )
        payload = {}

    vulnerable = payload.get("vulnerable_customer") is True
    priority = payload.get("priority_customer") is True

    pack_response = float(policy.response_for(severity, vulnerable=vulnerable))
    pack_restore = float(policy.restore_for(severity, vulnerable=vulnerable))

    contract_response = _contract_minutes(payload, "response_target_hours")
    contract_restore = _contract_minutes(payload, "restore_target_hours")

    if (
        contract_response is not None
        and contract_restore is not None
        and contract_restore < contract_response
    ):
        # Which of the pair is wrong is not knowable from here, and keeping either half would build
        # a clock out of one number from a record that contradicts itself. `SLAPolicy` validates
        # this same ordering, so falling back to the pack restores the invariant rather than
        # papering over it.
        flags.append(DataQualityFlag.CONFLICTING_SOURCES)
        notes.append(
            f"the contract's restore target ({contract_restore:.0f} min) is shorter than its "
            f"response target ({contract_response:.0f} min), which cannot be met in that order; "
            "both contract targets are discarded in favour of the policy pack's"
        )
        contract_response = contract_restore = None
    elif contract_response is None or contract_restore is None:
        flags.append(DataQualityFlag.MISSING_FIELD)
        notes.append(
            "the contract read did not carry usable response and restore targets, so the policy "
            f"pack's {severity.value} targets are used"
        )

    response_minutes, response_bound_by = _tighter(contract_response, pack_response)
    restore_minutes, restore_bound_by = _tighter(contract_restore, pack_restore)

    for label, bound_by, contract, pack in (
        ("response", response_bound_by, contract_response, pack_response),
        ("restore", restore_bound_by, contract_restore, pack_restore),
    ):
        if bound_by == "policy" and contract is not None:
            notes.append(
                f"the {label} target is the pack's internal {pack:.0f} min rather than the "
                f"contract's {contract:.0f} min; a miss here is an internal target, not a breach "
                "of contract"
            )

    context = SLAContext(
        sla_ref=read_text(payload, "sla_ref") or "",
        product_tier=read_text(payload, "product_tier") or "residential",
        clock_started_at=clock_started_at,
        response_target=timedelta(minutes=response_minutes),
        restore_target=timedelta(minutes=restore_minutes),
        business_hours_only=payload.get("business_hours_only") is True,
        vulnerable_customer=vulnerable,
        priority_customer=priority,
        credit_at_risk=payload.get("credit_at_risk") is True,
    )
    return ResolvedSLA(
        context=context,
        response_bound_by=response_bound_by,
        restore_bound_by=restore_bound_by,
        flags=tuple(flags),
        notes=tuple(notes),
    )


def _tighter(contract: float | None, pack: float) -> tuple[float, SLABound]:
    if contract is None:
        return pack, "policy"
    if contract == pack:
        return pack, "both"
    return (contract, "contract") if contract < pack else (pack, "policy")


@dataclass(frozen=True, slots=True)
class SLAStatus:
    """Where the clock stands at one instant.

    Every field is derived from `SLAContext` at the `now` recorded here, and nothing is stored back
    onto the context. That is D1's rule and it has a concrete consequence: a status object is only
    true for the instant it names, so it is built where it is read rather than carried in state.
    """

    now: datetime
    response_due_at: datetime
    restore_due_at: datetime
    response_breached: bool
    restore_breached: bool
    remaining: timedelta
    fraction_consumed: float
    at_risk: bool

    @property
    def remaining_minutes(self) -> float:
        """Negative once breached, which is what dispatch ranking wants: an incident forty minutes
        over is ahead of one forty minutes short."""
        return self.remaining.total_seconds() / 60.0


def sla_status(
    context: SLAContext,
    *,
    now: datetime,
    escalation: EscalationPolicy,
) -> SLAStatus:
    """Read the clock, using the pack's warning fraction for `at_risk`.

    The conversion in the middle of this function is the reason it exists.
    `escalation.sla_breach_warning_fraction` is 0.75 and means *three quarters of the budget spent*
    -- its own validator says a value at or above 1.0 would be "a breach report, not a warning",
    which only reads that way for consumption. `SLAContext.at_risk` takes the opposite fraction: how
    much budget must *remain*. Passing 0.75 straight through would mark every incident at risk
    within the first quarter of its budget, the escalation queue would fill with incidents that have
    six hours left, and the warning would be discarded as noise by the people it is for.

    `at_risk` stays true after the deadline passes, by `SLAContext.at_risk`'s own decision. Nothing
    here re-derives it: escalation reads this one field, so there is no second definition of
    "approaching breach" for the two to disagree over.
    """
    remaining_fraction = 1.0 - escalation.sla_breach_warning_fraction
    budget = context.restore_target + context.paused_duration
    remaining = context.time_remaining(now)
    if budget > timedelta(0):
        # Not clamped at 1.0. A dashboard on which a three-hour overrun and a one-minute overrun
        # both read as 100% consumed cannot say which incident to look at first.
        consumed = 1.0 - (remaining / budget)
    else:
        consumed = 1.0
    return SLAStatus(
        now=now,
        response_due_at=context.response_deadline(),
        restore_due_at=context.restore_deadline(),
        response_breached=now > context.response_deadline(),
        restore_breached=context.is_breached(now),
        remaining=remaining,
        fraction_consumed=round(max(0.0, consumed), 4),
        at_risk=context.at_risk(now, threshold_fraction=remaining_fraction),
    )


__all__ = ["ResolvedSLA", "SLABound", "SLAStatus", "resolve_sla", "sla_status"]
