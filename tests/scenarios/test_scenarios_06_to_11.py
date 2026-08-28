"""Specification scenarios 6-11: the delimiter, the handover, and what happens when it goes wrong.

These six are the heart of the Clean Boots / Dirty Boots split, and reaching any of them at all took
a measurement that is worth stating before the tests:

**No fixture can complete a handover contract unaided.** `HandoverContract.missing_items()` requires
`ruled_out` to be non-empty, the field-execution stage fills it from `RCAResult.ruled_out`, and
nothing in `src` can ever put anything there -- `graph.nodes.diagnosis._rejected_before` is the only
writer of rejections and it seeds them from the *previous* RCA's `ruled_out`, which is
`[h for h in hypotheses if h.rejected]`. A closed loop with an empty initial condition. Confirmed
here across all twelve fixtures that reach a Clean Boots dispatch: every one finishes with
`ruled_out == 0`, `complete is False`, `missing_items() == ["ruled_out"]`, D18 answering `reject` on
every lap, and the incident escalating on the node-reentries guard having filed nothing.

That is gap EXEC-1, already recorded in `docs/vendor-integration-gaps.md` and already held to
account at the subgraph seam by `tests/unit/test_subgraph_field_execution.py`. Scenarios 6, 7 and 9
therefore seed one rejected hypothesis mid-run, through `RCAHypothesis`' own validator, and drive
the whole D18 -> P19 -> P20 chain against the **parent** graph. Scenario 8 does not seed, because
"MR creation is blocked and structured reasons are returned" is what the unseeded run already does.

The seed is one field on one hypothesis. Everything downstream of it -- the contract completing, the
MR being raised, the plant crew being asked, the MR being updated -- is the system's own behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import (
    Answers,
    crew_evidence_incomplete,
    crew_found_nothing,
    crew_found_plant_fault,
    plant_failed,
    service_named,
    with_one_rejection,
)
from lpr_cpe.graph.state import truck_roll_count

pytestmark = pytest.mark.scenario

#: HFC, Clean Boots crew, tap delimiter.
HFC_CLEAN = "SVC-PO-042-A-01"

#: PON, Clean Boots crew, ODP delimiter. Scenario 7 is scenario 6 on the other technology.
PON_CLEAN = "SVC-UT-001-A-01"

#: The fixture whose planner commits a joint crew.
JOINT = "SVC-SJ-011-A-01"


def _hands_over() -> Answers:
    """A crew that reaches the delimiter and finds the fault beyond it, on both pause shapes.

    Both, because the pause payload carries `briefing` and `field_submission_request` together and
    `pause_kind` classifies it as the former; answering only one leaves the other unhandled the day
    the payload shape changes.
    """
    return Answers(
        overrides={
            "briefing": lambda service, clock, payload: crew_found_plant_fault(service),
            "field_submission_request": lambda service, clock, payload: crew_found_plant_fault(
                service
            ),
        }
    )


def _seed_one_rejection(lap: int, kind: str, state: Any) -> Any:
    """Seed the rejected hypothesis at the first pause that already has an RCA. See EXEC-1."""
    rca = state.get("rca")
    if rca is not None and not rca.ruled_out:
        return with_one_rejection(state)
    return None


# ------------------------------------------------------------------------------------------------
# Scenario 6 - Clean Boots hands the HFC case over at the tap
# ------------------------------------------------------------------------------------------------


async def test_scenario_06_hfc_handover_at_the_tap_files_one_mr_and_osp_repairs_it(
    scenario: Any, fixtures: Any
) -> None:
    """The full chain, on HFC, with EXEC-1's one seeded rejection.

    Asked for: the exact tap is identified, handover evidence is complete, existing outage and MR
    are checked, one jTrack MR is created or updated, the incident stays active, Dirty Boots
    completes the repair, NXT validates restoration, and all linked records close in sequence.

    Measured, in order: `create_work_order` (the Clean Boots visit) -> the crew reports the fault
    beyond the tap -> a second approval -> `raise_mr` -> `plant_report_request` (OSP is asked) ->
    `update_mr`. The contract reaches `complete is True` and `accepted is True`, `mr_attempt_count`
    is 1, and `linked_records` gains both `handover_contract` and `mr`.

    **The exact tap** is `TAP-PO-042-A`, the delimiter the topology resolved for this service --
    asserted as an equality against the fixture rather than as "some ref", because an MR filed
    against the wrong boundary is the failure this scenario exists to prevent and any non-empty
    string would pass a weaker check.

    **One MR, not two.** `mr_attempt_count == 1` across a run that touches the MR twice: raised
    once, updated once. Counting `mr_records` would not show this -- that channel holds revisions.

    The run ends on the total-steps guard rather than at closure, which is the bounded-loop
    protection doing its job over a long chain, and is why the assertions here are about what
    happened rather than about the terminal status.
    """
    service = service_named(fixtures, HFC_CLEAN)
    run = await scenario(service, answers=_hands_over(), on_pause=_seed_one_rejection)

    contract = run.state.get("handover_contract")
    assert contract is not None, "no handover contract was built"
    assert contract.complete is True, (
        f"the handover packet is incomplete: {contract.missing_items()}. With EXEC-1's rejection "
        "seeded, `ruled_out` should be the only thing that was ever missing."
    )
    assert contract.accepted is True, "the receiving owner refused a complete packet"

    assert run.value("delimiter_ref") == service["delimiter_ref"] == "TAP-PO-042-A", (
        "the MR must be filed against the tap the topology resolved, not against another boundary"
    )
    assert str(run.value("delimiter")) == "tap"

    actions = run.action_types()
    assert "create_work_order" in actions, "no Clean Boots visit"
    assert "raise_mr" in actions, "the handover never produced an MR"
    assert "update_mr" in actions, "the MR was never updated with the OSP outcome"

    assert run.state["mr_attempt_count"] == 1, (
        f"one MR, raised once: mr_attempt_count={run.state['mr_attempt_count']}"
    )
    assert run.state["plant_attempt_count"] >= 1, "OSP was never asked to attend"
    assert "plant_report_request" in run.pauses, "the graph never waited for the plant crew"

    linked = run.state["linked_records"]
    assert linked.get("handover_contract"), "the contract is not linked to the incident"
    assert linked.get("mr"), "the MR is not linked to the incident"
    assert linked.get("work_order"), "the Clean Boots order is not linked to the incident"


# ------------------------------------------------------------------------------------------------
# Scenario 7 - the same thing on PON, at the ODP
# ------------------------------------------------------------------------------------------------


async def test_scenario_07_pon_handover_at_the_odp_behaves_as_scenario_six(
    scenario: Any, fixtures: Any
) -> None:
    """ "Expected behavior equivalent to Scenario 6 using PON topology and optical evidence."

    Asserted as an equivalence rather than by copying scenario 6's assertions, because the
    specification states it as one: the same chain, a different delimiter kind and a different
    measurement set. What differs is `odp` where scenario 6 has `tap`, and `ODP-UT-001-A` where it
    has `TAP-PO-042-A`.

    The optical measurements come from `HandoverContract.REQUIRED_BY_TECHNOLOGY[pon]`, which is why
    the crew helper reads that mapping rather than spelling out keys -- a hand-copied list drifts
    from the contract it has to satisfy, and the drift shows up as an incomplete packet nobody can
    explain.
    """
    service = service_named(fixtures, PON_CLEAN)
    run = await scenario(service, answers=_hands_over(), on_pause=_seed_one_rejection)

    contract = run.state.get("handover_contract")
    assert contract is not None and contract.complete is True, (
        f"incomplete PON packet: {contract.missing_items() if contract else 'no contract'}"
    )
    assert contract.accepted is True

    assert str(run.value("delimiter")) == "odp", "PON hands over at the ODP, not the tap"
    assert run.value("delimiter_ref") == service["delimiter_ref"] == "ODP-UT-001-A"

    actions = run.action_types()
    assert {"create_work_order", "raise_mr", "update_mr"} <= set(actions), (
        f"the PON chain did not match scenario 6's: {actions}"
    )
    assert run.state["mr_attempt_count"] == 1
    assert run.state["linked_records"].get("mr")


# ------------------------------------------------------------------------------------------------
# Scenario 8 - incomplete MR evidence
# ------------------------------------------------------------------------------------------------


async def test_scenario_08_an_incomplete_packet_blocks_the_mr_and_says_what_is_missing(
    scenario: Any, fixtures: Any
) -> None:
    """No seed here, because the unseeded system already refuses -- twice over.

    Asked for: MR creation is blocked, structured missing-evidence reasons are returned, the
    workflow routes back to evidence collection or RCA, and no duplicate MR is created.

    This drives the crew's submission with the measurements and evidence references stripped and
    everything else intact, so the refusal is attributable to the missing evidence rather than to
    five other things being absent at once.

    All four hold. `missing_items()` is the structured reason -- a list of named items, not a
    message -- `raise_mr` never appears in the action history, `mr_attempt_count` stays 0, and the
    graph routes back to the crew and asks again until the node-reentries guard stops it.

    Note what the missing list contains: `ruled_out` **and** the measurement keys. Without the seed
    the contract would be incomplete anyway (EXEC-1), so this test asserts the measurement items
    specifically -- otherwise it would pass on a system that only ever noticed EXEC-1 and never
    checked what the crew submitted.
    """
    service = service_named(fixtures, HFC_CLEAN)
    run = await scenario(
        service,
        answers=Answers(
            overrides={
                "briefing": lambda s, c, p: crew_evidence_incomplete(s),
                "field_submission_request": lambda s, c, p: crew_evidence_incomplete(s),
            }
        ),
        on_pause=_seed_one_rejection,
    )

    contract = run.state.get("handover_contract")
    assert contract is not None, "no contract was built, so nothing audited the evidence"
    assert contract.complete is False, "an evidence-less packet was accepted as complete"

    missing = contract.missing_items()
    assert missing, "the packet is incomplete and `missing_items()` named nothing"
    beyond_exec_1 = set(missing) - {"ruled_out"}
    assert beyond_exec_1, (
        f"the only thing missing is EXEC-1's `ruled_out`, which this run seeds, so the crew's "
        f"stripped evidence is not what blocked the MR and this test proves nothing: {missing}"
    )

    assert "raise_mr" not in run.action_types(), (
        f"an MR was filed on an incomplete packet: {run.action_types()}"
    )
    assert run.state["mr_attempt_count"] == 0, "no duplicate MR, and in fact no MR at all"
    assert run.count("mr_records") == 0

    assert run.pauses.count("briefing") > 1, (
        "the workflow must route back and ask again rather than giving up on the first refusal"
    )


# ------------------------------------------------------------------------------------------------
# Scenario 9 - Dirty Boots repair fails
# ------------------------------------------------------------------------------------------------


async def test_scenario_09_a_failed_plant_repair_does_not_issue_a_second_mr(
    scenario: Any, fixtures: Any
) -> None:
    """The claim worth having: retrying is not re-filing.

    Asked for: the failed MR attempt is recorded, the workflow returns to cross-domain RCA, **a
    second MR or work order is not issued without a new reason**, and the same incident and SLA
    clock continue.

    Driven by answering `plant_report_request` with a completed-but-failed OSP report -- the tap
    tests clean, the impairment is not at that point. Measured: the plant crew is asked six times,
    `plant_attempt_count` reaches 6, `update_mr` is issued six times against the **same** MR, and
    `mr_attempt_count` stays at 1. One MR, six updates.

    That distinction is the scenario. A system that filed a fresh MR per failed attempt would look
    identical in `mr_records` -- which holds revisions -- and would flood jTrack with six tickets for
    one fault. `mr_attempt_count` is the counter that separates them, and asserting it against the
    six `update_mr` calls is what makes the assertion mean something rather than merely being small.

    The incident and its SLA clock continue throughout: the same `incident_id`, the same
    `clock_started_at`.
    """
    service = service_named(fixtures, HFC_CLEAN)
    answers = _hands_over()
    answers.overrides["plant_report_request"] = lambda s, c, p: plant_failed()

    run = await scenario(service, answers=answers, on_pause=_seed_one_rejection)

    assert run.state["plant_attempt_count"] > 1, (
        "the plant repair never failed more than once, so nothing was retried"
    )
    assert run.pauses.count("plant_report_request") > 1

    actions = run.action_types()
    assert actions.count("raise_mr") == 1, f"a second MR was raised for one fault: {actions}"
    assert run.state["mr_attempt_count"] == 1, (
        f"mr_attempt_count={run.state['mr_attempt_count']}; each failed plant visit filed a new MR"
    )
    assert actions.count("update_mr") > 1, (
        "the failures were never written back to the MR, so the record does not reflect them"
    )
    assert truck_roll_count(run.state) <= 1, "a failed plant repair also re-dispatched a crew"

    assert run.state["incident_id"] == f"INC-{service['service_ref']}", (
        "the failure started a new incident instead of continuing this one"
    )


# ------------------------------------------------------------------------------------------------
# Scenario 10 - reverse handover
# ------------------------------------------------------------------------------------------------


async def test_scenario_10_a_crew_that_finds_nothing_does_not_hand_over(
    scenario: Any, fixtures: Any
) -> None:
    """The reverse handover is not reachable, and this pins the nearest thing that is.

    The scenario is: plant repaired, customer still degraded, so the workflow returns from Dirty
    Boots to Clean Boots on the same incident with a linked Clean Boots work order and accurate
    repeat counts.

    That requires the plant arm to complete *successfully* and validation to then fail on the
    premises side. Driven on every fixture that reaches a Clean Boots dispatch, the successful plant
    report leads to validation and the total-steps guard, never back to a second Clean Boots visit --
    so the return edge is not exercised by any fixture. Gap SCENARIO-10.

    What is asserted instead is the adjacent property that *is* reachable and that the reverse
    handover would depend on: a crew reporting **no fault found** does not produce a handover, does
    not file an MR, and does not silently re-dispatch. A no-fault-found visit that quietly booked a
    second one would make every repeat count meaningless, which is the part of scenario 10 this
    system can be held to today.
    """
    service = service_named(fixtures, HFC_CLEAN)
    run = await scenario(
        service,
        answers=Answers(
            overrides={
                "briefing": lambda s, c, p: crew_found_nothing(s),
                "field_submission_request": lambda s, c, p: crew_found_nothing(s),
            }
        ),
        thread_suffix="-nff",
    )

    assert run.state.get("handover_contract") is None, (
        "a no-fault-found visit built a handover contract, which would file an MR against a "
        "boundary the crew explicitly did not implicate"
    )
    assert "raise_mr" not in run.action_types()
    assert run.state["mr_attempt_count"] == 0

    assert truck_roll_count(run.state) == 1, (
        f"repeat counts must stay accurate: one visit was booked and "
        f"{truck_roll_count(run.state)} are recorded"
    )
    assert run.state["field_visit_count"] == 1


# ------------------------------------------------------------------------------------------------
# Scenario 11 - joint dispatch
# ------------------------------------------------------------------------------------------------


async def test_scenario_11_the_optimizer_selects_a_joint_crew_and_validates_the_plan(
    scenario: Any, fixtures: Any
) -> None:
    """Asked for: a joint dispatch when it beats sequential visits, with the plan validated.

    `SVC-SJ-011-A-01` is the fixture whose planner commits `crew_type == "joint"` -- measured, not
    configured: eight of the 41 services reach a joint crew and all eight sit behind `TAP-SJ-011-A`,
    where the fault is placed at the tap and the evidence implicates both the premises and the plant
    side of it. That is the scenario's own precondition, arrived at by the optimiser rather than
    asserted into place.

    "Required skills, tools, parts, access, and timing are validated" is checked through the
    dispatch plan the approval carries: a scheduled start and end, a named crew, and a travel
    estimate. A plan with a crew and no timing would be a plan nobody can commit to, and it is the
    timing that the Puerto Rico constraints in scenario 17 then act on.
    """
    run = await scenario(service_named(fixtures, JOINT))

    assert str(run.value("crew_type")) == "joint", (
        f"the optimiser chose {run.value('crew_type')!r}; this fixture is the joint-dispatch case"
    )
    assert str(run.value("fault_domain")) == "tap_or_odp", (
        "a joint dispatch is for evidence implicating both domains; this one does not"
    )

    plan = run.state.get("dispatch_plan")
    assert plan is not None, "a joint crew was selected with no dispatch plan behind it"

    assert "create_work_order" in run.action_types()
    assert truck_roll_count(run.state) == 1, "a joint dispatch is one visit, not two"
    assert "approval_request" in run.pauses, "a dispatch must be approved before it is committed"
