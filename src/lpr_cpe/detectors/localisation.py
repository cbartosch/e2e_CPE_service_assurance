"""The two detectors that decide *where* the fault is: domain classifier and delimiter localiser.

Both read `context.prior` rather than telemetry, which is why the distinction between "the earlier
detectors have not run" and "they ran and were clean" is load-bearing here specifically. A
classifier that cannot tell those apart will report `NO_FAULT_FOUND` for an incident nobody
examined -- and `NO_FAULT_FOUND` is a closure reason, so that confusion closes live faults.
"""

from __future__ import annotations

from collections.abc import Iterable

from lpr_cpe.detectors.base import BaseDetector, DetectionContext, DetectorResult
from lpr_cpe.domain.diagnosis import AnomalyFinding
from lpr_cpe.domain.enums import (
    DataQualityFlag,
    FaultDomain,
    Severity,
    TestKind,
)

#: How far out each domain sits. Used to pick the outermost credible domain when several are
#: suspected: a shared plant fault explains the drop symptoms beneath it, but a drop fault does not
#: explain the neighbours', so the outer one is the better single answer.
_DOMAIN_REACH: dict[FaultDomain, int] = {
    FaultDomain.CUSTOMER_ENVIRONMENT: 0,
    FaultDomain.INSIDE_HOME_WIRING: 1,
    FaultDomain.CPE: 2,
    FaultDomain.PROVISIONING: 3,
    FaultDomain.SERVICE_PLATFORM: 4,
    FaultDomain.DROP: 5,
    FaultDomain.TAP_OR_ODP: 6,
    FaultDomain.DISTRIBUTION: 7,
    FaultDomain.FEEDER: 8,
    FaultDomain.NODE_OR_OLT: 9,
    FaultDomain.HEADEND_OR_CO: 10,
    FaultDomain.POWER: 11,
}


def domain_weights(findings: Iterable[AnomalyFinding]) -> dict[FaultDomain, float]:
    """How strongly the evidence points at each domain: sum of `score * confidence` per suspicion.

    Module-level and public because `decision_services.rca` needs the same numbers to set hypothesis
    posteriors, and the alternative -- multiplying score by confidence again over there -- would be
    a second formula for the strength of the evidence. The two would agree today and diverge the
    first time either was tuned, and the symptom would be an RCA whose leading hypothesis is not the
    domain the classifier chose. `RCAResult._primary_is_a_live_hypothesis_in_the_stated_domain`
    would turn that into a validation error inside an incident.

    A plain dict rather than a `Counter`: `Counter` is `dict[T, int]` and these weights are products
    of two floats, so accumulating into it would truncate every suspicion below 1.0 to zero.
    """
    weights: dict[FaultDomain, float] = {}
    for finding in findings:
        domain = finding.suspected_domain
        if domain is None:
            continue
        weights[domain] = weights.get(domain, 0.0) + finding.score * finding.confidence
    return weights


class FaultDomainClassifier(BaseDetector):
    """Fold the other detectors' suspicions into one domain, with the runners-up kept.

    Power wins outright when it is present. Everything else is a network hypothesis, and none of
    them are worth testing while the customer has no electricity -- so this is a short-circuit
    rather than a weight.
    """

    name = "fault_domain_classifier"
    version = "1.0.0"
    #: Its finding *is* the other detectors' findings, folded. Anything that counted it as evidence
    #: alongside them would give the winning domain a second vote cast by the count of the first.
    derives_from_prior = True

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        if context.prior is None:
            # The one thing this detector must never do is guess. Without the other detectors'
            # output there is no evidence to classify, and "no fault found" would be a lie.
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "no earlier detector results supplied, so there is nothing to classify",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        findings = context.findings_from()
        ran = [r for r in context.prior if r.ran]
        if not ran:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "every earlier detector was unavailable; the evidence base is empty",
                flags=[DataQualityFlag.ADAPTER_UNAVAILABLE],
            )

        if not findings:
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.1,
                        confidence=0.75,
                        severity=Severity.INFO,
                        explanation=(
                            f"{len(ran)} detector(s) ran and none found an anomaly. On the "
                            "evidence gathered this is no-fault-found rather than an unlocated "
                            "fault."
                        ),
                        affected=(),
                        features={"detectors_ran": float(len(ran))},
                        recommended_tests=(),
                        suspected_domain=FaultDomain.NO_FAULT_FOUND,
                    )
                ]
            )

        # Weight each suspicion by the strength of the finding that raised it, so a confident
        # optical breach outranks a speculative one rather than each counting as one vote.
        weights = domain_weights(findings)

        if not weights:
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.5,
                        confidence=0.4,
                        severity=Severity.MEDIUM,
                        explanation=(
                            f"{len(findings)} finding(s) but none names a fault domain, so the "
                            "location is genuinely unknown rather than agreed."
                        ),
                        affected=(),
                        features={"findings": float(len(findings))},
                        recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                        suspected_domain=FaultDomain.UNKNOWN,
                    )
                ]
            )

        if FaultDomain.POWER in weights:
            leader, margin, runners = FaultDomain.POWER, 1.0, []
        else:
            ranked = sorted(
                weights.items(),
                # Weight first, then reach outward, then the name: fully deterministic, because
                # two identical inputs must classify identically or the audit trail is worthless.
                key=lambda kv: (-kv[1], -_DOMAIN_REACH.get(kv[0], 0), kv[0].value),
            )
            leader = ranked[0][0]
            runners = [d for d, _ in ranked[1:3]]
            margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)

        total = sum(weights.values()) or 1.0
        share = weights[leader] / total
        contested = len(weights) > 1 and margin < context.threshold(
            "classifier.decisive_margin", 0.15
        )
        also = (
            f" Also suspected: {', '.join(d.value for d in runners)}."
            if runners
            else ""
        )
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(min(1.0, 0.4 + share * 0.6), 4),
                    # A contested classification is reported as contested rather than as a
                    # confident pick, so the policy engine's low-confidence interrupt can fire
                    # instead of a truck being sent on a coin toss.
                    confidence=round(0.45 if contested else min(0.9, 0.55 + share * 0.4), 4),
                    severity=Severity.HIGH if share >= 0.6 else Severity.MEDIUM,
                    explanation=(
                        f"Fault domain classified as {leader.value} on {share:.0%} of the weighted "
                        f"evidence from {len(findings)} finding(s)."
                        + (
                            " The leading domains are close, so this is not decisive."
                            if contested
                            else ""
                        )
                        + also
                    ),
                    affected=(),
                    features={
                        "domain_share": round(share, 4),
                        "domain_margin": round(margin, 4),
                        "findings": float(len(findings)),
                    },
                    recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                    suspected_domain=leader,
                )
            ]
        )


class DelimiterLocaliser(BaseDetector):
    """Which tap or ODP the fault sits behind, and whether it is actually above it.

    Two or more degraded delimiters under one node is not a delimiter fault at all -- it is the
    parent. Escalating in that case is what stops a crew being sent to replace a tap that is
    working correctly.
    """

    name = "delimiter_localiser"
    version = "1.0.0"
    requires = ("plant",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        plant = context.payload("plant")
        parent = plant.get("port") if isinstance(plant.get("port"), dict) else plant.get("node")
        delimiter = plant.get("delimiter") if isinstance(plant.get("delimiter"), dict) else None
        if not isinstance(parent, dict) and delimiter is None:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "neither a parent nor a delimiter view was fetched",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        by_delimiter: dict[str, float] = {}
        if isinstance(parent, dict):
            raw = parent.get("degraded_by_delimiter")
            if isinstance(raw, dict):
                by_delimiter = {
                    str(k): float(v) for k, v in raw.items() if isinstance(v, int | float)
                }

        degraded_delimiters = {k: v for k, v in by_delimiter.items() if v > 0}
        parent_ref = ""
        if isinstance(parent, dict):
            parent_ref = str(
                parent.get("node_ref") or parent.get("pon_port_ref") or parent.get("olt_ref") or ""
            )

        if len(degraded_delimiters) >= 2:
            spread = sum(degraded_delimiters.values())
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.85,
                        confidence=0.8,
                        severity=Severity.HIGH,
                        explanation=(
                            f"{len(degraded_delimiters)} delimiters under {parent_ref} are "
                            f"degraded ({spread:g} services in total). A fault common to several "
                            "delimiters sits above all of them, so this localises to the parent, "
                            "not to any one tap or ODP."
                        ),
                        affected=(parent_ref, *sorted(degraded_delimiters)),
                        features={
                            "degraded_delimiters": float(len(degraded_delimiters)),
                            "degraded_services": spread,
                        },
                        recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                        suspected_domain=FaultDomain.NODE_OR_OLT,
                        suspected_delimiter_ref=None,
                    )
                ]
            )

        if delimiter is None:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "no delimiter view, and the parent shows no degraded delimiters to localise to",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        delimiter_ref = str(delimiter.get("delimiter_ref") or "")
        kind = str(delimiter.get("delimiter_kind") or "delimiter")
        degraded = delimiter.get("degraded_count")
        in_service = delimiter.get("services_in_service")
        degraded_n = float(degraded) if isinstance(degraded, int | float) else 0.0
        total_n = float(in_service) if isinstance(in_service, int | float) else 0.0
        if degraded_n <= 0:
            return self.ok()

        fraction = degraded_n / total_n if total_n > 0 else 0.0
        alone = degraded_n <= 1
        flags: list[DataQualityFlag] = []
        audit_year = delimiter.get("last_audit_year")
        stale_audit = isinstance(audit_year, int) and (
            context.now.year - audit_year
            >= context.threshold("localiser.stale_audit_years", 4.0)
        )
        if stale_audit:
            flags.append(DataQualityFlag.STALE_DATA)

        return self.ok(
            [
                self.finding(
                    context,
                    score=round(min(1.0, 0.45 + fraction * 0.5), 4),
                    confidence=round(0.6 if alone else 0.85, 4),
                    severity=Severity.MEDIUM if alone else Severity.HIGH,
                    explanation=(
                        f"Fault localises to {kind} {delimiter_ref}: {degraded_n:g} of "
                        f"{total_n:g} services behind it are degraded"
                        + (
                            ", and it is the only degraded delimiter under its parent."
                            if len(degraded_delimiters) == 1
                            else "."
                        )
                        + (
                            " Only this service is affected, so the drop is as likely as the "
                            "delimiter itself."
                            if alone
                            else ""
                        )
                        + (
                            f" Inventory was last audited in {audit_year}, so the record may not "
                            "match the plant."
                            if stale_audit
                            else ""
                        )
                    ),
                    affected=(delimiter_ref,),
                    features={
                        "degraded_count": degraded_n,
                        "services_in_service": total_n,
                        "degraded_fraction": round(fraction, 4),
                    },
                    recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                    flags=tuple(flags),
                    suspected_domain=FaultDomain.DROP if alone else FaultDomain.TAP_OR_ODP,
                    suspected_delimiter_ref=delimiter_ref or None,
                )
            ],
            flags=flags,
        )


__all__ = ["DelimiterLocaliser", "FaultDomainClassifier"]
