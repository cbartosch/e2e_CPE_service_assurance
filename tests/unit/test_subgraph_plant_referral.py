"""D08's plant arm, driven from the state the parent actually hands it.

The arrival is built by running the parent with `interrupt_after=["generate_resolution_options"]`,
because D08 is a *chained* decision -- `BRANCH_TARGETS["D07"]["continue"]` is `"D08"`, so there is no
node named D08 to stop after. Measured at that stop, `aget_state(...).next` is `('plant_referral',)`
and `DECISIONS["D08"].route(values)` is `'plant_path'`, so the state the tests drive is the state the
parent would have driven, rather than one assembled to suit them.

Two fixtures, chosen for what they are rather than for passing
--------------------------------------------------------------
`SVC-VQ-002-A-01` is the `power` case, which is eight of the ten that reach here. `SVC-PO-042-A-04`
is `service_platform`, and it is the *counter*-case that matters most: `boundaries.crew_for` returns
`None` for the back-office domains, so `receiving_owner` is `None` and the referral has no crew to
name. Both still reach P20, because D08 diverts both halves.

What each arrives with, measured: `status` is `diagnosing`, `mr_records` and `action_history` are
both empty, the packet's `evidence_refs` is 5 and 9 respectively, and `mr_access_notes(values, None)`
composes a non-empty note from `topology` alone -- `'plant object ODP-VQ-002-A; at
18.15128,-65.43905; remote_island area'`. That last one is the field this whole arm was blocked on,
so it has a test of its own rather than being covered incidentally.

Why the refusal is driven and not constructed
---------------------------------------------
`abandon_plant_referral` has three arrivals and the fixtures produce none of them: driven with an
approving answer, all ten file an MR and none abandons. So the refusal is reached the way a human
would reach it -- by answering `rejected` at the interrupt -- rather than by writing a blocked
`PolicyDecision` into state, which would test the node against a decision the engine never made.

Mutation-checked: 10 defects reinstated one at a time, 9 caught. Each docstring below quotes the
failure actually produced, which for three of them is not the failure that was predicted:

* Skipping P19 and filing straight away never reaches the pause assertion. `ActionRequest`'s own
  validator refuses to build an unapproved `raise_mr` at all.
* Emptying `mr_access_notes` never reaches the required-fields assertion. `submit_mr` re-checks
  `REQUIRED_MR_FIELDS` and raises first, which is what that re-check exists to do.
* Removing the `already_referred` arm does not raise on an unmapped return value as predicted -- the
  edge map still holds the key. It files a second MR *without asking again*, because the first
  round's approval is still in state, which is the sharper version of the hazard.

The one that escaped is recorded where it happened, in
`test_filing_from_records_loads_osp_like_any_other_mr`: adding a `HANDOVER_ACCEPTANCE_RATE` emission
to the filing node left the test green, because that KPI returns `None` without a
`handover_contract` and so emits nothing for a test to see. The assertion that was supposed to catch
it has been deleted rather than left in place looking like a guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain import IncidentStatus, can_transition
from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ApprovalKind,
    CaseType,
    EventSource,
    FaultDomain,
    KPIName,
    Severity,
    Technology,
)
from lpr_cpe.domain.lifecycle import STAGE_TRANSITIONS, TRANSITIONS
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import (
    BRANCH_TARGETS,
    PENDING_STAGES,
    SUBGRAPH_SUCCESSOR,
    build_parent_graph,
)
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, ONWARD
from lpr_cpe.graph.routing import DECISIONS
from lpr_cpe.graph.state import current_mr_records, make_initial_state
from lpr_cpe.graph.subgraphs._mr import REQUIRED_MR_FIELDS
from lpr_cpe.graph.subgraphs.plant_referral import (
    ENTRY_NODE,
    REFERRAL_TARGETS,
    build_plant_referral_graph,
    plant_referral_packet,
    receiving_owner,
)
from lpr_cpe.simulation.loader import build_simulated_adapters

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: `power`, which is eight of the ten fixtures that reach this arm.
PLANT_SERVICE = "SVC-VQ-002-A-01"

#: `service_platform`. `crew_for` returns `None`, so nobody is dispatched and P20 still applies.
BACK_OFFICE_SERVICE = "SVC-PO-042-A-04"

APPROVED = {
    "status": "approved",
    "decided_by": "r.okonjo",
    "decided_by_role": "osp_engineer",
    "rationale": "plant fault confirmed from records; accept the referral",
}

REFUSED = {
    "status": "rejected",
    "decided_by": "r.okonjo",
    "decided_by_role": "osp_engineer",
    "rationale": "send a Clean crew to confirm the delimiter before this comes to OSP",
}

UPSTREAM = {
    "status": "approved",
    "decided_by": "sofia.reyes",
    "decided_by_role": "noc_supervisor",
    "rationale": "driving the incident to the D08 boundary",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock the sibling stage tests use, for their measured reason: inside a
    compiled graph the test cannot advance the clock between nodes, so a frozen one would stamp the
    evaluation, the approval and the filing with one instant.
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


async def _arrival(fixtures: Any, ref: str, tag: str) -> Any:
    """Everything upstream of D08, run for real, on one adapter set.

    One adapter set threaded through the parent and the stage for
    `test_subgraph_plant_execution`'s measured reason: a simulated MR exists only in the ledger of
    the adapter that filed it, so a stage driven on fresh adapters would file into one ledger and be
    read from another.
    """
    service = fixtures.services[ref]
    adapters = build_simulated_adapters(fixtures=fixtures, clock=_Ticking(NOW))
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent",
        checkpointer=InMemorySaver(),
        interrupt_after=["generate_resolution_options"],
    )
    ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    config = {"configurable": {"thread_id": f"parent-{tag}"}}
    await parent.ainvoke(_initial(service), context=ctx, config=config)
    for _ in range(8):
        snapshot = await parent.aget_state(config)
        if not snapshot.interrupts:
            break
        await parent.ainvoke(Command(resume=UPSTREAM), context=ctx, config=config)
    return service, (await parent.aget_state(config)).values, adapters


async def _drive(state: Any, tag: str, answer: Any, *, adapters: Any = None, laps: int = 6) -> Any:
    """Run this stage to a standstill, answering every pause, and report the payloads seen."""
    ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    graph = build_plant_referral_graph().compile(
        name="lpr_cpe_plant_referral", checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": f"referral-{tag}"}}
    await graph.ainvoke(state, context=ctx, config=config)

    seen: list[Any] = []
    for _ in range(laps):
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts:
            break
        payload = snapshot.interrupts[0].value
        seen.append(payload)
        await graph.ainvoke(Command(resume=answer), context=ctx, config=config)
    return (await graph.aget_state(config)).values, seen


def _detail(values: Any, node: str) -> dict[str, Any]:
    """The most recent audit detail written by one node."""
    return [e for e in values["audit_events"] if e.node == node][-1].detail


def _outcomes(values: Any, node: str) -> list[str]:
    return [e.outcome for e in values.get("audit_events") or [] if e.node == node]


def _new_kpis(before: Any, after: Any) -> set[KPIName]:
    seen = {event.event_id for event in before.get("kpi_events") or []}
    return {e.kpi_name for e in after.get("kpi_events") or [] if e.event_id not in seen}


@pytest.fixture
async def plant_arrival(fixtures: Any) -> Any:
    """The `power` case at D08, which the parent would route into this stage next."""
    return await _arrival(fixtures, PLANT_SERVICE, "plant")


@pytest.fixture
async def back_office_arrival(fixtures: Any) -> Any:
    """The `service_platform` case: a referral with no crew to name."""
    return await _arrival(fixtures, BACK_OFFICE_SERVICE, "backoffice")


# ------------------------------------------------------------------------------------------------
# The wiring, and the gap it closed
# ------------------------------------------------------------------------------------------------


def test_d08s_plant_arm_no_longer_ends_the_run() -> None:
    """The arm reaches a stage, that stage has a successor, and `PENDING_STAGES` has let it go.

    All three together, because each on its own can be true while the arm is still broken. An arm
    pointing at a stage that nothing leaves is a dead end one node further along; a `PENDING_STAGES`
    entry left behind would fail the build the *other* way, which `_check_pending_stages` calls
    stale. The three are the whole of what "wired" means here.

    Shown red by restoring `"plant_path": END`:

        AssertionError: D08's plant arm must reach the referral stage rather than END
        assert '__end__' == 'plant_referral'

    That mutation also errors the six driven tests in this module, because `_check_pending_stages`
    refuses the build before any of them reach a node: `GraphTopologyError: these exits reach END
    with nothing to explain them: ['D08:plant_path']`. Both are worth having. The build guard says
    the arm is undeclared; this test says which stage it is supposed to reach, which is the part a
    topology error cannot tell you.
    """
    assert BRANCH_TARGETS["D08"]["plant_path"] == "plant_referral", (
        "D08's plant arm must reach the referral stage rather than END"
    )
    assert SUBGRAPH_SUCCESSOR["plant_referral"] == "plant_execution", (
        "a filed MR belongs to the stage that chases it; P20 runs into P21"
    )
    assert "D08:plant_path" not in PENDING_STAGES, (
        "the gap is filled, so the entry declaring it would now be stale"
    )


def test_the_seam_records_the_hop_the_parent_cannot_see() -> None:
    """`diagnosing -> mr_raised` is one parent-visible jump over `awaiting_approval`.

    The entry is narrow on purpose and the narrowness is the point. `TRANSITIONS` is not widened,
    because the walk this stage takes was already legal hop by hop -- so the assertions below check
    both halves: that the middle is walkable, and that the endpoints are *not* a single legal node
    hop, which is what makes the seam entry necessary rather than redundant.

    `lifecycle`'s own two guards cover the table generically. This one ties the entry to the statuses
    *this* stage writes, which is the thing that would silently drift if a node's status changed.

    Shown red by deleting the entry:

        AssertionError: the parent is shown diagnosing -> mr_raised in one write
        assert (<IncidentStatus.DIAGNOSING: 'diagnosing'>,
                <IncidentStatus.MR_RAISED: 'mr_raised'>) in {(<IncidentStatus.DISPATCH_PLANNING: [...]
    """
    entry = (IncidentStatus.DIAGNOSING, IncidentStatus.MR_RAISED)
    assert entry in STAGE_TRANSITIONS, "the parent is shown diagnosing -> mr_raised in one write"
    assert STAGE_TRANSITIONS[entry] == (IncidentStatus.AWAITING_APPROVAL,), (
        "the middle is P19's approval and nothing else"
    )

    walk = (IncidentStatus.DIAGNOSING, IncidentStatus.AWAITING_APPROVAL, IncidentStatus.MR_RAISED)
    for before, after in pairwise(walk):
        assert can_transition(before, after), f"{before.value} -> {after.value} must be walkable"

    out_of_diagnosis = TRANSITIONS.get(IncidentStatus.DIAGNOSING, frozenset())
    assert IncidentStatus.MR_RAISED not in out_of_diagnosis, (
        "if a node could make this jump the seam entry would be redundant, which "
        "test_no_seam_entry_restates_a_hop_the_node_table_already_allows refuses"
    )

    for status in (IncidentStatus.DIAGNOSING, IncidentStatus.AWAITING_APPROVAL):
        assert can_transition(status, IncidentStatus.ESCALATED), (
            f"abandon_plant_referral escalates from {status.value} with no seam entry"
        )


def test_the_gate_hangs_off_node_one_and_again_off_the_wait() -> None:
    """`START` runs into the evaluation unconditionally; the question is asked on the way out.

    Two edges carry the same router, which is `field_execution`'s handover arrangement. The first
    reads a policy verdict that has just been written; the second reads a human's answer that has
    just arrived. An edge from `START` would need an `ESCALATED` arm nothing could take, because the
    parent's edge into this subgraph is guarded already -- only a node's own `check_budgets` can
    newly escalate.

    The tables are read off the `StateGraph` rather than compared with `REFERRAL_TARGETS`, which
    would only prove the table equals itself.

    Shown red by moving the first conditional edge onto `START`:

        AssertionError: START must run into evaluate_plant_referral unconditionally
        assert ('__start__', 'evaluate_plant_referral') in
        {('abandon_plant_referral', '__end__'), ('file_plant_referral_mr', '__end__')}

    The two plain edges left in that set are the terminals, which is the useful part of the message:
    with the gate on `START` this graph has no unconditional entry edge at all.
    """
    graph = build_plant_referral_graph()

    assert (START, ENTRY_NODE) in set(graph.edges), (
        "START must run into evaluate_plant_referral unconditionally"
    )
    assert START not in graph.branches, "the gate belongs after node one, not before it"

    expected = {**REFERRAL_TARGETS, ESCALATED: END}
    for source in (ENTRY_NODE, "request_plant_referral_approval"):
        gate = next(iter(graph.branches[source].values()))
        assert dict(gate.ends or {}) == expected, f"the gate on {source} must answer all four ways"

    onward = next(iter(graph.branches["prepare_plant_referral_approval"].values()))
    assert dict(onward.ends or {}) == {
        ONWARD: "request_plant_referral_approval",
        ESCALATED: END,
    }, "preparing the question runs into asking it, with nothing between the two"

    for terminal in ("file_plant_referral_mr", "abandon_plant_referral"):
        assert (terminal, END) in set(graph.edges), f"{terminal} ends the stage"


# ------------------------------------------------------------------------------------------------
# The referral itself
# ------------------------------------------------------------------------------------------------


async def test_an_approved_referral_files_an_mr_against_the_plant_object(
    plant_arrival: Any,
) -> None:
    """P19 asks, a human accepts, P20 files. The whole arm, on the state the parent hands it.

    The MR is read back off `current_mr_records` rather than off the audit trail, because that is
    what `plant_execution.route_plant_gate` reads and a referral that audited a filing without
    leaving a record behind would leave the next stage with nothing to chase.

    `mr_raised` is asserted on the status, which is the write the seam entry above exists for.

    The outcome is `simulated` rather than anything this stage chooses: `submit_mr` takes it from
    `create_mr`'s reply, and against the simulated adapter that is what comes back. Asserting the
    whole list rather than its last item is what pins the filing to exactly one.

    Shown red by returning `"file"` from the gate's `refer` arm, so the MR is filed without asking.
    The assertion below is never reached, because `ActionRequest` refuses to be built at all:

        pydantic_core._pydantic_core.ValidationError: 1 validation error for ActionRequest
          Value error, raise_mr needs approval per policy but carries no approval_ref; an action
          that reaches an adapter in this state is an unapproved production write

    Worth recording rather than tidying away: skipping P19 on this path is caught by the domain
    model before any test runs, so what the assertion below adds is the count -- one pause, not two
    and not none -- rather than being the only thing standing between a referral and an unapproved
    filing.
    """
    _service, values, adapters = plant_arrival
    final, seen = await _drive(values, "approved", APPROVED, adapters=adapters)

    assert len(seen) == 1, "the referral must pause for P19's approval before filing"
    request = seen[0]["approval_request"]
    assert request["kind"] == ApprovalKind.CLEAN_TO_DIRTY_HANDOVER.value
    assert "osp_engineer" in seen[0]["permitted_roles"]

    assert final["status"] is IncidentStatus.MR_RAISED
    (record,) = current_mr_records(final).values()
    assert record.plant_object_ref == "ODP-VQ-002-A"
    assert _outcomes(final, "file_plant_referral_mr") == [ActionOutcome.SIMULATED.value]
    assert not final.get("escalated")


async def test_the_filing_carries_every_field_jtrack_refuses_without(
    plant_arrival: Any,
) -> None:
    """All four of `REQUIRED_MR_FIELDS`, non-empty, on a case with no handover to read them from.

    This is the test the whole stage exists for. `create_mr` refuses a missing field
    *non-retryably*, and `access_notes` was absent on all ten fixtures that reach here -- so the arm
    could not have filed anything before `mr_access_notes` composed the note from `topology`. The
    assertion walks `REQUIRED_MR_FIELDS` itself rather than naming four fields, so a fifth
    requirement added to the simulator fails here instead of in production.

    Asserted on the `ActionRequest`'s `parameters` and not on the `MRRecord`, which was the first
    thing tried and is wrong: measured, `MRRecord` carries only `plant_object_ref` of the four, and
    reading the other three off the record reports them all absent. They are jTrack's *inputs*, not
    properties of the thing it returns, and `state["selected_action"]` is the payload as sent.

    Offenders are collected and asserted once rather than asserted inside the loop, so a run that
    breaks two fields reports both.

    Shown red by returning `""` from `mr_access_notes` -- and, like the test above, not by the
    assertion. `submit_mr` re-checks the list itself and raises before `create_mr` is called:

        ValueError: file_plant_referral_mr assembled an MR missing ['access_notes'], which
        `create_mr` refuses non-retryably. `mr_access_notes` names the plant object even when
        topology resolved nothing, and `plant_object_ref` falls back to the service reference, [...]

    So the defect is caught, but by the local re-check whose whole purpose is to catch it. What the
    assertion below adds is the case that check cannot see: a field that is present and wrong rather
    than absent. That is what the `startswith` line is for -- an `access_notes` composed from the
    wrong object would satisfy `submit_mr` and fail here.
    """
    _service, values, adapters = plant_arrival
    final, _seen = await _drive(values, "required", APPROVED, adapters=adapters)

    sent = final["selected_action"].parameters
    empty = [field for field in REQUIRED_MR_FIELDS if not sent.get(field)]
    assert empty == [], f"jTrack refuses these non-retryably: {empty}"

    assert sent["access_notes"].startswith("plant object ODP-VQ-002-A"), (
        "the note names the plant object first, so it is never empty even with no topology"
    )


async def test_a_refused_referral_escalates_and_files_nothing(plant_arrival: Any) -> None:
    """The human says no. The case goes to a person, not around the loop again.

    `escalated` and not `diagnosing`, which is where this stage differs from
    `field_execution.abandon_handover` on purpose: a refused handover has a Clean Boots finding
    diagnosis has not seen, and a refused referral has nothing new at all. Re-diagnosing would read
    the same `fault_domain` and route straight back here.

    The reason string is asserted on its arrival label rather than its whole text, because the label
    is what a supervisor filters on and the explanation is the human's own words.

    Shown red by writing `IncidentStatus.DIAGNOSING` in `abandon_plant_referral`:

        AssertionError: assert <IncidentStatus.DIAGNOSING: 'diagnosing'> is <IncidentStatus.ESCALATED: 'escalated'>
    """
    _service, values, adapters = plant_arrival
    final, seen = await _drive(values, "refused", REFUSED, adapters=adapters)

    assert len(seen) == 1
    assert final["status"] is IncidentStatus.ESCALATED
    assert final["escalated"] is True
    assert "approval_refused" in final["escalation_reason"]
    assert not current_mr_records(final), "a refused referral files nothing"
    assert final.get("pending_approval") is None, "the answered question is cleared"
    assert _outcomes(final, "abandon_plant_referral") == ["approval_refused"]


async def test_a_second_entry_does_not_file_a_second_mr(plant_arrival: Any) -> None:
    """An incident that already holds an MR passes straight through.

    Reachable rather than defensive: `BRANCH_TARGETS["D19"]["retry_diagnosis"]` runs back to
    `determine_root_cause`, which reaches D08 again through the D07 chain, so a case whose MR was
    rejected can arrive here holding one. Filing a second would give OSP two tickets for one
    boundary, and `route_plant_gate` reads only the outstanding one.

    It also closes a staler hazard: on re-entry the previous round's approval is still in state, so
    a gate that read the answer before checking for an MR would treat a decision made about the
    first referral as authorisation for the second.

    Driven by running the stage twice on one thread rather than by seeding an `MRRecord`, so the
    record under test is the one `submit_mr` actually writes.

    Shown red by deleting the `already_referred` clause from `route_plant_referral_gate`:

        AssertionError: one boundary, one MR
        assert 2 == 1

    That mutation is worth reading closely, because it demonstrates the staler hazard rather than
    merely failing. `seen == []` still *passed* under it: the second round asked nobody, because the
    first round's approval was still in state and the gate read it as authorisation. So the defect
    is not "asks twice" but "files twice without asking twice", which is why the MR count and not
    the pause count is what catches it.
    """
    _service, values, adapters = plant_arrival
    first, _seen = await _drive(values, "again-1", APPROVED, adapters=adapters)
    assert len(current_mr_records(first)) == 1

    second, seen = await _drive(first, "again-2", APPROVED, adapters=adapters)

    assert seen == [], "a referral already made asks nobody a second time"
    assert len(current_mr_records(second)) == 1, "one boundary, one MR"
    assert _outcomes(second, ENTRY_NODE)[-1] == "already_referred"
    assert second["status"] is IncidentStatus.MR_RAISED, "the status the first round left"


async def test_filing_from_records_loads_osp_like_any_other_mr(plant_arrival: Any) -> None:
    """`PLANT_REPAIR_BACKLOG` is emitted: an MR raised from records is still an MR OSP must clear.

    This test began as `..._and_the_handover_ones_are_not`, asserting that
    `HANDOVER_ACCEPTANCE_RATE` and `HANDOVER_REWORK_RATE` were absent, on the reasoning that a later
    edit might add them back for symmetry with `file_plant_mr`. The mutation pass deleted that half.
    Adding a second `emit_kpi` call for `HANDOVER_ACCEPTANCE_RATE` to the filing node left this test
    **green**: both those KPIs read `state["handover_contract"]` and return `None` without one, so
    the defect emits nothing and there is nothing for a test to see. An assertion no defect can
    falsify is not a guard, and keeping it would have advertised protection that was not there.

    What is left can fail. Swapping the emission to `KPIName.MR_REJECTION_RATE` gives:

        AssertionError: assert <KPIName.PLANT_REPAIR_BACKLOG: 'plant_repair_backlog'> in
        {<KPIName.MR_REJECTION_RATE: 'mr_rejection_rate'>,
         <KPIName.POLICY_BLOCK_RATE: 'policy_block_rate'>}

    The `POLICY_BLOCK_RATE` in that set is P19's, not this node's, which is why the assertion is
    membership rather than equality.
    """
    _service, values, adapters = plant_arrival
    final, _seen = await _drive(values, "kpi", APPROVED, adapters=adapters)

    assert KPIName.PLANT_REPAIR_BACKLOG in _new_kpis(values, final)


async def test_a_back_office_referral_names_no_crew(back_office_arrival: Any) -> None:
    """`service_platform` reaches P20 with nobody to dispatch, and the packet says so.

    The half of D08's diversion that is easy to forget. `boundaries.crew_for` returns `None` for the
    two back-office domains, so `receiving_owner` is `None` -- and a packet that defaulted it to
    `dirty` would tell an OSP dispatcher to send a crew to a provisioning fault. The referral is
    still correct: the MR is the record that the case has left the NOC.

    Shown red by falling back to `CrewType.DIRTY.value` in `receiving_owner`:

        AssertionError: nobody is dispatched to a provisioning fault
        assert 'dirty' is None
    """
    _service, values, adapters = back_office_arrival

    assert values["fault_domain"] is FaultDomain.SERVICE_PLATFORM
    assert crew_for(FaultDomain.SERVICE_PLATFORM) is None
    assert receiving_owner(FaultDomain.SERVICE_PLATFORM) is None, (
        "nobody is dispatched to a provisioning fault"
    )
    assert receiving_owner(FaultDomain.POWER) == "dirty", (
        "the same call names a crew for the plant domains, so the None above is not vacuous"
    )

    ctx = build_context(clock=_Ticking(NOW), adapters=adapters)  # type: ignore[arg-type]
    packet = plant_referral_packet(values, ctx)
    assert packet["03_proposed_domain"] is None, (
        "the packet carries the absence rather than a guess"
    )
    assert packet["07_crew_and_equipment_requirement"]["crew_type"] is None

    final, seen = await _drive(values, "backoffice", APPROVED, adapters=adapters)
    assert len(seen) == 1
    assert final["status"] is IncidentStatus.MR_RAISED
    (record,) = current_mr_records(final).values()
    assert record.plant_object_ref == "TAP-PO-042-A"


def test_the_answer_vocabulary_both_sides_of_the_seam_agree_on() -> None:
    """D08 has exactly two answers and the gate's two terminal ones go where they claim.

    The declared half of the pair below, and it costs nothing: no parent run, no adapters. The
    earlier docstring here claimed this test checked D08's answer on fixture state, which it never
    did -- that is the driven test's job and it is the next one down.

    `already_referred` is asserted with `is` and not `==` deliberately: it must be `langgraph`'s own
    `END` sentinel rather than a `"__end__"` string that happens to compare equal, because that is
    what `add_conditional_edges` matches against when it builds the map.

    No mutation is quoted because there is no code defect to reinstate -- both assertions read
    tables, and mutating a table to break a test that reads it proves only that the test reads it.
    What makes them worth keeping is the driven test below, which would go green against the wrong
    vocabulary if these two ever drifted.
    """
    assert set(DECISIONS["D08"].branches) == {"plant_path", "continue"}
    assert REFERRAL_TARGETS["already_referred"] is END
    assert REFERRAL_TARGETS["abandon"] == "abandon_plant_referral"


async def test_d08_diverts_the_fixtures_these_tests_use(fixtures: Any) -> None:
    """The other half of the check above, driven rather than declared.

    Separate from it because this one costs a parent run per fixture and that one costs nothing;
    keeping the cheap assertion out of an async test means the table is checked even when the drive
    is skipped.

    Shown red by pointing `PLANT_SERVICE` at `SVC-UT-001-B-01`, the one fixture that closes:

        AssertionError: SVC-UT-001-B-01 must arrive at D08's plant arm
        assert 'continue' == 'plant_path'
    """
    for ref, tag in ((PLANT_SERVICE, "d08-plant"), (BACK_OFFICE_SERVICE, "d08-back")):
        _service, values, _adapters = await _arrival(fixtures, ref, tag)
        assert values["status"] is IncidentStatus.DIAGNOSING
        assert DECISIONS["D08"].route(values) == "plant_path", (
            f"{ref} must arrive at D08's plant arm"
        )
        assert not current_mr_records(values), "nothing is filed before this stage runs"
