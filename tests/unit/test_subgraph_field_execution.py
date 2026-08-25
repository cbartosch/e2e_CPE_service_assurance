"""Stage 4, compiled and driven from the state the parent actually hands it.

The fixture is `SVC-SJ-011-A-01` again, and for a different reason than the sibling module's. It is
the one incident measured arriving here with a work order already booked -- `field_planning` commits
a joint crew for it -- and a booked order is the precondition every node past `route_visit_gate`
depends on. `SVC-SJ-011-B-01` is kept as the counter-case: it reaches this stage having deliberately
booked nothing, which is the arrival `no_visit` exists for.

The handover chain cannot be reached from any fixture, and that is the stage's headline finding
-------------------------------------------------------------------------------------------------
`HandoverContract.missing_items` requires `ruled_out` to be non-empty, this stage fills it from
`RCAResult.ruled_out`, and **no RCA in the fixture set rejects a hypothesis**: swept across all 41
services the census is `{rejected == 0: 41}`, against hypothesis counts of `{0: 9, 1: 16, 2: 8,
3: 7, 4: 1}`. So a correctly-keyed, otherwise-complete submission reaches `completeness == 0.857`
with `missing_items() == ['ruled_out']`, D18 answers `reject` on every lap, and the incident
escalates having filed nothing.

That is recorded as gap EXEC-1 in `docs/vendor-integration-gaps.md`, and two tests here hold it to
account from both sides: one pins the zero measurement and drives the escalation it causes, the
other seeds a single rejected hypothesis onto the same state and shows the whole D18 -> P19 -> P20
chain running. The seed is constructed through `RCAHypothesis`' validator rather than `model_copy`,
because `_rejection_is_explained` is the invariant that makes a rejection auditable and a copy would
skip it -- a test that seeded an unexplained rejection would be testing a state P10 cannot produce.

A fresh clock per drive, not a shared one
------------------------------------------
`mr_policy_input` reads `ctx.clock.local_now().time()` and the pack gates the handover on a shift
window, so with one advance-on-read clock shared across several drives a later drive's policy
verdict depends on how many nodes an earlier one ran. That was measured, not guessed: with a shared
context `evaluate_handover_policy` answered `requires_approval` on one drive and `blocked` on the
next from identical state. `_drive` therefore builds its own context.

What is deliberately not asserted here
--------------------------------------
The parent's edge into this subgraph is `test_builder.py`'s, and what happens on the far side of it
is `test_subgraph_plant_execution.py`'s. This stage's four exits are separated by D16, which the
parent re-reads from the same `FieldFinding` answered here -- so an exit's *destination* is not a
fact about this file, and asserting one here would give the parent two owners.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain.diagnosis import RCAHypothesis, RCAResult
from lpr_cpe.domain.enums import (
    CaseType,
    EventSource,
    IncidentStatus,
    MRStatus,
    Severity,
    Technology,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import HandoverContract
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED
from lpr_cpe.graph.state import current_mr_records, current_work_orders, make_initial_state
from lpr_cpe.graph.subgraphs.field_execution import (
    FIELD_EXECUTION_NODES,
    HANDOVER_TARGETS,
    SUBMISSION_EXTRAS,
    SUBMISSION_FIELDS,
    build_field_execution_graph,
)
from lpr_cpe.observability.kpi import MetricTimestamp
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The one incident measured arriving here with an order already booked. See the module docstring.
DISPATCHED_SERVICE = "SVC-SJ-011-A-01"

#: Arrives having booked nothing, which is what `no_visit` is for.
NO_ORDER_SERVICE = "SVC-SJ-011-B-01"

APPROVAL = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the tap is the confirmed delimiter; hand it to OSP",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock the sibling subgraph tests use, and for the same reason: inside a
    compiled graph the test cannot advance the clock between nodes, so a frozen one would stamp the
    arrival, the submission and the MR with the same instant.
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
            summary=f"loss of signal on {service['service_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


def _submission(service: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A plant handover the crew got right: bare measurement keys, both delimiting points named.

    The keys come out of `REQUIRED_BY_TECHNOLOGY` rather than being spelled here, because the whole
    point of `test_a_qualified_measurement_key_leaves_the_packet_incomplete` is that the key is the
    thing that matters, and a hand-copied list would drift away from the contract it must match.
    """
    required = HandoverContract.REQUIRED_BY_TECHNOLOGY[service["technology"]]
    submission = {
        "fault_domain": "tap_or_odp",
        "delimiter_kind": "tap" if service["technology"] == "hfc" else "odp",
        "delimiter_ref": service["delimiter_ref"],
        "fault_confirmed": True,
        "no_fault_found": False,
        "work_completed": False,
        "requires_plant_work": True,
        "requires_permit": False,
        "measurements": dict.fromkeys(required, -14.5),
        "parts_replaced": [],
        "evidence_refs": ["PHOTO-1"],
        "technician_note": "signal fails at the tap; the drop and the premises test clean",
        "recorded_by": "t.nguyen",
        "last_clean_point": "drop at premises",
        "first_failed_point": service["delimiter_ref"],
        "customer_confirmed": True,
    }
    submission.update(overrides)
    return submission


async def _seam(fixtures: Any, ref: str, tag: str) -> Any:
    """Run the parent to the end of `field_planning` and return what it would hand this stage.

    The parent is run for real rather than hand-built, for the sibling module's reason: a
    constructed work order would let this file pass while `commit_field_dispatch` booked something
    else, and `open_work_order` reading a *booked* order is the coupling worth policing.

    `interrupt_after=["field_planning"]` because the fork is wired -- left to itself the parent
    answers D13 and runs this very subgraph, and the test would never see the seam.
    """
    service = fixtures.services[ref]
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=build_memory_checkpointer(),
        interrupt_after=["field_planning"],
    )
    config = {"configurable": {"thread_id": f"parent-{tag}"}}
    await parent.ainvoke(_initial(service), context=ctx, config=config)
    for _ in range(6):
        snapshot = await parent.aget_state(config)
        if not snapshot.interrupts:
            break
        await parent.ainvoke(Command(resume=APPROVAL), context=ctx, config=config)
    return service, await parent.aget_state(config)


async def _drive(at_seam: Any, tag: str, answer: Any, laps: int = 14) -> Any:
    """Run the subgraph to a standstill, answering every interrupt, and report the payloads seen.

    `answer` is called with each interrupt payload so a caller can tell the submission pause from
    the approval pause. The lap ceiling is a test-harness stop, not the graph's -- the guard bounds
    re-entry and every measured run here stops well inside it.

    The context is built here rather than passed in. See the module docstring: sharing one
    advance-on-read clock across drives makes a later drive's policy verdict depend on an earlier
    drive's node count.
    """
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    graph = build_field_execution_graph().compile(
        name="lpr_cpe_field_execution", checkpointer=build_memory_checkpointer()
    )
    config = {"configurable": {"thread_id": f"field-{tag}"}}
    await graph.ainvoke(at_seam, context=ctx, config=config)

    seen: list[Any] = []
    for _ in range(laps):
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts:
            break
        payload = snapshot.interrupts[0].value
        seen.append(sorted(payload) if isinstance(payload, dict) else payload)
        await graph.ainvoke(Command(resume=answer(payload)), context=ctx, config=config)
    return (await graph.aget_state(config)).values, seen


def _with_one_rejection(values: Any) -> tuple[dict[str, Any], RCAHypothesis]:
    """The seam state plus the one fact EXEC-1 says no fixture supplies: a discarded explanation.

    Built through `RCAHypothesis(...)` rather than `model_copy`, because `_rejection_is_explained`
    refuses a rejection with no reason and `model_copy` would skip it -- seeding a state P10 cannot
    produce would make the evidence worthless. `RCAResult` is rebuilt the same way for the same
    reason.
    """
    rca: RCAResult = values["rca"]
    live = rca.hypotheses[0]
    rejected = RCAHypothesis(
        **{
            **live.model_dump(),
            "hypothesis_id": f"{live.hypothesis_id}-RULED-OUT",
            "rejected": True,
            "rejection_reason": "the drop tested clean end to end at the premises",
        }
    )
    seeded = dict(values)
    seeded["rca"] = RCAResult(**{**rca.model_dump(), "hypotheses": [*rca.hypotheses, rejected]})
    return seeded, rejected


def _is_submission_pause(payload: Any) -> bool:
    return isinstance(payload, dict) and "briefing" in payload


def _outcomes(values: Any, node: str) -> list[str]:
    return [e.outcome for e in values.get("audit_events") or [] if e.node == node]


@pytest.fixture
async def dispatched(fixtures: Any) -> Any:
    """The joint incident at the seam, with a requested joint work order on it."""
    return await _seam(fixtures, DISPATCHED_SERVICE, "joint")


@pytest.fixture
async def no_order(fixtures: Any) -> Any:
    """The incident that reaches this stage having booked nothing."""
    return await _seam(fixtures, NO_ORDER_SERVICE, "noorder")


# ------------------------------------------------------------------------------------------------
# The shape LangGraph received
# ------------------------------------------------------------------------------------------------


async def test_the_handover_gate_router_is_wired_on_both_edges_that_ask_the_question(
    dispatched: Any,
) -> None:
    """One router, two edges: out of the policy evaluation and out of the approval.

    Before and after the answer the question is the same -- *may responsibility move to OSP now?* --
    and it changes answer from `build_contract` to `commit` purely because the approval trail
    changed underneath it. Two routers would be two spellings of one question, and the second would
    be the one that forgot the pack can block.

    Reading `HANDOVER_TARGETS` would only prove the table equals itself, so both edges are read back
    out of the `StateGraph` and then *driven*. Driving is the half with teeth: `guarded()` returns a
    fresh closure per call with no `__wrapped__` on it, so the router a branch carries cannot be
    compared by identity, and `ends` is the targets only.

    Driven on the state a full run leaves, and on that state with the approval trail emptied,
    because those are the two the shared router must tell apart. The seam itself is no use for this
    -- no `raise_mr` decision exists there yet, so both edges answer `abandon` and a mis-wired one
    could hide behind the agreement.

    Shown red by giving `request_handover_approval` `guarded(route_handover_validation)` and D18's
    own map:

        AssertionError: request_handover_approval must route on the same four answers as the other
        gate edge
    """
    graph = build_field_execution_graph()
    expected = {**HANDOVER_TARGETS, ESCALATED: END}
    routers = []

    for source in ("evaluate_handover_policy", "request_handover_approval"):
        branches = graph.branches[source]
        assert len(branches) == 1, f"{source} should carry exactly one conditional edge"
        branch = next(iter(branches.values()))
        assert dict(branch.ends or {}) == expected, (
            f"{source} must route on the same four answers as the other gate edge"
        )
        routers.append(branch.path.func)

    service, snapshot = dispatched
    seeded, _rejected = _with_one_rejection(snapshot.values)
    approved, _seen = await _drive(
        seeded, "router", lambda p: _submission(service) if _is_submission_pause(p) else APPROVAL
    )
    assert [route(dict(approved)) for route in routers] == ["commit", "commit"]

    unanswered = dict(approved)
    unanswered["approvals"] = []
    answers = [route(unanswered) for route in routers]
    assert answers == ["build_contract", "build_contract"], (
        "both edges must read the approval trail, and an edge that read the packet instead would "
        f"answer request_approval here and file an MR nobody authorised. Got {answers}"
    )


def test_every_node_is_guarded_or_terminal() -> None:
    """No edge in this graph may bypass the escalation flag.

    Two loops make that load-bearing rather than decorative. D17's `more_tests` and D18's `reject`
    both return to `request_additional_field_tests`, which returns to the briefing, so an unguarded
    edge anywhere on that circuit is a cycle with no ceiling. And the step at the end of it is an MR
    filed against live plant after the incident's budget was declared exhausted, which is a crew
    dispatched by OSP rather than a wasted super-step.

    Shown red by wiring `prepare_handover_approval` onward with a plain `add_edge`:

        AssertionError: plain edges may only lead to END; found {'request_handover_approval'}.
        A plain edge between two working nodes is an unguarded step.
    """
    graph = build_field_execution_graph()
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
    graph = build_field_execution_graph()
    assert set(graph.nodes) == {name for name, _ in FIELD_EXECUTION_NODES}


def test_both_ways_of_asking_for_more_arrive_at_one_node() -> None:
    """D17's `more_tests` and D18's `reject` are the same node, reached from two decisions.

    They are one node on purpose: both mean *the crew has to tell us something else before this can
    proceed*, and splitting them would be two places to change the way a request is phrased. The
    node distinguishes them by reading whether a contract exists, which is why the audit outcome is
    `d17_insufficient` or `d18_reject` rather than one label.

    Pinned because the collapse is invisible in the source -- the two maps spell it with different
    keys -- and a later split would be silent.
    """
    graph = build_field_execution_graph()
    delimiter = next(iter(graph.branches["determine_delimiter"].values())).ends or {}
    validation = next(iter(graph.branches["build_handover_contract"].values())).ends or {}
    assert delimiter["more_tests"] == validation["reject"] == "request_additional_field_tests"


# ------------------------------------------------------------------------------------------------
# The briefing and the submission
# ------------------------------------------------------------------------------------------------


async def test_the_crew_is_briefed_and_asked_for_every_item_before_the_pause(
    dispatched: Any,
) -> None:
    """The stage stops at `capture_field_evidence` with the whole question already in the payload.

    A pause that asked for a submission without briefing the crew would be a technician reading the
    incident off a phone call, which is the failure P17 is written against. So the briefing, the
    request and what is still outstanding all travel in one interrupt.

    Fourteen briefing keys against P17's eleven bullets, because three bullets are split in the pack
    -- confidence from the suspected domain, prior MRs from prior work orders -- and `case_type` is
    the one key no bullet asks for: a crew sent by an alarm is being told something different from a
    crew sent by a customer's call.

    Shown red by dropping `case_type` from `briefing`:

        AssertionError: assert 13 == 14
    """
    service, snapshot = dispatched
    assert snapshot.next == ("field_execution",)
    orders = [(w.status, w.crew_type.value) for w in current_work_orders(snapshot.values).values()]
    assert orders == [(WorkOrderStatus.REQUESTED, "joint")], (
        "this fixture must still arrive with a booked joint order, or the module has stopped "
        f"exercising the arrival it was written for. Got {orders}"
    )

    graph = build_field_execution_graph().compile(
        name="lpr_cpe_field_execution", checkpointer=build_memory_checkpointer()
    )
    config = {"configurable": {"thread_id": "field-briefing"}}
    await graph.ainvoke(
        snapshot.values,
        context=build_context(clock=_Ticking(NOW)),  # type: ignore[arg-type]
        config=config,
    )
    paused = await graph.aget_state(config)
    assert paused.next == ("capture_field_evidence",)

    payload = paused.interrupts[0].value
    assert sorted(payload) == [
        "briefing",
        "field_submission_request",
        "outstanding_requests",
        "requested_items",
    ]
    assert payload["requested_items"] == [*SUBMISSION_FIELDS, *SUBMISSION_EXTRAS]
    assert payload["outstanding_requests"] == [], "nothing has been asked for yet on the first lap"
    assert payload["field_submission_request"]["visit_round"] == 1

    assert len(payload["briefing"]) == 14
    assert payload["briefing"]["case_type"] == CaseType.PROACTIVE_ALARM.value
    assert payload["briefing"]["ruled_out"] == [], (
        "the fixture RCA rejects nothing, so the crew is briefed with an empty ruled-out list -- "
        "see EXEC-1 and the two tests that hold it to account"
    )
    assert payload["briefing"]["delimiter_topology"]["delimiter_ref"] == service["delimiter_ref"]

    # Both arrival stamps are present, and this deliberately does *not* guard the clobber that
    # `observability.kpi.stamp` was written for. That defect is real at this site -- the briefing
    # node writes both into one update, and `update.update(mark(...))` replaces rather than merges
    # -- but it self-heals here across laps: each stamp is conditional on its own absence from
    # state, so the lap that loses `dispatched_at` re-writes it on the next one, and this fixture
    # reaches the pause with both. Reinstating the defect leaves this assertion green, so it is
    # recorded as a fact about the fixture rather than shipped as a guard that cannot fail. The
    # mechanism's own owner is `test_a_second_stamp_in_one_update_does_not_displace_the_first`.
    stamps = paused.values["metrics_timestamps"]
    assert MetricTimestamp.DISPATCHED_AT.value in stamps
    assert MetricTimestamp.ON_SITE_AT.value in stamps


async def test_an_unusable_submission_records_no_finding_and_asks_the_crew_again(
    dispatched: Any,
) -> None:
    """A submission that will not parse must not become a half-built finding.

    `field_submission` returns `None` for an absent, unparseable or self-contradictory answer, and
    every node downstream is written to survive that: `route_clean_boots_outcome` reads a missing
    finding as `delimit`, `determine_delimiter` audits `no_submission` and returns without touching
    the fault domain, and `route_delimiter_evidence` sends it back for more tests. The guard bounds
    how many times.

    This is the regression evidence for `determine_delimiter`'s early return, which originally
    claimed the no-finding case was unreachable and dereferenced the finding. Shown red by
    reinstating that claim:

        AttributeError: 'NoneType' object has no attribute 'delimiter_kind'
        During task with name 'determine_delimiter'
    """
    _service, snapshot = dispatched
    values, seen = await _drive(
        snapshot.values,
        "unusable",
        lambda p: "not a submission" if _is_submission_pause(p) else APPROVAL,
    )

    assert values.get("field_findings") in (None, []), (
        "an unusable submission must record no finding at all; a partial one would be read as "
        "evidence by every router downstream"
    )
    assert len(seen) > 1, "the crew must be asked again rather than the stage ending"
    assert _outcomes(values, "capture_field_evidence") == ["unusable_submission"] * len(seen)
    assert _outcomes(values, "determine_delimiter") == ["no_submission"] * len(seen)
    assert _outcomes(values, "request_additional_field_tests") == ["d17_insufficient"] * len(seen)
    assert values["status"] is IncidentStatus.ESCALATED and values["escalated"]


async def test_a_qualified_measurement_key_leaves_the_packet_incomplete(dispatched: Any) -> None:
    """`downstream_power_dbmv at tap 4` is not `downstream_power_dbmv`, and the contract says so.

    `build_handover_contract` copies the submission's measurement keys onto the contract unchanged
    and `missing_items` tests them with `in`, so a test point appended to a key leaves the required
    item missing and the packet permanently incomplete -- the crew answered and the graph cannot
    tell. The unit is already in the name and the test point has `first_failed_point` of its own, so
    the convention costs nothing; the trap is that nothing rejects the qualified key at the door.

    Asserted against the bare-key run rather than in isolation, because the claim is a *difference*:
    three items missing instead of none, on submissions identical in every other respect.

    Shown red by having `build_handover_contract` fold each key on its first space, which is
    the tempting fix and is worse than the trap -- it silently accepts a reading from a test
    point nobody named:

        AssertionError: a qualified measurement key must leave the required item missing.
        Got ['measurement:upstream_power_dbmv', 'measurement:downstream_snr_db',
        'ruled_out']
    """
    service, snapshot = dispatched
    qualified = {"downstream_power_dbmv at tap 4": -14.5}

    bare_values, _ = await _drive(
        snapshot.values,
        "bare",
        lambda p: _submission(service) if _is_submission_pause(p) else APPROVAL,
    )
    qualified_values, _ = await _drive(
        snapshot.values,
        "qualified",
        lambda p: (
            _submission(service, measurements=qualified) if _is_submission_pause(p) else APPROVAL
        ),
    )

    assert bare_values["handover_contract"].missing_items() == ["ruled_out"]
    assert qualified_values["handover_contract"].missing_items() == [
        "measurement:downstream_power_dbmv",
        "measurement:upstream_power_dbmv",
        "measurement:downstream_snr_db",
        "ruled_out",
    ], (
        "a qualified measurement key must leave the required item missing. Got "
        f"{qualified_values['handover_contract'].missing_items()}"
    )


# ------------------------------------------------------------------------------------------------
# The exit that never leaves the Clean Boots domain
# ------------------------------------------------------------------------------------------------


async def test_a_fault_fixed_at_the_premises_completes_the_order_and_goes_to_validation(
    dispatched: Any,
) -> None:
    """D16's `validate` arm: one lap, no handover, and the order closed by the node that ended it.

    The order is completed *here* rather than in `capture_field_evidence`, and the difference is the
    `more_tests` loop: `open_work_order` tests non-terminal, so completing on submission would make
    the next lap's `route_visit_gate` answer `no_visit` and the stage would end with an unanswered
    request for measurements. Only the three exits complete an order, and each is a place the visit
    genuinely ended.

    Shown red by dropping the completed order from this node's update:

        AssertionError: assert [<WorkOrderSt...E: 'on_site'>] == [<WorkOrderSt...
        'completed'>]
    """
    service, snapshot = dispatched
    values, seen = await _drive(
        snapshot.values,
        "cleanboots",
        lambda p: (
            _submission(
                service,
                work_completed=True,
                requires_plant_work=False,
                fault_domain="cpe",
                delimiter_kind="unknown",
                delimiter_ref=None,
            )
            if _is_submission_pause(p)
            else APPROVAL
        ),
    )

    assert len(seen) == 1, "a crew that fixed it asks once and leaves"
    assert _outcomes(values, "close_clean_boots_visit") == ["resolved_at_premises"]
    assert [w.status for w in current_work_orders(values).values()] == [WorkOrderStatus.COMPLETED]
    assert values["status"] is IncidentStatus.VALIDATING
    assert not values.get("escalated")
    assert values.get("handover_contract") is None, "nothing was handed over, so nothing was built"


# ------------------------------------------------------------------------------------------------
# EXEC-1: the fixture fact that closes the handover chain, from both sides
# ------------------------------------------------------------------------------------------------


def test_the_rca_this_stage_inherits_weighs_several_domains_and_discards_none(
    dispatched: Any,
) -> None:
    """The measurement behind gap EXEC-1, pinned so the entry cannot go quietly stale.

    P10 weighs more than one domain here and reports none of them as discarded. That is structural
    rather than thin fixtures: `graph.nodes.diagnosis._rejected_before` is the only thing in `src`
    that marks a hypothesis rejected, it seeds them from a *previous* RCA cycle, and no fixture path
    runs P10 twice -- so a first-cycle incident has an empty `ruled_out` by construction. The sweep
    behind the gap entry says the same thing across all 41 services; this pins the one incident the
    module drives, where it can be asserted without a 41-service run.

    Asserted on the RCA the parent concluded rather than on a constructed one, because a constructed
    `RCAResult` would let the assertion pass while `conclude` did something else entirely.

    Fails the day P10 starts reporting the domains it scored near zero as discarded -- which is the
    day the gap entry, this test and the seeded-state test below should be revisited together.

    The three posteriors here are 0.48, 0.31 and 0.21, so a threshold has to be well above
    zero to bite at all -- which is the finding restated: none of them is near enough to zero
    for `build_hypotheses` to have discarded it on the evidence. Shown red by rejecting any
    posterior below a quarter:

        AssertionError: EXEC-1 says a first-cycle RCA discards nothing. Got 1 of 3 rejected.
    """
    _service, snapshot = dispatched
    rca: RCAResult = snapshot.values["rca"]
    assert len(rca.hypotheses) > 1, (
        "this fixture must still weigh more than one domain, or the finding is 'nothing to discard' "
        f"rather than 'discarded nothing'. Got {len(rca.hypotheses)}"
    )
    assert rca.ruled_out == [], (
        f"EXEC-1 says a first-cycle RCA discards nothing. Got {len(rca.ruled_out)} of "
        f"{len(rca.hypotheses)} rejected."
    )


async def test_a_packet_with_nothing_ruled_out_is_rejected_until_the_incident_escalates(
    dispatched: Any,
) -> None:
    """The consequence of EXEC-1, driven rather than reasoned about.

    A packet naming no discarded explanation is one the receiving OSP crew cannot audit -- they get
    a conclusion and no working -- so `missing_items` demands it and D18 refuses without it. With
    the fixture RCA rejecting nothing that refusal is permanent: the crew is asked for more, submits
    the same complete evidence, and the loop runs until the guard escalates. Six laps, and nothing
    filed.

    `prepare_handover_approval`, `request_handover_approval` and `file_plant_mr` are asserted absent
    rather than left unmentioned, because "the chain is unreachable" is the claim and an
    absent-node assertion is the only thing that states it.

    Shown red by deleting the `if not self.ruled_out` clause from `missing_items`, which makes
    the packet complete on the first lap and the whole loop disappear:

        AssertionError: the guard, not the graph, is what stops this loop
        assert 2 == 6
    """
    service, snapshot = dispatched
    values, seen = await _drive(
        snapshot.values,
        "incomplete",
        lambda p: _submission(service) if _is_submission_pause(p) else APPROVAL,
    )

    assert len(seen) == 6, "the guard, not the graph, is what stops this loop"

    contract = values["handover_contract"]
    assert contract.missing_items() == ["ruled_out"]
    assert contract.completeness == pytest.approx(6 / 7)
    assert not contract.accepted

    assert _outcomes(values, "build_handover_contract") == ["incomplete"] * 6
    assert _outcomes(values, "request_additional_field_tests") == ["d18_reject"] * 6
    for unreached in ("prepare_handover_approval", "request_handover_approval", "file_plant_mr"):
        assert _outcomes(values, unreached) == [], (
            f"{unreached} must not be reachable from a fixture"
        )

    assert values["status"] is IncidentStatus.ESCALATED and values["escalated"]
    assert not current_mr_records(values), (
        "nothing may be filed against plant on an incomplete packet"
    )
    assert [w.status for w in current_work_orders(values).values()] == [WorkOrderStatus.ON_SITE]


async def test_one_rejected_hypothesis_completes_the_packet_and_files_the_mr(
    dispatched: Any,
) -> None:
    """Seed the one fact EXEC-1 is missing and the whole chain runs. Nothing else changes.

    This is the other half of the gap entry: the escalation above is caused by the fixture set and
    not by the stage, and the way to show that is to change exactly one thing. One rejected
    hypothesis on the same state, and completeness goes to 1.0, D18 answers `request_approval`, P19
    asks a supervisor, P20 files an MR against the tap and the incident ends at `mr_raised`.

    See `_with_one_rejection` for why the seed is constructed rather than copied.

    The MR is left at `submitted`: `create_mr` returns it that way and this stage never touches it
    again. That is the state `route_plant_outcome` reads as `await_plant`, and it is the arrival
    `subgraphs.plant_execution` is written for -- the handover ends here and the wait starts there.

    Shown red by flipping the seeded hypothesis back to `rejected=False`, which returns the
    state to the one every fixture produces and the run to the six-lap escalation above:

        AssertionError: the crew is asked once and the supervisor once; a second lap means
        the packet is still short
    """
    service, snapshot = dispatched
    seeded, rejected = _with_one_rejection(snapshot.values)

    values, seen = await _drive(
        seeded, "seeded", lambda p: _submission(service) if _is_submission_pause(p) else APPROVAL
    )

    assert seen == [
        ["briefing", "field_submission_request", "outstanding_requests", "requested_items"],
        ["approval_request", "permitted_roles"],
    ], (
        "the crew is asked once and the supervisor once; a second lap means the packet is still short"
    )

    contract = values["handover_contract"]
    assert contract.missing_items() == []
    assert contract.completeness == pytest.approx(1.0)
    assert contract.accepted and contract.accepted_by == APPROVAL["decided_by"]
    assert contract.ruled_out == [f"{rejected.statement} ({rejected.rejection_reason})"], (
        "the packet must carry the reason, not just the count -- that is what makes it auditable"
    )

    filed = list(current_mr_records(values).values())
    assert [m.status for m in filed] == [MRStatus.SUBMITTED], (
        "`create_mr` returns `submitted` and this stage never updates it, so an MR leaves here "
        "exactly as it was filed. `plant_execution.capture_plant_evidence` is what moves it; until "
        "that stage runs, `submitted` is what D19 reads as `await_plant`."
    )
    assert values["status"] is IncidentStatus.MR_RAISED
    assert not values.get("escalated")
    assert [w.status for w in current_work_orders(values).values()] == [WorkOrderStatus.COMPLETED]


async def test_the_filed_mr_is_traceable_from_both_links_and_from_the_closing_note(
    dispatched: Any,
) -> None:
    """Everything `file_plant_mr` adds to `_mr.submit_mr`'s update survives the merge.

    This exists because the mechanism moved out. `submit_mr` is shared with the NOC-direct filer
    and writes the status, the records and `linked_records["mr"]`; `file_plant_mr` merges three
    things of its own onto that -- the accepted contract, the second link, and the completed work
    order -- and a merge is a place where a write can be dropped in silence.

    **Measured: it could be, and nothing said so.** Two of those joins were mutated while the
    extraction was being verified and the whole 904-test suite stayed green, so "the refactor is
    proved by the suite staying green" was not true until this test existed. The two the suite
    already held were the status and the filed `MRStatus`, which the test above asserts.

    The four assertions are one claim: an operator holding the incident can get from it to the MR
    and to the packet it was filed against, and the two records agree about when. `linked_records`
    is what a reconciliation report reads and the note is what a crew reads on the order, so they
    must name the *same* reference -- `mr_reference` owns which string that is. The two are
    genuinely distinguishable here rather than coincidentally equal: jTrack's `external_ref` is
    eight uppercase hex (`MR-927BA175`) and our `derive_id` is twenty lowercase
    (`MR-bb5a4124a61e464c5789`), so a filer that quoted the wrong one could not hide.

    Shown red four times, one mutation per assertion.

    `mr_reference` returning `record.mr_id`, dropping jTrack's reference:

        AssertionError: the incident's MR link must name what jTrack called it, not what we
        called it before asking
        assert 'MR-bb5a4124a61e464c5789' == 'MR-927BA175'

    Dropping `handover_contract` from the merged `linked_records`:

        AssertionError: the packet the MR was filed against must stay reachable from the incident
        assert 'handover_contract' in {'canonical_incident': 'INC-SVC-SJ-011-A-01', 'customer_ref':
        'CUS-SJ-011-A-01', 'mr': 'MR-927BA175', 'parent_incident': 'NXT-ALM-31711604', ...}

    Spelling the note's reference `submission.record.mr_id` instead of `mr_reference`:

        AssertionError: the order's closing note and the incident's MR link must name one MR
        assert 'handed to OS...4a61e464c5789' == 'handed to OSP as MR-927BA175'

    And stamping the acceptance from a fresh `ctx.clock.now()` rather than `submission.completed_at`
    -- six seconds later under this module's advance-on-read clock, which is exactly the drift the
    field exists to prevent:

        AssertionError: the contract is accepted by the act of filing, so it cannot be stamped a
        tick later than the order that filing closed -- both read `submit_mr`'s `completed_at`
        assert datetime.datetime(2026, 3, 2, 14, 31, 33, tzinfo=datetime.timezone.utc) ==
        datetime.datetime(2026, 3, 2, 14, 31, 27, tzinfo=datetime.timezone.utc)
    """
    service, snapshot = dispatched
    seeded, _rejected = _with_one_rejection(snapshot.values)
    values, _seen = await _drive(
        seeded, "traceable", lambda p: _submission(service) if _is_submission_pause(p) else APPROVAL
    )

    contract = values["handover_contract"]
    [filed] = current_mr_records(values).values()
    links = values["linked_records"]

    assert links.get("mr") == (filed.external_ref or filed.mr_id), (
        "the incident's MR link must name what jTrack called it, not what we called it before asking"
    )
    assert "handover_contract" in links, (
        "the packet the MR was filed against must stay reachable from the incident"
    )
    assert links["handover_contract"] == contract.contract_id

    [order] = current_work_orders(values).values()
    assert order.completion_code == "handed_to_osp"
    assert order.notes[-1] == f"handed to OSP as {links['mr']}", (
        "the order's closing note and the incident's MR link must name one MR"
    )
    assert contract.accepted_at == order.completed_at, (
        "the contract is accepted by the act of filing, so it cannot be stamped a tick later than "
        "the order that filing closed -- both read `submit_mr`'s `completed_at`"
    )


# ------------------------------------------------------------------------------------------------
# The arrival that booked nothing
# ------------------------------------------------------------------------------------------------


async def test_an_arrival_with_no_work_order_ends_the_stage_without_escalating(
    no_order: Any,
) -> None:
    """`no_visit` is wired to `END`, not to the escalation arm, and that is the difference.

    Two of `field_planning`'s three exits reach here having deliberately booked nothing -- a plan
    queued for a dispatcher, a field plan abandoned -- and both are decisions somebody already made.
    Escalating them would turn a dispatcher's queue into an incident nobody queued, on the strength
    of this stage finding no order to open.

    So the stage records what it found and leaves the status alone: whatever the branch that booked
    nothing set is still the truth about the incident.

    Shown red by mapping `no_visit` onto the escalation path:

        AssertionError: an arrival that booked nothing is not an escalation; the branch that
        booked nothing already decided that
    """
    _service, snapshot = no_order
    assert not current_work_orders(snapshot.values), (
        "this fixture must still arrive with nothing booked, or the module has stopped exercising "
        "the arrival it was written for"
    )
    before = snapshot.values["status"]

    values, seen = await _drive(snapshot.values, "noorder", lambda p: APPROVAL)

    assert seen == [], "there is no crew to ask"
    assert _outcomes(values, "open_field_visit") == ["no_open_work_order"]
    assert not values.get("escalated"), (
        "an arrival that booked nothing is not an escalation; the branch that booked nothing "
        "already decided that"
    )
    assert values["status"] is before, "this stage has no news about an incident it never visited"
