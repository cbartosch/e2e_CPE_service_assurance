"""Stage 3's remote branch: choose one repair, ask if the pack demands it, do it, prove it.

This is P12 and D10, and the `high_risk_remote_action` gate that sits between them. D09 has already
answered "is an allowlisted remote repair eligible?"; everything here is downstream of a `remote`.

Five nodes, and why it is five rather than two
----------------------------------------------
The obvious shape is *execute* then *verify*. It is wrong in three separate places, and each extra
node is one of them.

* **Selecting is not executing.** `ActionRequest` refuses to be constructed with
  `policy_outcome=BLOCKED` and refuses to be constructed without an `approval_ref` when the outcome
  is `REQUIRES_APPROVAL`. So the policy evaluation cannot happen inside the node that builds the
  request -- by the time the verdict is known, the object that would carry it is already illegal.
  `select_remote_action` evaluates and records; `execute_remote_repair` builds the request only once
  the verdict is in state and the gate, if any, has been passed.
* **Asking is two nodes.** `graph.interrupts` explains this at length: a node writes state only by
  returning, and a gate does not return until it is answered, so a single gate node cannot record
  that it *is* waiting. `prepare_remote_approval` writes the question and returns;
  `request_remote_approval` reads it back and raises.
* **Executing is not verifying.** `RemoteAction.fixed_it` requires `verification_passed is True`,
  and the verification is a *later read of the device* -- a separate observation with its own
  timestamp. Folding it into the execute node would mean reading the device in the same super-step
  that wrote to it, which measures the state before the action has taken effect.

The one router, wired on two edges
----------------------------------
`route_remote_gate` is the only conditional edge inside this graph, and it is attached to both
`select_remote_action` and `request_remote_approval`. That is deliberate: after selection and after
an answer, the question is the identical one -- *may this action run now?* -- and the answer moves
from `approve` to `execute` or `abandon` purely because the approval trail changed underneath it.
Two routers would be two spellings of one question, and the second would be the one that forgot
about rejection.

It keys on `decision.required_approval_kind` rather than naming `HIGH_RISK_REMOTE_ACTION`. The
firmware update and the factory reset are the high-risk pair today, but a pack that raised some
other kind against a reboot is still honoured -- a hard-coded kind would sail past the demand and
execute unapproved.

Honoured is not the same as asked here, and the difference is `DEDICATED_GATE_APPROVAL_KINDS`.
Five kinds belong to a gate of their own, and since the readers in `routing` key on kind alone,
asking one of them *here* answers the owning gate's question for it -- a `low_confidence_rca`
collected at this gate was measured satisfying D06, which then skipped its own fail-closed branch.
So a demand of those five kinds abandons instead: the `PolicyDecision` reaches the parent, D10
sends the incident round, and the gate that owns the question asks it.

`exceptional_closure` used to be named here as the kind nobody owned and so one this gate would
still ask. `reconciliation_closure` owns it now and it joined the deny list with that stage. What
is left outside the list is `high_risk_remote_action`, which is not an oversight but the kind this
gate exists to ask -- and the one `PolicyEngine` falls back to when a rule demands approval without
naming one.

Why the node records the selection instead of the router re-deriving it
-----------------------------------------------------------------------
D09's docstring promises that "the option itself is found by `first_actionable_option`, which P12
calls again to learn which repair this branch was about". P12 calls it **once**, in
`select_remote_action`, and writes the answer to `resolution_plan.selected_option_id`; every later
reader takes `plan.selected`.

Calling it again in the router would be a bug, and a quiet one. `first_actionable_option` skips
options whose latest `PolicyDecision` is blocked -- and `select_remote_action` may have just
recorded exactly such a decision. The router would then be answering about the *next* option while
the node had selected the first, and the two would disagree about which repair the branch was for.
The plan is the one owner.

What D10 does not do, and where it lives
----------------------------------------
`route_remote_outcome` is **not** wired here. Both of its destinations -- validation and a fresh
diagnostic cycle -- are outside this graph, so it belongs on the parent's edge out of the subgraph
node. A subgraph cannot route to a sibling it does not contain.

Where the parent cannot see this
--------------------------------
While `request_remote_approval` is paused, `pending_approval` and `status=awaiting_approval` are in
*this* graph's checkpoint and not the parent's -- measured on langgraph 1.2.11 and set out in
`graph.interrupts`. Reading the parent alone reports the incident as `diagnosing`.
`graph.inspect.pending_approval_for` reads through the boundary and is the supported way to ask.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lpr_cpe.domain.enums import (
    ActionOutcome,
    ApprovalKind,
    EvidenceKind,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
)
from lpr_cpe.domain.governance import ActionRecord, ActionRequest, PolicyDecision
from lpr_cpe.domain.resolution import RemoteAction, ResolutionOption
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.interrupts import build_request, prepare_approval, request_approval
from lpr_cpe.graph.nodes._runtime import (
    Freshness,
    Gathered,
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    emit_kpi,
    make_evidence,
    node,
    preview,
)
from lpr_cpe.graph.routing import (
    DEDICATED_GATE_APPROVAL_KINDS,
    approval_granted,
    approval_outstanding,
    first_actionable_option,
    is_remote_option,
    latest_decision_of,
    latest_policy_decision,
)
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.graph.subgraphs._shared import (
    attempt_number,
    idempotency_key_for,
    policy_input_for,
    reachability_verdict,
)
from lpr_cpe.observability.kpi import MetricTimestamp, mark, stamp

# ------------------------------------------------------------------------------------------------
# Reading the incident for the policy engine
# ------------------------------------------------------------------------------------------------


def selected_remote_option(state: IncidentState) -> ResolutionOption | None:
    """The option `select_remote_action` chose, or `None` if it chose nothing.

    A one-line wrapper over `plan.selected`, named because three readers want it and the name is
    what stops a fourth from reaching for `first_actionable_option` instead. See the module
    docstring for why re-deriving it is a defect rather than a duplication.
    """
    plan = state.get("resolution_plan")
    return plan.selected if plan is not None else None


# ------------------------------------------------------------------------------------------------
# P12a -- select
# ------------------------------------------------------------------------------------------------


@node("select_remote_action")
async def select_remote_action(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Choose the repair D09 meant, put it to the policy engine, and record both.

    Records and does not act. The `PolicyDecision` this writes is what the gate router reads, what
    `/incidents/{id}/decisions` serves, and what an auditor reads a year later to see the rule that
    was in force -- so it is written whatever the verdict, including when the verdict is a block.

    **The status is deliberately not set here.** An earlier draft moved the incident to
    `remote_resolution` on selection, which reads as harmless and is not: a blocked or unapproved
    action leaves this graph without ever touching the device, and the incident would be recorded as
    having entered a stage it never entered. `execute_remote_repair` sets the status, because
    executing is what makes it true.

    The decision's id is re-keyed from the incident and the option. `PolicyEngine.evaluate` mints a
    `uuid4`, which is right for a stateless engine and wrong for a checkpointed graph:
    `append_unique` de-duplicates `policy_decisions` on the decision's natural key, and a fresh uuid
    on every replay would make one evaluation appear as several -- inflating the denominator of
    `policy_block_rate`, which is a rate this node also emits.
    """
    plan = state.get("resolution_plan")
    option = first_actionable_option(state, is_remote_option)
    cycle = state.get("diagnostic_cycles", 1)
    if plan is None or option is None:
        # D09 said a remote repair was eligible and by the time we got here none was. The honest
        # reading is that policy blocked the last candidate between the two, which is a recordable
        # fact rather than an error: the gate router sends this to `abandon` and D10 sends the
        # incident back for another diagnostic pass.
        #
        # The missing plan is folded into the same branch rather than raised separately, because it
        # is the same answer: `first_actionable_option` reads the plan and returns `None` when there
        # is none, so a plan-less incident already arrives here. Testing it explicitly is what lets
        # the writes below narrow to a plan that exists.
        return {
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="select_remote_action",
                    action="select_remote_action",
                    outcome="no_actionable_remote_option",
                    reason_code=ReasonCode.REMOTE_FIX_EXHAUSTED,
                    detail={"cycle": cycle},
                    discriminator=cycle,
                )
            ]
        }

    verdict = ctx.policy.evaluate(policy_input_for(state, ctx, option))
    decision = verdict.model_copy(
        update={
            "decision_id": derive_id(
                "POL", state.get("incident_id") or "", option.option_id, verdict.outcome.value
            )
        }
    )

    update: NodeUpdate = {
        "policy_decisions": [decision],
        "resolution_plan": plan.model_copy(update={"selected_option_id": option.option_id}),
        "audit_events": [
            audit(
                state,
                ctx,
                node="select_remote_action",
                action="select_remote_action",
                outcome=decision.outcome.value,
                subject_ref=option.target_ref,
                reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                detail={
                    "cycle": cycle,
                    "option_id": option.option_id,
                    "action_type": option.action_type.value,
                    "attempt": attempt_number(state, option.action_type),
                    "blast_radius": option.blast_radius,
                    "reversible": option.reversible,
                    "risk": option.risk,
                    "policy_decision_id": decision.decision_id,
                    "policy_version": decision.policy_version,
                    "matched_rule": decision.matched_rule,
                    "required_approval": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind
                        else None
                    ),
                    "explanation": decision.explanation,
                },
                discriminator=cycle,
            )
        ],
    }
    # `preview`, not `state`: `policy_block_rate` counts `policy_decisions`, and the decision it
    # must count is the one three lines above, still sitting unreduced in `update`. Measured against
    # the raw state this returned `[]` on every first evaluation of an incident -- the denominator
    # was empty, `KPINotDerivableError` was swallowed by design, and the KPI silently never fired.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.POLICY_BLOCK_RATE,
        node="select_remote_action",
        dimensions={"action_type": option.action_type.value},
        discriminator=cycle,
    )
    return update


# ------------------------------------------------------------------------------------------------
# The gate
# ------------------------------------------------------------------------------------------------


def route_remote_gate(state: IncidentState) -> Literal["approve", "execute", "abandon"]:
    """May the selected repair run now? Wired on the edge out of selection *and* out of the gate.

    Total, like every router, and conservative in every unset case: no option, no decision and a
    blocked decision all abandon. A missing `PolicyDecision` is the one worth naming -- it means
    `select_remote_action` did not record one, which is a defect upstream, and executing an
    unevaluated write because the evaluation is missing is the exact failure the policy engine
    exists to prevent.

    The `approve` -> `execute` transition is driven by `approval_outstanding`, which compares the
    latest demand's timestamp against the latest answer's rather than asking whether an answer
    exists. Both lists are append-only, so "has anyone ever approved this kind?" would stay true
    into the next diagnostic cycle and wave through a repair nobody had been asked about.

    A demand this gate may not raise abandons rather than asks: see
    `DEDICATED_GATE_APPROVAL_KINDS`. Only the `approve` branch is affected, so an answer the owning
    gate has already granted still reaches `execute` on the next pass and the repair is deferred
    rather than lost.
    """
    option = selected_remote_option(state)
    if option is None:
        return "abandon"
    decision = latest_policy_decision(state, option.action_type)
    if decision is None or decision.blocked:
        return "abandon"
    if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL:
        kind = decision.required_approval_kind
        if kind is None:
            # `PolicyDecision` validates this away at construction, so it is unreachable through the
            # engine. Abandoning rather than asserting keeps the router's never-raises promise: an
            # exception in a conditional edge aborts the super-step and the incident rolls back
            # with no record of the attempt.
            return "abandon"
        if approval_outstanding(state, kind):
            return "abandon" if kind in DEDICATED_GATE_APPROVAL_KINDS else "approve"
        return "execute" if approval_granted(state, kind) else "abandon"
    return "execute"


def _demanded_approval(
    state: IncidentState,
) -> tuple[ResolutionOption, PolicyDecision, ApprovalKind]:
    """The option, its decision and the approval kind the decision demands.

    Raises rather than returning `None` on any of the three. Every caller is a node reached only
    through `route_remote_gate`'s `approve` branch, which has already established all three; getting
    here without them means an edge bypasses the router, and `@node` deliberately does not catch
    that -- a checkpoint left un-advanced at the last node that completed is truthful and resumable,
    where a state update claiming nothing happened is neither.
    """
    option = selected_remote_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    kind = decision.required_approval_kind if decision is not None else None
    if option is None or decision is None or kind is None:
        raise ValueError(
            "the remote approval gate was reached without a selected option, a policy decision and "
            f"an approval kind (option={option is not None}, decision={decision is not None}, "
            f"kind={kind}). Only `route_remote_gate`'s `approve` branch may lead here, and it "
            "checks all three."
        )
    return option, decision, kind


@node("prepare_remote_approval")
async def prepare_remote_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write the question down, then return so it is committed before anyone is asked.

    The first half of the pair `graph.interrupts` describes. Everything about the question is built
    here and nothing is built in the node that raises, so `requested_at` is stamped once however
    many times the gate replays.

    `mark(APPROVAL_REQUESTED_AT)` is written here because this is the instant the wait starts.
    `KPICalculator.approval_wait` already reads that stamp for decided approvals -- it is the only
    way to recover the wait, since `ApprovalDecision` carries `decided_at` and not the request
    instant -- and this is its first writer. Stamping it in the node that *resumes* would measure
    zero every time.
    """
    option, decision, kind = _demanded_approval(state)
    attempt = attempt_number(state, option.action_type)
    request = build_request(
        state,
        ctx,
        kind=kind,
        question=(
            f"Approve {option.label.lower()} on {option.target_ref}? "
            f"This is attempt {attempt} of this action for the incident."
        ),
        attempt=attempt,
        action_type=option.action_type,
        target_ref=option.target_ref,
        recommendation=option.rationale or option.label,
        risk_summary=decision.explanation,
        blast_radius=option.blast_radius,
        reversible=option.reversible,
        policy_decision_id=decision.decision_id,
        context={
            "fault_domain": option.addresses_domain.value,
            "estimated_success_probability": option.estimated_success_probability,
            "estimated_minutes": option.estimated_duration.total_seconds() / 60.0,
            "customer_disruption": option.customer_disruption,
            "risk_class": option.risk,
            "policy_reason_codes": [code.value for code in decision.reason_codes],
            "matched_rule": decision.matched_rule,
            "policy_version": decision.policy_version,
        },
    )
    return {
        **prepare_approval(state, ctx, request),
        **mark(MetricTimestamp.APPROVAL_REQUESTED_AT, request.requested_at),
        "audit_events": [
            audit(
                state,
                ctx,
                node="prepare_remote_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=option.target_ref,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "approval_id": request.approval_id,
                    "kind": kind.value,
                    "attempt": attempt,
                    "required_role": request.required_role,
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_remote_approval")
async def request_remote_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Builds nothing; see `graph.interrupts`.

    Thin on purpose. Everything before `interrupt()` in a node runs again on resume, so the less
    there is before it, the less there is to re-run -- and a gate that built its own question here
    would build a different one each time.
    """
    return request_approval(state, ctx)


@node("abandon_remote_action")
async def abandon_remote_action(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Leave the branch without touching the device, and say why in the record.

    This node exists for the status, and the status is the whole of its justification. A rejected
    approval leaves the incident at `awaiting_approval`, and ending the subgraph there would
    checkpoint an incident that claims to be waiting for a decision that has already been made --
    the same lie `prepare_approval` and `request_approval` were split apart to avoid, arriving from
    the other end. `diagnosing` is the honest reading of an incident that has a root cause and no
    permitted remedy, and D10 will send it round for another pass.

    A no-op transition is legal, so the write is unconditional: from `awaiting_approval` it is a
    real move, and from `diagnosing` -- where a blocked action leaves it -- it changes nothing.
    """
    option = selected_remote_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    answer = (
        latest_decision_of(state, decision.required_approval_kind)
        if decision is not None and decision.required_approval_kind is not None
        else None
    )
    cycle = state.get("diagnostic_cycles", 1)

    if answer is not None and not answer.granted:
        outcome, reason = "approval_refused", ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE
    elif decision is not None and decision.blocked:
        outcome, reason = (
            "blocked_by_policy",
            (
                decision.reason_codes[0]
                if decision.reason_codes
                else ReasonCode.POLICY_NO_MATCHING_RULE
            ),
        )
    elif decision is not None and decision.required_approval_kind in DEDICATED_GATE_APPROVAL_KINDS:
        # Deferred, not exhausted: the same repair runs once the owning gate grants it, and
        # `REMOTE_FIX_EXHAUSTED` here would record that the branch had nothing left to try.
        outcome, reason = "approval_deferred_to_owning_gate", ReasonCode.POLICY_APPROVAL_REQUIRED
    else:
        outcome, reason = "no_permitted_remote_action", ReasonCode.REMOTE_FIX_EXHAUSTED

    return {
        "status": IncidentStatus.DIAGNOSING,
        "pending_approval": None,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_remote_action",
                action="abandon_remote_action",
                outcome=outcome,
                subject_ref=option.target_ref if option is not None else None,
                reason_code=reason,
                detail={
                    "cycle": cycle,
                    "option_id": option.option_id if option is not None else None,
                    "policy_outcome": decision.outcome.value if decision is not None else None,
                    "approval_status": answer.status.value if answer is not None else None,
                    "approval_rationale": answer.rationale if answer is not None else "",
                },
                discriminator=cycle,
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# P12b -- execute
# ------------------------------------------------------------------------------------------------


def _approval_ref(state: IncidentState, decision: PolicyDecision) -> str | None:
    """The approval this action runs under, or `None` when the pack demanded none.

    Read off `ApprovalDecision.approval_ref`, which is a derived property (`approval_id:decided_by`)
    rather than a stored field, so the reference on the action cannot disagree with the approval it
    names. `ActionRequest` refuses to construct without one when the outcome is `REQUIRES_APPROVAL`,
    which is what turns "we forgot the approval ref" from a silent unapproved write into a crash.
    """
    if decision.outcome is not PolicyOutcome.REQUIRES_APPROVAL:
        return None
    kind = decision.required_approval_kind
    answer = latest_decision_of(state, kind) if kind is not None else None
    return answer.approval_ref if answer is not None else None


@node("execute_remote_repair")
async def execute_remote_repair(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P12. Read the device, send the repair, record what came back.

    The pre-read is what makes the verification meaningful. `verify_remote_repair` compares the
    device against `pre_state`, and a comparison against a reading taken before the *diagnosis* --
    minutes and several adapter calls earlier -- would attribute to the reboot anything that changed
    in between. So the reading is taken here, in the same node, immediately before the write.

    It is not a replay guard, and an earlier draft that treated it as one has been removed. This
    node contains no interrupt, so it cannot replay within an invocation; the only way back in is a
    new diagnostic cycle, which is a new attempt and deserves a fresh reading. The simulator carries
    the same note against `apply_action` for the same reason: a condition that cannot change the
    outcome reads as though replays were dangerous, and no test could tell its presence from its
    absence.

    `remote_attempt_count` is written as an **absolute** count of distinct remote actions, never as
    `state.get(...) + 1`. It reduces with `take_max`, and an increment computed from a value the
    node read at entry is exactly the pattern that reducer exists to defeat.
    """
    plan = state.get("resolution_plan")
    option = selected_remote_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    if plan is None or option is None or decision is None:
        raise ValueError(
            "execute_remote_repair was reached with no resolution plan, no selected option or no "
            "policy decision for it. Every path here runs through `route_remote_gate`, which "
            "abandons in all three cases."
        )

    now = ctx.clock.now()
    attempt = attempt_number(state, option.action_type)
    cycle = state.get("diagnostic_cycles", 1)
    idempotency_key = idempotency_key_for(state, option)
    action_id = derive_id("ACT", state.get("incident_id") or "", option.option_id)

    gathered = Gathered(ctx, assessed_at=now)
    cpe_ref = state.get("cpe_ref") or option.target_ref
    pre_state = await gathered.fetch(
        "cpe_status_pre", ctx.adapters.cpe.read_status(cpe_ref), freshness=Freshness.TELEMETRY
    )

    request = ActionRequest(
        action_id=action_id,
        incident_id=state.get("incident_id") or "",
        action_type=option.action_type,
        target_ref=option.target_ref,
        requested_at=now,
        idempotency_key=idempotency_key,
        actor=ctx.automation_actor,
        reason_code=(
            ReasonCode.POLICY_APPROVAL_REQUIRED
            if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
            else ReasonCode.POLICY_ALLOWED
        ),
        correlation_id=state.get("correlation_id") or state.get("incident_id") or "",
        approval_ref=_approval_ref(state, decision),
        policy_decision_id=decision.decision_id,
        policy_outcome=decision.outcome,
        attempt=attempt,
        parameters=dict(option.parameters),
        reversible=option.reversible,
        expected_blast_radius=option.blast_radius,
    )

    result = await ctx.adapters.cpe.apply_action(request)
    completed_at = ctx.clock.now()
    outcome = ActionOutcome(str(result["outcome"]))

    remote_action = RemoteAction(
        action_id=action_id,
        action_type=option.action_type,
        target_ref=option.target_ref,
        idempotency_key=idempotency_key,
        requested_at=now,
        completed_at=completed_at,
        outcome=outcome,
        attempt=attempt,
        simulated=bool(result.get("simulated")),
        reason_code=request.reason_code,
        approval_ref=request.approval_ref,
        pre_state=dict(pre_state or {}),
        error=str(result.get("error") or ""),
    )
    record = ActionRecord(
        action_id=action_id,
        incident_id=request.incident_id,
        action_type=option.action_type,
        target_ref=option.target_ref,
        idempotency_key=idempotency_key,
        outcome=outcome,
        started_at=now,
        completed_at=completed_at,
        actor=ctx.automation_actor,
        reason_code=request.reason_code,
        approval_ref=request.approval_ref,
        correlation_id=request.correlation_id,
        attempt=attempt,
        simulated=bool(result.get("simulated")),
        external_ref=result.get("external_ref"),
        detail=str(result.get("detail") or ""),
        error=str(result.get("error") or ""),
    )

    attempted = list(plan.attempted_option_ids)
    if option.option_id not in attempted:
        attempted.append(option.option_id)

    update: NodeUpdate = {
        "status": IncidentStatus.REMOTE_RESOLUTION,
        "selected_action": request,
        "remote_actions": [remote_action],
        "action_history": [record],
        "resolution_plan": plan.model_copy(update={"attempted_option_ids": attempted}),
        "remote_attempt_count": _distinct_remote_actions(state, action_id),
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "updated_at": completed_at,
        **mark(MetricTimestamp.FIRST_ACTION_AT, now),
        "audit_events": [
            audit(
                state,
                ctx,
                node="execute_remote_repair",
                action="execute_remote_repair",
                outcome=outcome.value,
                subject_ref=option.target_ref,
                reason_code=request.reason_code,
                detail={
                    "cycle": cycle,
                    "attempt": attempt,
                    "action_id": action_id,
                    "action_type": option.action_type.value,
                    "idempotency_key": idempotency_key,
                    "approval_ref": request.approval_ref,
                    "policy_decision_id": decision.decision_id,
                    "policy_outcome": decision.outcome.value,
                    "simulated": bool(result.get("simulated")),
                    "replayed": bool(result.get("replayed")),
                    "external_ref": result.get("external_ref"),
                    "gate": result.get("gate"),
                    "detail": result.get("detail"),
                },
                discriminator=action_id,
            )
        ],
    }
    # `preview`, not `state`: `automation_coverage_rate`'s denominator is the executed entries of
    # `action_history`, and the entry that just executed is in `update`. See `select_remote_action`.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.AUTOMATION_COVERAGE_RATE,
        node="execute_remote_repair",
        dimensions={"action_type": option.action_type.value},
        discriminator=action_id,
    )
    return update


def _distinct_remote_actions(state: IncidentState, action_id: str) -> int:
    """How many distinct remote actions this incident will have attempted, counting this one.

    Distinct by `action_id` because `remote_actions` reduces with `append_revision`, which keeps a
    history: `verify_remote_repair` appends a revised copy of the same action, so `len()` over the
    list counts revisions rather than actions and would report two attempts for one reboot.
    """
    seen = {existing.action_id for existing in state.get("remote_actions", [])}
    seen.add(action_id)
    return len(seen)


# ------------------------------------------------------------------------------------------------
# P12c -- verify
# ------------------------------------------------------------------------------------------------


@node("verify_remote_repair")
async def verify_remote_repair(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Read the device again and say, on the record, whether the repair took.

    A separate node from the execution and a separate observation from it. "Proof before closure"
    means the verification has its own timestamp and its own evidence item, so a reviewer can see
    that somebody looked *after* rather than inferring it from the action having been acknowledged.

    The revised `RemoteAction` carries the same `action_id` as the one `execute_remote_repair`
    wrote. `remote_actions` reduces with `append_revision`, so the pair is kept as a history rather
    than collapsed -- which is what lets the record show an action that was sent and only later
    judged, instead of one that was born verified.
    """
    actions = state.get("remote_actions", [])
    if not actions:
        raise ValueError(
            "verify_remote_repair was reached with no remote action to verify. Only "
            "`execute_remote_repair` leads here, and it always records one."
        )
    action = actions[-1]

    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    cpe_ref = state.get("cpe_ref") or action.target_ref
    post_state = await gathered.fetch(
        "cpe_status_post", ctx.adapters.cpe.read_status(cpe_ref), freshness=Freshness.TELEMETRY
    )
    passed, summary = reachability_verdict(action.pre_state or None, post_state)

    evidence = make_evidence(
        state,
        ctx,
        node="verify_remote_repair",
        kind=EvidenceKind.CPE_STATUS,
        subject_ref=cpe_ref,
        summary=f"post-{action.action_type.value} verification: {summary}",
        source_system="cpe",
        payload=dict(post_state or {}),
        discriminator=action.action_id,
    )
    verified = action.model_copy(
        update={
            "post_state": dict(post_state or {}),
            "verified_at": now,
            "verification_summary": summary,
            "verification_passed": passed,
            "evidence_refs": [*action.evidence_refs, evidence.ref],
        }
    )

    # `None` is the third value and it is deliberately *not* mapped onto a reason code: the codes
    # say a fix was applied or none was found, and the whole point of the unverifiable case is that
    # we know neither. An audit event with no reason code reads as "not applicable", which is true.
    #
    # The `restored` branch asks `verified.fixed_it` rather than `passed`, and the difference is a
    # real case rather than defensive noise. `fixed_it` requires the action to have *run* as well as
    # verification to have passed, so a reboot the adapter reported as FAILED, on a device that came
    # back anyway, is `restored` with no fix claimed. Attributing that recovery to the action would
    # put a fix we know failed into `remote_fix_success_rate`.
    if passed is None:
        verdict_outcome, verdict_reason = "not_verifiable", None
    elif passed:
        verdict_outcome = "restored"
        verdict_reason = (
            ReasonCode.REMOTE_FIX_APPLIED if verified.fixed_it else ReasonCode.NO_FAULT_FOUND
        )
    else:
        verdict_outcome, verdict_reason = "not_restored", ReasonCode.NO_FAULT_FOUND

    update: NodeUpdate = {
        "remote_actions": [verified],
        "evidence": [evidence],
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="verify_remote_repair",
                action="verify_remote_repair",
                outcome=verdict_outcome,
                subject_ref=cpe_ref,
                reason_code=verdict_reason,
                detail={
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                    "verification_passed": passed,
                    "summary": summary,
                    "online_before": action.pre_state.get("online"),
                    "online_after": (post_state or {}).get("online"),
                    "evidence_ref": evidence.ref,
                },
                discriminator=action.action_id,
            )
        ],
    }
    if verified.fixed_it:
        # Both stamps, and they are not the same fact. `remote_fix_at` is when this branch worked;
        # `restored_at` is when the customer's service came back, which `time_to_restore_seconds`
        # measures. They coincide here because a verified remote fix *is* the restoration, and they
        # would not for a fix that needed a truck.
        stamp(update, MetricTimestamp.REMOTE_FIX_AT, now)
        stamp(update, MetricTimestamp.RESTORED_AT, now)
    return update


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The five nodes, in the order the specification walks them. Same shape as `PARENT_NODES` and
#: checked the same way, so a node registered under a name its decorator does not carry fails on
#: import rather than producing a graph whose topology and audit trail disagree.
REMOTE_RESOLUTION_NODES: tuple[tuple[str, Any], ...] = (
    ("select_remote_action", select_remote_action),
    ("prepare_remote_approval", prepare_remote_approval),
    ("request_remote_approval", request_remote_approval),
    ("execute_remote_repair", execute_remote_repair),
    ("verify_remote_repair", verify_remote_repair),
    ("abandon_remote_action", abandon_remote_action),
)

check_node_registry(REMOTE_RESOLUTION_NODES, "the remote-resolution node registry")

#: Where each of `route_remote_gate`'s answers goes. Read by the builder below and by the tests, so
#: the router's `Literal` and the wiring cannot drift apart silently.
GATE_TARGETS: dict[str, str] = {
    "approve": "prepare_remote_approval",
    "execute": "execute_remote_repair",
    "abandon": "abandon_remote_action",
}


def build_remote_resolution_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every edge is guarded, for the reason the parent's are: `escalation_update` stops a node from
    doing work but does not stop the graph, and an unguarded subgraph would walk its remaining four
    super-steps after the budget had already been declared exhausted.

    `context_schema=GraphContext` is repeated here rather than inherited. A compiled subgraph is a
    graph in its own right -- `get_runtime(GraphContext)` inside its nodes resolves against *its*
    schema -- so omitting it would make every node in this file raise on its first line while the
    parent's kept working.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in REMOTE_RESOLUTION_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "select_remote_action")

    gate_map: dict[Any, str] = {**GATE_TARGETS, ESCALATED: END}
    graph.add_conditional_edges("select_remote_action", guarded(route_remote_gate), gate_map)
    graph.add_conditional_edges("request_remote_approval", guarded(route_remote_gate), gate_map)

    graph.add_conditional_edges(
        "prepare_remote_approval",
        guarded(straight_on),
        {ONWARD: "request_remote_approval", ESCALATED: END},
    )
    graph.add_conditional_edges(
        "execute_remote_repair",
        guarded(straight_on),
        {ONWARD: "verify_remote_repair", ESCALATED: END},
    )
    graph.add_edge("verify_remote_repair", END)
    graph.add_edge("abandon_remote_action", END)
    return graph


def compile_remote_resolution_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission. A subgraph compiled as a node shares the
    parent's checkpointer -- LangGraph namespaces its state beneath the parent's thread -- and
    handing this one its own would give the incident two places to be resumed from.
    """
    return build_remote_resolution_graph().compile(name="lpr_cpe_remote_resolution")


__all__ = [
    "GATE_TARGETS",
    "REMOTE_RESOLUTION_NODES",
    "abandon_remote_action",
    "build_remote_resolution_graph",
    "compile_remote_resolution_graph",
    "execute_remote_repair",
    "prepare_remote_approval",
    "request_remote_approval",
    "route_remote_gate",
    "select_remote_action",
    "selected_remote_option",
    "verify_remote_repair",
]
