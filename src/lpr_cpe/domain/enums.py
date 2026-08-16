"""Controlled vocabularies.

Every status, severity, fault domain and reason code in the system is named here once. A string
literal for one of these anywhere else in the package is a bug: the reason codes in particular are
carried into `jTrack` MR records and audit events, and a typo'd reason code is a record that can
never be found again by the thing that files it.

`StrEnum` rather than `Enum` so a member serialises as its own value through Pydantic, JSON, YAML
and the policy pack without a custom encoder, and so `status == "open"` reads naturally in a test.
"""

from __future__ import annotations

from enum import StrEnum


class Technology(StrEnum):
    """The access technology of the service under assurance.

    One field rather than two code paths -- see IMPLEMENTATION_PLAN.md D5. `UNKNOWN` is a real state
    at intake, before inventory has been consulted, and is not a synonym for "either".
    """

    HFC = "hfc"
    PON = "pon"
    UNKNOWN = "unknown"


class CaseType(StrEnum):
    """Why this incident exists. Drives the first routing decision out of intake."""

    CUSTOMER_REPORTED = "customer_reported"
    PROACTIVE_ALARM = "proactive_alarm"
    PREDICTIVE_MAINTENANCE = "predictive_maintenance"
    POST_INSTALL_BASELINE = "post_install_baseline"
    BULK_DEGRADATION = "bulk_degradation"
    REPEAT_VISIT = "repeat_visit"


class EventSource(StrEnum):
    """Where the triggering signal came from. Kept distinct from `CaseType`:
    the same NXT alarm can open a proactive case or corroborate a customer-reported one."""

    NXT = "nxt"
    CPE_SCAN = "cpe_scan"
    CUSTOMER = "customer"
    CRM = "crm"
    FIELD = "field"
    SCHEDULER = "scheduler"
    WFM = "wfm"
    JTRACK = "jtrack"
    MANUAL = "manual"


class IncidentStatus(StrEnum):
    """The incident lifecycle. The transitions permitted between these are owned by
    `domain.lifecycle.TRANSITIONS`, not by whichever node happens to set the field."""

    NEW = "new"
    TRIAGING = "triaging"
    DIAGNOSING = "diagnosing"
    AWAITING_APPROVAL = "awaiting_approval"
    REMOTE_RESOLUTION = "remote_resolution"
    SELF_HELP = "self_help"
    AWAITING_CUSTOMER = "awaiting_customer"
    DISPATCH_PLANNING = "dispatch_planning"
    FIELD_IN_PROGRESS = "field_in_progress"
    AWAITING_HANDOVER = "awaiting_handover"
    MR_RAISED = "mr_raised"
    AWAITING_PLANT_REPAIR = "awaiting_plant_repair"
    VALIDATING = "validating"
    RECONCILING = "reconciling"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


class Severity(StrEnum):
    """Ordered low → critical. `rank()` exists because StrEnum comparison is alphabetical,
    which would put `critical` below `low`; anything sorting on severity must call it."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def from_rank(cls, rank: int) -> Severity:
        clamped = max(0, min(rank, len(_SEVERITY_ORDER) - 1))
        return _SEVERITY_ORDER[clamped]


_SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
)
_SEVERITY_RANK: dict[Severity, int] = {s: i for i, s in enumerate(_SEVERITY_ORDER)}


class FaultDomain(StrEnum):
    """Where the fault is, which is the question the whole diagnosis stage exists to answer.

    `INSIDE_HOME` versus `DROP` versus `PLANT` is the Clean/Dirty Boots boundary: Clean Boots own
    everything from the tap or ODP inward, Dirty Boots own the plant. `UNKNOWN` means diagnosis has
    not concluded; `MULTIPLE` means it has, and found more than one.
    """

    CPE = "cpe"
    INSIDE_HOME_WIRING = "inside_home_wiring"
    DROP = "drop"
    TAP_OR_ODP = "tap_or_odp"
    DISTRIBUTION = "distribution"
    FEEDER = "feeder"
    NODE_OR_OLT = "node_or_olt"
    HEADEND_OR_CO = "headend_or_co"
    POWER = "power"
    SERVICE_PLATFORM = "service_platform"
    PROVISIONING = "provisioning"
    CUSTOMER_ENVIRONMENT = "customer_environment"
    NO_FAULT_FOUND = "no_fault_found"
    MULTIPLE = "multiple"
    UNKNOWN = "unknown"


class CrewType(StrEnum):
    """Who can be sent. `JOINT` is a single dispatch of both, used when the delimiter is at the
    tap/ODP itself and neither crew can finish alone -- the case that otherwise becomes two
    sequential visits and a repeat-visit KPI hit."""

    CLEAN = "clean"
    DIRTY = "dirty"
    JOINT = "joint"


class AreaArchetype(StrEnum):
    """The four Puerto Rico operating contexts. Travel time, crew availability and parts logistics
    differ enough between them that the dispatch optimizer weights them explicitly."""

    METRO_MDU = "metro_mdu"
    COASTAL_CITY_SUBURB = "coastal_city_suburb"
    CENTRAL_MOUNTAIN_RURAL = "central_mountain_rural"
    REMOTE_ISLAND = "remote_island"


class DelimiterKind(StrEnum):
    """The demarcation object. Tap for HFC, ODP for PON -- resolved by one function
    (`decision_services.delimiter.delimiter_kind_for`) rather than branched on at each use."""

    TAP = "tap"
    ODP = "odp"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    """Every action the system can take, remote or physical. The policy pack keys on these,
    so adding a member without adding a policy rule makes the engine fail closed."""

    # remote, low risk
    READ_STATUS = "read_status"
    RUN_DIAGNOSTIC = "run_diagnostic"
    # remote, changes customer-visible state
    CPE_REBOOT = "cpe_reboot"
    CPE_RESYNC = "cpe_resync"
    CPE_FIRMWARE_UPDATE = "cpe_firmware_update"
    CPE_FACTORY_RESET = "cpe_factory_reset"
    WIFI_CHANNEL_CHANGE = "wifi_channel_change"
    WIFI_POWER_CHANGE = "wifi_power_change"
    PROFILE_CHANGE = "profile_change"
    REPROVISION = "reprovision"
    # network-affecting
    NODE_LEVEL_RESET = "node_level_reset"
    OLT_PORT_RESET = "olt_port_reset"
    BULK_CONFIG_PUSH = "bulk_config_push"
    # workflow
    SEND_SELF_HELP = "send_self_help"
    CREATE_WORK_ORDER = "create_work_order"
    CANCEL_WORK_ORDER = "cancel_work_order"
    RAISE_MR = "raise_mr"
    UPDATE_MR = "update_mr"
    NOTIFY_CUSTOMER = "notify_customer"
    CLOSE_INCIDENT = "close_incident"
    CREATE_PM_CASE = "create_pm_case"


class ActionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SIMULATED = "simulated"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    AWAITING_APPROVAL = "awaiting_approval"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


class SelfHelpOutcome(StrEnum):
    """Where a guided self-help session ended, and the word D12 routes on.

    Four terminal members for five situations: "the customer complied and the telemetry cannot say
    whether it worked" has no member of its own and is recorded as `NOT_RESOLVED`. That is the
    conservative direction and the deliberate one -- D12 sends `RESOLVED` to validation and
    everything else back round, and an unconfirmable step is not a restoration. The distinction is
    not lost, it is just not in this field: `SelfHelpSession.notes` carries the summary that says
    which of the two it was.

    Distinct from `ActionOutcome`, which is how a *remote* action ended. The two vocabularies share
    `TIMED_OUT` and nothing else, and a session that a customer declined has no `ActionOutcome`
    spelling at all.
    """

    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    NOT_RESOLVED = "not_resolved"
    DECLINED = "declined"
    TIMED_OUT = "timed_out"


class PolicyOutcome(StrEnum):
    """The three answers the policy engine may give. There is no fourth, and no `None`:
    an unmatched request is `BLOCKED`, which is what "fail closed" means here."""

    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    BLOCKED = "blocked"


class ReasonCode(StrEnum):
    """Machine-readable justification, carried on every action, policy decision and audit event.

    These leave the system -- into jTrack MRs, into WFM work-order notes, into the audit log -- so
    they are stable identifiers, not prose. Prose belongs in the adjacent `explanation` field.
    """

    # policy
    POLICY_ALLOWED = "POLICY_ALLOWED"
    POLICY_NO_MATCHING_RULE = "POLICY_NO_MATCHING_RULE"
    POLICY_WRITES_DISABLED = "POLICY_WRITES_DISABLED"
    POLICY_BLAST_RADIUS_EXCEEDED = "POLICY_BLAST_RADIUS_EXCEEDED"
    POLICY_MAINTENANCE_WINDOW_REQUIRED = "POLICY_MAINTENANCE_WINDOW_REQUIRED"
    POLICY_APPROVAL_REQUIRED = "POLICY_APPROVAL_REQUIRED"
    POLICY_ACTION_NOT_PERMITTED_FOR_ROLE = "POLICY_ACTION_NOT_PERMITTED_FOR_ROLE"
    POLICY_ATTEMPT_LIMIT_REACHED = "POLICY_ATTEMPT_LIMIT_REACHED"
    POLICY_EVIDENCE_INSUFFICIENT = "POLICY_EVIDENCE_INSUFFICIENT"
    POLICY_VULNERABLE_CUSTOMER_PROTECTION = "POLICY_VULNERABLE_CUSTOMER_PROTECTION"
    POLICY_QUIET_HOURS = "POLICY_QUIET_HOURS"
    POLICY_DUPLICATE_SUPPRESSED = "POLICY_DUPLICATE_SUPPRESSED"

    # diagnosis
    RCA_CONFIDENT = "RCA_CONFIDENT"
    RCA_LOW_CONFIDENCE = "RCA_LOW_CONFIDENCE"
    RCA_CONFLICTING_EVIDENCE = "RCA_CONFLICTING_EVIDENCE"
    DATA_QUALITY_INSUFFICIENT = "DATA_QUALITY_INSUFFICIENT"
    COMMON_CAUSE_CLUSTER = "COMMON_CAUSE_CLUSTER"
    RECENT_CHANGE_SUSPECTED = "RECENT_CHANGE_SUSPECTED"
    POWER_OR_WEATHER_CORRELATED = "POWER_OR_WEATHER_CORRELATED"

    # resolution
    REMOTE_FIX_APPLIED = "REMOTE_FIX_APPLIED"
    REMOTE_FIX_EXHAUSTED = "REMOTE_FIX_EXHAUSTED"
    SELF_HELP_SUCCEEDED = "SELF_HELP_SUCCEEDED"
    SELF_HELP_DECLINED = "SELF_HELP_DECLINED"
    SELF_HELP_TIMED_OUT = "SELF_HELP_TIMED_OUT"
    CUSTOMER_ACCESS_REQUIRED = "CUSTOMER_ACCESS_REQUIRED"
    PHYSICAL_FAULT_CONFIRMED = "PHYSICAL_FAULT_CONFIRMED"

    # field / handover
    HANDOVER_ACCEPTED = "HANDOVER_ACCEPTED"
    HANDOVER_REJECTED_INCOMPLETE = "HANDOVER_REJECTED_INCOMPLETE"
    HANDOVER_REJECTED_WRONG_DOMAIN = "HANDOVER_REJECTED_WRONG_DOMAIN"
    PLANT_FAULT_CONFIRMED = "PLANT_FAULT_CONFIRMED"
    NO_FAULT_FOUND = "NO_FAULT_FOUND"
    PARTS_UNAVAILABLE = "PARTS_UNAVAILABLE"
    ACCESS_DENIED = "ACCESS_DENIED"
    WEATHER_STOOD_DOWN = "WEATHER_STOOD_DOWN"

    # closure
    VALIDATED_STABLE = "VALIDATED_STABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    STABILITY_WINDOW_PENDING = "STABILITY_WINDOW_PENDING"
    CLOSED_NORMAL = "CLOSED_NORMAL"
    CLOSED_EXCEPTIONAL = "CLOSED_EXCEPTIONAL"
    CLOSED_DUPLICATE = "CLOSED_DUPLICATE"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"

    # control
    LOOP_LIMIT_REACHED = "LOOP_LIMIT_REACHED"
    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class EvidenceKind(StrEnum):
    """What a piece of evidence is. `evidence` in state holds these, not raw telemetry:
    a spectrum capture is an object reference plus metadata (see the spec's rule about
    not putting large blobs in graph state)."""

    NXT_ALARM = "nxt_alarm"
    RF_MEASUREMENT = "rf_measurement"
    PNM_CAPTURE = "pnm_capture"
    OPTICAL_MEASUREMENT = "optical_measurement"
    CPE_STATUS = "cpe_status"
    WIFI_SCAN = "wifi_scan"
    SPEED_TEST = "speed_test"
    TOPOLOGY_LOOKUP = "topology_lookup"
    INVENTORY_LOOKUP = "inventory_lookup"
    CHANGE_RECORD = "change_record"
    POWER_OUTAGE_REPORT = "power_outage_report"
    WEATHER_REPORT = "weather_report"
    CUSTOMER_STATEMENT = "customer_statement"
    TECHNICIAN_NOTE = "technician_note"
    PHOTO_REFERENCE = "photo_reference"
    TEST_RESULT = "test_result"
    CLUSTER_ANALYSIS = "cluster_analysis"
    MR_UPDATE = "mr_update"


class TestKind(StrEnum):
    # A diagnostic test on the line, not a pytest test. Any test module that imports this name has
    # it in its namespace under a `Test*` prefix, which is pytest's collection pattern -- without
    # this opt-out every such module emits a PytestCollectionWarning. Declared here, at the one
    # place the name is chosen, rather than aliased at each of the import sites. Enum leaves dunder
    # attributes as plain class attributes, so this does not become a member.
    __test__ = False

    HFC_RF_LEVELS = "hfc_rf_levels"
    HFC_PNM_SWEEP = "hfc_pnm_sweep"
    PON_OPTICAL_POWER = "pon_optical_power"
    PON_OMCI_STATUS = "pon_omci_status"
    CPE_CONNECTIVITY = "cpe_connectivity"
    CPE_WIFI_SURVEY = "cpe_wifi_survey"
    THROUGHPUT = "throughput"
    LATENCY_JITTER_LOSS = "latency_jitter_loss"
    PROVISIONING_CHECK = "provisioning_check"
    SERVICE_PLATFORM_CHECK = "service_platform_check"
    NEIGHBOUR_COMPARISON = "neighbour_comparison"


class TestStatus(StrEnum):
    __test__ = False  # See TestKind: a diagnostic test, not a pytest one.

    PLANNED = "planned"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    UNAVAILABLE = "unavailable"


class WorkOrderStatus(StrEnum):
    DRAFT = "draft"
    REQUESTED = "requested"
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    EN_ROUTE = "en_route"
    ON_SITE = "on_site"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED_ACCESS = "failed_access"


class MRStatus(StrEnum):
    """jTrack MR states. `SUBMITTED` versus `ACCEPTED` matters: a submitted MR that OSP never
    accepted is the silent stall the reconciliation stage exists to catch."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    PLANNED = "planned"
    COMPLETED = "completed"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class ApprovalKind(StrEnum):
    """The six interrupt points, named. The graph's interrupt payload carries one of these so the
    API can tell an operator what they are being asked, and so `POST /approvals` can be matched to
    the right pending interrupt."""

    LOW_CONFIDENCE_RCA = "low_confidence_rca"
    HIGH_RISK_REMOTE_ACTION = "high_risk_remote_action"
    DISPATCH = "dispatch"
    CLEAN_TO_DIRTY_HANDOVER = "clean_to_dirty_handover"
    HIGH_BLAST_RADIUS_ACTION = "high_blast_radius_action"
    EXCEPTIONAL_CLOSURE = "exceptional_closure"


class WifiBand(StrEnum):
    BAND_2_4 = "2.4GHz"
    BAND_5 = "5GHz"
    BAND_6 = "6GHz"


class HealthBand(StrEnum):
    """Deterministic banding of the Wi-Fi health score. The thresholds live in the policy pack;
    this enum is only the vocabulary. The model is never asked to produce one of these
    (IMPLEMENTATION_PLAN.md D6)."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AT_RISK = "at_risk"
    CRITICAL = "critical"


class DataQualityFlag(StrEnum):
    STALE_DATA = "stale_data"
    MISSING_FIELD = "missing_field"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    INCONSISTENT_TOPOLOGY = "inconsistent_topology"
    NO_BASELINE = "no_baseline"
    LOW_SAMPLE_COUNT = "low_sample_count"
    CLOCK_SKEW = "clock_skew"
    CONFLICTING_SOURCES = "conflicting_sources"


class CommunicationChannel(StrEnum):
    SMS = "sms"
    EMAIL = "email"
    PUSH = "push"
    VOICE = "voice"
    APP = "app"


class KPIName(StrEnum):
    """Every KPI the system emits. Calculated, never hard-coded -- the value on a `KPIEvent` is
    always derived from state at the moment of emission."""

    INCIDENTS_CREATED = "incidents_created"
    PROACTIVE_DETECTION_RATE = "proactive_detection_rate"
    PREDICTIVE_SCANS_RUN = "predictive_scans_run"
    PREDICTIVE_TRUE_POSITIVE_RATE = "predictive_true_positive_rate"
    TIME_TO_DETECT_SECONDS = "time_to_detect_seconds"
    TIME_TO_TRIAGE_SECONDS = "time_to_triage_seconds"
    TIME_TO_DIAGNOSE_SECONDS = "time_to_diagnose_seconds"
    TIME_TO_RESTORE_SECONDS = "time_to_restore_seconds"
    REMOTE_RESOLUTION_RATE = "remote_resolution_rate"
    SELF_HELP_SUCCESS_RATE = "self_help_success_rate"
    DISPATCH_AVOIDANCE_RATE = "dispatch_avoidance_rate"
    FIRST_TIME_FIX_RATE = "first_time_fix_rate"
    REPEAT_VISIT_RATE = "repeat_visit_rate"
    NO_FAULT_FOUND_RATE = "no_fault_found_rate"
    TRUCK_ROLLS_PER_INCIDENT = "truck_rolls_per_incident"
    HANDOVER_ACCEPTANCE_RATE = "handover_acceptance_rate"
    HANDOVER_REWORK_RATE = "handover_rework_rate"
    MR_CYCLE_TIME_SECONDS = "mr_cycle_time_seconds"
    MR_REJECTION_RATE = "mr_rejection_rate"
    PLANT_REPAIR_BACKLOG = "plant_repair_backlog"
    SLA_BREACH_RATE = "sla_breach_rate"
    SLA_AT_RISK_COUNT = "sla_at_risk_count"
    APPROVAL_WAIT_SECONDS = "approval_wait_seconds"
    APPROVAL_REJECTION_RATE = "approval_rejection_rate"
    POLICY_BLOCK_RATE = "policy_block_rate"
    AUTOMATION_COVERAGE_RATE = "automation_coverage_rate"
    DATA_QUALITY_DEFECT_RATE = "data_quality_defect_rate"
    CUSTOMER_CONTACTS_PER_INCIDENT = "customer_contacts_per_incident"
