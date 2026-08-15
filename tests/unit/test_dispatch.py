"""The dispatch optimizer, verified by execution against the real policy pack.

P15 forbids a language model choosing a schedule, so there is nothing here to mock: every test runs
the real solver over the real `pack.yaml` weights and asserts on the plan that comes back. A test
double would be testing the double.

The standard is the same as `test_decision_services.py`: falsification, not coverage. Two habits
follow from it.

**Every constraint is driven to failure on its own.** `test_each_constraint_is_reachable_alone`
parametrises all twelve, each with everything else satisfied. A constraint only ever observed
failing behind another one is a constraint nobody has tested -- it could be checking the wrong field,
or nothing at all, and the suite would stay green because a neighbour fired first.

**Scoring assertions show both answers.** A clamped floor like `urgency >= 0.0` passes against a
function that returns a constant, so the SLA and appointment tests below assert an *ordering*
between two inputs instead.

Four tests are marked REGRESSION and each names a defect found by running this code:

* `test_regression_a_zero_distance_island_trip_is_charged_one_crossing` -- the GIS fixture folded the
  ferry into `remote_island.fixed_overhead_minutes` (165.0, commented "The ferry is the overhead")
  while `gis.simulator` added `_FERRY_MINUTES = 95.0` again for `ferry_required`. A zero-kilometre
  trip on Vieques cost 260 minutes. The crossing now has one owner.
* `test_regression_pack_and_gis_fixture_price_the_same_geography` -- the two travel models diverged
  by up to 6x, because the pack had no ferry term at all and a comment claimed the crossing was
  folded into a 12 kph speed. `pack.yaml` points at this test by name.
* `test_regression_the_nearest_miss_is_ranked_by_shortfall` -- a crew missing three skills and a crew
  missing one each violate exactly one constraint, so ranking on the violation *count* broke the tie
  alphabetically and told the dispatcher about whichever `crew_id` sorted first.
* `test_regression_every_refusal_carries_a_parseable_code` -- the defect this package was shaped
  around, borrowed from the Wi-Fi breach-to-action map that keyed on prose and matched nothing.
  D14 routes on the binding constraint, so it has to be an enum a router can read.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lpr_cpe.dispatch.constraints import (
    ALL_CONSTRAINTS,
    Candidate,
    ConstraintCode,
    JobContext,
    all_violations,
    blocking_code,
    first_violation,
    satisfied_codes,
)
from lpr_cpe.dispatch.objective import (
    appointment_fit,
    blast_fraction,
    score_candidate,
    skill_overlap,
    sla_urgency,
)
from lpr_cpe.dispatch.optimizer import (
    DispatchProblem,
    GreedyDispatchOptimizer,
    select_optimizer,
    solve_dispatch,
)
from lpr_cpe.dispatch.travel import (
    MatrixTravelModel,
    PolicyTravelModel,
    TravelEstimate,
    haversine_km,
)
from lpr_cpe.domain.enums import AreaArchetype, CrewType, FaultDomain
from lpr_cpe.domain.field_ops import CrewSlot, DispatchPlan, DispatchRequirement
from lpr_cpe.policies import PolicyPack, load_pack
from lpr_cpe.policies.models import BlastRadiusPolicy, DispatchPolicy
from lpr_cpe.simulation.fixtures.network import AREAS

#: Fixed rather than `datetime.now(UTC)`. A schedule is a function of the clock: a baseline that
#: moved with the wall clock would satisfy the working-hours constraint in the morning and fail it
#: after 17:00, and the suite would be green or red depending on when it ran.
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)

#: Vieques. Far enough offshore that the crossing, not the driving, is the cost.
ISLAND = (18.15, -65.44)


@pytest.fixture(scope="module")
def pack() -> PolicyPack:
    return load_pack()


@pytest.fixture(scope="module")
def dispatch_policy(pack: PolicyPack) -> DispatchPolicy:
    return pack.dispatch


@pytest.fixture(scope="module")
def blast_policy(pack: PolicyPack) -> BlastRadiusPolicy:
    return pack.blast_radius


# -- builders --------------------------------------------------------------------------------
#
# Defaults chosen so the baseline candidate is feasible against every one of the twelve checks.
# Each constraint test then breaks exactly one thing, which is what makes the failure attributable.


def req(**kw: Any) -> DispatchRequirement:
    base: dict[str, Any] = {
        "requirement_id": "req-1",
        "incident_id": "inc-1",
        "created_at": NOW,
        "crew_type": CrewType.CLEAN,
        "fault_domain": FaultDomain.CUSTOMER_ENVIRONMENT,
        "area_archetype": AreaArchetype.COASTAL_CITY_SUBURB,
        "latitude": 18.0110,
        "longitude": -66.6140,
        "estimated_duration": timedelta(minutes=60),
    }
    base.update(kw)
    return DispatchRequirement(**base)


def crew(**kw: Any) -> CrewSlot:
    base: dict[str, Any] = {
        "crew_id": "crew-1",
        "crew_type": CrewType.CLEAN,
        "available_from": NOW,
        "available_until": NOW + timedelta(hours=8),
        "base_latitude": 18.0110,
        "base_longitude": -66.6140,
        "max_jobs": 6,
    }
    base.update(kw)
    return CrewSlot(**base)


def cand(
    r: DispatchRequirement | None = None,
    c: CrewSlot | None = None,
    ctx: JobContext | None = None,
    **kw: Any,
) -> Candidate:
    r = r if r is not None else req()
    start = kw.pop("start", NOW + timedelta(minutes=30))
    base: dict[str, Any] = {
        "requirement": r,
        "crew": c if c is not None else crew(),
        "context": ctx if ctx is not None else JobContext(requirement_id=r.requirement_id),
        "start": start,
        "end": start + r.estimated_duration,
        "travel": TravelEstimate(minutes=15.0, basis="estimated"),
    }
    base.update(kw)
    return Candidate(**base)


def problem(
    requirements: list[DispatchRequirement],
    crews: list[CrewSlot],
    *,
    policy: DispatchPolicy,
    blast: BlastRadiusPolicy,
    contexts: dict[str, JobContext] | None = None,
) -> DispatchProblem:
    return DispatchProblem(
        requirements=requirements,
        crews=crews,
        contexts=contexts or {},
        dispatch_policy=policy,
        blast_policy=blast,
        now=NOW,
    )


# -- travel ----------------------------------------------------------------------------------


def test_the_three_travel_terms_stay_separable(dispatch_policy: DispatchPolicy) -> None:
    """Drive, access overhead and ferry are combined but not fused.

    `drive_and_access_minutes` is the part a better route can reduce; the crossing is not, and a
    planner that could not tell them apart would keep proposing to shave a ferry.
    """
    model = PolicyTravelModel(dispatch_policy)
    island = model.between(
        from_lat=ISLAND[0],
        from_lon=ISLAND[1],
        to_lat=ISLAND[0],
        to_lon=ISLAND[1],
        archetype=AreaArchetype.REMOTE_ISLAND,
    )
    metro = model.between(
        from_lat=18.45,
        from_lon=-66.07,
        to_lat=18.495,
        to_lon=-66.07,
        archetype=AreaArchetype.METRO_MDU,
    )

    assert island.ferry_minutes == 95.0
    assert metro.ferry_minutes == 0.0
    assert island.drive_and_access_minutes == island.minutes - island.ferry_minutes


def test_missing_coordinates_are_flagged_rather_than_guessed(
    dispatch_policy: DispatchPolicy,
) -> None:
    """No coordinates still costs the access overhead, and says the number was not routed.

    Returning zero would make an unlocatable job look like the cheapest one in the queue, which is
    exactly backwards.
    """
    estimate = PolicyTravelModel(dispatch_policy).between(
        from_lat=None, from_lon=None, to_lat=None, to_lon=None, archetype=AreaArchetype.METRO_MDU
    )

    assert estimate.basis == "unknown"
    assert estimate.minutes == float(
        dispatch_policy.access_overhead_minutes(AreaArchetype.METRO_MDU)
    )


def test_an_unknown_archetype_is_priced_pessimistically(dispatch_policy: DispatchPolicy) -> None:
    """The same journey costs more with no archetype than with a known fast one.

    An optimistic default would let an unclassified job outrank a classified one on travel and be
    scheduled first on a number nobody stood behind.
    """
    model = PolicyTravelModel(dispatch_policy)
    kw: dict[str, Any] = {"from_lat": 18.0, "from_lon": -66.0, "to_lat": 18.1, "to_lon": -66.0}

    unknown = model.between(**kw, archetype=None)
    coastal = model.between(**kw, archetype=AreaArchetype.COASTAL_CITY_SUBURB)

    assert unknown.minutes > coastal.minutes


def test_a_routed_leg_is_distinguishable_from_an_estimated_one(
    dispatch_policy: DispatchPolicy,
) -> None:
    """`basis` is the whole point of the matrix model.

    A schedule costed on a straight-line average and one costed on a road network look identical,
    and only one of them is a reason to promise a customer an arrival time.
    """
    fallback = PolicyTravelModel(dispatch_policy)
    model = MatrixTravelModel(
        matrix={("18.0000,-66.0000", "18.1000,-66.0000"): 7.5},
        fallback=fallback,
        ferry_by_archetype={a: float(dispatch_policy.ferry_minutes(a)) for a in AreaArchetype},
    )
    kw: dict[str, Any] = {
        "from_lat": 18.0,
        "from_lon": -66.0,
        "archetype": AreaArchetype.COASTAL_CITY_SUBURB,
    }

    hit = model.between(**kw, to_lat=18.1, to_lon=-66.0)
    miss = model.between(**kw, to_lat=18.9, to_lon=-66.0)

    assert (hit.basis, hit.minutes) == ("routed", 7.5)
    assert miss.basis == "estimated"
    assert miss.minutes > 0.0


def test_haversine_is_zero_on_a_point_against_itself() -> None:
    assert haversine_km(18.0, -66.0, 18.0, -66.0) == 0.0


async def test_regression_a_zero_distance_island_trip_is_charged_one_crossing(
    adapters: Any, dispatch_policy: DispatchPolicy
) -> None:
    """REGRESSION: the ferry was counted twice, once by the fixture and once by the simulator.

    `remote_island.fixed_overhead_minutes` was 165.0 -- commented "The ferry is the overhead" --
    while `gis.simulator.travel_minutes` added `_FERRY_MINUTES = 95.0` on top whenever
    `ferry_required`. A crew standing on the site it was travelling to was billed 260 minutes.

    Both models are asserted because the defect was only in one of them, and a test that checked
    only the pack would have stayed green through the whole thing. The upper bound is what does the
    work: `>= 95` passes against the double count, which *is* the bug.
    """
    routed = await adapters.gis.travel_minutes(
        from_lat=ISLAND[0],
        from_lon=ISLAND[1],
        to_lat=ISLAND[0],
        to_lon=ISLAND[1],
        archetype=AreaArchetype.REMOTE_ISLAND.value,
    )
    estimated = PolicyTravelModel(dispatch_policy).between(
        from_lat=ISLAND[0],
        from_lon=ISLAND[1],
        to_lat=ISLAND[0],
        to_lon=ISLAND[1],
        archetype=AreaArchetype.REMOTE_ISLAND,
    )
    overhead = float(dispatch_policy.access_overhead_minutes(AreaArchetype.REMOTE_ISLAND))

    assert routed == pytest.approx(95.0 + overhead)
    assert estimated.minutes == pytest.approx(95.0 + overhead)
    assert routed < 2 * 95.0
    assert estimated.minutes < 2 * 95.0


@pytest.mark.parametrize("archetype", list(AreaArchetype), ids=lambda a: a.value)
def test_regression_pack_and_gis_fixture_price_the_same_geography(
    archetype: AreaArchetype, dispatch_policy: DispatchPolicy
) -> None:
    """REGRESSION: the planning fallback and the routing lookup disagreed by up to 6x.

    Two travel models exist on purpose -- `GISAdapter.travel_minutes` is a routing engine in
    production, the pack's archetype numbers are what dispatch falls back to when it is unavailable
    or coordinates are missing. What is not on purpose is them modelling different geography, which
    is what happened while the pack had no ferry term and a comment claiming the crossing was folded
    into a 12 kph speed.

    `pack.yaml` promises this test by name, so editing one model without the other goes red here.
    """
    area = AREAS[archetype.value]
    pack_per_km = 60.0 / dispatch_policy.speed_kph(archetype)
    gis_per_km = float(area["travel_minutes_per_km"])

    assert abs(pack_per_km - gis_per_km) / gis_per_km <= 0.05
    assert dispatch_policy.access_overhead_minutes(archetype) == float(
        area["fixed_overhead_minutes"]
    )
    # The crossing is priced where the fixture says there is one, and nowhere else.
    assert (dispatch_policy.ferry_minutes(archetype) > 0) is bool(area["ferry_required"])


# -- constraints -----------------------------------------------------------------------------


def _constraint_cases() -> list[tuple[ConstraintCode, Candidate]]:
    """One candidate per constraint, each failing that constraint and satisfying the other eleven."""
    later = (NOW + timedelta(hours=4), NOW + timedelta(hours=5))
    return [
        (ConstraintCode.SKILL, cand(req(skills_required=["fiber_splicing"]))),
        (ConstraintCode.CREW_TYPE, cand(req(crew_type=CrewType.DIRTY))),
        (ConstraintCode.EQUIPMENT, cand(req(equipment_required=["otdr"]))),
        (ConstraintCode.PARTS, cand(req(parts_required=["drop_cable"]))),
        (
            ConstraintCode.WORKING_HOURS,
            cand(
                req(estimated_duration=timedelta(hours=3)),
                start=NOW + timedelta(hours=7, minutes=30),
            ),
        ),
        (
            ConstraintCode.CUSTOMER_ACCESS,
            cand(
                req(customer_access_required=True, customer_availability_windows=[later]),
            ),
        ),
        (
            ConstraintCode.BUILDING_ACCESS,
            cand(ctx=JobContext(requirement_id="req-1", building_access_windows=(later,))),
        ),
        (
            ConstraintCode.SAFETY,
            cand(ctx=JobContext(requirement_id="req-1", aerial_work_required=True, wind_kph=60.0)),
        ),
        (ConstraintCode.GEOGRAPHY, cand(c=crew(area_archetypes=[AreaArchetype.METRO_MDU]))),
        (
            ConstraintCode.REMOTE_ACCESS_WINDOW,
            cand(req(area_archetype=AreaArchetype.REMOTE_ISLAND), start=NOW.replace(hour=14)),
        ),
        (ConstraintCode.CAPACITY, cand(jobs_already_assigned=6)),
        (
            ConstraintCode.WORK_ORDER_DEPENDENCY,
            cand(ctx=JobContext(requirement_id="req-1", depends_on=("req-0",))),
        ),
    ]


def test_the_baseline_candidate_satisfies_all_twelve(dispatch_policy: DispatchPolicy) -> None:
    """The control. Without it, every case below could be failing for the wrong reason."""
    baseline = cand()

    assert first_violation(baseline, dispatch_policy) is None
    assert set(satisfied_codes(baseline, dispatch_policy)) == set(ConstraintCode)


@pytest.mark.parametrize(
    ("expected", "candidate"), _constraint_cases(), ids=[c.value for c, _ in _constraint_cases()]
)
def test_each_constraint_is_reachable_alone(
    expected: ConstraintCode, candidate: Candidate, dispatch_policy: DispatchPolicy
) -> None:
    """Each of the twelve fires on its own, names itself, and drops out of the satisfied set.

    The third assertion is not redundant with the first: a check that reported a violation but was
    still counted as satisfied would leave `satisfied_codes` claiming twelve-of-twelve on a
    candidate nobody can work, and the plan's `satisfied:` note would be a lie.
    """
    violation = first_violation(candidate, dispatch_policy)

    assert violation is not None
    assert violation.code is expected
    assert expected not in satisfied_codes(candidate, dispatch_policy)


def test_the_twelve_cases_cover_every_declared_code() -> None:
    """The parametrisation above is exhaustive, and stays exhaustive when a code is added.

    Without this, adding a thirteenth `ConstraintCode` and forgetting the case would look green.
    """
    assert {code for code, _ in _constraint_cases()} == set(ConstraintCode)
    assert len(ALL_CONSTRAINTS) == len(ConstraintCode)


def test_unknown_wind_does_not_block_aerial_work(dispatch_policy: DispatchPolicy) -> None:
    """The one documented asymmetry: absent weather reads permissively.

    The alternative -- refusing every aerial job whenever the GIS adapter is down -- would turn one
    integration outage into an island-wide dispatch freeze. Refusing on a *measured* breach is the
    safety property; refusing on a missing reading is an availability bug.
    """
    candidate = cand(
        ctx=JobContext(requirement_id="req-1", aerial_work_required=True, wind_kph=None)
    )

    assert first_violation(candidate, dispatch_policy) is None


def test_a_job_that_overruns_its_window_fails_access(dispatch_policy: DispatchPolicy) -> None:
    """The window has to contain the whole visit, not just its start.

    Checking only the start is the natural off-by-one here, and it books a two-hour job into a
    one-hour window -- the crew arrives, the customer leaves at the hour, and the visit is recorded
    as a failed access nobody predicted.
    """
    candidate = cand(
        req(
            customer_access_required=True,
            estimated_duration=timedelta(hours=2),
            customer_availability_windows=[(NOW, NOW + timedelta(hours=1))],
        ),
        start=NOW,
    )

    violation = first_violation(candidate, dispatch_policy)

    assert violation is not None
    assert violation.code is ConstraintCode.CUSTOMER_ACCESS


def test_all_violations_reports_more_than_the_first(dispatch_policy: DispatchPolicy) -> None:
    """`first_violation` short-circuits for the hot path; the full list is what a dispatcher needs.

    A crew told only about the missing skill will fix that, come back, and be told about the
    missing equipment.
    """
    candidate = cand(req(skills_required=["x"], equipment_required=["y"]))

    codes = {v.code for v in all_violations(candidate, dispatch_policy)}

    assert {ConstraintCode.SKILL, ConstraintCode.EQUIPMENT} <= codes


def test_a_violation_renders_a_code_a_router_can_read(dispatch_policy: DispatchPolicy) -> None:
    """Rendered explanations round-trip back to the enum they came from."""
    violation = first_violation(cand(req(skills_required=["fiber_splicing"])), dispatch_policy)

    assert violation is not None
    assert blocking_code(violation.render()) is ConstraintCode.SKILL


# -- objective -------------------------------------------------------------------------------


def test_sla_urgency_is_measured_against_the_job_it_is_scoring() -> None:
    """The same remaining budget is more urgent for a longer job.

    A fixed horizon would rank ninety minutes of headroom identically for a fifteen-minute reset and
    a two-hour plant repair, and only one of those is in trouble.
    """
    assert sla_urgency(300.0, at_risk=False, job_minutes=120) > sla_urgency(
        300.0, at_risk=False, job_minutes=15
    )


def test_sla_urgency_saturates_when_the_budget_cannot_be_met() -> None:
    """Breached and unfinishable both score 1.0, and comfortable scores near zero."""
    assert sla_urgency(-5.0, at_risk=True, job_minutes=60) == 1.0
    assert sla_urgency(30.0, at_risk=False, job_minutes=60) == 1.0
    assert sla_urgency(6000.0, at_risk=False, job_minutes=60) < 0.05
    assert sla_urgency(120.0, at_risk=False, job_minutes=60) > sla_urgency(
        600.0, at_risk=False, job_minutes=60
    )


def test_absent_sla_data_returns_the_flag_rather_than_a_guess() -> None:
    """`None` remaining is not a middling urgency.

    Inventing one would sort real deadlines below imaginary ones, which is worse than admitting the
    context was not supplied.
    """
    assert sla_urgency(None, at_risk=False, job_minutes=60) == 0.0
    assert sla_urgency(None, at_risk=True, job_minutes=60) == 1.0


def test_blast_fraction_reuses_the_pack_threshold(blast_policy: BlastRadiusPolicy) -> None:
    """Normalised against the network-action threshold the pack already defines, and clamped.

    A second divisor invented here would drift from the blast-radius gate, and the dispatch queue
    would disagree with the gate about which incidents are network-scale.
    """
    assert blast_fraction(10_000, blast_policy) == 1.0
    assert blast_fraction(1, blast_policy) < 0.2
    assert blast_fraction(blast_policy.network_action_threshold, blast_policy) == 1.0


def test_skill_overlap_scores_a_job_needing_nothing_as_a_full_match() -> None:
    """No required skills is not a job every crew is equally unsuited to."""
    assert skill_overlap([], ["a"]) == 1.0
    assert skill_overlap(["a", "b"], ["a"]) == 0.5


def test_a_stated_window_outscores_no_window_which_outscores_a_missed_one() -> None:
    """The 0.5 middle is load-bearing.

    Scoring an unstated window 0.0 sorts every no-preference customer behind everyone who named a
    time, which over a full queue means the customers who ask for nothing are served last.
    """
    inside = cand(
        req(
            customer_access_required=True,
            customer_availability_windows=[(NOW, NOW + timedelta(hours=8))],
        )
    )
    missed = cand(
        req(
            customer_access_required=True,
            customer_availability_windows=[(NOW + timedelta(hours=6), NOW + timedelta(hours=7))],
        )
    )

    assert appointment_fit(inside) == 1.0
    assert appointment_fit(cand()) == 0.5
    assert appointment_fit(missed) == 0.0


def test_the_breakdown_nets_travel_off_the_benefits(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Six terms are retained separately, and the total is the five benefits minus the one cost.

    A single objective float is unauditable: "why did the Bayamon job go first" has no answer from
    it.
    """
    score = score_candidate(cand(), policy=dispatch_policy, blast=blast_policy)

    assert score.travel_cost > 0.0
    assert score.total == pytest.approx(
        score.sla_risk
        + score.blast_radius
        + score.crew_skill_match
        + score.appointment_window
        + score.vulnerable_customer
        - score.travel_cost
    )
    assert set(score.as_dict()) == {
        "sla_risk",
        "blast_radius",
        "travel_cost",
        "crew_skill_match",
        "appointment_window",
        "vulnerable_customer",
        "total",
    }


def test_a_vulnerable_customer_moves_exactly_one_term(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Which is how the weight is testable at all: setting one flag must move one term and the total.

    Asserting only on the total would pass against a weight wired into the wrong term.
    """
    plain = score_candidate(cand(), policy=dispatch_policy, blast=blast_policy)
    vulnerable = score_candidate(
        cand(ctx=JobContext(requirement_id="req-1", vulnerable_customer=True)),
        policy=dispatch_policy,
        blast=blast_policy,
    )

    assert vulnerable.total > plain.total
    assert vulnerable.vulnerable_customer > plain.vulnerable_customer
    changed = {
        term for term, value in vulnerable.as_dict().items() if value != plain.as_dict()[term]
    }
    assert changed == {"vulnerable_customer", "total"}


# -- solving ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def metro_problem(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> DispatchProblem:
    """Three nearby jobs, two crews, and three different SLA standings to sort by."""
    return problem(
        [
            req(requirement_id="req-a", latitude=18.0110, longitude=-66.6140),
            req(requirement_id="req-b", latitude=18.0200, longitude=-66.6200),
            req(requirement_id="req-c", latitude=18.0300, longitude=-66.6300),
        ],
        [crew(crew_id="crew-1"), crew(crew_id="crew-2")],
        policy=dispatch_policy,
        blast=blast_policy,
        contexts={
            "req-a": JobContext(requirement_id="req-a", sla_remaining_minutes=90.0),
            "req-b": JobContext(
                requirement_id="req-b", sla_remaining_minutes=600.0, affected_customers=40
            ),
            "req-c": JobContext(requirement_id="req-c", sla_remaining_minutes=None),
        },
    )


@pytest.fixture(scope="module")
def metro_plan(metro_problem: DispatchProblem) -> DispatchPlan:
    return solve_dispatch(metro_problem)


def test_a_feasible_problem_places_every_requirement(metro_plan: DispatchPlan) -> None:
    assert len(metro_plan.assignments) == 3
    assert metro_plan.unassigned == []
    assert metro_plan.solver_status == "ok"


def test_the_tightest_sla_is_scheduled_first(metro_plan: DispatchPlan) -> None:
    """`req-a` has 90 minutes for a 60-minute job; `req-b` has ten hours and forty customers.

    Blast radius is a term, not the term -- an incident that is about to breach outranks a larger
    one that is not.
    """
    assert metro_plan.assignments[0].requirement_id == "req-a"


def test_the_plan_reports_how_its_travel_was_costed(metro_plan: DispatchPlan) -> None:
    """The objective string carries the basis, so a reader knows whether it was routed.

    Without it a plan built entirely on straight-line fallbacks is indistinguishable from one
    costed on a road network.
    """
    assert metro_plan.objective.startswith("weighted_sla_and_travel:")
    assert metro_plan.objective.endswith("estimated")


def test_reported_totals_match_the_legs(metro_plan: DispatchPlan) -> None:
    """The summary is derived from the assignments rather than accumulated alongside them."""
    assert metro_plan.total_travel_minutes == pytest.approx(
        sum(a.travel_minutes for a in metro_plan.assignments), abs=0.05
    )
    assert metro_plan.solve_duration is not None
    assert metro_plan.objective_value is not None


def test_an_assignment_records_which_constraints_it_cleared(metro_plan: DispatchPlan) -> None:
    """Assigned requirements are explained too, not only refused ones.

    An approver reviewing a plan needs to see what was checked, not just what failed.
    """
    for assignment in metro_plan.assignments:
        note = metro_plan.constraint_explanation[assignment.requirement_id]
        assert note.startswith("satisfied:")


def test_the_plan_is_identical_under_shuffled_input(
    metro_problem: DispatchProblem,
    metro_plan: DispatchPlan,
    dispatch_policy: DispatchPolicy,
    blast_policy: BlastRadiusPolicy,
) -> None:
    """Twenty-four shuffles of both lists produce byte-identical assignments.

    This is the property that lets an approval be replayed: the plan recomputed during review has
    to be the plan the approver saw. Any ordering that fell back on set or dict iteration order
    would break here, and nowhere else.
    """
    rng = random.Random(20260814)
    baseline = [(a.requirement_id, a.crew_id, a.scheduled_start) for a in metro_plan.assignments]

    for _ in range(24):
        requirements = list(metro_problem.requirements)
        crews = list(metro_problem.crews)
        rng.shuffle(requirements)
        rng.shuffle(crews)
        other = solve_dispatch(
            problem(
                requirements,
                crews,
                policy=dispatch_policy,
                blast=blast_policy,
                contexts=dict(metro_problem.contexts),
            )
        )
        assert [
            (a.requirement_id, a.crew_id, a.scheduled_start) for a in other.assignments
        ] == baseline


def test_the_objective_value_is_stable_across_runs(
    metro_problem: DispatchProblem, metro_plan: DispatchPlan
) -> None:
    assert solve_dispatch(metro_problem).objective_value == metro_plan.objective_value


def test_no_crew_exceeds_its_cap_and_the_overflow_is_explained(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Eight jobs, two crews, three each. The two that do not fit are named, not dropped."""
    plan = solve_dispatch(
        problem(
            [req(requirement_id=f"req-{i:02d}") for i in range(8)],
            [crew(crew_id="crew-1", max_jobs=3), crew(crew_id="crew-2", max_jobs=3)],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    per_crew = dict.fromkeys(("crew-1", "crew-2"), 0)
    for assignment in plan.assignments:
        per_crew[assignment.crew_id] += 1

    assert max(per_crew.values()) <= 3
    assert len(plan.unassigned) == 2
    assert all(
        blocking_code(plan.constraint_explanation[r]) is ConstraintCode.CAPACITY
        for r in plan.unassigned
    )


def test_the_start_respects_both_the_customer_and_the_building_window(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Customer 09:00-12:00 and building 11:00-15:00 leaves exactly one hour that works.

    Honouring either window alone books a visit that fails at the door. The optimizer has to
    intersect them, not pick one.
    """
    plan = solve_dispatch(
        problem(
            [
                req(
                    requirement_id="req-w",
                    customer_access_required=True,
                    customer_availability_windows=[(NOW.replace(hour=9), NOW.replace(hour=12))],
                )
            ],
            [crew(crew_id="crew-1")],
            policy=dispatch_policy,
            blast=blast_policy,
            contexts={
                "req-w": JobContext(
                    requirement_id="req-w",
                    building_access_windows=((NOW.replace(hour=11), NOW.replace(hour=15)),),
                )
            },
        )
    )

    assert plan.assignments, plan.constraint_explanation
    assert plan.assignments[0].scheduled_start.hour == 11


def test_a_predecessor_is_pulled_ahead_of_its_more_urgent_dependent(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """`req-second` has 30 minutes left and `req-first` has eighty hours, and order still holds.

    Urgency alone would queue the dependent first, find its predecessor unscheduled, and refuse it
    on `work_order_dependency` -- a deadlock the optimizer created for itself.
    """
    plan = solve_dispatch(
        problem(
            [req(requirement_id="req-first"), req(requirement_id="req-second")],
            [crew(crew_id="crew-1")],
            policy=dispatch_policy,
            blast=blast_policy,
            contexts={
                "req-second": JobContext(
                    requirement_id="req-second",
                    sla_remaining_minutes=30.0,
                    depends_on=("req-first",),
                ),
                "req-first": JobContext(requirement_id="req-first", sla_remaining_minutes=5000.0),
            },
        )
    )

    assert [a.requirement_id for a in plan.assignments] == ["req-first", "req-second"]


def test_an_island_leg_carries_the_crossing_into_the_plan(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """The ferry survives the trip from the travel model into the assignment's `travel_minutes`.

    This is what makes joint dispatch worth doing on Vieques: a second visit costs another crossing,
    and a plan that priced the leg at eight minutes would never say so.
    """
    plan = solve_dispatch(
        problem(
            [
                req(
                    requirement_id="req-i1",
                    area_archetype=AreaArchetype.REMOTE_ISLAND,
                    latitude=ISLAND[0],
                    longitude=ISLAND[1],
                )
            ],
            [
                crew(
                    crew_id="crew-i",
                    base_latitude=ISLAND[0],
                    base_longitude=ISLAND[1],
                    area_archetypes=[AreaArchetype.REMOTE_ISLAND],
                )
            ],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    assert plan.assignments, plan.constraint_explanation
    assert plan.assignments[0].travel_minutes >= 95.0


def test_a_late_island_start_is_refused_with_the_access_window_named(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Everything else is satisfiable; the last boat is not.

    The crew is on shift until 20:00 and the job is otherwise clean, so nothing but the crossing
    window can explain the refusal -- which is what makes the reported code checkable.
    """
    plan = solve_dispatch(
        problem(
            [
                req(
                    requirement_id="req-i2",
                    area_archetype=AreaArchetype.REMOTE_ISLAND,
                    latitude=ISLAND[0],
                    longitude=ISLAND[1],
                    earliest_start=NOW.replace(hour=14),
                )
            ],
            [
                crew(
                    crew_id="crew-i",
                    base_latitude=ISLAND[0],
                    base_longitude=ISLAND[1],
                    available_from=NOW.replace(hour=13),
                    available_until=NOW.replace(hour=20),
                )
            ],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    assert plan.unassigned == ["req-i2"]
    assert (
        blocking_code(plan.constraint_explanation["req-i2"]) is ConstraintCode.REMOTE_ACCESS_WINDOW
    )


# -- refusals --------------------------------------------------------------------------------


def test_an_impossible_requirement_is_reported_not_dropped(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """D14's actual requirement: which constraint is binding, and no committed slot.

    A plan that silently omitted the job would leave the incident in `dispatch_planning` with a
    plan attached and nobody scheduled, and the state would look healthy.
    """
    plan = solve_dispatch(
        problem(
            [req(requirement_id="req-x", skills_required=["fiber_splicing", "bucket_truck"])],
            [crew(crew_id="crew-1")],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    assert plan.unassigned == ["req-x"]
    assert plan.solver_status == "partial"
    assert plan.assignments == []
    reason = plan.constraint_explanation["req-x"]
    assert blocking_code(reason) is ConstraintCode.SKILL
    assert "bucket_truck" in reason and "fiber_splicing" in reason


def test_no_crews_at_all_is_a_capacity_refusal(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """Mapped onto an existing code rather than given a thirteenth of its own.

    The twelve are the ones the specification names, and a runbook keyed on them would have no
    entry for an invented one. "No crew was available" is what capacity means at zero.
    """
    plan = solve_dispatch(
        problem([req(requirement_id="req-y")], [], policy=dispatch_policy, blast=blast_policy)
    )

    assert plan.unassigned == ["req-y"]
    assert blocking_code(plan.constraint_explanation["req-y"]) is ConstraintCode.CAPACITY


def test_regression_the_nearest_miss_is_ranked_by_shortfall(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """REGRESSION: ranking refusals by violation *count* broke ties alphabetically.

    `crew-far` holds nothing and `crew-near` holds two of the three required skills. Both fail
    exactly one constraint -- `skill` -- so a `len(violations)` ranking scored them equal and the
    tie-break on `crew_id` pointed the dispatcher at `crew-far`, the crew furthest from being able
    to do the job. Sorting on summed `shortfall` picks the crew one certificate away.

    Both assertions are needed: naming `crew-near` alone would pass against a rule that always
    reported the last crew evaluated.
    """
    plan = solve_dispatch(
        problem(
            [req(requirement_id="req-z", skills_required=["a", "b", "c"])],
            [crew(crew_id="crew-far", skills=[]), crew(crew_id="crew-near", skills=["a", "b"])],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    # Matched on whole words rather than substrings: "lacks" contains an "a", and a naive
    # `"a" not in reason` passes or fails on the phrasing rather than on the skills named.
    words = set(re.findall(r"[\w-]+", plan.constraint_explanation["req-z"]))

    assert "crew-near" in words
    assert "crew-far" not in words
    # And it names the one skill actually missing, not all three.
    assert "c" in words
    assert not ({"a", "b"} & words)


def test_regression_every_refusal_carries_a_parseable_code(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy
) -> None:
    """REGRESSION: prose is not a machine vocabulary.

    The defect this package was shaped around lived in the Wi-Fi breach-to-action map, which keyed
    on a human-readable phrase, matched nothing once the phrasing drifted, and silently recommended
    no action for every verdict. D14 routes on the binding constraint, so the same failure here
    would send infeasible dispatches to no queue at all -- while every explanation still read
    perfectly well to a human reviewing the plan.

    Every refusal the solver can produce is parsed back to the enum, over problems chosen to refuse
    for four different reasons.
    """
    plans = [
        solve_dispatch(
            problem(
                [req(requirement_id="req-x", skills_required=["fiber_splicing"])],
                [crew(crew_id="crew-1")],
                policy=dispatch_policy,
                blast=blast_policy,
            )
        ),
        solve_dispatch(
            problem([req(requirement_id="req-y")], [], policy=dispatch_policy, blast=blast_policy)
        ),
        solve_dispatch(
            problem(
                [req(requirement_id=f"req-{i:02d}") for i in range(4)],
                [crew(crew_id="crew-1", max_jobs=1)],
                policy=dispatch_policy,
                blast=blast_policy,
            )
        ),
        solve_dispatch(
            problem(
                [req(requirement_id="req-d", equipment_required=["bucket_truck"])],
                [crew(crew_id="crew-1")],
                policy=dispatch_policy,
                blast=blast_policy,
            )
        ),
    ]

    refused = [(p, r) for p in plans for r in p.unassigned]
    assert len(refused) >= 4, "no refusals to check"
    for plan, requirement_id in refused:
        reason = plan.constraint_explanation[requirement_id]
        assert blocking_code(reason) is not None, reason
        # The code is a prefix, not buried mid-sentence, so a router never has to scan prose.
        assert reason.split(":")[0] in {c.value for c in ConstraintCode}


# -- plan invariants -------------------------------------------------------------------------


def test_a_requirement_is_never_both_assigned_and_unassigned(
    dispatch_policy: DispatchPolicy, blast_policy: BlastRadiusPolicy, metro_plan: DispatchPlan
) -> None:
    """Holds for full, partial and capacity-limited plans alike."""
    partial = solve_dispatch(
        problem(
            [req(requirement_id="req-x", skills_required=["fiber_splicing"])],
            [crew(crew_id="crew-1")],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )
    capped = solve_dispatch(
        problem(
            [req(requirement_id=f"req-{i:02d}") for i in range(4)],
            [crew(crew_id="crew-1", max_jobs=2)],
            policy=dispatch_policy,
            blast=blast_policy,
        )
    )

    for plan in (metro_plan, partial, capped):
        assert not (plan.assigned_requirement_ids & set(plan.unassigned))
        assert all(r in plan.constraint_explanation for r in plan.unassigned)
        assert all(blocking_code(plan.constraint_explanation[r]) for r in plan.unassigned)


def test_the_solver_names_itself_in_the_plan(metro_plan: DispatchPlan) -> None:
    """So a plan reviewed a week later says what produced it."""
    assert GreedyDispatchOptimizer().name == "greedy"
    assert metro_plan.solver == "greedy"


def test_the_solver_seam_returns_greedy_either_way() -> None:
    """`prefer_solver` is a seam, not a feature, and this test says so out loud.

    There is no CP-SAT implementation -- tracked as gap DISPATCH-1. Asserting the factory honours a
    preference it cannot satisfy would be the kind of test that makes an unimplemented path look
    implemented, so this asserts the opposite: both settings return the greedy solver today, and
    this test is what goes red when that stops being true.
    """
    assert select_optimizer(prefer_solver=True).name == "greedy"
    assert select_optimizer(prefer_solver=False).name == "greedy"
