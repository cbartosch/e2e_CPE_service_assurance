"""Stage 1: taking custody of a signal and turning it into exactly one incident (P01-P06).

Six nodes, and the shape of the stage is a funnel: each one narrows what the next may assume.

| Node | Question it answers | What the next node may then assume |
| --- | --- | --- |
| P01 | Did a signal actually arrive? | there is an event |
| P02 | What does the event itself say, and how well? | the subject refs and a data-quality score |
| P03 | Who and where is this? | `topology`, `cpe`, a resolved technology |
| P04 | Is it already somebody else's problem? | `linked_records`, correlated evidence |
| P05 | How much does it matter? | `impact` |
| P06 | Which incident is it? | one canonical id, `thread_id == incident_id` |

Two things this stage does *not* do, both deliberate.

**It does not resolve the SLA.** `sla` is `write_once` and `make_initial_state` requires one, so the
entry point has already resolved it -- and it must, because the SLA clock starts when the signal is
received, not when a node gets round to looking. A node that refined it later would raise on the
second write, which is the state contract working as intended.

**It does not write to any external system.** P06 is named "create or attach to one incident" and
creates nothing outside this graph. The incident's identity is `incident_id`, minted at receipt; the
external service-problem record is `subgraphs.plant`'s business, behind a policy check and an
`ActionType` in the allowlist. P06 verifies and links; it does not call TMF. Getting this wrong
would put an unguarded external write in the one node every incident passes through.

Where the specification asks for something no adapter supplies
--------------------------------------------------------------
P04's list of things to compare against includes **planned maintenance**, and there is no
maintenance adapter in `integrations.base` -- so `linked_records["planned_maintenance"]` is never
written and D03 never routes on it. This is recorded here rather than papered over with a heuristic,
because a fabricated maintenance window would suppress real incidents. The key is in
`routing.PARENT_RECORD_KEYS` and the correlation below is written as a table, so wiring a real
source is one fetch and one row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from lpr_cpe.decision_services.delimiter import resolve_topology
from lpr_cpe.decision_services.impact import AffectedService, assess_impact
from lpr_cpe.domain.enums import (
    CaseType,
    DataQualityFlag,
    EventSource,
    EvidenceKind,
    FaultDomain,
    IncidentStatus,
    KPIName,
    ReasonCode,
    Technology,
)
from lpr_cpe.domain.records import AssuranceEvent, CPERecord
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.nodes._runtime import (
    Freshness,
    Gathered,
    NodeUpdate,
    audit,
    emit_kpi,
    make_evidence,
    node,
    preview,
)
from lpr_cpe.graph.routing import PARENT_RECORD_KEYS, PRIOR_INCIDENTS_KEY
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.integrations.base import AdapterUnavailableError
from lpr_cpe.observability.kpi import MetricTimestamp, mark

#: How far back P04 looks for alarms that might make this incident somebody else's.
#:
#: Not in the policy pack, and that is a judgement worth stating: the pack's numbers are ones an
#: operations owner tunes against outcomes, and this one is a property of how long NXT keeps an
#: alarm active rather than of how this business wants to behave. Widening it does not change a
#: decision, it changes how much history the query returns. The change-correlation window below
#: *is* in the pack, because a change three days old being "recent" is a real operational opinion.
ALARM_CORRELATION_WINDOW = timedelta(hours=24)

#: `AssuranceEvent.source` -> the kind of evidence the event constitutes. Exhaustive over
#: `EventSource`: a source with no mapping would silently produce untyped evidence that RCA could
#: not weigh, so the lookup below raises rather than defaulting.
_SOURCE_EVIDENCE_KIND: Mapping[EventSource, EvidenceKind] = {
    EventSource.NXT: EvidenceKind.NXT_ALARM,
    EventSource.CPE_SCAN: EvidenceKind.CPE_STATUS,
    EventSource.CUSTOMER: EvidenceKind.CUSTOMER_STATEMENT,
    EventSource.CRM: EvidenceKind.CUSTOMER_STATEMENT,
    EventSource.FIELD: EvidenceKind.TECHNICIAN_NOTE,
    EventSource.SCHEDULER: EvidenceKind.CPE_STATUS,
    EventSource.WFM: EvidenceKind.TECHNICIAN_NOTE,
    EventSource.JTRACK: EvidenceKind.MR_UPDATE,
    EventSource.MANUAL: EvidenceKind.TECHNICIAN_NOTE,
}

#: A thread id must survive being a Postgres key, a URL path segment and a log field. Checked at P06
#: rather than assumed, because the failure it prevents appears only under the Postgres checkpointer
#: and only for whichever id first contains something exotic.
_SAFE_THREAD_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


# ----------------------------------------------------------------------------------------------
# Shared readers
# ----------------------------------------------------------------------------------------------


def _latest_event(state: IncidentState) -> AssuranceEvent | None:
    """The most recent signal. Not the first: a second event on a live incident is newer news."""
    events = state.get("events", [])
    return events[-1] if events else None


def _subject_ref(state: IncidentState) -> str:
    """The single reference this incident is *about*, most specific first.

    One order, defined once. Evidence refs, audit subjects and the impact assessment all need "what
    is this about?", and three call sites each picking their own precedence is how the same incident
    comes to be filed under three different subjects.
    """
    topology = state.get("topology")
    return (
        state.get("service_ref")
        or state.get("cpe_ref")
        or state.get("customer_ref")
        or (topology.node_ref if topology else None)
        or state.get("incident_id")
        or ""
    )


def _as_technology(value: object, fallback: Technology) -> Technology:
    """A `Technology` from an adapter's string, or the fallback. Never raises on bad input."""
    if isinstance(value, Technology):
        return value
    if isinstance(value, str):
        try:
            return Technology(value)
        except ValueError:
            return fallback
    return fallback


def _cpe_record(payload: Mapping[str, Any]) -> CPERecord | None:
    """A `CPERecord` from an adapter payload, or `None` if it cannot be one.

    The filter is not defensive tidiness. `read_status` returns five keys the record does not
    declare -- `service_ref`, `observed_at`, `source_system`, `subject_ref`, `simulated` (measured)
    -- and `CPERecord` is `extra="forbid"`, so constructing it from the raw payload raises. Those
    five are provenance about the *read*; the record is about the device.
    """
    fields = {k: v for k, v in payload.items() if k in CPERecord.model_fields}
    if not fields.get("cpe_ref"):
        return None
    return CPERecord.model_validate(fields)


def _rows(value: object) -> list[dict[str, Any]]:
    """Adapter list payloads, defensively. A non-list is not rows, and pretending otherwise here
    would push the type error into a node's business logic where it reads as a data problem."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


# ----------------------------------------------------------------------------------------------
# P01 -- receive signal
# ----------------------------------------------------------------------------------------------


@node("receive_signal")
async def receive_signal(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P01. Take custody of the signal and record that we have it.

    Every accepted source -- predictive scan, NXT alarm or trend, network alarm, customer call, app,
    chat, care ticket, technician observation, an update to an existing incident, work order or MR
    -- is already an `AssuranceEvent` by the time it reaches here. Parsing HTTP into a domain object
    is the API's job, and doing it in a node would mean the graph could not be started from a test
    without a webhook payload.

    So what is left for P01 is the thing only P01 can do: say whether anything arrived at all, and
    move the incident out of `NEW`. An empty `events` list is not an exception --
    `route_event_validity` quarantines on it -- so it is recorded as an error and returned, which is
    what lets D01 make the decision rather than a `raise` making it silently.

    `DETECTED_AT` is stamped from `occurred_at`, the instant the *source* saw the condition, not
    from the clock. Stamping "now" would make time-to-detect measure how quickly this graph starts,
    which is a number nobody needs and which would look excellent during an outage.
    """
    event = _latest_event(state)
    if event is None:
        return {
            "errors": [
                {
                    "key": "receive_signal:no-event",
                    "node": "receive_signal",
                    "reason": "no AssuranceEvent in state; nothing to triage",
                }
            ],
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="receive_signal",
                    action="receive_signal",
                    outcome="rejected",
                    reason_code=ReasonCode.DATA_QUALITY_INSUFFICIENT,
                    detail={"reason": "state carried no event"},
                )
            ],
        }

    return {
        "status": IncidentStatus.TRIAGING,
        "audit_events": [
            audit(
                state,
                ctx,
                node="receive_signal",
                action="receive_signal",
                outcome="accepted",
                subject_ref=_subject_ref(state),
                detail={
                    "source": event.source.value,
                    "case_type": event.case_type.value,
                    "vendor_event_type": event.vendor_event_type,
                    "detection_latency_seconds": event.detection_latency.total_seconds(),
                },
            )
        ],
        **mark(MetricTimestamp.DETECTED_AT, event.occurred_at),
    }


# ----------------------------------------------------------------------------------------------
# P02 -- normalize event
# ----------------------------------------------------------------------------------------------


@node("normalize_event")
async def normalize_event(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P02. Score the event, promote what it knows into state, and record it as evidence.

    The specification's list for this step -- source system, source event id, event timestamp,
    receipt timestamp, technology, customer/service/CPE references, resource references,
    measurements, data-quality score, source lineage -- is mostly satisfied by `AssuranceEvent`'s
    own fields, which is what a canonical model is for. Three things are left, and they are this
    node:

    * **The data-quality score.** D01 reads it and quarantines on a blocking flag. It is computed
      here from the event alone, before any adapter has been asked, so an event that is malformed on
      its face never causes a fetch.
    * **Promotion.** A second event arriving on a live incident may carry a reference the first did
      not -- an NXT alarm names a network element, and the customer's call an hour later names the
      service. `make_initial_state` lifted the first event's refs; nothing lifts the second's.
      Promotion only ever *fills a gap*: an event that disagreed with an established subject would
      be a correlation failure, and silently repointing the incident at a different customer is the
      worst available response to one.
    * **Lineage.** The event becomes an `EvidenceItem` so that RCA can cite the thing that started
      the incident, rather than treating the trigger as something outside the evidence.

    Only two defects are flagged, and the ones deliberately *not* flagged matter as much. An unknown
    technology is `MISSING_FIELD`, because P03 needs it to choose a plant adapter. A source clock
    ahead of ours is `CLOCK_SKEW` -- `detection_latency` clamps the negative away so the KPI stays
    honest, and this is the record that it had to. Event *age* is not flagged: a customer reporting
    on Monday a problem that began on Saturday is the normal shape of a customer-reported case, and
    staleness there would quarantine the most common thing this system handles.

    This is also the one node that emits a KPI outside P06, and the branch that follows is why.
    D01's rejection path is required to "generate a data-quality metric" and then quarantines the
    event, so it never reaches P06 where every other KPI is measured -- a metric emitted there would
    exist for exactly the events that did not need it. Emitting on both branches is also what makes
    it a *rate*: the quarantined events are the numerator, and the ones that continue are the rest
    of the denominator.

    Today every emission is a denominator: nothing this node can flag is blocking, so D01 never
    actually quarantines -- gap INTAKE-1. The placement is still the one that survives closing that
    gap, which is the point of putting it here rather than waiting for a real rejection to exist.
    """
    event = _latest_event(state)
    if event is None:
        return {}

    gathered = Gathered(ctx, assessed_at=ctx.clock.now())
    if event.technology is Technology.UNKNOWN:
        gathered.add_flag(
            DataQualityFlag.MISSING_FIELD,
            f"event {event.event_id} carries no technology; the plant adapter cannot be chosen "
            "from it and must be resolved from the service record",
        )
    if event.occurred_at > event.received_at:
        skew = (event.occurred_at - event.received_at).total_seconds()
        gathered.add_flag(
            DataQualityFlag.CLOCK_SKEW,
            f"event {event.event_id} claims to have occurred {skew:.0f}s after it was received; "
            f"{event.source.value}'s clock is ahead of ours and detection latency is clamped to 0",
        )

    promoted: NodeUpdate = {}
    for field, value in (
        ("customer_ref", event.customer_ref),
        ("service_ref", event.service_ref),
        ("cpe_ref", event.cpe_ref),
    ):
        if value and not state.get(field):
            promoted[field] = value
    if state.get("technology", Technology.UNKNOWN) is Technology.UNKNOWN:
        promoted["technology"] = event.technology

    kind = _SOURCE_EVIDENCE_KIND[event.source]
    subject = _subject_ref(preview(state, promoted))
    evidence = make_evidence(
        state,
        ctx,
        node="normalize_event",
        kind=kind,
        subject_ref=subject,
        summary=event.summary,
        source_system=event.source.value,
        observed_at=event.occurred_at,
        payload={
            "event_id": event.event_id,
            "case_type": event.case_type.value,
            "severity": event.severity.value,
            "vendor_event_type": event.vendor_event_type,
            "detail": event.detail,
            "network_element_ref": event.network_element_ref,
            "dedupe_key": event.dedupe_key,
            "received_at": event.received_at.isoformat(),
        },
        discriminator=event.event_id,
    )

    scored: NodeUpdate = {
        **promoted,
        "evidence": [evidence],
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="normalize_event",
                action="normalize_event",
                outcome="normalized",
                subject_ref=subject,
                detail={
                    "event_id": event.event_id,
                    "evidence_kind": kind.value,
                    "promoted": sorted(promoted),
                },
                discriminator=event.event_id,
            )
        ],
    }

    # Measured after folding, because the assessment the KPI reports on is written by this very
    # update. Measuring `state` would report the previous event's score, or -- on the first event,
    # which is the quarantine case the metric exists for -- derive nothing and emit nothing.
    scored["kpi_events"] = emit_kpi(
        preview(state, scored),
        ctx,
        KPIName.DATA_QUALITY_DEFECT_RATE,
        node="normalize_event",
    )
    return scored


# ----------------------------------------------------------------------------------------------
# P03 -- resolve identity and topology
# ----------------------------------------------------------------------------------------------


@node("resolve_identity_and_topology")
async def resolve_identity_and_topology(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P03. Walk customer -> product -> service -> CPE -> premise -> delimiter -> upstream plant.

    The chain is walked in three phases and not one `gather`, because each phase's *arguments* come
    out of the previous one. A signal that names only a CPE has no service reference to look a
    service up by until `read_status` returns one; a service has no customer until TMF says which.
    Within a phase everything is concurrent.

        1. cpe.read_status                 -> service_ref, when the event named only a device
        2. tmf.fetch_service, plant.fetch_topology, cpe.read_status
                                           -> customer_ref, cpe_ref, technology, the plant chain
        3. tmf.fetch_customer              -> vulnerability and priority

    Technology is resolved *before* the plant adapter is chosen, and this is the ordering that makes
    the node work at all: `plant_adapter_for` accepts only `hfc` or `pon`, an event from a care
    system routinely carries `unknown`, and asking it first would fail every customer-reported case.
    So the service record is consulted first and its technology is authoritative -- it is the
    provisioning system's own answer, whereas the event's is whatever the alerting system inferred.

    `resolve_topology` derives the delimiter kind from the technology rather than from the payload,
    so a tap arriving on a PON service becomes `INCONSISTENT_TOPOLOGY` -- blocking, and correctly:
    that combination means the reference chain has crossed two customers, and the next node down
    would be assessing the blast radius of the wrong plant.

    D02 loops back here when identity is still unresolved. The bound on that loop is the node
    re-entry ceiling in `graph.guards`, enforced by the decorator before this body runs, which is
    why there is no retry count in this function.
    """
    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    adapters = ctx.adapters

    service_ref = state.get("service_ref")
    cpe_ref = state.get("cpe_ref")
    customer_ref = state.get("customer_ref")
    technology = state.get("technology", Technology.UNKNOWN)

    # -- phase 1: a device reference is enough to find the service it serves -------------------
    cpe_payload: dict[str, Any] | None = None
    if cpe_ref:
        cpe_payload = await gathered.fetch(
            "cpe.read_status", adapters.cpe.read_status(cpe_ref), freshness=Freshness.TELEMETRY
        )
        if cpe_payload and not service_ref:
            service_ref = str(cpe_payload.get("service_ref") or "") or None

    # -- phase 2: the service is the hub of the chain -------------------------------------------
    service_payload: dict[str, Any] | None = None
    topology_payload: dict[str, Any] | None = None
    if service_ref:
        service_payload = await gathered.fetch(
            "tmf.fetch_service", adapters.tmf.fetch_service(service_ref), freshness=Freshness.SLA
        )
        if service_payload:
            technology = _as_technology(service_payload.get("technology"), technology)
            customer_ref = customer_ref or str(service_payload.get("customer_ref") or "") or None
            cpe_ref = cpe_ref or str(service_payload.get("cpe_ref") or "") or None

        try:
            plant = adapters.plant_adapter_for(technology.value)
        except AdapterUnavailableError as exc:
            plant = None
            gathered.add_flag(
                DataQualityFlag.MISSING_FIELD,
                f"no plant adapter for technology {technology.value!r}: {exc}",
            )
        if plant is not None:
            topology_payload = await gathered.fetch(
                "plant.fetch_topology",
                plant.fetch_topology(service_ref),
                freshness=Freshness.TOPOLOGY,
            )
        # A device named only by the service record still has to be read; phase 1 skipped it.
        if cpe_payload is None and cpe_ref:
            cpe_payload = await gathered.fetch(
                "cpe.read_status", adapters.cpe.read_status(cpe_ref), freshness=Freshness.TELEMETRY
            )

    # -- phase 3: who the customer is changes what we owe them ----------------------------------
    customer_payload: dict[str, Any] | None = None
    if customer_ref:
        customer_payload = await gathered.fetch(
            "tmf.fetch_customer",
            adapters.tmf.fetch_customer(customer_ref),
            freshness=Freshness.SLA,
        )

    resolved = resolve_topology(topology_payload, technology=technology, resolved_at=now)
    for flag in resolved.flags:
        gathered.add_flag(flag)
    for note in resolved.notes:
        gathered.add_note(f"topology: {note}")

    cpe_record = _cpe_record(cpe_payload) if cpe_payload else None
    product_ref = None
    if service_payload:
        product_ref = str(service_payload.get("serviceSpecification") or "") or None

    evidence = [
        make_evidence(
            state,
            ctx,
            node="resolve_identity_and_topology",
            kind=EvidenceKind.TOPOLOGY_LOOKUP,
            subject_ref=service_ref or _subject_ref(state),
            summary=(
                f"{technology.value} chain resolved to "
                f"{resolved.context.delimiter_kind.value} {resolved.context.delimiter_ref or '?'}"
            ),
            source_system=str((topology_payload or {}).get("source_system") or "topology"),
            payload=dict(topology_payload or {}),
            discriminator=state.get("diagnostic_cycles", 0),
        )
    ]
    if cpe_payload:
        evidence.append(
            make_evidence(
                state,
                ctx,
                node="resolve_identity_and_topology",
                kind=EvidenceKind.CPE_STATUS,
                subject_ref=cpe_ref or "",
                summary=(
                    f"CPE {cpe_ref} reported "
                    f"{'online' if cpe_payload.get('online') else 'offline'} at identity resolution"
                ),
                source_system=str(cpe_payload.get("source_system") or "cpe"),
                payload=cpe_payload,
                discriminator=state.get("diagnostic_cycles", 0),
            )
        )

    update: NodeUpdate = {
        "technology": technology,
        "topology": resolved.context,
        "delimiter": resolved.context.delimiter_kind,
        "delimiter_ref": resolved.context.delimiter_ref,
        "evidence": evidence,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="resolve_identity_and_topology",
                action="resolve_identity",
                outcome="resolved" if resolved.context.delimiter_ref else "unresolved",
                subject_ref=service_ref or _subject_ref(state),
                detail={
                    "technology": technology.value,
                    "delimiter_kind": resolved.context.delimiter_kind.value,
                    "delimiter_ref": resolved.context.delimiter_ref,
                    "sources_usable": gathered.usable_sources,
                    "completeness": round(gathered.completeness_score, 3),
                },
                discriminator=state.get("node_visits", {}).get("resolve_identity_and_topology", 0),
            )
        ],
    }
    if service_ref:
        update["service_ref"] = service_ref
    if cpe_ref:
        update["cpe_ref"] = cpe_ref
    if customer_ref:
        update["customer_ref"] = customer_ref
    if product_ref:
        update["product_ref"] = product_ref
    if cpe_record is not None:
        update["cpe"] = cpe_record
    if customer_payload is not None:
        update["linked_records"] = {
            key: str(customer_payload[key])
            for key in ("customer_ref",)
            if customer_payload.get(key)
        }
    return update


# ----------------------------------------------------------------------------------------------
# P04 -- deduplicate and correlate
# ----------------------------------------------------------------------------------------------


@node("deduplicate_and_correlate")
async def deduplicate_and_correlate(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P04. Ask whether this event already belongs to something larger.

    D03 reads exactly one thing: whether any `routing.PARENT_RECORD_KEYS` entry is set in
    `linked_records`. So this node's whole job is to establish those honestly, and the mapping from
    evidence to key is where the judgement sits:

    * `parent_incident` -- an uncleared alarm on the node or OLT *above* this service. One plant
      element down is one incident, however many customers report it separately.
    * `outage` -- a commercial-power event at the premises location. The network is fine; the power
      is not, and no truck roll fixes that.
    * `service_problem` -- an open jTrack MR on this delimiter. The plant fault is already recorded
      and somebody is already working it.
    * `planned_maintenance` -- nothing establishes this. No adapter supplies it; see the module
      docstring.

    Everything else the specification lists to compare against -- recent configuration changes,
    weather, neighbouring CPE symptoms -- is *correlating* evidence rather than a parent, and is
    recorded as evidence for the detectors and RCA to weigh. The distinction is what stops a windy
    afternoon from suppressing every incident in the region.

    Neighbouring symptoms are read from alarms on the same delimiter, which is the only source here
    that names *which* peers are degraded. The delimiter view (`fetch_tap_view` / `fetch_odp_view`)
    counts them but does not name them, and P05 needs references. This is also why peers arrive with
    `vulnerable`, `priority` and `business` all false: an alarm row says a service is degraded and
    nothing about who holds it. Establishing that would mean a TMF lookup per peer -- the
    per-customer fan-out `AffectedService` exists to avoid -- and it under-counts rather than
    over-counts, so the error runs towards treating an incident as less special than it is.
    """
    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    adapters = ctx.adapters
    topology = state.get("topology")
    service_ref = state.get("service_ref")
    delimiter_ref = state.get("delimiter_ref") or (topology.delimiter_ref if topology else None)
    customer_ref = state.get("customer_ref")

    change_window_hours = float(
        ctx.policy.pack.detector_thresholds.get("change.correlation_window_hours", 72.0)
    )
    plant_refs = [
        ref
        for ref in (
            delimiter_ref,
            topology.node_ref if topology else None,
            topology.olt_ref if topology else None,
            topology.pon_port_ref if topology else None,
        )
        if ref
    ]

    calls: dict[str, Any] = {
        "nxt.alarms": adapters.nxt.fetch_alarms(since=now - ALARM_CORRELATION_WINDOW),
    }
    if plant_refs:
        calls["inventory.recent_changes"] = adapters.inventory.fetch_recent_changes(
            object_refs=plant_refs, since=now - timedelta(hours=change_window_hours)
        )
    if delimiter_ref:
        calls["jtrack.open_mrs"] = adapters.jtrack.fetch_open_mrs(delimiter_ref)
    if service_ref:
        calls["gis.location"] = adapters.gis.fetch_location(service_ref)
    if customer_ref:
        calls["tmf.customer"] = adapters.tmf.fetch_customer(customer_ref)

    payloads = await gathered.gather(calls)

    # Location is needed before power and weather can be asked for, so they are a second wave.
    location = payloads.get("gis.location") or {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if isinstance(latitude, int | float) and isinstance(longitude, int | float):
        payloads |= await gathered.gather(
            {
                "gis.power_outages": adapters.gis.fetch_power_outages(
                    latitude=float(latitude), longitude=float(longitude), radius_km=2.0
                ),
                "gis.weather": adapters.gis.fetch_weather(
                    latitude=float(latitude), longitude=float(longitude), at=now
                ),
            }
        )

    alarms = _rows(payloads.get("nxt.alarms"))
    changes = _rows(payloads.get("inventory.recent_changes"))
    open_mrs = _rows(payloads.get("jtrack.open_mrs"))
    outages = _rows(payloads.get("gis.power_outages"))

    linked: dict[str, str] = {}
    evidence = []
    upstream_refs = {ref for ref in plant_refs if ref != delimiter_ref}

    # -- a parent alarm on the plant element above this service ---------------------------------
    parent_alarms = [
        row
        for row in alarms
        if str(row.get("network_element_ref") or "") in upstream_refs
        and not row.get("cleared_at")
        and str(row.get("service_ref") or "") != (service_ref or "")
    ]
    if parent_alarms:
        parent = parent_alarms[0]
        linked["parent_incident"] = str(parent.get("alarm_id") or "")

    # -- a commercial-power event covering the premises ------------------------------------------
    if outages:
        linked["outage"] = str(outages[0].get("outage_id") or outages[0].get("ref") or "power")

    # -- an open MR means the plant fault is already recorded and being worked --------------------
    if open_mrs:
        linked["service_problem"] = str(open_mrs[0].get("mr_id") or open_mrs[0].get("ref") or "")

    # -- prior tickets on this customer, for D24's chronic-pattern check --------------------------
    customer = payloads.get("tmf.customer") or {}
    prior = [str(ref) for ref in (customer.get("open_ticket_refs") or []) if ref]
    if prior:
        linked[PRIOR_INCIDENTS_KEY] = ",".join(sorted(prior))

    # -- correlating evidence: neighbours, changes, power ------------------------------------------
    peer_alarms = [
        row
        for row in alarms
        if delimiter_ref
        and str(row.get("delimiter_ref") or "") == delimiter_ref
        and str(row.get("service_ref") or "") not in {"", service_ref or ""}
        and not row.get("cleared_at")
    ]
    for row in peer_alarms:
        evidence.append(
            make_evidence(
                state,
                ctx,
                node="deduplicate_and_correlate",
                kind=EvidenceKind.NXT_ALARM,
                subject_ref=str(row.get("service_ref") or ""),
                summary=(
                    f"neighbour {row.get('service_ref')} behind {delimiter_ref} is alarming: "
                    f"{row.get('alarm_type')} ({row.get('severity')})"
                ),
                source_system=str(row.get("source_system") or "nxt"),
                payload=row,
                observed_at=None,
                discriminator=str(row.get("alarm_id") or ""),
            )
        )
    for row in changes:
        evidence.append(
            make_evidence(
                state,
                ctx,
                node="deduplicate_and_correlate",
                kind=EvidenceKind.CHANGE_RECORD,
                subject_ref=str(row.get("object_ref") or delimiter_ref or ""),
                summary=(
                    f"{row.get('change_type', 'change')} on {row.get('object_ref')} "
                    f"within {change_window_hours:.0f}h of this event"
                ),
                source_system=str(row.get("source_system") or "inventory"),
                payload=row,
                discriminator=str(row.get("change_ref") or row.get("ref") or ""),
            )
        )
    for row in outages:
        evidence.append(
            make_evidence(
                state,
                ctx,
                node="deduplicate_and_correlate",
                kind=EvidenceKind.POWER_OUTAGE_REPORT,
                subject_ref=service_ref or _subject_ref(state),
                summary=(
                    f"commercial power event near the premises: {row.get('status', 'reported')}"
                ),
                source_system=str(row.get("source_system") or "gis"),
                payload=row,
                discriminator=str(row.get("outage_id") or row.get("ref") or ""),
            )
        )

    outcome = "associated" if any(linked.get(k) for k in PARENT_RECORD_KEYS) else "new_candidate"
    update: NodeUpdate = {
        "evidence": evidence,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="deduplicate_and_correlate",
                action="correlate",
                outcome=outcome,
                subject_ref=_subject_ref(state),
                reason_code=(
                    ReasonCode.POLICY_DUPLICATE_SUPPRESSED
                    if outcome == "associated"
                    else ReasonCode.COMMON_CAUSE_CLUSTER
                    if peer_alarms
                    else None
                ),
                detail={
                    "linked": {k: v for k, v in linked.items() if k in PARENT_RECORD_KEYS},
                    "peer_alarms": len(peer_alarms),
                    "recent_changes": len(changes),
                    "power_events": len(outages),
                    "open_mrs": len(open_mrs),
                },
                discriminator=state.get("node_visits", {}).get("deduplicate_and_correlate", 0),
            )
        ],
    }
    if linked:
        update["linked_records"] = linked
    return update


# ----------------------------------------------------------------------------------------------
# P05 -- assess impact and priority
# ----------------------------------------------------------------------------------------------


def _peers_from_evidence(state: IncidentState, delimiter_ref: str | None) -> list[AffectedService]:
    """The neighbours P04 recorded, read back as impact inputs.

    Read from `evidence` rather than passed directly because P04 and P05 are separate super-steps
    with a checkpoint between them: anything P05 needs has to have survived serialisation, and
    `evidence` is the append-only place that facts survive in. A `peers` field in `IncidentState`
    would be a second home for the same observation.
    """
    peers: dict[str, AffectedService] = {}
    for item in state.get("evidence", []):
        if item.kind is not EvidenceKind.NXT_ALARM or not item.subject_ref:
            continue
        if item.subject_ref == state.get("service_ref"):
            continue
        row_delimiter = str(item.payload.get("delimiter_ref") or "") or None
        if delimiter_ref is not None and row_delimiter != delimiter_ref:
            continue
        peers.setdefault(
            item.subject_ref,
            AffectedService(service_ref=item.subject_ref, delimiter_ref=row_delimiter),
        )
    return list(peers.values())


@node("assess_impact_and_priority")
async def assess_impact_and_priority(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P05. How many, how important, how urgent -- and whether this is risk or an outage.

    The arithmetic is `decision_services.impact`'s, not this node's, which is what keeps the same
    numbers coming out here and at the high-blast-radius approval gate. What P05 owns is the
    *inputs*: who the subject is, what the customer record says about them, and which neighbours P04
    actually found.

    `peers` is passed as a list and never as `None` once P04 has run, and the difference is
    load-bearing in `assess_impact`: `None` means correlation never happened and the count falls
    back to the plant element's population with a note saying so, while `[]` means it ran and found
    nobody, which is positive evidence for a fault confined to this premises. P04 always runs before
    P05 in the parent graph, so `[]` here is the truthful answer.

    The fault domain is still `UNKNOWN` at this point -- P10 has not run -- and that is passed
    through honestly rather than guessed. `impact_radius` widens the blast radius for a
    plant-domain fault, so guessing one here would inflate the count of an incident that turns out
    to be a single CPE, and D14's high-blast-radius approval would fire on a number nothing
    measured. P05 measures what is known now; the fault domain arrives later and D09 re-reads impact
    when it does.
    """
    now = ctx.clock.now()
    gathered = Gathered(ctx, assessed_at=now)
    topology = state.get("topology")
    service_ref = state.get("service_ref") or ""
    customer_ref = state.get("customer_ref")
    sla = state.get("sla")

    customer_payload: dict[str, Any] | None = None
    if customer_ref:
        customer_payload = await gathered.fetch(
            "tmf.fetch_customer",
            ctx.adapters.tmf.fetch_customer(customer_ref),
            freshness=Freshness.SLA,
        )
    customer = customer_payload or {}

    subject = AffectedService(
        service_ref=service_ref,
        delimiter_ref=state.get("delimiter_ref"),
        vulnerable=bool(customer.get("vulnerable_customer") or (sla and sla.vulnerable_customer)),
        priority=bool(customer.get("priority_customer") or (sla and sla.priority_customer)),
        business=bool(sla and sla.product_tier and sla.product_tier != "residential"),
        sla_at_risk=bool(sla and sla.at_risk(now)),
    )
    peers = _peers_from_evidence(state, state.get("delimiter_ref"))

    impact = assess_impact(
        assessed_at=now,
        subject=subject,
        fault_domain=state.get("fault_domain", FaultDomain.UNKNOWN),
        topology=topology,
        policy=ctx.policy.pack.blast_radius,
        findings=state.get("anomaly_findings", []),
        peers=peers,
    )

    return {
        "impact": impact,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="assess_impact_and_priority",
                action="assess_impact",
                outcome=impact.severity.value,
                subject_ref=service_ref or _subject_ref(state),
                detail={
                    "affected_customer_count": impact.affected_customer_count,
                    "count_is_estimated": impact.count_is_estimated,
                    "estimation_basis": impact.estimation_basis,
                    "blast_radius_scope": impact.blast_radius_scope,
                    "vulnerable_affected": impact.vulnerable_customers_affected,
                    "peers_named": len(peers),
                },
                discriminator=state.get("node_visits", {}).get("assess_impact_and_priority", 0),
            )
        ],
    }


# ----------------------------------------------------------------------------------------------
# P06 -- create or attach to one incident
# ----------------------------------------------------------------------------------------------


def _thread_id_defect(incident_id: str | None, thread_id: str | None) -> str | None:
    """Why this incident's identity is unusable, or `None` if it is fine.

    Three checks, and each one fails somewhere different if skipped. Absence fails at the first
    checkpoint write. Divergence from `incident_id` fails nowhere at all -- the graph runs, and the
    incident is simply unfindable by its own id, which is the failure that would survive to
    production. An unsafe character fails only under the Postgres checkpointer, only for that id.
    """
    if not incident_id:
        return "incident has no incident_id"
    if thread_id != incident_id:
        return f"thread_id {thread_id!r} does not equal incident_id {incident_id!r} (D1)"
    if not _SAFE_THREAD_ID.match(incident_id):
        return (
            f"incident_id {incident_id!r} is not safe as a persistence key: expected "
            f"{_SAFE_THREAD_ID.pattern}"
        )
    return None


@node("create_or_attach_incident")
async def create_or_attach_incident(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P06. Settle the incident's identity and close out triage.

    "Create or update the canonical incident" is, in a graph whose state *is* the incident, a
    statement about identity rather than about a write. The record exists from
    `make_initial_state`; what has to be true before anything acts on it is that its identity is one
    thing, is the LangGraph thread key, and can be persisted -- so this node verifies rather than
    creates. `thread_id = incident_id` is the specification's D1 and is set in exactly one place;
    this is the one place it is checked, and a violation escalates instead of raising, because an
    incident with a broken key still needs a human to see it.

    The KPI emissions are here because P06 is a stage boundary and there is no interrupt anywhere in
    P01-P11 -- so the node cannot replay mid-way and double-record. `emit_kpi` derives a stable
    event id in any case, which is what makes the same call safe to copy into a subgraph that does
    interrupt.
    """
    incident_id = state.get("incident_id")
    defect = _thread_id_defect(incident_id, state.get("thread_id"))
    if defect is not None:
        return {
            "escalated": True,
            "escalation_reason": f"incident identity is unusable: {defect}",
            "status": IncidentStatus.ESCALATED,
            "errors": [
                {"key": "create_or_attach_incident:identity", "reason": defect},
            ],
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="create_or_attach_incident",
                    action="verify_incident_identity",
                    outcome="escalated",
                    reason_code=ReasonCode.DATA_QUALITY_INSUFFICIENT,
                    detail={"defect": defect},
                )
            ],
        }

    now = ctx.clock.now()
    linked = {"canonical_incident": incident_id or ""}
    attached = [key for key in PARENT_RECORD_KEYS if state.get("linked_records", {}).get(key)]
    case_type = state.get("case_type", CaseType.CUSTOMER_REPORTED)

    triaged: NodeUpdate = {
        "linked_records": linked,
        "audit_events": [
            audit(
                state,
                ctx,
                node="create_or_attach_incident",
                action="create_or_attach_incident",
                outcome="attached" if attached else "created",
                subject_ref=_subject_ref(state),
                detail={
                    "incident_id": incident_id,
                    "thread_id": state.get("thread_id"),
                    "case_type": case_type.value,
                    "attached_to": attached,
                },
            )
        ],
        **mark(MetricTimestamp.TRIAGED_AT, now),
    }

    # `mark` returns a `metrics_timestamps` mapping and `time_to_triage` reads it, so the KPIs are
    # computed from the state as the reducers will leave it rather than as it was on entry.
    measured = preview(state, triaged)
    kpi_events = [
        event
        for kpi in (
            KPIName.INCIDENTS_CREATED,
            KPIName.PROACTIVE_DETECTION_RATE,
            KPIName.TIME_TO_DETECT_SECONDS,
            KPIName.TIME_TO_TRIAGE_SECONDS,
        )
        for event in emit_kpi(measured, ctx, kpi, node="create_or_attach_incident")
    ]
    triaged["kpi_events"] = kpi_events
    return triaged


INTAKE_NODES: Sequence[tuple[str, Any]] = (
    ("receive_signal", receive_signal),
    ("normalize_event", normalize_event),
    ("resolve_identity_and_topology", resolve_identity_and_topology),
    ("deduplicate_and_correlate", deduplicate_and_correlate),
    ("assess_impact_and_priority", assess_impact_and_priority),
    ("create_or_attach_incident", create_or_attach_incident),
)


__all__ = [
    "ALARM_CORRELATION_WINDOW",
    "INTAKE_NODES",
    "assess_impact_and_priority",
    "create_or_attach_incident",
    "deduplicate_and_correlate",
    "normalize_event",
    "receive_signal",
    "resolve_identity_and_topology",
]
