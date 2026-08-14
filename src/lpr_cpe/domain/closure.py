"""Restoration validation, reconciliation, and closure.

"Proof before closure" is implemented here as a type, not a convention. `ClosureRecord` refuses to
be constructed as a normal closure without a `ValidationResult` that passed, so the only way to
close an unvalidated incident is the `CLOSED_EXCEPTIONAL` path -- which requires an approval ref,
which means a named human. There is no third way, and that is the point: a closure code that can
be set freely is a closure code that will be set freely at the end of a long shift.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import Field, model_validator

from lpr_cpe.domain.base import DomainModel, FrozenDomainModel
from lpr_cpe.domain.enums import FaultDomain, ReasonCode


class ValidationResult(FrozenDomainModel):
    """Did the fix hold? Measured after the fact, over a stability window.

    `stability_window` and `samples_in_window` are both required for a pass. A single good reading
    taken thirty seconds after a reboot proves the device came back, not that the fault is gone --
    the post-fix stability detector needs a window, and `passed` cannot be true without one.
    """

    validation_id: str
    incident_id: str
    validated_at: datetime
    window_start: datetime
    stability_window: timedelta
    samples_in_window: int = Field(default=0, ge=0)
    min_samples_required: int = Field(default=2, ge=1)

    passed: bool = False
    kpi_before: dict[str, float] = Field(default_factory=dict)
    kpi_after: dict[str, float] = Field(default_factory=dict)
    improved_metrics: tuple[str, ...] = ()
    regressed_metrics: tuple[str, ...] = ()
    customer_confirmed: bool | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_code: ReasonCode = ReasonCode.STABILITY_WINDOW_PENDING
    summary: str = ""

    @model_validator(mode="after")
    def _pass_requires_a_window(self) -> Self:
        if self.passed:
            if self.stability_window <= timedelta(0):
                raise ValueError(
                    "passed=True with a zero-length stability_window: a fix that was never "
                    "observed over time has not been shown to hold"
                )
            if self.samples_in_window < self.min_samples_required:
                raise ValueError(
                    f"passed=True with {self.samples_in_window} samples but "
                    f"{self.min_samples_required} required"
                )
            if self.regressed_metrics:
                raise ValueError(
                    f"passed=True while these metrics regressed: {list(self.regressed_metrics)}"
                )
        return self

    @property
    def window_complete(self) -> bool:
        return self.validated_at >= self.window_start + self.stability_window


class ReconciliationResult(DomainModel):
    """Do the downstream systems agree with what we believe?

    `mismatches` holds one entry per disagreement, keyed by system, and `consistent` is derived from
    it. Reconciliation that reports a boolean without the differences is reconciliation nobody can
    act on: the operator is told something is wrong and not what.
    """

    reconciliation_id: str
    incident_id: str
    reconciled_at: datetime
    systems_checked: list[str] = Field(default_factory=list)
    systems_unreachable: list[str] = Field(default_factory=list)
    mismatches: list[dict[str, Any]] = Field(default_factory=list)
    inventory_updates_applied: list[str] = Field(default_factory=list)
    records_linked: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """Unreachable counts as inconsistent.

        We did not check it, so we do not know it agrees, and reporting `consistent=True` over an
        unreachable system is a green light for something never measured.
        """
        return not self.mismatches and not self.systems_unreachable

    @property
    def reason_code(self) -> ReasonCode | None:
        return None if self.consistent else ReasonCode.RECONCILIATION_MISMATCH


class ClosureRecord(FrozenDomainModel):
    """The final record. Immutable, and it cannot be built without proof.

    The two validators below are the whole "proof before closure" rule:

    * a `CLOSED_NORMAL` closure requires a `ValidationResult` that passed;
    * a `CLOSED_EXCEPTIONAL` closure requires an `approval_ref` and a reason.

    A `ClosureRecord` therefore cannot exist for an incident that was neither validated nor
    explicitly signed off, which is a stronger statement than any check in a node could make,
    because it holds for every path that reaches closure including ones added later.
    """

    closure_id: str
    incident_id: str
    closed_at: datetime
    closure_code: ReasonCode
    fault_domain: FaultDomain = FaultDomain.UNKNOWN
    root_cause_summary: str = ""
    resolution_summary: str = Field(min_length=1)

    validation: ValidationResult | None = None
    reconciliation_id: str | None = None
    approval_ref: str | None = None
    exceptional_reason: str = ""

    truck_rolls: int = Field(default=0, ge=0)
    remote_attempts: int = Field(default=0, ge=0)
    field_visits: int = Field(default=0, ge=0)
    mr_count: int = Field(default=0, ge=0)
    customer_contacts: int = Field(default=0, ge=0)
    time_to_restore: timedelta | None = None
    sla_met: bool | None = None
    linked_records: dict[str, str] = Field(default_factory=dict)
    closed_by: str = "system"

    @model_validator(mode="after")
    def _normal_closure_needs_a_passing_validation(self) -> Self:
        if self.closure_code is ReasonCode.CLOSED_NORMAL:
            if self.validation is None:
                raise ValueError(
                    "CLOSED_NORMAL with no ValidationResult: closure requires proof, so an "
                    "unvalidated incident must close as CLOSED_EXCEPTIONAL with an approval"
                )
            if not self.validation.passed:
                raise ValueError(
                    "CLOSED_NORMAL with a ValidationResult that did not pass "
                    f"(reason={self.validation.reason_code})"
                )
        return self

    @model_validator(mode="after")
    def _exceptional_closure_is_signed(self) -> Self:
        if self.closure_code is ReasonCode.CLOSED_EXCEPTIONAL:
            if not self.approval_ref:
                raise ValueError(
                    "CLOSED_EXCEPTIONAL requires approval_ref: an exceptional closure with no "
                    "named approver is an unvalidated closure with extra words"
                )
            if not self.exceptional_reason:
                raise ValueError("CLOSED_EXCEPTIONAL requires exceptional_reason")
        return self

    @property
    def validated(self) -> bool:
        return self.validation is not None and self.validation.passed

    @property
    def first_time_fix(self) -> bool:
        """One visit or none, and it held.

        Zero truck rolls counts: an incident resolved remotely is the best possible first-time fix,
        and excluding it would make the KPI improve when field work increases.
        """
        return self.validated and self.truck_rolls <= 1
