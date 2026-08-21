"""Stage 5's first half, compiled and run: the wait, the re-read, and the verdict.

The incident is carried through the real parent to P11 and then handed to this subgraph directly,
which is the same construction `test_subgraph_remote_resolution.py` uses and for the same reason: a
hand-built state would let this module pass while `assemble_case_evidence` gathered something
`_sources_read_before` cannot recognise. What *is* seeded is the repair -- an `ActionRecord` with a
`completed_at` -- because no fixture reaches a completed repair on its own today and the whole of
this stage is measured from that instant.

`pon_degraded_optical` is the service, and it is chosen for being the plainest rather than the most
interesting: it answers D08 `plant_path`, so the parent stops at P11 with evidence gathered, findings
recorded and nothing executed. A service that ran a branch would arrive here with an action history
of its own and the seeded repair would no longer be the only thing the window could be measured from.

What is deliberately not asserted here
--------------------------------------
D21 is not exercised. All three of its answers are outside this graph, so it lives on the parent's
edge and is tested in `test_routing.py` against constructed state and in `test_builder.py` as
wiring. P23 is not here either, for a stronger reason: it is a parent node. `test_nodes.py` has it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.detectors.base import DetectorResult
from lpr_cpe.domain.diagnosis import AnomalyFinding
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    CaseType,
    EventSource,
    EvidenceKind,
    IncidentStatus,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.domain.governance import ActionRecord
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, ONWARD
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.graph.subgraphs.restoration_validation import (
    RESTORATION_VALIDATION_NODES,
    SNAPSHOT_NODE,
    WAIT_NODE,
    build_restoration_validation_graph,
    fix_action_type,
    fix_completed_at,
    window_deadline,
)
from lpr_cpe.policies.loader import load_pack

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: Plant fault, so D08 diverts it and the parent stops at P11 having executed nothing. See the
#: module docstring for why the plainest service is the right one here.
PLANT_SERVICE = "SVC-UT-001-A-03"


class _Ticking(FrozenClock):
    """The advance-on-read clock the other graph-running modules use.

    Subclassed off `FrozenClock` so `local_now()` and `timezone` stay the production ones;
    `test_builder.py` records what the hand-rolled version failed to satisfy.
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
            summary=f"degraded optical on {service['service_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


def _repair(incident_id: str, *, finished: datetime, action: ActionType) -> ActionRecord:
    """One completed repair, which is the only thing `fix_completed_at` needs to find."""
    return ActionRecord(
        action_id="ACT-STAGE5-REPAIR",
        incident_id=incident_id,
        action_type=action,
        target_ref="ONT-UT-001-A-03",
        idempotency_key="IDKEY-STAGE5-REPAIR",
        outcome=ActionOutcome.SUCCEEDED,
        started_at=finished - timedelta(minutes=2),
        completed_at=finished,
        actor="test",
        reason_code=ReasonCode.REMOTE_FIX_APPLIED,
        correlation_id=f"COR-{incident_id}",
    )


@pytest.fixture
async def diagnosed(fixtures: Any) -> Any:
    """The parent run to P11, returned as a plain state for this subgraph to be handed.

    `interrupt_after` rather than a shortened graph: the eleven nodes that produced this state are
    the production ones, and the evidence `_sources_read_before` will read back is the evidence P07
    actually gathered.
    """
    service = fixtures.services[PLANT_SERVICE]
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    state = await parent.ainvoke(
        _initial(service), context=ctx, config={"configurable": {"thread_id": "parent-stage5"}}
    )
    return state, ctx


def _with_repair(
    state: dict[str, Any], *, minutes_ago: float, action: ActionType
) -> dict[str, Any]:
    return {
        **state,
        "action_history": [
            _repair(
                state["incident_id"],
                finished=NOW - timedelta(minutes=minutes_ago),
                action=action,
            )
        ],
    }


async def _run(state: dict[str, Any], ctx: Any, *, thread: str) -> Any:
    graph = build_restoration_validation_graph().compile(
        name="lpr_cpe_restoration_validation", checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": thread}}
    return graph, config, await graph.ainvoke(state, context=ctx, config=config)


# ------------------------------------------------------------------------------------------------
# The deadline the wait is measured against
# ------------------------------------------------------------------------------------------------


def test_the_deadline_is_the_repair_plus_the_window_that_repair_earns() -> None:
    """`window_deadline` composes the two helpers `assess_restoration` scores from, and no others.

    Both halves are asserted because both can be wrong in a way that still returns a plausible
    instant. The start is `fix_completed_at`, so a window measured from the incident rather than
    from the fix would be long expired before Stage 5 was reached. The length is keyed on
    `fix_action_type`, which reads `action_history` only -- a completed work order moves the start
    without naming an action, and `stability_window` reads that `None` as the plant case.

    The two numbers are the pack's, read from it rather than written here, because a test that
    hard-coded 30 and 60 would keep passing after the pack changed and would then be asserting
    yesterday's policy.
    """
    policy = load_pack().validation
    finished = NOW - timedelta(minutes=5)
    state: Any = {
        "action_history": [_repair("INC-W", finished=finished, action=ActionType.CPE_REBOOT)]
    }

    assert fix_completed_at(state) == finished
    assert fix_action_type(state) is ActionType.CPE_REBOOT
    assert window_deadline(state, policy) == finished + timedelta(
        minutes=policy.stability_window_minutes
    )


def test_no_recorded_repair_gives_no_deadline_rather_than_an_expired_one() -> None:
    """`None`, and it has to be `None` rather than "now" or "the incident's start".

    Those are the two defaults that look harmless and are not, and they fail in opposite
    directions -- `fix_completed_at` sets out both. What matters here is only that the absence
    survives the composition instead of being resolved into an instant by it, because
    `await_service_stability` distinguishes the two cases and cannot if this helper does not.
    """
    policy = load_pack().validation
    assert window_deadline({}, policy) is None  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------
# The wait
# ------------------------------------------------------------------------------------------------


async def test_a_running_window_pauses_the_stage_before_anything_is_read(diagnosed: Any) -> None:
    """The specification forbids sleeping through the window; this is what it does instead.

    Two claims, and the second is the one worth having. The pause is easy to see -- an
    `__interrupt__` carrying the deadline a scheduler is meant to wake the incident at. The claim
    underneath it is that **nothing was read**: no snapshot evidence, no verdict, and the status is
    still whatever P11 left. A wait that let the read happen first would spend every adapter call in
    the stage on a window that had not run.
    """
    state, ctx = diagnosed
    _, _, out = await _run(
        _with_repair(state, minutes_ago=5, action=ActionType.CPE_REBOOT), ctx, thread="running"
    )

    (pause,) = out["__interrupt__"]
    payload = pause.value["stability_window_wait"]
    assert payload["incident_id"] == state["incident_id"]
    assert payload["resume_at"] is not None
    assert payload["reason"] == "the stability window is still running"

    assert out.get("validation") is None
    assert out["status"] is not IncidentStatus.VALIDATING
    assert SNAPSHOT_NODE not in out["node_visits"]


async def test_an_elapsed_window_runs_the_stage_straight_through(diagnosed: Any) -> None:
    """Past the deadline the wait is not a wait, and all three nodes run in one pass.

    The audit event is asserted as well as the absence of a pause, because those are two different
    facts: `outcome="window_elapsed"` with `paused: False` is the node saying it never raised, and a
    run that paused and was resumed within the same invocation would also arrive here with a
    verdict.

    The verdict itself is asserted only as "a record exists". Whether it *passes* is
    `decision_services.restoration`'s judgement and is tested there against constructed findings;
    re-deriving it from a fixture would make this test fail whenever the simulator's readings
    changed, for reasons having nothing to do with the wait.
    """
    state, ctx = diagnosed
    _, _, out = await _run(
        _with_repair(state, minutes_ago=90, action=ActionType.CPE_REBOOT), ctx, thread="elapsed"
    )

    assert "__interrupt__" not in out
    assert out["status"] is IncidentStatus.VALIDATING
    assert out["validation"] is not None
    assert out["node_visits"][WAIT_NODE] == 1
    assert out["node_visits"][SNAPSHOT_NODE] == 1

    (waited,) = [event for event in out["audit_events"] if event.node == WAIT_NODE]
    assert waited.outcome == "window_elapsed"
    assert waited.detail["paused"] is False


async def test_an_unrecorded_repair_parks_rather_than_passing_through(diagnosed: Any) -> None:
    """No fix on the record is a pause with no deadline, and the payload says so in words.

    The alternative -- treating "nothing to measure" as "nothing to wait for" -- is what makes this
    worth a test of its own rather than a branch of the one above. It would put D21's observe loop
    into a spin that re-reads every adapter on each lap and stops only when the loop guard escalates
    six laps later. `resume_at: None` is what tells a scheduler it has no timer to set and a human
    is what this needs.
    """
    state, ctx = diagnosed
    _, _, out = await _run(state, ctx, thread="unrecorded")

    (pause,) = out["__interrupt__"]
    payload = pause.value["stability_window_wait"]
    assert payload["resume_at"] is None
    assert payload["reason"] == ("no completed repair is recorded, so no window can be measured")
    assert SNAPSHOT_NODE not in out["node_visits"]


async def test_a_wake_that_arrives_before_the_deadline_is_refused(diagnosed: Any) -> None:
    """The clock outranks the scheduler, and this is the test that found out it did not.

    Written to assert that early wakes cannot manufacture post-fix samples, it failed on a stronger
    fault than the one it was aimed at. The node paused once and then took *any* resume as the
    window having run: two early wakes and the whole stage had read the service and scored a
    verdict, 24 minutes early::

        E       AssertionError: the window is still running, so it must still be waiting
        E       assert '__interrupt__' in {'action_history': [ActionRecord(action_id=...

    Nothing closed wrongly -- the verdict came back `STABILITY_WINDOW_PENDING`, which is right -- so
    the damage was one edge further on, where D21 reads pending as `continue_observation` and
    re-enters against a budget of 6. `await_service_stability` records what that cost.

    Seen red again after the fix, by narrowing that node's `while` back to an `if`, which is the
    single character the whole guard turns on. One failure, this test, on the first resume::

        E           AssertionError: the window is still running, so it must still be waiting
        E           assert '__interrupt__' in {'action_history': [ActionRecord(...

    The other seven in this module stayed green, including the two that assert the stage *does* run
    -- so what this pins is the refusal alone and not the wait in general.

    Three resumes rather than one, because one would pass against a node that alternated. The
    evidence count is asserted too, and is the original point: `post_fix_looks` counts
    `EvidenceKind.CPE_STATUS` items, `min_post_fix_samples` is 3, and a wait that re-read the
    service on each wake would have satisfied the pack without the service having been watched.
    """
    state, ctx = diagnosed
    started = sum(1 for item in state.get("evidence", []) if item.kind is EvidenceKind.CPE_STATUS)

    graph, config, out = await _run(
        _with_repair(state, minutes_ago=5, action=ActionType.CPE_REBOOT), ctx, thread="woken"
    )
    for _ in range(3):
        out = await graph.ainvoke(
            Command(resume={"woken_by": "scheduler"}), context=ctx, config=config
        )
        assert "__interrupt__" in out, "the window is still running, so it must still be waiting"

    (pause,) = out["__interrupt__"]
    assert pause.value["stability_window_wait"]["reason"] == "the stability window is still running"

    #: `WAIT_NODE` absent rather than at 1: `@node` bumps the visit *after* the body returns, and a
    #: body parked in `interrupt()` has not returned. So a refused wake costs nothing at all -- not
    #: a re-entry, not an adapter call, not a visit -- which is the whole of the fix. The eleven
    #: entries that *are* here are the parent's, carried in on the seeded state.
    assert WAIT_NODE not in out["node_visits"]
    assert SNAPSHOT_NODE not in out["node_visits"]
    assert out.get("validation") is None
    assert (
        sum(1 for item in out.get("evidence", []) if item.kind is EvidenceKind.CPE_STATUS)
        == started
    )


async def test_a_wake_that_arrives_after_the_deadline_is_taken(diagnosed: Any) -> None:
    """The other half of the same claim: the loop lets go, and says it waited.

    Without this, `await_service_stability` could pass the test above by never resuming at all,
    which is a worse fault than the one being guarded. So the window is allowed to run out
    underneath a parked incident and the run is required to finish.

    It runs out by itself. The repair is dated so that the deadline falls half a minute ahead of
    the clock, and `_Ticking` moves three seconds per read -- so the wakes are refused until the
    reading passes the deadline and then one is taken. Re-dating the record between wakes was the
    obvious alternative and does not work: `action_history` reduces with `append_unique`, first
    write of an `action_id` wins, and `fix_completed_at` takes the `max` of what survives, so a
    window can be pushed back but never brought forward.

    Dated off `ctx.clock.now()` rather than off `NOW`, which the other tests here can use because
    they only need the deadline on one side or the other by minutes. This one needs it within
    seconds, and the fixture's clock is no longer at `NOW`: the parent run to P11 reads it about a
    hundred times on the way, measured at five and a half minutes of drift. Anchoring to `NOW`
    made the window already elapsed and the stage ran straight through without ever pausing.

    `outcome="resumed"` with `paused: True` is the pair that separates this from
    `test_an_elapsed_window_runs_the_stage_straight_through`: same completed stage, but that one
    never interrupted and this one was released from an interrupt.
    """
    state, ctx = diagnosed
    window = timedelta(minutes=load_pack().validation.stability_window_minutes)
    seeded = {
        **state,
        "action_history": [
            _repair(
                state["incident_id"],
                finished=ctx.clock.now() + timedelta(seconds=30) - window,
                action=ActionType.CPE_REBOOT,
            )
        ],
    }

    graph, config, out = await _run(seeded, ctx, thread="released")
    assert "__interrupt__" in out

    resumes = 0
    while "__interrupt__" in out and resumes < 20:
        out = await graph.ainvoke(
            Command(resume={"woken_by": "scheduler"}), context=ctx, config=config
        )
        resumes += 1

    assert "__interrupt__" not in out, f"still parked after {resumes} resumes"
    assert out["validation"] is not None
    assert out["node_visits"][WAIT_NODE] == 1
    assert out["node_visits"][SNAPSHOT_NODE] == 1

    (waited,) = [event for event in out["audit_events"] if event.node == WAIT_NODE]
    assert waited.outcome == "resumed"
    assert waited.detail["paused"] is True


# ------------------------------------------------------------------------------------------------
# What goes into the comparison
# ------------------------------------------------------------------------------------------------


def _finding(name: str, score: float) -> AnomalyFinding:
    return AnomalyFinding(
        detector_name=name,
        detector_version="1.0.0",
        observed_at=NOW,
        score=score,
        confidence=0.9,
        severity=Severity.HIGH,
        explanation=f"{name} scored {score}",
    )


#: One real detector and one derived one, on both sides. The derived detector's score *rises* across
#: the repair, which is the case the exclusion exists for: `no_fault_found_risk` scores highest
#: exactly when there is no fault left to find.
_STUB_RESULTS = [
    DetectorResult(
        detector_name="hfc_rf_pnm_degradation",
        detector_version="1.0.0",
        ran=True,
        findings=[_finding("hfc_rf_pnm_degradation", 0.05)],
    ),
    DetectorResult(
        detector_name="no_fault_found_risk",
        detector_version="1.0.0",
        ran=True,
        findings=[_finding("no_fault_found_risk", 0.95)],
        derived=True,
    ),
]


async def test_derived_detectors_are_dropped_from_both_sides_of_the_comparison(
    diagnosed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding that restates the other findings is not a reading of the service, on either side.

    `run_detectors` is stubbed rather than driven off a fixture, because the whole point is what
    `assess_restoration` does with a *known* pair of results, and a fixture that happened to leave
    every derived detector silent would let the filter be deleted without this going red.

    The two sides fail differently when the filter is removed, so both are asserted. The before-side
    is seeded with one finding per detector and `findings_before` counts what survives
    `_comparable_before`; the after-side is the stub's own two findings and `findings_after` counts
    what survives the comprehension. Both were measured red separately -- passing `ran_after` rather
    than `measured_after` to `_comparable_before`::

        assert judged.detail["findings_before"] == 1
        E       assert 2 == 1

    and dropping the `DERIVED_DETECTORS` clause from `findings_after`::

        assert judged.detail["findings_after"] == 1
        E       assert 2 == 1

    `detectors_ran_after` keeps both names while `detectors_excluded_as_derived` names the dropped
    one: a trail that narrowed the first would report the derived detector as never having looked.
    """
    state, ctx = diagnosed

    async def _stub(_context: Any) -> list[DetectorResult]:
        return _STUB_RESULTS

    monkeypatch.setattr("lpr_cpe.graph.subgraphs.restoration_validation.run_detectors", _stub)
    seeded = {
        **_with_repair(state, minutes_ago=90, action=ActionType.CPE_REBOOT),
        "anomaly_findings": [
            _finding("hfc_rf_pnm_degradation", 1.0),
            _finding("no_fault_found_risk", 1.0),
        ],
    }

    _, _, out = await _run(seeded, ctx, thread="derived-excluded")

    (judged,) = [event for event in out["audit_events"] if event.node == "assess_restoration"]
    assert judged.detail["detectors_ran_after"] == [
        "hfc_rf_pnm_degradation",
        "no_fault_found_risk",
    ]
    assert judged.detail["detectors_excluded_as_derived"] == ["no_fault_found_risk"]
    assert judged.detail["findings_before"] == 1
    assert judged.detail["findings_after"] == 1


# ------------------------------------------------------------------------------------------------
# The shape
# ------------------------------------------------------------------------------------------------


def test_the_stage_is_three_nodes_in_one_line_with_no_loop_back_to_the_wait() -> None:
    """Wait, read, judge -- and every edge guarded except the last, which has nowhere else to go.

    Read off the `StateGraph` rather than off the module's own registry, so this says "LangGraph was
    given this" and not "the tuple equals itself".

    The absence of a loop is the assertion worth reading. D21 owns the observe-again loop from the
    parent's edge, because two of its three answers leave this graph entirely; a second loop drawn
    here would give the incident two places to be waiting and only the parent's would show in the
    trace an operator reads.
    """
    graph = build_restoration_validation_graph()

    assert [name for name, _ in RESTORATION_VALIDATION_NODES] == [
        WAIT_NODE,
        SNAPSHOT_NODE,
        "assess_restoration",
    ]
    assert sorted(graph.edges) == [
        (START, WAIT_NODE),
        ("assess_restoration", END),
    ]
    ends = {
        source: dict(branch.ends or {})
        for source, branches in graph.branches.items()
        for branch in branches.values()
    }
    assert ends == {
        WAIT_NODE: {ONWARD: SNAPSHOT_NODE, ESCALATED: END},
        SNAPSHOT_NODE: {ONWARD: "assess_restoration", ESCALATED: END},
    }
