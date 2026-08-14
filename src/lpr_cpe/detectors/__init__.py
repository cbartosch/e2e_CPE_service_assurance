"""The detector layer: one shared contract, and the thirteen baseline detectors built on it.

Import the contract and the detectors from here. The diagnosis stage should ask `registry` for the
set rather than naming detectors one at a time -- a stage that imported thirteen names would have to
be edited to add a fourteenth, and the point of a uniform contract is that it does not.

`wifi_health_verdict` is re-exported deliberately. It is the single deterministic owner of the Wi-Fi
health score and band, and the graph needs it as much as the detector does; exporting it from the
package rather than from `cpe_wifi` alone is what stops a second implementation appearing beside it.
"""

from lpr_cpe.detectors.base import (
    BaseDetector,
    DetectionContext,
    Detector,
    DetectorResult,
)
from lpr_cpe.detectors.correlation import (
    CommonCauseClusterDetector,
    PowerWeatherCorrelationDetector,
    RecentChangeDetector,
)
from lpr_cpe.detectors.cpe_wifi import (
    DEFAULT_WIFI_THRESHOLDS,
    CPEWiFiAnomalyDetector,
    ServicePlatformAnomalyDetector,
    WifiVerdict,
    normalise_wifi_snapshot,
    wifi_health_verdict,
)
from lpr_cpe.detectors.localisation import DelimiterLocaliser, FaultDomainClassifier
from lpr_cpe.detectors.physical import (
    HFCRFDegradationDetector,
    PONOpticalDegradationDetector,
)
from lpr_cpe.detectors.registry import (
    all_detectors,
    classifying_detectors,
    run_detectors,
    telemetry_detectors,
)
from lpr_cpe.detectors.risk import (
    HandoverQualityValidator,
    NoFaultFoundRiskScorer,
    PostFixStabilityDetector,
    RepeatVisitRiskScorer,
)

__all__ = [
    "DEFAULT_WIFI_THRESHOLDS",
    "BaseDetector",
    "CPEWiFiAnomalyDetector",
    "CommonCauseClusterDetector",
    "DelimiterLocaliser",
    "DetectionContext",
    "Detector",
    "DetectorResult",
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
    "WifiVerdict",
    "all_detectors",
    "classifying_detectors",
    "normalise_wifi_snapshot",
    "run_detectors",
    "telemetry_detectors",
    "wifi_health_verdict",
]
