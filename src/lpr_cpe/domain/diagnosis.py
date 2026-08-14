"""Detection, prediction, impact, testing and root-cause analysis.

The shape that matters most here is `RCAResult.ruled_out`. A diagnosis that only says what it
believes is impossible to review: the reviewer cannot tell whether the alternative they have in mind
was considered and rejected, or never considered. So a hypothesis set carries the rejected
hypotheses and the evidence that rejected them, and the low-confidence-RCA interrupt hands the
reviewer both halves.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from lpr_cpe.domain.base import DomainModel, FrozenDomainModel
from lpr_cpe.domain.enums import (
    ActionType,
    DataQualityFlag,
    DelimiterKind,
    FaultDomain,
    HealthBand,
    ReasonCode,
    Severity,
    TestKind,
    TestStatus,
)


class AnomalyFinding(FrozenDomainModel):
    """One detector's output. The mandatory field list is the specification's, verbatim.

    Frozen, because a finding is a measurement: if a later stage disagrees it produces a *new*
    finding rather than editing this one, and the audit trail keeps both.
    """

    detector_name: str
    detector_version: str
    observed_at: datetime
    score: float
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Severity
    affected_objects: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    explanation: str = Field(min_length=1)
    contributing_features: dict[str, float] = Field(default_factory=dict)
    recommended_tests: tuple[TestKind, ...] = ()
    data_quality_warnings: tuple[DataQualityFlag, ...] = ()

    # Detectors that localise rather than merely score fill these in.
    suspected_domain: FaultDomain | None = None
    suspected_delimiter_ref: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return v

    @property
    def actionable(self) -> bool:
        """Whether this finding may be acted on without a human first.

        Two independent reasons to say no: low confidence, or a data-quality warning that means the
        score was computed over something we could not fully see.
        """
        return self.confidence >= 0.6 and not self.data_quality_warnings


class PredictionResult(FrozenDomainModel):
    """A forward-looking assessment, produced deterministically.

    `wifi_health_score` and `band` are computed by `decision_services.scoring` and are the fields
    the model is *not* allowed to author (IMPLEMENTATION_PLAN.md D6). `narrative` is the model's
    only contribution and is optional -- a prediction with no narrative is still a usable
    prediction, whereas a prediction whose score came from a model would not be.
    """

    model_name: str
    model_version: str
    predicted_at: datetime
    horizon: timedelta
    subject_ref: str

    failure_probability: float = Field(ge=0.0, le=1.0)
    wifi_health_score: float | None = Field(default=None, ge=0.0, le=100.0)
    band: HealthBand | None = None
    severity: Severity = Severity.LOW
    confidence: float = Field(ge=0.0, le=1.0)

    top_features: dict[str, float] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    data_quality_warnings: tuple[DataQualityFlag, ...] = ()
    recommended_actions: tuple[ActionType, ...] = ()

    narrative: str = ""
    narrative_source: str = "none"  # none | template | model

    @field_validator("predicted_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("predicted_at must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _band_requires_score(self) -> Self:
        # A band with no score is a verdict with nothing behind it -- exactly the shape D6 forbids.
        if self.band is not None and self.wifi_health_score is None:
            raise ValueError("band was set without wifi_health_score; the band must be derived")
        return self


class ImpactAssessment(DomainModel):
    """How many customers are affected, and how badly. The dispatch priority input.

    `affected_customer_count` is the *measured* number where topology gave us one, and
    `count_is_estimated` says which. The distinction is load-bearing: the high-blast-radius approval
    threshold is a policy comparison against this number, and comparing a threshold against a guess
    without knowing it is a guess is how a network-wide action gets auto-approved.
    """

    assessed_at: datetime
    affected_customer_count: int = Field(ge=0)
    count_is_estimated: bool = True
    estimation_basis: str = ""

    affected_service_refs: list[str] = Field(default_factory=list)
    affected_delimiter_refs: list[str] = Field(default_factory=list)
    blast_radius_scope: str = "single_premises"
    severity: Severity = Severity.MEDIUM

    vulnerable_customers_affected: int = Field(default=0, ge=0)
    priority_customers_affected: int = Field(default=0, ge=0)
    business_customers_affected: int = Field(default=0, ge=0)
    mdu_affected: bool = False

    sla_at_risk_count: int = Field(default=0, ge=0)
    revenue_at_risk: float | None = Field(default=None, ge=0)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _estimate_states_its_basis(self) -> Self:
        if self.count_is_estimated and not self.estimation_basis:
            raise ValueError(
                "count_is_estimated=True requires estimation_basis: an unexplained estimate is "
                "indistinguishable from a measurement once it is in a report"
            )
        return self

    @model_validator(mode="after")
    def _subsets_fit(self) -> Self:
        for name, value in (
            ("vulnerable_customers_affected", self.vulnerable_customers_affected),
            ("priority_customers_affected", self.priority_customers_affected),
            ("business_customers_affected", self.business_customers_affected),
        ):
            if value > self.affected_customer_count:
                raise ValueError(
                    f"{name} ({value}) exceeds affected_customer_count "
                    f"({self.affected_customer_count})"
                )
        return self


class PreventiveMaintenanceCase(DomainModel):
    """A predictive finding that warrants work before a customer notices.

    Distinct from an incident: there is no outage, no SLA clock and no customer contact yet. It
    becomes an incident only when a verdict crosses the dispatch band (D2).
    """

    case_id: str
    created_at: datetime
    subject_ref: str
    technology: str = "unknown"
    trigger: str = ""
    prediction: PredictionResult | None = None
    findings: list[AnomalyFinding] = Field(default_factory=list)
    impact: ImpactAssessment | None = None
    recommended_window: str = ""
    priority_score: float = Field(default=0.0, ge=0.0)
    status: str = "open"
    linked_incident_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class ServiceProblemRecord(DomainModel):
    """TMF-shaped service-problem view of the incident, for systems that expect that record.

    Field names are ours and TMF-*aligned*, not a claim of TMF621/656 conformance -- there is no
    verified schema to conform to (IMPLEMENTATION_PLAN.md A2), and `docs/vendor-integration-gaps.md`
    records that.
    """

    problem_id: str
    incident_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    severity: Severity
    category: str = ""
    originating_system: str = ""
    affected_service_refs: list[str] = Field(default_factory=list)
    root_cause_summary: str = ""
    resolution_summary: str = ""
    tmf_resource_type: str = "ServiceProblem"
    external_refs: dict[str, str] = Field(default_factory=dict)


class TestRequest(FrozenDomainModel):
    """One test to run, with the reason it is worth running.

    `expected_discrimination` is what stops the test plan becoming a shotgun: a test that cannot
    change the ranking of the current hypotheses is not worth a customer's line being taken out of
    service, so the planner has to say what this one would tell us.
    """

    request_id: str
    kind: TestKind
    target_ref: str
    requested_at: datetime
    reason: str = Field(min_length=1)
    expected_discrimination: str = ""
    disruptive: bool = False
    requires_customer_present: bool = False
    timeout: timedelta = timedelta(minutes=5)
    parameters: dict[str, Any] = Field(default_factory=dict)


class TestPlan(DomainModel):
    """An ordered set of tests. Cheap and non-disruptive first.

    The ordering is a property of the plan, not of the caller's loop: `ordered()` sorts
    non-disruptive before disruptive and no-customer-needed before customer-needed, so a node that
    iterates the plan cannot accidentally run the intrusive test first.
    """

    plan_id: str
    created_at: datetime
    hypothesis_refs: list[str] = Field(default_factory=list)
    requests: list[TestRequest] = Field(default_factory=list)
    stop_when: str = "first_conclusive"
    notes: list[str] = Field(default_factory=list)

    def ordered(self) -> list[TestRequest]:
        return sorted(
            self.requests,
            key=lambda r: (r.disruptive, r.requires_customer_present, r.kind.value),
        )

    @property
    def has_disruptive(self) -> bool:
        return any(r.disruptive for r in self.requests)


class TestResult(FrozenDomainModel):
    """The outcome of one `TestRequest`.

    `UNAVAILABLE` is deliberately distinct from `INCONCLUSIVE`: the first means we could not run the
    test, the second means we ran it and learned nothing. They lead to different routing -- an
    unavailable adapter is a data-quality problem, an inconclusive result is a diagnosis problem.
    """

    result_id: str
    request_id: str
    kind: TestKind
    target_ref: str
    status: TestStatus
    started_at: datetime
    completed_at: datetime | None = None
    measurements: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    breached_thresholds: tuple[str, ...] = ()
    summary: str = ""
    evidence_refs: tuple[str, ...] = ()
    failure_reason: ReasonCode | None = None

    @property
    def duration(self) -> timedelta | None:
        if self.completed_at is None:
            return None
        return max(self.completed_at - self.started_at, timedelta(0))

    @property
    def conclusive(self) -> bool:
        return self.status in (TestStatus.PASSED, TestStatus.FAILED)


class RCAHypothesis(FrozenDomainModel):
    """One candidate explanation, with what supports and what contradicts it.

    Both lists, always. A hypothesis carrying only supporting evidence is a hypothesis nobody tried
    to falsify, and its `posterior` would be a number with no test behind it.
    """

    hypothesis_id: str
    fault_domain: FaultDomain
    statement: str = Field(min_length=1)
    prior: float = Field(ge=0.0, le=1.0)
    posterior: float = Field(ge=0.0, le=1.0)
    supporting_evidence_refs: tuple[str, ...] = ()
    contradicting_evidence_refs: tuple[str, ...] = ()
    discriminating_tests: tuple[TestKind, ...] = ()
    suspected_delimiter_ref: str | None = None
    rejected: bool = False
    rejection_reason: str = ""

    @model_validator(mode="after")
    def _rejection_is_explained(self) -> Self:
        if self.rejected and not self.rejection_reason:
            raise ValueError(
                "a rejected hypothesis must carry rejection_reason: the reviewer at the "
                "low-confidence-RCA interrupt is being asked whether the rejection was right"
            )
        return self


class RCAResult(DomainModel):
    """The conclusion, its confidence, and everything ruled out on the way.

    `confidence` is the *fault-domain* confidence the specification asks for, not the confidence of
    the top hypothesis in isolation: a case with two hypotheses at 0.45 in different domains is a
    low-confidence case even though each hypothesis is individually unremarkable. `derive()`
    computes it from the hypothesis set so it cannot be asserted independently of the evidence.
    """

    concluded_at: datetime
    fault_domain: FaultDomain = FaultDomain.UNKNOWN
    delimiter_kind: DelimiterKind = DelimiterKind.UNKNOWN
    delimiter_ref: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    primary_hypothesis_id: str | None = None
    hypotheses: list[RCAHypothesis] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""
    reason_code: ReasonCode = ReasonCode.RCA_LOW_CONFIDENCE
    cycles_used: int = Field(default=1, ge=1)

    @property
    def ruled_out(self) -> list[RCAHypothesis]:
        return [h for h in self.hypotheses if h.rejected]

    @property
    def live(self) -> list[RCAHypothesis]:
        return sorted(
            (h for h in self.hypotheses if not h.rejected),
            key=lambda h: h.posterior,
            reverse=True,
        )

    def needs_human_review(self, threshold: float) -> bool:
        return self.confidence < threshold or self.fault_domain is FaultDomain.UNKNOWN
