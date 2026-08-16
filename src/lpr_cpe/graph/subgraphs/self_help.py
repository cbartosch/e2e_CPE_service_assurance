"""Stage 3's self-help branch: ask the customer to try something, then check whether it worked.

This is P13 and D12, the approval gate the pack may raise in front of them, and the
customer-response interrupt in the middle. D11 has already answered "is guided self-help
suitable?"; everything here is downstream of a `self_help`.

The one invariant this whole file exists to hold
------------------------------------------------
**The customer saying "done" is not restoration. The telemetry decides.**

`fetch_customer_responses` returns `customer_completed_step`, and it is tempting to read that as the
outcome -- it is the only unambiguous signal in the branch, and it arrives already parsed. It is not
the outcome. It is a report that somebody unplugged something, and the specification's D12 asks
whether self-help produced *stable restoration*. `verify_self_help` therefore takes the reply as
permission to look, never as the answer, and `SelfHelpSession.outcome` reaches `resolved` only when
`reachability_verdict` agrees. `routing.route_self_help_outcome` keys on exactly that word, so the
difference is the difference between closing an incident and rolling a truck.

The same rule read from the other end: the comms simulator's own `send_self_help` docstring refuses
to return a result because "the alternative -- returning 'self-help succeeded' from the send --
would let the workflow close an incident on the strength of having asked". This branch is the
caller that promise was made to.

Why the wait is three nodes and not one
---------------------------------------
`graph.interrupts` sets out why asking takes two nodes: a node writes state only by returning, and a
gate does not return until it is answered, so one node cannot record that it *is* waiting. Self-help
needs three, and the third is forced by `domain.lifecycle`. Measured against `require_transition`:

    diagnosing        -> self_help          OK
    awaiting_approval -> self_help          OK
    self_help         -> awaiting_customer  OK
    diagnosing        -> awaiting_customer  IllegalTransitionError
    self_help         -> awaiting_approval  IllegalTransitionError

So the incident cannot reach `awaiting_customer` in one move from where the branch starts, and a
node returns one status. Hence `send_self_help_instructions` (-> `self_help`), then
`mark_awaiting_customer` (-> `awaiting_customer`, and the deadline), then `await_customer_response`,
which raises the interrupt and builds nothing.

The last of those three transitions is also why `select_self_help_script` sets no status at all --
the same choice `select_remote_action` makes, but here it is load-bearing rather than tidy. Setting
`self_help` on selection would put the incident one illegal move away from the approval gate, and
`advance_status` raises: an incident that needed a supervisor's signature would crash instead of
asking for one.

The one router, wired on two edges
----------------------------------
`route_self_help_gate` is attached to both `select_self_help_script` and
`request_self_help_approval`, for the reason `remote_resolution` gives at length: after selection
and after an answer the question is identical, and two routers would be two spellings of it.

It keys on `decision.required_approval_kind` rather than naming a kind. `send_self_help` carries no
`approval_kind` in the pack -- it is `{ allowed: true, risk: low }` -- so the gate looks dead. It is
not: `PolicyEngine._check_rca_confidence` attaches `low_confidence_rca` to *any* write whose RCA is
below the bar for its decision class, and asking a customer to go and unplug their gateway on a
hypothesis nobody is confident in is exactly the case that demand exists for.

What is not here
----------------
`route_self_help_outcome` (D12) is not wired, for the same reason D10 is not wired in the remote
branch: its destinations -- validation, a fresh diagnostic cycle, field planning -- are all outside
this graph, and a subgraph cannot route to a sibling it does not contain.

Where the parent cannot see this
--------------------------------
Twice over. While `request_self_help_approval` or `await_customer_response` is paused, the status
and the pending question live in *this* graph's checkpoint; reading the parent reports
`diagnosing`. See `graph.interrupts` and use `graph.inspect.pending_approval_for`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from lpr_cpe.domain.enums import (
    ActionOutcome,
    ApprovalKind,
    CommunicationChannel,
    EvidenceKind,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
    SelfHelpOutcome,
)
from lpr_cpe.domain.governance import ActionRecord, ActionRequest, PolicyDecision
from lpr_cpe.domain.resolution import ResolutionOption, SelfHelpSession
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
    approval_granted,
    approval_outstanding,
    first_actionable_option,
    is_self_help_option,
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
from lpr_cpe.observability.kpi import MetricTimestamp, mark

#: The two things a customer can tell us that mean anything to this branch. Anything else --
#: silence, a malformed webhook body, free text we cannot parse -- is `None`, which is *not* a
#: decline. See `customer_reply`.
_UNDERSTOOD_REPLIES: frozenset[str] = frozenset({"completed", "declined"})


def selected_self_help_option(state: IncidentState) -> ResolutionOption | None:
    """The option `select_self_help_script` chose, or `None` if it chose nothing.

    The mirror of `remote_resolution.selected_remote_option`, and named for the same reason: the
    plan's `selected_option_id` is the one owner of "which option is this branch about", and a
    second reader reaching for `first_actionable_option` instead would answer about the next option
    whenever policy had just blocked the current one.
    """
    plan = state.get("resolution_plan")
    return plan.selected if plan is not None else None


def script_id_of(option: ResolutionOption) -> str:
    """Which self-help script this option means. Empty when the option does not carry one.

    Read off `parameters`, where `decision_services.resolution` puts it from the catalogue entry.
    Empty is passed through to the adapter rather than substituted here: `send_self_help` defaults a
    missing `script_id` to `reboot_gateway`, and a *second* default in this file would mean the
    instruction the customer receives depends on which of the two ran first.
    """
    return str(option.parameters.get("script_id") or "")


# ------------------------------------------------------------------------------------------------
# P13a -- select
# ------------------------------------------------------------------------------------------------


@node("select_self_help_script")
async def select_self_help_script(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Choose the instructions D11 meant, put them to the policy engine, and record both.

    The mirror of `select_remote_action`, including its refusal to set a status -- see the module
    docstring for why that refusal is not cosmetic here.

    A self-help send is a *customer contact*, so this is the first node in the codebase whose policy
    evaluation can be refused by the daily contact cap and the quiet-hours window.
    `_shared.policy_input_for` supplies `contacts_today`, `minutes_since_last_contact` and
    `local_time` for every action precisely so that this call needed no special case: the branch
    that made those fields matter is this one, and it got them by not being the branch that had to
    remember them.
    """
    plan = state.get("resolution_plan")
    option = first_actionable_option(state, is_self_help_option)
    cycle = state.get("diagnostic_cycles", 1)
    if plan is None or option is None:
        # D11 said self-help was suitable and by the time we got here nothing was left to send.
        # Recordable rather than exceptional, for the reason `select_remote_action` gives: policy
        # blocked the last candidate in between, and D12 will send this round again.
        return {
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="select_self_help_script",
                    action="select_self_help_script",
                    outcome="no_actionable_self_help_option",
                    reason_code=ReasonCode.SELF_HELP_DECLINED,
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
                node="select_self_help_script",
                action="select_self_help_script",
                outcome=decision.outcome.value,
                subject_ref=option.target_ref,
                reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                detail={
                    "cycle": cycle,
                    "option_id": option.option_id,
                    "script_id": script_id_of(option),
                    "attempt": attempt_number(state, option.action_type),
                    "requires_customer_present": option.requires_customer_present,
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
    # `preview`, not `state`. The decision this KPI must count is the one this node just made and
    # has not yet returned; `emit_kpi` swallows `KPINotDerivableError` by design, so reading the raw
    # state produces no event and no complaint. See `remote_resolution.select_remote_action`.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.POLICY_BLOCK_RATE,
        node="select_self_help_script",
        dimensions={"action_type": option.action_type.value},
        discriminator=cycle,
    )
    return update


# ------------------------------------------------------------------------------------------------
# The gate
# ------------------------------------------------------------------------------------------------


def route_self_help_gate(state: IncidentState) -> Literal["approve", "send", "abandon"]:
    """May these instructions go out now? Wired out of selection *and* out of the gate.

    Byte-for-byte the shape of `route_remote_gate`, and deliberately so: the two branches ask one
    question about two action classes, and the failure mode of writing it twice is that only one of
    them learns about rejection. Total, and conservative in every unset case.
    """
    option = selected_self_help_option(state)
    if option is None:
        return "abandon"
    decision = latest_policy_decision(state, option.action_type)
    if decision is None or decision.blocked:
        return "abandon"
    if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL:
        kind = decision.required_approval_kind
        if kind is None:
            # `PolicyDecision` validates this away, so it is unreachable through the engine.
            # Abandoning rather than asserting keeps the router's never-raises promise.
            return "abandon"
        if approval_outstanding(state, kind):
            return "approve"
        return "send" if approval_granted(state, kind) else "abandon"
    return "send"


def _demanded_approval(
    state: IncidentState,
) -> tuple[ResolutionOption, PolicyDecision, ApprovalKind]:
    """The option, its decision and the approval kind demanded. Raises on any of the three.

    Every caller is reached only through `route_self_help_gate`'s `approve` branch, which has
    already established all three; arriving without them means an edge bypasses the router.
    """
    option = selected_self_help_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    kind = decision.required_approval_kind if decision is not None else None
    if option is None or decision is None or kind is None:
        raise ValueError(
            "the self-help approval gate was reached without a selected option, a policy decision "
            f"and an approval kind (option={option is not None}, decision={decision is not None}, "
            f"kind={kind}). Only `route_self_help_gate`'s `approve` branch may lead here, and it "
            "checks all three."
        )
    return option, decision, kind


@node("prepare_self_help_approval")
async def prepare_self_help_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write the question down, then return so it is committed before anyone is asked.

    The question names the *script*, not the action type. An approver asked to authorise
    `send_self_help` is being asked to authorise a category; one asked to authorise "ask the
    customer to move their device closer to the gateway" is being asked about the thing that will
    actually happen in someone's home, and can refuse it on grounds the category hides.
    """
    option, decision, kind = _demanded_approval(state)
    attempt = attempt_number(state, option.action_type)
    script_id = script_id_of(option)
    request = build_request(
        state,
        ctx,
        kind=kind,
        question=(
            f"Approve sending self-help instructions ({script_id or 'default script'}) to the "
            f"customer for {option.target_ref}? This is attempt {attempt} of this action for the "
            "incident."
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
            "script_id": script_id,
            "estimated_success_probability": option.estimated_success_probability,
            "estimated_minutes": option.estimated_duration.total_seconds() / 60.0,
            "customer_disruption": option.customer_disruption,
            "requires_customer_present": option.requires_customer_present,
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
                node="prepare_self_help_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=option.target_ref,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "approval_id": request.approval_id,
                    "kind": kind.value,
                    "attempt": attempt,
                    "script_id": script_id,
                    "required_role": request.required_role,
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_self_help_approval")
async def request_self_help_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Builds nothing; see `graph.interrupts`."""
    return request_approval(state, ctx)


# ------------------------------------------------------------------------------------------------
# P13b -- send
# ------------------------------------------------------------------------------------------------


def _approval_ref(state: IncidentState, decision: PolicyDecision) -> str | None:
    """The approval this send runs under, or `None` when the pack demanded none.

    Read off `ApprovalDecision.approval_ref`, a derived property, so the reference on the action
    cannot disagree with the approval it names. `ActionRequest` refuses to construct without one
    when the outcome is `REQUIRES_APPROVAL`.
    """
    if decision.outcome is not PolicyOutcome.REQUIRES_APPROVAL:
        return None
    kind = decision.required_approval_kind
    answer = latest_decision_of(state, kind) if kind is not None else None
    return answer.approval_ref if answer is not None else None


@node("send_self_help_instructions")
async def send_self_help_instructions(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P13. Read the device, send the instructions, open the session.

    The pre-read is taken here, immediately before the send, and it is what makes the later
    verification a comparison rather than a snapshot -- the same argument `execute_remote_repair`
    makes, and it matters more here: the gap between reading and judging is a customer's response
    time, not a TR-069 round trip, so a pre-reading taken back at diagnosis could be an hour stale.

    **No `response_wait_minutes` is passed, and that is a recorded gap rather than an oversight.**
    The pack has no self-help response window; the choices were to invent a number here, to multiply
    the script's `expected_minutes` by a factor nobody has measured, or to let the adapter's stated
    30-minute default stand and say so. The deadline is then read back *off the adapter's result*
    rather than recomputed, so the deadline the session enforces is the one the customer was
    actually given. See `docs/vendor-integration-gaps.md`.

    Language and channel are likewise not supplied. `_resolve_language` defaults to Spanish, which
    is the right default in Puerto Rico, and `_masked_destination` documents that a real deployment
    addresses the send from the customer record. Passing a destination we do not have would be
    inventing one; the specification's "customer language and support preference" input to D11 has
    no home in state yet and is recorded with the rest.

    `self_help_attempt_count` is written as an **absolute** count. It reduces with `take_max`, and
    an increment computed from a value read at entry is what that reducer exists to defeat.
    """
    plan = state.get("resolution_plan")
    option = selected_self_help_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    if plan is None or option is None or decision is None:
        raise ValueError(
            "send_self_help_instructions was reached with no resolution plan, no selected option "
            "or no policy decision for it. Every path here runs through `route_self_help_gate`, "
            "which abandons in all three cases."
        )

    now = ctx.clock.now()
    attempt = attempt_number(state, option.action_type)
    cycle = state.get("diagnostic_cycles", 1)
    script_id = script_id_of(option)
    idempotency_key = idempotency_key_for(state, option)
    action_id = derive_id("ACT", state.get("incident_id") or "", option.option_id)

    gathered = Gathered(ctx, assessed_at=now)
    cpe_ref = state.get("cpe_ref") or option.target_ref
    pre_state = await gathered.fetch(
        "cpe_status_pre_self_help",
        ctx.adapters.cpe.read_status(cpe_ref),
        freshness=Freshness.TELEMETRY,
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

    result = await ctx.adapters.communications.send_self_help(request)
    completed_at = ctx.clock.now()
    outcome = ActionOutcome(str(result["outcome"]))

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

    session = SelfHelpSession(
        session_id=derive_id("SHS", request.incident_id, option.option_id),
        incident_id=request.incident_id,
        channel=CommunicationChannel(str(result.get("channel") or "sms")),
        started_at=now,
        steps_sent=[script_id or str(result.get("script_id") or "")],
        step_index=1,
        pre_state=dict(pre_state or {}),
        notes=[str(result.get("detail") or "")],
    )

    attempted = list(plan.attempted_option_ids)
    if option.option_id not in attempted:
        attempted.append(option.option_id)

    # The record of having contacted the customer, in the field the state contract set aside for it.
    #
    # Nothing wrote `customer_communications` before this node, and it is not an empty formality:
    # `KPICalculator.customer_contacts_per_incident` counts exactly this list, so an unwritten list
    # made the KPI report *zero contacts* for an incident that had just messaged the customer -- and
    # report it confidently, because that calculator never returns `None`. A silent wrong number is
    # worse than a missing one, and this is the node that knows the truth.
    #
    # Keyed on `action_id`, which `state._KEY_ATTRS` recognises, so `append_unique` collapses a
    # replay of this node instead of double-counting the message. The masked destination is copied
    # from the adapter rather than re-derived, for the same reason the audit detail copies it.
    #
    # This node is currently the only writer, and a contact cap enforced against a list one node
    # writes is not a cap -- gap SELFHELP-4. Two of these fields also read empty or defaulted today:
    # `destination_masked` is `None` because nothing in state holds a customer's address, and
    # `language` is the adapter's fallback rather than a recorded preference -- gap SELFHELP-3.
    communication = {
        "action_id": action_id,
        "incident_id": request.incident_id,
        "session_id": session.session_id,
        "direction": "outbound",
        "purpose": "self_help_instructions",
        "channel": str(result.get("channel") or ""),
        "language": str(result.get("language") or ""),
        "script_id": script_id,
        "destination_masked": result.get("destination_masked"),
        "sent_at": now.isoformat(),
        "simulated": bool(result.get("simulated")),
        "outcome": outcome.value,
    }

    update: NodeUpdate = {
        "status": IncidentStatus.SELF_HELP,
        "selected_action": request,
        "self_help_session": session,
        "action_history": [record],
        "customer_communications": [communication],
        "resolution_plan": plan.model_copy(update={"attempted_option_ids": attempted}),
        "self_help_attempt_count": attempt,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "updated_at": completed_at,
        **mark(MetricTimestamp.FIRST_ACTION_AT, now),
        "audit_events": [
            audit(
                state,
                ctx,
                node="send_self_help_instructions",
                action="send_self_help_instructions",
                outcome=outcome.value,
                subject_ref=option.target_ref,
                reason_code=request.reason_code,
                detail={
                    "cycle": cycle,
                    "attempt": attempt,
                    "action_id": action_id,
                    "session_id": session.session_id,
                    "script_id": script_id,
                    "channel": result.get("channel"),
                    "language": result.get("language"),
                    "idempotency_key": idempotency_key,
                    "approval_ref": request.approval_ref,
                    "policy_decision_id": decision.decision_id,
                    "policy_outcome": decision.outcome.value,
                    "simulated": bool(result.get("simulated")),
                    "replayed": bool(result.get("replayed")),
                    "external_ref": result.get("external_ref"),
                    # The destination is already masked by the adapter, at the boundary. Copied
                    # rather than re-derived so nothing in the graph ever holds the unmasked form.
                    "destination_masked": result.get("destination_masked"),
                    "expected_minutes": result.get("expected_minutes"),
                    "response_deadline": result.get("response_deadline"),
                },
                discriminator=action_id,
            )
        ],
    }
    # Both read facts this node is in the middle of writing -- the executed `ActionRecord` and the
    # `customer_communications` entry above -- so both are given `preview`, once, rather than the
    # raw state. `select_self_help_script` carries the full explanation.
    measured = preview(state, update)
    update["kpi_events"] = [
        *emit_kpi(
            measured,
            ctx,
            KPIName.AUTOMATION_COVERAGE_RATE,
            node="send_self_help_instructions",
            dimensions={"action_type": option.action_type.value},
            discriminator=action_id,
        ),
        # The only writer of this metric, and the only writer of the list it counts. That is not a
        # coincidence: the cap `PolicyEngine._check_customer_contact` enforces, the history
        # `_shared.contact_history` reads and the rate reported here have to describe one set of
        # events, or the dashboard will disagree with the refusals. The first two count
        # `CUSTOMER_CONTACT_ACTIONS` in `action_history`; this one counts
        # `customer_communications`, and they agree only because this node appends to both in the
        # same update. A second contact channel added anywhere else must do the same.
        *emit_kpi(
            measured,
            ctx,
            KPIName.CUSTOMER_CONTACTS_PER_INCIDENT,
            node="send_self_help_instructions",
            dimensions={"channel": str(result.get("channel") or "")},
            discriminator=action_id,
        ),
    ]
    return update


@node("mark_awaiting_customer")
async def mark_awaiting_customer(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Record that we are now waiting, and until when. Returns; it does not ask.

    The first half of the ask, and it is a node of its own for two reasons that happen to coincide.
    `graph.interrupts` gives the general one: a node that raised the interrupt could not also have
    written `awaiting_customer`, because it does not return until the customer has already answered
    -- the state would claim to be waiting for a reply it was holding. The lifecycle table gives the
    specific one: `diagnosing -> awaiting_customer` raises, so the move has to be made from
    `self_help`, which is the status the send node has just committed.

    The deadline is the adapter's, and this node reads it rather than computing one.
    `send_self_help` resolves the response window itself -- `response_wait_minutes` from the
    request's parameters, its own 30-minute default when nothing supplies one, which nothing does
    because the pack has no self-help window (gap SELFHELP-2) -- and returns the absolute instant
    as `response_deadline`. That is the deadline the *customer* was given. A second computation here
    would be a second policy, agreeing with the first only until either changed, and the symptom
    would be an incident timed out before the window the customer was told about had run.

    So `_deadline_from` reads it back off the send's audit event. When the adapter returned no
    deadline the session keeps `response_deadline=None` and `SelfHelpSession.timed_out` is `False`
    forever -- an unbounded wait rather than an invented one. That is the honest failure: it shows
    up as an incident that stays pending, not as a customer wrongly recorded as unresponsive.
    """
    session = state.get("self_help_session")
    if session is None:
        raise ValueError(
            "mark_awaiting_customer was reached with no self-help session. Only "
            "`send_self_help_instructions` leads here, and it always opens one."
        )
    now = ctx.clock.now()
    waiting = session.model_copy(
        update={"awaiting_response_since": now, "response_deadline": _deadline_from(state)}
    )
    return {
        "status": IncidentStatus.AWAITING_CUSTOMER,
        "self_help_session": waiting,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="mark_awaiting_customer",
                action="await_customer_response",
                outcome="awaiting_customer",
                subject_ref=waiting.incident_id,
                detail={
                    "session_id": waiting.session_id,
                    "script_id": waiting.steps_sent[-1] if waiting.steps_sent else None,
                    "channel": waiting.channel.value,
                    "response_deadline": (
                        waiting.response_deadline.isoformat()
                        if waiting.response_deadline is not None
                        else None
                    ),
                },
                discriminator=waiting.session_id,
            )
        ],
    }


def _deadline_from(state: IncidentState) -> datetime | None:
    """The deadline the adapter gave this send, read back off the audit trail.

    Read rather than recomputed; `mark_awaiting_customer` says why. Takes no `now`, deliberately:
    an absolute instant is what the adapter returned and what the customer was told, so there is
    nothing here for a clock to be relative to. A signature that accepted one would invite the
    recomputation it exists to prevent.

    The **latest** send wins, which matters on the second script: a branch that came back around
    has two of these events and the older deadline has already expired. Reversed iteration, and the
    first match returns even when its deadline is `None` -- falling through to an earlier event
    would resurrect a stale window for a send that deliberately set none.

    `str` is what crosses this boundary, because the audit detail is JSON-serialisable by contract
    and `simulated_base._offset_hours` renders ISO-8601. The `datetime` arm is not dead: an audit
    trail round-tripped through the checkpointer is not guaranteed to have been through JSON, and a
    helper that only handled one of the two would fail on whichever the caller happened not to have.

    A naive parse returns `None` rather than a bare instant. `SelfHelpSession.timed_out` compares
    against `ctx.clock.now()`, which is aware, and comparing the two raises `TypeError` -- inside a
    property, on some later node, long after the reading that caused it. Unbounded is the failure
    this branch already knows how to survive.
    """
    for event in reversed(state.get("audit_events", [])):
        if event.node != "send_self_help_instructions":
            continue
        raw = event.detail.get("response_deadline")
        if raw is None:
            return None
        parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo is not None else None
    return None


# ------------------------------------------------------------------------------------------------
# P13c -- the customer-response interrupt
# ------------------------------------------------------------------------------------------------


def customer_reply(answer: Any) -> str | None:
    """`"completed"`, `"declined"`, or `None` for "nothing usable came back".

    One parser for two channels, and that is the point of it being a function. The resume value
    arrives from an HTTP endpoint; `fetch_customer_responses` returns rows from the adapter's
    ledger. Both spell the answer `response: "completed" | "declined"`, and both may also carry the
    boolean `customer_completed_step`. Two parsers would eventually read one webhook two ways.

    Total, and `None` is deliberately **not** a decline. A garbled body, a resume with no payload,
    a timer tick that woke the graph to ask whether anything had arrived -- all of those mean *we
    still do not know*, and reading them as a refusal would end a customer's window early and roll
    a truck at them on the strength of a parse failure. `route_customer_answer` sends `None` back
    to the wait, where the deadline is the only thing that ends it.
    """
    if not isinstance(answer, dict):
        return None
    raw = str(answer.get("response") or "").strip().lower()
    if raw in _UNDERSTOOD_REPLIES:
        return raw
    completed = answer.get("customer_completed_step")
    if isinstance(completed, bool):
        return "completed" if completed else "declined"
    return None


@node("await_customer_response")
async def await_customer_response(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Pause until the customer answers, then record what they said. **Not whether it worked.**

    The interrupt is raised *first* and the adapter consulted only when the resume value carried
    nothing usable. The other order is tempting -- check for a reply, skip the pause if one is
    already there -- and it is wrong: everything before `interrupt()` re-runs on resume, so a
    pre-interrupt fetch that found a reply would return early on the resume pass and silently
    discard the answer the operator had just supplied. Resume value first makes the resume channel
    authoritative, which is what it is.

    The adapter fallback is what makes a timer-driven resume useful: a scheduler that wakes the
    incident with no payload gets "has anything arrived out of band?" answered, rather than being
    treated as the customer's reply. Rows are matched on the script we sent, because
    `fetch_customer_responses` returns every self-help reply for the incident and a second script in
    a later cycle must not be answered by the first one's reply.

    Three things end the wait and each writes `completed_at`, which is what
    `SelfHelpSession.awaiting_customer` reads and therefore what the router keys on:

    * a reply we understood -- `declined` is terminal here, `completed` sends us to the telemetry;
    * silence past the deadline -- `timed_out`;
    * nothing else. Silence before the deadline leaves the session untouched and still waiting.
    """
    session = state.get("self_help_session")
    if session is None:
        raise ValueError(
            "await_customer_response was reached with no self-help session. Only "
            "`mark_awaiting_customer` leads here, and it always records one."
        )

    answer = interrupt(
        {
            "customer_response_request": {
                "incident_id": session.incident_id,
                "session_id": session.session_id,
                "script_id": session.steps_sent[-1] if session.steps_sent else None,
                "channel": session.channel.value,
                "awaiting_since": (
                    session.awaiting_response_since.isoformat()
                    if session.awaiting_response_since is not None
                    else None
                ),
                "response_deadline": (
                    session.response_deadline.isoformat()
                    if session.response_deadline is not None
                    else None
                ),
            },
            "accepted_responses": sorted(_UNDERSTOOD_REPLIES),
        }
    )

    reply = customer_reply(answer)
    source = "resume"
    if reply is None:
        rows = await ctx.adapters.communications.fetch_customer_responses(session.incident_id)
        script = session.steps_sent[-1] if session.steps_sent else ""
        for row in rows:
            if script and str(row.get("script_id") or "") != script:
                continue
            reply = customer_reply(row)
            if reply is not None:
                source = "adapter"
                break

    now = ctx.clock.now()
    responses = list(session.customer_responses)
    if reply is not None:
        responses.append(reply)

    if reply == "declined":
        updated = session.model_copy(update={"customer_responses": responses}).complete(
            SelfHelpOutcome.DECLINED,
            at=now,
            note="the customer declined to carry out the step",
            reason_code=ReasonCode.SELF_HELP_DECLINED,
        )
        outcome = "declined"
    elif reply == "completed":
        # Left `in_progress`, and emphatically not `resolved`. The wait is over; the verification
        # has not happened. `verify_self_help` is the only node that may write that word, and it
        # writes it only when the telemetry agrees. See the module docstring.
        updated = session.model_copy(
            update={
                "customer_responses": responses,
                "completed_at": now,
                "notes": [
                    *session.notes,
                    "the customer reported the step complete; not yet verified",
                ],
            }
        )
        outcome = "completed"
    elif session.timed_out(now):
        updated = session.complete(
            SelfHelpOutcome.TIMED_OUT,
            at=now,
            note="no reply arrived before the deadline",
            reason_code=ReasonCode.SELF_HELP_TIMED_OUT,
        )
        outcome = "timed_out"
    else:
        # Still waiting, and the session is returned unchanged so that `awaiting_response_since`
        # keeps its original value: the wait is measured from when the customer was asked, not from
        # the last time something woke us up to check.
        updated = session
        outcome = "still_waiting"

    return {
        "self_help_session": updated,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="await_customer_response",
                action="await_customer_response",
                outcome=outcome,
                subject_ref=session.incident_id,
                reason_code=updated.reason_code,
                detail={
                    "session_id": session.session_id,
                    "reply": reply,
                    "reply_source": source if reply is not None else None,
                    "deadline_passed": session.timed_out(now),
                    "response_deadline": (
                        session.response_deadline.isoformat()
                        if session.response_deadline is not None
                        else None
                    ),
                },
                # Keyed on the outcome as well as the session so that a wait resumed twice -- a
                # timer tick, then the real reply -- records both, rather than de-duplicating the
                # answer away into the tick that preceded it.
                discriminator=f"{session.session_id}:{outcome}",
            )
        ],
    }


def route_customer_answer(state: IncidentState) -> Literal["verify", "wait", "abandon"]:
    """Where a resumed wait goes. Reads `SelfHelpSession`; decides nothing.

    `awaiting_customer` is the model's own property (`awaiting_response_since` set and
    `completed_at` unset), so "are we still waiting?" has one owner and this router is not a second
    definition of it. `wait` re-enters `await_customer_response`, which pauses again -- the cycle
    cannot spin, because every pass through it yields to an external event, and the deadline ends
    it.
    """
    session = state.get("self_help_session")
    if session is None:
        return "abandon"
    if session.awaiting_customer:
        return "wait"
    if session.outcome in {SelfHelpOutcome.DECLINED, SelfHelpOutcome.TIMED_OUT}:
        return "abandon"
    return "verify"


# ------------------------------------------------------------------------------------------------
# P13d -- verify
# ------------------------------------------------------------------------------------------------


@node("verify_self_help")
async def verify_self_help(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Read the device again and say, on the record, whether the customer's step restored service.

    Reached only when the customer said they did it. The reply is what earns the reading, and the
    reading is what decides the outcome -- `reachability_verdict`, the same function and therefore
    the same criterion `verify_remote_repair` applies to its own reboot.

    `resolved` needs `verification_passed is True`. The three-valued verdict's `None` -- "the device
    was online throughout, so this adapter cannot tell" -- is recorded as `not_resolved`, because
    `SelfHelpSession.outcome` has no fifth word and because not-knowing is not restoration. The
    summary keeps the distinction that the outcome word loses, and it is the summary a human reads
    before deciding whether the truck was necessary.

    **On today's fixture set the `resolved` branch is not reachable end to end**, and that is a
    property of the simulators rather than of this node. No adapter models the physical effect of a
    customer completing a step: the CPE simulator recovers a device for the actions *it* applies,
    and a hand power-cycle is not one of them. So the customer-environment fixture -- an online
    gateway, a static `congested_2g` profile -- lands on `None` however the customer answers.

    That is gap SELFHELP-1, and the question underneath it is what evidence should close a self-help
    session at all: reachability is the only symptom a TR-069 read exposes, and a Wi-Fi coverage
    complaint is exactly the fault it cannot see. The verdict itself is covered directly by a test
    that supplies the pre- and post-readings, so the arm is exercised rather than merely excused.
    """
    session = state.get("self_help_session")
    if session is None:
        raise ValueError(
            "verify_self_help was reached with no self-help session. Only "
            "`route_customer_answer`'s `verify` branch leads here, and it checks for one."
        )

    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    cpe_ref = state.get("cpe_ref") or session.incident_id
    post_state = await gathered.fetch(
        "cpe_status_post_self_help",
        ctx.adapters.cpe.read_status(cpe_ref),
        freshness=Freshness.TELEMETRY,
    )
    passed, summary = reachability_verdict(session.pre_state or None, post_state)

    evidence = make_evidence(
        state,
        ctx,
        node="verify_self_help",
        kind=EvidenceKind.CPE_STATUS,
        subject_ref=cpe_ref,
        summary=f"post-self-help verification: {summary}",
        source_system="cpe",
        payload=dict(post_state or {}),
        discriminator=session.session_id,
    )

    if passed:
        outcome, reason = SelfHelpOutcome.RESOLVED, ReasonCode.SELF_HELP_SUCCEEDED
    else:
        # `None` and `False` share the word and not the sentence. Neither is a restoration, and the
        # reason code is left unset for both rather than borrowed from a code that asserts something
        # we have not established -- an audit event with no reason code reads as "not applicable",
        # which for "the customer complied and we cannot tell" is exactly right.
        outcome, reason = SelfHelpOutcome.NOT_RESOLVED, None

    verified = session.model_copy(update={"post_state": dict(post_state or {})}).complete(
        outcome, at=now, note=summary, reason_code=reason
    )

    update: NodeUpdate = {
        "status": IncidentStatus.SELF_HELP,
        "self_help_session": verified,
        "evidence": [evidence],
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="verify_self_help",
                action="verify_self_help",
                outcome=outcome.value,
                subject_ref=cpe_ref,
                reason_code=reason,
                detail={
                    "session_id": session.session_id,
                    "script_id": session.steps_sent[-1] if session.steps_sent else None,
                    "verification_passed": passed,
                    "summary": summary,
                    "evidence_ref": evidence.ref,
                },
                discriminator=session.session_id,
            )
        ],
    }
    # `preview`, not `state`. `self_help_success_rate` returns `None` while the session reads
    # `in_progress`, and `in_progress` is exactly what `state` still holds -- this node is the one
    # replacing it. Measured: against the raw state this node emitted nothing, on every path,
    # including the `resolved` one it exists to report.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.SELF_HELP_SUCCESS_RATE,
        node="verify_self_help",
        dimensions={"script_id": session.steps_sent[-1] if session.steps_sent else ""},
        discriminator=session.session_id,
    )
    return update


@node("abandon_self_help")
async def abandon_self_help(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Leave the branch without a verified fix, and say which of the four ways in the record.

    This node exists for the status, exactly as `abandon_remote_action` does: an incident left at
    `awaiting_approval` or `awaiting_customer` would checkpoint as waiting for something that has
    already happened. `diagnosing` is the honest reading, and D12 decides where it goes from there.

    Four ways in, and they are not interchangeable to anyone reading the incident afterwards: policy
    blocked the send, an approver refused it, the customer declined, or the customer never answered.
    The last two are the ones that matter operationally -- a decline is a customer who will not do
    it, and a silence is a customer who may not have seen it, and the second is worth a different
    contact before it is worth a truck.

    A no-op transition is legal, so the status write is unconditional.

    **This node emits `SELF_HELP_SUCCESS_RATE` too, and it has to.** `verify_self_help` is the only
    other emitter and it is only ever reached by a customer who complied, so a rate built from that
    node alone is computed over the compliant only -- every decline and every silence drops out of
    the denominator, and the reported success rate rises each time a customer refuses. That is the
    same bias `first_time_fix_rate` documents itself against, arrived at from the other side.

    Emitted unconditionally, because the calculator already draws the line in the right place:
    `self_help_success_rate` returns `None` for a session that is absent or still `in_progress`,
    which is exactly the three approach routes that should not count -- policy blocked the send, an
    approver refused it, no option was permitted. Nothing was asked of the customer on any of them,
    so there is no self-help attempt to score. Re-testing that here would be a second copy of the
    rule, and the two would diverge the first time either changed.

    Raw `state`, not `preview`, and the contrast with `verify_self_help` is the point: that node
    emits after *replacing* the session, so it must read its own write. This one only reads the
    outcome `await_customer_response` already committed, and reducing an update that does not touch
    the session would say nothing new.
    """
    session = state.get("self_help_session")
    option = selected_self_help_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    answer = (
        latest_decision_of(state, decision.required_approval_kind)
        if decision is not None and decision.required_approval_kind is not None
        else None
    )
    cycle = state.get("diagnostic_cycles", 1)

    if session is not None and session.outcome is SelfHelpOutcome.DECLINED:
        outcome, reason = "customer_declined", ReasonCode.SELF_HELP_DECLINED
    elif session is not None and session.outcome is SelfHelpOutcome.TIMED_OUT:
        outcome, reason = "customer_did_not_respond", ReasonCode.SELF_HELP_TIMED_OUT
    elif answer is not None and not answer.granted:
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
    else:
        outcome, reason = "no_permitted_self_help_option", ReasonCode.SELF_HELP_DECLINED

    script_sent = session.steps_sent[-1] if session is not None and session.steps_sent else ""

    return {
        "status": IncidentStatus.DIAGNOSING,
        "pending_approval": None,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_self_help",
                action="abandon_self_help",
                outcome=outcome,
                subject_ref=option.target_ref if option is not None else None,
                reason_code=reason,
                detail={
                    "cycle": cycle,
                    "option_id": option.option_id if option is not None else None,
                    "session_id": session.session_id if session is not None else None,
                    "session_outcome": session.outcome.value if session is not None else None,
                    "policy_outcome": decision.outcome.value if decision is not None else None,
                    "approval_status": answer.status.value if answer is not None else None,
                    "approval_rationale": answer.rationale if answer is not None else "",
                },
                discriminator=cycle,
            )
        ],
        "kpi_events": emit_kpi(
            state,
            ctx,
            KPIName.SELF_HELP_SUCCESS_RATE,
            node="abandon_self_help",
            dimensions={"script_id": script_sent},
            discriminator=session.session_id if session is not None else cycle,
        ),
    }


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The eight nodes, in the order the specification walks them. Checked the same way `PARENT_NODES`
#: and `REMOTE_RESOLUTION_NODES` are, so a node registered under a name its decorator does not carry
#: fails on import rather than producing a graph whose topology and audit trail disagree.
SELF_HELP_NODES: tuple[tuple[str, Any], ...] = (
    ("select_self_help_script", select_self_help_script),
    ("prepare_self_help_approval", prepare_self_help_approval),
    ("request_self_help_approval", request_self_help_approval),
    ("send_self_help_instructions", send_self_help_instructions),
    ("mark_awaiting_customer", mark_awaiting_customer),
    ("await_customer_response", await_customer_response),
    ("verify_self_help", verify_self_help),
    ("abandon_self_help", abandon_self_help),
)

check_node_registry(SELF_HELP_NODES, "the self-help node registry")

#: Where each of `route_self_help_gate`'s answers goes. Read by the builder below and by the tests,
#: so the router's `Literal` and the wiring cannot drift apart silently.
GATE_TARGETS: dict[str, str] = {
    "approve": "prepare_self_help_approval",
    "send": "send_self_help_instructions",
    "abandon": "abandon_self_help",
}

#: The same, for `route_customer_answer`. `wait` points back at the node that raised the interrupt:
#: the branch's only cycle, and it is bounded by the deadline rather than by a step budget.
ANSWER_TARGETS: dict[str, str] = {
    "verify": "verify_self_help",
    "wait": "await_customer_response",
    "abandon": "abandon_self_help",
}


def build_self_help_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every edge is guarded, for the reason the parent's and the remote branch's are:
    `escalation_update` stops a node from doing work but does not stop the graph, and an unguarded
    subgraph would walk its remaining super-steps after the budget had been declared exhausted.

    `context_schema=GraphContext` is repeated rather than inherited -- a compiled subgraph is a
    graph in its own right, and `get_runtime(GraphContext)` inside its nodes resolves against *its*
    schema.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in SELF_HELP_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "select_self_help_script")

    gate_map: dict[Any, str] = {**GATE_TARGETS, ESCALATED: END}
    graph.add_conditional_edges("select_self_help_script", guarded(route_self_help_gate), gate_map)
    graph.add_conditional_edges(
        "request_self_help_approval", guarded(route_self_help_gate), gate_map
    )

    graph.add_conditional_edges(
        "prepare_self_help_approval",
        guarded(straight_on),
        {ONWARD: "request_self_help_approval", ESCALATED: END},
    )
    graph.add_conditional_edges(
        "send_self_help_instructions",
        guarded(straight_on),
        {ONWARD: "mark_awaiting_customer", ESCALATED: END},
    )
    graph.add_conditional_edges(
        "mark_awaiting_customer",
        guarded(straight_on),
        {ONWARD: "await_customer_response", ESCALATED: END},
    )
    answer_map: dict[Any, str] = {**ANSWER_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "await_customer_response", guarded(route_customer_answer), answer_map
    )
    graph.add_edge("verify_self_help", END)
    graph.add_edge("abandon_self_help", END)
    return graph


def compile_self_help_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission: a subgraph compiled as a node shares the
    parent's checkpointer, and handing this one its own would give the incident two places to be
    resumed from -- which for a branch with two separate pauses in it would be two wrong places.
    """
    return build_self_help_graph().compile(name="lpr_cpe_self_help")


__all__ = [
    "ANSWER_TARGETS",
    "GATE_TARGETS",
    "SELF_HELP_NODES",
    "abandon_self_help",
    "await_customer_response",
    "build_self_help_graph",
    "compile_self_help_graph",
    "customer_reply",
    "mark_awaiting_customer",
    "prepare_self_help_approval",
    "request_self_help_approval",
    "route_customer_answer",
    "route_self_help_gate",
    "script_id_of",
    "select_self_help_script",
    "selected_self_help_option",
    "send_self_help_instructions",
    "verify_self_help",
]
