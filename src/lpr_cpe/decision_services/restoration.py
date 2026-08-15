"""Did the fix hold? Measured over a window, not asserted at the moment of the fix.

`ValidationResult` refuses to be constructed with `passed=True` and no window, and `ClosureRecord`
refuses a normal closure without a passed validation. This module is what produces the record those
two rules are about, so it is where "proof before closure" is either real or a formality.

The failure it is written against is the thirty-second all-clear: a modem is rebooted, comes back,
one good telemetry read arrives, and the incident closes. Everything about that sequence is true and
none of it is evidence that the fault is gone -- it is evidence that the reboot completed. Three
independent conditions have to hold instead, and each one exists because it catches a case the
others do not:

* **The window has elapsed.** A marginal fault needs time to reappear.
* **Enough samples fell inside it.** A window with one reading in it is a moment with a long name.
* **The anomaly is gone, not smaller.** The pack asks for 70% of the original anomaly score to have
  disappeared, because a fault that improved is a fault that is still there.

And a fourth for the domains where telemetry cannot answer the question at all: a Wi-Fi coverage
complaint can have perfect readings on every metric while the customer still cannot use the far
bedroom, so the pack names those domains and the customer's own answer is required.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from lpr_cpe.domain.closure import ValidationResult
from lpr_cpe.domain.diagnosis import AnomalyFinding
from lpr_cpe.domain.enums import ActionType, FaultDomain, ReasonCode
from lpr_cpe.policies.models import ValidationPolicy

#: Fixes whose aftermath gets the longer window. Keyed on the *action taken*, not on the fault
#: domain, because the pack's reason for the longer window is that "a plant repair affects many
#: services" -- that is a statement about what was done. A CPE reboot performed to clear a symptom
#: of a distribution fault is still a CPE reboot, and holding it to the plant window would delay
#: every closure behind a misdiagnosis.
_PLANT_FIXES: frozenset[ActionType] = frozenset(
    {ActionType.RAISE_MR, ActionType.NODE_LEVEL_RESET, ActionType.OLT_PORT_RESET}
)


def stability_window(action: ActionType | None, policy: ValidationPolicy) -> timedelta:
    """How long service must be observed good after this kind of fix.

    `None` -- no action recorded -- takes the plant window rather than the shorter one. An
    unrecorded fix is not evidence of a small fix, and the direction that costs something is the
    other one: a premature all-clear on a plant repair is multiplied by everyone behind the element.
    """
    minutes = (
        policy.stability_window_minutes
        if action is not None and action not in _PLANT_FIXES
        else policy.stability_window_plant_minutes
    )
    return timedelta(minutes=minutes)


def anomaly_reduction(
    before: Sequence[AnomalyFinding],
    after: Sequence[AnomalyFinding],
) -> float | None:
    """What fraction of the original anomaly has gone, or `None` if there was none to go.

    The peak score on each side rather than the mean. A fix that cleared four minor findings and
    left the severe one has not restored the service, and averaging would report it as a 60%
    improvement.

    `None` when nothing scored before the fix: there is no denominator, and returning `0.0` would
    read as "the anomaly is entirely still present" for an incident whose detectors never found one.
    The caller decides what that means; `validate_restoration` treats it as satisfied and says so in
    the summary, because a criterion with no measurement behind it cannot be the thing that blocks a
    closure.
    """
    peak_before = max((f.score for f in before), default=0.0)
    if peak_before <= 0:
        return None
    peak_after = max((f.score for f in after), default=0.0)
    return max(0.0, min(1.0, (peak_before - peak_after) / peak_before))


def compare_kpis(
    before: Mapping[str, float],
    after: Mapping[str, float],
    higher_is_better: Mapping[str, bool],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split shared metrics into improved, regressed and undirected.

    `higher_is_better` has no default and no fallback, and metrics missing from it land in the third
    tuple rather than being guessed at. Direction cannot be inferred from a metric's name: `snr_db`
    rising is an improvement, `packet_loss_pct` rising is the fault getting worse, and optical
    `rx_power_dbm` is bad in *both* directions because an overloaded receiver saturates. A rule
    that got any one of those backwards would report a deteriorating service as restored, and
    `ValidationResult._pass_requires_a_window` refuses a pass with regressed metrics -- so a
    misclassified regression is not merely mislabelled, it lets a closure through.
    """
    improved: list[str] = []
    regressed: list[str] = []
    undirected: list[str] = []
    for metric in sorted(set(before) & set(after)):
        direction = higher_is_better.get(metric)
        if direction is None:
            undirected.append(metric)
            continue
        delta = after[metric] - before[metric]
        if delta == 0:
            continue
        if (delta > 0) == direction:
            improved.append(metric)
        else:
            regressed.append(metric)
    return tuple(improved), tuple(regressed), tuple(undirected)


def validate_restoration(
    *,
    validation_id: str,
    incident_id: str,
    validated_at: datetime,
    window_start: datetime,
    fault_domain: FaultDomain,
    policy: ValidationPolicy,
    action_taken: ActionType | None = None,
    samples_in_window: int = 0,
    findings_before: Sequence[AnomalyFinding] = (),
    findings_after: Sequence[AnomalyFinding] = (),
    kpi_before: Mapping[str, float] | None = None,
    kpi_after: Mapping[str, float] | None = None,
    higher_is_better: Mapping[str, bool] | None = None,
    customer_confirmed: bool | None = None,
    evidence_refs: Sequence[str] = (),
) -> ValidationResult:
    """Assemble the validation record for one attempt at proving the fix held.

    The three outcomes are not two. `STABILITY_WINDOW_PENDING` means *ask again later* -- the window
    has not elapsed, the samples have not arrived, or the customer has not answered.
    `VALIDATION_FAILED` means *this fix did not work*, and the incident goes back for another cycle.
    Collapsing pending into failed would send every incident round again the moment it was fixed;
    collapsing failed into pending would leave a failed fix sitting in a window that will never
    produce a different answer.

    Written to be called repeatedly as samples arrive, returning a new record each time rather than
    mutating one -- `ValidationResult` is frozen, and the audit trail keeps every attempt.
    """
    window = stability_window(action_taken, policy)
    kpi_b = dict(kpi_before or {})
    kpi_a = dict(kpi_after or {})
    improved, regressed, undirected = compare_kpis(kpi_b, kpi_a, higher_is_better or {})

    reduction = anomaly_reduction(findings_before, findings_after)
    reasons: list[str] = []

    window_complete = validated_at >= window_start + window
    if not window_complete:
        remaining = (window_start + window) - validated_at
        reasons.append(
            f"the {int(window.total_seconds() // 60)}-minute stability window has "
            f"{int(remaining.total_seconds() // 60)} minutes left"
        )
    if samples_in_window < policy.min_post_fix_samples:
        reasons.append(
            f"{samples_in_window} of the required {policy.min_post_fix_samples} post-fix samples "
            "have arrived"
        )
    pending = bool(reasons)

    failures: list[str] = []
    if reduction is None:
        reasons.append(
            "no anomaly was scored before the fix, so there is no reduction to measure and this "
            "criterion is treated as met"
        )
    elif reduction < policy.min_anomaly_reduction:
        failures.append(
            f"{reduction:.0%} of the original anomaly has cleared, short of the "
            f"{policy.min_anomaly_reduction:.0%} the policy pack requires"
        )
    if regressed:
        failures.append(f"these metrics got worse: {', '.join(regressed)}")
    if undirected:
        reasons.append(
            f"no direction was given for {', '.join(undirected)}, so they counted neither way"
        )

    confirmation_required = fault_domain in policy.require_customer_confirmation_for_domains
    if confirmation_required:
        if customer_confirmed is False:
            failures.append(
                "the customer says the problem is not fixed, which for this fault domain outranks "
                "the telemetry"
            )
        elif customer_confirmed is None:
            pending = True
            reasons.append(
                f"a {fault_domain.value} fault needs the customer's confirmation and they have not "
                "answered yet; telemetry cannot see what they are complaining about"
            )

    if failures:
        reason_code = ReasonCode.VALIDATION_FAILED
        passed = False
        summary = "Validation failed: " + "; ".join(failures) + "."
    elif pending:
        reason_code = ReasonCode.STABILITY_WINDOW_PENDING
        passed = False
        summary = "Validation pending: " + "; ".join(reasons) + "."
    else:
        reason_code = ReasonCode.VALIDATED_STABLE
        passed = True
        detail = (
            f"{reduction:.0%} of the anomaly cleared"
            if reduction is not None
            else "no anomaly was scored before the fix"
        )
        summary = (
            f"Service held for {int(window.total_seconds() // 60)} minutes over "
            f"{samples_in_window} samples; {detail}."
        )

    return ValidationResult(
        validation_id=validation_id,
        incident_id=incident_id,
        validated_at=validated_at,
        window_start=window_start,
        stability_window=window,
        samples_in_window=samples_in_window,
        min_samples_required=policy.min_post_fix_samples,
        passed=passed,
        kpi_before=kpi_b,
        kpi_after=kpi_a,
        improved_metrics=improved,
        regressed_metrics=regressed,
        customer_confirmed=customer_confirmed,
        evidence_refs=tuple(evidence_refs),
        reason_code=reason_code,
        summary=summary,
    )


__all__ = [
    "anomaly_reduction",
    "compare_kpis",
    "stability_window",
    "validate_restoration",
]
