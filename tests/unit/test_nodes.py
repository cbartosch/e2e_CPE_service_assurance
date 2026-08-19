"""The eleven parent nodes, run end to end against the simulator.

These are not unit tests of each node in isolation. P01-P11 is a pipeline whose interesting
properties are only visible once a real signal has been through all of it -- what the evidence
stage hands the diagnosis stage, whether anything was written, what the routers downstream will do
with the plan that comes out. Each test below drives the whole chain and then asserts one property
of the result.

The nodes are invoked through `fn.__wrapped__`, so the `@node` decorator's wrapper does not run --
it reaches for the LangGraph runtime to find `GraphContext`, and there is no runtime here. That
path, together with the budget guard and the conditional edges that read what the guard writes, is
covered by `tests/unit/test_builder.py`, which drives these same eleven nodes through the compiled
graph.

Both modules are worth having because they localise different faults. Driving the chain by hand in
a fixed order means no routing table decides what runs next, so a failure here is a failure of a
node; the builder's tests run the same nodes through the real edges, so a failure there that is
green here points at the wiring. One test below does call routers, but on the finished state and as
assertions about the plan -- D08, D09 and D11 belong to Stage 3 and wire no parent edge, so nothing
here depends on `builder.DECISION_AFTER`. `preview` folds each update into the state
exactly as the reducers will, and `conftest.build_context` assembles a detector snapshot by hand,
so that neither a graph nor a live adapter is needed to reach P11.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lpr_cpe.domain.closure import ValidationResult
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    CaseType,
    EventSource,
    FaultDomain,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.governance import ActionRecord, ActionRequest
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.nodes import (
    DIAGNOSIS_NODES,
    EVIDENCE_NODES,
    INTAKE_NODES,
    PARENT_NODES,
)
from lpr_cpe.graph.nodes._runtime import preview
from lpr_cpe.graph.nodes.closure import (
    confirm_customer_outcome,
    confirmation_required,
    customer_verdict,
)
from lpr_cpe.graph.nodes.diagnosis import determine_root_cause, generate_resolution_options
from lpr_cpe.graph.routing import (
    route_remote_eligibility,
    route_self_help_suitability,
    route_shared_or_plant,
)
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.integrations.communications.simulator import SELF_HELP_SCRIPTS
from lpr_cpe.policies.engine import PolicyEngine

#: Fixed so that every derived id and every KPI duration is reproducible. The simulator's fixtures
#: are dated relative to nothing, so any instant works; this one is written down rather than
#: `now()` so a failure is the same failure tomorrow.
NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The four fixture services these tests drive, by their `health` label. One degraded HFC service
#: behind a degraded tap, one degraded PON service, one PON service behind a power cut, and one
#: healthy service that nonetheless sits behind the same degraded tap as the first.
HEALTHS = ("hfc_degraded_upstream", "pon_degraded_optical", "pon_power_affected", "hfc_healthy")


class _Frozen:
    """A clock that only moves when a test moves it.

    Each node is given a distinct instant so that KPI durations are non-zero and ordering
    assertions mean something. A real `Clock` is a protocol; this satisfies it structurally.
    """

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def _event(service: dict[str, Any]) -> AssuranceEvent:
    return AssuranceEvent(
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
    )


async def _run_parent(service: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """P01 through P11 over one fixture service. Returns the final state and the context used.

    The three stage registries and not `PARENT_NODES`, which now holds five more. This helper walks
    the pipeline straight through, and the governance five are not on it: they sit on branch arms of
    D06 and D07 that a linear walk has no way to have chosen. Walking them anyway raised
    `RuntimeError: Called get_config outside of a runnable context` from
    `request_low_confidence_review`, which is `interrupt()` refusing to be called outside a graph --
    the honest complaint that this walk is not a graph run.
    """
    clock = _Frozen(NOW)
    ctx = build_context(clock=clock)  # type: ignore[arg-type]
    state = make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=_event(service),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )
    for _, fn in (*INTAKE_NODES, *EVIDENCE_NODES, *DIAGNOSIS_NODES):
        clock.advance(timedelta(seconds=3))
        update = await fn.__wrapped__(state, ctx)
        state = preview(state, update)
    return state, ctx


def _service(fixtures: Any, health: str) -> dict[str, Any]:
    return next(s for s in fixtures.services.values() if s["health"] == health)


# ------------------------------------------------------------------------------------------------
# The read-only guarantee
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("health", HEALTHS)
async def test_the_first_eleven_nodes_write_nothing(fixtures: Any, health: str) -> None:
    """P01-P11 is diagnosis, and diagnosis does not change the network.

    Asserted against `WriteGate.recorded` rather than against each adapter, because the gate is the
    single place every outbound write must pass through -- an adapter that wrote without asking
    would be a defect the gate's own tests catch, and one that asked would be recorded here. The
    specification puts the first write behind D07's approval and P12's execution, both of which are
    downstream of everything these nodes do.
    """
    _, ctx = await _run_parent(_service(fixtures, health))
    # `list(...)` because `recorded` is a tuple; comparing to `[]` fails on the type alone and
    # would pass this test off as a real failure. The empty sequence is the assertion.
    assert list(ctx.adapters.gate.recorded) == []


# ------------------------------------------------------------------------------------------------
# What the evidence stage hands the diagnosis stage
# ------------------------------------------------------------------------------------------------


async def test_p10_reclassifies_instead_of_reusing_the_evidence_stage_verdict(
    fixtures: Any,
) -> None:
    """P07 and P10 disagree about this service, and P10's answer is the one that is right.

    P07 runs the detectors over the evidence as assembled and its fault-domain classifier says
    `distribution`. P10 re-runs the classifier over the *folded* findings -- which by then include
    the test results P09 produced -- and says `tap_or_odp`. The fixture's ground truth is that five
    of the eight services behind `TAP-SJ-011-A` are degraded, so the tap is the common element and
    `distribution` is a level too far upstream.

    This is the test that would fail if somebody "optimised" P10 by reusing P07's finding, which
    looks like an obvious saving and quietly costs the diagnosis its accuracy.
    """
    service = _service(fixtures, "hfc_degraded_upstream")
    state, _ = await _run_parent(service)

    rca = state["rca"]
    assert rca.fault_domain is FaultDomain.TAP_OR_ODP
    assert rca.delimiter_ref == "TAP-SJ-011-A"

    # The evidence stage's own classifier finding is still in state, and still says otherwise.
    evidence_stage_verdict = [
        f.suspected_domain
        for f in state["anomaly_findings"]
        if f.detector_name == "fault_domain_classifier" and f.suspected_domain is not None
    ]
    assert FaultDomain.DISTRIBUTION in evidence_stage_verdict, (
        "P07's classifier no longer says `distribution` for this fixture, so this test is no "
        "longer demonstrating that P10 re-classifies. Check the fixture before relaxing it."
    )


# ------------------------------------------------------------------------------------------------
# What the routers downstream do with the plan
# ------------------------------------------------------------------------------------------------


async def test_a_tap_fault_reaches_field_planning_with_both_halves_of_a_joint_dispatch(
    fixtures: Any,
) -> None:
    """The tap is the one plant domain D08 does not divert, and that has to end somewhere sensible.

    `boundaries.crew_for(TAP_OR_ODP)` is `JOINT`, so D08 returns `continue` deliberately: diverting
    it down the plant path would skip the Clean Boots half of a joint visit. That is only correct
    if the plan actually contains both halves and if the intervening routers decline it.

    This has been wrong. With `raise_mr` as the tap's only catalogued option and `truck_roll=False`
    on it, `is_remote_option` -- "no truck and no customer" -- was true, D09 returned `remote`, and
    a day-long plant maintenance request was handed to the remote-repair stage as though it were a
    reboot. Both halves of the fix are asserted here: the plan has a Dirty Boots action and a Clean
    Boots one, and the route runs D08 -> D09 -> D11 -> field planning without either stage claiming
    it.
    """
    state, _ = await _run_parent(_service(fixtures, "hfc_degraded_upstream"))
    assert state["fault_domain"] is FaultDomain.TAP_OR_ODP

    plan = state["resolution_plan"]
    offered = {o.action_type for o in plan.options}
    assert ActionType.RAISE_MR in offered, "the Dirty Boots half"
    assert ActionType.CREATE_WORK_ORDER in offered, "the Clean Boots half"

    assert route_shared_or_plant(state) == "continue"
    assert route_remote_eligibility(state) == "self_help_check", (
        "a plant maintenance request is not a remote repair; if this says `remote` then an option "
        "in the tap's plan looks console-executable to `is_remote_option` again"
    )
    assert route_self_help_suitability(state) == "field_planning"


async def test_every_offered_option_carries_the_packs_risk_and_approval(fixtures: Any) -> None:
    """The pack's `ActionRule` is read to decide whether to offer an option; it is also recorded.

    A plan is checkpointed and read back during an audit, so an option that named only its action
    type would tell a reader what today's pack says rather than what applied when the plan was
    made. Each option's copy is checked against the pack it was built from, field by field, so a
    propagation that silently stopped copying would fail here rather than in a stale audit.
    """
    pack = PolicyEngine.load().pack
    state, _ = await _run_parent(_service(fixtures, "hfc_degraded_upstream"))
    options = state["resolution_plan"].options
    assert options, "this fixture must produce a non-empty plan for this test to mean anything"

    for option in options:
        rule = pack.remote_actions[option.action_type]
        assert option.risk == rule.risk
        assert option.required_approval == rule.approval_kind

    # And specifically: the joint dispatch's two halves need two different approvals.
    by_action = {o.action_type: o for o in options}
    assert by_action[ActionType.RAISE_MR].required_approval is not None
    assert by_action[ActionType.CREATE_WORK_ORDER].required_approval is not None
    assert (
        by_action[ActionType.RAISE_MR].required_approval
        != by_action[ActionType.CREATE_WORK_ORDER].required_approval
    )


# ------------------------------------------------------------------------------------------------
# The second pass through P11
# ------------------------------------------------------------------------------------------------


async def test_a_second_lap_of_the_self_help_loop_records_two_of_everything(fixtures: Any) -> None:
    """P10 and P11 re-run on D12's loop must each leave a second record, not overwrite the first.

    This is the lap D12's `retry_diagnosis` produces and it is the reason `resolution_cycles`
    exists. The loop is P10 -> P11 -> self_help -> P10, so it never passes through P07 and
    `diagnostic_cycles` does not move -- asserted first, because everything after it is vacuous if
    it does. Both nodes on the lap are run, in the order the edges run them, rather than P11 alone:
    the collision is a property of the lap and testing half of it would have missed P10's.

    The lap is constructed rather than driven. No simulator fixture reaches D12 with an unexhausted
    plan -- the one service offering a self-help script arrives there with `exhausted` already true
    -- so there is no fixture whose real edge traversal would exercise a second pass. What is
    faithful here is the shape: an option attempted and failed, then the two nodes asked again on
    the same diagnostic cycle.

    Both nodes keyed their `derive_id` discriminator on `diagnostic_cycles`, which does not move on
    this lap, so both derived their first pass's ids again. `append_unique` is first-write-wins, so
    each second record was dropped on the floor: the re-diagnosis left no trace at all, and the
    surviving plan record said `already_attempted: []` for an incident that had just failed a
    repair -- an audit trail positively asserting the opposite of what happened, which is worse
    than a missing record.

    Shown red against the pre-split nodes, one failure for each half. P11's:

        E       AssertionError: two passes through P11 minted one plan id
        E       assert 'RPLAN-228a28c9ba86d6de6d43' != 'RPLAN-228a28c9ba86d6de6d43'

    and P10's, with P11's discriminator fixed and P10's still on `diagnostic_cycles`:

        E       AssertionError: the re-diagnosis on this lap left no audit record
        E       assert 1 == 2
    """
    state, ctx = await _run_parent(_service(fixtures, "hfc_degraded_upstream"))
    first_plan = state["resolution_plan"]
    diagnostic_cycles = state["diagnostic_cycles"]
    assert first_plan.options, "this fixture must offer something for a second lap to mean anything"

    attempted = first_plan.options[0]
    state = preview(
        state,
        {
            "action_history": [
                ActionRecord(
                    action_id=f"ACT-{attempted.option_id}",
                    incident_id=state["incident_id"],
                    action_type=attempted.action_type,
                    target_ref=attempted.target_ref,
                    idempotency_key=f"IDK-{attempted.option_id}",
                    outcome=ActionOutcome.FAILED,
                    started_at=NOW,
                )
            ]
        },
    )
    for step in (determine_root_cause, generate_resolution_options):
        state = preview(state, await step.__wrapped__(state, ctx))
    second_plan = state["resolution_plan"]

    assert state["diagnostic_cycles"] == diagnostic_cycles, (
        "the self-help loop does not pass through P07, so if this moved then the lap being "
        "modelled is not D12's and this test is asserting nothing about it"
    )
    assert second_plan.plan_id != first_plan.plan_id, "two passes through P11 minted one plan id"

    def records(node: str) -> list[Any]:
        return [e for e in state["audit_events"] if e.node == node]

    # `append_unique` keeps the first write per event id, so a repeated id shows up here as a lost
    # record rather than as a duplicate one -- which is why these count rather than compare.
    assert len(records("determine_root_cause")) == 2, (
        "the re-diagnosis on this lap left no audit record"
    )
    assert len(records("generate_resolution_options")) == 2

    plans = records("generate_resolution_options")
    assert plans[0].detail["already_attempted"] == []
    assert plans[1].detail["already_attempted"] == [
        f"{second_plan.plan_id}-{attempted.action_type.value}"
    ], "the second record is what carries the failure forward, and it is in this plan's id space"

    # And the counter that made every one of them distinct is the one that moved, which is the
    # whole point of its being a separate field: two laps on one diagnostic cycle.
    assert state["resolution_cycles"] == 2


# ------------------------------------------------------------------------------------------------
# P23 -- the customer's word
# ------------------------------------------------------------------------------------------------


def test_p23_asks_only_where_the_pack_says_telemetry_cannot_see() -> None:
    """`confirmation_required` reads the pack's list and does not keep a second one.

    The specification's rule is that a customer is asked only where telemetry and service tests
    cannot establish the actual experience, and the pack already names those domains. Both
    directions are asserted over the whole enum, so a domain added to `FaultDomain` without a
    decision about it shows up here rather than defaulting to "no need to ask".

    The expected set is read from the pack, so this test asserts the *wiring* -- that the node
    consults the pack -- and not the pack's contents, which are the pack's own business and change
    without this file being wrong.
    """
    ctx = build_context(clock=_Frozen(NOW))  # type: ignore[arg-type]
    expected = ctx.policy.pack.validation.require_customer_confirmation_for_domains

    asked = {
        domain for domain in FaultDomain if confirmation_required({"fault_domain": domain}, ctx)
    }  # type: ignore[arg-type]
    assert asked == set(expected)

    # And an incident whose domain was never established is not one telemetry has spoken for, so
    # the default `UNKNOWN` must be decided by the same list rather than assumed either way.
    assert confirmation_required({}, ctx) is (FaultDomain.UNKNOWN in expected)  # type: ignore[arg-type]


def test_the_newest_understood_reply_is_the_customers_current_answer() -> None:
    """A customer who changes their mind is answered by the change, not by what they said first.

    `customer_verdict` takes the first row it understands and `fetch_customer_responses` returns
    newest first, so "first" here means "most recent" -- the two facts only compose because of the
    ordering, which is why the test below pins it against the adapter.

    Rows the vocabulary does not cover are skipped rather than treated as silence, which is the
    case the middle row covers: a reply about something else must not stop the reader before it
    reaches the answer underneath.

    `None` is asserted separately from `False` because `route_resolution` treats them oppositely --
    a `False` goes back to diagnosis, a `None` carries on -- and a reader that collapsed them would
    re-diagnose every incident nobody needed to phone.
    """
    newest_first = [
        {"response": "still_broken"},
        {"response": "completed"},
        {"response": "yes"},
    ]
    assert customer_verdict(newest_first) is False
    assert customer_verdict(newest_first[1:]) is True
    assert customer_verdict([{"response": " Fixed "}]) is True, "trimmed and case-folded"

    assert customer_verdict([]) is None
    assert customer_verdict([{"response": None}, {"free_text": "hello"}]) is None


async def test_the_adapter_returns_customer_replies_newest_first(fixtures: Any) -> None:
    """The undocumented ordering two readers depend on, given one place to go red.

    `CommunicationsAdapter.fetch_customer_responses` is typed `list[dict[str, Any]]` and says
    nothing about order, but `closure.customer_verdict` and `self_help.await_customer_response`
    both take the *first* matching row and both mean "the most recent". Neither could see this
    assumption break; the simulator could reorder and both would quietly start answering with a
    stale reply.

    Sent for real rather than hand-built, because a hand-built list would be asserting that
    `sorted` sorts. Every script in `SELF_HELP_SCRIPTS` goes out for one incident, which is what
    makes the timestamps differ: the simulator derives `responded_at` from a roll seeded on the
    script id.

    Shown red by dropping `reverse=True` from the simulator's sort, and the useful half of that
    measurement is what stayed green: forty other tests, including the whole of
    `test_subgraph_self_help.py`, which is the other reader::

        E       AssertionError: newest first, which is what 'first row' means
        E       assert ['2026-03-02T...187624+00:00'] == ['2026-03-02T...583576+00:00']
        E         At index 0 diff: '2026-03-02T14:18:56.583576+00:00' != '2026-03-02T14:20:57...

    So nothing else in the suite holds this. Both readers would have gone on answering with the
    oldest reply and every one of their own tests would have passed.

    The second assertion is the measured open item, not an aside. The simulator's `response` field
    only ever holds `completed` or `declined` -- the self-help vocabulary, which answers "did you
    carry out the step?" and not "is your service working?". So against the shipped adapter
    `customer_verdict` returns `None` for every incident, the validation stays unconfirmed, and an
    incident in a domain that requires the customer's word cannot get it. The outbound half that
    would ask is the gap `closure.py` sets out: contacting a customer needs an `ActionRequest`
    carrying a `PolicyDecision`, and Stage 5 has no `ResolutionOption` to build one from.
    """
    ctx = build_context(clock=_Frozen(NOW))  # type: ignore[arg-type]
    comms = ctx.adapters.communications
    incident = "INC-P23-ORDERING"
    for index, script in enumerate(sorted(SELF_HELP_SCRIPTS)):
        await comms.send_self_help(
            ActionRequest(
                action_id=f"ACT-P23-{index}",
                incident_id=incident,
                action_type=ActionType.SEND_SELF_HELP,
                target_ref=_service(fixtures, "hfc_degraded_upstream")["customer_ref"],
                requested_at=NOW,
                idempotency_key=f"IDKEY-P23-{index}",
                actor="test",
                reason_code=ReasonCode.SELF_HELP_SUCCEEDED,
                correlation_id=f"COR-{incident}",
                parameters={"script_id": script, "channel": "sms", "language": "en"},
            )
        )

    rows = await comms.fetch_customer_responses(incident)
    assert len(rows) > 1, "one row cannot show an order"
    stamps = [str(row["responded_at"]) for row in rows]
    assert stamps == sorted(stamps, reverse=True), "newest first, which is what 'first row' means"

    assert customer_verdict(rows) is None
    assert {row["response"] for row in rows} <= {"completed", "declined"}


async def test_p23_records_the_customers_denial_without_erasing_the_telemetrys_pass(
    fixtures: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `passed` record carrying `customer_confirmed=False` reads like a contradiction, and is one.

    That is the whole reason D22 exists: the readings say the service is fine and the customer says
    it is not, and something has to decide which wins. Overwriting `passed` here would destroy the
    evidence that they disagreed and leave D22 routing on a record that no longer shows the
    conflict -- so the node revises one field and leaves the rest of `validate_restoration`'s
    judgement alone.

    The reply is injected. The simulator cannot produce this vocabulary at all, as the test above
    measures, so driving it through the shipped adapter would assert nothing: the verdict would be
    `None` and no copy would be made. Patched at the adapter rather than by passing rows in,
    because the node's own read is part of what is under test.

    Shown red by adding `"passed": verdict` to the node's `model_copy`, which is the tempting
    version -- the customer says no, so record that it did not pass::

        E       AssertionError: the telemetry did pass; the disagreement is the record
        E       assert False is True

    That mutation leaves a record D22 still routes correctly on, which is why it needs a test of
    its own: the incident goes back to diagnosis either way, and the only thing lost is the reason.
    """
    state, ctx = await _run_parent(_service(fixtures, "pon_degraded_optical"))
    passed = ValidationResult(
        validation_id="VAL-P23",
        incident_id=state["incident_id"],
        validated_at=NOW,
        window_start=NOW - timedelta(hours=1),
        stability_window=timedelta(minutes=30),
        samples_in_window=3,
        min_samples_required=3,
        passed=True,
    )

    async def denied(incident_id: str) -> list[dict[str, Any]]:
        return [{"incident_id": incident_id, "response": "still_broken"}]

    monkeypatch.setattr(ctx.adapters.communications, "fetch_customer_responses", denied)
    update = await confirm_customer_outcome.__wrapped__(preview(state, {"validation": passed}), ctx)

    revised = update["validation"]
    assert revised.customer_confirmed is False
    assert revised.passed is True, "the telemetry did pass; the disagreement is the record"
    assert revised.validation_id == passed.validation_id
    assert revised.model_dump(exclude={"customer_confirmed"}) == passed.model_dump(
        exclude={"customer_confirmed"}
    ), "one field revised, and the window this node did not observe left alone"

    (recorded,) = update["audit_events"]
    assert recorded.outcome == "denied"
    assert recorded.detail["customer_confirmed"] is False
    assert recorded.detail["validation_id"] == passed.validation_id


async def test_p23_refuses_a_state_that_never_reached_a_verdict(fixtures: Any) -> None:
    """No validation is a wiring fault, not an incident condition, so it raises rather than routes.

    D21's `confirm_outcome` is the only edge into this node and `route_stability` gives that answer
    only for a validation that passed, so arriving here without one means the graph was rewired
    wrongly. Returning a `None` verdict instead would send the incident to D22, which reads an
    absent validation as `retry_diagnosis` -- a plausible-looking re-diagnosis that hides the
    rewiring for as long as anyone cares to look.
    """
    state, ctx = await _run_parent(_service(fixtures, "pon_degraded_optical"))
    assert state.get("validation") is None, (
        "P11 does not produce one, which is what makes this real"
    )

    with pytest.raises(ValueError, match="no validation record"):
        await confirm_customer_outcome.__wrapped__(state, ctx)


# ------------------------------------------------------------------------------------------------
# The registry
# ------------------------------------------------------------------------------------------------


def test_the_parent_registry_is_the_eleven_nodes_in_specification_order() -> None:
    """`PARENT_NODES` is a tuple because the builder wires edges between consecutive entries.

    `nodes/__init__` already checks at import that each registry key matches the name its `@node`
    decorator stamps into the audit trail. What it cannot check is that the order is the one the
    specification numbers, because nothing in the code knows what P01 means. That is written here.

    The eleven are asserted as a prefix rather than as the whole tuple, because what follows them
    is not the next specification number. P23 comes next and Stage 5 is where it belongs; then the
    governance five, which are not a specification stage at all -- they belong to D06 and D07, which
    are asked *between* P10 and P11 and after P11 respectively. Order still matters for all
    seventeen, and the next two tests cover the rest of it; splitting the assertion three ways keeps
    this one about the numbering the specification actually gives.
    """
    assert [name for name, _ in PARENT_NODES][:11] == [
        "receive_signal",
        "normalize_event",
        "resolve_identity_and_topology",
        "deduplicate_and_correlate",
        "assess_impact_and_priority",
        "create_or_attach_incident",
        "assemble_case_evidence",
        "create_diagnostic_test_plan",
        "execute_read_only_tests",
        "determine_root_cause",
        "generate_resolution_options",
    ]


def test_p23_sits_between_the_diagnosis_line_and_the_governance_five() -> None:
    """One entry, and both of the edges its position implies are ones the builder must not draw.

    A place in this tuple is a pair of plain edges -- one in from the entry before, one out to the
    entry after -- unless `DECISION_AFTER` suppresses them. Twelfth is the only place P23 can go,
    and both neighbours say why.

    Before it: `generate_resolution_options`, which is in `DECISION_AFTER` under D07. So no edge is
    drawn *into* P23 from the diagnosis line, which is correct -- P23 is reached from D21's
    `confirm_outcome` and from nowhere else. After it: the first of the governance five, and P23 is
    itself in `DECISION_AFTER` under D22, so no edge is drawn out of it either.

    Last would be the obvious alternative and is the one that is actually unsafe. `record_escalation`
    ends the tuple because `_plain_edges` reads a node with no successor as terminal and
    `_DELIBERATE_TERMINALS` vouches for it; appending Stage 5 after it would draw
    `record_escalation -> confirm_customer_outcome` and resume an incident a human had been handed.
    """
    assert [name for name, _ in PARENT_NODES][11:12] == ["confirm_customer_outcome"]


def test_the_governance_five_are_appended_in_gate_pair_order() -> None:
    """The tail of `PARENT_NODES`, whose order `builder._plain_edges` reads as edges.

    Order is load-bearing in a way it is not for the eleven, and differently: `_plain_edges` draws
    an edge between each consecutive pair whose left member is absent from `DECISION_AFTER`. Both
    `request_` nodes are in that table, so the only joins drawn here are the two `prepare ->
    request` pairs -- and that is what this order buys. Swapping either pair, or interleaving them,
    would either drop a gate's own edge or draw one from a gate into the next gate's question.

    `record_escalation` is last because a node with no successor is what `_plain_edges` reads as
    terminal, and `_DELIBERATE_TERMINALS` is what stops `_check_pending_stages` reading that
    terminal as owed work. Stage 5 arriving in front of these five rather than behind them is that
    same argument seen from the other side; `test_p23_sits_between_the_diagnosis_line_and_the_
    governance_five` holds it.
    """
    assert [name for name, _ in PARENT_NODES][12:] == [
        "prepare_low_confidence_review",
        "request_low_confidence_review",
        "prepare_blast_radius_approval",
        "request_blast_radius_approval",
        "record_escalation",
    ]
