"""Stage 3's remote branch, compiled and run against a device that is genuinely offline.

The fixture that makes this module possible is `SVC-UT-001-B-01`: healthy optical readings behind
an ONT that has not informed in 31 hours. Healthy plant plus a silent device localises to
`FaultDomain.CPE`, which is the one domain whose catalogue entry is four CPE actions -- so it is the
only service in the fixture set that reaches P12 with something P12 can actually execute. Every
other health either localises to plant (and D08 diverts it) or offers no console-executable repair.
Finding that out took a sweep of all six healths; it is recorded here so the next reader does not
repeat it.

The device also *recovers* from the repair, which is what makes `verification_passed is True`
reachable rather than merely representable. `test_adapters.py` established that the simulator models
the effect; this module establishes that the graph reads it back and records it. All four of the
domain's `cpe_*` options are in the simulator's recovering set -- measured, not assumed -- which is
why the `paused` fixture can pick among them on approval-kind grounds without weakening that.

What is deliberately not asserted here
--------------------------------------
`route_remote_outcome` (D10) is not exercised. Both of its destinations are outside this graph, so
it belongs on the parent's edge out of the subgraph node and is tested in `test_routing.py` against
constructed state. Asserting it here would be asserting it in the one place it is not wired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.decision_services.resolution import plan_resolution
from lpr_cpe.domain.boundaries import BACK_OFFICE_DOMAINS, crew_for
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    CaseType,
    CrewType,
    EventSource,
    FaultDomain,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.governance import ActionRecord, PolicyDecision
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.domain.resolution import RemoteAction
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED
from lpr_cpe.graph.routing import DEDICATED_GATE_APPROVAL_KINDS, is_remote_option
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.graph.subgraphs._shared import (
    attempt_number,
    evidence_support,
    executed_idempotency_keys,
)
from lpr_cpe.graph.subgraphs.remote_resolution import (
    GATE_TARGETS,
    REMOTE_RESOLUTION_NODES,
    build_remote_resolution_graph,
    route_remote_gate,
    selected_remote_option,
    verify_remote_repair,
)
from lpr_cpe.integrations.cpe.simulator import SUPPORTED_ACTIONS
from lpr_cpe.observability.kpi import KPICalculator
from lpr_cpe.policies.loader import load_pack

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: Healthy plant, silent ONT. See the module docstring for why this one service and no other.
OFFLINE_CPE_SERVICE = "SVC-UT-001-B-01"

APPROVAL = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the plant reads clean and the cheap repairs are spent; the firmware is the suspect",
}


class _Ticking(FrozenClock):
    """The same advance-on-read clock `test_builder.py` uses, and for the same reason: inside a
    compiled graph the test cannot advance the clock between nodes, and a verification read that
    carried the same timestamp as the action would make `evidence_age_minutes` zero by construction.

    Subclassed off `FrozenClock` so `local_now()` and `timezone` come from the production clock;
    see `test_builder.py` for why the hand-rolled version that stopped at `now()` was not enough.
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
            summary=f"no contact from {service['cpe_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


@pytest.fixture
async def paused(fixtures: Any) -> Any:
    """An incident carried through the parent to P11 and into the subgraph, stopped at the gate.

    The parent is run for real rather than hand-built. A constructed `resolution_plan` would let
    this module pass while `generate_resolution_options` offered something P12 cannot execute, which
    is precisely the coupling `test_only_cpe_executable_actions_can_reach_the_remote_branch` exists
    to police -- and a fabricated fixture would police it against itself.

    P11 is asked for rather than assumed, because since the resolution fork was wired the parent no
    longer stops there by itself: it answers D09 `remote` and enters this very subgraph. That it
    still pauses at all is luck this fixture happens to have -- the parent reaches the approval gate
    inside the fork, and a paused subgraph's writes never reach the parent, so the state came back
    pre-fork and looked untouched. The sibling self-help fixture has no gate on its path, ran the
    whole branch and escalated on the diagnostic-cycle budget, and thirteen of its tests failed as
    `assert () == ('await_customer_response',)`. Same premise, and only one of the two was told.

    Why the two cheap repairs are marked attempted
    ----------------------------------------------
    Left alone this fixture selects `cpe_reboot`, and a reboot is `risk: low,
    requires_approval: false` in the pack -- so the only thing that ever sent it to a gate was its
    `low_confidence_rca` demand, raised because the RCA reads 0.50. That kind belongs to D06
    (`DEDICATED_GATE_APPROVAL_KINDS`), and this gate now declines it, so the reboot no longer pauses
    here at all. Marking the two reversible repairs attempted promotes `cpe_firmware_update`, whose
    `risk: high` class demands `high_risk_remote_action` -- the kind this gate owns -- and which the
    engine was measured demanding even at confidence 0.50, `_most_restrictive` preferring it over the
    confidence rule. So the pause is now reached by this gate's own question rather than by
    intercepting another gate's.

    The options are still the ones `generate_resolution_options` produced; only the attempt log is
    seeded, which is why this does not become the fabricated plan the paragraph above refuses.
    `cpe_factory_reset` would not serve: the engine returns `blocked` for it here.
    """
    service = fixtures.services[OFFLINE_CPE_SERVICE]
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    parent_final = await parent.ainvoke(
        _initial(service), context=ctx, config={"configurable": {"thread_id": "parent-remote"}}
    )
    plan = parent_final["resolution_plan"]
    parent_final = {
        **parent_final,
        "resolution_plan": plan.model_copy(
            update={
                "attempted_option_ids": [
                    option.option_id
                    for option in plan.ranked()
                    if option.action_type in {ActionType.CPE_REBOOT, ActionType.CPE_RESYNC}
                ]
            }
        ),
    }

    graph = build_remote_resolution_graph().compile(
        name="lpr_cpe_remote_resolution", checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": f"remote-{OFFLINE_CPE_SERVICE}"}}
    first = await graph.ainvoke(parent_final, context=ctx, config=config)
    return graph, ctx, config, first


# ------------------------------------------------------------------------------------------------
# The invariant that keeps a non-CPE action out of the CPE adapter
# ------------------------------------------------------------------------------------------------


def test_only_cpe_executable_actions_can_reach_the_remote_branch() -> None:
    """No fault domain D08 forwards may offer a `remote` option the CPE adapter cannot perform.

    `execute_remote_repair` hands the selected option straight to `ctx.adapters.cpe.apply_action`,
    and that method *raises* `AdapterError` on an action outside `SUPPORTED_ACTIONS` -- measured,
    not assumed: driving `raise_mr` into the subgraph produces

        AdapterError: cpe: raise_mr is not a CPE action; known: bulk_config_push, ...

    and `@node` deliberately does not catch it, so the incident dies mid-branch.

    Nothing prevents that today except an agreement between three modules that never mention each
    other. `is_remote_option` classifies by logistics -- no truck, no customer -- which is the right
    axis for D08/D09/D11 and says nothing about whether a device can execute the thing. The
    catalogue does offer options that satisfy it and are not CPE actions: `olt_port_reset` and
    `node_level_reset` under `node_or_olt`, `raise_mr` under `headend_or_co` and `service_platform`,
    `notify_customer` under `power`. Every one of them is safe *only* because `route_shared_or_plant`
    diverts its domain first.

    So the invariant is real, load-bearing, and written down nowhere. It is written down here. Add a
    catalogue row for a non-CPE action under `cpe`, `customer_environment`, `drop`,
    `inside_home_wiring` or `tap_or_odp` -- or widen D08 so a diverted domain stops being diverted --
    and this goes red instead of a live incident going red.

    Narrowing `is_remote_option` was considered and rejected. The three predicates partition the plan
    exactly once on (truck, customer); adding "and the CPE can do it" to one of them leaves
    `olt_port_reset` in no class at all, which trades a guarded crash for an option that silently
    stops being offered.
    """
    pack = load_pack()
    offenders: list[str] = []
    for domain in FaultDomain:
        if crew_for(domain) is CrewType.DIRTY or domain in BACK_OFFICE_DOMAINS:
            continue  # D08 diverts it; it never reaches D09.
        plan = plan_resolution(
            plan_id="RPLAN-invariant",
            created_at=NOW,
            fault_domain=domain,
            target_ref="CPE-INVARIANT",
            allowlist=pack.remote_actions,
            blast_radius_policy=pack.blast_radius,
        )
        offenders.extend(
            f"{domain.value}/{option.action_type.value}"
            for option in plan.options
            if is_remote_option(option) and option.action_type not in SUPPORTED_ACTIONS
        )

    assert not offenders, (
        f"{offenders} would be selected by `select_remote_action` and then raise AdapterError in "
        "`execute_remote_repair`. Either the CPE adapter must learn the action, or the catalogue "
        "must not offer it for a domain D08 forwards, or D08 must divert that domain."
    )


def test_the_offline_cpe_fixture_still_reaches_a_remote_repair(fixtures: Any) -> None:
    """The control for the fixture this whole module depends on.

    Every behavioural test below routes through `SVC-UT-001-B-01` and would pass vacuously -- by
    abandoning at the gate and asserting nothing about execution -- if that service stopped
    localising to `FaultDomain.CPE`. This asserts the precondition separately so the failure names
    the fixture rather than showing up as six confusing assertion errors about missing actions.
    """
    service = fixtures.services[OFFLINE_CPE_SERVICE]
    assert service["health"] == "pon_healthy", (
        "the point of this service is a *healthy* plant reading behind a silent device; a degraded "
        "profile would localise to the drop and never offer a CPE action"
    )
    device = fixtures.cpe_for_service(OFFLINE_CPE_SERVICE, system="test")
    assert device["online"] is False, "an online device gives the repair nothing to restore"


# ------------------------------------------------------------------------------------------------
# The shape LangGraph received
# ------------------------------------------------------------------------------------------------


def test_the_gate_router_is_wired_on_both_edges_that_ask_the_question() -> None:
    """One router, two edges. See the module docstring in `remote_resolution` for why.

    Read back out of the `StateGraph` rather than off `GATE_TARGETS`, which would only prove the
    table equals itself.
    """
    graph = build_remote_resolution_graph()
    expected = {**GATE_TARGETS, ESCALATED: END}

    for source in ("select_remote_action", "request_remote_approval"):
        branches = graph.branches[source]
        assert len(branches) == 1, f"{source} should carry exactly one conditional edge"
        assert dict(next(iter(branches.values())).ends or {}) == expected, (
            f"{source} must route on the same three answers as the other gate edge; two spellings "
            "of one question is how the second one forgets about rejection"
        )


def test_every_node_is_guarded_or_terminal() -> None:
    """No edge in this graph may bypass the escalation flag.

    `guards.ESCALATED` exists because an incident that exhausted its budget at P04 otherwise walked
    five further super-steps. A subgraph that wired a plain `add_edge` between two working nodes
    would reintroduce exactly that, one stage lower down.
    """
    graph = build_remote_resolution_graph()
    for source, branches in graph.branches.items():
        for branch in branches.values():
            assert ESCALATED in (branch.ends or {}), (
                f"the conditional edge out of {source} has no {ESCALATED} branch, so a guarded "
                "incident would continue through it"
            )

    # The two nodes with a plain edge are the two that end the graph, where there is nothing left
    # to divert away from.
    plain = {end for start, end in graph.edges if start != START}
    assert plain == {END}, (
        f"plain edges may only lead to END; found {plain - {END}}. A plain edge between two working "
        "nodes is an unguarded step."
    )


def test_the_registry_matches_what_the_graph_contains() -> None:
    graph = build_remote_resolution_graph()
    assert set(graph.nodes) == {name for name, _ in REMOTE_RESOLUTION_NODES}


# ------------------------------------------------------------------------------------------------
# The pause
# ------------------------------------------------------------------------------------------------


async def test_the_gate_pauses_with_the_question_committed(paused: Any) -> None:
    """`prepare_approval` must have landed in the checkpoint *before* the interrupt.

    This is the property the two-node gate exists for. A single node that raised `interrupt()` and
    then returned `AWAITING_APPROVAL` would leave the paused checkpoint claiming the incident was
    still `diagnosing`, and the operator UI would have nothing to render.
    """
    graph, _ctx, config, first = paused

    state = await graph.aget_state(config)
    assert state.next == ("request_remote_approval",)
    assert len(state.interrupts) == 1

    assert first["status"] is IncidentStatus.AWAITING_APPROVAL
    request = first["pending_approval"]
    assert request is not None, "the question must be in state, not only in the interrupt payload"
    assert request.action_type is ActionType.CPE_FIRMWARE_UPDATE
    assert request.kind is ApprovalKind.HIGH_RISK_REMOTE_ACTION, (
        "this gate may only ask a kind it owns; a kind with its own gate elsewhere must be declined "
        "here, because the readers that match answers to questions key on kind alone"
    )

    payload = state.interrupts[0].value
    assert payload["approval_request"]["approval_id"] == request.approval_id, (
        "the payload the operator is shown and the request in state are one question; two ids "
        "would mean the answer could not be matched back"
    )
    assert "noc_supervisor" in payload["permitted_roles"], (
        "the pack names one role per kind but rbac permits a set; sending only the pack's role "
        "would tell a supervisor they cannot answer a question they can"
    )


async def test_selection_does_not_claim_a_stage_the_incident_has_not_entered(paused: Any) -> None:
    """`select_remote_action` records the decision but must not set `REMOTE_RESOLUTION`.

    Asserted at the pause, which is the only moment the distinction is observable: selection has
    happened and execution has not. If selection set the status, an incident blocked here forever
    would read as though it had touched the device.
    """
    _graph, _ctx, _config, first = paused

    decisions = first["policy_decisions"]
    assert [d.outcome for d in decisions] == [PolicyOutcome.REQUIRES_APPROVAL]
    assert first["remote_actions"] == [], "nothing may have been sent before the answer"
    assert first["action_history"] == []
    assert first.get("remote_attempt_count", 0) == 0

    selected = selected_remote_option(first)
    assert selected is not None and selected.action_type is ActionType.CPE_FIRMWARE_UPDATE, (
        "the plan must own which repair this branch is about; re-deriving it in the router would "
        "pick the next option once a BLOCKED decision was recorded"
    )


# ------------------------------------------------------------------------------------------------
# The resume
# ------------------------------------------------------------------------------------------------


async def test_an_approved_repair_executes_and_is_verified_against_a_fresh_read(
    paused: Any,
) -> None:
    """The whole point of the branch: a device that was offline is repaired and comes back.

    `verification_passed is True` is reachable only because the flash genuinely changes the
    simulator's world -- the post-read shows `online: True` where the pre-read showed `False`. That
    is the difference between testing the graph and testing a mock of it. `CPE_FIRMWARE_UPDATE` is in
    the simulator's recovering set exactly as `CPE_REBOOT` is, which is what lets this fixture ask
    the gate's own question without weakening the assertion.
    """
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    assert final["status"] is IncidentStatus.REMOTE_RESOLUTION
    assert final["pending_approval"] is None, "the question is answered; it must stop being pending"

    approvals = final["approvals"]
    assert len(approvals) == 1
    assert approvals[0].kind is ApprovalKind.HIGH_RISK_REMOTE_ACTION
    assert approvals[0].decided_by_role == "noc_supervisor"

    verified = final["remote_actions"][-1]
    assert verified.action_type is ActionType.CPE_FIRMWARE_UPDATE
    assert verified.outcome is ActionOutcome.SIMULATED
    assert verified.pre_state["online"] is False
    assert verified.post_state["online"] is True
    assert verified.verification_passed is True
    assert verified.fixed_it is True, (
        "`fixed_it` is what D10 reads; an action that ran and verified must satisfy it or the "
        "incident loops back into diagnosis having already been repaired"
    )

    state = await graph.aget_state(config)
    assert state.next == (), "the graph must be finished, not waiting"
    assert state.interrupts == ()


async def test_verification_appends_a_revision_rather_than_a_second_action(paused: Any) -> None:
    """`remote_actions` reduces with `append_revision`, and this is why.

    `execute_remote_repair` writes the action unverified; `verify_remote_repair` writes the same
    `action_id` back with the verdict filled in. Under `append_unique` the second write would be
    discarded as a duplicate and no incident would ever record a verification; under a plain append
    the history would show two repairs and `remote_attempt_count` would double-count.
    """
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    actions = final["remote_actions"]
    assert len(actions) == 2, "one revision before verification and one after"
    assert actions[0].action_id == actions[1].action_id, "a revision, not a second action"
    assert actions[0].verification_passed is None, "the first revision predates the verdict"
    assert actions[1].verification_passed is True

    assert final["remote_attempt_count"] == 1, (
        "the counter is distinct `action_id`s, not `len(remote_actions)`; counting the list would "
        "report two attempts for one repair the moment verification appended its copy"
    )
    assert len(final["action_history"]) == 1, "one ActionRecord per attempt, not per revision"
    assert final["action_history"][0].attempt == 1


async def test_the_execution_is_audited_and_keyed_for_idempotency(paused: Any) -> None:
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    record = final["action_history"][0]
    assert record.idempotency_key, "a write with no idempotency key cannot be safely retried"
    assert record.was_attempted is True
    assert executed_idempotency_keys(final) == {record.idempotency_key}
    assert attempt_number(final, ActionType.CPE_FIRMWARE_UPDATE) == 2, (
        "one flash has been attempted, so the next would be the second"
    )
    assert attempt_number(final, ActionType.CPE_REBOOT) == 1, (
        "the count is per action type. The fixture marks the reboot *option* attempted to promote "
        "the high-risk repair, but no reboot ever reached a device, so its own budget is untouched"
    )

    trail = [(e.node, e.action, e.outcome) for e in final["audit_events"]]
    assert ("execute_remote_repair", "execute_remote_repair", "simulated") in trail
    assert ("verify_remote_repair", "verify_remote_repair", "restored") in trail

    verify_event = next(e for e in final["audit_events"] if e.node == "verify_remote_repair")
    assert verify_event.reason_code is ReasonCode.REMOTE_FIX_APPLIED


async def test_the_attempt_count_skips_actions_that_never_left_the_process(paused: Any) -> None:
    """`attempt_number` counts `was_attempted`, not rows, and the two only differ off the happy path.

    The end-to-end assertion above cannot tell these apart: one repair ran, so every row in
    `action_history` is an attempted row and both readings give 2. The difference appears the moment
    a row exists that never reached the adapter -- a decision the policy engine refused, an action
    parked awaiting approval. Counting those would consume the `attempt_limits.remote` budget with
    attempts nobody made, and the incident would exhaust its retries without having tried anything.

    This is the same set `ActionRecord.was_attempted` owns for `generate_resolution_options`, which
    is why it is asked rather than re-spelled here: a second private copy of "did this reach the
    outside world" is how the two readers drift, and the symptom is silent in both directions.
    """
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    attempted = final["action_history"][0]
    assert attempted.was_attempted is True
    parked = attempted.model_copy(
        update={"action_id": "ACT-NEVER-SENT", "outcome": ActionOutcome.BLOCKED_BY_POLICY}
    )
    assert parked.was_attempted is False, "the premise: policy refused it, so nothing was sent"

    with_parked = dict(final)
    with_parked["action_history"] = [attempted, parked]
    assert attempt_number(with_parked, ActionType.CPE_FIRMWARE_UPDATE) == 2, (
        "two rows, one attempt: the next flash is still the second. Counting rows would report "
        "the third and burn a retry the customer never received"
    )


async def test_a_device_that_recovered_after_a_failed_action_claims_no_fix(fixtures: Any) -> None:
    """Verification passing is not the same as the repair having worked, and the audit says which.

    `verify_remote_repair` picks its reason code off `RemoteAction.fixed_it`, not off the verdict.
    The two differ in exactly one situation: the adapter reported the action `FAILED`, and the
    device came back anyway. A real ACS produces this whenever a CPE takes the reboot and then the
    session times out before it can acknowledge -- the reboot happened, the write is recorded as
    failed. Attributing that recovery to the action would put a fix we know failed into
    `remote_fix_success_rate`, and the rate is the number the remote branch is judged on.

    Driven at the node rather than through the graph because the fixture-backed simulator *cannot*
    produce this state: `simulate_write` returns `SIMULATED` when the gate permits and
    `outcome_if_refused` when it does not, so `FAILED` is unreachable from any fixture. That is a
    property of the simulator, not of the system, and it is why the distinction needs a test of its
    own -- replacing `verified.fixed_it` with `passed` here leaves every end-to-end test in this
    module green, which is how the distinction would otherwise be refactored away as dead defence.

    The pre-read saying `online: False` against a device that reads online now is the whole setup:
    it is what makes `_verdict` return `True` so that the two properties can disagree.
    """
    service = fixtures.services["SVC-SJ-011-A-04"]
    assert fixtures.cpe_for_service(service["service_ref"], system="test")["online"] is True, (
        "this test needs a device that reads online, so that verification passes on its own terms"
    )

    state = _initial(service)
    state["remote_actions"] = [
        RemoteAction(
            action_id="ACT-FAILED-BUT-RECOVERED",
            action_type=ActionType.CPE_REBOOT,
            target_ref=service["cpe_ref"],
            idempotency_key="idem-failed-but-recovered",
            requested_at=NOW,
            outcome=ActionOutcome.FAILED,
            error="ACS session timed out before the CPE acknowledged the reboot",
            pre_state={"online": False},
        )
    ]

    update = await verify_remote_repair.__wrapped__(state, build_context(clock=_Ticking(NOW)))  # type: ignore[arg-type]

    verified = update["remote_actions"][-1]
    assert verified.verification_passed is True, (
        "offline before and online after is the one unambiguous symptom this adapter has; the "
        "verification genuinely passed"
    )
    assert verified.fixed_it is False, (
        "`fixed_it` also requires the action to have run, and this one reported FAILED. If these "
        "two agree, the distinction the reason code below rests on has been collapsed"
    )

    event = next(e for e in update["audit_events"] if e.node == "verify_remote_repair")
    assert event.outcome == "restored", "service came back, and the audit trail should say so"
    assert event.reason_code is ReasonCode.NO_FAULT_FOUND, (
        "the service was restored but not by us; claiming REMOTE_FIX_APPLIED here credits the "
        "remote branch with a fix its own adapter reported as failed"
    )


async def test_the_verification_read_becomes_evidence_the_next_cycle_can_use(paused: Any) -> None:
    """A post-action read is an observation, and observations are evidence.

    Without this the next diagnostic cycle would re-derive the device's state from the *pre*-action
    read still sitting in evidence, and conclude the device was offline immediately after watching
    it come back.
    """
    graph, ctx, config, first = paused
    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)

    before = {item.ref for item in first["evidence"]}
    added = [item for item in final["evidence"] if item.ref not in before]
    assert len(added) == 1, "exactly one new observation: the verification read"
    assert added[0].source_system == "cpe"
    assert "cpe_firmware_update" in added[0].summary

    verified = final["remote_actions"][-1]
    assert added[0].ref in verified.evidence_refs, (
        "the action must point at the reading that verified it, or the audit trail asserts a "
        "restoration with nothing behind it"
    )

    sources, age = evidence_support(final, ctx.clock.now())
    assert sources >= 1
    assert age is not None and age >= 0.0


# ------------------------------------------------------------------------------------------------
# Refusal
# ------------------------------------------------------------------------------------------------


async def test_a_refused_approval_abandons_without_touching_the_device(paused: Any) -> None:
    """A rejection must leave the incident resumable and *not* awaiting anything.

    `abandon_remote_action` exists for this and only this. Without it the branch would end on the
    status `prepare_remote_approval` wrote, and the checkpoint would claim the incident was waiting
    for an approval that had already been refused.
    """
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(
        Command(
            resume={
                "status": "rejected",
                "decided_by": "sofia.reyes",
                "decided_by_role": "noc_supervisor",
                "rationale": "the customer is on a call; flash after 18:00",
            }
        ),
        context=ctx,
        config=config,
    )

    assert final["status"] is IncidentStatus.DIAGNOSING, (
        "a refused approval returns the incident to diagnosis, not to a stage it never entered"
    )
    assert final["pending_approval"] is None
    assert final["remote_actions"] == [], "a refusal must not reach the device"
    assert final["action_history"] == []
    assert final["approvals"][-1].rationale.startswith("the customer is on a call")


async def test_a_role_that_may_not_approve_is_recorded_as_a_rejection(paused: Any) -> None:
    """The resume value arrives over HTTP and is checked, not trusted.

    A refusal must be *recorded* rather than raised: the incident has to stay resumable so the right
    person can answer.
    """
    graph, ctx, config, _first = paused
    final = await graph.ainvoke(
        Command(
            resume={
                "status": "approved",
                "decided_by": "automation",
                "decided_by_role": "automation",
                "rationale": "self-approved",
            }
        ),
        context=ctx,
        config=config,
    )

    decision = final["approvals"][-1]
    assert decision.status.value == "rejected", "AUTOMATION approving itself is the case this stops"
    assert decision.reason_code is ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE
    assert final["remote_actions"] == []


# ------------------------------------------------------------------------------------------------
# The router, on constructed state
# ------------------------------------------------------------------------------------------------


async def test_the_gate_router_abandons_every_unset_case(paused: Any) -> None:
    """Total and conservative. A router that raised would abort the super-step with no record."""
    _graph, _ctx, _config, first = paused

    assert route_remote_gate({}) == "abandon", "no plan at all"
    assert route_remote_gate({"resolution_plan": None}) == "abandon"

    without_decision = dict(first)
    without_decision["policy_decisions"] = []
    assert route_remote_gate(without_decision) == "abandon", (
        "an unevaluated write is the exact failure the policy engine exists to prevent"
    )

    assert route_remote_gate(first) == "approve", "the question is outstanding at the pause"


async def test_the_gate_router_abandons_an_action_policy_has_blocked(paused: Any) -> None:
    """A blocked decision abandons here rather than being left for the domain model to catch.

    `ActionRequest` refuses construction with `policy_outcome=BLOCKED`, so dropping the router's
    check does not put a blocked write on the wire. It converts a clean abandon into a dead
    incident. Both halves of that were measured rather than reasoned: constructing the model with
    `policy_outcome=BLOCKED` raises

        cpe_firmware_update was blocked by policy and must not be built into an ActionRequest at all

    and the sibling mutation -- routing past the `REQUIRES_APPROVAL` branch instead -- was observed
    raising its own `ActionRequest` validator inside `execute_remote_repair`, uncaught by `@node`,
    taking nine tests down as errors rather than failures. So the incident dies mid-branch with no
    `abandon` recorded and nothing an operator can read. Defence in depth is only defence if the
    outer layer is tested too, and the inner layer here turns a routing mistake into a dead
    incident rather than a refusal.

    Reachable rather than hypothetical: `_check_attempts` returns a finding whose `blocks` defaults
    to `True` once `attempt > attempt_limits.remote`, so the repair after the ceiling arrives here
    blocked. `first_actionable_option` would skip such an option, but the router reads
    `plan.selected` -- by design, per the module docstring -- so this is the check that catches it.
    """
    _graph, _ctx, _config, first = paused
    option = selected_remote_option(first)
    assert option is not None, "the fixture must have selected something for this to mean anything"

    blocked = dict(first)
    blocked["policy_decisions"] = [
        PolicyDecision(
            decision_id="POL-BLOCKED-BY-ATTEMPT-LIMIT",
            decided_at=NOW,
            action_type=option.action_type,
            outcome=PolicyOutcome.BLOCKED,
            reason_codes=(ReasonCode.POLICY_ATTEMPT_LIMIT_REACHED,),
            policy_version="test",
        )
    ]
    assert route_remote_gate(blocked) == "abandon", (
        "an action the pack has blocked must not be routed to execution on the grounds that a "
        "decision exists; the router has to read what the decision said"
    )


async def test_this_gate_declines_a_kind_another_gate_owns(paused: Any) -> None:
    """A variable-kind gate may only ask a kind no other gate asks, because the readers key on kind.

    `latest_decision_of` and `approval_outstanding` match an answer to a question by `ApprovalKind`
    alone -- `ApprovalDecision` carries no `action_type`, `target_ref` or `policy_decision_id` to
    narrow it with. That is sound only while exactly one gate raises each kind. This gate takes its
    kind from the `PolicyDecision`, so without the `DEDICATED_GATE_APPROVAL_KINDS` check it will
    ask whichever kind the pack demanded, including kinds that already have a gate of their own.

    Not hypothetical -- this was the live case. `rca.min_for_remote_action` demands
    `low_confidence_rca` on the `cpe_reboot` this fixture used to select, and a real-code sweep of
    all 82 runs -- the 41 fixture services under both case types -- found this gate intercepting
    it: `prepare_low_confidence_review` ran
    **zero** times across the corpus before the check was added and once after, because D06's only
    natural trigger was being consumed here. Worse than duplicated work: `route_rca_confidence`
    returned `continue` on the answer this gate collected, skipping the `rca is None` fail-closed
    branch that is the entire reason D06 exists.

    Deleting the `DEDICATED_GATE_APPROVAL_KINDS` term from `route_remote_gate` was observed turning
    this red as

        AssertionError: this gate would ask ['clean_to_dirty_handover', 'dispatch',
        'high_blast_radius_action', 'low_confidence_rca'], and every one of those has a gate of
        its own. [...]

    and turning *nothing else in the suite* red -- measured over the whole of `tests/`, with only
    this and its `test_subgraph_self_help.py` twin failing. `test_the_gate_router_abandons_every_
    unset_case` in particular stays green, because the kind this gate does own routes to `approve`
    either way. All four are reported together rather than asserted one at a time so the failure
    names the whole leak, and iteration is sorted so the text is reproducible.
    """
    _graph, _ctx, _config, first = paused
    option = selected_remote_option(first)
    assert option is not None, "the fixture must have selected something for this to mean anything"

    asked: list[str] = []
    for kind in sorted(DEDICATED_GATE_APPROVAL_KINDS, key=lambda k: k.value):
        demanding = dict(first)
        demanding["policy_decisions"] = [
            PolicyDecision(
                decision_id=f"POL-DEMANDS-{kind.value}",
                decided_at=NOW,
                action_type=option.action_type,
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                required_approval_kind=kind,
                reason_codes=(ReasonCode.POLICY_APPROVAL_REQUIRED,),
                policy_version="test",
            )
        ]
        if route_remote_gate(demanding) != "abandon":
            asked.append(kind.value)

    assert not asked, (
        f"this gate would ask {asked}, and every one of those has a gate of its own. The answer is "
        "keyed on kind, so the owning gate reads it as already given and skips itself."
    )


# ------------------------------------------------------------------------------------------------
# What the branch measures about itself
# ------------------------------------------------------------------------------------------------


async def test_the_branch_records_the_decisions_and_the_actions_it_took(paused: Any) -> None:
    """Both KPIs this branch emits are derived from facts the emitting node is itself writing.

    That is the whole difficulty. A node returns a partial mapping and LangGraph reduces it *after*
    the node finishes, so `emit_kpi(state, ...)` measures the world as it was on the way in --
    `policy_block_rate` counted an empty `policy_decisions` and `automation_coverage_rate` an empty
    `action_history`. `KPINotDerivableError` is swallowed by design, so both simply produced nothing
    and no test noticed: this module asserted a great deal about the branch and nothing about what
    it reported. `preview` applies the declared reducers, and these two assertions are what stop the
    argument being reintroduced.

    Both are counted as *new since the parent*, because the parent graph emits six KPIs of its own
    before the subgraph is entered and a bare `in` check would pass on those.
    """
    graph, ctx, config, first = paused
    before = {e.kpi_name for e in first["kpi_events"]}

    gate = [e for e in first["kpi_events"] if e.kpi_name == KPIName.POLICY_BLOCK_RATE]
    assert len(gate) == 1, (
        "`select_remote_action` writes the policy decision and then counts the decisions; against "
        "the raw state the list it counted was still empty and this KPI never fired at all"
    )
    assert gate[0].numerator == pytest.approx(0.0)
    assert gate[0].denominator == pytest.approx(1.0), "one decision was taken, so one is counted"
    assert gate[0].dimensions["action_type"] == "cpe_firmware_update"

    final = await graph.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)
    coverage = [e for e in final["kpi_events"] if e.kpi_name == KPIName.AUTOMATION_COVERAGE_RATE]
    assert len(coverage) == 1, (
        "`execute_remote_repair` appends to `action_history` and then measures it; same defect, "
        "same fix, and this one was invisible until the approval had been given"
    )
    assert coverage[0].denominator == pytest.approx(1.0)
    assert coverage[0].value == pytest.approx(0.0), (
        "the repair carried an `approval_ref`, so it was not unattended. A 1.0 here would mean the "
        "rate had stopped noticing that a human was asked -- which is the only thing it measures"
    )
    assert KPIName.AUTOMATION_COVERAGE_RATE not in before, (
        "the control: this KPI must be new since the gate, or the assertion above is reading an "
        "event the parent graph emitted and would hold with the subgraph emitting nothing"
    )


def _executed_record(outcome: ActionOutcome, *, approval_ref: str | None = None) -> ActionRecord:
    """One `action_history` row, varying only in the two fields the coverage rate reads."""
    return ActionRecord(
        action_id=f"ACT-{outcome.value}",
        incident_id="INC-COVERAGE",
        action_type=ActionType.CPE_REBOOT,
        target_ref="CPE-COVERAGE",
        idempotency_key=f"idem-{outcome.value}",
        outcome=outcome,
        started_at=NOW,
        approval_ref=approval_ref,
    )


def _counted_in_coverage(state: Any, record: ActionRecord) -> bool:
    """Whether `automation_coverage_rate` put this row in its denominator.

    Asked through the public result rather than the comprehension: a row the method excludes is the
    only row present, so the denominator is empty and the rate declines to be measured at all.
    """
    probe = dict(state)
    probe["action_history"] = [record]
    measured = KPICalculator().automation_coverage_rate(probe)
    return measured is not None and measured.denominator == pytest.approx(1.0)


def test_the_coverage_denominator_is_the_set_was_attempted_owns(fixtures: Any) -> None:
    """`automation_coverage_rate`'s denominator and `ActionRecord.was_attempted` are one set.

    They were two, and they had already drifted over `TIMED_OUT`: the KPI spelled its own
    `{SUCCEEDED, PARTIAL, FAILED, SIMULATED}` inline, while `was_attempted` counts a timeout
    deliberately -- we sent it and never learned the result. That is the second private copy
    `was_attempted`'s docstring exists to warn about.

    Nothing caught it because no code path in `src/` assigns `FAILED`, `PARTIAL` or `TIMED_OUT`;
    every reference is a read (gap CPE-9). The divergence is therefore unreachable from any fixture
    today and goes live the moment the simulator grows a failure mode, which is exactly the kind of
    defect that gets found in a dashboard rather than in a test run.

    Asserted over *every* `ActionOutcome` member rather than over the one that drifted, because the
    next drift will be a new member taught to one reader and not the other.
    """
    state = _initial(fixtures.services[OFFLINE_CPE_SERVICE])

    disagreed: list[str] = []
    for outcome in ActionOutcome:
        record = _executed_record(outcome)
        counted = _counted_in_coverage(state, record)
        if counted is not record.was_attempted:
            disagreed.append(
                f"{outcome.value}: counted by the KPI={counted}, "
                f"was_attempted={record.was_attempted}"
            )

    assert not disagreed, (
        f"{disagreed} -- the coverage denominator has stopped being `was_attempted`. Whichever of "
        "the two learned about the outcome first, the other is now measuring a different "
        "population, and the rate moves for a reason nobody can find in the incident"
    )


def test_an_action_whose_result_we_never_learned_still_counts_against_coverage(
    fixtures: Any,
) -> None:
    """A timeout belongs in the denominator, and this pins the direction rather than the agreement.

    The test above would also pass if the two sets were reconciled the *other* way, by teaching
    `was_attempted` to drop `TIMED_OUT`. This is the argument against doing that, made executable.

    Approval is decided before the send, so the outcome cannot change whether a human was asked --
    and timeouts concentrate in the slow network-affecting actions the pack gates behind approval.
    Excluding them therefore removes *attended* rows first: the two actions here are a clean 50%,
    and dropping the timed-out one reports 100% automated for an incident where a supervisor was
    woken up. The rate would climb the more work went unconfirmed, which is the same direction of
    error as counting refusals -- the thing the denominator was narrowed to prevent in the first
    place.
    """
    state = _initial(fixtures.services[OFFLINE_CPE_SERVICE])
    probe = dict(state)
    probe["action_history"] = [
        _executed_record(ActionOutcome.SUCCEEDED),
        _executed_record(ActionOutcome.TIMED_OUT, approval_ref="APR-SUPERVISOR-WOKEN"),
    ]

    measured = KPICalculator().automation_coverage_rate(probe)

    assert measured is not None
    assert measured.denominator == pytest.approx(2.0), (
        "the reboot that timed out was still sent; excluding it here is what makes the rate rise "
        "as more actions go unconfirmed"
    )
    assert measured.value == pytest.approx(0.5), (
        "one of two actions was unattended. A 1.0 would be the rate reporting full automation for "
        "an incident that asked a human, because the row carrying the `approval_ref` was the one "
        "dropped"
    )
