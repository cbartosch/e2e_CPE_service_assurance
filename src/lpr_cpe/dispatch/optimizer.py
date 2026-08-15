"""The deterministic scheduler: which crew, at what time, and why not for everything else.

`DispatchOptimizer` is a `Protocol` with one method, so the greedy implementation here can be
swapped for a constraint solver without any caller changing. That replaceability is the
specification's requirement; OR-Tools is named as a possible baseline, not as the contract.

**Why greedy is the implementation that ships.** It is fully tested against every constraint and
every fallback path, and a tested greedy plan is worth more than an untested optimal one -- a solver
whose infeasibility reporting has never been exercised fails first on exactly the day it matters.
`select_optimizer` is the extension point, and it reports what it chose rather than silently
degrading: a plan produced by the fallback says `solver="greedy"`, so nobody reads a heuristic
schedule as an optimal one.

**Determinism is a hard requirement, not a nicety.** Approvals resume from a checkpoint, and the
plan an approver saw must be the plan that gets committed. Every ordering in this module therefore
has an explicit tie-break on a stable identifier, and no iteration depends on set or dict ordering.
`test_dispatch.py` asserts this by solving shuffled inputs and comparing plans.

**What "no feasible slot" must produce.** D14 requires the blocking constraint, alternatives, and a
dispatcher queue -- and explicitly forbids committing an infeasible slot. So an unplaceable
requirement is never dropped: it lands in `DispatchPlan.unassigned` with the violations of its
*nearest miss*, which is the candidate that failed fewest checks. That is the one the dispatcher can
most cheaply unblock, and it is chosen deterministically rather than by whichever crew was tried
first.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from lpr_cpe.dispatch.constraints import (
    Candidate,
    ConstraintCode,
    ConstraintViolation,
    JobContext,
    all_violations,
    first_violation,
    satisfied_codes,
)
from lpr_cpe.dispatch.objective import ScoreBreakdown, score_candidate, urgency_rank
from lpr_cpe.dispatch.travel import PolicyTravelModel, TravelEstimate, TravelModel
from lpr_cpe.domain.field_ops import CrewSlot, DispatchAssignment, DispatchPlan, DispatchRequirement
from lpr_cpe.policies.models import BlastRadiusPolicy, DispatchPolicy

#: Granularity for advancing a start time to find a feasible slot, in minutes. Five is fine enough
#: that no realistic appointment window is missed by rounding and coarse enough that the search over
#: an eight-hour shift stays small. It is a search step, not a business rule, which is why it lives
#: here rather than in the pack -- a dispatcher has no reason to tune it and every reason to be
#: confused by it.
_SLOT_STEP_MINUTES = 5

#: How many steps forward the search will try before giving up on a crew. Twelve hours at the step
#: above. Bounded because an unbounded search over a job that can never fit would walk forward
#: forever, and the answer after two days of stepping is the same as the answer after twelve hours:
#: not on this shift.
_MAX_SLOT_STEPS = 144


@dataclass(frozen=True, slots=True)
class DispatchProblem:
    """Everything the solve needs, gathered by the caller, in one immutable object.

    Immutable and complete on purpose. An optimizer that reached out for a missing fact mid-solve
    would be neither pure nor replayable, and the two callers that matter -- the graph node and the
    approval replay -- must be able to produce the identical plan from the identical problem.
    """

    requirements: Sequence[DispatchRequirement]
    crews: Sequence[CrewSlot]
    contexts: Mapping[str, JobContext]
    dispatch_policy: DispatchPolicy
    blast_policy: BlastRadiusPolicy
    now: datetime
    travel: TravelModel | None = None
    plan_id: str = "plan-1"

    def travel_model(self) -> TravelModel:
        """The supplied model, or the pack's estimate. Never absent, never zero."""
        return self.travel if self.travel is not None else PolicyTravelModel(self.dispatch_policy)

    def context_for(self, requirement_id: str) -> JobContext:
        """A requirement with no context gets an empty one rather than an error.

        The permissive read is safe because every field's default is either neutral or visibly
        absent (`sla_remaining_minutes=None`), and the alternative -- refusing to plan a job whose
        SLA lookup failed -- would let an adapter outage stop dispatch entirely.
        """
        return self.contexts.get(requirement_id, JobContext(requirement_id=requirement_id))


class DispatchOptimizer(Protocol):
    """One method, so that replacing it is a one-line change at the call site."""

    name: str

    def solve(self, problem: DispatchProblem) -> DispatchPlan: ...


@dataclass
class _Route:
    """A crew's day as it is being built up. Mutable, local to one solve, never escapes."""

    crew: CrewSlot
    assignments: list[DispatchAssignment] = field(default_factory=list)
    free_from: datetime | None = None
    at_lat: float | None = None
    at_lon: float | None = None

    def position(self) -> tuple[float | None, float | None]:
        """Where the crew is when the next job starts: the last job, or the depot."""
        if self.at_lat is not None and self.at_lon is not None:
            return self.at_lat, self.at_lon
        return self.crew.base_latitude, self.crew.base_longitude

    def ready_at(self, now: datetime) -> datetime:
        return max(self.free_from or self.crew.available_from, self.crew.available_from, now)


def _window_starts(
    earliest: datetime,
    duration: timedelta,
    windows: Sequence[tuple[datetime, datetime]],
) -> list[datetime]:
    """Candidate start times, in order, cheapest first.

    With windows, only their opening times are tried (and `earliest` itself where it already sits
    inside one): a job that does not fit a window at its opening will not fit it later, because the
    window only shrinks. Without windows, the search steps forward at `_SLOT_STEP_MINUTES` to let a
    later start clear a constraint that an earlier one does not -- the island cutoff and shift end
    are the two that actually bite.
    """
    if windows:
        starts = {max(earliest, w_start) for w_start, w_end in windows if earliest <= w_end}
        starts.add(earliest)
        return sorted(starts)
    step = timedelta(minutes=_SLOT_STEP_MINUTES)
    return [earliest + step * i for i in range(_MAX_SLOT_STEPS)]


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One evaluated (crew, start) pair: either a score or the reasons it was refused."""

    crew_id: str
    start: datetime
    candidate: Candidate
    score: ScoreBreakdown | None
    violations: tuple[ConstraintViolation, ...]

    @property
    def feasible(self) -> bool:
        return self.score is not None


class GreedyDispatchOptimizer:
    """Most urgent job first; for each, the best-scoring feasible crew and start.

    Greedy over a queue ordered by `objective.urgency_rank`, which is a property of the job alone.
    Ordering by anything involving travel would make a job's queue position depend on which crew was
    considered first, and the plan would then vary with input order.

    The result is not optimal and does not claim to be: `DispatchPlan.solver` reads `greedy`, and
    `objective_value` is the sum of the accepted candidates' scores rather than a bound. What it is
    instead is explainable -- every assignment has a breakdown, every refusal has a code -- and for
    a queue that a dispatcher overrides by hand several times a shift, explainable beats optimal.
    """

    name = "greedy"

    def solve(self, problem: DispatchProblem) -> DispatchPlan:
        started = time.perf_counter()
        travel = problem.travel_model()
        routes = {
            crew.crew_id: _Route(crew=crew)
            for crew in sorted(problem.crews, key=lambda c: c.crew_id)
        }
        scheduled: set[str] = set()
        explanations: dict[str, str] = {}
        unassigned: list[str] = []
        objective_total = 0.0
        travel_bases: set[str] = set()

        for requirement in self._queue(problem):
            context = problem.context_for(requirement.requirement_id)
            attempts = self._attempts(requirement, context, routes, scheduled, problem, travel)
            best = self._best(attempts)
            if best is None:
                unassigned.append(requirement.requirement_id)
                explanations[requirement.requirement_id] = self._why_not(attempts)
                continue

            assert best.score is not None  # `_best` only returns feasible attempts
            route = routes[best.crew_id]
            route.assignments.append(
                DispatchAssignment(
                    requirement_id=requirement.requirement_id,
                    crew_id=best.crew_id,
                    crew_type=best.candidate.crew.crew_type,
                    scheduled_start=best.candidate.start,
                    scheduled_end=best.candidate.end,
                    travel_minutes=best.candidate.travel.minutes,
                    sequence_index=len(route.assignments),
                )
            )
            route.free_from = best.candidate.end
            route.at_lat = requirement.latitude
            route.at_lon = requirement.longitude
            scheduled.add(requirement.requirement_id)
            objective_total += best.score.total
            travel_bases.add(best.candidate.travel.basis)
            explanations[requirement.requirement_id] = self._satisfied_note(
                best, problem.dispatch_policy
            )

        assignments = sorted(
            (a for route in routes.values() for a in route.assignments),
            key=lambda a: (a.scheduled_start, a.crew_id, a.requirement_id),
        )
        return DispatchPlan(
            plan_id=problem.plan_id,
            created_at=problem.now,
            objective=f"weighted_sla_and_travel:{self._basis_label(travel_bases)}",
            solver=self.name,
            solver_status="ok" if not unassigned else "partial",
            assignments=assignments,
            unassigned=sorted(unassigned),
            constraint_explanation=explanations,
            objective_value=round(objective_total, 4),
            total_travel_minutes=round(sum(a.travel_minutes for a in assignments), 1),
            solve_duration=timedelta(seconds=round(time.perf_counter() - started, 6)),
        )

    # -- queue ------------------------------------------------------------------------------------

    def _queue(self, problem: DispatchProblem) -> list[DispatchRequirement]:
        """Requirements in the order they will be placed: most urgent first, ties by id.

        Predecessors are pulled ahead of their dependants regardless of urgency, because
        `check_work_order_dependency` refuses a job whose predecessor is not yet on the plan, and a
        queue that offered them in the wrong order would report a false infeasibility. One pass is
        enough for the single-level dependencies the domain models allow; a deeper graph would need
        a topological sort, and `depends_on` would need to permit it first.
        """

        def rank(r: DispatchRequirement) -> tuple[float, str]:
            context = problem.context_for(r.requirement_id)
            return (
                -urgency_rank(
                    r.priority_score,
                    context.sla_remaining_minutes,
                    at_risk=context.sla_at_risk,
                    affected=context.affected_customers,
                    job_minutes=r.estimated_duration.total_seconds() / 60.0,
                    blast=problem.blast_policy,
                ),
                r.requirement_id,
            )

        ordered = sorted(problem.requirements, key=rank)
        by_id = {r.requirement_id: r for r in ordered}
        placed: list[DispatchRequirement] = []
        seen: set[str] = set()
        for requirement in ordered:
            context = problem.context_for(requirement.requirement_id)
            for predecessor_id in sorted(context.depends_on):
                predecessor = by_id.get(predecessor_id)
                if predecessor is not None and predecessor_id not in seen:
                    placed.append(predecessor)
                    seen.add(predecessor_id)
            if requirement.requirement_id not in seen:
                placed.append(requirement)
                seen.add(requirement.requirement_id)
        return placed

    # -- candidate generation ---------------------------------------------------------------------

    def _attempts(
        self,
        requirement: DispatchRequirement,
        context: JobContext,
        routes: Mapping[str, _Route],
        scheduled: set[str],
        problem: DispatchProblem,
        travel: TravelModel,
    ) -> list[_Attempt]:
        """Every (crew, start) worth evaluating for one requirement.

        Stops at the first feasible start per crew. Later starts for the same crew score no better
        -- travel and the job are unchanged, and every time-sensitive term either stays flat or
        decays -- so continuing would multiply the search for no gain.
        """
        attempts: list[_Attempt] = []
        for crew_id in sorted(routes):
            route = routes[crew_id]
            from_lat, from_lon = route.position()
            estimate = travel.between(
                from_lat=from_lat,
                from_lon=from_lon,
                to_lat=requirement.latitude,
                to_lon=requirement.longitude,
                archetype=requirement.area_archetype,
            )
            earliest = route.ready_at(problem.now) + timedelta(minutes=estimate.minutes)
            if requirement.earliest_start is not None:
                earliest = max(earliest, requirement.earliest_start)
            windows = self._windows(requirement, context)
            crew_attempts = self._starts_for_crew(
                requirement, context, route, estimate, earliest, windows, scheduled, problem
            )
            attempts.extend(crew_attempts)
        return attempts

    def _starts_for_crew(
        self,
        requirement: DispatchRequirement,
        context: JobContext,
        route: _Route,
        estimate: TravelEstimate,
        earliest: datetime,
        windows: Sequence[tuple[datetime, datetime]],
        scheduled: set[str],
        problem: DispatchProblem,
    ) -> list[_Attempt]:
        attempts: list[_Attempt] = []
        for start in _window_starts(earliest, requirement.estimated_duration, windows):
            end = start + requirement.estimated_duration
            if requirement.latest_finish is not None and end > requirement.latest_finish:
                break
            candidate = Candidate(
                requirement=requirement,
                crew=route.crew,
                context=context,
                start=start,
                end=end,
                travel=estimate,
                jobs_already_assigned=len(route.assignments),
                already_scheduled=frozenset(scheduled),
            )
            violation = first_violation(candidate, problem.dispatch_policy)
            if violation is None:
                attempts.append(
                    _Attempt(
                        crew_id=route.crew.crew_id,
                        start=start,
                        candidate=candidate,
                        score=score_candidate(
                            candidate,
                            policy=problem.dispatch_policy,
                            blast=problem.blast_policy,
                        ),
                        violations=(),
                    )
                )
                break
            attempts.append(
                _Attempt(
                    crew_id=route.crew.crew_id,
                    start=start,
                    candidate=candidate,
                    score=None,
                    violations=tuple(all_violations(candidate, problem.dispatch_policy)),
                )
            )
            if self._permanent(violation.code):
                # Skill, crew type, equipment, parts, geography and capacity do not change with the
                # clock. Stepping the start time to retry them would burn the whole search budget
                # rediscovering the same refusal 144 times.
                break
        return attempts

    @staticmethod
    def _permanent(code: ConstraintCode) -> bool:
        """Whether a refusal is time-invariant for a given crew and job."""
        return code in {
            ConstraintCode.SKILL,
            ConstraintCode.CREW_TYPE,
            ConstraintCode.EQUIPMENT,
            ConstraintCode.PARTS,
            ConstraintCode.GEOGRAPHY,
            ConstraintCode.CAPACITY,
            ConstraintCode.WORK_ORDER_DEPENDENCY,
        }

    @staticmethod
    def _windows(
        requirement: DispatchRequirement, context: JobContext
    ) -> list[tuple[datetime, datetime]]:
        """Customer and building windows, intersected.

        Intersected rather than concatenated: a job needs the resident *and* the riser room, so the
        feasible period is the overlap. Concatenating would offer start times that satisfy one and
        fail the other, and the constraint checks would then reject every start the search proposed.
        """
        customer = list(requirement.customer_availability_windows)
        building = list(context.building_access_windows)
        if not customer:
            return building
        if not building:
            return customer
        overlaps = [
            (max(c_start, b_start), min(c_end, b_end))
            for c_start, c_end in customer
            for b_start, b_end in building
            if max(c_start, b_start) < min(c_end, b_end)
        ]
        return sorted(overlaps)

    # -- selection and reporting -------------------------------------------------------------------

    @staticmethod
    def _best(attempts: Sequence[_Attempt]) -> _Attempt | None:
        """Highest-scoring feasible attempt; ties broken on start time then crew id."""
        feasible = [a for a in attempts if a.feasible]
        if not feasible:
            return None
        return min(
            feasible,
            key=lambda a: (-(a.score.total if a.score else 0.0), a.start, a.crew_id),
        )

    @staticmethod
    def _why_not(attempts: Sequence[_Attempt]) -> str:
        """The nearest miss, rendered.

        The candidate a dispatcher can most cheaply unblock: fewest constraints violated, then
        smallest total shortfall within them. Both terms are needed and the second was added after
        the first alone got it wrong -- a crew missing three skills and a crew missing one each
        violate exactly one constraint, so ranking on the count broke the tie alphabetically and
        pointed at whichever crew_id sorted first. Ties beyond that break on crew id and start, so
        the queued reason does not change between identical runs.

        With no crews at all the answer is `capacity`, not a new code. Inventing a thirteenth would
        put a reason in the dispatcher queue that the runbook has no entry for.
        """
        if not attempts:
            return ConstraintViolation(
                ConstraintCode.CAPACITY, "no crew slots were supplied to the optimizer"
            ).render()
        nearest = min(
            attempts,
            key=lambda a: (
                len(a.violations),
                sum(v.shortfall for v in a.violations),
                a.crew_id,
                a.start,
            ),
        )
        codes = ",".join(v.code.value for v in nearest.violations)
        detail = "; ".join(v.detail for v in nearest.violations)
        head = nearest.violations[0].code.value if nearest.violations else ConstraintCode.CAPACITY
        return f"{head}: blocked by {codes} -- {detail}"

    @staticmethod
    def _satisfied_note(attempt: _Attempt, policy: DispatchPolicy) -> str:
        """What an assigned requirement cleared, and what it scored.

        The specification asks for satisfied *and* binding constraints. Recording only the binding
        ones would leave an assigned requirement with an empty entry, which reads the same whether
        twelve checks passed or none ran.
        """
        codes = ",".join(c.value for c in satisfied_codes(attempt.candidate, policy))
        total = attempt.score.total if attempt.score else 0.0
        return f"satisfied: {codes} (score {total:.2f})"

    @staticmethod
    def _basis_label(bases: set[str]) -> str:
        """One word for how the plan's travel was arrived at.

        `mixed` when the legs disagree, which happens when a routing engine answers some pairs and
        times out on others. Reporting the majority basis instead would let a plan that is mostly
        estimates describe itself as routed.
        """
        if not bases:
            return "none"
        if len(bases) == 1:
            return next(iter(bases))
        return "mixed"


def select_optimizer(*, prefer_solver: bool = True) -> DispatchOptimizer:
    """The optimizer to use, preferring a constraint solver when one is installed.

    OR-Tools is an optional extra (`pip install -e .[optimizer]`) and is **not** wired up: there is
    no CP-SAT implementation in this package, only the seam for one. That is a deliberate choice
    rather than an oversight. An untested solver path is worse than no solver path, because it is
    the infeasibility reporting -- not the optimisation -- that a dispatcher depends on, and that is
    exactly the code an unexercised implementation gets wrong.

    The function keeps its shape so that adding `CpSatDispatchOptimizer` is a change here and
    nowhere else. Tracked as gap DISPATCH-1 in `docs/vendor-integration-gaps.md`.
    """
    del prefer_solver  # honoured once a solver implementation exists
    return GreedyDispatchOptimizer()


def solve_dispatch(problem: DispatchProblem) -> DispatchPlan:
    """Convenience wrapper: pick an optimizer and run it."""
    return select_optimizer().solve(problem)


__all__ = [
    "DispatchOptimizer",
    "DispatchProblem",
    "GreedyDispatchOptimizer",
    "select_optimizer",
    "solve_dispatch",
]
