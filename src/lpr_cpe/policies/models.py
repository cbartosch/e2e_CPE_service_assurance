"""The policy pack as types.

`pack.yaml` is data an operator edits. This module is what turns it into something the engine can
trust: every section is a Pydantic model with `extra="forbid"`, and the cross-section rules that
YAML cannot express are model validators. The result is that a malformed pack fails at load, naming
the row, rather than producing a `None` that surfaces four layers away as a permissive default.

Three kinds of check live here, and the distinction matters:

**Shape.** `extra="forbid"` plus field types. Catches `min_sources_for_dispath: 3`, which a
`dict[str, Any]` pack would accept and then quietly ignore while the real threshold stayed at its
default. This is the whole reason the pack is not read as a plain mapping.

**Vocabulary.** Every key that names an `ActionType`, `ApprovalKind`, `Severity`, `AreaArchetype`,
`FaultDomain`, `HealthBand`, `DataQualityFlag` or `rbac.Role` is parsed *into* that enum. A pack
naming `senior_engineer` as an approver role is refused, because `Role` has no such member and an
approval gate whose required role nobody can hold is a gate that can never be passed. That was a
real defect in the first draft of `pack.yaml`, found by reading two files side by side -- which is
exactly the method that does not scale, hence these validators.

**Coherence between sections.** `remote_actions[*].risk` must name a defined risk class;
`max_attempts_key` must name a defined attempt limit; `approvals[*].required_role` must be a role
that `rbac.approvers_for()` agrees can approve that kind; `blocking_flags` must be a superset of the
code's floor. These are the rules that make the pack a single coherent document rather than fifteen
independent ones, and none of them can be expressed in the YAML itself.

What is deliberately *not* here: defaults for thresholds a detector owns. `detector_thresholds` is a
free mapping precisely because each detector states its own default at the call site, and mirroring
those defaults here would create a second owner for every one of them.
"""

from __future__ import annotations

from datetime import time
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    AreaArchetype,
    DataQualityFlag,
    FaultDomain,
    HealthBand,
    Severity,
)
from lpr_cpe.domain.records import DataQualityAssessment
from lpr_cpe.security.rbac import Role, approvers_for

#: A probability or fraction. Named so the bound appears once rather than in fourteen fields.
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
#: A duration in minutes, at least one. Zero would mean "no window", which every caller of these
#: fields would have to special-case; a pack that means that should omit the row.
Minutes = Annotated[int, Field(ge=1)]


class PackSection(BaseModel):
    """Base for every section.

    `extra="forbid"` is the load-bearing setting: it is what makes a mistyped key a startup failure.
    `frozen=True` because the engine caches a loaded pack per file digest and hands the same
    instance
    to every caller -- a mutable section would let one node's convenience edit become another
    incident's policy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# -------------------------------------------------------------------------------------------------
# 1-2. Evidence
# -------------------------------------------------------------------------------------------------


class EvidencePolicy(PackSection):
    min_sources_for_diagnosis: int = Field(ge=1)
    min_sources_for_remote_action: int = Field(ge=1)
    min_sources_for_dispatch: int = Field(ge=1)
    min_sources_for_closure: int = Field(ge=1)

    max_telemetry_age_minutes: Minutes
    max_topology_age_minutes: Minutes
    max_sla_age_minutes: Minutes
    max_age_for_dispatch_minutes: Minutes

    blocking_flags: tuple[DataQualityFlag, ...]

    @model_validator(mode="after")
    def _cannot_loosen_the_code_floor(self) -> Self:
        # The pack may block on more than the code does, never on less. Without this check the row
        # reads as an operator control over a safety floor, and the first person to shorten the list
        # would silently permit automated action on data the models already consider unsafe.
        floor = DataQualityAssessment.BLOCKING_FLAGS
        missing = floor - set(self.blocking_flags)
        if missing:
            raise ValueError(
                "evidence.blocking_flags must be a superset of "
                "DataQualityAssessment.BLOCKING_FLAGS; missing "
                f"{sorted(f.value for f in missing)}. The pack may tighten this rule, not loosen it"
            )
        return self

    @model_validator(mode="after")
    def _dispatch_is_the_strictest_gate(self) -> Self:
        # Dispatch is the expensive, hard-to-reverse decision. A pack that asked for less
        # corroborated or more stale evidence before sending a truck than before rebooting a modem
        # is almost certainly a mistake in editing, and it is worth refusing rather than honouring.
        if self.min_sources_for_dispatch < self.min_sources_for_remote_action:
            raise ValueError(
                f"min_sources_for_dispatch ({self.min_sources_for_dispatch}) is below "
                f"min_sources_for_remote_action ({self.min_sources_for_remote_action}): sending a "
                "crew cannot need less corroboration than a remote fix"
            )
        if self.max_age_for_dispatch_minutes > self.max_telemetry_age_minutes * 4:
            raise ValueError(
                f"max_age_for_dispatch_minutes ({self.max_age_for_dispatch_minutes}) exceeds four "
                f"telemetry polling intervals ({self.max_telemetry_age_minutes}): a crew would be "
                "sent on data that may predate the fault"
            )
        return self

    def max_age_minutes_for(self, *, dispatch: bool) -> int:
        """The freshness window that applies to the decision at hand.

        One method rather than each caller picking a field, because "which window applies" is the
        part that gets got wrong -- and getting it wrong in the permissive direction is invisible.
        """
        return self.max_age_for_dispatch_minutes if dispatch else self.max_telemetry_age_minutes

    def min_sources_for(self, decision_class: str) -> int:
        """The corroboration bar for a named decision, defaulting to the strictest.

        Same fail-closed lookup as `RCAPolicy.minimum_for`: an unrecognised decision class is held
        to the highest bar in the section rather than the lowest. A misspelled class at a new call
        site should make the engine harder to satisfy, not easier -- the first is a visible bug
        report from an operator, the second is an action that should not have happened.
        """
        return {
            "diagnosis": self.min_sources_for_diagnosis,
            "remote_action": self.min_sources_for_remote_action,
            "dispatch": self.min_sources_for_dispatch,
            "mr": self.min_sources_for_dispatch,
            "closure": self.min_sources_for_closure,
        }.get(
            decision_class,
            max(
                self.min_sources_for_diagnosis,
                self.min_sources_for_remote_action,
                self.min_sources_for_dispatch,
                self.min_sources_for_closure,
            ),
        )


# -------------------------------------------------------------------------------------------------
# 3. RCA confidence
# -------------------------------------------------------------------------------------------------


class RCAPolicy(PackSection):
    min_for_autonomous_action: Fraction
    min_for_remote_action: Fraction
    min_for_dispatch: Fraction
    min_for_mr: Fraction
    review_below: Fraction
    ambiguity_margin: Fraction

    @model_validator(mode="after")
    def _review_is_below_every_action_bar(self) -> Self:
        # If `review_below` ever rose above an action threshold there would be a confidence band
        # that both required human review and permitted autonomous action, and which of the two
        # happened would depend on the order the engine asked its questions. That is the worst kind
        # of bug: correct-looking code, order-dependent safety.
        bars = {
            "min_for_remote_action": self.min_for_remote_action,
            "min_for_dispatch": self.min_for_dispatch,
            "min_for_mr": self.min_for_mr,
            "min_for_autonomous_action": self.min_for_autonomous_action,
        }
        overlapping = {name: v for name, v in bars.items() if v <= self.review_below}
        if overlapping:
            raise ValueError(
                f"rca.review_below ({self.review_below}) is at or above {overlapping}: that "
                "leaves a confidence band which is simultaneously reviewable and actionable"
            )
        return self

    def minimum_for(self, kind: str) -> float:
        """The confidence bar for a named decision, defaulting to the strictest.

        An unrecognised decision name gets the *highest* bar in the section, not a permissive one.
        This is the fail-closed rule applied to a lookup: a new call site that misspells its kind is
        held to the autonomous-action standard rather than to none.
        """
        return {
            "remote_action": self.min_for_remote_action,
            "dispatch": self.min_for_dispatch,
            "mr": self.min_for_mr,
            "autonomous": self.min_for_autonomous_action,
        }.get(
            kind,
            max(
                self.min_for_autonomous_action,
                self.min_for_remote_action,
                self.min_for_dispatch,
                self.min_for_mr,
            ),
        )


# -------------------------------------------------------------------------------------------------
# 4-5. Risk classes and the remote-action allowlist
# -------------------------------------------------------------------------------------------------


class RiskClass(PackSection):
    requires_approval: bool
    approval_kind: ApprovalKind | None = None
    max_blast_radius: int = Field(ge=0)
    reversible: bool

    @model_validator(mode="after")
    def _approval_names_its_interrupt(self) -> Self:
        # `PolicyDecision` refuses a REQUIRES_APPROVAL outcome without an approval kind, because the
        # graph would not know which interrupt to raise. Catching it here means the failure is a
        # startup error naming the risk class, not a validation error mid-incident.
        if self.requires_approval and self.approval_kind is None:
            raise ValueError(
                "a risk class with requires_approval=true must name an approval_kind; the graph "
                "routes on it to choose an interrupt"
            )
        return self


class ActionRule(PackSection):
    allowed: bool
    risk: str = Field(min_length=1)
    #: Which `attempt_limits` counter this action spends. Absent for actions that are naturally
    #: bounded (a cancellation, a notification), because inventing a limit for those would produce a
    #: counter nothing ever reads.
    max_attempts_key: str | None = None
    #: Overrides the risk class's approval kind. `create_work_order` is medium risk but its
    #: interrupt is `dispatch`, not `high_risk_remote_action`: the operator is being asked about
    #: sending a crew, and the question they see has to match the decision they are making.
    approval_kind: ApprovalKind | None = None
    requires_maintenance_window: bool = False
    max_blast_radius: int | None = Field(default=None, ge=0)


# -------------------------------------------------------------------------------------------------
# 6. Blast radius
# -------------------------------------------------------------------------------------------------


class BlastRadiusPolicy(PackSection):
    delimiter_default: int = Field(ge=1)
    tap_default: int = Field(ge=1)
    odp_default: int = Field(ge=1)
    distribution_default: int = Field(ge=1)
    node_default: int = Field(ge=1)
    pon_port_default: int = Field(ge=1)
    olt_default: int = Field(ge=1)

    network_action_threshold: int = Field(ge=1)
    common_cause_threshold: int = Field(ge=2)
    common_cause_peer_fraction: Fraction

    @model_validator(mode="after")
    def _defaults_nest_outward(self) -> Self:
        # A tap sits inside a distribution leg which sits inside a node; a PON port sits inside an
        # OLT. If the defaults did not increase outward, an action against a larger element could
        # estimate a smaller blast radius than the same action against a smaller one, and the
        # blast-radius gate would let the more dangerous action through.
        for smaller, larger in (
            ("delimiter_default", "distribution_default"),
            ("distribution_default", "node_default"),
            ("pon_port_default", "olt_default"),
        ):
            if getattr(self, smaller) > getattr(self, larger):
                raise ValueError(
                    f"blast_radius.{smaller} ({getattr(self, smaller)}) exceeds {larger} "
                    f"({getattr(self, larger)}): the estimate must grow as the element does"
                )
        return self


# -------------------------------------------------------------------------------------------------
# 7. Approvals
# -------------------------------------------------------------------------------------------------


class ApprovalRule(PackSection):
    required_role: Role
    expires_after_minutes: Minutes
    escalate_on_expiry: bool


# -------------------------------------------------------------------------------------------------
# 8-11. Attempt limits
# -------------------------------------------------------------------------------------------------


class AttemptLimits(PackSection):
    remote: int = Field(ge=1)
    self_help: int = Field(ge=1)
    work_order: int = Field(ge=1)
    mr: int = Field(ge=1)
    plant: int = Field(ge=1)
    total_steps: int = Field(ge=10)
    max_subgraph_reentries: int = Field(ge=1)
    require_reason_beyond: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reason_thresholds_are_reachable(self) -> Self:
        # `require_reason_beyond.work_order: 5` with `work_order: 2` is a rule that can never fire,
        # which reads in review as a control that exists. It does not.
        for key, threshold in self.require_reason_beyond.items():
            limit = getattr(self, key, None)
            if limit is None:
                raise ValueError(
                    f"attempt_limits.require_reason_beyond names {key!r}, which is not an attempt "
                    f"limit; known limits are {sorted(self.counter_names())}"
                )
            if threshold >= limit:
                raise ValueError(
                    f"require_reason_beyond.{key} ({threshold}) is at or above the hard limit "
                    f"({limit}), so the rule can never fire"
                )
        return self

    @classmethod
    def counter_names(cls) -> frozenset[str]:
        """The per-action counters, excluding the graph-level guards.

        `ActionRule.max_attempts_key` is validated against this, so a pack cannot point an action at
        `total_steps` and accidentally give it a limit of 200 reboots.
        """
        return frozenset({"remote", "self_help", "work_order", "mr", "plant"})

    def limit_for(self, key: str | None) -> int | None:
        if key is None:
            return None
        value = getattr(self, key, None)
        return int(value) if isinstance(value, int) else None


# -------------------------------------------------------------------------------------------------
# 12-13. Validation and closure
# -------------------------------------------------------------------------------------------------


class ValidationPolicy(PackSection):
    stability_window_minutes: Minutes
    stability_window_plant_minutes: Minutes
    min_post_fix_samples: int = Field(ge=1)
    min_anomaly_reduction: Fraction
    require_customer_confirmation_for_domains: tuple[FaultDomain, ...]

    @model_validator(mode="after")
    def _plant_window_is_not_shorter(self) -> Self:
        if self.stability_window_plant_minutes < self.stability_window_minutes:
            raise ValueError(
                f"stability_window_plant_minutes ({self.stability_window_plant_minutes}) is below "
                f"stability_window_minutes ({self.stability_window_minutes}): a repair affecting "
                "many services cannot be proven in less time than one affecting a single service"
            )
        return self

    def window_minutes(self, *, plant: bool) -> int:
        return self.stability_window_plant_minutes if plant else self.stability_window_minutes


class ClosurePolicy(PackSection):
    require_validation_passed: bool
    require_reconciliation: bool
    require_linked_records_consistent: bool
    allow_exceptional_closure: bool
    exceptional_closure_requires_approval: bool
    reopen_creates_linked_incident: bool

    @model_validator(mode="after")
    def _the_exception_keeps_its_approval(self) -> Self:
        # An exceptional closure is the only exit from an incident without proof. Permitting it
        # while dropping its approval would turn "closed exceptionally, signed by a supervisor" into
        # "closed", which is the specification's forbidden case wearing a different reason code.
        if self.allow_exceptional_closure and not self.exceptional_closure_requires_approval:
            raise ValueError(
                "allow_exceptional_closure=true with exceptional_closure_requires_approval=false "
                "would let the workflow close an incident without proof and without a signature"
            )
        return self


# -------------------------------------------------------------------------------------------------
# 14-15. Reconciliation and escalation
# -------------------------------------------------------------------------------------------------


class ReconciliationPolicy(PackSection):
    max_retries: int = Field(ge=0)
    retry_backoff_seconds: tuple[int, ...]
    escalate_on_persistent_mismatch: bool
    systems: tuple[str, ...]

    @model_validator(mode="after")
    def _backoff_covers_the_retries(self) -> Self:
        # A three-retry policy with two backoff entries leaves the third retry's delay to whatever
        # the caller does when the list runs out -- usually zero, which turns a backoff into a tight
        # loop against a system that is already struggling.
        if len(self.retry_backoff_seconds) < self.max_retries:
            raise ValueError(
                f"retry_backoff_seconds has {len(self.retry_backoff_seconds)} entries for "
                f"max_retries={self.max_retries}: the later retries would have no delay"
            )
        if any(b <= 0 for b in self.retry_backoff_seconds):
            raise ValueError("retry_backoff_seconds must all be positive")
        return self

    def backoff_for(self, attempt: int) -> int:
        """Delay before `attempt` (1-based). Clamped to the last entry rather than wrapping."""
        if not self.retry_backoff_seconds:
            return 0
        index = min(max(attempt, 1), len(self.retry_backoff_seconds)) - 1
        return self.retry_backoff_seconds[index]


class EscalationPolicy(PackSection):
    on_loop_limit: bool
    on_adapter_unavailable: bool
    on_approval_expiry: bool
    on_persistent_reconciliation_mismatch: bool
    on_conflicting_evidence: bool
    sla_breach_warning_fraction: Fraction
    target_role: Role

    @field_validator("sla_breach_warning_fraction")
    @classmethod
    def _warning_precedes_the_breach(cls, v: float) -> float:
        if v >= 1.0:
            raise ValueError(
                "sla_breach_warning_fraction must be below 1.0; a warning at or after the deadline "
                "is a breach report, not a warning"
            )
        return v


# -------------------------------------------------------------------------------------------------
# Customer contact
# -------------------------------------------------------------------------------------------------


class CustomerContactPolicy(PackSection):
    quiet_hours_start: time
    quiet_hours_end: time
    quiet_hours_override_severity: Severity
    max_contacts_per_incident_per_day: int = Field(ge=1)
    min_minutes_between_contacts: int = Field(ge=0)
    vulnerable_customer_skip_self_help: bool
    vulnerable_customer_priority_boost: int = Field(ge=0)

    def in_quiet_hours(self, local: time) -> bool:
        """Whether `local` (operating-timezone wall clock) falls in the quiet window.

        The window wraps midnight, which is the case a naive `start <= t <= end` comparison gets
        exactly backwards -- it would return False for 02:00 and True for 13:00. Written once here
        rather than at each call site for that reason.
        """
        if self.quiet_hours_start <= self.quiet_hours_end:
            return self.quiet_hours_start <= local < self.quiet_hours_end
        return local >= self.quiet_hours_start or local < self.quiet_hours_end


# -------------------------------------------------------------------------------------------------
# Health bands
# -------------------------------------------------------------------------------------------------


class HealthBandPolicy(PackSection):
    healthy_at_or_above: float = Field(ge=0, le=100)
    degraded_at_or_above: float = Field(ge=0, le=100)
    at_risk_at_or_above: float = Field(ge=0, le=100)
    event_threshold_band: HealthBand
    dispatch_threshold_band: HealthBand

    @model_validator(mode="after")
    def _bands_descend(self) -> Self:
        if not (self.healthy_at_or_above > self.degraded_at_or_above > self.at_risk_at_or_above):
            raise ValueError(
                "health bands must strictly descend: healthy > degraded > at_risk, got "
                f"{self.healthy_at_or_above} > {self.degraded_at_or_above} > "
                f"{self.at_risk_at_or_above}"
            )
        return self

    def band_for(self, score: float) -> HealthBand:
        """Score (0-100) to band, for a score with no breach list attached.

        The language model never produces a `HealthBand`; the field is stripped from its schema and
        this method's output is merged in afterwards, so a verdict is reproducible from the score
        (IMPLEMENTATION_PLAN.md D6).

        This is not the owner of the *Wi-Fi* band. `detectors.cpe_wifi.wifi_health_verdict` is, and
        it applies one extra rule this method cannot: a breached metric denies `HEALTHY` whatever
        the score. The cheapest breach costs 0.10, so a score of 0.90 with a coverage breach reaches
        this method's healthy floor and is not healthy. The three boundaries below are the ones that
        function reads, passed to it through its threshold mapping, so the numbers have one owner
        even though the rule has two callers -- and `wifi_health_verdict` never returns a *better*
        band than this method would for the same score.
        """
        if score >= self.healthy_at_or_above:
            return HealthBand.HEALTHY
        if score >= self.degraded_at_or_above:
            return HealthBand.DEGRADED
        if score >= self.at_risk_at_or_above:
            return HealthBand.AT_RISK
        return HealthBand.CRITICAL

    def at_or_below(self, band: HealthBand, threshold: HealthBand) -> bool:
        """Whether `band` is as bad as, or worse than, `threshold`.

        Needed because `HealthBand` is a `StrEnum` and `<=` on it compares alphabetically --
        "at_risk" < "healthy" is True by accident, and "critical" < "degraded" is True for the wrong
        reason. Ordering health bands by string is a bug waiting for a rename.
        """
        order = (HealthBand.HEALTHY, HealthBand.DEGRADED, HealthBand.AT_RISK, HealthBand.CRITICAL)
        return order.index(band) >= order.index(threshold)


# -------------------------------------------------------------------------------------------------
# Scan
# -------------------------------------------------------------------------------------------------


class ScanPolicy(PackSection):
    windows_local: tuple[time, ...]
    timezone: str = Field(min_length=1)
    post_install_baseline_delay_hours: int = Field(ge=1)
    max_devices_per_run: int = Field(ge=1)

    @field_validator("windows_local")
    @classmethod
    def _at_least_one_window(cls, v: tuple[time, ...]) -> tuple[time, ...]:
        if not v:
            raise ValueError("scan.windows_local is empty: the predictive sweep would never run")
        return tuple(sorted(set(v)))


# -------------------------------------------------------------------------------------------------
# Dispatch
# -------------------------------------------------------------------------------------------------


class DispatchObjectiveWeights(PackSection):
    sla_risk: float = Field(ge=0)
    blast_radius: float = Field(ge=0)
    travel_minutes: float = Field(ge=0)
    crew_skill_match: float = Field(ge=0)
    appointment_window: float = Field(ge=0)
    vulnerable_customer: float = Field(ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> Self:
        if not any(getattr(self, f) > 0 for f in type(self).model_fields):
            raise ValueError(
                "every dispatch objective weight is zero: the optimizer would rank all schedules "
                "equally and return an arbitrary one"
            )
        return self


class DispatchPolicy(PackSection):
    archetype_speed_kph: dict[AreaArchetype, float]
    archetype_access_overhead_minutes: dict[AreaArchetype, int]
    default_visit_minutes: Minutes
    clean_boots_visit_minutes: Minutes
    dirty_boots_visit_minutes: Minutes
    joint_visit_minutes: Minutes
    objective_weights: DispatchObjectiveWeights
    max_jobs_per_crew_per_shift: int = Field(ge=1)
    shift_minutes: Minutes
    max_overtime_minutes: int = Field(ge=0)
    require_crew_type_match: bool
    respect_appointment_windows: bool
    aerial_work_max_wind_kph: float = Field(gt=0)
    remote_island_latest_start_local: time

    @model_validator(mode="after")
    def _every_archetype_is_priced(self) -> Self:
        # A missing archetype would fall back to a default speed, and the default that makes metro
        # traffic plausible makes the mountains impossible. The optimizer would then confidently
        # schedule a day of work that cannot be driven.
        for label, mapping in (
            ("archetype_speed_kph", self.archetype_speed_kph),
            ("archetype_access_overhead_minutes", self.archetype_access_overhead_minutes),
        ):
            missing = set(AreaArchetype) - set(mapping)
            if missing:
                raise ValueError(
                    f"dispatch.{label} is missing {sorted(a.value for a in missing)}: travel would "
                    "fall back to a default that is wrong for those areas"
                )
        if any(v <= 0 for v in self.archetype_speed_kph.values()):
            raise ValueError("dispatch.archetype_speed_kph must all be positive")
        return self

    @model_validator(mode="after")
    def _a_single_visit_fits_in_a_shift(self) -> Self:
        longest = max(
            self.default_visit_minutes,
            self.clean_boots_visit_minutes,
            self.dirty_boots_visit_minutes,
            self.joint_visit_minutes,
        )
        if longest > self.shift_minutes + self.max_overtime_minutes:
            raise ValueError(
                f"the longest visit ({longest} min) does not fit in a shift plus overtime "
                f"({self.shift_minutes} + {self.max_overtime_minutes}): every schedule containing "
                "one would be reported infeasible"
            )
        return self

    def speed_kph(self, archetype: AreaArchetype) -> float:
        return self.archetype_speed_kph[archetype]

    def access_overhead_minutes(self, archetype: AreaArchetype) -> int:
        return self.archetype_access_overhead_minutes[archetype]


# -------------------------------------------------------------------------------------------------
# SLA
# -------------------------------------------------------------------------------------------------


class SLAPolicy(PackSection):
    response_minutes: dict[Severity, int]
    restore_minutes: dict[Severity, int]
    clock: str = Field(min_length=1)
    vulnerable_customer_tighten_bands: int = Field(ge=0)

    @model_validator(mode="after")
    def _every_severity_has_a_deadline_and_they_order(self) -> Self:
        for label, mapping in (
            ("response_minutes", self.response_minutes),
            ("restore_minutes", self.restore_minutes),
        ):
            missing = set(Severity) - set(mapping)
            if missing:
                raise ValueError(
                    f"sla.{label} is missing {sorted(s.value for s in missing)}: an incident at "
                    "that severity would have no deadline, and 'no deadline' reads as 'never late'"
                )
            if any(v <= 0 for v in mapping.values()):
                raise ValueError(f"sla.{label} values must be positive")
            # A higher severity must not be given a longer clock. Getting this backwards produces a
            # system that is most relaxed about its worst incidents.
            ordered = [mapping[s] for s in sorted(Severity, key=lambda s: s.rank())]
            if ordered != sorted(ordered, reverse=True):
                raise ValueError(
                    f"sla.{label} does not tighten as severity rises: "
                    f"{ {s.value: mapping[s] for s in sorted(Severity, key=lambda s: s.rank())} }"
                )
        for sev in Severity:
            if self.restore_minutes[sev] < self.response_minutes[sev]:
                raise ValueError(
                    f"sla.restore_minutes[{sev.value}] ({self.restore_minutes[sev]}) is below "
                    f"response_minutes ({self.response_minutes[sev]}): service cannot be restored "
                    "before it is responded to"
                )
        return self

    def response_for(self, severity: Severity, *, vulnerable: bool = False) -> int:
        return self.response_minutes[self._effective(severity, vulnerable=vulnerable)]

    def restore_for(self, severity: Severity, *, vulnerable: bool = False) -> int:
        return self.restore_minutes[self._effective(severity, vulnerable=vulnerable)]

    def _effective(self, severity: Severity, *, vulnerable: bool) -> Severity:
        if not vulnerable or not self.vulnerable_customer_tighten_bands:
            return severity
        return Severity.from_rank(severity.rank() + self.vulnerable_customer_tighten_bands)


# -------------------------------------------------------------------------------------------------
# The pack
# -------------------------------------------------------------------------------------------------


class PolicyPack(PackSection):
    """One parsed, validated, self-consistent policy pack.

    `content_hash` and `policy_version` are set by the loader, not read from the file: a version
    string that the file's author maintains by hand is a version string that stops matching the file
    the first time someone edits a threshold in a hurry. See `loader.load_pack`.
    """

    version: str = Field(min_length=1)
    description: str = ""

    evidence: EvidencePolicy
    rca: RCAPolicy
    risk_classes: dict[str, RiskClass]
    remote_actions: dict[ActionType, ActionRule]
    blast_radius: BlastRadiusPolicy
    approvals: dict[ApprovalKind, ApprovalRule]
    attempt_limits: AttemptLimits
    validation: ValidationPolicy
    closure: ClosurePolicy
    reconciliation: ReconciliationPolicy
    escalation: EscalationPolicy
    customer_contact: CustomerContactPolicy
    health_bands: HealthBandPolicy
    scan: ScanPolicy
    detector_thresholds: dict[str, float] = Field(default_factory=dict)
    dispatch: DispatchPolicy
    sla: SLAPolicy

    #: Digest of the parsed content, filled in by the loader.
    content_hash: str = ""

    # -- cross-section coherence -----------------------------------------------------------------

    @model_validator(mode="after")
    def _allowlist_is_exhaustive(self) -> Self:
        # The allowlist must name every `ActionType`. An absent one is *already* blocked by the
        # engine's fail-closed lookup, so this check is not a safety control -- it is a
        # discoverability control: a new action type would otherwise be unusable for a reason nobody
        # can find, and the person debugging it would look at the engine rather than at this file.
        missing = set(ActionType) - set(self.remote_actions)
        if missing:
            raise ValueError(
                f"remote_actions omits {sorted(a.value for a in missing)}. Every ActionType "
                "needs a row, including a deliberate `allowed: false` -- an omission is "
                "indistinguishable from an oversight"
            )
        return self

    @model_validator(mode="after")
    def _action_rules_reference_defined_things(self) -> Self:
        for action, rule in self.remote_actions.items():
            if rule.risk not in self.risk_classes:
                raise ValueError(
                    f"remote_actions.{action.value}.risk={rule.risk!r} is not a defined risk "
                    f"class; known: {sorted(self.risk_classes)}"
                )
            if (
                rule.max_attempts_key is not None
                and rule.max_attempts_key not in AttemptLimits.counter_names()
            ):
                raise ValueError(
                    f"remote_actions.{action.value}.max_attempts_key="
                    f"{rule.max_attempts_key!r} is not a per-action attempt counter; known: "
                    f"{sorted(AttemptLimits.counter_names())}"
                )
            # If an action can require approval, the kind it would raise must be configured -- both
            # in this pack (for the expiry and the role) and in rbac (for who may answer).
            kind = rule.approval_kind or self.risk_classes[rule.risk].approval_kind
            if self.risk_classes[rule.risk].requires_approval or rule.approval_kind is not None:
                if kind is None:
                    raise ValueError(
                        f"remote_actions.{action.value} can require approval but no approval_kind "
                        "resolves from either the action or its risk class"
                    )
                if kind not in self.approvals:
                    raise ValueError(
                        f"remote_actions.{action.value} would raise a {kind.value} interrupt, but "
                        "approvals has no rule for it: the interrupt would have no expiry and no "
                        "required role"
                    )
        return self

    @model_validator(mode="after")
    def _all_six_interrupts_are_configured(self) -> Self:
        missing = set(ApprovalKind) - set(self.approvals)
        if missing:
            raise ValueError(
                f"approvals omits {sorted(k.value for k in missing)}; the graph raises all six "
                "interrupt kinds and each needs an expiry and a required role"
            )
        return self

    @model_validator(mode="after")
    def _required_roles_can_actually_approve(self) -> Self:
        # The check that catches the defect this module's docstring describes. `rbac` owns who may
        # approve what; this pack names a floor role per kind. If the two disagree, the interrupt
        # names a role whose holder `can_approve()` will refuse -- an approval gate that cannot be
        # passed, discovered at 02:00 by whoever is on call.
        for kind, rule in self.approvals.items():
            permitted = approvers_for(kind)
            if not permitted:
                raise ValueError(
                    f"rbac defines no approvers for {kind.value}; the pack cannot name a floor "
                    "role for an interrupt nobody may answer"
                )
            if rule.required_role not in permitted:
                raise ValueError(
                    f"approvals.{kind.value}.required_role={rule.required_role.value!r} is not in "
                    f"rbac.approvers_for({kind.value}) = "
                    f"{sorted(r.value for r in permitted)}: the interrupt would name a role that "
                    "can_approve() refuses"
                )
        return self

    @model_validator(mode="after")
    def _escalation_target_is_a_real_supervisor(self) -> Self:
        # Escalation exists to reach a human with more authority than the workflow. A target role
        # that may not approve anything is a dead end that looks like a route.
        if not any(self.escalation.target_role in approvers_for(k) for k in ApprovalKind):
            raise ValueError(
                f"escalation.target_role={self.escalation.target_role.value!r} may not approve any "
                "of the six interrupt kinds, so escalating to it cannot unblock an incident"
            )
        return self

    @model_validator(mode="after")
    def _network_threshold_sits_below_the_network_class_cap(self) -> Self:
        # `network_action_threshold` is the point past which any action becomes a network event. If
        # it exceeded the network risk class's own cap, there would be actions large enough to
        # breach the cap while never being reclassified -- blocked with the wrong reason code, or
        # worse, allowed.
        network = self.risk_classes.get("network")
        if network is not None and self.blast_radius.network_action_threshold > (
            network.max_blast_radius
        ):
            raise ValueError(
                f"blast_radius.network_action_threshold "
                f"({self.blast_radius.network_action_threshold}) exceeds risk_classes.network."
                f"max_blast_radius ({network.max_blast_radius})"
            )
        return self

    # -- accessors ------------------------------------------------------------------------------

    def rule_for(self, action_type: ActionType) -> ActionRule | None:
        """The rule for an action, or `None`; `None` must be read as a block, not a default."""
        return self.remote_actions.get(action_type)

    def risk_class_for(self, action_type: ActionType) -> RiskClass | None:
        rule = self.rule_for(action_type)
        if rule is None:
            return None
        return self.risk_classes.get(rule.risk)

    def approval_kind_for(self, action_type: ActionType) -> ApprovalKind | None:
        """Which interrupt this action would raise, action row overriding risk class."""
        rule = self.rule_for(action_type)
        if rule is None:
            return None
        if rule.approval_kind is not None:
            return rule.approval_kind
        klass = self.risk_classes.get(rule.risk)
        return klass.approval_kind if klass else None

    def blast_radius_cap_for(self, action_type: ActionType) -> int | None:
        """The tighter of the action's own cap and its risk class's."""
        rule = self.rule_for(action_type)
        klass = self.risk_class_for(action_type)
        caps = [
            c
            for c in (
                rule.max_blast_radius if rule else None,
                klass.max_blast_radius if klass else None,
            )
            if c is not None
        ]
        return min(caps) if caps else None

    def attempt_limit_for(self, action_type: ActionType) -> int | None:
        rule = self.rule_for(action_type)
        return self.attempt_limits.limit_for(rule.max_attempts_key if rule else None)

    def threshold(self, name: str, default: float) -> float:
        """A detector threshold override, or the detector's own stated default.

        Mirrors `DetectionContext.threshold` so the detectors' call sites read the same whether they
        are handed a context or the pack.
        """
        return float(self.detector_thresholds.get(name, default))

    def summary(self) -> dict[str, Any]:
        """A compact, serialisable view for `/health`, the docs generator and audit events.

        Deliberately not `model_dump()`: this is the answer to "which pack is running", and a caller
        wanting the whole document should say so.
        """
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "actions_allowed": sum(1 for r in self.remote_actions.values() if r.allowed),
            "actions_total": len(self.remote_actions),
            "actions_refused": sorted(
                a.value for a, r in self.remote_actions.items() if not r.allowed
            ),
            "approval_kinds": sorted(k.value for k in self.approvals),
            "risk_classes": sorted(self.risk_classes),
            "detector_overrides": len(self.detector_thresholds),
        }
