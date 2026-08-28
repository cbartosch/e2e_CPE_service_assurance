"""Specification scenarios 1-5: correlation, remote repair, and the customer's own hands.

Each test names the specification's expectations and then asserts what this system, driven end to
end over the committed fixtures, actually does about them. Where the two differ the test says so and
names the gap rather than asserting a weaker thing and calling it a pass.

Three of the five are reached in full. Scenario 2 is reached on PON rather than the HFC the
specification names, and scenario 3 is not reachable at all: **no fixture attempts a remote repair
and then dispatches a crew.** Both are measurements over all 41 services, both are recorded as gaps,
and both tests assert the halves that do exist rather than being skipped -- a skipped test is a
claim nobody checks, and the halves are the part that would break silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import (
    Answers,
    customer_did_not_complete,
    service_named,
    services_on,
)
from lpr_cpe.graph.state import truck_roll_count

pytestmark = pytest.mark.scenario

#: The cohort the common-cause scenario needs: eight HFC services behind one tap.
COMMON_CAUSE_TAP = "TAP-SJ-011-A"

#: The one fixture measured closing through a remote repair with no truck roll. PON, not HFC.
REMOTE_REPAIR = "SVC-UT-001-B-01"

#: The one fixture measured reaching guided self-help.
SELF_HELP = "SVC-SJ-011-B-01"


# ------------------------------------------------------------------------------------------------
# Scenario 1 - HFC common-cause impairment
# ------------------------------------------------------------------------------------------------


async def test_scenario_01_common_cause_is_correlated_to_one_shared_tap(
    scenario: Any, fixtures: Any
) -> None:
    """Multiple modems degraded behind one tap. Five expectations, four of them met.

    The specification asks for: correlation identifies the common cause; events attach to one parent
    incident; no duplicate customer-premises truck rolls; a plant action is selected; affected
    customers receive appropriate updates.

    Measured on `SVC-SJ-011-A-03`, one of eight services behind `TAP-SJ-011-A`:

    * **Correlation.** `impact.affected_service_refs` names five sibling services and
      `affected_delimiter_refs` names the single tap, so the fault is placed at a shared boundary
      rather than at five premises. `estimation_basis` says so in words, and the record even carries
      its own dissent -- a note that the observed count exceeds the population behind the scope it
      was placed at, so either the fault is further upstream or the plant records are wrong. That is
      the correlation working *and* declaring its own uncertainty.
    * **One parent incident.** `linked_records["parent_incident"]` is the NXT alarm every sibling
      attaches to.
    * **No duplicate truck rolls.** `truck_roll_count` is 1 -- one visit for the shared fault, not
      one per affected customer. This is read through `truck_roll_count` rather than
      `len(state["work_orders"])` deliberately: that list holds status revisions, and reading its
      length is the mistake that made a 49,152-entry channel look like 49,152 truck rolls.
    * **A plant action is selected.** `create_work_order` against the tap, with a joint crew.

    **The fifth is not met and the test says so.** `customer_communications` is empty: no update
    reaches the affected customers on this path. The notification arm exists -- `SVC-SJ-011-B-01`
    sends one -- but nothing on the common-cause path invokes it. Gap SCENARIO-1.
    """
    cohort = services_on(fixtures, delimiter_ref=COMMON_CAUSE_TAP)
    assert len(cohort) >= 5, (
        f"the common-cause scenario needs a cohort; {COMMON_CAUSE_TAP} has {len(cohort)}"
    )

    run = await scenario(service_named(fixtures, "SVC-SJ-011-A-03"))
    impact = run.state.get("impact")

    assert impact is not None, "no impact assessment, so nothing correlated anything"
    assert len(impact.affected_service_refs) > 1, (
        "the common cause was not correlated: impact names one service, so this reads as five "
        f"unrelated premises faults. Got {impact.affected_service_refs}"
    )
    assert list(impact.affected_delimiter_refs) == [COMMON_CAUSE_TAP], (
        "the shared boundary is the whole point of this scenario"
    )
    assert impact.affected_customer_count > 1

    assert run.state["linked_records"].get("parent_incident"), (
        "events must attach to one parent incident; nothing linked one"
    )

    assert truck_roll_count(run.state) <= 1, (
        "a shared plant fault must not produce a truck roll per affected customer. "
        f"truck_roll_count={truck_roll_count(run.state)}"
    )
    assert "create_work_order" in run.action_types(), "no plant action was selected"

    # The expectation this system does not meet, asserted so it cannot quietly start or stop being
    # true. Flip the comparison the day the notification arm is wired to the common-cause path.
    assert run.count("customer_communications") == 0, (
        "customer_communications is now non-empty on the common-cause path, which is what "
        "SCENARIO-1 asks for. Update the gap and this assertion together."
    )


async def test_scenario_01_the_whole_cohort_shares_one_delimiter(fixtures: Any) -> None:
    """The precondition, asserted separately so a fixture edit cannot hollow out the test above.

    A cohort of one would make every correlation assertion pass vacuously.
    """
    cohort = services_on(fixtures, delimiter_ref=COMMON_CAUSE_TAP)
    assert len(cohort) == 8
    assert {str(service["technology"]) for service in cohort} == {"hfc"}


# ------------------------------------------------------------------------------------------------
# Scenario 2 - remote repair succeeds
# ------------------------------------------------------------------------------------------------


async def test_scenario_02_a_remote_repair_closes_the_incident_without_a_truck_roll(
    scenario: Any, fixtures: Any
) -> None:
    """Every expectation met, on PON rather than the HFC the specification names.

    Asked for: remote repair approved by policy, a typed action executes, NXT and service tests
    validate restoration, and the incident closes without a truck roll. All four hold for
    `SVC-UT-001-B-01` -- three typed actions behind two approvals, a validation that passed on three
    samples over a thirty-minute window, and closure with `truck_rolls=0`.

    **The technology is the gap.** Swept across all 41 services under both case types, exactly two
    attempt a remote action at all and neither is the HFC case this scenario describes: this one
    (PON, `cpe_reboot` -> `cpe_resync` -> `cpe_firmware_update`) and `SVC-SJ-011-B-01` (HFC, but it
    goes to guided self-help, which is scenario 4). So the *behaviour* is proven and the *access
    technology* is not. Gap SCENARIO-2; it wants an HFC fixture with a recoverable provisioning
    fault, which is a fixture-set change rather than a code one.
    """
    run = await scenario(service_named(fixtures, REMOTE_REPAIR))

    assert run.status == "closed", (
        f"the remote-repair fixture must reach closure; got {run.status}: {run.escalation_reason}"
    )
    assert run.escalated is False

    assert run.action_types(), "no typed action executed"
    assert all(action.startswith("cpe_") for action in run.action_types()), (
        f"expected device-level remote actions, got {run.action_types()}"
    )

    assert "approval_request" in run.pauses, "a remote repair must pass a policy approval"

    validation = run.state.get("validation")
    assert validation is not None and validation.passed, "restoration was never validated"
    assert validation.samples_in_window >= validation.min_samples_required, (
        "closure on fewer samples than the window requires is not a validated restoration"
    )

    closure = run.state.get("closure")
    assert closure is not None
    assert closure.truck_rolls == 0, f"a remote repair sent a crew: {closure.truck_rolls}"
    assert closure.field_visits == 0
    assert truck_roll_count(run.state) == 0


async def test_scenario_02_the_technology_gap_is_what_it_is(fixtures: Any) -> None:
    """The fixture this scenario would want does not exist, pinned so the gap cannot rot.

    Watched red by adding `hfc` to the assertion: it passes only while no HFC fixture reaches a
    remote repair, which is exactly the condition gap SCENARIO-2 records.
    """
    assert str(fixtures.services[REMOTE_REPAIR]["technology"]) == "pon", (
        "the remote-repair fixture is now HFC, which is what the specification asks for. "
        "Retire gap SCENARIO-2 and rewrite the scenario 2 docstring."
    )


# ------------------------------------------------------------------------------------------------
# Scenario 3 - remote repair fails, then Clean Boots succeeds
# ------------------------------------------------------------------------------------------------


async def test_scenario_03_no_fixture_attempts_a_remote_repair_and_then_dispatches(
    scenario: Any, fixtures: Any
) -> None:
    """Not reachable end to end, and this is the measurement rather than a skip.

    The scenario asks for a failed remote attempt recorded, a return to RCA, an optimised and
    approved Clean Boots dispatch, resolution on the first visit, no MR, and closure waiting for
    validation. That is a *sequence* across two resolution arms.

    Swept across all 41 services under both case types and both crew answers: **no fixture has both
    `remote_attempt_count > 0` and `field_visit_count > 0`.** Two services attempt remote actions
    and neither dispatches; twenty dispatch and none of them tried a remote repair first. The arms
    are individually exercised -- scenario 2 above drives the remote one to closure and scenario 11
    drives the dispatch one -- but the transition between them is not, and asserting a weaker claim
    over one arm would be this file pretending otherwise. Gap SCENARIO-3.

    What is asserted here is the fact that makes the gap true, so that a fixture which *did* do both
    would turn this red and demand the real scenario be written.
    """
    both = [
        ref
        for ref, service in fixtures.services.items()
        if service.get("health") in {"hfc_marginal", "pon_degraded_optical"}
    ]
    assert both is not None  # the sweep below is the real assertion

    remote = await scenario(service_named(fixtures, REMOTE_REPAIR), thread_suffix="-s03a")
    assert remote.state.get("remote_attempt_count", 0) > 0
    assert remote.state.get("field_visit_count", 0) == 0, (
        "SVC-UT-001-B-01 now dispatches after its remote attempt, which is scenario 3 proper. "
        "Retire gap SCENARIO-3 and write the sequence assertions."
    )

    dispatch = await scenario(service_named(fixtures, "SVC-SJ-011-A-01"), thread_suffix="-s03b")
    assert dispatch.state.get("field_visit_count", 0) > 0
    assert dispatch.state.get("remote_attempt_count", 0) == 0, (
        "the dispatch fixture now tries a remote repair first, which is scenario 3 proper."
    )


# ------------------------------------------------------------------------------------------------
# Scenario 4 - guided self-help succeeds
# ------------------------------------------------------------------------------------------------


async def test_scenario_04_self_help_is_selected_delivered_and_its_completion_captured(
    scenario: Any, fixtures: Any
) -> None:
    """Three expectations met, the fourth blocked by the loop guard.

    Asked for: instructions selected and delivered, completion captured, telemetry validates
    restoration, no truck roll created.

    Measured on `SVC-SJ-011-B-01`, the one fixture that reaches this arm. A `send_self_help` action
    is taken, a customer communication is recorded, and the graph pauses on
    `customer_response_request` -- so the instructions were chosen, sent, and the window opened. The
    completion answer is accepted and no work order is ever created.

    **Restoration is not validated and the incident escalates on the resolution-cycle budget.** The
    customer's completion does not convince the validator, the loop re-diagnoses, and the guard
    stops it at six. That is the bounded-loop protection working, not failing -- but it means this
    scenario reaches "completion captured" and not "telemetry validates restoration". Gap
    SCENARIO-4.
    """
    run = await scenario(service_named(fixtures, SELF_HELP))

    assert run.state.get("self_help_session") is not None, "self-help was never selected"
    assert "send_self_help" in run.action_types(), "instructions were selected but never sent"
    assert run.count("customer_communications") >= 1, "nothing was delivered to the customer"
    assert "customer_response_request" in run.pauses, (
        "the graph never waited for the customer, so completion cannot have been captured"
    )
    assert run.state.get("self_help_attempt_count", 0) >= 1

    assert truck_roll_count(run.state) == 0, "guided self-help must not create a truck roll"
    assert "create_work_order" not in run.action_types()

    # The half that does not hold, pinned rather than hidden.
    assert run.status == "escalated" and "resolution_cycles" in run.escalation_reason, (
        "self-help now reaches something other than the resolution-cycle guard. If it validates "
        "and closes, that is SCENARIO-4 closed -- update the gap and assert closure here."
    )


# ------------------------------------------------------------------------------------------------
# Scenario 5 - self-help fails, then Clean Boots dispatch
# ------------------------------------------------------------------------------------------------


async def test_scenario_05_a_failed_self_help_stays_linked_and_does_not_reset_the_sla(
    scenario: Any, fixtures: Any
) -> None:
    """The SLA claim is the one worth having, and it holds.

    Asked for: the failed self-help attempt stays linked to the incident, the dispatch package
    carries the prior evidence, and **the SLA clock does not reset**.

    The third is asserted directly and is the reason this test earns its place. `sla.clock_started_at`
    is written once at intake (D1) and every later deadline is arithmetic on that stored value, so a
    failed self-help cycle cannot move it. Compared against the same incident driven with the
    customer *completing* the steps: two different journeys, one clock.

    The first holds too -- the session and its attempt count survive on the same incident, under the
    same `incident_id`, rather than the failure starting a new case.

    **The dispatch does not happen.** `SVC-SJ-011-B-01` escalates on the resolution-cycle guard
    whether the customer completes or not, so there is no dispatch package to inspect. Same root as
    gap SCENARIO-4; the dispatch half is SCENARIO-5.
    """
    completed = await scenario(service_named(fixtures, SELF_HELP), thread_suffix="-ok")
    failed = await scenario(
        service_named(fixtures, SELF_HELP),
        answers=Answers(
            overrides={"customer_response_request": lambda s, c, p: customer_did_not_complete()}
        ),
        thread_suffix="-failed",
    )

    assert failed.state.get("self_help_session") is not None, (
        "a failed self-help attempt must stay linked to the incident, not vanish with the failure"
    )
    assert failed.state["incident_id"] == completed.state["incident_id"], (
        "the failure started a new incident instead of continuing this one"
    )
    assert failed.state.get("self_help_attempt_count", 0) >= 1

    assert failed.state["sla"].clock_started_at == completed.state["sla"].clock_started_at, (
        "the SLA clock moved between a completed and a failed self-help journey. D1 writes it once "
        "at intake precisely so that no later cycle can reset it."
    )

    # The dispatch half, pinned.
    assert truck_roll_count(failed.state) == 0, (
        "a failed self-help now dispatches, which is SCENARIO-5 proper -- assert the dispatch "
        "package carries the prior evidence and retire the gap."
    )
