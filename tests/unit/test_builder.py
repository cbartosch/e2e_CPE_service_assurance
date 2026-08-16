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

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END

import lpr_cpe.graph.builder as builder_module
from lpr_cpe.config.clock import FrozenClock
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


async def _run(service: dict[str, Any], **ctx_kwargs: Any) -> tuple[dict[str, Any], Any]:
    ctx = build_context(clock=_Ticking(NOW), **ctx_kwargs)  # type: ignore[arg-type]
    final = await compile_parent_graph().ainvoke(_initial(service), context=ctx)
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
        # P11 -> Stage 3, which does not exist yet. See PENDING_STAGES.
        "generate_resolution_options": {ONWARD: END, ESCALATED: END},
    }

    # The only unconditional edge in the graph, and the reason `receive_signal` is P01.
    assert sorted(graph.edges) == [("__start__", "receive_signal")]


def test_the_compiled_graph_contains_the_eleven_nodes_and_nothing_else() -> None:
    """A twelfth node would be reachable from nowhere; a missing one, an edge to nothing."""
    nodes = set(compile_parent_graph().get_graph().nodes)
    assert nodes == {name for name, _ in PARENT_NODES} | {"__start__", "__end__"}


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


def test_the_three_unbuilt_exits_are_the_ones_named() -> None:
    """What is left to build, written down where the builder will not let it go stale.

    Each of these is a subgraph in `PARENT_NODES`' terms -- Stage 3 onwards is not parent-graph
    work, because every stage past P11 contains an interrupt. The list shrinks as they land, and
    `_check_pending_stages` is what makes it shrink rather than rot.
    """
    assert set(PENDING_STAGES) == {
        "D04:preventive",
        "D06:approve_low_confidence",
        f"{ONWARD}:generate_resolution_options",
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
