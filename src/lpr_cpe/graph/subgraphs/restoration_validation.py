"""Stage 5's first half: P22, did the fix hold? Read the service again, then judge what came back.

`decision_services.restoration` already owns the judgement -- the window, the sample count and the
anomaly reduction -- and `ValidationResult` already refuses to record a pass without them. This
module is what feeds that function from a real incident, so the only things decided here are *what
to re-read* and *what counts as a sample*. Both are derived rather than listed, for reasons set out
against each helper below.

Three nodes: wait, read, judge
-----------------------------
Reading and judging are split for the same reason `execute_remote_repair` and `verify_remote_repair`
are two: the reading is a separate observation from the judgement, with its own instant and its own
evidence items. Folding them together would let one node write the evidence and cite it in the same
breath, and an auditor could no longer see that somebody looked before deciding. It also puts the
adapter calls in a node of their own, which is what lets `assess_restoration` read
`state["evidence"]` for its sample count instead of counting what it just fetched.

The wait is separate again because everything before `interrupt()` re-runs on resume. A node that
paused and then read would re-read on every resume, and each of those reads would be stamped with a
new instant and counted as another post-fix sample -- so an incident woken three times by a
scheduler would satisfy `min_post_fix_samples` without the service having been observed three times.

Why the after-pass runs the full detector suite
-----------------------------------------------
`anomaly_reduction` divides the peak score after by the peak score before, so any detector that
scored before and did not run after removes its own score from the numerator and reports the
difference as improvement. A narrow re-read is therefore not a cheaper version of this measurement,
it is a flattering one.

`_comparable_before` closes that off from the other end: the before-findings are filtered to the
detectors that actually *ran* in the after-pass, so a detector whose source was unreachable this
time is excluded from both sides rather than counted as cleared. When nothing ran at all the pass
contributes no sample, which leaves the record pending instead of passing it on an empty comparison.

Why derived detectors are excluded from the comparison
------------------------------------------------------
`DERIVED_DETECTORS` -- the detectors whose `derives_from_prior` is set -- are dropped from both
sides. Their findings restate the other findings rather than measuring the service, which
`DetectionContext.findings_from` already refuses to double-count for the same reason. Here it is
worse than double-counting, because two of them move *against* the service's health: a repair is
exactly the condition under which `no_fault_found_risk` scores highest.

Measured on SVC-SJ-011-A-01 with the tap repaired and every independent detector clean, against the
shipped pack's `min_anomaly_reduction` of 0.7:

| comparison | peak before | peak after | reduction | verdict |
| --- | --- | --- | --- | --- |
| derived counted | 1.0 | 0.95 (`no_fault_found_risk`) | 0.05 | fails |
| derived excluded | 1.0 | 0.0 | 1.0 | passes |

`anomaly_reduction` takes the peak on each side, so a single derived finding at 0.95 holds the
after-peak up on its own however clean the real detectors are. That is why the exclusion is applied
to the finding lists rather than to the verdict: there is no threshold that survives a measure whose
worst reading is produced by success.

`detectors_ran_after` in the audit detail stays the full set, and
`detectors_excluded_as_derived` names what was dropped. A trail that recorded only the narrowed set
would imply the derived detectors had not run.

What this stage does not ask
----------------------------
D21 is **not** wired here. All three of its answers -- keep observing, back to diagnosis, confirm
the outcome with the customer -- are outside this graph, so it belongs on the parent's edge out of
the subgraph node, exactly as D10 and D12 do for the two Stage 3 branches. See `graph.builder`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from lpr_cpe.decision_services.restoration import stability_window, validate_restoration
from lpr_cpe.detectors import run_detectors
from lpr_cpe.detectors.base import DetectionContext
from lpr_cpe.domain.enums import (
    ActionType,
    EvidenceKind,
    FaultDomain,
    IncidentStatus,
    KPIName,
)
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
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
from lpr_cpe.graph.nodes.evidence import (
    DERIVED_DETECTORS,
    SOURCES,
    place_payloads,
    reads_for,
    subject_of,
)
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.observability.kpi import MetricTimestamp, mark

if TYPE_CHECKING:
    from datetime import datetime

    from lpr_cpe.domain.diagnosis import AnomalyFinding
    from lpr_cpe.policies.models import ValidationPolicy

#: The node whose evidence marks one post-fix look. Named once because two helpers below join on it.
SNAPSHOT_NODE = "snapshot_post_fix_state"

#: The node that owns the pause. Named for the same reason: its own audit trail joins on it.
WAIT_NODE = "await_service_stability"


# ------------------------------------------------------------------------------------------------
# Reading the incident for the validator
# ------------------------------------------------------------------------------------------------


def fix_completed_at(state: IncidentState) -> datetime | None:
    """When the most recent repair finished. `None` when no repair has been recorded at all.

    The stability window is measured from the *fix*, not from the incident, and the fix is whichever
    action or field visit completed last -- a remote reboot, a plant repair, a technician closing a
    work order. `action_history` and `work_orders` are read together because neither is complete on
    its own: the first has no row for a truck roll, and the second has none for a reboot.

    `None` is left for the caller to handle rather than defaulted to the clock here, because the
    two readings differ in the direction that matters. `validate_restoration` needs a
    `window_start`, and supplying "now" makes every window incomplete, which keeps the incident
    observing. Supplying the incident's own creation instant would make the window elapse the moment
    Stage 5 was reached and let an unrecorded fix close on the first sample.
    """
    stamps = [record.completed_at for record in state.get("action_history", [])]
    stamps += [order.completed_at for order in state.get("work_orders", [])]
    recorded = [stamp for stamp in stamps if stamp is not None]
    return max(recorded) if recorded else None


def fix_action_type(state: IncidentState) -> ActionType | None:
    """The kind of repair the window is being measured for, or `None` if none was recorded.

    `stability_window` keys the longer plant window on this, so it must name the action that the
    window is *about*: the last one to complete, matching `fix_completed_at`. An earlier attempt's
    type would give a reprovision the window of the reboot that preceded it.

    Read from `action_history` only. A completed work order moves `fix_completed_at`, but there is
    no `ActionType` for "a technician finished", and `stability_window` treats `None` as the plant
    case -- the longer window -- which is the right reading for work that needed a truck.
    """
    latest: datetime | None = None
    action: ActionType | None = None
    for record in state.get("action_history", []):
        if record.completed_at is None:
            continue
        if latest is None or record.completed_at > latest:
            latest, action = record.completed_at, record.action_type
    return action


def window_deadline(state: IncidentState, policy: ValidationPolicy) -> datetime | None:
    """The instant the stability window closes, or `None` when no repair has been recorded.

    The same two inputs `assess_restoration` judges from -- `fix_completed_at` for the start and
    `fix_action_type` for the length -- so the node that waits and the node that scores cannot
    disagree about when the window ends. Anything else would let the graph resume a minute before
    the validator would accept it and spend a whole cycle finding that out.

    `None` is not "wait no time". It is "there is nothing to measure from", which
    `await_service_stability` turns into a pause with no deadline rather than a pass straight
    through; see that node for why parking is the safe reading of a Stage 5 with no recorded fix.
    """
    started = fix_completed_at(state)
    if started is None:
        return None
    return started + stability_window(fix_action_type(state), policy)


def post_fix_payloads(state: IncidentState, look: int) -> dict[str, Any]:
    """The readings `snapshot_post_fix_state` took on this pass, keyed by gather name.

    Read back out of the evidence rather than re-fetched. The two-node split exists so that the
    reading is a separate observation from the judgement, and a second gather here would defeat it
    twice over: every adapter would be called again, and the detectors would score a *third*
    snapshot that no evidence item records -- so the finding an operator sees would cite readings
    that were not the ones it was derived from.

    Joined on the look number, which is the discriminator the snapshot node stamped into each ref.
    Recomputing the ref is what makes the join exact; matching on `source_system` alone would also
    pick up P07's readings of the same sources.
    """
    incident_id = state.get("incident_id") or ""
    subject_ref = subject_of(state).service_ref or incident_id
    wanted = {
        derive_id(
            "EV", incident_id, SNAPSHOT_NODE, source.kind.value, subject_ref, f"{name}#{look}"
        ): name
        for name, source in SOURCES.items()
    }
    return {
        wanted[item.ref]: _unwrap(item.payload)
        for item in state.get("evidence", [])
        if item.ref in wanted
    }


def _unwrap(payload: dict[str, Any]) -> Any:
    """The inverse of the envelope the snapshot node wraps a non-mapping reading in.

    `EvidenceItem.payload` is a `dict`, and two sources answer with a list -- recent plant changes
    and power outages. They are stored under `rows` and taken back out here, because
    `place_payloads` hands the value straight to a detector and one that expected a list would find
    a dict with a single key and report itself unavailable on evidence that was in fact gathered.
    """
    if set(payload) == {"rows"}:
        return payload["rows"]
    return payload


def post_fix_looks(state: IncidentState, window_start: datetime) -> int:
    """How many usable reads of the service have landed since the fix -- the sample count.

    Counted from the evidence rather than from `node_visits`, because a visit that reached no
    adapter is not a sample and the loop guard's counter cannot tell the difference. One
    `CPE_STATUS` item is written per pass by whichever node did the reading, so counting that kind
    counts passes; counting every item would multiply each pass by the number of sources it read and
    satisfy `min_post_fix_samples` on the first look.

    A P07 re-run inside the window is counted, and that is correct rather than tolerated -- it read
    the same service through the same adapters, and excluding it would discard a real observation
    for having been gathered by a different stage.
    """
    return sum(
        1
        for item in state.get("evidence", [])
        if item.kind is EvidenceKind.CPE_STATUS and item.recorded_at >= window_start
    )


def _comparable_before(
    findings: list[AnomalyFinding], measured_after: frozenset[str]
) -> list[AnomalyFinding]:
    """The pre-fix findings from detectors that ran again, so both sides measure the same thing.

    See the module docstring: a detector that scored before and could not look after would otherwise
    have its score vanish from the numerator and be reported as anomaly that had cleared.

    `measured_after` is already free of derived detectors, so passing it here excludes them from
    this side too. `AnomalyFinding` carries no `derived` flag -- only `DetectorResult` does -- so
    filtering the before-side by name against a set that has been narrowed once is the only way to
    drop the same detectors from both sides.
    """
    return [finding for finding in findings if finding.detector_name in measured_after]


def _sources_read_before(state: IncidentState) -> tuple[str, ...]:
    """The gather names this incident's evidence was actually produced from.

    Derived from `EvidenceItem.source_system`, which P07 and the preventive subgraph both stamp
    with the gather name for exactly this kind of reader, rather than restated as a
    technology-conditional list here. A second copy of that list would be correct on the day it was
    written and would drift the first time a source was added, and the way it would drift -- reading
    fewer sources after the fix than before it -- is the asymmetry the module docstring is about.
    """
    read = {item.source_system for item in state.get("evidence", [])}
    return tuple(sorted(read & set(SOURCES)))


# ------------------------------------------------------------------------------------------------
# P22a -- let the window run
# ------------------------------------------------------------------------------------------------


@node(WAIT_NODE)
async def await_service_stability(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Hold the incident until the stability window has run, then let the reading proceed.

    The specification forbids sleeping through this wait and names what to do instead: persist the
    state and resume it from a scheduled timer event. `interrupt()` is that -- the incident is
    checkpointed here and a scheduler wakes it at the deadline the payload carries.

    The clock is read **before** the interrupt, which is the opposite of `await_customer_response`
    and deliberately so. The rule there is that a pre-interrupt fetch would return early on the
    resume pass and silently discard the answer the operator had just supplied. There is no answer
    to discard here: a timer resume carries nothing, and the clock *is* what the resume signifies.
    Checking it first is therefore how the wait ends, not a way of missing it.

    Why a resume is not taken at its word
    -------------------------------------
    The clock is checked again *after* the interrupt, in a loop, and this was got wrong first: the
    node paused once and then let any resume through, however early. Nothing closed wrongly --
    `assess_restoration` scored the short window `STABILITY_WINDOW_PENDING`, which is correct -- so
    the fault was invisible in the verdict and showed up one edge later. D21 reads a pending
    validation as `continue_observation` and sends the incident back in, and `@node` charges each
    completed re-entry against `attempt_limits.max_subgraph_reentries`. Measured against the shipped
    pack that is 6, so six early wakes escalate an incident whose window is perfectly healthy, for a
    reason having nothing to do with the incident. A scheduler that fires twice, or a retry on a
    timer delivery, is enough.

    Re-checking costs nothing because the wait has nothing to re-do: LangGraph re-runs the whole
    node body on every resume and hands each `interrupt()` call the resume value matching its
    position, so a call whose value has not arrived pauses again. The loop is therefore the shape
    that says "wake me, and I will tell you whether it is time".

    A refused wake is deliberately not counted, having been tried and measured. A lap counter is not
    a count of wakes: because the body restarts each resume, the loop re-evaluates its condition
    once per already-consumed lap, and against a clock that advances on read -- which is what the
    graph-running tests use -- those reads move time themselves, so the loop can exit several laps
    before the last resume it was handed. Measured, eight resumes recorded two. Under `SystemClock`
    the number would be right, which is the trap: a field that means what it says in production and
    something else in every test. An early-firing timer is a fact about the scheduler and is visible
    where the scheduler is; the audit record here says only whether this node paused at all.

    A `None` deadline means no repair has been recorded at all, and this node parks rather than
    passing through. That looks harsh for an incident whose fix simply went unlogged, and it is the
    safer of the two failures: passing through would put the loop D21 closes -- observe, re-read,
    still pending, observe again -- into a spin that re-reads every adapter each time and stops only
    when the loop guard escalates. Parking stops it at once, with a pending interrupt a human can
    see and answer.

    That case is pointedly *not* looped, and the asymmetry is the whole rule in one line: this node
    re-checks whatever it can measure and defers to the resumer on whatever it cannot. A timed
    window has a clock to appeal to and the resumer's opinion does not outrank it. An unrecorded
    repair has nothing to appeal to, so looping there would ignore the one person -- a human who has
    seen the parked interrupt and knows the fix landed out of band -- who can settle it.
    """
    deadline = window_deadline(state, ctx.policy.pack.validation)
    waiting = {
        "stability_window_wait": {
            "incident_id": state.get("incident_id") or "",
            "resume_at": deadline.isoformat() if deadline is not None else None,
            "reason": (
                "the stability window is still running"
                if deadline is not None
                else "no completed repair is recorded, so no window can be measured"
            ),
        }
    }

    paused = False
    if deadline is None:
        interrupt(waiting)
        paused = True
    else:
        while ctx.clock.now() < deadline:
            interrupt(waiting)
            paused = True

    return {
        "audit_events": [
            audit(
                state,
                ctx,
                node=WAIT_NODE,
                action="await_service_stability",
                outcome="resumed" if paused else "window_elapsed",
                subject_ref=subject_of(state).service_ref or state.get("incident_id") or "",
                detail={
                    "resume_at": deadline.isoformat() if deadline is not None else None,
                    "paused": paused,
                },
                discriminator=state.get("node_visits", {}).get(WAIT_NODE, 0),
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# P22b -- read the service again
# ------------------------------------------------------------------------------------------------


@node(SNAPSHOT_NODE)
async def snapshot_post_fix_state(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Re-read every source this incident was diagnosed from, and record the readings as evidence.

    The specification's list for P22 -- NXT snapshot, HFC or PON health, CPE reachability, service
    tests, Wi-Fi where relevant -- is exactly the set P07 gathered, so it is taken from what P07
    recorded rather than written out again. `_sources_read_before` says why.

    The evidence is discriminated by the look number so that a second pass through the window
    produces its own refs. `make_evidence` derives the ref from the incident, the node, the kind and
    the subject, all of which are identical on every pass; without the discriminator `append_unique`
    would keep the first look's payload and silently discard every later one -- which is the reading
    that would prove the fix held.

    `IncidentStatus.VALIDATING` is set here rather than in `assess_restoration`, because reading the
    service after a repair *is* the validating stage, and a pass that ends without a verdict has
    still entered it.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    names = _sources_read_before(state)
    look = state.get("node_visits", {}).get(SNAPSHOT_NODE, 0) + 1

    gathered = Gathered(ctx, assessed_at=now)
    payloads = await gathered.gather(reads_for(ctx, subject, names), freshness=Freshness.TELEMETRY)

    evidence = [
        make_evidence(
            state,
            ctx,
            node=SNAPSHOT_NODE,
            kind=SOURCES[name].kind,
            subject_ref=subject.service_ref or subject.incident_id,
            summary=f"{SOURCES[name].label} re-read for post-fix validation, look {look}",
            source_system=name,
            payload=payload if isinstance(payload, dict) else {"rows": payload},
            observed_at=now,
            discriminator=f"{name}#{look}",
        )
        for name, payload in sorted(payloads.items())
    ]

    return {
        "status": IncidentStatus.VALIDATING,
        "evidence": evidence,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node=SNAPSHOT_NODE,
                action="snapshot_post_fix_state",
                outcome="read" if payloads else "nothing_readable",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "look": look,
                    "sources_requested": len(names),
                    "sources_read": len(payloads),
                    "sources_usable": gathered.usable,
                },
                discriminator=look,
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# P22c -- judge what came back
# ------------------------------------------------------------------------------------------------


@node("assess_restoration")
async def assess_restoration(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Score the post-fix snapshot and hand the comparison to `validate_restoration`.

    Writes `validation` on every pass, including the pending ones, because D21 routes on it and
    `route_stability` reads an absent record as "P22 has not finished". A node that only wrote the
    record once it had something conclusive to say would leave the router unable to tell an
    incomplete window from an unstarted stage.

    The post-fix findings are deliberately **not** written to `anomaly_findings`. That list is the
    diagnosis's record of what was wrong, `findings_before` is read from it on the next pass, and
    appending the after-findings would fold this pass's readings into the baseline the following
    pass measures against -- so the window would compare the fix against itself.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    policy = ctx.policy.pack.validation
    look = state.get("node_visits", {}).get(SNAPSHOT_NODE, 0)

    payloads = post_fix_payloads(state, look)
    results = await run_detectors(
        DetectionContext(
            incident_id=subject.incident_id,
            now=now,
            technology=subject.technology,
            cpe=state.get("cpe"),
            topology=state.get("topology"),
            sla=state.get("sla"),
            thresholds=dict(ctx.policy.pack.detector_thresholds),
            **place_payloads(payloads),
        )
    )
    ran_after = frozenset(result.detector_name for result in results if result.ran)
    measured_after = ran_after - DERIVED_DETECTORS
    findings_after = [
        finding
        for result in results
        for finding in result.findings
        if result.detector_name not in DERIVED_DETECTORS
    ]
    findings_before = _comparable_before(list(state.get("anomaly_findings", [])), measured_after)

    fixed_at = fix_completed_at(state)
    # No recorded repair means nothing to measure a window from. Starting it at `now` keeps the
    # incident observing rather than letting it pass on a window that never ran; see
    # `fix_completed_at` for why the other default is the dangerous one.
    window_start = fixed_at or now
    samples = post_fix_looks(state, window_start) if ran_after else 0

    prior = state.get("validation")
    result = validate_restoration(
        validation_id=derive_id("VAL", subject.incident_id, look),
        incident_id=subject.incident_id,
        validated_at=now,
        window_start=window_start,
        fault_domain=state.get("fault_domain", FaultDomain.UNKNOWN),
        policy=policy,
        action_taken=fix_action_type(state),
        samples_in_window=samples,
        findings_before=findings_before,
        findings_after=findings_after,
        customer_confirmed=prior.customer_confirmed if prior is not None else None,
        evidence_refs=tuple(
            item.ref
            for item in state.get("evidence", [])
            if item.recorded_at >= window_start and item.source_system in SOURCES
        ),
    )

    update: NodeUpdate = {
        "validation": result,
        **mark(MetricTimestamp.VALIDATED_AT, now),
        "audit_events": [
            audit(
                state,
                ctx,
                node="assess_restoration",
                action="assess_restoration",
                outcome=result.reason_code.value,
                subject_ref=subject.service_ref or subject.incident_id,
                reason_code=result.reason_code,
                detail={
                    "look": look,
                    "validation_id": result.validation_id,
                    "passed": result.passed,
                    "window_start": window_start.isoformat(),
                    "window_minutes": int(result.stability_window.total_seconds() // 60),
                    "window_complete": result.window_complete,
                    "samples_in_window": samples,
                    "min_samples_required": result.min_samples_required,
                    "detectors_ran_after": sorted(ran_after),
                    "detectors_excluded_as_derived": sorted(ran_after & DERIVED_DETECTORS),
                    "findings_before": len(findings_before),
                    "findings_after": len(findings_after),
                    "customer_confirmed": result.customer_confirmed,
                    "summary": result.summary,
                },
                discriminator=look,
            )
        ],
    }
    if result.passed:
        # The restoration is only now on the record, so this is where `restored_at` becomes true for
        # a fix nobody could verify at the moment it was applied -- a plant repair, a field visit.
        # `mark` merges, and `verify_remote_repair` may already have stamped the same instant for a
        # remote fix it could verify itself; the later stamp wins and both describe the same event.
        update.update(mark(MetricTimestamp.RESTORED_AT, now))
        # `preview`, not `state`: `time_to_restore_seconds` reads the stamp two lines above, which
        # is still sitting unreduced in `update`. See `select_remote_action` for the same trap.
        update["kpi_events"] = emit_kpi(
            preview(state, update),
            ctx,
            KPIName.TIME_TO_RESTORE_SECONDS,
            node="assess_restoration",
            discriminator=look,
        )
    return update


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The three nodes, in the order P22 walks them. Checked like every other registry, so a node whose
#: decorator disagrees with its key fails on import rather than at the first traced incident.
RESTORATION_VALIDATION_NODES: tuple[tuple[str, Any], ...] = (
    (WAIT_NODE, await_service_stability),
    (SNAPSHOT_NODE, snapshot_post_fix_state),
    ("assess_restoration", assess_restoration),
)

check_node_registry(RESTORATION_VALIDATION_NODES, "the restoration-validation node registry")


def build_restoration_validation_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Both edges are guarded for the reason every other subgraph's are: `escalation_update` stops a
    node from doing work but does not stop the graph, so an unguarded edge would run the assessment
    after the budget had already been declared exhausted -- and write a `ValidationResult` built
    from a snapshot nobody gathered.

    Linear, with no loop back to the wait: D21 owns that loop from the parent's edge, because two of
    its three answers leave this graph entirely. A second loop here would give the incident two
    places to be waiting and only one of them would appear in the parent's trace.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in RESTORATION_VALIDATION_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, WAIT_NODE)
    graph.add_conditional_edges(
        WAIT_NODE, guarded(straight_on), {ONWARD: SNAPSHOT_NODE, ESCALATED: END}
    )
    graph.add_conditional_edges(
        SNAPSHOT_NODE, guarded(straight_on), {ONWARD: "assess_restoration", ESCALATED: END}
    )
    graph.add_edge("assess_restoration", END)
    return graph


def compile_restoration_validation_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent. No checkpointer, by design.

    A subgraph compiled as a node shares the parent's checkpointer -- LangGraph namespaces its state
    beneath the parent's thread -- and handing this one its own would give the incident two places
    to be resumed from.
    """
    return build_restoration_validation_graph().compile(name="lpr_cpe_restoration_validation")


__all__ = [
    "RESTORATION_VALIDATION_NODES",
    "SNAPSHOT_NODE",
    "WAIT_NODE",
    "assess_restoration",
    "await_service_stability",
    "build_restoration_validation_graph",
    "compile_restoration_validation_graph",
    "fix_action_type",
    "fix_completed_at",
    "post_fix_looks",
    "snapshot_post_fix_state",
    "window_deadline",
]
