"""KPI calculation. Every value is derived from `IncidentState`; nothing here is a constant.

The specification's rule is "do not hard-code KPI values as outcomes -- calculate them from event
timestamps and case history", and this module is where that is kept. Each method takes state and
returns a `KPIValue` or `None`. `None` means *not measurable for this incident yet* and produces no
event: a KPI emitted as `0.0` because the input was missing is indistinguishable, in a dashboard,
from a genuine zero, and the genuine zeros here are the good news (no truck rolls, no repeat
visits).

Three kinds of unavailability, kept apart because they need different answers:

* **not yet** -- `time_to_restore_seconds` before restoration. `calculate` returns `None`,
  `emit_all` skips it, and the next emission after validation will have it.
* **not derivable from one incident's state** -- `NOT_DERIVABLE_FROM_STATE`. Named explicitly, with
  what each would need. These are not emitted at all, and no placeholder value is invented for them.
* **no `KPIName` member exists** -- `SPEC_KPIS_WITHOUT_ENUM_MEMBER`. Several KPIs the specification
  lists have no member in `domain.enums.KPIName`, so they cannot be emitted without extending that
  enum. They are recorded here rather than quietly dropped.

Rates carry `numerator` and `denominator` (the `KPIEvent` validator insists, and it is right to:
averaging averages is wrong and a dashboard that does it reports a number nobody can reproduce). For
a single incident the denominator is usually 1 -- one incident's contribution -- and the aggregate
is `sum(numerator) / sum(denominator)` across events. A rate whose denominator would be 0 for this
incident (a self-help success rate for an incident that never used self-help) is not emitted,
because a 0/0 contribution is not a measurement.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from lpr_cpe.config.clock import Clock, SystemClock
from lpr_cpe.domain.base import new_id
from lpr_cpe.domain.enums import (
    ApprovalStatus,
    CaseType,
    KPIName,
    MRStatus,
    PolicyOutcome,
    ReasonCode,
    SelfHelpOutcome,
)
from lpr_cpe.domain.governance import KPIEvent
from lpr_cpe.graph.state import (
    IncidentState,
    current_mr_records,
    current_work_orders,
    open_mr_count,
    truck_roll_count,
)

# --------------------------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------------------------

UNIT_RATE: Final = "rate"
UNIT_SECONDS: Final = "seconds"
UNIT_COUNT: Final = "count"


@dataclass(frozen=True, slots=True)
class KPIValue:
    """A computed KPI, before it becomes an event.

    Separate from `KPIEvent` so a calculation can be unit-tested without a clock or an id generator,
    and so `emit` has exactly one job: turn a measurement into a record.
    """

    value: float
    unit: str
    numerator: float | None = None
    denominator: float | None = None

    @classmethod
    def rate(cls, numerator: float, denominator: float) -> KPIValue | None:
        """A rate, or `None` when the denominator is zero.

        The `None` is the whole point: an incident that never reached self-help contributes nothing
        to the self-help success rate, and contributing `0.0` would drag the fleet-wide rate down
        with a case that was never eligible.
        """
        if denominator <= 0:
            return None
        return cls(
            value=numerator / denominator,
            unit=UNIT_RATE,
            numerator=numerator,
            denominator=denominator,
        )

    @classmethod
    def seconds(cls, delta: timedelta | None) -> KPIValue | None:
        if delta is None:
            return None
        return cls(value=max(delta.total_seconds(), 0.0), unit=UNIT_SECONDS)

    @classmethod
    def count(cls, n: float) -> KPIValue:
        return cls(value=float(n), unit=UNIT_COUNT)


# --------------------------------------------------------------------------------------------
# The timestamp vocabulary
# --------------------------------------------------------------------------------------------


class MetricTimestamp(StrEnum):
    """Keys written into `state["metrics_timestamps"]`.

    This module is the *consumer* of those timestamps, so it owns their names. A node writing
    `"triage_done"` while this module reads `"triaged_at"` produces a KPI that is silently never
    measurable, and nothing fails -- which is why the vocabulary is an enum and `mark()` below is
    the only supported way to write one.

    `metrics_timestamps` is `dict[str, str]` in the state contract (ISO-8601 strings), not
    `datetime`, because it is checkpointed and round-tripped through JSON.
    """

    CREATED_AT = "created_at"
    DETECTED_AT = "detected_at"
    TRIAGED_AT = "triaged_at"
    DIAGNOSED_AT = "diagnosed_at"
    FIRST_ACTION_AT = "first_action_at"
    REMOTE_FIX_AT = "remote_fix_at"
    SELF_HELP_SENT_AT = "self_help_sent_at"
    DISPATCHED_AT = "dispatched_at"
    ON_SITE_AT = "on_site_at"
    HANDOVER_AT = "handover_at"
    MR_SUBMITTED_AT = "mr_submitted_at"
    RESTORED_AT = "restored_at"
    VALIDATED_AT = "validated_at"
    CLOSED_AT = "closed_at"
    CUSTOMER_FIRST_NOTIFIED_AT = "customer_first_notified_at"
    APPROVAL_REQUESTED_AT = "approval_requested_at"


def mark(key: MetricTimestamp, when: datetime) -> dict[str, dict[str, str]]:
    """The partial state update a node returns to record a KPI timestamp.

    Returns the whole `{"metrics_timestamps": {...}}` shape so a node writes
    `return {**other_updates, **mark(MetricTimestamp.TRIAGED_AT, now)}` and cannot accidentally
    replace the dict instead of merging into it -- `merge_dict` in the state contract handles the
    merge, but only if the update is shaped like this.

    Refuses a naive datetime. A KPI computed from a mixture of aware and naive timestamps raises
    deep inside an arithmetic expression, at emission time, on whichever incident happened to record
    the naive one.
    """
    if when.tzinfo is None:
        raise ValueError(f"metric timestamp {key} must be timezone-aware")
    return {"metrics_timestamps": {key.value: when.isoformat()}}


def stamp(update: MutableMapping[str, Any], key: MetricTimestamp, when: datetime) -> None:
    """Record a metric timestamp on an update **that may already carry one**. Use this, not `mark`.

    `mark` returns the whole `{"metrics_timestamps": {...}}` shape, and its own docstring explains
    that the shape is what makes `{**other_updates, **mark(...)}` safe. That is true of the literal
    form and false of the other one: `update.update(mark(...))` is a plain `dict.update` on the
    outer mapping, so it **replaces** the `metrics_timestamps` key rather than merging into it. The
    stamp already there is gone, silently, and `merge_dict` never sees it -- the reducer merges what
    a node *returned* into state, and by then the node has already dropped it.

    Found by a mutation sweep on 2026-08-24, at three sites where two stamps were written in one
    update and only the second survived:

    | site | written | kept |
    | --- | --- | --- |
    | `restoration_validation.assess_restoration` | `validated_at`, `restored_at` | `restored_at` |
    | `remote_resolution.verify_remote_repair` | `remote_fix_at`, `restored_at` | `restored_at` |
    | `field_execution.record_field_arrival` | `dispatched_at`, `on_site_at` | `on_site_at` |

    Each of the three sits directly under a comment asserting that both stamps matter and are
    different facts. No KPI reads any of the three lost keys today, so no number was wrong -- which
    is exactly why nothing noticed, and exactly why this is worth a helper rather than three fixes:
    the failure is silent at the moment it happens and only becomes visible when somebody finally
    reads the key.

    Merges rather than replaces, and returns `None` so it cannot be mistaken for `mark` at a call
    site. `mark` is kept for the literal form, which was always correct.
    """
    stamped = mark(key, when)["metrics_timestamps"]
    update["metrics_timestamps"] = {**update.get("metrics_timestamps", {}), **stamped}


def _read_ts(state: IncidentState, key: MetricTimestamp) -> datetime | None:
    """Parse a recorded timestamp, tolerating an absent or malformed one.

    A malformed timestamp yields `None` rather than raising: a KPI emission runs at the end of a
    node, and a bad string written days earlier must not be what stops an incident progressing. It
    shows up as a KPI that is never measurable, which is visible in the KPI stream itself.
    """
    raw = state.get("metrics_timestamps", {}).get(key.value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --------------------------------------------------------------------------------------------
# Honest gaps
# --------------------------------------------------------------------------------------------

NOT_DERIVABLE_FROM_STATE: Final[frozenset[KPIName]] = frozenset(
    {
        KPIName.PREDICTIVE_SCANS_RUN,
        KPIName.PREDICTIVE_TRUE_POSITIVE_RATE,
    }
)
"""KPIs that one incident's state cannot produce, and that this module therefore never emits.

Not emitting them is the point. Each could be given a plausible-looking value from an incident's
state, and each such value would be wrong in the same direction -- upward, because incidents are the
*successful* detections and the denominator lives outside them.

* `PREDICTIVE_SCANS_RUN` -- the twice-daily CPE scan is a batch detector job, explicitly *not* an
  incident thread (specification, Predictive CPE/Wi-Fi scan reference). A clean scan produces no
  `AssuranceEvent` and therefore no state. Needs: a counter emitted by the scan runner, per run,
  with the size of the population it swept.
* `PREDICTIVE_TRUE_POSITIVE_RATE` -- needs the full set of predictions with outcome labels,
  including every prediction that never became an incident (a true negative) and every threshold
  crossing that turned out to be nothing. An incident-scoped view contains only cases that crossed
  the threshold, so any rate computed from it is 1.0 by construction. Needs: a labelled prediction
  ledger and a follow-up window.
"""

SPEC_KPIS_WITHOUT_ENUM_MEMBER: Final[dict[str, str]] = {
    "truck_roll_after_remote_repair": (
        "derivable from this state (a truck roll following a successful remote fix) but has no "
        "KPIName member; needs one added to domain.enums.KPIName"
    ),
    "truck_roll_after_self_help": ("derivable from this state but has no KPIName member"),
    "operationally_avoidable_dispatch_rate": (
        "needs a policy definition of 'avoidable' (which reason codes count) plus a reviewer "
        "label; no KPIName member"
    ),
    "one_two_three_four_plus_visit_distribution": (
        "a distribution rather than a scalar; derivable per incident as a visit count, but needs "
        "either four KPIName members or a bucketed dimension"
    ),
    "correct_fault_domain_rate": (
        "needs the ground-truth fault domain from the closed case review; the state holds the "
        "predicted domain only"
    ),
    "correct_tap_odp_handover_rate": ("needs the ground-truth delimiter, same as above"),
    "repeat_mr_rate": (
        "needs the plant-object MR history across incidents; one incident cannot see the previous "
        "MR against the same tap"
    ),
    "premature_closure_rate": (
        "needs a reopen within a window, which is a fact about a LATER incident"
    ),
    "seven_day_reopen_rate": ("needs a 7-day follow-up window and the linked reopened incident"),
    "thirty_day_recurrence_rate": ("needs a 30-day window over the premises history"),
    "detection_precision_and_recall": (
        "needs labelled negatives; see PREDICTIVE_TRUE_POSITIVE_RATE in NOT_DERIVABLE_FROM_STATE"
    ),
    "rca_top_one_and_top_three_accuracy": (
        "needs the confirmed root cause to compare the ranked hypothesis list against; the field "
        "finding gives it only for dispatched cases"
    ),
    "customer_notification_latency": (
        "derivable from metrics_timestamps (created_at -> customer_first_notified_at) but has no "
        "KPIName member"
    ),
}
"""KPIs the specification's list names that `KPIName` has no member for.

Recorded rather than silently skipped. Extending `KPIName` is the fix for the ones marked derivable,
and that is a change to `domain.enums`, which owns the vocabulary -- inventing a string here would
create a second, competing vocabulary and the aggregation layer would see both.
"""


class KPINotDerivableError(ValueError):
    """Raised by `emit` when the requested KPI cannot be measured from this state.

    A distinct exception rather than a `None` return, because `emit` is the *deliberate* single-KPI
    path: a caller naming a KPI wants that KPI, and handing back `None` invites
    `event = emit(...) or fallback`. `emit_all` uses `calculate` and skips instead.
    """


# --------------------------------------------------------------------------------------------
# The calculator
# --------------------------------------------------------------------------------------------


class KPICalculator:
    """Derives every measurable KPI from `IncidentState`.

    Holds a `Clock` because four KPIs are relative to now (SLA breach, SLA at risk, approval wait,
    and restoration time for an incident that has been restored but not yet closed). Injected rather
    than read from the module, so a scenario test can assert an exact number instead of a range.
    """

    __slots__ = ("_calculators", "_clock")

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        # One dispatch table, built from bound methods. A `match` statement over `KPIName` would
        # compile just as well and would not be enumerable: this dict lets a test assert that every
        # `KPIName` member is either in here or in `NOT_DERIVABLE_FROM_STATE`, which is the check
        # that catches a KPI added to the enum and never implemented.
        self._calculators: dict[KPIName, Callable[[IncidentState], KPIValue | None]] = {
            KPIName.INCIDENTS_CREATED: self.incidents_created,
            KPIName.PROACTIVE_DETECTION_RATE: self.proactive_detection_rate,
            KPIName.TIME_TO_DETECT_SECONDS: self.time_to_detect,
            KPIName.TIME_TO_TRIAGE_SECONDS: self.time_to_triage,
            KPIName.TIME_TO_DIAGNOSE_SECONDS: self.time_to_diagnose,
            KPIName.TIME_TO_RESTORE_SECONDS: self.time_to_restore,
            KPIName.REMOTE_RESOLUTION_RATE: self.remote_resolution_rate,
            KPIName.SELF_HELP_SUCCESS_RATE: self.self_help_success_rate,
            KPIName.DISPATCH_AVOIDANCE_RATE: self.dispatch_avoidance_rate,
            KPIName.FIRST_TIME_FIX_RATE: self.first_time_fix_rate,
            KPIName.REPEAT_VISIT_RATE: self.repeat_visit_rate,
            KPIName.NO_FAULT_FOUND_RATE: self.no_fault_found_rate,
            KPIName.TRUCK_ROLLS_PER_INCIDENT: self.truck_rolls_per_incident,
            KPIName.HANDOVER_ACCEPTANCE_RATE: self.handover_acceptance_rate,
            KPIName.HANDOVER_REWORK_RATE: self.handover_rework_rate,
            KPIName.MR_CYCLE_TIME_SECONDS: self.mr_cycle_time,
            KPIName.MR_REJECTION_RATE: self.mr_rejection_rate,
            KPIName.PLANT_REPAIR_BACKLOG: self.plant_repair_backlog,
            KPIName.SLA_BREACH_RATE: self.sla_breach_rate,
            KPIName.SLA_AT_RISK_COUNT: self.sla_at_risk_count,
            KPIName.APPROVAL_WAIT_SECONDS: self.approval_wait,
            KPIName.APPROVAL_REJECTION_RATE: self.approval_rejection_rate,
            KPIName.POLICY_BLOCK_RATE: self.policy_block_rate,
            KPIName.AUTOMATION_COVERAGE_RATE: self.automation_coverage_rate,
            KPIName.DATA_QUALITY_DEFECT_RATE: self.data_quality_defect_rate,
            KPIName.CUSTOMER_CONTACTS_PER_INCIDENT: self.customer_contacts_per_incident,
        }

    # -- volume and detection ------------------------------------------------------------------

    def incidents_created(self, state: IncidentState) -> KPIValue | None:
        """One, once the incident exists. The denominator every other rate is aggregated against."""
        return KPIValue.count(1) if state.get("created_at") is not None else None

    def proactive_detection_rate(self, state: IncidentState) -> KPIValue | None:
        """Was this incident found before the customer told us?

        `CUSTOMER_REPORTED` and `REPEAT_VISIT` are reactive; the other four case types are ours. The
        distinction is the case type rather than the event source, because a customer-reported fault
        corroborated by an NXT alarm is still reactive -- we did not act first.
        """
        case_type = state.get("case_type")
        if case_type is None:
            return None
        proactive = case_type in {
            CaseType.PROACTIVE_ALARM,
            CaseType.PREDICTIVE_MAINTENANCE,
            CaseType.POST_INSTALL_BASELINE,
            CaseType.BULK_DEGRADATION,
        }
        return KPIValue.rate(1.0 if proactive else 0.0, 1.0)

    # -- durations -----------------------------------------------------------------------------

    def time_to_detect(self, state: IncidentState) -> KPIValue | None:
        """How long the signal took to reach us: the first event's `occurred_at` -> `received_at`.

        Uses `AssuranceEvent.detection_latency`, which clamps at zero, rather than subtracting here:
        a vendor clock running ahead of ours would otherwise produce a negative latency that
        averages into the KPI and quietly improves it.
        """
        events = state.get("events", [])
        if not events:
            return None
        first = min(events, key=lambda e: e.received_at)
        return KPIValue.seconds(first.detection_latency)

    def time_to_triage(self, state: IncidentState) -> KPIValue | None:
        """Incident creation -> triage complete. The spec's "mean time to incident creation" pairs
        with this: creation is `created_at`, and the gap to `triaged_at` is what triage cost."""
        start = state.get("created_at") or _read_ts(state, MetricTimestamp.CREATED_AT)
        end = _read_ts(state, MetricTimestamp.TRIAGED_AT)
        if start is None or end is None:
            return None
        return KPIValue.seconds(end - start)

    def time_to_diagnose(self, state: IncidentState) -> KPIValue | None:
        """Creation -> RCA concluded.

        Reads `rca.concluded_at` in preference to the recorded timestamp: the RCA object carries the
        authoritative instant, and a node that forgot to call `mark()` should still produce this
        KPI.
        """
        start = state.get("created_at") or _read_ts(state, MetricTimestamp.CREATED_AT)
        rca = state.get("rca")
        end = rca.concluded_at if rca is not None else _read_ts(state, MetricTimestamp.DIAGNOSED_AT)
        if start is None or end is None:
            return None
        return KPIValue.seconds(end - start)

    def time_to_restore(self, state: IncidentState) -> KPIValue | None:
        """End-to-end restoration: the SLA clock start -> proof that service was restored.

        The end is the *validation*, not the closure. Closure can lag restoration by the whole
        stability window plus a reconciliation pass, and reporting that as restoration time makes
        the KPI a measure of our paperwork rather than of the customer's outage.

        The start is `sla.clock_started_at` -- the one clock (D1) -- falling back to `created_at`
        for an incident whose SLA context is somehow absent.
        """
        sla = state.get("sla")
        start = sla.clock_started_at if sla is not None else state.get("created_at")
        if start is None:
            return None
        validation = state.get("validation")
        end: datetime | None = None
        if validation is not None and validation.passed:
            end = validation.validated_at
        else:
            end = _read_ts(state, MetricTimestamp.RESTORED_AT)
        if end is None:
            closure = state.get("closure")
            end = closure.closed_at if closure is not None else None
        if end is None:
            return None
        return KPIValue.seconds(end - start)

    # -- resolution lanes ----------------------------------------------------------------------

    def remote_resolution_rate(self, state: IncidentState) -> KPIValue | None:
        """Resolved by a verified remote action, with nobody sent out.

        `RemoteAction.fixed_it` is the test, and it requires `verification_passed is True` -- an ACS
        that acknowledged a reboot is not a reboot that fixed anything. A remote fix followed by a
        truck roll is not a remote resolution, which is why the truck-roll count is in the
        condition.
        """
        if not self._is_resolved(state):
            return None
        remote_fixed = any(a.fixed_it for a in state.get("remote_actions", []))
        resolved_remotely = remote_fixed and truck_roll_count(state) == 0
        return KPIValue.rate(1.0 if resolved_remotely else 0.0, 1.0)

    def self_help_success_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the incidents that went to guided self-help, how many the customer resolved.

        Denominator is 1 only when a session exists: an incident that never offered self-help is not
        a self-help failure.
        """
        session = state.get("self_help_session")
        if session is None:
            return None
        if session.outcome is SelfHelpOutcome.IN_PROGRESS:
            return None
        return KPIValue.rate(1.0 if session.outcome is SelfHelpOutcome.RESOLVED else 0.0, 1.0)

    def dispatch_avoidance_rate(self, state: IncidentState) -> KPIValue | None:
        """Resolved without a crew travelling: remotely, by self-help, or by plant work alone."""
        if not self._is_resolved(state):
            return None
        return KPIValue.rate(1.0 if truck_roll_count(state) == 0 else 0.0, 1.0)

    # -- field work ----------------------------------------------------------------------------

    def first_time_fix_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the incidents that needed a visit, how many were fixed on the first one.

        Denominator excludes incidents with no visit. Including them would make this KPI improve
        every time a remote fix succeeded, which is already `dispatch_avoidance_rate` -- two KPIs
        measuring one thing, one of which then never moves.
        """
        rolls = truck_roll_count(state)
        if rolls == 0 or not self._is_resolved(state):
            return None
        return KPIValue.rate(1.0 if rolls == 1 else 0.0, 1.0)

    def repeat_visit_rate(self, state: IncidentState) -> KPIValue | None:
        """Two or more truck rolls for one incident.

        Scoped to this incident deliberately. The other repeat-visit definition -- a second visit to
        the same premises within 30 days, on a *new* incident -- cannot be seen from one state and
        is listed in `SPEC_KPIS_WITHOUT_ENUM_MEMBER` as `thirty_day_recurrence_rate`.
        """
        rolls = truck_roll_count(state)
        if rolls == 0:
            return None
        return KPIValue.rate(1.0 if rolls >= 2 else 0.0, 1.0)

    def no_fault_found_rate(self, state: IncidentState) -> KPIValue | None:
        """The strict definition: a crew travelled and found nothing.

        Strict means *every* finding is no-fault-found and none confirmed a fault. A visit that
        found a plant fault at the tap and also cleared the inside wiring is not a no-fault-found
        visit, and the loose definition ("any NFF finding") would count it as one.
        """
        rolls = truck_roll_count(state)
        if rolls == 0:
            return None
        findings = state.get("field_findings", [])
        if not findings:
            return None
        nff = all(f.no_fault_found for f in findings) and not any(
            f.fault_confirmed for f in findings
        )
        return KPIValue.rate(1.0 if nff else 0.0, 1.0)

    def truck_rolls_per_incident(self, state: IncidentState) -> KPIValue | None:
        """This incident's truck-roll count, as a rate over one incident.

        A rate rather than a count so that "average dispatches per incident" re-aggregates
        correctly: `sum(numerator) / sum(denominator)` is the fleet average, whereas averaging
        per-incident counts would weight a one-incident hour the same as a hundred-incident one.
        """
        return KPIValue.rate(float(truck_roll_count(state)), 1.0)

    # -- handover and plant --------------------------------------------------------------------

    def handover_acceptance_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the Clean-to-Dirty handovers offered, how many OSP accepted."""
        contract = state.get("handover_contract")
        if contract is None or contract.accepted is None:
            return None
        return KPIValue.rate(1.0 if contract.accepted else 0.0, 1.0)

    def handover_rework_rate(self, state: IncidentState) -> KPIValue | None:
        """Handovers rejected as incomplete or wrong-domain -- the second-visit generator.

        Counts a rejection for either documented reason, and also an incomplete packet that was
        accepted anyway: `HandoverContract.completeness` is derived from `missing_items()`, so a
        packet below 1.0 cost OSP something to work with even if they took it.
        """
        contract = state.get("handover_contract")
        if contract is None or contract.accepted is None:
            return None
        rejected = contract.accepted is False
        rework = rejected or contract.completeness < 1.0
        return KPIValue.rate(1.0 if rework else 0.0, 1.0)

    def mr_cycle_time(self, state: IncidentState) -> KPIValue | None:
        """Mean submitted -> closed across this incident's MRs.

        `MRRecord.cycle_time()` returns `None` until the MR closes, so an incident with one closed
        and one open MR reports the closed one's cycle time rather than nothing.
        """
        cycles = [mr.cycle_time() for mr in current_mr_records(state).values() if mr.cycle_time()]
        durations = [c for c in cycles if c is not None]
        if not durations:
            return None
        total = sum(durations, timedelta(0))
        return KPIValue.seconds(total / len(durations))

    def mr_rejection_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the MRs actually filed, how many jTrack rejected.

        Denominator counts MRs that reached jTrack: a draft MR that was never submitted was never
        rejected, and including drafts would make the rate improve when the workflow stalls.
        """
        records = list(current_mr_records(state).values())
        submitted = [
            mr for mr in records if mr.submitted_at is not None or mr.status is not MRStatus.DRAFT
        ]
        if not submitted:
            return None
        rejected = sum(1 for mr in submitted if mr.status is MRStatus.REJECTED)
        return KPIValue.rate(float(rejected), float(len(submitted)))

    def plant_repair_backlog(self, state: IncidentState) -> KPIValue | None:
        """This incident's contribution to the OSP backlog: MRs filed and not yet finished.

        A count, not a rate: the backlog is a level, and the fleet figure is the sum across open
        incidents at a point in time. Emitted even when zero, because zero is the informative value
        here -- it is what says this incident is no longer waiting on plant.
        """
        if not state.get("mr_records"):
            return None
        return KPIValue.count(open_mr_count(state))

    # -- SLA -----------------------------------------------------------------------------------

    def sla_breach_rate(self, state: IncidentState) -> KPIValue | None:
        """Breached against the restore deadline.

        For a closed incident the answer is fixed -- measured at closure. For an open one it is
        measured against the clock now, so an incident that is currently in breach reports it rather
        than waiting for closure to admit it.
        """
        sla = state.get("sla")
        if sla is None:
            return None
        closure = state.get("closure")
        if closure is not None:
            if closure.sla_met is not None:
                return KPIValue.rate(0.0 if closure.sla_met else 1.0, 1.0)
            breached = closure.closed_at > sla.restore_deadline()
            return KPIValue.rate(1.0 if breached else 0.0, 1.0)
        return KPIValue.rate(1.0 if sla.is_breached(self._clock.now()) else 0.0, 1.0)

    def sla_at_risk_count(self, state: IncidentState) -> KPIValue | None:
        """1 while this incident is inside the at-risk fraction of its restore budget, else 0.

        `SLAContext.at_risk` counts a breached SLA as at risk, deliberately -- a dashboard where
        `at_risk` falls as incidents breach out of it reads as an improvement.
        """
        sla = state.get("sla")
        if sla is None or state.get("closure") is not None:
            return None
        return KPIValue.count(1 if sla.at_risk(self._clock.now()) else 0)

    # -- governance ----------------------------------------------------------------------------

    def approval_wait(self, state: IncidentState) -> KPIValue | None:
        """How long approvals have held this incident up.

        For the approval currently at an interrupt, the wait so far (`now - requested_at`). For
        decided ones, the wait needs the request instant, and `ApprovalDecision` carries only
        `decided_at` -- so it is read from `metrics_timestamps[approval_requested_at]`, which the
        approval node writes via `mark()`. Where neither is available this returns `None` rather
        than guessing: an approval wait of "0 seconds" would be indistinguishable from an instant
        approval, and instant approvals are the thing worth noticing.
        """
        pending = state.get("pending_approval")
        if pending is not None:
            return KPIValue.seconds(self._clock.now() - pending.requested_at)
        approvals = state.get("approvals", [])
        requested = _read_ts(state, MetricTimestamp.APPROVAL_REQUESTED_AT)
        if not approvals or requested is None:
            return None
        return KPIValue.seconds(approvals[-1].decided_at - requested)

    def approval_rejection_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the approvals decided, how many a human refused.

        Expired and withdrawn are excluded from the denominator: neither is a judgement about the
        request, and counting an expiry as an approval or a rejection misattributes a queueing
        failure to the operator.
        """
        decided = [
            a
            for a in state.get("approvals", [])
            if a.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        ]
        if not decided:
            return None
        rejected = sum(1 for a in decided if a.status is ApprovalStatus.REJECTED)
        return KPIValue.rate(float(rejected), float(len(decided)))

    def policy_block_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the actions policy evaluated, how many it blocked outright.

        `REQUIRES_APPROVAL` is not a block -- it is a gate that was passed through. Merging the two
        would make a well-governed incident look obstructed.
        """
        decisions = state.get("policy_decisions", [])
        if not decisions:
            return None
        blocked = sum(1 for d in decisions if d.outcome is PolicyOutcome.BLOCKED)
        return KPIValue.rate(float(blocked), float(len(decisions)))

    def automation_coverage_rate(self, state: IncidentState) -> KPIValue | None:
        """Of the actions that actually executed, how many needed no human.

        "Needed no human" means no `approval_ref` on the record. Denominator excludes actions that
        were blocked, skipped or left awaiting approval: those did not happen, and counting them
        would let the rate rise as more work was refused.

        `ActionRecord.was_attempted` is asked rather than re-spelled here. It owns "did this reach
        the external system", and this method was the second private copy its docstring warns
        about -- one that had already drifted, omitting `TIMED_OUT`.

        `TIMED_OUT` belongs in the denominator, and the argument for dropping it -- that we cannot
        confirm such an action executed -- does not survive the set it would leave behind. `FAILED`
        is *confirmed* not to have worked and is counted, so this denominator has never meant
        "took effect"; it means "we sent it". The approval was decided before the send in either
        case, so the outcome cannot change whether a human was asked. Dropping timeouts would also
        bias the rate upward: they concentrate in the slow network-affecting actions the pack gates
        behind approval, so the rows lost are the *attended* ones and coverage climbs the more work
        goes unconfirmed -- the same direction of error as counting refusals, above.
        """
        executed = [a for a in state.get("action_history", []) if a.was_attempted]
        if not executed:
            return None
        unattended = sum(1 for a in executed if not a.approval_ref)
        return KPIValue.rate(float(unattended), float(len(executed)))

    def data_quality_defect_rate(self, state: IncidentState) -> KPIValue | None:
        """Incidents worked with at least one data-quality flag raised.

        Requires an assessment to exist. An incident nobody assessed is not a clean incident, and
        emitting 0 for it would report our failure to look as good data quality.
        """
        assessment = state.get("data_quality")
        if assessment is None:
            return None
        return KPIValue.rate(1.0 if assessment.flags else 0.0, 1.0)

    def customer_contacts_per_incident(self, state: IncidentState) -> KPIValue | None:
        """Outbound customer communications, as a rate over one incident.

        Emitted from zero contacts upward: for a proactive incident fixed before the customer
        noticed, zero is the target and a missing KPI would hide the achievement.
        """
        contacts = len(state.get("customer_communications", []))
        return KPIValue.rate(float(contacts), 1.0)

    # -- shared predicates ---------------------------------------------------------------------

    def _is_resolved(self, state: IncidentState) -> bool:
        """Whether this incident has actually been restored.

        Three lane KPIs are conditional on it, and each of them would otherwise report a
        *provisional* answer that flips later: an incident with a successful remote fix and a
        pending validation is not yet a remote resolution, and emitting it as one means the KPI has
        to be retracted when the validation fails.
        """
        validation = state.get("validation")
        if validation is not None and validation.passed:
            return True
        closure = state.get("closure")
        return closure is not None and closure.closure_code in {
            ReasonCode.CLOSED_NORMAL,
            ReasonCode.CLOSED_EXCEPTIONAL,
        }

    # -- emission ------------------------------------------------------------------------------

    def dimensions(self, state: IncidentState) -> dict[str, str]:
        """The slicing every event carries: technology, area archetype, crew type, case type.

        One dimension set for every KPI, so `REPEAT_VISIT_RATE` and `NO_FAULT_FOUND_RATE` can be
        sliced the same way. Absent values are omitted rather than sent as `"unknown"`: `Technology`
        already has an `UNKNOWN` member that means "inventory has not been consulted", and a
        synthesised `"unknown"` string would be indistinguishable from it.
        """
        out: dict[str, str] = {}
        technology = state.get("technology")
        if technology is not None:
            out["technology"] = str(technology)
        case_type = state.get("case_type")
        if case_type is not None:
            out["case_type"] = str(case_type)
        topology = state.get("topology")
        if topology is not None and topology.area_archetype is not None:
            out["area_archetype"] = str(topology.area_archetype)
        crew_type = state.get("crew_type")
        if crew_type is not None:
            out["crew_type"] = str(crew_type)
        else:
            crews = {str(wo.crew_type) for wo in current_work_orders(state).values()}
            if len(crews) == 1:
                out["crew_type"] = crews.pop()
            elif crews:
                # Both crews worked this incident: that IS the joint case, and reporting one of them
                # would attribute the outcome to whichever happened to sort first.
                out["crew_type"] = "joint"
        return out

    def calculate(self, state: IncidentState, kpi_name: KPIName) -> KPIValue | None:
        """The value for one KPI, or `None` if it is not measurable from this state yet.

        Also `None` for a member of `NOT_DERIVABLE_FROM_STATE`: those are never measurable here, by
        construction, and the set says why.
        """
        calculator = self._calculators.get(kpi_name)
        if calculator is None:
            return None
        return calculator(state)

    def emit(
        self,
        state: IncidentState,
        kpi_name: KPIName,
        *,
        dimensions: dict[str, str] | None = None,
        emitted_at: datetime | None = None,
    ) -> KPIEvent:
        """Build the `KPIEvent` for one KPI. Raises `KPINotDerivableError` if it cannot be measured.

        `dimensions` is merged over the derived set rather than replacing it, so a caller can add a
        slice (a scan-run id, say) without dropping the technology.
        """
        if kpi_name in NOT_DERIVABLE_FROM_STATE:
            raise KPINotDerivableError(
                f"{kpi_name} cannot be derived from one incident's state; see "
                f"NOT_DERIVABLE_FROM_STATE for what it needs instead"
            )
        computed = self.calculate(state, kpi_name)
        if computed is None:
            raise KPINotDerivableError(
                f"{kpi_name} is not measurable for incident {state.get('incident_id')} yet: the "
                "inputs it is derived from are absent, and a placeholder value would be a fiction"
            )
        merged = self.dimensions(state)
        if dimensions:
            merged.update(dimensions)
        return KPIEvent(
            event_id=new_id("KPI"),
            kpi_name=kpi_name,
            emitted_at=emitted_at if emitted_at is not None else self._clock.now(),
            value=computed.value,
            unit=computed.unit,
            incident_id=state.get("incident_id"),
            dimensions=merged,
            numerator=computed.numerator,
            denominator=computed.denominator,
        )

    def emit_all(self, state: IncidentState) -> list[KPIEvent]:
        """Every KPI measurable from this state right now, in `KPIName` declaration order.

        Skips what is not yet measurable and everything in `NOT_DERIVABLE_FROM_STATE`. Declaration
        order rather than insertion order so the emitted list is stable between runs -- the state
        reducer de-duplicates on `event_id`, but a test asserting on the sequence should not depend
        on dict ordering.

        Safe to call on every super-step: `append_unique` keys on `event_id`, which is fresh per
        emission, so repeated calls append rather than replace. Callers are expected to emit at
        stage boundaries, not per node.
        """
        events: list[KPIEvent] = []
        for kpi_name in KPIName:
            if kpi_name in NOT_DERIVABLE_FROM_STATE:
                continue
            computed = self.calculate(state, kpi_name)
            if computed is None:
                continue
            events.append(self.emit(state, kpi_name))
        return events

    def implemented(self) -> Sequence[KPIName]:
        """Every KPI this calculator can derive. For the docs generator and the coverage test."""
        return tuple(k for k in KPIName if k in self._calculators)
