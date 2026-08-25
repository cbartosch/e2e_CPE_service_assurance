"""Stage 5's closure half, compiled and run: the six reads, the retry ceiling, and the two codes.

The incident is carried through the real parent to P11 and then handed to this subgraph directly,
which is `test_subgraph_restoration_validation.py`'s construction and is here for a sharper version
of its reason. **No fixture reaches this stage.** Swept over all 41 fixture services under three
case profiles -- proactive/high, customer-reported/medium, customer-reported/low -- 20 stop at
`validating`, 20 at `diagnosing`, one escalates, and none arrive: the simulator derives telemetry
from each service's static `health` field, so no repair this workflow performs changes a reading,
`assess_restoration` scores every fix as having cleared 0% of the anomaly, and D21 answers
`retry_diagnosis` until the resolution budget escalates. So what is seeded is the one thing no
fixture can produce -- P22's `ValidationResult` -- and everything else on the state is the parent's.

Two services, and the difference between them is the policy engine's, not this module's
--------------------------------------------------------------------------------------
`evaluate_closure_policy` passes `rca.confidence` to the engine, and the shipped pack's
`rca.min_for_closure` is 0.75. Measured at P11: `SVC-UT-001-A-01` arrives with 0.95 and
`SVC-UT-001-A-03` with 0.2891. That single number is what separates the three closure outcomes, so
each test below uses the service whose *own* RCA produces the decision it is about:

| service | rca | validation | engine says | this stage does |
| --- | --- | --- | --- | --- |
| `SVC-UT-001-A-01` | 0.95 | passed | `allowed` | closes `CLOSED_NORMAL` |
| `SVC-UT-001-A-03` | 0.29 | failed | `requires_approval` `exceptional_closure` | asks, then closes |
| `SVC-UT-001-A-03` | 0.29 | passed | `requires_approval` `low_confidence_rca` | abandons |

The third row is the one worth reading twice. It is not a contrived input: it is what the shipped
pack says about a real fixture, and it is the case `route_closure_gate`'s docstring calls "an
unanswerable demand". A stubbed `PolicyDecision` would have let this module assert whatever the
gate happened to do.

What is deliberately not asserted here
--------------------------------------
D23 and D24 are not tested as routers. Both live in `graph.routing`, both are asked from this
graph's edges, and `test_routing.py` has them against constructed state; what is asserted here is
where each answer *lands*. The parent's view of this subgraph is `test_builder.py`'s -- including
the `STAGE_TRANSITIONS` seam, which this stage does not cross.

Nothing asserts a KPI's *value*. `KPICalculator` owns those and `test_persistence.py` measures
them; re-deriving one here would make this module fail whenever a rate's definition moved, for
reasons having nothing to do with closure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain.closure import ValidationResult
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    CaseType,
    CrewType,
    EventSource,
    FaultDomain,
    IncidentStatus,
    MRStatus,
    ReasonCode,
    Severity,
    Technology,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import MRRecord, WorkOrder
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, ONWARD
from lpr_cpe.graph.routing import PRIOR_INCIDENTS_KEY, latest_policy_decision
from lpr_cpe.graph.state import make_initial_state, truck_roll_count
from lpr_cpe.graph.subgraphs.reconciliation_closure import (
    GATE_TARGETS,
    RECONCILERS,
    RECONCILIATION_CLOSURE_NODES,
    RETRY_KEY,
    SERVICE_PROBLEM_KEY,
    build_reconciliation_closure_graph,
    reconcile_communications,
    reconcile_jtrack,
    reconcile_nxt,
    reconcile_tmf,
    reconcile_wfm,
    route_closure_gate,
)
from lpr_cpe.policies.engine import PolicyEngine
from lpr_cpe.policies.loader import load_pack
from lpr_cpe.policies.models import PolicyPack
from lpr_cpe.simulation.loader import build_simulated_adapters

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: RCA 0.95 at P11, which is over the pack's `rca.min_for_closure` of 0.75. The only service class
#: from which a `CLOSED_NORMAL` is reachable at all -- 13 of the 41 fixtures clear the bar and all
#: 13 are `*_healthy`.
CONFIDENT = "SVC-UT-001-A-01"

#: RCA 0.2891 at P11. Under the bar, so the engine demands an approval for every closure, and it is
#: `exceptional_closure` whether the validation passed or failed -- only the reason codes differ, and
#: with them the question. See the table in the module docstring.
UNCONFIDENT = "SVC-UT-001-A-03"

APPROVE = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "the records agree and the customer is content; close it",
}
REJECT = {**APPROVE, "status": "rejected", "rationale": "the service is not restored; do not close"}


class _Ticking(FrozenClock):
    """The advance-on-read clock the other graph-running modules use."""

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


async def _to_p11(fixtures: Any, ref: str, *, thread: str, adapters: Any = None) -> Any:
    """One real parent run, stopped after P11. Returns the state and the context that produced it.

    `adapters` is threaded through rather than left to `build_context`, which builds a fresh
    simulator per call: the two tests that reach into the adapter set -- to take TMF down, and to
    count what was written to it -- need the same instance the graph is using.
    """
    ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    state = await parent.ainvoke(
        _initial(fixtures.services[ref]),
        context=ctx,
        config={"configurable": {"thread_id": thread}},
    )
    return state, ctx


@pytest.fixture
async def confident(fixtures: Any) -> Any:
    return await _to_p11(fixtures, CONFIDENT, thread="p11-confident")


@pytest.fixture
async def unconfident(fixtures: Any) -> Any:
    return await _to_p11(fixtures, UNCONFIDENT, thread="p11-unconfident")


def _validated(state: dict[str, Any], *, passed: bool, **extra: Any) -> dict[str, Any]:
    """P22's output, seeded, plus the status D22 would have handed this stage.

    `validating` is not decoration. `prepare_exceptional_closure_approval` writes
    `awaiting_approval`, and `domain.lifecycle` does not permit that from `validating` -- so the run
    only survives its own gate because P24 writes `reconciling` on its first line. Seeding any other
    entry status would test a graph the parent cannot produce.
    """
    incident_id = str(extra.get("incident_id") or state["incident_id"])
    return {
        **state,
        **extra,
        "status": IncidentStatus.VALIDATING,
        "validation": ValidationResult(
            validation_id=f"VAL-{incident_id}",
            incident_id=incident_id,
            validated_at=NOW,
            window_start=NOW - timedelta(minutes=30),
            stability_window=timedelta(minutes=30),
            samples_in_window=3 if passed else 0,
            passed=passed,
            reason_code=ReasonCode.VALIDATED_STABLE if passed else ReasonCode.VALIDATION_FAILED,
            summary=(
                "the service held across the whole window"
                if passed
                else "the service is still degraded after the repair"
            ),
        ),
    }


async def _run(state: dict[str, Any], ctx: Any, *, thread: str) -> Any:
    graph = build_reconciliation_closure_graph().compile(
        name="lpr_cpe_reconciliation_closure", checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": thread}}
    return graph, config, await graph.ainvoke(state, context=ctx, config=config)


def _audit(out: dict[str, Any], node: str) -> list[Any]:
    return [event for event in out["audit_events"] if event.node == node]


# ------------------------------------------------------------------------------------------------
# P24: which systems get asked
# ------------------------------------------------------------------------------------------------


def test_the_pack_names_only_systems_this_stage_can_actually_read(adapters: Any) -> None:
    """The guard on the defect that shipped: a pack entry no adapter serves, closing nothing.

    `ReconciliationPolicy.systems` read `tmf/wfm/jtrack/inventory/service_platform` until
    2026-08-19. `service_platform` is not a member of `SimulatedAdapters` and has no entry in
    `RECONCILERS`, so `_reads_for` could only ever put it in `systems_unreachable` --  and
    `ReconciliationResult.consistent` counts an unreachable system as inconsistent, so D23 answered
    `reconcile_retry` on every incident, three times, and then escalated it. Every closure in the
    system was unreachable and the graph was structurally fine.

    Both halves are checked because a name can be wrong in two ways. Absent from `RECONCILERS` means
    nothing here knows how to read the payload; absent from `all_adapters()` means there is nobody to
    ask. `service_platform` was both at once.

    The pack is loaded rather than described, so this fails when the *pack* changes rather than when
    someone edits a list in this file to match.

    Reinstated by putting `service_platform` back in `pack.yaml`:
    `AssertionError: policy.reconciliation.systems names systems this stage cannot read:
    ['service_platform: no entry in RECONCILERS', 'service_platform: no adapter in
    all_adapters()']`. Seven of this module's ten tests fell over on that one line of YAML, which is
    the measurement that makes the case for this test being cheap and first: every closure path
    ended in the retry loop, and the six that are not about reconciliation at all failed with
    `KeyError: 'record_chronic_pattern'` and friends -- symptoms three nodes downstream of a name.
    """
    named = load_pack().reconciliation.systems
    servable = adapters.all_adapters()

    offenders: list[str] = []
    for name in named:
        if name not in RECONCILERS:
            offenders.append(f"{name}: no entry in RECONCILERS")
        if name not in servable:
            offenders.append(f"{name}: no adapter in all_adapters()")
    assert not offenders, (
        f"policy.reconciliation.systems names systems this stage cannot read: {offenders}"
    )

    # Not the inverse -- `RECONCILERS` may know how to read a system the pack has stopped asking
    # about, and that is a dormant reader rather than a defect. What must not happen is the pack
    # asking for something nobody can answer.
    assert named, "an empty systems list would make every reconciliation vacuously consistent"


def test_what_p24_refuses_to_call_a_disagreement(now: datetime) -> None:
    """Three payloads that look like trouble and are not, each for its own recorded reason.

    Every one of these would, if counted, hold an incident through three retries and then escalate
    it -- which is the `service_platform` failure arriving by a different door. They are asserted
    together because they are one claim: a reader may only report a disagreement it can name.

    * **A live NXT alarm.** The simulator derives alarms from the fixture's static `health`, so no
      repair clears one; 8 of the 41 services carry an uncleared alarm permanently. The count is a
      note.
    * **A record the WFM does not hold**, when the WFM itself says the answer is simulated. Its own
      docstring: "a miss after a restart is expected and is **not** evidence that WFM and the
      workflow disagree."
    * **A customer nobody asked.** `customer_verdict` returns `None` for no replies, and `is False`
      rather than falsiness is what keeps that out of the mismatch list -- every proactive incident
      would otherwise be inconsistent.

    Reinstated by dropping the `_unfound` clause from `reconcile_wfm`:
    `AssertionError: these are notes, not disagreements: ["wfm did not hold WO-1: [{'system':
    'wfm', 'record': 'work_order', 'ours': 'completed', 'theirs': '', 'detail': 'work order WO-1 is
    finished here and still  in the WFM'}]"]`. The empty `theirs` is the giveaway -- the node was
    comparing against a record it had just been told does not exist.
    """
    order = WorkOrder(
        work_order_id="WO-1",
        incident_id="INC-1",
        crew_type=CrewType.DIRTY,
        status=WorkOrderStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    state: Any = {"work_orders": [order]}

    alarm_mismatches, alarm_notes = reconcile_nxt(state, [{"alarm_id": "A-1", "cleared_at": None}])
    unfound_mismatches, unfound_notes = reconcile_wfm(
        state, {"work_order_ref": "WO-1", "found": False, "simulated": True}
    )
    silent_mismatches, _ = reconcile_communications(state, [])

    offenders = [
        f"{label}: {found}"
        for label, found in (
            ("an uncleared alarm", alarm_mismatches),
            ("wfm did not hold WO-1", unfound_mismatches),
            ("nobody replied", silent_mismatches),
        )
        if found
    ]
    assert not offenders, f"these are notes, not disagreements: {offenders}"

    # Silence is not the same as saying nothing happened: each one leaves the operator a line.
    assert "1 uncleared alarm(s) of 1" in alarm_notes[0]
    assert "not treated as a disagreement" in unfound_notes[0]


def test_a_finished_work_order_the_wfm_still_has_open_is_a_disagreement(now: datetime) -> None:
    """The positive half, without which the test above passes on a reader that reports nothing.

    This is the failure closure exists to prevent, and it is the one direction worth asserting: a
    live work order behind a closed incident sends a crew to a fault nobody is expecting them at.
    The inverse cannot arise -- this workflow is the only writer of `work_orders`.

    The two payloads differ in one key, `simulated`, and that is the whole of `_unfound`'s
    conservatism: a *real* system reporting a record missing is a genuine mismatch, and the clause
    stops applying the moment the adapter behind it is not a fixture.

    Reinstated by widening `_unfound` to `payload.get("found") is False`, dropping the `simulated`
    half: `AssertionError: a real WFM saying the record is gone is a disagreement / assert []` --
    the two payloads then produce identical output and the reader has stopped distinguishing a
    fixture's amnesia from a live system's.
    """
    order = WorkOrder(
        work_order_id="WO-1",
        incident_id="INC-1",
        crew_type=CrewType.DIRTY,
        status=WorkOrderStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    state: Any = {"work_orders": [order]}

    (still_open,), _ = reconcile_wfm(state, {"work_order_ref": "WO-1", "status": "dispatched"})
    assert still_open["system"] == "wfm"
    assert still_open["ours"] == WorkOrderStatus.COMPLETED.value
    assert still_open["theirs"] == "dispatched"

    real_miss, _ = reconcile_wfm(
        state, {"work_order_ref": "WO-1", "found": False, "simulated": False}
    )
    assert real_miss, "a real WFM saying the record is gone is a disagreement"

    (denial,), _ = reconcile_communications(state, [{"response": "still_broken"}])
    assert denial["system"] == "communications"


# ------------------------------------------------------------------------------------------------
# The five readers the 2026-08-24 sweep found nothing holding
# ------------------------------------------------------------------------------------------------
#
# Every test in this block closes a mutation that survived both this module's own tests and the
# whole suite. None of them is a boundary quibble: each is a reader deciding whether to hold an
# incident open, and the mutation that survived made it say no.


def test_the_gate_will_not_close_an_incident_the_engine_never_evaluated() -> None:
    """No `CLOSE_INCIDENT` decision at all must abandon, exactly as a blocked one does.

    The severest survivor of the sweep. `route_closure_gate` opens
    `if decision is None or decision.blocked: return "abandon"`, and rewriting that to
    `if decision is not None and decision.blocked` — so a *missing* decision falls through to the
    `is not REQUIRES_APPROVAL` clause and answers `close` — passed every test in this repository.
    An incident would be closed, a `ClosureRecord` written and `IncidentStatus.CLOSED` set, on an
    action the policy engine had never been asked about.

    It survived because nothing reached the gate without a decision: `evaluate_closure_policy` runs
    immediately before it on every wired path and always records one. That makes this a guard on a
    state the graph cannot currently produce — which is exactly the kind the sweep is for, because
    the clause is load-bearing the moment anything else routes here, and until then nothing else
    can tell whether it works.

    Watched red under that mutation::

        AssertionError: an incident with no CLOSE_INCIDENT decision was closed
        assert 'close' == 'abandon'
    """
    assert route_closure_gate({}) == "abandon", (  # type: ignore[arg-type]
        "an incident with no CLOSE_INCIDENT decision was closed"
    )


def test_only_the_two_terminal_wfm_statuses_excuse_a_finished_work_order(now: datetime) -> None:
    """Which WFM statuses count as agreement, asserted as a set rather than by one example.

    `reconcile_wfm` excuses `{"cancelled", "completed"}` and reports everything else as a
    disagreement. The existing positive test uses `dispatched`, so **adding a third member to that
    set changes nothing it asserts** — and the sweep's mutation added `in_progress`, which is the
    one status a real WFM is most likely to be sitting in when a crew is still on site. The failure
    that hides is the one this module exists to prevent, in the module's own words: a live work
    order behind a closed incident sends a crew to a fault nobody is expecting them at.

    Driven over the whole vocabulary rather than the one extra member, so a fourth status added to
    the WFM cannot arrive unclassified.

    Watched red by adding `in_progress` to the excused set::

        AssertionError: 'in_progress' in the WFM behind a finished work order is a disagreement
    """
    order = WorkOrder(
        work_order_id="WO-1",
        incident_id="INC-1",
        crew_type=CrewType.DIRTY,
        status=WorkOrderStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    state: Any = {"work_orders": [order]}

    excused = {"cancelled", "completed"}
    for status in ("requested", "dispatched", "in_progress", "on_site", "completed", "cancelled"):
        mismatches, _ = reconcile_wfm(state, {"work_order_ref": "WO-1", "status": status})
        if status in excused:
            assert not mismatches, f"{status!r} is terminal in the WFM and agrees with us"
        else:
            assert mismatches, (
                f"{status!r} in the WFM behind a finished work order is a disagreement"
            )


def test_an_mr_finished_here_and_open_in_jtrack_is_a_disagreement(now: datetime) -> None:
    """`reconcile_jtrack` had no test of any kind, and its mismatch clause could be deleted whole.

    The sweep replaced `if ours.terminal and bool(payload.get("open")):` with `if False:` and
    nothing anywhere went red. That clause is the jTrack half of the same failure the WFM half is
    written for, and gap EXEC-2 records it as a live condition rather than a hypothetical: two of
    the 41 fixture runs escalate on exactly this mismatch, so the branch is reached in a sweep and
    was still held to account by nothing in the committed suite.

    All four of the function's answers are driven, because the mismatch is only meaningful against
    the three cases that must *not* produce one.

    Watched red by neutering the clause::

        AssertionError: an MR finished here and open in jTrack is a disagreement
    """
    record = MRRecord(
        mr_id="MR-1",
        incident_id="INC-1",
        external_ref="JT-1",
        plant_object_ref="ODP-1",
        fault_domain=FaultDomain.DISTRIBUTION,
        status=MRStatus.CLOSED,
        created_at=now,
        updated_at=now,
    )
    state: Any = {"mr_records": [record]}

    (open_there,), _ = reconcile_jtrack(
        state, {"mr_ref": "JT-1", "status": "in_progress", "open": True}
    )
    assert open_there["system"] == "jtrack"
    assert open_there["ours"] == MRStatus.CLOSED.value
    assert open_there["theirs"] == "in_progress", (
        "an MR finished here and open in jTrack is a disagreement"
    )

    agreed, notes = reconcile_jtrack(state, {"mr_ref": "JT-1", "status": "closed", "open": False})
    assert not agreed and notes, "a closed MR on both sides is a note"

    unknown, unknown_notes = reconcile_jtrack(state, {"mr_ref": "JT-9", "status": "open"})
    assert not unknown, "an MR jTrack holds and we do not is a note, not a disagreement"
    assert "unknown here" in unknown_notes[0]

    missing, missing_notes = reconcile_jtrack(
        state, {"mr_ref": "JT-1", "found": False, "simulated": True}
    )
    assert not missing, "a simulator that has forgotten the MR is not a disagreement"
    assert "not treated as a disagreement" in missing_notes[0]


def test_a_service_record_that_could_not_be_read_is_a_disagreement() -> None:
    """An unreadable TMF record must hold the closure, not pass it.

    `reconcile_tmf`'s `data_available is False` arm could be removed whole without failing anything.
    What it guards is the difference between "we checked the customer's service record and it
    matches" and "we could not read it" — and closing on the second while reporting the first is a
    closure with no customer-facing check behind it at all.

    The `no record returned` arm is asserted beside it because the two are one claim: an answer this
    reader cannot use is a reason to keep looking, whatever shape the non-answer arrived in.

    Watched red by neutering the `data_available` clause::

        AssertionError: a service record that could not be read is not agreement
    """
    state: Any = {"service_ref": "SVC-1", "customer_ref": "CUST-1"}

    unreadable, _ = reconcile_tmf(
        state,
        {"service_ref": "SVC-1", "data_available": False, "data_quality_notes": ["upstream 503"]},
    )
    assert unreadable, "a service record that could not be read is not agreement"
    assert "upstream 503" in unreadable[0]["detail"], "the reason has to reach the operator"

    absent, _ = reconcile_tmf(state, None)
    assert absent, "no record at all is not agreement either"

    wrong_customer, _ = reconcile_tmf(state, {"service_ref": "SVC-1", "customer_ref": "CUST-2"})
    assert wrong_customer and wrong_customer[0]["theirs"] == "CUST-2"

    agreed, notes = reconcile_tmf(
        state, {"service_ref": "SVC-1", "customer_ref": "CUST-1", "state": "active"}
    )
    assert not agreed and notes, "the same customer on both sides is a note"


def test_a_cleared_alarm_is_not_counted_as_a_live_one() -> None:
    """The alarm count an operator reads must be of alarms that are still up.

    `reconcile_nxt` produces no mismatch by design, so the *only* thing it contributes is that
    sentence — and dropping `row.get("cleared_at") is None` from the filter left every test green
    while turning "1 uncleared alarm(s) of 3" into "3 uncleared alarm(s) of 3". A note nobody can
    trust is worse than no note, because this one is the reason the reader refuses to call an alarm
    a disagreement at all.

    Watched red by dropping the filter::

        AssertionError: assert '1 uncleared alarm(s) of 3' in 'nxt: 3 uncleared alarm(s) of 3 ...'
    """
    rows = [
        {"alarm_id": "A-1", "cleared_at": None},
        {"alarm_id": "A-2", "cleared_at": "2026-03-02T14:00:00+00:00"},
        {"alarm_id": "A-3", "cleared_at": "2026-03-02T14:05:00+00:00"},
    ]
    mismatches, notes = reconcile_nxt({}, rows)  # type: ignore[arg-type]
    assert not mismatches, "an alarm is cleared by the network, not by us"
    assert "1 uncleared alarm(s) of 3" in notes[0]


async def test_the_closure_record_counts_the_trucks_the_incident_actually_used(
    confident: Any,
) -> None:
    """`ClosureRecord.truck_rolls` is the incident's own count, and no fixture could show it.

    The sweep replaced `truck_rolls=truck_roll_count(state)` with `truck_rolls=0` and nothing went
    red. That is not a weak assertion — it is an **equivalent mutant over every reachable state**,
    and measuring why is the useful part: only one of the 41 fixture services reaches `closed` at
    all, `SVC-UT-001-B-01`, and it gets there on the remote path with no work order ever booked. So
    `truck_roll_count` is 0 on every closure this system can currently produce, and `0` and the real
    count are the same number.

    An equivalent mutant is normally left alone. This one is not, because the equivalence is a
    property of the *fixtures* rather than of the code — the field-path incidents all escalate today
    (gap EXEC-1), and the day one of them closes this field starts carrying a number somebody reads.
    So the truck roll is seeded rather than waited for.

    `en_route` is the weakest status that counts: `WorkOrder.counted_as_truck_roll` deliberately
    excludes `requested`, because a booking is not a visit. Seeding the weakest one means the test
    fails if that boundary is ever widened *or* narrowed.

    No `linked_records["work_order"]` is seeded with it, and that is deliberate: `_probe_targets`
    only asks the WFM when one is present, so this keeps the reconciliation consistent and the run
    on its unattended path. The claim under test is what the record counts, not what the WFM says.

    Watched red by the mutation::

        AssertionError: the closure record says 0 truck rolls for an incident that used 1
    """
    state, ctx = confident
    visited = WorkOrder(
        work_order_id="WO-SEEDED",
        incident_id=str(state["incident_id"]),
        crew_type=CrewType.CLEAN,
        status=WorkOrderStatus.EN_ROUTE,
        created_at=NOW,
        updated_at=NOW,
    )
    _, _, out = await _run(
        _validated(state, passed=True, work_orders=[visited]), ctx, thread="trucks"
    )

    assert out["status"] is IncidentStatus.CLOSED, "the seeded order must not divert the run"
    assert truck_roll_count(out) == 1, "en_route is a truck that travelled"
    assert out["closure"].truck_rolls == 1, (
        f"the closure record says {out['closure'].truck_rolls} truck rolls for an incident that "
        "used 1"
    )


# ------------------------------------------------------------------------------------------------
# D23: the retry ceiling
# ------------------------------------------------------------------------------------------------


async def test_an_unreachable_system_is_retried_to_the_ceiling_and_then_escalated(
    fixtures: Any,
) -> None:
    """A system that will not answer holds the closure, and the hold is bounded twice over.

    TMF is taken down with the simulator's own `simulate_unavailable`, which exists so that
    `DataQualityFlag.ADAPTER_UNAVAILABLE` can be reached. The read then raises, `Gathered.gather`
    drops the name from the payloads, `_reads_for`'s bucket puts it in `systems_unreachable`, and
    `ReconciliationResult.consistent` is `False` -- which is the model's decision, not the node's.

    What the counts pin is that the loop is bounded by the *pack* and not by the step budget. Three
    reads and three holds against `max_retries: 3`, with the retry counter landing on 3 rather than
    on 4: `hold_for_reconciliation_retry` writes `read + 1` and escalates on the same pass it writes
    the last one, so a fourth read never happens. Both numbers are read from the pack rather than
    written here.

    The backoffs are asserted because the node advertises a delay it does not take, and that is a
    real departure from the specification's "retry with limits". If the sequence stopped matching
    `backoff_for`, an orchestrator reading the audit trail would be waiting the wrong interval.

    `evaluate_closure_policy` never running is the load-bearing negative. The whole point of D23 is
    that an incident whose records do not agree is never put to the closure policy at all -- an
    engine asked anyway would answer `blocked` on `RECONCILIATION_MISMATCH`, `route_closure_gate`
    would send it to `abandon_closure`, and the incident would escalate for the right reason by the
    wrong route, with no retry ever attempted.

    Reinstated by making `hold_for_reconciliation_retry` write `attempt` as
    `state["retries"][RETRY_KEY]` rather than `+ 1`, so the counter never moves:
    `AssertionError: assert 7 == 3 / + where 3 = ReconciliationPolicy(max_retries=3, ...)
    .max_retries`. Seven reads against a downed adapter, stopped by `@node`'s visit budget rather
    than by the pack -- and the budget is why the visit counts are asserted before the retry
    counter, since the run does terminate and only the *number* says which limit ended it.
    """
    adapters = build_simulated_adapters(fixtures=fixtures)
    state, ctx = await _to_p11(fixtures, CONFIDENT, thread="p11-down", adapters=adapters)
    policy = load_pack().reconciliation

    adapters.tmf.simulate_unavailable("the records system is not answering")
    _, _, out = await _run(_validated(state, passed=True), ctx, thread="unreachable")

    assert out["reconciliation"].systems_unreachable == ["tmf"]
    assert out["reconciliation"].consistent is False
    assert out["reconciliation"].mismatches == [], "unreachable is not the same as disagreeing"

    assert out["node_visits"]["reconcile_linked_systems"] == policy.max_retries
    assert out["node_visits"]["hold_for_reconciliation_retry"] == policy.max_retries
    assert out["retries"][RETRY_KEY] == policy.max_retries, (
        "the loop must stop at the pack's ceiling, not the step budget"
    )

    holds = _audit(out, "hold_for_reconciliation_retry")
    assert [event.detail["recommended_backoff_seconds"] for event in holds] == [
        policy.backoff_for(n) for n in (1, 2, 3)
    ]
    assert [event.outcome for event in holds] == ["retrying", "retrying", "escalated"]
    assert all(event.detail["inconsistent_systems"] == ["tmf"] for event in holds)

    assert out["escalated"] is True
    assert out["status"] is IncidentStatus.ESCALATED
    assert "tmf still disagrees" in out["escalation_reason"]

    assert "evaluate_closure_policy" not in out["node_visits"]
    assert out.get("closure") is None


# ------------------------------------------------------------------------------------------------
# P25: the two closure codes, and the one that is not reached
# ------------------------------------------------------------------------------------------------


async def test_a_consistent_reconciliation_closes_normally_with_no_approver_named(
    confident: Any,
) -> None:
    """The stage's only unattended path, end to end. Nothing had ever run it.

    Every one of the pack's six systems is asked and every one answers, so D23 says `close`, the
    engine says `allowed` on a 0.95 RCA, and the incident goes `reconciling -> resolved -> closed`
    across two nodes. `approval_ref is None` is the claim that makes it *unattended*: a normal
    closure that carried an approval reference would mean the gate had been asked and this path is
    the one where it is not.

    `CLOSED_NORMAL` is asserted through `ClosureRecord`, which refuses that code without a passing
    `ValidationResult` -- so the code is not a label this node chose but one the model let it keep.

    The two statuses are asserted as a pair because the hop is not this stage's to choose.
    `TRANSITIONS[reconciling]` is `['awaiting_approval', 'cancelled', 'escalated', 'resolved']` --
    `closed` is not on it -- while `TRANSITIONS[resolved]` does list `closed`, and `closed` is
    terminal. So P25b writing `resolved` is the only legal way to reach the status P26 writes, and
    the split across two nodes is `domain.lifecycle`'s requirement rather than a stylistic one.

    Reinstated by having `close_linked_records` write `IncidentStatus.CLOSED`:
    `lpr_cpe.domain.lifecycle.IllegalTransitionError: illegal incident transition reconciling ->
    closed; permitted from reconciling: ['awaiting_approval', 'cancelled', 'escalated', 'resolved']
    / During task with name 'close_linked_records'`. Raised on the write itself, by `advance_status`
    inside the channel, rather than anywhere downstream -- the reducer is what makes the shortcut
    unavailable, so the split cannot be undone by a later edit that looks locally reasonable. The
    recurrence test falls with it, since it closes too.
    """
    state, ctx = confident
    _, _, out = await _run(_validated(state, passed=True), ctx, thread="normal")

    assert "__interrupt__" not in out
    assert out["status"] is IncidentStatus.CLOSED
    assert out["escalated"] is False

    reconciliation = out["reconciliation"]
    assert reconciliation.systems_checked == sorted(load_pack().reconciliation.systems)
    assert reconciliation.systems_unreachable == []
    assert reconciliation.consistent is True

    decision = latest_policy_decision(out, ActionType.CLOSE_INCIDENT)
    assert decision is not None
    assert decision.required_approval_kind is None

    closure = out["closure"]
    assert closure.closure_code is ReasonCode.CLOSED_NORMAL
    assert closure.validated is True
    assert closure.approval_ref is None
    assert closure.reconciliation_id == reconciliation.reconciliation_id
    assert closure.closed_by == ctx.automation_actor

    assert out["node_visits"]["close_linked_records"] == 1
    assert out["node_visits"]["update_kpis_and_learning"] == 1
    assert out["kpi_events"], "P26 emits every KPI this incident can support"

    (closed,) = _audit(out, "update_kpis_and_learning")
    assert len(closed.detail["outcome_labels"]) == 16


async def test_a_failed_validation_closes_only_over_a_named_signature(unconfident: Any) -> None:
    """Proof before closure, as a pause: the exceptional path is the only way past a failed window.

    The engine demands `exceptional_closure` here rather than `low_confidence_rca` even though this
    incident's RCA is 0.2891 and would earn the latter on its own -- `_most_restrictive` prefers the
    exceptional kind, which is what keeps this path reachable for a weak RCA at all. Both reason
    codes are on the decision and the ordering is the engine's, so it is asserted as a set.

    The pause is asserted before the answer because the two halves are separately breakable. A gate
    that built the question and closed anyway would still produce a `ClosureRecord`; what says it
    genuinely waited is `awaiting_approval` committed to state with `pending_approval` holding the
    request, and `close_linked_records` unvisited.

    On the far side, `CLOSED_EXCEPTIONAL` is enforced by `ClosureRecord`, which refuses that code
    with no `approval_ref` and no `exceptional_reason`. So the approver's identity on the record is
    not this node being tidy: the record could not have been constructed without it.

    `reversible=False` is on the request, and it is the honest flag. A closed incident is reopened
    as a *new linked incident* -- `ClosurePolicy.reopen_creates_linked_incident` is `True` -- so
    what is being authorised is not undone by an undo.

    Reinstated by dropping the approver from the record -- `closed_by=ctx.automation_actor`
    unconditionally, rather than `answer.decided_by if answer is not None`:
    `AssertionError: assert 'lpr-cpe-automation' == 'sofia.reyes'`. The pause still happened, the
    approval was still recorded in `approvals`, and the closure record said the robot did it.

    The mutation tried first was reading `kind` from `decision.required_approval_kind` at the
    `build_request` call site instead of naming `EXCEPTIONAL_CLOSURE`, on the theory that a question
    asked under the wrong kind would be answered and never seen. It went green on all ten:
    `route_closure_gate` returns `abandon` unless the demanded kind *is* `EXCEPTIONAL_CLOSURE`, so
    this node is unreachable with any other, and the two spellings are provably equivalent. The
    hardcoded kind is belt-and-braces rather than the thing holding the gate together.
    """
    state, ctx = unconfident
    graph, config, out = await _run(_validated(state, passed=False), ctx, thread="exceptional")

    decision = latest_policy_decision(out, ActionType.CLOSE_INCIDENT)
    assert decision is not None
    assert decision.required_approval_kind is ApprovalKind.EXCEPTIONAL_CLOSURE
    assert {code.value for code in decision.reason_codes} == {
        "RCA_LOW_CONFIDENCE",
        "VALIDATION_FAILED",
    }

    assert out["status"] is IncidentStatus.AWAITING_APPROVAL
    assert out["pending_approval"].kind is ApprovalKind.EXCEPTIONAL_CLOSURE
    assert "close_linked_records" not in out["node_visits"]
    assert out.get("closure") is None

    (pause,) = out["__interrupt__"]
    request = pause.value["approval_request"]
    assert request["kind"] == ApprovalKind.EXCEPTIONAL_CLOSURE.value
    assert request["reversible"] is False
    assert request["context"]["validation_passed"] is False
    assert "noc_supervisor" in pause.value["permitted_roles"]

    out = await graph.ainvoke(Command(resume=APPROVE), context=ctx, config=config)

    assert out["status"] is IncidentStatus.CLOSED
    closure = out["closure"]
    assert closure.closure_code is ReasonCode.CLOSED_EXCEPTIONAL
    assert closure.validated is False
    assert closure.approval_ref
    assert closure.exceptional_reason
    assert closure.closed_by == "sofia.reyes"


async def test_a_refused_exceptional_closure_leaves_no_closure_record(unconfident: Any) -> None:
    """The gate has to be able to say no, and a no must not be a slow yes.

    Without this the test above passes against a gate that asks and then closes regardless -- the
    approval would be recorded, the pause would be real, and the incident would close over a
    rejection. `closure is None` is the assertion that cannot be satisfied by accident:
    `close_linked_records` is the only node that writes it.

    `abandon_closure` separates its three arrivals in the audit event, and `rejected` is asserted
    rather than just "abandoned" because "the policy blocked it" and "a human said no" call for
    different responses from whoever picks the incident up.

    Reinstated by having `route_closure_gate` return `"close"` for any answered request rather than
    branching on the status:
    `AssertionError: assert ClosureRecord(closure_id='CLS-f1f82ff9ab30765666b1',
    incident_id='INC-SVC-UT-001-A-03', ..., closed_by='sofia.reyes') is None`. The incident closed
    as exceptional over the signature of the supervisor who had just refused it, and every other
    test in this module stayed green.
    """
    state, ctx = unconfident
    graph, config, out = await _run(_validated(state, passed=False), ctx, thread="refused")
    assert "__interrupt__" in out

    out = await graph.ainvoke(Command(resume=REJECT), context=ctx, config=config)

    assert out.get("closure") is None
    assert out["escalated"] is True
    assert out["status"] is IncidentStatus.ESCALATED
    assert out["escalation_reason"] == "the exceptional closure was not approved"

    (abandoned,) = _audit(out, "abandon_closure")
    assert abandoned.outcome == "rejected"
    assert abandoned.detail["approval_status"] == "rejected"
    assert "update_kpis_and_learning" not in out["node_visits"]


async def test_a_weak_rca_on_a_validated_incident_asks_rather_than_abandoning(
    unconfident: Any,
) -> None:
    """The case that used to be unclosable: the repair worked and the diagnosis behind it was thin.

    A validated, reconciled incident whose RCA is 0.2891. This asked for `low_confidence_rca` until
    `PolicyEngine._check_confidence` learned to raise `exceptional_closure` for `CLOSE_INCIDENT`,
    and `low_confidence_rca` is D06's kind -- on the parent, upstream of this stage, and never
    reached again. So the incident was abandoned to a human, while the *same* incident with a
    **failed** validation closed over one supervisor's signature. Proving the service restored made
    it less closable than failing to prove it, for every RCA under the 0.75 bar.

    The reason codes are asserted as an exact set, and that is the assertion that separates this
    test from `test_a_failed_validation_closes_only_over_a_named_signature` above. Both now reach
    `exceptional_closure`; only the *reasons* differ, and only the reasons decide what the approver
    is told. A remap that fired on every closure regardless of the validation would leave both tests
    green on the kind and be caught here.

    Hence the question is asserted whole rather than by substring. It is the operator-visible half
    of the fix: the old sentence was fixed text naming a failed restoration validation, which for
    this incident was simply untrue.

    `validated is True` on a `CLOSED_EXCEPTIONAL` record is not a contradiction and is asserted to
    pin that. `close_linked_records` keys the code on `outcome is REQUIRES_APPROVAL` -- did this
    closure need a signature -- while `validated` records what the window measured. This incident
    was restored *and* closed exceptionally, and the record says both.

    The two halves are separately breakable and were measured red separately. Reverting
    `_check_confidence` to `approval_kind=ApprovalKind.LOW_CONFIDENCE_RCA`::

        assert decision.required_approval_kind is ApprovalKind.EXCEPTIONAL_CLOSURE
        E       AssertionError: assert <ApprovalKind.LOW_CONFIDENCE_RCA: 'low_confidence_rca'> is
                <ApprovalKind.EXCEPTIONAL_CLOSURE: 'exceptional_closure'>

    and putting the old fixed sentence back at the `build_request` call site::

        E       AssertionError: assert 'Approve clos...own restored.' == 'Approve clos...nfidence bar.'
        E         - Approve closing INC-SVC-UT-001-A-03 on the exceptional path? The linked records
                    reconcile, but the root cause behind it is below the confidence bar.
        E         + Approve closing INC-SVC-UT-001-A-03 without a passing restoration validation? The
                    linked records reconcile, but the service has not been shown restored.

    -- which is the defect stated in the approver's own words: the incident whose validation had
    just passed, described to the supervisor as one that had not been shown restored.

    Both times the two neighbouring closure tests stayed green, which is the point: the defect lived
    only on the arm nothing exercised.
    """
    state, ctx = unconfident
    graph, config, out = await _run(_validated(state, passed=True), ctx, thread="weak-rca")

    assert out["validation"].passed is True
    assert out["reconciliation"].consistent is True

    decision = latest_policy_decision(out, ActionType.CLOSE_INCIDENT)
    assert decision is not None
    assert decision.blocked is False
    assert decision.required_approval_kind is ApprovalKind.EXCEPTIONAL_CLOSURE
    assert {code.value for code in decision.reason_codes} == {"RCA_LOW_CONFIDENCE"}

    assert out["status"] is IncidentStatus.AWAITING_APPROVAL
    assert out["node_visits"]["prepare_exceptional_closure_approval"] == 1
    assert out.get("closure") is None

    (pause,) = out["__interrupt__"]
    request = pause.value["approval_request"]
    assert request["question"] == (
        f"Approve closing {out['incident_id']} on the exceptional path? The linked records "
        "reconcile, but the root cause behind it is below the confidence bar."
    )
    assert request["context"]["validation_passed"] is True

    out = await graph.ainvoke(Command(resume=APPROVE), context=ctx, config=config)

    assert out["status"] is IncidentStatus.CLOSED
    closure = out["closure"]
    assert closure.closure_code is ReasonCode.CLOSED_EXCEPTIONAL
    assert closure.validated is True
    assert closure.approval_ref
    assert closure.closed_by == "sofia.reyes"


def _pack_demanding(kind: ApprovalKind) -> PolicyPack:
    """The shipped pack with `close_incident` naming `kind`, round-tripped through validation.

    Dumped and re-validated rather than `model_copy`-ed, because the claim being tested is that a
    *valid* pack can still reach the abandon arm. `model_copy` skips every validator, so a pack it
    produced would prove nothing about what the loader would accept.
    """
    data = load_pack().model_dump()
    data["remote_actions"][ActionType.CLOSE_INCIDENT]["approval_kind"] = kind.value
    return PolicyPack.model_validate(data)


async def test_a_demand_this_gate_does_not_own_goes_to_a_human_rather_than_through(
    confident: Any,
) -> None:
    """An unanswerable demand escalates instead of falling through to a close.

    This used to be reachable on the shipped pack, by the RCA route the test above now owns. It is
    not any more: swept over 32,256 combinations of the inputs `evaluate_closure_policy` varies,
    nothing the shipped pack produces for `CLOSE_INCIDENT` demands a kind other than
    `exceptional_closure`. Deleting the arm was the alternative and was rejected -- the pack is
    data, and `ActionRule.approval_kind` is a field an operator can set on any row -- so the driver
    here is a pack that sets it, which is exactly the shape of the configuration the arm defends
    against.

    A 0.95-RCA service, so `_check_confidence` and `_check_closure` both stay silent and the
    risk-class finding is the only one on the decision. That matters: with a weak RCA the engine
    raises `exceptional_closure` too and `_most_restrictive` prefers it -- measured, the demand
    comes back `exceptional_closure` and the gate answers it -- so this test would quietly stop
    testing its own arm.

    `outcome == "unanswerable"` is asserted because it is the audit trail's only way to distinguish
    this from the two other things that reach `abandon_closure`. An operator reading `blocked` would
    go looking for a policy rule; the answer is that the question needs a different gate.

    Reinstated by deleting the `required_approval_kind is not EXCEPTIONAL_CLOSURE` clause from
    `route_closure_gate`, so an unowned demand falls through to `approve`::

        assert "__interrupt__" not in out
        E       AssertionError: assert '__interrupt__' not in {'__interrupt__': [Interrupt(value=
                {'approval_request': {'approval_id': 'APR-141227696383c4365cf7', 'incident_id':
                'IN...

    The stage paused, asking a supervisor to approve an *exceptional closure* for an incident that
    had validated perfectly well, under a kind the engine never demanded.
    """
    state, _ = confident
    clock = _Ticking(NOW)
    ctx = build_context(
        clock=clock,
        policy=PolicyEngine(_pack_demanding(ApprovalKind.DISPATCH), clock=clock),
    )
    _, _, out = await _run(_validated(state, passed=True), ctx, thread="unownable")

    assert out["validation"].passed is True
    assert out["reconciliation"].consistent is True

    decision = latest_policy_decision(out, ActionType.CLOSE_INCIDENT)
    assert decision is not None
    assert decision.blocked is False
    assert decision.required_approval_kind is ApprovalKind.DISPATCH

    assert "__interrupt__" not in out
    assert "prepare_exceptional_closure_approval" not in out["node_visits"]
    assert out.get("closure") is None
    assert out["status"] is IncidentStatus.ESCALATED
    assert out["escalated"] is True

    (abandoned,) = _audit(out, "abandon_closure")
    assert abandoned.outcome == "unanswerable"
    assert abandoned.detail["required_approval_kind"] == ApprovalKind.DISPATCH.value
    assert "no gate on the closure path owns" in out["escalation_reason"]


# ------------------------------------------------------------------------------------------------
# D24 and P26
# ------------------------------------------------------------------------------------------------


async def test_a_recurrence_updates_one_service_problem_rather_than_opening_a_second(
    fixtures: Any,
) -> None:
    """ "Do not hide chronic problems by treating every recurrence as isolated", enforced by a key.

    The specification's instruction is about *aggregation*, and the mechanism is
    `upsert_service_problem` keyed on the service. A key built from the incident would satisfy every
    other assertion in this module -- the node would run, the record would be written, the label
    would be set -- and would open one problem record per recurrence, which is the exact behaviour
    the instruction forbids. So the two incidents are run against one adapter set and what is
    asserted is that the second wrote nothing.

    `replayed` distinguishes the two runs from the inside, and `recorded_writes` from the outside.
    Both are asserted because the first is the simulator reporting on itself and the second is the
    ledger; a node that fabricated a `replayed` flag would still have written twice.

    D24 is reached here through `prior_incidents`, the cheapest of `route_chronic_pattern`'s four
    ORs to seed and the only one that does not require inventing a work order. The other three are
    the router's to test and `test_routing.py` has them.

    Reinstated by keying the idempotency on `subject.incident_id` instead of `target_ref`:
    `AssertionError: two recurrences on one service must collapse onto one problem record /
    assert 'SP-8EBE4186' == 'SP-9A4F209A'`. Two problem records for one service, which is the
    behaviour the specification names and the only assertion in this module that catches it -- the
    node ran, the record was written and the label was set on both laps.
    """
    adapters = build_simulated_adapters(fixtures=fixtures)
    state, ctx = await _to_p11(fixtures, CONFIDENT, thread="p11-recurrence", adapters=adapters)
    linked = {**state.get("linked_records", {}), PRIOR_INCIDENTS_KEY: "INC-LAST-MONTH"}

    problems: list[str] = []
    replays: list[bool] = []
    for lap, incident_id in enumerate(("INC-RECUR-1", "INC-RECUR-2")):
        seeded = _validated(state, passed=True, incident_id=incident_id, linked_records=linked)
        _, _, out = await _run(seeded, ctx, thread=f"recurrence-{lap}")

        assert out["node_visits"]["record_chronic_pattern"] == 1
        (recorded,) = _audit(out, "record_chronic_pattern")
        assert recorded.detail["signals"]["prior_incidents"] == "INC-LAST-MONTH"
        problems.append(out["linked_records"][SERVICE_PROBLEM_KEY])
        replays.append(bool(recorded.detail["replayed"]))

    assert problems[0] == problems[1], (
        "two recurrences on one service must collapse onto one problem record"
    )
    assert replays == [False, True]
    assert len(adapters.tmf.recorded_writes) == 1

    # The chronic finding is a fact about the service and must not move the incident's status: both
    # laps still close, and `record_chronic_pattern` writes no status at all.
    assert out["status"] is IncidentStatus.CLOSED
    (closed,) = _audit(out, "update_kpis_and_learning")
    assert closed.detail["outcome_labels"]["chronic_fault"] is True


# ------------------------------------------------------------------------------------------------
# The shape
# ------------------------------------------------------------------------------------------------


def test_the_stage_is_nine_nodes_with_one_retry_loop_and_one_gate() -> None:
    """Every edge pinned longhand, read off the `StateGraph` rather than off the module's registry.

    Three properties are worth naming. The retry edge is the graph's only cycle and it returns to
    the *reader*, so a retry re-reads rather than re-judging a stale result. Both gate nodes route
    through the same `GATE_TARGETS`, so the arm that closes and the arm that asks cannot be wired
    apart. And `abandon_closure` is a leaf: nothing follows a closure this stage refused to perform,
    which is what makes `escalated` the end of the line rather than a flag to be stepped over.

    `ESCALATED: END` on every branch is `guarded()`'s sentinel, and it appears even on the edge out
    of `update_kpis_and_learning`'s predecessors because a budget can fire anywhere. The two edges
    that are *not* conditional are the two that have nowhere else to go.

    Reinstated by pointing `hold_for_reconciliation_retry`'s `ONWARD` at `evaluate_closure_policy`,
    the plausible mistake, since it makes the happy path shorter:
    `AssertionError: Differing items: {'hold_for_reconciliation_retry': {'__escalated__':
    '__end__', '__onward__': 'evaluate_closure_policy'}} != {'hold_for_reconciliation_retry':
    {'__escalated__': '__end__', '__onward__': 'reconcile_linked_systems'}}`, with the other six
    branches identical. The ceiling test falls with it at `assert 1 == 3` -- one read, one hold, and
    then the closure policy judging a reconciliation nobody re-ran.
    """
    graph = build_reconciliation_closure_graph()

    assert [name for name, _ in RECONCILIATION_CLOSURE_NODES] == [
        "reconcile_linked_systems",
        "hold_for_reconciliation_retry",
        "evaluate_closure_policy",
        "prepare_exceptional_closure_approval",
        "request_exceptional_closure_approval",
        "abandon_closure",
        "close_linked_records",
        "record_chronic_pattern",
        "update_kpis_and_learning",
    ]
    assert sorted(graph.edges) == [
        (START, "reconcile_linked_systems"),
        ("abandon_closure", END),
        ("update_kpis_and_learning", END),
    ]

    ends = {
        source: dict(branch.ends or {})
        for source, branches in graph.branches.items()
        for branch in branches.values()
    }
    gate = {**GATE_TARGETS, ESCALATED: END}
    assert ends == {
        "reconcile_linked_systems": {
            "close": "evaluate_closure_policy",
            "reconcile_retry": "hold_for_reconciliation_retry",
            "escalate": END,
            ESCALATED: END,
        },
        "hold_for_reconciliation_retry": {
            ONWARD: "reconcile_linked_systems",
            ESCALATED: END,
        },
        "evaluate_closure_policy": gate,
        "prepare_exceptional_closure_approval": {
            ONWARD: "request_exceptional_closure_approval",
            ESCALATED: END,
        },
        "request_exceptional_closure_approval": gate,
        "close_linked_records": {
            "chronic": "record_chronic_pattern",
            "done": "update_kpis_and_learning",
            ESCALATED: END,
        },
        "record_chronic_pattern": {ONWARD: "update_kpis_and_learning", ESCALATED: END},
    }
