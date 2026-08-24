"""The runtime context, the bounded-loop guard, the approval gates, and reading a paused incident.

Three habits, the same ones the policy tests are built on.

**Both directions on every gate.** A guard that has only been seen refusing is a guard that might
refuse everything. Every budget is driven to its limit *and* checked just below it.

**A bound nobody can reach is not a bound.** The first draft of `guards.py` had two ceilings on one
counter and the looser one was checked first, so it could never fire -- caught by sweeping the
counter rather than by reading the code.
`test_every_budget_fires_exactly_at_its_limit_and_not_one_entry_late` is what keeps that from coming
back, and it is parametrised over `list(BudgetKind)` so a new bound cannot skip it.

**Assert the mechanism, not the outcome.** A rejected approval is not interesting on its own; *why*
it was rejected is, because two very different failures -- "you may not approve this" and "nobody is
named" -- would otherwise be indistinguishable in the audit trail.

Every assertion here has been watched fail against the defect it names: each one was reinstated in
turn and the naming test confirmed to go red. Two did not, on the first run, and both were real gaps
rather than harness artefacts -- see the docstrings of
`test_the_operator_is_shown_every_role_that_may_answer_not_just_the_packs_one` and
`test_the_reason_names_the_count_the_limit_and_where_the_limit_came_from`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Command, interrupt

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.config.settings import Settings
from lpr_cpe.domain.enums import ApprovalKind, ApprovalStatus, IncidentStatus
from lpr_cpe.graph import inspect as gi
from lpr_cpe.graph.context import GraphContext, build_context
from lpr_cpe.graph.guards import (
    BudgetKind,
    BudgetVerdict,
    check_budgets,
    escalation_update,
    reentry_budget,
    step_budget,
)
from lpr_cpe.graph.interrupts import (
    approval_id_for,
    build_request,
    prepare_approval,
    request_approval,
)
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.observability.kpi import MetricTimestamp, mark, stamp
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer
from lpr_cpe.security.rbac import approvers_for

AT = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)


@pytest.fixture
def ctx() -> GraphContext:
    """A context on a frozen clock, so every timestamp in a test is the one the test chose."""
    return build_context(clock=FrozenClock(AT))


def _state(**overrides: Any) -> IncidentState:
    base: dict[str, Any] = {
        "incident_id": "INC-1",
        "correlation_id": "COR-1",
        "node_visits": {},
        "diagnostic_cycles": 0,
        "resolution_cycles": 0,
    }
    base.update(overrides)
    return IncidentState(**base)  # type: ignore[typeddict-item]


# ------------------------------------------------------------------------------------------------
# Context
# ------------------------------------------------------------------------------------------------


def test_the_context_is_frozen_so_a_node_cannot_write_state_through_the_side_door(
    ctx: GraphContext,
) -> None:
    """A mutable context would be state that no reducer guards and no checkpoint records.

    Asserted on `FrozenInstanceError` rather than on the message, which is CPython's wording and
    not ours: 3.14 says "cannot assign to field 'settings'", earlier versions said something else.
    The exception *type* is the contract `@dataclass(frozen=True)` actually offers.
    """
    before = ctx.settings
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.settings = Settings()  # type: ignore[misc]
    assert ctx.settings is before, "the assignment raised but landed anyway"


def test_a_missing_policy_pack_refuses_to_build_a_context_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_context` is a *start*, and a start with an unusable pack should not happen.

    The engine also offers `load_or_unavailable`, which returns an engine that blocks everything.
    That is the right choice for a *reload* in a process already serving, and the wrong one here:
    it would turn a typo in the YAML into a run of incidents blocked one at a time instead of one
    loud failure at boot.
    """
    from lpr_cpe.policies import PACK_PATH_ENV_VAR, PolicyPackError, clear_pack_cache

    monkeypatch.setenv(PACK_PATH_ENV_VAR, "does-not-exist.yaml")
    clear_pack_cache()
    try:
        with pytest.raises(PolicyPackError):
            build_context(clock=FrozenClock(AT))
    finally:
        clear_pack_cache()


# ------------------------------------------------------------------------------------------------
# The bounded-loop guard
# ------------------------------------------------------------------------------------------------


def test_a_healthy_incident_passes_every_budget(ctx: GraphContext) -> None:
    """The other direction. Without this, a guard that refused everything would look correct."""
    assert check_budgets(_state(), ctx, node="triage").within_budget


def _counter_at(kind: BudgetKind, n: int) -> IncidentState:
    """A state whose `kind` counter reads exactly `n`, with the other three left clear.

    `TOTAL_STEPS` piles its visits on a node the guard is not asked about, so the re-entry bound --
    which reads the same `node_visits` dict -- stays well clear and cannot fire first and mask it.
    """
    if kind is BudgetKind.TOTAL_STEPS:
        return _state(node_visits={"some-other-node": n})
    if kind is BudgetKind.NODE_REENTRIES:
        return _state(node_visits={"triage": n})
    if kind is BudgetKind.DIAGNOSTIC_CYCLES:
        return _state(diagnostic_cycles=n)
    if kind is BudgetKind.RESOLUTION_CYCLES:
        return _state(resolution_cycles=n)
    raise AssertionError(f"no way to drive {kind} to its limit, so it is an untested bound")


def _limit_of(kind: BudgetKind, ctx: GraphContext) -> int:
    """The limit `kind` binds at, asked of the module rather than written down here again."""
    if kind is BudgetKind.TOTAL_STEPS:
        return step_budget(ctx)[0]
    if kind is BudgetKind.NODE_REENTRIES:
        return reentry_budget(ctx, "triage")[0]
    if kind is BudgetKind.DIAGNOSTIC_CYCLES:
        return ctx.max_diagnostic_cycles
    if kind is BudgetKind.RESOLUTION_CYCLES:
        return ctx.max_resolution_cycles
    raise AssertionError(f"no way to read {kind}'s limit, so it is an untested bound")


@pytest.mark.parametrize("kind", list(BudgetKind), ids=str)
def test_every_budget_fires_exactly_at_its_limit_and_not_one_entry_late(
    kind: BudgetKind, ctx: GraphContext
) -> None:
    """Each bound is reachable on its own, and stops the graph at the limit rather than past it.

    Two claims in one sweep, because separating them is what let a bug through. The first draft
    checked reachability by driving total steps to 65 against a limit of 60 -- which fires under
    `>=` and under `>` alike, so the off-by-one this test is named for survived. Every bound is now
    checked at `limit - 1` (must pass) and at `limit` (must fail): `node_visits` counts visits
    already *completed*, so a node entering for the (limit + 1)th time sees `limit` and stops now.

    Reachability matters because the first draft of `guards.py` had two ceilings on one counter with
    the looser checked first, so the tighter could never fire -- a bound that reads like protection
    and provides none. Parametrising over `list(BudgetKind)` rather than a written-out list is what
    keeps that closed: a new member with no way to drive it raises in `_counter_at`. That is not a
    hypothetical guard -- adding `RESOLUTION_CYCLES` failed here, and only here, before its two
    helpers learned it.
    """
    limit = _limit_of(kind, ctx)

    assert check_budgets(_counter_at(kind, limit - 1), ctx, node="triage").within_budget, (
        f"{kind} stopped the graph one entry early, at {limit - 1} against a limit of {limit}"
    )

    verdict = check_budgets(_counter_at(kind, limit), ctx, node="triage")
    assert not verdict.within_budget, f"{kind} did not fire at its own limit of {limit}"
    assert verdict.kind is kind, f"{limit} on {kind} was reported as {verdict.kind}"
    assert verdict.owner, "a fired budget must name where its limit came from"
    assert verdict.owner in verdict.reason


def test_the_reason_names_the_count_the_limit_and_where_the_limit_came_from() -> None:
    """An escalation reading "loop limit reached" is a page nobody can act on.

    Built from a verdict with three *distinct* values rather than from a real `check_budgets` call,
    which is the point: at a boundary `observed` equals `limit`, so a reason that had dropped the
    limit entirely would still appear to contain it. That is not hypothetical -- it is what let
    `owner_not_reported` survive the first mutation run of this file.
    """
    reason = BudgetVerdict(False, BudgetKind.TOTAL_STEPS, 61, 60, "settings.max_graph_steps").reason
    assert "61" in reason, "the reason did not say how far the incident got"
    assert "60" in reason, "the reason did not say what the limit was"
    assert "settings.max_graph_steps" in reason, "the reason did not say where to read the limit"
    assert BudgetVerdict(True).reason == "", "a passing verdict must not carry an escalation reason"


def test_the_step_budget_can_be_bound_by_either_owner(ctx: GraphContext) -> None:
    """Both claimants are live, so neither is a decoy.

    At the shipped defaults the engineering circuit breaker (60) is tighter than the operational
    budget (200) and binds. Raising it above 200 hands the decision to the pack. Asserted as an
    ordering between two configurations rather than as two literals, so re-tuning either default
    does not silently turn this into a test of nothing.
    """
    tight_limit, tight_owner = step_budget(ctx)
    loose_limit, loose_owner = step_budget(
        build_context(clock=FrozenClock(AT), settings=Settings(max_graph_steps=500))
    )
    assert tight_owner == "settings.max_graph_steps"
    assert loose_owner == "policy.attempt_limits.total_steps"
    assert tight_limit < loose_limit


def test_a_per_node_override_can_tighten_or_loosen_the_reentry_bound() -> None:
    """A scenario test needs to trip one node's guard without editing the pack every test reads."""
    tight = build_context(clock=FrozenClock(AT), node_visit_budget={"triage": 2})
    loose = build_context(clock=FrozenClock(AT), node_visit_budget={"triage": 20})
    assert not check_budgets(_state(node_visits={"triage": 2}), tight, node="triage").within_budget
    assert check_budgets(_state(node_visits={"triage": 10}), loose, node="triage").within_budget
    assert reentry_budget(tight, "triage")[1].startswith("GraphContext.node_visit_budget")
    assert reentry_budget(tight, "other")[1] == "policy.attempt_limits.max_subgraph_reentries"


def test_escalation_names_the_bound_that_fired_and_where_to_read_it(ctx: GraphContext) -> None:
    """ "Escalated: loop limit" without the limit is a page nobody can act on.

    The count comes off `ctx` rather than being written here, for the reason `_limit_of` gives one
    screen up: a literal is a second copy of `settings.max_diagnostic_cycles` that goes stale in
    silence. This one did. Raising the default from 3 to 6, so the resolution fork could be reached
    at all, left this state comfortably *inside* the budget -- and the break surfaced as
    `escalation_update` refusing a passing verdict, which says nothing about the number that moved.
    """
    cycles = ctx.max_diagnostic_cycles
    verdict = check_budgets(_state(diagnostic_cycles=cycles), ctx, node="diagnose")
    update = escalation_update(_state(diagnostic_cycles=cycles), ctx, verdict, node="diagnose")
    assert update["escalated"] is True
    assert update["status"] is IncidentStatus.ESCALATED
    event = update["audit_events"][0]
    assert event.detail["budget"] == BudgetKind.DIAGNOSTIC_CYCLES
    assert event.detail["limit"] == cycles
    assert event.detail["owner"] == "settings.max_diagnostic_cycles"
    assert event.policy_version == ctx.policy.policy_version


def test_escalating_twice_records_one_event_because_the_id_is_derived_not_minted(
    ctx: GraphContext,
) -> None:
    """A replayed guard must not appear to have escalated twice.

    `append_unique` de-duplicates on the natural key, which for an `AuditEvent` is its id -- so a
    `uuid4` here would defeat it. The contrast case is the point: a *different* bound is a different
    escalation and must keep its own id.
    """
    state = _state(diagnostic_cycles=ctx.max_diagnostic_cycles)
    verdict = check_budgets(state, ctx, node="diagnose")
    first = escalation_update(state, ctx, verdict, node="diagnose")["audit_events"][0]
    second = escalation_update(state, ctx, verdict, node="diagnose")["audit_events"][0]
    assert first.event_id == second.event_id

    other = escalation_update(
        state, ctx, BudgetVerdict(False, BudgetKind.TOTAL_STEPS, 60, 60, "x"), node="diagnose"
    )["audit_events"][0]
    assert other.event_id != first.event_id


def test_escalating_a_healthy_incident_raises_rather_than_returning_nothing(
    ctx: GraphContext,
) -> None:
    """Returning `{}` would let the graph run on while the caller believed it had stopped it."""
    with pytest.raises(ValueError, match="passing verdict"):
        escalation_update(_state(), ctx, BudgetVerdict(True), node="triage")


# ------------------------------------------------------------------------------------------------
# The approval gates
# ------------------------------------------------------------------------------------------------


def test_the_approval_id_is_stable_for_one_question_and_distinct_for_the_next() -> None:
    """`attempt` is what separates a deliberate re-ask from a replay of the first ask."""
    assert approval_id_for("INC-1", ApprovalKind.DISPATCH, 1) == approval_id_for(
        "INC-1", ApprovalKind.DISPATCH, 1
    )
    assert approval_id_for("INC-1", ApprovalKind.DISPATCH, 1) != approval_id_for(
        "INC-1", ApprovalKind.DISPATCH, 2
    )
    assert approval_id_for("INC-1", ApprovalKind.DISPATCH, 1) != approval_id_for(
        "INC-1", ApprovalKind.HIGH_RISK_REMOTE_ACTION, 1
    )


def test_expiry_and_required_role_come_from_the_pack_not_from_the_caller(
    ctx: GraphContext,
) -> None:
    """A caller that could set its own expiry could make a high-risk approval stand for a week."""
    request = build_request(
        _state(), ctx, kind=ApprovalKind.HIGH_BLAST_RADIUS_ACTION, question="q", attempt=1
    )
    rule = ctx.policy.pack.approvals[ApprovalKind.HIGH_BLAST_RADIUS_ACTION]
    assert request.required_role == rule.required_role.value
    assert request.expires_at is not None
    assert (request.expires_at - request.requested_at).total_seconds() / 60 == (
        rule.expires_after_minutes
    )


def _gate_app(kind: ApprovalKind = ApprovalKind.DISPATCH, *, nested: bool = True) -> Any:
    """A parent graph whose only node is a subgraph containing the two-node gate.

    Nested by default: all six real gates are, and nesting is what makes the parent's state
    understate a paused incident. A flat test graph would not exercise the case that matters.

    `nested=False` puts the same two nodes directly in the parent. It is a **control**, not an
    alternative arrangement to support -- several of the readers here are correct on a flat graph
    and wrong on a nested one, and asserting only the nested case leaves no evidence of which half
    of the behaviour is the surprising one.
    """

    async def prepare(state: IncidentState) -> dict[str, Any]:
        runtime = get_runtime(GraphContext)
        return prepare_approval(
            state,
            runtime.context,
            build_request(
                state, runtime.context, kind=kind, question="Send a crew to ODP-7?", attempt=1
            ),
        )

    async def ask(state: IncidentState) -> dict[str, Any]:
        return request_approval(state, get_runtime(GraphContext).context)

    outer = StateGraph(IncidentState, context_schema=GraphContext)
    if nested:
        inner = StateGraph(IncidentState)
        inner.add_node("prepare", prepare)
        inner.add_node("ask", ask)
        inner.add_edge(START, "prepare")
        inner.add_edge("prepare", "ask")
        inner.add_edge("ask", END)

        outer.add_node("gate", inner.compile())
        outer.add_edge(START, "gate")
        outer.add_edge("gate", END)
    else:
        outer.add_node("prepare", prepare)
        outer.add_node("ask", ask)
        outer.add_edge(START, "prepare")
        outer.add_edge("prepare", "ask")
        outer.add_edge("ask", END)
    return outer.compile(checkpointer=build_memory_checkpointer())


async def _answer(answer: Any, ctx: GraphContext, thread: str) -> dict[str, Any]:
    """Run to the pause, hand back `answer`, and return the final state."""
    app = _gate_app()
    config = {"configurable": {"thread_id": thread}}
    paused = await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING}, config, context=ctx
    )
    assert "__interrupt__" in paused, "the gate did not pause, so no answer was ever required"
    return dict(
        await app.ainvoke(
            Command(resume={i.id: answer for i in paused["__interrupt__"]}), config, context=ctx
        )
    )


async def test_a_permitted_role_approves(ctx: GraphContext) -> None:
    """The positive control. Every rejection below is only meaningful next to this."""
    out = await _answer(
        {"status": "approved", "decided_by": "alice", "decided_by_role": "noc_operator"},
        ctx,
        "ok",
    )
    decision = out["approvals"][0]
    assert decision.status is ApprovalStatus.APPROVED
    assert decision.decided_by == "alice"
    assert out["pending_approval"] is None


async def test_a_supervisor_may_answer_a_question_addressed_to_an_operator(
    ctx: GraphContext,
) -> None:
    """Why the payload carries `permitted_roles` and not just the pack's single `required_role`.

    The pack names one role per kind; `security.rbac` permits a set. Sending only the pack's role
    would tell supervisors they cannot answer questions they can.
    """
    out = await _answer(
        {"status": "approved", "decided_by": "sue", "decided_by_role": "noc_supervisor"}, ctx, "sup"
    )
    assert out["approvals"][0].status is ApprovalStatus.APPROVED


async def test_the_operator_is_shown_every_role_that_may_answer_not_just_the_packs_one(
    ctx: GraphContext,
) -> None:
    """The other half of the supervisor case, and the half an operator actually sees.

    That `can_approve` permits a set is worth nothing if the payload shown to a human names one
    role: the UI built on it would tell a supervisor they cannot answer a question they can, and the
    incident would sit unanswered while a permitted approver looked at it. Narrowing
    `permitted_roles` back to the pack's single `required_role` passes every other test in this
    file, which is why this one exists.
    """
    app = _gate_app()
    paused = await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING},
        {"configurable": {"thread_id": "payload"}},
        context=ctx,
    )
    payload = paused["__interrupt__"][0].value

    shown = payload["permitted_roles"]
    assert set(shown) == {r.value for r in approvers_for(ApprovalKind.DISPATCH)}
    assert len(shown) > 1, (
        "the payload named a single permitted role. If the RBAC table genuinely permits only one "
        "role for dispatch now, this test no longer distinguishes the set from the pack's single "
        "`required_role` -- pick a kind that still has several, or delete `permitted_roles`."
    )
    assert payload["approval_request"]["required_role"] in shown


async def test_the_payload_is_json_because_the_checkpointer_has_to_store_it(
    ctx: GraphContext,
) -> None:
    """Handing `interrupt()` a Pydantic model works in memory and fails against Postgres.

    Asserted on the *shape* rather than by expecting a crash, because in-memory is exactly where the
    mistake does not raise -- so a test that only ran the graph would be green on the code that
    breaks in production. A `datetime` that is still a `datetime` here means `mode="json"` was
    dropped.
    """
    app = _gate_app()
    paused = await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING},
        {"configurable": {"thread_id": "json"}},
        context=ctx,
    )
    request = paused["__interrupt__"][0].value["approval_request"]

    assert isinstance(request, dict), f"the payload carried a live {type(request).__name__}"
    assert isinstance(request["requested_at"], str), (
        f"requested_at came through as {type(request['requested_at']).__name__}, so the dump was "
        "not in JSON mode. This survives the in-memory checkpointer and fails against Postgres."
    )
    assert isinstance(request["kind"], str)


@pytest.mark.parametrize(
    ("answer", "expected_in_rationale"),
    [
        pytest.param(
            {"status": "approved", "decided_by": "bot", "decided_by_role": "automation"},
            "may not approve",
            id="automation_cannot_approve_its_own_action",
        ),
        pytest.param(
            {"status": "approved", "decided_by": "bob", "decided_by_role": "field_technician"},
            "may not approve",
            id="a_role_outside_the_allowlist",
        ),
        pytest.param(
            {"status": "approved", "decided_by_role": "noc_operator"},
            "no `decided_by`",
            id="an_approval_nobody_is_named_for",
        ),
        pytest.param(
            {"status": "yolo", "decided_by": "alice", "decided_by_role": "noc_operator"},
            "unrecognised approval status",
            id="an_unrecognised_status",
        ),
        pytest.param("approved", "malformed approval response", id="not_a_mapping_at_all"),
    ],
)
async def test_an_unacceptable_answer_is_recorded_as_a_rejection_not_raised(
    answer: Any, expected_in_rationale: str, ctx: GraphContext
) -> None:
    """Every bad answer leaves a resumable incident and an explained refusal.

    Raising here would strand the incident at the one moment a human is already involved, and show
    an operator a stack trace instead of "your role cannot approve this". The rationale is asserted
    rather than just the status, because these five failures are operationally different and an
    audit trail that called them all "rejected" would be useless.

    `automation_cannot_approve_its_own_action` is the one with teeth: it is what stops the graph
    from satisfying its own gates.
    """
    out = await _answer(answer, ctx, f"bad-{expected_in_rationale[:8]}")
    decision = out["approvals"][0]
    assert decision.status is ApprovalStatus.REJECTED
    assert expected_in_rationale in decision.rationale


async def test_a_rejection_with_no_stated_reason_still_records_a_rationale(
    ctx: GraphContext,
) -> None:
    """`ApprovalDecision` refuses an unexplained rejection, and the graph routes on the rationale."""
    out = await _answer(
        {"status": "rejected", "decided_by": "alice", "decided_by_role": "noc_operator"},
        ctx,
        "norat",
    )
    decision = out["approvals"][0]
    assert decision.status is ApprovalStatus.REJECTED
    assert decision.rationale


async def test_reaching_the_ask_without_the_prepare_is_refused(ctx: GraphContext) -> None:
    """Every gate is the pair. A lone `ask` means an edge skips the node that records the question."""

    async def ask(state: IncidentState) -> dict[str, Any]:
        return request_approval(state, get_runtime(GraphContext).context)

    inner = StateGraph(IncidentState)
    inner.add_node("ask", ask)
    inner.add_edge(START, "ask")
    inner.add_edge("ask", END)
    outer = StateGraph(IncidentState, context_schema=GraphContext)
    outer.add_node("gate", inner.compile())
    outer.add_edge(START, "gate")
    outer.add_edge("gate", END)
    app = outer.compile(checkpointer=build_memory_checkpointer())

    with pytest.raises(ValueError, match="no pending_approval"):
        await app.ainvoke(
            {"incident_id": "INC-1"}, {"configurable": {"thread_id": "lone"}}, context=ctx
        )


# ------------------------------------------------------------------------------------------------
# Reading a paused incident
# ------------------------------------------------------------------------------------------------


async def test_the_parent_alone_understates_a_paused_incident_and_inspect_corrects_it(
    ctx: GraphContext,
) -> None:
    """The finding this module exists for, asserted with its own negative control.

    A subgraph's writes reach the parent when the subgraph node *completes*, and a paused one has
    not. So the obvious implementation of a state-inspection endpoint -- read `.values` off the
    parent -- reports whatever stage the incident was in when it entered the subgraph, while it has
    in fact been sitting on someone's approval queue.

    **`dispatch_planning` below is this harness's figure and not the real graph's**, and the
    distinction is worth keeping because the number looks like a measurement and is not one.
    `_gate_app` is handed `status=DISPATCH_PLANNING` on the way in, so that is simply what the
    parent still holds at the pause -- which is what makes it a clean statement of the mechanism:
    one seeded field, and the paused child disagreeing with it. Driven through the *real* parent the
    same read returns `diagnosing`, because `DISPATCH_PLANNING` is written only inside
    `subgraphs/field_planning.py` and a paused subgraph never delivers the write. `graph.inspect`'s
    module docstring carries that measurement; this test is about the mechanism, and seeding the
    status is what keeps the two independent rather than two copies of one run.

    The naive read is asserted to be wrong as well as the corrected read to be right. Without that
    half, a future LangGraph that started propagating subgraph writes eagerly would leave
    `effective_state` looking necessary when it had become redundant.
    """
    app = _gate_app()
    config = {"configurable": {"thread_id": "paused"}}
    await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING}, config, context=ctx
    )

    naive = (await app.aget_state(config)).values
    assert naive.get("status") is IncidentStatus.DISPATCH_PLANNING
    assert naive.get("pending_approval") is None

    effective = await gi.effective_state(app, config)
    assert effective["status"] is IncidentStatus.AWAITING_APPROVAL
    assert effective["pending_approval"] is not None

    pending = await gi.pending_approval_for(app, config)
    assert pending is not None
    assert pending.kind is ApprovalKind.DISPATCH
    assert pending.question == "Send a crew to ODP-7?"
    assert await gi.is_awaiting_human(app, config) is True


async def test_a_gate_that_never_wrote_the_status_is_still_recognised_as_waiting(
    ctx: GraphContext,
) -> None:
    """Why `is_awaiting_human` is defined on the interrupt and not on `status`.

    The two can disagree, and this is the disagreement that matters: a gate added without its
    `prepare_approval` half pauses the graph without ever writing `AWAITING_APPROVAL`. Defined on
    the status field, the incident would be reported as running while it sat blocked on a human --
    and the status is just a field some node wrote, where the interrupt is the thing that actually
    stopped the graph.

    The status is asserted to be the stale one as well, so this stays a test about the disagreement
    rather than becoming a second copy of the test above.
    """

    async def bare_gate(state: IncidentState) -> dict[str, Any]:
        interrupt({"question": "a gate someone wrote without its prepare"})
        return {}

    inner = StateGraph(IncidentState)
    inner.add_node("bare", bare_gate)
    inner.add_edge(START, "bare")
    inner.add_edge("bare", END)
    outer = StateGraph(IncidentState, context_schema=GraphContext)
    outer.add_node("gate", inner.compile())
    outer.add_edge(START, "gate")
    outer.add_edge("gate", END)
    app = outer.compile(checkpointer=build_memory_checkpointer())

    config = {"configurable": {"thread_id": "bare"}}
    await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING}, config, context=ctx
    )

    effective = await gi.effective_state(app, config)
    assert effective["status"] is IncidentStatus.DISPATCH_PLANNING, (
        "this gate was supposed to pause without recording that it was waiting; if the status is "
        "now correct the disagreement is gone and this test proves nothing"
    )
    assert await gi.is_awaiting_human(app, config) is True
    assert await gi.pending_approval_for(app, config) is None
    assert len(await gi.interrupt_payloads(app, config)) == 1


async def test_a_resumed_incident_reports_no_pending_approval(ctx: GraphContext) -> None:
    """`None` must mean *not waiting*, so the not-waiting case has to be exercised too."""
    app = _gate_app()
    config = {"configurable": {"thread_id": "resumed"}}
    await app.ainvoke(
        {"incident_id": "INC-1", "status": IncidentStatus.DISPATCH_PLANNING}, config, context=ctx
    )
    payloads = await gi.interrupt_payloads(app, config)
    assert len(payloads) == 1
    assert payloads[0]["value"]["approval_request"]["question"] == "Send a crew to ODP-7?"

    await app.ainvoke(
        Command(
            resume={
                p["id"]: {
                    "status": "approved",
                    "decided_by": "alice",
                    "decided_by_role": "noc_operator",
                }
                for p in payloads
            }
        ),
        config,
        context=ctx,
    )
    assert await gi.is_awaiting_human(app, config) is False
    assert await gi.pending_approval_for(app, config) is None


async def test_the_interrupt_payload_is_json_serialisable(ctx: GraphContext) -> None:
    """A Pydantic model in the payload works in memory and fails against Postgres.

    Asserted with `json.dumps` rather than by round-tripping through the in-memory checkpointer,
    which is exactly the backend that would not catch it.
    """
    import json

    app = _gate_app()
    config = {"configurable": {"thread_id": "json"}}
    await app.ainvoke({"incident_id": "INC-1"}, config, context=ctx)
    payloads = await gi.interrupt_payloads(app, config)
    assert json.loads(json.dumps(payloads[0]["value"]))["permitted_roles"]


async def test_the_asking_node_is_named_in_full_and_the_parents_next_names_only_the_subgraph(
    ctx: GraphContext,
) -> None:
    """Which node is asking, with the flat graph as the control that shows why this is not obvious.

    A console has to say *what* is being approved, and the interrupt cannot tell it: `Interrupt`
    carries an opaque `id` and a `value`, and `from_ns` is a classmethod rather than the namespace
    field it reads as. So the name is taken from the tasks.

    The obvious alternative -- the parent's own `next` -- is asserted here to be insufficient rather
    than merely not used. It reports `("gate",)`, which is the *subgraph*, not the step inside it:
    every real gate would render as "resolution" and no incident would ever say which question it
    was blocked on.

    The flat control is the half that explains how such a bug survives review. Flattened, the naive
    read is **right**, because there is no subgraph to hide behind -- so a reader who tests only the
    arrangement they happened to build sees the parent's `next` work perfectly.

    Seen to go red. Reimplemented as `tuple(root.next)`, the nested case failed and the flat case
    and all 29 other tests in this module passed:

        AssertionError: the paused node must be named from the outside in, subgraph then step
        assert ('gate',) == ('gate', 'ask')
          Right contains one more item: 'ask'

    That asymmetry is what the flat control costs four lines to record.

    The resumed assertion at the end is a **boundary check, not a guard**, and is marked as such
    because the distinction matters in this repo. It could not be made to fail: measured on 1.2.11,
    a completed thread and a never-invoked one both report `next=()` and `tasks=[]`, so no
    implementation of this function distinguishes them and none returns a path for either. It is
    kept for the empty branch it covers, not as evidence of anything.
    """
    nested = _gate_app()
    nested_config = {"configurable": {"thread_id": "path-nested"}}
    await nested.ainvoke({"incident_id": "INC-1"}, nested_config, context=ctx)

    assert await gi.awaiting_node_path(nested, nested_config) == ("gate", "ask"), (
        "the paused node must be named from the outside in, subgraph then step"
    )
    assert (await nested.aget_state(nested_config)).next == ("gate",), (
        "the control: the parent's own `next` names the subgraph and stops there. If this is now "
        "the full path, LangGraph propagates it and `awaiting_node_path` has become a wrapper"
    )

    flat = _gate_app(nested=False)
    flat_config = {"configurable": {"thread_id": "path-flat"}}
    await flat.ainvoke({"incident_id": "INC-1"}, flat_config, context=ctx)

    assert await gi.awaiting_node_path(flat, flat_config) == ("ask",)
    assert (await flat.aget_state(flat_config)).next == ("ask",), (
        "flattened, the naive read is correct -- which is why the nested failure is easy to miss"
    )

    # Boundary, not guard -- see the docstring. Resuming the nested gate must empty the path.
    await nested.ainvoke(
        Command(
            resume={
                p["id"]: {
                    "status": "approved",
                    "decided_by": "op-1",
                    "decided_by_role": "noc_operator",
                }
                for p in await gi.interrupt_payloads(nested, nested_config)
            }
        ),
        nested_config,
        context=ctx,
    )
    assert await gi.awaiting_node_path(nested, nested_config) == ()
    assert await gi.is_awaiting_human(nested, nested_config) is False


# ------------------------------------------------------------------------------------------------
# How a node composes a metric timestamp into an update it is already building
# ------------------------------------------------------------------------------------------------


def test_a_second_stamp_in_one_update_does_not_displace_the_first() -> None:
    """`stamp` merges where `update.update(mark(...))` replaced, and this is the mechanism's owner.

    `mark` returns the whole `{"metrics_timestamps": {...}}` shape, and its docstring explains that
    the shape is what makes `{**other_updates, **mark(...)}` safe. That is true of the literal form
    and false of the other one: `update.update(mark(...))` is a plain `dict.update` on the *outer*
    mapping, so it replaces the `metrics_timestamps` key rather than merging into it. `merge_dict`
    cannot recover the loss -- the reducer merges what a node returned, and the node has already
    dropped it.

    Three sites did exactly that, each writing two stamps into one update and keeping only the
    second. Found by mutation sweep on 2026-08-24; `observability.kpi.stamp` lists them.

    This test exists because the three sites cannot all hold themselves to account.
    `restoration_validation` and `remote_resolution` each have an end-to-end guard that goes red
    when the old idiom is reinstated. `field_execution` does not and cannot: both of its stamps are
    conditional on their own absence from state, so the lap that loses `dispatched_at` re-writes it
    on the next one and the fixture reaches its pause with both present. The end-to-end symptom is
    masked there while the defect is not, so the mechanism is asserted directly instead -- which is
    also the only place the *contrast* between the two idioms can be stated.

    Shown red by reverting `stamp` to `update.update(mark(key, when))`:

        AssertionError: the earlier stamp must survive a later one written into the same update
        assert 'triaged_at' in {'restored_at': '2026-03-02T14:30:00+00:00'}
    """
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    later = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)

    # The literal form, which was always correct, and then a second stamp on top of it.
    update: dict[str, Any] = {
        "status": IncidentStatus.VALIDATING,
        **mark(MetricTimestamp.TRIAGED_AT, now),
    }
    stamp(update, MetricTimestamp.RESTORED_AT, later)

    assert MetricTimestamp.TRIAGED_AT.value in update["metrics_timestamps"], (
        "the earlier stamp must survive a later one written into the same update"
    )
    assert update["metrics_timestamps"] == {
        MetricTimestamp.TRIAGED_AT.value: now.isoformat(),
        MetricTimestamp.RESTORED_AT.value: later.isoformat(),
    }
    assert update["status"] is IncidentStatus.VALIDATING, "nothing else on the update may move"

    # The positive control: the idiom this replaced really did lose it, so the assertions above are
    # about `stamp` doing something rather than about the situation being harmless.
    clobbered: dict[str, Any] = {**mark(MetricTimestamp.TRIAGED_AT, now)}
    clobbered.update(mark(MetricTimestamp.RESTORED_AT, later))
    assert MetricTimestamp.TRIAGED_AT.value not in clobbered["metrics_timestamps"]

    # And an update with no stamps yet is the ordinary case, which must still work.
    fresh: dict[str, Any] = {}
    stamp(fresh, MetricTimestamp.CLOSED_AT, later)
    assert fresh == {"metrics_timestamps": {MetricTimestamp.CLOSED_AT.value: later.isoformat()}}
