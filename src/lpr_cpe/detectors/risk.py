"""The four risk and quality scorers.

None of these diagnose a fault. They score the *response*: whether dispatching now would waste a
visit, whether this customer is about to be visited again, whether a handover carries enough for
the receiving crew to act, and whether a fix actually held. All four read `context.prior` or
`context.history` rather than telemetry.

They are scorers, not gates. Each returns a finding the policy engine and the graph can act on; not
one of them refuses anything by itself, because a detector that could block would be a second
policy layer sitting outside the pack -- and the pack is meant to be the only place an operational
threshold is written down.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.detectors.base import BaseDetector, DetectionContext, DetectorResult
from lpr_cpe.domain.enums import (
    DataQualityFlag,
    FaultDomain,
    Severity,
    TestKind,
)


class NoFaultFoundRiskScorer(BaseDetector):
    """How likely a truck roll ends with "nothing wrong here".

    The expensive mistake this prevents is dispatching against a customer-environment problem: the
    crew finds working plant, the incident closes as no-fault-found, and the customer's Wi-Fi is
    still bad the next day. High risk here is an argument for self-help, not for a visit.
    """

    name = "no_fault_found_risk"
    version = "1.0.0"
    #: Scores the response rather than the fault, off the other detectors' output. Marked derived so
    #: nothing downstream reads its `NO_FAULT_FOUND` domain back in as evidence that no fault exists
    #: -- that circle would let a low-evidence incident talk itself into being a closed one.
    derives_from_prior = True

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        if context.prior is None:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "no earlier detector results supplied, so dispatch risk cannot be scored",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        findings = context.findings_from()
        ran = [r for r in context.prior if r.ran]
        if not ran:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "every earlier detector was unavailable",
                flags=[DataQualityFlag.ADAPTER_UNAVAILABLE],
            )

        physical = {
            FaultDomain.DROP,
            FaultDomain.TAP_OR_ODP,
            FaultDomain.DISTRIBUTION,
            FaultDomain.FEEDER,
            FaultDomain.NODE_OR_OLT,
            FaultDomain.HEADEND_OR_CO,
        }
        soft = {
            FaultDomain.CUSTOMER_ENVIRONMENT,
            FaultDomain.INSIDE_HOME_WIRING,
            FaultDomain.CPE,
            FaultDomain.SERVICE_PLATFORM,
            FaultDomain.PROVISIONING,
        }
        physical_weight = sum(
            f.score * f.confidence for f in findings if f.suspected_domain in physical
        )
        soft_weight = sum(f.score * f.confidence for f in findings if f.suspected_domain in soft)
        power_weight = sum(
            f.score * f.confidence for f in findings if f.suspected_domain is FaultDomain.POWER
        )

        # No physical evidence at all is the strongest predictor: there is nothing at the premises
        # for a crew to repair.
        if physical_weight == 0.0:
            risk = 0.85
        else:
            # Capped below 1.0 deliberately. Soft evidence outweighing physical evidence argues
            # against dispatch; it never proves the plant is sound, and a detector that claimed it
            # did would be overriding the physical detectors from outside their own evidence.
            share = soft_weight / (physical_weight + soft_weight + 0.001)
            risk = min(0.8, share)
        if power_weight > 0:
            risk = max(risk, 0.9)
        if not findings:
            risk = 0.95

        features = {
            "physical_evidence": round(physical_weight, 4),
            "soft_evidence": round(soft_weight, 4),
            "power_evidence": round(power_weight, 4),
        }
        bar = context.threshold("nff.report_above", 0.5)
        if risk < bar:
            return self.ok()

        reason = (
            "no utility power at the premises, so there is nothing a network crew can fix"
            if power_weight > 0
            else "no physical-plant evidence was found at all"
            if physical_weight == 0.0
            else "the evidence points mostly at the home and the CPE rather than the plant"
        )
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(risk, 4),
                    confidence=0.7,
                    severity=Severity.HIGH if risk >= 0.8 else Severity.MEDIUM,
                    explanation=(
                        f"No-fault-found risk {risk:.0%}: {reason}. Prefer remote resolution or "
                        "self-help over a visit until physical evidence exists."
                    ),
                    affected=(),
                    features=features,
                    recommended_tests=(TestKind.NEIGHBOUR_COMPARISON, TestKind.CPE_WIFI_SURVEY),
                    suspected_domain=FaultDomain.NO_FAULT_FOUND if not findings else None,
                )
            ]
        )


class RepeatVisitRiskScorer(BaseDetector):
    """Has this service been here before?

    A third visit in a month is evidence the first two diagnosed the wrong thing. Reporting it lets
    the graph escalate rather than repeat, which is the only way the repeat-visit KPI improves
    rather than merely being measured.
    """

    name = "repeat_visit_risk"
    version = "1.0.0"
    requires = ("history",)
    derives_from_prior = True

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        history = context.payload("history")
        raw = history.get("previous_incidents")
        # `list[Any]`, not `list[dict[...]]`. `isinstance(raw, list)` proves the container and
        # nothing about what is in it, and annotating the stronger type would tell the typechecker
        # the per-element `isinstance` below is dead code -- deleting the one guard that makes a
        # malformed row survivable.
        previous: list[Any] = raw if isinstance(raw, list) else []
        window_days = context.threshold("repeat.window_days", 30.0)

        recent: list[dict[str, Any]] = []
        for incident in previous:
            if not isinstance(incident, dict):
                continue
            age = incident.get("closed_days_ago")
            if isinstance(age, int | float) and float(age) <= window_days:
                recent.append(incident)

        visits = sum(1 for i in recent if i.get("dispatched") is True)
        features = {
            "incidents_in_window": float(len(recent)),
            "visits_in_window": float(visits),
        }
        bar = context.threshold("repeat.visits_before_escalation", 2.0)
        if visits < bar:
            return self.ok()

        nff_before = sum(
            1 for i in recent if str(i.get("closure_reason") or "") == "no_fault_found"
        )
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(min(1.0, 0.5 + visits * 0.15), 4),
                    confidence=0.85,
                    severity=Severity.HIGH,
                    explanation=(
                        f"{visits} field visit(s) to this service in the last {window_days:.0f} "
                        f"days"
                        + (f", {nff_before} closed as no-fault-found" if nff_before else "")
                        + ". Repeating the same diagnosis is unlikely to find what the previous "
                        "visits missed; escalate rather than dispatch again."
                    ),
                    affected=(),
                    features=features,
                    recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                    suspected_domain=None,
                )
            ]
        )


class HandoverQualityValidator(BaseDetector):
    """Does the clean-to-dirty package carry enough for the receiving crew to act?

    Checked before the handover is offered rather than after it is rejected. A rejected handover
    costs a full cycle -- the incident sits waiting while somebody works out what was missing --
    and every field named here is one the receiving crew would otherwise have to ask for.
    """

    name = "handover_quality"
    version = "1.0.0"
    requires = ("history",)
    derives_from_prior = True

    #: What a dirty crew cannot start without. Each is a question they would have to come back and
    #: ask, and each round trip is measured in hours.
    _REQUIRED = (
        ("fault_domain", "which domain the fault was localised to"),
        ("delimiter_ref", "which tap or ODP to work at"),
        ("evidence_refs", "the evidence the diagnosis rests on"),
        ("access_notes", "how to reach the plant"),
        ("safety_notes", "what the hazards are"),
    )

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        history = context.payload("history")
        package = history.get("handover_package")
        if not isinstance(package, dict):
            return DetectorResult.not_applicable(
                self.name, self.version, "no handover package to validate on this incident"
            )

        absent = [
            (field, why)
            for field, why in self._REQUIRED
            if not package.get(field)
        ]
        features = {
            "required_fields": float(len(self._REQUIRED)),
            "missing_fields": float(len(absent)),
        }
        if not absent:
            return self.ok()

        share = len(absent) / len(self._REQUIRED)
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(min(1.0, 0.4 + share), 4),
                    confidence=0.9,
                    severity=Severity.HIGH if share >= 0.4 else Severity.MEDIUM,
                    explanation=(
                        f"Handover package is missing {len(absent)} of {len(self._REQUIRED)} "
                        "required fields: "
                        + "; ".join(f"{field} ({why})" for field, why in absent)
                        + ". Offering it now invites a rejection and a wasted cycle."
                    ),
                    affected=(),
                    features=features,
                    recommended_tests=(),
                    suspected_domain=None,
                )
            ]
        )


class PostFixStabilityDetector(BaseDetector):
    """Did the fix hold for the full stability window?

    Measured over a window rather than at a single moment, because the failure this exists to catch
    is the fix that looks good for ten minutes. A reboot restores service briefly and the same
    fault returns within the hour; closing on the first green reading is how that becomes a repeat
    visit.
    """

    name = "post_fix_stability"
    version = "1.0.0"
    requires = ("history",)
    derives_from_prior = True

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        history = context.payload("history")
        raw = history.get("post_fix_samples")
        # As in `RepeatVisitRiskScorer`: the container is checked here, each row is checked below.
        samples: list[Any] = raw if isinstance(raw, list) else []
        if not samples:
            return DetectorResult.not_applicable(
                self.name, self.version, "no fix has been applied yet, so there is nothing to hold"
            )

        window_minutes = context.threshold("validation.stability_window_minutes", 30.0)
        min_samples = context.threshold("validation.min_samples", 3.0)
        covered = max(
            (float(s.get("minutes_since_fix", 0.0)) for s in samples if isinstance(s, dict)),
            default=0.0,
        )
        healthy = [s for s in samples if isinstance(s, dict) and s.get("healthy") is True]
        features = {
            "samples": float(len(samples)),
            "healthy_samples": float(len(healthy)),
            "window_covered_minutes": covered,
        }

        if len(samples) < min_samples or covered < window_minutes:
            # Not yet a failure -- just not yet provable. Saying so keeps the incident open rather
            # than closing it on insufficient evidence.
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.4,
                        confidence=0.8,
                        severity=Severity.LOW,
                        explanation=(
                            f"Stability window incomplete: {len(samples):g} sample(s) over "
                            f"{covered:.0f} of {window_minutes:.0f} minutes. The fix has not yet "
                            "been observed long enough to call it held."
                        ),
                        affected=(),
                        features=features,
                        recommended_tests=(TestKind.THROUGHPUT, TestKind.LATENCY_JITTER_LOSS),
                        flags=(DataQualityFlag.LOW_SAMPLE_COUNT,),
                        suspected_domain=None,
                    )
                ],
                flags=[DataQualityFlag.LOW_SAMPLE_COUNT],
            )

        regressions = len(samples) - len(healthy)
        if regressions == 0:
            return self.ok()

        return self.ok(
            [
                self.finding(
                    context,
                    score=round(min(1.0, 0.5 + regressions / len(samples)), 4),
                    confidence=0.85,
                    severity=Severity.HIGH,
                    explanation=(
                        f"{regressions} of {len(samples)} samples across the "
                        f"{window_minutes:.0f}-minute window were unhealthy. The fix did not hold; "
                        "closing now would produce a repeat visit."
                    ),
                    affected=(),
                    features=features,
                    recommended_tests=(TestKind.THROUGHPUT, TestKind.LATENCY_JITTER_LOSS),
                    suspected_domain=None,
                )
            ]
        )


__all__ = [
    "HandoverQualityValidator",
    "NoFaultFoundRiskScorer",
    "PostFixStabilityDetector",
    "RepeatVisitRiskScorer",
]
