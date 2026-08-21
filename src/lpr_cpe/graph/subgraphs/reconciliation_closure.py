"""Stage 5's second half: bring the linked systems into agreement, then close. P24-P26, D23, D24.

`builder.PENDING_STAGES` named this as what `D22:reconcile` was waiting for: "an incident now waits
out its stability window, is judged against the pre-fix readings and has the customer's word
recorded, and stops at the point where the linked records would be brought into agreement". This is
that point onwards, and it is the last stage of the lifecycle -- `IncidentStatus.CLOSED` has no
outward transition, so `builder._DELIBERATE_TERMINALS` is where this subgraph belongs rather than
`PENDING_STAGES`.

Why nine nodes
--------------
* **Reading the systems is not judging them.** `reconcile_linked_systems` writes a
  `ReconciliationResult`; D23 reads `ReconciliationResult.consistent`. The model owns the verdict,
  which is what stops the node and the router disagreeing about what "consistent" means.
* **Retrying is a node, not a loop in the reader.** `hold_for_reconciliation_retry` is the
  specification's four remedies for D23's "no" -- hold closure, record which system is
  inconsistent, retry with limits, escalate the unresolved -- and it has to be its own node because
  the limit is `retries["reconciliation"]`, which `merge_retries` reduces per key. A counter bumped
  inside the reader would be bumped on the *first* pass too.
* **Evaluating the closure is not performing it.** `ActionRequest` refuses an approval-requiring
  outcome with no `approval_ref`, so the verdict cannot be recorded by the node that closes. The
  same rule split `evaluate_handover_policy` from `file_plant_mr`.
* **Asking is two nodes.** `prepare_exceptional_closure_approval` writes the question and returns;
  `request_exceptional_closure_approval` raises it. Everything before `interrupt()` re-runs on
  resume, so a node that built *and* raised would re-stamp `requested_at` on every resume.
* **P26 is last, and has to be.** It writes `IncidentStatus.CLOSED`, and `domain.lifecycle` gives
  `closed` no outward transition -- so anything scheduled after it could not legally write a status,
  and a fired budget in `@node` would try to: `check_budgets` returns `escalation_update(...)`,
  which sets `ESCALATED`. D24 is therefore asked *before* P26, which is also the order the
  specification's own label list implies: the last of P26's sixteen labels is "Chronic fault".

The status path, measured against `domain.lifecycle.can_transition`
-------------------------------------------------------------------
    validating        -> reconciling        True
    reconciling       -> reconciling        True    (the retry loop, a legal no-op self-transition)
    reconciling       -> awaiting_approval  True
    awaiting_approval -> resolved           True
    reconciling       -> resolved           True
    resolved          -> closed             True
    validating        -> awaiting_approval  False

The last row is the load-bearing one and it decides the shape of the stage: `prepare_approval`
writes `awaiting_approval` unconditionally, and an incident arriving from `confirm_customer_outcome`
is still `validating`. So P24 must write `reconciling` before any gate can pause, which it does on
its first line. This is the same constraint that forced `evaluate_handover_policy` ahead of P18 in
`field_execution`, discovered the same way.

What P24 does *not* treat as a mismatch, and why the distinction was measured
-----------------------------------------------------------------------------
**NXT alarms are counted, never reconciled.** The simulator derives alarms from each fixture's
static `health` field, so no repair this workflow can perform clears one. Swept over all 41 fixture
services: `hfc_marginal` (1 service), `hfc_degraded_upstream` (5), `pon_degraded_optical` (1) and
`pon_power_affected` (1) each carry one alarm with `cleared_at: null`, and the other 33 carry none.
Counting a live alarm as a mismatch would hold those 8 open through every retry and then escalate
them -- the identical defect that `ReconciliationPolicy.systems` had when it named
`service_platform`, a system no adapter serves. The count goes in `notes`, where an operator can
see it.

**A record nobody could find is a note, not a disagreement.** Both `wfm.fetch_work_order` and
`jtrack.fetch_mr` return `data_available: False` with `simulated: True` for a reference they do not
hold, and `wfm`'s own docstring says why: "a miss after a restart is expected and is **not**
evidence that WFM and the workflow disagree." Treating it as one would make every restarted thread
unclosable.

**A system with nothing to compare is still checked.** A pack-named system for which this incident
holds no reference is reached through `health()` and recorded in `systems_checked` with a note.
That is honestly weaker than a record comparison and is said so; the alternative -- omitting it --
would let `systems_checked` imply a comparison that never happened.

**A pack-named system with no adapter is `systems_unreachable`, not silence.** The call map is
built from `ctx.policy.pack.reconciliation.systems` against `ctx.adapters.all_adapters()`, so a
system added to the pack without a reader here appears in the result instead of being skipped. That
is what the old `service_platform` entry did silently.

The closure policy table, measured against the shipped pack on 2026-08-20
-------------------------------------------------------------------------
With `actor_role=automation` and six evidence sources 0.5 min old:

| inputs | outcome | approval kind | reason codes |
| --- | --- | --- | --- |
| rca 0.82 | allowed | -- | `POLICY_ALLOWED` |
| rca 0.50, or absent | requires_approval | `exceptional_closure` | `RCA_LOW_CONFIDENCE` |
| rca 0.82, validation failed | requires_approval | `exceptional_closure` | `VALIDATION_FAILED` |
| rca 0.50, validation failed | requires_approval | `exceptional_closure` | both, RCA first |
| rca 0.82, not reconciled | **blocked** | -- | `RECONCILIATION_MISMATCH` |

Rows two and three used to demand different kinds, and only one of them was answerable here: a weak
RCA raised `low_confidence_rca`, which this gate does not own, so *proving* the service restored
made an incident less closable than failing to prove it for every RCA below the 0.75 bar.
`PolicyEngine._check_confidence` now raises `exceptional_closure` for `CLOSE_INCIDENT` -- see its
docstring for the measurement -- which is why every `requires_approval` row above names the one kind
this stage owns.

Three things follow. `evaluate_closure_policy` **must** pass `rca_confidence`, or every closure
takes the exceptional path rather than the ordinary one. The reason codes are what the approver's
question is built from, so row four asks about both objections in one sentence -- see
`_closure_question`. And D23 has to have answered `close` before the engine is asked at all, because
the alternative is a blocked decision, which `route_closure_gate` sends to `abandon_closure`.

Why `route_closure_gate` names its kind instead of reading the decision's
-------------------------------------------------------------------------
It is a dedicated gate, built on `field_execution.route_handover_gate` rather than on
`route_remote_gate`. `EXCEPTIONAL_CLOSURE` joins `routing.DEDICATED_GATE_APPROVAL_KINDS` with this
stage, and the deny list is read by the *variable-kind* gates: it stops `route_remote_gate` and
`route_self_help_gate` asking a question this gate owns. A gate that consulted the deny list for
its own kind would decline to ask its own question. So the kind is hardcoded here and at the
`build_request(kind=...)` call site, which is what
`test_every_gate_that_names_its_own_kind_is_listed_as_owning_it` scans for.

The consequence is that a demand for any *other* kind routes to `abandon_closure`: an unanswerable
demand belongs with a human. That arm was the common case until the engine stopped raising
`low_confidence_rca` here, and is now a defence rather than a live path. Swept over 32,256
combinations of the inputs `evaluate_closure_policy` varies -- rca, validation, reconciliation,
blast radius, severity, actor role, attempt and one data-quality flag -- against the shipped pack on
2026-08-20: 24,192 blocked, 6,912 demanded `exceptional_closure`, 1,152 were allowed outright, and
**nothing** demanded a kind this gate does not own. The arm stays because the pack is data: a
`close_incident` rule that named an `approval_kind`, or a future check raising one, would reach it,
and abandoning is the right answer when it does.

There is deliberately no `approval_outstanding` clause, for `route_handover_gate`'s measured reason:
`interrupt()` means the pause *is* the wait, so a router asking "is an answer outstanding?" would
be asking about a super-step that cannot have happened.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.closure import ClosureRecord, ReconciliationResult
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    DataQualityFlag,
    FaultDomain,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
    Severity,
)
from lpr_cpe.domain.governance import ActionRecord, ActionRequest, PolicyDecision
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.interrupts import build_request, prepare_approval, request_approval
from lpr_cpe.graph.nodes._runtime import (
    Gathered,
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    emit_kpi,
    node,
    preview,
)
from lpr_cpe.graph.nodes.closure import customer_verdict
from lpr_cpe.graph.nodes.evidence import Subject, subject_of
from lpr_cpe.graph.routing import (
    PRIOR_INCIDENTS_KEY,
    latest_decision_of,
    latest_field_finding,
    latest_policy_decision,
    route_chronic_pattern,
    route_reconciliation,
)
from lpr_cpe.graph.state import (
    IncidentState,
    current_mr_records,
    current_work_orders,
    truck_roll_count,
)
from lpr_cpe.graph.subgraphs._shared import attempt_number, evidence_support
from lpr_cpe.observability.kpi import KPICalculator, KPIValue, MetricTimestamp, mark
from lpr_cpe.policies.engine import PolicyInput

#: The `retries` key this stage bounds itself with. Named once so the node that bumps it and the
#: node that reads the ceiling cannot drift apart, and spelled after the pack section that supplies
#: the ceiling -- `ReconciliationPolicy.max_retries`.
RETRY_KEY = "reconciliation"

#: `linked_records` key for the TMF656 service problem D24 opens. Already one of
#: `routing.PARENT_RECORD_KEYS`, which is what a later incident correlates against.
SERVICE_PROBLEM_KEY = "service_problem"


# ------------------------------------------------------------------------------------------------
# P24: reading each system of record
# ------------------------------------------------------------------------------------------------


def _mismatch(
    system: str, record: str, ours: object, theirs: object, detail: str
) -> dict[str, Any]:
    """One disagreement, in the shape `ReconciliationResult.mismatches` carries.

    `ours` and `theirs` are both recorded because a reconciliation report that says only "these
    disagree" leaves an operator to re-run both reads by hand to find out how.
    """
    return {"system": system, "record": record, "ours": ours, "theirs": theirs, "detail": detail}


def _unfound(system: str, payload: Mapping[str, Any]) -> bool:
    """Whether this payload is the adapter saying it does not hold the reference we asked about.

    Distinguished from a disagreement because the simulators say it is: both `wfm.fetch_work_order`
    and `jtrack.fetch_mr` return `found: False` with `simulated: True` for an unknown reference,
    and WFM's docstring records that "a miss after a restart is expected and is **not** evidence
    that WFM and the workflow disagree." The `simulated` half of the test is what keeps this
    conservative: a *real* system reporting a record missing is a genuine mismatch, and this clause
    stops applying the moment the adapter behind it is not a fixture.
    """
    del system
    return payload.get("found") is False and bool(payload.get("simulated"))


def reconcile_nxt(state: IncidentState, payload: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Count the open alarms. Never a mismatch -- see the module docstring for the measurement."""
    del state
    rows = payload if isinstance(payload, list) else []
    live = [row for row in rows if isinstance(row, Mapping) and row.get("cleared_at") is None]
    return [], [
        f"nxt: {len(live)} uncleared alarm(s) of {len(rows)} raised since the incident opened; "
        "recorded for the operator, not reconciled -- an alarm is cleared by the network, not by us"
    ]


def reconcile_tmf(state: IncidentState, payload: object) -> tuple[list[dict[str, Any]], list[str]]:
    """The customer-facing service record: does it still describe the customer we closed for?"""
    if not isinstance(payload, Mapping):
        return [
            _mismatch("tmf", "service", state.get("service_ref"), None, "no record returned")
        ], []
    if payload.get("data_available") is False:
        return [
            _mismatch(
                "tmf",
                "service",
                state.get("service_ref"),
                None,
                "the service record could not be read: "
                + "; ".join(
                    str(n) for n in payload.get("data_quality_notes") or ["no reason given"]
                ),
            )
        ], []
    ours = state.get("customer_ref")
    theirs = payload.get("customer_ref")
    if ours and theirs and str(ours) != str(theirs):
        return [
            _mismatch(
                "tmf",
                "service",
                ours,
                theirs,
                "the customer on the service record is not the customer on the incident",
            )
        ], []
    return [], [f"tmf: service {payload.get('service_ref')} is {payload.get('state')}"]


def reconcile_wfm(state: IncidentState, payload: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Our work order against the WFM's. `WorkOrder.terminal` is the one owner of "finished".

    The comparison is deliberately one-directional -- open there while finished here -- because
    that is the failure closure exists to prevent: a live work order behind a closed incident sends
    a crew to a fault nobody is expecting them at. The inverse (finished there, live here) cannot
    arise from this workflow, which is the only writer of `work_orders`, and asserting it would be
    a guard on a state nothing can produce.
    """
    if not isinstance(payload, Mapping):
        return [], []
    ref = str(payload.get("work_order_ref") or "")
    if _unfound("wfm", payload):
        return [], [f"wfm: no record of work order {ref}; not treated as a disagreement"]
    ours = current_work_orders(state).get(ref)
    theirs = str(payload.get("status") or "")
    if ours is None:
        return [], [f"wfm: {ref} is {theirs} there and unknown here"]
    if ours.terminal and theirs not in {"cancelled", "completed"}:
        return [
            _mismatch(
                "wfm",
                "work_order",
                ours.status.value,
                theirs,
                f"work order {ref} is finished here and still {theirs} in the WFM",
            )
        ], []
    return [], [f"wfm: work order {ref} is {theirs}"]


def reconcile_jtrack(
    state: IncidentState, payload: object
) -> tuple[list[dict[str, Any]], list[str]]:
    """Our MR against jTrack's. `open` and `awaiting_acceptance` are the adapter's own derivations.

    Both flags are read rather than re-derived from the status string. The simulator's comment on
    `awaiting_acceptance` says why: "naming it here saves every caller re-deriving it and getting
    the boundary wrong." `MRRecord.terminal` is the matching owner on our side.
    """
    if not isinstance(payload, Mapping):
        return [], []
    ref = str(payload.get("mr_ref") or "")
    if _unfound("jtrack", payload):
        return [], [f"jtrack: no record of MR {ref}; not treated as a disagreement"]
    ours = next(
        (mr for mr in current_mr_records(state).values() if ref in {mr.external_ref, mr.mr_id}),
        None,
    )
    theirs = str(payload.get("status") or "")
    if ours is None:
        return [], [f"jtrack: MR {ref} is {theirs} there and unknown here"]
    if ours.terminal and bool(payload.get("open")):
        return [
            _mismatch(
                "jtrack",
                "mr",
                ours.status.value,
                theirs,
                f"MR {ref} is finished here and still open in jTrack",
            )
        ], []
    note = f"jtrack: MR {ref} is {theirs}"
    if payload.get("awaiting_acceptance"):
        note += " and has not been accepted by OSP"
    return [], [note]


def reconcile_communications(
    state: IncidentState, payload: object
) -> tuple[list[dict[str, Any]], list[str]]:
    """The customer's last word. A denial is a mismatch with the incident we are about to close.

    `customer_verdict` is imported from `graph.nodes.closure` rather than re-implemented: P23 reads
    the same rows through it, and two readers of one channel with two vocabularies is how a customer
    comes to have said different things to two parts of the same workflow.

    `is False` and not falsiness, for `route_resolution`'s reason: `None` is a customer nobody
    needed to ask, and counting that as a denial would make every proactive incident inconsistent.
    """
    del state
    rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
    verdict = customer_verdict(rows)
    if verdict is False:
        return [
            _mismatch(
                "communications",
                "customer_response",
                "restored",
                "customer says the service is still not working",
                "the customer's latest reply denies the restoration this closure asserts",
            )
        ], []
    return [], [f"communications: {len(rows)} reply/replies read, verdict {verdict}"]


def reconcile_inventory(
    state: IncidentState, payload: object
) -> tuple[list[dict[str, Any]], list[str]]:
    """The plant object the fault was delimited to. Read to confirm it still exists and is readable.

    No field of ours is compared against it. This workflow never writes to inventory -- nothing in
    `src` calls `update_plant_object` -- so there is no value here that could have diverged, and
    inventing a comparison would be asserting agreement about a fact only one side holds. What the
    read establishes is that the reference the MR and the closure record both carry resolves.
    """
    del state
    if not isinstance(payload, Mapping):
        return [], []
    ref = str(payload.get("object_ref") or "")
    if payload.get("data_available") is False:
        return [], [f"inventory: {ref} is not in the plant inventory; recorded, not reconciled"]
    return [], [f"inventory: {ref} resolves as {payload.get('object_kind')}"]


#: Pack system name -> the pure reader for its payload. The pack is the one owner of *which*
#: systems are reconciled; this table is the one owner of *how* each is read. A name in the pack
#: with no entry here lands in `systems_unreachable` rather than being skipped, which is what the
#: removed `service_platform` entry used to be able to do silently.
RECONCILERS: Mapping[str, Any] = {
    "nxt": reconcile_nxt,
    "tmf": reconcile_tmf,
    "wfm": reconcile_wfm,
    "jtrack": reconcile_jtrack,
    "communications": reconcile_communications,
    "inventory": reconcile_inventory,
}


def _reads_for(
    state: IncidentState, ctx: GraphContext, subject: Subject, since: datetime
) -> tuple[dict[str, Awaitable[Any]], dict[str, Awaitable[bool]], list[str]]:
    """The record read per system, the health check where there is no record, and the unservable.

    Three buckets rather than one because they mean three different things at D23. A record read
    can disagree. A health check can only say the system answered. A system the pack names and this
    process cannot reach at all has not been checked, and `ReconciliationResult.consistent` counts
    an unreachable system as inconsistent -- which is the model's decision and the right one.
    """
    linked = state.get("linked_records", {})
    adapters = ctx.adapters.all_adapters()
    reads: dict[str, Awaitable[Any]] = {}
    probes: dict[str, Awaitable[bool]] = {}
    unservable: list[str] = []

    for name in ctx.policy.pack.reconciliation.systems:
        if name not in RECONCILERS or name not in adapters:
            unservable.append(name)
            continue
        if name == "nxt":
            reads[name] = ctx.adapters.nxt.fetch_alarms(
                since=since, service_ref=subject.service_ref
            )
        elif name == "tmf" and subject.service_ref:
            reads[name] = ctx.adapters.tmf.fetch_service(subject.service_ref)
        elif name == "wfm" and linked.get("work_order"):
            reads[name] = ctx.adapters.wfm.fetch_work_order(linked["work_order"])
        elif name == "jtrack" and linked.get("mr"):
            reads[name] = ctx.adapters.jtrack.fetch_mr(linked["mr"])
        elif name == "communications":
            reads[name] = ctx.adapters.communications.fetch_customer_responses(subject.incident_id)
        elif name == "inventory" and subject.delimiter_ref:
            reads[name] = ctx.adapters.inventory.fetch_plant_object(subject.delimiter_ref)
        else:
            probes[name] = adapters[name].health()
    return reads, probes, unservable


@node("reconcile_linked_systems")
async def reconcile_linked_systems(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P24. Read every system the pack names and record where they disagree with us.

    `Gathered` without a `freshness`, which every other caller supplies. Freshness bounds
    *telemetry* -- `policy.evidence.max_telemetry_age_minutes` and its siblings -- and these are
    record reads. A work order does not go stale; it is either the status we hold or it is not, and
    passing a freshness here would raise `STALE_DATA` against a ticket that had simply not been
    touched recently.

    `data_quality` is written rather than left alone, and that is safe because
    `Gathered.assessment(previous=...)` **folds**: flags and notes union, the completeness score is
    the lower of the two. An adapter that could not answer here therefore shows up in the same
    assessment P03, P05 and P07 built, which is what `policy_block_rate` and
    `data_quality_defect_rate` both read.

    `reconciliation_id` is discriminated by the retry attempt. `reconciliation` is a plain field
    with last-write-wins so the id does not have to move for the state to be right -- but the audit
    event does, `audit` keys on the node and `audit_events` reduces first-write-wins, and a second
    pass whose id had not moved would leave no trace that a retry happened at all.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    attempt = int(state.get("retries", {}).get(RETRY_KEY, 0))
    since = state.get("created_at") or (now - timedelta(days=1))

    reads, probes, unservable = _reads_for(state, ctx, subject, since)
    gathered = Gathered(ctx, assessed_at=now)
    payloads = await gathered.gather(reads)
    reachable = {name: await probe for name, probe in probes.items()}

    mismatches: list[dict[str, Any]] = []
    notes: list[str] = []
    unreachable: list[str] = sorted(unservable)
    for name in unservable:
        gathered.add_flag(
            DataQualityFlag.ADAPTER_UNAVAILABLE,
            f"{name}: named by policy.reconciliation.systems and served by no adapter here",
        )

    for name in sorted(reads):
        if name not in payloads:
            unreachable.append(name)
            continue
        found, said = RECONCILERS[name](state, payloads[name])
        mismatches.extend(found)
        notes.extend(said)

    for name in sorted(probes):
        if reachable[name]:
            notes.append(f"{name}: reachable, but this incident holds no record to compare")
        else:
            unreachable.append(name)

    result = ReconciliationResult(
        reconciliation_id=derive_id("REC", subject.incident_id, attempt),
        incident_id=subject.incident_id,
        reconciled_at=now,
        systems_checked=sorted(set(reads) | set(probes)),
        systems_unreachable=sorted(set(unreachable)),
        mismatches=mismatches,
        inventory_updates_applied=[],
        records_linked=dict(state.get("linked_records", {})),
        notes=notes,
    )

    return {
        "status": IncidentStatus.RECONCILING,
        "reconciliation": result,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="reconcile_linked_systems",
                action="reconcile_linked_systems",
                outcome="consistent" if result.consistent else "inconsistent",
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "reconciliation_id": result.reconciliation_id,
                    "attempt": attempt,
                    "systems_requested": list(ctx.policy.pack.reconciliation.systems),
                    "systems_checked": result.systems_checked,
                    "systems_unreachable": result.systems_unreachable,
                    "systems_without_a_reader": sorted(unservable),
                    "mismatches": mismatches,
                    "notes": notes,
                },
                discriminator=attempt,
            )
        ],
    }


@node("hold_for_reconciliation_retry")
async def hold_for_reconciliation_retry(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """D23's "no": hold closure, record who disagrees, retry within the pack's limit, then escalate.

    The counter is written as `read + 1` and not as an absolute. `retries` reduces with
    `merge_retries`, which takes the per-key `max`, so a replayed super-step that computes the same
    `read + 1` twice leaves the value where it was -- which is the property an absolute count would
    also have, but only by accident of nothing else writing the key.

    **It cannot sleep, and does not pretend to.** `ReconciliationPolicy.backoff_for(attempt)` gives
    30/120/600 seconds for the shipped pack; a node that awaited it would hold a graph worker for
    ten minutes and spend the step budget on wall-clock time. The delay is recorded in the audit
    detail as what an orchestrator should wait, and the graph re-reads immediately. That is a real
    difference from the specification's "retry with limits" and is written down rather than hidden:
    the *limit* is enforced, the *backoff* is advertised.

    Escalation is `escalated=True` **and** `status=ESCALATED` together. `guarded()` reads
    `state["escalated"]` and nothing else, so the flag is what stops the loop; the status is what an
    operator reads. Writing only one of them is how an incident comes to be escalated in the trace
    and running in the graph.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    result = state.get("reconciliation")
    policy = ctx.policy.pack.reconciliation
    attempt = int(state.get("retries", {}).get(RETRY_KEY, 0)) + 1
    spent = attempt >= policy.max_retries
    escalating = spent and policy.escalate_on_persistent_mismatch

    disagreeing = sorted(
        {str(m.get("system")) for m in (result.mismatches if result is not None else [])}
        | set(result.systems_unreachable if result is not None else [])
    )

    update: NodeUpdate = {
        "retries": {RETRY_KEY: attempt},
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="hold_for_reconciliation_retry",
                action="hold_closure_for_reconciliation",
                outcome="escalated" if escalating else "retrying",
                subject_ref=subject.service_ref or subject.incident_id,
                reason_code=ReasonCode.RECONCILIATION_MISMATCH,
                detail={
                    "attempt": attempt,
                    "max_retries": policy.max_retries,
                    "inconsistent_systems": disagreeing,
                    "mismatches": list(result.mismatches) if result is not None else [],
                    # What an orchestrator should wait before re-running the thread. This node
                    # returns immediately; see the docstring.
                    "recommended_backoff_seconds": policy.backoff_for(attempt),
                    "reconciliation_id": result.reconciliation_id if result is not None else None,
                },
                discriminator=attempt,
            )
        ],
    }
    if escalating:
        update["escalated"] = True
        update["status"] = IncidentStatus.ESCALATED
        update["escalation_reason"] = (
            f"reconciliation did not converge after {attempt} attempts; "
            f"{', '.join(disagreeing) or 'no system'} still disagrees with the incident"
        )
    return update


# ------------------------------------------------------------------------------------------------
# P25: authorising and performing the closure
# ------------------------------------------------------------------------------------------------


@node("evaluate_closure_policy")
async def evaluate_closure_policy(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P25a. Put the closure to the policy engine. Writes no status -- it decides nothing itself.

    A `PolicyInput` built here rather than through `_shared.policy_input_for`, which takes a
    `ResolutionOption`. Nothing is being resolved at closure, and synthesising an option to satisfy
    that signature would put a fabricated option in `resolution_plan` -- the same objection
    `graph.nodes.closure` records against synthesising one to contact a customer.

    `rca_confidence` is supplied and must be. Measured against the shipped pack: omitted, every
    closure comes back `requires_approval` on `RCA_LOW_CONFIDENCE`, so the unattended `allowed` arm
    would be unreachable for every incident and each one would stop for a supervisor's signature.
    Until `_check_confidence` learned to raise `exceptional_closure` here the symptom was worse --
    the demand named `low_confidence_rca`, which this stage's gate does not own, and every closure
    was abandoned rather than asked.

    `validation_passed` is `validation.passed` and not `True`. That is the input that decides
    between the two closure codes, and hardcoding it would make `_check_closure`'s
    `EXCEPTIONAL_CLOSURE` finding unreachable and `ClosureRecord`'s second validator undemonstrable.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    validation = state.get("validation")
    reconciliation = state.get("reconciliation")
    rca = state.get("rca")
    source_count, age = evidence_support(state, now)
    quality = state.get("data_quality")
    impact = state.get("impact")

    decision = ctx.policy.evaluate(
        PolicyInput(
            action_type=ActionType.CLOSE_INCIDENT,
            incident_id=subject.incident_id,
            target_ref=subject.service_ref or subject.incident_id,
            actor_role=ctx.automation_role,
            rca_confidence=rca.confidence if rca is not None else None,
            evidence_source_count=source_count,
            evidence_age_minutes=age,
            data_quality_flags=tuple(quality.flags) if quality is not None else (),
            attempt=attempt_number(state, ActionType.CLOSE_INCIDENT),
            severity=impact.severity if impact is not None else Severity.MEDIUM,
            validation_passed=validation.passed if validation is not None else None,
            reconciled=reconciliation.consistent if reconciliation is not None else None,
        )
    )

    return {
        "policy_decisions": [decision],
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="evaluate_closure_policy",
                action="evaluate_closure_policy",
                outcome=decision.outcome.value,
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "decision_id": decision.decision_id,
                    "required_approval_kind": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind is not None
                        else None
                    ),
                    "reason_codes": [code.value for code in decision.reason_codes],
                    "explanation": decision.explanation,
                    "validation_passed": validation.passed if validation is not None else None,
                    "reconciled": (
                        reconciliation.consistent if reconciliation is not None else None
                    ),
                    "rca_confidence": rca.confidence if rca is not None else None,
                },
            )
        ],
    }


def route_closure_gate(state: IncidentState) -> Literal["approve", "close", "abandon"]:
    """The exceptional-closure gate. Shaped on `field_execution.route_handover_gate`.

    Every arm and why it is where it is:

    * no decision, or a blocked one -> `abandon`. `_check_closure` blocks on
      `RECONCILIATION_MISMATCH` and `_check_evidence` on stale evidence; neither is a question a
      human can be asked, so both go to a human wholesale.
    * an outcome that is not `requires_approval` -> `close`. `allowed` is the ordinary path.
    * a demand for a kind this gate does not own -> `abandon`. No shipped-pack input reaches this
      any more -- see the module docstring's sweep -- but the kind is data, and nothing here could
      answer a question belonging to a gate in an earlier stage.
    * a demand for `exceptional_closure` with no answer yet -> `approve`, which asks.
    * an answer -> `close` or `abandon` by its status.
    """
    decision = latest_policy_decision(state, ActionType.CLOSE_INCIDENT)
    if decision is None or decision.blocked:
        return "abandon"
    if decision.outcome is not PolicyOutcome.REQUIRES_APPROVAL:
        return "close"
    if decision.required_approval_kind is not ApprovalKind.EXCEPTIONAL_CLOSURE:
        return "abandon"
    answer = latest_decision_of(state, ApprovalKind.EXCEPTIONAL_CLOSURE)
    if answer is None:
        return "approve"
    return "close" if answer.status is ApprovalStatus.APPROVED else "abandon"


#: What a signature on the exceptional path is accepting, one clause per reason the engine demanded
#: it. Every reason code that can carry `EXCEPTIONAL_CLOSURE` needs an entry here, which
#: `test_every_exceptional_closure_reason_has_a_question_clause` is what keeps true.
_CLOSURE_CONCERNS = {
    ReasonCode.RCA_LOW_CONFIDENCE: "the root cause behind it is below the confidence bar",
    ReasonCode.RCA_CONFLICTING_EVIDENCE: "its two leading root causes are too close to rank",
    ReasonCode.VALIDATION_FAILED: "the service has not been shown restored",
}


def _closure_question(incident_id: str, decision: PolicyDecision) -> str:
    """The question, built from the objections the engine actually raised.

    This was a fixed sentence naming a failed validation, which became a lie the moment
    `_check_confidence` learned to raise `EXCEPTIONAL_CLOSURE` for a weak root cause: the commonest
    exceptional closure is now one where the validation *passed* and the diagnosis was thin, and the
    approver was being told the opposite of what the decision said.

    Read off `decision.reason_codes` rather than re-derived from state. The approver is answering
    one specific evaluation, and re-reading `validation.passed` here would let the question describe
    a later reading than the one being signed for. The codes keep the order the checks ran in, which
    `PolicyEngine._decision` preserves deliberately as the decision's own narrative, so two
    identical decisions produce the same sentence.
    """
    concerns = [
        _CLOSURE_CONCERNS[code] for code in decision.reason_codes if code in _CLOSURE_CONCERNS
    ]
    return (
        f"Approve closing {incident_id} on the exceptional path? "
        f"The linked records reconcile, but {' and '.join(concerns)}."
    )


@node("prepare_exceptional_closure_approval")
async def prepare_exceptional_closure_approval(
    state: IncidentState, ctx: GraphContext
) -> NodeUpdate:
    """Write the question down, then return. The first half of `graph.interrupts`' pair.

    `ApprovalKind.EXCEPTIONAL_CLOSURE` is named directly rather than read from
    `decision.required_approval_kind`, and the two are equivalent rather than the first being a
    defence: `route_closure_gate` returns `abandon` unless the demanded kind *is*
    `EXCEPTIONAL_CLOSURE`, so this node cannot be entered under any other. Measured by swapping the
    literal for the decision's field, which changed nothing. What naming it buys is that the kind
    asked here and the kind `route_closure_gate` later looks for with
    `latest_decision_of(state, ApprovalKind.EXCEPTIONAL_CLOSURE)` are the same token, so no future
    edit to the engine can make the question unanswerable without the gate changing too. The
    engine's own demand is recorded in the audit detail either way.

    `reversible=False`. A closed incident is reopened as a *new linked incident* --
    `ClosurePolicy.reopen_creates_linked_incident` is `True` in the shipped pack -- so the thing the
    approver is authorising is not undone by an undo.
    """
    decision = latest_policy_decision(state, ActionType.CLOSE_INCIDENT)
    if decision is None:
        raise ValueError(
            "prepare_exceptional_closure_approval was reached with no CLOSE_INCIDENT decision. "
            "`route_closure_gate` reaches here only from a decision demanding "
            "`exceptional_closure`, which cannot exist without one."
        )

    subject = subject_of(state)
    validation = state.get("validation")
    attempt = attempt_number(state, ActionType.CLOSE_INCIDENT)
    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.EXCEPTIONAL_CLOSURE,
        question=_closure_question(subject.incident_id, decision),
        attempt=attempt,
        action_type=ActionType.CLOSE_INCIDENT,
        target_ref=subject.service_ref or subject.incident_id,
        recommendation="close as exceptional, recording which precondition was waived and why",
        risk_summary=decision.explanation,
        reversible=False,
        policy_decision_id=decision.decision_id,
        context={
            "validation_id": validation.validation_id if validation is not None else None,
            "validation_passed": validation.passed if validation is not None else None,
            "validation_summary": validation.summary if validation is not None else "",
            "reconciliation": _reconciliation_context(state),
            "truck_rolls": truck_roll_count(state),
            "remote_attempts": state.get("remote_attempt_count", 0),
            "mr_count": state.get("mr_attempt_count", 0),
            "policy_reason_codes": [code.value for code in decision.reason_codes],
            "matched_rule": decision.matched_rule,
            "policy_version": decision.policy_version,
        },
    )
    return {
        **prepare_approval(state, ctx, request),
        **mark(MetricTimestamp.APPROVAL_REQUESTED_AT, request.requested_at),
        "audit_events": [
            audit(
                state,
                ctx,
                node="prepare_exceptional_closure_approval",
                action="request_exceptional_closure_approval",
                outcome="prepared",
                subject_ref=request.target_ref,
                detail={
                    "approval_id": request.approval_id,
                    "kind": ApprovalKind.EXCEPTIONAL_CLOSURE.value,
                    "policy_demanded_kind": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind is not None
                        else None
                    ),
                    "attempt": attempt,
                },
                discriminator=attempt,
            )
        ],
    }


def _reconciliation_context(state: IncidentState) -> dict[str, Any]:
    """What the approver needs to see about P24's result, without the whole payload dump."""
    result = state.get("reconciliation")
    if result is None:
        return {}
    return {
        "reconciliation_id": result.reconciliation_id,
        "systems_checked": list(result.systems_checked),
        "systems_unreachable": list(result.systems_unreachable),
        "mismatches": list(result.mismatches),
    }


@node("request_exceptional_closure_approval")
async def request_exceptional_closure_approval(
    state: IncidentState, ctx: GraphContext
) -> NodeUpdate:
    """Raise the interrupt and record whatever comes back. The thin half of the pair.

    Nothing is built here on purpose: everything above `interrupt()` re-runs on resume, so a node
    that also composed the question would re-stamp `requested_at` every time a supervisor looked at
    it.
    """
    return request_approval(state, ctx)


@node("abandon_closure")
async def abandon_closure(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Every closure this stage may not perform. A human's incident from here.

    Three quite different situations land here and the audit event separates them, because "the
    policy blocked it", "a human said no" and "the engine wants an approval nobody on this path can
    give" call for three different responses and one outcome string would erase the difference.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    decision = latest_policy_decision(state, ActionType.CLOSE_INCIDENT)
    answer = latest_decision_of(state, ApprovalKind.EXCEPTIONAL_CLOSURE)
    demanded = decision.required_approval_kind if decision is not None else None

    if decision is None or decision.blocked:
        outcome, reason = "blocked", "the closure policy blocked this closure"
    elif answer is not None and answer.status is not ApprovalStatus.APPROVED:
        outcome, reason = "rejected", "the exceptional closure was not approved"
    else:
        outcome, reason = (
            "unanswerable",
            f"the closure policy demands {demanded.value if demanded else 'an approval'}, "
            "which no gate on the closure path owns",
        )

    return {
        "status": IncidentStatus.ESCALATED,
        "escalated": True,
        "escalation_reason": reason,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_closure",
                action="abandon_closure",
                outcome=outcome,
                subject_ref=subject.service_ref or subject.incident_id,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "reason": reason,
                    "decision_id": decision.decision_id if decision is not None else None,
                    "required_approval_kind": demanded.value if demanded is not None else None,
                    "approval_status": answer.status.value if answer is not None else None,
                    "reason_codes": (
                        [code.value for code in decision.reason_codes]
                        if decision is not None
                        else []
                    ),
                },
            )
        ],
    }


@node("close_linked_records")
async def close_linked_records(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P25b. Build the closure record. `resolved`, not `closed` -- P26 writes that.

    Splitting the two statuses across two nodes is not bookkeeping. `closed` has no outward
    transition, and `@node` calls `check_budgets` on entry: a fired budget returns
    `escalation_update(...)`, which writes `ESCALATED`. Any node after the one that writes `closed`
    would therefore be one exhausted budget away from an illegal transition.

    `time_to_restore` and `sla_met` are both derived from `KPICalculator`, which already owns the
    first: it ends at `validation.validated_at` rather than at closure, because "closure can lag
    restoration by the whole stability window plus a reconciliation pass, and reporting that as
    restoration time makes the KPI a measure of our paperwork rather than of the customer's
    outage." Re-deriving either here would give one number two owners that could disagree, which is
    exactly the defect `route_chronic_pattern`'s docstring records about reading
    `ClosureRecord.truck_rolls` instead of `truck_roll_count`.

    `sla_met` is left `None` when there is no SLA context or no restoration time. `False` would
    assert a breach that was never measured, and `ClosureRecord` keeps the three apart.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    validation = state.get("validation")
    reconciliation = state.get("reconciliation")
    rca = state.get("rca")
    decision = latest_policy_decision(state, ActionType.CLOSE_INCIDENT)
    answer = latest_decision_of(state, ApprovalKind.EXCEPTIONAL_CLOSURE)
    exceptional = decision is not None and decision.outcome is PolicyOutcome.REQUIRES_APPROVAL

    calculator = KPICalculator(ctx.clock)
    restored = calculator.time_to_restore(state)
    time_to_restore = timedelta(seconds=restored.value) if restored is not None else None
    contacts = calculator.customer_contacts_per_incident(state)

    sla = state.get("sla")
    sla_met: bool | None = None
    if sla is not None and time_to_restore is not None:
        sla_met = sla.clock_started_at + time_to_restore <= sla.restore_deadline()

    record = ClosureRecord(
        closure_id=derive_id("CLS", subject.incident_id),
        incident_id=subject.incident_id,
        closed_at=now,
        closure_code=ReasonCode.CLOSED_EXCEPTIONAL if exceptional else ReasonCode.CLOSED_NORMAL,
        fault_domain=state.get("fault_domain", FaultDomain.UNKNOWN),
        root_cause_summary=rca.summary if rca is not None else "",
        resolution_summary=_resolution_summary(state),
        validation=validation,
        reconciliation_id=(
            reconciliation.reconciliation_id if reconciliation is not None else None
        ),
        approval_ref=answer.approval_ref if answer is not None else None,
        exceptional_reason=(decision.explanation if exceptional and decision is not None else ""),
        truck_rolls=truck_roll_count(state),
        remote_attempts=state.get("remote_attempt_count", 0),
        field_visits=state.get("field_visit_count", 0),
        mr_count=state.get("mr_attempt_count", 0),
        customer_contacts=int(contacts.value) if contacts is not None else 0,
        time_to_restore=time_to_restore,
        sla_met=sla_met,
        linked_records=dict(state.get("linked_records", {})),
        closed_by=answer.decided_by if answer is not None else ctx.automation_actor,
    )

    return {
        "status": IncidentStatus.RESOLVED,
        "closure": record,
        **mark(MetricTimestamp.CLOSED_AT, now),
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="close_linked_records",
                action="close_linked_records",
                outcome=record.closure_code.value,
                subject_ref=subject.service_ref or subject.incident_id,
                reason_code=record.closure_code,
                detail={
                    "closure_id": record.closure_id,
                    "validated": record.validated,
                    "first_time_fix": record.first_time_fix,
                    "truck_rolls": record.truck_rolls,
                    "remote_attempts": record.remote_attempts,
                    "field_visits": record.field_visits,
                    "mr_count": record.mr_count,
                    "customer_contacts": record.customer_contacts,
                    "time_to_restore_seconds": (
                        time_to_restore.total_seconds() if time_to_restore is not None else None
                    ),
                    "sla_met": sla_met,
                    "approval_ref": record.approval_ref,
                    "linked_records": record.linked_records,
                },
            )
        ],
    }


def _resolution_summary(state: IncidentState) -> str:
    """One line for `ClosureRecord.resolution_summary`, which has `min_length=1`.

    Composed from what actually happened rather than from the RCA: the root cause has its own field
    on the record, and a summary that repeated it would leave nowhere to say how the fault was
    dealt with. Never empty -- the fallback names the absence, because a validator that refuses an
    empty string is refusing it at closure, where there is nothing left to go back and fill in.
    """
    lanes: list[str] = []
    if any(a.fixed_it for a in state.get("remote_actions", [])):
        lanes.append("a verified remote action")
    session = state.get("self_help_session")
    if session is not None:
        lanes.append(f"guided self-help ({session.outcome.value})")
    rolls = truck_roll_count(state)
    if rolls:
        lanes.append(f"{rolls} field visit(s)")
    if state.get("mr_attempt_count", 0):
        lanes.append(f"{state.get('mr_attempt_count', 0)} plant maintenance request(s)")
    if not lanes:
        return "closed with no recorded resolution action"
    return "resolved by " + ", ".join(lanes)


# ------------------------------------------------------------------------------------------------
# D24 and P26
# ------------------------------------------------------------------------------------------------


@node("record_chronic_pattern")
async def record_chronic_pattern(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """D24's "yes": open or update the TMF656 service problem, so the recurrence is not lost.

    "Do not hide chronic problems by treating every recurrence as isolated" is the specification's
    instruction, and `upsert_service_problem` is the method for it -- upsert rather than create,
    keyed on the idempotency key, so a second incident on the same service updates one problem
    record instead of opening a sibling. This is that method's first caller in `src`.

    The idempotency key is the **service**, not the incident. That is the whole point: two
    recurrences on one service must collapse onto one problem record, and keying on the incident
    would open one per recurrence, which is the behaviour the specification forbids by name.

    **It goes through the policy engine, and may well be refused.** Measured against the shipped
    pack with `actor_role=automation`: `CREATE_PM_CASE` is `allowed` at an RCA confidence of 0.75 or
    better and `requires_approval` naming `low_confidence_rca` below it, including when the
    confidence is absent -- because `_check_confidence` raises `RCA_LOW_CONFIDENCE` on a missing
    confidence regardless of the bar, and `_DECISION_CLASS[CREATE_PM_CASE]` is `"diagnosis"`, for
    which `RCAPolicy.minimum_for` has no entry and falls through to the strictest bar in the pack.
    The demand is recorded and nothing is sent: `ActionRequest` refuses `REQUIRES_APPROVAL` with no
    `approval_ref`, and this stage's one gate owns `exceptional_closure`, not this.

    Writing no status is deliberate. The incident is `resolved` and P26 will write `closed`; a
    chronic pattern is a fact about the *service*, and letting it move the incident's status would
    make the recurrence look like a change in this incident's progress.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    target_ref = subject.service_ref or subject.incident_id
    impact = state.get("impact")
    rca = state.get("rca")
    source_count, age = evidence_support(state, now)
    quality = state.get("data_quality")

    decision = ctx.policy.evaluate(
        PolicyInput(
            action_type=ActionType.CREATE_PM_CASE,
            incident_id=subject.incident_id,
            target_ref=target_ref,
            actor_role=ctx.automation_role,
            rca_confidence=rca.confidence if rca is not None else None,
            evidence_source_count=source_count,
            evidence_age_minutes=age,
            data_quality_flags=tuple(quality.flags) if quality is not None else (),
            attempt=attempt_number(state, ActionType.CREATE_PM_CASE),
            severity=impact.severity if impact is not None else Severity.MEDIUM,
        )
    )
    signals = _chronic_signals(state)
    detail: dict[str, Any] = {
        "decision_id": decision.decision_id,
        "outcome": decision.outcome.value,
        "signals": signals,
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
                    node="record_chronic_pattern",
                    action="create_service_problem",
                    outcome="not_recorded",
                    subject_ref=target_ref,
                    detail={**detail, "explanation": decision.explanation},
                )
            ],
        }

    idempotency_key = derive_id("IDK", target_ref, ActionType.CREATE_PM_CASE.value)
    action_id = derive_id("ACT", subject.incident_id, ActionType.CREATE_PM_CASE.value)
    request = ActionRequest(
        action_id=action_id,
        incident_id=subject.incident_id,
        action_type=ActionType.CREATE_PM_CASE,
        target_ref=target_ref,
        requested_at=now,
        idempotency_key=idempotency_key,
        actor=ctx.automation_actor,
        reason_code=ReasonCode.POLICY_ALLOWED,
        correlation_id=state.get("correlation_id") or subject.incident_id,
        policy_decision_id=decision.decision_id,
        policy_outcome=decision.outcome,
        attempt=attempt_number(state, ActionType.CREATE_PM_CASE),
        parameters={
            "chronic_signals": signals,
            "fault_domain": state.get("fault_domain", FaultDomain.UNKNOWN).value,
            "root_cause_summary": rca.summary if rca is not None else "",
            "linked_records": dict(state.get("linked_records", {})),
            "customer_ref": state.get("customer_ref"),
            "cpe_ref": state.get("cpe_ref"),
            "delimiter_ref": subject.delimiter_ref,
        },
        reversible=True,
        expected_blast_radius=impact.affected_customer_count if impact is not None else 1,
    )

    result = await ctx.adapters.tmf.upsert_service_problem(request)
    completed_at = ctx.clock.now()
    external_ref = result.get("external_ref")
    record = ActionRecord(
        action_id=action_id,
        incident_id=subject.incident_id,
        action_type=ActionType.CREATE_PM_CASE,
        target_ref=target_ref,
        idempotency_key=idempotency_key,
        outcome=ActionOutcome(str(result["outcome"])),
        started_at=now,
        completed_at=completed_at,
        actor=ctx.automation_actor,
        reason_code=request.reason_code,
        correlation_id=request.correlation_id,
        attempt=request.attempt,
        simulated=bool(result.get("simulated")),
        external_ref=external_ref,
        detail=str(result.get("detail") or ""),
        error=str(result.get("error") or ""),
    )

    update: NodeUpdate = {
        "policy_decisions": [decision],
        "action_history": [record],
        "updated_at": completed_at,
        "audit_events": [
            audit(
                state,
                ctx,
                node="record_chronic_pattern",
                action="create_service_problem",
                outcome=record.outcome.value,
                subject_ref=target_ref,
                detail={**detail, "external_ref": external_ref, "replayed": result.get("replayed")},
            )
        ],
    }
    if external_ref:
        update["linked_records"] = {SERVICE_PROBLEM_KEY: external_ref}
    return update


def _chronic_signals(state: IncidentState) -> dict[str, Any]:
    """The four things `route_chronic_pattern` ORs, itemised for the problem record.

    The router answers *whether*; a service-problem record an operator will read wants *which*.
    Re-listing them is a second reader of the same four facts and is worth the duplication only
    because it changes nothing: the router still decides, and this is describing its decision.

    Each of the four is read the way `route_chronic_pattern` reads it, down to `PRIOR_INCIDENTS_KEY`
    rather than the literal `"prior_incidents"`. `case_type` is reported as `None` rather than
    defaulted, because the router's `is CaseType.REPEAT_VISIT` treats an absent case type as "not a
    repeat"; substituting a default here would name a case type nobody set.
    """
    case_type = state.get("case_type")
    return {
        "case_type": case_type.value if case_type is not None else None,
        "truck_rolls": truck_roll_count(state),
        "mr_attempts": state.get("mr_attempt_count", 0),
        "prior_incidents": state.get("linked_records", {}).get(PRIOR_INCIDENTS_KEY),
    }


def _label(value: KPIValue | None) -> bool | None:
    """A 0-or-1 KPI rate read as a label. `None` stays `None` -- it means "not applicable here"."""
    return None if value is None else value.value > 0.0


def outcome_labels(state: IncidentState, calculator: KPICalculator) -> dict[str, bool | None]:
    """P26's sixteen structured outcome labels, in the specification's order.

    Each label names an owner rather than deriving the fact again, which is why most of them are one
    line: `KPICalculator` already owns eight of the sixteen and `route_chronic_pattern` owns a
    ninth. Three more are scored against the **field finding**, which is the only independent ground
    truth an incident carries -- a technician who went and looked. Scoring the RCA against the
    detectors instead would be circular, because `determine_root_cause` reads the findings the
    detectors produced.

    Two are `None` for every incident and are emitted anyway, which is the same choice
    `kpi.NOT_DERIVABLE_FROM_STATE` makes: naming a label that cannot be produced is more useful than
    dropping it, and a training set silently missing a column is worse than one with an explicit
    gap.

    * `reopen` -- a reopen is by definition a later event.
      `ClosurePolicy.reopen_creates_linked_incident` is `True`, so it arrives as a *new* incident
      carrying a link back; nothing this thread can observe at closure could set it.
    * `avoidable_dispatch` -- needs the counterfactual "would a remote action have fixed this",
      which no state carries. The nearest observable, "the crew travelled and found nothing", is
      already `no_fault_found`, and emitting it twice under two names would let anything trained on
      these labels count one fact as two.
    """
    finding = latest_field_finding(state)
    rca = state.get("rca")
    closure = state.get("closure")
    rejections = calculator.mr_rejection_rate(state)
    return {
        "detector_accuracy": finding.fault_confirmed if finding is not None else None,
        "root_cause_accuracy": (
            rca.delimiter_kind is finding.delimiter_kind
            if rca is not None and finding is not None
            else None
        ),
        "fault_domain_accuracy": (
            rca.fault_domain is finding.fault_domain
            if rca is not None and finding is not None
            else None
        ),
        "remote_action_success": _label(calculator.remote_resolution_rate(state)),
        "self_help_success": _label(calculator.self_help_success_rate(state)),
        "correct_dispatch": (
            crew_for(finding.fault_domain) is state.get("crew_type")
            if finding is not None
            else None
        ),
        "first_time_field_resolution": _label(calculator.first_time_fix_rate(state)),
        "no_fault_found": _label(calculator.no_fault_found_rate(state)),
        "avoidable_dispatch": None,
        "correct_delimiter_handover": _label(calculator.handover_acceptance_rate(state)),
        "mr_acceptance": None if rejections is None else rejections.value == 0.0,
        "repeat_work_order": _label(calculator.repeat_visit_rate(state)),
        "repeat_mr": (
            state.get("mr_attempt_count", 0) > 1 if state.get("mr_attempt_count", 0) else None
        ),
        "premature_closure": (
            closure.closure_code is ReasonCode.CLOSED_EXCEPTIONAL if closure is not None else None
        ),
        "reopen": None,
        "chronic_fault": route_chronic_pattern(state) == "chronic",
    }


@node("update_kpis_and_learning")
async def update_kpis_and_learning(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P26. Emit every derivable KPI and the outcome labels, and write `closed`. The last node.

    `emit_kpi` per KPI rather than `KPICalculator.emit_all`, and the difference is replay safety
    rather than taste: `emit_all` mints `new_id("KPI")`, which is `uuid4`, and `kpi_events`
    de-duplicates on `event_id` -- so a replayed super-step would record every measurement twice
    with two ids and double the numerator of every rate built from it. `emit_kpi` re-keys on the
    incident, the KPI and the node, which is the granularity at which one node measuring one thing
    should appear once. It also swallows `KPINotDerivableError`, so the two members of
    `NOT_DERIVABLE_FROM_STATE` and everything this incident cannot support simply produce nothing.

    The labels go in the audit event's `detail` because `IncidentState` has no field for them, and
    adding one for a value written once at the end of the lifecycle would put a column on every
    incident to hold a fact only closed ones have. The audit trail is already the durable record
    that a training set is extracted from.

    Emitted from `preview(state, update)` and not from `state`: the closure record is P25's, but
    `metrics_timestamps[CLOSED_AT]` and the `closed` status are written in this same update, and
    `sla_breach_rate` and `time_to_restore` both read through them. `select_remote_action` and
    `assess_restoration` hit the same trap.
    """
    now = ctx.clock.now()
    subject = subject_of(state)
    update: NodeUpdate = {
        "status": IncidentStatus.CLOSED,
        "updated_at": now,
    }

    previewed = preview(state, update)
    calculator = KPICalculator(ctx.clock)
    labels = outcome_labels(previewed, calculator)

    events = []
    for kpi_name in KPIName:
        events.extend(emit_kpi(previewed, ctx, kpi_name, node="update_kpis_and_learning"))
    update["kpi_events"] = events
    update["audit_events"] = [
        audit(
            state,
            ctx,
            node="update_kpis_and_learning",
            action="update_kpis_and_learning",
            outcome="closed",
            subject_ref=subject.service_ref or subject.incident_id,
            detail={
                "outcome_labels": labels,
                "labels_derived": sum(1 for v in labels.values() if v is not None),
                "kpis_emitted": sorted(event.kpi_name.value for event in events),
            },
        )
    ]
    return update


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The nine nodes. Checked like every other registry, so a node whose decorator disagrees with its
#: key fails on import rather than at the first traced incident.
RECONCILIATION_CLOSURE_NODES: tuple[tuple[str, Any], ...] = (
    ("reconcile_linked_systems", reconcile_linked_systems),
    ("hold_for_reconciliation_retry", hold_for_reconciliation_retry),
    ("evaluate_closure_policy", evaluate_closure_policy),
    ("prepare_exceptional_closure_approval", prepare_exceptional_closure_approval),
    ("request_exceptional_closure_approval", request_exceptional_closure_approval),
    ("abandon_closure", abandon_closure),
    ("close_linked_records", close_linked_records),
    ("record_chronic_pattern", record_chronic_pattern),
    ("update_kpis_and_learning", update_kpis_and_learning),
)

check_node_registry(RECONCILIATION_CLOSURE_NODES, "the reconciliation-closure node registry")

#: Where each answer of `route_closure_gate` goes. Named once because both the node that evaluates
#: the policy and the node that raises the interrupt route on it, and two copies of a three-way map
#: is two chances to wire one arm differently.
GATE_TARGETS: dict[str, str] = {
    "approve": "prepare_exceptional_closure_approval",
    "close": "close_linked_records",
    "abandon": "abandon_closure",
}


def build_reconciliation_closure_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    D23's `escalate` arm maps to `END`, sharing a destination with `ESCALATED`. That is not a
    redundant arm: `guarded()` reads `state["escalated"]` before the router's question, so the
    router's own `escalate` clause is structurally pre-empted and exists to make the router total
    over the state it is given -- which is how `remote_resolution`'s `DELIMITER_TARGETS` treats the
    same situation.

    The retry edge goes back to `reconcile_linked_systems` through `straight_on`, which means the
    loop terminates in two independent ways: `hold_for_reconciliation_retry` sets `escalated` when
    the pack's `max_retries` is spent, and `@node`'s `check_budgets` bounds re-entry regardless of
    what the pack says. Two bounds on one loop, of which only the first is an operator's to change.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in RECONCILIATION_CLOSURE_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "reconcile_linked_systems")
    graph.add_conditional_edges(
        "reconcile_linked_systems",
        guarded(route_reconciliation),
        {
            "close": "evaluate_closure_policy",
            "reconcile_retry": "hold_for_reconciliation_retry",
            "escalate": END,
            ESCALATED: END,
        },
    )
    graph.add_conditional_edges(
        "hold_for_reconciliation_retry",
        guarded(straight_on),
        {ONWARD: "reconcile_linked_systems", ESCALATED: END},
    )

    gate_map: dict[Any, str] = {**GATE_TARGETS, ESCALATED: END}
    graph.add_conditional_edges("evaluate_closure_policy", guarded(route_closure_gate), gate_map)
    graph.add_conditional_edges(
        "prepare_exceptional_closure_approval",
        guarded(straight_on),
        {ONWARD: "request_exceptional_closure_approval", ESCALATED: END},
    )
    graph.add_conditional_edges(
        "request_exceptional_closure_approval", guarded(route_closure_gate), gate_map
    )

    graph.add_conditional_edges(
        "close_linked_records",
        guarded(route_chronic_pattern),
        {
            "chronic": "record_chronic_pattern",
            "done": "update_kpis_and_learning",
            ESCALATED: END,
        },
    )
    graph.add_conditional_edges(
        "record_chronic_pattern",
        guarded(straight_on),
        {ONWARD: "update_kpis_and_learning", ESCALATED: END},
    )
    graph.add_edge("update_kpis_and_learning", END)
    graph.add_edge("abandon_closure", END)
    return graph


def compile_reconciliation_closure_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent. No checkpointer, by design.

    A subgraph compiled as a node shares the parent's checkpointer -- LangGraph namespaces its state
    beneath the parent's thread -- and handing this one its own would give the incident two places
    to be resumed from, which matters here because this stage pauses.
    """
    return build_reconciliation_closure_graph().compile(name="lpr_cpe_reconciliation_closure")


__all__ = [
    "GATE_TARGETS",
    "RECONCILERS",
    "RECONCILIATION_CLOSURE_NODES",
    "RETRY_KEY",
    "SERVICE_PROBLEM_KEY",
    "abandon_closure",
    "build_reconciliation_closure_graph",
    "close_linked_records",
    "compile_reconciliation_closure_graph",
    "evaluate_closure_policy",
    "hold_for_reconciliation_retry",
    "outcome_labels",
    "prepare_exceptional_closure_approval",
    "reconcile_communications",
    "reconcile_inventory",
    "reconcile_jtrack",
    "reconcile_linked_systems",
    "reconcile_nxt",
    "reconcile_tmf",
    "reconcile_wfm",
    "record_chronic_pattern",
    "request_exceptional_closure_approval",
    "route_closure_gate",
    "update_kpis_and_learning",
]
