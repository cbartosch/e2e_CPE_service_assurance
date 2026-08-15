"""The twenty-four decision points, as pure functions of state.

The specification names D01 to D24 and requires that they be "conditional transitions", explicitly
not "an unconstrained conversational agent". This module is all twenty-four of them and nothing
else: no adapter is called here, no clock is read, no policy is evaluated, no language model is
consulted.

Why a router reads and never decides
------------------------------------
A conditional edge function's return value is not written to the checkpoint. LangGraph records
which node ran next; it does not record *why*, and there is no partial-state update to attach a
reason to. So anything a router works out for itself is invisible to the audit trail, to
`/incidents/{id}/decisions`, and to the operator reading a paused incident at three in the morning.

That is the whole design rule. **A node decides and records; a router reads the record.** Every
branch below is a question about something already in state, put there by a node that also wrote an
`AuditEvent` or a `PolicyDecision` explaining it. Where the specification says "use policy
configuration" (D15) or "thresholds must be configurable by action risk" (D06), the threshold is
applied by `policies.engine` and lands here as a `PolicyDecision` -- the router asks whether one was
recorded, never what the number was.

This is a choice rather than a limitation. `get_runtime(GraphContext).context` was measured to work
inside a conditional-edge function on langgraph 1.2.11 (see `tests/unit/test_graph_context.py`), so
these functions *could* reach the policy pack, the clock and the adapters. They do not, and the
signature `(IncidentState) -> str` is what enforces it.

Two consequences worth stating, because they are what makes the tests short:

* **A router is a pure function of a dict.** Testing D13 needs a dict with one key, not a compiled
  graph, a checkpointer and a live Postgres.
* **A router never raises.** An exception inside a conditional edge aborts the super-step, and a
  super-step that aborts writes *nothing* -- the incident rolls back to its previous checkpoint with
  no record of the attempt. Every function here is total: unset fields, `None`s and empty lists all
  produce a branch. The branch chosen for a missing input is the conservative one, and where that is
  not obvious the docstring says which way and why.

Where the bounds are, since they are not here
---------------------------------------------
Several decisions ("use a bounded enrichment retry", "retry with limits", "escalate if the boundary
cannot be established") pair a loop with a limit. The limit is *not* re-implemented here.
`graph.guards.check_budgets` owns all three bounds, nodes call it on entry and `escalation_update`
records the outcome as `escalated` plus a reason naming which budget and which owner supplied it.
The routers read `state["escalated"]`.

That is why only six routers offer a give-up branch -- D02, D05, D07, D13, D17, D23 -- and they are
exactly the six where the specification names a give-up remedy. Adding the branch anywhere else
would be a second bound with no owner. The remaining loops are bounded the same way, but the
escalation edge is wired by `graph.builder` from the guard rather than chosen here.

Branch names
------------
A branch names the **answer**, in the specification's own vocabulary where it has one --
`quarantine` is D01's own remedy, `clean`/`dirty`/`joint` D13's own question. It never names the
node the builder happens to wire it to. Node names change when the graph is refactored; the
answer to "which crew is required?" does not. `graph.builder` owns the answer-to-node mapping and
asserts each `path_map`'s keys against `Decision.branches` below, so the two cannot drift.

The `DECISIONS` table
---------------------
Every router appears in `DECISIONS` with its specification heading. That table is what
`graph.builder` iterates to wire `add_conditional_edges`, what `docs/` renders, and what
`tests/unit/test_routing.py` compares against the `### D01 — ...` headings parsed out of
`docs/specification.md`. One table, three readers: a decision cannot be implemented but unwired,
wired but undocumented, or renamed in the specification without a test failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from lpr_cpe.domain.boundaries import BACK_OFFICE_DOMAINS, crew_for
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    CaseType,
    CrewType,
    DelimiterKind,
    FaultDomain,
    MRStatus,
    TestStatus,
)
from lpr_cpe.graph.state import (
    IncidentState,
    current_mr_records,
    truck_roll_count,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from lpr_cpe.domain.diagnosis import TestResult
    from lpr_cpe.domain.field_ops import FieldFinding
    from lpr_cpe.domain.governance import ApprovalDecision, PolicyDecision
    from lpr_cpe.domain.resolution import ResolutionOption

# --------------------------------------------------------------------------------------------
# Vocabulary shared with the nodes that write it
# --------------------------------------------------------------------------------------------

#: `linked_records` keys meaning "this event already belongs to something larger". P04 writes them;
#: D03 reads them. Named once so the correlation node and the router cannot disagree about the
#: spelling -- a typo either side silently creates the duplicate incident D03 exists to prevent.
PARENT_RECORD_KEYS: tuple[str, ...] = (
    "parent_incident",
    "outage",
    "planned_maintenance",
    "service_problem",
)

#: The `linked_records` key under which P04 records earlier incidents on the same subject. Read by
#: D24, which is the only decision that cares about history beyond this incident.
PRIOR_INCIDENTS_KEY = "prior_incidents"

#: Case types that describe a risk rather than an outage. D04 may still route one to the active path
#: when impact assessment finds real customers affected; it never routes the other way.
PREVENTIVE_CASE_TYPES: frozenset[CaseType] = frozenset(
    {
        CaseType.PREDICTIVE_MAINTENANCE,
        CaseType.POST_INSTALL_BASELINE,
    }
)


# --------------------------------------------------------------------------------------------
# Shape of a resolution option
# --------------------------------------------------------------------------------------------
#
# D09, D11 and the field path each ask "is this option mine?", and the honest answer is already on
# the option: what it needs in order to run. A remote repair needs neither a van nor the customer; a
# guided self-help needs the customer but no van; anything needing a van is field work.
#
# This deliberately does *not* restate which `ActionType`s are remote. `policies.models.PolicyPack`
# owns that allowlist and its `_allowlist_is_exhaustive` validator refuses a pack that omits an
# action, so a second list here would be a second answer to a question that already has one -- and
# the one that drifts, because nothing loads it.


def is_remote_option(option: ResolutionOption) -> bool:
    """Executable from a console: no truck, no customer."""
    return not option.requires_truck_roll and not option.requires_customer_present


def is_self_help_option(option: ResolutionOption) -> bool:
    """The customer performs it. No truck, but nothing happens unless they are there."""
    return not option.requires_truck_roll and option.requires_customer_present


def is_field_option(option: ResolutionOption) -> bool:
    """Somebody drives to it. Whether the customer must also be present is a scheduling constraint,
    not a different class of work, which is why it does not appear here."""
    return option.requires_truck_roll


# --------------------------------------------------------------------------------------------
# Readers over the recorded governance trail
# --------------------------------------------------------------------------------------------


def latest_policy_decision(
    state: IncidentState, action_type: ActionType | None = None
) -> PolicyDecision | None:
    """The most recent policy evaluation, optionally for one action type.

    Filtering by action type is the default expectation and not an optimisation: one pass of the
    policy engine over a resolution plan records a decision per candidate action, so "the latest
    decision" without a subject is whichever candidate happened to be evaluated last. A router that
    read that would let a block on `olt_port_reset` suppress a `send_self_help`.
    """
    decisions = state.get("policy_decisions", [])
    if action_type is not None:
        decisions = [d for d in decisions if d.action_type is action_type]
    return decisions[-1] if decisions else None


def latest_decision_of(state: IncidentState, kind: ApprovalKind) -> ApprovalDecision | None:
    """The most recent human answer of one approval kind, or `None` if nobody has been asked."""
    matches = [a for a in state.get("approvals", []) if a.kind is kind]
    return max(matches, key=lambda a: a.decided_at, default=None)


def approval_outstanding(state: IncidentState, kind: ApprovalKind) -> bool:
    """Whether the most recent demand for this approval kind is still unanswered.

    Compared by timestamp rather than by presence, and that is what makes the gates terminate.
    `policy_decisions` and `approvals` are both append-only, so a demand recorded during the first
    diagnostic cycle is still in the list during the fourth. "Has anyone ever demanded this?" would
    therefore stay true forever and the gate would re-ask on every pass; "was the *latest* demand
    answered?" closes once, and re-opens exactly when a later policy evaluation demands it again --
    which is the correct behaviour for a second dispatch proposed after the first was rejected.

    Both timestamps are timezone-aware: `DomainModel` coerces every `datetime` field as it is
    constructed, so the comparison cannot raise the naive-vs-aware `TypeError` that would abort the
    super-step -- which is the one way a reader of two models could break the never-raises promise.
    """
    demands = [
        d.decided_at for d in state.get("policy_decisions", []) if d.required_approval_kind is kind
    ]
    if not demands:
        return False
    answers = [a.decided_at for a in state.get("approvals", []) if a.kind is kind]
    if not answers:
        return True
    return max(answers) < max(demands)


def approval_granted(state: IncidentState, kind: ApprovalKind) -> bool:
    """Whether the standing answer to this kind is `approved`. Absent counts as not granted."""
    answer = latest_decision_of(state, kind)
    return answer is not None and answer.status is ApprovalStatus.APPROVED


# --------------------------------------------------------------------------------------------
# Readers over evidence
# --------------------------------------------------------------------------------------------


def identity_is_resolved(state: IncidentState) -> bool:
    """Whether P03 produced enough of the chain for the decisions that follow to mean anything.

    "Enough" differs by case, and pretending otherwise is what makes this worth naming. A subscriber
    case must reach the tap or ODP, because D03 correlates neighbours across that delimiter and D17
    is defined in terms of it. A bare network alarm has no subscriber to hang a delimiter off, and
    demanding one would loop P03 until the guard escalated every node alarm the system receives; for
    those, naming the element is the whole of the requirement.
    """
    topology = state.get("topology")
    if topology is None:
        return False
    subject = state.get("cpe_ref") or state.get("service_ref") or state.get("customer_ref")
    if subject:
        return bool(topology.delimiter_ref) and topology.delimiter_kind is not DelimiterKind.UNKNOWN
    return bool(topology.node_ref or topology.olt_ref or topology.cmts_ref)


def latest_field_finding(state: IncidentState) -> FieldFinding | None:
    """The most recently recorded finding, or `None`.

    The *latest*, never `any(...)` across all of them. A second visit that discovers plant work must
    override the first visit's "resolved at the premises", and an `any()` over both would keep
    answering with whichever finding happened to be listed first.
    """
    findings = state.get("field_findings", [])
    return findings[-1] if findings else None


def latest_conclusive_test(state: IncidentState) -> TestResult | None:
    """The most recent test that actually produced a verdict.

    `inconclusive` and `unavailable` results are skipped rather than treated as failures: a test
    that could not run is not evidence that the service is broken, and reading it as one would send
    a reverse handover at D20 every time a probe timed out.
    """
    return next((r for r in reversed(state.get("test_results", [])) if r.conclusive), None)


# --------------------------------------------------------------------------------------------
# Stage 1 -- detect, validate, correlate
# --------------------------------------------------------------------------------------------


def route_event_validity(state: IncidentState) -> Literal["quarantine", "continue"]:
    """D01. Quarantine only on a *recorded* reason; an unassessed event continues.

    The asymmetry is deliberate. Quarantining costs a queue somebody has to drain; continuing costs
    an incident that D03 deduplicates and closure discards. Neither is free, but only one of them is
    silent, and the failure mode that matters here is a hurricane in which the assessing adapter is
    down and every real outage is dropped as "unassessed". So `data_quality is None` continues.

    A *blocking* flag is different. `ADAPTER_UNAVAILABLE`, `CONFLICTING_SOURCES` and
    `INCONSISTENT_TOPOLOGY` mean the subject of the event may be the wrong customer, and an incident
    opened against the wrong customer can end in a truck at the wrong address. That one is worth a
    queue. The remaining flags -- staleness, a missing field, no baseline -- weaken confidence
    without misidentifying anybody, and D05 is where they are weighed.
    """
    if not state.get("events"):
        return "quarantine"
    assessment = state.get("data_quality")
    if assessment is not None and assessment.blocking:
        return "quarantine"
    return "continue"


def route_identity_resolution(
    state: IncidentState,
) -> Literal["enrich", "manual_review", "continue"]:
    """D02. Retry enrichment until the guard says stop, then hand it to a human.

    The specification's "bounded enrichment retry" has its bound in `graph.guards`, not here: P03
    calls `check_budgets` on entry and `escalation_update` sets `escalated` when the node's re-entry
    ceiling is reached. So the loop and the limit stay in one place and this router only asks which
    of the two states the incident is in.

    `escalated` is checked *first*, before resolution. An incident that ran out of budget and then
    resolved on its final pass has still burned its budget, and continuing would leave the
    escalation recorded in the audit trail but not acted on -- the state and the graph disagreeing
    about whether a human was involved.
    """
    if state.get("escalated"):
        return "manual_review"
    if identity_is_resolved(state):
        return "continue"
    return "enrich"


def route_correlation(state: IncidentState) -> Literal["associate", "continue"]:
    """D03. Associate with the parent record P04 found, or carry on as a new candidate."""
    linked = state.get("linked_records", {})
    if any(linked.get(key) for key in PARENT_RECORD_KEYS):
        return "associate"
    return "continue"


def route_predictive_or_active(state: IncidentState) -> Literal["preventive", "active"]:
    """D04. A predictive case with live impact is an incident, whatever it was filed as.

    The override runs one way only. Impact assessment finding affected customers promotes a
    predictive case to the active path; nothing demotes an active case to preventive, because the
    cost of being wrong is a customer left in a maintenance queue during an outage.
    """
    if state.get("case_type") not in PREVENTIVE_CASE_TYPES:
        return "active"
    impact = state.get("impact")
    if impact is not None and impact.affected_customer_count > 0:
        return "active"
    return "preventive"


# --------------------------------------------------------------------------------------------
# Stage 2 -- evidence and diagnosis
# --------------------------------------------------------------------------------------------


def route_evidence_sufficiency(
    state: IncidentState,
) -> Literal["gather_more", "manual_review", "continue"]:
    """D05. `sufficient_for_action`, which is the model's answer and not this router's.

    Unlike D01, an absent assessment routes to `gather_more`. The remedy is cheap and reversible --
    reassemble evidence and ask again -- and the bound on how often is the same guard as everywhere
    else, which surfaces here as `manual_review`.
    """
    if state.get("escalated"):
        return "manual_review"
    assessment = state.get("data_quality")
    if assessment is None:
        return "gather_more"
    return "continue" if assessment.sufficient_for_action else "gather_more"


def route_rca_confidence(
    state: IncidentState,
) -> Literal["approve_low_confidence", "retry_diagnosis", "continue"]:
    """D06. Ask when policy demands it or when there is no root cause at all; obey the answer.

    A rejection routes to `retry_diagnosis`, not to `continue`. The reviewer rejecting a
    low-confidence RCA is saying the analysis is not good enough to act on, and the only branch that
    respects that is going back for more evidence. Routing a rejection forward would make the gate
    ceremonial.

    `rca is None` asks rather than continues. This is the one place fail-closed means *ask a human*
    rather than *stop*: an incident that reached the confidence gate with no analysis behind it is
    precisely what L2 review exists for.
    """
    if approval_outstanding(state, ApprovalKind.LOW_CONFIDENCE_RCA):
        return "approve_low_confidence"
    answer = latest_decision_of(state, ApprovalKind.LOW_CONFIDENCE_RCA)
    if answer is not None:
        return "continue" if answer.status is ApprovalStatus.APPROVED else "retry_diagnosis"
    if state.get("rca") is None:
        return "approve_low_confidence"
    return "continue"


# --------------------------------------------------------------------------------------------
# Stage 3 -- select and execute the resolution
# --------------------------------------------------------------------------------------------


def _candidate_decisions(state: IncidentState) -> list[PolicyDecision]:
    """The standing policy decision for each option still on the table.

    Per option and latest-per-action-type, so an old block on an action that has since been
    re-evaluated does not outvote the re-evaluation, and a block on an option nobody is proposing
    any more does not count at all.
    """
    plan = state.get("resolution_plan")
    if plan is None:
        return []
    out: list[PolicyDecision] = []
    for option in plan.untried():
        decision = latest_policy_decision(state, option.action_type)
        if decision is not None:
            out.append(decision)
    return out


def route_safety_and_blast_radius(
    state: IncidentState,
) -> Literal["approve_high_blast_radius", "escalate", "continue"]:
    """D07. Both remedies the specification offers, told apart in the policy engine's vocabulary.

    "Require human approval **or** escalation" is two different outcomes and the policy engine
    already distinguishes them: `requires_approval` is a question a supervisor can answer,
    `blocked` is a refusal no approval payload can override. So a standing demand routes to the
    gate, and a policy that has blocked every remaining option routes to a human -- there is nothing
    left for the graph to try.

    A *refused* high-blast-radius approval escalates rather than continuing, for the same reason
    D06's rejection goes back to diagnosis.

    An incident with no policy decisions at all continues. That is not this router failing open: it
    is this router refusing to invent a safety condition nobody recorded. The actions that matter
    are gated individually at D15 and by `policies.engine` before execution, and a workflow that
    reached here having never evaluated policy has a defect upstream that a spurious escalation here
    would hide rather than fix.
    """
    if approval_outstanding(state, ApprovalKind.HIGH_BLAST_RADIUS_ACTION):
        return "approve_high_blast_radius"
    answer = latest_decision_of(state, ApprovalKind.HIGH_BLAST_RADIUS_ACTION)
    if answer is not None and answer.status is not ApprovalStatus.APPROVED:
        return "escalate"
    candidates = _candidate_decisions(state)
    if candidates and all(d.blocked for d in candidates):
        return "escalate"
    return "continue"


def route_shared_or_plant(state: IncidentState) -> Literal["plant_path", "continue"]:
    """D08. Divert only the faults that need *no* premises visit.

    `crew_for(...) is DIRTY` rather than `is_plant_side(...)`, and the difference is the tap. The
    tap and the ODP are plant-side by the responsibility boundary but their remedy is a joint
    dispatch, so sending them down the plant path here would skip the Clean Boots half of a joint
    visit and strand the customer's side of the fault. `domain.boundaries` already draws that
    distinction; asking it the question it answers is cheaper than restating the domain list and
    getting the tap wrong, which is the mistake this line exists to not make.

    Back-office domains -- provisioning, service platform -- join the same branch. No crew attends
    them, but they are equally not a truck roll, and D08's remedy list names them explicitly.
    """
    domain = state.get("fault_domain", FaultDomain.UNKNOWN)
    if crew_for(domain) is CrewType.DIRTY or domain in BACK_OFFICE_DOMAINS:
        return "plant_path"
    return "continue"


def route_remote_eligibility(state: IncidentState) -> Literal["remote", "self_help_check"]:
    """D09. An untried console-executable option that policy has not blocked.

    Eligibility's long list -- confidence, risk, CPE state, blast radius, firmware compatibility,
    rollback, authorisation -- is evaluated by `policies.engine` and arrives as a `PolicyDecision`;
    "prior failed attempts" is `ResolutionPlan.untried()`. Both are read, neither is recomputed.

    `requires_approval` is *not* treated as ineligible. A remote repair that needs a supervisor is
    still the right resolution, and the subgraph behind this branch raises the interrupt. Skipping
    to self-help because a question would have to be asked is how an approval gate turns into a
    silent downgrade of the remedy.
    """
    plan = state.get("resolution_plan")
    if plan is None:
        return "self_help_check"
    for option in plan.untried():
        if not is_remote_option(option):
            continue
        decision = latest_policy_decision(state, option.action_type)
        if decision is not None and decision.blocked:
            continue
        return "remote"
    return "self_help_check"


def route_remote_outcome(state: IncidentState) -> Literal["verify", "retry_diagnosis"]:
    """D10. `fixed_it` -- succeeded *and* verified. An unverified success is not a restoration."""
    actions = state.get("remote_actions", [])
    if any(action.fixed_it for action in actions):
        return "verify"
    return "retry_diagnosis"


def route_self_help_suitability(state: IncidentState) -> Literal["self_help", "field_planning"]:
    """D11. An untried option the customer can perform, that policy has not blocked.

    Suitability's inputs -- language, complexity, safety, customer presence, likelihood of success
    -- belong to whoever built the option, which is why the question here is only whether such an
    option survived into the plan. A self-help option judged unsuitable is one never generated.
    """
    plan = state.get("resolution_plan")
    if plan is None:
        return "field_planning"
    for option in plan.untried():
        if not is_self_help_option(option):
            continue
        decision = latest_policy_decision(state, option.action_type)
        if decision is not None and decision.blocked:
            continue
        return "self_help"
    return "field_planning"


def route_self_help_outcome(
    state: IncidentState,
) -> Literal["verify", "retry_diagnosis", "field_planning"]:
    """D12. Re-diagnose while options remain; stop re-diagnosing once they do not.

    "Return to diagnosis or proceed to field planning according to policy" is answered by
    `ResolutionPlan.exhausted`, which is a recorded fact rather than a threshold. While untried
    options remain, another diagnostic pass can still change which one is chosen -- that is the
    "re-diagnose before repeating work" principle. Once every option has been attempted, a further
    pass over the same evidence produces the same plan, and looping there would burn the graph's
    step budget to arrive at field planning anyway.
    """
    session = state.get("self_help_session")
    if session is not None and session.outcome == "resolved":
        return "verify"
    plan = state.get("resolution_plan")
    if plan is None or plan.exhausted:
        return "field_planning"
    return "retry_diagnosis"


def route_dispatch_type(state: IncidentState) -> Literal["clean", "dirty", "joint", "escalate"]:
    """D13. `domain.boundaries.crew_for`, and an explicit branch for its `None`.

    `None` is a real answer with three causes -- back-office, no fault found, diagnosis incomplete
    -- and none is a crew. Mapping it to a default crew is the failure this decision is written
    to avoid: `unknown` silently becoming `clean` is a truck sent to a customer whose fault nobody
    has located yet.

    The mapping is spelled out member by member rather than returned as `crew.value`. The two are
    equal today, and a `Literal` return type that mypy can check is worth more than the line it
    saves: a fourth `CrewType` member would be a type error here rather than a branch name the
    builder has no edge for.
    """
    crew = crew_for(state.get("fault_domain", FaultDomain.UNKNOWN))
    if crew is CrewType.CLEAN:
        return "clean"
    if crew is CrewType.DIRTY:
        return "dirty"
    if crew is CrewType.JOINT:
        return "joint"
    return "escalate"


def route_dispatch_constraints(state: IncidentState) -> Literal["queue_for_dispatcher", "continue"]:
    """D14. Every requirement assigned, or a dispatcher looks at it. Never a partial commit.

    Checking coverage of `dispatch_requirements` as well as `unassigned` is not belt and braces: an
    optimiser that returned a plan omitting a requirement entirely would have an empty `unassigned`
    list and satisfy the first check while leaving a crew unscheduled. `DispatchPlan` explains its
    own `unassigned` entries; this asks the complementary question, which is whether anything went
    missing without being explained.
    """
    plan = state.get("dispatch_plan")
    if plan is None:
        return "queue_for_dispatcher"
    if plan.unassigned or not plan.assignments:
        return "queue_for_dispatcher"
    required = {
        requirement.requirement_id for requirement in state.get("dispatch_requirements", [])
    }
    if required - plan.assigned_requirement_ids:
        return "queue_for_dispatcher"
    return "continue"


def route_dispatch_approval(
    state: IncidentState,
) -> Literal["approve_dispatch", "commit", "replan"]:
    """D15. The default is to ask. Only a recorded policy allowance skips the gate.

    "By default, require human approval before committing a field slot and reserving parts" makes
    silence mean *ask*, so an incident with no policy decision and no answer routes to the gate. The
    only path straight to `commit` is a `PolicyDecision` for `create_work_order` that is allowed and
    demands no approval kind -- an explicit, versioned, auditable statement that this dispatch does
    not need a human, rather than the absence of one saying it does.

    A rejection routes to `replan`, not to `commit` and not back to the same gate. The dispatcher
    refusing a slot is refusing *that* slot; re-optimising is the response, and it is what produces
    the later policy evaluation that re-opens the gate through `approval_outstanding`.
    """
    if approval_outstanding(state, ApprovalKind.DISPATCH):
        return "approve_dispatch"
    answer = latest_decision_of(state, ApprovalKind.DISPATCH)
    if answer is not None:
        return "commit" if answer.status is ApprovalStatus.APPROVED else "replan"
    decision = latest_policy_decision(state, ActionType.CREATE_WORK_ORDER)
    if decision is not None and decision.allowed and decision.required_approval_kind is None:
        return "commit"
    return "approve_dispatch"


# --------------------------------------------------------------------------------------------
# Stage 4 -- Clean Boots execution and handover
# --------------------------------------------------------------------------------------------


def route_clean_boots_outcome(state: IncidentState) -> Literal["validate", "delimit"]:
    """D16. Finished, and finished *inside* the premises domain -- both, or it is a delimiting job.

    `work_completed and not requires_plant_work`. A technician who replaced a drop and also recorded
    that the tap needs work has not resolved it within the Clean Boots domain, and reading only
    `work_completed` would send that case to validation and then round the loop again when
    validation failed, having lost the finding that said where to go next.
    """
    finding = latest_field_finding(state)
    if finding is None:
        return "delimit"
    if finding.work_completed and not finding.requires_plant_work:
        return "validate"
    return "delimit"


def route_delimiter_evidence(
    state: IncidentState,
) -> Literal["handover", "more_tests", "escalate"]:
    """D17. Three things, all of them, before an MR exists: a finding, plant work, a delimiter.

    "Do not create an incomplete MR" is the requirement, and each clause here is one way an MR can
    be incomplete. A finding naming `tap_or_odp` with `delimiter_ref` unset places the fault beyond
    a boundary nobody can dispatch to.

    There is deliberately no fourth clause checking that the fault domain is plant-side. It reads
    like the obvious safety net and it is unreachable: `FieldFinding` refuses to construct with
    `requires_plant_work=True` and a premises-side domain -- "OSP cannot action this" -- so once the
    second clause has passed, `is_plant_side(finding.fault_domain)` holds by construction. The
    mutation sweep is what found it, by removing the clause and watching nothing go red. A branch no
    state can enter is a branch no test can hold to account, and keeping it would have implied this
    router owns a boundary rule that `domain.field_ops` and `domain.boundaries` already own between
    them.

    `escalate` is the specification's "escalate if the boundary cannot be established", and it
    arrives the same way as everywhere else -- the guard bounds the `more_tests` loop and sets
    `escalated`. There is no separate count of attempts here.
    """
    if state.get("escalated"):
        return "escalate"
    finding = latest_field_finding(state)
    if finding is None or not finding.requires_plant_work:
        return "more_tests"
    if finding.delimiter_kind is DelimiterKind.UNKNOWN or not finding.delimiter_ref:
        return "more_tests"
    return "handover"


def route_handover_validation(state: IncidentState) -> Literal["request_approval", "reject"]:
    """D18. `HandoverContract.complete`, which is the contract's own audit of its 24 required items.

    The list belongs to the model -- `missing_items()` enumerates what is absent and `complete` is
    derived from it -- so a router that re-checked individual fields would be a second, shorter
    version of the same list, and the shorter one always wins by accident.

    `accepted is False` is checked separately from completeness because they fail for different
    reasons: a complete contract can still be rejected by the receiving owner as duplicative, and
    that rejection carries a `rejection_reason` the diagnosis path needs.
    """
    contract = state.get("handover_contract")
    if contract is None:
        return "reject"
    if contract.accepted is False:
        return "reject"
    return "request_approval" if contract.complete else "reject"


def route_plant_outcome(
    state: IncidentState,
) -> Literal["restored", "await_plant", "retry_diagnosis"]:
    """D19. Three outcomes, because "not restored" and "not finished" are not the same thing.

    An MR still with OSP has its own branch. Collapsing it into `retry_diagnosis` would re-open
    diagnosis while a crew is standing at the pole, and the specification is explicit that the
    response to a failure is to re-diagnose *and* "do not automatically duplicate the MR" -- which
    is exactly what a diagnosis pass fired against an in-flight MR would eventually do.

    No MR at all routes to `retry_diagnosis`. Every path into Stage 4's plant branch runs through
    P20, which creates or updates one, so reaching D19 with none means the plant action produced no
    record -- and an unrecorded action is not a restoration.
    """
    records = current_mr_records(state)
    if not records:
        return "retry_diagnosis"
    latest = max(records.values(), key=lambda record: record.updated_at)
    if latest.status in (MRStatus.COMPLETED, MRStatus.CLOSED):
        return "restored"
    if latest.awaiting_osp:
        return "await_plant"
    return "retry_diagnosis"


def route_residual_customer_impact(state: IncidentState) -> Literal["reverse_handover", "verify"]:
    """D20. A failed customer-side test after plant restoration sends the case back to Clean Boots.

    Silence routes to `verify`, which is not the same as declaring the customer fixed: P22 runs the
    validation and D21 refuses to close on it. So the conservative branch is the one that gathers
    evidence, and a reverse handover -- a second truck, a second work order -- is reserved for a
    test that actually failed.
    """
    result = latest_conclusive_test(state)
    if result is not None and result.status is TestStatus.FAILED:
        return "reverse_handover"
    return "verify"


# --------------------------------------------------------------------------------------------
# Stage 5 -- verify, reconcile, close, learn
# --------------------------------------------------------------------------------------------


def route_stability(
    state: IncidentState,
) -> Literal["continue_observation", "retry_diagnosis", "confirm_outcome"]:
    """D21. The specification's three outcomes, in the order that makes each reachable.

    A regression is checked before the window, because a metric that got *worse* is degradation
    remaining and no amount of further observation improves it. An incomplete window is checked
    before the final fallthrough, because that is "improving but incomplete". What is left --
    window complete, nothing regressed, still not passing -- is a fix that did not take.

    An absent validation observes rather than closes. `ValidationResult` refuses to record `passed`
    without a completed window, so the only thing missing evidence can mean here is that P22 has not
    finished.
    """
    validation = state.get("validation")
    if validation is None:
        return "continue_observation"
    if validation.passed:
        return "confirm_outcome"
    if validation.regressed_metrics:
        return "retry_diagnosis"
    if not validation.window_complete:
        return "continue_observation"
    return "retry_diagnosis"


def route_resolution(state: IncidentState) -> Literal["reconcile", "retry_diagnosis"]:
    """D22. A customer who says it is not fixed outranks telemetry that says it is.

    `customer_confirmed is False`, not falsiness: `None` means P23 decided telemetry was sufficient
    and nobody was asked, which is the specification's own rule for when to ask. Treating that as a
    denial would send every incident that did not need a phone call back to diagnosis.
    """
    validation = state.get("validation")
    if validation is None:
        return "retry_diagnosis"
    if validation.customer_confirmed is False:
        return "retry_diagnosis"
    return "reconcile" if validation.passed else "retry_diagnosis"


def route_reconciliation(state: IncidentState) -> Literal["close", "reconcile_retry", "escalate"]:
    """D23. `ReconciliationResult.consistent`, which counts an unreachable system as inconsistent.

    That is the model's decision and the right one: a system nobody could reach has not been shown
    to agree, and closing an incident whose ticket may still be open in a system that timed out is
    premature closure Stage 5 exists to prevent.
    """
    if state.get("escalated"):
        return "escalate"
    result = state.get("reconciliation")
    if result is None:
        return "reconcile_retry"
    return "close" if result.consistent else "reconcile_retry"


def route_chronic_pattern(state: IncidentState) -> Literal["chronic", "done"]:
    """D24. Four independent signals, any one of which makes this a repeat.

    `truck_roll_count(state)` rather than `ClosureRecord.truck_rolls`, although by D24 both exist
    and hold the same number. The closure record copies the state helper, so reading the copy would
    put a second reader on a derived value and make the two capable of disagreeing; the helper is
    the one owner.

    "Do not hide chronic problems by treating every recurrence as isolated" is why the signals are
    OR-ed rather than scored. A case filed as a repeat visit is chronic even if this particular
    visit went perfectly.
    """
    if state.get("case_type") is CaseType.REPEAT_VISIT:
        return "chronic"
    if truck_roll_count(state) > 1:
        return "chronic"
    if state.get("mr_attempt_count", 0) > 1:
        return "chronic"
    if state.get("linked_records", {}).get(PRIOR_INCIDENTS_KEY):
        return "chronic"
    return "done"


# --------------------------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision point: its identifier, the question, the router, and the answers it may give.

    `branches` is stated separately from the router's `Literal` return type on purpose. They are two
    independent claims about the same function -- one the type checker reads, one `graph.builder`
    reads -- and `tests/unit/test_routing.py` asserts they agree. A single source would make the
    agreement unfalsifiable.
    """

    id: str
    question: str
    route: Callable[[IncidentState], str]
    branches: tuple[str, ...]


def _decision(
    identifier: str, question: str, route: Callable[[IncidentState], str], *branches: str
) -> tuple[str, Decision]:
    return identifier, Decision(identifier, question, route, branches)


#: Every conditional transition in the workflow, keyed by its specification identifier.
#:
#: `graph.builder` iterates this to wire `add_conditional_edges`; the tests compare each `question`
#: against the `### D01 — ...` headings in `docs/specification.md`. A decision that is implemented
#: but missing here is unwired and unreachable, which the coverage assertion in the test module
#: catches by counting.
DECISIONS: Mapping[str, Decision] = dict(
    (
        _decision(
            "D01",
            "Is the event valid and actionable?",
            route_event_validity,
            "quarantine",
            "continue",
        ),
        _decision(
            "D02",
            "Is identity and topology sufficiently resolved?",
            route_identity_resolution,
            "enrich",
            "manual_review",
            "continue",
        ),
        _decision(
            "D03",
            "Is this planned work, a known outage, or part of an existing common cause?",
            route_correlation,
            "associate",
            "continue",
        ),
        _decision(
            "D04",
            "Is this predictive risk only or an active service incident?",
            route_predictive_or_active,
            "preventive",
            "active",
        ),
        _decision(
            "D05",
            "Is the evidence complete and fresh enough for the next decision?",
            route_evidence_sufficiency,
            "gather_more",
            "manual_review",
            "continue",
        ),
        _decision(
            "D06",
            "Is root-cause confidence sufficient for the proposed action?",
            route_rca_confidence,
            "approve_low_confidence",
            "retry_diagnosis",
            "continue",
        ),
        _decision(
            "D07",
            "Is there a safety, security, or high-blast-radius condition?",
            route_safety_and_blast_radius,
            "approve_high_blast_radius",
            "escalate",
            "continue",
        ),
        _decision(
            "D08",
            "Is this a shared network, provisioning, or plant issue?",
            route_shared_or_plant,
            "plant_path",
            "continue",
        ),
        _decision(
            "D09",
            "Is an allowlisted remote repair eligible?",
            route_remote_eligibility,
            "remote",
            "self_help_check",
        ),
        _decision(
            "D10",
            "Did remote repair produce stable restoration?",
            route_remote_outcome,
            "verify",
            "retry_diagnosis",
        ),
        _decision(
            "D11",
            "Is guided customer self-help suitable?",
            route_self_help_suitability,
            "self_help",
            "field_planning",
        ),
        _decision(
            "D12",
            "Did self-help produce stable restoration?",
            route_self_help_outcome,
            "verify",
            "retry_diagnosis",
            "field_planning",
        ),
        _decision(
            "D13",
            "Which dispatch type is required: Clean Boots, Dirty Boots, or joint?",
            route_dispatch_type,
            "clean",
            "dirty",
            "joint",
            "escalate",
        ),
        _decision(
            "D14",
            "Are all dispatch constraints satisfied?",
            route_dispatch_constraints,
            "queue_for_dispatcher",
            "continue",
        ),
        _decision(
            "D15",
            "Is dispatch approval required?",
            route_dispatch_approval,
            "approve_dispatch",
            "commit",
            "replan",
        ),
        _decision(
            "D16",
            "Was the issue resolved within the Clean Boots service domain?",
            route_clean_boots_outcome,
            "validate",
            "delimit",
        ),
        _decision(
            "D17",
            "Is evidence sufficient to place the fault beyond the HFC tap or PON ODP boundary?",
            route_delimiter_evidence,
            "handover",
            "more_tests",
            "escalate",
        ),
        _decision(
            "D18",
            "Is the handover complete and non-duplicative?",
            route_handover_validation,
            "request_approval",
            "reject",
        ),
        _decision(
            "D19",
            "Did the Dirty Boots or plant action restore the affected network domain?",
            route_plant_outcome,
            "restored",
            "await_plant",
            "retry_diagnosis",
        ),
        _decision(
            "D20",
            "Is customer service still degraded after plant restoration?",
            route_residual_customer_impact,
            "reverse_handover",
            "verify",
        ),
        _decision(
            "D21",
            "Is the service stable for the required observation window?",
            route_stability,
            "continue_observation",
            "retry_diagnosis",
            "confirm_outcome",
        ),
        _decision(
            "D22",
            "Is the incident resolved?",
            route_resolution,
            "reconcile",
            "retry_diagnosis",
        ),
        _decision(
            "D23",
            "Are all linked records consistent?",
            route_reconciliation,
            "close",
            "reconcile_retry",
            "escalate",
        ),
        _decision(
            "D24",
            "Is this a chronic or repeating pattern?",
            route_chronic_pattern,
            "chronic",
            "done",
        ),
    )
)
