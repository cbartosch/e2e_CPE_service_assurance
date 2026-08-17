"""Stage 3's field branch, compiled and run against the one incident that carries both field options.

The fixture is `SVC-SJ-011-A-01`, and it is not an arbitrary pick. A `TAP_OR_ODP` fault is offered
two options by `decision_services.resolution` -- the jTrack MR first, because the delimiter is plant
and OSP owns the repair, and the work order second as the customer half of a joint visit -- and
**both** carry `requires_truck_roll`. That makes it the only shape in the fixture set where
`is_field_option` and `is_dispatchable_option` disagree, which is the disagreement the whole module
rests on. A `drop` fixture would exercise every node here and prove nothing about the narrowing,
because its plan holds one option and either predicate returns it.

Measured over the fixture set, 16 of the 50 arrivals are joint and every one of them has this shape.

Two exits are not reachable from any fixture and are tested on constructed state
--------------------------------------------------------------------------------
`queue_for_dispatcher` never runs in the wired graph: swept across all 41 services and both case
types, the 50 arrivals leave through `commit_field_dispatch` (40) and `abandon_field_planning` (10)
and nothing else. The simulated WFM always has a crew and the pack never blocks a work order, so
neither of D14's refusal arms nor `route_dispatch_gate`'s fourth answer is ever taken. That is a
property of the simulator, not of the code, and a real dispatcher's day is mostly the case the
simulator cannot produce -- so the node is driven directly, from a plan whose assignment has been
removed and whose requirement is unassigned.

Two tests here exist to keep a document honest
-----------------------------------------------
`test_the_constraints_nothing_feeds_are_starved_and_not_broken` pins the measurement behind gap
FIELD-1 in `docs/vendor-integration-gaps.md`: three of the optimizer's twelve constraints read
requirement fields that nothing in `src` writes. It fails the day one of them acquires a writer,
which is the day the gap entry should be deleted rather than left standing to be read as current --
a document describing a closed gap is worse than no document. It also shows the three constraints
refusing when they *are* fed, because a prose claim cannot distinguish a starved check from a broken
one and only the first is a vendor gap.

What is deliberately not asserted here
--------------------------------------
D11 and D12, the two decisions that lead *into* this subgraph, are the parent's edges and are pinned
in `test_builder.py`. Asserting them here would assert them in the one place they are not wired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.dispatch.constraints import ConstraintCode, blocking_code
from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    CaseType,
    CrewType,
    EventSource,
    FaultDomain,
    IncidentStatus,
    PolicyOutcome,
    ReasonCode,
    Severity,
    Technology,
    WorkOrderStatus,
)
from lpr_cpe.domain.governance import PolicyDecision
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED
from lpr_cpe.graph.routing import is_field_option
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.graph.subgraphs.field_planning import (
    DISPATCH_TARGETS,
    FIELD_PLANNING_NODES,
    abandon_field_planning,
    build_field_planning_graph,
    build_field_requirement,
    commit_field_dispatch,
    dispatch_round,
    is_dispatchable_option,
    optimize_field_schedule,
    queue_for_dispatcher,
    route_dispatch_gate,
    route_field_gate,
    selected_field_option,
)
from lpr_cpe.observability.kpi import MetricTimestamp

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The joint case: a tap fault whose plan carries an MR *and* a work order. See the module docstring.
JOINT_SERVICE = "SVC-SJ-011-A-01"

#: The one incident measured arriving here through D12 rather than D11, having passed `self_help` --
#: which is what makes it the honest source of a foreign `selected_option_id`.
SELF_HELP_SERVICE = "SVC-SJ-011-B-01"

APPROVAL = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the tap is the confirmed delimiter; send the joint crew",
}

REJECTION = {
    "status": "rejected",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "that crew is on the far side of the district at 15:00; find another slot",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock the sibling subgraph tests use, and for the same reason: inside a
    compiled graph the test cannot advance the clock between nodes, so a frozen one would stamp the
    approval request and the work order with the same instant.

    Subclassed off `FrozenClock` so `local_now()` and `timezone` come from the production clock --
    `optimize_field_schedule` asks the pack for a shift window and a hand-rolled clock that stopped
    at `now()` would not answer.
    """

    def now(self) -> datetime:
        return self.advance(timedelta(seconds=3))


def _initial(service: dict[str, Any]) -> Any:
    return make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=AssuranceEvent(
            event_id=f"EVT-{service['service_ref']}",
            source=EventSource.NXT,
            case_type=CaseType.PROACTIVE_ALARM,
            technology=Technology(service["technology"]),
            severity=Severity.HIGH,
            occurred_at=NOW - timedelta(minutes=6),
            received_at=NOW - timedelta(minutes=5),
            customer_ref=service["customer_ref"],
            service_ref=service["service_ref"],
            cpe_ref=service["cpe_ref"],
            summary=f"loss of signal on {service['service_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


async def _drive_to_the_gate(fixtures: Any, ref: str, tag: str) -> Any:
    """Parent to P11, then the subgraph standalone, stopping wherever it stops.

    The parent is run for real rather than hand-built, for the reason the remote branch's fixture
    gives: a constructed `resolution_plan` would let this module pass while
    `generate_resolution_options` offered something else entirely, and the coupling between the two
    is exactly what `test_the_field_branch_selects_the_work_order_and_not_the_mr` exists to police.

    `interrupt_after=["generate_resolution_options"]` because since the fork was wired the parent no
    longer stops at P11 by itself -- it answers D11 `field_planning` and runs this very subgraph.
    """
    service = fixtures.services[ref]
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    parent_final = await parent.ainvoke(
        _initial(service), context=ctx, config={"configurable": {"thread_id": f"parent-{tag}"}}
    )

    graph = build_field_planning_graph().compile(
        name="lpr_cpe_field_planning", checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": f"field-{tag}"}}
    first = await graph.ainvoke(parent_final, context=ctx, config=config)
    return graph, ctx, config, first, parent_final


@pytest.fixture
async def paused(fixtures: Any) -> Any:
    """The joint incident, stopped at the dispatch approval gate with a slot already solved."""
    return await _drive_to_the_gate(fixtures, JOINT_SERVICE, "joint")


@pytest.fixture
async def no_dispatch(fixtures: Any) -> Any:
    """The self-help incident, which reaches P14 and finds nothing P14 can dispatch."""
    return await _drive_to_the_gate(fixtures, SELF_HELP_SERVICE, "selfhelp")


# ------------------------------------------------------------------------------------------------
# The shape LangGraph received
# ------------------------------------------------------------------------------------------------


def test_the_gate_router_is_wired_on_both_edges_that_ask_the_question(paused: Any) -> None:
    """One router, two edges: out of the evaluation and out of the gate.

    After evaluating and after an answer the question is identical -- *may this dispatch be
    committed now?* -- and the answer moves from `approve_dispatch` to `commit` or `replan` purely
    because the approval trail changed underneath it. Two routers would be two spellings of one
    question and the second would be the one that forgot about rejection.

    Both halves are read back out of the `StateGraph`, because reading `DISPATCH_TARGETS` would only
    prove the table equals itself. The second half is the one that has teeth, and it was added after
    the first was measured toothless: `guarded()` returns a fresh closure per call with no
    `__wrapped__` on it, so the router a branch carries cannot be compared by identity, and the
    `ends` mapping is the *targets* only. Rewiring this edge to `route_dispatch_approval` -- the
    exact mistake the wrapper exists to prevent -- left the whole file green.

    So the two edges are driven, on the one state where the two routers disagree: a decision the
    pack blocked, which D15's fall-through answers `approve_dispatch` and this graph must answer
    `queue_for_dispatcher`. With the second half in place, that rewiring reads:

        AssertionError: both edges must read what the decision said; an edge wired to
        `route_dispatch_approval` answers approve_dispatch here and puts a blocked action to a
        human. Got ['queue_for_dispatcher', 'approve_dispatch']
    """
    graph = build_field_planning_graph()
    expected = {**DISPATCH_TARGETS, ESCALATED: END}
    routers = []

    for source in ("evaluate_dispatch_policy", "request_dispatch_approval"):
        branches = graph.branches[source]
        assert len(branches) == 1, f"{source} should carry exactly one conditional edge"
        branch = next(iter(branches.values()))
        assert dict(branch.ends or {}) == expected, (
            f"{source} must route on the same four answers as the other gate edge"
        )
        routers.append(branch.path.func)

    _graph, _ctx, _config, first = paused[:4]
    option = selected_field_option(first)
    assert option is not None
    blocked = dict(first)
    blocked["policy_decisions"] = [
        PolicyDecision(
            decision_id="POL-BLOCKED-BY-BLAST-RADIUS",
            decided_at=NOW,
            action_type=option.action_type,
            outcome=PolicyOutcome.BLOCKED,
            reason_codes=(ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,),
            policy_version="test",
        )
    ]

    answers = [route(blocked) for route in routers]
    assert answers == ["queue_for_dispatcher", "queue_for_dispatcher"], (
        "both edges must read what the decision said; an edge wired to `route_dispatch_approval` "
        f"answers approve_dispatch here and puts a blocked action to a human. Got {answers}"
    )
    assert [route(dict(first)) for route in routers] == ["approve_dispatch", "approve_dispatch"]


def test_every_node_is_guarded_or_terminal() -> None:
    """No edge in this graph may bypass the escalation flag.

    `guards.ESCALATED` exists because an incident that exhausted its budget at P04 otherwise walked
    five further super-steps. Here the step it would walk is a work order sent to the WFM after the
    budget had been declared exhausted, which is a truck rather than a wasted super-step.

    The `replan` loop makes this load-bearing rather than decorative: D15's third answer returns to
    P15, so an unguarded edge out of the gate would be a cycle with no ceiling on it at all.

    Shown red by wiring `commit_field_dispatch` onward with a plain `add_edge`:

        AssertionError: plain edges may only lead to END; found {'queue_for_dispatcher'}.
        A plain edge between two working nodes is an unguarded step.
    """
    graph = build_field_planning_graph()
    for source, branches in graph.branches.items():
        for branch in branches.values():
            assert ESCALATED in (branch.ends or {}), (
                f"the conditional edge out of {source} has no {ESCALATED} branch, so a guarded "
                "incident would continue through it"
            )

    plain = {end for start, end in graph.edges if start != START}
    assert plain == {END}, (
        f"plain edges may only lead to END; found {plain - {END}}. A plain edge between two working "
        "nodes is an unguarded step."
    )


def test_the_registry_matches_what_the_graph_contains() -> None:
    graph = build_field_planning_graph()
    assert set(graph.nodes) == {name for name, _ in FIELD_PLANNING_NODES}


# ------------------------------------------------------------------------------------------------
# The narrowing the module rests on
# ------------------------------------------------------------------------------------------------


def test_the_field_branch_selects_the_work_order_and_not_the_mr(paused: Any) -> None:
    """`is_field_option` returns the MR here; `is_dispatchable_option` must not.

    This is the module's central claim, asserted on the one fixture where the two predicates
    disagree. Both options carry `requires_truck_roll` -- honestly, because an MR causes plant work
    -- so the logistics predicate returns whichever comes first, and the planner puts the MR first.

    What follows a selected MR is not a wrong-but-harmless choice. `wfm.create_work_order` refuses
    any other action type by name, and `route_dispatch_approval` reads
    `latest_policy_decision(state, CREATE_WORK_ORDER)` literally, so the MR would sail past the
    approval gate unasked and then die at the adapter.

    Shown red by reducing `is_dispatchable_option` to `is_field_option(option)`:

        At index 0 diff: (<ActionType.RAISE_MR: 'raise_mr'>, True, True)
                      != (<ActionType.RAISE_MR: 'raise_mr'>, True, False)
    """
    _graph, _ctx, _config, first, parent_final = paused

    plan = parent_final["resolution_plan"]
    offered = [(o.action_type, is_field_option(o), is_dispatchable_option(o)) for o in plan.options]
    assert offered == [
        (ActionType.RAISE_MR, True, False),
        (ActionType.CREATE_WORK_ORDER, True, True),
    ], (
        "the joint fixture must still offer both, MR first, or this test has stopped exercising "
        "the disagreement it was written for"
    )

    selected = selected_field_option(first)
    assert selected is not None and selected.action_type is ActionType.CREATE_WORK_ORDER
    assert first["crew_type"] is CrewType.JOINT


def test_a_selection_left_by_another_branch_is_not_treated_as_this_one_s(no_dispatch: Any) -> None:
    """`selected_option_id` is one field shared by three branches, and this is the branch that can
    be entered after another has written it.

    D12's `field_planning` arm is reached from `self_help`, which selects an option of its own on
    the way past. `SVC-SJ-011-B-01` is the one incident measured arriving that way, and its plan
    holds three options, all of them non-dispatchable -- so a `send_self_help` selection sitting in
    `selected_option_id` is a state this branch genuinely receives, not a fabricated one. The
    selection below is set through the model's own validator, which requires the id to be among the
    plan's options: it is a legal plan, just not one this subgraph may act on.

    Without the class check that stale selection comes back here as though this subgraph had chosen
    it. Today the gate still escalates, because `route_field_gate` also requires a requirement and
    there is none -- but that is the requirement clause covering for this one, and it stops covering
    the moment a second round leaves a requirement behind.

    Shown red by dropping the check and returning `plan.selected` unfiltered:

        assert ResolutionOption(option_id='RPLAN-8855c2510a6768bfe847-send_self_help',
               action_type=<ActionType.SEND_SELF_HELP: 'send_self_help'>, ...) is None
    """
    _graph, _ctx, _config, _first, parent_final = no_dispatch

    plan = parent_final["resolution_plan"]
    self_help = next(o for o in plan.options if o.action_type is ActionType.SEND_SELF_HELP)
    assert not is_dispatchable_option(self_help)

    stale = dict(parent_final)
    stale["resolution_plan"] = plan.model_copy(update={"selected_option_id": self_help.option_id})
    assert stale["resolution_plan"].selected is self_help, "the stale selection must really be set"

    assert selected_field_option(stale) is None, (
        "an option another branch selected is not this branch's; returning it here is how a "
        "send_self_help ActionRequest reaches wfm.create_work_order"
    )
    assert route_field_gate(stale) == "escalate"
    assert route_dispatch_gate(stale) == "queue_for_dispatcher"


# ------------------------------------------------------------------------------------------------
# The pause
# ------------------------------------------------------------------------------------------------


async def test_the_gate_pauses_with_the_slot_already_in_the_question(paused: Any) -> None:
    """`prepare_approval` must have landed in the checkpoint *before* the interrupt, carrying the
    crew and the time.

    An operator approving "a dispatch" is approving a truck, a crew type and a slot. An approval
    screen that named only the incident would be one where nobody could tell a 09:00 Clean Boots
    visit from a 15:00 joint one -- so the assignment is asserted to be *in the question*, not
    merely in state next to it.

    Shown red by reducing the question to the incident and the round:

        AssertionError: assert 'CREW-JOINT-SJ-01' in 'Approve a dispatch for this incident?
        Proposal 1.'
    """
    graph, _ctx, config, first = paused[:4]

    state = await graph.aget_state(config)
    assert state.next == ("request_dispatch_approval",)
    assert len(state.interrupts) == 1

    assert first["status"] is IncidentStatus.AWAITING_APPROVAL
    request = first["pending_approval"]
    assert request is not None, "the question must be in state, not only in the interrupt payload"
    assert request.kind is ApprovalKind.DISPATCH, (
        "`route_dispatch_approval` asks `approval_outstanding(state, DISPATCH)` literally, so a "
        "question asked under any other kind would be answered and then not seen"
    )
    assert request.action_type is ActionType.CREATE_WORK_ORDER

    assignment = first["dispatch_plan"].assignments[0]
    assert assignment.crew_id in request.question
    assert assignment.scheduled_start.isoformat(timespec="minutes") in request.question

    payload = state.interrupts[0].value
    assert payload["approval_request"]["approval_id"] == request.approval_id
    assert "noc_supervisor" in payload["permitted_roles"]


async def test_nothing_is_booked_before_the_answer(paused: Any) -> None:
    """The pause is the only moment at which "planned but not dispatched" is observable.

    What must be empty is everything that represents a commitment to the outside world. The status
    is not among them: by the pause, P15 and the gate have both written it, so it says nothing about
    what P14 did. That claim is asserted separately, on the node's own update -- see
    `test_recording_a_requirement_does_not_claim_the_dispatch_stage`, which exists because adding
    `"status": DISPATCH_PLANNING` to P14 left this test green.
    """
    _graph, _ctx, _config, first = paused[:4]

    assert first["work_orders"] == [], "nothing may be booked before the answer"
    assert first["action_history"] == []
    assert first.get("field_visit_count", 0) == 0
    assert first.get("selected_action") is None
    assert first["dispatch_plan"].approved is False
    assert MetricTimestamp.FIRST_ACTION_AT.value not in first["metrics_timestamps"], (
        "the approval request is not an action; stamping it here would date the incident's first "
        "action to a question nobody had answered"
    )

    assert [d.outcome for d in first["policy_decisions"]] == [PolicyOutcome.REQUIRES_APPROVAL]
    outcomes = [(e.node, e.outcome) for e in first["audit_events"] if e.node in _FIELD_NODES]
    assert outcomes == [
        ("build_field_requirement", "requirement_recorded"),
        ("optimize_field_schedule", "scheduled"),
        ("evaluate_dispatch_policy", "requires_approval"),
        ("prepare_dispatch_approval", "awaiting_approval"),
    ]


_FIELD_NODES = {name for name, _ in FIELD_PLANNING_NODES}


async def test_recording_a_requirement_does_not_claim_the_dispatch_stage(paused: Any) -> None:
    """P14 writes no status at all, and that is only visible on P14's own update.

    An incident whose requirement no crew can take never enters dispatch, so recording
    `dispatch_planning` here would claim a stage it did not reach -- and the incident that proves it
    is the one that stops at D13, where nothing downstream ever overwrites the claim. In the happy
    path P15 writes the status one super-step later, which is why this cannot be asserted at the
    pause: adding the key to P14 leaves every pause-time assertion in this file green.

    So the body is called directly. `route_field_gate` is asked alongside it, because "P14 recorded
    a requirement" and "the gate lets it through" are the two halves of the same claim.

    Shown red by adding `"status": IncidentStatus.DISPATCH_PLANNING` to P14's update:

        AssertionError: P14 records and does not schedule; it wrote
        status=<IncidentStatus.DISPATCH_PLANNING: 'dispatch_planning'>, which an incident that
        then escalates at D13 would carry out of the branch
    """
    _graph, ctx, _config, _first, parent_final = paused

    update = await build_field_requirement.__wrapped__(parent_final, ctx)  # type: ignore[attr-defined]
    assert "status" not in update, (
        f"P14 records and does not schedule; it wrote status={update.get('status')!r}, which an "
        "incident that then escalates at D13 would carry out of the branch"
    )
    assert update["crew_type"] is CrewType.JOINT
    assert len(update["dispatch_requirements"]) == 1

    assert route_field_gate({**parent_final, **update}) == "joint"


def test_the_requirement_records_customer_presence_as_a_note_and_not_as_a_flag(paused: Any) -> None:
    """The joint work order needs the customer in, and `DispatchRequirement` refuses
    `customer_access_required` with no window.

    Three options, one of which is neither a lie nor a refusal to plan. Setting the flag with a
    fabricated window would schedule against an appointment nobody made -- the exact failure the
    validator exists to prevent, committed by the caller instead of the model. Setting it with no
    window is unconstructible. Dropping the fact would hand the dispatcher a joint visit with no
    hint that the customer has to be in.

    Measured: nothing in the fixture set holds a customer availability window; the 41 service
    records carry no contact or appointment field at all. When one does, the flag can be set and
    this test should change with it.

    Shown red by setting the flag anyway -- and note it is the model, not the assertion, that
    refuses, which is the point: the fabricated-window variant is the only one the validator
    cannot catch, and that is why the note was chosen over it.

        pydantic_core._pydantic_core.ValidationError: 1 validation error for DispatchRequirement
          Value error, customer_access_required=True with no customer_availability_windows: this
          dispatch would be scheduled blind and fail access
    """
    _graph, _ctx, _config, first = paused[:4]
    requirement = first["dispatch_requirements"][-1]
    option = selected_field_option(first)
    assert option is not None and option.requires_customer_present, (
        "the joint work order is the one that needs the customer present; without that this test "
        "asserts nothing"
    )

    assert requirement.customer_access_required is False
    assert any("the customer must be present" in note for note in requirement.notes)
    assert requirement.crew_type is CrewType.JOINT
    assert requirement.estimated_duration == timedelta(minutes=150), (
        "the visit length comes from the pack by crew type, not from the option's "
        "estimated_duration, which is the lead time to a restored service"
    )


# ------------------------------------------------------------------------------------------------
# The commit
# ------------------------------------------------------------------------------------------------


async def test_an_approved_dispatch_books_a_requested_order_and_counts_no_truck_roll(
    paused: Any,
) -> None:
    """P16's whole argument in one assertion: a `REQUESTED` work order is a booking, not a visit.

    `wfm.create_work_order` returns `status=requested`; nobody has been dispatched and nobody is on
    site. So the status stays `DISPATCH_PLANNING` rather than moving to `field_in_progress` --
    `domain.lifecycle` does not permit `awaiting_approval -> field_in_progress` at all, so the
    honest write is also the only legal one -- and `DISPATCHED_AT` is not stamped.

    `TRUCK_ROLLS_PER_INCIDENT` is not emitted either, and that is the same fact a third time: it
    counts `counted_as_truck_roll`, which is `False` for everything this node writes, so it would
    report 0.0 for every dispatched incident and drag the average to zero with the very incidents
    that caused the trucks.
    """
    graph, ctx, config, first = paused[:4]
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    assert final["status"] is IncidentStatus.DISPATCH_PLANNING
    assert final["pending_approval"] is None

    (order,) = final["work_orders"]
    assert order.status is WorkOrderStatus.REQUESTED
    assert order.counted_as_truck_roll is False
    assert order.visit_number == 1 and final["field_visit_count"] == 1
    assert order.assigned_crew_id == first["dispatch_plan"].assignments[0].crew_id
    assert order.requirement_id == first["dispatch_requirements"][-1].requirement_id

    assert MetricTimestamp.DISPATCHED_AT.value not in final["metrics_timestamps"], (
        "nobody has been dispatched; Stage 4 advances the order and owns this timestamp"
    )
    emitted = {event.kpi_name for event in final["kpi_events"]}
    assert MetricTimestamp.DISPATCHED_AT.value not in emitted


async def test_the_commit_records_the_approval_it_ran_under(paused: Any) -> None:
    """The action, the plan and the work order must all name the same answer.

    `approval_ref` is a derived property (`approval_id:decided_by`) rather than a stored field, so
    the reference on the action cannot disagree with the approval it names. What this pins is that
    the reference is carried at all -- `ActionRequest` refuses a `REQUIRES_APPROVAL` outcome with no
    `approval_ref`, so an unrecorded answer is a dead incident rather than an unauthorised write.
    """
    graph, ctx, config, first = paused[:4]
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    answer = final["approvals"][-1]
    assert answer.status.value == "approved" and answer.kind is ApprovalKind.DISPATCH
    assert answer.approval_id == first["pending_approval"].approval_id

    request = final["selected_action"]
    assert request.policy_outcome is PolicyOutcome.REQUIRES_APPROVAL
    assert request.approval_ref == answer.approval_ref
    assert final["dispatch_plan"].approved is True
    assert final["dispatch_plan"].approval_ref == answer.approval_ref

    (record,) = final["action_history"]
    assert record.outcome is ActionOutcome.SIMULATED
    assert record.approval_ref == answer.approval_ref
    assert record.reason_code is ReasonCode.POLICY_APPROVAL_REQUIRED


async def test_the_committed_option_is_marked_attempted_so_a_later_cycle_skips_it(
    paused: Any,
) -> None:
    """`first_actionable_option` skips an option already in `attempted_option_ids`.

    An incident that comes back through P11 after this dispatch fails must not be handed the same
    option again. The append is guarded on membership because `attempted_option_ids` is a plain list
    on a model we `model_copy`, and an unconditional append would double the entry if this node were
    ever re-entered on the same option.

    Shown red by skipping the append:

        AssertionError: assert [] == ['RPLAN-228a28c9ba86d6de6d43-create_work_order']
          Right contains one more item: 'RPLAN-228a28c9ba86d6de6d43-create_work_order'
    """
    graph, ctx, config, first = paused[:4]
    assert first["resolution_plan"].attempted_option_ids == []

    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)
    option = selected_field_option(final)
    assert option is not None
    assert final["resolution_plan"].attempted_option_ids == [option.option_id]
    assert option not in final["resolution_plan"].untried()


async def test_the_first_action_timestamp_is_not_moved_by_a_later_dispatch(paused: Any) -> None:
    """`metrics_timestamps` reduces with `merge_dict`, which is last-writer-wins per key.

    An unconditional stamp here would move "first" forward to whichever action ran last, which is
    the one quantity the name promises it is not -- and the incidents it would corrupt are exactly
    the ones that tried a remote repair before the truck, where time-to-first-action is the number
    the remote branch is judged on.

    Driven by re-entering the node body on a state that already holds the stamp, because the branch
    only has a second action in it if the incident was here twice.

    Shown red by making the stamp unconditional (`if True:`):

        AssertionError: first_action_at was already 2026-03-02T14:36:30+00:00; a second commit
        must not write the key at all, because merge_dict would let the later value win
    """
    graph, ctx, config, _first = paused[:4]
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    stamped = final["metrics_timestamps"][MetricTimestamp.FIRST_ACTION_AT.value]
    again = await commit_field_dispatch.__wrapped__(final, ctx)  # type: ignore[attr-defined]

    assert "metrics_timestamps" not in again, (
        f"first_action_at was already {stamped}; a second commit must not write the key at all, "
        "because merge_dict would let the later value win"
    )


# ------------------------------------------------------------------------------------------------
# The replan loop, and the two ids that make it terminate
# ------------------------------------------------------------------------------------------------


async def test_a_refused_slot_is_re_optimised_and_asked_again_as_a_new_question(
    paused: Any,
) -> None:
    """D15's `replan` returns to P15, and the second proposal must be a second question.

    The dispatcher refusing a slot is refusing *that* slot, so re-optimising is the response rather
    than escalating. That loop terminates only because two derived ids advance with it, and both
    had to be keyed deliberately:

    * `approvals` de-duplicates on `approval_id`, first-write-wins, so a re-proposal keyed on
      `attempt_number` would derive the first question's id and the second refusal would be silently
      dropped. The counter is the dispatch round, because a rejected dispatch reached no adapter and
      the action attempt does not move.
    * `policy_decisions` de-duplicates on `decision_id`, so a POL id keyed on the incident and the
      option alone would collapse the second evaluation into the first --
      `approval_outstanding` compares `max(answers) < max(demands)`, and the demand's timestamp
      would never advance past the rejection, leaving the gate shut on a question already answered.
    """
    graph, ctx, config, first = paused[:4]
    second = await graph.ainvoke(Command(resume=REJECTION), context=ctx, config=config)

    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("request_dispatch_approval",), "a refusal re-plans, it does not exit"
    assert second["status"] is IncidentStatus.AWAITING_APPROVAL
    assert second["work_orders"] == [], "a refusal must not reach the WFM"
    assert second["node_visits"]["optimize_field_schedule"] == 2

    assert second["dispatch_plan"].plan_id != first["dispatch_plan"].plan_id
    assert second["pending_approval"].approval_id != first["pending_approval"].approval_id
    assert len(second["policy_decisions"]) == 2, (
        "the second evaluation must be a second decision; collapsing it is what leaves replan "
        "spinning until the re-entry budget fires"
    )

    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)
    assert [answer.status.value for answer in final["approvals"]] == ["rejected", "approved"]
    assert final["status"] is IncidentStatus.DISPATCH_PLANNING
    assert len(final["work_orders"]) == 1


async def test_the_round_the_operator_is_shown_is_the_round_of_the_plan(paused: Any) -> None:
    """`dispatch_round` counts *completed* passes, and the difference was a measured defect.

    `@node` writes the visit after the body returns, so a node downstream of P15 reads the round
    that built the plan it is holding, while P15 itself -- mid-pass, not yet counted -- adds one. An
    earlier draft folded the `+ 1` into `dispatch_round`, which made every downstream reader off by
    one against its own plan. What an operator was shown on the very first proposal was, verbatim:

        Approve a joint dispatch to TAP-SJ-011-A at 2026-03-02T14:59+00:00, crew
        CREW-JOINT-SJ-01? This is dispatch proposal 2 for the incident.

    and `queue_for_dispatcher` carried a compensating `- 1` whose only job was to undo it. Two of
    the three callers correcting the same function is the function being wrong.

    The ids still advanced under the old numbering, so nothing failed -- which is why this is
    asserted on the operator-facing text rather than on the derived ids the previous test covers.

    Shown red by restoring the `+ 1` inside `dispatch_round`:

        AssertionError: assert 2 == 1
         +  where 2 = dispatch_round({'__interrupt__': [Interrupt(value={'approval_request': ...
    """
    graph, ctx, config, first = paused[:4]
    assert dispatch_round(first) == 1
    assert "dispatch proposal 1 for the incident" in first["pending_approval"].question

    second = await graph.ainvoke(Command(resume=REJECTION), context=ctx, config=config)
    assert dispatch_round(second) == 2
    assert "dispatch proposal 2 for the incident" in second["pending_approval"].question


# ------------------------------------------------------------------------------------------------
# The two exits that do not dispatch
# ------------------------------------------------------------------------------------------------


async def test_an_infeasible_plan_queues_for_a_human_with_the_constraint_named(
    paused: Any,
) -> None:
    """D14's requirement in one node, driven on constructed state because no fixture reaches it.

    The simulated WFM always has a crew and the pack never blocks a work order, so all 50 measured
    arrivals leave through the other two exits. A real dispatcher's day is mostly this case, and it
    is the one the optimizer's twelve constraints exist for -- so the plan below is the fixture's
    own, with the assignment removed and the requirement moved to `unassigned` carrying the
    rendered violation the solver would have written.

    What is asserted is that the machine-readable code survives the round trip through that string.
    `blocking_code` recovers it, `_REASON_BY_CONSTRAINT` maps the four the reason vocabulary has a
    word for, and the rest queue with no reason code -- which reads as "not applicable" and is true.
    Inventing a code per constraint would be inventing entries in a vocabulary that closure and
    reconciliation also read.

    The status stays `dispatch_planning`, and that is the whole difference from
    `abandon_field_planning`: the incident is still going to get a visit, once a human unblocks it.
    """
    _graph, ctx, _config, first = paused[:4]
    requirement_id = first["dispatch_requirements"][-1].requirement_id

    infeasible = dict(first)
    infeasible["dispatch_plan"] = first["dispatch_plan"].model_copy(
        update={
            "assignments": [],
            "unassigned": [requirement_id],
            "constraint_explanation": {
                requirement_id: "parts: no crew carries SPLICE-KIT-9 (score 0.00)"
            },
        }
    )
    infeasible["policy_decisions"] = []

    assert route_dispatch_gate(infeasible) == "queue_for_dispatcher", (
        "an unevaluated dispatch must not be put to a human as though the pack had permitted it"
    )

    update = await queue_for_dispatcher.__wrapped__(infeasible, ctx)  # type: ignore[attr-defined]
    assert update["status"] is IncidentStatus.DISPATCH_PLANNING
    assert update["pending_approval"] is None

    (event,) = update["audit_events"]
    assert event.outcome == "no_feasible_slot"
    assert event.reason_code is ReasonCode.PARTS_UNAVAILABLE
    assert event.detail["blocking_code"] == "parts"
    assert event.detail["unassigned"] == [requirement_id]
    assert event.detail["round"] == 1, "the round that produced the plan, not the next one"


async def test_a_dispatch_the_pack_blocked_queues_rather_than_asking_for_approval(
    paused: Any,
) -> None:
    """`route_dispatch_approval`'s fall-through would put a blocked action to a human.

    It presupposes an evaluated action: finding no decision, or one that is merely not-blocked, it
    answers `approve_dispatch`. So an evaluation the pack **blocked** would reach an operator as
    though the pack permitted it, and an operator who approved it would be authorising an action the
    engine had already refused. That fourth answer is the whole of the difference between this
    wrapper and D15.

    Defence in depth rather than the only defence: `ActionRequest` refuses construction with
    `policy_outcome=BLOCKED`, so dropping this check does not put a blocked write on the wire -- it
    converts a clean queue into a dead incident, uncaught by `@node`, with nothing an operator can
    read. The outer layer is only a defence if it is tested too.

    Shown red by deleting the `decision is None or decision.blocked` clause from the wrapper:

        AssertionError: assert 'approve_dispatch' == 'queue_for_dispatcher'
    """
    _graph, _ctx, _config, first = paused[:4]
    option = selected_field_option(first)
    assert option is not None

    blocked = dict(first)
    blocked["policy_decisions"] = [
        PolicyDecision(
            decision_id="POL-BLOCKED-BY-BLAST-RADIUS",
            decided_at=NOW,
            action_type=option.action_type,
            outcome=PolicyOutcome.BLOCKED,
            reason_codes=(ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,),
            policy_version="test",
        )
    ]
    assert route_dispatch_gate(blocked) == "queue_for_dispatcher"


async def test_nothing_to_dispatch_leaves_the_branch_agreeing_with_the_node_that_owns_the_fact(
    no_dispatch: Any,
) -> None:
    """P14 and P16's sibling must not give two names to one cause.

    `SVC-SJ-011-B-01` localises to `customer_environment`, whose crew is Clean Boots -- so a crew
    exists and the branch still cannot proceed, because none of its three options is a work order.
    An earlier draft of `abandon_field_planning` asked `crew_for(domain) is None` first, and over
    the fixture set that made this node contradict the node that owns the fact:

        build_field_requirement:no_dispatchable_option    10
        abandon_field_planning:no_crew_for_domain          9
        abandon_field_planning:options_exhausted           1

    Ten incidents, one cause, two nodes reporting it differently. The missing crew was a
    *consequence* of an unknown fault domain, not the reason the branch gave up: with a crew there
    would still have been nothing to send them to do.

    Both halves of that are asserted, and the second is the one that matters. `SVC-SJ-011-B-01`
    localises to `customer_environment`, whose crew is Clean Boots -- so as it arrives, a crew-first
    ordering happens to give the same answer and proves nothing. Nine of the ten measured incidents
    localised to `unknown`, where `crew_for` returns `None`, and that is the state the ordering is
    actually wrong in. It is reconstructed below rather than reached, because every fixture that
    produces it in the wired parent has also exhausted its plan by then and would be caught by the
    first branch instead -- which is the branch under test hiding the branch under test.

    The status returns to `diagnosing`, not to a dispatcher's queue: a visit nobody can schedule is
    a dispatcher's problem, but no visit to schedule is a diagnosis problem, and queuing it would
    give a human a job with nothing in it.

    Shown red by putting `if crew is None:` back at the head of the branch:

        AssertionError: asking `crew is None` first answers no_crew_for_domain here, which
        contradicts the node that owns the fact and points the repair at the crew roster
        instead of the plan
        assert 'no_crew_for_domain' == 'no_dispatchable_option'

    The first of the two cases below stays green under that mutation, which is how the reconstructed
    second one came to be written at all.
    """
    _graph, ctx, _config, first, _parent = no_dispatch

    assert selected_field_option(first) is None
    assert first.get("dispatch_requirements", []) == []
    assert route_field_gate(first) == "escalate"

    update = await abandon_field_planning.__wrapped__(first, ctx)  # type: ignore[attr-defined]
    (event,) = update["audit_events"]
    assert update["status"] is IncidentStatus.DIAGNOSING
    assert update["pending_approval"] is None
    assert event.outcome == "no_dispatchable_option", (
        "the same words `build_field_requirement` used, on the incident that owns the fact"
    )
    assert event.detail["crew"] == CrewType.CLEAN.value
    assert event.detail["offered"] == [
        "wifi_channel_change",
        "wifi_power_change",
        "send_self_help",
    ]

    recorded = [e.outcome for e in first["audit_events"] if e.node == "build_field_requirement"]
    assert recorded == ["no_dispatchable_option"]

    # The nine: an unknown domain, so no crew -- and still not the reason.
    undiagnosed = dict(first)
    undiagnosed["fault_domain"] = FaultDomain.UNKNOWN
    assert crew_for(FaultDomain.UNKNOWN) is None, "the premise of the case, from the owning module"

    second = await abandon_field_planning.__wrapped__(undiagnosed, ctx)  # type: ignore[attr-defined]
    (event,) = second["audit_events"]
    assert event.detail["crew"] is None
    assert event.outcome == "no_dispatchable_option", (
        "asking `crew is None` first answers no_crew_for_domain here, which contradicts the node "
        "that owns the fact and points the repair at the crew roster instead of the plan"
    )
    assert event.reason_code is ReasonCode.REMOTE_FIX_EXHAUSTED


async def test_the_constraints_nothing_feeds_are_starved_and_not_broken(paused: Any) -> None:
    """Gap FIELD-1, held to its measurement so it cannot outlive the thing it describes.

    Three of the optimizer's twelve constraints read fields that **no code in `src` writes**:
    `DispatchRequirement.skills_required`, `parts_required` and `equipment_required`. `SKILL`,
    `PARTS` and `EQUIPMENT` therefore cannot refuse anything this stage produces, and `EQUIPMENT`
    is dead from both ends -- the WFM rows carry no `carried_equipment` key either, so a requirement
    that *did* name equipment would be refused by every crew rather than matched against one.

    Recording that in a document is not enough, because a document cannot tell an inert constraint
    from a broken one. So the second half feeds each of the three and shows the same P15, on the same
    crew rows, refusing three different ways. That is the difference between "nothing asks" and
    "asking does not work", and only one of them is a vendor gap.

    The first half is the part that goes stale: the day something writes a skill or a part, these
    assertions fail, and FIELD-1 should be deleted rather than left standing. That is deliberate --
    an entry describing a gap that has been closed is worse than no entry, because it is read as
    current.

    The last assertion is the reason the gap is worth writing down at all. The stored explanation
    for the *scheduled* requirement claims all twelve constraints satisfied, and `satisfied_codes`
    exists because "an empty violation list is equally consistent with twelve passing checks and
    with twelve checks that never ran". On this path it reports the first while the second is nearer
    the truth, so a reviewer reading that line is told twelve cleared when five were asked anything.

    Shown red by giving P14 a skill it cannot know (`skills_required=["tap_replacement"]`, which
    the joint crew happens to hold, so the solve still succeeds and only the pin notices):

        E       AssertionError: nothing in `src` writes skills_required -- if that changed, gap
                FIELD-1 is stale and this test is the reminder
        E       assert ['tap_replacement'] == []
        E         Left contains one more item: 'tap_replacement'
    """
    _graph, ctx, _config, first = paused[:4]
    requirement = first["dispatch_requirements"][-1]

    assert requirement.skills_required == [], (
        "nothing in `src` writes skills_required -- if that changed, gap FIELD-1 is stale and this "
        "test is the reminder"
    )
    assert requirement.parts_required == []
    assert requirement.equipment_required == []

    rows = await ctx.adapters.wfm.fetch_crew_availability(
        area=requirement.area_archetype.value,
        crew_type=requirement.crew_type.value,
        window_start=NOW,
        window_end=NOW + timedelta(hours=9),
    )
    assert rows, "the premise of the whole file: this fixture has a joint crew"
    for row in rows:
        assert "carried_equipment" not in row, (
            "the crew half of EQUIPMENT. `_crew_slot` passes an empty list because there is no key "
            "to read, which is why the constraint is dead from both ends"
        )

    # Starved, not broken: feed each field a value no crew can satisfy and the same solve refuses.
    for field_name, demanded, code, names_it in (
        ("parts_required", "SPLICE-KIT-9", ConstraintCode.PARTS, "van stock lacks SPLICE-KIT-9"),
        ("skills_required", "confined_space", ConstraintCode.SKILL, "lacks confined_space"),
        (
            "equipment_required",
            "bucket_truck",
            ConstraintCode.EQUIPMENT,
            "is not carrying bucket_truck",
        ),
    ):
        demanding = dict(first)
        demanding["dispatch_requirements"] = [
            requirement.model_copy(update={field_name: [demanded]})
        ]
        update = await optimize_field_schedule.__wrapped__(demanding, ctx)  # type: ignore[attr-defined]
        plan = update["dispatch_plan"]
        explanation = plan.constraint_explanation[requirement.requirement_id]

        assert list(plan.unassigned) == [requirement.requirement_id], (
            f"{field_name} is unwritten, not unenforced: naming {demanded} must refuse the crew"
        )
        assert blocking_code(explanation) is code
        assert names_it in explanation, "the dispatcher is told which item, not merely which code"

    satisfied = first["dispatch_plan"].constraint_explanation[requirement.requirement_id]
    assert satisfied.startswith("satisfied: ")
    assert len(satisfied.partition("satisfied: ")[2].partition(" (")[0].split(",")) == 12, (
        "all twelve are reported satisfied on a path that can only feed five -- the failure mode "
        "`satisfied_codes` was written to prevent, and the reason FIELD-1 is worth recording"
    )


async def test_an_unschedulable_solve_is_audited_with_the_code_that_blocked_it(
    paused: Any,
) -> None:
    """P15's audit reason comes off the plan, not off a guess about which constraint bit.

    This was a real defect, found by measurement rather than by reading: the node stamped
    `ReasonCode.CUSTOMER_ACCESS_REQUIRED` on *every* infeasible solve. That code is right for
    exactly one constraint -- `CUSTOMER_ACCESS` -- and per gap FIELD-1 that is one of the seven
    constraints which cannot refuse anything this stage produces. So the one case it described was
    the one case that cannot happen, and every case that can (a crew short of a part, no capacity in
    the window, the wrong archetype) was filed under a customer who was never asked to be home.

    An audit trail that names the wrong cause is worse than one that names none, because the reason
    code is what closure and reconciliation read, and `blocking_code` had already recovered the
    right answer two lines below for `detail`. The fix was to stop having two owners: `_reason_for`
    is the mapping `queue_for_dispatcher` uses, so the two nodes cannot describe one refusal
    differently.

    A skill block is asserted alongside the parts one because it maps to **nothing**, and that is
    correct rather than a hole -- `_REASON_BY_CONSTRAINT` covers the four codes the reason vocabulary
    has a word for, and inventing `SKILL_MISSING` would add an entry to a vocabulary other stages
    read. `None` reads as "not applicable", and the machine-readable code is in `detail` regardless.

    Shown red by restoring the hardcode (`reason_code=None if assigned else
    ReasonCode.CUSTOMER_ACCESS_REQUIRED`):

        E       AssertionError: a parts refusal filed as a customer-access failure
        E       assert <ReasonCode.CUSTOMER_ACCESS_REQUIRED: 'CUSTOMER_ACCESS_REQUIRED'> is
                <ReasonCode.PARTS_UNAVAILABLE: 'PARTS_UNAVAILABLE'>
        E        +  where <ReasonCode.CUSTOMER_ACCESS_REQUIRED: ...> = AuditEvent(
                    event_id='AUD-7d43c3d1d2fec543c76f', ..., detail={... 'blocking_code': 'parts',
                    'explanation': 'parts: blocked by parts -- crew CREW-JOINT-SJ-01 van stock
                    lacks SPLICE-KIT-9'}, ...).reason_code

    The detail in that very traceback is the argument: `blocking_code` had already recovered
    `'parts'` two lines below, on the same event that was filing it as a customer-access failure.
    """
    _graph, ctx, _config, first = paused[:4]
    requirement = first["dispatch_requirements"][-1]

    scheduled = [e for e in first["audit_events"] if e.node == "optimize_field_schedule"]
    assert [e.outcome for e in scheduled] == ["scheduled"]
    assert scheduled[0].reason_code is None, "a solve that placed the job has nothing to explain"
    assert scheduled[0].detail["blocking_code"] is None

    for field_name, demanded, expected in (
        ("parts_required", "SPLICE-KIT-9", ReasonCode.PARTS_UNAVAILABLE),
        ("skills_required", "confined_space", None),
    ):
        demanding = dict(first)
        demanding["dispatch_requirements"] = [
            requirement.model_copy(update={field_name: [demanded]})
        ]
        update = await optimize_field_schedule.__wrapped__(demanding, ctx)  # type: ignore[attr-defined]

        (event,) = update["audit_events"]
        assert event.outcome == "no_feasible_slot"
        assert event.reason_code is expected, (
            "a parts refusal filed as a customer-access failure"
            if expected is not None
            else "a skill has no word in the reason vocabulary, and inventing one is not the fix"
        )
        assert event.detail["blocking_code"] == field_name.partition("_")[0].replace(
            "skills", "skill"
        ), "the machine-readable code is in the detail whether or not the vocabulary has a word"
