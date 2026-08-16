"""Stage 2, first half: assemble the case, plan tests, run them (P07-P09).

Three nodes and one snapshot. P07 fetches every source the detectors read, records each read as an
`EvidenceItem`, and runs all thirteen detectors over the result. P08 asks which further reading
would change the ranking of the hypotheses those findings produce. P09 takes those readings and
judges them.

Two properties hold this stage together, and both are structural rather than conventional.

**A finding cites the evidence its detector actually read.** `BaseDetector.finding` stamps
`evidence_refs` from `DetectionContext.evidence`, all of it, for every finding. Handing the
detectors the whole case would therefore make every finding cite every source -- and
`rca.build_hypotheses` counts *distinct evidence refs* to decide whether a hypothesis is
corroborated, so every domain would score full corroboration and the factor would become
decorative. The rca module's own docstring names what is left when it does: share alone, which
"makes a lone weak finding certain". So the detectors run against an empty `evidence` list and P07
stamps each finding afterwards with the refs of the sources that detector declared in `requires`.
The join is on `requires` itself, so there is no second table saying who reads what.

**The verdict on a test is the detector's, not this module's.** P09 could compare a fresh RF
reading against a threshold and call it pass or fail, and there would then be two places in this
repository that decide what a bad downstream power level is. Instead each supported test names the
detector that owns its reading, and P09 runs that detector over the fresh payload: a finding is a
failure, a clean result is a pass, and an unavailable one is unavailable. The numbers stay in
`detectors/` and the pack, where they are versioned.

Where the specification asks for more than the adapters supply
--------------------------------------------------------------
P07's list includes DHCP, DNS, AAA and provisioning evidence, technician measurements and photos.
`integrations.base` has no adapter for any of them; `service_platform` here is the CPE's own
throughput diagnostic and nothing else, and the field is named for what it holds. Prior repair
outcomes are the same story -- `linked_records` carries prior *ticket references* from P04 and no
adapter resolves them into outcomes, so `history["previous_incidents"]` is built from this
incident's own completed work orders, which the graph does hold.

P08 plans only the eleven-member `TestKind` enum's *supported* subset -- the six whose reading both
an adapter can produce and a detector can judge. The other five are named in `TestPlan.notes` when a
hypothesis asks for them, in the same way `decision_services.resolution` names the actions the pack
filtered out. A planned test nothing can run or score is a cycle spent discovering that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lpr_cpe.decision_services.rca import build_hypotheses
from lpr_cpe.detectors.base import DetectionContext, Detector, DetectorResult
from lpr_cpe.detectors.correlation import CommonCauseClusterDetector
from lpr_cpe.detectors.cpe_wifi import CPEWiFiAnomalyDetector, ServicePlatformAnomalyDetector
from lpr_cpe.detectors.physical import HFCRFDegradationDetector, PONOpticalDegradationDetector
from lpr_cpe.detectors.registry import all_detectors, run_detectors
from lpr_cpe.domain.diagnosis import AnomalyFinding, TestPlan, TestRequest, TestResult
from lpr_cpe.domain.enums import (
    EvidenceKind,
    IncidentStatus,
    ReasonCode,
    Technology,
    TestKind,
    TestStatus,
)
from lpr_cpe.domain.records import EvidenceItem
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.nodes._runtime import (
    Freshness,
    Gathered,
    NodeUpdate,
    audit,
    derive_id,
    make_evidence,
    node,
)
from lpr_cpe.graph.state import IncidentState, current_work_orders, latest_by_id
from lpr_cpe.integrations.base import AdapterError, AdapterUnavailableError

# ----------------------------------------------------------------------------------------------
# The snapshot P07 assembles
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Source:
    """One adapter read, and where its answer lands.

    `field` is the `DetectionContext` attribute the payload becomes part of, and it is the join key
    between a detector's `requires` and the evidence behind its findings. `slot` is the key within
    that field for the three composite ones (`nxt`, `plant`); `None` means the payload *is* the
    field.
    """

    field: str
    slot: str | None
    kind: EvidenceKind
    label: str


#: Every source P07 reads, keyed by the name it is gathered under. The key is what appears in
#: `DataQualityAssessment.missing_sources` when a read fails, so it is written as
#: `system.reading` rather than as prose -- an operator reading the assessment needs to know which
#: adapter to go and look at.
#:
#: The set is technology-conditional at the call site, not here: an HFC service has no optical
#: reading and asking for one produces a `not_applicable` from the adapter and a phantom
#: data-quality defect on the incident. Which sources apply is `_sources_for` below.
_SOURCES: Mapping[str, _Source] = {
    "nxt.rf": _Source("nxt", "rf", EvidenceKind.RF_MEASUREMENT, "DOCSIS RF levels"),
    "nxt.pnm": _Source("nxt", "pnm", EvidenceKind.PNM_CAPTURE, "PNM capture"),
    "nxt.service_group": _Source(
        "nxt", "service_group", EvidenceKind.CLUSTER_ANALYSIS, "service-group health"
    ),
    "plant.optical": _Source(
        "plant", "optical", EvidenceKind.OPTICAL_MEASUREMENT, "ONT optical levels"
    ),
    "plant.port": _Source("plant", "port", EvidenceKind.TOPOLOGY_LOOKUP, "upstream port health"),
    # The delimiter view is where `degraded_count` against `services_in_service` comes from, which
    # is the one reading in this set that is about the neighbours rather than about this service.
    "plant.delimiter": _Source(
        "plant", "delimiter", EvidenceKind.CLUSTER_ANALYSIS, "tap or ODP view"
    ),
    "cpe.status": _Source("cpe_raw", None, EvidenceKind.CPE_STATUS, "CPE status"),
    "cpe.wifi": _Source("wifi", None, EvidenceKind.WIFI_SCAN, "Wi-Fi radio status"),
    "cpe.throughput": _Source(
        "service_platform", None, EvidenceKind.SPEED_TEST, "downstream throughput"
    ),
    "inventory.recent_changes": _Source(
        "recent_changes", None, EvidenceKind.CHANGE_RECORD, "recent plant changes"
    ),
    "gis.power_outages": _Source(
        "power_outages", None, EvidenceKind.POWER_OUTAGE_REPORT, "commercial power"
    ),
    "gis.weather": _Source("weather", None, EvidenceKind.WEATHER_REPORT, "weather"),
}

#: The context fields P07 fills from the graph's own state rather than from an adapter. Named here
#: because `_evidence_for_detector` joins on field names and would otherwise treat a detector that
#: requires `history` as one whose evidence went missing.
_STATE_FIELDS = frozenset({"history", "peers", "prior"})

#: Every detector whose findings restate other findings. Read off the registry rather than listed,
#: so a new derived detector is excluded from hypothesis weighting the moment it is registered
#: rather than the next time somebody remembers this constant exists. See
#: `DetectionContext.findings_from` for why double-counting them is not merely untidy.
DERIVED_DETECTORS: frozenset[str] = frozenset(
    d.name for d in all_detectors() if getattr(d, "derives_from_prior", False)
)


def _sources_for(technology: Technology) -> tuple[str, ...]:
    """Which reads apply to this service.

    `UNKNOWN` gets both physical sets. That is deliberate and matches `BaseDetector.detect`'s reason
    for not excluding `UNKNOWN` from `applies_to`: a missing technology label is a metadata gap, and
    skipping both physical reads because of one would leave the incident with no physical evidence
    at all -- which the no-fault-found scorer reads as an argument against dispatch. The cost of
    asking is one adapter call that answers `not_applicable`.
    """
    shared = (
        "cpe.status",
        "cpe.wifi",
        "cpe.throughput",
        "inventory.recent_changes",
        "gis.power_outages",
        "gis.weather",
        "plant.port",
        "plant.delimiter",
    )
    hfc = ("nxt.rf", "nxt.pnm", "nxt.service_group")
    pon = ("plant.optical",)
    match technology:
        case Technology.HFC:
            return hfc + shared
        case Technology.PON:
            return pon + shared
        case _:
            return hfc + pon + shared


@dataclass(frozen=True, slots=True)
class _Subject:
    """The references a read needs, pulled out of state once.

    A frozen record rather than a dict so that a reader asking for a reference that does not exist
    fails where it is written instead of returning `None` into an adapter call.
    """

    incident_id: str
    technology: Technology
    service_ref: str
    cpe_ref: str
    delimiter_ref: str
    node_ref: str
    pon_port_ref: str
    service_group_ref: str
    latitude: float | None
    longitude: float | None


def _subject_of(state: IncidentState) -> _Subject:
    topology = state.get("topology")
    return _Subject(
        incident_id=state.get("incident_id") or "",
        technology=state.get("technology", Technology.UNKNOWN),
        service_ref=state.get("service_ref") or "",
        cpe_ref=state.get("cpe_ref") or "",
        delimiter_ref=state.get("delimiter_ref")
        or (topology.delimiter_ref if topology else None)
        or "",
        node_ref=(topology.node_ref if topology else None) or "",
        pon_port_ref=(topology.pon_port_ref if topology else None) or "",
        service_group_ref=(topology.service_group_ref if topology else None) or "",
        latitude=topology.latitude if topology else None,
        longitude=topology.longitude if topology else None,
    )


def _reads(ctx: GraphContext, subject: _Subject, names: Iterable[str]) -> dict[str, Any]:
    """The awaitable for each named source that has the reference it needs.

    A source whose reference is missing is **left out** rather than called with an empty string.
    Calling `fetch_node_health("")` produces an `AdapterError` and therefore a data-quality defect
    that says the adapter failed, when what actually happened is that P03 never resolved a node.
    The two need different remedies -- one is an outage, the other is an enrichment retry -- so
    they must not arrive looking the same.

    Each entry is built **only if its name was asked for**, and the calls are therefore made behind
    a zero-argument lambda rather than eagerly into a lookup table. Building the whole table and
    then selecting from it creates a coroutine for every source this technology does not use --
    `fetch_optical_levels` on an HFC service -- and then drops it unawaited. Python only warns
    about that; a real adapter would have opened a connection for a reply nobody reads.
    """
    adapters = ctx.adapters
    factories: dict[str, tuple[str, Callable[[], Any]]] = {
        "nxt.rf": (
            subject.service_ref,
            lambda: adapters.nxt.fetch_rf_measurements(subject.service_ref),
        ),
        "nxt.pnm": (
            subject.service_ref,
            lambda: adapters.nxt.fetch_pnm_capture(subject.service_ref),
        ),
        "plant.optical": (
            subject.service_ref,
            lambda: adapters.pon.fetch_optical_levels(subject.service_ref),
        ),
        "nxt.service_group": (
            subject.service_group_ref,
            lambda: adapters.nxt.fetch_service_group_health(subject.service_group_ref),
        ),
        "cpe.status": (subject.cpe_ref, lambda: adapters.cpe.read_status(subject.cpe_ref)),
        "cpe.wifi": (subject.cpe_ref, lambda: adapters.cpe.read_wifi_status(subject.cpe_ref)),
        "cpe.throughput": (
            subject.cpe_ref,
            lambda: adapters.cpe.run_diagnostic(subject.cpe_ref, "download_speed"),
        ),
        "plant.port": (
            subject.pon_port_ref if subject.technology is Technology.PON else subject.node_ref,
            (
                (lambda: adapters.pon.fetch_pon_port_health(subject.pon_port_ref))
                if subject.technology is Technology.PON
                else (lambda: adapters.hfc.fetch_node_health(subject.node_ref))
            ),
        ),
        "plant.delimiter": (
            subject.delimiter_ref,
            (
                (lambda: adapters.pon.fetch_odp_view(subject.delimiter_ref))
                if subject.technology is Technology.PON
                else (lambda: adapters.hfc.fetch_tap_view(subject.delimiter_ref))
            ),
        ),
    }
    calls: dict[str, Any] = {}
    for name in names:
        entry = factories.get(name)
        if entry is None:
            continue
        ref, make = entry
        if ref:
            calls[name] = make()
    return calls


def _place(payloads: Mapping[str, Any]) -> dict[str, Any]:
    """Fold gathered payloads into `DetectionContext` keyword arguments.

    `nxt` and `plant` are composites and are only created when at least one of their slots
    answered. That is the difference between `{"rf": ...}` with a missing PNM key -- which
    `HFCRFDegradationDetector` handles as an absent capture -- and `nxt=None`, which the `requires`
    gate reports as the detector having been unable to look at all.
    """
    fields: dict[str, Any] = {}
    for name, payload in payloads.items():
        source = _SOURCES[name]
        if source.slot is None:
            fields[source.field] = payload
        else:
            fields.setdefault(source.field, {})[source.slot] = payload
    return fields


def _throughput_snapshot(subject: _Subject, payload: object) -> dict[str, Any]:
    """`run_diagnostic`'s envelope reshaped into what `ServicePlatformAnomalyDetector` reads.

    The detector wants `{"download_speed": {...}}`; the adapter returns the measurement under
    `result` alongside its own provenance. Reshaping here rather than in the detector keeps the
    detector readable from a snapshot that no adapter produced, which is what makes it testable
    without a network.
    """
    result = payload.get("result") if isinstance(payload, Mapping) else None
    return {
        "service_ref": subject.service_ref,
        "download_speed": result if isinstance(result, dict) else {},
        "observed_at": payload.get("observed_at") if isinstance(payload, Mapping) else None,
    }


def _history_of(state: IncidentState, now: datetime) -> dict[str, Any]:
    """The incident's own record, in the shape the three risk detectors read.

    Every key here is built from something the graph observed. Where there is no such thing the key
    is left out, and the detector reports `not_applicable` -- which is the honest answer and, unlike
    an empty list, is not counted as a clean scan.

    `previous_incidents` is this incident's own completed field visits rather than the service's
    history across incidents, because no adapter resolves the prior ticket references P04 recorded
    into outcomes. It is the narrower claim, and it is the one the repeat-visit scorer most needs:
    two visits on *this* incident that found nothing is exactly the case where sending a third is
    the wrong move.

    `dispatched` is `WorkOrder.counted_as_truck_roll` and not `True`, because the work-order model
    is where "a crew actually travelled" is decided -- an order cancelled before travel is a row,
    not a visit, and hardcoding `True` here would inflate the repeat-visit score with orders nobody
    ever drove to. `closure_reason` is lowercased because `ReasonCode` is an upper-case wire
    vocabulary and the detector's is lower-case; the case fold is the whole of the translation.
    """
    history: dict[str, Any] = {}

    visits: list[dict[str, Any]] = []
    for order in current_work_orders(state).values():
        if order.completed_at is None:
            continue
        reason = order.reason_code
        visits.append(
            {
                "closed_days_ago": max((now - order.completed_at).total_seconds() / 86400.0, 0.0),
                "dispatched": order.counted_as_truck_roll,
                "closure_reason": reason.value.lower() if reason is not None else "",
            }
        )
    if visits:
        history["previous_incidents"] = visits

    contract = state.get("handover_contract")
    if contract is not None:
        history["handover_package"] = {
            "fault_domain": contract.fault_domain.value,
            "delimiter_ref": contract.delimiter_ref,
            "evidence_refs": list(contract.evidence_refs),
            "access_notes": contract.access_notes,
            "safety_notes": contract.safety_notes,
        }

    samples = _post_fix_samples(state)
    if samples:
        history["post_fix_samples"] = samples
    return history


def _post_fix_samples(state: IncidentState) -> list[dict[str, Any]]:
    """Verifications recorded at or after the most recent completed remote action.

    "The fix" is the last action that completed, and a sample is a verification of an action timed
    from it. Verifications of earlier attempts are excluded rather than folded in: they measure how
    a *superseded* fix was holding, and including them would let a reboot that held for an hour
    before the fault returned vouch for the reprovision that followed it.
    """
    actions = latest_by_id(state.get("remote_actions", []), "action_id").values()
    completed = [a for a in actions if a.completed_at is not None]
    if not completed:
        return []
    fix_at = max(a.completed_at for a in completed if a.completed_at is not None)
    samples: list[dict[str, Any]] = []
    for action in completed:
        if action.verified_at is None or action.verified_at < fix_at:
            continue
        samples.append(
            {
                "minutes_since_fix": (action.verified_at - fix_at).total_seconds() / 60.0,
                "healthy": action.verification_passed is True,
            }
        )
    return samples


def _peer_rows(state: IncidentState) -> list[dict[str, Any]]:
    """The neighbouring services P04 named, as plain rows.

    No shipped detector reads `DetectionContext.peers` -- `CommonCauseClusterDetector` works from
    the delimiter view's counts instead. It is filled anyway because the field's contract says
    `None` means "never fetched", and a future detector handed `None` would report itself
    unavailable on an incident where the neighbours were in fact known. Leaving a known fact out of
    the context is the more expensive of the two mistakes and the quieter one.
    """
    return [
        {
            "service_ref": item.subject_ref,
            "evidence_ref": item.ref,
            "summary": item.summary,
        }
        for item in state.get("evidence", [])
        if item.kind is EvidenceKind.NXT_ALARM and item.subject_ref
    ]


def _evidence_for_detector(
    detector: Detector, by_field: Mapping[str, list[str]], everything: Sequence[str]
) -> tuple[str, ...]:
    """The evidence refs a finding from this detector should cite.

    A derived detector cites everything, because its finding really does rest on the whole pass --
    and it is excluded from hypothesis weighting anyway, so the breadth costs nothing. A telemetry
    detector cites the sources named in its own `requires`, which is the declaration of what it
    reads. A detector requiring only state-supplied fields (`history`) cites nothing, because no
    adapter read stands behind it.
    """
    requires: tuple[str, ...] = getattr(detector, "requires", ())
    if getattr(detector, "derives_from_prior", False) or not requires:
        return tuple(everything)
    refs: list[str] = []
    for field in requires:
        if field in _STATE_FIELDS:
            continue
        refs.extend(by_field.get(field, ()))
    return tuple(dict.fromkeys(refs))


def _stamped(
    results: Sequence[DetectorResult],
    detectors: Sequence[Detector],
    evidence: Sequence[EvidenceItem],
) -> list[AnomalyFinding]:
    """Each finding re-issued citing the evidence its own detector read.

    `AnomalyFinding` is frozen, so this is a copy rather than an edit, and the original never
    reaches state -- there is one version of each finding and it is the one that carries its
    provenance.
    """
    by_field: dict[str, list[str]] = {}
    for item, source in zip(evidence, _sources_of(evidence), strict=True):
        by_field.setdefault(source.field, []).append(item.ref)
    everything = [item.ref for item in evidence]
    by_name = {d.name: d for d in detectors}

    findings: list[AnomalyFinding] = []
    for result in results:
        detector = by_name.get(result.detector_name)
        refs = (
            _evidence_for_detector(detector, by_field, everything)
            if detector is not None
            else tuple(everything)
        )
        findings.extend(f.model_copy(update={"evidence_refs": refs}) for f in result.findings)
    return findings


def _sources_of(evidence: Sequence[EvidenceItem]) -> list[_Source]:
    """The `_Source` behind each item, recovered from the name stamped on it at creation.

    `EvidenceItem.source_system` holds the gather name for items P07 created, so the mapping back
    is a lookup rather than a second list kept in step with the first.
    """
    return [_SOURCES[item.source_system] for item in evidence]


# ----------------------------------------------------------------------------------------------
# P07 -- assemble the case evidence
# ----------------------------------------------------------------------------------------------


@node("assemble_case_evidence")
async def assemble_case_evidence(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P07. Fetch every source once, record each as evidence, run all thirteen detectors.

    One fetch, thirteen readers. The detectors do not fetch -- that is the property that lets them
    be tested from a snapshot with no network -- so the graph is where the snapshot is built, and
    building it once rather than per detector is the difference between twelve adapter calls and a
    hundred and fifty-six.

    The diagnostic cycle counter is bumped here rather than at P10, because this node is the one
    D05 sends the incident back to. A counter incremented at the end of the stage would not count
    the passes that never reached the end, which are precisely the passes a bound exists to limit.
    It also discriminates the evidence refs: a second pass records a genuinely new reading of the
    same source, and `evidence` reduces with `append_unique`, so without the cycle in the key the
    fresher reading would be silently dropped in favour of the stale one the loop was re-entered to
    replace.
    """
    now = ctx.clock.now()
    cycle = state.get("diagnostic_cycles", 0) + 1
    gathered = Gathered(ctx, assessed_at=now)
    subject = _subject_of(state)

    calls = _reads(ctx, subject, _sources_for(subject.technology))
    # One freshness class for the batch. The age limits only bite on payloads that stamp
    # `observed_at`, and every one of these is a current-state reading rather than a structural
    # record -- a tap view from yesterday is as misleading as an RF reading from yesterday.
    payloads = await gathered.gather(calls, freshness=Freshness.TELEMETRY)
    if "gis.power_outages" in _sources_for(subject.technology) and subject.latitude is not None:
        payloads |= await _fetch_geo(ctx, gathered, subject, now)

    if "cpe.throughput" in payloads:
        payloads["cpe.throughput"] = _throughput_snapshot(subject, payloads["cpe.throughput"])

    evidence = [
        make_evidence(
            state,
            ctx,
            node="assemble_case_evidence",
            kind=_SOURCES[name].kind,
            subject_ref=_evidence_subject(name, subject),
            summary=f"{_SOURCES[name].label} read for cycle {cycle}",
            # The gather name, not the adapter's own label: `_sources_of` reads it back to find
            # which context field this item stands behind.
            source_system=name,
            payload=payload if isinstance(payload, Mapping) else {"rows": payload},
            discriminator=f"{name}#{cycle}",
        )
        for name, payload in sorted(payloads.items())
    ]

    detectors = all_detectors()
    context = DetectionContext(
        incident_id=subject.incident_id,
        now=now,
        technology=subject.technology,
        cpe=state.get("cpe"),
        topology=state.get("topology"),
        sla=state.get("sla"),
        history=_history_of(state, now),
        peers=_peer_rows(state),
        # Empty on purpose: see the module docstring. Findings are stamped below with the refs of
        # the sources their own detector declared, which is what keeps RCA's corroboration count
        # from reading every source as backing every hypothesis.
        evidence=[],
        thresholds=dict(ctx.policy.pack.detector_thresholds),
        **_place(payloads),
    )
    results = await run_detectors(context)
    findings = _stamped(results, detectors, evidence)

    for result in results:
        for flag in result.data_quality_warnings:
            gathered.add_flag(flag, f"{result.detector_name}: {result.unavailable_reason}".strip())

    ran = [r for r in results if r.ran]
    return {
        # Set here rather than in P10 because this is where the stage begins, and every path into
        # stage 2 -- first pass, D05's `gather_more`, D06's `retry_diagnosis` -- comes through this
        # node. A retry that skipped evidence assembly would be re-testing yesterday's readings.
        # `can_transition` allows the no-op, so a second cycle re-writing `diagnosing` is legal.
        "status": IncidentStatus.DIAGNOSING,
        "diagnostic_cycles": cycle,
        "evidence": evidence,
        "anomaly_findings": findings,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="assemble_case_evidence",
                action="assemble_case_evidence",
                outcome="assembled",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "cycle": cycle,
                    "sources_read": len(payloads),
                    "sources_usable": gathered.usable,
                    "detectors_ran": len(ran),
                    "detectors_unavailable": len(results) - len(ran),
                    "findings": len(findings),
                },
                discriminator=cycle,
            )
        ],
    }


async def _fetch_geo(
    ctx: GraphContext, gathered: Gathered, subject: _Subject, now: datetime
) -> dict[str, Any]:
    """Power and weather, which need coordinates P03 resolved rather than a service reference."""
    if subject.latitude is None or subject.longitude is None:
        return {}
    return await gathered.gather(
        {
            "gis.power_outages": ctx.adapters.gis.fetch_power_outages(
                latitude=subject.latitude, longitude=subject.longitude, radius_km=2.0
            ),
            "gis.weather": ctx.adapters.gis.fetch_weather(
                latitude=subject.latitude, longitude=subject.longitude, at=now
            ),
        },
        freshness=Freshness.TELEMETRY,
    )


def _evidence_subject(name: str, subject: _Subject) -> str:
    """What each read is *about*, which is not always the service.

    A tap view is about the tap and a node health read is about the node. Filing all twelve under
    the service reference would make the evidence list unreadable at exactly the moment it matters
    -- when a reviewer is trying to tell which readings were of the shared plant and which were of
    this customer's line.
    """
    match name:
        case "plant.delimiter":
            return subject.delimiter_ref or subject.service_ref
        case "plant.port":
            return (
                subject.pon_port_ref if subject.technology is Technology.PON else subject.node_ref
            ) or subject.service_ref
        case "nxt.service_group":
            return subject.service_group_ref or subject.service_ref
        case "cpe.status" | "cpe.wifi":
            return subject.cpe_ref or subject.service_ref
        case _:
            return subject.service_ref or subject.incident_id


# ----------------------------------------------------------------------------------------------
# The supported tests
# ----------------------------------------------------------------------------------------------

#: A reader takes the context and the subject and returns two things: the overlay to merge into the
#: verdict `DetectionContext`, and the raw payload to record as evidence and mine for measurements.
#: They are separate because the overlay is often a reshaping -- the throughput snapshot, the `nxt`
#: composite -- and the evidence should carry what the adapter actually said.
_Reader = Callable[[GraphContext, "_Subject"], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]


async def _read_rf(ctx: GraphContext, subject: _Subject) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = await ctx.adapters.nxt.fetch_rf_measurements(subject.service_ref)
    return {"nxt": {"rf": payload}}, payload


async def _read_pnm(ctx: GraphContext, subject: _Subject) -> tuple[dict[str, Any], dict[str, Any]]:
    # The RF detector reads both slots and scores the capture only alongside the levels, so a PNM
    # sweep is fetched with the levels it is interpreted against rather than on its own.
    levels = await ctx.adapters.nxt.fetch_rf_measurements(subject.service_ref)
    capture = await ctx.adapters.nxt.fetch_pnm_capture(subject.service_ref)
    return {"nxt": {"rf": levels, "pnm": capture}}, capture


async def _read_optical(
    ctx: GraphContext, subject: _Subject
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = await ctx.adapters.pon.fetch_optical_levels(subject.service_ref)
    return {"plant": {"optical": payload}}, payload


async def _read_throughput(
    ctx: GraphContext, subject: _Subject
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = await ctx.adapters.cpe.run_diagnostic(subject.cpe_ref, "download_speed")
    return {"service_platform": _throughput_snapshot(subject, payload)}, payload


async def _read_wifi(ctx: GraphContext, subject: _Subject) -> tuple[dict[str, Any], dict[str, Any]]:
    # `CPEWiFiAnomalyDetector` requires `cpe_raw` and reads `wifi`; a survey handed only the radio
    # snapshot would report itself unavailable on a device that is simply offline, which is a
    # finding rather than a gap.
    status = await ctx.adapters.cpe.read_status(subject.cpe_ref)
    radios = await ctx.adapters.cpe.read_wifi_status(subject.cpe_ref)
    return {"cpe_raw": status, "wifi": radios}, radios


async def _read_delimiter(
    ctx: GraphContext, subject: _Subject
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = (
        await ctx.adapters.pon.fetch_odp_view(subject.delimiter_ref)
        if subject.technology is Technology.PON
        else await ctx.adapters.hfc.fetch_tap_view(subject.delimiter_ref)
    )
    return {"plant": {"delimiter": payload}}, payload


@dataclass(frozen=True, slots=True)
class _TestSpec:
    """One test this system can actually run and actually score.

    `judge` is the detector that owns the reading. It is a factory rather than an instance because
    the detectors are cheap to construct and a shared instance across concurrent incidents would be
    shared mutable state for no benefit.
    """

    reason: str
    discrimination: str
    reader: _Reader
    judge: Callable[[], Detector]
    kind_of_evidence: EvidenceKind
    technologies: tuple[Technology, ...] = ()
    disruptive: bool = False
    customer_present: bool = False
    needs: tuple[str, ...] = ("service_ref",)
    timeout: timedelta = timedelta(minutes=2)


#: The six tests with both an adapter that can produce the reading and a detector that can judge it.
#: The other five members of `TestKind` -- `CPE_CONNECTIVITY`, `LATENCY_JITTER_LOSS`,
#: `PON_OMCI_STATUS`, `PROVISIONING_CHECK`, `SERVICE_PLATFORM_CHECK` -- are absent for one of two
#: reasons and both are recorded in the plan's notes when a hypothesis asks for them:
#:
#: * `CPE_CONNECTIVITY` and `LATENCY_JITTER_LOSS` have an adapter (`ip_ping`) and no detector. There
#:   is nothing in this repository that says what packet loss is too much, and inventing a number
#:   here would put a threshold outside the pack where nobody would look for it.
#: * `PON_OMCI_STATUS`, `PROVISIONING_CHECK` and `SERVICE_PLATFORM_CHECK` have neither a dedicated
#:   reading nor a scorer. `docs/vendor-integration-gaps.md` is where they belong until one exists.
SUPPORTED_TESTS: Mapping[TestKind, _TestSpec] = {
    TestKind.HFC_RF_LEVELS: _TestSpec(
        reason="a fresh DOCSIS level set separates a plant impairment from a stale reading",
        discrimination="levels within spec rule out the HFC physical domains for this window",
        reader=_read_rf,
        judge=HFCRFDegradationDetector,
        kind_of_evidence=EvidenceKind.RF_MEASUREMENT,
        technologies=(Technology.HFC,),
    ),
    TestKind.HFC_PNM_SWEEP: _TestSpec(
        reason="a PNM sweep locates an impedance discontinuity the level set only implies",
        discrimination="a clean sweep argues against drop and in-home wiring faults",
        reader=_read_pnm,
        judge=HFCRFDegradationDetector,
        kind_of_evidence=EvidenceKind.PNM_CAPTURE,
        technologies=(Technology.HFC,),
        timeout=timedelta(minutes=5),
    ),
    TestKind.PON_OPTICAL_POWER: _TestSpec(
        reason="current optical levels separate a fibre fault from an ONT fault",
        discrimination="Rx power in spec with the ONT registered rules out the fibre path",
        reader=_read_optical,
        judge=PONOpticalDegradationDetector,
        kind_of_evidence=EvidenceKind.OPTICAL_MEASUREMENT,
        technologies=(Technology.PON,),
    ),
    TestKind.THROUGHPUT: _TestSpec(
        reason="throughput against the sold rate says whether the customer has what they bought",
        discrimination=(
            "full-rate throughput rules out the service platform and the access layer, "
            "leaving the in-home network"
        ),
        reader=_read_throughput,
        judge=ServicePlatformAnomalyDetector,
        kind_of_evidence=EvidenceKind.SPEED_TEST,
        # Read-only, and still disruptive: it saturates the line it measures. `TestPlan.ordered()`
        # is what keeps it behind the passive reads, and `has_disruptive` is what lets a caller see
        # that the plan contains one.
        disruptive=True,
        needs=("service_ref", "cpe_ref"),
    ),
    TestKind.CPE_WIFI_SURVEY: _TestSpec(
        reason="a radio survey separates an in-home radio problem from an access-network one",
        discrimination="healthy radios move the suspicion outward, past the gateway",
        reader=_read_wifi,
        judge=CPEWiFiAnomalyDetector,
        kind_of_evidence=EvidenceKind.WIFI_SCAN,
        needs=("cpe_ref",),
    ),
    TestKind.NEIGHBOUR_COMPARISON: _TestSpec(
        reason="the neighbours behind this delimiter say whether the fault is shared",
        discrimination=(
            "healthy neighbours confine the fault to this drop; degraded ones move it above "
            "the delimiter"
        ),
        reader=_read_delimiter,
        judge=CommonCauseClusterDetector,
        kind_of_evidence=EvidenceKind.CLUSTER_ANALYSIS,
        needs=("delimiter_ref",),
    ),
}


def _runnable(kind: TestKind, subject: _Subject) -> bool:
    """Whether this test applies to the technology and has the references it needs."""
    spec = SUPPORTED_TESTS.get(kind)
    if spec is None:
        return False
    if (
        spec.technologies
        and subject.technology is not Technology.UNKNOWN
        and subject.technology not in spec.technologies
    ):
        return False
    return all(getattr(subject, need) for need in spec.needs)


def _target_of(kind: TestKind, subject: _Subject) -> str:
    """What the test is performed against, which the result is filed under."""
    match kind:
        case TestKind.NEIGHBOUR_COMPARISON:
            return subject.delimiter_ref
        case TestKind.CPE_WIFI_SURVEY:
            return subject.cpe_ref
        case _:
            return subject.service_ref or subject.cpe_ref


# ----------------------------------------------------------------------------------------------
# P08 -- create the diagnostic test plan
# ----------------------------------------------------------------------------------------------


@node("create_diagnostic_test_plan")
async def create_diagnostic_test_plan(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P08. The minimum set of readings that would change the ranking of the live hypotheses.

    "Minimum safe set" is not a cap this node picks. It is the output of
    `rca.build_hypotheses`, whose `discriminating_tests` are deliberately the *rivals'* recommended
    tests: a test that can only confirm what is already believed cannot change a ranking, so it is
    not in the set. A hypothesis with no rival contributes its own tests, because with nothing to
    discriminate from the question becomes whether the single explanation holds up at all.

    Derived findings are excluded before the hypotheses are built. The domain classifier's finding
    *is* the telemetry findings folded, and letting it back in would give the leading domain a
    second vote cast by the count of the first -- which here would mean planning the tests that
    confirm it rather than the tests that could refute it.

    Nothing is planned that cannot be run and scored. The dropped kinds are named in
    `TestPlan.notes` rather than omitted silently, which is the same rule
    `decision_services.resolution` follows for actions the pack has blocked: a reader who asked for
    a test and does not see it needs to be told why, or the next reader will ask again.
    """
    now = ctx.clock.now()
    cycle = state.get("diagnostic_cycles", 1)
    subject = _subject_of(state)
    findings = live_findings(state)
    hypotheses = build_hypotheses(findings, evidence=ctx.policy.pack.evidence)
    live = [h for h in hypotheses if not h.rejected]

    wanted: list[TestKind] = []
    for hypothesis in live:
        for kind in hypothesis.discriminating_tests:
            if kind not in wanted:
                wanted.append(kind)

    notes: list[str] = []
    requests: list[TestRequest] = []
    for kind in wanted:
        if kind not in SUPPORTED_TESTS:
            notes.append(
                f"{kind.value} would discriminate here but no adapter produces that reading or no "
                "detector scores it, so it was not planned"
            )
            continue
        if not _runnable(kind, subject):
            notes.append(
                f"{kind.value} does not apply to a {subject.technology.value} service or the "
                "reference it needs was never resolved, so it was not planned"
            )
            continue
        spec = SUPPORTED_TESTS[kind]
        requests.append(
            TestRequest(
                request_id=derive_id("REQ", subject.incident_id, kind.value, cycle),
                kind=kind,
                target_ref=_target_of(kind, subject),
                requested_at=now,
                reason=spec.reason,
                expected_discrimination=spec.discrimination,
                disruptive=spec.disruptive,
                requires_customer_present=spec.customer_present,
                timeout=spec.timeout,
            )
        )

    if not live:
        notes.append(
            "no live hypothesis to discriminate between, so there is nothing a test could settle"
        )

    plan = TestPlan(
        plan_id=derive_id("PLAN", subject.incident_id, cycle),
        created_at=now,
        hypothesis_refs=[h.hypothesis_id for h in live],
        requests=requests,
        # Every planned test was selected because it separates hypotheses that are still live.
        # Stopping at the first conclusive one would leave the rest of the ranking untested and
        # send the incident to RCA with a diagnosis that half its own plan disagrees with.
        stop_when="all_planned",
        notes=notes,
    )
    return {
        "test_plan": plan,
        "audit_events": [
            audit(
                state,
                ctx,
                node="create_diagnostic_test_plan",
                action="create_diagnostic_test_plan",
                outcome="planned" if requests else "nothing_to_test",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "cycle": cycle,
                    "hypotheses": len(live),
                    "requested": [r.kind.value for r in plan.ordered()],
                    "unavailable": len(notes),
                    "disruptive": plan.has_disruptive,
                },
                discriminator=cycle,
            )
        ],
    }


def live_findings(state: IncidentState) -> list[AnomalyFinding]:
    """Findings that carry their own evidence, summarising ones removed and repeats folded.

    Public because P10 needs exactly the same set and a second filter there would be a second
    answer to which findings count -- and the two would be discovered to disagree by an `RCAResult`
    whose leading hypothesis is not the domain its own classifier chose.

    **Why the fold.** `localisation.domain_weights` sums `score x confidence` over every finding, so
    one detector saying the same thing twice weighs twice. That is not hypothetical here, it is a
    loop this stage builds itself: a detector fires in P07, its domain's hypothesis asks for the
    tests it recommended, P08 plans them, P09 re-reads the same sources and the *same detector*
    scores them again. A domain's share therefore ends up partly a function of how many tests
    happened to be planned for it -- a diagnosis reporting on the plan rather than on the plant.

    This was measured, not reasoned about. Running P01-P09 over the `hfc_degraded_upstream` fixture
    `SVC-SJ-011-A-01` leaves `hfc_rf_pnm_degradation` with three findings of score 1.0, and the
    posteriors come out `distribution` 0.4287 against `tap_or_odp` 0.3625 -- so the incident is
    diagnosed as this one service's own path. Five of the eight services behind `TAP-SJ-011-A` are
    degraded. It is the tap. With the fold the same run gives `tap_or_odp` 0.4827 against
    `distribution` 0.3065, and the technician is sent to the shared plant instead of to a house
    whose drop is fine.

    So a `(detector, domain)` pair contributes **once**, at its highest score, citing the union of
    the evidence behind every one of its verdicts. Share is counted once because it is one claim;
    corroboration keeps every source because `build_hypotheses` counts distinct evidence refs and
    P09's re-reads are genuinely distinct reads. Recommended tests are unioned so a later reading
    that suggests a new test does not lose it to an earlier one that scored higher.

    Order is the first appearance of each pair, so the set stays deterministic and the P07 reading
    -- the one taken before any test was chosen -- leads.
    """
    folded: dict[tuple[str, str], AnomalyFinding] = {}
    for finding in state.get("anomaly_findings", []):
        if finding.detector_name in DERIVED_DETECTORS:
            continue
        key = (
            finding.detector_name,
            finding.suspected_domain.value if finding.suspected_domain else "",
        )
        seen = folded.get(key)
        if seen is None:
            folded[key] = finding
            continue
        refs = tuple(sorted({*seen.evidence_refs, *finding.evidence_refs}))
        tests = tuple(dict.fromkeys((*seen.recommended_tests, *finding.recommended_tests)))
        strongest = (
            finding
            if (finding.score * finding.confidence) > (seen.score * seen.confidence)
            else seen
        )
        folded[key] = strongest.model_copy(
            update={
                "evidence_refs": refs,
                "recommended_tests": tests,
                "observed_at": max(seen.observed_at, finding.observed_at),
            }
        )
    return list(folded.values())


# ----------------------------------------------------------------------------------------------
# P09 -- execute the read-only tests
# ----------------------------------------------------------------------------------------------


@node("execute_read_only_tests")
async def execute_read_only_tests(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P09. Take each planned reading and let its owning detector score it.

    Every entry in `SUPPORTED_TESTS` calls a `fetch_*`, `read_*` or `run_diagnostic` method. None of
    them reaches `apply_action`, which is the only path through `integrations` that touches the
    write gate -- so "read-only" here is a property of the table rather than a check at the call
    site, and `tests/` asserts it by running this node and requiring the gate to have recorded
    nothing.

    Results are recorded for tests that failed to execute as well as for tests that ran.
    `TestStatus.UNAVAILABLE` and `TestStatus.INCONCLUSIVE` are different answers -- we could not
    look, against we looked and learned nothing -- and the specification asks for "any failure to
    execute" to be stored precisely so that the second is never reported as the first.

    The findings the scoring detectors produce are appended to `anomaly_findings`, which is what
    makes the plan worth running: without it a test could confirm or refute a hypothesis and RCA
    would never see the answer, and P08's whole selection criterion would be ceremony.
    """
    now = ctx.clock.now()
    cycle = state.get("diagnostic_cycles", 1)
    subject = _subject_of(state)
    plan = state.get("test_plan")
    if plan is None or not plan.requests:
        return {
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="execute_read_only_tests",
                    action="execute_read_only_tests",
                    outcome="no_plan",
                    subject_ref=subject.service_ref or subject.incident_id,
                    detail={"cycle": cycle},
                    discriminator=cycle,
                )
            ]
        }

    results: list[TestResult] = []
    evidence: list[EvidenceItem] = []
    findings: list[AnomalyFinding] = []

    for request in plan.ordered():
        outcome = await _execute(state, ctx, request, subject, now=now, cycle=cycle)
        results.append(outcome.result)
        evidence.extend(outcome.evidence)
        findings.extend(outcome.findings)
        if plan.stop_when == "first_conclusive" and outcome.result.conclusive:
            break

    return {
        "test_results": results,
        "evidence": evidence,
        "anomaly_findings": findings,
        "audit_events": [
            audit(
                state,
                ctx,
                node="execute_read_only_tests",
                action="execute_read_only_tests",
                outcome="executed",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "cycle": cycle,
                    "planned": len(plan.requests),
                    "run": len(results),
                    "conclusive": sum(1 for r in results if r.conclusive),
                    "unavailable": sum(1 for r in results if r.status is TestStatus.UNAVAILABLE),
                },
                discriminator=cycle,
            )
        ],
    }


@dataclass(frozen=True, slots=True)
class _Executed:
    """What one test produced: the result, the reading, and any finding scored from it."""

    result: TestResult
    evidence: tuple[EvidenceItem, ...]
    findings: tuple[AnomalyFinding, ...]


async def _execute(
    state: IncidentState,
    ctx: GraphContext,
    request: TestRequest,
    subject: _Subject,
    *,
    now: datetime,
    cycle: int,
) -> _Executed:
    """One test: read, score, record. Never raises -- a failure is an answer with a reason.

    The two failure modes are kept apart at the point they occur, because nothing downstream can
    tell them apart afterwards. `AdapterUnavailableError` means the system could not be reached and
    the incident has a data-quality problem; a plain `AdapterError` means this one call was refused
    and the incident has a reference problem. `ReasonCode` carries which:
    `ADAPTER_UNAVAILABLE` for the first, `DATA_QUALITY_INSUFFICIENT` for the second. Neither is
    `POLICY_EVIDENCE_INSUFFICIENT` -- that code means a policy rule withheld an action for want of
    evidence, which is a decision somebody made, not a call that did not come back.
    """
    spec = SUPPORTED_TESTS.get(request.kind)
    started = now
    if spec is None:
        # `ADAPTER_UNAVAILABLE` rather than a data-quality code: nothing is wrong with the data,
        # there is simply no adapter behind this kind. See `SUPPORTED_TESTS` for the five kinds
        # that have no reader or no scorer -- P08 does not plan them, so arriving here means the
        # plan came from somewhere else.
        return _unavailable(
            request,
            started=started,
            completed=ctx.clock.now(),
            reason=ReasonCode.ADAPTER_UNAVAILABLE,
            summary=(
                f"{request.kind.value} is not a test this system can run; it must have been added "
                "to the plan by something other than P08"
            ),
        )

    try:
        overlay, reading = await spec.reader(ctx, subject)
    except AdapterUnavailableError as exc:
        return _unavailable(
            request,
            started=started,
            completed=ctx.clock.now(),
            reason=ReasonCode.ADAPTER_UNAVAILABLE,
            summary=f"{exc}",
        )
    except AdapterError as exc:
        return _unavailable(
            request,
            started=started,
            completed=ctx.clock.now(),
            reason=ReasonCode.DATA_QUALITY_INSUFFICIENT,
            summary=f"{exc}",
        )

    item = make_evidence(
        state,
        ctx,
        node="execute_read_only_tests",
        kind=spec.kind_of_evidence,
        subject_ref=request.target_ref,
        summary=f"{request.kind.value} run for cycle {cycle}",
        source_system=f"test.{request.kind.value}",
        payload=reading,
        discriminator=f"{request.request_id}#{cycle}",
    )

    verdict = await spec.judge().detect(
        DetectionContext(
            incident_id=subject.incident_id,
            now=now,
            technology=subject.technology,
            cpe=state.get("cpe"),
            topology=state.get("topology"),
            sla=state.get("sla"),
            evidence=[item],
            thresholds=dict(ctx.policy.pack.detector_thresholds),
            **overlay,
        )
    )
    completed = ctx.clock.now()

    if not verdict.ran:
        # The detector declined. `not_applicable` carries no data-quality warnings and means the
        # reading does not apply to this subject, which is inconclusive rather than a defect;
        # anything else means it could not read what it was given.
        applicable = not verdict.data_quality_warnings
        return _Executed(
            result=TestResult(
                result_id=derive_id("RES", subject.incident_id, request.request_id, cycle),
                request_id=request.request_id,
                kind=request.kind,
                target_ref=request.target_ref,
                status=TestStatus.INCONCLUSIVE if applicable else TestStatus.UNAVAILABLE,
                started_at=started,
                completed_at=completed,
                summary=verdict.unavailable_reason,
                evidence_refs=(item.ref,),
                failure_reason=(None if applicable else ReasonCode.DATA_QUALITY_INSUFFICIENT),
            ),
            evidence=(item,),
            findings=(),
        )

    failed = bool(verdict.findings)
    return _Executed(
        result=TestResult(
            result_id=derive_id("RES", subject.incident_id, request.request_id, cycle),
            request_id=request.request_id,
            kind=request.kind,
            target_ref=request.target_ref,
            status=TestStatus.FAILED if failed else TestStatus.PASSED,
            started_at=started,
            completed_at=completed,
            measurements=_measurements(verdict, reading),
            # The numbers each feature was compared against stay in the detector and the pack. A
            # copy here would be a second set that could disagree with the one that produced this
            # verdict, and the disagreement would surface as a result whose summary contradicts its
            # own thresholds.
            thresholds={},
            breached_thresholds=tuple(
                sorted({k for f in verdict.findings for k in f.contributing_features})
            ),
            summary=(
                "; ".join(f.explanation for f in verdict.findings)
                if failed
                else f"{verdict.detector_name} read this and found nothing out of spec"
            ),
            evidence_refs=(item.ref,),
        ),
        evidence=(item,),
        findings=tuple(
            f.model_copy(update={"evidence_refs": (item.ref,)}) for f in verdict.findings
        ),
    )


def _unavailable(
    request: TestRequest,
    *,
    started: datetime,
    completed: datetime,
    reason: ReasonCode,
    summary: str,
) -> _Executed:
    """A test that never produced a reading, with the reason it did not."""
    return _Executed(
        result=TestResult(
            result_id=derive_id("RES", request.request_id, "unavailable"),
            request_id=request.request_id,
            kind=request.kind,
            target_ref=request.target_ref,
            status=TestStatus.UNAVAILABLE,
            started_at=started,
            completed_at=completed,
            summary=summary,
            failure_reason=reason,
        ),
        evidence=(),
        findings=(),
    )


def _measurements(verdict: DetectorResult, reading: Mapping[str, Any]) -> dict[str, float]:
    """The numbers behind the verdict.

    The detector's own `contributing_features` first, because those are the values it computed and
    compared -- a fraction of the sold rate, a margin against a floor -- rather than the raw fields
    it derived them from. A clean result has no features, so the reading's own numeric fields stand
    in: a passing test still has to say what it measured, or "passed" is an assertion with nothing
    behind it.
    """
    features = {k: float(v) for f in verdict.findings for k, v in f.contributing_features.items()}
    if features:
        return features
    numbers: dict[str, float] = {}
    for key, value in reading.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        numbers[key] = float(value)
    return numbers


EVIDENCE_NODES: Sequence[tuple[str, Any]] = (
    ("assemble_case_evidence", assemble_case_evidence),
    ("create_diagnostic_test_plan", create_diagnostic_test_plan),
    ("execute_read_only_tests", execute_read_only_tests),
)

__all__ = [
    "DERIVED_DETECTORS",
    "EVIDENCE_NODES",
    "SUPPORTED_TESTS",
    "assemble_case_evidence",
    "create_diagnostic_test_plan",
    "execute_read_only_tests",
    "live_findings",
]
