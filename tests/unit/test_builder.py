"""The parent graph, compiled and run.

`test_nodes.py` drives P01-P11 by calling `fn.__wrapped__`, which skips the `@node` decorator
because there is no LangGraph runtime in a plain function call. Everything the wrapper does was
therefore untested until this module: `get_runtime(GraphContext).context`, the `check_budgets` call
on entry, the `bump_visit` counter the guard reads, and the escalation return that skips the body.
All four are exercised the moment a compiled graph is invoked, so these tests invoke one.

That is also why the fixtures are driven end to end again here rather than trusted from
`test_nodes.py`. The two modules assert different things about the same eleven steps: that they
compute the right answer, and that a real graph can run them at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

import lpr_cpe.graph.builder as builder_module
from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.config.settings import Settings
from lpr_cpe.domain.enums import (
    ActionType,
    CaseType,
    EventSource,
    FaultDomain,
    IncidentStatus,
    KPIName,
    Severity,
    Technology,
)
from lpr_cpe.domain.field_ops import HandoverContract
from lpr_cpe.domain.lifecycle import STAGE_TRANSITIONS, TRANSITIONS
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import (
    BRANCH_TARGETS,
    DECISION_AFTER,
    ESCALATED,
    ONWARD,
    PENDING_STAGES,
    SUBGRAPH_NODES,
    SUBGRAPH_SUCCESSOR,
    GraphTopologyError,
    build_parent_graph,
    compile_parent_graph,
)
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.nodes import CLOSURE_NODES, GOVERNANCE_NODES, PARENT_NODES
from lpr_cpe.graph.nodes._runtime import derive_id
from lpr_cpe.graph.state import make_initial_state, total_steps

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

HEALTHS = ("hfc_degraded_upstream", "pon_degraded_optical", "pon_power_affected", "hfc_healthy")

#: The approval each health profile's single pass parks on. A `node_visits` assertion cannot see
#: this -- a paused subgraph has written nothing the parent can read -- which is how the prose in
#: `test_a_fixture_runs_the_whole_parent_graph_without_writing_anything` went stale twice.
PARKS_AT = {
    "hfc_degraded_upstream": "dispatch",
    "pon_degraded_optical": "clean_to_dirty_handover",
    "pon_power_affected": "clean_to_dirty_handover",
    "hfc_healthy": "dispatch",
}


class _Ticking(FrozenClock):
    """A clock that advances on every read.

    Unlike `test_nodes.py`'s frozen clock, nothing here can advance it between nodes: the graph
    calls the nodes, not the test. Advancing on read is what keeps KPI durations non-zero, and it
    stays deterministic because the number of reads is a property of the run, not of the wall.

    Subclassed off `FrozenClock` rather than written from scratch so that `local_now()` and
    `timezone` are the *production* implementations. A hand-rolled double that stopped at `now()`
    would satisfy nothing that reads the operating timezone -- and the policy engine's daily contact
    cap is counted against a local date, so the omission would surface as a cap that silently
    stopped applying rather than as an error here.

    `local_now()` is inherited unchanged and so does **not** tick: it renders the instant the last
    `now()` returned. That is the closest an advance-on-read clock can get to the protocol's "the
    same instant", and it keeps a node that reads both from straddling two of them.
    """

    def now(self) -> datetime:
        return self.advance(timedelta(seconds=3))


def _initial(service: dict[str, Any], *, case_type: CaseType = CaseType.PROACTIVE_ALARM) -> Any:
    return make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=AssuranceEvent(
            event_id=f"EVT-{service['service_ref']}",
            source=EventSource.NXT,
            case_type=case_type,
            technology=Technology(service["technology"]),
            severity=Severity.HIGH,
            occurred_at=NOW - timedelta(minutes=6),
            received_at=NOW - timedelta(minutes=5),
            customer_ref=service["customer_ref"],
            service_ref=service["service_ref"],
            cpe_ref=service["cpe_ref"],
            summary=f"degraded service on {service['service_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


def _service(fixtures: Any, health: str) -> dict[str, Any]:
    return next(s for s in fixtures.services.values() if s["health"] == health)


async def _run(
    service: dict[str, Any],
    *,
    case_type: CaseType = CaseType.PROACTIVE_ALARM,
    **ctx_kwargs: Any,
) -> tuple[dict[str, Any], Any]:
    ctx = build_context(clock=_Ticking(NOW), **ctx_kwargs)  # type: ignore[arg-type]
    final = await compile_parent_graph().ainvoke(
        _initial(service, case_type=case_type), context=ctx
    )
    return final, ctx


# ------------------------------------------------------------------------------------------------
# The shape LangGraph actually received
# ------------------------------------------------------------------------------------------------


def test_langgraph_holds_the_topology_the_specification_numbers() -> None:
    """Every edge, longhand, read back out of the `StateGraph` rather than off the builder's table.

    Asserted against `graph.branches[...].ends` -- what LangGraph stored -- and not against
    `BRANCH_TARGETS`, which would only prove the table equals itself. `_check_tables` already ties
    the table to `DECISIONS` and to the node registry; what neither it nor `DECISIONS` can know is
    that P02 is followed by D01 and P06 by P07, because nothing in the code knows what P-numbers
    mean. That is the same division of labour as `test_nodes.py`'s registry-order test.

    Not asserted against `compiled.get_graph().edges`: that rendering collapses parallel edges by
    `(source, target)`, so D01's `quarantine` and the guard's `__escalated__` -- both of which end
    at `END` from `normalize_event` -- come back as one edge with whichever label survived. A test
    written against it would silently stop checking half the branches.
    """
    graph = build_parent_graph()
    ends = {
        source: dict(branch.ends or {})
        for source, branches in graph.branches.items()
        for branch in branches.values()
    }

    assert ends == {
        # P01 -> P02. No decision between them; the guard's branch is the builder's.
        "receive_signal": {ONWARD: "normalize_event", ESCALATED: END},
        # P02 -> D01. The question is about the normalised event's data-quality score, which is
        # what P02 computes, so it cannot be asked before P02 has run.
        "normalize_event": {
            "quarantine": END,
            "continue": "resolve_identity_and_topology",
            ESCALATED: END,
        },
        # P03 -> D02. `enrich` is the bounded enrichment retry; its bound is the guard's.
        "resolve_identity_and_topology": {
            "enrich": "resolve_identity_and_topology",
            "manual_review": END,
            "continue": "deduplicate_and_correlate",
            ESCALATED: END,
        },
        # P04 -> D03. Both answers reach P05: the specification's "if yes" arm ends "continue to
        # impact assessment for the affected customer", which is where "if no" goes too.
        "deduplicate_and_correlate": {
            "associate": "assess_impact_and_priority",
            "continue": "assess_impact_and_priority",
            ESCALATED: END,
        },
        # P05 -> D04. The one decision whose answers leave in opposite directions: `active`
        # continues down the main line to P06, `preventive` hands the thread to a subgraph that
        # ends it.
        "assess_impact_and_priority": {
            "preventive": "preventive_maintenance",
            "active": "create_or_attach_incident",
            ESCALATED: END,
        },
        # P06 -> P07. The stage boundary between intake and evidence, and a plain edge.
        "create_or_attach_incident": {ONWARD: "assemble_case_evidence", ESCALATED: END},
        # P07 -> D05.
        "assemble_case_evidence": {
            "gather_more": "assemble_case_evidence",
            "manual_review": END,
            "continue": "create_diagnostic_test_plan",
            ESCALATED: END,
        },
        # P08 -> P09 -> P10. Plan, run, conclude, with nothing to decide in between.
        "create_diagnostic_test_plan": {ONWARD: "execute_read_only_tests", ESCALATED: END},
        "execute_read_only_tests": {ONWARD: "determine_root_cause", ESCALATED: END},
        # P10 -> D06. `retry_diagnosis` returns to P07, not to P10: a rejected RCA is not
        # re-derivable from the evidence that produced it.
        "determine_root_cause": {
            "approve_low_confidence": "prepare_low_confidence_review",
            "retry_diagnosis": "assemble_case_evidence",
            "continue": "generate_resolution_options",
            ESCALATED: END,
        },
        # P11 -> D07, D08, D09, D11 -- four questions with no step between any two of them, so
        # LangGraph holds *one* edge carrying the union of their terminal answers. The comment on
        # each names the question it came from, which is the thing the edge itself no longer says.
        #
        # `continue` and `self_help_check` are the assertion worth reading. They are how the chain
        # gets from one question to the next, they appear in `BRANCH_TARGETS` under D07, D08 and
        # D09, and they are absent here -- consumed by `_cascade` rather than routed. An edge
        # labelled `continue` leaving P11 would mean the composition was never applied.
        #
        # `plant_path` reached `END` until `plant_referral` was written, and deleting that stage's
        # `PENDING_STAGES` line is the change this destination records.
        "generate_resolution_options": {
            "approve_high_blast_radius": "prepare_blast_radius_approval",  # D07
            "escalate": "record_escalation",  # D07
            "plant_path": "plant_referral",  # D08
            "remote": "remote_resolution",  # D09
            "self_help": "self_help",  # D11
            "field_planning": "field_planning",  # D11
            ESCALATED: END,
        },
        # D06's and D07's own gates, and the only place in the graph where a decision's answer
        # leads to a node that asks the *same* decision again. Both `prepare` nodes reach their
        # `request` by a plain edge, because a gate pair has nothing to decide between recording
        # the demand and raising the interrupt.
        "prepare_low_confidence_review": {ONWARD: "request_low_confidence_review", ESCALATED: END},
        "prepare_blast_radius_approval": {ONWARD: "request_blast_radius_approval", ESCALATED: END},
        # The `request` nodes re-ask, and the cycle they close is the assertion worth reading:
        # `approve_low_confidence` points back at the `prepare` node it came from. That terminates
        # because `request_approval` writes the human's answer into `approvals` and both routers
        # consult it before re-testing the condition that opened the gate -- `approval_outstanding`
        # compares the latest demand's timestamp against the latest answer's, so an answered demand
        # stops being outstanding. `test_governance_nodes.py` drives the cycle and measures that the
        # gate is entered exactly once per demand.
        #
        # The cycle is spelled out here rather than elided because it is the only one in the graph
        # where a decision's answer leads to a node that asks the same decision. What this test does
        # *not* guard is the clause ordering inside `route_rca_confidence`, and that was measured
        # rather than assumed: moving the `rca is None` clause above the answer check -- the
        # ordering that would let an unanswerable gate re-open forever -- turns exactly one test in
        # the suite red, and it is neither this one nor anything that runs the graph::
        #
        #     FAILED tests/unit/test_routing.py::
        #       test_every_declared_branch_is_reached_by_the_state_written_for_it[D06-retry_diagnosis]
        #     E   AssertionError: assert 'approve_low_confidence' == 'retry_diagnosis'
        #
        # No graph run notices, because P10 produces an RCA on every lap, so the state that would
        # spin cannot survive a retry. The reaching-state table is what holds that ordering; this
        # edge only holds that the loop is drawn.
        "request_low_confidence_review": {
            "approve_low_confidence": "prepare_low_confidence_review",
            "retry_diagnosis": "assemble_case_evidence",
            "continue": "generate_resolution_options",
            ESCALATED: END,
        },
        # D07's gate re-enters the same four-question cascade P11 does, not D07 alone: the answer
        # that releases the gate is `continue`, and `continue` is what `_cascade` consumes to ask
        # D08. So a granted approval carries on to the plant/remote/self-help fork in one step
        # rather than needing a node between the gate and the rest of the chain.
        "request_blast_radius_approval": {
            "approve_high_blast_radius": "prepare_blast_radius_approval",
            "escalate": "record_escalation",
            "plant_path": "plant_referral",
            "remote": "remote_resolution",
            "self_help": "self_help",
            "field_planning": "field_planning",
            ESCALATED: END,
        },
        # D07's other arm, and the one node in the graph that is terminal on purpose rather than
        # for want of a successor. `_DELIBERATE_TERMINALS` is what tells `_check_pending_stages`
        # the difference; without the entry this node would be reported as an unbuilt exit.
        "record_escalation": {ONWARD: END, ESCALATED: END},
        # P16 -> P17, and the only edge in the graph that comes from neither a decision nor a
        # position in `PARENT_NODES`. A subgraph has no position, so `_plain_edges` cannot pair it
        # with a neighbour; `SUBGRAPH_SUCCESSOR` is the third way a stage acquires a successor, and
        # the warrant for using it here is that the specification puts no decision between the two.
        #
        # The edge fires on all three of planning's exits -- `commit_field_dispatch`,
        # `queue_for_dispatcher` and `abandon_field_planning` -- and that is the design rather than
        # an oversight. Only the first writes `work_orders`, so the other two reach Stage 4 with
        # nothing booked and `route_visit_gate` answers `no_visit`. Excluding them in the parent
        # would have left that arm unreachable, and an arm no state can enter is an arm no test can
        # hold to account.
        "field_planning": {ONWARD: "field_execution", ESCALATED: END},
        # The two terminal subgraphs, and the only edges here that carry no question at all. Both
        # keys end at `END`, which is what `_plain_edges` produces for anything in neither
        # `DECISION_AFTER` nor `SUBGRAPH_SUCCESSOR` -- the same edge the last ordered step would
        # get, and the reason the loop that draws it was dead code until the first of these was
        # wired. `preventive_maintenance` picks its own disposition internally and every
        # disposition is the end of that thread, so it leaves the parent nothing to ask.
        #
        # `field_execution` used to be the second of these and is not any more: it now carries D16,
        # below. Its exits were never a terminal disposition -- they stopped for want of the plant
        # stage, which is the correction `PENDING_STAGES` records.
        #
        # `reconciliation_closure` is the third, and the only one of the three that is terminal
        # because the workflow is *over* rather than because something is unwritten -- which is why
        # it is the second name in `_DELIBERATE_TERMINALS` and has no `PENDING_STAGES` line. Its
        # main line ends at P26, which writes `IncidentStatus.CLOSED`, and `domain.lifecycle` gives
        # `closed` no outward transition: there is not merely no next node here but no legal one.
        #
        # Guarded even so, and these are the three guarded edges in the graph where that buys
        # nothing: both keys go to `END`, so `guarded` reading `escalated` cannot change where the
        # run goes. It is here because the terminal loop draws one kind of edge rather than two,
        # and a special case for "the destinations happen to be equal" would be a branch in the
        # builder that no run can distinguish. The escalation was recorded inside the subgraph, by
        # that subgraph's own guarded edges, before the parent saw the state at all.
        "preventive_maintenance": {ONWARD: END, ESCALATED: END},
        "reconciliation_closure": {ONWARD: END, ESCALATED: END},
        # Stage 4's two halves. D16 is asked here *and* inside `field_execution`, which is the one
        # decision in the graph that appears twice, and it is not a duplicate: the subgraph has a
        # single exit and four dispositions to say through it, and `_check_tables` admits only
        # decisions that are in `routing.DECISIONS`, so no local gate can sit on this edge. The
        # second reading is the first one's answer by construction -- none of the four nodes that
        # end the stage writes a `FieldFinding`, and that is all `route_clean_boots_outcome` reads.
        "field_execution": {
            "validate": "restoration_validation",
            "delimit": "plant_execution",
            ESCALATED: END,
        },
        # `SUBGRAPH_SUCCESSOR`'s second entry, P20 into P21, and the D08-direct way into the plant
        # stage. Together with `field_execution`'s `delimit` above it makes two feeders for one
        # stage, which is the fact neither builder table can hold: `SUBGRAPH_SUCCESSOR` knows this
        # edge and `BRANCH_TARGETS` knows that one, and only reading them back out of the same
        # `StateGraph` shows that they converge.
        "plant_referral": {ONWARD: "plant_execution", ESCALATED: END},
        # `await_plant` is a self-loop, and unlike `SUBGRAPH_SUCCESSOR`'s -- which `_check_tables`
        # refuses outright -- this one is a `BRANCH_TARGETS` entry and allowed. What stops it is
        # `capture_plant_evidence`, which raises an interrupt for OSP's report, so a lap that
        # arrives with nothing new parks rather than spinning; the guard's ceiling sits underneath.
        # `restored` is absent because it names D20 rather than a node: `_cascade` follows it inside
        # the edge function and what reaches the path map is always one of D20's own answers.
        "plant_execution": {
            "await_plant": "plant_execution",
            "retry_diagnosis": "determine_root_cause",
            "reverse_handover": "field_planning",
            "verify": "restoration_validation",
            ESCALATED: END,
        },
        # The other two subgraphs. D10 and D12 are asked *here* and not inside them because every
        # destination either answer has is a sibling the subgraph does not contain -- a subgraph
        # cannot route to P07, and `retry_diagnosis` is most of the point of both.
        #
        # Both `verify` answers reach the same stage, which is the whole reason Stage 5 shortened
        # the pending list rather than leaving it level: one subgraph closed two exits.
        "remote_resolution": {
            "verify": "restoration_validation",
            "retry_diagnosis": "assemble_case_evidence",
            ESCALATED: END,
        },
        # D12's `retry_diagnosis` goes to P10 and D10's to P07, which is not a copy-paste slip:
        # self-help changes nothing the diagnostic reads unless it worked, so the same evidence
        # supports a second opinion, while a remote repair that did not hold means the device has
        # changed since the evidence was gathered.
        "self_help": {
            "verify": "restoration_validation",
            "retry_diagnosis": "determine_root_cause",
            "field_planning": "field_planning",
            ESCALATED: END,
        },
        # D21, and the only subgraph in the graph that is its own destination.
        # `continue_observation` re-enters the stage it came from, which is what the specification's
        # "continue observation when evidence is improving but incomplete" asks for, and the reason
        # it is allowed here is worth stating because `_check_tables` refuses the same shape one
        # table over: a `SUBGRAPH_SUCCESSOR` self-loop is a build-time error on the grounds that
        # "the guard's re-entry budget would be the only thing stopping the run". Here it is not the
        # only thing. The subgraph's head is `await_service_stability`, which raises an interrupt
        # while the window is still running, so a lap that arrives early parks instead of reading
        # again -- and `min_post_fix_samples` stays a count of observations rather than of laps.
        # The guard's ceiling is still underneath it, six by `attempt_limits.max_subgraph_reentries`,
        # because `@node` checks budgets on entry like every other node.
        #
        # `confirm_outcome` is the answer that makes P23 a parent node rather than a fourth node in
        # the subgraph. D21 and D22 both answer `retry_diagnosis`, and `_terminal_targets` refuses
        # two decisions in one chain that share an answer -- one edge would carry both and the
        # branch could no longer name which question was asked. So the two cannot follow one node,
        # and P23 sits between them giving each a node of its own to hang from. Sharing an answer
        # across *different* edges is fine and already happens twice: D10 and D12 both answer
        # `retry_diagnosis`, to different nodes; D21 and D22 both answer it to the same one.
        "restoration_validation": {
            "continue_observation": "restoration_validation",
            "retry_diagnosis": "determine_root_cause",
            "confirm_outcome": "confirm_customer_outcome",
            ESCALATED: END,
        },
        # D22 -> the reconciliation stage, which is now written. This edge used to be the fourth of
        # four exits falling to `END` for want of something unwritten; the remaining three are in
        # `PENDING_STAGES`, which says what each waits on.
        "confirm_customer_outcome": {
            "reconcile": "reconciliation_closure",
            "retry_diagnosis": "determine_root_cause",
            ESCALATED: END,
        },
    }

    # The only unconditional edge in the graph, and the reason `receive_signal` is P01.
    assert sorted(graph.edges) == [("__start__", "receive_signal")]


def test_the_compiled_graph_contains_the_eleven_steps_the_three_subgraphs_and_nothing_else() -> (
    None
):
    """A fifteenth node would be reachable from nowhere; a missing one, an edge to nothing.

    The subgraphs are asserted from `SUBGRAPH_NODES` rather than by name, so that this says "the
    table was wired" and not "these three happen to be here". That is why wiring the preventive
    stage cost this test's *body* nothing and its *name* a word: the assertion followed the table,
    and only the sentence describing it had to be told. A compiled subgraph is added exactly as a
    function is, and `get_graph()` reports it as one node -- its own five, six or eight are one
    level down, which is what `xray` renders and what nothing here needs.
    """
    nodes = set(compile_parent_graph().get_graph().nodes)
    expected = {name for name, _ in PARENT_NODES} | set(SUBGRAPH_NODES)
    assert nodes == expected | {"__start__", "__end__"}
    assert not set(SUBGRAPH_NODES) & {name for name, _ in PARENT_NODES}, (
        "a subgraph is in PARENT_NODES too, so `_plain_edges` has drawn an edge into it from "
        "whatever is written above it -- see the module docstring on why they have no order"
    )


# ------------------------------------------------------------------------------------------------
# Running it
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("health", HEALTHS)
async def test_a_fixture_runs_the_whole_parent_graph_without_writing_anything(
    fixtures: Any, health: str
) -> None:
    """Each of the four services reaches P11 in one pass, and no adapter was asked to write.

    Two claims in one run, because the second is only interesting if the first holds -- a graph
    that stopped at P03 would also have written nothing. `node_visits` is asserted exactly rather
    than by count: it is the guard's own counter, written by the `@node` wrapper this module exists
    to exercise, and a run that looped P07 three times would still total eleven if the assertion
    were `len(...) == 11`.

    Where each of the four stops was measured rather than reasoned about, and the answer is now the
    same for all four: each enters a subgraph and parks in it. Two divert at D08
    (`pon_degraded_optical` and `pon_power_affected`, both `plant_path`) and stop at P19, asking an
    `osp_engineer` for `clean_to_dirty_handover` on a `raise_mr` against the ODP that `delimiter_ref`
    named. The other two walk the whole chain to D11 and answer `field_planning` --
    `hfc_degraded_upstream` and `hfc_healthy`, both diagnosed `tap_or_odp`, whose only options are
    `raise_mr` and `create_work_order` and both of those set `requires_truck_roll`.
    `is_remote_option` and `is_self_help_option` reject a truck roll, so there is nothing for D09 or
    D11 to choose, and they stop at that stage's `dispatch` gate.

    So all four of these runs enter a subgraph, and the eleven are still eleven anyway. This
    paragraph has now gone stale the same way twice, and how is the point worth keeping. When it was
    first written, `D11:field_planning` was a pending exit to `END`; wiring the stage changed where
    the answer goes without changing anything this test asserts. Then `D08:plant_path` was a pending
    exit, and wiring `plant_referral` did it again -- the sentence saying those two runs ended the
    run survived the edit that gave them somewhere to go.

    The mechanism is the same both times: a paused subgraph's writes have not reached the parent, so
    `ainvoke` returns with an `__interrupt__` in it and `node_visits` untouched. That is the property
    `graph.subgraphs` documents and `graph.inspect` exists to work around, and here it means an
    assertion counting parent visits cannot tell a stage that is not entered from one that is entered
    and parks. So `PARKS_AT` is asserted below: the second rewire put an interrupt where there had
    been none at all, and reading `__interrupt__` is what makes a third such edit turn this red
    instead of leaving the prose to rot again.

    What that line adds over the visit count was measured rather than assumed, because the two
    obvious mutations do not both reach it. Making `route_plant_referral_gate` answer `abandon`
    instead of `refer` for a case with no decision yet never gets there: the stage runs to
    completion instead of parking, so its writes *do* reach the parent and the visit count catches
    it two lines earlier, `Left contains 2 more items: {'abandon_plant_referral': 1,
    'evaluate_plant_referral': 1}`. Filing P19's approval under `ApprovalKind.DISPATCH` is the one
    that reaches it -- the run still parks, the count is still eleven, and only this line notices::

        E   AssertionError: the run stopped somewhere other than the paragraph above says
        E   assert 'dispatch' == 'clean_to_dirty_handover'

    Not the only guard for that defect, and the difference is the point. Six tests in
    `test_subgraph_plant_referral.py` went red too, but the sharpest of them reads `assert 6 == 1`:
    the gate stops recognising the answer, so the approval is asked six times until the guard's
    budget stops it, and the failure names a symptom three nodes downstream of the cause. This one
    names the cause. Both are worth having.

    Only these four, and `_service` takes the *first* service of each health profile -- the fork is
    not dead across the fixture set. Swept over all 41 services, two do enter `remote_resolution`
    (`SVC-UT-001-B-01` and `SVC-SJ-011-B-01`) and none reach `self_help`, for a reason the self-help
    module's docstring records: the budget, not the wiring.

    Wiring D06's and D07's gates added five nodes to `PARENT_NODES` that this run must *not* visit,
    and Stage 5 added a sixth, P23. The expectation subtracts both registries by name rather than
    listing the eleven, so a twelfth node on the linear line still turns this red while a node
    reachable only through a branch does not. What the two subtracted registries have in common is
    exactly that: the governance five are reached only from D06's and D07's own arms, and
    `confirm_customer_outcome` only from D21's `confirm_outcome`, which is three stages past where
    any of these four gets to.

    The governance five are asserted absent rather than left unmentioned, and that absence is the
    measurement, not an accident of these four services: with both routers instrumented at
    `_cascade`'s call site and all 82 fixture cases driven to completion, D06 and D07 were asked
    134 times between them and every single answer was `continue`. The fixture corpus records no
    `PolicyDecision` of either gating kind, and `route_rca_confidence`'s other opening -- `rca is
    None` -- never fires either, because P10 always produces one. The arms are reachable and the
    mechanism was watched working (`SVC-SJ-011-B-01-proactive_alarm` re-enters both decisions three
    times as the retry arm carries it back upstream, seeing 0, then 1, then 2 policy decisions);
    what the corpus lacks is a decision of the right *kind*. `test_governance_nodes.py` therefore
    seeds one rather than hoping for it, and this test holds the complementary claim: nothing seeds
    one by accident.

    This run is also what guards `_cascade`. Composing the chain has two halves -- the path map
    (`_terminal_targets`) and the router (`_cascade`) -- and only the map is asserted structurally,
    by `test_langgraph_holds_the_topology_the_specification_numbers`. Reverting the router half
    alone, to `guarded(DECISIONS[identifier].route)`, leaves a graph that builds and compiles and
    then fails here, on all four services::

        E   KeyError: 'continue'
        E   During task with name 'generate_resolution_options' and id '83deb6e4-...'
        langgraph/graph/_branch.py:203: KeyError

    -- the branch returning the answer that `_terminal_targets` consumed. That mutation was watched:
    six tests went red, every one of them a test that runs the graph, and the structural test stayed
    **green**, which is what shows the two halves need two guards rather than one.
    """
    final, ctx = await _run(_service(fixtures, health))

    off_the_line = {name for name, _ in (*CLOSURE_NODES, *GOVERNANCE_NODES)}
    assert final["node_visits"] == {name: 1 for name, _ in PARENT_NODES if name not in off_the_line}
    assert total_steps(final) == 11
    assert final["escalated"] is False
    assert final["status"] is IncidentStatus.DIAGNOSING
    assert final["resolution_plan"] is not None
    # A tuple, so `== []` would fail on the type alone and read as a real defect.
    assert list(ctx.adapters.gate.recorded) == []

    (pause,) = final["__interrupt__"]
    assert pause.value["approval_request"]["kind"] == PARKS_AT[health], (
        "the run stopped somewhere other than the paragraph above says"
    )


async def test_the_data_quality_metric_is_emitted_before_the_branch_that_needs_it(
    fixtures: Any,
) -> None:
    """D01's rejection path must "generate a data-quality metric", so P02 has to be the one to.

    Every other KPI is emitted by P06, which the quarantine branch never reaches -- it leaves the
    graph at D01, two nodes earlier. Emitting from P02 is what puts the metric on both sides of that
    branch, and this asserts the side that is reachable with the fixtures: a healthy event scores
    zero defects and still emits, because a rate needs a denominator and a KPI that only appeared
    for bad events would report a defect rate of 1.0 forever.

    `KPIEvent` has no node field -- the node is one of the parts `emit_kpi` hashes into the event
    id, which is what makes a replayed node record the same measurement once rather than twice. So
    the id is rebuilt here from the parts P02 should have used, and comparing it is how this test
    knows *which* node emitted the metric rather than merely that something did.

    The trailing `""` is `emit_kpi`'s `discriminator`, and it is spelled out rather than omitted
    because `derive_id` joins its parts with a separator: dropping an empty trailing part would hash
    different material and this comparison would fail for a reason that has nothing to do with what
    it is testing. P02 runs once per incident and so passes no discriminator; a node inside a
    diagnostic loop passes the cycle counter, which is what keeps its successive measurements
    distinct under `append_unique`.
    """
    final, _ = await _run(_service(fixtures, "hfc_healthy"))

    name = KPIName.DATA_QUALITY_DEFECT_RATE
    quality = [e for e in final["kpi_events"] if e.kpi_name == name.value]
    assert len(quality) == 1, "exactly one node measures data quality, and it is P02"

    measured = quality[0]
    expected = derive_id("KPI", final["incident_id"], name, "normalize_event", "")
    assert measured.event_id == expected, (
        "the data-quality metric is no longer keyed to P02; if it moved to a node after D01 then "
        "the quarantine branch emits nothing, which is the case the specification asks for"
    )
    assert measured.numerator == 0.0, "this fixture's event is clean on its face"
    assert measured.denominator == 1.0


async def test_a_power_cut_is_diagnosed_rather_than_sent_to_data_quality_review(
    fixtures: Any,
) -> None:
    """An ONT with no mains power is a diagnosis, not a missing adapter.

    This fixture used to escalate. `SimulatedCPEAdapter.run_diagnostic` raised
    `AdapterUnavailableError` for an offline device, that flag is in
    `DataQualityAssessment.BLOCKING_FLAGS`, and it was the only blocking flag in the case -- so D05
    answered `gather_more` on every pass, P07 re-ran until the diagnostic-cycle budget escalated it,
    and P08 to P11 never ran. The evidence to close the case was already in state throughout: a
    dying gasp, an open utility outage in `linked_records`, and a `power_correlation` finding.

    Asserted here rather than only in the adapter's own tests because the adapter's contract looked
    locally reasonable -- "the device is offline, so I cannot answer" -- and only the whole chain
    shows what it cost. The two assertions that would go red on a revert are `fault_domain` and
    `escalated`; the rest state what a correct power case looks like.
    """
    final, _ = await _run(_service(fixtures, "pon_power_affected"))

    assert final["escalated"] is False
    assert final["fault_domain"] is FaultDomain.POWER
    assert final["node_visits"]["assemble_case_evidence"] == 1, "the evidence stage did not loop"

    assessment = final["data_quality"]
    assert assessment.blocking == [], (
        "an offline CPE is raising a blocking data-quality flag again; the ACS answered, so this "
        "is a result and not an unreachable system"
    )
    assert assessment.sufficient_for_action

    # The whole set and not just membership: `notify_customer` is the worked example under CPE-7 and
    # RESOLUTION-5 in `docs/vendor-integration-gaps.md`, where the open question is that
    # `is_remote_option` accepts it and only D08 diverting `power` keeps it away from D09. A third
    # option appearing here could make that note stale, so this is the place to hear about it.
    offered = {o.action_type for o in final["resolution_plan"].options}
    assert offered == {ActionType.NOTIFY_CUSTOMER, ActionType.RAISE_MR}, (
        "the power plan is no longer exactly [notify_customer, raise_mr]; somebody still has to "
        "tell the customer why, and the two gap notes quote this set"
    )


#: A service that is mildly off and that correlation finds no peers for -- the shape a predictive
#: case actually has. Named by ref rather than found by health label because `_service` returns the
#: first match, and the first `hfc_healthy` fixture sits behind the degraded SJ-011-A tap with five
#: correlated peers: healthy itself, and an active case however it is filed.
QUIET_SERVICE = "SVC-PO-042-A-04"


async def test_a_predictive_case_on_a_quiet_service_actually_reaches_the_preventive_arm(
    fixtures: Any,
) -> None:
    """D04's `preventive` arm is reachable from a real intake run, and this is what proves it.

    `test_routing` asks the router directly, on a state assembled by hand. That is what let the
    original fault survive: the state it asked about -- an assessment with
    `affected_customer_count == 0` -- is one the pipeline cannot produce. `blast_radius.size_of`
    floors a single-premises radius at `count=1, measured=True`, P05 always runs before D04 and
    always returns an assessment, so the old `> 0` test was satisfied by the subject alone. Every
    one of the 41 fixture services, filed as `PREDICTIVE_MAINTENANCE`, came out `active`. The unit
    test was green throughout.

    So this one runs the compiled graph and reads which nodes were entered. `escalated is False`
    matters as much as the arm itself: the guard also stops a run short, and without it a budget
    escalation at P05 would look exactly like the branch being taken.

    Shown red by restoring `route_predictive_or_active`'s original `affected_customer_count > 0`::

        AssertionError: a predictive case with no correlated peers walked on into incident
        creation; D04's preventive arm is unreachable again
        assert 'create_or_attach_incident' not in {'assemble_case_evidence': 1,
        'assess_impact_and_priority': 1, 'create_diagnostic_test_plan': 1,
        'create_or_attach_incident': 1, ...}

    It stays green under `> 1`, which is the point of the companion assertion in `test_routing`:
    the two tests fail for different reasons and neither covers the other.
    """
    quiet = fixtures.services[QUIET_SERVICE]

    preventive, _ = await _run(quiet, case_type=CaseType.PREDICTIVE_MAINTENANCE)
    assert preventive["escalated"] is False, "stopped by the guard, which is not the branch"
    assert preventive["node_visits"]["assess_impact_and_priority"] == 1
    assert "create_or_attach_incident" not in preventive["node_visits"], (
        "a predictive case with no correlated peers walked on into incident creation; D04's "
        "preventive arm is unreachable again"
    )

    # The same service, same fixtures, filed as an alarm: the other arm, so the assertion above is
    # about the case type and not about this service being unable to get past P05 at all.
    active, _ = await _run(quiet, case_type=CaseType.PROACTIVE_ALARM)
    assert active["node_visits"]["create_or_attach_incident"] == 1


# ------------------------------------------------------------------------------------------------
# Running it *through* a subgraph, which is not the same as running it up to one
# ------------------------------------------------------------------------------------------------

#: Answered to every approval gate on the way through, so the run reaches the boundary at all. Which
#: gate is being answered does not matter here -- `test_graph_foundations.py` owns who may decide
#: what; this section owns what the parent sees once they have.
SEAM_APPROVAL = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "driving the incident across the stage boundary",
}


def _clean_boots_submission(service: dict[str, Any]) -> dict[str, Any]:
    """A visit that fixed the fault and owes no plant work -- the exit that leaves `validating`.

    The measurement keys come out of `REQUIRED_BY_TECHNOLOGY` rather than being spelled here, for
    `test_subgraph_field_execution.py`'s reason: the key is the thing the contract checks, and a
    hand-copied list would drift away from the contract it has to match.
    """
    required = HandoverContract.REQUIRED_BY_TECHNOLOGY[service["technology"]]
    return {
        "fault_domain": "drop",
        "delimiter_kind": "tap" if service["technology"] == "hfc" else "odp",
        "delimiter_ref": service["delimiter_ref"],
        "fault_confirmed": True,
        "no_fault_found": False,
        "work_completed": True,
        "requires_plant_work": False,
        "requires_permit": False,
        "measurements": dict.fromkeys(required, -14.5),
        "parts_replaced": ["drop cable"],
        "evidence_refs": ["PHOTO-1"],
        "technician_note": "replaced the drop; the premises test clean",
        "recorded_by": "t.nguyen",
        "last_clean_point": "drop at premises",
        "first_failed_point": service["delimiter_ref"],
        "customer_confirmed": True,
    }


def _let_the_window_elapse(payload: dict[str, Any], clock: FrozenClock) -> dict[str, str]:
    """Answer `await_service_stability` the way a scheduler does -- by having let the time pass.

    The wait node is `while ctx.clock.now() < deadline: interrupt(waiting)`, so the resume value is
    not what releases it; the clock is. Handing it a signature and leaving the clock where it was
    re-raises the identical interrupt, which is why `_walk` stops on this pause rather than
    answering it by default: measured, a run answered the same window ten times and then parked at
    `remote_resolution`, a stage it had in fact finished with.
    """
    resume_at = (payload["stability_window_wait"] or {}).get("resume_at")
    if resume_at:
        target = datetime.fromisoformat(resume_at)
        if target > clock.now():
            clock.set(target + timedelta(minutes=1))
    return {"resumed_by": "timer"}


async def _walk(
    service: dict[str, Any], *, thread: str, laps: int = 10, timer: bool = False
) -> tuple[Any, list[Any]]:
    """Drive the parent to a standstill, answering every pause, and report the statuses it held.

    `_run` cannot be reused: it compiles without a checkpointer, and without one an interrupt is not
    resumable -- `ainvoke` returns an `__interrupt__` and the run is over. Every existing test in
    this module therefore stops at the first gate, which is upstream of every stage boundary.

    `timer` is off by default because a stability window is the one pause with no human on the other
    end, and whether the clock is allowed to move is a different question from whether a decision is
    forthcoming. Left off, the run stops *at* the window and reports where the parent got before any
    time had to pass -- which is what the boundary below it is about. Turned on, the wait releases
    and the run carries on into the closure stage.

    The walk is read off `aget_state_history`, consecutive duplicates removed, so it is the sequence
    of values the *parent's* `status` channel actually took. That is a different list from the one
    the incident lived through, and the difference is the whole point.
    """
    clock = _Ticking(NOW)
    ctx = build_context(clock=clock)
    parent = build_parent_graph().compile(name="lpr_cpe_parent", checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": thread}}
    await parent.ainvoke(_initial(service), context=ctx, config=config)
    for _ in range(laps):
        snapshot = await parent.aget_state(config)
        if not snapshot.interrupts:
            break
        payload = snapshot.interrupts[0].value
        answer: Any
        if isinstance(payload, dict) and "stability_window_wait" in payload:
            if not timer:
                break
            answer = _let_the_window_elapse(payload, clock)
        elif isinstance(payload, dict) and "briefing" in payload:
            answer = _clean_boots_submission(service)
        else:
            answer = SEAM_APPROVAL
        await parent.ainvoke(Command(resume=answer), context=ctx, config=config)

    history = [snap.values.get("status") async for snap in parent.aget_state_history(config)]
    walk: list[Any] = []
    for status in reversed(history):
        if status is not None and (not walk or walk[-1] is not status):
            walk.append(status)
    return (await parent.aget_state(config)).values, walk


async def test_the_parent_is_shown_one_status_per_subgraph_not_the_ones_it_walked(
    fixtures: Any,
) -> None:
    """The stage boundary, crossed for real. This is the test whose absence cost a production run.

    A compiled subgraph shares the parent's `status` channel, so a reader expects the parent to see
    every value the child wrote. It does not: it is handed one write per channel, the child's last.
    Nothing in the suite noticed, because the three modules that cross a boundary all cross it the
    same careful way -- `interrupt_after=["field_planning"]`, then the seam state handed to a
    *standalone* compile of the child. That arrangement is the one in which the boundary is never
    exercised. Driven through the real parent instead, 20 of the 41 fixture services died here on
    `IllegalTransitionError: dispatch_planning -> validating`, and no service had ever reached
    `closed`.

    Two facts are asserted, and the second is the one that dates.

    **The run completes.** Reverting `domain.lifecycle`'s `STAGE_TRANSITIONS` to `{}` fails this
    test in `_walk` rather than at an assertion::

        E   lpr_cpe.domain.lifecycle.IllegalTransitionError: illegal incident transition
        E   dispatch_planning -> validating; permitted from dispatch_planning:
        E   ['awaiting_approval', 'cancelled', 'diagnosing', 'escalated', 'field_in_progress']
        E   During task with name 'field_execution' and id '...'

    **`field_in_progress` never appears.** That is the mechanism, not a consequence, and it is why
    the seam table is a fix rather than a workaround. If a LangGraph release starts forwarding a
    child's intermediate writes, this assertion goes red while everything else stays green -- and
    the correct response then is to delete `STAGE_TRANSITIONS`, not to update this line.

    That absence needs a control, or it proves only that nothing wrote the status. The control is
    `open_field_visit`, which is one of the three nodes in `field_execution.py` that writes
    `FIELD_IN_PROGRESS`, and which the parent's own `node_visits` records as having run once. So the
    same run reports the node and not its write -- and the difference is the reducer, not the graph:
    `node_visits` merges, so the child's last value contains every earlier one, while
    `advance_status` replaces, so the child's last value is all there is.

    The seam is asserted to be exactly one pair, because `field_planning` collapses too and its
    collapse is invisible: `diagnosing -> dispatch_planning` happens to be a legal single hop, so
    the same mechanism has been running unnoticed at that boundary since the stage was wired -- and
    `awaiting_approval`, which that stage really does pass through at its dispatch gate, is missing
    from the walk for the same reason. A test that only asserted "no exception" would distinguish
    neither, and would pass on a run that crossed no boundary at all.
    """
    service = _service(fixtures, "hfc_degraded_upstream")
    final, walk = await _walk(service, thread="seam-hfc")

    assert final["status"] is IncidentStatus.VALIDATING
    assert walk == [
        IncidentStatus.NEW,
        IncidentStatus.TRIAGING,
        IncidentStatus.DIAGNOSING,
        IncidentStatus.DISPATCH_PLANNING,
        IncidentStatus.VALIDATING,
    ]
    assert IncidentStatus.FIELD_IN_PROGRESS not in walk
    assert IncidentStatus.AWAITING_APPROVAL not in walk

    # The control: the node whose write is missing did run, and the parent knows it did.
    assert final["node_visits"]["open_field_visit"] == 1

    crossed = [(a, b) for a, b in pairwise(walk) if b not in TRANSITIONS[a]]
    assert crossed == [(IncidentStatus.DISPATCH_PLANNING, IncidentStatus.VALIDATING)]
    assert all(pair in STAGE_TRANSITIONS for pair in crossed)


#: The one service measured to run event-to-closure. Named by ref rather than found by health label
#: for `QUIET_SERVICE`'s reason and then some: eighteen fixtures share its `pon_healthy` label, and
#: the first of them -- `SVC-UT-001-A-01`, the same ODP one letter over -- takes the field path and
#: escalates. What separates them is peers: four services sit behind this delimiter and eight behind
#: that one, which is enough to move correlation off the shared-plant reading and onto the remote
#: fork. So the label is not the thing being selected for and spelling it would be misleading.
CLOSING_SERVICE = "SVC-UT-001-B-01"


async def test_the_closure_stage_collapses_onto_the_parent_as_one_hop_too(fixtures: Any) -> None:
    """The second seam, and the first run of any kind to reach `closed`.

    `field_execution`'s boundary above is the same mechanism, but it was found by 20 services dying
    on it. This one could not be found that way, because nothing was arriving: every incident left
    the closure stage through `abandon_closure`, whose `escalated` is a legal single hop from
    `validating` and so crosses no seam at all. What was holding them there was
    `PolicyEngine._check_confidence` demanding `low_confidence_rca` for a `close_incident` -- a kind
    `route_closure_gate` does not own. Fixing that put a run through the stage for the first time,
    and it failed immediately in `_walk`::

        E   lpr_cpe.domain.lifecycle.IllegalTransitionError: illegal incident transition
        E   validating -> closed; permitted from validating: ['cancelled', 'diagnosing',
        E   'dispatch_planning', 'escalated', 'reconciling', 'remote_resolution']
        E   During task with name 'reconciliation_closure' and id '...'

    So the order matters and is worth stating: this seam was unreachable, not absent, and the entry
    that fixes it could not have been written from the table alone. The pair is legal only because
    something walked the middle, and until the closure gate could answer its own question nothing
    ever did.

    **`reconciling` and `resolved` never appear.** That is the mechanism rather than a consequence,
    and the control is the same shape as the field seam's: `reconcile_linked_systems` writes
    `RECONCILING` and `close_linked_records` writes `RESOLVED`, and the parent's `node_visits`
    records both as having run once. Same run, the nodes and not their writes. If a LangGraph
    release starts forwarding a child's intermediate writes these two assertions go red together,
    and the answer then is to delete the seam entry rather than to edit this line.

    `awaiting_approval` is deliberately *not* asserted absent, unlike at the field seam. It is in
    this walk -- raised by the diagnosis gate, in the parent, where the parent can see it -- so its
    presence says nothing either way about the closure stage's own approval, and asserting on it
    would make the test fail for a reason it is not about.

    The walk itself is not asserted whole. It laps `diagnosing -> remote_resolution -> validating`
    three times before it closes, because a remote repair changes nothing a later read sees --
    fixture telemetry is keyed on a static `health` field with no writer. That is a real gap and it
    is not this test's; pinning the lap count here would make an unrelated fix look like a
    regression. What is pinned is the seam, which is what the entry under test authorises.
    """
    service = fixtures.services[CLOSING_SERVICE]
    final, walk = await _walk(service, thread="seam-closure", timer=True)

    assert final["status"] is IncidentStatus.CLOSED
    assert walk[-2:] == [IncidentStatus.VALIDATING, IncidentStatus.CLOSED]
    assert IncidentStatus.RECONCILING not in walk
    assert IncidentStatus.RESOLVED not in walk

    # The controls: both nodes whose writes are missing did run, and the parent knows it.
    assert final["node_visits"]["reconcile_linked_systems"] == 1
    assert final["node_visits"]["close_linked_records"] == 1

    # ...and it closed the ordinary way, so the middle recorded in the seam entry is the one walked.
    assert "prepare_exceptional_closure_approval" not in final["node_visits"]
    assert "abandon_closure" not in final["node_visits"]

    crossed = [(a, b) for a, b in pairwise(walk) if b not in TRANSITIONS[a]]
    assert crossed == [(IncidentStatus.VALIDATING, IncidentStatus.CLOSED)]
    assert all(pair in STAGE_TRANSITIONS for pair in crossed)


# ------------------------------------------------------------------------------------------------
# The escalation edge the builder owns
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "escalating_node"),
    [
        (4, "assess_impact_and_priority"),
        (7, "create_diagnostic_test_plan"),
        (9, "determine_root_cause"),
    ],
)
async def test_the_guard_stops_the_graph_at_the_node_that_escalated(
    fixtures: Any, budget: int, escalating_node: str
) -> None:
    """Nothing runs after the budget is exhausted -- which is the builder's edge, not the guard's.

    `escalation_update` sets `escalated` and skips the node's body, but on its own that does not
    stop the *graph*: only D02 and D05 read the flag. Without the edge wired here, a budget
    exhausted at P08 was measured to carry on through P09 and P10 -- three node entries and three
    checkpoint writes past the limit, and a recorded `total_steps` of 10 against a limit of 7.

    The three budgets are chosen to escalate at three different points: after a decision that does
    not read the flag (D04), between two plain edges, and immediately before D06 -- the one parent
    decision with a loop and no give-up branch of its own.

    `total_steps == budget + 1` is the floor, not slack. The guard is evaluated on entry, so the
    node that discovers the exhausted budget has already been entered and counted; stopping any
    earlier would mean deciding not to enter a node before checking whether it could.
    """
    final, ctx = await _run(
        _service(fixtures, "hfc_degraded_upstream"), step_budget_override=budget
    )

    assert final["escalated"] is True
    assert final["status"] is IncidentStatus.ESCALATED
    assert str(budget) in final["escalation_reason"]

    order = [name for name, _ in PARENT_NODES]
    entered = set(final["node_visits"])
    assert entered == set(order[: order.index(escalating_node) + 1])
    assert total_steps(final) == budget + 1

    # The body of the escalating node never ran, so it cannot have written anything either.
    assert list(ctx.adapters.gate.recorded) == []


async def test_an_escalation_is_recorded_before_the_edge_acts_on_it() -> None:
    """The graph stops on a fact in state, never on a router's private conclusion.

    `routing.py`'s rule -- a node decides and records, a router reads the record -- applies to the
    builder's edge too. If the escalation reached `END` without an `AuditEvent` behind it, an
    operator would find an incident that stopped for no stated reason, which is the failure mode
    conditional edges cannot report on because their return value is never checkpointed.
    """
    from lpr_cpe.simulation.loader import load_fixtures

    service = next(
        s for s in load_fixtures().services.values() if s["health"] == "hfc_degraded_upstream"
    )
    final, _ = await _run(service, step_budget_override=5)

    escalations = [e for e in final["audit_events"] if e.outcome == "escalated"]
    assert len(escalations) == 1, (
        "exactly one escalation, recorded once even though ids are derived"
    )
    assert escalations[0].node == "create_or_attach_incident"
    assert escalations[0].detail["limit"] == 5
    assert escalations[0].detail["owner"] == "GraphContext.step_budget_override"


#: The one service that reaches the resolution fork with both loops live. Named by ref rather than
#: found by health label, because its label is `hfc_healthy` -- healthy plant behind a Wi-Fi
#: complaint -- and fourteen other services share it.
BOTH_LOOPS_SERVICE = "SVC-SJ-011-B-01"


@pytest.mark.parametrize(
    ("field", "kind", "escalating_node", "steps", "diagnostic", "resolution"),
    [
        ("max_diagnostic_cycles", "diagnostic_cycles", "create_diagnostic_test_plan", 16, 2, 1),
        ("max_resolution_cycles", "resolution_cycles", "select_remote_action", 20, 2, 2),
    ],
)
async def test_each_cycle_budget_stops_the_same_run_at_its_own_point(
    fixtures: Any,
    field: str,
    kind: str,
    escalating_node: str,
    steps: int,
    diagnostic: int,
    resolution: int,
) -> None:
    """Two counters, one service, two different stopping points -- which is what makes them two.

    `guards.py` resolves two owners of *one* quantity into a single check and refuses to carry them
    as two, so a pair of bounds that always fired together would be a pair that should have been
    collapsed. This is the measurement that says they do not: the same fixture, run twice on the
    same graph with one budget lowered each time, escalates at a different node after a different
    number of steps and with a different counter spent. Set to 2 and not to 1 because 1 stops the
    run before the loops it is supposed to bound have started.

    Both are asserted on the same parametrisation so that neither can be quietly weakened alone,
    and every number below is read off a real run rather than computed here. The pairs are the
    point: `diagnostic_cycles` stops at P08 with the resolution counter still at 1, having never
    reached the fork; `resolution_cycles` carries four steps further, into the remote subgraph,
    with the *same* two diagnostic cycles spent. A single counter cannot produce both rows.

    Shown red by collapsing them back into one -- pointing `check_budgets`'s resolution arm at
    `diagnostic_cycles`, which is the state the split undid. The diagnostic row stays green and
    only the resolution one moves, which is the whole claim::

        AssertionError: assert ['create_diag...ic_test_plan'] == ['select_remote_action']
          At index 0 diff: 'create_diagnostic_test_plan' != 'select_remote_action'
    """
    final, _ = await _run(fixtures.services[BOTH_LOOPS_SERVICE], settings=Settings(**{field: 2}))

    assert final["escalated"] is True
    assert final["status"] is IncidentStatus.ESCALATED
    assert final["escalation_reason"] == (
        f"{kind} budget exhausted: observed 2, limit 2 (from settings.{field})"
    )

    escalations = [e for e in final["audit_events"] if e.outcome == "escalated"]
    assert [e.node for e in escalations] == [escalating_node]
    assert escalations[0].detail["budget"] == kind
    assert escalations[0].detail["owner"] == f"settings.{field}"

    assert total_steps(final) == steps
    assert final["diagnostic_cycles"] == diagnostic
    assert final["resolution_cycles"] == resolution


# ------------------------------------------------------------------------------------------------
# The checks that keep the three tables in step
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("what", "attribute", "mutation"),
    [
        (
            "an answer with no destination",
            "BRANCH_TARGETS",
            {"D01": {"continue": "resolve_identity_and_topology"}},
        ),
        (
            "a destination for an answer no router gives",
            "BRANCH_TARGETS",
            {"D03": {"associate": "assess_impact_and_priority", "continue": "x", "defer": END}},
        ),
        (
            "a destination that is not a registered node",
            "BRANCH_TARGETS",
            {"D04": {"preventive": END, "active": "preventive_subgraph"}},
        ),
        (
            "a decision wired after a node that does not exist",
            "DECISION_AFTER",
            {"triage_the_thing": "D01"},
        ),
    ],
)
def test_the_builder_refuses_a_topology_that_disagrees_with_routing(
    monkeypatch: pytest.MonkeyPatch, what: str, attribute: str, mutation: dict[str, Any]
) -> None:
    """`routing.py` delegates this check here by name; these are the ways it can fail.

    From that module's docstring: "`graph.builder` owns the answer-to-node mapping and asserts each
    `path_map`'s keys against `Decision.branches` below, so the two cannot drift." Drift has two
    directions and both are errors -- an answer with no destination is a branch the router can
    return and the graph cannot follow, and a destination for an answer no router gives is an edge
    nothing can traverse. LangGraph reports neither: the first raises at runtime on the one incident
    unlucky enough to take the branch, and the second never raises at all.
    """
    monkeypatch.setattr(
        builder_module, attribute, {**getattr(builder_module, attribute), **mutation}
    )
    with pytest.raises(GraphTopologyError):
        build_parent_graph()


def test_a_chain_that_loops_is_refused_rather_than_left_to_spin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure a decision chain can have that a plain table cannot.

    `_cascade` follows answers until one names a node, so D09 answering back to D07 is not a wrong
    edge or a missing destination -- it is an edge function that never returns, inside a super-step,
    with no checkpoint written and nothing in the log. LangGraph cannot report it because the branch
    never hands control back for LangGraph to have an opinion about.

    Removing the `_check_chains()` line was measured, and the result is worse than a build that
    hangs. **The build succeeds.** `chain_from` de-duplicates, so the build-time walk of a looping
    table terminates perfectly happily -- it returned `('D07', 'D08', 'D09', 'D11')` and a five-entry
    path map, and `build_parent_graph()` returned a graph. Only `_cascade` loops, and only when an
    incident reaches P11: run on a daemon thread it was still running after two seconds, with
    nothing returned. So the defect this check exists for is invisible to every other check in this
    module and to compilation, and surfaces as one incident that never finishes.

    The mutation below points D09 back at the head. `_check_chains` runs before the orphan check for
    that reason: without it, the first thing to notice would be that D11 has become unreachable --
    the loop stole its only inbound answer -- and the build would fail with a message about an
    orphan, which is true and is not the problem.
    """
    monkeypatch.setattr(
        builder_module,
        "BRANCH_TARGETS",
        {**BRANCH_TARGETS, "D09": {"remote": "remote_resolution", "self_help_check": "D07"}},
    )
    with pytest.raises(GraphTopologyError, match="loops"):
        build_parent_graph()


def test_two_decisions_in_one_chain_may_not_give_the_same_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One edge carries the whole chain, so one answer cannot mean two things on it.

    D10 and D12 both answer `retry_diagnosis`, to different nodes, and both are correct: they are
    asked after different nodes, so they are different edges. Two decisions *in one chain* are one
    edge, and `path_map[answer]` would silently keep whichever was written last -- the graph would
    build, compile, run, and route D08's `plant_path` wherever D07 had sent its own.

    Spelled here as D08 answering `escalate` because that is the plausible version: the answer is
    already in the chain's vocabulary, one question earlier, so it reads as a reasonable thing to
    write. The check is what makes it a build error instead of a branch going somewhere D07 chose.

    It routes to **P07 rather than to `END`**, and that too is load-bearing. Sent to `END` the
    mutation is caught downstream by `_check_pending_stages` instead::

        E   GraphTopologyError: these exits reach END with nothing to explain them:
            ['D08:escalate'].

    which is a true complaint about a different defect, and would let this test pass with the
    collision refusal deleted. A duplicate answer pointing at a *node* is the case no other check
    in this module can see, and deleting the refusal then gives the honest red::

        E   Failed: DID NOT RAISE <class 'lpr_cpe.graph.builder.GraphTopologyError'>

    **Both tables have to be mutated, and the first attempt at this test proved it.** Wiring the
    answer in `BRANCH_TARGETS` alone never reaches the collision check::

        E   AssertionError: Regex pattern did not match.
        E     Expected regex: 'both answer'
        E     Actual message: "D08 wires ['continue', 'escalate', 'plant_path'] but
              routing.DECISIONS declares ['continue', 'plant_path']."

    -- the `Decision.branches` comparison is a stronger gate and catches it first. So the collision
    is only reachable when both decisions *genuinely declare* the answer, which is not a contrivance:
    `routing.py` has no rule against two decisions sharing an answer word, and D02 and D05 both
    declare `manual_review` while D10 and D12 both declare `retry_diagnosis`. What is new is those
    two being in one chain.
    """
    from lpr_cpe.graph.routing import DECISIONS

    monkeypatch.setattr(
        builder_module,
        "DECISIONS",
        {
            **DECISIONS,
            "D08": replace(DECISIONS["D08"], branches=("plant_path", "continue", "escalate")),
        },
    )
    monkeypatch.setattr(
        builder_module,
        "BRANCH_TARGETS",
        {
            **BRANCH_TARGETS,
            "D08": {
                "plant_path": END,
                "continue": "D09",
                "escalate": "assemble_case_evidence",
            },
        },
    )
    with pytest.raises(GraphTopologyError, match="both answer"):
        build_parent_graph()


def test_a_chain_hanging_off_nothing_is_an_orphan_however_long_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DECISION_AFTER` is no longer the list of wired decisions, so "orphan" had to be redefined.

    D08 follows no node and is not an orphan -- D07 answers to it. The check therefore walks
    `chain_from` from each head rather than reading `DECISION_AFTER.values()`, and this is the case
    that separates the two: D13 and D14 chained to each other and to nothing else would pass a check
    that only asked "is it a value of some other decision's table?", because each is.

    The answers below are D13's and D14's **real** ones, and that is load-bearing rather than
    tidiness. The first version invented `dispatch` and `defer`, which made the test pass under a
    deliberately naive orphan check::

        E   GraphTopologyError: D13 wires ['defer', 'dispatch'] but routing.DECISIONS declares
            ['clean', 'dirty', 'escalate', 'joint'].

    -- caught by the `Decision.branches` comparison two checks later, not by the orphan check at
    all. A test that would pass with the check deleted is not a guard for it. With the real answers
    the only thing wrong with this pair is that nothing asks them, which is the whole point.

    They route to P07 for the same reason and it took a second attempt to see it. Routing them to
    `END` let `_check_pending_stages` catch the mutation instead::

        E   GraphTopologyError: these exits reach END with nothing to explain them:
            ['D13:clean', 'D13:dirty', 'D13:escalate', 'D14:queue_for_dispatcher'].

    Pointing every answer at a node leaves nothing else to object to, and the naive check then gives
    `DID NOT RAISE`, which is the honest red.
    """
    unreachable = "assemble_case_evidence"
    monkeypatch.setattr(
        builder_module,
        "BRANCH_TARGETS",
        {
            **BRANCH_TARGETS,
            "D13": {
                "clean": unreachable,
                "dirty": unreachable,
                "joint": "D14",
                "escalate": unreachable,
            },
            "D14": {"queue_for_dispatcher": unreachable, "continue": "D13"},
        },
    )
    with pytest.raises(GraphTopologyError, match="decisions nothing asks"):
        build_parent_graph()


def test_wiring_a_pending_stage_forces_its_line_to_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direction of the `PENDING_STAGES` check that nothing else in the codebase would notice.

    Forgetting to *add* an entry is caught the moment a new answer is routed to `END`. Forgetting to
    *remove* one survives indefinitely: the graph is correct, the tests pass, and the documentation
    goes on claiming a stage is missing that has been built. So the check runs both ways, and this
    is the way that matters.

    The mutation used to be `D06:approve_low_confidence`, and wiring D06's gate is what proved the
    check works for real rather than only under `monkeypatch`: repointing that answer at
    `prepare_low_confidence_review` and leaving the line in `PENDING_STAGES` failed the build with
    exactly the message this test asserts. Which then cost this test its subject -- the entry is
    gone, so the mutation raises nothing::

        E   Failed: DID NOT RAISE <class 'lpr_cpe.graph.builder.GraphTopologyError'>

    `D08:plant_path` took its place and has now been spent the same way, which is the point rather
    than a nuisance: the check is a property of the table, not of whichever gap happened to be open
    when it was written.

    The one entry left is node-shaped, so the mutation had to change shape with it -- and that is a
    gain and not a compromise. `_check_pending_stages` derives its gaps twice, once from the
    `BRANCH_TARGETS` answers that end at `END` and once from the terminal nodes `_plain_edges` finds
    no successor for, and both previous mutations were `Dnn:answer`, so only the first set was ever
    exercised. Giving `preventive_maintenance` a `SUBGRAPH_SUCCESSOR` entry takes it out of the
    second set, which puts the half of the check that had never been mutated under test.
    """
    monkeypatch.setattr(
        builder_module,
        "SUBGRAPH_SUCCESSOR",
        {**SUBGRAPH_SUCCESSOR, "preventive_maintenance": "assemble_case_evidence"},
    )
    with pytest.raises(GraphTopologyError, match="no longer reach END"):
        build_parent_graph()


def test_a_new_dead_end_has_to_say_why_it_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `END` that nobody explained is a run that looks finished when it was abandoned."""
    monkeypatch.setattr(
        builder_module,
        "BRANCH_TARGETS",
        {**BRANCH_TARGETS, "D03": {"associate": END, "continue": "assess_impact_and_priority"}},
    )
    with pytest.raises(GraphTopologyError, match="nothing to explain them"):
        build_parent_graph()


def test_a_node_that_ends_the_workflow_has_to_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node-shaped half of the same rule, and the only entry in the table that carries it.

    `record_escalation` is terminal on purpose: the incident is a human's, and
    `IncidentStatus.ESCALATED` moves onward to nine other statuses so a supervisor resumes the
    thread. `_plain_edges` cannot tell that apart from P11's old fall off the end, which was a
    missing stage -- both are a node with no successor. `_DELIBERATE_TERMINALS` is the difference,
    and emptying it shows the check reaches the node half at all::

        E   GraphTopologyError: these exits reach END with nothing to explain them:
            ['__onward__:record_escalation'].

    Before D07's escalation arm was wired there was nothing in this table, so the branch that reads
    it ran on every build and excused nothing. That is worth a test rather than a comment: an empty
    frozenset and a table nobody consults look identical from the outside.
    """
    monkeypatch.setattr(builder_module, "_DELIBERATE_TERMINALS", frozenset())
    with pytest.raises(GraphTopologyError, match=r"__onward__:record_escalation"):
        build_parent_graph()


def test_the_unbuilt_exits_are_the_ones_named() -> None:
    """What is left to build, written down where the builder will not let it go stale.

    One, and the count has moved in both directions on the way there, which is the shape to expect.
    This opening line read "Four" while the assertion below held two, and that is worth admitting
    rather than quietly correcting: the builder keeps the *table* from going stale and nothing keeps
    this sentence from it, so the number here is read off the assertion at the bottom and not
    carried forward.

    Wiring the resolution fork made it *longer*: a stage deletes one line --
    `ONWARD:generate_resolution_options`, P11's old fall off the end -- and adds one for every branch
    it opens that leads somewhere still unwritten, and Stage 3 asks four questions and answers seven
    of its branches to `END`.

    Wiring the preventive stage moved it neither way: one entry changed *kind* rather than going
    away. `D04:preventive` was a decision answer that fell to `END`; it now reaches a subgraph, and
    that subgraph is what falls to `END`, so the gap is spelled `__onward__:preventive_maintenance`.
    A stage can be wired and still owe something.

    Wiring field planning is the first edit that made the list *shorter*, and it did both things at
    once. Two decision answers went away -- `D11:field_planning` and `D12:field_planning`, which
    were the same missing stage named twice because two decisions reached it -- and one
    `__onward__` entry replaced them, because P14/P15/P16 ran and then stopped at Stage 4. Two
    exits collapsing into one is the whole benefit of a subgraph being a node: the gap became a
    property of the stage rather than of every branch that points at it.

    `__onward__:preventive_maintenance` survived that edit but no longer says the same thing. It
    used to mean "P14 does not exist"; P14 exists, and what is missing is narrower and worth having
    named -- the preventive case reaches a disposition with no `resolution_plan` and no
    `ResolutionOption`, which is what `build_field_requirement` selects from, so the two stages
    cannot be joined by an edge alone.

    Wiring field execution moved it neither way, and for a third reason again. This entry did not
    change kind and it did not collapse anything: it changed *stage*.
    `__onward__:field_planning` went away because planning now has a successor, and
    `__onward__:field_execution` took its place because execution does not. The list is a frontier,
    and a frontier that advances one stage keeps its length.

    Its three exits stop for three different reasons, which is why the entry is one line and not
    three: `file_plant_mr` waits on the OSP status feed that keeps P21 out of the build,
    `close_clean_boots_visit` writes `validating` and waits on D20 -- the decision that would route
    a restored plant case into the stage that reads it, which is inside this same unwritten half --
    and `abandon_handover` waits on no stage at all: it writes `diagnosing`, whose destinations P07
    and P10 both exist, and what is missing is an edge the parent cannot draw while the
    specification defines no decision after the Clean Boots arm.

    Wiring D06's and D07's gates is the largest single shrink so far, and the only one that closed
    exits without adding a stage. Three lines went away at once -- `D06:approve_low_confidence`,
    `D07:approve_high_blast_radius` and `D07:escalate` -- because all three were answers falling to
    `END` for want of a node rather than for want of a subgraph, and five plain nodes in
    `graph.nodes.governance` is the whole of what they were waiting for. Nothing replaced them:
    the two gates re-enter the decisions they came from, and `record_escalation` is terminal on
    purpose, which is a different thing from terminal for want of a successor and is spelled out in
    `_DELIBERATE_TERMINALS` rather than here. An exit can be closed by a node; a stage cannot.

    Wiring Stage 5's first half is the second edit to make the list shorter, and it is the field
    planning collapse again: `D10:verify` and `D12:verify` were one missing stage named twice
    because two decisions reached it, and both now point at `restoration_validation`. What replaced
    them is one line, not two, and is not an `__onward__` -- the stage runs, D21 and D22 are asked,
    and it is D22's `reconcile` arm alone that falls to `END`. Two entries out and one in.

    That is a *net* shrink of one and hides a second collapse worth naming, because two of the three
    things the old pair waited on have now been paid off by the same edit. `D10:verify` and
    `D12:verify` stopped being pending, and so did the sentence above about `close_clean_boots_visit`
    -- it writes `validating` and the stage that reads `validating` exists now. What still holds
    that exit open is D20, one decision inside the field-execution half, not a stage. A frontier
    entry that survives an edit is worth re-reading rather than re-counting.

    Wiring Stage 5's second half took `D22:reconcile` off the list and put **nothing** in its place,
    which no earlier edit managed. Every previous stage that closed an exit opened another, because
    a new subgraph is terminal until whatever follows it is written, and a terminal subgraph owes an
    `__onward__` line. `reconciliation_closure` owes none: its main line ends at P26, which writes
    `IncidentStatus.CLOSED`, and `domain.lifecycle` gives `closed` no outward transition. So it is
    declared in `_DELIBERATE_TERMINALS` beside `record_escalation` -- the two ways this workflow can
    legitimately stop -- rather than confessed here. That is the difference between a frontier that
    advances and a frontier that closes, and it is the first time this list has recorded the second.

    Wiring the plant branch is the second edit to add a stage and put nothing in its place, and the
    first where the stage that was added is not itself terminal. `plant_execution` is followed by
    D19, whose three answers reach `plant_execution`, `determine_root_cause` and D20, and D20's two
    reach `field_planning` and `restoration_validation` -- five destinations that all already
    existed. So the frontier did not advance one stage this time; it closed.

    Two of the three sentences at the top of this docstring were also wrong, and this is where they
    are paid off rather than edited away. `file_plant_mr` was said to wait on the OSP status feed:
    it waits on nothing, because P21 takes the crew's report through `interrupt()` and the feed is a
    second channel into the same parser. `close_clean_boots_visit` was said to wait on D20: it
    answers D16 `validate` and goes to restoration validation, which is what D16's own specification
    text says, and D20 is nowhere on that path. Only the third sentence held -- `abandon_handover`
    really was waiting on an edge rather than a stage, and D16 re-read on the parent's edge is that
    edge.

    `D08:plant_path` survived that edit and, like `__onward__:preventive_maintenance` before it, no
    longer said what it used to. It meant "the plant branch is unwritten"; the branch was written,
    and what was left was one filing node -- a D08-direct case has no `HandoverContract` for
    `file_plant_mr` to read `REQUIRED_MR_FIELDS` from, which the specification itself says is correct
    for that case.

    Writing `plant_referral` closed it, making this the third edit to add a stage and put nothing in
    its place, and the first to close an exit by *deleting* what it was waiting on rather than by
    building it. The contract was never the field source it looked like: `graph.subgraphs._mr`
    derives all four of `REQUIRED_MR_FIELDS` from the state instead, so a case with no contract has
    the same four to send as a case with one. `access_notes` was the only one with nowhere else to
    come from -- absent on all ten fixtures that reach this arm -- and it is composed from what
    `topology` resolved. `field_execution` was migrated onto the same helper, so the derivation has
    one owner and not two.

    What is left is one entry, and it is the seam kind: a stage that exists on each side and no edge
    that may join them, because the receiving stage reads a model the sending one never builds.

    `_check_pending_stages` is what makes this shrink rather than rot: the entries here are checked
    against the tables in both directions, so an exit that stops reaching `END` fails the build. Both
    directions were seen red on this edit rather than reasoned about. Leaving the line in place with
    the stage wired::

        GraphTopologyError: PENDING_STAGES still lists exits that no longer reach END:
          ['D08:plant_path']. The stage was wired; delete its line.

    and deleting the line while `plant_path` still fell to `END`::

        GraphTopologyError: these exits reach END with nothing to explain them: ['D08:plant_path'].
          A run that stops there looks like a run that finished. [...]

    The first is the direction nothing else in the codebase would have caught.
    """
    assert set(PENDING_STAGES) == {f"{ONWARD}:preventive_maintenance"}
    assert all(text.strip() for text in PENDING_STAGES.values()), "each has to say what is missing"


def test_the_guards_branch_cannot_be_confused_with_an_answer() -> None:
    """`__escalated__` is the builder's, and no router in `routing.py` can return it.

    Spelled in LangGraph's own sentinel style for the same reason `__start__` is: a branch name
    that could collide with a specification answer would make `_check_tables`' subtraction hide a
    real mismatch instead of reporting it.
    """
    from lpr_cpe.graph.routing import DECISIONS

    every_answer = {answer for decision in DECISIONS.values() for answer in decision.branches}
    assert ESCALATED not in every_answer
    assert ONWARD not in every_answer
    assert ESCALATED.startswith("__") and ESCALATED.endswith("__")

    # And every decision the parent wires gets it, so no node can escalate into a branch that
    # carries on regardless.
    graph = build_parent_graph()
    for source in DECISION_AFTER:
        branch = next(iter(graph.branches[source].values()))
        assert (branch.ends or {})[ESCALATED] == END
