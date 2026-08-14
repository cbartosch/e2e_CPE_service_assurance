"""Validated domain models.

The 34 record types the specification's "Required domain models" list names, plus six the workflow
needs in order to keep the required ones honest: `ActionRequest` (the typed envelope that makes the
six mandatory action fields unforgettable), `ActionRecord` (what happened, as distinct from what was
intended), `AuditEvent`, `CrewSlot`, `DispatchAssignment` and `WifiRadioSnapshot`.

The count is 34, not the 33 a quick read of the list suggests. It is asserted by
`tests/unit/test_domain_exports.py`, which parses the specification's own bullet list and compares
it to `__all__` -- a number in a docstring is a claim, and this one is checked.

Import from this module, not from the submodules. The split into `records` / `diagnosis` /
`resolution` / `governance` / `field_ops` / `closure` is a file-size convenience and is not a
contract; moving a model between them must not break a caller.
"""

from lpr_cpe.domain.base import (
    DomainModel,
    FrozenDomainModel,
    evidence_ref,
    idempotency_key,
    new_correlation_id,
    new_id,
    new_incident_id,
    object_reference,
)
from lpr_cpe.domain.closure import ClosureRecord, ReconciliationResult, ValidationResult
from lpr_cpe.domain.diagnosis import (
    AnomalyFinding,
    ImpactAssessment,
    PredictionResult,
    PreventiveMaintenanceCase,
    RCAHypothesis,
    RCAResult,
    ServiceProblemRecord,
    TestPlan,
    TestRequest,
    TestResult,
)
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    AreaArchetype,
    CaseType,
    CommunicationChannel,
    CrewType,
    DataQualityFlag,
    DelimiterKind,
    EventSource,
    EvidenceKind,
    FaultDomain,
    HealthBand,
    IncidentStatus,
    KPIName,
    MRStatus,
    PolicyOutcome,
    ReasonCode,
    Severity,
    Technology,
    TestKind,
    TestStatus,
    WifiBand,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import (
    CrewSlot,
    DispatchAssignment,
    DispatchPlan,
    DispatchRequirement,
    FieldFinding,
    HandoverContract,
    MRRecord,
    MRRequest,
    WorkOrder,
)
from lpr_cpe.domain.governance import (
    ActionRecord,
    ActionRequest,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    KPIEvent,
    PolicyDecision,
)
from lpr_cpe.domain.lifecycle import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    can_transition,
    require_transition,
)
from lpr_cpe.domain.records import (
    AssuranceEvent,
    CPERecord,
    DataQualityAssessment,
    EvidenceItem,
    SLAContext,
    TopologyContext,
    WifiRadioSnapshot,
)
from lpr_cpe.domain.resolution import (
    RemoteAction,
    ResolutionOption,
    ResolutionPlan,
    SelfHelpSession,
)

__all__ = [
    # base
    "DomainModel",
    "FrozenDomainModel",
    "evidence_ref",
    "idempotency_key",
    "new_correlation_id",
    "new_id",
    "new_incident_id",
    "object_reference",
    # enums
    "ActionOutcome",
    "ActionType",
    "ApprovalKind",
    "ApprovalStatus",
    "AreaArchetype",
    "CaseType",
    "CommunicationChannel",
    "CrewType",
    "DataQualityFlag",
    "DelimiterKind",
    "EventSource",
    "EvidenceKind",
    "FaultDomain",
    "HealthBand",
    "IncidentStatus",
    "KPIName",
    "MRStatus",
    "PolicyOutcome",
    "ReasonCode",
    "Severity",
    "Technology",
    "TestKind",
    "TestStatus",
    "WifiBand",
    "WorkOrderStatus",
    # lifecycle
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "can_transition",
    "require_transition",
    # the 34 models the specification requires by name, alphabetical
    "AnomalyFinding",
    "ApprovalDecision",
    "ApprovalRequest",
    "AssuranceEvent",
    "CPERecord",
    "ClosureRecord",
    "DataQualityAssessment",
    "DispatchPlan",
    "DispatchRequirement",
    "EvidenceItem",
    "FieldFinding",
    "HandoverContract",
    "ImpactAssessment",
    "KPIEvent",
    "MRRecord",
    "MRRequest",
    "PolicyDecision",
    "PredictionResult",
    "PreventiveMaintenanceCase",
    "RCAHypothesis",
    "RCAResult",
    "ReconciliationResult",
    "RemoteAction",
    "ResolutionOption",
    "ResolutionPlan",
    "SLAContext",
    "SelfHelpSession",
    "ServiceProblemRecord",
    "TestPlan",
    "TestRequest",
    "TestResult",
    "TopologyContext",
    "ValidationResult",
    "WorkOrder",
    # supporting
    "ActionRecord",
    "ActionRequest",
    "AuditEvent",
    "CrewSlot",
    "DispatchAssignment",
    "WifiRadioSnapshot",
]
