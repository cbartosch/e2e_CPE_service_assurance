"""Dispatch, work orders, field findings, the Clean-to-Dirty handover, and jTrack MRs.

`HandoverContract` is the centre of gravity. The Clean/Dirty Boots boundary is where this workflow
either saves a truck roll or wastes two: a handover that arrives without the measurements OSP needs
gets rejected, and the customer waits for a second Clean Boots visit that exists only to gather what
the first visit should have recorded. So the contract carries a `completeness` computation and the
handover approval interrupt is fed by it, rather than by a technician's confidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from lpr_cpe.domain.base import DomainModel, FrozenDomainModel
from lpr_cpe.domain.enums import (
    AreaArchetype,
    CrewType,
    DelimiterKind,
    FaultDomain,
    MRStatus,
    ReasonCode,
    Severity,
    WorkOrderStatus,
)


class DispatchRequirement(DomainModel):
    """What the visit needs, decided before anyone is chosen to do it.

    Separating the requirement from the plan is what lets the optimizer be a pure function: it is
    handed requirements and crews and returns an assignment, and it never has to decide *whether* a
    visit is needed.
    """

    requirement_id: str
    incident_id: str
    created_at: datetime
    crew_type: CrewType
    fault_domain: FaultDomain
    delimiter_kind: DelimiterKind = DelimiterKind.UNKNOWN
    delimiter_ref: str | None = None
    area_archetype: AreaArchetype | None = None

    skills_required: list[str] = Field(default_factory=list)
    parts_required: list[str] = Field(default_factory=list)
    equipment_required: list[str] = Field(default_factory=list)
    estimated_duration: timedelta = timedelta(hours=1)
    customer_access_required: bool = False
    customer_availability_windows: list[tuple[datetime, datetime]] = Field(default_factory=list)
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    priority_score: float = Field(default=0.0, ge=0.0)
    weather_sensitive: bool = False
    permit_required: bool = False
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _access_needs_a_window(self) -> Self:
        # A visit that needs the customer present but has no window is a visit that will fail
        # access, be recorded as a truck roll, and count against first-time-fix.
        if self.customer_access_required and not self.customer_availability_windows:
            raise ValueError(
                "customer_access_required=True with no customer_availability_windows: this "
                "dispatch would be scheduled blind and fail access"
            )
        return self

    @model_validator(mode="after")
    def _window_ordering(self) -> Self:
        if (
            self.earliest_start is not None
            and self.latest_finish is not None
            and self.latest_finish <= self.earliest_start
        ):
            raise ValueError("latest_finish must be after earliest_start")
        return self


class CrewSlot(DomainModel):
    """An available crew and when it is free. Input to the optimizer, not a persisted record."""

    crew_id: str
    crew_type: CrewType
    skills: list[str] = Field(default_factory=list)
    available_from: datetime
    available_until: datetime
    base_latitude: float | None = Field(default=None, ge=-90, le=90)
    base_longitude: float | None = Field(default=None, ge=-180, le=180)
    area_archetypes: list[AreaArchetype] = Field(default_factory=list)
    max_jobs: int = Field(default=6, ge=1)
    # Parts are consumed and equipment is not, which is why they are two lists rather than one:
    # a splice trailer is still on the van after the third job, a drop cable is not. The dispatch
    # optimizer checks them with the same subset test but the resupply question differs.
    carried_parts: list[str] = Field(default_factory=list)
    carried_equipment: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shift_is_positive(self) -> Self:
        if self.available_until <= self.available_from:
            raise ValueError("available_until must be after available_from")
        return self


class DispatchAssignment(FrozenDomainModel):
    """One requirement assigned to one crew at one time."""

    requirement_id: str
    crew_id: str
    crew_type: CrewType
    scheduled_start: datetime
    scheduled_end: datetime
    travel_minutes: float = Field(default=0.0, ge=0.0)
    sequence_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _positive_duration(self) -> Self:
        if self.scheduled_end <= self.scheduled_start:
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class DispatchPlan(DomainModel):
    """The optimizer's output, with the reason anything was left out.

    `unassigned` and `constraint_explanation` are required together. A plan that silently drops a
    requirement is worse than no plan: the incident sits in `dispatch_planning` with a plan attached
    and nobody scheduled, and the state looks healthy.
    """

    plan_id: str
    created_at: datetime
    objective: str = "weighted_sla_and_travel"
    solver: str = "greedy"
    solver_status: str = "ok"
    assignments: list[DispatchAssignment] = Field(default_factory=list)
    unassigned: list[str] = Field(default_factory=list)
    constraint_explanation: dict[str, str] = Field(default_factory=dict)
    objective_value: float | None = None
    total_travel_minutes: float = Field(default=0.0, ge=0.0)
    solve_duration: timedelta | None = None
    approved: bool = False
    approval_ref: str | None = None

    @model_validator(mode="after")
    def _unassigned_is_explained(self) -> Self:
        missing = [r for r in self.unassigned if r not in self.constraint_explanation]
        if missing:
            raise ValueError(
                f"unassigned requirements with no constraint_explanation: {missing}. A dropped "
                "requirement must say which constraint dropped it, in machine-readable form"
            )
        return self

    @property
    def assigned_requirement_ids(self) -> set[str]:
        return {a.requirement_id for a in self.assignments}


class WorkOrder(DomainModel):
    """A dispatch as the WFM sees it. Append-only in state -- updates append a new revision.

    `visit_number` is on the record rather than derived from the count of work orders because a
    cancelled-before-travel order is not a visit, and counting rows would inflate the repeat-visit
    KPI with orders nobody ever drove to.
    """

    work_order_id: str
    incident_id: str
    external_ref: str | None = None
    crew_type: CrewType
    status: WorkOrderStatus = WorkOrderStatus.DRAFT
    created_at: datetime
    updated_at: datetime
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    dispatched_at: datetime | None = None
    on_site_at: datetime | None = None
    completed_at: datetime | None = None
    assigned_crew_id: str | None = None
    visit_number: int = Field(default=1, ge=1)
    requirement_id: str | None = None
    idempotency_key: str = ""
    instructions: str = ""
    parts_used: list[str] = Field(default_factory=list)
    completion_code: str = ""
    reason_code: ReasonCode | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def counted_as_truck_roll(self) -> bool:
        """A visit only counts once a crew actually travelled or arrived."""
        return self.status in (
            WorkOrderStatus.EN_ROUTE,
            WorkOrderStatus.ON_SITE,
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.INCOMPLETE,
            WorkOrderStatus.FAILED_ACCESS,
        )

    @property
    def terminal(self) -> bool:
        return self.status in (
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.CANCELLED,
            WorkOrderStatus.FAILED_ACCESS,
            WorkOrderStatus.INCOMPLETE,
        )

    def on_site_duration(self) -> timedelta | None:
        if self.on_site_at is None or self.completed_at is None:
            return None
        return max(self.completed_at - self.on_site_at, timedelta(0))


class FieldFinding(FrozenDomainModel):
    """What the technician found, in structured form plus their note.

    `technician_note` is *data*, never an instruction. It is redacted before any model call and, per
    the specification's prompt-injection rule, no code path treats its contents as directives --
    `security.injection` is where that is enforced.
    """

    finding_id: str
    work_order_id: str
    incident_id: str
    recorded_at: datetime
    recorded_by: str
    fault_domain: FaultDomain
    delimiter_kind: DelimiterKind = DelimiterKind.UNKNOWN
    delimiter_ref: str | None = None
    fault_confirmed: bool = False
    no_fault_found: bool = False
    measurements: dict[str, float] = Field(default_factory=dict)
    photos: tuple[dict[str, Any], ...] = ()
    technician_note: str = ""
    parts_replaced: tuple[str, ...] = ()
    work_completed: bool = False
    requires_plant_work: bool = False
    requires_permit: bool = False
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _not_both(self) -> Self:
        if self.fault_confirmed and self.no_fault_found:
            raise ValueError("a finding cannot both confirm a fault and be no-fault-found")
        return self

    @model_validator(mode="after")
    def _plant_work_names_a_domain(self) -> Self:
        # A "send it to OSP" finding that does not say which plant object is the problem is the
        # single commonest cause of an MR rejection.
        plant_domains = {
            FaultDomain.TAP_OR_ODP,
            FaultDomain.DISTRIBUTION,
            FaultDomain.FEEDER,
            FaultDomain.NODE_OR_OLT,
            FaultDomain.HEADEND_OR_CO,
            FaultDomain.POWER,
        }
        if self.requires_plant_work and self.fault_domain not in plant_domains:
            raise ValueError(
                f"requires_plant_work=True but fault_domain={self.fault_domain} is not a plant "
                f"domain; OSP cannot action this"
            )
        return self


class HandoverContract(DomainModel):
    """The Clean-to-Dirty Boots handover packet, with an explicit completeness test.

    `REQUIRED_MEASUREMENTS` is by technology, because the evidence OSP needs to accept an HFC plant
    fault is not the evidence they need for a PON one. `missing_items()` is the *only* place that
    judgement is made; the handover approval interrupt and the MR builder both call it, so a packet
    that would be rejected downstream is rejected before a human is asked to approve it.
    """

    contract_id: str
    incident_id: str
    created_at: datetime
    from_crew_type: CrewType = CrewType.CLEAN
    to_crew_type: CrewType = CrewType.DIRTY
    technology: str = "unknown"
    fault_domain: FaultDomain = FaultDomain.UNKNOWN
    delimiter_kind: DelimiterKind = DelimiterKind.UNKNOWN
    delimiter_ref: str | None = None

    measurements: dict[str, float] = Field(default_factory=dict)
    ruled_out: list[str] = Field(default_factory=list)
    photos: list[dict[str, Any]] = Field(default_factory=list)
    access_notes: str = ""
    safety_notes: str = ""
    field_finding_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    accepted: bool | None = None
    accepted_at: datetime | None = None
    accepted_by: str = ""
    rejection_reason: ReasonCode | None = None
    rejection_detail: str = ""

    # The measurements each technology's OSP team needs before it will accept a plant fault. Named
    # once, here; `missing_items()`, the approval gate and the MR builder all read this.
    # ClassVar, not a field: a per-instance copy would let a packet lower its own bar and then
    # report itself complete.
    REQUIRED_BY_TECHNOLOGY: ClassVar[dict[str, tuple[str, ...]]] = {
        "hfc": ("downstream_power_dbmv", "upstream_power_dbmv", "downstream_snr_db"),
        "pon": ("rx_optical_power_dbm", "tx_optical_power_dbm"),
        "unknown": (),
    }

    def missing_items(self) -> list[str]:
        """Everything an OSP reviewer would ask for and not find.

        Ordered and deduplicated so the message an operator sees is stable between runs -- a
        rejection reason that reorders itself looks like a different rejection.
        """
        missing: list[str] = []
        for key in self.REQUIRED_BY_TECHNOLOGY.get(self.technology, ()):
            if key not in self.measurements:
                missing.append(f"measurement:{key}")
        if self.delimiter_ref is None:
            missing.append("delimiter_ref")
        if self.fault_domain in (FaultDomain.UNKNOWN, FaultDomain.NO_FAULT_FOUND):
            missing.append("fault_domain")
        if not self.ruled_out:
            missing.append("ruled_out")
        if not self.field_finding_ids:
            missing.append("field_finding")
        return missing

    @property
    def complete(self) -> bool:
        return not self.missing_items()

    @property
    def completeness(self) -> float:
        """0.0-1.0, for the KPI.

        Derived from the same list as `complete`, so the two cannot disagree.
        """
        required = len(self.REQUIRED_BY_TECHNOLOGY.get(self.technology, ())) + 4
        return max(0.0, 1.0 - len(self.missing_items()) / required)


class MRRequest(FrozenDomainModel):
    """A maintenance request about to be filed in jTrack.

    Built from a `HandoverContract` only when that contract is complete. `idempotency_key` is
    mandatory because filing the same MR twice creates two OSP work items for one fault, and the
    second one is closed as a duplicate weeks later by someone who has to work out why it exists.
    """

    request_id: str
    incident_id: str
    created_at: datetime
    idempotency_key: str = Field(min_length=8)
    fault_domain: FaultDomain
    delimiter_ref: str | None = None
    plant_object_ref: str
    severity: Severity
    description: str = Field(min_length=1)
    measurements: dict[str, float] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    handover_contract_id: str | None = None
    affected_customer_count: int = Field(default=1, ge=0)
    requested_priority: str = "standard"
    permit_required: bool = False
    actor: str = ""
    correlation_id: str = ""


class MRRecord(DomainModel):
    """An MR as jTrack reports it. Append-only in state; each update appends a revision.

    `submitted_at` and `accepted_at` are separate fields, not one `status` transition timestamp,
    because the gap between them is the metric that matters: an MR submitted and never accepted is
    the silent stall reconciliation exists to find.
    """

    mr_id: str
    incident_id: str
    external_ref: str | None = None
    status: MRStatus = MRStatus.DRAFT
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    accepted_at: datetime | None = None
    planned_start: datetime | None = None
    completed_at: datetime | None = None
    closed_at: datetime | None = None
    fault_domain: FaultDomain = FaultDomain.UNKNOWN
    plant_object_ref: str = ""
    severity: Severity = Severity.MEDIUM
    idempotency_key: str = ""
    rejection_reason: str = ""
    osp_owner: str = ""
    revision: int = Field(default=1, ge=1)
    notes: list[str] = Field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in (MRStatus.CLOSED, MRStatus.CANCELLED, MRStatus.REJECTED)

    @property
    def awaiting_osp(self) -> bool:
        return self.status in (
            MRStatus.SUBMITTED,
            MRStatus.ACCEPTED,
            MRStatus.PLANNED,
            MRStatus.IN_PROGRESS,
        )

    def acceptance_latency(self) -> timedelta | None:
        if self.submitted_at is None or self.accepted_at is None:
            return None
        return max(self.accepted_at - self.submitted_at, timedelta(0))

    def cycle_time(self) -> timedelta | None:
        if self.submitted_at is None or self.closed_at is None:
            return None
        return max(self.closed_at - self.submitted_at, timedelta(0))
