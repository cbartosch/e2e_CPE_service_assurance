"""Stage 4's plant wait: the MR is with OSP. Chase it, and record what a Dirty crew sends back.

P20's second instruction -- "update the existing MR when appropriate" -- and P21, the crew's own
report. `field_execution` files the MR and stops; this is what the incident does between filing and
D19, and it is the only caller of `update_mr` in `src`.

Three nodes and a local gate
----------------------------
* **Asking jTrack is not telling jTrack.** `search_plant_mr` performs P20's own first instruction,
  the duplicate-suppression read, and it is a node of its own for a second reason as well:
  `update_mr` refuses **non-retryably** when jTrack is not holding the MR open, and the only honest
  way to know that before building an `ActionRequest` is to have asked. This is `file_plant_mr`'s
  argument for re-checking `REQUIRED_MR_FIELDS` locally, applied to the other adapter refusal --
  checking beforehand turns an incident that dies mid-stage into a recorded outcome on a path the
  state machine has a status for.
* **Telling OSP is not hearing from OSP.** `update_plant_mr` sends our chase;
  `capture_plant_evidence` pauses for their answer. Splitting them is the rule `graph.interrupts`
  states: everything before `interrupt()` re-runs on resume, so a node that chased *and* waited
  would re-send the chase on every resume -- and the update's key moves per lap (below), so the
  ledger would not suppress it.

The gate hangs off `search_plant_mr` rather than off `START`, for `build_field_execution_graph`'s
reason: the parent's edge into this subgraph is already guarded, so an edge from `START` would carry
an `ESCALATED` arm nothing can take, whereas an edge out of node one can take it because `@node`
calls `check_budgets` on entry.

What the gate reads, and why it is D19's reading
------------------------------------------------
`outstanding_plant_mr` takes the latest revision by `updated_at` and tests `awaiting_osp` -- the
same two steps, in the same order, that `route_plant_outcome` takes. That is deliberate and it is
what makes the stage total. Whenever D19 will answer `await_plant` and route back here, the gate
answers `chase`; whenever it will answer `restored` or `retry_diagnosis`, the gate answers
`no_plant_action` and the stage is a no-op. So there is no state in which this subgraph acts on an
MR it cannot act on, and none in which it declines one it should have chased.

`no_plant_action` is a real arm reached by real paths, not a defensive default. Two of
`field_execution`'s four exits hold no MR at all -- `abandon_handover` refuses the handover before
`file_plant_mr` runs, and `route_visit_gate`'s `no_visit` never opened a visit -- and a rejected or
cancelled MR reaches it too, which matters because those are exactly the states `update_mr` refuses.

The policy verdict on an update, measured
-----------------------------------------
`update_plant_mr` puts the chase to the engine itself rather than reading a decision somebody else
recorded, because no upstream node evaluates `UPDATE_MR`. Measured against the shipped pack with
`actor_role=automation`: `update_mr` is `{allowed: true, risk: low}` with **no** `approval_kind`,
its decision class is `diagnosis`, and `RCAPolicy.minimum_for("diagnosis")` is 0.75 -- so a
confidence of 0.75 or better is `allowed`, and anything below it, including an absent confidence,
is `requires_approval` naming `low_confidence_rca`. `ActionRequest` refuses `REQUIRES_APPROVAL`
with no `approval_ref`, and this stage owns no interrupt for it, so that verdict is recorded and
nothing is sent -- `record_chronic_pattern`'s shape, for the same reason.

**No blast radius is claimed, and the omission is load-bearing.** `PolicyInput.blast_radius`
defaults to `None` and `_check_blast_radius` returns immediately on it. Supplying the incident's
affected count instead was measured: the `low` risk class caps blast radius at **1**, the cap check
emits a *blocking* finding and returns before the `network_action_threshold` clause, and
`update_mr` has no `approval_kind` for that clause to route to anyway. So an update carrying a
plant-scale radius is `blocked` at 50 services and at 500 -- which would be every MR worth filing,
and a policy nobody means: the pack expresses a refusal it means with `allowed: false`, as it does
for `bulk_config_push`. The quantity is right rather than merely convenient. Appending a note to a
ticket dispatches nobody and touches no service; the services sit behind the plant object the *MR*
is about, and `raise_mr` is the action that already carried that number to the engine and to an
approver.

The key moves per lap, and the create's does not
------------------------------------------------
`mr_idempotency_key` is keyed on the plant object precisely so that a re-offered packet cannot file
a second MR. `plant_update_idempotency_key` carries the attempt, which is the opposite rule, and
`update_mr`'s own docstring is where it comes from: each update "carries its own idempotency key so
it is not mistaken for a replay of the creation". A fixed key would have `SimulatedAdapterBase`
return every chase after the first as `replayed` and OSP would be told once, however long the MR
sat. `attempt_number` is the right counter rather than a clock reading because it counts actions
that reached the adapter: `with_retry`'s retries happen inside one node execution and share the
number, so a transient failure is still suppressed, while a genuine second lap gets a new key.

What the capture may write, and the rule it does not inherit
------------------------------------------------------------
`capture_plant_evidence` records the status OSP reported, from the whole of `MRStatus`, and records
no status at all when the report is absent or unparseable. It does **not** call `update_mr`: the
report comes *from* OSP, and echoing it back would tell them what they just told us.

An earlier reading of this module had it refuse to write `MRStatus.ACCEPTED`, on the strength of
`create_mr`'s comment that "a simulator that reported ACCEPTED would hide the silent stall". That
rule is real and it does not transfer. It forbids *us* manufacturing OSP's acceptance at the moment
we file; here the OSP side is the one answering, acceptance is the first item on P21's capture list,
and `MRRecord.accepted_at` exists so that `acceptance_latency()` can be measured. What the stall
argument does forbid is a **default**, and there is none: an absent status leaves the record
untouched and the crew is asked again, which is `field_submission`'s rule and for its reason.

A supplied completion instant is used when it parses and is timezone-aware; otherwise the instant we
were told is recorded and the audit event says which, so a reader knows `mr_cycle_time` is an upper
bound rather than OSP's own measurement. A naive instant is dropped rather than localised --
`MRRecord.cycle_time()` subtracts two datetimes and a naive one raises against an aware one, so
guessing the zone here would trade a missing KPI for a crashing one.

`RESTORED_AT` is deliberately not stamped. `restoration_validation` already owns that stamp for "a
fix nobody could verify at the moment it was applied -- a plant repair, a field visit", and a second
owner is how the two disagree.

What this stage cannot close
----------------------------
There is no OSP status feed. `JTrackAdapter` can be asked about an MR and told to append to one, but
nothing pushes a Dirty crew's progress at us, so the report arrives through `interrupt()` with no
adapter fallback -- exactly as `capture_field_evidence` takes the Clean Boots half, and for the same
recorded reason. That is gap EXEC-2 in `docs/vendor-integration-gaps.md`; when the feed exists, this
is the node that gains a second channel and `plant_report` is the one parser both would go through.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    EvidenceKind,
    IncidentStatus,
    KPIName,
    MRStatus,
    PolicyOutcome,
    ReasonCode,
    Severity,
)
from lpr_cpe.domain.field_ops import MRRecord
from lpr_cpe.domain.governance import ActionRecord, ActionRequest
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.nodes._runtime import (
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    emit_kpi,
    make_evidence,
    node,
    preview,
)
from lpr_cpe.graph.state import IncidentState, current_mr_records
from lpr_cpe.graph.subgraphs._shared import (
    attempt_number,
    evidence_support,
    executed_idempotency_keys,
)
from lpr_cpe.policies.engine import PolicyInput

#: The node that owns the pause. Named once because the round counter joins on it.
CAPTURE_NODE = "capture_plant_evidence"

#: The node that asks jTrack what it is holding. Named for the same reason: `known_open_mr_refs`
#: reads its own event back off the trail.
SEARCH_NODE = "search_plant_mr"


# ------------------------------------------------------------------------------------------------
# Reading the incident for the plant wait
# ------------------------------------------------------------------------------------------------


def latest_mr(state: IncidentState) -> MRRecord | None:
    """The MR revision D19 will judge, or `None` when the incident holds no MR at all.

    Read through `current_mr_records`, which collapses `mr_records`' revisions to the current view,
    then the most recently updated of those -- the same two steps `route_plant_outcome` takes. One
    reading rather than two similar ones, so the gate below cannot drift from the decision that
    follows this stage.
    """
    records = current_mr_records(state)
    if not records:
        return None
    return max(records.values(), key=lambda record: record.updated_at)


def outstanding_plant_mr(state: IncidentState) -> MRRecord | None:
    """The MR this stage may act on: one OSP is holding and has not finished.

    `awaiting_osp` is the model's own boundary -- submitted, accepted, planned, in progress -- and
    it is deliberately narrower than `not terminal`. A `draft` MR is mutable at the adapter but has
    never been sent, so chasing it would be asking OSP about something they have not seen; D19 reads
    it as `retry_diagnosis`, which is the true answer for a create that did not land.
    """
    record = latest_mr(state)
    return record if record is not None and record.awaiting_osp else None


def known_open_mr_refs(state: IncidentState) -> tuple[str, ...]:
    """What jTrack last reported open, read back off the event that recorded it.

    Off the audit trail rather than out of a state field, for `outstanding_requests`' reason: the
    node that asked is the owner of the answer, and a second home for it is a second thing that can
    disagree. Only the most recent search counts -- an MR closed since the last lap is not still
    open because an earlier lap found it so.
    """
    for event in reversed(state.get("audit_events", [])):
        if event.node != SEARCH_NODE:
            continue
        found = event.detail.get("open_mr_refs")
        return tuple(str(ref) for ref in found) if isinstance(found, list) else ()
    return ()


def plant_round(state: IncidentState) -> int:
    """How many times OSP has already been asked to report. Zero before the first pass.

    Counted off `node_visits`, which `@node` writes last and refuses to let a body override, so the
    count cannot be evaded by the node it bounds. Completed passes, like `visit_round`, so the node
    asking adds one for its own round.

    This is the discriminator on the evidence ref and the audit event. Both de-duplicate on a
    derived id, so a second genuine report whose id did not move would collapse into the first and
    the trail would show one exchange with OSP where there were two.
    """
    return int(state.get("node_visits", {}).get(CAPTURE_NODE, 0))


def plant_update_idempotency_key(state: IncidentState, mr_ref: str, attempt: int) -> str:
    """The key this chase is sent under. Carries the attempt, unlike the create's.

    See the module docstring: `mr_idempotency_key` is fixed per plant object so a re-offered packet
    cannot file a second MR, and this one must move per lap or the ledger would return every chase
    after the first as `replayed` and OSP would be told once however long the MR sat.
    """
    return derive_id(
        "IDK", state.get("incident_id") or "", ActionType.UPDATE_MR.value, mr_ref, attempt
    )


def update_policy_input(
    state: IncidentState, ctx: GraphContext, *, target_ref: str, idempotency_key: str
) -> PolicyInput:
    """Everything the engine may consider about appending to this MR.

    `blast_radius` is deliberately absent; the module docstring records what was measured when it
    was supplied. The three readings that *are* supplied come from `_shared`, so the engine is asked
    about corroboration, duplicate suppression and attempt count in exactly the words every other
    branch uses.
    """
    rca = state.get("rca")
    impact = state.get("impact")
    quality = state.get("data_quality")
    source_count, age_minutes = evidence_support(state, ctx.clock.now())
    return PolicyInput(
        action_type=ActionType.UPDATE_MR,
        incident_id=state.get("incident_id") or "",
        target_ref=target_ref,
        actor_role=ctx.automation_role,
        rca_confidence=rca.confidence if rca is not None else None,
        evidence_source_count=source_count,
        evidence_age_minutes=age_minutes,
        data_quality_flags=tuple(quality.flags) if quality is not None else (),
        attempt=attempt_number(state, ActionType.UPDATE_MR),
        severity=impact.severity if impact is not None else Severity.MEDIUM,
        local_time=ctx.clock.local_now().time(),
        idempotency_key=idempotency_key,
        executed_idempotency_keys=executed_idempotency_keys(state),
    )


# ------------------------------------------------------------------------------------------------
# The question this stage asks that the specification does not number
# ------------------------------------------------------------------------------------------------


def route_plant_gate(state: IncidentState) -> Literal["chase", "no_plant_action"]:
    """Is there an MR with OSP to chase? Local, because no numbered decision asks this.

    D19 asks what the plant action *achieved*, which is a question for after this stage. This one is
    whether there is a plant action at all, and it exists because this stage is reached from every
    exit of `field_execution` that is not a Clean Boots resolution -- and two of those filed
    nothing. See the module docstring for why this reading is D19's reading rather than merely
    similar to it.
    """
    return "chase" if outstanding_plant_mr(state) is not None else "no_plant_action"


# ------------------------------------------------------------------------------------------------
# Reading what OSP sent back
# ------------------------------------------------------------------------------------------------

#: What P21 asks a Dirty crew to report, in `MRRecord` terms. `status` is the only one that is
#: required: it is what D19 reads, and a report that does not say where the MR got to has not
#: reported anything the workflow can act on.
PLANT_REPORT_FIELDS: tuple[str, ...] = (
    "status",
    "osp_owner",
    "note",
    "evidence_refs",
    "rejection_reason",
    "completed_at",
    "planned_start",
)

#: The four P21 capture items no model holds. Carried in the audit event this stage writes rather
#: than in new state fields -- `SUBMISSION_EXTRAS`' choice and its reason, that the node which
#: recorded a fact is a better owner of it than a second field that can drift.
PLANT_REPORT_EXTRAS: tuple[str, ...] = (
    "resolution_code",
    "components_changed",
    "measurements",
    "dispatch_reference",
)


def plant_report(answer: Any) -> dict[str, Any] | None:
    """OSP's report as `MRRecord` fields, or `None` for nothing usable.

    Total and `None`-returning, for `field_submission`'s reason: a resume with no payload, a timer
    tick and a garbled body all mean *we still do not know what OSP did*, and leaving the record
    where it was is a better answer than a status invented to fill the gap. D19 reads an unchanged
    `submitted` as `await_plant`, so an unusable report costs one bounded lap.

    An unrecognised status is unusable rather than coerced, which is `_requested_status`' rule at
    the adapter applied at the parser: a typo'd state that silently becomes a plausible one is how
    an MR ends up reported as progressing when nobody has touched it.

    Coercion is confined to shapes. A naive instant is dropped rather than localised, because
    `cycle_time()` subtracts it from an aware one and raises.
    """
    if not isinstance(answer, dict):
        return None
    try:
        status = MRStatus(str(answer.get("status") or "").strip().lower())
    except ValueError:
        return None

    return {
        "status": status,
        "osp_owner": str(answer.get("osp_owner") or "").strip(),
        "note": str(answer.get("note") or "").strip(),
        "rejection_reason": str(answer.get("rejection_reason") or "").strip(),
        "evidence_refs": _strings(answer.get("evidence_refs")),
        "completed_at": _instant(answer.get("completed_at")),
        "planned_start": _instant(answer.get("planned_start")),
    }


def plant_report_extras(answer: Any) -> dict[str, Any]:
    """The four unmodelled capture items, shaped for an audit event's `detail`.

    Separate from `plant_report` because the destination is different, not because the input is --
    `submission_extras`' argument. Never `None`-valued for a missing item in a report whose status
    parsed: an absent resolution code does not invalidate a crew telling us the MR is closed.
    """
    if not isinstance(answer, dict):
        return dict.fromkeys(PLANT_REPORT_EXTRAS)
    raw_measurements = answer.get("measurements")
    measurements: dict[str, float] = {}
    if isinstance(raw_measurements, dict):
        for key, value in raw_measurements.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            measurements[str(key)] = float(value)
    return {
        "resolution_code": str(answer.get("resolution_code") or "") or None,
        "components_changed": list(_strings(answer.get("components_changed"))),
        "measurements": measurements,
        "dispatch_reference": str(answer.get("dispatch_reference") or "") or None,
    }


def _strings(raw: Any) -> tuple[str, ...]:
    """A reported list as a tuple of non-empty strings. Anything else is an empty tuple."""
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def _instant(raw: Any) -> datetime | None:
    """A reported instant, or `None` for anything that is not an aware datetime."""
    value = raw if isinstance(raw, datetime) else None
    if value is None and isinstance(raw, str) and raw.strip():
        try:
            value = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    if value is None or value.tzinfo is None:
        return None
    return value


# ------------------------------------------------------------------------------------------------
# P20b -- what jTrack is holding, and what we tell it
# ------------------------------------------------------------------------------------------------


@node(SEARCH_NODE)
async def search_plant_mr(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Ask jTrack what is open against this plant object. P20's first instruction, on the way back.

    `file_plant_mr` runs the same read before filing, to suppress a duplicate. This one runs it to
    find out whether there is still anything to chase: `update_mr` raises a **non-retryable**
    `AdapterError` when jTrack is not holding the MR open, and a record read is how to know that
    without catching it. `fetch_open_mrs` is a collection query and answers `[]` rather than
    raising, so "OSP has nothing open here" and "we could not ask" are not the same outcome and
    neither is an exception.

    Writes no status. Asking a question does not move an incident, and the gate on this node's edge
    is what decides whether anything else here runs.
    """
    now = ctx.clock.now()
    record = latest_mr(state)
    if record is None:
        return {
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node=SEARCH_NODE,
                    action="fetch_open_mrs",
                    outcome="no_mr_on_record",
                    detail={
                        "open_mr_refs": [],
                        "reason": (
                            "the incident holds no MR, so there is no plant object to search "
                            "against; D19 reads this as an unrecorded plant action"
                        ),
                    },
                    discriminator=str(plant_round(state)),
                )
            ],
        }

    open_mrs = await ctx.adapters.jtrack.fetch_open_mrs(record.plant_object_ref)
    refs = [str(row.get("mr_ref") or row.get("external_ref") or "") for row in open_mrs]
    return {
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node=SEARCH_NODE,
                action="fetch_open_mrs",
                outcome="searched",
                subject_ref=record.plant_object_ref,
                detail={
                    "mr_id": record.mr_id,
                    "mr_ref": record.external_ref,
                    "status": record.status.value,
                    "awaiting_osp": record.awaiting_osp,
                    # Recorded even when empty, for `file_plant_mr`'s reason: an absent key would be
                    # indistinguishable from not having asked, and `known_open_mr_refs` reads this.
                    "open_mr_refs": refs,
                    "round": plant_round(state),
                },
                discriminator=str(plant_round(state)),
            )
        ],
    }


@node("update_plant_mr")
async def update_plant_mr(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Append our chase to the MR OSP is holding. P20's "update the existing MR when appropriate".

    Three outcomes, all of them reachable. `not_held` is jTrack not having the MR open -- which the
    simulator also produces for a thread resumed in a fresh process, since a simulated MR exists
    only if `create_mr` ran here; `not_sent` is the policy engine refusing, which the module
    docstring measures; `sent` is the append.

    **No status is requested.** `_requested_status` leaves the MR where it is when `status` is
    absent, and that is the whole intent: our note tells OSP something, it does not move their
    ticket. Asking for a status here would be this workflow deciding what OSP has done, which is the
    distinction `create_mr` draws between filing and accepting.

    `plant_attempt_count` is written absolute and only on the branch that sent, because
    `attempt_number` counts `ActionRecord.was_attempted` -- a refusal reached no adapter and must
    not look like a chase OSP ignored.
    """
    record = outstanding_plant_mr(state)
    if record is None:
        raise ValueError(
            "update_plant_mr was reached with no MR awaiting OSP. `route_plant_gate` sends that "
            "case to the end of the stage, so this edge cannot produce one."
        )

    now = ctx.clock.now()
    mr_ref = record.external_ref or record.mr_id
    target_ref = record.plant_object_ref
    attempt = attempt_number(state, ActionType.UPDATE_MR)

    if mr_ref not in known_open_mr_refs(state):
        return {
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="update_plant_mr",
                    action="update_mr",
                    outcome="not_held",
                    subject_ref=target_ref,
                    reason_code=ReasonCode.ADAPTER_UNAVAILABLE,
                    detail={
                        "mr_id": record.mr_id,
                        "mr_ref": mr_ref,
                        "status": record.status.value,
                        "open_mr_refs": list(known_open_mr_refs(state)),
                        "reason": (
                            "jTrack is not holding this MR open, so an update would be refused "
                            "non-retryably; nothing was sent and OSP is still asked to report"
                        ),
                    },
                    discriminator=f"{mr_ref}:{attempt}",
                )
            ],
        }

    idempotency_key = plant_update_idempotency_key(state, mr_ref, attempt)
    verdict = ctx.policy.evaluate(
        update_policy_input(state, ctx, target_ref=target_ref, idempotency_key=idempotency_key)
    )
    decision = verdict.model_copy(
        update={
            "decision_id": derive_id(
                "POL",
                state.get("incident_id") or "",
                ActionType.UPDATE_MR.value,
                record.mr_id,
                attempt,
                verdict.outcome.value,
            )
        }
    )
    detail: dict[str, Any] = {
        "mr_id": record.mr_id,
        "mr_ref": mr_ref,
        "status": record.status.value,
        "attempt": attempt,
        "idempotency_key": idempotency_key,
        "policy_decision_id": decision.decision_id,
        "policy_outcome": decision.outcome.value,
        "required_approval_kind": (
            decision.required_approval_kind.value
            if decision.required_approval_kind is not None
            else None
        ),
    }

    if decision.outcome is not PolicyOutcome.ALLOWED:
        return {
            "policy_decisions": [decision],
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="update_plant_mr",
                    action="update_mr",
                    outcome="not_sent",
                    subject_ref=target_ref,
                    reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                    detail={**detail, "explanation": decision.explanation},
                    discriminator=f"{mr_ref}:{attempt}",
                )
            ],
        }

    action_id = derive_id("ACT", state.get("incident_id") or "", record.mr_id, attempt)
    request = ActionRequest(
        action_id=action_id,
        incident_id=state.get("incident_id") or "",
        action_type=ActionType.UPDATE_MR,
        target_ref=target_ref,
        requested_at=now,
        idempotency_key=idempotency_key,
        actor=ctx.automation_actor,
        reason_code=ReasonCode.POLICY_ALLOWED,
        correlation_id=state.get("correlation_id") or state.get("incident_id") or "",
        policy_decision_id=decision.decision_id,
        policy_outcome=decision.outcome,
        attempt=attempt,
        parameters={
            "mr_ref": mr_ref,
            "note": (
                f"chase {attempt} from incident {state.get('incident_id')}: the MR is "
                f"{record.status.value} and the service is still degraded"
            ),
            "evidence_refs": [item.ref for item in state.get("evidence", [])],
        },
        # An MR is a conversation and `update_mr` appends rather than overwrites, so there is
        # nothing to undo: a thing said to OSP cannot be unsaid.
        reversible=False,
    )

    result = await ctx.adapters.jtrack.update_mr(request)
    completed_at = ctx.clock.now()
    outcome = ActionOutcome(str(result["outcome"]))
    action = ActionRecord(
        action_id=action_id,
        incident_id=request.incident_id,
        action_type=ActionType.UPDATE_MR,
        target_ref=target_ref,
        idempotency_key=idempotency_key,
        outcome=outcome,
        started_at=now,
        completed_at=completed_at,
        actor=ctx.automation_actor,
        reason_code=request.reason_code,
        correlation_id=request.correlation_id,
        attempt=attempt,
        simulated=bool(result.get("simulated")),
        external_ref=result.get("external_ref"),
        detail=str(result.get("detail") or ""),
        error=str(result.get("error") or ""),
    )
    return {
        "selected_action": request,
        "action_history": [action],
        # Absolute, never `state.get(...) + 1`: `plant_attempt_count` reduces with `take_max` and an
        # increment computed at entry is what that reducer exists to defeat.
        "plant_attempt_count": attempt,
        "updated_at": completed_at,
        "audit_events": [
            audit(
                state,
                ctx,
                node="update_plant_mr",
                action="update_mr",
                outcome=outcome.value,
                subject_ref=target_ref,
                reason_code=request.reason_code,
                detail={
                    **detail,
                    "action_id": action_id,
                    "simulated": bool(result.get("simulated")),
                    "replayed": bool(result.get("replayed")),
                    "gate": result.get("gate"),
                    "detail": result.get("detail"),
                },
                discriminator=action_id,
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# P21 -- the crew's own report
# ------------------------------------------------------------------------------------------------


@node(CAPTURE_NODE)
async def capture_plant_evidence(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Pause for the Dirty crew's report and record it as an `MRRecord` revision. P21.

    `interrupt()` with no adapter fallback, for `capture_field_evidence`'s measured reason:
    `JTrackAdapter` has no method that reports OSP's progress, so a fallback written against it
    would be a branch that cannot produce a report. That is gap EXEC-2.

    An unusable report records no revision at all, and D19 already knows what to do with that -- the
    MR is still `submitted`, `await_plant` routes back here and the guard bounds how often. See
    `plant_report` for why a status nobody recognises counts as unusable rather than being corrected
    into one that parses.

    The status is `awaiting_plant_repair` on both branches, including the one where the crew reports
    the work finished. The incident's status says where the *incident* is; D19 is what reads the MR
    and moves it on, and writing `validating` here would answer D19 before it was asked.
    """
    record = outstanding_plant_mr(state)
    if record is None:
        raise ValueError(
            "capture_plant_evidence was reached with no MR awaiting OSP. `route_plant_gate` sends "
            "that case to the end of the stage, so this edge cannot produce one."
        )

    round_number = plant_round(state) + 1
    answer = interrupt(
        {
            "plant_report_request": {
                "incident_id": state.get("incident_id"),
                "mr_id": record.mr_id,
                "mr_ref": record.external_ref,
                "plant_object_ref": record.plant_object_ref,
                "status": record.status.value,
                "osp_owner": record.osp_owner,
                "submitted_at": (
                    record.submitted_at.isoformat() if record.submitted_at is not None else None
                ),
                "round": round_number,
            },
            "requested_items": [*PLANT_REPORT_FIELDS, *PLANT_REPORT_EXTRAS],
        }
    )

    parsed = plant_report(answer)
    now = ctx.clock.now()
    if parsed is None:
        return {
            "status": IncidentStatus.AWAITING_PLANT_REPAIR,
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node=CAPTURE_NODE,
                    action=CAPTURE_NODE,
                    outcome="unusable_report",
                    subject_ref=record.mr_id,
                    reason_code=ReasonCode.DATA_QUALITY_INSUFFICIENT,
                    detail={
                        "mr_id": record.mr_id,
                        "round": round_number,
                        "keys": sorted(answer) if isinstance(answer, dict) else [],
                        "reason": (
                            "the report was absent, unparseable, or named a status that is not an "
                            "MRStatus; no revision was recorded and OSP will be asked again"
                        ),
                    },
                    discriminator=f"{record.mr_id}:{round_number}",
                )
            ],
        }

    status: MRStatus = parsed["status"]
    finished = status in (MRStatus.COMPLETED, MRStatus.CLOSED)
    supplied = parsed["completed_at"]
    finished_at = supplied or now
    newly_accepted = status is MRStatus.ACCEPTED and record.accepted_at is None
    extras = plant_report_extras(answer)
    note = parsed["note"] or f"OSP reported {status.value} on round {round_number}"

    # `model_copy` and not a rebuilt `MRRecord`, the trade `_close_work_order` documents: it
    # bypasses validation, so the values above must be ones the model would have accepted, but
    # rebuilding would mean re-listing eighteen fields and a field forgotten here is an
    # `idempotency_key` or a `submitted_at` silently dropped from the record of the MR.
    revision = record.model_copy(
        update={
            "status": status,
            "updated_at": now,
            "revision": record.revision + 1,
            "osp_owner": parsed["osp_owner"] or record.osp_owner,
            "rejection_reason": parsed["rejection_reason"] or record.rejection_reason,
            "planned_start": parsed["planned_start"] or record.planned_start,
            "accepted_at": now if newly_accepted else record.accepted_at,
            "completed_at": finished_at if finished else record.completed_at,
            "closed_at": finished_at if status is MRStatus.CLOSED else record.closed_at,
            "notes": [*record.notes, note],
        }
    )

    evidence = make_evidence(
        state,
        ctx,
        node=CAPTURE_NODE,
        kind=EvidenceKind.MR_UPDATE,
        subject_ref=record.plant_object_ref,
        summary=f"OSP reported MR {record.external_ref or record.mr_id} {status.value}: {note}",
        # The system of record for the MR, not the channel the words arrived through. A future OSP
        # feed would be the same fact from the same system; see EXEC-2.
        source_system="jtrack",
        observed_at=finished_at if finished else now,
        payload={
            "mr_id": record.mr_id,
            "mr_ref": record.external_ref,
            "status": status.value,
            "evidence_refs": list(parsed["evidence_refs"]),
            **extras,
        },
        discriminator=f"{record.mr_id}:{round_number}",
    )

    update: NodeUpdate = {
        "status": IncidentStatus.AWAITING_PLANT_REPAIR,
        "mr_records": [revision],
        "evidence": [evidence],
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node=CAPTURE_NODE,
                action=CAPTURE_NODE,
                outcome=status.value,
                subject_ref=record.mr_id,
                reason_code=(
                    ReasonCode.PLANT_FAULT_CONFIRMED
                    if finished
                    else ReasonCode.HANDOVER_REJECTED_INCOMPLETE
                    if status is MRStatus.REJECTED
                    else ReasonCode.STABILITY_WINDOW_PENDING
                ),
                detail={
                    "mr_id": record.mr_id,
                    "mr_ref": record.external_ref,
                    "round": round_number,
                    "status": status.value,
                    "previous_status": record.status.value,
                    "revision": revision.revision,
                    "osp_owner": revision.osp_owner,
                    "rejection_reason": revision.rejection_reason,
                    "evidence_refs": list(parsed["evidence_refs"]),
                    "evidence_ref": evidence.ref,
                    # Whether the completion instant is OSP's own or the moment they told us. The
                    # module docstring says why the difference is worth recording.
                    "completion_time_supplied": supplied is not None,
                    "completed_at": (
                        revision.completed_at.isoformat()
                        if revision.completed_at is not None
                        else None
                    ),
                    # The four items no model holds. This event is their owner.
                    **extras,
                },
                discriminator=f"{record.mr_id}:{round_number}",
            )
        ],
    }
    # `preview`, not `state`: all three KPIs read the MR revision that is still sitting unreduced in
    # `update`. `mr_cycle_time` returns nothing until an MR closes, and `emit_kpi` drops what state
    # cannot derive, so the three are emitted unconditionally rather than behind a status test that
    # would be a second, shorter version of what each calculator already decides.
    seen = preview(state, update)
    update["kpi_events"] = [
        *emit_kpi(
            seen,
            ctx,
            KPIName.PLANT_REPAIR_BACKLOG,
            node=CAPTURE_NODE,
            dimensions={"fault_domain": record.fault_domain.value},
            discriminator=f"{record.mr_id}:{round_number}",
        ),
        *emit_kpi(
            seen,
            ctx,
            KPIName.MR_REJECTION_RATE,
            node=CAPTURE_NODE,
            dimensions={"fault_domain": record.fault_domain.value},
            discriminator=f"{record.mr_id}:{round_number}",
        ),
        *emit_kpi(
            seen,
            ctx,
            KPIName.MR_CYCLE_TIME_SECONDS,
            node=CAPTURE_NODE,
            dimensions={"fault_domain": record.fault_domain.value},
            discriminator=f"{record.mr_id}:{round_number}",
        ),
    ]
    return update


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The three nodes, in the order the specification walks them. Checked the same way as
#: `PARENT_NODES`, so a node registered under a name its decorator does not carry fails on import
#: rather than producing a graph whose topology and audit trail disagree.
PLANT_EXECUTION_NODES: tuple[tuple[str, Any], ...] = (
    (SEARCH_NODE, search_plant_mr),
    ("update_plant_mr", update_plant_mr),
    (CAPTURE_NODE, capture_plant_evidence),
)

check_node_registry(PLANT_EXECUTION_NODES, "the plant-execution node registry")

#: `route_plant_gate`'s two answers. `no_plant_action` ends the stage rather than escalating, for
#: `VISIT_TARGETS`' reason: an incident that never filed an MR arrived here deliberately, and D19
#: reads the absence as `retry_diagnosis` -- which is a diagnosis to redo, not an incident to page
#: somebody about.
PLANT_TARGETS: dict[str, str] = {
    "chase": "update_plant_mr",
    "no_plant_action": END,
}


def build_plant_execution_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every onward edge is guarded, for the reason the parent's are: `escalation_update` stops a node
    from doing work but does not stop the graph, so an unguarded edge would chase an MR after the
    budget had been declared exhausted.

    The edge out of `update_plant_mr` is `straight_on` and not a router. All three of its outcomes
    -- sent, refused by policy, not held by jTrack -- lead to the same place, because the report OSP
    sends is not conditional on our having managed to chase them for it.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in PLANT_EXECUTION_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, SEARCH_NODE)

    plant_map: dict[Any, str] = {**PLANT_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(SEARCH_NODE, guarded(route_plant_gate), plant_map)

    graph.add_conditional_edges(
        "update_plant_mr", guarded(straight_on), {ONWARD: CAPTURE_NODE, ESCALATED: END}
    )
    graph.add_edge(CAPTURE_NODE, END)
    return graph


def compile_plant_execution_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission. A subgraph compiled as a node shares the
    parent's checkpointer -- LangGraph namespaces its state beneath the parent's thread -- and
    handing this one its own would give the incident two places to be resumed from.
    """
    return build_plant_execution_graph().compile(name="lpr_cpe_plant_execution")


__all__ = [
    "CAPTURE_NODE",
    "PLANT_EXECUTION_NODES",
    "PLANT_REPORT_EXTRAS",
    "PLANT_REPORT_FIELDS",
    "PLANT_TARGETS",
    "SEARCH_NODE",
    "build_plant_execution_graph",
    "capture_plant_evidence",
    "compile_plant_execution_graph",
    "known_open_mr_refs",
    "latest_mr",
    "outstanding_plant_mr",
    "plant_report",
    "plant_report_extras",
    "plant_round",
    "plant_update_idempotency_key",
    "route_plant_gate",
    "search_plant_mr",
    "update_plant_mr",
    "update_policy_input",
]
