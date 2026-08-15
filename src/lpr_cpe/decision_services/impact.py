"""Who is affected, how badly, and how severe that makes the incident.

`ImpactAssessment` is the dispatch priority input and the high-blast-radius approval input, so the
two things this module must not do are guess and double-count.

**Guessing** is handled by delegating the count to `decision_services.blast_radius`, which returns
its own provenance; `count_is_estimated` and `estimation_basis` are copied from there rather than
decided here.

**Double-counting** is the reason severity is *not* raised for a vulnerable customer. Vulnerability
already acts twice: `SLAPolicy._effective` tightens the clock by a band, and
`customer_contact.vulnerable_customer_priority_boost` raises the dispatch priority. Raising severity
as well would tighten the clock a second time -- severity is the key into `sla.response_minutes` --
and the same attribute would have moved the deadline twice through two different code paths, which
is close to impossible to see in a review of either one. So `vulnerable_customers_affected` is
reported, and the severity is the fault's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from lpr_cpe.decision_services.blast_radius import impact_radius
from lpr_cpe.domain.diagnosis import AnomalyFinding, ImpactAssessment
from lpr_cpe.domain.enums import FaultDomain, Severity
from lpr_cpe.domain.records import TopologyContext
from lpr_cpe.policies.models import BlastRadiusPolicy


@dataclass(frozen=True, slots=True)
class AffectedService:
    """One service known to be affected, and the attributes that change what we owe it.

    A flat record rather than a reference into the customer store, because impact is assessed inside
    a graph node that must not fan out into per-customer lookups: the caller gathers these once from
    the correlation result and hands them over. It carries no name, address or contact detail --
    `security.redaction` keeps those out of state, and there is no field here that could hold one.
    """

    service_ref: str
    delimiter_ref: str | None = None
    vulnerable: bool = False
    priority: bool = False
    business: bool = False
    sla_at_risk: bool = False


def assess_impact(
    *,
    assessed_at: datetime,
    subject: AffectedService,
    fault_domain: FaultDomain,
    topology: TopologyContext | None,
    policy: BlastRadiusPolicy,
    findings: Sequence[AnomalyFinding] = (),
    peers: Sequence[AffectedService] | None = None,
    monthly_revenue_per_service: float | None = None,
) -> ImpactAssessment:
    """Assemble the assessment.

    `peers` is the other services correlation found with the same symptom, and `None` is not `()`.
    `None` means correlation has not run, so the only thing known is the subject and the count falls
    to the plant element's population. `()` means correlation ran and found nobody else, which is
    positive evidence that the fault is confined to this premises -- and the count still falls to
    the element's population, because five of a tap's eight customers not having called yet is the
    normal state of a tap fault at 09:00. What `()` changes is the note, so a reviewer can tell a
    quiet morning from a correlation query that never ran.

    `monthly_revenue_per_service` is optional and stays `None` when absent rather than becoming
    `0.0`. A revenue-at-risk of zero on an outage would be read as an outage that costs nothing.
    """
    known = [subject, *(peers or [])]
    seen: dict[str, AffectedService] = {}
    for service in known:
        if service.service_ref and service.service_ref not in seen:
            seen[service.service_ref] = service
    services = list(seen.values())

    radius = impact_radius(
        topology,
        fault_domain=fault_domain,
        policy=policy,
        # The subject counts as one observation of the symptom -- it is the service that reported.
        corroborating_services=None if peers is None else len(services),
    )

    notes: list[str] = list(radius.notes)
    if peers is None:
        notes.append(
            "correlation has not run, so only the reporting service is known by name; the count is "
            "the plant element's population"
        )
    elif len(services) < radius.count:
        notes.append(
            f"{len(services)} of the {radius.count} affected services are known by reference; the "
            "rest are behind the same plant element and have not reported"
        )

    severity = incident_severity(findings, affected_count=radius.count, policy=policy)

    revenue: float | None = None
    if monthly_revenue_per_service is not None:
        revenue = round(monthly_revenue_per_service * radius.count, 2)

    delimiters = sorted(
        {s.delimiter_ref for s in services if s.delimiter_ref}
        | ({topology.delimiter_ref} if topology and topology.delimiter_ref else set())
    )

    return ImpactAssessment(
        assessed_at=assessed_at,
        affected_customer_count=radius.count,
        count_is_estimated=radius.estimated,
        estimation_basis=radius.basis,
        affected_service_refs=sorted(seen),
        affected_delimiter_refs=delimiters,
        blast_radius_scope=radius.scope.value,
        severity=severity,
        vulnerable_customers_affected=sum(1 for s in services if s.vulnerable),
        priority_customers_affected=sum(1 for s in services if s.priority),
        business_customers_affected=sum(1 for s in services if s.business),
        mdu_affected=bool(topology and topology.mdu_ref),
        sla_at_risk_count=sum(1 for s in services if s.sla_at_risk),
        revenue_at_risk=revenue,
        notes=notes,
    )


def incident_severity(
    findings: Sequence[AnomalyFinding],
    *,
    affected_count: int,
    policy: BlastRadiusPolicy,
) -> Severity:
    """The incident's severity: the worst thing detected, or the scale of it, whichever is higher.

    Two independent floors, because either alone gets a real case wrong:

    * **What was detected.** The highest severity any detector assigned. A single-premises fibre cut
      is critical at a count of one, and a scale-only rule would file it as low.
    * **How many it reaches.** At `blast_radius.common_cause_threshold` services the incident is a
      cluster, and individual truck rolls start being actively wrong -- five technicians each
      finding nothing at five houses. At `network_action_threshold` it is a plant event. Detector
      severity alone would leave a node outage at whatever severity one modem's symptoms scored,
      which is how an outage gets a four-hour residential clock.

    The scale floor applies to an estimated count as well as a measured one. Making it conditional
    on measurement would mean the same tap fault is `HIGH` where inventory has homes-behind data and
    `MEDIUM` where it does not, which makes severity a report on our record-keeping rather than on
    the fault. `ImpactAssessment.count_is_estimated` carries the caveat instead.
    """
    detected = max((f.severity.rank() for f in findings), default=Severity.INFO.rank())
    if affected_count >= policy.network_action_threshold:
        scale = Severity.CRITICAL.rank()
    elif affected_count >= policy.common_cause_threshold:
        scale = Severity.HIGH.rank()
    else:
        scale = Severity.INFO.rank()
    return Severity.from_rank(max(detected, scale))


def is_common_cause(
    *,
    affected_count: int,
    peers_behind_delimiter: int | None,
    policy: BlastRadiusPolicy,
) -> bool:
    """Whether this is one fault seen many times, rather than many faults.

    The pack states the rule twice over because two different shapes of evidence reach it.
    `common_cause_threshold` is an absolute count: three services with the same symptom is a
    cluster whatever they sit behind. `common_cause_peer_fraction` is relative: two of a four-way
    tap's customers is half the tap and points at the tap, while two of a 2000-home OLT's is two
    unrelated faults. Either being met is enough -- they catch different plant.

    `peers_behind_delimiter` is `None` when the delimiter's population is unknown, and then only the
    absolute rule can fire. Substituting the pack's default tap size here would let a *default*
    decide whether a truck roll is suppressed.
    """
    if affected_count >= policy.common_cause_threshold:
        return True
    if peers_behind_delimiter:
        return affected_count / peers_behind_delimiter >= policy.common_cause_peer_fraction
    return False


__all__ = ["AffectedService", "assess_impact", "incident_severity", "is_common_cause"]
