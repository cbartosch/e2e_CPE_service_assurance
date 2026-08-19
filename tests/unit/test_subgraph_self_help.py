"""Stage 3's self-help branch, compiled and run against the one customer who can be asked anything.

The fixture that makes this module possible is `SVC-SJ-011-B-01`, and it is the only one: a sweep of
all 41 services through the real parent graph found exactly one that reaches D11 with a self-help
option attached -- healthy HFC plant, a gateway that is up, and a Wi-Fi complaint that localises to
`FaultDomain.customer_environment` at confidence 0.950, whose catalogue offers `move_device_closer`.
Every other service either localises somewhere D08 diverts or offers only options that need no
customer. That sweep is slow and is not repeated here; `test_the_self_help_fixture_still_offers_a
_script` asserts the properties it depends on so a drift names the fixture instead of surfacing as
six confusing failures about a missing session.

Wiring the resolution fork changed what "reaches D11" is worth, and the sweep was re-run against the
wired parent: `SVC-SJ-011-B-01` is still the only service that reaches this subgraph. It answers
D09 `remote` first, because both Wi-Fi settings options act without the customer and
`first_actionable_option` offers them ahead of `send_self_help`, so two diagnostic cycles are spent
before the script is even proposed. At the `max_diagnostic_cycles` of 3 that shipped with the fork
the guard refused the third pass and this branch was unreachable end to end; the setting is now 6,
derived in `config.settings` from the largest plan the fixture set produces rather than from this
one service. `test_the_shipped_cycle_budget_admits_the_self_help_branch` is what holds it there.

Reachable is not the same as convenient, and this module still drives the subgraph from P11. Going
through the parent costs three diagnostic cycles and two spent remote actions before the first
self-help node runs, and every test below would inherit that state -- so the branch would be
exercised only in the one condition the parent happens to deliver it in. See `_parent_to_p11`.

The same fixture is also the reason `resolved` is unreachable end to end, and that is the honest
shape of this branch today rather than a gap in the tests. No adapter models the physical effect of
a customer completing a step: the CPE simulator recovers a device for the actions *it* applies, and
a hand power-cycle is not one of them. The gateway is online before and after, so
`reachability_verdict` returns `None` -- "cannot be told from here" -- however the customer answers.
The verdict is therefore covered directly, with supplied readings, and `verify_self_help`'s
`resolved` arm is driven at node level. It is recorded in `docs/vendor-integration-gaps.md`.

What is deliberately not asserted here
--------------------------------------
`route_self_help_outcome` (D12) is not exercised. Both of its destinations are outside this graph,
so it belongs on the parent's edge out of the subgraph node and is tested in `test_routing.py`
against constructed state -- asserting it here would assert it in the one place it is not wired.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config import get_settings
from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    CaseType,
    CommunicationChannel,
    EventSource,
    FaultDomain,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.domain.resolution import SelfHelpSession
from lpr_cpe.graph.builder import build_parent_graph, compile_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, BudgetKind, check_budgets
from lpr_cpe.graph.nodes._runtime import derive_id
from lpr_cpe.graph.routing import (
    DEDICATED_GATE_APPROVAL_KINDS,
    first_actionable_option,
    is_self_help_option,
)
from lpr_cpe.graph.state import make_initial_state, total_steps
from lpr_cpe.graph.subgraphs._shared import reachability_verdict
from lpr_cpe.graph.subgraphs.self_help import (
    ANSWER_TARGETS,
    GATE_TARGETS,
    SELF_HELP_NODES,
    build_self_help_graph,
    customer_reply,
    route_customer_answer,
    route_self_help_gate,
    script_id_of,
    verify_self_help,
)
from lpr_cpe.policies.engine import PolicyEngine, PolicyInput
from lpr_cpe.policies.loader import load_pack

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: 00:05 in `America/Puerto_Rico`, which is UTC-04:00 and has no DST. Inside the pack's quiet hours
#: (21:00-07:00) by a wide margin, and chosen at 04:00 UTC rather than, say, 02:00 so the reading is
#: unambiguous to someone checking the arithmetic by hand.
QUIET_HOURS_NOW = datetime(2026, 3, 3, 4, 0, tzinfo=UTC)

#: Healthy HFC plant, an online gateway, a Wi-Fi complaint. The only service of 41 that gets here.
SELF_HELP_SERVICE = "SVC-SJ-011-B-01"

#: `CommunicationsSimulator.fetch_customer_responses` seeds its roll on `f"{incident_id}:{script_id}"`
#: and the script is always `move_device_closer` here, so the incident id alone picks the customer's
#: answer. These three were found by sweeping ids until each of the three real outcomes appeared;
#: `roll < 0.15` is silence, `roll >= 0.35` is compliance, and between them is a decline.
#:
#: Driving the outcomes this way rather than by injecting a reply keeps the adapter in the loop --
#: a change to the simulator's reply shape breaks these tests, which is the point of them.
COMPLIED = "INC-SJ-011-B-01-001"  # roll 0.8457
SILENT = "INC-SJ-011-B-01-002"  # roll 0.1174, no row at all
DECLINED = "INC-SJ-011-B-01-017"  # roll 0.3336


class _Ticking(FrozenClock):
    """The same advance-on-read clock the remote branch's module uses, and for the same reason: a
    verification read carrying the action's own timestamp would make `evidence_age_minutes` zero by
    construction.

    Three seconds and not more. A twenty-minute tick was tried first and quietly broke the timeout
    test in two ways at once -- it walked local time into quiet hours and staled the telemetry, so
    the send was refused for `POLICY_QUIET_HOURS` and `POLICY_EVIDENCE_INSUFFICIENT` before the wait
    it was meant to exercise had begun. Time is moved deliberately, with `advance`, where a test
    needs it moved.
    """

    def now(self) -> datetime:
        return self.advance(timedelta(seconds=3))


def _initial(service: dict[str, Any], incident_id: str, now: datetime = NOW) -> Any:
    return make_initial_state(
        incident_id=incident_id,
        correlation_id=f"COR-{service['service_ref']}",
        event=AssuranceEvent(
            event_id=f"EVT-{service['service_ref']}",
            source=EventSource.NXT,
            case_type=CaseType.PROACTIVE_ALARM,
            technology=Technology(service["technology"]),
            severity=Severity.HIGH,
            occurred_at=now - timedelta(minutes=6),
            received_at=now - timedelta(minutes=5),
            customer_ref=service["customer_ref"],
            service_ref=service["service_ref"],
            cpe_ref=service["cpe_ref"],
            summary=f"degraded wifi reported at {service['cpe_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=now - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=now,
    )


async def _parent_to_p11(initial: Any, ctx: Any, thread: str) -> Any:
    """The parent, run for real and stopped the moment `generate_resolution_options` has run.

    Stopping has to be asked for now, and did not before the resolution fork was wired. While D07's
    answers all led to `END`, a plain `ainvoke` came back at P11 on its own and "parent to P11" cost
    nothing to say. Wired, this service answers D07 `continue`, D08 `continue`, D09 `remote`, and the
    parent goes on to spend both remote options, retry diagnosis after each, and escalate with
    `diagnostic_cycles budget exhausted: observed 3, limit 3`. Handing *that* state to the subgraph
    made `select_self_help_script`'s guard return `ESCALATED` on the first step, so the branch ran to
    `END` without pausing and thirteen tests here failed as `assert () == ('await_customer_response',)`.

    `interrupt_after` is therefore how P11 is named, and the checkpointer is what `interrupt_after`
    requires rather than anything this module reads back.
    """
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    return await parent.ainvoke(
        initial, context=ctx, config={"configurable": {"thread_id": thread}}
    )


async def _drive(fixtures: Any, incident_id: str, now: datetime = NOW) -> Any:
    """Parent to P11, then into the subgraph until it stops. Returns `(graph, ctx, config, state)`.

    The parent is run for real rather than hand-built, for the reason the remote module gives: a
    constructed `resolution_plan` would let this module pass while `generate_resolution_options`
    offered something the communications adapter cannot send.
    """
    service = fixtures.services[SELF_HELP_SERVICE]
    ctx = build_context(clock=_Ticking(now))  # type: ignore[arg-type]
    thread = f"parent-{incident_id}-{now.isoformat()}"
    parent_final = await _parent_to_p11(_initial(service, incident_id, now), ctx, thread)

    graph = build_self_help_graph().compile(name="lpr_cpe_self_help", checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"self-help-{incident_id}-{now.isoformat()}"}}
    first = await graph.ainvoke(parent_final, context=ctx, config=config)
    return graph, ctx, config, first


@pytest.fixture
async def paused(fixtures: Any) -> Any:
    """An incident carried into the branch and stopped at the customer-response interrupt."""
    return await _drive(fixtures, COMPLIED)


# ------------------------------------------------------------------------------------------------
# The control
# ------------------------------------------------------------------------------------------------


async def test_the_self_help_fixture_still_offers_a_script(fixtures: Any) -> None:
    """The precondition every behavioural test below rests on.

    Each of them would pass vacuously -- abandoning at the gate, asserting nothing about a session --
    if this service stopped localising to `customer_environment` or its catalogue stopped offering a
    customer-performable option. Asserting it separately makes a drift name the fixture.
    """
    service = fixtures.services[SELF_HELP_SERVICE]
    assert service["health"] == "hfc_healthy", (
        "the point of this service is a *healthy* plant behind a Wi-Fi complaint; a degraded "
        "profile would localise to the access network and never ask the customer anything"
    )

    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    final = await _parent_to_p11(_initial(service, COMPLIED), ctx, f"control-{COMPLIED}")

    rca = final["rca"]
    assert rca.fault_domain is FaultDomain.CUSTOMER_ENVIRONMENT
    assert rca.confidence == pytest.approx(0.95), (
        "0.95 is above the pack's `rca.min_for_remote_action` of 0.65, which is why the end-to-end "
        "tests below reach the send rather than the approval gate. If this drops under 0.65 they "
        "will all divert and `test_a_low_confidence_rca_needs_an_approval` becomes the live path"
    )

    option = first_actionable_option(final, is_self_help_option)
    assert option is not None, "no self-help option was offered; every test below is now vacuous"
    assert option.action_type is ActionType.SEND_SELF_HELP
    assert script_id_of(option) == "move_device_closer"

    device = fixtures.cpe_for_service(SELF_HELP_SERVICE, system="test")
    assert device["online"] is True, (
        "an offline gateway here would make `reachability_verdict` able to return True and the "
        "module docstring's account of why `resolved` is unreachable would be wrong"
    )


async def test_the_shipped_cycle_budget_admits_the_self_help_branch(fixtures: Any) -> None:
    """The parent must actually be able to get here on the settings the process ships with.

    This is the only test that runs the fork end to end, and it exists because the alternative is
    invisible: a `max_diagnostic_cycles` one too low leaves the subgraph wired, compiled, checked by
    `_check_tables`, reported by `lpr-cpe topology` and reachable in exactly no run. Nothing else in
    the suite would have noticed -- every other test here enters the branch from P11, and the
    builder's tests asserts the edge exists rather than that anything traverses it.

    Read through `get_settings()` rather than parametrised over budgets, because the claim is about
    the shipped number and a test that supplied its own would prove the graph works at *some*
    setting, which was never in doubt.

    Seen to go red at the previous default of 3::

        AssertionError: the parent never entered the self_help subgraph on the shipped
        settings.max_diagnostic_cycles of 3; it spends a cycle per resolution option and this
        service is offered two remote ones first
        assert 'self_help' in {'remote_resolution'}

    Two remote options are asserted alongside, because they are *why* three was not enough and a
    catalogue change that dropped one would make the budget look more generous than it is.
    """
    entered: set[str] = set()
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    async for namespace, _update in compile_parent_graph().astream(
        _initial(fixtures.services[SELF_HELP_SERVICE], COMPLIED),
        context=ctx,
        stream_mode="updates",
        subgraphs=True,
    ):
        if namespace:
            entered.add(namespace[0].split(":")[0])

    budget = get_settings().max_diagnostic_cycles
    assert "self_help" in entered, (
        f"the parent never entered the self_help subgraph on the shipped "
        f"settings.max_diagnostic_cycles of {budget}; it spends a cycle per resolution option and "
        "this service is offered two remote ones first"
    )
    assert "remote_resolution" in entered, (
        "the remote branch is supposed to be tried and exhausted before the customer is asked to "
        "do anything; reaching self-help without it means D09 stopped preferring a remote repair"
    )


#: One lap of D12's loop, in the order the edges run it: back to P10, forward through P11, then the
#: five subgraph nodes a complying customer's pass actually costs. Measured by resuming `_drive`
#: past the interrupt -- the approval gate is not on it, because this service's RCA confidence of
#: 0.95 clears the pack's threshold and the send goes straight out.
D12_LAP = (
    "determine_root_cause",
    "generate_resolution_options",
    "select_self_help_script",
    "send_self_help_instructions",
    "mark_awaiting_customer",
    "await_customer_response",
    "verify_self_help",
)


async def test_the_resolution_bound_is_what_stops_the_self_help_loop_and_stops_it_first(
    fixtures: Any,
) -> None:
    """`resolution_cycles` must be reachable on this loop before any other bound fires, or it is
    decoration.

    `guards.py` refuses a bound that cannot fire -- "worse than no bound, because it reads like
    protection" -- and a fourth check has already been deleted from that module once for sitting
    behind a tighter one on the same counter. `resolution_cycles` was added as a *fourth* bound on
    the argument that D12's loop moves it and moves nothing else, so the argument owes exactly this:
    walk that loop and show which of the four actually stops it.

    `test_every_budget_fires_exactly_at_its_limit_and_not_one_entry_late` does not answer it. That
    one drives each counter on a synthetic state with the other three at zero, which proves the
    check is wired and proves nothing about whether a real trajectory can reach it -- the state this
    loop produces has 27 steps and three diagnostic cycles already spent before the first lap.

    So the start state is the real one: the parent run to the customer interrupt, on the shipped
    settings, exactly as the test above drives it. Only the laps are modelled, because no fixture
    traverses D12's retry edge -- this service arrives there with `exhausted` already true, so
    driving it would walk to `field_planning` rather than round the loop. `D12_LAP` is the measured
    shape and is checked against what the real run visited, so a branch that changes shape fails
    here rather than silently modelling a loop that no longer exists.

    Measured at the shipped settings, the loop stops on lap 3 with room to spare on every other
    bound: `resolution_cycles` 6 of 6, `total_steps` 48 of 60, `diagnostic_cycles` 3 of 6, and 4
    visits to the self-help node being entered against a re-entry ceiling of 6. The margin is the
    point -- it is what makes this a live bound rather than a formality, and it is what would
    disappear first if `max_graph_steps` were tightened or `max_resolution_cycles` raised.

    Seen red under `LPR_MAX_RESOLUTION_CYCLES=12`, which is the shape of the failure this test
    exists to catch -- the bound stops being the one that fires and the loop runs two steps past
    the circuit breaker instead::

        AssertionError: the self-help loop is not bounded by resolution_cycles at the shipped
        settings; total_steps stopped it first, which makes the resolution bound decoration
        assert <BudgetKind.TOTAL_STEPS: 'total_steps'> is <BudgetKind.RESOLUTION_CYCLES:
        'resolution_cycles'>
         +  where <BudgetKind.TOTAL_STEPS: 'total_steps'> = BudgetVerdict(within_budget=False,
        kind=<BudgetKind.TOTAL_STEPS: 'total_steps'>, observed=62, limit=60,
        owner='settings.max_graph_steps').kind
    """
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    reached: dict[str, int] = {}
    final: Any = None
    async for _namespace, update in compile_parent_graph().astream(
        _initial(fixtures.services[SELF_HELP_SERVICE], COMPLIED),
        context=ctx,
        stream_mode="values",
        subgraphs=True,
    ):
        if isinstance(update, dict) and "node_visits" in update:
            for name, count in update["node_visits"].items():
                reached[name] = max(reached.get(name, 0), count)
            final = update

    subgraph_names = {name for name, _ in SELF_HELP_NODES}
    on_the_lap = {name for name in reached if name in subgraph_names}
    assert on_the_lap and on_the_lap <= set(D12_LAP), (
        f"the branch visited {sorted(on_the_lap - set(D12_LAP))}, which D12_LAP does not model, so "
        "the lap below is not the lap this service actually walks"
    )

    state = dict(final)
    state["node_visits"] = dict(state["node_visits"])
    verdict = check_budgets(state, ctx, node="select_self_help_script")
    assert verdict.within_budget, "the parent escalated before the loop was even entered"

    laps = 0
    while verdict.within_budget:
        laps += 1
        assert laps <= 20, "the loop ran away, so none of the four bounds is holding it"
        for name in D12_LAP:
            state["node_visits"][name] = state["node_visits"].get(name, 0) + 1
        state["resolution_cycles"] += 1
        verdict = check_budgets(state, ctx, node="select_self_help_script")

    assert verdict.kind is BudgetKind.RESOLUTION_CYCLES, (
        f"the self-help loop is not bounded by resolution_cycles at the shipped settings; "
        f"{verdict.kind} stopped it first, which makes the resolution bound decoration"
    )
    assert verdict.owner == "settings.max_resolution_cycles"

    # And the margin. Each of these is a bound that did *not* fire, so each is a way this test
    # would stop meaning anything if it quietly closed up.
    assert total_steps(state) < ctx.max_graph_steps, "no headroom left under the step budget"
    assert state["diagnostic_cycles"] < ctx.max_diagnostic_cycles, (
        "the diagnostic bound was one lap from firing too, so this loop is no longer showing that "
        "the two counters come apart"
    )


# ------------------------------------------------------------------------------------------------
# Topology
# ------------------------------------------------------------------------------------------------


def test_the_gate_router_is_wired_on_both_edges_that_ask_the_question() -> None:
    """One router, two edges -- the same shape the remote branch has, for the same reason.

    Read back out of the `StateGraph` rather than off `GATE_TARGETS`, which would only prove the
    table equals itself.
    """
    graph = build_self_help_graph()
    expected = {**GATE_TARGETS, ESCALATED: END}

    for source in ("select_self_help_script", "request_self_help_approval"):
        branches = graph.branches[source]
        assert len(branches) == 1, f"{source} should carry exactly one conditional edge"
        assert dict(next(iter(branches.values())).ends or {}) == expected, (
            f"{source} must route on the same three answers as the other gate edge; two spellings "
            "of one question is how the second one forgets about the refusal"
        )


def test_the_answer_router_can_send_the_wait_back_to_itself() -> None:
    """`wait` is this graph's only cycle, and it must point at the node that raises the interrupt.

    A `wait` that pointed anywhere else would either drop the customer's window on the floor or
    spin: the cycle is safe *because* every pass through it yields to an external event, and the
    deadline is what ends it.
    """
    graph = build_self_help_graph()
    branches = graph.branches["await_customer_response"]
    ends = dict(next(iter(branches.values())).ends or {})
    assert ends == {**ANSWER_TARGETS, ESCALATED: END}
    assert ends["wait"] == "await_customer_response"


def test_every_node_is_guarded_or_terminal() -> None:
    """No edge in this graph may bypass the escalation flag."""
    graph = build_self_help_graph()
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
    graph = build_self_help_graph()
    assert set(graph.nodes) == {name for name, _ in SELF_HELP_NODES}


# ------------------------------------------------------------------------------------------------
# The pause
# ------------------------------------------------------------------------------------------------


async def test_the_branch_stops_at_the_customer_and_says_what_it_is_waiting_for(
    paused: Any,
) -> None:
    """The paused checkpoint has to be readable by whatever answers it.

    An interrupt payload that named no session would make the resume endpoint guess which of an
    incident's sessions the reply belonged to, and on the second script it would guess wrong.
    """
    graph, _ctx, config, state = paused
    snapshot = graph.get_state(config)

    assert snapshot.next == ("await_customer_response",)
    assert len(snapshot.interrupts) == 1
    assert state["status"] is IncidentStatus.AWAITING_CUSTOMER, (
        "the status is written by `mark_awaiting_customer` *before* the interrupt is raised; a "
        "single node that did both would checkpoint as still sending"
    )

    payload = snapshot.interrupts[0].value
    request = payload["customer_response_request"]
    session = state["self_help_session"]
    assert request["incident_id"] == COMPLIED
    assert request["session_id"] == session.session_id
    assert request["script_id"] == "move_device_closer"
    assert request["channel"] == "sms"
    assert request["response_deadline"] == session.response_deadline.isoformat()
    assert payload["accepted_responses"] == ["completed", "declined"], (
        "the payload must publish the vocabulary it will accept, or a caller has to read the source "
        "to find out that 'yes' is not a reply this branch understands"
    )
    assert session.awaiting_customer is True


async def test_the_deadline_is_the_adapter_s_and_not_a_second_computation(paused: Any) -> None:
    """`mark_awaiting_customer` reads the window back; it does not work one out.

    `send_self_help` already resolved the response window and told the customer about it. A node
    that recomputed one here would be a second policy agreeing with the first only until either
    changed, and the symptom would be an incident timed out before the window the customer was
    given had run.
    """
    _graph, _ctx, _config, state = paused
    session = state["self_help_session"]
    sent = [e for e in state["audit_events"] if e.node == "send_self_help_instructions"][-1]

    assert session.response_deadline is not None
    assert session.response_deadline.isoformat() == sent.detail["response_deadline"], (
        "the session's deadline and the one the adapter returned have diverged, which means "
        "something recomputed it"
    )
    assert session.response_deadline > session.awaiting_response_since


# ------------------------------------------------------------------------------------------------
# The three things a customer can do
# ------------------------------------------------------------------------------------------------


async def test_a_customer_who_says_done_is_verified_rather_than_believed(paused: Any) -> None:
    """**The invariant this whole branch exists to hold.** The customer's word is not restoration.

    The customer here genuinely complied -- the adapter's deterministic reply for this incident is
    `completed` -- and the session still ends `not_resolved`, because the telemetry cannot confirm
    it. That is the conservative direction and the correct one: `resolved` routes to validation and
    closure, and closing on an unverifiable claim is how an incident is closed on a fault that is
    still there.

    The distinction the outcome word loses is kept in the note, which is what a human reads before
    deciding whether the truck was necessary.
    """
    graph, ctx, config, _state = paused
    final = await graph.ainvoke(
        Command(resume={"response": "completed"}), context=ctx, config=config
    )

    session = final["self_help_session"]
    assert session.customer_responses == ["completed"], "the reply itself must survive"
    assert session.outcome == "not_resolved", (
        "the customer said they did it and the device was online throughout, so nothing was "
        "confirmed. `resolved` here would close an incident on a claim"
    )
    assert session.reason_code is None, (
        "neither SELF_HELP_SUCCEEDED nor SELF_HELP_DECLINED is true of 'we cannot tell'; an absent "
        "reason code reads as not-applicable, which is exactly right"
    )
    assert "neither confirmed nor refuted" in session.notes[-1]

    verified = [e for e in final["audit_events"] if e.node == "verify_self_help"]
    assert len(verified) == 1
    assert verified[0].outcome == "not_resolved"
    assert verified[0].detail["verification_passed"] is None, (
        "three-valued, and the audit trail has to carry the third value as itself: False would say "
        "the step failed, which is a different and unevidenced claim"
    )


async def test_a_customer_who_declines_ends_the_branch_without_touching_the_device(
    fixtures: Any,
) -> None:
    """A decline is terminal here, and it must not spend a CPE read finding that out."""
    graph, ctx, config, _first = await _drive(fixtures, DECLINED)
    final = await graph.ainvoke(
        Command(resume={"source": "scheduler_tick"}), context=ctx, config=config
    )

    assert graph.get_state(config).next == ()
    session = final["self_help_session"]
    assert session.customer_responses == ["declined"]
    assert session.outcome == "declined"
    assert session.reason_code is ReasonCode.SELF_HELP_DECLINED
    assert session.post_state == {}, (
        "a declined step changed nothing, so there is nothing to verify; a post-reading here would "
        "be a CPE call made to confirm that nothing happened"
    )

    nodes = [e.node for e in final["audit_events"]]
    assert "verify_self_help" not in nodes
    abandoned = [e for e in final["audit_events"] if e.node == "abandon_self_help"][-1]
    assert abandoned.outcome == "customer_declined"
    assert final["status"] is IncidentStatus.DIAGNOSING, (
        "leaving this at `awaiting_customer` would checkpoint the incident as waiting for something "
        "that has already happened"
    )


async def test_a_silent_customer_times_out_only_once_the_deadline_has_passed(
    fixtures: Any,
) -> None:
    """Silence before the deadline is not an answer, and silence after it is.

    Both halves in one test on purpose: the first resume proves the wait is not ended by being
    checked on, which is the failure that would let a scheduler tick roll a truck at a customer who
    still had twenty minutes to reply.
    """
    graph, ctx, config, _first = await _drive(fixtures, SILENT)

    still_waiting = await graph.ainvoke(
        Command(resume={"source": "scheduler_tick"}), context=ctx, config=config
    )
    assert graph.get_state(config).next == ("await_customer_response",), (
        "a tick inside the window must put the incident back to sleep, not end its wait"
    )
    assert still_waiting["self_help_session"].outcome == "in_progress"
    assert still_waiting["self_help_session"].customer_responses == []

    ctx.clock.advance(timedelta(hours=2))
    final = await graph.ainvoke(
        Command(resume={"source": "scheduler_tick"}), context=ctx, config=config
    )

    assert graph.get_state(config).next == ()
    session = final["self_help_session"]
    assert session.outcome == "timed_out"
    assert session.reason_code is ReasonCode.SELF_HELP_TIMED_OUT
    assert session.customer_responses == [], (
        "nothing was said, and recording a synthetic 'no reply' response would put words in the "
        "customer's mouth"
    )

    abandoned = [e for e in final["audit_events"] if e.node == "abandon_self_help"][-1]
    assert abandoned.outcome == "customer_did_not_respond", (
        "a silence and a decline are not interchangeable operationally: one is a customer who will "
        "not do it, the other may simply not have seen the message"
    )
    assert final["status"] is IncidentStatus.DIAGNOSING


# ------------------------------------------------------------------------------------------------
# The resume channel
# ------------------------------------------------------------------------------------------------


async def test_an_empty_resume_map_never_reaches_the_node(paused: Any) -> None:
    """`Command(resume={})` is silently discarded by LangGraph, and an API must not send one.

    Measured against langgraph 1.2.11: `_loop.py` decides whether a resume value is a map of
    interrupt-id to value with `isinstance(resume, dict) and all(is_xxh3_128_hexdigest(k) for k in
    resume)`, and `all()` over an empty dict is `True`. So `{}` is read as "a map that resumes
    nothing", the interrupt is left unsatisfied, and the graph re-pauses having run nothing.

    This is pinned rather than worked around because the failure is invisible: an endpoint that
    forwarded an empty webhook body would return 200, leave the incident exactly where it was, and
    produce no audit event to say so. `{"source": ...}` and `""` both reach the node.
    """
    graph, ctx, config, _state = paused

    after = await graph.ainvoke(Command(resume={}), context=ctx, config=config)
    snapshot = graph.get_state(config)

    assert snapshot.next == ("await_customer_response",)
    assert len(snapshot.interrupts) == 1
    assert not [e for e in after["audit_events"] if e.node == "await_customer_response"], (
        "the node left no audit event, which means it never ran -- the resume was dropped before "
        "delivery rather than being delivered and found unusable"
    )


async def test_a_bare_tick_falls_through_to_the_adapter(paused: Any) -> None:
    """A resume carrying no answer is a question, not a reply.

    This is what makes a timer-driven resume useful: the scheduler wakes the incident, the node
    finds nothing in the payload, and asks the adapter whether anything arrived out of band. The
    audit records which of the two channels the answer came from, because "the operator told us" and
    "the SMS gateway told us" are different provenances.
    """
    graph, ctx, config, _state = paused
    final = await graph.ainvoke(
        Command(resume={"source": "scheduler_tick"}), context=ctx, config=config
    )

    answered = [e for e in final["audit_events"] if e.node == "await_customer_response"][-1]
    assert answered.detail["reply"] == "completed"
    assert answered.detail["reply_source"] == "adapter", (
        "the tick carried no answer, so the reply must be attributed to the adapter that did"
    )
    assert answered.detail["deadline_passed"] is False


def test_customer_reply_reads_both_channels_and_refuses_to_guess() -> None:
    """One parser for the webhook and the adapter row, and `None` is never a decline.

    A garbled body, an empty tick, a reply in words nobody defined -- all of them mean *we still do
    not know*, and reading any of them as a refusal would end a customer's window early on the
    strength of a parse failure.
    """
    assert customer_reply({"response": "completed"}) == "completed"
    assert customer_reply({"response": "DECLINED"}) == "declined", "case is not the customer's job"
    assert customer_reply({"response": "  completed "}) == "completed"
    assert customer_reply({"customer_completed_step": True}) == "completed"
    assert customer_reply({"customer_completed_step": False}) == "declined"

    assert customer_reply({}) is None
    assert customer_reply({"source": "scheduler_tick"}) is None
    assert customer_reply({"response": "yes"}) is None, (
        "'yes' is not in the accepted vocabulary the interrupt published, and guessing that it "
        "means compliance is how a branch invents an answer"
    )
    assert customer_reply(None) is None
    assert customer_reply("completed") is None, (
        "a bare string is not the documented shape; accepting it would make the two channels two "
        "formats again"
    )
    assert customer_reply({"customer_completed_step": "true"}) is None, (
        "the boolean arm is `isinstance(..., bool)` on purpose -- a truthy string is a caller "
        "sending the wrong type, not a customer complying"
    )


# ------------------------------------------------------------------------------------------------
# Policy
# ------------------------------------------------------------------------------------------------


async def test_quiet_hours_stops_the_message_before_it_is_sent(fixtures: Any) -> None:
    """The customer-contact guard this branch introduced, exercised end to end.

    Run at 00:05 in `America/Puerto_Rico`. The whole reason `_shared.contact_history` takes a local
    instant rather than a UTC one is that the operating zone is UTC-04:00, so a UTC day boundary
    falls at 20:00 local -- and the same offset is what puts this run inside quiet hours.
    """
    graph, _ctx, config, final = await _drive(fixtures, COMPLIED, now=QUIET_HOURS_NOW)

    assert graph.get_state(config).next == (), "the branch must finish, not sit at an interrupt"
    assert final.get("self_help_session") is None, "nothing may have been sent"

    decision = [d for d in final["policy_decisions"] if d.action_type is ActionType.SEND_SELF_HELP][
        -1
    ]
    assert decision.outcome is PolicyOutcome.BLOCKED
    assert ReasonCode.POLICY_QUIET_HOURS in decision.reason_codes
    assert decision.matched_rule == "customer_contact.quiet_hours_start"

    selected = [e for e in final["audit_events"] if e.node == "select_self_help_script"][-1]
    assert selected.outcome == "blocked"
    abandoned = [e for e in final["audit_events"] if e.node == "abandon_self_help"][-1]
    assert abandoned.outcome == "blocked_by_policy"
    assert abandoned.reason_code is ReasonCode.POLICY_QUIET_HOURS, (
        "the abandon must carry the reason the send was refused, not a generic one; 'we did not "
        "message the customer at midnight' is the fact an auditor needs"
    )
    assert final["status"] is IncidentStatus.DIAGNOSING


def test_a_low_confidence_rca_puts_the_message_behind_an_approval() -> None:
    """The approval gate on this branch is live, and the fixture cannot reach it.

    `SVC-SJ-011-B-01` diagnoses at 0.950, comfortably over the bar, so no end-to-end run in this
    module pauses at the approval interrupt -- which would make the gate look like dead code. It is
    not: the engine is asked directly, on either side of the threshold.

    The bar is `rca.min_for_remote_action`, measured at 0.65 and inclusive. `SEND_SELF_HELP` is
    graded against the *remote* threshold rather than `min_for_dispatch` (0.70) because being wrong
    about asking someone to move their router costs them a minute, which is the whole point of the
    pack grading confidence by consequence.
    """
    engine = PolicyEngine(load_pack())

    def evaluate(confidence: float | None) -> Any:
        return engine.evaluate(
            PolicyInput(
                action_type=ActionType.SEND_SELF_HELP,
                incident_id="INC-GATE",
                target_ref="CPE-GATE",
                actor_role="automation",
                rca_confidence=confidence,
                evidence_source_count=3,
                evidence_age_minutes=2.0,
                attempt=1,
                blast_radius=1,
                severity=Severity.HIGH,
                local_time=time(14, 30),
                contacts_today=0,
                minutes_since_last_contact=None,
            )
        )

    assert evaluate(0.65).outcome is PolicyOutcome.ALLOWED, "the threshold is inclusive"

    held = evaluate(0.6499)
    assert held.outcome is PolicyOutcome.REQUIRES_APPROVAL
    assert held.required_approval_kind is ApprovalKind.LOW_CONFIDENCE_RCA
    assert ReasonCode.RCA_LOW_CONFIDENCE in held.reason_codes

    assert evaluate(None).outcome is PolicyOutcome.REQUIRES_APPROVAL, (
        "no RCA at all must be at least as restrictive as a bad one; treating a missing confidence "
        "as passing is the fail-open direction"
    )


async def test_the_gate_router_holds_an_action_that_needs_approving(paused: Any) -> None:
    """`route_self_help_gate` reads the decision; it does not re-make it.

    Driven on the paused state with the decision swapped, because the fixture's confidence never
    produces one. A router that looked at `option.required_approval` instead -- which the plan does
    carry -- would authorise against whatever the pack said when the plan was built, and for an
    incident that sat overnight that is a stale answer.
    """
    _graph, _ctx, _config, state = paused
    assert route_self_help_gate(state) == "send", "the control: 0.95 goes straight through"

    decision = [d for d in state["policy_decisions"] if d.action_type is ActionType.SEND_SELF_HELP][
        -1
    ]

    def _demanding(kind: ApprovalKind) -> dict[str, Any]:
        return {
            **state,
            "policy_decisions": [
                *[d for d in state["policy_decisions"] if d is not decision],
                decision.model_copy(
                    update={
                        "outcome": PolicyOutcome.REQUIRES_APPROVAL,
                        "required_approval_kind": kind,
                    }
                ),
            ],
        }

    assert route_self_help_gate(_demanding(ApprovalKind.HIGH_RISK_REMOTE_ACTION)) == "approve"

    blocked = dict(state)
    blocked["policy_decisions"] = [
        *[d for d in state["policy_decisions"] if d is not decision],
        decision.model_copy(update={"outcome": PolicyOutcome.BLOCKED}),
    ]
    assert route_self_help_gate(blocked) == "abandon"


async def test_this_gate_declines_a_kind_another_gate_owns(paused: Any) -> None:
    """A variable-kind gate may only ask a kind no other gate asks, because the readers key on kind.

    `latest_decision_of` and `approval_outstanding` match an answer to a question by
    `ApprovalKind` alone -- `ApprovalDecision` carries no `action_type`, `target_ref` or
    `policy_decision_id` to narrow it with. That is sound only while exactly one gate raises each
    kind. This gate takes its kind from the `PolicyDecision`, so without the
    `DEDICATED_GATE_APPROVAL_KINDS` check it will happily ask `low_confidence_rca` -- and the answer
    then satisfies `prepare_low_confidence_review`, whose whole job is the `rca is None` fail-closed
    branch this one knows nothing about.

    Reachable, not hypothetical: `rca.min_for_self_help` demands exactly this kind whenever
    confidence drops below 0.65, which `test_self_help_needs_a_confident_rca` above pins.

    Deleting the `DEDICATED_GATE_APPROVAL_KINDS` term from `route_self_help_gate` was observed
    turning this red as

        AssertionError: this gate would ask ['clean_to_dirty_handover', 'dispatch',
        'high_blast_radius_action', 'low_confidence_rca'], and every one of those has a gate of
        its own. [...]

    while `test_the_gate_router_holds_an_action_that_needs_approving` above stayed green, because a
    kind this gate owns routes to `approve` either way. All four are reported together rather than
    asserted one at a time so the failure names the whole leak, and iteration is sorted so the text
    is reproducible.
    """
    _graph, _ctx, _config, state = paused
    decision = [d for d in state["policy_decisions"] if d.action_type is ActionType.SEND_SELF_HELP][
        -1
    ]

    asked: list[str] = []
    for kind in sorted(DEDICATED_GATE_APPROVAL_KINDS, key=lambda k: k.value):
        demanding = {
            **state,
            "policy_decisions": [
                *[d for d in state["policy_decisions"] if d is not decision],
                decision.model_copy(
                    update={
                        "outcome": PolicyOutcome.REQUIRES_APPROVAL,
                        "required_approval_kind": kind,
                    }
                ),
            ],
        }
        if route_self_help_gate(demanding) != "abandon":
            asked.append(kind.value)

    assert not asked, (
        f"this gate would ask {asked}, and every one of those has a gate of its own. The answer is "
        "keyed on kind, so the owning gate reads it as already given and skips itself."
    )


# ------------------------------------------------------------------------------------------------
# The verdict
# ------------------------------------------------------------------------------------------------


def test_reachability_verdict_keeps_cannot_tell_apart_from_failed() -> None:
    """Three-valued, and the third value is the one that matters here.

    Reachability is the only unambiguous symptom the CPE adapter -- and TR-069 -- exposes, so a
    device that was online throughout has no observable before-and-after. Collapsing that to a
    boolean does damage in both directions: `True` closes a step that changed nothing, `False`
    re-diagnoses a repair that worked.

    Shared with the remote branch deliberately: the customer's power-cycle is judged by exactly the
    criterion the ACS reboot is judged by, because they are the same event with a different actor.
    Until this module existed the function had no test at all.
    """
    passed, why = reachability_verdict({"online": False}, {"online": True})
    assert passed is True
    assert "re-established" in why

    passed, why = reachability_verdict({"online": True}, {"online": False})
    assert passed is False
    assert "still offline" in why

    passed, why = reachability_verdict({"online": True}, {"online": True})
    assert passed is None, (
        "online before and online after is the Wi-Fi and throughput case -- every fault this "
        "branch actually handles. Answering True here would close them all on no evidence"
    )
    assert "neither confirmed nor refuted" in why

    passed, why = reachability_verdict(None, {"online": True})
    assert passed is None, "with no reading before, nothing can be attributed to the action"
    assert "no reading was taken before" in why

    passed, why = reachability_verdict({"online": False}, None)
    assert passed is False, (
        "a verification that could not be performed is not a verification that passed"
    )
    assert "could not be reached" in why


async def test_verify_records_resolved_when_the_device_actually_came_back(
    fixtures: Any,
) -> None:
    """The `resolved` arm, driven at node level because no fixture can reach it.

    The customer-environment simulator holds its gateway online, so the end-to-end runs above can
    only ever land on `None`. Supplying an offline pre-reading is the smallest change that makes the
    successful path observable, and it is the branch that decides whether an incident may be closed.
    """
    _graph, _ctx, _config, state = await _drive(fixtures, COMPLIED)
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]

    driven = dict(state)
    driven["self_help_session"] = SelfHelpSession(
        session_id="SHS-CAME-BACK",
        incident_id=COMPLIED,
        channel=CommunicationChannel.SMS,
        started_at=NOW,
        steps_sent=["reboot_gateway"],
        step_index=1,
        customer_responses=["completed"],
        completed_at=NOW + timedelta(minutes=8),
        pre_state={"online": False},
    )
    assert route_customer_answer(driven) == "verify"

    update = await verify_self_help.__wrapped__(driven, ctx)  # type: ignore[attr-defined]

    session = update["self_help_session"]
    assert session.outcome == "resolved"
    assert session.reason_code is ReasonCode.SELF_HELP_SUCCEEDED
    assert session.post_state["online"] is True

    recorded = update["audit_events"][0]
    assert recorded.detail["verification_passed"] is True
    assert update["evidence"][0].source_system == "cpe", (
        "the reading has to become evidence, or a later cycle re-reads the device to learn what "
        "this one already established"
    )


# ------------------------------------------------------------------------------------------------
# What reaches the record
# ------------------------------------------------------------------------------------------------


async def test_the_send_records_the_contact_it_made(paused: Any) -> None:
    """The one node that messages a customer is the one that must write it down.

    `customer_communications` had no writer at all before this branch, and the omission was not
    inert: `customer_contacts_per_incident` counts exactly this list and never returns `None`, so an
    incident that had just texted its customer reported *zero contacts*, confidently. A silently
    wrong number is worse than a missing one.
    """
    _graph, _ctx, _config, state = paused

    contacts = state["customer_communications"]
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact["direction"] == "outbound"
    assert contact["purpose"] == "self_help_instructions"
    assert contact["channel"] == "sms"
    assert contact["script_id"] == "move_device_closer"
    assert contact["session_id"] == state["self_help_session"].session_id
    assert contact["action_id"], (
        "the entry is keyed on `action_id` so `append_unique` collapses a replay of this node; "
        "without a recognised key a re-run would double-count the message"
    )

    sent = [
        record
        for record in state["action_history"]
        if record.action_type is ActionType.SEND_SELF_HELP
    ]
    assert len(sent) == len(contacts), (
        "the contact cap the policy engine enforces counts `action_history`, and the rate the "
        "dashboard reports counts `customer_communications`. They agree only because this node "
        "appends to both; if they ever diverge the dashboard will disagree with the refusals"
    )


async def test_every_stage_of_the_branch_reaches_the_kpi_record(paused: Any) -> None:
    """A KPI emitted from the node that records the fact must read its own write.

    Every one of these was emitted against the node's *input* state and therefore never fired at
    all. `emit_kpi` swallows `KPINotDerivableError` by design -- a KPI state cannot yet support is
    normal at a stage boundary -- so the failure was perfectly silent: no exception, no event, and a
    green test suite. `preview` applies the declared reducers and is what the intake and diagnosis
    nodes already used.

    Asserted as a set rather than one by one so that a KPI which stops firing is caught even if no
    test names it.
    """
    graph, ctx, config, first = paused

    emitted = {e.kpi_name for e in first["kpi_events"]}
    assert KPIName.POLICY_BLOCK_RATE in emitted, (
        "`select_self_help_script` makes the decision and then counts the decisions; against raw "
        "state the list it counts was still empty"
    )
    assert KPIName.AUTOMATION_COVERAGE_RATE in emitted
    assert KPIName.CUSTOMER_CONTACTS_PER_INCIDENT in emitted

    contacts = next(
        e for e in first["kpi_events"] if e.kpi_name == KPIName.CUSTOMER_CONTACTS_PER_INCIDENT
    )
    assert contacts.value == pytest.approx(1.0), (
        "one message was sent; a zero here is the unwritten-list defect returning"
    )
    assert contacts.dimensions["channel"] == "sms"

    final = await graph.ainvoke(
        Command(resume={"response": "completed"}), context=ctx, config=config
    )
    success = [e for e in final["kpi_events"] if e.kpi_name == KPIName.SELF_HELP_SUCCESS_RATE]
    assert len(success) == 1, (
        "`verify_self_help` replaces the session and then measures it; against raw state the "
        "session still read `in_progress`, for which the calculator correctly returns None"
    )
    assert success[0].value == pytest.approx(0.0)
    assert success[0].dimensions["script_id"] == "move_device_closer"


async def test_a_customer_who_refuses_still_counts_against_the_success_rate(
    fixtures: Any,
) -> None:
    """The denominator must include the people who said no.

    `verify_self_help` is only ever reached by a customer who complied, so a success rate emitted
    from there alone is computed over the compliant only -- and would climb every time somebody
    refused. `abandon_self_help` emits it too, and relies on the calculator's own guard to exclude
    the sessions where nothing was ever asked.
    """
    graph, ctx, config, _first = await _drive(fixtures, DECLINED)
    final = await graph.ainvoke(
        Command(resume={"source": "scheduler_tick"}), context=ctx, config=config
    )

    success = [e for e in final["kpi_events"] if e.kpi_name == KPIName.SELF_HELP_SUCCESS_RATE]
    assert len(success) == 1, "a decline is a self-help attempt that did not succeed"
    assert success[0].value == pytest.approx(0.0)
    assert [e.node for e in final["audit_events"] if e.node == "verify_self_help"] == [], (
        "the control: this rate was emitted without the verify node running at all"
    )

    # Which node emitted it, checked rather than inferred. `KPIEvent` has no `node` field -- the
    # emitter survives only inside `event_id`, which `emit_kpi` derives from the incident, the KPI,
    # the node and a discriminator. Re-deriving it here is the only way to say "this came from
    # `abandon_self_help`", and it is worth saying: move the emission back into `verify_self_help`
    # and the assertions above still hold on some other path, while this one does not.
    session = final["self_help_session"]
    assert success[0].event_id == derive_id(
        "KPI", DECLINED, KPIName.SELF_HELP_SUCCESS_RATE, "abandon_self_help", session.session_id
    )


async def test_a_blocked_send_is_not_counted_as_a_failed_self_help(fixtures: Any) -> None:
    """Nothing was asked of the customer, so there is no attempt to score.

    The counterpart to the test above, and the reason `abandon_self_help` emits unconditionally
    rather than testing the outcome itself: the calculator already returns `None` for a session that
    is absent or still running, which is exactly the three routes where the customer was never
    contacted. A second copy of that rule in the node would diverge the first time either changed.
    """
    _graph, _ctx, _config, final = await _drive(fixtures, COMPLIED, now=QUIET_HOURS_NOW)

    assert final.get("self_help_session") is None

    def named(kpi: KPIName) -> list[Any]:
        return [e for e in final["kpi_events"] if e.kpi_name == kpi]

    # The positive control goes first, and shares the comparison. This assertion is a negative one,
    # so a filter that matches nothing passes it while proving nothing -- which is not hypothetical:
    # these were written as `e.kpi_name is KPIName.X` while `KPIEvent.kpi_name` was declared `str`,
    # so pydantic handed back a plain `str` and the identity was never true. The presence check
    # failed loudly and named the mistake; had only the absence check existed it would have stayed
    # green. The field is a `KPIName` now, which removes the trap -- but the control stays: it costs
    # one assertion and it is what makes the absence check below mean something.
    assert named(KPIName.POLICY_BLOCK_RATE), (
        "the block itself is what this incident has to show for the branch -- and if this is empty, "
        "the absence check below is measuring a broken filter rather than the node's behaviour"
    )
    assert not named(KPIName.SELF_HELP_SUCCESS_RATE), (
        "quiet hours stopped the message before it went out. Scoring that as a self-help failure "
        "would make the success rate fall every time the pack correctly refused to wake somebody"
    )
