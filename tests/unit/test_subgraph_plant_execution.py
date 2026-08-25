"""Stage 4's plant wait, driven from the state `field_execution` actually leaves behind.

The arrival is built by running the parent to the `field_planning` seam and then driving
`field_execution` for real, because the only state this stage is written for is one that holds a
filed MR -- and an `MRRecord` constructed here would let the file pass while `file_plant_mr` filed
something else. `SVC-SJ-011-A-01` is the fixture that gets there; `SVC-SJ-011-B-01` is kept as the
counter-case, arriving having booked no visit and filed nothing.

One adapter set across both drives, and it is not tidiness
----------------------------------------------------------
`build_context` builds a fresh `SimulatedAdapters` when it is not handed one, and a simulated MR
exists only in the ledger of the adapter that filed it. So a plant drive on fresh adapters finds
`fetch_open_mrs == []` and `update_plant_mr` records `not_held` -- which is a real outcome with a
test of its own below, but it is *every* outcome if the adapters are rebuilt by accident. The helper
therefore threads one set through the parent, `field_execution` and this stage, and the `not_held`
test is the one place that deliberately does not.

The clock is still rebuilt per drive, for `test_subgraph_field_execution`'s measured reason.

No fixture sends the chase, and the seed says so
-------------------------------------------------
`update_mr`'s decision class is `diagnosis` and `RCAPolicy.minimum_for("diagnosis")` is 0.75.
Measured on the arrival above, `rca.confidence` is **0.2952** -- the leader at 0.4827 against a rival
at 0.3065 -- so the engine answers `requires_approval` naming `low_confidence_rca`, this stage owns
no interrupt for that, and nothing is sent. Swept across the boundary on that same state: 0.74 is
`requires_approval`, 0.75 is `allowed`.

So `not_sent` is what the fixture set produces and `sent` has to be seeded. `_confident_rca` seeds it
the way `_with_one_rejection` seeds EXEC-1's missing fact: through the model's own constructor, and
through `RCAResult.derive` rather than by asserting a number, so the confidence is *computed* from
the hypotheses exactly as P10 computes it. Rivals are rejected with reasons -- which is what makes
them rejectable -- and the surviving hypothesis carries 0.82, which `derive` turns into 0.82 because
nothing competes with it.

What this stage still cannot do
-------------------------------
Nothing pushes OSP's progress at us, so every report here arrives through `interrupt()`. That is gap
EXEC-2, and the tests below drive it as a pause rather than working around it.

Mutation-checked: 12 defects reinstated one at a time, 12 caught, each docstring quoting the message
actually produced. Two of the twelve produced something other than what was predicted and the
docstrings now say so -- one because pytest truncates both sides of a set comparison to the same
string, and one because removing the check under test does not reach the exception it guards against,
the policy having refused first. Only the lifecycle-row defect had collateral, and it was correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain import IncidentStatus, can_transition
from lpr_cpe.domain.diagnosis import RCAHypothesis, RCAResult
from lpr_cpe.domain.enums import (
    CaseType,
    EventSource,
    EvidenceKind,
    FaultDomain,
    KPIName,
    MRStatus,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.field_ops import HandoverContract, MRRecord
from lpr_cpe.domain.governance import AuditEvent
from lpr_cpe.domain.lifecycle import STAGE_TRANSITIONS
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, ONWARD
from lpr_cpe.graph.state import current_mr_records, make_initial_state
from lpr_cpe.graph.subgraphs.field_execution import build_field_execution_graph
from lpr_cpe.graph.subgraphs.plant_execution import (
    CAPTURE_NODE,
    PLANT_REPORT_EXTRAS,
    PLANT_REPORT_FIELDS,
    PLANT_TARGETS,
    SEARCH_NODE,
    build_plant_execution_graph,
    known_open_mr_refs,
    latest_mr,
    outstanding_plant_mr,
    plant_report,
    plant_report_extras,
    route_plant_gate,
)
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer
from lpr_cpe.simulation.loader import build_simulated_adapters

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The one fixture measured reaching this stage with an MR filed against a plant object.
DISPATCHED_SERVICE = "SVC-SJ-011-A-01"

#: Arrives having booked no visit, so `file_plant_mr` never ran and there is nothing to chase.
NO_ORDER_SERVICE = "SVC-SJ-011-B-01"

APPROVAL = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the tap is the confirmed delimiter; hand it to OSP",
}

#: OSP reporting the repair finished, with an instant of their own and all four unmodelled items.
CLOSED_REPORT = {
    "status": "closed",
    "osp_owner": "osp.crew.7",
    "note": "tap replaced and re-terminated",
    "evidence_refs": ["OSP-PHOTO-2"],
    "completed_at": "2026-03-02T16:05:00+00:00",
    "resolution_code": "TAP_REPLACED",
    "components_changed": ["tap"],
    "measurements": {"downstream_power_dbmv": -2.5},
    "dispatch_reference": "OSP-DISP-88",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock the sibling stage tests use, and for the same reason: inside a
    compiled graph the test cannot advance the clock between nodes, so a frozen one would stamp the
    search, the chase and the report with one instant and `mr_cycle_time` would be zero.
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


def _submission(service: dict[str, Any]) -> dict[str, Any]:
    """The Clean Boots packet that completes the handover, keyed off the contract's own table."""
    required = HandoverContract.REQUIRED_BY_TECHNOLOGY[service["technology"]]
    return {
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


def _with_one_rejection(values: Any) -> dict[str, Any]:
    """EXEC-1's missing fact, seeded so the handover packet can complete and an MR can be filed.

    Constructed through `RCAHypothesis` rather than `model_copy` because `_rejection_is_explained`
    is what makes a rejection auditable and a copy would skip it. Same seed, same reason, as
    `test_subgraph_field_execution._with_one_rejection`; without it no fixture reaches this stage
    with anything filed and the whole module would have nothing to drive.
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
    return seeded


def _confident_rca(values: Any) -> dict[str, Any]:
    """The same incident with an RCA the pack would act on. See the module docstring for the bar.

    Rebuilt through `RCAResult.derive`, not by writing `confidence=0.82` onto a copy. `derive`'s
    formula is `leader / (leader + rival) * leader`, so the seed has to do the two things a real
    confident diagnosis does -- discard the rivals, with reasons, and leave one hypothesis carrying
    the weight. A number asserted onto the model would pass the same tests while describing a state
    P10 cannot produce.
    """
    rca: RCAResult = values["rca"]
    hypotheses: list[RCAHypothesis] = []
    for hypothesis in rca.hypotheses:
        fields = hypothesis.model_dump()
        if hypothesis.rejected:
            hypotheses.append(RCAHypothesis(**fields))
        elif hypothesis.fault_domain is rca.fault_domain:
            hypotheses.append(RCAHypothesis(**{**fields, "posterior": 0.82}))
        else:
            hypotheses.append(
                RCAHypothesis(
                    **{
                        **fields,
                        "rejected": True,
                        "rejection_reason": (
                            "the plant delimiter was confirmed on site, which rules this out"
                        ),
                    }
                )
            )
    seeded = dict(values)
    seeded["rca"] = RCAResult.derive(
        concluded_at=rca.concluded_at,
        fault_domain=rca.fault_domain,
        hypotheses=hypotheses,
        delimiter_kind=rca.delimiter_kind,
        delimiter_ref=rca.delimiter_ref,
        evidence_refs=list(rca.evidence_refs),
        summary=rca.summary,
        cycles_used=rca.cycles_used,
    )
    return seeded


async def _arrival(fixtures: Any, ref: str, tag: str, *, seed: bool = True) -> Any:
    """Everything upstream of this stage, run for real, on one adapter set.

    The parent is stopped at `field_planning` and `field_execution` is driven standalone, which is
    the arrangement every stage test here uses: left to itself the parent answers D16 and runs this
    very subgraph, and the test would never see what it was handed.
    """
    service = fixtures.services[ref]
    adapters = build_simulated_adapters(fixtures=fixtures, clock=_Ticking(NOW))

    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=build_memory_checkpointer(),
        interrupt_after=["field_planning"],
    )
    parent_ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    parent_config = {"configurable": {"thread_id": f"parent-{tag}"}}
    await parent.ainvoke(_initial(service), context=parent_ctx, config=parent_config)
    for _ in range(6):
        snapshot = await parent.aget_state(parent_config)
        if not snapshot.interrupts:
            break
        await parent.ainvoke(Command(resume=APPROVAL), context=parent_ctx, config=parent_config)

    values = (await parent.aget_state(parent_config)).values
    if seed:
        values = _with_one_rejection(values)

    stage = build_field_execution_graph().compile(
        name="lpr_cpe_field_execution", checkpointer=build_memory_checkpointer()
    )
    stage_ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    stage_config = {"configurable": {"thread_id": f"field-{tag}"}}
    await stage.ainvoke(values, context=stage_ctx, config=stage_config)
    for _ in range(14):
        snapshot = await stage.aget_state(stage_config)
        if not snapshot.interrupts:
            break
        payload = snapshot.interrupts[0].value
        reply: Any = (
            _submission(service)
            if isinstance(payload, dict) and "briefing" in payload
            else APPROVAL
        )
        await stage.ainvoke(Command(resume=reply), context=stage_ctx, config=stage_config)
    return service, (await stage.aget_state(stage_config)).values, adapters


async def _drive(
    state: Any, tag: str, answer: Any, *, adapters: Any = None, laps: int = 8, clock: Any = None
) -> Any:
    """Run this stage to a standstill, answering every pause, and report the payloads seen.

    `adapters=None` is not a default so much as a case: it makes `build_context` construct a fresh
    set, which is the process-restart arrangement `not_held` exists for.

    `clock=None` builds a fresh `_Ticking(NOW)`, which is right for a single drive and **wrong for
    a caller driving the stage more than once**. Each fresh clock restarts at `NOW` and advances on
    read, so two drives that run the same nodes stamp the same instants -- measured, a second OSP
    report re-stamped `accepted_at` with a value equal to the first, and the mutation that proves
    the field is write-once survived because the two instants collided. A caller modelling D19's
    self-loop passes one clock through every round.
    """
    ctx = build_context(clock=clock or _Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    graph = build_plant_execution_graph().compile(
        name="lpr_cpe_plant_execution", checkpointer=build_memory_checkpointer()
    )
    config = {"configurable": {"thread_id": f"plant-{tag}"}}
    await graph.ainvoke(state, context=ctx, config=config)

    seen: list[Any] = []
    for _ in range(laps):
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts:
            break
        payload = snapshot.interrupts[0].value
        seen.append(payload)
        await graph.ainvoke(Command(resume=answer(payload)), context=ctx, config=config)
    return (await graph.aget_state(config)).values, seen


def _outcomes(values: Any, node: str) -> list[str]:
    return [e.outcome for e in values.get("audit_events") or [] if e.node == node]


def _detail(values: Any, node: str) -> dict[str, Any]:
    """The most recent audit detail written by one node."""
    return [e for e in values["audit_events"] if e.node == node][-1].detail


def _new_kpis(before: Any, after: Any) -> set[KPIName]:
    seen = {event.event_id for event in before.get("kpi_events") or []}
    return {e.kpi_name for e in after.get("kpi_events") or [] if e.event_id not in seen}


@pytest.fixture
async def with_mr(fixtures: Any) -> Any:
    """The incident arriving with an MR filed against the tap and jTrack still holding it open."""
    return await _arrival(fixtures, DISPATCHED_SERVICE, "mr")


@pytest.fixture
async def without_mr(fixtures: Any) -> Any:
    """The incident arriving having filed nothing, which is what `no_plant_action` is for."""
    return await _arrival(fixtures, NO_ORDER_SERVICE, "nomr", seed=False)


# ------------------------------------------------------------------------------------------------
# The four readers the 2026-08-24 sweep found nothing holding
# ------------------------------------------------------------------------------------------------
#
# Every mutation closed below survived this module's own tests *and* the whole suite. They are
# grouped as four claims rather than seven tests because that is how many distinct questions they
# are: which MR, whether to chase it, what jTrack said last, and what OSP's answer is allowed to
# change.


def _mr(mr_id: str, *, status: MRStatus, updated_at: datetime, **extra: Any) -> MRRecord:
    return MRRecord(
        mr_id=mr_id,
        incident_id="INC-1",
        external_ref=f"JT-{mr_id}",
        plant_object_ref="ODP-1",
        fault_domain=FaultDomain.DISTRIBUTION,
        status=status,
        created_at=NOW,
        updated_at=updated_at,
        **extra,
    )


def test_the_stage_chases_the_newest_mr_and_only_while_osp_holds_it() -> None:
    """Which MR this stage acts on, and whether it acts at all. Three survivors, one question.

    `latest_mr` takes `max(..., key=updated_at)`; the sweep replaced it with the dict head and
    nothing noticed, because every state in the suite holds exactly one MR. `outstanding_plant_mr`
    then narrows to `awaiting_osp`, and dropping that clause also went unnoticed — which matters
    more than the first, because `route_plant_gate` reads it: an incident whose MR OSP has already
    **closed** would be chased again, `update_plant_mr` would file a fresh chase note against a
    finished repair, and D19's `await_plant` self-loop would spin until the re-entry budget stopped
    it.

    Two MRs are seeded with distinct `updated_at`, and the newest is deliberately *not* first in
    insertion order, so the dict head and the newest are different records rather than accidentally
    the same one.

    Watched red three ways::

        latest_mr -> next(iter(...)):        assert 'MR-old' == 'MR-new'
        outstanding_plant_mr -> record:      assert 'chase' == 'no_plant_action'
        route_plant_gate -> latest_mr:       assert 'chase' == 'no_plant_action'
    """
    older = _mr("MR-old", status=MRStatus.SUBMITTED, updated_at=NOW)
    newer = _mr("MR-new", status=MRStatus.IN_PROGRESS, updated_at=NOW + timedelta(hours=1))
    state: Any = {"mr_records": [older, newer]}

    assert latest_mr(state) is not None
    assert latest_mr(state).mr_id == "MR-new", "the newest revision is the one this stage acts on"

    # Both are still with OSP, so there is something to chase.
    assert outstanding_plant_mr(state) is not None
    assert route_plant_gate(state) == "chase"

    # The newest is finished. Nothing is outstanding, whatever the older one says -- and the older
    # one is deliberately left `submitted` so that a reader taking the dict head would still chase.
    finished: Any = {
        "mr_records": [
            older,
            _mr("MR-new", status=MRStatus.CLOSED, updated_at=NOW + timedelta(hours=2)),
        ]
    }
    assert outstanding_plant_mr(finished) is None, "OSP has closed the MR this stage was chasing"
    assert route_plant_gate(finished) == "no_plant_action"


def test_the_open_mr_refs_are_the_ones_the_latest_search_returned(now: datetime) -> None:
    """`known_open_mr_refs` walks the audit trail backwards, and the direction is load-bearing.

    `update_plant_mr` refuses to send an update for an MR jTrack is not holding open, and this is
    the reader that decides. Reversing the walk — taking the *first* search rather than the last —
    survived everything, because no state in the suite held two searches. On a second lap it would
    answer with the previous lap's refs: an MR jTrack had since closed would still be chased, and
    one it had just opened would be refused as `not_held`.

    Watched red by dropping `reversed`::

        AssertionError: assert ('JT-2',) == ('JT-1',)
    """

    def _search(refs: list[str], at: datetime) -> AuditEvent:
        return AuditEvent(
            event_id=f"EV-{at.isoformat()}",
            incident_id="INC-1",
            occurred_at=at,
            node=SEARCH_NODE,
            action="fetch_open_mrs",
            outcome="searched",
            actor="automation",
            detail={"open_mr_refs": refs},
        )

    state: Any = {
        "audit_events": [
            _search(["JT-1"], now),
            _search(["JT-2"], now + timedelta(minutes=5)),
        ]
    }
    assert known_open_mr_refs(state) == ("JT-2",), "the latest search is the one jTrack answered"

    assert known_open_mr_refs({}) == (), "no search yet is not the same as an empty search"


def test_a_report_that_is_not_a_mapping_is_refused_rather_than_read_as_empty() -> None:
    """OSP's answer has to be a mapping with a status this system knows, or it is not a report.

    `plant_report` returns `None` for anything else, and `capture_plant_evidence` records
    `unusable_report` and asks again rather than writing a revision. The sweep replaced the
    `isinstance` refusal with `answer = {}`, which then falls through to the `MRStatus("")`
    `ValueError` and still returns `None` — so that one is equivalent. What was **not** equivalent,
    and survived, is the pair together: coercing an unparseable status to `SUBMITTED` writes a
    revision claiming OSP said something it did not.

    Driven over the shapes a webhook actually produces, because that is where a non-mapping comes
    from: a bare string, a list, `None`, and a mapping whose status is not an `MRStatus`.

    Watched red by defaulting the status to `SUBMITTED`::

        AssertionError: 'in_the_van' is not an MRStatus and must not be read as one
    """
    for answer in (None, "closed", ["closed"], 7, {"status": "in_the_van"}, {}):
        assert plant_report(answer) is None, (
            f"{answer!r} is not a plant report and must not be read as one"
        )

    usable = plant_report({"status": "completed", "osp_owner": "osp.crew"})
    assert usable is not None and usable["status"] is MRStatus.COMPLETED


async def test_osps_answer_moves_only_the_fields_that_answer_names(with_mr: Any) -> None:
    """`accepted_at` is stamped once and `closed_at` only by a close. Two survivors, one rule.

    Both mutations widened what a report may overwrite. `accepted_at` is
    `now if newly_accepted else record.accepted_at`, where `newly_accepted` requires
    `record.accepted_at is None`; dropping that half re-stamps the acceptance instant every time OSP
    re-reports `accepted`, so "when did OSP take this" becomes "when did we last ask". And
    `closed_at` is stamped `if status is MRStatus.CLOSED`; widening it to `if finished` back-dates a
    closure onto an MR that OSP reported merely `completed`, which is the distinction
    `MRRecord.terminal` and the jTrack reconciler both read.

    Three rounds are driven against the real node rather than the model, because the rule is the
    node's: `accepted`, `accepted`, then `completed`.

    Each round is a **separate invocation**, and that is the shape rather than a convenience.
    `capture_plant_evidence` runs straight to `END` inside this subgraph, so a second report is not
    another lap within one run -- it is D19 answering `await_plant` and the parent re-entering the
    stage. Driving it as three invocations of the compiled subgraph, each fed the previous one's
    state, is that loop with the parent left out.

    Watched red twice::

        newly_accepted -> status is ACCEPTED:  AssertionError: accepted_at moved on the second
                                               accepted report
        closed_at -> if finished:              AssertionError: a completed MR that OSP never closed
                                               has no closed_at
    """
    _service, arrival, adapters = with_mr

    def reporting(status: str) -> Any:
        def answer(payload: Any) -> Any:
            del payload
            return {"status": status, "osp_owner": "osp.crew", "note": f"OSP says {status}"}

        return answer

    # One clock across all three rounds. A fresh one per drive restarts at `NOW` and reaches the
    # stamp after the same number of reads, so the two `accepted` instants come out equal and the
    # re-stamping mutation becomes invisible -- measured, and `_drive`'s docstring records it.
    clock = _Ticking(NOW)
    state = arrival
    for round_number, status in enumerate(("accepted", "accepted", "completed"), start=1):
        state, seen = await _drive(
            state, f"osp-{round_number}", reporting(status), adapters=adapters, laps=2, clock=clock
        )
        assert seen, f"round {round_number} did not reach the report gate"

    accepted = [r for r in state["mr_records"] if r.accepted_at is not None]
    assert accepted, "an accepted report has to stamp accepted_at at least once"
    stamps = {r.accepted_at for r in accepted}
    assert len(stamps) == 1, f"accepted_at moved on the second accepted report: {sorted(stamps)}"

    final = max(state["mr_records"], key=lambda r: r.revision)
    assert final.status is MRStatus.COMPLETED
    assert final.completed_at is not None, "a completed MR carries a completion instant"
    assert final.closed_at is None, "a completed MR that OSP never closed has no closed_at"


# ------------------------------------------------------------------------------------------------
# The shape LangGraph received
# ------------------------------------------------------------------------------------------------


def test_the_gate_hangs_off_the_search_and_not_off_start() -> None:
    """`START` runs into node one unconditionally; the question is asked on the way out of it.

    An edge from `START` would have to carry an `ESCALATED` arm, and nothing could ever take it: the
    parent's edge into this subgraph is guarded already, so an escalated incident does not arrive
    here. It is a node's *own* `check_budgets` that can newly escalate, which is why the guarded
    edge belongs after `search_plant_mr` rather than before it -- the same placement, for the same
    reason, as `build_field_execution_graph`'s visit gate.

    The tables are read back off the `StateGraph` rather than compared with `PLANT_TARGETS`, which
    would only prove the table equals itself. `guarded()` returns a fresh closure per call with no
    `__wrapped__`, so the router itself cannot be compared by identity and `ends` is what there is.

    Shown red by moving the conditional edge onto `START`, which leaves one plain edge in the whole
    stage:

        AssertionError: START must run into search_plant_mr unconditionally
        assert ('__start__', 'search_plant_mr') in {('capture_plant_evidence', '__end__')}
    """
    graph = build_plant_execution_graph()

    assert (START, SEARCH_NODE) in set(graph.edges), (
        "START must run into search_plant_mr unconditionally"
    )
    assert START not in graph.branches, "the gate belongs after node one, not before it"
    assert (CAPTURE_NODE, END) in set(graph.edges)

    gate = next(iter(graph.branches[SEARCH_NODE].values()))
    assert dict(gate.ends or {}) == {**PLANT_TARGETS, ESCALATED: END}

    onward = next(iter(graph.branches["update_plant_mr"].values()))
    assert dict(onward.ends or {}) == {ONWARD: CAPTURE_NODE, ESCALATED: END}, (
        "all three chase outcomes lead to the capture; OSP's report is not conditional on our "
        "having managed to chase them for it"
    )


def test_the_status_this_stage_writes_needs_no_seam_entry() -> None:
    """`awaiting_plant_repair` is one legal hop from every status that can reach the capture.

    Only the `chase` arm reaches `capture_plant_evidence`, and `route_plant_gate` takes it only when
    an MR is `awaiting_osp`. An incident holds one of those in exactly two statuses: `mr_raised`,
    which `file_plant_mr` writes, and `awaiting_plant_repair` itself, which is D19's `await_plant`
    self-loop coming back round. Both are single hops, so unlike `field_execution` this stage needs
    no `STAGE_TRANSITIONS` entry -- and an entry appearing here later would be evidence that
    something started writing a status the incident cannot get to in one step.

    Reinstated by removing `AWAITING_PLANT_REPAIR` from `TRANSITIONS[MR_RAISED]`:

        AssertionError: mr_raised cannot reach awaiting_plant_repair in one hop, so the parent
        would refuse this stage's only status write

    Six of the drives below died with it, and that collateral is what makes this more than a table
    read -- the refusal is not hypothetical, it is where every chase in this module ends:

        IllegalTransitionError: illegal incident transition mr_raised -> awaiting_plant_repair;
        permitted from mr_raised: ['awaiting_handover', 'cancelled', 'escalated']
        During task with name 'capture_plant_evidence'
    """
    for entry in (IncidentStatus.MR_RAISED, IncidentStatus.AWAITING_PLANT_REPAIR):
        assert can_transition(entry, IncidentStatus.AWAITING_PLANT_REPAIR), (
            f"{entry.value} cannot reach awaiting_plant_repair in one hop, so the parent would "
            "refuse this stage's only status write"
        )

    seams = sorted(f"{a.value} -> {b.value}" for a, b in STAGE_TRANSITIONS if b is entry)
    assert not seams, f"this stage walks no middle, so it should own no seam entry: {seams}"


# ------------------------------------------------------------------------------------------------
# The arrival with nothing to chase
# ------------------------------------------------------------------------------------------------


async def test_an_arrival_with_no_mr_says_nothing_about_the_incident(without_mr: Any) -> None:
    """`no_plant_action` ends the stage and leaves the status where the branch that got here set it.

    Two of `field_execution`'s exits reach this stage having filed nothing, and both were decisions
    somebody already made -- a visit that was never booked, a handover that was refused. Escalating
    that, or writing `awaiting_plant_repair` over it, would be this stage inventing a plant wait out
    of the absence of one. D19 reads the missing MR as `retry_diagnosis`, which is a diagnosis to
    redo rather than an incident to page anyone about.

    `search_plant_mr` does not reach jTrack at all here: with no MR on the incident there is no
    plant object to search against, which is the `no_mr_on_record` outcome.

    Shown red by mapping `no_plant_action` onto `update_plant_mr`, which is the plausible mistake --
    the node raises rather than filing anything, so the arm cannot be quietly wrong:

        ValueError: update_plant_mr was reached with no MR awaiting OSP. `route_plant_gate` sends
        that case to the end of the stage, so this edge cannot produce one.
    """
    _service, at_seam, adapters = without_mr
    assert not current_mr_records(at_seam), (
        "this fixture must still arrive with nothing filed, or the module has stopped exercising "
        "the arrival it was written for"
    )
    before = at_seam["status"]

    values, seen = await _drive(at_seam, "nomr", lambda p: CLOSED_REPORT, adapters=adapters)

    assert seen == [], "there is no crew to ask about an MR that was never raised"
    assert _outcomes(values, SEARCH_NODE) == ["no_mr_on_record"]
    assert _outcomes(values, "update_plant_mr") == []
    assert _outcomes(values, CAPTURE_NODE) == []
    assert not values.get("escalated")
    assert values["status"] is before, (
        "this stage has no news about a plant repair nobody asked for"
    )


# ------------------------------------------------------------------------------------------------
# The chase
# ------------------------------------------------------------------------------------------------


async def test_the_rca_every_fixture_produces_refuses_the_chase_and_still_asks_osp(
    with_mr: Any,
) -> None:
    """`not_sent` is the measured default, and it does not stop the stage.

    The pack allows `update_mr` outright, so the refusal is not about the action: it is the
    `diagnosis` class bar of 0.75 against this incident's 0.2952. `ActionRequest` refuses a
    `REQUIRES_APPROVAL` verdict with no `approval_ref` and this stage owns no interrupt to get one,
    so the verdict is recorded and nothing is sent.

    The half that matters is what happens next. Not being able to chase OSP is not a reason to stop
    waiting on them, so the edge runs on to the capture regardless and the incident still ends at
    `awaiting_plant_repair`. `plant_attempt_count` stays at 0, because `attempt_number` counts
    actions that reached an adapter and a refusal reached none -- a chase OSP ignored and a chase we
    never sent are different facts.

    Shown red by writing `plant_attempt_count` on the refusal branch as well:

        AssertionError: a refused chase never reached OSP, so it must not count as an attempt
        they ignored
        assert 1 == 0
    """
    _service, at_seam, adapters = with_mr
    assert at_seam["rca"].confidence == pytest.approx(0.2952), (
        "the module docstring's measured bar is quoted against this number; if the fixture moved, "
        "re-measure the 0.75 sweep rather than editing this line"
    )

    values, seen = await _drive(at_seam, "refused", lambda p: CLOSED_REPORT, adapters=adapters)

    assert _outcomes(values, "update_plant_mr") == ["not_sent"]
    detail = _detail(values, "update_plant_mr")
    assert detail["policy_outcome"] == "requires_approval"
    assert detail["required_approval_kind"] == "low_confidence_rca"

    assert len(seen) == 1, "OSP is still asked to report, whether or not we managed to chase them"
    assert values["status"] is IncidentStatus.AWAITING_PLANT_REPAIR
    assert values.get("plant_attempt_count", 0) == 0, (
        "a refused chase never reached OSP, so it must not count as an attempt they ignored"
    )


async def test_a_confident_rca_sends_the_chase_and_the_next_lap_carries_a_new_key(
    with_mr: Any,
) -> None:
    """The key moves per lap, which is the opposite of the rule the MR's creation follows.

    `mr_idempotency_key` is fixed per plant object precisely so a re-offered packet cannot file a
    second MR. A chase must not inherit that: `SimulatedAdapterBase` returns a repeat of a known key
    as `replayed` and does not touch the ledger, so a fixed key would tell OSP once however long the
    MR sat there. `attempt_number` is what moves it, and it counts actions that reached the adapter
    -- so `with_retry`'s retries inside one node share a key and a genuine second lap does not.

    Driven as two laps rather than asserted from the helper, because the claim is about what the
    *second* visit does with state the first one left. D19's `await_plant` arm is what brings the
    incident back round, and the MR is still `in_progress` after lap one, so it is still
    `awaiting_osp` and still chaseable.

    Shown red by dropping `attempt` from `plant_update_idempotency_key`:

        AssertionError: the second lap must not reuse the first lap's key, or the ledger replays
        it and OSP is told once
        assert 'IDK-48093c034692add5caff' != 'IDK-48093c034692add5caff'
    """
    _service, at_seam, adapters = with_mr
    seeded = _confident_rca(at_seam)
    assert seeded["rca"].confidence == pytest.approx(0.82), "the seed must clear the 0.75 bar"

    first, _seen = await _drive(
        seeded,
        "sent1",
        lambda p: {"status": "in_progress", "note": "crew mobilised"},
        adapters=adapters,
    )
    assert _outcomes(first, "update_plant_mr") == ["simulated"]
    assert first["plant_attempt_count"] == 1

    second, _again = await _drive(first, "sent2", lambda p: CLOSED_REPORT, adapters=adapters)
    keys = [d["idempotency_key"] for d in (_detail(first, "update_plant_mr"),)]
    keys.append(_detail(second, "update_plant_mr")["idempotency_key"])
    attempts = [d["attempt"] for d in (_detail(first, "update_plant_mr"),)]
    attempts.append(_detail(second, "update_plant_mr")["attempt"])

    assert attempts == [1, 2]
    assert keys[0] != keys[1], (
        "the second lap must not reuse the first lap's key, or the ledger replays it and OSP is "
        "told once"
    )
    assert second["plant_attempt_count"] == 2
    assert [m.status for m in current_mr_records(second).values()] == [MRStatus.CLOSED]


async def test_jtrack_not_holding_the_mr_is_recorded_rather_than_raised(with_mr: Any) -> None:
    """`update_mr` refuses non-retryably, so the refusal is anticipated instead of caught.

    Reproduced by rebuilding the adapters, which is the process-restart case the simulator makes
    real: a simulated MR exists only in the ledger of the adapter that filed it, so a fresh set
    answers `fetch_open_mrs` with `[]` while the incident still carries the record. That is exactly
    the shape of the production failure -- jTrack no longer holding open an MR our state believes in
    -- and asking first is what turns it from an exception mid-stage into an outcome on a path the
    state machine has a status for.

    The capture still runs. Whether jTrack will take our note has no bearing on whether OSP has done
    the work.

    Shown red by widening the check so it can never trip -- and the `AdapterError` that was expected
    is not what came out, which is worth recording. With the check gone the node falls through to the
    policy call, which refuses this incident's 0.2952 exactly as the test above shows, so the adapter
    is never reached at all:

        AssertionError: assert ['not_sent'] == ['not_held']
        At index 0 diff: 'not_sent' != 'not_held'

    So on this state it is the policy, not the exception, that the check is standing in front of --
    and `not_sent` recorded against an MR jTrack is not holding is the wrong entry in the trail,
    because it says we chose not to chase rather than that there was nothing there to chase.
    """
    _service, at_seam, _adapters = with_mr

    values, seen = await _drive(at_seam, "notheld", lambda p: CLOSED_REPORT)

    assert _outcomes(values, SEARCH_NODE) == ["searched"]
    assert _detail(values, SEARCH_NODE)["open_mr_refs"] == [], (
        "a rebuilt adapter set holds no MR this process did not file through it"
    )
    assert _outcomes(values, "update_plant_mr") == ["not_held"]
    assert _detail(values, "update_plant_mr")["reason"].startswith("jTrack is not holding")
    assert len(seen) == 1 and _outcomes(values, CAPTURE_NODE) == ["closed"]


# ------------------------------------------------------------------------------------------------
# The report
# ------------------------------------------------------------------------------------------------


async def test_the_report_moves_the_mr_and_keeps_osps_own_completion_instant(with_mr: Any) -> None:
    """P21, recorded as a revision, with the three KPIs the closure lets `preview` derive.

    The pause asks for all eleven items -- the seven `MRRecord` holds and the four no model does --
    and the four unmodelled ones are carried on the audit event rather than in new state fields,
    because the node that recorded a fact is a better owner of it than a second field that can
    drift.

    `completion_time_supplied` is the flag worth having: a cycle time measured from the instant OSP
    told us is an upper bound on the one measured from when they finished, and a reader who cannot
    tell which they are looking at will treat both as OSP's own measurement.

    The KPIs are emitted off `preview`, not `state`: all three read the revision still sitting
    unreduced in this node's own update, so reading `state` would measure the MR as it was before
    the report. `mr_cycle_time` in particular returns nothing at all until an MR closes, which is
    why the `in_progress` lap in the sibling test emits two KPIs here and this one emits three.

    Shown red by passing `state` instead of `preview(state, update)` to `emit_kpi`. Pytest truncates
    both sides to the same string, so the line that carries the diagnosis is the one under it:

        AssertionError: assert {<KPIName.MR_...air_backlog'>} == {<KPIName.MR_...air_backlog'>}
        Extra items in the right set:
        <KPIName.MR_CYCLE_TIME_SECONDS: 'mr_cycle_time_seconds'>
    """
    _service, at_seam, adapters = with_mr

    values, seen = await _drive(at_seam, "report", lambda p: CLOSED_REPORT, adapters=adapters)

    assert sorted(seen[0]) == ["plant_report_request", "requested_items"]
    assert seen[0]["requested_items"] == [*PLANT_REPORT_FIELDS, *PLANT_REPORT_EXTRAS]
    assert seen[0]["plant_report_request"]["round"] == 1

    (record,) = current_mr_records(values).values()
    assert record.status is MRStatus.CLOSED
    assert record.revision == 2
    assert record.completed_at == datetime(2026, 3, 2, 16, 5, tzinfo=UTC)
    assert record.closed_at == record.completed_at
    assert record.notes[-1] == CLOSED_REPORT["note"]

    detail = _detail(values, CAPTURE_NODE)
    assert detail["previous_status"] == MRStatus.SUBMITTED.value
    assert detail["completion_time_supplied"] is True
    assert detail["resolution_code"] == "TAP_REPLACED"
    assert detail["measurements"] == {"downstream_power_dbmv": -2.5}

    evidence = values["evidence"][-1]
    assert evidence.kind is EvidenceKind.MR_UPDATE
    assert evidence.source_system == "jtrack", (
        "the system of record for the MR, not the channel the words happened to arrive through"
    )
    assert _new_kpis(at_seam, values) == {
        KPIName.PLANT_REPAIR_BACKLOG,
        KPIName.MR_REJECTION_RATE,
        KPIName.MR_CYCLE_TIME_SECONDS,
    }


async def test_an_unusable_report_records_no_revision_and_asks_again(with_mr: Any) -> None:
    """A status nobody recognises leaves the MR alone rather than being corrected into one that is.

    A resume with no payload, a timer tick and a garbled body all mean the same thing -- *we still
    do not know what OSP did* -- and inventing a status to fill that gap is how an MR comes to be
    reported as progressing when nobody has touched it. So nothing is written: no revision, no
    evidence, no KPI. The MR stays `submitted`, which D19 reads as `await_plant`, and the cost of an
    unusable report is one bounded lap.

    The status *is* still written, and that is not an inconsistency. The incident really is waiting
    on a plant repair; it is the MR we have learned nothing new about.

    Shown red by falling back to `MRStatus.SUBMITTED` when the parse fails, which is the tempting
    mistake because it looks like a no-op and is in fact a revision saying nothing changed:

        AssertionError: an unusable report must not produce a revision; the MR is where it was
        assert (<MRStatus.SU...ubmitted'>, 2) == (<MRStatus.SU...ubmitted'>, 1)
    """
    _service, at_seam, adapters = with_mr

    values, seen = await _drive(
        at_seam,
        "unusable",
        lambda p: {"status": "banana", "note": "crew says it is fine"},
        adapters=adapters,
    )

    assert len(seen) == 1
    (record,) = current_mr_records(values).values()
    assert (record.status, record.revision) == (MRStatus.SUBMITTED, 1), (
        "an unusable report must not produce a revision; the MR is where it was"
    )
    assert _outcomes(values, CAPTURE_NODE) == ["unusable_report"]
    detail = _detail(values, CAPTURE_NODE)
    assert detail["keys"] == ["note", "status"], "the keys we did get are the diagnosis"
    assert _new_kpis(at_seam, values) == set()
    assert values["status"] is IncidentStatus.AWAITING_PLANT_REPAIR


def test_a_naive_completion_instant_is_dropped_rather_than_localised() -> None:
    """Guessing the zone would trade a missing KPI for a crashing one.

    `MRRecord.cycle_time()` subtracts `submitted_at` from `completed_at` and Python raises on a
    naive/aware pair, so a localised guess would be a `TypeError` at the point a KPI is derived --
    hours after the report, in a node that did nothing wrong. Dropping it means the instant we were
    told is used instead and `completion_time_supplied` records that it was.

    The unrecognised status is here too because it is the same rule at a different field: coercion
    is confined to shapes, and a value whose *meaning* is unknown is not a shape problem.

    Shown red by falling back to `datetime.fromisoformat(...).replace(tzinfo=UTC)`:

        AssertionError: a naive instant has no zone to trust; it must be dropped
        assert datetime.datetime(2026, 3, 2, 16, 5) is None
    """
    assert plant_report(None) is None
    assert plant_report({"status": "banana"}) is None
    assert plant_report({}) is None

    naive = plant_report({"status": "closed", "completed_at": "2026-03-02T16:05:00"})
    assert naive is not None
    assert naive["completed_at"] is None, "a naive instant has no zone to trust; it must be dropped"

    aware = plant_report({"status": "closed", "completed_at": "2026-03-02T16:05:00+00:00"})
    assert aware is not None
    assert aware["completed_at"] == datetime(2026, 3, 2, 16, 5, tzinfo=UTC)

    # A measurement that is a bool is not a measurement. `isinstance(True, int)` is what this is for.
    assert plant_report_extras({"measurements": {"flagged": True, "level": 2}})["measurements"] == {
        "level": 2.0
    }


async def test_escalation_stops_the_stage_after_the_search(with_mr: Any) -> None:
    """The guard is on the edge, so node one runs and nothing after it does.

    `escalation_update` stops a node from doing work but does not stop the graph, which is why every
    onward edge here is `guarded`. An unguarded one would chase OSP about an MR after the incident's
    budget had been declared exhausted -- and worse, would then pause for a report nobody is coming
    to give, holding the thread open at an `interrupt()` the parent has already routed away from.

    Shown red by wiring `route_plant_gate` bare instead of `guarded(route_plant_gate)`. The refusal
    the pack happens to give is what stops the chase reaching OSP; the outcome is still recorded, and
    an outcome is exactly what an escalated incident must not have here:

        AssertionError: an escalated incident must not be chased or paused
        assert ['not_sent'] == []
    """
    _service, at_seam, adapters = with_mr
    escalated = {**at_seam, "escalated": True}

    values, seen = await _drive(escalated, "esc", lambda p: CLOSED_REPORT, adapters=adapters)

    assert seen == []
    assert _outcomes(values, "update_plant_mr") == [], (
        "an escalated incident must not be chased or paused"
    )
    assert _outcomes(values, CAPTURE_NODE) == []
    assert current_mr_records(values)[next(iter(current_mr_records(at_seam)))].status is (
        MRStatus.SUBMITTED
    )


def test_the_report_fields_are_the_ones_the_parser_returns() -> None:
    """The pause's request list and the parser's output must not drift apart.

    `PLANT_REPORT_FIELDS` is what a crew is *asked* for and `plant_report` is what is *read* back.
    Two lists, one contract: an item added to the request that the parser ignores is a question
    whose answer is discarded, and a field the parser reads that nobody was asked for is a silent
    dependency on the caller having guessed.

    Shown red by adding `"crew_notes"` to `PLANT_REPORT_FIELDS`:

        AssertionError: asked for but never read: ['crew_notes']
    """
    parsed = plant_report({"status": "closed"})
    assert parsed is not None
    unread = sorted(set(PLANT_REPORT_FIELDS) - set(parsed))
    assert not unread, f"asked for but never read: {unread}"
    unasked = sorted(set(parsed) - set(PLANT_REPORT_FIELDS))
    assert not unasked, f"read but never asked for: {unasked}"

    extras = plant_report_extras({})
    assert sorted(extras) == sorted(PLANT_REPORT_EXTRAS)
    assert set(PLANT_REPORT_FIELDS) & set(PLANT_REPORT_EXTRAS) == set(), (
        "an item belongs either to the record or to the audit event, not to both"
    )


async def test_a_rejection_is_recorded_with_the_reason_osp_gave(with_mr: Any) -> None:
    """OSP refusing the MR is the end of the plant theory, and the trail has to say why.

    This is the arrival D19 answers `retry_diagnosis` on, and it reaches that answer by two separate
    readings that must agree. `rejection_reason` is what a human needs -- an MR that came back with
    no reason is one nobody can act on -- and `awaiting_osp` is what the *routing* needs:
    `MRStatus.REJECTED` is outside it, so the next lap's gate answers `no_plant_action` and the
    stage stops chasing something OSP has already refused.

    The reason code is `HANDOVER_REJECTED_INCOMPLETE` rather than the `STABILITY_WINDOW_PENDING`
    that an in-progress report carries, because those are what a reviewer filters the trail by and
    collapsing them would hide a refusal among the waits.

    Shown red by dropping the `status is MRStatus.REJECTED` arm from the reason-code choice, so a
    rejection is recorded as a wait:

        AssertionError: assert <ReasonCode.STABILITY_WINDOW_PENDING: 'STABILITY_WINDOW_PENDING'>
        is <ReasonCode.HANDOVER_REJECTED_INCOMPLETE: 'HANDOVER_REJECTED_INCOMPLETE'>
    """
    _service, at_seam, adapters = with_mr
    report = {
        "status": "rejected",
        "rejection_reason": "the tap is on a pole scheduled for replacement; raise against the pole",
        "note": "returned to the raising crew",
    }

    values, _seen = await _drive(at_seam, "rejected", lambda p: report, adapters=adapters)

    (record,) = current_mr_records(values).values()
    assert record.status is MRStatus.REJECTED
    assert record.rejection_reason == report["rejection_reason"]
    assert not record.awaiting_osp, (
        "a refused MR is not still with OSP; the next lap's gate must stop chasing it"
    )

    event = [e for e in values["audit_events"] if e.node == CAPTURE_NODE][-1]
    assert event.reason_code is ReasonCode.HANDOVER_REJECTED_INCOMPLETE
    assert KPIName.MR_REJECTION_RATE in _new_kpis(at_seam, values)
