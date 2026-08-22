"""D06's and D07's own five nodes, driven through the real parent graph.

Why this module seeds state instead of picking a fixture
--------------------------------------------------------
Every other stage in this suite is exercised by finding a service that reaches it. **Two of these
three arms are reached by no service. The third is, and this module used to say otherwise.** The
old claim rested on instrumenting `route_rca_confidence` and `route_safety_and_blast_radius` at
`builder._cascade`'s call site over a drive that answered two of the five pause types, which put
D06 and D07 at "134 times between them and `continue` every single time".

Re-swept with each pause type answered in the shape its own parser accepts, D06 answers
`approve_low_confidence` for nine of the 41 services under either crew answer, while D07 answers
`continue` at all 358 of its asks. So seeding is still the only way to reach
`prepare_blast_radius_approval` and `record_escalation`, and it is no longer the only way to reach
`prepare_low_confidence_review` -- the fixtures corroborate that pair, and the tests below pin the
behaviour that a corpus-wide sweep is far too slow to assert. `graph.nodes.governance` has the
figures.

The arms are not dead, and `graph.nodes.governance` sets out why at length: both routers gate on
`policy_decisions`, every writer of that field is inside a subgraph *downstream* of both decisions,
and the retry arms carry a run back upstream of them. That is exactly how the nine arrive: each
takes the arm on its second D06 ask and never on its first. What the corpus never produces is a
decision of the kind D07 reads.

So the demand is seeded. `_seeded` runs the real parent up to a real node with `interrupt_after`,
calls `aupdate_state` to append one `PolicyDecision`, and resumes. Nothing about the graph is
stubbed: the routers, the gate pair, the interrupt, the checkpointer and the resume channel are all
production code, and the seeded record is the sort of row `policies.engine` writes.

That technique rests on a property of langgraph 1.2.11 worth writing down, because the tests below
are meaningless without it. `aupdate_state` **re-evaluates the outgoing branch**. Paused after
`determine_root_cause` the snapshot reports `next == ('generate_resolution_options',)`; appending the
demand and reading it again reports `next == ('prepare_low_confidence_review',)`, with no node having
run in between. The seed is therefore doing what a policy evaluation upstream would have done, not
smuggling the run past the router.

The other half of the parent graph's gate design
------------------------------------------------
These two gates are flat -- in the parent, not in a subgraph -- and `graph.interrupts` describes the
trap that creates. Measured here, paused at D06's gate::

    tasks == [('request_low_confidence_review', state is None)]

There is no child snapshot to read through, so code written for the other four gates that reaches
for `.tasks[0].state.values` finds nothing. `test_the_flat_gate_needs_no_subgraph_to_be_visible`
pins both halves of that: what works and what would not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import lpr_cpe.graph.inspect as gi
from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    CaseType,
    EventSource,
    IncidentStatus,
    PolicyOutcome,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.governance import PolicyDecision
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.nodes.governance import prepare_blast_radius_approval
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.observability.kpi import MetricTimestamp

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: Any service that runs the whole linear chain to P11 will do; this one is the first
#: `hfc_degraded_upstream`, diagnosed `tap_or_odp`, and its plan offers `raise_mr` and
#: `create_work_order`. Both matter: the blast-radius gate needs an *untried* option whose action
#: type it can name, and `test_policy_blocking_every_remedy...` needs to block more than one.
HEALTH = "hfc_degraded_upstream"

APPROVE = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the reading is thin but the remedy is reversible",
}

REFUSE = {
    "status": "rejected",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "too many services at risk during the evening peak",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock `test_builder.py` explains. Same reason, same subclassing."""

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


def _demand(
    kind: ApprovalKind,
    action: ActionType = ActionType.RAISE_MR,
    *,
    outcome: PolicyOutcome = PolicyOutcome.REQUIRES_APPROVAL,
    at: datetime = NOW,
) -> PolicyDecision:
    """One row of the sort `policies.engine` writes, shaped for whichever gate is under test."""
    return PolicyDecision(
        decision_id=f"POL-{kind.value if kind else outcome.value}-{action.value}",
        decided_at=at,
        action_type=action,
        outcome=outcome,
        reason_codes=(ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,),
        explanation="the proposed action reaches more services than the pack permits unreviewed",
        policy_version="test-pack",
        matched_rule="blast_radius.services",
        required_approval_kind=kind,
        required_role="noc_supervisor",
    )


async def _seeded(fixtures: Any, after: str, thread: str, **seed: Any) -> Any:
    """Run the parent to `after`, write `seed` into the checkpoint, and resume one leg.

    Returns `(app, ctx, config)` with the graph wherever the seeded state sends it. `after` is a real
    node and `interrupt_after` is LangGraph's own pause, so nothing here bypasses a router.
    """
    service = next(s for s in fixtures.services.values() if s["health"] == HEALTH)
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    app = build_parent_graph().compile(
        name="lpr_cpe_parent", checkpointer=InMemorySaver(), interrupt_after=[after]
    )
    config: Any = {"configurable": {"thread_id": thread}}
    await app.ainvoke(_initial(service), context=ctx, config=config)
    await app.aupdate_state(config, seed)
    await app.ainvoke(None, context=ctx, config=config)
    return app, ctx, config


@pytest.fixture
async def at_rca_gate(fixtures: Any) -> Any:
    """Paused at D06's gate on a policy demand, which is the arm with a `PolicyDecision` behind it."""
    return await _seeded(
        fixtures,
        "determine_root_cause",
        "gov-d06",
        policy_decisions=[_demand(ApprovalKind.LOW_CONFIDENCE_RCA)],
    )


@pytest.fixture
async def at_blast_gate(fixtures: Any) -> Any:
    """Paused at D07's gate. Seeded after P11 so the plan exists for the question to name."""
    return await _seeded(
        fixtures,
        "generate_resolution_options",
        "gov-d07",
        policy_decisions=[_demand(ApprovalKind.HIGH_BLAST_RADIUS_ACTION)],
    )


# ------------------------------------------------------------------------------------------------
# D06 -- the low-confidence RCA review
# ------------------------------------------------------------------------------------------------


async def test_a_policy_demand_diverts_p10_into_the_gate_pair_and_stops_there(
    at_rca_gate: Any,
) -> None:
    """The whole reason there are two nodes and not one.

    `prepare_low_confidence_review` has *run* and `request_low_confidence_review` has *not*, and the
    checkpoint between them is the point: `pending_approval` and `awaiting_approval` are committed
    while the graph is stopped. A single node calling `interrupt()` could not have written either,
    because it does not return until the answer is in hand -- so an incident sitting on somebody's
    queue would be indistinguishable from one mid-computation.
    """
    app, _ctx, config = at_rca_gate
    snapshot = await app.aget_state(config)

    assert snapshot.next == ("request_low_confidence_review",)
    assert snapshot.values["node_visits"]["prepare_low_confidence_review"] == 1
    assert "request_low_confidence_review" not in snapshot.values["node_visits"]
    assert "generate_resolution_options" not in snapshot.values["node_visits"], (
        "D06 diverted the run; P11 must not have run yet"
    )

    assert snapshot.values["status"] is IncidentStatus.AWAITING_APPROVAL
    assert snapshot.values["pending_approval"] is not None
    assert len(snapshot.interrupts) == 1


async def test_the_flat_gate_needs_no_subgraph_to_be_visible(at_rca_gate: Any) -> None:
    """The mirror image of the trap `graph.interrupts` documents, and the reason it says "four".

    For the four nested gates the parent's own `.values` understates the incident -- at the real
    dispatch gate `status` reads `diagnosing` while the paused child reads `awaiting_approval` -- so
    a caller must go through `graph.inspect`. These two are the opposite case and it is asserted
    here so the docstring in `interrupts` is not the only record of it: the parent's `.values` is
    complete, and the interrupted task has **no child state at all**::

        tasks == [('request_low_confidence_review', state is None)]

    Both are asserted, and the second is the one that matters. Code generalised from the nested
    gates that reaches for `.tasks[0].state.values` gets `None` here, not a fallback.

    `pending_approval_for` is asserted to agree with the raw read rather than trusted to. It walks
    `_snapshots` innermost-first, and for a flat gate that list is one long -- so the equality is
    what shows the supported reader works either way, which is what makes it safe to keep telling
    callers to use it.
    """
    app, _ctx, config = at_rca_gate
    snapshot = await app.aget_state(config, subgraphs=True)

    assert [(t.name, getattr(t, "state", None)) for t in snapshot.tasks] == [
        ("request_low_confidence_review", None)
    ]

    raw = snapshot.values["pending_approval"]
    found = await gi.pending_approval_for(app, config)
    assert found is not None and found.approval_id == raw.approval_id
    assert (await gi.effective_state(app, config))["status"] is IncidentStatus.AWAITING_APPROVAL
    assert await gi.awaiting_node_path(app, config) == ("request_low_confidence_review",)
    assert await gi.is_awaiting_human(app, config) is True


async def test_the_question_names_the_confidence_and_the_policy_that_objected(
    at_rca_gate: Any,
) -> None:
    """What the reviewer is shown. An approval request nobody can act on is not a gate.

    `policy_decision_id` ties the question back to the row that demanded it, which is the join a
    reviewer needs to see *why* this is being asked rather than merely *what*. `risk_summary` is the
    policy's own explanation for the same reason -- restating it in this node's words would be a
    second copy of the pack's reasoning, drifting from the first.

    `required_role` is the assertion that surprised. The seeded `PolicyDecision` carries
    `required_role="noc_supervisor"` and the request comes back `noc_operator`, because
    `interrupts.build_request` takes the role from `ctx.policy.pack.approvals[kind]` and not from
    the decision -- deliberately, per its own docstring: a caller that could name the role could
    make a high-blast-radius approval answerable by anyone. So the *pack* decides who may answer a
    kind of question, and a policy row that disagrees is ignored rather than obeyed. It is asserted
    against the seeded value on purpose; the two differ, so this cannot pass by coincidence.
    """
    app, _ctx, config = at_rca_gate
    request = await gi.pending_approval_for(app, config)
    assert request is not None

    assert request.kind is ApprovalKind.LOW_CONFIDENCE_RCA
    assert request.required_role == "noc_operator"
    assert request.policy_decision_id == "POL-low_confidence_rca-raise_mr"
    assert request.risk_summary.startswith("the proposed action reaches more services")
    assert "below the bar for acting on it unreviewed" in request.question
    assert "This is review 1." in request.question

    assert request.context["rca_present"] is True
    assert 0.0 < request.context["confidence"] < 1.0
    assert request.context["policy_reason_codes"] == ["POLICY_BLAST_RADIUS_EXCEEDED"]

    values = (await app.aget_state(config)).values
    assert MetricTimestamp.APPROVAL_REQUESTED_AT.value in values["metrics_timestamps"]


async def test_a_missing_rca_opens_the_same_gate_with_a_different_question(
    fixtures: Any,
) -> None:
    """D06's second opening, and the only one that leaves no `PolicyDecision` behind.

    `route_rca_confidence` answers `approve_low_confidence` on `rca is None` as well as on a policy
    demand, which is why `prepare_low_confidence_review` cannot insist on finding a demand the way
    the blast-radius gate can. No fixture produces it -- P10 always reaches a root cause -- so the
    RCA is cleared through the checkpoint, which is the same seam the demand is seeded through and
    exercises the same router.

    The empty `recommendation` is asserted deliberately. There is no summary to recommend, and a
    gate that filled the field with a placeholder would put words in the diagnostic's mouth on the
    one occasion the diagnostic said nothing.
    """
    app, _ctx, config = await _seeded(fixtures, "determine_root_cause", "gov-d06-norca", rca=None)
    request = await gi.pending_approval_for(app, config)
    assert request is not None

    assert request.question.startswith("P10 reached no root cause for this incident.")
    assert request.risk_summary == "No RCA was produced, so there is no confidence figure to weigh."
    assert request.recommendation == ""
    assert request.action_type is None
    assert request.policy_decision_id is None
    assert request.context["rca_present"] is False
    assert request.context["confidence"] is None
    assert request.context["policy_reason_codes"] == []


async def test_an_approved_rca_carries_on_to_p11_no_longer_awaiting_anything(
    at_rca_gate: Any,
) -> None:
    """Approved, the incident continues -- and stops claiming to be waiting.

    The status write in `request_low_confidence_review` is the assertion here, and it is the one
    thing this gate does that `remote_resolution`'s does not. There, both arms lead to a node that
    writes a status of its own. Here, D06's `continue` arm leads to P11, which writes none, so
    without the restore the incident would carry `awaiting_approval` through P11, D07, D08 and D09
    -- a checkpoint claiming to wait on a decision already in hand.

    Shown red by deleting `"status": IncidentStatus.DIAGNOSING` from `request_low_confidence_review`.
    One test in the whole suite goes red, and it is this one::

        E   AssertionError: assert <IncidentStatus.AWAITING_APPROVAL: 'awaiting_approval'>
            is <IncidentStatus.DIAGNOSING: 'diagnosing'>

    That nothing else notices is the finding, not a footnote: the lie is invisible to every
    structural test and to every other run, and it would have reached an operator's console as an
    incident sitting on an approval queue that nobody was being asked anything about.
    """
    app, ctx, config = at_rca_gate
    final = await app.ainvoke(Command(resume=APPROVE), context=ctx, config=config)

    assert final["status"] is IncidentStatus.DIAGNOSING
    assert final["pending_approval"] is None
    assert final["escalated"] is False
    assert final["node_visits"]["generate_resolution_options"] == 1

    assert len(final["approvals"]) == 1
    answer = final["approvals"][0]
    assert answer.kind is ApprovalKind.LOW_CONFIDENCE_RCA
    assert answer.status is ApprovalStatus.APPROVED
    assert answer.decided_by == "sofia.reyes"


async def test_a_refused_rca_goes_back_for_evidence_rather_than_forward(
    at_rca_gate: Any,
) -> None:
    """A rejected low-confidence RCA returns to P07, and the loop closes rather than spinning.

    Two claims. The first is `route_rca_confidence`'s: refusing means the analysis is not good
    enough to act on, so the only branch that respects the refusal is going back for more evidence
    -- P07 through P10 each run a second time.

    The second is termination, and it is the one this wiring could plausibly have got wrong.
    `request_low_confidence_review` is in `DECISION_AFTER` under D06, so it re-asks the very
    decision that sent the run to it, and D06's `approve_low_confidence` arm points back at
    `prepare_low_confidence_review`. That is a cycle in the compiled graph. It closes because
    `request_approval` writes the answer into `approvals` and the router consults
    `latest_decision_of` *before* re-testing what opened the gate -- so the second pass answers
    `retry_diagnosis`, and the gate is visited exactly once.
    """
    app, ctx, config = at_rca_gate
    final = await app.ainvoke(Command(resume=REFUSE), context=ctx, config=config)

    visits = final["node_visits"]
    assert visits["assemble_case_evidence"] == 2
    assert visits["determine_root_cause"] == 2
    assert visits["prepare_low_confidence_review"] == 1, "the answered gate must not re-open"
    assert visits["request_low_confidence_review"] == 1

    assert final["status"] is IncidentStatus.DIAGNOSING
    assert final["escalated"] is False
    assert final["approvals"][0].status is ApprovalStatus.REJECTED


# ------------------------------------------------------------------------------------------------
# D07 -- the blast-radius approval
# ------------------------------------------------------------------------------------------------


async def test_the_blast_radius_question_names_the_option_the_policy_objected_to(
    at_blast_gate: Any,
) -> None:
    """The demand carries an action type; the plan carries the option. The question needs both.

    `_option_under_review` matches them on `action_type` over `plan.untried()`, so the supervisor is
    told what will actually happen -- the target and the blast figure come from the option, not from
    the policy row, which knows only the class of action.
    """
    app, _ctx, config = at_blast_gate
    snapshot = await app.aget_state(config)
    assert snapshot.next == ("request_blast_radius_approval",)

    request = await gi.pending_approval_for(app, config)
    assert request is not None
    assert request.kind is ApprovalKind.HIGH_BLAST_RADIUS_ACTION
    assert request.action_type is ActionType.RAISE_MR
    assert request.target_ref is not None
    assert request.blast_radius is not None and request.blast_radius > 0
    assert f"It affects {request.blast_radius} services" in request.question
    assert "This is request 1 for this action." in request.question

    assert request.context["option_still_proposed"] is True
    assert request.context["matched_rule"] == "blast_radius.services"
    assert request.context["policy_version"] == "test-pack"


async def test_an_approved_action_re_enters_the_cascade_rather_than_a_node_after_it(
    at_blast_gate: Any,
) -> None:
    """Releasing D07's gate resumes the four-question chain, not just D07.

    `request_blast_radius_approval` sits in `DECISION_AFTER` under D07, and D07 is the head of the
    cascade `_cascade` composes -- so its `continue` is consumed to ask D08, whose `continue` asks
    D09, and so on. One node's edge carries the whole remaining fork, which is why no node was
    written to sit between the gate and the rest of the chain.

    Measured rather than asserted structurally: this fixture's plan is two truck-roll options, so
    D08 forwards, D09 and D11 both decline the remote and self-help classes, and the run lands in
    `field_planning` -- where that subgraph's *own* gate stops it. That last pause is the nested
    case, and `pending_approval` reading `None` on the parent while the graph is stopped is
    precisely the asymmetry `test_the_flat_gate_needs_no_subgraph_to_be_visible` shows the other
    side of.
    """
    app, ctx, config = at_blast_gate
    await app.ainvoke(Command(resume=APPROVE), context=ctx, config=config)
    snapshot = await app.aget_state(config, subgraphs=True)

    assert snapshot.values["node_visits"]["request_blast_radius_approval"] == 1
    assert snapshot.values["status"] is IncidentStatus.DIAGNOSING
    assert snapshot.values["escalated"] is False
    assert snapshot.values["approvals"][0].status is ApprovalStatus.APPROVED

    assert snapshot.next == ("field_planning",)
    assert snapshot.values["pending_approval"] is None
    assert await gi.pending_approval_for(app, config) is not None, (
        "the parent's own copy is None because a *nested* gate now holds the question; the "
        "supported reader must still find it"
    )


async def test_a_refused_blast_radius_approval_escalates_and_names_who_refused(
    at_blast_gate: Any,
) -> None:
    """D07's second remedy. The incident stops, the case does not close.

    `escalated` and the status move together, following `guards.escalation_update`'s doctrine
    without reusing its code: that helper takes a `BudgetVerdict` and stamps `LOOP_LIMIT_REACHED`,
    and this escalation is not a spent budget. Manufacturing a failing verdict to reach the shared
    helper would put a loop-limit reason code on a case that never looped.
    """
    app, ctx, config = at_blast_gate
    final = await app.ainvoke(Command(resume=REFUSE), context=ctx, config=config)

    assert final["escalated"] is True
    assert final["status"] is IncidentStatus.ESCALATED
    assert final["escalation_reason"] == (
        "sofia.reyes refused the high-blast-radius approval: "
        "too many services at risk during the evening peak"
    )
    assert final["node_visits"]["record_escalation"] == 1

    event = next(e for e in final["audit_events"] if e.node == "record_escalation")
    assert event.action == "escalate"
    assert event.outcome == "approval_refused"
    assert event.detail["refused_by"] == "sofia.reyes"
    assert event.detail["blocked_actions"] == [], "a refusal is not a block; the lists differ"


async def test_policy_blocking_every_remedy_escalates_without_asking_anyone(
    fixtures: Any,
) -> None:
    """D07's other route to the same node, and the one that reaches it with no gate in between.

    `blocked` is a refusal no approval payload can override, so there is nothing to ask -- the run
    goes straight from P11 to `record_escalation`, and `route_safety_and_blast_radius` says why the
    two are told apart: `requires_approval` is a question a supervisor can answer.

    The two causes are distinguished in the audit trail because the remedies differ. A refused
    approval is a decision somebody made and can revisit; every candidate blocked is a policy state
    that will refuse the same actions again until the pack or the plan changes. Both actions are
    named, which is what makes the escalation actionable rather than a shrug -- and it takes *both*
    being blocked, because `_candidate_decisions` requires `all(...)`.
    """
    service = next(s for s in fixtures.services.values() if s["health"] == HEALTH)
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    app = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    config: Any = {"configurable": {"thread_id": "gov-blocked"}}
    await app.ainvoke(_initial(service), context=ctx, config=config)

    untried = (await app.aget_state(config)).values["resolution_plan"].untried()
    assert len(untried) == 2, "this fixture is chosen for offering more than one remedy to block"
    await app.aupdate_state(
        config,
        {
            "policy_decisions": [
                _demand(kind=None, action=o.action_type, outcome=PolicyOutcome.BLOCKED)
                for o in untried
            ]
        },
    )
    assert (await app.aget_state(config)).next == ("record_escalation",), (
        "no gate stands between P11 and the escalation when there is nothing to ask"
    )

    final = await app.ainvoke(None, context=ctx, config=config)

    assert final["escalated"] is True
    assert final["status"] is IncidentStatus.ESCALATED
    assert final["escalation_reason"] == (
        "policy blocks every remedy still on the table (create_work_order, raise_mr). "
        "No action can be taken without a human decision."
    )
    assert final["approvals"] == [], "nobody was asked, so nobody answered"
    assert "prepare_blast_radius_approval" not in final["node_visits"]

    event = next(e for e in final["audit_events"] if e.node == "record_escalation")
    assert event.outcome == "blocked_by_policy"
    assert event.detail["blocked_actions"] == ["create_work_order", "raise_mr"]
    assert event.detail["refused_by"] is None


async def test_the_blast_radius_gate_refuses_to_invent_a_question(fixtures: Any) -> None:
    """Reached with no demand behind it, the gate raises rather than asking something plausible.

    Unlike D06's gate this one may insist, because `route_safety_and_blast_radius` reaches it on
    `approval_outstanding` alone and that is false when no decision requires the kind. So arriving
    here without one means an edge leads somewhere it should not, and a gate that quietly
    substituted a generic question would turn a wiring defect into a supervisor being asked to
    approve an action nobody proposed.

    Driven through `__wrapped__` because there is no state the *graph* can be in that reaches this
    node without a demand -- which is the point of the guard, and also why driving the node directly
    is the only way to test the claim.

    Shown red by deleting the `raise`. The node then runs on four lines further before falling over
    on the same `None`::

        >   subject = option.label.lower() if option is not None else demand.action_type.value
        E   AttributeError: 'NoneType' object has no attribute 'action_type'

        src\\lpr_cpe\\graph\\nodes\\governance.py:205: AttributeError

    which is the whole argument for the guard: same defect, but reported as an attribute error deep
    in a question-building expression rather than as the wiring mistake it is.
    """
    service = next(s for s in fixtures.services.values() if s["health"] == HEALTH)
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    state = _initial(service)

    with pytest.raises(ValueError, match="no policy decision demanding"):
        await prepare_blast_radius_approval.__wrapped__(state, ctx)


async def test_the_gate_asks_about_the_latest_demand_and_not_the_first(fixtures: Any) -> None:
    """`_standing_demand` takes `max(decided_at)`, matching `routing.approval_outstanding` exactly.

    `policy_decisions` is append-only, so an incident that looped carries every demand it ever
    raised. `approval_outstanding` decides whether the gate is *entered* by comparing the latest
    demand's timestamp against the latest answer's; this decides what the gate *asks*. Ordering them
    differently -- by list position, say -- would let the gate ask about a stale demand and then
    close on a fresh one, and the reviewer would have answered a question about the wrong action.

    Seeded newest-first so list order and timestamp order disagree. Shown red by replacing
    `_standing_demand`'s `max(demands, key=...)` with `demands[-1]`, which is the reading a hurried
    reviewer would call equivalent::

        E   AssertionError: assert 'POL-earlier' == 'POL-later'

    Only this test notices. Every other assertion in the module seeds one demand, where the two
    readings agree.
    """
    later = _demand(ApprovalKind.HIGH_BLAST_RADIUS_ACTION, at=NOW + timedelta(minutes=5))
    earlier = _demand(ApprovalKind.HIGH_BLAST_RADIUS_ACTION, at=NOW - timedelta(minutes=5))
    assert later.decision_id == earlier.decision_id, (
        "same id by construction; only `decided_at` differs, so `append_unique` would drop the "
        "second -- which is why they are told apart below by timestamp and not by identity"
    )

    app, _ctx, config = await _seeded(
        fixtures,
        "generate_resolution_options",
        "gov-d07-latest",
        policy_decisions=[
            later.model_copy(update={"decision_id": "POL-later", "explanation": "the fresh one"}),
            earlier.model_copy(update={"decision_id": "POL-earlier", "explanation": "superseded"}),
        ],
    )

    request = await gi.pending_approval_for(app, config)
    assert request is not None
    assert request.policy_decision_id == "POL-later"
    assert request.risk_summary == "the fresh one"
