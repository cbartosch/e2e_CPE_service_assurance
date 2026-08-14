"""The two access-layer detectors: DOCSIS RF/PNM, and PON optical.

These two are mutually exclusive by technology, and that is a property of the plant rather than a
limitation here: NXT raises for a PON service because a PON service has no DOCSIS RF, and the PON
adapter raises for HFC for the same reason in reverse. Each therefore reports `not_applicable` --
which is *not* a data-quality defect -- when handed the other technology. A `MISSING_FIELD` there
would make every PON incident carry phantom defects from this module and drag the policy pack's
evidence checks towards blocking healthy services.

The context keys these read are assembled by the diagnosis stage, one fetch per adapter call:

    ctx.nxt   = {"rf": <fetch_rf_measurements>, "pnm": <fetch_pnm_capture>,
                 "service_group": <fetch_service_group_health>}
    ctx.plant = {"optical": <fetch_optical_levels>, "port": <fetch_pon_port_health>,
                 "delimiter": <fetch_odp_view or fetch_tap_view>}

Sub-payloads are optional within those dicts: a PNM capture that did not come back costs confidence
and adds a warning, it does not stop the RF half from running. That is the whole reason the
detectors take a snapshot rather than fetching -- one unavailable capture must not cost the other
twelve detectors their findings.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.detectors.base import BaseDetector, DetectionContext, DetectorResult
from lpr_cpe.domain.enums import (
    DataQualityFlag,
    FaultDomain,
    Severity,
    Technology,
    TestKind,
)


def _num(payload: dict[str, Any], key: str) -> float | None:
    """A numeric field, or `None` when absent or explicitly null.

    Null is not zero. `rx_optical_power_dbm: null` on a powered-off ONT means "no measurement
    exists", and reading it as 0.0 dBm would be a spectacularly good optical reading.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class HFCRFDegradationDetector(BaseDetector):
    """DOCSIS RF levels, codeword errors and the PNM pre-equalisation summary.

    Scores the worst single impairment rather than an average. Averaging is how an upstream that is
    9 dB hot gets diluted by four healthy downstream metrics into a "mild" finding, and upstream
    power is precisely the symptom that predicts a drop or tap fault.
    """

    name = "hfc_rf_pnm_degradation"
    version = "1.0.0"
    requires = ("nxt",)
    #: Declared rather than checked inside `_detect`, because `requires` runs before `_detect` and a
    #: PON service has no `nxt` payload at all -- the in-body check was unreachable for exactly the
    #: services it existed to exempt.
    applies_to = (Technology.HFC,)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        nxt = context.payload("nxt")
        rf = nxt.get("rf") or {}
        if not isinstance(rf, dict) or not rf:
            return DetectorResult.unavailable(
                self.name, self.version, "no RF measurement in the snapshot"
            )

        flags: list[DataQualityFlag] = []
        if rf.get("data_available") is False:
            flags.append(DataQualityFlag.ADAPTER_UNAVAILABLE)

        us_power_max = context.threshold("hfc.upstream_power_max_dbmv", 51.0)
        ds_power_min = context.threshold("hfc.downstream_power_min_dbmv", -5.0)
        us_snr_min = context.threshold("hfc.upstream_snr_min_db", 28.0)
        ds_snr_min = context.threshold("hfc.downstream_snr_min_db", 32.0)
        uncorr_max = context.threshold("hfc.uncorrectable_codewords_max", 500.0)
        t3_max = context.threshold("hfc.t3_timeouts_max", 3.0)
        flap_max = context.threshold("hfc.flap_count_max", 3.0)

        # (label, observed, breach amount in its own unit, weight towards the score)
        breaches: list[tuple[str, float, float]] = []
        features: dict[str, float] = {}

        us_power = _num(rf, "upstream_power_dbmv")
        if us_power is not None:
            features["upstream_power_dbmv"] = us_power
            if us_power > us_power_max:
                breaches.append(("upstream transmit power", us_power, us_power - us_power_max))

        ds_power = _num(rf, "downstream_power_dbmv")
        if ds_power is not None:
            features["downstream_power_dbmv"] = ds_power
            if ds_power < ds_power_min:
                breaches.append(("downstream receive power", ds_power, ds_power_min - ds_power))

        us_snr = _num(rf, "upstream_snr_db")
        if us_snr is not None:
            features["upstream_snr_db"] = us_snr
            if us_snr < us_snr_min:
                breaches.append(("upstream MER", us_snr, us_snr_min - us_snr))

        ds_snr = _num(rf, "downstream_snr_db")
        if ds_snr is not None:
            features["downstream_snr_db"] = ds_snr
            if ds_snr < ds_snr_min:
                breaches.append(("downstream MER", ds_snr, ds_snr_min - ds_snr))

        uncorr = _num(rf, "uncorrectable_codewords")
        if uncorr is not None:
            features["uncorrectable_codewords"] = uncorr
            if uncorr > uncorr_max:
                # Normalised against the threshold so the score stays in range no matter how bad
                # the count gets; 4180 against a 500 bar is "very bad", not "8.36 bad".
                breaches.append(("uncorrectable codewords", uncorr, min(uncorr / uncorr_max, 6.0)))

        t3 = _num(rf, "t3_timeouts")
        if t3 is not None:
            features["t3_timeouts"] = t3
            if t3 > t3_max:
                breaches.append(("T3 ranging timeouts", t3, t3 - t3_max))

        flaps = _num(rf, "flap_count_24h")
        if flaps is not None:
            features["flap_count_24h"] = flaps
            if flaps > flap_max:
                breaches.append(("modem flaps in 24h", flaps, flaps - flap_max))

        if not features:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "RF payload carried no numeric measurements",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        pnm = nxt.get("pnm") if isinstance(nxt.get("pnm"), dict) else None
        pnm_note = ""
        if pnm is None:
            flags.append(DataQualityFlag.MISSING_FIELD)
        else:
            ripple = _num(pnm, "in_channel_ripple_db")
            main_tap = _num(pnm, "main_tap_energy_ratio_db")
            pnm_conf = _num(pnm, "confidence") or 0.0
            ripple_max = context.threshold("hfc.pnm_ripple_max_db", 4.0)
            main_tap_min = context.threshold("hfc.pnm_main_tap_ratio_min_db", 15.0)
            if ripple is not None:
                features["pnm_in_channel_ripple_db"] = ripple
            if main_tap is not None:
                features["pnm_main_tap_energy_ratio_db"] = main_tap
            impaired = (ripple is not None and ripple > ripple_max) or (
                main_tap is not None and main_tap < main_tap_min
            )
            if impaired and pnm_conf >= context.threshold("hfc.pnm_confidence_min", 0.5):
                suspected = str(pnm.get("suspected_impairment") or "an impedance discontinuity")
                distance = _num(pnm, "distance_to_fault_m")
                where = f" about {distance:.0f} m from the modem" if distance is not None else ""
                pnm_note = f" Pre-equalisation points at {suspected}{where}."
                breaches.append(("PNM pre-equalisation", pnm_conf, 1.0))

        if not breaches:
            return self.ok(flags=flags)

        worst = max(breaches, key=lambda b: b[2])
        score = min(1.0, 0.45 + 0.1 * len(breaches) + min(worst[2], 3.0) * 0.08)
        severity = (
            Severity.CRITICAL
            if score >= 0.85
            else Severity.HIGH
            if score >= 0.65
            else Severity.MEDIUM
        )
        # A missing PNM capture costs confidence rather than blocking the finding: the RF levels
        # alone are enough to act on, they are just less specific about where the fault sits.
        confidence = 0.85 if pnm is not None else 0.7
        readings = ", ".join(f"{label} {value:g}" for label, value, _ in breaches)
        verdict = str(rf.get("rf_verdict") or "impairment")

        return self.ok(
            [
                self.finding(
                    context,
                    score=score,
                    confidence=confidence,
                    severity=severity,
                    explanation=(
                        f"DOCSIS RF is out of spec on {len(breaches)} measurement(s): {readings}. "
                        f"NXT calls this '{verdict}'.{pnm_note}"
                    ),
                    affected=(str(rf.get("service_ref") or ""),),
                    features=features,
                    recommended_tests=(TestKind.HFC_RF_LEVELS, TestKind.HFC_PNM_SWEEP),
                    flags=tuple(flags),
                    suspected_domain=(
                        FaultDomain.DROP if "upstream" in worst[0] else FaultDomain.DISTRIBUTION
                    ),
                    suspected_delimiter_ref=context.topology.delimiter_ref
                    if context.topology
                    else None,
                )
            ],
            flags=flags,
        )


class PONOpticalDegradationDetector(BaseDetector):
    """ONT optical levels, OMCI state and the dying-gasp signal.

    The dying gasp is handled before the optical levels, not after. An ONT that lost mains power
    reports no optical measurement at all, and a detector that checked levels first would read the
    resulting nulls as a catastrophic fibre fault and send a fibre crew to a house with no
    electricity. Power is the more likely and the cheaper explanation, so it is tested first.
    """

    name = "pon_optical_degradation"
    version = "1.0.0"
    requires = ("plant",)
    applies_to = (Technology.PON,)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        plant = context.payload("plant")
        optical = plant.get("optical") or {}
        if not isinstance(optical, dict) or not optical:
            return DetectorResult.unavailable(
                self.name, self.version, "no optical measurement in the snapshot"
            )

        flags: list[DataQualityFlag] = []
        features: dict[str, float] = {}
        ont_state = str(optical.get("ont_operational_state") or "")
        omci_state = str(optical.get("omci_state") or "")
        dying_gasp = _num(optical, "dying_gasp_events_24h") or 0.0
        los = _num(optical, "los_events_24h") or 0.0
        features["los_events_24h"] = los
        features["dying_gasp_events_24h"] = dying_gasp

        if dying_gasp > 0 or ont_state == "power_off":
            # Deliberately not an optical fault, and deliberately not a data-quality defect: the
            # measurement is absent for a reason the network told us, and the power/weather
            # detector is the one that should confirm the cause.
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.8,
                        confidence=0.75,
                        severity=Severity.HIGH,
                        explanation=(
                            f"ONT is {ont_state or 'unreachable'} (OMCI "
                            f"{omci_state or 'unknown'}) with {dying_gasp:g} dying-gasp event(s) "
                            "in 24h, so no optical measurement exists for this window. Check "
                            "utility power before treating this as a fibre fault."
                        ),
                        affected=(str(optical.get("service_ref") or ""),),
                        features=features,
                        recommended_tests=(TestKind.PON_OMCI_STATUS,),
                        suspected_domain=FaultDomain.POWER,
                        suspected_delimiter_ref=str(optical.get("odp_ref") or "") or None,
                    )
                ]
            )

        if omci_state == "not_present":
            # Registered on the port but not talking OMCI, and no dying gasp to explain it. That
            # is a provisioning or ONT fault rather than an optical one, and saying so keeps the
            # incident away from a fibre crew.
            return self.ok(
                [
                    self.finding(
                        context,
                        score=0.7,
                        confidence=0.7,
                        severity=Severity.HIGH,
                        explanation=(
                            "ONT is not present on OMCI while mains power looks intact. This is a "
                            "provisioning or ONT fault, not an optical one."
                        ),
                        affected=(str(optical.get("service_ref") or ""),),
                        features=features,
                        recommended_tests=(TestKind.PON_OMCI_STATUS, TestKind.PROVISIONING_CHECK),
                        suspected_domain=FaultDomain.PROVISIONING,
                        suspected_delimiter_ref=str(optical.get("odp_ref") or "") or None,
                    )
                ]
            )

        rx = _num(optical, "rx_optical_power_dbm")
        olt_rx = _num(optical, "olt_rx_from_ont_dbm")
        ber = _num(optical, "ber_estimate")
        if rx is None and olt_rx is None:
            flags.append(DataQualityFlag.MISSING_FIELD)
            return DetectorResult.unavailable(
                self.name,
                self.version,
                f"ONT reports {ont_state or 'no state'} and carries no optical levels",
                flags=flags,
            )

        rx_min = context.threshold("pon.rx_optical_power_min_dbm", -27.0)
        olt_rx_min = context.threshold("pon.olt_rx_min_dbm", -28.0)
        ber_max = context.threshold("pon.ber_max", 1e-8)
        los_max = context.threshold("pon.los_events_max", 1.0)

        breaches: list[tuple[str, float, float]] = []
        if rx is not None:
            features["rx_optical_power_dbm"] = rx
            if rx < rx_min:
                breaches.append(("ONT receive power", rx, rx_min - rx))
        if olt_rx is not None:
            features["olt_rx_from_ont_dbm"] = olt_rx
            if olt_rx < olt_rx_min:
                breaches.append(("OLT receive from ONT", olt_rx, olt_rx_min - olt_rx))
        if ber is not None:
            features["ber_estimate"] = ber
            if ber > ber_max:
                breaches.append(("bit error rate", ber, 3.0))
        if los > los_max:
            breaches.append(("loss-of-signal events", los, los - los_max))

        if not breaches:
            return self.ok(flags=flags)

        # Both directions attenuated is a span problem; one direction alone is more likely the
        # ONT or its patch lead, and that distinction is what decides whether a crew is needed.
        both_directions = any("ONT receive" in b[0] for b in breaches) and any(
            "OLT receive" in b[0] for b in breaches
        )
        worst = max(breaches, key=lambda b: b[2])
        score = min(1.0, 0.5 + 0.1 * len(breaches) + min(worst[2], 4.0) * 0.07)
        severity = Severity.CRITICAL if score >= 0.85 else Severity.HIGH
        readings = ", ".join(f"{label} {value:g}" for label, value, _ in breaches)
        verdict = str(optical.get("optical_verdict") or "attenuation")

        return self.ok(
            [
                self.finding(
                    context,
                    score=score,
                    confidence=0.85 if both_directions else 0.7,
                    severity=severity,
                    explanation=(
                        f"Optical budget is out of spec: {readings}. The PON adapter calls this "
                        f"'{verdict}'. "
                        + (
                            "Attenuation in both directions points at the shared span rather than "
                            "the ONT."
                            if both_directions
                            else "Only one direction is affected, which points at the ONT or its "
                            "patch lead rather than the fibre span."
                        )
                    ),
                    affected=(str(optical.get("service_ref") or ""),),
                    features=features,
                    recommended_tests=(TestKind.PON_OPTICAL_POWER, TestKind.PON_OMCI_STATUS),
                    flags=tuple(flags),
                    suspected_domain=(
                        FaultDomain.DISTRIBUTION if both_directions else FaultDomain.DROP
                    ),
                    suspected_delimiter_ref=str(optical.get("odp_ref") or "") or None,
                )
            ],
            flags=flags,
        )


__all__ = ["HFCRFDegradationDetector", "PONOpticalDegradationDetector"]
