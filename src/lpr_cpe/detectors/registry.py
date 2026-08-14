"""The thirteen baseline detectors as one ordered set, and the runner over them.

Order is not cosmetic. The six telemetry detectors run first and the classifiers run second,
because `FaultDomainClassifier`, `DelimiterLocaliser` and the risk scorers read
`DetectionContext.prior` -- they classify over the others' findings rather than over telemetry.
Running the set in one call is what guarantees they see a complete `prior` rather than whatever
happened to have finished.

`run_detectors` returns every result, including the ones that could not run. Filtering the
unavailable ones out here would be the same conflation `DetectorResult.ran` exists to prevent: the
caller needs to know the difference between twelve clean detectors and eleven clean detectors plus
one broken adapter.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from lpr_cpe.detectors.base import DetectionContext, Detector, DetectorResult
from lpr_cpe.detectors.correlation import (
    CommonCauseClusterDetector,
    PowerWeatherCorrelationDetector,
    RecentChangeDetector,
)
from lpr_cpe.detectors.cpe_wifi import (
    CPEWiFiAnomalyDetector,
    ServicePlatformAnomalyDetector,
)
from lpr_cpe.detectors.localisation import DelimiterLocaliser, FaultDomainClassifier
from lpr_cpe.detectors.physical import (
    HFCRFDegradationDetector,
    PONOpticalDegradationDetector,
)
from lpr_cpe.detectors.risk import (
    HandoverQualityValidator,
    NoFaultFoundRiskScorer,
    PostFixStabilityDetector,
    RepeatVisitRiskScorer,
)


def telemetry_detectors() -> list[Detector]:
    """The seven that read telemetry. Independent of each other, so they may run concurrently."""
    return [
        HFCRFDegradationDetector(),
        PONOpticalDegradationDetector(),
        CPEWiFiAnomalyDetector(),
        ServicePlatformAnomalyDetector(),
        CommonCauseClusterDetector(),
        RecentChangeDetector(),
        PowerWeatherCorrelationDetector(),
    ]


def classifying_detectors() -> list[Detector]:
    """The six that read the others' output. Must run after `telemetry_detectors`."""
    return [
        FaultDomainClassifier(),
        DelimiterLocaliser(),
        NoFaultFoundRiskScorer(),
        RepeatVisitRiskScorer(),
        HandoverQualityValidator(),
        PostFixStabilityDetector(),
    ]


def all_detectors() -> list[Detector]:
    """All thirteen, in the order they must run."""
    return telemetry_detectors() + classifying_detectors()


async def run_detectors(context: DetectionContext) -> list[DetectorResult]:
    """Run the full set against one snapshot and return all thirteen results.

    The telemetry pass is gathered concurrently -- the detectors never fetch, so they cannot
    contend on anything, and thirteen sequential awaits would add latency for no isolation.

    The classifying pass is sequential and *accumulating*: each classifier sees the results of the
    ones before it, not just the telemetry. That is what the declared order is for. Handing all six
    the same telemetry-only `prior` made the order decorative and produced a specific wrong answer:
    `DelimiterLocaliser` would find a degraded tap and `NoFaultFoundRiskScorer`, unable to see it,
    would report no physical evidence and an 85% chance of a wasted visit for the same incident.

    Double-counting is prevented by `DetectorResult.derived` rather than by withholding results, so
    a classifier can read the summaries when it wants them and is not charged for them when it does
    not -- see `DetectionContext.findings_from`.
    """
    telemetry = await asyncio.gather(*(d.detect(context) for d in telemetry_detectors()))
    results: list[DetectorResult] = list(telemetry)
    for detector in classifying_detectors():
        results.append(await detector.detect(replace(context, prior=list(results))))
    return results


__all__ = [
    "CPEWiFiAnomalyDetector",
    "CommonCauseClusterDetector",
    "DelimiterLocaliser",
    "FaultDomainClassifier",
    "HFCRFDegradationDetector",
    "HandoverQualityValidator",
    "NoFaultFoundRiskScorer",
    "PONOpticalDegradationDetector",
    "PostFixStabilityDetector",
    "PowerWeatherCorrelationDetector",
    "RecentChangeDetector",
    "RepeatVisitRiskScorer",
    "ServicePlatformAnomalyDetector",
    "all_detectors",
    "classifying_detectors",
    "run_detectors",
    "telemetry_detectors",
]
