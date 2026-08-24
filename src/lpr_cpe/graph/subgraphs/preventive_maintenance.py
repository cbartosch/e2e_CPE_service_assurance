"""D04's preventive arm: look at the subject, open a case, and choose what to do about it.

The specification gives this stage three sentences -- "create or update a preventive-maintenance
case", "select remote prevention, planned Clean Boots work, planned Dirty Boots work, or
monitoring", "keep it linked to any later service incident" -- and the second one is the reason
there are five nodes rather than one.

Why this stage reads the network at all
---------------------------------------
`route_predictive_or_active`'s own docstring hands it over, and the sentence is worth quoting
because it is the whole justification for `assess_predictive_risk` existing:

    Note what this cannot know. Every detector runs at P07 or later, so `anomaly_findings` is empty
    here and no reading of *this* subject's current health exists at D04 at all. [...] **The
    preventive stage is what has to notice and link.**

So D04 routes here on corroboration alone -- case type, and whether correlation saw any *other*
service -- having never looked at this premises. A stage that then chose a disposition from what
D04 left in state would be choosing from nothing.

The read is deliberately narrow: four of `graph.nodes.evidence.SOURCES`' twelve, not all of them.
P07 takes everything because it is building a case for a root cause; this is a forecast, and the
question is only whether the access layer and the radios are healthy. `cpe.wifi` feeds
`decision_services.forecast.forecast_wifi`; `nxt.rf` and `nxt.pnm` feed
`HFCRFDegradationDetector`; `plant.optical` feeds `PONOpticalDegradationDetector`. Those two
detectors are instantiated here by name rather than through `run_detectors`, which runs all
thirteen: eleven of them classify over an RCA that this path does not have, and a `no_fault_found`
risk score computed with `prior=[]` is a statement about how little ran, not about the service.

Why the Wi-Fi forecast is not the whole test
--------------------------------------------
An earlier draft keyed the field-work arm on `forecast.should_dispatch`, which compares the Wi-Fi
band against `health_bands.dispatch_threshold_band` -- `critical` in the shipped pack. Measured
over the simulator: of the 41 fixture services, **zero** produce a critical Wi-Fi band, and of the
17 that reach `D04:preventive`, twelve are `healthy`, two `at_risk` and three have no radio data at
all. The arm could not be taken. Worse, it was blind in the direction that matters: the same 17
contain three services with real access-layer degradation -- `SVC-PO-042-A-04` (medium,
distribution), `SVC-UT-001-A-03` (critical, distribution) and `SVC-VQ-002-A-01` (high, power) --
and the radios see none of them. This paragraph used to give one reason for that, "the fault is in
the fibre", and only the middle one is: `SVC-UT-001-A-03` is a PON span attenuating in both
directions, `SVC-PO-042-A-04` is HFC with DOCSIS RF out of spec at `TAP-PO-042-A`, and
`SVC-VQ-002-A-01` is an ONT with no utility power, raised by `pon_optical_degradation` on a dying
gasp with no optical measurement in the window at all. Three layers, one thing in common: every one
of them is upstream of the radios. Two of the three band `healthy` and the third has no radio data,
so the Wi-Fi read is not a weaker signal here, it is silent. A predictive stage that sent a crew for
a busy 2.4 GHz channel and not for a dying ONT is worse than one that never dispatches.

So the two acting arms key on different evidence, and each on the evidence that can actually
support it: field work on an actionable physical finding, remote prevention on the levers
`forecast_wifi` derived from the radios. The second is the sharper case, because both services that
take it band `at_risk` -- one below `critical` in `HealthBand`'s order -- and `should_dispatch` is
therefore `False` for both. The band that arm reads would act on neither, and both name
`wifi_channel_change` and `cpe_resync`.

Clean Boots and Dirty Boots are one arm here, and where the distinction went
---------------------------------------------------------------------------
The specification names four dispositions and this stage offers three. The missing seam is between
planned Clean Boots work and planned Dirty Boots work, and it is missing because **nothing in this
stage decides it**: `domain.boundaries.crew_for` decides it, from the finding's `suspected_domain`,
and P14 is the stage that reads it. Minting a second answer here would give the crew choice two
owners, and the one that drifted would be the one nothing loaded.

It is not a hypothetical seam either way. Measured over all 41 fixtures, every physical finding
this stage can produce classifies to `CrewType.DIRTY`; not one produces `CrewType.CLEAN`. Splitting
the arm would therefore have added a branch that no fixture takes, to answer a question this stage
does not own. `plan_preventive_field_work` records the domain and the crew `crew_for` derives.

Where the field-work arm does *not* go, and why that is a measurement
---------------------------------------------------------------------
`builder.PENDING_STAGES` used to name P14/D13 as the owner of what happens next, and to hold this
stage's exit open on that basis: the seam was said to be waiting only on somebody deciding what a
preventive `ResolutionOption` is. It is not waiting on that. **No such option can exist**, and the
measurement is the all-`DIRTY` one above read together with the resolution catalogue:

    domain                crew    offers create_work_order
    cpe                   clean   yes
    inside_home_wiring    clean   yes
    drop                  clean   yes
    tap_or_odp            joint   yes
    distribution          dirty   no      <- two of the three arrivals
    feeder                dirty   no
    node_or_olt           dirty   no
    headend_or_co         dirty   no
    power                 dirty   no      <- the third
    service_platform      none    no

Every `DIRTY` domain offers `raise_mr` and no work order; every domain that offers a work order is
`CLEAN` or `JOINT`. That is not a gap in the catalogue -- it is the Clean/Dirty delimiter, which is
what `raise_mr` carrying the `clean_to_dirty_handover` approval kind already says. Work upstream of
the tap or ODP is a maintenance request to OSP, and `field_planning` commits exactly one action
type: `is_dispatchable_option` is `requires_truck_roll and action_type is CREATE_WORK_ORDER`,
narrowed on purpose because `wfm.create_work_order` refuses anything else by name.

So an edge from here to P14 would hand it a plan it must reject. Driven through the real parent on
2026-08-23, three of the 41 services take this arm -- `SVC-PO-042-A-04` and `SVC-UT-001-A-03` on
`distribution`, `SVC-VQ-002-A-01` on `power` -- and all three would reach `route_field_gate`'s
`escalate` and land in `abandon_field_planning`, which writes `diagnosing`. The other two
dispositions cannot be held back from following them: a conditional exit from a subgraph needs a
`routing.DECISIONS` member and the specification declares no decision after D04's preventive arm,
so the edge would be unconditional and a service whose disposition was *monitor it* would walk on
through field execution, restoration validation and closure.

The stage is therefore terminal on purpose and is declared in `builder.DELIBERATE_TERMINALS`. The
arm records the domain, the crew and a window; what is missing is a preventive-maintenance queue
that re-reads the case, which is gap PREVENTIVE-2 and is not an edge in this graph. PREVENTIVE-4
records what it would take to make this arm *act* -- a preventive MR through `subgraphs._mr`, which
is a third entrance to a filing mechanism that already has two, and which would still end here.

Why the policy engine is not consulted
--------------------------------------
`PolicyEngine.evaluate` is called in exactly two places in this repository -- `remote_resolution`
and `self_help` -- and both gate the *execution* of a `ResolutionOption` against a customer's
service. Nothing on this path executes anything: the specification's verb is "select", P11 never
runs, and there is no `ResolutionOption` to pass. Every other consumer of the pack in
`graph.nodes` reads it directly -- twelve call sites, none of them `evaluate` -- and this stage
does the same, taking `evidence.min_sources_for_diagnosis`, `health_bands` and
`detector_thresholds` from `ctx.policy.pack`.

Calling it anyway was tried on paper and is wrong in a way worth recording, because the failure is
silent rather than loud. `_DECISION_CLASS[CREATE_PM_CASE]` is `"diagnosis"`, `RCAPolicy.minimum_for`
has no `"diagnosis"` entry and falls through to `max(...)` of the four named bars -- 0.75, the
strictest in the pack -- and `_check_confidence` raises `RCA_LOW_CONFIDENCE` whenever
`rca_confidence is None` **regardless of the bar**. Measured: `evaluate` returns
`REQUIRES_APPROVAL` with `ApprovalKind.LOW_CONFIDENCE_RCA`. The preventive path has no root cause
by construction -- D04 branches before P07 and P10 -- so every PM case would demand a human
approval, naming an interrupt only reachable from D06, which this branch never visits.

The obvious repair, passing `prediction.confidence`, is a category error and the source says so:
`forecast._confidence` is `min(1, len(features)/4) * (1 - 0.1*len(flags))` -- read completeness,
not causal certainty. It would report a radio that did not answer as *doubt about the root cause*,
and would gate on the same fact `_check_evidence` already gates on, under a second name.

The evidence bar cannot fire at the shipped pack, and stays anyway
------------------------------------------------------------------
`open_preventive_case` gates the disposition on `evidence.min_sources_for_diagnosis`, which is `2`
in the shipped pack, and **no incident reaching this stage through the parent can be below it**.
Measured: over all 41 fixture services the smallest number of distinct source systems present at
D04 is **3**, before this stage reads anything. The floor is structural rather than a property of
the fixture set -- P02 emits one evidence item unconditionally under `event.source.value`, P03 emits
one unconditionally under the plant adapter's name or the literal `"topology"`, and no `EventSource`
value collides with `pon`, `hfc` or `topology`, so two distinct systems are on the state by the time
D04 is asked. Failing every adapter this stage calls does not help: the count is taken over the
whole `evidence` list, so it cannot fall.

That is the shape `graph.guards` deleted a bound for, so the difference matters. The bound it
deleted was dominated by *another bound on the same counter* -- dead whatever anyone configured.
This one is dead only at the shipped number, and the number is an operator's to change; it is the
same two-owner situation as `step_budget`, where the setting binds at the defaults and the pack
binds above them, and the resolution there is likewise not to delete a live owner but to assert
both directions. `test_subgraph_preventive_maintenance.py` does exactly that: one test measures the
floor and records that the default cannot be missed, another raises the pack's bar and shows the
arm firing -- and firing hard enough to overrule an actionable finding, which is the whole of what
the bar is for.

What the bar is *not* asked to do is report how much this stage saw. That is a different quantity
-- `sources_requested`, `sources_read` and `sources_usable` are on `assess_predictive_risk`'s audit
event -- and gating on it here under the pack's name would give one pack field two meanings, which
is how a threshold comes to be tightened for one reader and loosened for another.

What is emitted, and what is not
--------------------------------
No KPI. `KPIName.PREDICTIVE_SCANS_RUN` and `PREDICTIVE_TRUE_POSITIVE_RATE` are both in
`observability.kpi.NOT_DERIVABLE_FROM_STATE`, so `emit_kpi` provably returns `[]` for them -- the
twice-daily scan is a batch job rather than an incident thread, and a true-positive rate read off
one incident's state is 1.0 by construction. The four that P06 emits, `INCIDENTS_CREATED` among
them, belong to the active arm: no incident is created here, and emitting a creation count would
be a lie about the one thing this branch is defined by not doing.

Discriminators are omitted from every `derive_id` call below, and that rests on a topology fact:
the parent has one edge into this subgraph and none back, so each node runs at most once per
thread and the natural keys are already unique. A replay re-enters the same task and produces the
same ids, which is what `append_unique` wants. If a later edit routes anything back in here, the
audit events and evidence of the second pass will collide with the first's and be dropped -- add a
pass counter at that point, as P07 discriminates on `diagnostic_cycles`.

`diagnostic_cycles` is deliberately *not* bumped. It counts passes through the diagnosis stage and
bounds them through `check_budgets`; borrowing it for a stage that is not that one would make one
budget mean two things.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lpr_cpe.decision_services.forecast import forecast_wifi
from lpr_cpe.detectors import (
    DetectionContext,
    HFCRFDegradationDetector,
    PONOpticalDegradationDetector,
)
from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.diagnosis import AnomalyFinding, PredictionResult, PreventiveMaintenanceCase
from lpr_cpe.domain.enums import ActionType, IncidentStatus, Technology
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.nodes._runtime import (
    Freshness,
    Gathered,
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    make_evidence,
    node,
)
from lpr_cpe.graph.nodes.evidence import SOURCES, Subject, place_payloads, reads_for, subject_of
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.graph.subgraphs._shared import evidence_support

# ------------------------------------------------------------------------------------------------
# What this stage reads
# ------------------------------------------------------------------------------------------------

#: The four sources of `graph.nodes.evidence.SOURCES` this stage takes, by technology. Named here
#: rather than derived from the detectors' `requires`, because `requires` names a *field* -- `nxt`,
#: `plant` -- and two sources land in `nxt`. The slot is the fact that matters and only `SOURCES`
#: holds it; see the comment above `Source` for what a private copy of that table would break.
#:
#: `cpe.wifi` is in both because the radios are read whatever the access technology is, and it is
#: the only source the remote-prevention arm has anything to say about.
_SOURCES_BY_TECHNOLOGY: dict[Technology, tuple[str, ...]] = {
    Technology.HFC: ("cpe.wifi", "nxt.rf", "nxt.pnm"),
    Technology.PON: ("cpe.wifi", "plant.optical"),
}

#: What an unknown technology reads. The radios only: `nxt.rf` on a PON service and
#: `plant.optical` on an HFC one both resolve to an adapter that has nothing to say, and the
#: `AdapterUnavailableError` that follows would be recorded as a data-quality defect against a
#: service whose only defect is that P03 did not resolve its technology.
_SOURCES_UNKNOWN: tuple[str, ...] = ("cpe.wifi",)


#: The two detectors this stage runs, by name. Read back by `physical_findings` so that "did the
#: access layer say anything?" is asked of the detectors that looked at the access layer, rather
#: than of whatever happens to be in `anomaly_findings`.
_PHYSICAL_DETECTOR_NAMES: frozenset[str] = frozenset(
    {HFCRFDegradationDetector.name, PONOpticalDegradationDetector.name}
)

#: The case status `open_preventive_case` writes when the pack's evidence minimum was not met. The
#: bar is the pack's and only a node can read the pack, so the *node* decides and the router reads
#: the decision -- see `route_preventive_disposition` for why the router does not ask again.
INSUFFICIENT_EVIDENCE: str = "insufficient_evidence"


def sources_for(technology: Technology) -> tuple[str, ...]:
    """Which of the twelve `SOURCES` this stage reads for a given access technology."""
    return _SOURCES_BY_TECHNOLOGY.get(technology, _SOURCES_UNKNOWN)


def physical_findings(state: IncidentState) -> list[AnomalyFinding]:
    """The access-layer findings this stage produced, actionable ones only.

    Filtered on `AnomalyFinding.actionable` -- confidence at or above 0.6 and no data-quality
    warning -- because the alternative is sending a crew on a reading the detector itself flagged
    as computed over something it could not fully see. `anomaly_findings` is empty at D04 by
    construction, so everything in it here was written by `assess_predictive_risk` and there is
    nothing older to filter out.
    """
    return [
        finding
        for finding in state.get("anomaly_findings", [])
        if finding.detector_name in _PHYSICAL_DETECTOR_NAMES and finding.actionable
    ]


# ------------------------------------------------------------------------------------------------
# Assess
# ------------------------------------------------------------------------------------------------


@node("assess_predictive_risk")
async def assess_predictive_risk(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Read the radios and the access layer, forecast the Wi-Fi, and score what came back.

    Sets `IncidentStatus.DIAGNOSING`, which is legal from `TRIAGING` and true of what this node
    does: it reads telemetry and runs detectors, which is diagnosis whatever the case type says.
    None of the three arms below sets a status, and that is the same rule applied the other way --
    `DISPATCH_PLANNING` on an arm that ends the run would record the incident as having entered a
    stage it never entered, which is the mistake `select_remote_action` documents.

    `forecast_wifi` returns `None` for a CPE whose radios reported nothing, and that `None` is
    written to `prediction` unchanged rather than turned into a zero score. A predictive sweep that
    read an unanswered ACS as the worst possible Wi-Fi would fill the queue with houses whose only
    fault is that nobody was home to the management channel.
    """
    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    subject = subject_of(state)
    names = sources_for(subject.technology)

    calls = reads_for(ctx, subject, names)
    payloads = await gathered.gather(calls, freshness=Freshness.TELEMETRY)

    evidence = [
        make_evidence(
            state,
            ctx,
            node="assess_predictive_risk",
            kind=SOURCES[name].kind,
            subject_ref=subject.service_ref or subject.incident_id,
            summary=f"{SOURCES[name].label} read for the preventive assessment",
            # The gather name, exactly as P07 stamps it, so `SOURCES[item.source_system]` reads
            # back the same way from either producer.
            source_system=name,
            payload=payload if isinstance(payload, dict) else {"rows": payload},
        )
        for name, payload in sorted(payloads.items())
    ]
    refs_by_name = {name: item.ref for name, item in zip(sorted(payloads), evidence, strict=True)}

    findings = await _run_physical_detectors(state, ctx, subject, payloads, now)
    prediction = forecast_wifi(
        payloads.get("cpe.wifi"),
        predicted_at=now,
        subject_ref=subject.service_ref or subject.incident_id,
        bands=ctx.policy.pack.health_bands,
        thresholds=dict(ctx.policy.pack.detector_thresholds),
        data_quality_warnings=(),
        # The radio read alone, not every ref. A forecast derived from the Wi-Fi snapshot is not
        # evidenced by an optical measurement, and citing both would make the assessment look
        # doubly corroborated to anything counting refs.
        evidence_refs=((refs_by_name["cpe.wifi"],) if "cpe.wifi" in refs_by_name else ()),
    )
    if prediction is None and "cpe.wifi" in payloads:
        gathered.add_note("the CPE answered but reported no readable Wi-Fi metric")

    return {
        "status": IncidentStatus.DIAGNOSING,
        "evidence": evidence,
        "anomaly_findings": findings,
        "prediction": prediction,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="assess_predictive_risk",
                action="assess_predictive_risk",
                outcome="assessed",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "technology": subject.technology.value,
                    "sources_requested": len(names),
                    "sources_read": len(payloads),
                    "sources_usable": gathered.usable,
                    "physical_findings": len(findings),
                    "wifi_band": prediction.band.value
                    if prediction is not None and prediction.band is not None
                    else None,
                },
            )
        ],
    }


async def _run_physical_detectors(
    state: IncidentState,
    ctx: GraphContext,
    subject: Subject,
    payloads: dict[str, Any],
    now: datetime,
) -> list[AnomalyFinding]:
    """The two access-layer detectors, against a snapshot built from the narrow read.

    `prior` is left at its default `None` rather than set to `[]`, and the difference is the one
    `DetectionContext` documents: `None` says the classifying pass has not run, `[]` says it ran
    and found nothing. Neither of these two reads `prior`, but the snapshot is what an audit reader
    sees, and one that claimed eleven classifiers had run and produced nothing would be false.

    Each detector's `applies_to` does the technology filter, so both are offered the context and
    the one that does not apply returns `not_applicable` without reading anything. Selecting here
    instead would put the HFC/PON split in a second place, and `BaseDetector` already refuses to
    run a detector outside its technology.
    """
    context = DetectionContext(
        incident_id=subject.incident_id,
        now=now,
        technology=subject.technology,
        cpe=state.get("cpe"),
        topology=state.get("topology"),
        sla=state.get("sla"),
        thresholds=dict(ctx.policy.pack.detector_thresholds),
        **place_payloads(payloads),
    )
    findings: list[AnomalyFinding] = []
    for detector in (HFCRFDegradationDetector(), PONOpticalDegradationDetector()):
        result = await detector.detect(context)
        findings.extend(result.findings)
    return findings


# ------------------------------------------------------------------------------------------------
# Open the case
# ------------------------------------------------------------------------------------------------


@node("open_preventive_case")
async def open_preventive_case(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Create or update the preventive-maintenance case, whatever the assessment found.

    The case is a **record of the assessment**, so it is opened even when the evidence is too thin
    to act on. "We looked at this service on this date and here is what we saw" is the thing a
    preventive-maintenance queue is made of, and withholding the record on thin evidence would
    delete the only trace that the service had been looked at. What the evidence bar gates is the
    *disposition*, which is where `PolicyEngine._check_evidence`'s own rule puts it: evidence gates
    the decisions that consume evidence.

    **The bar is applied here rather than in the router, and it has to be.** A router is a pure
    function of state -- it is handed no `GraphContext` and cannot reach the policy pack -- so a
    router that wanted `evidence.min_sources_for_diagnosis` would have to hold a copy of the number,
    and a copy of a pack value is a threshold that stops changing when the pack does. The node reads
    the pack, writes `INSUFFICIENT_EVIDENCE` to `status`, and the router reads that.

    `case_id` is derived from the incident alone. There is at most one PM case per thread, `pm_case`
    is last-write-wins for that reason, and a second pass would be an update to this case rather
    than a second one -- which is exactly what the specification's "create **or update**" asks for.

    `linked_incident_id` stays `None` and that is not an omission. P06 is on D04's other arm, so no
    incident record exists on this path; `linked_records["pm_case"]` is what a later thread reads to
    find this case, which is the mechanism behind "keep it linked to any later service incident".
    The link is made from the incident's side because the incident is the thing that arrives later.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    prediction = state.get("prediction")
    findings = physical_findings(state)
    case_type = state.get("case_type")
    case_id = derive_id("PMC", state.get("incident_id") or "")

    source_count, _age = evidence_support(state, now)
    minimum = ctx.policy.pack.evidence.min_sources_for_diagnosis
    enough = source_count >= minimum

    case = PreventiveMaintenanceCase(
        case_id=case_id,
        created_at=now,
        subject_ref=subject.service_ref or subject.incident_id,
        technology=subject.technology.value,
        trigger=case_type.value if case_type is not None else "unknown",
        prediction=prediction,
        findings=list(findings),
        impact=state.get("impact"),
        # Written by whichever arm runs next, because the window is a property of the disposition
        # and not of the case. `record_monitoring` leaves it empty, which is the honest reading of
        # "nothing is scheduled".
        recommended_window="",
        priority_score=_priority_of(prediction, findings),
        status="open" if enough else INSUFFICIENT_EVIDENCE,
        linked_incident_id=None,
        notes=[],
    )

    return {
        "pm_case": case,
        "linked_records": {"pm_case": case_id},
        "audit_events": [
            audit(
                state,
                ctx,
                node="open_preventive_case",
                action="create_pm_case",
                outcome="opened" if enough else INSUFFICIENT_EVIDENCE,
                subject_ref=case.subject_ref,
                detail={
                    "case_id": case_id,
                    "trigger": case.trigger,
                    "priority_score": case.priority_score,
                    "findings": len(findings),
                    "sources_read": source_count,
                    "minimum_sources": minimum,
                },
            )
        ],
    }


def _priority_of(prediction: PredictionResult | None, findings: list[AnomalyFinding]) -> float:
    """How this case ranks against the others in the queue. A weight, not a probability.

    The worse of two readings rather than their sum: a service with a dying ONT and perfect Wi-Fi
    should outrank one with mediocre both, and adding them would invert that. `failure_probability`
    is already damped by a half in `forecast`, so a physical finding's score -- which runs to 1.0 --
    dominates it, which is the intended ordering: a measured optical fault outranks a forecast.

    Zero when nothing was readable, and that is a real answer. A case with no reading behind it
    ranks last, which is where a case nobody can act on belongs.
    """
    worst_finding = max((f.score for f in findings), default=0.0)
    forecast = prediction.failure_probability if prediction is not None else 0.0
    return round(max(worst_finding, forecast), 4)


# ------------------------------------------------------------------------------------------------
# The disposition
# ------------------------------------------------------------------------------------------------


def route_preventive_disposition(
    state: IncidentState,
) -> Literal["field_work", "remote_prevention", "monitoring"]:
    """Which of the specification's dispositions this case gets.

    Three questions, asked in this order, and the order is the argument:

    1. **Was there enough evidence to act at all?** Read off `case.status`, which
       `open_preventive_case` set from the pack's `evidence.min_sources_for_diagnosis`. One reading
       is one system's opinion of itself, and a case that schedules work on it schedules work on a
       single unverified number. Below the bar the answer is `monitoring` -- not a fourth
       "insufficient" arm, because the disposition really is to watch and re-scan, and inventing a
       branch to say so would be a branch with nothing different at the end of it.
    2. **Did the access layer say anything actionable?** Before the radios, because it is the more
       serious fault and the one a crew can fix. See the module docstring for the measurement: the
       Wi-Fi forecast is blind to a degrading ONT, and asking it first would send a technician for
       a busy channel while the fibre died.
    3. **Did the radios name a lever?** `PredictionResult.recommended_actions` is derived from the
       breached metrics by an exact lookup against the detector's own metric names, so a non-empty
       tuple means a specific remote change is indicated -- not merely that the Wi-Fi is poor. A
       verdict breaching only `throughput_mbps` names no lever and correctly falls through to
       monitoring: low throughput is a symptom of one of the other four or of something outside the
       radios, and guessing a lever for it would attach an action to the breach least able to
       justify it.

    Every one of the three is read off `pm_case` rather than re-derived from `evidence` and
    `anomaly_findings`, for the reason `remote_resolution`'s docstring gives about
    `first_actionable_option`: `open_preventive_case` has already answered them and written the
    answers down, and a router that asked again is a second answer waiting to disagree with the
    first. It is also what makes the pack reachable from here at all -- see that node's docstring.

    Ask this once, on the state `open_preventive_case` produced, and never again afterwards
    ---------------------------------------------------------------------------------------
    The arm this selects then overwrites the field question 1 reads: `_record_disposition` replaces
    `case.status` with the arm's own disposition, so `INSUFFICIENT_EVIDENCE` is gone from the state
    the moment `record_monitoring` has run. Called a second time on a finished state the function
    therefore falls past question 1 and answers from `findings` alone -- measured on
    `SVC-UT-001-A-03` with the bar raised to 8, the run visits `record_monitoring` and the re-read
    says `field_work`.

    Nothing in the wired graph does that: this is a conditional edge, LangGraph evaluates it exactly
    once at the point it is attached, and all three arms go to `END`. The note is here because the
    tests for this module got it wrong first, and because *which arm ran* has an owner that does not
    move -- `node_visits` -- which is what anyone asking after the fact should read instead.

    Making the status immutable to fix this would be the wrong repair. A case that has been
    dispositioned genuinely is no longer `open`, the status is the field that says so, and freezing
    it to keep a router re-runnable would preserve a query nothing needs at the cost of the record
    the queue actually reads.
    """
    case = state.get("pm_case")
    if case is None:
        # Totality, not a path. `pm_case` is optional on the state so this has to be handled, and
        # it cannot be reached through the wired graph: the only way `open_preventive_case` writes
        # no case is an exhausted budget, and the `@node` wrapper then returns `escalated` without
        # running the body, so `guarded` answers `ESCALATED` before this function is called at all.
        # Measured -- `node_visit_budget={"open_preventive_case": 0}` ends the run with `pm_case`
        # None and no arm visited; `test_subgraph_preventive_maintenance.py` asserts it.
        #
        # It returns rather than raises because a router may not raise -- `straight_on`'s docstring
        # is where that contract is written -- and monitoring is the arm that commits to nothing,
        # which is what a stage that did not finish should be recorded as having chosen. The three
        # arms take the opposite view of the same impossibility, and say why.
        return "monitoring"
    if case.status == INSUFFICIENT_EVIDENCE:
        return "monitoring"
    if case.findings:
        return "field_work"
    if case.prediction is not None and case.prediction.recommended_actions:
        return "remote_prevention"
    return "monitoring"


DISPOSITION_TARGETS: dict[str, str] = {
    "field_work": "plan_preventive_field_work",
    "remote_prevention": "apply_remote_prevention",
    "monitoring": "record_monitoring",
}


def _case_or_raise(state: IncidentState, node_name: str) -> PreventiveMaintenanceCase:
    """The case an arm is about to write its disposition onto, or a loud failure.

    Raises rather than recording a disposition against nothing, and the precedent is
    `guards.escalation_update`, which raises on a passing verdict for the same reason: *"quietly
    returning `{}` would let the graph continue while the caller believed it had stopped it"*. An
    arm reached with no case has been reached by something other than the edge below, because that
    edge cannot produce one -- `route_preventive_disposition` says at length why -- so the state it
    would record is a state nobody can explain.

    `route_preventive_disposition` handles the identical impossibility by returning `monitoring`
    instead, and the two are not inconsistent. A router may not raise; a node may, and a node is
    where the write would happen.
    """
    case = state.get("pm_case")
    if case is None:
        raise ValueError(
            f"{node_name} was entered with no `pm_case`. Only `open_preventive_case` writes one "
            "and only its conditional edge reaches this node, so either that edge has been "
            "rewired or an arm has been given a second predecessor. There is nothing to record a "
            "disposition against."
        )
    return case


# ------------------------------------------------------------------------------------------------
# The three arms
# ------------------------------------------------------------------------------------------------


@node("plan_preventive_field_work")
async def plan_preventive_field_work(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Record that this case needs a visit, and which crew, and stop.

    Records rather than dispatches, and the specification's verb for D04 is "select". What this
    node contributes is the finding's `suspected_domain` and the crew `boundaries.crew_for` derives
    from it, written onto the case where a maintenance queue can read them.

    **Stopping here is the end of the thread and not a handover to P14.** That is a change of
    reading rather than of behaviour -- this node has always stopped -- and the module docstring
    carries the measurement: every crew this arm can name is `DIRTY`, no `DIRTY` domain has a
    work-order option, and `field_planning` commits nothing else. The crew named here is for the
    queue and for a human, not for a stage downstream.

    A finding with no `suspected_domain` yields no crew, and that is recorded as `None` rather than
    guessed at. `crew_for` returns `None` for `UNKNOWN` deliberately -- "diagnosis incomplete" is
    one of its three distinct causes -- and a stage that substituted `DIRTY` because most faults are
    would be sending the more expensive crew on the strength of a default.
    """
    case = _case_or_raise(state, "plan_preventive_field_work")
    findings = physical_findings(state)
    worst = max(findings, key=lambda f: f.score, default=None)
    domain = worst.suspected_domain if worst is not None else None
    crew = crew_for(domain) if domain is not None else None

    return _record_disposition(
        state,
        ctx,
        node_name="plan_preventive_field_work",
        case=case,
        status="planned_field_work",
        window=_window_for(worst),
        note=(
            f"planned field work: {domain.value if domain else 'unclassified'} fault, "
            f"{crew.value if crew else 'crew undetermined'}. "
            "Selection only -- this case is queued for a maintenance window, not dispatched"
        ),
        detail={
            "suspected_domain": domain.value if domain is not None else None,
            "crew": crew.value if crew is not None else None,
            "detector": worst.detector_name if worst is not None else None,
            "score": worst.score if worst is not None else None,
        },
    )


@node("apply_remote_prevention")
async def apply_remote_prevention(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Record the remote levers the forecast indicated, and stop.

    Selects and does not execute, for two reasons that point the same way. The specification's D04
    verb is "select"; and `remote_resolution` is the stage that executes a remote action, with the
    policy evaluation, the approval interrupt and the before-and-after verification that make
    executing one safe. Reaching around it to write to a device from here would be doing the
    dangerous half of that stage without the half that makes it safe.
    """
    case = _case_or_raise(state, "apply_remote_prevention")
    actions: tuple[ActionType, ...] = (
        case.prediction.recommended_actions if case.prediction is not None else ()
    )
    return _record_disposition(
        state,
        ctx,
        node_name="apply_remote_prevention",
        case=case,
        status="remote_prevention_selected",
        window="next_maintenance_window",
        note=(
            "selected remote prevention: "
            + ", ".join(a.value for a in actions)
            + ". Selection only -- `remote_resolution` executes remote actions"
        ),
        detail={"recommended_actions": [a.value for a in actions]},
    )


@node("record_monitoring")
async def record_monitoring(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Record that nothing is warranted yet, and why. The most common arm, by design.

    Measured over the simulator, twelve of the seventeen fixtures that reach `D04:preventive` land
    here. That is the intended shape of a preventive stage rather than a sign it is not working: a
    predictive sweep whose usual answer was "send someone" would be a sweep with its threshold in
    the wrong place.

    The reason is written into the case's notes because "we looked and it was fine" and "we could
    not see enough to say" are the same disposition and different facts, and only one of them is a
    reason to scan again sooner.
    """
    case = _case_or_raise(state, "record_monitoring")
    sources, _age = evidence_support(state, ctx.clock.now())
    bar = ctx.policy.pack.evidence.min_sources_for_diagnosis
    band = (
        case.prediction.band.value
        if case.prediction is not None and case.prediction.band is not None
        else None
    )
    if case.status == INSUFFICIENT_EVIDENCE:
        reason = (
            f"monitoring: {sources} source(s) read against a minimum of {bar}, which is too thin "
            "to schedule work on"
        )
    elif band is None:
        reason = "monitoring: no readable Wi-Fi metric and no actionable access-layer finding"
    else:
        reason = (
            f"monitoring: Wi-Fi band {band}, no actionable access-layer finding and no remote "
            "lever indicated"
        )

    return _record_disposition(
        state,
        ctx,
        node_name="record_monitoring",
        case=case,
        status="monitoring",
        window="",
        note=reason,
        detail={"sources_read": sources, "minimum_sources": bar, "wifi_band": band},
    )


def _window_for(worst: AnomalyFinding | None) -> str:
    """When the visit should happen, from the severity of what was found.

    Three named windows rather than a date, because this stage holds no calendar and P14 does. A
    stage that invented "2026-08-23" would be inventing an appointment nobody had checked a crew
    was free for.
    """
    if worst is None:
        return "next_maintenance_window"
    return {
        "critical": "within_24_hours",
        "high": "within_72_hours",
    }.get(worst.severity.value, "next_maintenance_window")


def _record_disposition(
    state: IncidentState,
    ctx: GraphContext,
    *,
    node_name: str,
    case: PreventiveMaintenanceCase,
    status: str,
    window: str,
    note: str,
    detail: dict[str, Any],
) -> NodeUpdate:
    """Write the chosen disposition onto the case and audit it. Shared by all three arms.

    Shared because the three differ in *what* they decided and not at all in how a decision is
    recorded, and three copies of this would be three chances for one arm to stop writing the
    audit event. `_shared.py`'s docstring makes the same argument about the second caller; here
    there were three from the first line.

    `model_copy` rather than a fresh `PreventiveMaintenanceCase`, and the bypass it implies is
    accounted for: the three fields updated are `status`, `recommended_window` and `notes`, all
    unconstrained on the model, so there is no validator to skip. Rebuilding the case would restate
    twelve fields at three call sites, and the field that got left out would be the one nothing
    noticed.

    The case is not optional here. It was, briefly, on the theory that the guard could escalate
    between `open_preventive_case` and an arm -- which it cannot, because the `@node` wrapper
    returns without running the body and `guarded` diverts to `END` before an arm is entered.
    `_case_or_raise` is where that is now stated, once, rather than as a `None` check in three
    signatures that quietly recorded a disposition against nothing.
    """
    updated = case.model_copy(
        update={
            "status": status,
            "recommended_window": window,
            "notes": [*case.notes, note],
        }
    )
    return {
        "pm_case": updated,
        "audit_events": [
            audit(
                state,
                ctx,
                node=node_name,
                action=node_name,
                outcome=status,
                subject_ref=case.subject_ref,
                detail={**detail, "note": note},
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

PREVENTIVE_MAINTENANCE_NODES: tuple[tuple[str, Any], ...] = (
    ("assess_predictive_risk", assess_predictive_risk),
    ("open_preventive_case", open_preventive_case),
    ("plan_preventive_field_work", plan_preventive_field_work),
    ("apply_remote_prevention", apply_remote_prevention),
    ("record_monitoring", record_monitoring),
)

check_node_registry(PREVENTIVE_MAINTENANCE_NODES, "the preventive-maintenance node registry")


def build_preventive_maintenance_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled.

    The two edges that go somewhere are guarded and the three that go to `END` are plain, which is
    what its two siblings do and is not an inconsistency. `guarded` exists to divert an escalated
    thread to `END` instead of the next node; on an edge whose only destination is already `END`
    there is nothing to divert it from, and wrapping it would add a branch both of whose arms are
    the same. What matters is that no *onward* edge is unguarded -- `escalation_update` stops a node
    from doing work but does not stop the graph, so an unguarded edge out of
    `assess_predictive_risk` would open a case on an assessment that never ran.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in PREVENTIVE_MAINTENANCE_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "assess_predictive_risk")
    graph.add_conditional_edges(
        "assess_predictive_risk",
        guarded(straight_on),
        {ONWARD: "open_preventive_case", ESCALATED: END},
    )
    disposition_map: dict[Any, str] = {**DISPOSITION_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "open_preventive_case", guarded(route_preventive_disposition), disposition_map
    )
    for name in DISPOSITION_TARGETS.values():
        graph.add_edge(name, END)
    return graph


def compile_preventive_maintenance_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer, like its two siblings: a subgraph compiled as a node shares the parent's, and
    one of its own would give the thread a second place to be resumed from. This graph holds no
    interrupt today, which makes that argument look academic -- it is not.
    `plan_preventive_field_work` is where a "schedule this visit?" gate would go if the pack ever
    demanded one, and a subgraph that had acquired its own checkpointer before then would fail at
    exactly that edit.
    """
    return build_preventive_maintenance_graph().compile(name="lpr_cpe_preventive_maintenance")


__all__ = [
    "DISPOSITION_TARGETS",
    "INSUFFICIENT_EVIDENCE",
    "PREVENTIVE_MAINTENANCE_NODES",
    "apply_remote_prevention",
    "assess_predictive_risk",
    "build_preventive_maintenance_graph",
    "compile_preventive_maintenance_graph",
    "open_preventive_case",
    "physical_findings",
    "plan_preventive_field_work",
    "record_monitoring",
    "route_preventive_disposition",
    "sources_for",
]
