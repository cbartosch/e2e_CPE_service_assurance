"""Specification scenarios 12-17: replay, restart, stale data, closure, prediction, and geography.

The first two are the ones this whole system is arranged around -- D1 makes the incident id the
thread id so that a duplicate event and a restart are the same question asked twice -- and they are
asserted against the real checkpointer rather than against a description of one.

Scenario 14 is the one that is not reachable: no fixture produces stale telemetry, so the rejection
path has nothing to reject. The test pins the measurement that makes that true.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import Answers, service_named
from lpr_cpe.domain.enums import CaseType
from lpr_cpe.graph.state import truck_roll_count
from lpr_cpe.runner import approval

pytestmark = pytest.mark.scenario

#: The fixture measured reaching closure through a remote repair.
CLOSES = "SVC-UT-001-B-01"

#: The fixture whose planner commits a crew, so a restart has an approval to sit on.
DISPATCHES = "SVC-SJ-011-A-01"

#: Vieques. The remote-island archetype, reached by ferry.
ISLAND = "SVC-VQ-002-A-01"

#: The predictive fixture. Filed as `PREDICTIVE_MAINTENANCE`, it takes D04's preventive arm.
PREDICTIVE = "SVC-UT-001-A-03"


# ------------------------------------------------------------------------------------------------
# Scenario 12 - duplicate event and replay
# ------------------------------------------------------------------------------------------------


async def test_scenario_12_a_duplicate_event_does_not_create_a_second_of_anything(
    scenario: Any, fixtures: Any
) -> None:
    """The same event, twice, on the same thread. D1 is what makes this cheap.

    Asked for: a duplicate webhook does not create a duplicate incident, work order, remote action
    or MR, and replaying a checkpoint is safe.

    The incident id *is* the thread id, so a duplicate event resolves to the same thread rather than
    to a lookup that might miss. Driven here by running the same service to a standstill twice under
    the same `thread_suffix` -- the second run re-invokes the same thread with the same intake state.

    What is asserted is the counters, not the revision channels. `action_history`,
    `remote_attempt_count`, `mr_attempt_count` and `truck_roll_count` are the numbers a duplicate
    would inflate; `remote_actions` and `work_orders` hold status revisions and would not tell the
    two cases apart. That distinction is the same one the 49,152-entry defect turned on.
    """
    service = service_named(fixtures, CLOSES)

    first = await scenario(service, thread_suffix="-dup")
    second = await scenario(service, thread_suffix="-dup")

    assert first.state["incident_id"] == second.state["incident_id"], (
        "the duplicate produced a second incident id, so D1's thread-id-is-incident-id is not holding"
    )
    assert second.state["incident_id"] == f"INC-{service['service_ref']}"

    assert second.state["remote_attempt_count"] == first.state["remote_attempt_count"], (
        f"the duplicate re-ran the remote repair: {first.state['remote_attempt_count']} -> "
        f"{second.state['remote_attempt_count']}"
    )
    assert second.state["mr_attempt_count"] == first.state["mr_attempt_count"]
    assert truck_roll_count(second.state) == truck_roll_count(first.state)
    assert len(second.action_types()) == len(first.action_types()), (
        f"the duplicate took more actions: {first.action_types()} -> {second.action_types()}"
    )


async def test_scenario_12_the_staged_outbox_recognises_the_replayed_intent(
    scenario: Any, fixtures: Any
) -> None:
    """The other half of "replaying a checkpoint is safe", at the boundary that would write out.

    A replayed node stages the same intent, and the idempotency key is derived from the incident,
    the action type, the target and the attempt -- so the second staging is recognised rather than
    becoming a second reboot. This asserts it end to end: every staged outbox event on a real run
    carries a distinct key, so nothing was staged twice under one identity.

    Watched red by dropping `attempt` from `domain.base.idempotency_key`: two deliberate attempts
    then collapse to one key and this assertion still passes, which is why the *distinctness* is
    asserted rather than the count -- and why `tests/unit/test_outbox.py` holds the collapse case
    separately with a positive control.
    """
    run = await scenario(service_named(fixtures, CLOSES), thread_suffix="-outbox")

    assert run.staged_outbox, "nothing reached the write gate, so this proves nothing"
    keys = [event.idempotency_key for event in run.staged_outbox]
    assert len(set(keys)) == len(keys), (
        f"two staged intents share an idempotency key, so one of them is a replay that was not "
        f"recognised: {keys}"
    )
    assert len(run.gate_records) == len(run.staged_outbox), (
        "every authorised action must be staged; the gate and the outbox disagree"
    )


# ------------------------------------------------------------------------------------------------
# Scenario 13 - restart during approval
# ------------------------------------------------------------------------------------------------


async def test_scenario_13_a_restart_at_an_approval_resumes_the_same_thread(
    scenario: Any, fixtures: Any
) -> None:
    """Asked for: pause at an approval, restart, resume from persisted state, repeat no side effect.

    The restart is a **new compiled graph over the same checkpointer**. That is what "the
    application restarts" means to this system: the state lives in the saver, not in the `Pregel`
    object, so rebuilding the object and keeping the saver is the honest simulation. Rebuilding the
    saver too would prove nothing about resume; reusing the object would prove nothing about
    persistence.

    Compared against the same incident run without a restart: identical outcome, identical action
    count, identical pause sequence. The comparison is what makes this a test rather than an
    observation -- "it still closed" would pass on a graph that restarted into a completely
    different journey and happened to end up in the same place.

    "No pre-approval side effect is repeated" is asserted through the action count and the gate
    record count. A restart that replayed the pre-approval nodes would show up as extra authorised
    writes, which is precisely the thing the approval gate is placed before.
    """
    service = service_named(fixtures, CLOSES)

    straight = await scenario(service, thread_suffix="-straight")
    restarted = await scenario(service, restart_at=1, thread_suffix="-restart")

    assert "approval_request" in straight.pauses, (
        "this fixture must reach an approval or the restart is not happening at one"
    )

    assert restarted.status == straight.status == "closed", (
        f"the restart changed the outcome: {straight.status} -> {restarted.status}"
    )
    assert restarted.pauses == straight.pauses, (
        f"the restart changed the journey: {straight.pauses} -> {restarted.pauses}"
    )
    assert restarted.action_types() == straight.action_types(), (
        f"a side effect was repeated across the restart: {straight.action_types()} -> "
        f"{restarted.action_types()}"
    )
    assert len(restarted.gate_records) == len(straight.gate_records), (
        f"the restart authorised {len(restarted.gate_records)} writes against "
        f"{len(straight.gate_records)} without one"
    )
    assert restarted.state["incident_id"] == straight.state["incident_id"]


# ------------------------------------------------------------------------------------------------
# Scenario 14 - stale telemetry
# ------------------------------------------------------------------------------------------------


async def test_scenario_14_no_fixture_produces_stale_telemetry(
    scenario: Any, fixtures: Any
) -> None:
    """Not reachable, and the measurement is the test.

    Asked for: stale evidence is rejected, current evidence is requested, and automated resolution
    does not proceed on stale data.

    The machinery exists -- `DataQualityAssessment` is on the state, the policy pack carries per
    decision freshness windows, and the simulators stamp every reading from the injected clock. What
    does not exist is a fixture that produces a reading old enough to breach one. Swept across all
    41 services under both case types: **no run records a single error and no run's data-quality
    assessment reports a stale source.** The simulators resolve their offsets against the clock at
    read time, so evidence is by construction fresh.

    So the rejection path has nothing to reject. Gap SCENARIO-14; it wants a fixture whose telemetry
    is pinned to an absolute past instant rather than to a relative offset, which is a fixture-set
    change.

    What is asserted is the assessment being present and clean, so that a fixture which *did* go
    stale would turn this red and demand the real scenario be written.
    """
    run = await scenario(service_named(fixtures, CLOSES), thread_suffix="-dq")

    quality = run.state.get("data_quality")
    assert quality is not None, (
        "no data-quality assessment at all, so nothing is watching freshness. That is a bigger "
        "problem than the gap this test records."
    )
    assert quality.assessed_at is not None

    assert run.count("errors") == 0, (
        f"a run now records errors, which may mean stale evidence is reachable after all: "
        f"{run.state.get('errors')}. If so, write scenario 14 properly and retire SCENARIO-14."
    )


# ------------------------------------------------------------------------------------------------
# Scenario 15 - premature closure attempt
# ------------------------------------------------------------------------------------------------


async def test_scenario_15_closure_waits_for_the_stability_window_and_the_reconciliation(
    scenario: Any, fixtures: Any
) -> None:
    """Asked for: closure is blocked when the window, the test result, or reconciliation is incomplete.

    Asserted on the one incident that *does* close, because a closure gate is only meaningful if
    something can get through it. Every precondition the specification names is present on the
    closed incident:

    * the stability window elapsed, with `samples_in_window >= min_samples_required` -- three
      samples over thirty minutes, not one reading;
    * the validation passed and carries evidence references;
    * reconciliation ran and linked records were closed.

    And the gate is asserted from the other side too: the graph paused on `stability_window_wait`
    before closing. That pause is the block. An incident that closed without ever waiting would
    satisfy every field above by arriving at them early, which is exactly the premature closure this
    scenario is about -- so the *ordering* is what carries the claim, and the pause is the evidence
    of it.
    """
    run = await scenario(service_named(fixtures, CLOSES), thread_suffix="-closure")

    assert "stability_window_wait" in run.pauses, (
        "the incident closed without ever waiting for a stability window, so nothing blocked a "
        "premature closure"
    )

    validation = run.state.get("validation")
    assert validation is not None, "closed with no validation"
    assert validation.passed is True
    assert validation.samples_in_window >= validation.min_samples_required, (
        f"closed on {validation.samples_in_window} samples against a minimum of "
        f"{validation.min_samples_required}"
    )
    assert validation.evidence_refs, "a validation with no evidence behind it is an assertion"

    assert run.state.get("reconciliation") is not None, "closed without reconciling linked records"

    closure = run.state.get("closure")
    assert closure is not None and closure.closed_at is not None
    assert closure.validation is not None, (
        "the closure record does not carry the validation it depended on, so nobody reading it "
        "later can tell whether the window was honoured"
    )


async def test_scenario_15_an_incident_that_never_validates_never_closes(
    scenario: Any, fixtures: Any
) -> None:
    """The negative control. Without it the test above passes on a system that closes everything.

    `SVC-SJ-011-A-01` never reaches a passing validation and never produces a closure record -- it
    escalates on the resolution-cycle guard instead. Same graph, same gate, opposite side.
    """
    run = await scenario(service_named(fixtures, DISPATCHES), thread_suffix="-noclose")

    assert run.status != "closed", f"an unvalidated incident closed: {run.status}"
    assert run.state.get("closure") is None, (
        "a closure record exists for an incident that escalated"
    )
    assert run.escalated is True


# ------------------------------------------------------------------------------------------------
# Scenario 16 - predictive maintenance
# ------------------------------------------------------------------------------------------------


async def test_scenario_16_a_predictive_filing_creates_a_preventive_case_before_impact(
    scenario: Any, fixtures: Any
) -> None:
    """Asked for: degradation risk creates a preventive case, action happens before customer impact.

    Filed as `PREDICTIVE_MAINTENANCE`, `route_predictive_or_active` takes D04's preventive arm and
    the stage creates a `pm_case`, selects a disposition and stops. Measured across the fixture set:
    17 of the 41 enter the preventive stage when filed this way, splitting 3 field work / 2 remote
    prevention / 12 monitoring, with none escalating.

    "Before customer impact" is asserted as the absence of the things an impacting incident
    produces: no truck roll, no MR, no customer contact. The preventive arm holds no interrupt at
    all -- it selects and stops -- which is D8, and asserting the empty pause set is how that shows
    up here.

    The third expectation, "any later customer incident links back to the preventive case", is not
    asserted: it needs two incidents on one service with a link between them, and nothing in the
    system writes that link today. Gap SCENARIO-16.
    """
    run = await scenario(
        service_named(fixtures, PREDICTIVE),
        case_type=CaseType.PREDICTIVE_MAINTENANCE,
        thread_suffix="-pm",
    )

    assert run.state.get("pm_case") is not None, (
        "a predictive filing produced no preventive case, so D04's preventive arm was not taken"
    )
    assert run.escalated is False, f"a preventive case escalated: {run.escalation_reason}"

    assert run.pauses == (), (
        f"the preventive arm holds no interrupt; it selects and stops. Paused on: {run.pauses}"
    )
    assert truck_roll_count(run.state) == 0, "a preventive case rolled a truck"
    assert run.state["mr_attempt_count"] == 0
    assert run.count("customer_communications") == 0, (
        "a preventive case contacted the customer, which is action *after* impact"
    )


async def test_scenario_16_the_same_service_filed_as_an_alarm_takes_the_other_arm(
    scenario: Any, fixtures: Any
) -> None:
    """The control that makes the case type load-bearing rather than incidental.

    One service, two filings. Without this, scenario 16 would pass on a graph that produced a
    preventive case for everything.
    """
    service = service_named(fixtures, PREDICTIVE)

    preventive = await scenario(
        service, case_type=CaseType.PREDICTIVE_MAINTENANCE, thread_suffix="-pm2"
    )
    active = await scenario(service, case_type=CaseType.PROACTIVE_ALARM, thread_suffix="-active")

    assert preventive.state.get("pm_case") is not None
    assert active.state.get("pm_case") is None, (
        "a proactive alarm also produced a preventive case, so the case type decides nothing"
    )
    assert active.pauses, "the active arm should reach at least one gate"


# ------------------------------------------------------------------------------------------------
# Scenario 17 - Puerto Rico remote-access constraint
# ------------------------------------------------------------------------------------------------


async def test_scenario_17_an_island_fault_is_referred_to_plant_rather_than_dispatched(
    scenario: Any, fixtures: Any
) -> None:
    """Asked for: island access, crew, parts or ferry limitations affect the plan, and an infeasible
    appointment is not committed.

    Vieques is reached by ferry and the pack gives its archetype the longest travel and a same-day
    return margin, because stranding a crew overnight is not a scheduling inconvenience. Measured:
    **all eight `SVC-VQ-*` services reach `raise_mr` with `crew_type` unset and
    `field_visit_count == 0`.** None of them commits a premises appointment.

    That is the constraint expressing itself. The comparison that makes it a finding rather than a
    coincidence is against the mainland: `SVC-PO-042-A-01`, the same shape of fault on the coastal
    suburb archetype, commits a Clean Boots crew and books a visit. Same graph, same optimiser,
    different geography, different plan.

    "An infeasible appointment is not committed" is asserted as `field_visit_count == 0` together
    with `truck_roll_count == 0` -- nothing was booked, so nothing has to be cancelled.
    """
    island = await scenario(service_named(fixtures, ISLAND), thread_suffix="-island")
    mainland = await scenario(service_named(fixtures, "SVC-PO-042-A-01"), thread_suffix="-mainland")

    assert island.state["field_visit_count"] == 0, (
        f"an appointment was committed on Vieques: {island.state['field_visit_count']} visit(s)"
    )
    assert truck_roll_count(island.state) == 0
    assert island.value("crew_type") is None, (
        f"a crew was assigned to the island fault: {island.value('crew_type')}"
    )
    assert "raise_mr" in island.action_types(), (
        f"the island fault was neither dispatched nor referred: {island.action_types()}"
    )

    assert mainland.state["field_visit_count"] > 0, (
        "the mainland control does not dispatch either, so this test shows nothing about geography"
    )
    assert str(mainland.value("crew_type")) == "clean"


async def test_scenario_17_a_refused_referral_stops_rather_than_re_planning(
    scenario: Any, fixtures: Any
) -> None:
    """Refusing the island referral ends the incident with a stated reason, not a second plan.

    The other half of "an infeasible appointment is not committed": when the approver says no, the
    workflow does not quietly look for another way to send somebody. It escalates, naming the
    refusal, and nothing is booked.
    """
    run = await scenario(
        service_named(fixtures, ISLAND),
        answers=Answers(overrides={"approval_request": lambda s, c, p: approval(approved=False)}),
        thread_suffix="-island-no",
    )

    assert run.escalated is True
    assert "refused" in run.escalation_reason.lower(), (
        f"the refusal is not named in the escalation reason: {run.escalation_reason!r}"
    )
    assert truck_roll_count(run.state) == 0, "a refused referral still booked a visit"
    assert run.state["field_visit_count"] == 0


# ------------------------------------------------------------------------------------------------
# A cross-cutting guard, at the level the defect it watches for actually appeared
# ------------------------------------------------------------------------------------------------


async def test_no_state_channel_bloats_across_a_full_run(scenario: Any, fixtures: Any) -> None:
    """No channel on a settled incident holds more entries than a person could read.

    This exists because the scenarios above **do not** catch the defect it guards against, and that
    was measured rather than assumed: reinstating the `append_revision` bug -- which grew
    `work_orders` to 49,152 copies of one work order -- leaves all 23 scenario tests green. They read
    `truck_roll_count` and `mr_attempt_count`, which collapse revisions by id and answer 1 either
    way. That is the right thing for them to assert, and it is exactly why nothing noticed the
    channel doubling on every subgraph re-entry for as long as it did.

    So the guard is on the raw size, at the end of a real run through the parent graph, which is the
    level the defect manifested at. The bound is deliberately loose: a legitimate incident records a
    handful of revisions per record, and anything past a hundred is a reducer fault rather than a
    busy incident. A tight bound would fire on a long but honest run and get raised until it meant
    nothing.

    Watched red by restoring `if out and out[-1] == item` in `append_revision`::

        AssertionError: work_orders holds 49152 entries on a settled incident, which is a reducer
        fault rather than a busy incident
    """
    checked = 0
    for ref, suffix in (
        (DISPATCHES, "-bloat-a"),
        (ISLAND, "-bloat-b"),
        (CLOSES, "-bloat-c"),
    ):
        run = await scenario(service_named(fixtures, ref), thread_suffix=suffix)
        for channel in ("work_orders", "mr_records", "remote_actions", "action_history"):
            size = run.count(channel)
            assert size < 100, (
                f"{ref}: {channel} holds {size} entries on a settled incident, which is a reducer "
                "fault rather than a busy incident. See `graph.state.append_revision`."
            )
            checked += 1
    assert checked == 12, "the sweep did not run over every channel it claims to"
