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
from typing import Any

import pytest
from langgraph.graph import END

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
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import (
    BRANCH_TARGETS,
    DECISION_AFTER,
    ESCALATED,
    ONWARD,
    PENDING_STAGES,
    SUBGRAPH_NODES,
    GraphTopologyError,
    build_parent_graph,
    compile_parent_graph,
)
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.nodes import PARENT_NODES
from lpr_cpe.graph.nodes._runtime import derive_id
from lpr_cpe.graph.state import make_initial_state, total_steps

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

HEALTHS = ("hfc_degraded_upstream", "pon_degraded_optical", "pon_power_affected", "hfc_healthy")


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
        # P05 -> D04.
        "assess_impact_and_priority": {
            "preventive": END,
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
            "approve_low_confidence": END,
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
        "generate_resolution_options": {
            "approve_high_blast_radius": END,  # D07
            "escalate": END,  # D07
            "plant_path": END,  # D08
            "remote": "remote_resolution",  # D09
            "self_help": "self_help",  # D11
            "field_planning": END,  # D11
            ESCALATED: END,
        },
        # The two subgraphs. D10 and D12 are asked *here* and not inside them because every
        # destination either answer has is a sibling the subgraph does not contain -- a subgraph
        # cannot route to P07, and `retry_diagnosis` is most of the point of both.
        "remote_resolution": {
            "verify": END,
            "retry_diagnosis": "assemble_case_evidence",
            ESCALATED: END,
        },
        # D12's `retry_diagnosis` goes to P10 and D10's to P07, which is not a copy-paste slip:
        # self-help changes nothing the diagnostic reads unless it worked, so the same evidence
        # supports a second opinion, while a remote repair that did not hold means the device has
        # changed since the evidence was gathered.
        "self_help": {
            "verify": END,
            "retry_diagnosis": "determine_root_cause",
            "field_planning": END,
            ESCALATED: END,
        },
    }

    # The only unconditional edge in the graph, and the reason `receive_signal` is P01.
    assert sorted(graph.edges) == [("__start__", "receive_signal")]


def test_the_compiled_graph_contains_the_eleven_steps_the_two_subgraphs_and_nothing_else() -> None:
    """A fourteenth node would be reachable from nowhere; a missing one, an edge to nothing.

    The subgraphs are asserted from `SUBGRAPH_NODES` rather than by name, so that this says "the
    table was wired" and not "these two happen to be here". A compiled subgraph is added exactly as
    a function is, and `get_graph()` reports it as one node -- its own eight are one level down,
    which is what `xray` renders and what nothing here needs.
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

    Since the resolution fork was wired, the exact `node_visits` carries a third claim it did not
    ask for: **no fixture enters a subgraph**, so the eleven are still eleven. That is the routers
    answering honestly rather than the fork being broken, and it was measured. Two services divert
    at D08 (`pon_degraded_optical` and `pon_power_affected`, both `plant_path`) and two run the
    whole chain to D11 and answer `field_planning` -- `hfc_degraded_upstream` and `hfc_healthy`,
    both diagnosed `tap_or_odp`, whose only options are `raise_mr` and `create_work_order` and
    both of those set `requires_truck_roll`. `is_remote_option` and `is_self_help_option` reject a
    truck roll, so there is nothing for D09 or D11 to choose.

    Only these four, and `_service` takes the *first* service of each health profile -- the fork is
    not dead across the fixture set. Swept over all 41 services, two do enter `remote_resolution`
    (`SVC-UT-001-B-01` and `SVC-SJ-011-B-01`) and none reach `self_help`, for a reason the self-help
    module's docstring records: the budget, not the wiring. So a service that changed profile could
    move into the fork here, and this test should go red when it does -- the number to update is
    then the twelfth and thirteenth visit, not the assertion.

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

    assert final["node_visits"] == {name: 1 for name, _ in PARENT_NODES}
    assert total_steps(final) == 11
    assert final["escalated"] is False
    assert final["status"] is IncidentStatus.DIAGNOSING
    assert final["resolution_plan"] is not None
    # A tuple, so `== []` would fail on the type alone and read as a real defect.
    assert list(ctx.adapters.gate.recorded) == []


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
    """
    monkeypatch.setattr(
        builder_module,
        "BRANCH_TARGETS",
        {
            **BRANCH_TARGETS,
            "D06": {**BRANCH_TARGETS["D06"], "approve_low_confidence": "determine_root_cause"},
        },
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


def test_the_unbuilt_exits_are_the_ones_named() -> None:
    """What is left to build, written down where the builder will not let it go stale.

    Nine, and the list got *longer* when the resolution fork was wired, which is the shape to
    expect. Wiring a stage deletes one line -- `ONWARD:generate_resolution_options`, P11's old fall
    off the end -- and adds one for every branch the new stage opens that leads somewhere still
    unwritten. Stage 3 asks four questions and answers seven of its branches to `END`.

    `_check_pending_stages` is what makes this shrink rather than rot: the entries here are checked
    against the tables in both directions, so an exit that stops reaching `END` fails the build.
    """
    assert set(PENDING_STAGES) == {
        "D04:preventive",
        "D06:approve_low_confidence",
        "D07:approve_high_blast_radius",
        "D07:escalate",
        "D08:plant_path",
        "D10:verify",
        "D11:field_planning",
        "D12:verify",
        "D12:field_planning",
    }
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
