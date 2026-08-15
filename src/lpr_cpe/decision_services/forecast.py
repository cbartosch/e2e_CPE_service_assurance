"""The predictive assessment: a `PredictionResult` assembled around a verdict it does not compute.

`PredictionResult`'s own docstring names this module and states the constraint it is under:
`wifi_health_score` and `band` come from `detectors.cpe_wifi.wifi_health_verdict` and are not
recomputed here. That is not a stylistic preference. Those two fields are what a customer is told
about their Wi-Fi and what decides whether an engineer is sent, and a second implementation of the
score would be discovered to disagree with the first by a customer who was told two different things
about the same week.

So this module does the assembling: it converts the verdict's 0-1 score to the 0-100 scale
`PredictionResult` declares, attaches the band's severity from the map the detector also uses, sets
a confidence from how much of the radio snapshot was actually readable, and answers the two
questions the pack asks of a scan result -- is this worth raising an event for, and is it worth
sending someone.

`failure_probability` is the one number produced here, and it is a ranking weight rather than a
calibrated probability. Nothing in this repository has observed how often a Wi-Fi score of 0.35
precedes a fault; `docs/vendor-integration-gaps.md` records that a deployment replaces this with its
own outcome history. Until then it orders a scan queue and should not be quoted to anyone as a
percentage.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from lpr_cpe.detectors.cpe_wifi import SEVERITY_BY_BAND, WifiVerdict, wifi_health_verdict
from lpr_cpe.domain.diagnosis import PredictionResult
from lpr_cpe.domain.enums import ActionType, DataQualityFlag, HealthBand
from lpr_cpe.policies.models import HealthBandPolicy

#: The predictive assessment's name and version, recorded on every `PredictionResult`. Not a model
#: in the machine-learning sense and deliberately named so: `deterministic_wifi_health` is a
#: reproducible function of a radio snapshot, and calling it something that sounded trained would
#: invite the score to be read as one.
MODEL_NAME = "deterministic_wifi_health"
MODEL_VERSION = "1.0.0"

#: How far ahead the assessment claims to speak. Seven days because the scan cadence is twice daily
#: and a horizon shorter than the interval between scans would leave gaps that nothing had assessed.
DEFAULT_HORIZON = timedelta(days=7)

#: Which remote action each breached metric points at, keyed on `WifiVerdict.breached_metrics` --
#: the detector's stable metric names, which are also its `features` keys.
#:
#: Keyed on those and not on `WifiVerdict.breaches`, which is prose. The first version of this map
#: matched prose by substring and matched nothing at all: the breach reads "airtime utilisation 82%"
#: and the key was `"utilization"`, a spelling the detector never uses. Every lookup missed, so
#: `recommended_actions` returned an empty tuple for every verdict while its docstring described the
#: link it was supposed to be making. An exact lookup against a named vocabulary is what makes that
#: failure impossible to have silently, and `test_decision_services.py` asserts these keys are all
#: metrics `wifi_health_verdict` can actually emit.
#:
#: `throughput_mbps` is deliberately absent. Low Wi-Fi throughput is a symptom of one of the other
#: four metrics or of something outside the radios entirely -- it names no lever of its own, and
#: guessing one would attach an action to the breach least able to justify it. A verdict breaching
#: only throughput therefore recommends nothing, and its band still drives the event and dispatch
#: decisions below.
_ACTION_FOR_BREACH: dict[str, ActionType] = {
    # A busy channel is answered by a different channel.
    "utilization_pct": ActionType.WIFI_CHANNEL_CHANGE,
    # A raised noise floor is interference, and the remote answer is likewise to move off it.
    "noise_floor_dbm": ActionType.WIFI_CHANNEL_CHANGE,
    # Weak signal at the worst client is the one coverage lever the system holds unattended;
    # repositioning the gateway is the customer's action and belongs in guided self-help.
    "worst_rssi_dbm": ActionType.WIFI_POWER_CHANGE,
    # Errors and retransmissions are most often radio or driver state, which a resync clears.
    "error_rate_pct": ActionType.CPE_RESYNC,
}


def forecast_wifi(
    wifi: dict[str, Any] | None,
    *,
    predicted_at: datetime,
    subject_ref: str,
    bands: HealthBandPolicy,
    thresholds: dict[str, float] | None = None,
    horizon: timedelta = DEFAULT_HORIZON,
    data_quality_warnings: Sequence[DataQualityFlag] = (),
    evidence_refs: Sequence[str] = (),
) -> PredictionResult | None:
    """Assess one service's Wi-Fi, or return `None` when the radios reported nothing.

    `None` rather than a zero score, for the same reason `wifi_health_verdict` returns `None`: a
    score of zero is the worst possible Wi-Fi, and an unread radio is not that. A predictive sweep
    that turned unreadable CPEs into critical results would fill the dispatch queue with houses
    whose only fault is that the ACS did not answer.

    `thresholds` is the pack's `detector_thresholds`, passed through untouched. The three band
    boundaries travel in it -- `DEFAULT_WIFI_THRESHOLDS` documents why they are stated on a 0-1
    scale there and a 0-100 scale in `health_bands` -- so this function must not band the score
    itself even though it holds a `HealthBandPolicy`. It holds one for `at_or_below`, which is a
    different question.
    """
    if wifi is None:
        return None
    verdict = wifi_health_verdict(wifi, thresholds)
    if verdict is None:
        return None

    flags = tuple(data_quality_warnings)
    return PredictionResult(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        predicted_at=predicted_at,
        horizon=horizon,
        subject_ref=subject_ref,
        failure_probability=failure_probability(verdict),
        # The verdict scores 0-1; `PredictionResult` declares 0-100. Converting here rather than
        # changing either side keeps the detector's thresholds and the pack's bands each on the
        # scale their own file states.
        wifi_health_score=round(verdict.score * 100.0, 2),
        band=verdict.band,
        severity=SEVERITY_BY_BAND[verdict.band],
        confidence=_confidence(verdict, flags),
        top_features=dict(verdict.features),
        evidence_refs=tuple(evidence_refs),
        data_quality_warnings=flags,
        recommended_actions=recommended_actions(verdict),
        narrative="",
        narrative_source="none",
    )


def failure_probability(verdict: WifiVerdict) -> float:
    """How likely this service is to produce a fault inside the horizon, as a ranking weight.

    The complement of the health score, damped by a half so that a health score of zero produces
    0.5 rather than certainty. The damping is the honest part: a Wi-Fi health score of zero means
    every metric measured is bad *now*, which is a strong statement about the present and a weak one
    about the next seven days -- plenty of households live with terrible Wi-Fi and never call.
    Reporting that as a failure probability of 1.0 would put it above a confirmed optical
    degradation in any queue that sorted on this field.
    """
    return round((1.0 - verdict.score) * 0.5, 4)


def recommended_actions(verdict: WifiVerdict) -> tuple[ActionType, ...]:
    """The remote actions this verdict's breaches point at, deduplicated and ordered.

    Recommendations, not decisions: `policies.engine` still gates each one and
    `decision_services.resolution` still ranks them against alternatives. What this adds is the link
    from a specific breached metric to the action that addresses *that* metric, which is lost if a
    caller only sees the band -- "critical Wi-Fi" suggests everything and therefore nothing.

    Empty is a real answer, returned in two distinct cases that both mean "no unattended action
    follows": nothing breached, or the only breaches are metrics `_ACTION_FOR_BREACH` names no lever
    for. Neither is an error, and neither should be read as the Wi-Fi being fine -- `band` answers
    that question.

    Order follows the detector's own metric order, so the same verdict always yields the same tuple.
    """
    actions: list[ActionType] = []
    for metric in verdict.breached_metrics:
        action = _ACTION_FOR_BREACH.get(metric)
        if action is not None and action not in actions:
            actions.append(action)
    return tuple(actions)


def _confidence(verdict: WifiVerdict, flags: Sequence[DataQualityFlag]) -> float:
    """How much of the assessment rests on measurement rather than on absence.

    `WifiVerdict.features` holds one entry per metric that was actually readable, so its size is a
    direct count of the evidence. Four features is a complete snapshot; one is a score computed from
    a quarter of the picture and reported on the same scale as a complete one, which is precisely
    what `confidence` exists to distinguish.

    Each data-quality warning takes a further tenth, floored at 0.1 rather than zero: a confidence
    of zero would say the assessment carries no information, and the reason to keep it above that is
    that `PredictionResult` is still evidence of what was read even when the read was poor.
    """
    completeness = min(1.0, len(verdict.features) / 4.0)
    return round(max(0.1, completeness * (1.0 - 0.1 * len(flags))), 4)


def should_raise_event(band: HealthBand, bands: HealthBandPolicy) -> bool:
    """Whether a scan result at this band enters the workflow, or is only recorded for KPIs.

    `HealthBandPolicy.at_or_below` does the comparison, because `HealthBand` is a `StrEnum` and `<=`
    on it sorts alphabetically -- `"at_risk" < "healthy"` is true by accident and `"critical" <
    "degraded"` is true for the wrong reason. This is the line between watching and acting, and it
    is not a line to get from a string comparison.
    """
    return bands.at_or_below(band, bands.event_threshold_band)


def should_dispatch(band: HealthBand, bands: HealthBandPolicy) -> bool:
    """Whether a scan result at this band is bad enough to be worth someone's visit.

    A separate threshold from `should_raise_event` and a stricter one, which is the whole point of
    the pack carrying two: everything that crosses the event line gets a case and a record, and only
    what crosses this one gets a van. Collapsing them would either bury real degradation in a KPI
    report or send a technician to every household whose 2.4 GHz radio is busy.
    """
    return bands.at_or_below(band, bands.dispatch_threshold_band)


__all__ = [
    "DEFAULT_HORIZON",
    "MODEL_NAME",
    "MODEL_VERSION",
    "failure_probability",
    "forecast_wifi",
    "recommended_actions",
    "should_dispatch",
    "should_raise_event",
]
