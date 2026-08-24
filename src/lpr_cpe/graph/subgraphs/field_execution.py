"""Stage 4's Clean Boots arm: turn the booking into a visit, and the visit into a handover.

This is P17, D16, D17, P18, D18, P19 and P20. `builder.PENDING_STAGES` named it as the thing every
exit of `field_planning` was waiting for: "nothing advances a work order once it exists.
`commit_field_dispatch` books one and the WFM returns it `requested`, which is why the status stays
`dispatch_planning` and no truck-roll KPI is emitted -- P17 is what turns a booking into a visit,
and P18/P19 the handover contract when Clean Boots hands to Dirty."

Where this stage stops, and why it is not P21
---------------------------------------------
P21, D19 and D20 are not here, and what stops them is the *entry decision*, not a missing vendor
capability. An earlier reading of this claimed otherwise and it was measured wrong; the correction
is kept here because the wrong reading is the more persuasive of the two.

That reading enumerated `route_plant_outcome`'s (D19's) three answers by MR status:

* `restored` needs an MR at `completed` or `closed`;
* `retry_diagnosis` needs one at `draft`, `rejected` or `cancelled`;
* `await_plant` is everything `MRRecord.awaiting_osp` covers.

and concluded that since `create_mr` writes `submitted` and -- as was true when that was written --
nothing called `update_mr`, D19 would answer `await_plant` for every incident and D20 would sit
behind an arm no state could enter.

**The enumeration is missing a state, and it is the one this stage reaches most often: no MR at
all**, which `current_mr_records` returns empty for and the router answers `retry_diagnosis`. Two of
the four ways out of this subgraph produce exactly that -- `abandon_handover` refuses the handover
before any MR is filed, and `route_visit_gate`'s `no_visit` never opened a visit -- so
`retry_diagnosis` is enterable with no new capability whatever. `restored` needs a revision at
`completed`, and the specification says where one comes from: P21 is a *capture* list -- acceptance,
assignment, dispatch, measurements, repair actions, components changed, photos, resolution code,
completion time, post-repair evidence -- which is the crew's own report. That is the same thing
`capture_field_evidence` takes for the Clean Boots half, through `interrupt()` with no adapter
fallback. The OSP-side status feed recorded as EXEC-2 is a real vendor gap and is not this one: it
would be a *second* channel into the same parser, exactly as FIELD-3 is for `field_submission`.

What blocked it was that this subgraph has **one exit and four things to say through it**.
`close_clean_boots_visit` writes `validating`, and D16's own specification text sends that case to
restoration validation; `file_plant_mr` writes `mr_raised` and belongs in the plant wait;
`abandon_handover` writes `diagnosing` and belongs back at P10; `no_visit` booked nothing at all.
`graph.builder` may only ask questions that are in `routing.DECISIONS`, so a local gate cannot
separate them on the parent's edge -- it has to be a numbered one. D16 is that decision, re-read on
the parent's edge from the same `FieldFinding` this stage already answered it from, and it is now
wired: `validate` reaches the restoration validation the specification names for it, and `delimit`
reaches `subgraphs.plant_execution`, which is P21/D19/D20 built on the reading above.

Eleven nodes, and why it is eleven
----------------------------------
* **A briefing is not a submission.** `open_field_visit` advances the work order and hands the crew
  what the specification's eleven briefing items say they get; `capture_field_evidence` pauses for
  what they send back. Splitting them is the same rule `graph.interrupts` states for approvals:
  everything before `interrupt()` re-runs on resume, so a node that briefed *and* waited would
  re-derive the briefing on every resume and re-stamp the visit's timestamps.
* **Delimiting is not handing over.** `determine_delimiter` promotes the boundary the technician
  found onto state, where D17 and `HandoverContract` both read it. It writes no packet, because the
  packet may not exist yet -- D17's whole job is to say so.
* **Evaluating is not committing.** The rule `remote_resolution` set and `field_planning` repeated:
  `ActionRequest` refuses `policy_outcome=BLOCKED` and refuses an approval-requiring outcome with no
  `approval_ref`, so the verdict cannot be recorded by the node that builds the request.
* **Asking is two nodes.** `prepare_handover_approval` writes the question and returns;
  `request_handover_approval` reads it back and raises.
* **"Come back with more" is not "OSP said no".** `request_additional_field_tests` and
  `abandon_handover` are different answers and must not be one node, and here the *lifecycle* forces
  it rather than merely recommending it. Measured against `domain.lifecycle.can_transition`:

      field_in_progress -> awaiting_handover  True
      field_in_progress -> awaiting_approval  False
      awaiting_handover -> field_in_progress  True
      awaiting_handover -> diagnosing         False
      awaiting_approval -> diagnosing         True
      awaiting_approval -> field_in_progress  False

  An incomplete packet is refused while the incident is `awaiting_handover`, from which only
  `field_in_progress` is legal. A refused *approval* is refused while it is `awaiting_approval`,
  from which only `diagnosing` is. The specification's two D18-failure destinations -- "return to
  diagnosis or Clean Boots evidence collection" -- are therefore not interchangeable: each failure
  has exactly one legal home, and one node cannot hold both.

The order of the policy evaluation, which the same table decides
-----------------------------------------------------------------
`evaluate_handover_policy` runs **before** P18, while the incident is still `field_in_progress`. The
tempting order is to build the packet first and put the finished thing to the engine, and it is
illegal: `prepare_approval` writes `awaiting_approval` unconditionally, `field_in_progress ->
awaiting_approval` is refused, so P18 must write `awaiting_handover` first -- and a policy *block*
discovered after that write would have nowhere legal to go, because `awaiting_handover ->
diagnosing` is refused too. Evaluating first means a blocked MR is still `field_in_progress` when
`abandon_handover` sends it to `diagnosing`.

Two gaps this stage closes, and one it only records
---------------------------------------------------
`HandoverContract.missing_items()` checks the technology's measurements, the delimiter reference, a
classified fault domain, a non-empty `ruled_out` and at least one finding id. It does **not** check
`access_notes` or `evidence_refs` -- and `SimulatedJTrackAdapter.REQUIRED_MR_FIELDS` is
`("plant_object_ref", "fault_description", "evidence_refs", "access_notes")`, refused
*non-retryably*. A `complete` contract could therefore be rejected at the adapter for a field the
contract's own audit never asked about. `build_handover_contract` fills both from the finding and
from the crew's access note, and `file_plant_mr` re-checks them against the same tuple before it
builds an `ActionRequest`, rather than by catching the adapter's exception.

What it cannot close: `missing_handover_fields` is public on the simulator for exactly this purpose,
but it is not on the `JTrackAdapter` protocol, so it cannot be reached through `ctx.adapters.jtrack`
under `mypy --strict`. `REQUIRED_MR_FIELDS` is imported instead, which keeps one owner of the list;
the protocol gap is gap JTRACK-3 in `docs/vendor-integration-gaps.md`.

And `WFMAdapter` has no method that advances a work order at all -- `create_work_order`,
`cancel_work_order`, `fetch_work_order`, and `fetch_work_order` reports only `requested` or
`cancelled`. So `en_route`, `on_site` and `completed` are written from the crew's own submission and
from the graph's decision about where the case goes, never read back from the WFM, and there is no
adapter fallback for a resume that carried nothing usable -- unlike `self_help`, which has
`fetch_customer_responses`. That is gap FIELD-3.

Where the parent cannot see this
--------------------------------
While `capture_field_evidence` or `request_handover_approval` is paused, the pause is in *this*
graph's checkpoint. `graph.inspect` reads through the boundary; the parent alone reports whatever
`field_planning` last left behind.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from lpr_cpe.domain.boundaries import is_plant_side
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    CrewType,
    DelimiterKind,
    FaultDomain,
    IncidentStatus,
    KPIName,
    ReasonCode,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import (
    DispatchRequirement,
    FieldFinding,
    HandoverContract,
    WorkOrder,
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
from lpr_cpe.graph.routing import (
    latest_decision_of,
    latest_field_finding,
    latest_policy_decision,
    route_clean_boots_outcome,
    route_delimiter_evidence,
    route_handover_validation,
)
from lpr_cpe.graph.state import (
    IncidentState,
    current_mr_records,
    current_work_orders,
    truck_roll_count,
)
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
from lpr_cpe.observability.kpi import MetricTimestamp, mark, stamp

# ------------------------------------------------------------------------------------------------
# Reading the incident for the visit
# ------------------------------------------------------------------------------------------------


def visit_round(state: IncidentState) -> int:
    """Which pass through `open_field_visit` the crew is on. One-based; zero before the first.

    Counted off `node_visits`, which `@node` writes last and refuses to let a body override, so it
    cannot be evaded by the node it bounds. `dispatch_round` says the rest of it: the count of
    *completed* passes, so a node downstream of the briefing reads the round that produced the
    finding it is holding, and the briefing itself has to add one.

    This is the discriminator on the `FieldFinding` id. `field_findings` reduces with
    `append_unique`, first-write-wins on the id, so a second visit whose id did not move would be
    silently dropped -- and `latest_field_finding` would keep answering D16 and D17 with the finding
    that had already been judged insufficient. The `more_tests` loop would then never terminate on
    anything but the guard.
    """
    return int(state.get("node_visits", {}).get("open_field_visit", 0))


def handover_round(state: IncidentState) -> int:
    """Which pass through `build_handover_contract` produced the packet now in state.

    Keyed the same way and for the same reason as `dispatch_round`: `approval_id_for` warns that
    "callers pass the relevant attempt counter", `approvals` de-duplicates on `approval_id`
    first-write-wins, and a packet rejected once and re-offered after another visit is two questions
    that must appear in the audit trail as two.

    Not `attempt_number(state, RAISE_MR)`, which counts actions that reached jTrack. A packet
    refused at D18 reached no adapter, so that counter does not move and the second question would
    derive the first question's id.
    """
    return int(state.get("node_visits", {}).get("build_handover_contract", 0))


def open_work_order(state: IncidentState) -> WorkOrder | None:
    """The order this visit is about: the latest revision that has not reached a terminal state.

    Read through `current_work_orders`, which collapses `work_orders`' revisions to the current view
    -- `len()` over the raw list counts status changes rather than orders, which is the mistake
    `_distinct_work_orders` exists to avoid on the other side of the boundary.

    Non-terminal is the whole of the test, and it is what makes the `more_tests` loop work. Only the
    three exits that end the visit write `completed` or `incomplete`; a crew asked for another
    reading stays `on_site`, and this keeps returning their order. Once one of the exits has run,
    every order is terminal and `route_visit_gate` answers `no_visit` -- which is also, without any
    special case for it, the correct answer for `queue_for_dispatcher` and `abandon_field_planning`,
    neither of which booked anything at all.

    The most recently created of the candidates, not the first. A reverse handover books a second
    order while the first is still open, and the visit being briefed is the later one.
    """
    candidates = [order for order in current_work_orders(state).values() if not order.terminal]
    if not candidates:
        return None
    return max(candidates, key=lambda order: order.created_at)


def outstanding_requests(state: IncidentState) -> list[str]:
    """What a previous pass asked the crew to go back and measure.

    Read off the audit trail rather than carried in a state field, for the reason
    `self_help._deadline_from` gives: the request was recorded by the node that made it, and a
    second home for it is a second thing that can disagree. Only the latest pass's list is returned
    -- an item supplied on the second visit is not still outstanding on the third.
    """
    for event in reversed(state.get("audit_events", [])):
        if event.node != "request_additional_field_tests":
            continue
        missing = event.detail.get("requested")
        if isinstance(missing, list):
            return [str(item) for item in missing]
        return []
    return []


# ------------------------------------------------------------------------------------------------
# The two questions this stage asks that the specification does not number
# ------------------------------------------------------------------------------------------------


def route_visit_gate(state: IncidentState) -> Literal["capture", "no_visit"]:
    """Is there a visit to brief? Local, because no numbered decision asks this.

    It exists because this stage is reached from every exit of `field_planning`, and only one of
    those three booked anything. `queue_for_dispatcher` handed the requirement to a human and
    `abandon_field_planning` gave up; both leave `work_orders` empty, and a stage that assumed a
    booking would open a visit against an order that does not exist.

    The same answer ends the stage after it has run. `open_work_order` tests non-terminal, and the
    three exits are the only nodes that write a terminal status, so a second entry after a completed
    handover finds nothing open and stops -- which is why this is not a check for "have we been here
    before". Those are different questions with the same answer today, and the one asked here is the
    one whose answer is still correct after Stage 4 gains its plant branch.
    """
    return "capture" if open_work_order(state) is not None else "no_visit"


def route_handover_gate(state: IncidentState) -> Literal["build_contract", "commit", "abandon"]:
    """Policy first, then the answer, then the packet. On two edges, like `route_dispatch_gate`.

    One router on both the edge out of `evaluate_handover_policy` and the edge out of
    `request_handover_approval`, and that is what makes the resumed pass correct rather than
    accidental: the node that raised the interrupt re-runs from its start, so the edge leaving it is
    evaluated twice against two different states, and a router that could only read the second would
    have nothing to say on the pass that pauses.

    The order of the three tests is the order of authority. `PolicyEngine` is the only thing that
    authorises an action, so a blocked decision is answered before the approval is looked for --
    otherwise an operator's stale `approved` from an earlier round would carry an MR the pack has
    since refused. Then the approval: absent means the question has not been put yet, which is the
    first pass and routes to the packet. Present and not `approved` means refused.

    There is deliberately no `approval_outstanding` clause. It reads like the missing case -- "the
    question is open, wait" -- and it is unreachable twice over: measured, that helper returns
    `False` when the pack records no demand at all, and when the pack does demand one, `interrupt()`
    means the pause *is* the wait and this edge is not evaluated until an answer exists. A branch no
    state can enter is a branch no test can hold to account.
    """
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    if decision is None or decision.blocked:
        return "abandon"
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)
    if answer is None:
        return "build_contract"
    return "commit" if answer.status is ApprovalStatus.APPROVED else "abandon"


# ------------------------------------------------------------------------------------------------
# Reading what the technician sent back
# ------------------------------------------------------------------------------------------------


def field_submission(answer: Any) -> dict[str, Any] | None:
    """The technician's submission as `FieldFinding` keywords, or `None` for nothing usable.

    Total and `None`-returning, for `customer_reply`'s reason: a resume with no payload, a timer
    tick, a garbled body all mean *we still do not know what the crew found*, and the loop that asks
    again is a better answer than a finding invented to fill the gap. `route_clean_boots_outcome`
    reads `None` as `delimit` and `route_delimiter_evidence` as `more_tests`, so an unusable
    submission costs one bounded lap rather than an exception in a node holding an open work order.

    A **contradictory** submission is also unusable, and that is the part worth arguing.
    `FieldFinding` refuses two combinations -- confirmed *and* no-fault-found, and plant work in a
    premises domain -- and both are checks this parser could satisfy by editing the crew's answer
    instead: drop `no_fault_found`, or promote the domain to `tap_or_odp`. Either would construct,
    and either would be this module deciding what a technician meant. The second is worse than the
    first, because `plant_object_ref` and then jTrack would file an MR against a boundary nobody
    reported. So the contradiction is rejected whole and the crew is asked again, which is the only
    step that can actually resolve it.

    Coercion is confined to shapes, never to meanings. Measurement values that are not numbers are
    dropped rather than defaulted -- a `downstream_snr_db` of `0.0` invented from `"n/a"` is a
    reading an OSP reviewer would act on -- and dropping them leaves the packet incomplete, which
    D18 already knows how to say.
    """
    if not isinstance(answer, dict):
        return None
    try:
        domain = FaultDomain(str(answer.get("fault_domain") or "").strip().lower())
    except ValueError:
        return None

    confirmed = bool(answer.get("fault_confirmed"))
    nothing_found = bool(answer.get("no_fault_found"))
    plant_work = bool(answer.get("requires_plant_work"))
    if confirmed and nothing_found:
        return None
    if plant_work and not is_plant_side(domain):
        return None

    try:
        kind = DelimiterKind(str(answer.get("delimiter_kind") or "unknown").strip().lower())
    except ValueError:
        kind = DelimiterKind.UNKNOWN

    raw_measurements = answer.get("measurements")
    measurements: dict[str, float] = {}
    if isinstance(raw_measurements, dict):
        for key, value in raw_measurements.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            measurements[str(key)] = float(value)

    reference = str(answer.get("delimiter_ref") or "").strip()
    return {
        "fault_domain": domain,
        "delimiter_kind": kind,
        "delimiter_ref": reference or None,
        "fault_confirmed": confirmed,
        "no_fault_found": nothing_found,
        "measurements": measurements,
        "technician_note": str(answer.get("technician_note") or ""),
        "parts_replaced": _strings(answer.get("parts_replaced")),
        "work_completed": bool(answer.get("work_completed")),
        "requires_plant_work": plant_work,
        "requires_permit": bool(answer.get("requires_permit")),
        "evidence_refs": _strings(answer.get("evidence_refs")),
        "recorded_by": str(answer.get("recorded_by") or "field_crew"),
    }


def _strings(raw: Any) -> tuple[str, ...]:
    """A submitted list as a tuple of non-empty strings. Anything else is an empty tuple."""
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


# ------------------------------------------------------------------------------------------------
# Ending the visit
# ------------------------------------------------------------------------------------------------


def _close_work_order(
    order: WorkOrder, *, status: WorkOrderStatus, code: str, at: datetime, note: str
) -> WorkOrder:
    """The order as it stands once the visit is over. A revision, not a mutation.

    `work_orders` reduces with `append_revision`, so an exit returns the whole order again with a
    terminal status on it and the reducer replaces the current view while keeping the history.
    `completed_at` is stamped for `incomplete` as well as `completed`: the field says when the visit
    ended, not whether it succeeded, and `MR_CYCLE_TIME_SECONDS` and the reconciliation report both
    read it as the former.

    `model_copy` and not a rebuilt `WorkOrder`, and the trade is deliberate. It bypasses validation
    -- so this helper must not be handed a status the lifecycle forbids -- but rebuilding would mean
    re-listing twenty fields at three call sites, and a field forgotten at one of them is a crew id
    or a `scheduled_start` silently dropped from the audit record of the visit. The three callers
    each pass a literal from `WorkOrderStatus`, which is the narrower thing to keep right.
    """
    return order.model_copy(
        update={
            "status": status,
            "updated_at": at,
            "completed_at": at,
            "completion_code": code,
            "notes": [*order.notes, note],
        }
    )


# ------------------------------------------------------------------------------------------------
# P17a -- the briefing pack
# ------------------------------------------------------------------------------------------------


def briefing(state: IncidentState) -> dict[str, Any]:
    """The eleven things P17 requires a technician be given, read from wherever each already lives.

    Eleven bullets, fourteen keys: the suspected domain is split from its confidence and prior work
    orders from prior MRs, and `case_type` is the one key no bullet asks for -- a crew sent by an
    alarm is being told something different from a crew sent by a customer's call.

    A function and not a node's local, because two nodes need it and for different purposes:
    `open_field_visit` records which items it could fill so the audit trail says what the crew was
    told, and `capture_field_evidence` puts it in the interrupt payload so they are actually told
    it. Building it twice would let the trail and the technician's screen disagree about the same
    visit.

    Every value is derived, none stored. There is no `briefing` field on `IncidentState` and there
    should not be: each item here has an owner already -- `events` the symptom, `rca` the suspected
    domain and the ruled-out causes, `topology` the tap or ODP, `test_results` the tests performed,
    `dispatch_requirements` the parts and skills -- and a stored copy would be a second version of
    eleven facts, going stale one field at a time.

    Ruled-out causes come from `RCAHypothesis.rejected` rather than a list somebody maintained.
    `rejection_reason` is carried with each, because "we ruled out the drop" is not actionable and
    "we ruled out the drop, the reflection is upstream of it" tells the crew where not to spend an
    hour.
    """
    rca = state.get("rca")
    topology = state.get("topology")
    sla = state.get("sla")
    impact = state.get("impact")
    case_type = state.get("case_type")
    status = state.get("status")
    technology = state.get("technology")
    requirements = state.get("dispatch_requirements", [])
    requirement = requirements[-1] if requirements else None
    events = state.get("events", [])
    orders = current_work_orders(state)

    return {
        "original_symptom": events[0].summary if events else "",
        "case_type": case_type.value if case_type is not None else None,
        "incident_state": status.value if status is not None else None,
        "nxt_evidence": [
            {"ref": item.ref, "kind": item.kind.value, "summary": item.summary}
            for item in state.get("evidence", [])
        ],
        "suspected_fault_domain": rca.fault_domain.value if rca is not None else None,
        "fault_domain_confidence": rca.confidence if rca is not None else None,
        "tests_performed": [
            {
                "kind": result.kind.value,
                "target_ref": result.target_ref,
                "status": result.status.value,
                "summary": result.summary,
            }
            for result in state.get("test_results", [])
        ],
        "ruled_out": _ruled_out(state),
        "required_tests": list(requirement.notes) if requirement is not None else [],
        "parts_and_tools": {
            "parts": list(requirement.parts_required) if requirement is not None else [],
            "equipment": list(requirement.equipment_required) if requirement is not None else [],
            "skills": list(requirement.skills_required) if requirement is not None else [],
            "permit_required": requirement.permit_required if requirement is not None else False,
        },
        "delimiter_topology": {
            "technology": technology.value if technology is not None else None,
            "delimiter_kind": topology.delimiter_kind.value if topology is not None else None,
            "delimiter_ref": topology.delimiter_ref if topology is not None else None,
            "node_ref": topology.node_ref if topology is not None else None,
            "cmts_ref": topology.cmts_ref if topology is not None else None,
            "olt_ref": topology.olt_ref if topology is not None else None,
            "pon_port_ref": topology.pon_port_ref if topology is not None else None,
            "primary_splitter_ref": topology.primary_splitter_ref if topology is not None else None,
            "odp_ref": topology.odp_ref if topology is not None else None,
            "homes_behind_delimiter": (
                topology.homes_behind_delimiter if topology is not None else None
            ),
        },
        "prior_work_orders": [
            {
                "work_order_id": order.work_order_id,
                "status": order.status.value,
                "visit_number": order.visit_number,
                "completion_code": order.completion_code,
            }
            for order in orders.values()
        ],
        "prior_mrs": [
            {
                "mr_id": record.mr_id,
                "external_ref": record.external_ref,
                "status": record.status.value,
                "plant_object_ref": record.plant_object_ref,
            }
            for record in current_mr_records(state).values()
        ],
        "success_criteria": {
            # What "resolved within the Clean Boots domain" means, spelled for the crew rather than
            # left as an inference from D16. The router reads `work_completed and not
            # requires_plant_work`, and this is that condition in words.
            "resolve_within_premises": (
                "work completed at the premises with no plant work required; "
                "if plant work is required, establish the exact tap or ODP instead"
            ),
            "restore_deadline": sla.restore_deadline().isoformat() if sla is not None else None,
            "affected_customers": impact.affected_customer_count if impact is not None else None,
            "outstanding_requests": outstanding_requests(state),
        },
    }


def _ruled_out(state: IncidentState) -> list[str]:
    """Rejected hypotheses, each with the reason it was rejected, in the order RCA recorded them.

    Through `RCAResult.ruled_out` rather than a second filter over `hypotheses`, because the model
    already owns that predicate and a copy of it here would be a second place to change. No fallback
    for a missing reason either: `RCAHypothesis._rejection_is_explained` refuses to construct a
    rejected hypothesis without one, so a clause for the empty string is one no state can enter.
    """
    rca = state.get("rca")
    if rca is None:
        return []
    return [f"{h.statement} ({h.rejection_reason})" for h in rca.ruled_out]


@node("open_field_visit")
async def open_field_visit(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Turn a booking into a visit: put the crew on site, and count the truck roll. P17a.

    This is where `commit_field_dispatch` deliberately stopped. That node books an order and the WFM
    returns it `requested`, which `WorkOrder.counted_as_truck_roll` does not count -- so
    `TRUCK_ROLLS_PER_INCIDENT` was not emitted there and could not be, because no truck had moved.
    Writing `on_site` here is what makes the count true, and it is why the KPI is emitted from this
    node and not from the one that made the booking.

    **Idempotent, and that is the whole reason the arrival stamps are conditional.** The
    `more_tests` loop re-enters this node with the same order still open, and
    `dispatched_at`/`on_site_at` must record when the crew first arrived, not when they were last
    asked for another reading. `metrics_timestamps` reduces with `merge_dict` -- last writer wins
    per key -- so an unconditional stamp would walk `ON_SITE_AT` forward on every lap and quietly
    shorten every duration derived from it.

    The KPI discriminator is the work order id, not the visit round. A second lap is the same truck
    at the same address, so `emit_kpi` collapsing the two is the correct arithmetic; a genuine
    second visit needs a second work order, which only another pass through `field_planning` can
    book.

    Nothing is written when no order is open. All three of `field_planning`'s exits lead here and
    only one of them booked anything, so this node has to be able to say "there is no visit" -- and
    saying it by writing `field_in_progress` anyway would leave a queued requirement claiming a crew
    was at the door. `route_visit_gate` reads the same absence and ends the stage.
    """
    order = open_work_order(state)
    now = ctx.clock.now()
    pack = briefing(state)

    if order is None:
        return {
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="open_field_visit",
                    action="open_field_visit",
                    outcome="no_open_work_order",
                    reason_code=ReasonCode.NO_FAULT_FOUND,
                    detail={
                        "work_orders": len(current_work_orders(state)),
                        "reason": (
                            "no non-terminal work order; the planning stage queued or abandoned "
                            "this requirement, or the visit has already ended"
                        ),
                    },
                    discriminator="no_open_work_order",
                )
            ],
        }

    round_number = visit_round(state) + 1
    arrived = order.on_site_at or now
    dispatched = order.dispatched_at or order.scheduled_start or now
    # `instructions` is left alone. `commit_field_dispatch` set it from the requirement's notes and
    # owns it; the briefing reaches the crew through the interrupt payload, where they can answer
    # it.
    on_site = order.model_copy(
        update={
            "status": WorkOrderStatus.ON_SITE,
            "updated_at": now,
            "dispatched_at": dispatched,
            "on_site_at": arrived,
        }
    )

    update: NodeUpdate = {
        "status": IncidentStatus.FIELD_IN_PROGRESS,
        "work_orders": [on_site],
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="open_field_visit",
                action="open_field_visit",
                outcome="on_site",
                subject_ref=order.work_order_id,
                reason_code=ReasonCode.CUSTOMER_ACCESS_REQUIRED,
                detail={
                    "work_order_id": order.work_order_id,
                    "external_ref": order.external_ref,
                    "crew_type": order.crew_type.value,
                    "crew_id": order.assigned_crew_id,
                    "visit_number": order.visit_number,
                    "round": round_number,
                    "dispatched_at": dispatched.isoformat(),
                    "on_site_at": arrived.isoformat(),
                    # The keys, not the values. The pack is large and every item in it already has
                    # an owner elsewhere in state; what the trail needs to show is which of the
                    # fourteen the crew could actually be given, so a briefing that arrived empty is
                    # visible.
                    "briefing_items": sorted(pack),
                    "briefing_gaps": sorted(key for key, value in pack.items() if not value),
                    "outstanding_requests": outstanding_requests(state),
                },
                discriminator=f"{order.work_order_id}:{round_number}",
            )
        ],
    }
    if MetricTimestamp.DISPATCHED_AT.value not in state.get("metrics_timestamps", {}):
        stamp(update, MetricTimestamp.DISPATCHED_AT, dispatched)
    if MetricTimestamp.ON_SITE_AT.value not in state.get("metrics_timestamps", {}):
        stamp(update, MetricTimestamp.ON_SITE_AT, arrived)
    # `preview`, not `state`: both KPIs are keyed on `truck_roll_count`, which counts orders whose
    # current status is a travelled one -- and the revision that makes this one travelled is in
    # `update`.
    seen = preview(state, update)
    update["kpi_events"] = [
        *emit_kpi(
            seen,
            ctx,
            KPIName.TRUCK_ROLLS_PER_INCIDENT,
            node="open_field_visit",
            dimensions={"crew_type": order.crew_type.value},
            discriminator=order.work_order_id,
        ),
        *emit_kpi(
            seen,
            ctx,
            KPIName.REPEAT_VISIT_RATE,
            node="open_field_visit",
            dimensions={"crew_type": order.crew_type.value},
            discriminator=order.work_order_id,
        ),
    ]
    return update


# ------------------------------------------------------------------------------------------------
# P17b -- what the crew found
# ------------------------------------------------------------------------------------------------

#: Every key a submission may carry, sent with the interrupt so the crew is asked for them by name.
#: Advisory rather than enforced: `field_submission` reads what arrives, and an item withheld leaves
#: the packet incomplete -- which is D18's answer to give, not this list's.
#:
#: P17 names thirteen things to capture and most map onto a model field: arrival and departure onto
#: `WorkOrder.dispatched_at`/`on_site_at`/`completed_at`, measurements onto
#: `FieldFinding.measurements`, components changed onto `parts_replaced`, disposition onto
#: `work_completed`/`fault_confirmed`/`no_fault_found`, actions taken onto `technician_note`.
#:
#: A measurement is keyed by the bare quantity name `REQUIRED_BY_TECHNOLOGY` lists --
#: `downstream_power_dbmv`, not `downstream_power_dbmv at tap 4`. `build_handover_contract` copies
#: these keys onto the contract unchanged and `HandoverContract.missing_items` tests them with `in`,
#: so a qualifier appended to a key leaves the required item missing and the packet permanently
#: incomplete. The unit is already in the name and the test point has its own home below, so nothing
#: is lost by keeping the key bare.
#:
#: Three P17 items have no modelled home: the last known clean point, the first known failed point,
#: and the customer's confirmation. `SUBMISSION_EXTRAS` is those three, and they are carried in the
#: audit event this node writes rather than in new state fields -- the same choice
#: `outstanding_requests` makes and for the same reason, that the node which recorded a fact is a
#: better owner of it than a second field that can drift. `_submission_extras` reads them back for
#: the handover packet, where P18 item 15 requires them.
SUBMISSION_FIELDS: tuple[str, ...] = (
    "fault_domain",
    "delimiter_kind",
    "delimiter_ref",
    "fault_confirmed",
    "no_fault_found",
    "work_completed",
    "requires_plant_work",
    "requires_permit",
    "measurements",
    "parts_replaced",
    "evidence_refs",
    "technician_note",
    "recorded_by",
)

#: The three P17 capture items no domain model holds. See `SUBMISSION_FIELDS`.
SUBMISSION_EXTRAS: tuple[str, ...] = (
    "last_clean_point",
    "first_failed_point",
    "customer_confirmed",
)


def submission_extras(answer: Any) -> dict[str, Any]:
    """The three unmodelled capture items, shaped for an audit event's `detail`.

    Separate from `field_submission` because the destination is different, not because the input is:
    that function returns `FieldFinding` keywords and this returns audit detail, and one function
    returning both would have to be unpacked by its caller into two places anyway.

    Never `None`. An absent extra is an absent extra -- it does not invalidate a submission whose
    measurements and disposition are sound, and D18 is where an incomplete packet is refused.
    """
    if not isinstance(answer, dict):
        return dict.fromkeys(SUBMISSION_EXTRAS)
    confirmed = answer.get("customer_confirmed")
    return {
        "last_clean_point": str(answer.get("last_clean_point") or "") or None,
        "first_failed_point": str(answer.get("first_failed_point") or "") or None,
        "customer_confirmed": confirmed if isinstance(confirmed, bool) else None,
    }


def _submission_extras(state: IncidentState) -> dict[str, Any]:
    """The latest submission's unmodelled items, read back off the trail that recorded them."""
    for event in reversed(state.get("audit_events", [])):
        if event.node != "capture_field_evidence" or event.outcome != "recorded":
            continue
        return {key: event.detail.get(key) for key in SUBMISSION_EXTRAS}
    return dict.fromkeys(SUBMISSION_EXTRAS)


@node("capture_field_evidence")
async def capture_field_evidence(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Pause for the technician's submission and record it as a `FieldFinding`. P17b.

    `interrupt()` with **no adapter fallback**, unlike `await_customer_response`, and the difference
    is measured rather than stylistic: `WFMAdapter` has four methods and none of them returns
    findings. `fetch_work_order` answers with a status that the simulator only ever sets to
    `requested` or `cancelled`, so a fallback written against it would be a branch that cannot
    produce a submission. That gap is recorded as FIELD-3 in `docs/vendor-integration-gaps.md`; when
    the WFM grows a completion feed, this is the node that gains the second channel, and
    `field_submission` is already the one parser both would go through.

    The order stays `on_site`. It is tempting to complete it here -- the crew has submitted, the
    visit looks over -- and it would break the `more_tests` loop silently: `open_work_order` tests
    non-terminal, so a completed order makes the next lap's `route_visit_gate` answer `no_visit` and
    the stage would end with an unanswered request for measurements. Only the three exits complete
    an order, and each of them is a place the visit genuinely ended.

    An unusable submission records no finding at all, and the routers already know what to do with
    that: `route_clean_boots_outcome` reads a missing finding as `delimit` and
    `route_delimiter_evidence` as `more_tests`, so the crew is asked again and the guard bounds how
    often. See `field_submission` for why a contradictory answer counts as unusable rather than
    being quietly corrected into one that constructs.
    """
    order = open_work_order(state)
    if order is None:
        raise ValueError(
            "capture_field_evidence was reached with no open work order. `route_visit_gate` sends "
            "that case to the end of the stage, so this edge cannot produce one."
        )

    round_number = visit_round(state)
    answer = interrupt(
        {
            "field_submission_request": {
                "incident_id": state.get("incident_id"),
                "work_order_id": order.work_order_id,
                "external_ref": order.external_ref,
                "crew_type": order.crew_type.value,
                "crew_id": order.assigned_crew_id,
                "visit_round": round_number,
                "on_site_at": order.on_site_at.isoformat() if order.on_site_at else None,
            },
            "briefing": briefing(state),
            "requested_items": [*SUBMISSION_FIELDS, *SUBMISSION_EXTRAS],
            "outstanding_requests": outstanding_requests(state),
        }
    )

    parsed = field_submission(answer)
    now = ctx.clock.now()
    if parsed is None:
        return {
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="capture_field_evidence",
                    action="capture_field_evidence",
                    outcome="unusable_submission",
                    subject_ref=order.work_order_id,
                    reason_code=ReasonCode.DATA_QUALITY_INSUFFICIENT,
                    detail={
                        "work_order_id": order.work_order_id,
                        "round": round_number,
                        "keys": sorted(answer) if isinstance(answer, dict) else [],
                        "reason": (
                            "the submission was absent, unparseable, or contradicted a finding "
                            "invariant; no finding was recorded and the crew will be asked again"
                        ),
                    },
                    discriminator=f"{order.work_order_id}:{round_number}",
                )
            ],
        }

    finding = FieldFinding(
        finding_id=derive_id(
            "FF", state.get("incident_id") or "", order.work_order_id, round_number
        ),
        work_order_id=order.work_order_id,
        incident_id=state.get("incident_id") or "",
        recorded_at=now,
        **parsed,
    )
    update: NodeUpdate = {
        "field_findings": [finding],
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="capture_field_evidence",
                action="capture_field_evidence",
                outcome="recorded",
                subject_ref=order.work_order_id,
                reason_code=(
                    ReasonCode.PHYSICAL_FAULT_CONFIRMED
                    if finding.fault_confirmed
                    else ReasonCode.NO_FAULT_FOUND
                    if finding.no_fault_found
                    else ReasonCode.RCA_LOW_CONFIDENCE
                ),
                detail={
                    "finding_id": finding.finding_id,
                    "work_order_id": order.work_order_id,
                    "round": round_number,
                    "fault_domain": finding.fault_domain.value,
                    "delimiter_kind": finding.delimiter_kind.value,
                    "delimiter_ref": finding.delimiter_ref,
                    "fault_confirmed": finding.fault_confirmed,
                    "no_fault_found": finding.no_fault_found,
                    "work_completed": finding.work_completed,
                    "requires_plant_work": finding.requires_plant_work,
                    "requires_permit": finding.requires_permit,
                    "measurements": sorted(finding.measurements),
                    "parts_replaced": list(finding.parts_replaced),
                    "evidence_refs": list(finding.evidence_refs),
                    "recorded_by": finding.recorded_by,
                    "supplied": sorted(parsed),
                    # The three items no model holds. This event is their owner; see
                    # `SUBMISSION_EXTRAS` for why they are not state fields.
                    **submission_extras(answer),
                },
                discriminator=finding.finding_id,
            )
        ],
    }
    # `preview`, not `state`: `no_fault_found_rate` reads every finding on the incident and the one
    # that decides the answer is in `update`.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.NO_FAULT_FOUND_RATE,
        node="capture_field_evidence",
        dimensions={"fault_domain": finding.fault_domain.value},
        discriminator=finding.finding_id,
    )
    return update


# ------------------------------------------------------------------------------------------------
# D16 -- resolved inside the Clean Boots domain
# ------------------------------------------------------------------------------------------------


@node("close_clean_boots_visit")
async def close_clean_boots_visit(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """The crew fixed it at the premises. Complete the order and hand the incident to validation.

    `validating` and not `restored`. The same rule `verify_self_help` keeps: the crew's disposition
    is a claim about the premises, and only Stage 5's stability window turns a claim into a
    restoration. So this node ends the *visit* and nothing else, which is also why it emits no
    `FIRST_TIME_FIX_RATE` -- that KPI is keyed on a closure record this incident does not have yet.

    `completion_code` is `resolved_at_premises`, spelled to match what D16 actually asked:
    `work_completed and not requires_plant_work`. "Resolved" alone would be read by the
    reconciliation report as a claim about the service.
    """
    order = open_work_order(state)
    finding = latest_field_finding(state)
    if order is None or finding is None:
        raise ValueError(
            "close_clean_boots_visit was reached without an open work order and a finding. D16 is "
            "evaluated on the edge out of `capture_field_evidence`, which records the finding "
            "against the order this node closes."
        )

    now = ctx.clock.now()
    completed = _close_work_order(
        order,
        status=WorkOrderStatus.COMPLETED,
        code="resolved_at_premises",
        at=now,
        note=f"resolved within the Clean Boots domain: {finding.fault_domain.value}",
    )
    return {
        "status": IncidentStatus.VALIDATING,
        "work_orders": [completed],
        "fault_domain": finding.fault_domain,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="close_clean_boots_visit",
                action="close_field_visit",
                outcome="resolved_at_premises",
                subject_ref=order.work_order_id,
                reason_code=ReasonCode.PHYSICAL_FAULT_CONFIRMED,
                detail={
                    "work_order_id": order.work_order_id,
                    "finding_id": finding.finding_id,
                    "fault_domain": finding.fault_domain.value,
                    "parts_replaced": list(finding.parts_replaced),
                    "visit_number": order.visit_number,
                    "round": visit_round(state),
                },
                discriminator=finding.finding_id,
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# The delimiter, and asking for what is missing
# ------------------------------------------------------------------------------------------------


@node("determine_delimiter")
async def determine_delimiter(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Promote the boundary the crew established onto the incident. Nothing else.

    D17 reads the *finding*, so this node is not what decides whether the boundary is good enough --
    it copies what the crew reported up to `delimiter`/`delimiter_ref`, where `plant_object_ref`,
    the handover contract and every downstream reader look for it. Without the promotion those
    readers would each have to reach back into `field_findings`, which is how a tap identifier comes
    to exist in two places and differ in one.

    The status stays `field_in_progress`. The crew is still at the address; the decision about
    whether their reading places the fault beyond the boundary happens on the edge out of here, and
    one of its two answers sends them back to work.

    An empty reading is copied as an empty reading. Falling back to `topology.delimiter_ref` here
    would be this node substituting the planning stage's guess for the crew's measurement, and D17
    would then see a delimiter that nobody stood next to.

    No reading *at all* is a third case and not an error; see the body for why it is reachable.
    """
    now = ctx.clock.now()
    finding = latest_field_finding(state)
    if finding is None:
        # The first visit whose submission was unusable. D16 answers `delimit` on a missing finding
        # and there is no earlier one to fall back on, so this is a state the stage genuinely
        # reaches -- recording the gap and letting D17 answer `more_tests` is what
        # `field_submission` promises when it returns `None`, and it costs one bounded lap instead
        # of an exception in a node holding an open work order. Nothing is written to `delimiter` or
        # `fault_domain`: there is no reading, and `UNKNOWN` there would be this node answering for
        # the crew.
        return {
            "status": IncidentStatus.FIELD_IN_PROGRESS,
            "updated_at": now,
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="determine_delimiter",
                    action="determine_delimiter",
                    outcome="no_submission",
                    reason_code=ReasonCode.POLICY_EVIDENCE_INSUFFICIENT,
                    detail={
                        "requested": _missing_for_delimiting(None),
                        "round": visit_round(state),
                    },
                    discriminator=f"no_submission:{visit_round(state)}",
                )
            ],
        }

    topology = state.get("topology")
    update: NodeUpdate = {
        "status": IncidentStatus.FIELD_IN_PROGRESS,
        "delimiter": finding.delimiter_kind,
        "fault_domain": finding.fault_domain,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="determine_delimiter",
                action="determine_delimiter",
                outcome=(
                    "delimited"
                    if finding.delimiter_ref and finding.delimiter_kind is not DelimiterKind.UNKNOWN
                    else "not_delimited"
                ),
                subject_ref=finding.delimiter_ref,
                reason_code=(
                    ReasonCode.PLANT_FAULT_CONFIRMED
                    if finding.requires_plant_work
                    else ReasonCode.RCA_LOW_CONFIDENCE
                ),
                detail={
                    "finding_id": finding.finding_id,
                    "delimiter_kind": finding.delimiter_kind.value,
                    "delimiter_ref": finding.delimiter_ref,
                    "fault_domain": finding.fault_domain.value,
                    "requires_plant_work": finding.requires_plant_work,
                    "plant_side": is_plant_side(finding.fault_domain),
                    "topology_delimiter_ref": (
                        topology.delimiter_ref if topology is not None else None
                    ),
                },
                discriminator=finding.finding_id,
            )
        ],
    }
    if finding.delimiter_ref:
        # Conditional because `delimiter_ref` is a plain last-write-wins field: an empty reading
        # written over a reference an earlier lap established would erase evidence rather than fail
        # to add any. The `delimiter` kind is written unconditionally -- `UNKNOWN` there is the
        # crew's actual answer and D17 reads it as one.
        update["delimiter_ref"] = finding.delimiter_ref
    return update


@node("request_additional_field_tests")
async def request_additional_field_tests(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Name what is missing and send the crew back for it. Two arrivals, one node.

    D17's "request missing tests" and D18's "return to Clean Boots evidence collection" are the same
    action on different evidence, so they are one node and the audit event's `arrival` says which
    asked. Two nodes would be two copies of the loop back to the briefing, and the second copy is
    always the one that gets a fix late.

    Which one asked is decided by whether a packet was built from the evidence currently in hand --
    `finding.finding_id in contract.field_finding_ids` -- and **not** by whether a contract exists
    at all. The presence test is the obvious version and it is wrong after the first rejection: the
    contract is a plain last-write-wins field, so it survives the lap that rejected it, and a later
    D17 arrival would then be filed as a D18 rejection. The reference test is exact, because a
    contract naming an older finding is by definition a packet that the evidence has since moved
    past.

    What is missing is read from whichever source knows: the contract's own `missing_items()` when a
    packet was built and rejected, and otherwise the three clauses D17 tests. Neither list is
    re-derived here from scratch -- `HandoverContract.missing_items` owns the first and would
    disagree with a shorter local copy the moment a required measurement is added.

    `outstanding_requests` reads this event back on the next lap, which is why the list goes in
    `detail["requested"]` and not into a state field of its own. The request was made here; a second
    home for it is a second thing that can disagree about what is still owed.

    The loop is bounded by nothing this node does. `@node` calls `check_budgets` on entry, and
    `max_subgraph_reentries` stops the sixth re-entry of the briefing -- so the escalation happens
    at the node being re-entered rather than being counted here, which is why there is no attempt
    counter in this body.
    """
    contract = state.get("handover_contract")
    finding = latest_field_finding(state)
    now = ctx.clock.now()

    rejected = contract is not None and (
        finding is None or finding.finding_id in contract.field_finding_ids
    )
    if contract is not None and rejected:
        arrival, requested = "d18_reject", contract.missing_items()
        reason = ReasonCode.HANDOVER_REJECTED_INCOMPLETE
    else:
        arrival, reason = "d17_insufficient", ReasonCode.POLICY_EVIDENCE_INSUFFICIENT
        requested = _missing_for_delimiting(finding)

    return {
        "status": IncidentStatus.FIELD_IN_PROGRESS,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="request_additional_field_tests",
                action="request_additional_field_tests",
                outcome=arrival,
                subject_ref=finding.finding_id if finding is not None else None,
                reason_code=reason,
                detail={
                    "arrival": arrival,
                    "requested": requested,
                    "finding_id": finding.finding_id if finding is not None else None,
                    "contract_id": contract.contract_id if contract is not None else None,
                    "completeness": contract.completeness if contract is not None else None,
                    "round": visit_round(state),
                },
                discriminator=f"{arrival}:{visit_round(state)}",
            )
        ],
    }


def _missing_for_delimiting(finding: FieldFinding | None) -> list[str]:
    """What D17 wanted and did not find, in the same order that router tests for it."""
    if finding is None:
        return ["field_finding"]
    missing: list[str] = []
    if not finding.requires_plant_work:
        missing.append("plant_work_disposition")
    if finding.delimiter_kind is DelimiterKind.UNKNOWN:
        missing.append("delimiter_kind")
    if not finding.delimiter_ref:
        missing.append("delimiter_ref")
    return missing


# ------------------------------------------------------------------------------------------------
# The policy verdict on filing an MR
# ------------------------------------------------------------------------------------------------


@node("evaluate_handover_policy")
async def evaluate_handover_policy(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Put the MR to the policy engine **before** the packet is built. P19's authorisation.

    The order is forced by `domain.lifecycle`, and this is the one place in the stage where a
    seemingly cosmetic sequencing choice is load-bearing. `abandon_handover` writes `diagnosing`;
    measured against `can_transition`, `awaiting_handover -> diagnosing` is **False** while
    `field_in_progress -> diagnosing` is True. P18 writes `awaiting_handover`. So an evaluation
    placed after the packet would leave a policy-blocked MR sitting in a status from which the only
    honest destination is unreachable, and the reducer -- `advance_status` raises rather than warns
    -- would fail the run.

    Evaluating first also means the engine is asked about the action rather than about the
    paperwork, which is the right question: nothing the packet contains changes whether this role
    may raise an MR at this blast radius at this hour.

    The decision id is re-keyed from the incident, the action and the **finding**. `PolicyEngine
    .evaluate` mints a `uuid4`, so an unkeyed decision appears to `append_unique` as a new one on
    every replay; keying on the finding rather than on the delimiter is what makes a re-evaluation
    after another visit visible, since that is the thing which changed.
    """
    finding = latest_field_finding(state)
    if finding is None:
        raise ValueError(
            "evaluate_handover_policy was reached with no field finding. D17 routes a missing "
            "finding to `more_tests`, so the `handover` edge cannot produce one."
        )

    target_ref = plant_object_ref(state, finding)
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
                finding.finding_id,
                verdict.outcome.value,
            )
        }
    )

    update: NodeUpdate = {
        "policy_decisions": [decision],
        "updated_at": ctx.clock.now(),
        "audit_events": [
            audit(
                state,
                ctx,
                node="evaluate_handover_policy",
                action="evaluate_handover_policy",
                outcome=decision.outcome.value,
                subject_ref=target_ref,
                reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                detail={
                    "finding_id": finding.finding_id,
                    "action_type": ActionType.RAISE_MR.value,
                    "plant_object_ref": target_ref,
                    "attempt": attempt_number(state, ActionType.RAISE_MR),
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
        node="evaluate_handover_policy",
        dimensions={"action_type": ActionType.RAISE_MR.value},
        discriminator=decision.decision_id,
    )
    return update


# ------------------------------------------------------------------------------------------------
# P18 -- the handover contract
# ------------------------------------------------------------------------------------------------


def handover_packet(state: IncidentState, finding: FieldFinding) -> dict[str, Any]:
    """P18's twenty-four required items, numbered as the specification numbers them.

    The document and the *model* are deliberately not the same thing. `HandoverContract` holds what
    D18 validates -- `missing_items()` is its own audit of measurements, delimiter, domain,
    ruled-out causes and finding references -- and this holds everything an OSP reviewer is entitled
    to see, which is nine items wider than any field on that model. Putting all twenty-four on the
    model would mean nineteen new fields that no router reads and that `completeness` would then
    have to decide whether to count.

    So the packet travels where a reader needs it: the audit event P18 writes, the approval payload
    P19 puts to a human, and the MR parameters P20 sends to jTrack. One function, three readers, and
    a numbered key each so a reviewer can check the specification against the payload item by item.

    Nothing here is stored. Every value is read from whichever field already owns it, which is why
    an incident that never had a `topology` resolved produces a packet with `None` against item 7
    rather than a packet that silently omits item 7 -- `build_handover_contract` records exactly
    which of the twenty-four came back empty, and that is what an operator needs to see before
    approving.
    """
    topology = state.get("topology")
    sla = state.get("sla")
    impact = state.get("impact")
    rca = state.get("rca")
    cpe = state.get("cpe")
    technology = state.get("technology")
    requirements = state.get("dispatch_requirements", [])
    requirement = requirements[-1] if requirements else None
    orders = current_work_orders(state)
    extras = _submission_extras(state)

    return {
        "01_incident_id": state.get("incident_id"),
        # Item 2 is "the unchanged SLA clock", and the deadlines are what make it checkable: both
        # are derived from `clock_started_at`, which intake writes once, so a handover cannot reset
        # the clock by restating it. The stored fields are durations, not instants.
        "02_sla_clock": {
            "clock_started_at": sla.clock_started_at.isoformat() if sla is not None else None,
            "restore_deadline": sla.restore_deadline().isoformat() if sla is not None else None,
            "response_deadline": sla.response_deadline().isoformat() if sla is not None else None,
            "paused_intervals": len(sla.paused_intervals) if sla is not None else 0,
        },
        "03_technology": technology.value if technology is not None else None,
        "04_delimiter_ref": plant_object_ref(state, finding),
        "05_address_and_gis": {
            "latitude": topology.latitude if topology is not None else None,
            "longitude": topology.longitude if topology is not None else None,
            "mdu_ref": topology.mdu_ref if topology is not None else None,
            "area_archetype": (
                topology.area_archetype.value
                if topology is not None and topology.area_archetype is not None
                else None
            ),
        },
        "06_identifiers": {
            "customer_ref": state.get("customer_ref"),
            "product_ref": state.get("product_ref"),
            "service_ref": state.get("service_ref"),
            "cpe_ref": state.get("cpe_ref"),
            "cpe_model": cpe.model if cpe is not None else None,
        },
        "07_network_context": {
            "node_ref": topology.node_ref if topology is not None else None,
            "cmts_ref": topology.cmts_ref if topology is not None else None,
            "service_group_ref": topology.service_group_ref if topology is not None else None,
            "olt_ref": topology.olt_ref if topology is not None else None,
            "pon_port_ref": topology.pon_port_ref if topology is not None else None,
            "primary_splitter_ref": topology.primary_splitter_ref if topology is not None else None,
            "split_ratio": topology.split_ratio if topology is not None else None,
            "headend_ref": topology.headend_ref if topology is not None else None,
        },
        "08_fault_domain": finding.fault_domain.value,
        "09_fault_domain_confidence": rca.confidence if rca is not None else None,
        "10_evidence_refs": sorted({*finding.evidence_refs, *(rca.evidence_refs if rca else ())}),
        "11_ruled_out": _ruled_out(state),
        "12_nxt_snapshot": [
            {
                "ref": item.ref,
                "kind": item.kind.value,
                "observed_at": item.observed_at.isoformat(),
                "summary": item.summary,
            }
            for item in state.get("evidence", [])
        ],
        "13_actions_attempted": [
            {
                "action_type": record.action_type.value,
                "outcome": record.outcome.value,
                "target_ref": record.target_ref,
                "attempt": record.attempt,
            }
            for record in state.get("action_history", [])
        ],
        "14_field_measurements": dict(finding.measurements),
        "15_clean_and_failed_point": {
            "last_clean_point": extras["last_clean_point"],
            "first_failed_point": extras["first_failed_point"],
        },
        "16_photos": [dict(photo) for photo in finding.photos],
        "17_parts_used": sorted(
            {*finding.parts_replaced, *(part for o in orders.values() for part in o.parts_used)}
        ),
        "18_required_skill_and_equipment": {
            "skills": list(requirement.skills_required) if requirement is not None else [],
            "equipment": list(requirement.equipment_required) if requirement is not None else [],
            "permit_required": finding.requires_permit,
        },
        "19_customer_access_required": (
            requirement.customer_access_required if requirement is not None else None
        ),
        "20_priority_and_sla": {
            "severity": impact.severity.value if impact is not None else None,
            "product_tier": sla.product_tier if sla is not None else None,
            "vulnerable_customer": sla.vulnerable_customer if sla is not None else None,
            "affected_customer_count": (
                impact.affected_customer_count if impact is not None else None
            ),
            "sla_at_risk_count": impact.sla_at_risk_count if impact is not None else None,
        },
        "21_deduplication": {
            # What we know before P20 asks jTrack. The authoritative answer comes from
            # `fetch_open_mrs` at the moment of filing; this is the incident's own record of what it
            # already raised, which is what a human approving the handover can see.
            "known_mrs": [
                {"mr_id": r.mr_id, "external_ref": r.external_ref, "status": r.status.value}
                for r in current_mr_records(state).values()
            ],
            "idempotency_key": mr_idempotency_key(state, plant_object_ref(state, finding)),
            "affected_delimiter_refs": (
                list(impact.affected_delimiter_refs) if impact is not None else []
            ),
        },
        "22_prior_records": {
            "linked_records": dict(state.get("linked_records", {})),
            "work_orders": [
                {"work_order_id": o.work_order_id, "status": o.status.value}
                for o in orders.values()
            ],
        },
        "23_repeat_visit_count": truck_roll_count(state),
        "24_recommended_plant_action": _recommended_action(finding, extras),
    }


def _recommended_action(finding: FieldFinding, extras: dict[str, Any]) -> str:
    """Item 24, composed from the crew's own findings rather than inferred by a model.

    An MR whose recommended action is a sentence generated from a fault domain is an MR an OSP
    engineer learns to ignore. This is the technician's note and the boundary they established,
    joined -- and when they wrote no note, the boundary alone, which is at least true.
    """
    where = f"{finding.delimiter_kind.value} {finding.delimiter_ref}".strip()
    parts = [
        f"inspect and repair at {where}" if finding.delimiter_ref else "establish the boundary"
    ]
    if extras["first_failed_point"]:
        parts.append(f"first failed point {extras['first_failed_point']}")
    if finding.technician_note:
        parts.append(f"field note: {finding.technician_note}")
    if finding.requires_permit:
        parts.append("a permit is required")
    return "; ".join(parts)


@node("build_handover_contract")
async def build_handover_contract(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Assemble the Clean-to-Dirty packet and put the incident into `awaiting_handover`. P18.

    The contract carries what D18 validates and the packet carries all twenty-four items; see
    `handover_packet` for why those are two objects and not one. What this node adds on top of both
    is the two fields a reviewer needs and no model derives: `access_notes`, so a Dirty Boots crew
    knows how to reach the object, and `safety_notes`, so they know what they are walking into.

    Neither is allowed to be empty, and that is a decision about MR rejections rather than about
    tidiness. `REQUIRED_MR_FIELDS` includes `access_notes`, and `jtrack.simulator.create_mr` raises
    a **non-retryable** `AdapterError` on a missing one -- so an empty string here becomes a dead
    incident at P20, two nodes and one human approval later. Composed from the topology and the
    requirement, with the delimiter as the floor, they are always something an engineer can act on.

    `contract_id` is keyed on the finding, not on the round. Two rounds that produced the same
    finding are the same packet; a new visit produces a new finding and so a new packet, which is
    what makes `field_finding_ids` a usable answer to "which evidence was this built from" in
    `request_additional_field_tests`.
    """
    finding = latest_field_finding(state)
    if finding is None:
        raise ValueError(
            "build_handover_contract was reached with no field finding. `route_handover_gate` only "
            "reaches here from a policy decision, and `evaluate_handover_policy` cannot produce "
            "one without a finding."
        )

    now = ctx.clock.now()
    packet = handover_packet(state, finding)
    topology = state.get("topology")
    requirements = state.get("dispatch_requirements", [])
    requirement = requirements[-1] if requirements else None
    contract = HandoverContract(
        contract_id=derive_id("HOC", state.get("incident_id") or "", finding.finding_id),
        incident_id=state.get("incident_id") or "",
        created_at=now,
        from_crew_type=CrewType.CLEAN,
        to_crew_type=CrewType.DIRTY,
        technology=packet["03_technology"] or "unknown",
        fault_domain=finding.fault_domain,
        delimiter_kind=finding.delimiter_kind,
        delimiter_ref=plant_object_ref(state, finding) or None,
        measurements=dict(finding.measurements),
        ruled_out=_ruled_out(state),
        photos=[dict(photo) for photo in finding.photos],
        access_notes=mr_access_notes(state, finding),
        safety_notes=_safety_notes(finding, requirement),
        field_finding_ids=[finding.finding_id],
        evidence_refs=list(packet["10_evidence_refs"]),
    )

    missing = contract.missing_items()
    return {
        "status": IncidentStatus.AWAITING_HANDOVER,
        "handover_contract": contract,
        "delimiter": finding.delimiter_kind,
        "fault_domain": finding.fault_domain,
        **mark(MetricTimestamp.HANDOVER_AT, now),
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="build_handover_contract",
                action="build_handover_contract",
                outcome="complete" if contract.complete else "incomplete",
                subject_ref=contract.delimiter_ref,
                reason_code=(
                    ReasonCode.PLANT_FAULT_CONFIRMED
                    if contract.complete
                    else ReasonCode.HANDOVER_REJECTED_INCOMPLETE
                ),
                detail={
                    "contract_id": contract.contract_id,
                    "finding_id": finding.finding_id,
                    "round": handover_round(state) + 1,
                    "completeness": contract.completeness,
                    "missing_items": missing,
                    # The item numbers that came back empty, so a reviewer can check the packet
                    # against the specification's list without reading the whole payload.
                    "packet_gaps": sorted(key for key, value in packet.items() if not value),
                    "packet": packet,
                    "topology_source": (topology.topology_source if topology is not None else None),
                },
                discriminator=contract.contract_id,
            )
        ],
    }


def _safety_notes(finding: FieldFinding, requirement: DispatchRequirement | None) -> str:
    """What the receiving crew is walking into. Never empty; see `build_handover_contract`."""
    parts: list[str] = []
    if finding.requires_permit:
        parts.append("a permit is required before work starts")
    if requirement is not None:
        if requirement.weather_sensitive:
            parts.append("weather sensitive")
        if requirement.equipment_required:
            parts.append(f"equipment: {', '.join(requirement.equipment_required)}")
    if not parts:
        parts.append("no specific hazard was reported by the Clean Boots crew")
    return "; ".join(parts)


# ------------------------------------------------------------------------------------------------
# P19 -- the approval for the change of responsibility domain
# ------------------------------------------------------------------------------------------------


@node("prepare_handover_approval")
async def prepare_handover_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write the question down, with the eight things P19 requires in it, then return. P19a.

    The first half of the pair `graph.interrupts` describes, and the same shape as
    `prepare_dispatch_approval`: the whole question is built here so `requested_at` is stamped once
    however many times the node that raises replays.

    `ApprovalKind.CLEAN_TO_DIRTY_HANDOVER` is named directly rather than read from
    `decision.required_approval_kind`, for `prepare_dispatch_approval`'s reason:
    `route_handover_gate` asks `latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)`
    literally, so a question asked under any other kind would be answered and then not seen. The
    pack's demand is still recorded in the audit detail, which is where a disagreement between the
    two would show up.

    `attempt=handover_round(state)` and not `attempt_number(state, RAISE_MR)`. A packet refused
    here reached no adapter, so the action counter does not move and a second question would derive
    the first one's id -- which `approvals` de-duplicates away, first-write-wins, leaving the second
    refusal invisible.
    """
    contract = state.get("handover_contract")
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    finding = latest_field_finding(state)
    if contract is None or decision is None or finding is None:
        raise ValueError(
            "prepare_handover_approval was reached without a contract, a policy decision and a "
            "finding. D18 is evaluated on the edge out of `build_handover_contract`, which is "
            "downstream of `evaluate_handover_policy` and cannot run without the finding."
        )

    round_number = handover_round(state)
    packet = handover_packet(state, finding)
    target_ref = plant_object_ref(state, finding)
    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.CLEAN_TO_DIRTY_HANDOVER,
        question=(
            f"Approve handing responsibility for {state.get('incident_id')} from Clean Boots to "
            f"Dirty Boots at {target_ref}, and raise a jTrack MR against it? This is handover "
            f"proposal {round_number} for the incident."
        ),
        attempt=round_number,
        action_type=ActionType.RAISE_MR,
        target_ref=target_ref,
        recommendation=packet["24_recommended_plant_action"],
        risk_summary=decision.explanation,
        blast_radius=packet["20_priority_and_sla"]["affected_customer_count"],
        # An MR is not reversible in the sense this flag means: the OSP work it schedules is
        # physical. `remote_resolution` uses the option's own flag; there is no option here, and
        # asserting `True` would understate the decision the approver is being asked to make.
        reversible=False,
        policy_decision_id=decision.decision_id,
        context={
            # P19's eight required items, each named as the specification names it.
            "incident": state.get("incident_id"),
            "current_domain": CrewType.CLEAN.value,
            "proposed_domain": CrewType.DIRTY.value,
            "confidence": packet["09_fault_domain_confidence"],
            "missing_evidence": contract.missing_items(),
            "existing_mr_result": packet["21_deduplication"],
            "crew_and_equipment_requirement": packet["18_required_skill_and_equipment"],
            "sla_impact": packet["20_priority_and_sla"],
            # Beyond the eight, so the approval screen can show the packet the decision rests on.
            "contract_id": contract.contract_id,
            "completeness": contract.completeness,
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
                node="prepare_handover_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=target_ref,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "approval_id": request.approval_id,
                    "kind": ApprovalKind.CLEAN_TO_DIRTY_HANDOVER.value,
                    "round": round_number,
                    "contract_id": contract.contract_id,
                    "completeness": contract.completeness,
                    "required_role": request.required_role,
                    "policy_required_approval": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind
                        else None
                    ),
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_handover_approval")
async def request_handover_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Builds nothing; see `graph.interrupts`."""
    return request_approval(state, ctx)


# ------------------------------------------------------------------------------------------------
# P20 -- the MR
# ------------------------------------------------------------------------------------------------


@node("file_plant_mr")
async def file_plant_mr(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Assemble the Clean Boots handover package and file the MR against it. P20, one of two ways.

    The *mechanism* is not here. `_mr.submit_mr` owns the duplicate-suppression read, the
    `REQUIRED_MR_FIELDS` re-check, the `ActionRequest`, the `MRRecord`, the `ActionRecord` and the
    `mr_raised` write, because P20 has two entrances -- "the handover evidence, or the NOC/plant
    evidence package when the case reached this step directly from D08 without a Clean Boots visit"
    -- and `plant_execution` reads the MR back off whichever of them wrote it. Two filers assembling
    that record differently would give D19 two different answers to "is this still with OSP?" and
    nothing would fail. See that module's docstring for the rest of the argument.

    What is left here is the part only a Clean Boots handover has, and it is four things.

    **The package.** `parameters` is built from the `HandoverContract` a technician's finding
    produced: the measurements they took, the causes they ruled out, their access and safety notes,
    the crew id that raised it. The NOC-direct filer has none of that and says so; that difference
    is the specification's and not an accident to be factored away.

    **The contract's acceptance.** Marked here, and the justification is who approved it. The pack
    requires `Role.OSP_ENGINEER` for `CLEAN_TO_DIRTY_HANDOVER` -- the approver *is* the receiving
    owner -- so `accepted_by` is that person and not a guess about what OSP will later say.
    `MRStatus.SUBMITTED` still means OSP has not accepted the *MR*; the two acceptances are
    different facts and the simulator's docstring is explicit about keeping them apart. It is
    stamped at `submission.action.completed_at` rather than a fresh `ctx.clock.now()`, so the
    acceptance and the filing carry the one instant.

    **The work order.** Completed with `handed_to_osp`, because the Clean Boots visit genuinely is
    over -- and unlike `resolved_at_premises`, that code makes no claim about the service. The
    NOC-direct path has no work order at all, which is the other half of why this cannot be shared.

    **The handover KPIs.** All three return `None` until `contract.accepted is not None`, so they
    are emitted over `preview` and only from the path that has a contract at all.
    """
    contract = state.get("handover_contract")
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    finding = latest_field_finding(state)
    order = open_work_order(state)
    if contract is None or decision is None or finding is None:
        raise ValueError(
            "file_plant_mr was reached without a contract, a policy decision and a finding. "
            "`route_handover_gate` reaches here only from an approved decision, which cannot exist "
            "without all three."
        )

    target_ref = plant_object_ref(state, finding)
    packet = handover_packet(state, finding)
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)
    accepted_by = answer.decided_by if answer is not None else ctx.automation_actor

    submission = await submit_mr(
        state,
        ctx,
        node_name="file_plant_mr",
        parameters={
            "plant_object_ref": target_ref,
            "fault_description": packet["24_recommended_plant_action"],
            "evidence_refs": list(contract.evidence_refs),
            "access_notes": contract.access_notes,
            "safety_notes": contract.safety_notes,
            "crew_type_required": CrewType.DIRTY.value,
            "priority": mr_severity(state).value,
            "homes_affected": packet["20_priority_and_sla"]["affected_customer_count"],
            "suspected_fault_class": finding.fault_domain.value,
            "raised_by_crew_id": order.assigned_crew_id if order is not None else None,
            "contract_id": contract.contract_id,
            "handover_completeness": contract.completeness,
            "measurements": dict(contract.measurements),
            "ruled_out": list(contract.ruled_out),
        },
        target_ref=target_ref,
        fault_domain=finding.fault_domain,
        decision=decision,
        approval=answer,
        discriminator=contract.contract_id,
        blast_radius=packet["20_priority_and_sla"]["affected_customer_count"],
        notes=[f"raised from handover contract {contract.contract_id}"],
        evidence_refs=tuple(contract.evidence_refs),
        detail={
            "contract_id": contract.contract_id,
            "completeness": contract.completeness,
            "accepted_by": accepted_by,
        },
        refusal_hint=(
            "D18 requires `HandoverContract.complete` before the approval is asked for, and "
            "`build_handover_contract` guarantees non-empty access notes, so this combination "
            f"should be unreachable -- see the contract's `missing_items()`: "
            f"{contract.missing_items()}"
        ),
    )

    completed_at = submission.completed_at
    update = submission.update
    update["handover_contract"] = contract.model_copy(
        update={"accepted": True, "accepted_at": completed_at, "accepted_by": accepted_by}
    )
    update["linked_records"] = {
        **update["linked_records"],
        "handover_contract": contract.contract_id,
    }
    if order is not None:
        update["work_orders"] = [
            _close_work_order(
                order,
                status=WorkOrderStatus.COMPLETED,
                code="handed_to_osp",
                at=completed_at,
                note=f"handed to OSP as {mr_reference(submission.record)}",
            )
        ]
    # `preview`, not `state`: both handover KPIs return `None` until `contract.accepted is not
    # None`, and the copy that sets it is in `update`.
    seen = preview(state, update)
    update["kpi_events"] = [
        *emit_kpi(
            seen,
            ctx,
            KPIName.HANDOVER_ACCEPTANCE_RATE,
            node="file_plant_mr",
            dimensions={"fault_domain": finding.fault_domain.value},
            discriminator=contract.contract_id,
        ),
        *emit_kpi(
            seen,
            ctx,
            KPIName.HANDOVER_REWORK_RATE,
            node="file_plant_mr",
            dimensions={"fault_domain": finding.fault_domain.value},
            discriminator=contract.contract_id,
        ),
        *emit_kpi(
            seen,
            ctx,
            KPIName.PLANT_REPAIR_BACKLOG,
            node="file_plant_mr",
            dimensions={"fault_domain": finding.fault_domain.value},
            discriminator=submission.record.mr_id,
        ),
    ]
    return update


# ------------------------------------------------------------------------------------------------
# The way out that files nothing
# ------------------------------------------------------------------------------------------------


@node("abandon_handover")
async def abandon_handover(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Policy refused the MR, or a human did. Record the refusal and send the case to diagnosis.

    Two arrivals, one node, and here the lifecycle is what allows it: `abandon_handover` writes
    `diagnosing`, and measured against `can_transition` both of its arrival statuses reach it --
    `field_in_progress -> diagnosing` when `evaluate_handover_policy` blocked, and
    `awaiting_approval -> diagnosing` when the approver refused. `awaiting_handover -> diagnosing`
    is **False**, which is the whole reason the policy verdict is taken before P18 rather than
    after.

    `accepted=False` with a reason code, because `route_handover_validation` reads exactly that on
    any later pass -- and because `handover_rework_rate` is `None` until `accepted` is set either
    way. A refused handover that left the field `None` would be a handover that never happened as
    far as the dashboard is concerned, which is the opposite of what a rework metric is for.

    The work order is completed `incomplete` and not `cancelled`. The crew went, and
    `WorkOrder.counted_as_truck_roll` counts both -- but `cancelled` does not, and a refused
    handover that erased the truck roll from `TRUCK_ROLLS_PER_INCIDENT` would improve the metric by
    failing.

    `diagnosing` and not `escalated`. A refused MR is a case that needs re-diagnosis with the field
    evidence added, which is what the specification asks for at D19 and is the more useful answer
    here too; the guard is what escalates, when the loop has genuinely run out.
    """
    decision = latest_policy_decision(state, ActionType.RAISE_MR)
    answer = latest_decision_of(state, ApprovalKind.CLEAN_TO_DIRTY_HANDOVER)
    contract = state.get("handover_contract")
    finding = latest_field_finding(state)
    order = open_work_order(state)
    now = ctx.clock.now()

    if decision is not None and decision.blocked:
        arrival, outcome = "policy_blocked", ActionOutcome.BLOCKED_BY_POLICY.value
        reason = (
            decision.reason_codes[0]
            if decision.reason_codes
            else ReasonCode.POLICY_NO_MATCHING_RULE
        )
        explanation = decision.explanation
    elif answer is not None:
        arrival, outcome = "approval_refused", answer.status.value
        reason = answer.reason_code or ReasonCode.HANDOVER_REJECTED_WRONG_DOMAIN
        explanation = answer.rationale
    else:
        # `route_handover_gate` sends a *missing* decision here too, which is the state an incident
        # reaches only if it entered the stage mid-way. Named rather than folded into either branch
        # above, because the repair is upstream and a reader needs to see that.
        arrival, outcome = "no_policy_decision", ActionOutcome.SKIPPED.value
        reason = ReasonCode.POLICY_EVIDENCE_INSUFFICIENT
        explanation = "the handover gate was reached with no policy decision for raise_mr"

    update: NodeUpdate = {
        "status": IncidentStatus.DIAGNOSING,
        "pending_approval": None,
        "updated_at": now,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_handover",
                action="abandon_handover",
                outcome=arrival,
                subject_ref=contract.delimiter_ref if contract is not None else None,
                reason_code=reason,
                detail={
                    "arrival": arrival,
                    "action_outcome": outcome,
                    "contract_id": contract.contract_id if contract is not None else None,
                    "completeness": contract.completeness if contract is not None else None,
                    "finding_id": finding.finding_id if finding is not None else None,
                    "policy_decision_id": decision.decision_id if decision is not None else None,
                    "policy_outcome": decision.outcome.value if decision is not None else None,
                    "approval_id": answer.approval_id if answer is not None else None,
                    "approval_status": answer.status.value if answer is not None else None,
                    "explanation": explanation,
                },
                discriminator=f"{arrival}:{handover_round(state)}",
            )
        ],
    }
    if contract is not None:
        update["handover_contract"] = contract.model_copy(
            update={
                "accepted": False,
                "rejection_reason": reason,
                "rejection_detail": explanation,
            }
        )
    if order is not None:
        update["work_orders"] = [
            _close_work_order(
                order,
                status=WorkOrderStatus.INCOMPLETE,
                code="handover_refused",
                at=now,
                note=f"handover refused: {arrival}",
            )
        ]
    if contract is not None:
        seen = preview(state, update)
        update["kpi_events"] = [
            *emit_kpi(
                seen,
                ctx,
                KPIName.HANDOVER_ACCEPTANCE_RATE,
                node="abandon_handover",
                dimensions={"arrival": arrival},
                discriminator=contract.contract_id,
            ),
            *emit_kpi(
                seen,
                ctx,
                KPIName.HANDOVER_REWORK_RATE,
                node="abandon_handover",
                dimensions={"arrival": arrival},
                discriminator=contract.contract_id,
            ),
        ]
    return update


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The eleven nodes, in the order the specification walks them. Checked the same way as
#: `PARENT_NODES`, so a node registered under a name its decorator does not carry fails on import
#: rather than producing a graph whose topology and audit trail disagree.
FIELD_EXECUTION_NODES: tuple[tuple[str, Any], ...] = (
    ("open_field_visit", open_field_visit),
    ("capture_field_evidence", capture_field_evidence),
    ("close_clean_boots_visit", close_clean_boots_visit),
    ("determine_delimiter", determine_delimiter),
    ("request_additional_field_tests", request_additional_field_tests),
    ("evaluate_handover_policy", evaluate_handover_policy),
    ("build_handover_contract", build_handover_contract),
    ("prepare_handover_approval", prepare_handover_approval),
    ("request_handover_approval", request_handover_approval),
    ("file_plant_mr", file_plant_mr),
    ("abandon_handover", abandon_handover),
)

check_node_registry(FIELD_EXECUTION_NODES, "the field-execution node registry")

#: `route_visit_gate`'s two answers. `no_visit` ends the stage rather than escalating: two of
#: `field_planning`'s three exits reach here having deliberately booked nothing, and an escalation
#: would turn a dispatcher queue into an incident nobody queued.
VISIT_TARGETS: dict[str, str] = {
    "capture": "capture_field_evidence",
    "no_visit": END,
}

#: D16's two answers.
CLEAN_BOOTS_TARGETS: dict[str, str] = {
    "validate": "close_clean_boots_visit",
    "delimit": "determine_delimiter",
}

#: D17's three. `escalate` is wired to `END` and is unreachable through `guarded`, which answers
#: `ESCALATED` first on exactly the state -- `escalated` set -- that this router reads for it. It is
#: still mapped, because `add_conditional_edges` raises on an unmapped return value and the router's
#: signature promises three.
DELIMITER_TARGETS: dict[str, str] = {
    "handover": "evaluate_handover_policy",
    "more_tests": "request_additional_field_tests",
    "escalate": END,
}

#: D18's two, renamed onto this stage's nodes. `reject` is the *same* node D17's `more_tests` uses;
#: `request_additional_field_tests` reads which edge it arrived on off the contract.
VALIDATION_TARGETS: dict[str, str] = {
    "request_approval": "prepare_handover_approval",
    "reject": "request_additional_field_tests",
}

#: `route_handover_gate`'s three. Attached to two edges, like `field_planning`'s `DISPATCH_TARGETS`.
HANDOVER_TARGETS: dict[str, str] = {
    "build_contract": "build_handover_contract",
    "commit": "file_plant_mr",
    "abandon": "abandon_handover",
}


def build_field_execution_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every onward edge is guarded, for the reason the parent's are: `escalation_update` stops a node
    from doing work but does not stop the graph, so an unguarded edge would file an MR after the
    budget had been declared exhausted.

    The gate hangs off `open_field_visit` rather than off `START`, and that is
    `build_field_requirement`'s precedent rather than a stylistic echo. A conditional edge from
    `START` would carry an `ESCALATED` arm that nothing can take -- the parent's edge into this
    subgraph is already guarded, so an escalated incident never arrives -- whereas an edge out of
    node one can take it, because `@node` calls `check_budgets` on entry and returns
    `escalation_update` when a bound has already fired.

    `context_schema=GraphContext` is repeated rather than inherited: a compiled subgraph is a graph
    in its own right, so `get_runtime(GraphContext)` inside its nodes resolves against *its* schema.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in FIELD_EXECUTION_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "open_field_visit")

    visit_map: dict[Any, str] = {**VISIT_TARGETS, ESCALATED: END}
    graph.add_conditional_edges("open_field_visit", guarded(route_visit_gate), visit_map)

    clean_map: dict[Any, str] = {**CLEAN_BOOTS_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "capture_field_evidence", guarded(route_clean_boots_outcome), clean_map
    )

    delimiter_map: dict[Any, str] = {**DELIMITER_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "determine_delimiter", guarded(route_delimiter_evidence), delimiter_map
    )

    # Back to the briefing. Guarded like every other edge, so a loop that exhausted
    # `max_subgraph_reentries` stops here rather than re-entering the node that would escalate.
    graph.add_conditional_edges(
        "request_additional_field_tests",
        guarded(straight_on),
        {ONWARD: "open_field_visit", ESCALATED: END},
    )

    handover_map: dict[Any, str] = {**HANDOVER_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "evaluate_handover_policy", guarded(route_handover_gate), handover_map
    )
    graph.add_conditional_edges(
        "request_handover_approval", guarded(route_handover_gate), handover_map
    )

    validation_map: dict[Any, str] = {**VALIDATION_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "build_handover_contract", guarded(route_handover_validation), validation_map
    )
    graph.add_conditional_edges(
        "prepare_handover_approval",
        guarded(straight_on),
        {ONWARD: "request_handover_approval", ESCALATED: END},
    )

    graph.add_edge("close_clean_boots_visit", END)
    graph.add_edge("file_plant_mr", END)
    graph.add_edge("abandon_handover", END)
    return graph


def compile_field_execution_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission. A subgraph compiled as a node shares the
    parent's checkpointer -- LangGraph namespaces its state beneath the parent's thread -- and
    handing this one its own would give the incident two places to be resumed from.
    """
    return build_field_execution_graph().compile(name="lpr_cpe_field_execution")


__all__ = [
    "CLEAN_BOOTS_TARGETS",
    "DELIMITER_TARGETS",
    "FIELD_EXECUTION_NODES",
    "HANDOVER_TARGETS",
    "SUBMISSION_EXTRAS",
    "SUBMISSION_FIELDS",
    "VALIDATION_TARGETS",
    "VISIT_TARGETS",
    "abandon_handover",
    "briefing",
    "build_field_execution_graph",
    "build_handover_contract",
    "capture_field_evidence",
    "close_clean_boots_visit",
    "compile_field_execution_graph",
    "determine_delimiter",
    "evaluate_handover_policy",
    "field_submission",
    "file_plant_mr",
    "handover_packet",
    "handover_round",
    "mr_idempotency_key",
    "mr_policy_input",
    "open_field_visit",
    "open_work_order",
    "outstanding_requests",
    "plant_object_ref",
    "prepare_handover_approval",
    "request_additional_field_tests",
    "request_handover_approval",
    "route_handover_gate",
    "route_visit_gate",
    "submission_extras",
    "visit_round",
]
