"""CPE and Wi-Fi anomaly detection, and the service-platform check.

`wifi_health_verdict` is public and is the *only* place a Wi-Fi health score and band are computed.
The specification is explicit that the verdict and score are a fault-severity determination and
must stay deterministic, with the language model restricted to narrative. Exporting one function
that both this detector and the graph call is option (a) from that section -- the model never gets
the chance to derive a verdict, so there is nothing to drift.

The score and band have one owner for a second reason: `AnomalyFinding.contributing_features` is
`dict[str, float]` and cannot carry a `HealthBand`, so a detector that wanted to publish its band
through the finding would have to encode the enum as a number. Rather than do that, the band is
recomputed by whoever needs it from the same function, against the same thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lpr_cpe.detectors.base import BaseDetector, DetectionContext, DetectorResult
from lpr_cpe.domain.enums import (
    DataQualityFlag,
    FaultDomain,
    HealthBand,
    Severity,
    TestKind,
)

#: Defaults pinned against the fixture Wi-Fi profiles: `clean` sits clear of every bar, while
#: `congested_2g` breaches utilisation, error rate and noise floor, and `weak_coverage` breaches
#: RSSI. A detector whose defaults fired on the clean profile would be measuring nothing.
DEFAULT_WIFI_THRESHOLDS: dict[str, float] = {
    "wifi.utilization_max_pct": 70.0,
    "wifi.worst_rssi_min_dbm": -78.0,
    "wifi.error_rate_max_pct": 5.0,
    "wifi.noise_floor_max_dbm": -85.0,
    "wifi.throughput_min_mbps": 80.0,
    # Band boundaries, on this function's 0-1 scale. These are the same three numbers as
    # `health_bands` in the policy pack, which states them on a 0-100 scale, and
    # `tests/unit/test_decision_services.py` holds the two to each other. They are stated twice
    # because neither side can import the other -- a detector takes a threshold mapping, not a pack
    # -- and the alternative to a test is the pack and the detector disagreeing about the band on a
    # score of 0.62, which is the band that decides whether a crew is sent.
    "wifi.band_healthy_at_or_above": 0.80,
    "wifi.band_degraded_at_or_above": 0.60,
    "wifi.band_at_risk_at_or_above": 0.40,
}


@dataclass(frozen=True, slots=True)
class WifiVerdict:
    """The deterministic half of the Wi-Fi assessment: a score, a band, and why.

    The "why" is carried twice on purpose, in two vocabularies that must not be confused:

    * `breaches` is prose, for the customer narrative and the operator's first read. It contains the
      measured value, so it changes wording as freely as the reading changes.
    * `breached_metrics` names the *metric* that breached, using the same keys as `features`. It is
      the machine-readable half, and it exists because `decision_services.forecast` maps a breach to
      the remote action that addresses it. That map was first written against the prose and matched
      nothing -- "utilization" never appears in "airtime utilisation 82%" -- so it silently
      recommended no action at all. A parallel tuple of stable keys is what makes such a map
      checkable, and `test_decision_services.py` asserts every key in it is a metric this function
      can actually emit.

    Every entry in `breached_metrics` is a key of `features`; the reverse does not hold, because a
    metric can be read and be fine.
    """

    score: float
    band: HealthBand
    breaches: tuple[str, ...]
    features: dict[str, float]
    breached_metrics: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.band is HealthBand.HEALTHY


#: How much of a metric's weight is spent simply by crossing its threshold, with the remainder
#: graded by how far past it the reading sits. Crossing the line is itself the signal -- a threshold
#: that only mattered once you were far beyond it would not be a threshold. Without this, a client
#: 6 dB below the coverage floor scored 0.94 and was called healthy while the verdict simultaneously
#: listed its RSSI as a breach.
_BREACH_BASE = 0.5

#: TR-181 band labels as the CPE adapter emits them, mapped to the suffix used in the flat metric
#: names. Anything else is ignored rather than guessed at.
_BAND_SUFFIX = {"2.4GHz": "2g", "5GHz": "5g"}

#: How bad a health band is, as an incident severity. Public and complete over the four bands
#: because `decision_services.forecast` sets `PredictionResult.severity` from a band and the
#: detector below sets a finding's severity from the same band -- the two are answering one question
#: and would otherwise answer it in two places. `HEALTHY` maps to `INFO` rather than being omitted:
#: a predictive scan that finds nothing still produces a record, and a record with no severity would
#: have to be given one by whoever read it.
SEVERITY_BY_BAND: dict[HealthBand, Severity] = {
    HealthBand.HEALTHY: Severity.INFO,
    HealthBand.DEGRADED: Severity.MEDIUM,
    HealthBand.AT_RISK: Severity.HIGH,
    HealthBand.CRITICAL: Severity.CRITICAL,
}


def _num(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _breach(weight: float, over: float, span: float) -> float:
    """Penalty for one breached metric: a fixed cost for crossing, graded by how far past."""
    return weight * (_BREACH_BASE + (1.0 - _BREACH_BASE) * min(over / span, 1.0))


def normalise_wifi_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a TR-069 Wi-Fi read into the flat metric names the verdict scores.

    The CPE adapter returns `Device.WiFi.Radio.*` and `Device.WiFi.AccessPoint.*` parameter objects
    because that is what an ACS returns. Comparing thresholds directly against those paths would put
    TR-181 vocabulary inside every threshold name and every policy-pack key, and would tie the
    scoring rules to one southbound protocol -- the next CPE integration would need its own copy of
    the arithmetic. Translating once, here, keeps `wifi_health_verdict` the single owner of what the
    numbers *mean* while this function owns where they came from.

    A payload that is already flat passes through untouched, so callers holding a summary rather
    than a device read do not have to fabricate a TR-069 envelope to be scored.
    """
    if "radios" not in payload and "access_points" not in payload:
        return payload

    flat: dict[str, Any] = {}
    error_rates: list[float] = []
    radios = payload.get("radios")
    for radio in radios if isinstance(radios, list) else []:
        if not isinstance(radio, dict):
            continue
        band = _BAND_SUFFIX.get(str(radio.get("Device.WiFi.Radio.OperatingFrequencyBand") or ""))
        if band is None:
            continue
        util = _num(radio, "Device.WiFi.Radio.Stats.ChannelUtilization")
        if util is not None:
            flat[f"utilization_{band}_pct"] = util
        noise = _num(radio, "Device.WiFi.Radio.Stats.NoiseFloor")
        if noise is not None:
            flat[f"noise_floor_{band}_dbm"] = noise
        err = _num(radio, "Device.WiFi.Radio.Stats.ErrorRatePct")
        if err is not None:
            error_rates.append(err)
    if error_rates:
        flat["error_rate_pct"] = max(error_rates)

    # The worst *active* client. An associated-but-idle device parked at the edge of coverage is not
    # what the customer is complaining about, and letting it set the worst-RSSI figure would make
    # every home with a forgotten device in the garage look like a coverage fault.
    signals: list[float] = []
    aps = payload.get("access_points")
    for ap in aps if isinstance(aps, list) else []:
        if not isinstance(ap, dict):
            continue
        clients = ap.get("Device.WiFi.AccessPoint.AssociatedDevice")
        for client in clients if isinstance(clients, list) else []:
            if not isinstance(client, dict):
                continue
            if client.get("Device.WiFi.AccessPoint.AssociatedDevice.Active") is not True:
                continue
            rssi = _num(client, "Device.WiFi.AccessPoint.AssociatedDevice.SignalStrength")
            if rssi is not None:
                signals.append(rssi)
    if signals:
        flat["worst_rssi_dbm"] = min(signals)
        flat["client_count"] = float(len(signals))

    # Throughput is deliberately absent: a TR-069 read carries PHY rates, not measured throughput,
    # and the only throughput figure the system holds is the service speed test that
    # `ServicePlatformAnomalyDetector` already owns. Passing that in here would blame the radios for
    # an access-network fault. A genuine Wi-Fi speed test may still be supplied by the caller.
    return flat


def wifi_health_verdict(
    wifi: dict[str, Any],
    thresholds: dict[str, float] | None = None,
) -> WifiVerdict | None:
    """Score Wi-Fi health from a radio snapshot. `None` when the radios reported nothing.

    Accepts either a TR-069 device read or an already-flat metric summary; see
    `normalise_wifi_snapshot`.

    Returning `None` rather than a zero score is the point: the `no_radio_data` profile has every
    metric null, and a zero score would read as the worst possible Wi-Fi rather than as an absence
    of measurement. The caller turns that into a data-quality warning.
    """
    wifi = normalise_wifi_snapshot(wifi)
    bar = {**DEFAULT_WIFI_THRESHOLDS, **(thresholds or {})}
    features: dict[str, float] = {}
    # Metric key and prose kept in one list so they cannot drift apart. Two lists appended to at ten
    # sites is two chances per breach for one to be forgotten, and the failure is silent in exactly
    # the direction that matters: a prose breach with no metric key is a breach no action maps to.
    flagged: list[tuple[str, str]] = []
    penalties = 0.0

    # The worse of the two bands, not their mean. A congested 2.4 GHz radio is a real customer
    # complaint even when 5 GHz is idle, and averaging the pair is how that complaint disappears.
    util = max(
        (
            v
            for v in (_num(wifi, "utilization_2g_pct"), _num(wifi, "utilization_5g_pct"))
            if v is not None
        ),
        default=None,
    )
    rssi = _num(wifi, "worst_rssi_dbm")
    err = _num(wifi, "error_rate_pct")
    noise = max(
        (
            v
            for v in (_num(wifi, "noise_floor_2g_dbm"), _num(wifi, "noise_floor_5g_dbm"))
            if v is not None
        ),
        default=None,
    )
    tput = _num(wifi, "throughput_mbps")

    if util is None and rssi is None and err is None and noise is None and tput is None:
        return None

    if util is not None:
        features["utilization_pct"] = util
        if util > bar["wifi.utilization_max_pct"]:
            flagged.append(("utilization_pct", f"airtime utilisation {util:g}%"))
            penalties += _breach(0.30, util - bar["wifi.utilization_max_pct"], 30.0)
    if rssi is not None:
        features["worst_rssi_dbm"] = rssi
        if rssi < bar["wifi.worst_rssi_min_dbm"]:
            flagged.append(("worst_rssi_dbm", f"worst client RSSI {rssi:g} dBm"))
            penalties += _breach(0.30, bar["wifi.worst_rssi_min_dbm"] - rssi, 12.0)
    if err is not None:
        features["error_rate_pct"] = err
        if err > bar["wifi.error_rate_max_pct"]:
            flagged.append(("error_rate_pct", f"error rate {err:g}%"))
            penalties += _breach(0.20, err - bar["wifi.error_rate_max_pct"], 8.0)
    if noise is not None:
        features["noise_floor_dbm"] = noise
        if noise > bar["wifi.noise_floor_max_dbm"]:
            flagged.append(("noise_floor_dbm", f"noise floor {noise:g} dBm"))
            penalties += _breach(0.20, noise - bar["wifi.noise_floor_max_dbm"], 10.0)
    if tput is not None:
        features["throughput_mbps"] = tput
        if tput < bar["wifi.throughput_min_mbps"]:
            flagged.append(("throughput_mbps", f"throughput {tput:g} Mbps"))
            penalties += _breach(0.20, bar["wifi.throughput_min_mbps"] - tput, 60.0)

    score = max(0.0, min(1.0, 1.0 - penalties))
    # HEALTHY requires *both* a healthy score and nothing breached. The score alone is not enough:
    # the cheapest single breach costs 0.10, so a client 6 dB below the coverage floor scored 0.90
    # and was called healthy while the same verdict listed the RSSI breach that made it not -- two
    # halves of one object contradicting each other, and the half the customer narrative reads is
    # the band. The breach test is this function's own; the three boundaries belong to the policy
    # pack and arrive through `thresholds`.
    if not flagged and score >= bar["wifi.band_healthy_at_or_above"]:
        band = HealthBand.HEALTHY
    elif score >= bar["wifi.band_degraded_at_or_above"]:
        band = HealthBand.DEGRADED
    elif score >= bar["wifi.band_at_risk_at_or_above"]:
        band = HealthBand.AT_RISK
    else:
        band = HealthBand.CRITICAL
    return WifiVerdict(
        score=score,
        band=band,
        breaches=tuple(prose for _, prose in flagged),
        features=features,
        breached_metrics=tuple(metric for metric, _ in flagged),
    )


class CPEWiFiAnomalyDetector(BaseDetector):
    """The CPE itself, then its radios.

    Order matters. An offline CPE makes every Wi-Fi metric meaningless, so the device is checked
    first and the radio assessment is skipped rather than run against nulls -- otherwise a customer
    whose gateway is unplugged gets a confident "your Wi-Fi is congested" narrative.
    """

    name = "cpe_wifi_anomaly"
    version = "1.0.0"
    requires = ("cpe_raw",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        cpe = context.payload("cpe_raw")
        flags: list[DataQualityFlag] = []

        if cpe.get("online") is False:
            notes = "; ".join(str(x) for x in (cpe.get("data_quality_notes") or []))
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.75,
                        confidence=0.8,
                        severity=Severity.HIGH,
                        explanation=(
                            "CPE is offline, so no radio measurement is meaningful this window."
                            + (f" Adapter notes: {notes}." if notes else "")
                        ),
                        affected=(str(cpe.get("cpe_ref") or ""),),
                        features={},
                        recommended_tests=(TestKind.CPE_CONNECTIVITY,),
                        suspected_domain=FaultDomain.CPE,
                    )
                ]
            )

        wifi = context.wifi
        if wifi is None:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "CPE is online but no Wi-Fi snapshot was fetched",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        verdict = wifi_health_verdict(wifi, context.thresholds)
        if verdict is None:
            # Radios present but reporting nothing. That is a defect in the read, not a healthy
            # result, and `ran=False` keeps it out of the clean-scan numerator.
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "radios returned no measurements",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        if verdict.healthy:
            return self.ok(flags=flags)

        severity = SEVERITY_BY_BAND[verdict.band]
        return self.ok(
            [
                self.finding(
                    context,
                    # The anomaly score is the complement of the health score: a health of 0.30 is
                    # an anomaly of 0.70. Publishing health as the anomaly score would invert every
                    # downstream threshold comparison.
                    score=round(1.0 - verdict.score, 4),
                    confidence=0.8,
                    severity=severity,
                    explanation=(
                        f"Wi-Fi health {verdict.score:.2f} ({verdict.band.value}); "
                        f"{', '.join(verdict.breaches)}. This is an in-home radio problem, not an "
                        "access-network fault."
                    ),
                    affected=(str(cpe.get("cpe_ref") or ""),),
                    features=verdict.features,
                    recommended_tests=(TestKind.CPE_WIFI_SURVEY, TestKind.THROUGHPUT),
                    flags=tuple(flags),
                    suspected_domain=FaultDomain.CUSTOMER_ENVIRONMENT,
                )
            ],
            flags=flags,
        )


class ServicePlatformAnomalyDetector(BaseDetector):
    """Throughput against the *sold* rate, plus provisioning consistency.

    Judged as a fraction of what the customer bought rather than in raw Mbps. A 100 Mbps product
    delivering 94 Mbps is healthy and a 1 Gbps product delivering 310 Mbps is not, and a single
    Mbps threshold cannot express both -- it would either fail every entry tier or pass every
    gigabit fault.
    """

    name = "service_platform_anomaly"
    version = "1.0.0"
    requires = ("service_platform",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        platform = context.payload("service_platform")
        download = platform.get("download_speed")
        if not isinstance(download, dict) or not download:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "no download measurement in the service-platform snapshot",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        fraction = _num(download, "fraction_of_sold")
        if fraction is None:
            sold = _num(download, "sold_mbps")
            got = _num(download, "throughput_mbps")
            fraction = (got / sold) if sold and got is not None and sold > 0 else None
        if fraction is None:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "throughput present but the sold rate is unknown, so no ratio can be formed",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        floor = context.threshold("platform.throughput_fraction_min", 0.6)
        features = {"fraction_of_sold": fraction}
        sold_mbps = _num(download, "sold_mbps")
        if sold_mbps is not None:
            features["sold_mbps"] = sold_mbps

        if fraction >= floor:
            return self.ok()

        score = min(1.0, 0.4 + (floor - fraction) * 1.5)
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(score, 4),
                    confidence=0.7,
                    severity=Severity.HIGH if fraction < floor * 0.6 else Severity.MEDIUM,
                    explanation=(
                        f"Throughput is {fraction:.0%} of the sold rate, below the {floor:.0%} "
                        "floor. Rules out neither the platform nor the access layer on its own; "
                        "it says the customer is not getting what they bought."
                    ),
                    affected=(str(platform.get("service_ref") or ""),),
                    features=features,
                    recommended_tests=(TestKind.THROUGHPUT, TestKind.SERVICE_PLATFORM_CHECK),
                    suspected_domain=FaultDomain.SERVICE_PLATFORM,
                )
            ]
        )


__all__ = [
    "DEFAULT_WIFI_THRESHOLDS",
    "CPEWiFiAnomalyDetector",
    "ServicePlatformAnomalyDetector",
    "WifiVerdict",
    "normalise_wifi_snapshot",
    "wifi_health_verdict",
]
