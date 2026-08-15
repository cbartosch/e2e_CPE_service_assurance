"""The twelve hard constraints, each a named function that says why it refused.

A hard constraint is not a preference with a large weight. A schedule that violates one is
*infeasible*, and the specification's D14 is explicit about what follows: identify the blocking
constraint, search alternatives, queue for dispatcher action, and do not commit the slot. None of
that is possible against a solver that returns "no solution".

**The codes are a vocabulary, not prose.** `ConstraintCode` is a `StrEnum` and every explanation
begins with one, because D14's "identify the blocking constraint" is a machine reading this, and
matching downstream logic against human-readable text is how a link rots without anyone noticing.
That is not hypothetical here: the Wi-Fi breach-to-action map in `decision_services.forecast` was
first keyed on detector prose, matched none of it, and silently recommended no action for every
verdict. The lesson was cheap to learn once and is not being re-learned. `blocking_code` parses the
code back out, and `test_dispatch.py` asserts every explanation this module produces round-trips
through it.

**Each constraint is a separate function** rather than a branch in one big `is_feasible`, so that a
test can drive one to failure with everything else satisfied. A feasibility check that can only be
tested end-to-end gets tested by whichever constraint happens to fail first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from lpr_cpe.dispatch.travel import TravelEstimate
from lpr_cpe.domain.enums import AreaArchetype
from lpr_cpe.domain.field_ops import CrewSlot, DispatchRequirement
from lpr_cpe.policies.models import DispatchPolicy


class ConstraintCode(StrEnum):
    """The twelve the specification names, and nothing else.

    Kept one-to-one with that list deliberately. A thirteenth code invented here would be a
    constraint the dispatcher's runbook has no entry for, and the queue-for-dispatcher-action path
    would present a reason nobody is trained to clear.
    """

    SKILL = "skill"
    CREW_TYPE = "crew_type"
    EQUIPMENT = "equipment"
    PARTS = "parts"
    WORKING_HOURS = "working_hours"
    CUSTOMER_ACCESS = "customer_access"
    BUILDING_ACCESS = "building_access"
    SAFETY = "safety"
    GEOGRAPHY = "geography"
    REMOTE_ACCESS_WINDOW = "remote_access_window"
    CAPACITY = "capacity"
    WORK_ORDER_DEPENDENCY = "work_order_dependency"


@dataclass(frozen=True, slots=True)
class JobContext:
    """Facts about one job that other modules own, carried in rather than recomputed.

    Every field here has an owner elsewhere: SLA standing is
    `decision_services.sla.sla_status`, the customer count is `decision_services.blast_radius`,
    the wind is `GISAdapter.fetch_weather`. The optimizer reads them and does not re-derive them,
    because a second SLA calculation here would eventually disagree with the one the escalation
    queue is sorted by, and the two would be reported as one number.

    Defaults are the permissive ones for constraints that need data to refuse -- an absent wind
    reading does not block aerial work. The exception is `parts_in_stock`, which defaults `True`
    only because `DispatchRequirement.parts_required` being empty is the common case; where parts
    are named and stock is unknown, the caller must say so rather than let the default speak.
    """

    requirement_id: str
    sla_remaining_minutes: float | None = None
    sla_at_risk: bool = False
    affected_customers: int = 1
    vulnerable_customer: bool = False
    wind_kph: float | None = None
    aerial_work_required: bool = False
    building_access_windows: tuple[tuple[datetime, datetime], ...] = ()
    depends_on: tuple[str, ...] = ()
    parts_in_stock: bool = True
    first_time_fix_probability: float | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (requirement, crew, start time) triple, fully priced, awaiting judgement."""

    requirement: DispatchRequirement
    crew: CrewSlot
    context: JobContext
    start: datetime
    end: datetime
    travel: TravelEstimate
    jobs_already_assigned: int = 0
    already_scheduled: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    """Why one candidate was refused, in a form both a human and a router can read.

    `shortfall` is *how badly*, and it exists because counting refusals alone cannot rank near
    misses. A crew missing one skill and a crew missing three both fail exactly one constraint, so
    the dispatcher queue picked between them alphabetically and routinely pointed at the crew that
    was further away from being able to do the job. One missing part is a transfer between vans;
    three missing skills is a different crew.

    It counts *items*, not severity, and stays 1 for the constraints that are not about a set --
    a window clash is not more or less clashing.
    """

    code: ConstraintCode
    detail: str
    shortfall: int = 1

    def render(self) -> str:
        """`"<code>: <detail>"`. The format `blocking_code` parses and `DispatchPlan` stores."""
        return f"{self.code.value}: {self.detail}"


def blocking_code(explanation: str) -> ConstraintCode | None:
    """Recover the code from a rendered explanation, or `None` if it carries none.

    Exists so that D14's routing reads the code rather than pattern-matching the sentence. Returns
    `None` rather than raising on an unparseable string: explanations also carry the satisfied-set
    summary for assigned requirements, and that is not a violation.
    """
    head, _, _ = explanation.partition(":")
    try:
        return ConstraintCode(head.strip())
    except ValueError:
        return None


def _missing(required: Sequence[str], held: Sequence[str]) -> list[str]:
    """Set difference, ordered, so the same shortfall always renders the same string."""
    return sorted(set(required) - set(held))


def _within_any(
    start: datetime, end: datetime, windows: Sequence[tuple[datetime, datetime]]
) -> bool:
    """Whether the whole job fits inside one window.

    The *whole* job, not just its start. A visit that begins inside a two-hour appointment window
    and runs ninety minutes past it has still missed the appointment, and scheduling on the start
    alone is how a system reports full compliance while customers stand in doorways.
    """
    return any(w_start <= start and end <= w_end for w_start, w_end in windows)


# -- the twelve ---------------------------------------------------------------------------------


def check_skill(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    missing = _missing(c.requirement.skills_required, c.crew.skills)
    if missing:
        return ConstraintViolation(
            ConstraintCode.SKILL,
            f"crew {c.crew.crew_id} lacks {','.join(missing)}",
            shortfall=len(missing),
        )
    return None


def check_crew_type(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """Exact match when the pack demands one.

    No substitution rule -- not even the tempting "a joint crew can do a clean job". A joint crew
    doing clean work is two crews doing one crew's job, which is the cost joint dispatch exists to
    avoid, and it would be chosen freely by an optimizer that treats it as merely permitted.
    """
    if policy.require_crew_type_match and c.crew.crew_type is not c.requirement.crew_type:
        return ConstraintViolation(
            ConstraintCode.CREW_TYPE,
            f"requirement needs {c.requirement.crew_type.value}, crew {c.crew.crew_id} is "
            f"{c.crew.crew_type.value}",
        )
    return None


def check_equipment(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    missing = _missing(c.requirement.equipment_required, c.crew.carried_equipment)
    if missing:
        return ConstraintViolation(
            ConstraintCode.EQUIPMENT,
            f"crew {c.crew.crew_id} is not carrying {','.join(missing)}",
            shortfall=len(missing),
        )
    return None


def check_parts(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    """Van stock, then warehouse stock. Two failures, one code, different details.

    Distinguished in the detail because the dispatcher's remedy differs: a part on another van is a
    transfer, a part nobody has is a back-order and a different appointment.
    """
    missing = _missing(c.requirement.parts_required, c.crew.carried_parts)
    if missing:
        return ConstraintViolation(
            ConstraintCode.PARTS,
            f"crew {c.crew.crew_id} van stock lacks {','.join(missing)}",
            shortfall=len(missing),
        )
    if c.requirement.parts_required and not c.context.parts_in_stock:
        return ConstraintViolation(
            ConstraintCode.PARTS,
            f"{','.join(sorted(c.requirement.parts_required))} not in stock at any depot",
            shortfall=len(set(c.requirement.parts_required)),
        )
    return None


def check_working_hours(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """The shift, plus the pack's overtime allowance, and not a minute more.

    Overtime is a budget rather than an exception: `max_overtime_minutes` is what the pack is
    willing to buy, so a job ending inside it is feasible and one ending past it is not. Treating
    overrun as merely expensive would let the objective's other terms buy an unbounded amount of it
    whenever an incident looked urgent enough.
    """
    if c.start < c.crew.available_from:
        return ConstraintViolation(
            ConstraintCode.WORKING_HOURS,
            f"start {c.start.isoformat()} precedes crew {c.crew.crew_id} shift start "
            f"{c.crew.available_from.isoformat()}",
        )
    latest = c.crew.available_until + timedelta(minutes=policy.max_overtime_minutes)
    if c.end > latest:
        over = (c.end - c.crew.available_until).total_seconds() / 60.0
        return ConstraintViolation(
            ConstraintCode.WORKING_HOURS,
            f"end {c.end.isoformat()} is {over:.0f} min past crew {c.crew.crew_id} shift end, "
            f"above the {policy.max_overtime_minutes} min overtime allowance",
        )
    return None


def check_customer_access(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    if not policy.respect_appointment_windows or not c.requirement.customer_access_required:
        return None
    windows = c.requirement.customer_availability_windows
    if not windows:
        # `DispatchRequirement` already refuses this combination at construction, so reaching here
        # means the model was bypassed. Refusing again costs nothing and keeps the optimizer from
        # depending on a validator elsewhere staying as it is.
        return ConstraintViolation(
            ConstraintCode.CUSTOMER_ACCESS, "customer access required with no availability window"
        )
    if not _within_any(c.start, c.end, windows):
        return ConstraintViolation(
            ConstraintCode.CUSTOMER_ACCESS,
            f"{c.start.isoformat()}-{c.end.isoformat()} falls outside the "
            f"{len(windows)} customer window(s)",
        )
    return None


def check_building_access(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    """Concierge and riser-room hours, which are the building's, not the customer's.

    Separate from `customer_access` because they fail independently and are cleared by different
    people: a resident who is home at 19:00 in a building whose riser room locks at 17:00 is an
    access failure nobody at the door can fix.
    """
    windows = c.context.building_access_windows
    if windows and not _within_any(c.start, c.end, windows):
        return ConstraintViolation(
            ConstraintCode.BUILDING_ACCESS,
            f"{c.start.isoformat()}-{c.end.isoformat()} falls outside the "
            f"{len(windows)} building access window(s)",
        )
    return None


def check_safety(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """Aerial work stands down above the pack's wind limit.

    An unknown wind does not block the job. That is the one place in this module where missing data
    is read permissively, and it is a deliberate asymmetry: a weather adapter outage that grounded
    every aerial dispatch in Puerto Rico would be a larger outage than the one being fixed. The
    absence is visible -- `JobContext.wind_kph` is `None`, not zero -- so a caller that wants to
    fail closed can refuse before it gets here.
    """
    if not c.context.aerial_work_required or c.context.wind_kph is None:
        return None
    if c.context.wind_kph > policy.aerial_work_max_wind_kph:
        return ConstraintViolation(
            ConstraintCode.SAFETY,
            f"wind {c.context.wind_kph:.0f} kph exceeds the aerial limit of "
            f"{policy.aerial_work_max_wind_kph:.0f} kph",
        )
    return None


def check_geography(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    """A crew that lists archetypes works only those. A crew that lists none works anywhere.

    Empty means unrestricted rather than unqualified, which is the read that matches how the field
    is populated: a roster import that omits the column should widen the search and be visibly
    wrong, not silently strand every job.
    """
    area = c.requirement.area_archetype
    if area is not None and c.crew.area_archetypes and area not in c.crew.area_archetypes:
        return ConstraintViolation(
            ConstraintCode.GEOGRAPHY,
            f"crew {c.crew.crew_id} does not cover {area.value}",
        )
    return None


def check_remote_access_window(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """The last departure that still gets the crew home.

    Applies to `remote_island` only, and bites on the *start* rather than the end, because what is
    being protected is the return crossing. A job that finishes at 16:00 having started at 14:00 has
    still missed the last ferry, and stranding a crew overnight is a different kind of failure from
    being late -- it costs the next day's schedule as well as this one's.
    """
    if c.requirement.area_archetype is not AreaArchetype.REMOTE_ISLAND:
        return None
    cutoff = policy.remote_island_latest_start_local
    if c.start.time() > cutoff:
        return ConstraintViolation(
            ConstraintCode.REMOTE_ACCESS_WINDOW,
            f"start {c.start.time().isoformat(timespec='minutes')} is past the "
            f"{cutoff.isoformat(timespec='minutes')} island cutoff; the crew would not get back",
        )
    return None


def check_capacity(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """The tighter of the crew's own limit and the pack's.

    Two limits because they mean different things: `CrewSlot.max_jobs` is this crew on this shift
    (an apprentice, a half day), `max_jobs_per_crew_per_shift` is the organisation's ceiling. Taking
    the minimum means neither can be raised by editing the other.
    """
    limit = min(c.crew.max_jobs, policy.max_jobs_per_crew_per_shift)
    if c.jobs_already_assigned >= limit:
        return ConstraintViolation(
            ConstraintCode.CAPACITY,
            f"crew {c.crew.crew_id} already holds {c.jobs_already_assigned} job(s), at the "
            f"limit of {limit}",
        )
    return None


def check_work_order_dependency(c: Candidate, _: DispatchPolicy) -> ConstraintViolation | None:
    """Predecessors must already be on the plan.

    Ordering only, not timing: this says the pole change is scheduled before the drop replacement
    is considered, and the greedy pass placing predecessors first is what makes that meaningful. A
    solver that reorders freely would need the start-time comparison as well, which is why the check
    is named for the dependency rather than for the sequence.
    """
    unmet = sorted(set(c.context.depends_on) - c.already_scheduled)
    if unmet:
        return ConstraintViolation(
            ConstraintCode.WORK_ORDER_DEPENDENCY,
            f"depends on unscheduled {','.join(unmet)}",
        )
    return None


#: Evaluated in this order, and the order is a decision. The cheapest and most permanent refusals
#: come first: a crew that lacks the skill will lack it at every start time, so reporting that
#: before a window clash saves the dispatcher from rescheduling a crew that could never do the job.
#: `first_violation` short-circuits, so this order is also what a single reported reason will be.
ALL_CONSTRAINTS: tuple[Callable[[Candidate, DispatchPolicy], ConstraintViolation | None], ...] = (
    check_skill,
    check_crew_type,
    check_equipment,
    check_parts,
    check_geography,
    check_capacity,
    check_work_order_dependency,
    check_safety,
    check_working_hours,
    check_customer_access,
    check_building_access,
    check_remote_access_window,
)


def first_violation(c: Candidate, policy: DispatchPolicy) -> ConstraintViolation | None:
    """The first refusal in `ALL_CONSTRAINTS` order, or `None` if the candidate is feasible."""
    for check in ALL_CONSTRAINTS:
        violation = check(c, policy)
        if violation is not None:
            return violation
    return None


def all_violations(c: Candidate, policy: DispatchPolicy) -> list[ConstraintViolation]:
    """Every refusal, for the dispatcher queue.

    `first_violation` answers "can this be scheduled"; this answers "what would it take". Clearing
    one blocker and rediscovering the next on the following run is the interaction that makes an
    optimizer feel adversarial, so the queued explanation carries the whole set.
    """
    found = [check(c, policy) for check in ALL_CONSTRAINTS]
    return [v for v in found if v is not None]


def satisfied_codes(c: Candidate, policy: DispatchPolicy) -> tuple[ConstraintCode, ...]:
    """Which constraints this candidate clears.

    The specification asks for satisfied *and* binding constraints, and the satisfied set is the
    half that is easy to omit. It is what shows that a constraint was actually evaluated rather
    than skipped: an empty violation list is equally consistent with twelve passing checks and with
    twelve checks that never ran.
    """
    violated = {v.code for v in all_violations(c, policy)}
    return tuple(code for code in ConstraintCode if code not in violated)


__all__ = [
    "ALL_CONSTRAINTS",
    "Candidate",
    "ConstraintCode",
    "ConstraintViolation",
    "JobContext",
    "all_violations",
    "blocking_code",
    "check_building_access",
    "check_capacity",
    "check_crew_type",
    "check_customer_access",
    "check_equipment",
    "check_geography",
    "check_parts",
    "check_remote_access_window",
    "check_safety",
    "check_skill",
    "check_work_order_dependency",
    "check_working_hours",
    "first_violation",
    "satisfied_codes",
]
