"""D08's plant arm: refer the fault to OSP with no Clean Boots visit behind it. P19 and P20.

`builder.PENDING_STAGES` named this as `D08:plant_path`, and named it accurately: "P20, P21, D19 and
D20 are built and wired now, so what is missing is narrower than this entry used to say -- it is the
MR filing for a case that has no handover." The specification gives P20 two entrances, "the
handover evidence, or the NOC/plant evidence package when the case reached this step directly from
D08 without a Clean Boots visit", and D08's own remedy list says the second one is still bound by
P19: "the same policy-driven MR-approval requirement described in P19 still applies, using NOC/plant
evidence in place of a handover contract".

That sentence is this module. `_mr.submit_mr` is the filing itself, shared with
`field_execution.file_plant_mr` so the two entrances cannot drift; what is here is the
authorisation, the evidence package and the three ways out.

Ten incidents were arriving here and stopping silently
------------------------------------------------------
Measured over the 41 fixture services before this stage existed: 30 escalated, 1 closed, and **10
ended at `diagnosing` with no pause, no error and nothing to resume** -- `SVC-PO-042-A-04`,
`SVC-UT-001-A-03`, and the eight `SVC-VQ-002-*`. D08 answered `plant_path`, the arm went to `END`,
and a run that had done nothing was indistinguishable from one that had finished. Their domains were
`power` on eight, `distribution` on one and `service_platform` on one, which is both halves of what
D08 diverts: plant faults whose crew is Dirty, and the back-office domains no crew attends at all.

Re-swept with this stage wired, the ten stalls are gone: 40 escalated and 1 closed, no exceptions,
and nothing left at `diagnosing`. What the ten do now, measured individually, is walk the whole arm
-- one visit each to `evaluate_plant_referral`, `prepare_plant_referral_approval` and
`file_plant_referral_mr`, one `MRRecord` apiece, no abandons -- then cross into `plant_execution`,
chase the MR six times and escalate on `node_reentries budget exhausted: observed 6, limit 6`.

That last hop is worth being plain about, because "escalated" reads like failure. It is D19's
pre-existing `await_plant` self-loop reaching the guard's ceiling: the simulated OSP never completes
an MR, so the answer stays "still with OSP" until the budget stops it. The stage's own contribution
is the difference between a run that stopped without a record and one that filed an MR, handed it to
the stage that owns plant work, and escalated with a reason a supervisor can act on. Closing those
ten needs an OSP that finishes repairs, which is the recovery-model gap, not this one.

Why the evidence package is not a `HandoverContract`
---------------------------------------------------
The obvious economy would be to build one and reuse P18's machinery. Measured, it would audit
`incomplete` on every one of the ten, because `HandoverContract.missing_items()` checks things that
exist only after a visit:

* the technology's **field measurements** -- nobody took any;
* at least one **finding id** -- `field_findings` is empty, no crew submitted anything;
* a non-empty **`ruled_out`** -- `rca.ruled_out` is empty on all ten of them.

And `CrewType` has no NOC member (`clean`, `dirty`, `joint`), so the contract could not even name
who holds the case now. The specification agrees rather than being worked around: on this path the
"Clean-Boots-specific `HandoverContract` fields (technician measurements, last-clean/first-failed
point, parts used) do not apply and may be omitted". So `plant_referral_packet` is a pure function
of state, stored nowhere, and `completeness` is a question this path does not ask.

What the referral can actually be filled from, and the one field that blocked it
-------------------------------------------------------------------------------
`REQUIRED_MR_FIELDS` is `("plant_object_ref", "fault_description", "evidence_refs",
"access_notes")`, refused *non-retryably* by `create_mr`. Measured across the ten: `delimiter_ref`
is resolved on all ten and real (`TAP-PO-042-A`, `ODP-UT-001-A`, `ODP-VQ-002-A`, `ODP-VQ-002-B`),
`rca.summary` is non-empty on all ten, `rca.evidence_refs` runs 1 to 9 -- and `access_notes` is
**absent on all ten**. That one field is what stood between this arm and a filed MR, and it is why
`_mr.mr_access_notes` was moved out of `field_execution`: the note composes from `topology`, which
P03 resolved long before D08, rather than from the technician's note nobody wrote.

Four nodes and a wait, and why `escalated` is the third way out
---------------------------------------------------------------
`evaluate_plant_referral` puts the MR to the engine before anything is assembled, for
`evaluate_handover_policy`'s reason turned around. There the order was forced by the lifecycle;
here it is forced by honesty about what a block means -- the pack refusing an MR for a case with no
crew, no premises visit and no remote option is the pack saying nobody automated may act, and
assembling a package first would be assembling a package for a question already answered.

`prepare_plant_referral_approval` and `request_plant_referral_approval` are two nodes for
`graph.interrupts`' reason: everything before `interrupt()` re-runs on resume, so a node that both
built the question and waited would re-stamp `requested_at` on every resume.

`abandon_plant_referral` writes `escalated`, where `field_execution.abandon_handover` writes
`diagnosing`. The difference is what a refusal leaves behind. A refused *handover* has a Clean Boots
finding that diagnosis has not yet seen, so another lap is a lap with new evidence in it. A refused
*referral* has nothing new at all -- no crew went, no action was attempted (measured:
`action_history` is empty on all ten), and D08 reads the same `fault_domain` it read the first time,
so `diagnosing` would route straight back here with identical inputs. The case is a human's.

The lifecycle, and the row that turned out not to be needed
-----------------------------------------------------------
The parent enters at `diagnosing` and is shown `mr_raised`, one collapsed jump. The plan was to add
`DIAGNOSING -> AWAITING_HANDOVER` to `TRANSITIONS` and record the middle as `(AWAITING_HANDOVER,)`,
by analogy with the Clean Boots seam. Measured against `can_transition`, the node table already
permits the walk this stage actually takes:

    diagnosing        -> awaiting_approval  True
    awaiting_approval -> mr_raised          True
    diagnosing        -> awaiting_handover  False
    diagnosing        -> mr_raised          False

`prepare_approval` writes `awaiting_approval` and `diagnosing` reaches it in one hop, so nothing new
is needed in the node table -- only the seam entry `(DIAGNOSING, MR_RAISED): (AWAITING_APPROVAL,)`
recording the middle the parent cannot see. The `escalated` exit needs no entry either: both
`diagnosing -> escalated` and `awaiting_approval -> escalated` were already single legal hops. The
analogy was the wrong guide and the table was the right one.

Where the parent cannot see this
--------------------------------
While `request_plant_referral_approval` is paused, the pause is in *this* graph's checkpoint, and
the parent alone reports whatever `generate_resolution_options` last left behind. `graph.inspect`
reads through the boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    FaultDomain,
    IncidentStatus,
    KPIName,
    ReasonCode,
)
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.interrupts import build_request, prepare_approval, request_approval
from lpr_cpe.graph.nodes._runtime import (
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    emit_kpi,
    node,
    preview,
)
from lpr_cpe.graph.routing import latest_decision_of, latest_policy_decision
from lpr_cpe.graph.state import IncidentState, current_mr_records
from lpr_cpe.graph.subgraphs._mr import (
    mr_access_notes,
    mr_idempotency_key,
    mr_policy_input,
    mr_reference,
    mr_severity,
    plant_object_ref,
    submit_mr,
)
from lpr_cpe.graph.subgraphs._shared import attempt_number
from lpr_cpe.observability.kpi import MetricTimestamp, mark

#: The node the round counter joins on, named once because three readers key on it: the approval id,
#: the MR's action id and the audit discriminators. It is the *first* node rather than the one that
#: asks, so the count reads one on the first question -- `handover_round`'s arrangement, and for the
#: same reason: `@node` writes `node_visits` last, so a node cannot see its own visit.
ENTRY_NODE = "evaluate_plant_referral"


# ------------------------------------------------------------------------------------------------
# Reading the incident for the referral
# ------------------------------------------------------------------------------------------------


def referral_round(state: IncidentState) -> int:
    """Which pass through this stage the case is on. One-based inside the stage; zero before it.

    `approvals` de-duplicates on `approval_id` first-write-wins and `approval_id_for` warns that
    "callers pass the relevant attempt counter", so a second referral whose round did not move would
    have its question silently dropped and the first answer read in its place.

    Not `attempt_number(state, RAISE_MR)`, which counts actions that reached jTrack. A referral the
    pack blocked reached no adapter, so that counter never moves; measured on the ten fixtures that
    arrive here it is `1` for every one of them, because `action_history` is empty -- D08 diverts
    before D09, so nothing has been attempted at all.
    """
    return int(state.get("node_visits", {}).get(ENTRY_NODE, 0))


def referral_target(state: IncidentState) -> str:
    """What the referral is about: the plant object P03 resolved. Never a finding's.

    `plant_object_ref(state, None)` rather than the two-argument form, and the `None` is the whole
    character of this path -- no crew attended, so nobody delimited, and the reference comes from
    the plant records instead. Measured, all ten fixtures that reach here carry one and none falls
    through to the service reference.

    Named as a function rather than computed twice, because the gate and three nodes each need it
    and it is the join between them: `mr_idempotency_key` derives from it, so two readings that
    disagreed would file two MRs for one boundary.
    """
    return plant_object_ref(state, None)


def referral_fault_domain(state: IncidentState) -> FaultDomain:
    """The domain D08 routed on. Read from state, not from `rca`, though both hold one.

    `route_shared_or_plant` reads `state["fault_domain"]`, and this stage exists because of the
    answer it gave. Reading `rca.fault_domain` here instead would be a second opinion about the
    thing that has already been decided, and the two are only equal until something writes one
    without the other.
    """
    return state.get("fault_domain") or FaultDomain.UNKNOWN


def receiving_owner(domain: FaultDomain) -> str | None:
    """Who the referral is *to*, as `domain.boundaries` derives it. `None` for the back office.

    P19's "proposed domain" and P18's "correct receiving owner" are the same question, and
    `crew_for` is the one place that answers it. Measured, D08's plant arm admits two kinds:
    `distribution`, `feeder`, `node_or_olt`, `headend_or_co` and `power` all answer `dirty`, while
    `service_platform` and `provisioning` answer `None` -- they are in `BACK_OFFICE_DOMAINS`, and
    D08 diverts them because no crew attends them, not because a Dirty crew does.

    `None` is therefore a real answer and is passed through as one rather than defaulted to `dirty`.
    An MR whose `crew_type_required` claimed a Dirty crew for a provisioning fault would send a
    truck to a database, and it would send it on our say-so rather than on the boundary model's.
    """
    crew = crew_for(domain)
    return crew.value if crew is not None else None


def plant_referral_packet(state: IncidentState, ctx: GraphContext) -> dict[str, Any]:
    """The NOC/plant evidence package: P19's eight items, then what P20's MR needs.

    Numbered only where the specification numbers. P19 lists eight things the approval payload "must
    show" and those are `01` to `08`, so a reviewer can check the payload against the document item
    by item -- `handover_packet`'s arrangement. The package itself it does not enumerate; it names
    it ("the NOC/plant evidence package") and says which Clean-Boots fields may be omitted from it.
    Those keys are therefore spelled rather than numbered, because a number would imply a list
    somebody could check them against.

    Stored nowhere and read by exactly three callers, again like `handover_packet`: the audit event
    `prepare_plant_referral_approval` writes, the approval payload P19 puts to a human, and the MR
    parameters P20 sends to jTrack. Every value is read from whichever field already owns it, so a
    gap arrives as a `None` against a named key rather than as a missing key.

    **`current_domain` is the automation's role, and that is the honest spelling.** The Clean Boots
    packet says `clean` because a Clean crew held the case; here nobody does, and `CrewType` has no
    NOC member to name. `ctx.automation_role` is what the policy engine was asked about and what the
    audit trail records as actor, so it is what "current domain" truthfully is on this path.

    **`existing_mr_result` is what the incident knows, not what jTrack knows.** The authoritative
    duplicate check happens inside `submit_mr`, at the moment of filing, through `fetch_open_mrs`.
    This is the same distinction `handover_packet` draws at item 21 and for the same reason: a human
    approving the referral can only be shown what has already been read.
    """
    topology = state.get("topology")
    rca = state.get("rca")
    impact = state.get("impact")
    sla = state.get("sla")
    cpe = state.get("cpe")
    technology = state.get("technology")
    domain = referral_fault_domain(state)
    target_ref = referral_target(state)

    return {
        # P19's eight, each named as the specification names it.
        "01_incident": state.get("incident_id"),
        "02_current_domain": ctx.automation_role.value,
        "03_proposed_domain": receiving_owner(domain),
        "04_confidence": rca.confidence if rca is not None else None,
        # "Missing evidence, if any" -- and on this path the answer is structural rather than a
        # per-incident gap: the Clean Boots items are missing by construction, and naming them is
        # what tells the approver they are approving without them rather than in spite of them.
        "05_missing_evidence": [
            "field measurements: no crew attended",
            "last-clean and first-failed point: no crew attended",
            "parts used: no crew attended",
        ],
        "06_existing_mr_result": {
            "known_mrs": [
                {"mr_id": r.mr_id, "external_ref": r.external_ref, "status": r.status.value}
                for r in current_mr_records(state).values()
            ],
            "idempotency_key": mr_idempotency_key(state, target_ref),
            "affected_delimiter_refs": (
                list(impact.affected_delimiter_refs) if impact is not None else []
            ),
        },
        "07_crew_and_equipment_requirement": {
            # No `DispatchRequirement` exists on this path and none can: `build_field_requirement`
            # runs in field planning, which D08 diverted around. Measured, `dispatch_requirements`
            # is empty on all ten. So the requirement is what the boundary model derives, and the
            # equipment is OSP's to decide once they hold the MR.
            "crew_type": receiving_owner(domain),
            "equipment": [],
            "permit_required": None,
            "source": "domain.boundaries.crew_for; no dispatch requirement was built",
        },
        "08_sla_impact": {
            "severity": impact.severity.value if impact is not None else None,
            "product_tier": sla.product_tier if sla is not None else None,
            "vulnerable_customer": sla.vulnerable_customer if sla is not None else None,
            "affected_customer_count": (
                impact.affected_customer_count if impact is not None else None
            ),
            "sla_at_risk_count": impact.sla_at_risk_count if impact is not None else None,
            "clock_started_at": sla.clock_started_at.isoformat() if sla is not None else None,
            "restore_deadline": sla.restore_deadline().isoformat() if sla is not None else None,
        },
        # The evidence package. Unnumbered; see the docstring.
        "plant_object_ref": target_ref,
        "fault_domain": domain.value,
        "fault_description": rca.summary if rca is not None else "",
        "evidence_refs": sorted(rca.evidence_refs) if rca is not None else [],
        "access_notes": mr_access_notes(state, None),
        "safety_notes": _safety_notes(state),
        "identifiers": {
            "customer_ref": state.get("customer_ref"),
            "product_ref": state.get("product_ref"),
            "service_ref": state.get("service_ref"),
            "cpe_ref": state.get("cpe_ref"),
            "cpe_model": cpe.model if cpe is not None else None,
            "technology": technology.value if technology is not None else None,
        },
        "address_and_gis": {
            "latitude": topology.latitude if topology is not None else None,
            "longitude": topology.longitude if topology is not None else None,
            "mdu_ref": topology.mdu_ref if topology is not None else None,
            "area_archetype": (
                topology.area_archetype.value
                if topology is not None and topology.area_archetype is not None
                else None
            ),
        },
        "network_context": {
            "node_ref": topology.node_ref if topology is not None else None,
            "cmts_ref": topology.cmts_ref if topology is not None else None,
            "service_group_ref": topology.service_group_ref if topology is not None else None,
            "olt_ref": topology.olt_ref if topology is not None else None,
            "pon_port_ref": topology.pon_port_ref if topology is not None else None,
            "primary_splitter_ref": topology.primary_splitter_ref if topology is not None else None,
            "split_ratio": topology.split_ratio if topology is not None else None,
            "headend_ref": topology.headend_ref if topology is not None else None,
            "homes_behind_delimiter": (
                topology.homes_behind_delimiter if topology is not None else None
            ),
            "topology_source": topology.topology_source if topology is not None else None,
        },
        "nxt_snapshot": [
            {
                "ref": item.ref,
                "kind": item.kind.value,
                "observed_at": item.observed_at.isoformat(),
                "summary": item.summary,
            }
            for item in state.get("evidence", [])
        ],
        # Empty on all ten measured, and that emptiness is the point of D08: no truck was sent and
        # no remote repair tried, because the fault is not on the customer's side of the boundary.
        "actions_attempted": [
            {
                "action_type": record.action_type.value,
                "outcome": record.outcome.value,
                "target_ref": record.target_ref,
                "attempt": record.attempt,
            }
            for record in state.get("action_history", [])
        ],
        "diagnosis": {
            "delimiter_kind": (
                rca.delimiter_kind.value
                if rca is not None and rca.delimiter_kind is not None
                else None
            ),
            "cycles_used": rca.cycles_used if rca is not None else None,
            "reason_code": (rca.reason_code.value if rca is not None and rca.reason_code else None),
            "hypotheses": (
                [
                    {
                        "statement": h.statement,
                        "domain": h.fault_domain.value,
                        "posterior": h.posterior,
                    }
                    for h in rca.live
                ]
                if rca is not None
                else []
            ),
        },
        "referral_round": referral_round(state),
        "prior_records": dict(state.get("linked_records", {})),
    }


def _safety_notes(state: IncidentState) -> str:
    """What the receiving crew is walking into, on a path where nobody has been to look.

    `field_execution._safety_notes` can say "no specific hazard was reported by the Clean Boots
    crew" because a Clean Boots crew reported. Here the truthful note is the *absence* of a survey,
    and the difference matters to whoever picks the MR up: "nobody reported a hazard" and "nobody
    looked" are not the same sentence, and only one of them is true on this path.

    The area archetype is included because it is the only hazard-adjacent thing state holds without
    a visit. Measured, `remote_island` on eight of the ten -- which is a real access constraint and
    the kind of thing an OSP planner acts on.
    """
    topology = state.get("topology")
    parts = ["no site survey: this referral was raised from records, with no crew on site"]
    if topology is not None and topology.area_archetype is not None:
        parts.append(f"access archetype {topology.area_archetype.value}")
    return "; ".join(parts)


# ------------------------------------------------------------------------------------------------
# The gate
# ------------------------------------------------------------------------------------------------


def route_plant_referral_gate(
    state: IncidentState,
) -> Literal["refer", "file", "abandon", "already_referred"]:
    """Already done, then policy, then the answer. On two edges, like `route_handover_gate`.

    One router on the edge out of `evaluate_plant_referral` and on the edge out of
    `request_plant_referral_approval`, which is what makes the resumed pass correct rather than
    accidental: the node that raised the interrupt re-runs from its start, so the edge leaving it is
    evaluated twice against two different states.

    **`already_referred` is first, and it is the entry guard rather than a tidy-up.** This stage is
    re-enterable: D19's `retry_diagnosis` reaches `determine_root_cause`, which reaches D08 again
    through the D07 chain, which reaches here. On that second pass `approvals` still holds the first
    round's `approved`, and `latest_decision_of` would hand it over as though it answered a question
    nobody had asked -- so an incident could file a second MR carrying an approval given for the
    first. Testing for an MR first is what closes that, and it is the same shape as
    `route_visit_gate`: ask whether the thing this stage does has already been done.

    Scoped to "any MR" and not "an MR for this plant object", which is P20's own instruction read
    plainly: "Update the existing MR when appropriate. Otherwise create one MR." An incident whose
    re-diagnosis moved the boundary needs its MR *updated*, and `plant_execution.update_plant_mr` is
    the node that owns that. One incident, one referral.

    Then the order of authority, `route_handover_gate`'s: `PolicyEngine` is the only thing that
    authorises an action, so a blocked decision is answered before the approval is looked for.
    Absent approval means the question has not been put yet; present and not `approved` means
    refused. There is no `approval_outstanding` clause here for the reason there is none there --
    `interrupt()` means the pause *is* the wait, so this edge is not evaluated until an answer
    exists.

    `already_referred` is unreachable on the second edge: nothing between the two writes an MR. It
    is mapped there anyway because `add_conditional_edges` raises on an unmapped return value,
    which is the arrangement `DELIMITER_TARGETS` already documents for D17's `escalate`.
    """
    if current_mr_records(state):
        return "already_referred"
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    if decision is None or decision.blocked:
        return "abandon"
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)
    if answer is None:
        return "refer"
    return "file" if answer.status is ApprovalStatus.APPROVED else "abandon"


# ------------------------------------------------------------------------------------------------
# P19 -- the authorisation, then the question
# ------------------------------------------------------------------------------------------------


@node(ENTRY_NODE)
async def evaluate_plant_referral(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Put the MR to the policy engine before anything is assembled. P19's authorisation.

    Writes no status, and that is deliberate rather than incidental. The incident stays
    `diagnosing`, which is the one status from which both of this stage's destinations are legal:
    `awaiting_approval` for the question, `escalated` for the refusal. A node that advanced the
    status here would have to pick one of them before the engine had answered.

    Returns without evaluating when the incident already holds an MR -- `open_field_visit`'s early
    return, and `route_plant_referral_gate` explains why the question is asked at all. The audit
    event is still written, because "this stage was entered and declined to act" is exactly the fact
    that was invisible when this arm went to `END`.

    The decision id is re-keyed from the incident, the action, the plant object and the diagnostic
    cycle count. `PolicyEngine.evaluate` mints a `uuid4`, so an unkeyed decision looks new to
    `append_unique` on every replay. `evaluate_handover_policy` keys on the finding because a second
    visit is what changes its answer; here there is no finding, and what changes between passes is
    the diagnosis -- so `rca.cycles_used` is the discriminator, alongside the plant object in case
    re-diagnosis moved the boundary.
    """
    now = ctx.clock.now()
    target_ref = referral_target(state)
    held = current_mr_records(state)
    if held:
        return {
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node=ENTRY_NODE,
                    action=ENTRY_NODE,
                    outcome="already_referred",
                    subject_ref=target_ref,
                    reason_code=ReasonCode.POLICY_DUPLICATE_SUPPRESSED,
                    detail={
                        "mr_ids": sorted(held),
                        "external_refs": sorted(
                            r.external_ref for r in held.values() if r.external_ref
                        ),
                        "reason": (
                            "this incident has already referred a fault to OSP; P20 updates an "
                            "existing MR rather than filing a second, and plant_execution owns that"
                        ),
                    },
                    discriminator="already_referred",
                )
            ],
        }

    rca = state.get("rca")
    if rca is None:
        raise ValueError(
            "evaluate_plant_referral was reached with no RCA result. D08 is asked in the chain "
            "after `generate_resolution_options`, which is downstream of `determine_root_cause`, "
            "and D08 routes on the `fault_domain` that node writes -- so the plant arm cannot be "
            "entered without one."
        )

    verdict = ctx.policy.evaluate(
        mr_policy_input(
            state, ctx, target_ref=target_ref, idempotency_key=mr_idempotency_key(state, target_ref)
        )
    )
    decision = verdict.model_copy(
        update={
            "decision_id": derive_id(
                "POL",
                state.get("incident_id") or "",
                ActionType.RAISE_MR.value,
                target_ref,
                str(rca.cycles_used),
                verdict.outcome.value,
            )
        }
    )

    update: NodeUpdate = {
        "policy_decisions": [decision],
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node=ENTRY_NODE,
                action=ENTRY_NODE,
                outcome=decision.outcome.value,
                subject_ref=target_ref,
                reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                detail={
                    "action_type": ActionType.RAISE_MR.value,
                    "plant_object_ref": target_ref,
                    "fault_domain": referral_fault_domain(state).value,
                    "receiving_owner": receiving_owner(referral_fault_domain(state)),
                    "round": referral_round(state),
                    "attempt": attempt_number(state, ActionType.RAISE_MR),
                    "rca_confidence": rca.confidence,
                    "rca_cycles_used": rca.cycles_used,
                    "policy_decision_id": decision.decision_id,
                    "policy_version": decision.policy_version,
                    "matched_rule": decision.matched_rule,
                    "required_approval": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind
                        else None
                    ),
                    "required_role": decision.required_role,
                    "explanation": decision.explanation,
                },
                discriminator=decision.decision_id,
            )
        ],
    }
    # `preview`, not `state`: `policy_block_rate` counts `policy_decisions`, and the one it must
    # count is still sitting unreduced in `update`.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.POLICY_BLOCK_RATE,
        node=ENTRY_NODE,
        dimensions={"action_type": ActionType.RAISE_MR.value},
        discriminator=decision.decision_id,
    )
    return update


@node("prepare_plant_referral_approval")
async def prepare_plant_referral_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Assemble the NOC/plant package and write the question down, then return. P19a.

    The first half of the pair `graph.interrupts` describes, and the same shape as
    `prepare_handover_approval`: the whole question is built here so `requested_at` is stamped once
    however many times the node that raises replays.

    **`ApprovalKind.CLEAN_TO_DIRTY_HANDOVER`, on a path where no Clean crew attended.** The name
    reads wrong and the kind is right, for two reasons that agree. Measured, the pack demands this
    kind on all ten fixtures that reach here -- `required_approval_kind` is
    `clean_to_dirty_handover` and `matched_rule` is `rca.min_for_mr` on every one of them -- so any
    other kind would be a question asked under a name the engine did not ask for. And
    `route_plant_referral_gate` reads `latest_decision_of(state, CLEAN_TO_DIRTY_HANDOVER)`
    literally, as `route_handover_gate` does, so a question asked under another kind would be
    answered and then never seen. What the pack is actually pricing is the transfer of
    responsibility to OSP, the same transfer whether or not a Clean crew was involved: the rule's
    `required_role` is `osp_engineer` and it holds for 480 minutes. The audit detail records the
    pack's demand next to the kind asked, which is where a disagreement between them would surface.

    `required_role` is not the same as who may answer, and the difference matters on this path.
    Measured, `rbac.approvers_for(CLEAN_TO_DIRTY_HANDOVER)` permits four -- `admin`,
    `field_technician`, `noc_supervisor`, `osp_engineer` -- and `request_approval` sends that set as
    `permitted_roles` for the reason its docstring gives, that a supervisor may answer an engineer's
    question. So a NOC supervisor authorising a referral is the pack working as written and not a
    role check being skipped: driving the ten fixtures with a `noc_supervisor` answer files the MR.

    `attempt=referral_round(state)` and not `attempt_number(state, RAISE_MR)`, for
    `prepare_handover_approval`'s reason: a referral the pack blocked reached no adapter, so the
    action counter does not move and a second question would derive the first one's id -- which
    `approvals` de-duplicates away, leaving the second refusal invisible.
    """
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    if decision is None:
        raise ValueError(
            "prepare_plant_referral_approval was reached with no policy decision for raise_mr. "
            "`route_plant_referral_gate` answers `abandon` for a missing decision, so the `refer` "
            "edge cannot produce one."
        )

    packet = plant_referral_packet(state, ctx)
    target_ref = referral_target(state)
    domain = referral_fault_domain(state)
    round_number = referral_round(state)
    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.CLEAN_TO_DIRTY_HANDOVER,
        question=(
            f"Approve referring {state.get('incident_id')} to OSP at {target_ref} and raising a "
            f"jTrack MR against it? The fault is in the {domain.value} domain and no premises "
            f"visit was made, so this package is built from records. Referral {round_number}."
        ),
        attempt=round_number,
        action_type=ActionType.RAISE_MR,
        target_ref=target_ref,
        recommendation=packet["fault_description"],
        risk_summary=decision.explanation,
        blast_radius=packet["08_sla_impact"]["affected_customer_count"],
        # An MR is not reversible in the sense this flag means: the OSP work it schedules is
        # physical. `file_plant_mr` says the same and for the same reason.
        reversible=False,
        policy_decision_id=decision.decision_id,
        context={
            "incident": packet["01_incident"],
            "current_domain": packet["02_current_domain"],
            "proposed_domain": packet["03_proposed_domain"],
            "confidence": packet["04_confidence"],
            "missing_evidence": packet["05_missing_evidence"],
            "existing_mr_result": packet["06_existing_mr_result"],
            "crew_and_equipment_requirement": packet["07_crew_and_equipment_requirement"],
            "sla_impact": packet["08_sla_impact"],
            # Beyond the eight, so the approval screen can show the package the decision rests on.
            "packet": packet,
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
                node="prepare_plant_referral_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=target_ref,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "approval_id": request.approval_id,
                    "kind": ApprovalKind.CLEAN_TO_DIRTY_HANDOVER.value,
                    "round": round_number,
                    "plant_object_ref": target_ref,
                    "fault_domain": domain.value,
                    "receiving_owner": packet["03_proposed_domain"],
                    "required_role": request.required_role,
                    "policy_required_approval": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind
                        else None
                    ),
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                    # The whole package. `prepare_*` is this stage's only chance to record it: there
                    # is no P18 node here to write it down, and P20 sends jTrack the fields rather
                    # than the document.
                    "packet": packet,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_plant_referral_approval")
async def request_plant_referral_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Builds nothing; see `graph.interrupts`."""
    return request_approval(state, ctx)


# ------------------------------------------------------------------------------------------------
# P20 -- the MR, from the NOC/plant package
# ------------------------------------------------------------------------------------------------


@node("file_plant_referral_mr")
async def file_plant_referral_mr(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Assemble the NOC/plant package and file the MR against it. P20, the second way in.

    The mechanism is `_mr.submit_mr` and none of it is here: the jTrack search, the
    `REQUIRED_MR_FIELDS` re-check, the `ActionRequest`, the `MRRecord`, the `ActionRecord`, the
    `mr_raised` write. That module's docstring argues why -- `plant_execution` reads the MR back
    entirely off the record, so two filers that built it differently would give D19 two different
    answers to "is this still with OSP?" for incidents in the same state, and nothing would fail.

    What is here is the `parameters` payload, which is deliberately *not* shared. The two filers
    assemble genuinely different packages, and the difference is the specification's: one from a
    `HandoverContract` a technician's finding produced, one from the diagnosis alone.
    `REQUIRED_MR_FIELDS` is what holds them to a common floor.

    No work order is closed and no contract is marked accepted, because there is neither. That is
    the whole of what this filer does less than `file_plant_mr`, and it is why
    `HANDOVER_ACCEPTANCE_RATE` and `HANDOVER_REWORK_RATE` are not emitted: measured, both read
    `state["handover_contract"]` and return `None` without one, so emitting them would emit nothing
    while implying a handover happened. `PLANT_REPAIR_BACKLOG` *is* emitted, because an MR filed
    from records puts the same load on OSP as one filed from a visit.

    That omission is a convention and not an invariant, and the difference was measured rather than
    assumed: adding a `HANDOVER_ACCEPTANCE_RATE` emission here leaves this stage's tests green,
    because the emission produces no event. So nothing enforces it and no test claims to. If a
    `handover_contract` is ever set on this path the omission stops being free, and this paragraph
    is then the only record of why it was ever here.
    """
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    if decision is None:
        raise ValueError(
            "file_plant_referral_mr was reached with no policy decision for raise_mr. "
            "`route_plant_referral_gate` answers `abandon` for a missing decision, so the `file` "
            "edge cannot produce one."
        )

    packet = plant_referral_packet(state, ctx)
    target_ref = referral_target(state)
    domain = referral_fault_domain(state)
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)

    submission = await submit_mr(
        state,
        ctx,
        node_name="file_plant_referral_mr",
        parameters={
            "plant_object_ref": target_ref,
            "fault_description": packet["fault_description"],
            "evidence_refs": packet["evidence_refs"],
            "access_notes": packet["access_notes"],
            "safety_notes": packet["safety_notes"],
            "crew_type_required": packet["03_proposed_domain"],
            "priority": mr_severity(state).value,
            "homes_affected": packet["08_sla_impact"]["affected_customer_count"],
            "suspected_fault_class": domain.value,
            # What tells an OSP engineer which of P20's two entrances this MR came through, and
            # therefore which fields they should not expect to find.
            "evidence_path": "noc_plant_package",
            "site_survey_performed": False,
            "referral_round": packet["referral_round"],
            "network_context": packet["network_context"],
            "address_and_gis": packet["address_and_gis"],
            "diagnosis": packet["diagnosis"],
            "omitted_clean_boots_fields": packet["05_missing_evidence"],
        },
        target_ref=target_ref,
        fault_domain=domain,
        decision=decision,
        approval=answer,
        discriminator=f"{target_ref}:{packet['referral_round']}",
        blast_radius=packet["08_sla_impact"]["affected_customer_count"] or 1,
        notes=[f"referred from diagnosis, no premises visit; referral {packet['referral_round']}"],
        evidence_refs=tuple(packet["evidence_refs"]),
        detail={
            "evidence_path": "noc_plant_package",
            "receiving_owner": packet["03_proposed_domain"],
            "referral_round": packet["referral_round"],
            "rca_confidence": packet["04_confidence"],
            "accepted_by": answer.decided_by if answer is not None else ctx.automation_actor,
        },
        refusal_hint=(
            "`mr_access_notes` names the plant object even when topology resolved nothing, and "
            "`plant_object_ref` falls back to the service reference, so the only field that can "
            "arrive empty here is `fault_description` or `evidence_refs`, from an RCA that ended "
            f"without either -- the RCA holds {len(packet['evidence_refs'])} evidence reference(s) "
            f"and a summary of {len(packet['fault_description'])} character(s)"
        ),
    )

    update = submission.update
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.PLANT_REPAIR_BACKLOG,
        node="file_plant_referral_mr",
        dimensions={"fault_domain": domain.value},
        discriminator=submission.record.mr_id,
    )
    return update


# ------------------------------------------------------------------------------------------------
# The refusal
# ------------------------------------------------------------------------------------------------


@node("abandon_plant_referral")
async def abandon_plant_referral(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Policy refused the MR, or a human did. Record the refusal and hand the case to a person.

    Three arrivals, one node, the same three `abandon_handover` distinguishes -- and the same
    lifecycle argument permits it here: measured, `diagnosing -> escalated` and
    `awaiting_approval -> escalated` are legal single hops, so the policy block (which arrives while
    the incident is still `diagnosing`) and the refused approval (which arrives at
    `awaiting_approval`) can share a destination that neither of `abandon_handover`'s two could.

    `escalated` and not `diagnosing`, which is where the module docstring's argument lands: a
    refused referral has nothing new to re-diagnose with. No crew went, `action_history` is empty on
    all ten measured cases, and D08 reads the same `fault_domain` it read the first time -- so
    `diagnosing` would route back here with identical inputs and the loop would end on the budget
    rather than on anything having been decided. `escalation_reason` is what a supervisor picking
    this up needs, so it names which of the three refusals happened.

    No work order to close and no contract to mark rejected, unlike `abandon_handover`. There is
    therefore no `HANDOVER_ACCEPTANCE_RATE` to emit either: measured, it reads
    `state["handover_contract"]` and returns `None` without one.
    """
    now = ctx.clock.now()
    target_ref = referral_target(state)
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)

    if decision is not None and decision.blocked:
        arrival = "policy_blocked"
        reason = (
            decision.reason_codes[0]
            if decision.reason_codes
            else ReasonCode.POLICY_NO_MATCHING_RULE
        )
        explanation = decision.explanation or "the policy pack blocked the referral"
    elif answer is not None:
        arrival = "approval_refused"
        reason = answer.reason_code or ReasonCode.HANDOVER_REJECTED_WRONG_DOMAIN
        explanation = answer.rationale or "the plant referral was not approved"
    else:
        # `route_plant_referral_gate` sends a *missing* decision here too, which is the state an
        # incident reaches only if it entered the stage mid-way. Named rather than folded into
        # either branch above, because the repair is upstream and a reader needs to see that.
        arrival = "no_policy_decision"
        reason = ReasonCode.POLICY_EVIDENCE_INSUFFICIENT
        explanation = "the referral gate was reached with no policy decision for raise_mr"

    return {
        "status": IncidentStatus.ESCALATED,
        "escalated": True,
        "escalation_reason": f"plant referral refused ({arrival}): {explanation}",
        "pending_approval": None,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_plant_referral",
                action="abandon_plant_referral",
                outcome=arrival,
                subject_ref=target_ref,
                reason_code=reason,
                detail={
                    "arrival": arrival,
                    "plant_object_ref": target_ref,
                    "fault_domain": referral_fault_domain(state).value,
                    "round": referral_round(state),
                    "policy_decision_id": decision.decision_id if decision is not None else None,
                    "policy_outcome": decision.outcome.value if decision is not None else None,
                    "policy_reason_codes": (
                        [code.value for code in decision.reason_codes]
                        if decision is not None
                        else []
                    ),
                    "approval_id": answer.approval_id if answer is not None else None,
                    "approval_status": answer.status.value if answer is not None else None,
                    "explanation": explanation,
                },
                discriminator=f"{arrival}:{referral_round(state)}",
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The five nodes, in the order the specification walks them. Checked the way `PARENT_NODES` is, so
#: a node registered under a name its decorator does not carry fails on import rather than
#: producing a graph whose topology and audit trail disagree.
PLANT_REFERRAL_NODES: tuple[tuple[str, Any], ...] = (
    (ENTRY_NODE, evaluate_plant_referral),
    ("prepare_plant_referral_approval", prepare_plant_referral_approval),
    ("request_plant_referral_approval", request_plant_referral_approval),
    ("file_plant_referral_mr", file_plant_referral_mr),
    ("abandon_plant_referral", abandon_plant_referral),
)

check_node_registry(PLANT_REFERRAL_NODES, "the plant-referral node registry")

#: `route_plant_referral_gate`'s four answers, on two edges like `field_execution`'s
#: `HANDOVER_TARGETS`. `already_referred` ends the stage rather than escalating, for
#: `VISIT_TARGETS`' reason: an incident that already holds an MR arrived here by a route working as
#: designed, and the parent's edge runs it into `plant_execution`, the stage that owns an open MR.
REFERRAL_TARGETS: dict[str, str] = {
    "refer": "prepare_plant_referral_approval",
    "file": "file_plant_referral_mr",
    "abandon": "abandon_plant_referral",
    "already_referred": END,
}


def build_plant_referral_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every onward edge is guarded, for the reason the parent's are: `escalation_update` stops a node
    from doing work but does not stop the graph, so an unguarded edge would file an MR after the
    budget had been declared exhausted.

    The gate hangs off `evaluate_plant_referral` rather than off `START`, which is the subgraph
    contract's rule and not a style choice: `guarded` answers `ESCALATED` before it consults the
    router, and an edge from `START` would be evaluated against a state no node in this graph had
    written yet -- leaving the `ESCALATED` arm unreachable and therefore untestable.

    The edge out of `prepare_plant_referral_approval` is `straight_on`. There is nothing to decide
    between writing the question and raising it; the decision is what comes back.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in PLANT_REFERRAL_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, ENTRY_NODE)

    referral_map: dict[Any, str] = {**REFERRAL_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(ENTRY_NODE, guarded(route_plant_referral_gate), referral_map)
    graph.add_conditional_edges(
        "request_plant_referral_approval", guarded(route_plant_referral_gate), referral_map
    )

    graph.add_conditional_edges(
        "prepare_plant_referral_approval",
        guarded(straight_on),
        {ONWARD: "request_plant_referral_approval", ESCALATED: END},
    )
    graph.add_edge("file_plant_referral_mr", END)
    graph.add_edge("abandon_plant_referral", END)
    return graph


def compile_plant_referral_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission. A subgraph compiled as a node shares the
    parent's checkpointer -- LangGraph namespaces its state beneath the parent's thread -- and
    handing this one its own would give the incident two places to be resumed from.
    """
    return build_plant_referral_graph().compile(name="lpr_cpe_plant_referral")


__all__ = [
    "ENTRY_NODE",
    "PLANT_REFERRAL_NODES",
    "REFERRAL_TARGETS",
    "abandon_plant_referral",
    "build_plant_referral_graph",
    "compile_plant_referral_graph",
    "evaluate_plant_referral",
    "file_plant_referral_mr",
    "mr_reference",
    "plant_referral_packet",
    "prepare_plant_referral_approval",
    "receiving_owner",
    "referral_fault_domain",
    "referral_round",
    "referral_target",
    "request_plant_referral_approval",
    "route_plant_referral_gate",
]
