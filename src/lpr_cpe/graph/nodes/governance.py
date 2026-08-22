"""The two parent-level approval gates and the escalation D07 asks for.

Five nodes closing three exits that `PENDING_STAGES` had listed as owed:
`D06:approve_low_confidence`, `D07:approve_high_blast_radius` and `D07:escalate`. Five and not three
because `graph.interrupts` makes every gate a pair -- a node that raises `interrupt()` does not
return until the answer is in hand, so it cannot also have recorded that it was waiting.

Why these gates are flat in the parent
--------------------------------------
Every other gate in this system sits inside a subgraph, and `graph.interrupts` explains at length
why that costs a reader something: the parent's `.values` shows `pending_approval` as `None` while
the paused subgraph's shows it set, so a caller has to go through `graph.inspect` to see what is
being asked. These three do not pay that cost, because D06 and D07 are the *parent's* decisions.
There is no subgraph to resume -- the answer is read by a router attached to a parent node -- and
putting the gate in a subgraph purely to preserve a symmetry would checkpoint the question one level
below the decision that consumes it.

`graph.inspect._snapshots` puts the root first, so a flat gate is visible to `pending_approval_for`
without any of the nesting the other six need. `builder._plain_edges` wires these five by their
position in `PARENT_NODES` alone, which is the extension point its docstring anticipates.

What the fixtures do with these arms, measured
----------------------------------------------
**D06's review arm is taken. D07's two are not. This section used to say none of them was.** The
figure it carried -- 134 invocations, 67 of D06 and 67 of D07, every one answering `continue` --
came from routers instrumented at `builder._cascade`'s call site, over a drive that resumed *every
interrupt with an approval*. Only two of the five pause types accept one, so that drive re-asked
the same crew until a re-entry budget stopped it; `docs/workflow-diagram.md` §6 records the cost.

Re-measured with each pause type answered in the shape its own parser accepts, over all 41
services under both crew answers, wrapping `DECISIONS[...].route` rather than the composed edge:

| gate | asks, handover / premises | `continue` | the other arms |
| --- | --- | --- | --- |
| D06 | 138 / 238 | 129 / 229 | `approve_low_confidence`, 9 and 9 |
| D07 | 129 / 229 | 129 / 229 | none, in either sweep |

The nine are `SVC-UT-001-B-01` and the eight `SVC-VQ-002-*`, the same nine under both crew
answers, entering once each. The counts close against node entries rather than standing alone:
D06 is asked after `determine_root_cause` (129 entries) *and* after the gate's own
`request_low_confidence_review` (9), which is the 138.

The mechanism this section already described is what carries the demand, and it is now watched
rather than inferred. Both routers gate on `policy_decisions`, every caller of `ctx.policy.evaluate`
is inside a subgraph downstream of both decisions, so a demand reaches D06 or D07 only second-hand,
carried back by D10's `retry_diagnosis` to P07 or D12's to P10. All nine take the arm on their
*second* ask and none on their first: ask 1 sees no demand of the kind and answers `continue`, ask
2 sees one and answers `approve_low_confidence`, and every ask after that reads the recorded answer
and continues. What was wrong was the generalisation drawn from it. "The observed
`required_approval_kind` at every ask is `None`" holds for `HIGH_BLAST_RADIUS_ACTION` and not for
`LOW_CONFIDENCE_RCA`, which `policies.engine._check_confidence` raises for any non-read-only action
whose RCA sits under its class bar.

`route_rca_confidence`'s other opening does still never fire. `rca is None` was not the clause
behind any of D06's 376 asks, because P10 always produces one -- so the no-RCA half of
`prepare_low_confidence_review`'s question is the part no fixture covers, and
`test_governance_nodes.py` is the only thing holding it.

D07's two arms being reachable and unexercised is precisely the case in which leaving them at `END`
is dangerous rather than merely incomplete: `_check_pending_stages` puts it as "a run that stops
there looks like a run that finished". Until this module existed, an incident whose remedies were
all blocked by policy terminated silently with `escalated` false and the status still `diagnosing`.

The consequence for the tests is that they cannot be fixture-driven. They seed `policy_decisions`
and drive the real parent from there; see `tests/unit/test_governance_nodes.py`.
"""

from __future__ import annotations

from lpr_cpe.domain.enums import ApprovalKind, IncidentStatus, ReasonCode
from lpr_cpe.domain.governance import PolicyDecision
from lpr_cpe.domain.resolution import ResolutionOption
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.interrupts import build_request, prepare_approval, request_approval
from lpr_cpe.graph.nodes._runtime import NodeUpdate, audit, node
from lpr_cpe.graph.routing import latest_decision_of
from lpr_cpe.graph.state import IncidentState, visit_count
from lpr_cpe.observability.kpi import MetricTimestamp, mark


def _standing_demand(state: IncidentState, kind: ApprovalKind) -> PolicyDecision | None:
    """The latest policy decision demanding this approval kind, answered or not.

    By `max(decided_at)` and not by list order, to match `routing.approval_outstanding` exactly.
    That function decides whether the gate is entered; this one decides what it asks. Ordering them
    differently would let the gate ask about one demand and close on another.
    """
    demands = [d for d in state.get("policy_decisions", []) if d.required_approval_kind is kind]
    return max(demands, key=lambda d: d.decided_at, default=None)


def _option_under_review(state: IncidentState, decision: PolicyDecision) -> ResolutionOption | None:
    """The still-untried option the demand was raised against, if the plan still proposes it.

    `None` is a real answer rather than an error: `policy_decisions` is append-only and P11 may have
    redrawn the plan since, so a demand can outlive the option that provoked it. The gate still has
    an action type to ask about, which is what `approval_id_for`'s subject needs.

    The two `None`s are not the same kind of thing, which is why the coverage report shows the first
    uncovered. The `next(..., None)` is the reachable case just described. The `plan is None` return
    above it is unreachable -- D07 sits downstream of `generate_resolution_options`, so nothing that
    gets here has an unset plan -- and it is kept because the declared type says otherwise:
    `state.py`'s `resolution_plan: ResolutionPlan | None` is initialised to `None`, and deleting the
    guard is rejected before it can be run. Shown red by removing it:

        governance.py:81: error: Item "None" of "ResolutionPlan | None" has no attribute "untried"

    So this is a branch the type system requires and no state can enter -- the one shape of dead
    code worth keeping, and the reason it has no test.
    """
    plan = state.get("resolution_plan")
    if plan is None:
        return None
    return next((o for o in plan.untried() if o.action_type is decision.action_type), None)


@node("prepare_low_confidence_review")
async def prepare_low_confidence_review(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write down the question for the L2/SME reviewer, then return so it is committed.

    Reached on either of D06's two review conditions, and it must serve both. `route_rca_confidence`
    answers `approve_low_confidence` when policy has demanded the kind *or* when `rca is None`, and
    only the first of those leaves a `PolicyDecision` behind -- so unlike `remote_resolution`'s gate
    this one cannot insist on finding a demand. A missing RCA is the more serious of the two and the
    question says so.

    `attempt` is this node's own visit count and deliberately not `diagnostic_cycles`. The subject
    handed to `approval_id_for` is empty for a kind with no action type, so for this gate the
    attempt counter is the entire difference between two questions -- and P10, one node upstream,
    records what keying on `diagnostic_cycles` costs on exactly these cycles: D12's
    `retry_diagnosis` returns to P10 without passing P07, so the counter does not move, two runs
    derive the same id, and `append_unique` keeps the first. Measured there on `SVC-SJ-011-A-01`,
    both `AUD-0dafcf9872465b830461`. A visit count moves on every lap by construction.
    """
    rca = state.get("rca")
    demand = _standing_demand(state, ApprovalKind.LOW_CONFIDENCE_RCA)
    attempt = visit_count(state, "prepare_low_confidence_review") + 1

    if rca is None:
        question = (
            "P10 reached no root cause for this incident. Accept the diagnosis as it stands, or "
            f"reject to send it back for more evidence? This is review {attempt}."
        )
        risk = "No RCA was produced, so there is no confidence figure to weigh."
    else:
        question = (
            f"P10 puts the fault in {rca.fault_domain.value} at confidence "
            f"{rca.confidence:.2f}, below the bar for acting on it unreviewed. Accept this "
            f"root cause? This is review {attempt}."
        )
        risk = demand.explanation if demand is not None else rca.summary

    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.LOW_CONFIDENCE_RCA,
        question=question,
        attempt=attempt,
        recommendation=rca.summary if rca is not None else "",
        risk_summary=risk,
        policy_decision_id=demand.decision_id if demand is not None else None,
        context={
            "rca_present": rca is not None,
            "confidence": rca.confidence if rca is not None else None,
            "fault_domain": rca.fault_domain.value if rca is not None else None,
            "delimiter_kind": rca.delimiter_kind.value if rca is not None else None,
            "hypotheses": [
                {"hypothesis_id": h.hypothesis_id, "posterior": h.posterior}
                for h in (rca.hypotheses if rca is not None else [])
            ],
            "diagnostic_cycles": state.get("diagnostic_cycles", 1),
            "policy_reason_codes": (
                [code.value for code in demand.reason_codes] if demand is not None else []
            ),
        },
    )
    return {
        **prepare_approval(state, ctx, request),
        **mark(MetricTimestamp.APPROVAL_REQUESTED_AT, request.requested_at),
        "audit_events": [
            audit(
                state,
                ctx,
                node="prepare_low_confidence_review",
                action="request_approval",
                outcome="awaiting_approval",
                reason_code=ReasonCode.RCA_LOW_CONFIDENCE,
                detail={
                    "approval_id": request.approval_id,
                    "attempt": attempt,
                    "rca_present": rca is not None,
                    "required_role": request.required_role,
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_low_confidence_review")
async def request_low_confidence_review(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt, record the answer, and put the status back to `diagnosing`.

    The status write is the one thing this gate does that `remote_resolution`'s does not, and the
    difference is in what comes next. There, both arms lead to a node that writes a status of its
    own -- the executor on approval, `abandon_remote_action` on refusal. Here, D06's `continue` arm
    leads to P11, which writes no status at all, so the incident would carry `awaiting_approval`
    through P11, D07, D08 and D09 until some subgraph finally corrected it. That is the same lie
    `prepare_approval` and `request_approval` were split apart to avoid, arriving from the other
    end: a checkpoint claiming to wait on a decision already in hand.

    `diagnosing` is honest on both arms. Approved, the incident has an accepted root cause and is
    choosing a remedy; refused, D06 sends it to P07, which writes `diagnosing` itself anyway.
    """
    return {**request_approval(state, ctx), "status": IncidentStatus.DIAGNOSING}


@node("prepare_blast_radius_approval")
async def prepare_blast_radius_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write down the question for the supervisor who must sign off a wide-reaching action.

    Unlike D06's gate this one may insist on a demand, because `route_safety_and_blast_radius`
    reaches it on `approval_outstanding` alone and that is false when no decision requires the kind.
    Arriving without one means an edge leads here that should not, and a gate that quietly invented
    a question would hide it.

    The option may still be missing, and `_option_under_review` says why that is tolerated. Its
    absence costs the question its target and blast figure, not its identity: `approval_id_for`'s
    subject is built from the action type, which the decision always carries.
    """
    demand = _standing_demand(state, ApprovalKind.HIGH_BLAST_RADIUS_ACTION)
    if demand is None:
        raise ValueError(
            "the blast-radius gate was reached with no policy decision demanding "
            "HIGH_BLAST_RADIUS_ACTION. Only D07's `approve_high_blast_radius` arm may lead here, "
            "and `routing.approval_outstanding` answers it by finding exactly such a decision."
        )

    option = _option_under_review(state, demand)
    attempt = visit_count(state, "prepare_blast_radius_approval") + 1
    blast = option.blast_radius if option is not None else None
    subject = option.label.lower() if option is not None else demand.action_type.value

    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.HIGH_BLAST_RADIUS_ACTION,
        question=(
            f"Approve {subject}"
            + (f" on {option.target_ref}" if option is not None else "")
            + (f"? It affects {blast} services" if blast is not None else "?")
            + f". This is request {attempt} for this action."
        ),
        attempt=attempt,
        action_type=demand.action_type,
        target_ref=option.target_ref if option is not None else None,
        recommendation=(option.rationale or option.label) if option is not None else "",
        risk_summary=demand.explanation,
        blast_radius=blast,
        reversible=option.reversible if option is not None else True,
        policy_decision_id=demand.decision_id,
        context={
            "option_still_proposed": option is not None,
            "matched_rule": demand.matched_rule,
            "policy_version": demand.policy_version,
            "policy_reason_codes": [code.value for code in demand.reason_codes],
            "customer_disruption": option.customer_disruption if option is not None else None,
            "risk_class": option.risk if option is not None else "",
        },
    )
    return {
        **prepare_approval(state, ctx, request),
        **mark(MetricTimestamp.APPROVAL_REQUESTED_AT, request.requested_at),
        "audit_events": [
            audit(
                state,
                ctx,
                node="prepare_blast_radius_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=option.target_ref if option is not None else None,
                reason_code=ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,
                detail={
                    "approval_id": request.approval_id,
                    "attempt": attempt,
                    "action_type": demand.action_type.value,
                    "blast_radius": blast,
                    "required_role": request.required_role,
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_blast_radius_approval")
async def request_blast_radius_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Restores `diagnosing` for D06's reason.

    Both of D07's onward arms need it. Approved, the run continues into the D08/D09 chain, and D08's
    plant arm still ends at `END` -- an incident parked there under `awaiting_approval` would claim
    to be waiting on an answer it has. Refused, `record_escalation` moves it to `escalated`, and
    `awaiting_approval -> escalated` is a legal transition, so this write is not load-bearing on
    that arm; it is written unconditionally because the node cannot see which arm it is on.
    """
    return {**request_approval(state, ctx), "status": IncidentStatus.DIAGNOSING}


@node("record_escalation")
async def record_escalation(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Hand the incident to a human and say why. D07's third remedy.

    Not `guards.escalation_update`, which is the closest thing already written and is the wrong
    tool: it takes a `BudgetVerdict`, raises on a passing one and stamps `LOOP_LIMIT_REACHED`. This
    escalation is not a spent budget. It is an incident with a root cause and no permitted remedy --
    `ESCALATED_TO_HUMAN` -- and manufacturing a failing verdict to reach the shared helper would put
    a loop-limit reason code on a case that never looped.

    `escalated` is set and the status moves, following that helper's doctrine rather than its code:
    the machine stops, the case does not close. `IncidentStatus.ESCALATED` moves onward to nine
    other statuses precisely so a supervisor who takes the incident over can resume the thread.

    The two causes are distinguished because the remedies differ. A refused approval is a decision
    somebody made and can revisit; every candidate blocked is a policy state that will refuse the
    same actions again until the pack or the plan changes.
    """
    answer = latest_decision_of(state, ApprovalKind.HIGH_BLAST_RADIUS_ACTION)
    blocked = [d for d in state.get("policy_decisions", []) if d.blocked]
    now = ctx.clock.now()

    if answer is not None and not answer.granted:
        outcome = "approval_refused"
        reason_code = ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE
        reason = (
            f"{answer.decided_by or 'a reviewer'} refused the high-blast-radius approval: "
            f"{answer.rationale}"
        )
    else:
        actions = sorted({d.action_type.value for d in blocked})
        codes = [code for d in blocked for code in d.reason_codes]
        outcome = "blocked_by_policy"
        reason_code = codes[0] if codes else ReasonCode.ESCALATED_TO_HUMAN
        reason = (
            "policy blocks every remedy still on the table"
            + (f" ({', '.join(actions)})" if actions else "")
            + ". No action can be taken without a human decision."
        )

    return {
        "escalated": True,
        "escalation_reason": reason,
        "status": IncidentStatus.ESCALATED,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="record_escalation",
                action="escalate",
                outcome=outcome,
                reason_code=reason_code,
                detail={
                    "cause": outcome,
                    "blocked_actions": sorted({d.action_type.value for d in blocked}),
                    "refused_by": answer.decided_by if answer is not None else None,
                    "resolution_cycles": state.get("resolution_cycles", 0),
                },
                discriminator=visit_count(state, "record_escalation"),
            )
        ],
    }


#: The five nodes, in the order `builder._plain_edges` reads them.
#:
#: Order is load-bearing here in a way it is not for the other three registries, because
#: `_plain_edges` draws an edge between every consecutive pair whose left member is not in
#: `DECISION_AFTER`. Both `request_` nodes are in that table, which is what stops an edge being
#: drawn from either of them to whatever follows, and leaves exactly the two `prepare -> request`
#: joins this gate design needs. `record_escalation` is last and so is read as terminal.
GOVERNANCE_NODES: tuple[tuple[str, object], ...] = (
    ("prepare_low_confidence_review", prepare_low_confidence_review),
    ("request_low_confidence_review", request_low_confidence_review),
    ("prepare_blast_radius_approval", prepare_blast_radius_approval),
    ("request_blast_radius_approval", request_blast_radius_approval),
    ("record_escalation", record_escalation),
)


__all__ = [
    "GOVERNANCE_NODES",
    "prepare_blast_radius_approval",
    "prepare_low_confidence_review",
    "record_escalation",
    "request_blast_radius_approval",
    "request_low_confidence_review",
]
