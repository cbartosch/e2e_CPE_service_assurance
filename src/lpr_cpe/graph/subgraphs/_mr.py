"""Filing a jTrack MR: the parts that must be identical whoever files it.

Extracted, not designed up front, and by `_shared`'s rule -- every function here was written inside
`field_execution.file_plant_mr` first, and the second caller is what moved it. That caller is
`plant_referral`, the NOC and provisioning entry into the plant branch: the specification's P20 has
two ways in, "the handover evidence, or the NOC/plant evidence package when the case reached this
step directly from D08 without a Clean Boots visit", and until that second way was built the first
one was the only owner of everything below.

**What would drift, if it were copied instead.** `plant_execution` is the stage that reads the MR
back, and it reads it entirely off the `MRRecord` this module writes: `outstanding_plant_mr` takes
the latest revision by `updated_at` and tests `awaiting_osp`, `search_plant_mr` re-reads jTrack by
`plant_object_ref`, and `plant_update_idempotency_key` extends `idempotency_key`. Two filers that
built that record differently would give D19 two different answers to "is this still with OSP?" for
two incidents in the same state, and nothing would fail -- the second one would simply be routed to
`retry_diagnosis` while the first waited. That is the failure mode `_shared` was created for.

**What is deliberately *not* here.** The `parameters` payload. The two filers assemble genuinely
different packages -- one from a `HandoverContract` a technician's finding produced, one from the
diagnosis alone -- and that difference is the specification's, not an accident to be factored away.
`REQUIRED_MR_FIELDS` is what holds the two to a common floor, and `submit_mr` checks it on both
paths so neither can reach the adapter's non-retryable refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    FaultDomain,
    IncidentStatus,
    MRStatus,
    PolicyOutcome,
    ReasonCode,
    Severity,
)
from lpr_cpe.domain.field_ops import MRRecord
from lpr_cpe.domain.governance import ActionRecord, ActionRequest
from lpr_cpe.graph.nodes._runtime import NodeUpdate, audit, derive_id
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.graph.subgraphs._shared import (
    attempt_number,
    evidence_support,
    executed_idempotency_keys,
)
from lpr_cpe.integrations.jtrack.simulator import REQUIRED_MR_FIELDS
from lpr_cpe.observability.kpi import MetricTimestamp, mark
from lpr_cpe.policies.engine import PolicyInput

if TYPE_CHECKING:
    from lpr_cpe.domain.field_ops import FieldFinding
    from lpr_cpe.domain.governance import ApprovalDecision, PolicyDecision
    from lpr_cpe.graph.context import GraphContext


def plant_object_ref(state: IncidentState, finding: FieldFinding | None) -> str:
    """What the MR is filed against: the delimiter a crew found, or topology's, or the service.

    `MRRequest.plant_object_ref` and `REQUIRED_MR_FIELDS` both insist on this, and D17 has already
    refused to reach P18 without a `delimiter_ref`, so on the Clean Boots path the first branch is
    the one that runs. The fall-back is the service reference and not an empty string: an MR whose
    subject is `""` is the non-retryable adapter refusal this module exists to prevent, and the
    service is at least something an OSP engineer can look up.

    `finding` is optional because the D08-direct case has none by construction -- no crew attended,
    so nobody delimited -- and that case takes `state["delimiter_ref"]`, which P03 resolved from the
    plant records. Measured across the ten fixture services that reach D08's plant arm, all ten
    carry one: four taps and ODPs by name, and none fell through to the service.
    """
    if finding is not None and finding.delimiter_ref:
        return finding.delimiter_ref
    return state.get("delimiter_ref") or state.get("service_ref") or ""


def mr_access_notes(state: IncidentState, finding: FieldFinding | None) -> str:
    """How a crew reaches the object. One of `REQUIRED_MR_FIELDS`, and never empty.

    Moved here from `field_execution._access_notes` by the rule this module is built on: the second
    caller is what moves a function, and `plant_referral` is it. The move matters more than most,
    because `access_notes` is the *one* required field a D08-direct case cannot borrow from
    anywhere. Measured across the ten fixtures that reach D08's plant arm, `plant_object_ref`,
    `fault_description` and `evidence_refs` are all fillable from `delimiter_ref` and `rca` -- and
    `access_notes` is absent on all ten, which is what makes it the field that would have produced
    `create_mr`'s non-retryable refusal.

    Never empty, and that is by construction rather than by check: the first part is always present,
    naming the plant object or saying it is unidentified. What the rest adds is what `topology`
    resolved. Measured on those ten: latitude and longitude on all ten, `area_archetype` on all ten
    (`remote_island` on eight of them), and `mdu_ref` on **none** -- so the `if topology.mdu_ref`
    clause is the one part of this that the D08-direct path never exercises. It is not removed,
    because the Clean Boots path shares this function and an MDU is exactly the case where an access
    note earns its keep.

    `finding` is optional for `plant_object_ref`'s reason -- no crew attended a D08-direct case --
    and the technician's note is what its absence costs. That is a real loss, not a formality: the
    difference between "a Clean crew stood at this cabinet" and "nobody has been" is the difference
    `plant_referral` records in its safety notes instead.
    """
    topology = state.get("topology")
    parts = [f"plant object {plant_object_ref(state, finding) or 'unidentified'}"]
    if topology is not None:
        if topology.latitude is not None and topology.longitude is not None:
            parts.append(f"at {topology.latitude:.5f},{topology.longitude:.5f}")
        if topology.mdu_ref:
            parts.append(f"MDU {topology.mdu_ref}")
        if topology.area_archetype is not None:
            parts.append(f"{topology.area_archetype.value} area")
    if finding is not None and finding.technician_note:
        parts.append(f"Clean Boots note: {finding.technician_note}")
    return "; ".join(parts)


def mr_idempotency_key(state: IncidentState, target_ref: str) -> str:
    """The key the MR is filed under. Derived, so the policy check and the send agree.

    Keyed on the **plant object**, not on the finding. Three consequences, all wanted. Across the
    `more_tests` loop the finding id moves on every lap while the tap does not, so a packet refused
    and re-offered for the same boundary keeps one key and jTrack's ledger suppresses the duplicate
    -- which is D18's "non-duplicative" enforced at the adapter as well as asserted by a router. A
    crew who re-delimits to a *different* object gets a different key, because that genuinely is a
    different MR. And the two filers agree without coordinating: a NOC referral and a Clean Boots
    handover that name the same ODP derive the same key, so the second is returned `replayed`
    rather than filed, which is what stops one incident holding two MRs for one boundary.

    Not keyed on the attempt, for `idempotency_key_for`'s reason: a key that moved with the retry
    counter would make every retry a fresh write and nothing would ever be suppressed.
    """
    return derive_id("IDK", state.get("incident_id") or "", ActionType.RAISE_MR.value, target_ref)


def mr_severity(state: IncidentState) -> Severity:
    """The MR's priority, read off `impact` and not off the packet.

    Item 20 of the handover packet carries `severity.value` for a human to read, and
    `MRRecord.severity` is the enum. Round-tripping through the string leaves pydantic to coerce it
    back, which works right up to the first severity whose name and value differ.
    """
    impact = state.get("impact")
    return impact.severity if impact is not None else Severity.MEDIUM


def mr_reference(record: MRRecord) -> str:
    """What to call this MR outside the graph: jTrack's reference, or ours until jTrack answers.

    `linked_records["mr"]` is what an operator quotes to OSP and what `file_plant_mr` writes into
    the work order's closing note, and the two must be the same string -- a note naming our internal
    id beside a link naming jTrack's would leave a reader unable to tell whether they were one MR or
    two. `external_ref` is `None` until the adapter has accepted the write, which is why the
    fall-back exists at all rather than being a defensive `or`.
    """
    return record.external_ref or record.mr_id


def mr_policy_input(
    state: IncidentState, ctx: GraphContext, *, target_ref: str, idempotency_key: str
) -> PolicyInput:
    """Everything the engine may consider about filing this MR.

    Built here rather than through `_shared.policy_input_for`, and the difference is not
    duplication. That function is keyed on a `ResolutionOption` -- it reads `option.action_type`,
    `option.blast_radius`, `option.reversible` and derives the idempotency key from `option_id` --
    and `field_execution` has no option anywhere in its state to key on. Its MR comes from a
    `FieldFinding` a technician submitted. That is the argument, and it is structural: one of the
    two filers simply cannot call that function.

    What it does *not* re-derive is any of the readings. `evidence_support`,
    `executed_idempotency_keys` and `attempt_number` are imported from `_shared`, so the engine is
    asked about corroboration, duplicate suppression and attempt count in exactly the words the
    other branches use.

    `blast_radius` comes from `impact` rather than from the option, and that is a second, smaller
    choice whose size was overstated here until it was measured. `plant_referral`'s case *does* have
    a `raise_mr` option -- P11 puts one in the plan for all ten D08-direct fixtures -- so for that
    filer both numbers are available, and they differ: measured, `option.blast_radius` is 1 on all
    ten while `impact.affected_customer_count` is 1 on six of them and 2 on four. The reason to
    prefer `impact` is ownership rather than magnitude -- it is the assessment that owns how many
    customers a fault reaches. An earlier draft of this paragraph argued the point by contrasting
    the option with `topology.homes_behind_delimiter`, which says 8 or 16 across the same ten and is
    a field neither this function nor the option reads.

    `contacts_today` and `minutes_since_last_contact` are left at their defaults, unlike
    `policy_input_for`, which supplies them for every action. `_check_customer_contact` returns
    immediately for anything outside `CUSTOMER_CONTACT_ACTIONS` and `raise_mr` is outside it -- but
    the argument in `policy_input_for` was that passing them unconditionally is what stops the next
    branch from being written without them, and that argument is about a function every branch
    shares. This one serves one action type, named in its own signature.
    """
    rca = state.get("rca")
    impact = state.get("impact")
    sla = state.get("sla")
    quality = state.get("data_quality")
    source_count, age_minutes = evidence_support(state, ctx.clock.now())
    return PolicyInput(
        action_type=ActionType.RAISE_MR,
        incident_id=state.get("incident_id") or "",
        target_ref=target_ref,
        actor_role=ctx.automation_role,
        rca_confidence=rca.confidence if rca is not None else None,
        evidence_source_count=source_count,
        evidence_age_minutes=age_minutes,
        data_quality_flags=tuple(quality.flags) if quality is not None else (),
        attempt=attempt_number(state, ActionType.RAISE_MR),
        blast_radius=impact.affected_customer_count if impact is not None else 1,
        severity=impact.severity if impact is not None else Severity.MEDIUM,
        vulnerable_customer=sla.vulnerable_customer if sla is not None else False,
        local_time=ctx.clock.local_now().time(),
        idempotency_key=idempotency_key,
        executed_idempotency_keys=executed_idempotency_keys(state),
    )


@dataclass(frozen=True)
class MRSubmission:
    """What `submit_mr` filed, and the part of the node update that is the same for both filers.

    The records are returned alongside `update` rather than only inside it because both callers
    read them back: `file_plant_mr` needs `record.mr_id` for its KPI discriminator and
    `request.approval_ref` for its contract copy, and digging either out of a dict of lists would
    be a second way to spell something `submit_mr` already knows.

    `completed_at` is the instant the MR reached jTrack, and it is the one a caller should reuse
    rather than re-read off the clock. `file_plant_mr` stamps the contract accepted at it: a second
    `ctx.clock.now()` would put the acceptance a tick *after* the filing under any advancing clock,
    which is a lie about the order of two things that happened together. It is a field of its own
    and not `action.completed_at`, which `ActionRecord` declares optional -- an action that never
    finished has none -- so a caller reaching through the record would have to narrow a `None` that
    cannot occur here.
    """

    request: ActionRequest
    result: dict[str, Any]
    record: MRRecord
    action: ActionRecord
    completed_at: datetime
    update: NodeUpdate


async def submit_mr(
    state: IncidentState,
    ctx: GraphContext,
    *,
    node_name: str,
    parameters: dict[str, Any],
    target_ref: str,
    fault_domain: FaultDomain,
    decision: PolicyDecision,
    approval: ApprovalDecision | None,
    discriminator: str,
    blast_radius: int,
    notes: list[str],
    evidence_refs: tuple[str, ...],
    detail: dict[str, Any],
    refusal_hint: str,
) -> MRSubmission:
    """Search jTrack, file the MR, and record what came back. P20's mechanism, for either package.

    The duplicate-suppression read runs first, because that is P20's own first instruction and
    because `fetch_open_mrs` is the only way to know: two crews confirming the same tap on the same
    afternoon must not produce two MRs, and neither must a NOC referral filed while one is already
    open. The result is recorded whichever way it comes back -- including empty, because "we asked
    and there were none" is the fact P20 requires and an absent key would be indistinguishable from
    not having asked. It is written into `parameters` here rather than by the caller so that the
    field cannot be forgotten by the next filer.

    `REQUIRED_MR_FIELDS` is re-checked before the `ActionRequest` is built, duplicating a check the
    adapter also makes. That is deliberate: `create_mr` raises a **non-retryable** `AdapterError` on
    a missing field, so the alternative to checking is an incident that dies two nodes after a human
    approved it. Checking locally turns that into a recorded refusal on a path the state machine has
    a status for. `refusal_hint` is the caller's own account of why the combination should have been
    unreachable, because the invariant differs: D18 requires `HandoverContract.complete` on one
    path, and topology resolution on the other.

    `approval` is passed in rather than read here, and that is which-owns-what rather than taste.
    Finding the answer means naming an `ApprovalKind`, and the filer is the one entitled to name it:
    it is the filer that asked the question, so a filer given a different kind tomorrow keeps the
    request's `approval_ref` and the record's `osp_owner` pointing at the answer it actually got.
    `None` is legitimate throughout and means nobody was asked, which is what the engine allowing
    the MR outright looks like -- `ActionRequest` only insists on an `approval_ref` when the policy
    outcome demands one.

    `update_mr` is not called here, and its absence is measured rather than an oversight. The
    specification's "update the existing MR when appropriate" needs a reachable state in which an MR
    already exists, and neither filer runs downstream of one. The only path that holds one is Stage
    4's plant branch, and that is where the call lives: `plant_execution.update_plant_mr`, updating
    the MR this function filed. What protects against the duplicate here is the idempotency key.

    The status is `mr_raised` and the incident stays active, which is P20's last line. Everything
    beyond that -- a work order to complete, a contract to mark accepted, KPIs about a handover that
    happened -- belongs to whichever filer had one, and is merged into `update` by the caller.
    """
    now = ctx.clock.now()
    existing = await ctx.adapters.jtrack.fetch_open_mrs(target_ref)
    parameters["known_open_mrs"] = [
        str(row.get("mr_ref") or row.get("external_ref") or "") for row in existing
    ]

    absent = [field for field in REQUIRED_MR_FIELDS if not parameters.get(field)]
    if absent:
        raise ValueError(
            f"{node_name} assembled an MR missing {absent}, which `create_mr` refuses "
            f"non-retryably. {refusal_hint}"
        )

    idempotency_key = mr_idempotency_key(state, target_ref)
    incident_id = state.get("incident_id") or ""
    action_id = derive_id("ACT", incident_id, discriminator)
    mr_id = derive_id("MR", incident_id, discriminator)
    attempt = attempt_number(state, ActionType.RAISE_MR)
    request = ActionRequest(
        action_id=action_id,
        incident_id=incident_id,
        action_type=ActionType.RAISE_MR,
        target_ref=target_ref,
        requested_at=now,
        idempotency_key=idempotency_key,
        actor=ctx.automation_actor,
        reason_code=(
            ReasonCode.POLICY_APPROVAL_REQUIRED
            if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
            else ReasonCode.POLICY_ALLOWED
        ),
        correlation_id=state.get("correlation_id") or incident_id,
        approval_ref=approval.approval_ref if approval is not None else None,
        policy_decision_id=decision.decision_id,
        policy_outcome=decision.outcome,
        attempt=attempt,
        parameters=parameters,
        reversible=False,
        expected_blast_radius=blast_radius or 1,
    )

    result = await ctx.adapters.jtrack.create_mr(request)
    completed_at = ctx.clock.now()
    outcome = ActionOutcome(str(result["outcome"]))
    external_ref = result.get("external_ref")
    severity = mr_severity(state)

    record = MRRecord(
        mr_id=mr_id,
        incident_id=incident_id,
        external_ref=external_ref,
        status=MRStatus(str(result.get("status") or MRStatus.SUBMITTED.value)),
        created_at=now,
        updated_at=completed_at,
        submitted_at=completed_at,
        fault_domain=fault_domain,
        plant_object_ref=target_ref,
        severity=severity,
        idempotency_key=idempotency_key,
        osp_owner=approval.decided_by if approval is not None else "",
        notes=notes,
    )
    action = ActionRecord(
        action_id=action_id,
        incident_id=incident_id,
        action_type=ActionType.RAISE_MR,
        target_ref=target_ref,
        idempotency_key=idempotency_key,
        outcome=outcome,
        started_at=now,
        completed_at=completed_at,
        actor=ctx.automation_actor,
        reason_code=request.reason_code,
        approval_ref=request.approval_ref,
        correlation_id=request.correlation_id,
        attempt=attempt,
        simulated=bool(result.get("simulated")),
        external_ref=external_ref,
        detail=str(result.get("detail") or ""),
        error=str(result.get("error") or ""),
        evidence_refs=evidence_refs,
    )

    update: NodeUpdate = {
        "status": IncidentStatus.MR_RAISED,
        "selected_action": request,
        "mr_records": [record],
        "action_history": [action],
        # Absolute, never `state.get(...) + 1`: `mr_attempt_count` reduces with `take_max` and an
        # increment computed at entry is exactly what that reducer exists to defeat. Counted
        # distinct by id for `_distinct_work_orders`' reason -- `mr_records` keeps revisions.
        "mr_attempt_count": len({*(r.mr_id for r in state.get("mr_records", [])), mr_id}),
        "linked_records": {"mr": mr_reference(record)},
        **mark(MetricTimestamp.MR_SUBMITTED_AT, completed_at),
        "updated_at": completed_at,
        "audit_events": [
            audit(
                state,
                ctx,
                node=node_name,
                action="create_mr",
                outcome=outcome.value,
                subject_ref=target_ref,
                reason_code=request.reason_code,
                detail={
                    "action_id": action_id,
                    "mr_id": mr_id,
                    "external_ref": external_ref,
                    "status": record.status.value,
                    "plant_object_ref": target_ref,
                    "attempt": attempt,
                    "idempotency_key": idempotency_key,
                    "approval_ref": request.approval_ref,
                    "policy_decision_id": decision.decision_id,
                    "policy_outcome": decision.outcome.value,
                    "open_mrs_before": parameters["known_open_mrs"],
                    "simulated": bool(result.get("simulated")),
                    "replayed": bool(result.get("replayed")),
                    "gate": result.get("gate"),
                    "detail": result.get("detail"),
                    **detail,
                },
                discriminator=action_id,
            )
        ],
    }
    return MRSubmission(
        request=request,
        result=result,
        record=record,
        action=action,
        completed_at=completed_at,
        update=update,
    )
