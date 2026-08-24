"""Stage 3's field branch: turn the chosen repair into a scheduled visit, or say why there is none.

This is P14, D13, P15, D14, D15 and P16 -- the largest unwired arm in the graph. Measured over the
simulator, 52 of the 82 fixture runs (41 services x two case types) reach it. This paragraph used to
say 50, because it counted two of the three arms that arrive here:

| arm | runs | which |
| --- | --- | --- |
| `D11:field_planning` | 49 | 29 proactive, 20 predictive |
| `D12:field_planning` | 1 | proactive |
| `D20:reverse_handover` | 2 | proactive |

The arm that went missing is the one `builder.BRANCH_TARGETS` argues for at length immediately above
its own table, and it does not behave like the other two. `SVC-PO-042-A-04` and `SVC-UT-001-A-03`
are diverted at `D08:plant_path` before D09 is ever asked, so D11 and D12 never see them; they
arrive from `plant_execution` by way of `D19:restored`, and they arrive six times each. Every count
above is the same under either crew answer.

So it is not true that everything here is downstream of a router that has already answered "no
remote repair and no self-help; somebody has to drive to it". 50 of the 52 are. The reverse-handover
pair is downstream of one that answered "the plant work is finished, now book the customer half" --
a different question that happens to need the same stage. Of the 52, the 40 named further down reach
the dispatch gate and commit a visit, and none reach `queue_for_dispatcher`. How many also pass
through `abandon_field_planning` is the one figure a crew answer moves: 12 when the submission hands
over to plant, all 52 when it closes at the premises.

Why P14 does not use `is_field_option`
--------------------------------------
`routing.is_field_option` is `option.requires_truck_roll`, and it had no caller. Using it would have
been the obvious reading of "the field branch picks the field option", and it is wrong for a third
of the arrivals. Measured at the edge -- snapshotting state at the moment LangGraph evaluates D11
rather than re-deriving it afterwards, because a later node consumes the option the router read:

    SVC-SJ-011-A-01  pred  D11:field_planning  first=raise_mr
                     all=[raise_mr->TAP-SJ-011-A | create_work_order->TAP-SJ-011-A]

    --- action_type of the first field option ---
      create_work_order              24
      raise_mr                       16
      no untried field option        10
    --- fault_domain -> crew at the edge ---
      drop -> clean                  24
      tap_or_odp -> joint            16
      unknown                         9
      customer_environment            1

The denominator is 50: the runs in which D11 answers `field_planning` at least once, one more than
the 49 in the table above, because the run that arrives first through D12 is asked D11 later too.
The third row is a correction. Without it the tables read as exhaustive at 24 + 16 = 40, and a
reader reconciling that against the arrival count would think ten runs had gone missing; what those
ten have is no untried field option at the edge at all.

Read per arrival instead of per run the tables stop being stable: the same sweep gives 97 arrivals
under a handover submission and 297 under a premises one, because the stage is re-entered on the
way round, and `raise_mr` moves from 16 to 96 with it. The first-arrival reading is identical under
both crew answers, which is why it is the one recorded.

The 16 `raise_mr` arrivals are the joint ones. `decision_services.resolution` offers a `TAP_OR_ODP`
fault two options -- the MR first, because "the delimiter is plant, so OSP owns the repair", and the
work order second as "the customer half of a joint visit" -- and both carry `requires_truck_roll`,
which is honest: an MR causes plant work. So `is_field_option` returns the MR, and this subgraph
cannot commit an MR. `wfm.create_work_order` refuses any other action type by name, and
`route_dispatch_approval` reads `latest_policy_decision(state, ActionType.CREATE_WORK_ORDER)`
literally, so a selected MR would sail past the approval gate unasked and then crash at the adapter.

`is_dispatchable_option` therefore narrows to the action type this subgraph can actually commit, and
every joint arrival carries one. The MR is Stage 5's business -- the jTrack request and the
Clean-to-Dirty handover -- and `builder.PENDING_STAGES` names that as the owner.

Eight nodes, and why it is eight
--------------------------------
* **Requiring is not scheduling.** `build_field_requirement` decides *what the visit needs*;
  `optimize_field_schedule` decides *who does it and when*. `dispatch.optimizer` documents that
  separation as the thing that lets the solve be a pure function of `DispatchProblem`, and folding
  the two would put an adapter call inside the object the approval replay has to reproduce exactly.
* **Selecting is not executing.** The same argument `remote_resolution` makes: `ActionRequest`
  refuses `policy_outcome=BLOCKED` and refuses to be built without an `approval_ref` when the
  outcome is `REQUIRES_APPROVAL`, so the evaluation cannot happen inside the node building it.
  `evaluate_dispatch_policy` records the verdict; `commit_field_dispatch` builds the request.
* **Asking is two nodes.** `prepare_dispatch_approval` writes the question and returns;
  `request_dispatch_approval` reads it back and raises. See `graph.interrupts`.
* **"No slot" is not "no dispatch".** `queue_for_dispatcher` and `abandon_field_planning` are
  different answers and must not be one node. A requirement nobody could schedule is still a
  requirement -- it goes to a human with the blocking `ConstraintCode` attached, and the incident
  stays in `dispatch_planning`. An incident with nothing to dispatch at all has left this branch,
  and goes back to `diagnosing` for another pass.

Two local routers, and why neither duplicates the one it wraps
--------------------------------------------------------------
D13, D14 and D15 live in `graph.routing` and are imported, not re-implemented -- `_check_tables`
only validates decisions in the parent's `BRANCH_TARGETS`, so a decision wired inside a subgraph is
fine, and `preventive_maintenance` already does this with `route_preventive_disposition`. D14 is
wired verbatim. The other two are wrapped, and in both cases because the delegated router answers a
question whose node cannot honour it:

* `route_field_gate` asks first whether P14 selected anything. `route_dispatch_type` reads
  `fault_domain` alone, so an incident whose plan offered no work-order option still gets `clean` or
  `joint` from it -- and `optimize_field_schedule` would then solve for a requirement that does not
  exist. The wrapper answers `escalate`, which is D13's own word for "no crew should be sent".
* `route_dispatch_gate` asks first whether the policy engine allowed it. `route_dispatch_approval`
  presupposes an evaluated action: its fall-through reads a `PolicyDecision` and, finding none,
  answers `approve_dispatch` -- so a *blocked* dispatch would be put to a human as though the pack
  permitted it. Blocked and unevaluated both queue for the dispatcher instead, which is why this
  router has a fourth answer that D15 does not.

Like `route_remote_gate`, `route_dispatch_gate` is attached to **two** edges -- out of the policy
evaluation and out of the gate -- because after evaluating and after an answer the question is the
identical one, *may this dispatch be committed now?*

Rejection re-plans, and how that terminates
--------------------------------------------
D15's third answer is `replan`, and its docstring owns what that means: "The dispatcher refusing a
slot is refusing *that* slot; re-optimising is the response, and it is what produces the later
policy evaluation that re-opens the gate through `approval_outstanding`." So `replan` returns to
P15, not to the dispatcher queue.

That loop terminates only because two derived ids advance with it, and both had to be keyed
deliberately:

* `approval_outstanding` compares `max(answers) < max(demands)`, where a demand is a
  `PolicyDecision` carrying `required_approval_kind`. `policy_decisions` de-duplicates on
  `decision_id`, so a POL id derived from the incident and the option alone would collapse the
  second evaluation into the first, and the demand's timestamp would never advance past the
  rejection. The id therefore includes the plan id.
* `approvals` de-duplicates on `approval_id`, first-write-wins, and `approval_id_for` says exactly
  what to do about it: "a dispatch rejected once and re-proposed with a different crew is two
  questions and must appear in the audit trail as two. Callers pass the relevant attempt counter."
  The counter here is the dispatch round, not `attempt_number` -- a rejected dispatch reached no
  adapter, so the action attempt does not move, and the second refusal would be silently dropped.

`dispatch_round` reads `node_visits`, which `@node` owns and no body may override. Beyond that the
loop is bounded by `policy.attempt_limits.max_subgraph_reentries`, which is what stops a dispatcher
who rejects everything from spinning.

Why P16 does not write `field_in_progress`
-------------------------------------------
`wfm.create_work_order` returns `status=requested`. Nobody has been dispatched, nobody is on site,
and `WorkOrder.counted_as_truck_roll` deliberately excludes `REQUESTED` for that reason. Two things
follow, and they are the same fact twice:

* The status stays `DISPATCH_PLANNING`. `domain.lifecycle` does not permit
  `awaiting_approval -> field_in_progress` at all, so the honest write is also the only legal one --
  a no-op arriving straight from the constraint check, a real move back from an approved gate.
* `MetricTimestamp.DISPATCHED_AT` is **not** stamped, for the same reason.
  `KPIName.TRUCK_ROLLS_PER_INCIDENT` is not emitted either: it counts `counted_as_truck_roll`, which
  is `False` for everything this node writes, so it would report 0.0 for every dispatched incident
  and the average would be dragged to zero by the very incidents that caused the trucks. Stage 4
  advances the work order's status, and that is where both belong.

`FIRST_ACTION_AT` *is* stamped, but only when it is absent. `metrics_timestamps` reduces with
`merge_dict`, which is last-writer-wins per key, so an unconditional stamp would move "first"
forward on any incident that had already tried a remote repair.

What state cannot supply, and what that costs
----------------------------------------------
Five of the optimizer's twelve constraints can refuse something this stage produces: `CREW_TYPE`,
`WORKING_HOURS`, `GEOGRAPHY`, `REMOTE_ACCESS_WINDOW` and `CAPACITY`. The other seven cannot, and the
reasons are not the same reason -- six are short of a fact and one is simply not applicable yet. All
seven are recorded as gap FIELD-1 in `docs/vendor-integration-gaps.md` rather than papered over with
a plausible default, because a fabricated part number would refuse real crews:

* **`SKILL`**, **`PARTS`** and **`EQUIPMENT`** -- `DispatchRequirement.skills_required`,
  `parts_required` and `equipment_required` are read in five places across `dispatch/` and written
  nowhere in `src`. Nothing in state names what a visit needs: `ResolutionOption.parameters` is
  empty for all 40 measured field options. `EQUIPMENT` is dead from both ends, because
  `wfm.fetch_crew_availability` returns no `carried_equipment` either -- see `_crew_slot`.
* **`SAFETY`** -- `check_safety` returns early unless `JobContext.aerial_work_required`, which is
  never set anywhere in `src`, and nothing distinguishes aerial from buried plant. Setting it from
  the fault domain would be a guess, and passing `wind_kph` without it would be a reading nothing
  reads.
* **`CUSTOMER_ACCESS`** is the different case, handled rather than merely absent: see
  `build_field_requirement`, and gap FIELD-2.
* **`BUILDING_ACCESS`** and **`WORK_ORDER_DEPENDENCY`** are argued where they are left unset, in
  `_job_context`. The second is the one omission on this list that is not a gap: one requirement per
  incident means there is no predecessor to be unmet.

Where the parent cannot see this
--------------------------------
While `request_dispatch_approval` is paused, `pending_approval` and `status=awaiting_approval` are
in *this* graph's checkpoint and not the parent's. `graph.inspect.pending_approval_for` reads
through the boundary; the parent alone reports the incident as `diagnosing` -- measured at that
pause on all 40 fixture runs that reach the gate.

Not `dispatch_planning`, which is the plausible guess and the wrong one. This module writes that
status at three sites and every one of them is on this side of the boundary, so the parent cannot
be holding it while the gate is open: the write that would set it is precisely what a paused
subgraph has not delivered.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lpr_cpe.dispatch.constraints import ConstraintCode, JobContext, blocking_code
from lpr_cpe.dispatch.optimizer import DispatchProblem, solve_dispatch
from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    CrewType,
    DelimiterKind,
    FaultDomain,
    IncidentStatus,
    KPIName,
    PolicyOutcome,
    ReasonCode,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import CrewSlot, DispatchPlan, DispatchRequirement, WorkOrder
from lpr_cpe.domain.governance import ActionRecord, ActionRequest, PolicyDecision
from lpr_cpe.domain.resolution import ResolutionOption
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.interrupts import build_request, prepare_approval, request_approval
from lpr_cpe.graph.nodes._runtime import (
    Freshness,
    Gathered,
    NodeUpdate,
    audit,
    check_node_registry,
    derive_id,
    emit_kpi,
    node,
    preview,
)
from lpr_cpe.graph.routing import (
    first_actionable_option,
    is_field_option,
    latest_decision_of,
    latest_policy_decision,
    route_dispatch_approval,
    route_dispatch_constraints,
    route_dispatch_type,
)
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.graph.subgraphs._shared import (
    attempt_number,
    idempotency_key_for,
    policy_input_for,
)
from lpr_cpe.observability.kpi import MetricTimestamp, mark, stamp

# ------------------------------------------------------------------------------------------------
# Reading the incident for the optimizer
# ------------------------------------------------------------------------------------------------


def is_dispatchable_option(option: ResolutionOption) -> bool:
    """A field option this subgraph can actually commit: one that ends in a work order.

    Narrower than `is_field_option`, which is `requires_truck_roll` and returns the jTrack MR for
    every joint arrival. See the module docstring for the measurement and for what selecting the MR
    would do at the adapter. Built on `is_field_option` rather than beside it so that a future
    action type which needs a truck is refused here by *this* function's second clause, and not by
    silently disagreeing with the class the router used.
    """
    return is_field_option(option) and option.action_type is ActionType.CREATE_WORK_ORDER


def selected_field_option(state: IncidentState) -> ResolutionOption | None:
    """The option `build_field_requirement` chose, or `None` if it chose nothing.

    A wrapper over `plan.selected`, for the reason `remote_resolution.selected_remote_option` gives:
    re-deriving the selection in a later reader is a defect rather than a duplication, because
    `first_actionable_option` skips options whose latest decision is blocked and
    `evaluate_dispatch_policy` may have just recorded exactly such a decision.

    Unlike its two siblings it also checks the *class* of what it read, and that is not belt and
    braces. `selected_option_id` is one field shared by all three resolution branches, and this is
    the only branch that can be entered after another has already written it: D12's `field_planning`
    arm is reached from `self_help`, which selects an option of its own on the way past. Measured
    over the fixture set, exactly one incident arrives that way -- `SVC-SJ-011-B-01`, proactive --
    and it arrives with a self-help option sitting in `selected_option_id` and no dispatchable
    option for P14 to replace it with.

    Without the class check that stale selection is returned here as though this subgraph had
    chosen it. Today the gate still escalates, because `route_field_gate` also requires a
    requirement and there is none -- but that is the requirement clause covering for this one, and
    it stops covering the moment a second round leaves a requirement behind. What follows then is
    `commit_field_dispatch` building an `ActionRequest` with `action_type=send_self_help` and
    `wfm.create_work_order` raising `AdapterError`, which is precisely the failure the module
    docstring argues about for `raise_mr`. The same argument, one field further along.

    The check is `is_dispatchable_option` and not `first_actionable_option`, so it refuses only what
    this subgraph could never commit. A work-order option the policy engine has just *blocked* still
    comes back, which is what `route_dispatch_gate` needs to route it to the dispatcher's queue.
    """
    plan = state.get("resolution_plan")
    option = plan.selected if plan is not None else None
    if option is None or not is_dispatchable_option(option):
        return None
    return option


def dispatch_round(state: IncidentState) -> int:
    """Which pass through P15 produced the plan now in state. One-based; zero before the first.

    Counted off `node_visits`, which `@node` writes last and refuses to let a body override, so it
    cannot be evaded by the node it bounds. This is the discriminator that makes a re-proposed
    dispatch a *second* question rather than a replay of the first -- see the module docstring for
    the two ids that depend on it and for what each collapses into if it does not move.

    The count of *completed* passes, not of the next one, and the difference was a measured defect
    rather than a matter of taste. `@node` writes the visit after the body returns, so a node
    downstream of P15 reads the round that built the plan it is holding, while P15 itself -- which
    is mid-pass and not yet counted -- has to add one. An earlier draft folded that `+ 1` in here,
    which made the downstream readers off by one against their own plan: `prepare_dispatch_approval`
    asked an
    operator to "approve dispatch proposal 2" for the first proposal ever made, and
    `queue_for_dispatcher` carried a compensating `- 1` that existed only to undo it. Two of the
    three callers correcting the same function is the function being wrong.

    Deterministic under replay, because a replayed super-step is handed the same checkpoint.
    """
    return int(state.get("node_visits", {}).get("optimize_field_schedule", 0))


def field_requirement(state: IncidentState) -> DispatchRequirement | None:
    """The requirement this branch is scheduling, or `None` before P14 has run.

    The last one, not the first. `dispatch_requirements` is a plain list with last-write-wins, so
    there is exactly one today; taking the last is what keeps this correct if a stage is ever added
    that plans two visits, where the one being scheduled is the one most recently written.
    """
    requirements = state.get("dispatch_requirements", [])
    return requirements[-1] if requirements else None


# ------------------------------------------------------------------------------------------------
# P14 -- what the visit needs
# ------------------------------------------------------------------------------------------------


@node("build_field_requirement")
async def build_field_requirement(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Choose the work-order option, and write down what a crew would need to do it.

    Records and does not schedule. The status is deliberately not set, for the reason
    `select_remote_action` gives: an incident whose requirement no crew can take never enters
    dispatch, and recording `dispatch_planning` here would claim a stage it did not reach.
    `optimize_field_schedule` sets it, because solving is what makes it true.

    The crew comes from `domain.boundaries.crew_for` and nowhere else. D13 reads the same function,
    so a second derivation here would be a second answer waiting to disagree with the router that
    sent us; what the node contributes is *recording* it, so that P15 and the WFM query read a
    decision rather than re-deriving one. `crew_for` returns `None` for the back-office and
    undispatchable domains, and that is not a defect -- it is the case `route_field_gate` escalates.

    Where the visit's length comes from
    -----------------------------------
    `dispatch.clean_boots_visit_minutes` and its two siblings, by crew type, and **not**
    `ResolutionOption.estimated_duration`. Those two look like the same quantity and are not: the
    option's 240 minutes for a drop repair is the lead time to a restored service, which is what
    ranks it against a five-minute reboot, while the pack's 60 is how long a crew stands at the
    pole. Handing the optimizer the lead time would book a Clean Boots crew out for half a shift.

    Customer access, and the one constraint that is handled rather than recorded as a gap
    -------------------------------------------------------------------------------------
    The joint work order carries `requires_customer_present=True`, and `DispatchRequirement` refuses
    `customer_access_required` with no window -- correctly: "this dispatch would be scheduled blind
    and fail access". Measured, nothing in the fixture set holds a customer availability window; the
    41 service records carry no contact or appointment field at all.

    So the requirement records the access need in `notes` and leaves the flag `False`, which is the
    only one of three options that is neither a lie nor a refusal to plan. Setting the flag with a
    fabricated window would schedule against an appointment nobody made -- the exact failure the
    validator exists to prevent, committed by the caller instead of the model. Setting the flag with
    no window is unconstructible. Dropping the fact entirely would hand the dispatcher a joint visit
    with no hint that the customer has to be in. `CUSTOMER_ACCESS` therefore cannot refuse anything
    on this path either, and that is the same gap as the other three -- but it is the one where
    state could supply the missing fact tomorrow, from a CRM appointment call, without any other
    change here than passing the windows through.
    """
    plan = state.get("resolution_plan")
    option = first_actionable_option(state, is_dispatchable_option)
    domain = state.get("fault_domain", FaultDomain.UNKNOWN)
    crew = crew_for(domain)
    cycle = state.get("diagnostic_cycles", 1)

    if plan is None or option is None or crew is None:
        # Three impossibilities with one honest reading: this branch was entered and there is
        # nothing here it can dispatch. Recorded rather than raised -- `route_field_gate` sends it
        # to `abandon_field_planning` and the parent's D11 sends the incident round again.
        return {
            "audit_events": [
                audit(
                    state,
                    ctx,
                    node="build_field_requirement",
                    action="build_field_requirement",
                    outcome="no_dispatchable_option",
                    reason_code=ReasonCode.REMOTE_FIX_EXHAUSTED,
                    detail={
                        "cycle": cycle,
                        "fault_domain": domain.value,
                        "crew": crew.value if crew is not None else None,
                        "has_plan": plan is not None,
                        "offered": [o.action_type.value for o in (plan.options if plan else [])],
                    },
                    discriminator=cycle,
                )
            ]
        }

    now = ctx.clock.now()
    topology = state.get("topology")
    impact = state.get("impact")
    requirement = DispatchRequirement(
        requirement_id=derive_id("REQ", state.get("incident_id") or "", option.option_id),
        incident_id=state.get("incident_id") or "",
        created_at=now,
        crew_type=crew,
        fault_domain=domain,
        delimiter_kind=topology.delimiter_kind if topology is not None else DelimiterKind.UNKNOWN,
        delimiter_ref=topology.delimiter_ref if topology is not None else None,
        area_archetype=topology.area_archetype if topology is not None else None,
        estimated_duration=_visit_length(ctx, crew),
        customer_access_required=False,
        priority_score=_priority_of(option, impact.affected_customer_count if impact else 1),
        latitude=topology.latitude if topology is not None else None,
        longitude=topology.longitude if topology is not None else None,
        notes=_requirement_notes(option),
    )

    return {
        "dispatch_requirements": [requirement],
        "crew_type": crew,
        "resolution_plan": plan.model_copy(update={"selected_option_id": option.option_id}),
        "audit_events": [
            audit(
                state,
                ctx,
                node="build_field_requirement",
                action="build_field_requirement",
                outcome="requirement_recorded",
                subject_ref=option.target_ref,
                reason_code=ReasonCode.PHYSICAL_FAULT_CONFIRMED,
                detail={
                    "cycle": cycle,
                    "requirement_id": requirement.requirement_id,
                    "option_id": option.option_id,
                    "action_type": option.action_type.value,
                    "fault_domain": domain.value,
                    "crew": crew.value,
                    "area_archetype": (
                        requirement.area_archetype.value
                        if requirement.area_archetype is not None
                        else None
                    ),
                    "delimiter_ref": requirement.delimiter_ref,
                    "visit_minutes": requirement.estimated_duration.total_seconds() / 60.0,
                    "priority_score": requirement.priority_score,
                    "customer_present_expected": option.requires_customer_present,
                },
                discriminator=cycle,
            )
        ],
    }


def _visit_length(ctx: GraphContext, crew: CrewType) -> timedelta:
    """How long the crew is on site, from the pack, by crew type.

    `default_visit_minutes` is the fallback for a crew type the pack has not named, which cannot
    happen while `CrewType` has three members -- it is here so that adding a fourth produces a
    plannable job rather than a `KeyError` inside a node.
    """
    dispatch = ctx.policy.pack.dispatch
    minutes = {
        CrewType.CLEAN: dispatch.clean_boots_visit_minutes,
        CrewType.DIRTY: dispatch.dirty_boots_visit_minutes,
        CrewType.JOINT: dispatch.joint_visit_minutes,
    }.get(crew, dispatch.default_visit_minutes)
    return timedelta(minutes=minutes)


def _priority_of(option: ResolutionOption, affected: int) -> float:
    """How this job ranks against the others in the queue. A weight, not a probability.

    `objective.urgency_rank` already folds the SLA and the blast radius from `JobContext`, so this
    term must not repeat either -- counting the same customers twice would let one large outage
    outrank every SLA in the queue. What is left, and what nothing else in the solve can see, is how
    likely this particular repair is to work: an option the planner rated 0.85 is worth sending
    before one it rated 0.4, at equal urgency.

    Scaled by the log of the affected count only through `urgency_rank`; here `affected` is used
    solely to keep a single-premises job from outranking a multi-dwelling one at identical
    confidence, which is a tie-break rather than a weighting.
    """
    return round(option.estimated_success_probability + min(affected, 100) / 1000.0, 4)


def _requirement_notes(option: ResolutionOption) -> list[str]:
    """What a dispatcher reading the queue needs and the structured fields cannot hold.

    The customer-presence note is the one that matters, and the node docstring says why it is a note
    rather than `customer_access_required=True`.
    """
    notes = [f"{option.label} ({option.action_type.value}) on {option.target_ref}"]
    if option.rationale:
        notes.append(option.rationale)
    if option.requires_customer_present:
        notes.append(
            "the customer must be present for this visit; no availability window is known, so it "
            "is recorded here rather than as customer_access_required -- see field_planning.P14"
        )
    return notes


# ------------------------------------------------------------------------------------------------
# D13 -- which crew
# ------------------------------------------------------------------------------------------------


def route_field_gate(state: IncidentState) -> Literal["clean", "dirty", "joint", "escalate"]:
    """Is there a visit to schedule, and whose is it? D13, with the precondition it presupposes.

    `route_dispatch_type` reads `fault_domain` alone, which is the right shape for the question it
    was written to answer -- and it is asked here in a state where the domain can be dispatchable
    while the *plan* offered nothing to dispatch. That combination is real:
    `first_actionable_option` skips blocked options, so an incident whose only work-order option was
    blocked in an earlier cycle arrives at a `drop` domain with nothing to send anyone to do.

    `escalate` is D13's own word for that case, so the wrapper widens the precondition without
    inventing an answer, and the branch table below stays the four D13 declares.

    Total and never raises, like every router. It reads only the two facts P14 writes, so the
    escalating case is exactly "P14 recorded no requirement", however that came about.
    """
    if selected_field_option(state) is None or field_requirement(state) is None:
        return "escalate"
    return route_dispatch_type(state)


# ------------------------------------------------------------------------------------------------
# P15 -- who, and when
# ------------------------------------------------------------------------------------------------


@node("optimize_field_schedule")
async def optimize_field_schedule(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Ask the WFM which crews are free, solve, and record the plan with its refusals.

    Sets `DISPATCH_PLANNING`, which is where the incident genuinely is once a solve has been
    attempted -- including when the solve found nothing, because "we tried to schedule this and
    could not" is a dispatch-planning state and `queue_for_dispatcher` leaves it there.

    Re-entered on `replan`. Everything it writes is re-derived from state and the adapter, and the
    plan id carries `dispatch_round`, so a second pass produces a genuinely second plan rather than
    an update that `append_unique` would collapse. See the module docstring for the two ids that
    depend on that and for why the loop terminates.

    Why the crew search window is one shift
    ---------------------------------------
    `now` to `now + shift_minutes + max_overtime_minutes`, both from the pack, so an operator who
    lengthens the working day lengthens the search by the same edit. It is the longest a single
    job's placement can need: a crew that cannot start inside one shift's worth of clock cannot take
    this job today, and `check_working_hours` would refuse it anyway.

    The consequence is that this stage does not book tomorrow. An incident raised after the last
    shift ends finds no crew and queues for a dispatcher with `capacity` against it, which is a true
    statement about today and an incomplete one about the week; recorded as gap FIELD-3.

    Why a crew's booked jobs are folded into `max_jobs`
    --------------------------------------------------
    `check_capacity` compares `Candidate.jobs_already_assigned` -- how many jobs *this solve* has
    given the crew -- against `min(crew.max_jobs, max_jobs_per_crew_per_shift)`. It knows nothing of
    the shift the crew is already half way through, so a crew holding five of seven booked jobs
    would be offered all seven again. Subtracting at the boundary is the only place the two counts
    can be reconciled without teaching the optimizer about the WFM.

    A crew with nothing left is dropped rather than passed with `max_jobs=0`, which `CrewSlot`
    refuses (`ge=1`) -- and the two statements are the same one: a full crew is not a slot.
    """
    requirement = field_requirement(state)
    if requirement is None:
        raise ValueError(
            "optimize_field_schedule was reached with no dispatch requirement. Only "
            "`route_field_gate` leads here and it escalates when P14 recorded none."
        )

    now = ctx.clock.now()
    # `+ 1` here and nowhere else: this node is the pass, and `@node` has not counted it yet.
    round_number = dispatch_round(state) + 1
    dispatch_policy = ctx.policy.pack.dispatch
    horizon = timedelta(
        minutes=dispatch_policy.shift_minutes + dispatch_policy.max_overtime_minutes
    )

    gathered = Gathered(ctx, assessed_at=now)
    rows = await gathered.fetch(
        "wfm.crew_availability",
        ctx.adapters.wfm.fetch_crew_availability(
            # The archetype, because that is what the topology resolved and what
            # `check_geography` compares. The adapter accepts an area reference too and
            # normalises either; an unknown one returns no crews rather than raising.
            area=requirement.area_archetype.value if requirement.area_archetype else "",
            crew_type=requirement.crew_type.value,
            window_start=now,
            window_end=now + horizon,
        ),
        freshness=Freshness.DISPATCH,
    )
    crews = [slot for slot in (_crew_slot(row) for row in rows or []) if slot is not None]
    if not crews:
        gathered.add_note(
            f"no {requirement.crew_type.value} crew has capacity in "
            f"{requirement.area_archetype.value if requirement.area_archetype else 'this area'} "
            f"within {horizon.total_seconds() / 3600:.0f}h"
        )

    plan = solve_dispatch(
        DispatchProblem(
            requirements=[requirement],
            crews=crews,
            contexts={requirement.requirement_id: _job_context(state, requirement, now)},
            dispatch_policy=dispatch_policy,
            blast_policy=ctx.policy.pack.blast_radius,
            now=now,
            # No travel model, so `DispatchProblem.travel_model` falls back to `PolicyTravelModel`
            # and the pack's per-archetype speeds. The seam is built, not missing: `prefetch_travel`
            # resolves the GIS adapter into a `MatrixTravelModel` before the solve, which is what a
            # synchronous `TravelModel` inside a pure search cannot do for itself. It is left
            # unwired because the fixture GIS is a straight-line model too -- measured, routing this
            # job moves travel 23.8 -> 23.9 minutes -- so it would buy a `basis` and not an answer.
            # The plan says which model spoke (`objective` ends `:estimated`). Gap FIELD-4.
            plan_id=derive_id(
                "DPL", state.get("incident_id") or "", requirement.requirement_id, round_number
            ),
        )
    )
    assigned = plan.assigned_requirement_ids
    refusal = plan.constraint_explanation.get(requirement.requirement_id, "")
    # Read the code off the plan rather than asserting one. An earlier draft stamped
    # `CUSTOMER_ACCESS_REQUIRED` on every infeasible solve, which was wrong for every case that can
    # actually occur here and right only for the one that cannot -- measured, a requirement naming
    # a part no van carries was audited as a customer-access failure. `_reason_for` is the mapping
    # `queue_for_dispatcher` uses, so the two nodes cannot describe one refusal differently.
    blocked_by = None if assigned else blocking_code(refusal)

    return {
        "status": IncidentStatus.DISPATCH_PLANNING,
        "dispatch_plan": plan,
        "data_quality": gathered.assessment(previous=state.get("data_quality")),
        "audit_events": [
            audit(
                state,
                ctx,
                node="optimize_field_schedule",
                action="optimize_field_schedule",
                outcome="scheduled" if assigned else "no_feasible_slot",
                subject_ref=requirement.delimiter_ref or requirement.incident_id,
                reason_code=_reason_for(blocked_by),
                detail={
                    "round": round_number,
                    "plan_id": plan.plan_id,
                    "requirement_id": requirement.requirement_id,
                    "crew_type": requirement.crew_type.value,
                    "crews_offered": len(crews),
                    "crews_returned": len(rows or []),
                    "solver": plan.solver,
                    "solver_status": plan.solver_status,
                    "assignments": [
                        {
                            "crew_id": a.crew_id,
                            "start": a.scheduled_start.isoformat(),
                            "travel_minutes": a.travel_minutes,
                        }
                        for a in plan.assignments
                    ],
                    "blocking_code": blocked_by.value if blocked_by is not None else None,
                    "explanation": refusal,
                },
                discriminator=plan.plan_id,
            )
        ],
    }


def _crew_slot(row: dict[str, Any]) -> CrewSlot | None:
    """One WFM row as the optimizer's input, or `None` for a crew with no capacity left.

    `carried_equipment` is absent from every row the adapter returns and is left empty rather than
    guessed at, which is why `EQUIPMENT` cannot refuse anything here; see the module docstring.
    `on_call` is read and discarded for the same reason in reverse -- there is no constraint that
    consumes it, and folding it into `max_jobs` would be inventing a rule the pack does not state.
    """
    booked = int(row.get("jobs_already_booked", 0) or 0)
    remaining = int(row["max_jobs"]) - booked
    if remaining < 1:
        return None
    return CrewSlot(
        crew_id=str(row["crew_id"]),
        crew_type=CrewType(str(row["crew_type"])),
        skills=list(row.get("skills") or []),
        available_from=row["available_from"],
        available_until=row["available_until"],
        base_latitude=row.get("base_latitude"),
        base_longitude=row.get("base_longitude"),
        area_archetypes=list(row.get("area_archetypes") or []),
        max_jobs=remaining,
        carried_parts=list(row.get("carried_parts") or []),
        carried_equipment=[],
    )


def _job_context(
    state: IncidentState, requirement: DispatchRequirement, now: datetime
) -> JobContext:
    """What the objective and the constraints may consider about this job, read off state.

    Four fields are supplied and six are deliberately left at their defaults. The omissions are the
    interesting half:

    * **`wind_kph` and `aerial_work_required`** stay unset together. `check_safety` returns early
      unless the work is aerial, and nothing distinguishes an aerial drop from a buried one, so a
      wind reading alone would be a number no check consults. P07 already gathers `gis.weather` into
      evidence, so the reading is there the day something can say which spans are aerial.
    * **`building_access_windows`** stays empty. `TopologyContext.mdu_ref` says a service is in a
      multi-dwelling unit but nothing holds the riser-room hours, and `check_building_access` treats
      an empty tuple as "no restriction" -- which is the permissive direction, and the visible one.
    * **`depends_on`** stays empty. One requirement per incident today; a predecessor would come
      from a plant repair this stage does not create.
    * **`parts_in_stock`** stays `True`, which only matters when `parts_required` is non-empty, and
      it never is -- nothing in `src` writes that field, which is half of gap FIELD-1. The default
      is not load-bearing today and would be the wrong one the moment it were: where parts are
      named and stock is unknown, `JobContext` says the caller must pass `False` rather than let
      this speak.
    * **`first_time_fix_probability`** stays `None` because nothing in `dispatch` reads it. It is
      carried on the option instead, folded into `priority_score`, which `urgency_rank` does read.
    """
    sla = state.get("sla")
    impact = state.get("impact")
    return JobContext(
        requirement_id=requirement.requirement_id,
        sla_remaining_minutes=(
            sla.time_remaining(now).total_seconds() / 60.0 if sla is not None else None
        ),
        sla_at_risk=sla.at_risk(now) if sla is not None else False,
        affected_customers=impact.affected_customer_count if impact is not None else 1,
        vulnerable_customer=sla.vulnerable_customer if sla is not None else False,
    )


# ------------------------------------------------------------------------------------------------
# The policy gate
# ------------------------------------------------------------------------------------------------


@node("evaluate_dispatch_policy")
async def evaluate_dispatch_policy(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Put the scheduled dispatch to the policy engine, and record the verdict whatever it is.

    A node of its own for the reason `remote_resolution` sets out: `ActionRequest` refuses
    `policy_outcome=BLOCKED` and refuses an approval-requiring outcome with no `approval_ref`, so by
    the time the verdict is known the object that would carry it is already illegal. The verdict is
    recorded here and `commit_field_dispatch` builds the request once the gate has been passed.

    The decision id is re-keyed from the incident, the option **and the plan**. `PolicyEngine
    .evaluate` mints a `uuid4`, making one evaluation appear as several to `append_unique`;
    dropping the plan id from the key would do the opposite and make the second round's evaluation
    invisible, which is the failure that would leave `replan` spinning. Both directions of that are
    in the module docstring.

    Note what is *not* re-checked here: whether the plan is feasible. D14 has already answered that
    and `route_dispatch_constraints` is wired between the two, so an infeasible plan never reaches
    this node -- which is what lets the policy input describe an action that has a crew and a time.
    """
    option = selected_field_option(state)
    plan = state.get("dispatch_plan")
    if option is None or plan is None:
        raise ValueError(
            "evaluate_dispatch_policy was reached with no selected option or no dispatch plan. "
            "Every path here runs through `route_field_gate` and D14, which between them establish "
            "both."
        )

    verdict = ctx.policy.evaluate(policy_input_for(state, ctx, option))
    decision = verdict.model_copy(
        update={
            "decision_id": derive_id(
                "POL",
                state.get("incident_id") or "",
                option.option_id,
                plan.plan_id,
                verdict.outcome.value,
            )
        }
    )

    update: NodeUpdate = {
        "policy_decisions": [decision],
        "audit_events": [
            audit(
                state,
                ctx,
                node="evaluate_dispatch_policy",
                action="evaluate_dispatch_policy",
                outcome=decision.outcome.value,
                subject_ref=option.target_ref,
                reason_code=decision.reason_codes[0] if decision.reason_codes else None,
                detail={
                    "plan_id": plan.plan_id,
                    "option_id": option.option_id,
                    "action_type": option.action_type.value,
                    "attempt": attempt_number(state, option.action_type),
                    "blast_radius": option.blast_radius,
                    "policy_decision_id": decision.decision_id,
                    "policy_version": decision.policy_version,
                    "matched_rule": decision.matched_rule,
                    "required_approval": (
                        decision.required_approval_kind.value
                        if decision.required_approval_kind
                        else None
                    ),
                    "explanation": decision.explanation,
                },
                discriminator=decision.decision_id,
            )
        ],
    }
    # `preview`, not `state`: `policy_block_rate` counts `policy_decisions`, and the one it must
    # count is still sitting unreduced in `update`. See `select_remote_action` for the measurement.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.POLICY_BLOCK_RATE,
        node="evaluate_dispatch_policy",
        dimensions={"action_type": option.action_type.value},
        discriminator=decision.decision_id,
    )
    return update


def route_dispatch_gate(
    state: IncidentState,
) -> Literal["approve_dispatch", "commit", "replan", "queue_for_dispatcher"]:
    """May this dispatch be committed now? Wired out of the policy evaluation *and* out of the gate.

    D15 with its precondition made explicit. `route_dispatch_approval`'s fall-through reads
    `latest_policy_decision(state, CREATE_WORK_ORDER)` and, finding none or finding one that is
    merely not-blocked, answers `approve_dispatch` -- so an evaluation the pack **blocked** would be
    put to a human as though the pack permitted it, and a human who approved it would be authorising
    an action the engine had already refused. A blocked or missing decision queues for the
    dispatcher, which is a real desk with a real remedy, rather than an approval nobody may grant.

    That fourth answer is the whole of the difference. Once a decision exists and is not blocked,
    the question is D15's and the answer is D15's -- including `replan`, which returns to P15 rather
    than to the queue because D15's docstring says what a rejection means: the dispatcher is
    refusing *that slot*.

    Attached to two edges, as `route_remote_gate` is, and for the same reason: after the evaluation
    and after an answer the question is identical, and the answer moves from `approve_dispatch` to
    `commit` or `replan` purely because the approval trail changed underneath it. Two routers would
    be two spellings of one question and the second would be the one that forgot about rejection.
    """
    option = selected_field_option(state)
    if option is None:
        return "queue_for_dispatcher"
    decision = latest_policy_decision(state, option.action_type)
    if decision is None or decision.blocked:
        return "queue_for_dispatcher"
    return route_dispatch_approval(state)


# ------------------------------------------------------------------------------------------------
# D15 -- the approval pair
# ------------------------------------------------------------------------------------------------


def _dispatch_context(
    state: IncidentState,
) -> tuple[ResolutionOption, PolicyDecision, DispatchPlan, DispatchRequirement]:
    """The four things an approval or a commit is about, or a loud failure naming what was absent.

    Raises rather than returning `None` on any of them. Every caller is reached only through
    `route_dispatch_gate`, which has already established the option and an unblocked decision, and
    through D14, which has already established an assigned plan; getting here without one means an
    edge bypasses a router. `@node` deliberately does not catch that -- a checkpoint left at the
    last node that completed is truthful and resumable, where a state update claiming nothing
    happened is neither.
    """
    option = selected_field_option(state)
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    plan = state.get("dispatch_plan")
    requirement = field_requirement(state)
    if option is None or decision is None or plan is None or requirement is None:
        raise ValueError(
            "the dispatch gate was reached without a selected option, a policy decision, a "
            f"dispatch plan and a requirement (option={option is not None}, "
            f"decision={decision is not None}, plan={plan is not None}, "
            f"requirement={requirement is not None}). Only `route_dispatch_gate` and D14 may lead "
            "here, and between them they check all four."
        )
    return option, decision, plan, requirement


@node("prepare_dispatch_approval")
async def prepare_dispatch_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Write the question down, with the slot in it, then return so it is committed.

    The first half of the pair `graph.interrupts` describes. The whole question is built here and
    nothing is built in the node that raises, so `requested_at` is stamped once however many times
    the gate replays.

    `ApprovalKind.DISPATCH` is named directly, unlike `remote_resolution`, which keys off whatever
    kind the pack's decision demanded. That is not an inconsistency but a consequence of who reads
    the answer: `route_dispatch_approval` asks `approval_outstanding(state, ApprovalKind.DISPATCH)`
    literally, so a question asked under any other kind would be answered and then not seen. The
    generality lives where its reader is general.

    The question carries the crew and the time. An operator approving "a dispatch" is approving a
    truck, a crew type and a slot, and an approval screen that named only the incident would be one
    where nobody could tell a 09:00 Clean Boots visit from a 15:00 joint one.
    """
    option, decision, plan, requirement = _dispatch_context(state)
    round_number = dispatch_round(state)
    assignment = next(
        (a for a in plan.assignments if a.requirement_id == requirement.requirement_id), None
    )
    if assignment is None:
        raise ValueError(
            f"the dispatch approval gate was reached with no assignment for "
            f"{requirement.requirement_id!r} on plan {plan.plan_id!r}. D14 routes an unassigned "
            "requirement to the dispatcher queue, so this edge cannot produce one."
        )

    when = assignment.scheduled_start.isoformat(timespec="minutes")
    request = build_request(
        state,
        ctx,
        kind=ApprovalKind.DISPATCH,
        question=(
            f"Approve a {requirement.crew_type.value} dispatch to {option.target_ref} at {when}, "
            f"crew {assignment.crew_id}? This is dispatch proposal {round_number} for the incident."
        ),
        # The dispatch round, not `attempt_number`. A rejected dispatch reached no adapter, so the
        # action attempt does not move and a re-proposal would derive the first question's id --
        # which `approvals` de-duplicates away, first-write-wins. See the module docstring.
        attempt=round_number,
        action_type=option.action_type,
        target_ref=option.target_ref,
        recommendation=option.rationale or option.label,
        risk_summary=decision.explanation,
        blast_radius=option.blast_radius,
        reversible=option.reversible,
        policy_decision_id=decision.decision_id,
        context={
            "fault_domain": requirement.fault_domain.value,
            "crew_type": requirement.crew_type.value,
            "crew_id": assignment.crew_id,
            "scheduled_start": assignment.scheduled_start.isoformat(),
            "scheduled_end": assignment.scheduled_end.isoformat(),
            "travel_minutes": assignment.travel_minutes,
            "area_archetype": (
                requirement.area_archetype.value if requirement.area_archetype is not None else None
            ),
            "delimiter_ref": requirement.delimiter_ref,
            "plan_id": plan.plan_id,
            "solver": plan.solver,
            "objective_value": plan.objective_value,
            "customer_present_expected": option.requires_customer_present,
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
                node="prepare_dispatch_approval",
                action="request_approval",
                outcome="awaiting_approval",
                subject_ref=option.target_ref,
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                detail={
                    "approval_id": request.approval_id,
                    "kind": ApprovalKind.DISPATCH.value,
                    "round": round_number,
                    "crew_id": assignment.crew_id,
                    "scheduled_start": assignment.scheduled_start.isoformat(),
                    "required_role": request.required_role,
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
                discriminator=request.approval_id,
            )
        ],
    }


@node("request_dispatch_approval")
async def request_dispatch_approval(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Raise the interrupt and record the answer. Builds nothing; see `graph.interrupts`.

    Thin on purpose. Everything before `interrupt()` re-runs on resume, so the less there is before
    it the less there is to re-run -- and a gate that built its own question here would build a
    different one each time.
    """
    return request_approval(state, ctx)


# ------------------------------------------------------------------------------------------------
# P16 -- commit
# ------------------------------------------------------------------------------------------------


def _approval_ref(state: IncidentState, decision: PolicyDecision) -> str | None:
    """The approval this dispatch runs under, or `None` when the pack demanded none.

    Read off `ApprovalDecision.approval_ref`, a derived property (`approval_id:decided_by`) rather
    than a stored field, so the reference on the action cannot disagree with the approval it names.
    """
    if decision.outcome is not PolicyOutcome.REQUIRES_APPROVAL:
        return None
    kind = decision.required_approval_kind
    answer = latest_decision_of(state, kind) if kind is not None else None
    return answer.approval_ref if answer is not None else None


@node("commit_field_dispatch")
async def commit_field_dispatch(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Send the work order to the WFM and record what came back. P16.

    The status stays `DISPATCH_PLANNING` and no truck-roll KPI is emitted; the module docstring
    argues both at length, and they are the same fact -- a `REQUESTED` work order is a booking, not
    a visit, and `WorkOrder.counted_as_truck_roll` says so.

    The idempotency key is `idempotency_key_for`, keyed on the option. Across a `replan` loop that
    is deliberately the *same* key: the rejected rounds reached no adapter, and the visit being
    booked is the same visit at a different time. A genuine second visit comes from a later
    resolution plan, whose options carry a new plan id and so a new key.

    `field_visit_count` is written as an absolute count of distinct work orders, never as
    `state.get(...) + 1`. It reduces with `take_max`, and an increment computed from a value read at
    entry is exactly the pattern that reducer exists to defeat -- and `work_orders` reduces with
    `append_revision`, so `len()` over it counts revisions rather than orders.
    """
    option, decision, plan, requirement = _dispatch_context(state)
    assignment = next(
        (a for a in plan.assignments if a.requirement_id == requirement.requirement_id), None
    )
    if assignment is None:
        raise ValueError(
            f"commit_field_dispatch was reached with no assignment for "
            f"{requirement.requirement_id!r} on plan {plan.plan_id!r}. D14 routes an unassigned "
            "requirement to the dispatcher queue, so this edge cannot produce one."
        )

    now = ctx.clock.now()
    attempt = attempt_number(state, option.action_type)
    idempotency_key = idempotency_key_for(state, option)
    action_id = derive_id("ACT", state.get("incident_id") or "", option.option_id)
    work_order_id = derive_id("WO", state.get("incident_id") or "", requirement.requirement_id)

    request = ActionRequest(
        action_id=action_id,
        incident_id=state.get("incident_id") or "",
        action_type=option.action_type,
        target_ref=option.target_ref,
        requested_at=now,
        idempotency_key=idempotency_key,
        actor=ctx.automation_actor,
        reason_code=(
            ReasonCode.POLICY_APPROVAL_REQUIRED
            if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
            else ReasonCode.POLICY_ALLOWED
        ),
        correlation_id=state.get("correlation_id") or state.get("incident_id") or "",
        approval_ref=_approval_ref(state, decision),
        policy_decision_id=decision.decision_id,
        policy_outcome=decision.outcome,
        attempt=attempt,
        # ISO strings rather than `datetime`s: these travel through the WFM's result payload into an
        # audit event's `detail`, and a mapping that is JSON by the time it is checkpointed is one
        # fewer thing to discover against Postgres.
        parameters={
            "crew_id": assignment.crew_id,
            "crew_type": assignment.crew_type.value,
            "scheduled_start": assignment.scheduled_start.isoformat(),
            "scheduled_end": assignment.scheduled_end.isoformat(),
            "skills_required": list(requirement.skills_required),
            "parts_required": list(requirement.parts_required),
            "customer_access_required": requirement.customer_access_required,
            "requirement_id": requirement.requirement_id,
            "plan_id": plan.plan_id,
        },
        reversible=option.reversible,
        expected_blast_radius=option.blast_radius,
    )

    result = await ctx.adapters.wfm.create_work_order(request)
    completed_at = ctx.clock.now()
    outcome = ActionOutcome(str(result["outcome"]))

    work_order = WorkOrder(
        work_order_id=work_order_id,
        incident_id=request.incident_id,
        external_ref=result.get("external_ref"),
        crew_type=assignment.crew_type,
        status=WorkOrderStatus(str(result.get("status") or WorkOrderStatus.REQUESTED.value)),
        created_at=now,
        updated_at=completed_at,
        scheduled_start=assignment.scheduled_start,
        scheduled_end=assignment.scheduled_end,
        assigned_crew_id=assignment.crew_id,
        visit_number=_distinct_work_orders(state, work_order_id),
        requirement_id=requirement.requirement_id,
        idempotency_key=idempotency_key,
        instructions="; ".join(requirement.notes),
        reason_code=request.reason_code,
    )
    record = ActionRecord(
        action_id=action_id,
        incident_id=request.incident_id,
        action_type=option.action_type,
        target_ref=option.target_ref,
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
        external_ref=result.get("external_ref"),
        detail=str(result.get("detail") or ""),
        error=str(result.get("error") or ""),
    )

    update: NodeUpdate = {
        "status": IncidentStatus.DISPATCH_PLANNING,
        "selected_action": request,
        "work_orders": [work_order],
        "action_history": [record],
        "dispatch_plan": plan.model_copy(
            update={"approved": True, "approval_ref": request.approval_ref}
        ),
        "field_visit_count": work_order.visit_number,
        "linked_records": {"work_order": result.get("external_ref") or work_order_id},
        "updated_at": completed_at,
        "audit_events": [
            audit(
                state,
                ctx,
                node="commit_field_dispatch",
                action="create_work_order",
                outcome=outcome.value,
                subject_ref=option.target_ref,
                reason_code=request.reason_code,
                detail={
                    "action_id": action_id,
                    "work_order_id": work_order_id,
                    "external_ref": result.get("external_ref"),
                    "status": work_order.status.value,
                    "crew_id": assignment.crew_id,
                    "crew_type": assignment.crew_type.value,
                    "scheduled_start": assignment.scheduled_start.isoformat(),
                    "visit_number": work_order.visit_number,
                    "attempt": attempt,
                    "idempotency_key": idempotency_key,
                    "approval_ref": request.approval_ref,
                    "policy_decision_id": decision.decision_id,
                    "policy_outcome": decision.outcome.value,
                    "plan_id": plan.plan_id,
                    "simulated": bool(result.get("simulated")),
                    "replayed": bool(result.get("replayed")),
                    "gate": result.get("gate"),
                    "detail": result.get("detail"),
                },
                discriminator=action_id,
            )
        ],
    }
    resolution = state.get("resolution_plan")
    if resolution is not None and option.option_id not in resolution.attempted_option_ids:
        # Same bookkeeping `execute_remote_repair` does, and for the same reason:
        # `first_actionable_option` skips an option already in `attempted_option_ids`, so an
        # incident that comes back through P11 after this dispatch fails must not be handed this
        # option a second time. Guarded on membership because `attempted_option_ids` is a plain list
        # on a model we `model_copy` -- appending unconditionally would double an entry if this node
        # were ever re-entered on the same option.
        update["resolution_plan"] = resolution.model_copy(
            update={"attempted_option_ids": [*resolution.attempted_option_ids, option.option_id]}
        )
    if MetricTimestamp.FIRST_ACTION_AT.value not in state.get("metrics_timestamps", {}):
        # Conditional, because `metrics_timestamps` reduces with `merge_dict` -- last writer wins
        # per key -- and an incident that tried a remote repair first already holds the true first
        # action. An unconditional stamp here would move "first" forward to whichever action ran
        # last, which is the one quantity the name promises it is not.
        stamp(update, MetricTimestamp.FIRST_ACTION_AT, now)
    # `preview`, not `state`: `automation_coverage_rate`'s denominator is the executed entries of
    # `action_history`, and the entry that just executed is in `update`.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.AUTOMATION_COVERAGE_RATE,
        node="commit_field_dispatch",
        dimensions={"action_type": option.action_type.value},
        discriminator=action_id,
    )
    return update


def _distinct_work_orders(state: IncidentState, work_order_id: str) -> int:
    """How many work orders this incident will hold, counting this one.

    Distinct by id because `work_orders` reduces with `append_revision`, which keeps a history:
    Stage 4 appends revised copies of the same order as the crew travels and arrives, so `len()`
    over the list counts status changes rather than visits.
    """
    seen = {existing.work_order_id for existing in state.get("work_orders", [])}
    seen.add(work_order_id)
    return len(seen)


# ------------------------------------------------------------------------------------------------
# The two ways out that do not dispatch
# ------------------------------------------------------------------------------------------------


@node("queue_for_dispatcher")
async def queue_for_dispatcher(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Hand the requirement to a human, with the constraint that blocked it named.

    D14's requirement in one node: the blocking constraint, the alternatives, and a queue -- and
    explicitly *not* committing an infeasible slot. `dispatch.optimizer` does the first two, landing
    an unplaceable requirement in `DispatchPlan.unassigned` with the violations of its nearest miss;
    `blocking_code` recovers the machine-readable code from that rendered string, which is what it
    was written for and had no caller of until now.

    The status stays `dispatch_planning`, and that is the whole difference between this node and
    `abandon_field_planning`. The incident is still going to get a visit -- a dispatcher has to
    unblock it first. Sending it back to `diagnosing` would lose the requirement and the plan, and
    the next pass would re-derive an identical one and queue it again.

    Two arrivals, one node, and the audit event says which: an infeasible plan from D14, and a
    blocked or unevaluated policy decision from `route_dispatch_gate`. They are one desk. What the
    dispatcher does about a crew that lacks a skill and about a pack that refuses the action differ,
    but both are "this cannot proceed automatically and a person must look", and splitting them
    would put the same queue behind two names.
    """
    option = selected_field_option(state)
    requirement = field_requirement(state)
    plan = state.get("dispatch_plan")
    decision = latest_policy_decision(state, option.action_type) if option is not None else None
    refusal = (
        plan.constraint_explanation.get(requirement.requirement_id, "")
        if plan is not None and requirement is not None
        else ""
    )
    code = blocking_code(refusal)

    outcome: str
    # `ReasonCode | None`, because the "no feasible slot" arm genuinely has no code for nine of the
    # twelve constraints and `_reason_for` says so by returning `None`. Widening here rather than
    # substituting a stand-in: `audit` accepts `None` and renders it as absent, where a filler code
    # would put a wrong word in a vocabulary that closure and reconciliation both read.
    reason: ReasonCode | None
    if decision is not None and decision.blocked:
        outcome = "blocked_by_policy"
        reason = (
            decision.reason_codes[0]
            if decision.reason_codes
            else ReasonCode.POLICY_NO_MATCHING_RULE
        )
    elif decision is None and plan is not None and plan.assigned_requirement_ids:
        outcome, reason = "unevaluated_dispatch", ReasonCode.POLICY_NO_MATCHING_RULE
    else:
        outcome = "no_feasible_slot"
        reason = _reason_for(code)

    return {
        "status": IncidentStatus.DISPATCH_PLANNING,
        "pending_approval": None,
        "audit_events": [
            audit(
                state,
                ctx,
                node="queue_for_dispatcher",
                action="queue_for_dispatcher",
                outcome=outcome,
                subject_ref=option.target_ref if option is not None else None,
                reason_code=reason,
                detail={
                    "requirement_id": (
                        requirement.requirement_id if requirement is not None else None
                    ),
                    "plan_id": plan.plan_id if plan is not None else None,
                    "round": dispatch_round(state),
                    "crew_type": requirement.crew_type.value if requirement is not None else None,
                    "blocking_code": code.value if code is not None else None,
                    "explanation": refusal,
                    "unassigned": list(plan.unassigned) if plan is not None else [],
                    "policy_outcome": decision.outcome.value if decision is not None else None,
                },
                discriminator=plan.plan_id if plan is not None else "no-plan",
            )
        ],
    }


#: The reason code that goes on a queued requirement, by the constraint that blocked it. Only the
#: three the vocabulary has a word for are mapped; everything else queues with no reason code, which
#: reads as "not applicable" and is true. Inventing a code per constraint would be inventing entries
#: in a reason vocabulary the closure and reconciliation stages also read.
_REASON_BY_CONSTRAINT: dict[ConstraintCode, ReasonCode] = {
    ConstraintCode.PARTS: ReasonCode.PARTS_UNAVAILABLE,
    ConstraintCode.CUSTOMER_ACCESS: ReasonCode.CUSTOMER_ACCESS_REQUIRED,
    ConstraintCode.BUILDING_ACCESS: ReasonCode.ACCESS_DENIED,
    ConstraintCode.SAFETY: ReasonCode.WEATHER_STOOD_DOWN,
}


def _reason_for(code: ConstraintCode | None) -> ReasonCode | None:
    return _REASON_BY_CONSTRAINT.get(code) if code is not None else None


@node("abandon_field_planning")
async def abandon_field_planning(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Leave the branch without booking anything, and say why in the record.

    This node exists for the status, as `abandon_remote_action` does. An incident that arrived at
    field planning with nothing to dispatch has a root cause and no remedy this branch can apply;
    `diagnosing` is the honest reading of that, and the parent's D11 will send it round again.

    The distinction from `queue_for_dispatcher` is whether there is a *requirement*. A visit nobody
    can schedule is a dispatcher's problem. No visit to schedule is a diagnosis problem, and putting
    it in a dispatcher's queue would give a human a job with nothing in it.

    The order the questions are asked in is the fix for a measured misattribution
    -------------------------------------------------------------------------------
    The only edge into this node is `route_field_gate`'s `escalate`, and that answer is given when
    the *option* or the *requirement* is absent -- it does not ask about the crew at all. An earlier
    draft here asked `crew_for(domain) is None` first, and over the fixture set that made this node
    contradict the node that owns the fact. Swept across all 41 services and both case types:

        build_field_requirement:no_dispatchable_option    10
        abandon_field_planning:no_crew_for_domain          9
        abandon_field_planning:options_exhausted           1

    Ten incidents, one cause, two nodes reporting it differently. Checking what was actually absent
    in each of the ten found `plan=yes opt=none exhausted=True` for all ten -- nine of them with
    `fault_domain=unknown`, which is why `crew_for` returned `None` and why the crew-first ordering
    caught them. But the missing crew is a *consequence* of the unknown domain, not the reason this
    branch gave up: with a crew there would still have been nothing to send them to do. Asking the
    gate's own question first makes the two nodes agree, and leaves `no_crew_for_domain` for the
    case it actually describes -- an option to dispatch and nobody whose job it is.

    `options_exhausted` and `no_dispatchable_option` both mean "no option", and they are kept apart
    because they are different repairs: everything was tried, versus nothing on offer was a work
    order. Only the first is reachable from a fixture today; the second has a unit test.
    """
    option = selected_field_option(state)
    requirement = field_requirement(state)
    domain = state.get("fault_domain", FaultDomain.UNKNOWN)
    crew = crew_for(domain)
    plan = state.get("resolution_plan")
    cycle = state.get("diagnostic_cycles", 1)

    if option is None and plan is not None and plan.exhausted:
        outcome, reason = "options_exhausted", ReasonCode.REMOTE_FIX_EXHAUSTED
    elif option is None:
        outcome, reason = "no_dispatchable_option", ReasonCode.REMOTE_FIX_EXHAUSTED
    elif crew is None:
        outcome, reason = "no_crew_for_domain", ReasonCode.NO_FAULT_FOUND
    else:
        # Option and crew both present and the gate still escalated, so it was the requirement that
        # was missing -- P14 ran and wrote none. Named rather than folded into the branch above,
        # because the repair is in `build_field_requirement` and not in the resolution plan.
        outcome, reason = "no_requirement_built", ReasonCode.NO_FAULT_FOUND

    return {
        "status": IncidentStatus.DIAGNOSING,
        "pending_approval": None,
        "audit_events": [
            audit(
                state,
                ctx,
                node="abandon_field_planning",
                action="abandon_field_planning",
                outcome=outcome,
                reason_code=reason,
                detail={
                    "cycle": cycle,
                    "fault_domain": domain.value,
                    "crew": crew.value if crew is not None else None,
                    "option_id": option.option_id if option is not None else None,
                    "requirement_id": (
                        requirement.requirement_id if requirement is not None else None
                    ),
                    "exhausted": plan.exhausted if plan is not None else None,
                    "offered": [o.action_type.value for o in (plan.options if plan else [])],
                },
                discriminator=cycle,
            )
        ],
    }


# ------------------------------------------------------------------------------------------------
# The graph
# ------------------------------------------------------------------------------------------------

#: The eight nodes, in the order the specification walks them. Same shape as `PARENT_NODES` and
#: checked the same way, so a node registered under a name its decorator does not carry fails on
#: import rather than producing a graph whose topology and audit trail disagree.
FIELD_PLANNING_NODES: tuple[tuple[str, Any], ...] = (
    ("build_field_requirement", build_field_requirement),
    ("optimize_field_schedule", optimize_field_schedule),
    ("evaluate_dispatch_policy", evaluate_dispatch_policy),
    ("prepare_dispatch_approval", prepare_dispatch_approval),
    ("request_dispatch_approval", request_dispatch_approval),
    ("commit_field_dispatch", commit_field_dispatch),
    ("queue_for_dispatcher", queue_for_dispatcher),
    ("abandon_field_planning", abandon_field_planning),
)

check_node_registry(FIELD_PLANNING_NODES, "the field-planning node registry")

#: Where each of `route_field_gate`'s answers goes. The three crew types converge on one node --
#: the crew is already recorded on the requirement and the optimizer reads it from there, so three
#: identical solve nodes would be three copies differing only in a value they do not use.
CREW_TARGETS: dict[str, str] = {
    "clean": "optimize_field_schedule",
    "dirty": "optimize_field_schedule",
    "joint": "optimize_field_schedule",
    "escalate": "abandon_field_planning",
}

#: D14's two answers.
CONSTRAINT_TARGETS: dict[str, str] = {
    "queue_for_dispatcher": "queue_for_dispatcher",
    "continue": "evaluate_dispatch_policy",
}

#: `route_dispatch_gate`'s four. `replan` goes back to P15 and not to the queue; see D15's own
#: docstring for why a refused slot is a re-optimisation rather than an escalation.
DISPATCH_TARGETS: dict[str, str] = {
    "approve_dispatch": "prepare_dispatch_approval",
    "commit": "commit_field_dispatch",
    "replan": "optimize_field_schedule",
    "queue_for_dispatcher": "queue_for_dispatcher",
}


def build_field_planning_graph() -> StateGraph[
    IncidentState, GraphContext, IncidentState, IncidentState
]:
    """Assemble the subgraph, uncompiled. Same contract as `builder.build_parent_graph`.

    Every onward edge is guarded, for the reason the parent's are: `escalation_update` stops a node
    from doing work but does not stop the graph, so an unguarded edge would send a work order to the
    WFM after the budget had been declared exhausted.

    `context_schema=GraphContext` is repeated rather than inherited. A compiled subgraph is a graph
    in its own right -- `get_runtime(GraphContext)` inside its nodes resolves against *its* schema
    -- so omitting it would make every node here raise on its first line while the parent's kept
    working.
    """
    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )
    for name, fn in FIELD_PLANNING_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, "build_field_requirement")

    crew_map: dict[Any, str] = {**CREW_TARGETS, ESCALATED: END}
    graph.add_conditional_edges("build_field_requirement", guarded(route_field_gate), crew_map)

    constraint_map: dict[Any, str] = {**CONSTRAINT_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "optimize_field_schedule", guarded(route_dispatch_constraints), constraint_map
    )

    dispatch_map: dict[Any, str] = {**DISPATCH_TARGETS, ESCALATED: END}
    graph.add_conditional_edges(
        "evaluate_dispatch_policy", guarded(route_dispatch_gate), dispatch_map
    )
    graph.add_conditional_edges(
        "request_dispatch_approval", guarded(route_dispatch_gate), dispatch_map
    )
    graph.add_conditional_edges(
        "prepare_dispatch_approval",
        guarded(straight_on),
        {ONWARD: "request_dispatch_approval", ESCALATED: END},
    )

    graph.add_edge("commit_field_dispatch", END)
    graph.add_edge("queue_for_dispatcher", END)
    graph.add_edge("abandon_field_planning", END)
    return graph


def compile_field_planning_graph() -> Any:
    """Compile the subgraph for use as a single node in the parent.

    No checkpointer argument, and that is not an omission. A subgraph compiled as a node shares the
    parent's checkpointer -- LangGraph namespaces its state beneath the parent's thread -- and
    handing this one its own would give the incident two places to be resumed from.
    """
    return build_field_planning_graph().compile(name="lpr_cpe_field_planning")


__all__ = [
    "CONSTRAINT_TARGETS",
    "CREW_TARGETS",
    "DISPATCH_TARGETS",
    "FIELD_PLANNING_NODES",
    "abandon_field_planning",
    "build_field_planning_graph",
    "build_field_requirement",
    "commit_field_dispatch",
    "compile_field_planning_graph",
    "dispatch_round",
    "evaluate_dispatch_policy",
    "field_requirement",
    "is_dispatchable_option",
    "optimize_field_schedule",
    "prepare_dispatch_approval",
    "queue_for_dispatcher",
    "request_dispatch_approval",
    "route_dispatch_gate",
    "route_field_gate",
    "selected_field_option",
]
