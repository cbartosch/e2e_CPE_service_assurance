"""The six approval gates, and the one function that raises all of them.

The specification names six moments a human must be asked: low-confidence RCA, high-risk remote
action, dispatch, clean-to-dirty handover, high-blast-radius action, and exceptional closure. They
differ in who may answer and how long the question stands, and in nothing else -- so there is one
`request_approval` and six thin callers, rather than six near-copies that drift apart.

Three rules hold at every gate.

**A gate asks; it does not act.** Everything before `interrupt()` in a node runs again on resume
(measured in `tests/unit/test_langgraph_replay_contract.py`), so a gate that performed the action it
was asking about would perform it twice -- once before anyone answered. The action belongs to a
separate downstream node carrying an idempotency key. This is D3, and it is the reason these
functions return an `ApprovalDecision` and never touch an adapter.

**An answer is checked, not trusted.** The resume value arrives from an HTTP endpoint. A role that
may not approve this kind does not approve it by saying so: `can_approve` is consulted and a refusal
is recorded as a rejection with a rationale, not silently dropped and not raised as an exception --
the incident must stay resumable so the right person can answer. `AUTOMATION` fails this check by
design, which is what stops the graph from approving itself.

**The question is deterministic.** `approval_id` is derived from the incident, the kind and the
attempt, never from `uuid4`. `approvals` is de-duplicated on that id by `append_unique`, so a
replayed gate collapses into the one decision it already recorded instead of appearing to have been
answered twice.

Why asking takes two nodes
--------------------------
A node writes state only by returning, and a gate does not return until it is answered. So a single
gate node cannot record that it *is* waiting -- the earliest it could write `status =
AWAITING_APPROVAL` is after the answer arrives, at which point the field is a lie: the state would
claim to be waiting for a decision it is holding. The first draft did exactly that.

So the pair is `prepare_approval` then `request_approval`:

* `prepare_approval` builds the question, writes it to `pending_approval` and moves the status to
  `AWAITING_APPROVAL`. It returns immediately, so both are committed *before* anyone is asked.
* `request_approval` reads that same request back out of state and raises the interrupt. It builds
  nothing.

Reading the request back rather than rebuilding it is what makes the pair replay-safe. Both nodes
would otherwise call `clock.now()` and produce two different `requested_at` values for one question
-- and the gate re-runs on resume, so it would produce a third. Built once, read twice.

While the question stands it is visible in two places, and they are not duplicates.
`pending_approval` in state is *that a question is outstanding*, which survives into the checkpoint
and drives routing; `aget_state(config).interrupts[i].value` is *the payload the operator is being
shown*, which LangGraph owns and discards on resume.

Where the parent cannot see it
------------------------------
`prepare_approval` commits before the pause -- but only into the **subgraph's** state. Measured on
langgraph 1.2.11, with the gate nested and the incident paused:

    parent   aget_state(config).values        status=dispatch_planning   pending_approval=None
    subgraph aget_state(config, subgraphs=True).tasks[0].state.values
                                              status=awaiting_approval   pending_approval=set

A subgraph's writes reach the parent when the subgraph node *completes*, and a paused one has not.
So the parent's own state understates what is happening for exactly as long as a human is being
waited on. Reading the parent alone would report an incident as `dispatch_planning` while it is in
fact blocked on an approval -- the single most misleading answer this system could give.

This is a property of nesting, not of these functions, and every one of the six gates is nested.
`graph.inspect.pending_approval_for` is the supported way to ask, and it reads through the boundary.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    IncidentStatus,
    ReasonCode,
)
from lpr_cpe.domain.governance import ApprovalDecision, ApprovalRequest
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.security.rbac import Role, approvers_for, can_approve

if TYPE_CHECKING:
    from lpr_cpe.graph.context import GraphContext


def approval_id_for(incident_id: str, kind: ApprovalKind, attempt: int, subject: str = "") -> str:
    """A stable id for one asking of one question.

    `attempt` is what keeps a *second* genuine request distinguishable from a replay of the first:
    a dispatch rejected once and re-proposed with a different crew is two questions and must appear
    in the audit trail as two. Callers pass the relevant attempt counter, so an accidental repeat
    cannot manufacture a new id but a deliberate one can.

    `subject` is what keeps two *different* questions of the same kind apart, and it was added
    because the incident and the attempt are not enough. `high_risk_remote_action` covers both the
    firmware update and the factory reset; an incident that offers the firmware update in one
    diagnostic cycle and the factory reset in the next asks about each at attempt 1, so both derived
    the same `approval_id`. `approvals` de-duplicates on that id and keeps the *first* write, so
    approving the firmware update silently pre-approved the factory reset -- an unasked human
    authorising the most destructive action in the CPE catalogue. `build_request` fills this in from
    the action and its target; a gate with no action type (`low_confidence_rca`) leaves it empty,
    because for those the attempt counter really is the whole of the difference.
    """
    material = f"{incident_id}\x1f{kind.value}\x1f{attempt}\x1f{subject}"
    return f"APR-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _rejection(
    request: ApprovalRequest, ctx: GraphContext, *, by: str, role: str, rationale: str
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        incident_id=request.incident_id,
        kind=request.kind,
        status=ApprovalStatus.REJECTED,
        decided_at=ctx.clock.now(),
        decided_by=by,
        decided_by_role=role,
        rationale=rationale,
        reason_code=ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE,
    )


def _decision_from_answer(
    request: ApprovalRequest, ctx: GraphContext, answer: Any
) -> ApprovalDecision:
    """Turn whatever came back through the resume channel into a decision, or a rejection.

    Deliberately total: every malformed answer produces a *recorded* rejection rather than an
    exception. An exception here would leave the incident un-resumable at the one moment a human is
    already involved, and the operator would see a stack trace instead of "your role cannot approve
    this". The graph routes on the returned status either way.
    """
    if not isinstance(answer, dict):
        return _rejection(
            request,
            ctx,
            by="unknown",
            role="",
            rationale=(
                f"malformed approval response: expected a mapping with 'status' and 'decided_by', "
                f"got {type(answer).__name__}"
            ),
        )

    decided_by = str(answer.get("decided_by") or "").strip()
    raw_role = answer.get("decided_by_role")
    role = Role.parse(raw_role) if raw_role is not None else None
    raw_status = str(answer.get("status") or "").strip().lower()

    if not decided_by:
        return _rejection(
            request,
            ctx,
            by="unknown",
            role=str(raw_role or ""),
            rationale=(
                "approval response carried no `decided_by`. An approval nobody is named for is "
                "not an approval -- it is an unattributable change to a customer's service."
            ),
        )

    if not can_approve(role, request.kind):
        permitted = sorted(r.value for r in approvers_for(request.kind))
        return _rejection(
            request,
            ctx,
            by=decided_by,
            role=str(raw_role or ""),
            rationale=(
                f"{raw_role or 'no role'} may not approve {request.kind.value}; "
                f"permitted: {', '.join(permitted)}"
            ),
        )

    try:
        status = ApprovalStatus(raw_status)
    except ValueError:
        return _rejection(
            request,
            ctx,
            by=decided_by,
            role=role.value if role else "",
            rationale=(
                f"unrecognised approval status {raw_status!r}; "
                f"expected one of {', '.join(s.value for s in ApprovalStatus)}"
            ),
        )

    rationale = str(answer.get("rationale") or "")
    if status is ApprovalStatus.REJECTED and not rationale:
        # `ApprovalDecision` refuses a rejection with no rationale, and it is right to: the graph
        # routes on it. Supplying a placeholder here keeps that validator meaningful for callers
        # who *do* explain themselves, while still recording the refusal.
        rationale = "rejected without a stated reason"

    return ApprovalDecision(
        approval_id=request.approval_id,
        incident_id=request.incident_id,
        kind=request.kind,
        status=status,
        decided_at=ctx.clock.now(),
        decided_by=decided_by,
        decided_by_role=role.value if role else "",
        rationale=rationale,
        reason_code=answer.get("reason_code"),
        conditions=tuple(answer.get("conditions") or ()),
        modified_action=answer.get("modified_action"),
    )


def build_request(
    state: IncidentState,
    ctx: GraphContext,
    *,
    kind: ApprovalKind,
    question: str,
    attempt: int,
    action_type: ActionType | None = None,
    target_ref: str | None = None,
    recommendation: str = "",
    risk_summary: str = "",
    blast_radius: int | None = None,
    reversible: bool = True,
    policy_decision_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> ApprovalRequest:
    """Assemble the question. Expiry and required role come from the pack, never from the caller.

    A caller that could pass its own `expires_after` would be a caller that could make a high-blast-
    radius approval stand for a week. The pack owns both, so changing either is a versioned change.

    The `subject` handed to `approval_id_for` is derived here rather than taken as an argument, so a
    caller cannot forget it and collide two questions into one id; see that function for what that
    collision cost.
    """
    rule = ctx.policy.pack.approvals[kind]
    now = ctx.clock.now()
    subject = f"{action_type.value}:{target_ref or ''}" if action_type is not None else ""
    return ApprovalRequest(
        approval_id=approval_id_for(state.get("incident_id") or "", kind, attempt, subject),
        incident_id=state.get("incident_id") or "",
        kind=kind,
        requested_at=now,
        expires_at=now + timedelta(minutes=rule.expires_after_minutes),
        action_type=action_type,
        target_ref=target_ref,
        required_role=rule.required_role.value,
        question=question,
        recommendation=recommendation,
        risk_summary=risk_summary,
        blast_radius=blast_radius,
        reversible=reversible,
        policy_decision_id=policy_decision_id,
        context=context or {},
    )


def prepare_approval(
    state: IncidentState, ctx: GraphContext, request: ApprovalRequest
) -> dict[str, Any]:
    """Record that a question is about to be asked. The first half of every gate.

    Returns rather than interrupting, so `pending_approval` and the `AWAITING_APPROVAL` status are
    committed to the checkpoint *before* the graph pauses -- into the state of whichever graph this
    node belongs to. For a nested gate that is the subgraph's state, not the parent's; see the
    module docstring and use `graph.inspect.pending_approval_for` to read it.
    """
    return {
        "pending_approval": request,
        "status": IncidentStatus.AWAITING_APPROVAL,
        "updated_at": request.requested_at,
    }


def request_approval(state: IncidentState, ctx: GraphContext) -> dict[str, Any]:
    """Raise the interrupt on the pending request and return the answer. The second half.

    Reads the request from state rather than taking it as an argument: it must be the *same*
    question `prepare_approval` recorded, and rebuilding it here would re-stamp `requested_at` on
    every replay of this node.

    The payload is dumped to JSON mode because the checkpointer serialises it; handing `interrupt()`
    a Pydantic model works in memory and fails against Postgres, which is the worst possible place
    to discover it.

    `permitted_roles` is included even though `required_role` is already in the request. The pack
    names one role per kind, but `security.rbac` permits a *set* -- a supervisor can answer an
    operator's question. Sending only the pack's single role would have operators told they cannot
    answer questions they can.
    """
    request = state.get("pending_approval")
    if request is None:
        raise ValueError(
            "request_approval reached with no pending_approval in state. Every gate is the pair "
            "`prepare_approval` -> `request_approval`; reaching the second without the first means "
            "an edge skips the node that records what is being asked."
        )

    answer = interrupt(
        {
            "approval_request": request.model_dump(mode="json"),
            "permitted_roles": sorted(r.value for r in approvers_for(request.kind)),
        }
    )
    decision = _decision_from_answer(request, ctx, answer)
    return {
        "approvals": [decision],
        "pending_approval": None,
        "updated_at": decision.decided_at,
    }
