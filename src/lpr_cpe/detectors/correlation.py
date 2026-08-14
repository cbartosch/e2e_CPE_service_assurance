"""The three detectors that look outside the subject: neighbours, changes, and the environment.

All three exist to stop the workflow from repairing a symptom. A tap with five degraded homes is
one plant fault, not five drop faults; a service that broke four hours after an amplifier
realignment is a change to roll back rather than a fault to diagnose; and a dark ONT inside an open
utility outage footprint needs no crew at all. Each of these is cheaper than the alternative, which
is why they run before the localiser rather than after it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lpr_cpe.detectors.base import BaseDetector, DetectionContext, DetectorResult
from lpr_cpe.domain.enums import (
    DataQualityFlag,
    FaultDomain,
    Severity,
    TestKind,
)


def _num(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _hours_between(then: object, now: datetime) -> float | None:
    """Hours from an ISO timestamp to `now`, or `None` if it will not parse.

    Returns `None` rather than 0.0 on a bad timestamp: zero would mean "just now", which is the
    most alarming possible answer and exactly the wrong default for a value we failed to read.
    """
    if isinstance(then, datetime):
        parsed = then
    elif isinstance(then, str):
        try:
            parsed = datetime.fromisoformat(then)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or now.tzinfo is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


class CommonCauseClusterDetector(BaseDetector):
    """Are the neighbours behind the same delimiter degraded too?

    The fraction matters more than the count. Three degraded homes behind an 8-way tap is a
    different claim from three behind a 128-way splitter, and only the fraction distinguishes
    "shared plant fault" from "three unrelated drop faults that happen to be nearby".
    """

    name = "common_cause_cluster"
    version = "1.0.0"
    requires = ("plant",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        plant = context.payload("plant")
        delimiter = plant.get("delimiter")
        if not isinstance(delimiter, dict) or not delimiter:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "no delimiter view in the snapshot, so neighbours cannot be compared",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        in_service = _num(delimiter, "services_in_service")
        degraded = _num(delimiter, "degraded_count")
        if in_service is None or degraded is None or in_service <= 0:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                "delimiter view carries no serviceable counts",
                flags=[DataQualityFlag.MISSING_FIELD],
            )

        fraction = degraded / in_service
        min_peers = context.threshold("cluster.min_degraded_peers", 2.0)
        min_fraction = context.threshold("cluster.min_degraded_fraction", 0.34)
        features = {
            "degraded_count": degraded,
            "services_in_service": in_service,
            "degraded_fraction": round(fraction, 4),
        }
        flags: list[DataQualityFlag] = []
        if in_service < context.threshold("cluster.min_sample_for_confidence", 4.0):
            # A 2-of-2 cluster is arithmetically 100% and evidentially almost nothing.
            flags.append(DataQualityFlag.LOW_SAMPLE_COUNT)

        if degraded < min_peers or fraction < min_fraction:
            return self.ok(flags=flags)

        delimiter_ref = str(delimiter.get("delimiter_ref") or "")
        kind = str(delimiter.get("delimiter_kind") or "delimiter")
        score = min(1.0, 0.5 + fraction * 0.5)
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(score, 4),
                    confidence=0.6 if flags else 0.85,
                    severity=Severity.HIGH if fraction >= 0.5 else Severity.MEDIUM,
                    explanation=(
                        f"{degraded:g} of {in_service:g} services behind {kind} {delimiter_ref} "
                        f"are degraded ({fraction:.0%}). This is one shared fault, not "
                        f"{degraded:g} separate ones, and repairing it per-customer would mean "
                        f"{degraded:g} truck rolls for a single cause."
                    ),
                    affected=(delimiter_ref,),
                    features=features,
                    recommended_tests=(TestKind.NEIGHBOUR_COMPARISON,),
                    flags=tuple(flags),
                    suspected_domain=FaultDomain.TAP_OR_ODP,
                    suspected_delimiter_ref=delimiter_ref or None,
                )
            ],
            flags=flags,
        )


class RecentChangeDetector(BaseDetector):
    """Did somebody touch this plant just before it broke?

    Only changes inside the correlation window count. A firmware upgrade 430 hours ago is not the
    reason a service degraded this morning, and a detector without a window would happily blame it
    -- which is worse than saying nothing, because it sends the investigation somewhere false.
    """

    name = "recent_change"
    version = "1.0.0"
    requires = ("recent_changes",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        changes = context.rows("recent_changes")
        window_hours = context.threshold("change.correlation_window_hours", 72.0)
        if not changes:
            return self.ok()

        correlated: list[tuple[dict[str, Any], float]] = []
        undated = 0
        for change in changes:
            age = _hours_between(change.get("changed_at"), context.now)
            if age is None:
                offset = _num(change, "changed_offset_hours")
                age = abs(offset) if offset is not None else None
            if age is None:
                undated += 1
                continue
            if age <= window_hours:
                correlated.append((change, age))

        flags = [DataQualityFlag.MISSING_FIELD] if undated else []
        if not correlated:
            return self.ok(flags=flags)

        change, age = min(correlated, key=lambda c: c[1])
        refs = [str(r) for r in (change.get("object_refs") or [])]
        change_type = str(change.get("change_type") or "a plant change")
        # Recency is the evidence. Something done 2 hours ago is a far stronger candidate than
        # something done 70 hours ago, even though both are inside the window.
        score = min(1.0, 0.9 - (age / window_hours) * 0.45)
        return self.ok(
            [
                self.finding(
                    context,
                    score=round(score, 4),
                    confidence=0.7,
                    severity=Severity.MEDIUM,
                    explanation=(
                        f"{change_type} ({change.get('change_ref')}) touched this plant "
                        f"{age:.0f}h ago, inside the {window_hours:.0f}h correlation window. "
                        "Check whether backing it out restores service before diagnosing further."
                    ),
                    affected=tuple(refs),
                    features={
                        "change_age_hours": round(age, 2),
                        "changes_in_window": float(len(correlated)),
                    },
                    recommended_tests=(TestKind.PROVISIONING_CHECK,),
                    flags=tuple(flags),
                    suspected_domain=FaultDomain.PROVISIONING,
                )
            ],
            flags=flags,
        )


class PowerWeatherCorrelationDetector(BaseDetector):
    """Utility power and weather around the subject.

    An open outage whose footprint contains the customer is the cheapest possible explanation for a
    dark CPE, and confirming it is what stops a fibre crew being sent to a house with no
    electricity. Weather is reported separately because it does not explain a fault -- it
    constrains whether anyone can safely work on one.
    """

    name = "power_weather_correlation"
    version = "1.0.0"
    requires = ("power_outages",)

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        outages = context.rows("power_outages")
        weather = context.weather or {}
        findings = []
        flags: list[DataQualityFlag] = []

        open_outages = [
            o
            for o in outages
            if o.get("open") is True or str(o.get("status") or "").lower() == "open"
        ]
        covering = [o for o in open_outages if o.get("point_inside_footprint") is True]
        # Fall back to the radius when the adapter did not pre-compute containment.
        if not covering:
            for outage in open_outages:
                distance = _num(outage, "distance_km")
                radius = _num(outage, "radius_km")
                if distance is not None and radius is not None and distance <= radius:
                    covering.append(outage)

        if covering:
            outage = min(covering, key=lambda o: _num(o, "distance_km") or 0.0)
            affected_count = _num(outage, "customers_affected") or 0.0
            restore = outage.get("estimated_restore_at")
            hours = _hours_between(restore, context.now)
            eta = (
                f" Utility estimates restoration in about {abs(hours):.0f}h."
                if hours is not None and hours < 0
                else ""
            )
            findings.append(
                self.finding(
                    context,
                    score=0.9,
                    confidence=0.9,
                    severity=Severity.HIGH,
                    explanation=(
                        f"An open utility outage ({outage.get('outage_ref')}, cause "
                        f"{outage.get('cause')}) covers this location and affects "
                        f"{affected_count:g} customers. Service loss here is explained by mains "
                        f"power, and no network repair is warranted until power returns.{eta}"
                    ),
                    affected=(str(outage.get("outage_ref") or ""),),
                    features={
                        "outage_distance_km": _num(outage, "distance_km") or 0.0,
                        "customers_affected": affected_count,
                    },
                    recommended_tests=(),
                    suspected_domain=FaultDomain.POWER,
                )
            )

        if weather:
            unsafe = weather.get("field_work_safe") is False
            lightning = weather.get("lightning_within_10km") is True
            if unsafe or lightning:
                advisory = str(weather.get("advisory") or "").strip()
                findings.append(
                    self.finding(
                        context,
                        score=0.5,
                        confidence=0.8,
                        severity=Severity.MEDIUM,
                        # Deliberately INFO-adjacent in meaning: this does not diagnose the fault,
                        # it constrains the response. Scoring it as a fault would let weather
                        # outrank the actual cause in the ranked hypothesis list.
                        explanation=(
                            "Conditions at the customer location are unsafe for field work"
                            + (f": {advisory}" if advisory else "")
                            + ". This constrains dispatch; it is not itself a fault."
                        ),
                        affected=(),
                        features={
                            "wind_kph": _num(weather, "wind_kph") or 0.0,
                            "rain_mm_1h": _num(weather, "rain_mm_1h") or 0.0,
                        },
                        recommended_tests=(),
                        # No domain, deliberately. Weather is not a place a fault can be, and
                        # naming one made it vote: tagged CUSTOMER_ENVIRONMENT it counted as soft
                        # evidence in the no-fault-found scorer, so a lightning advisory over a
                        # healthy premises read as "the problem is inside the home" and argued
                        # against dispatching to a plant fault the localiser had already found.
                        suspected_domain=None,
                    )
                )
        else:
            flags.append(DataQualityFlag.MISSING_FIELD)

        return self.ok(findings, flags=flags)


__all__ = [
    "CommonCauseClusterDetector",
    "PowerWeatherCorrelationDetector",
    "RecentChangeDetector",
]
