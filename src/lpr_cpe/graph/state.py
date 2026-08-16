"""The graph state contract and its reducers.

A LangGraph node returns a partial mapping and the runtime merges it. The default merge is
last-writer-wins, which is wrong for most of the fields here: two nodes that both observe evidence
in the same super-step would silently keep one observation, and an incident that revisits diagnosis
would overwrite the audit trail of its first pass. So the interesting fields carry explicit
reducers, and the reducers are where the invariants live.

Four reducer families, each solving a distinct problem:

* **`append_unique`** -- evidence, test results, actions, approvals, audit events, errors, KPI
  events. Append-only, de-duplicated on a natural key. De-duplication matters because a node that
  replays after an `interrupt()` re-runs from its start (measured; see IMPLEMENTATION_PLAN.md §2),
  so anything it appended before the interrupt is appended again. Without a key, every approval
  gate would double the audit trail.
* **`append_revision`** -- work orders and MR records. These *change*, but the history of the change
  is the evidence, so an update appends a new revision and `latest_by_id` reads back the current
  view. Nothing overwrites in place.
* **`write_once`** -- `sla`, `created_at`, `incident_id`. A second write is refused, not ignored.
  The SLA clock is the reason: "the clock never resets" is only true if something refuses to reset
  it, and a node cannot be relied on to check first.
* **`advance_status`** -- validates against `domain.lifecycle.TRANSITIONS`. An illegal transition
  raises. A wrong-but-plausible status is worse than a crash because a dashboard reports it as
  normal.

Counters use `max` rather than `+`. A replayed node that returns `remote_attempt_count: 2` after an
interrupt must not make it 4; the node computes the absolute value it intends and the reducer takes
the higher, which is idempotent under replay in a way that addition is not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, TypedDict, TypeVar

from lpr_cpe.domain.closure import ClosureRecord, ReconciliationResult, ValidationResult
from lpr_cpe.domain.diagnosis import (
    AnomalyFinding,
    ImpactAssessment,
    PredictionResult,
    RCAResult,
    TestPlan,
    TestResult,
)
from lpr_cpe.domain.enums import (
    CaseType,
    CrewType,
    DelimiterKind,
    EventSource,
    FaultDomain,
    IncidentStatus,
    Technology,
)
from lpr_cpe.domain.field_ops import (
    DispatchPlan,
    DispatchRequirement,
    FieldFinding,
    HandoverContract,
    MRRecord,
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
from lpr_cpe.domain.lifecycle import require_transition
from lpr_cpe.domain.records import (
    AssuranceEvent,
    CPERecord,
    DataQualityAssessment,
    EvidenceItem,
    SLAContext,
    TopologyContext,
)
from lpr_cpe.domain.resolution import (
    RemoteAction,
    ResolutionOption,
    ResolutionPlan,
    SelfHelpSession,
)

T = TypeVar("T")

# --------------------------------------------------------------------------------------------
# Reducers
# --------------------------------------------------------------------------------------------

# The attribute used to de-duplicate each appended type, tried in order. Named once here so a new
# appended type cannot quietly fall back to "no key" and lose de-duplication.
#
# `decision_id` is here because `PolicyDecision` was the type that had fallen back. It carries none
# of the other six, so it was keyed on its `repr` -- which contains `decided_at`, so two recordings
# of one decision a microsecond apart were two entries rather than one. `policy_block_rate` divides
# by the length of that list and `approval_outstanding` compares `max(answers) < max(demands)`;
# both read the duplicate as a genuine second evaluation. The `repr` fallback is the right default
# for the plain dicts in `errors` and wrong for anything that has an identity, which is what this
# tuple exists to enumerate.
_KEY_ATTRS = (
    "ref",
    "result_id",
    "action_id",
    "approval_id",
    "decision_id",
    "event_id",
    "finding_id",
    "key",
)


def _natural_key(item: Any) -> str:
    """The de-duplication key for an appended item.

    Falls back to a stable repr rather than to identity: two structurally identical error dicts
    appended by a replayed node are the same error, and keying them on `id()` would keep both.
    """
    for attr in _KEY_ATTRS:
        value = getattr(item, attr, None)
        if isinstance(value, str) and value:
            return f"{type(item).__name__}:{value}"
    if isinstance(item, dict):
        for attr in _KEY_ATTRS:
            value = item.get(attr)
            if isinstance(value, str) and value:
                return f"dict:{value}"
        return "dict:" + repr(sorted((k, repr(v)) for k, v in item.items()))
    return f"{type(item).__name__}:{item!r}"


def append_unique[T](current: list[T] | None, update: list[T] | T | None) -> list[T]:
    """Append-only with de-duplication on the natural key. First write of a key wins.

    First-write-wins, not last: the first append is the one whose timestamp and provenance were
    recorded at the moment of observation. A replay appending the same key again is a replay, not
    new information.
    """
    out: list[T] = list(current or [])
    if update is None:
        return out
    incoming = update if isinstance(update, list) else [update]
    seen = {_natural_key(x) for x in out}
    for item in incoming:
        key = _natural_key(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def append_revision[T](current: list[T] | None, update: list[T] | T | None) -> list[T]:
    """Append-only *without* de-duplication, for records that legitimately recur.

    A work order moving `scheduled` -> `dispatched` -> `on_site` produces three entries with the
    same id, and that sequence is the record. `latest_by_id` reads the current view. An identical
    consecutive duplicate is dropped, which is what makes this safe under node replay: a replayed
    node re-appending the same state is a no-op, but a genuine status change is not.
    """
    out: list[T] = list(current or [])
    if update is None:
        return out
    incoming = update if isinstance(update, list) else [update]
    for item in incoming:
        if out and out[-1] == item:
            continue
        out.append(item)
    return out


def write_once[T](current: T | None, update: T | None) -> T | None:
    """Refuse a second, differing write.

    An identical re-write is allowed, because a replayed node returns the same value it returned
    before and failing there would make every interrupt fatal. A *different* value is an error:
    silently keeping the first would leave the node believing it had changed something.
    """
    if current is None:
        return update
    if update is None or update == current:
        return current
    raise ValueError(
        f"write_once field already holds {current!r}; refusing to overwrite with {update!r}. "
        "The SLA clock and the incident identity are set at intake and never reset -- a re-open "
        "creates a linked incident instead."
    )


def advance_status(
    current: IncidentStatus | None, update: IncidentStatus | None
) -> IncidentStatus | None:
    """Validate the transition against the lifecycle table."""
    if update is None:
        return current
    if current is None:
        return update
    return require_transition(current, update)


def take_max(current: int | None, update: int | None) -> int:
    """Counters are absolute, not increments -- idempotent under node replay."""
    if update is None:
        return current or 0
    return max(current or 0, update)


def merge_dict(current: dict[str, Any] | None, update: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow merge, last writer wins per key. For `linked_records` and `metrics_timestamps`."""
    out = dict(current or {})
    if update:
        out.update(update)
    return out


def merge_retries(current: dict[str, int] | None, update: dict[str, int] | None) -> dict[str, int]:
    """Per-key `max`, for the same reason `take_max` exists."""
    out = dict(current or {})
    for key, value in (update or {}).items():
        out[key] = max(out.get(key, 0), value)
    return out


def latest_by_id[T](revisions: list[T], id_attr: str) -> dict[str, T]:
    """Collapse an `append_revision` list to the current view, keyed by id.

    Last write wins here -- the opposite of `append_unique` -- because these entries are successive
    states of one object and the last one is the current one.
    """
    out: dict[str, T] = {}
    for item in revisions:
        key = getattr(item, id_attr, None)
        if isinstance(key, str) and key:
            out[key] = item
    return out


# --------------------------------------------------------------------------------------------
# The state
# --------------------------------------------------------------------------------------------


class IncidentState(TypedDict, total=False):
    """Top-level graph state.

    `total=False` throughout: LangGraph nodes return partial mappings, and declaring fields required
    would make every node's return type a lie. `make_initial_state` is the only place a complete
    state is built, and it is what intake calls.

    No large blob appears here. Spectrum captures, photos and PDF reports are `object_reference`
    dicts (`domain.base.object_reference`) -- state is checkpointed on every super-step, so a 4 MB
    capture in state is 4 MB written per step per incident.
    """

    # -- identity ------------------------------------------------------------------------------
    # `thread_id` is a copy of `incident_id` (D1). It is carried explicitly because the value is
    # also the LangGraph thread key, and code that reads it should not have to know that.
    incident_id: Annotated[str | None, write_once]
    thread_id: Annotated[str | None, write_once]
    correlation_id: str
    source: EventSource
    case_type: CaseType
    status: Annotated[IncidentStatus | None, advance_status]
    technology: Technology

    # -- subject -------------------------------------------------------------------------------
    customer_ref: str | None
    product_ref: str | None
    service_ref: str | None
    cpe_ref: str | None
    cpe: CPERecord | None
    topology: TopologyContext | None
    sla: Annotated[SLAContext | None, write_once]

    # -- observation ---------------------------------------------------------------------------
    events: Annotated[list[AssuranceEvent], append_unique]
    evidence: Annotated[list[EvidenceItem], append_unique]
    data_quality: DataQualityAssessment | None
    anomaly_findings: Annotated[list[AnomalyFinding], append_unique]
    prediction: PredictionResult | None
    impact: ImpactAssessment | None

    # -- diagnosis -----------------------------------------------------------------------------
    test_plan: TestPlan | None
    test_results: Annotated[list[TestResult], append_unique]
    rca: RCAResult | None
    fault_domain: FaultDomain
    delimiter: DelimiterKind
    delimiter_ref: str | None
    diagnostic_cycles: Annotated[int, take_max]

    # -- resolution ----------------------------------------------------------------------------
    # `resolution_cycles` is to P11 what `diagnostic_cycles` is to P07, and it is a separate number
    # because the two stages are re-entered by separate loops. D12's `retry_diagnosis` goes back to
    # P10, so the self-help loop reaches P11 again without passing through P07 at all -- measured,
    # by walking the tables: of the five cycles in the parent graph, that one contains P11 and not
    # P07. One counter therefore cannot serve both.
    resolution_cycles: Annotated[int, take_max]
    resolution_options: list[ResolutionOption]
    resolution_plan: ResolutionPlan | None
    selected_action: ActionRequest | None
    action_history: Annotated[list[ActionRecord], append_unique]
    remote_actions: Annotated[list[RemoteAction], append_revision]
    self_help_session: SelfHelpSession | None

    # -- attempt counters. Absolute values merged with `max`, never incremented in place. --------
    remote_attempt_count: Annotated[int, take_max]
    self_help_attempt_count: Annotated[int, take_max]
    field_visit_count: Annotated[int, take_max]
    mr_attempt_count: Annotated[int, take_max]
    plant_attempt_count: Annotated[int, take_max]

    # -- field work ----------------------------------------------------------------------------
    dispatch_requirements: list[DispatchRequirement]
    dispatch_plan: DispatchPlan | None
    crew_type: CrewType | None
    work_orders: Annotated[list[WorkOrder], append_revision]
    field_findings: Annotated[list[FieldFinding], append_unique]
    handover_contract: HandoverContract | None
    mr_records: Annotated[list[MRRecord], append_revision]

    # -- governance ----------------------------------------------------------------------------
    # `pending_approval` is the request currently at an interrupt. It is NOT append-only: there is
    # at most one, and clearing it is how the resume path signals that the gate is closed.
    pending_approval: ApprovalRequest | None
    approvals: Annotated[list[ApprovalDecision], append_unique]
    policy_decisions: Annotated[list[PolicyDecision], append_unique]

    # -- closure -------------------------------------------------------------------------------
    validation: ValidationResult | None
    reconciliation: ReconciliationResult | None
    closure: ClosureRecord | None
    linked_records: Annotated[dict[str, str], merge_dict]

    # -- communication and audit ---------------------------------------------------------------
    customer_communications: Annotated[list[dict[str, Any]], append_unique]
    audit_events: Annotated[list[AuditEvent], append_unique]
    kpi_events: Annotated[list[KPIEvent], append_unique]

    # -- control -------------------------------------------------------------------------------
    errors: Annotated[list[dict[str, Any]], append_unique]
    retries: Annotated[dict[str, int], merge_retries]
    escalated: bool
    escalation_reason: str
    # Node-visit counts, for the bounded-loop guard. Separate from `retries`, which counts
    # *failures*: a node can be legitimately revisited many times without a single retry.
    node_visits: Annotated[dict[str, int], merge_retries]

    # -- timing --------------------------------------------------------------------------------
    metrics_timestamps: Annotated[dict[str, str], merge_dict]
    created_at: Annotated[datetime | None, write_once]
    updated_at: datetime | None


def make_initial_state(
    *,
    incident_id: str,
    correlation_id: str,
    event: AssuranceEvent,
    sla: SLAContext,
    now: datetime,
) -> IncidentState:
    """The one place a complete `IncidentState` is constructed.

    `thread_id` is set from `incident_id` here and nowhere else, which is the only mechanical
    guarantee that D1 holds. Everything else starts empty; a node that needs a value it does not
    find is expected to say so through `data_quality`, not to invent a default.
    """
    return IncidentState(
        incident_id=incident_id,
        thread_id=incident_id,
        correlation_id=correlation_id,
        source=event.source,
        case_type=event.case_type,
        status=IncidentStatus.NEW,
        technology=event.technology,
        customer_ref=event.customer_ref,
        product_ref=None,
        service_ref=event.service_ref,
        cpe_ref=event.cpe_ref,
        cpe=None,
        topology=None,
        sla=sla,
        events=[event],
        evidence=[],
        data_quality=None,
        anomaly_findings=[],
        prediction=None,
        impact=None,
        test_plan=None,
        test_results=[],
        rca=None,
        fault_domain=FaultDomain.UNKNOWN,
        delimiter=DelimiterKind.UNKNOWN,
        delimiter_ref=None,
        diagnostic_cycles=0,
        resolution_cycles=0,
        resolution_options=[],
        resolution_plan=None,
        selected_action=None,
        action_history=[],
        remote_actions=[],
        self_help_session=None,
        remote_attempt_count=0,
        self_help_attempt_count=0,
        field_visit_count=0,
        mr_attempt_count=0,
        plant_attempt_count=0,
        dispatch_requirements=[],
        dispatch_plan=None,
        crew_type=None,
        work_orders=[],
        field_findings=[],
        handover_contract=None,
        mr_records=[],
        pending_approval=None,
        approvals=[],
        policy_decisions=[],
        validation=None,
        reconciliation=None,
        closure=None,
        linked_records={},
        customer_communications=[],
        audit_events=[],
        kpi_events=[],
        errors=[],
        retries={},
        escalated=False,
        escalation_reason="",
        node_visits={},
        metrics_timestamps={"created_at": now.isoformat()},
        created_at=now,
        updated_at=now,
    )


# --------------------------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------------------------
#
# These exist so that "how many truck rolls has this incident had?" has one answer. Computing it at
# each call site from `work_orders` is how two dashboards end up disagreeing -- one counting rows,
# the other counting revisions.


def current_work_orders(state: IncidentState) -> dict[str, WorkOrder]:
    return latest_by_id(state.get("work_orders", []), "work_order_id")


def current_mr_records(state: IncidentState) -> dict[str, MRRecord]:
    return latest_by_id(state.get("mr_records", []), "mr_id")


def truck_roll_count(state: IncidentState) -> int:
    """Distinct work orders a crew actually travelled to.

    Counts current *states*, not revisions: an order that reached `on_site` and then `completed`
    appears twice in the revision list and is one truck roll.
    """
    return sum(1 for wo in current_work_orders(state).values() if wo.counted_as_truck_roll)


def open_mr_count(state: IncidentState) -> int:
    return sum(1 for mr in current_mr_records(state).values() if mr.awaiting_osp)


def approval_for(state: IncidentState, approval_id: str) -> ApprovalDecision | None:
    return next((a for a in state.get("approvals", []) if a.approval_id == approval_id), None)


def latest_approval(state: IncidentState) -> ApprovalDecision | None:
    approvals = state.get("approvals", [])
    return approvals[-1] if approvals else None


def evidence_by_ref(state: IncidentState) -> dict[str, EvidenceItem]:
    return {e.ref: e for e in state.get("evidence", [])}


def visit_count(state: IncidentState, node: str) -> int:
    return state.get("node_visits", {}).get(node, 0)


def bump_visit(state: IncidentState, node: str) -> dict[str, int]:
    """The partial update a node returns to record its own visit.

    Absolute, not an increment, for the same reason as the counters: a replayed node must not
    inflate the count and trip the loop guard early.
    """
    return {node: visit_count(state, node) + 1}


def total_steps(state: IncidentState) -> int:
    return sum(state.get("node_visits", {}).values())
