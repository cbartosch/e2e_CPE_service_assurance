"""The twenty-four decision points, exercised one branch at a time.

Three habits, the same ones the boundary and policy tests are built on.

**A hand-written table, not a derived one.** `REACHING_STATES` names, for every decision and every
branch it declares, a state that reaches it. Nothing in it is computed from `routing.py`. A table
built by running the routers and recording what came back would agree with the implementation by
construction and would go on agreeing after the implementation broke.

**Check a partition, not a membership.** `REACHING_STATES` is parametrised over `DECISIONS` and each
decision's `branches`, so a branch added without a state is a collection error rather than an
untested path -- and `test_no_router_ever_answers_outside_its_declared_branches` runs every router
over every state in the corpus, so a router that answers something it never declared is caught even
when the state was built for a different decision.

**Assert the reasoning, not just the outcome.** The reachability table proves each branch is
*possible*. It does not prove the router chose it for the right reason, and most of the mistakes
worth catching here are plausible near-misses that the table would still pass: reading the first
field finding rather than the latest, treating "nobody asked the customer" as "the customer said
no", collapsing an MR still with OSP into a failure. Each of those has a named test.

Each named defect below was reinstated in `routing.py` and the test named for it watched to fail --
46 mutations, 46 caught, and the first pass of 48 left six survivors that were worth more than the
42 catches. Four were real gaps and are now tests in this file; one was a branch of `routing.py` no
state could reach, since removed; one was provably equivalent. IMPLEMENTATION_PLAN.md §5 records
which was which. That is the claim being made here -- not that all 233 assertions were mutated
individually, which they were not.
"""

from __future__ import annotations

import re
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lpr_cpe.domain.closure import ReconciliationResult, ValidationResult
from lpr_cpe.domain.diagnosis import ImpactAssessment, RCAResult, TestResult
from lpr_cpe.domain.enums import (
    ActionOutcome,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    CaseType,
    CommunicationChannel,
    CrewType,
    DataQualityFlag,
    DelimiterKind,
    EventSource,
    FaultDomain,
    MRStatus,
    PolicyOutcome,
    ReasonCode,
    Technology,
    TestKind,
    TestStatus,
    WorkOrderStatus,
)
from lpr_cpe.domain.field_ops import (
    DispatchAssignment,
    DispatchPlan,
    DispatchRequirement,
    FieldFinding,
    HandoverContract,
    MRRecord,
    WorkOrder,
)
from lpr_cpe.domain.governance import ApprovalDecision, PolicyDecision
from lpr_cpe.domain.records import AssuranceEvent, DataQualityAssessment, TopologyContext
from lpr_cpe.domain.resolution import (
    RemoteAction,
    ResolutionOption,
    ResolutionPlan,
    SelfHelpSession,
)
from lpr_cpe.graph.routing import (
    DECISIONS,
    Decision,
    approval_outstanding,
    identity_is_resolved,
    is_field_option,
    is_remote_option,
    is_self_help_option,
    latest_decision_of,
    latest_field_finding,
)
from lpr_cpe.graph.state import IncidentState, current_mr_records

AT = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)
LATER = AT + timedelta(hours=1)
LATEST = AT + timedelta(hours=2)

SPECIFICATION = Path(__file__).resolve().parents[2] / "docs" / "specification.md"

# ------------------------------------------------------------------------------------------------
# Builders. Minimal valid instances -- every default here is one the routers do not read, so a test
# that cares about a field always sets it and a reader can see which fields a decision depends on.
# ------------------------------------------------------------------------------------------------


def _state(**overrides: Any) -> IncidentState:
    return IncidentState(**overrides)  # type: ignore[typeddict-item]


def _event() -> AssuranceEvent:
    return AssuranceEvent(
        event_id="EV-1",
        source=EventSource.NXT,
        case_type=CaseType.PROACTIVE_ALARM,
        occurred_at=AT,
        received_at=AT,
        summary="downstream SNR degraded",
        service_ref="SVC-1",
    )


def _quality(*flags: DataQualityFlag, completeness: float = 1.0) -> DataQualityAssessment:
    return DataQualityAssessment(assessed_at=AT, flags=list(flags), completeness_score=completeness)


def _topology(**overrides: Any) -> TopologyContext:
    base: dict[str, Any] = {
        "technology": Technology.HFC,
        "delimiter_kind": DelimiterKind.TAP,
        "delimiter_ref": "TAP-1",
        "node_ref": "NODE-1",
    }
    base.update(overrides)
    return TopologyContext(**base)


def _impact(count: int, *refs: str) -> ImpactAssessment:
    """An assessment whose population and whose *observed* set are supplied independently.

    Two arguments because D04 is the decision that turns on which of the two it reads, and a helper
    that derived one from the other would make the distinction untestable.
    """
    return ImpactAssessment(
        assessed_at=AT,
        affected_customer_count=count,
        count_is_estimated=False,
        affected_service_refs=list(refs),
    )


def _rca(confidence: float = 0.9) -> RCAResult:
    return RCAResult(concluded_at=AT, fault_domain=FaultDomain.CPE, confidence=confidence)


def _policy(
    *,
    action_type: ActionType = ActionType.CPE_REBOOT,
    outcome: PolicyOutcome = PolicyOutcome.ALLOWED,
    kind: ApprovalKind | None = None,
    at: datetime = AT,
    decision_id: str = "POL-1",
) -> PolicyDecision:
    codes = () if outcome is PolicyOutcome.ALLOWED else (ReasonCode.POLICY_APPROVAL_REQUIRED,)
    return PolicyDecision(
        decision_id=decision_id,
        decided_at=at,
        action_type=action_type,
        outcome=outcome,
        reason_codes=codes,
        policy_version="test-1",
        required_approval_kind=kind,
    )


def _approval(
    kind: ApprovalKind,
    status: ApprovalStatus,
    *,
    at: datetime = LATER,
    approval_id: str | None = None,
) -> ApprovalDecision:
    # The id defaults to one per kind, which is the usual case. It is overridable because
    # `approvals` is de-duplicated on it: two answers to the same kind are two records, and a test
    # that gave them one id would be asserting against a list the reducer would have collapsed.
    return ApprovalDecision(
        approval_id=approval_id or f"APR-{kind.value}",
        incident_id="INC-1",
        kind=kind,
        status=status,
        decided_at=at,
        decided_by="sam",
        decided_by_role="supervisor",
        rationale="because",
    )


def _option(
    *,
    option_id: str = "OPT-1",
    action_type: ActionType = ActionType.CPE_REBOOT,
    truck: bool = False,
    customer: bool = False,
) -> ResolutionOption:
    return ResolutionOption(
        option_id=option_id,
        action_type=action_type,
        target_ref="CPE-1",
        label="reboot the gateway",
        addresses_domain=FaultDomain.CPE,
        estimated_success_probability=0.6,
        requires_truck_roll=truck,
        requires_customer_present=customer,
    )


def _plan(options: list[ResolutionOption], *, attempted: list[str] | None = None) -> ResolutionPlan:
    return ResolutionPlan(
        plan_id="PLAN-1",
        created_at=AT,
        fault_domain=FaultDomain.CPE,
        options=options,
        attempted_option_ids=attempted or [],
    )


def _remote_action(*, fixed: bool) -> RemoteAction:
    return RemoteAction(
        action_id="ACT-1",
        action_type=ActionType.CPE_REBOOT,
        target_ref="CPE-1",
        idempotency_key="KEY-1",
        requested_at=AT,
        outcome=ActionOutcome.SUCCEEDED if fixed else ActionOutcome.FAILED,
        verification_passed=True if fixed else None,
        verified_at=LATER if fixed else None,
    )


def _session(outcome: str) -> SelfHelpSession:
    return SelfHelpSession(
        session_id="SH-1",
        incident_id="INC-1",
        channel=CommunicationChannel.SMS,
        started_at=AT,
        outcome=outcome,
    )


def _requirement(requirement_id: str = "REQ-1") -> DispatchRequirement:
    return DispatchRequirement(
        requirement_id=requirement_id,
        incident_id="INC-1",
        created_at=AT,
        crew_type=CrewType.CLEAN,
        fault_domain=FaultDomain.CPE,
    )


def _dispatch_plan(
    *, assigned: list[str] | None = None, unassigned: list[str] | None = None
) -> DispatchPlan:
    return DispatchPlan(
        plan_id="DP-1",
        created_at=AT,
        assignments=[
            DispatchAssignment(
                requirement_id=requirement_id,
                crew_id="CREW-1",
                crew_type=CrewType.CLEAN,
                scheduled_start=LATER,
                scheduled_end=LATEST,
            )
            for requirement_id in (assigned or [])
        ],
        unassigned=unassigned or [],
        constraint_explanation=dict.fromkeys(unassigned or [], "no crew with the skill"),
    )


def _finding(finding_id: str = "FF-1", **overrides: Any) -> FieldFinding:
    base: dict[str, Any] = {
        "finding_id": finding_id,
        "work_order_id": "WO-1",
        "incident_id": "INC-1",
        "recorded_at": AT,
        "recorded_by": "tech-9",
        "fault_domain": FaultDomain.CPE,
    }
    base.update(overrides)
    return FieldFinding(**base)


def _contract(**overrides: Any) -> HandoverContract:
    base: dict[str, Any] = {
        "contract_id": "HC-1",
        "incident_id": "INC-1",
        "created_at": AT,
        "technology": "hfc",
        "fault_domain": FaultDomain.TAP_OR_ODP,
        "delimiter_kind": DelimiterKind.TAP,
        "delimiter_ref": "TAP-1",
        "measurements": {
            "downstream_power_dbmv": 3.0,
            "upstream_power_dbmv": 44.0,
            "downstream_snr_db": 31.0,
        },
        "ruled_out": ["cpe"],
        "field_finding_ids": ["FF-1"],
    }
    base.update(overrides)
    return HandoverContract(**base)


def _mr(status: MRStatus, *, mr_id: str = "MR-1", updated_at: datetime = AT) -> MRRecord:
    return MRRecord(
        mr_id=mr_id, incident_id="INC-1", created_at=AT, updated_at=updated_at, status=status
    )


def _validation(**overrides: Any) -> ValidationResult:
    base: dict[str, Any] = {
        "validation_id": "VAL-1",
        "incident_id": "INC-1",
        "validated_at": LATER,
        "window_start": AT,
        "stability_window": timedelta(minutes=30),
        "samples_in_window": 4,
        "min_samples_required": 2,
    }
    base.update(overrides)
    return ValidationResult(**base)


def _open_window(**overrides: Any) -> ValidationResult:
    """A validation whose stability window has not elapsed yet.

    Named because the obvious knob is the wrong one. `window_complete` is
    `validated_at >= window_start + stability_window` -- it does not read `samples_in_window` at
    all; that count belongs to the *pass* rule in `_pass_requires_a_window`. Zeroing the samples
    leaves the window complete, which routes D21 to "a fix that did not take" rather than to "still
    watching", and the test would then assert the right branch for the wrong reason.
    """
    return _validation(stability_window=timedelta(hours=4), **overrides)


def _reconciliation(**overrides: Any) -> ReconciliationResult:
    base: dict[str, Any] = {
        "reconciliation_id": "REC-1",
        "incident_id": "INC-1",
        "reconciled_at": LATER,
        "systems_checked": ["nxt", "crm"],
    }
    base.update(overrides)
    return ReconciliationResult(**base)


def _test(status: TestStatus, *, result_id: str = "TR-1") -> TestResult:
    return TestResult(
        result_id=result_id,
        request_id="REQ-1",
        kind=TestKind.CPE_CONNECTIVITY,
        target_ref="CPE-1",
        status=status,
        started_at=AT,
    )


def _work_order(status: WorkOrderStatus, *, work_order_id: str = "WO-1") -> WorkOrder:
    return WorkOrder(
        work_order_id=work_order_id,
        incident_id="INC-1",
        crew_type=CrewType.CLEAN,
        status=status,
        created_at=AT,
        updated_at=AT,
    )


# ------------------------------------------------------------------------------------------------
# One state per branch, written by hand
# ------------------------------------------------------------------------------------------------
#
# Read this as the specification's decision table restated in the vocabulary of `IncidentState`. It
# is the second, independent statement of what each router does; `routing.py` is the first.

REACHING_STATES: dict[str, dict[str, IncidentState]] = {
    "D01": {
        "quarantine": _state(
            events=[_event()], data_quality=_quality(DataQualityFlag.CONFLICTING_SOURCES)
        ),
        "continue": _state(events=[_event()]),
    },
    "D02": {
        "manual_review": _state(escalated=True),
        "continue": _state(cpe_ref="CPE-1", topology=_topology()),
        "enrich": _state(cpe_ref="CPE-1"),
    },
    "D03": {
        "associate": _state(linked_records={"outage": "OUT-1"}),
        "continue": _state(linked_records={"customer_ticket": "TT-1"}),
    },
    "D04": {
        "preventive": _state(case_type=CaseType.PREDICTIVE_MAINTENANCE),
        "active": _state(case_type=CaseType.CUSTOMER_REPORTED),
    },
    "D05": {
        "manual_review": _state(escalated=True),
        "gather_more": _state(data_quality=_quality(completeness=0.1)),
        "continue": _state(data_quality=_quality()),
    },
    "D06": {
        "approve_low_confidence": _state(),
        "retry_diagnosis": _state(
            approvals=[_approval(ApprovalKind.LOW_CONFIDENCE_RCA, ApprovalStatus.REJECTED)]
        ),
        "continue": _state(rca=_rca()),
    },
    "D07": {
        "approve_high_blast_radius": _state(
            policy_decisions=[
                _policy(
                    outcome=PolicyOutcome.REQUIRES_APPROVAL,
                    kind=ApprovalKind.HIGH_BLAST_RADIUS_ACTION,
                )
            ]
        ),
        "escalate": _state(
            approvals=[_approval(ApprovalKind.HIGH_BLAST_RADIUS_ACTION, ApprovalStatus.REJECTED)]
        ),
        "continue": _state(rca=_rca()),
    },
    "D08": {
        "plant_path": _state(fault_domain=FaultDomain.FEEDER),
        "continue": _state(fault_domain=FaultDomain.CPE),
    },
    "D09": {
        "remote": _state(resolution_plan=_plan([_option()])),
        "self_help_check": _state(resolution_plan=_plan([_option(truck=True)])),
    },
    "D10": {
        "verify": _state(remote_actions=[_remote_action(fixed=True)]),
        "retry_diagnosis": _state(remote_actions=[_remote_action(fixed=False)]),
    },
    "D11": {
        "self_help": _state(
            resolution_plan=_plan([_option(action_type=ActionType.SEND_SELF_HELP, customer=True)])
        ),
        "field_planning": _state(resolution_plan=_plan([_option(truck=True)])),
    },
    "D12": {
        "verify": _state(self_help_session=_session("resolved")),
        "retry_diagnosis": _state(
            self_help_session=_session("not_resolved"), resolution_plan=_plan([_option()])
        ),
        "field_planning": _state(
            self_help_session=_session("not_resolved"),
            resolution_plan=_plan([_option()], attempted=["OPT-1"]),
        ),
    },
    "D13": {
        "clean": _state(fault_domain=FaultDomain.CPE),
        "dirty": _state(fault_domain=FaultDomain.FEEDER),
        "joint": _state(fault_domain=FaultDomain.TAP_OR_ODP),
        "escalate": _state(fault_domain=FaultDomain.NO_FAULT_FOUND),
    },
    "D14": {
        "queue_for_dispatcher": _state(
            dispatch_requirements=[_requirement()],
            dispatch_plan=_dispatch_plan(unassigned=["REQ-1"]),
        ),
        "continue": _state(
            dispatch_requirements=[_requirement()],
            dispatch_plan=_dispatch_plan(assigned=["REQ-1"]),
        ),
    },
    "D15": {
        "approve_dispatch": _state(),
        "commit": _state(approvals=[_approval(ApprovalKind.DISPATCH, ApprovalStatus.APPROVED)]),
        "replan": _state(approvals=[_approval(ApprovalKind.DISPATCH, ApprovalStatus.REJECTED)]),
    },
    "D16": {
        "validate": _state(field_findings=[_finding(work_completed=True)]),
        "delimit": _state(field_findings=[_finding(work_completed=False)]),
    },
    "D17": {
        "escalate": _state(escalated=True),
        "handover": _state(
            field_findings=[
                _finding(
                    fault_domain=FaultDomain.TAP_OR_ODP,
                    requires_plant_work=True,
                    delimiter_kind=DelimiterKind.TAP,
                    delimiter_ref="TAP-1",
                )
            ]
        ),
        "more_tests": _state(field_findings=[_finding(requires_plant_work=False)]),
    },
    "D18": {
        "request_approval": _state(handover_contract=_contract()),
        "reject": _state(handover_contract=_contract(ruled_out=[])),
    },
    "D19": {
        "restored": _state(mr_records=[_mr(MRStatus.COMPLETED)]),
        "await_plant": _state(mr_records=[_mr(MRStatus.IN_PROGRESS)]),
        "retry_diagnosis": _state(mr_records=[_mr(MRStatus.REJECTED)]),
    },
    "D20": {
        "reverse_handover": _state(test_results=[_test(TestStatus.FAILED)]),
        "verify": _state(test_results=[_test(TestStatus.PASSED)]),
    },
    "D21": {
        "continue_observation": _state(validation=_open_window()),
        "retry_diagnosis": _state(validation=_validation(regressed_metrics=("snr",))),
        "confirm_outcome": _state(validation=_validation(passed=True)),
    },
    "D22": {
        "reconcile": _state(validation=_validation(passed=True)),
        "retry_diagnosis": _state(validation=_validation()),
    },
    "D23": {
        "escalate": _state(escalated=True),
        "close": _state(reconciliation=_reconciliation()),
        "reconcile_retry": _state(reconciliation=_reconciliation(systems_unreachable=["crm"])),
    },
    "D24": {
        "chronic": _state(case_type=CaseType.REPEAT_VISIT),
        "done": _state(case_type=CaseType.CUSTOMER_REPORTED),
    },
}

#: Every state built above, flattened. Used to check that no router answers outside its own
#: vocabulary even when handed a state assembled for a different decision.
CORPUS: list[IncidentState] = [_state(), *(s for d in REACHING_STATES.values() for s in d.values())]

BRANCH_CASES = [
    pytest.param(decision_id, branch, id=f"{decision_id}-{branch}")
    for decision_id, decision in DECISIONS.items()
    for branch in decision.branches
]


# ------------------------------------------------------------------------------------------------
# The table itself
# ------------------------------------------------------------------------------------------------


def test_the_table_holds_exactly_the_twenty_four_decisions_the_specification_names() -> None:
    """Transcribed, not generated. `[f"D{n:02d}" for n in range(1, 25)]` would pass on a table
    holding twenty-four entries called anything at all, which is the mistake worth catching: a
    decision implemented under the wrong identifier is one the builder wires to the wrong node."""
    assert list(DECISIONS) == [
        "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08",
        "D09", "D10", "D11", "D12", "D13", "D14", "D15", "D16",
        "D17", "D18", "D19", "D20", "D21", "D22", "D23", "D24",
    ]  # fmt: skip


def test_each_decision_carries_its_own_identifier() -> None:
    """The key and the `id` field are two places one identifier is written. `_decision` sets both
    from one argument today; if that ever stops being true, the builder wires by key and the docs
    render by field, and they would disagree silently."""
    for key, decision in DECISIONS.items():
        assert decision.id == key


def test_every_question_is_the_specifications_own_heading() -> None:
    """Parsed out of `docs/specification.md`, not copied from `routing.py`.

    This is what makes the questions load-bearing rather than decorative. A specification revision
    that rewords D17 fails here, and the failure names the decision whose implementation now has to
    be re-read -- which is the only mechanical link between a prose requirement and the code that
    claims to satisfy it.
    """
    headings = dict(
        re.findall(r"^### (D\d\d) — (.+)$", SPECIFICATION.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert len(headings) == 24, "the specification's decision headings did not parse as expected"
    assert {k: d.question for k, d in DECISIONS.items()} == headings


@pytest.mark.parametrize("decision_id", list(DECISIONS))
def test_each_routers_declared_branches_match_its_annotated_return_type(decision_id: str) -> None:
    """`branches` and the `Literal[...]` return type are two independent claims about one function.

    mypy reads the annotation; `graph.builder` reads the tuple. Neither is derived from the other,
    so a branch added to one and not the other is a real divergence -- the builder would have no
    edge for a value the router can return, and the graph would raise at runtime on the one
    incident that took it.
    """
    decision = DECISIONS[decision_id]
    annotated = set(typing.get_args(typing.get_type_hints(decision.route)["return"]))
    assert annotated == set(decision.branches)


def test_no_branch_is_declared_twice() -> None:
    assert all(len(set(d.branches)) == len(d.branches) for d in DECISIONS.values())


def test_the_decision_record_is_frozen() -> None:
    """`graph.builder`, the docs and the tests all read the same objects. A mutable entry would let
    a caller retune the graph's wiring for everybody else that imports the module."""
    with pytest.raises(AttributeError):
        DECISIONS["D01"].question = "something else"  # type: ignore[misc]


# ------------------------------------------------------------------------------------------------
# Reachability, totality and vocabulary
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("decision_id", "branch"), BRANCH_CASES)
def test_every_declared_branch_is_reached_by_the_state_written_for_it(
    decision_id: str, branch: str
) -> None:
    """A declared branch nothing can reach is a graph edge that will never be taken.

    Parametrised over `DECISIONS` rather than over `REACHING_STATES`, so adding a branch without
    adding a state is a `KeyError` here rather than an untested path nobody notices.
    """
    state = REACHING_STATES[decision_id][branch]
    assert DECISIONS[decision_id].route(state) == branch


def test_the_reaching_table_names_no_branch_that_does_not_exist() -> None:
    """The other direction. Removing a branch from a router without removing its state would leave
    a test asserting behaviour the graph no longer has."""
    for decision_id, by_branch in REACHING_STATES.items():
        assert set(by_branch) == set(DECISIONS[decision_id].branches)


@pytest.mark.parametrize("decision_id", list(DECISIONS))
def test_no_router_raises_on_a_state_with_nothing_in_it(decision_id: str) -> None:
    """An exception in a conditional edge aborts the super-step, and an aborted super-step writes
    nothing -- so the incident rolls back with no record that anything was attempted. The empty
    state is the worst case every router has to survive, because a node that failed before writing
    anything leaves exactly this."""
    decision = DECISIONS[decision_id]
    assert decision.route(_state()) in decision.branches


@pytest.mark.parametrize("decision_id", list(DECISIONS))
def test_no_router_ever_answers_outside_its_declared_branches(decision_id: str) -> None:
    """Every router against every state in the corpus, not just its own.

    A router reached with a state assembled for a different stage is not hypothetical: a reverse
    handover at D20 carries an MR record, a self-help session and a dispatch plan all at once. This
    is the cheapest way to find a router that falls off the end of its `if` chain and returns
    `None`, which the graph would report as a missing edge rather than as a routing bug.
    """
    decision = DECISIONS[decision_id]
    for state in CORPUS:
        answer = decision.route(state)
        assert answer in decision.branches, f"{decision_id} answered {answer!r} for {state!r}"


# ------------------------------------------------------------------------------------------------
# Stage 1
# ------------------------------------------------------------------------------------------------


def test_an_event_nobody_has_assessed_still_opens_an_incident() -> None:
    """D01 fails *open* on a missing assessment, and that is the deliberate half of the asymmetry.

    The failure mode being avoided is the one that matters during a storm: the adapter that scores
    data quality goes down, `data_quality` is `None` for every event that arrives, and a router that
    demanded an assessment would quarantine every real outage at exactly the moment the queue nobody
    is draining fills up.
    """
    assert DECISIONS["D01"].route(_state(events=[_event()])) == "continue"


def test_only_a_flag_that_could_misidentify_the_customer_quarantines_an_event() -> None:
    """Stale data weakens confidence; conflicting sources may name the wrong subscriber.

    Parametrised across the whole flag set rather than spot-checked, because the interesting
    property is the *split*: `DataQualityAssessment.BLOCKING_FLAGS` owns which side each flag falls
    on, and a router that re-listed them would be a second copy free to drift. The three blocking
    flags are the ones after which an incident could be opened against the wrong customer and a
    truck sent to the wrong address.
    """
    blocking = {
        DataQualityFlag.ADAPTER_UNAVAILABLE,
        DataQualityFlag.CONFLICTING_SOURCES,
        DataQualityFlag.INCONSISTENT_TOPOLOGY,
    }
    assert blocking == set(DataQualityAssessment.BLOCKING_FLAGS), (
        "the blocking set moved; re-read D01's trade-off before updating this list"
    )
    for flag in DataQualityFlag:
        state = _state(events=[_event()], data_quality=_quality(flag))
        expected = "quarantine" if flag in blocking else "continue"
        assert DECISIONS["D01"].route(state) == expected, flag


def test_an_event_with_no_signal_behind_it_is_quarantined() -> None:
    """Empty `events` is the one case D01 refuses regardless of data quality: there is nothing to
    open an incident about, and `make_initial_state` always puts the triggering event in the list."""
    assert DECISIONS["D01"].route(_state(data_quality=_quality())) == "quarantine"


def test_an_exhausted_budget_outranks_an_enrichment_that_finally_succeeded() -> None:
    """D02 checks `escalated` before resolution, and the order is the assertion.

    An incident that resolved its topology on the pass that also exhausted its budget has still
    exhausted its budget. Continuing would leave `escalated=True` and an escalation audit event in
    the record while the graph carried on unattended -- the state and the machine disagreeing about
    whether a human is involved, which is the one disagreement an operator cannot detect.
    """
    resolved_and_escalated = _state(cpe_ref="CPE-1", topology=_topology(), escalated=True)
    assert DECISIONS["D02"].route(resolved_and_escalated) == "manual_review"


def test_a_subscriber_case_needs_a_delimiter_and_a_network_alarm_does_not() -> None:
    """`identity_is_resolved` asks a different question of the two, on purpose.

    D03 correlates neighbours across the tap or ODP and D17 is defined in terms of it, so a
    subscriber case without one is not resolved. A node alarm has no subscriber to hang a delimiter
    off; demanding one would loop P03 until the guard escalated every network alarm the system ever
    receives, which is a whole class of input silently routed to a human.
    """
    subscriber = _state(cpe_ref="CPE-1", topology=_topology(delimiter_ref=None))
    assert not identity_is_resolved(subscriber)
    assert identity_is_resolved(_state(cpe_ref="CPE-1", topology=_topology()))

    network_only = _state(topology=_topology(delimiter_ref=None))
    assert identity_is_resolved(network_only)
    assert not identity_is_resolved(_state(topology=_topology(delimiter_ref=None, node_ref=None)))


def test_a_predictive_case_with_live_customers_is_an_incident() -> None:
    """D04's override runs one way. Correlation promotes; nothing demotes."""
    promoted = _state(
        case_type=CaseType.PREDICTIVE_MAINTENANCE,
        service_ref="SVC-1",
        impact=_impact(12, "SVC-1", "SVC-2"),
    )
    assert DECISIONS["D04"].route(promoted) == "active"

    alone = _state(
        case_type=CaseType.PREDICTIVE_MAINTENANCE,
        service_ref="SVC-1",
        impact=_impact(1, "SVC-1"),
    )
    assert DECISIONS["D04"].route(alone) == "preventive"

    reported_with_no_measured_impact = _state(
        case_type=CaseType.CUSTOMER_REPORTED, service_ref="SVC-1", impact=_impact(1, "SVC-1")
    )
    assert DECISIONS["D04"].route(reported_with_no_measured_impact) == "active", (
        "a customer on the phone is an active incident whatever the blast radius says"
    )


def test_a_predictive_case_is_not_promoted_by_a_population_nobody_observed() -> None:
    """The population and the observed set are different quantities, and D04 reads the second.

    A tap the pack sizes at 8 with only the subject seen is one forecast about one premises, and
    the 8 is `blast_radius.size_of`'s default for a delimiter whose homes-behind count is missing
    from plant records -- a number about record-keeping, not about customers in trouble. Reading
    the population here promotes that forecast to an outage on the strength of a default.

    This is the assertion that would have caught the original fault. `affected_customer_count` was
    floored at 1 by the single-premises basis "the fault is at this premises, so one service is
    affected", so `> 0` was true of every case that ever reached D04 and `preventive` was
    unreachable -- 41 of 41 fixture services filed as predictive came out `active`. Both the old
    threshold and the obvious repair, `> 1`, fail here; only the observed set answers it.

    Shown red by pointing the router at `affected_customer_count > 1`, the repair that looks
    sufficient and is not::

        AssertionError: assert 'active' == 'preventive'
        - preventive
        + active

    The compiled-graph test in `test_builder` stays green under that same revert, because on every
    fixture the count and the observed set happen to agree. This is the case that separates them.
    """
    sized_but_unobserved = _state(
        case_type=CaseType.PREDICTIVE_MAINTENANCE,
        service_ref="SVC-1",
        impact=_impact(8, "SVC-1"),
    )
    assert DECISIONS["D04"].route(sized_but_unobserved) == "preventive"


# ------------------------------------------------------------------------------------------------
# Stage 2
# ------------------------------------------------------------------------------------------------


def test_evidence_that_was_never_assessed_is_gathered_again_rather_than_acted_on() -> None:
    """D05 fails *closed* where D01 fails open, and the difference is what the remedy costs.

    Reassembling evidence is cheap, reversible and bounded by the same guard as every other loop.
    Quarantining an event is none of those. Two decisions reading the same field can still be right
    to disagree, and this pair is where that is easiest to get backwards.
    """
    assert DECISIONS["D05"].route(_state()) == "gather_more"
    assert DECISIONS["D01"].route(_state(events=[_event()])) == "continue"


def test_a_complete_but_self_contradictory_assessment_is_still_not_sufficient() -> None:
    """D05 asks the model, and the model's answer is not the completeness score.

    `sufficient_for_action` is `not blocking and completeness >= 0.5` -- two clauses, and the one a
    router is tempted to inline is the numeric one. This state has full completeness and a blocking
    flag: every field is present and two sources disagree about what they say. Re-deriving the
    threshold here would read that as sufficient and act on evidence that names the wrong customer.
    """
    contradictory = _quality(DataQualityFlag.CONFLICTING_SOURCES, completeness=1.0)
    assert contradictory.completeness_score == 1.0
    assert not contradictory.sufficient_for_action
    assert DECISIONS["D05"].route(_state(data_quality=contradictory)) == "gather_more"


def test_a_rejected_low_confidence_review_goes_back_to_diagnosis_and_not_forward() -> None:
    """The reviewer is saying the analysis is not good enough to act on.

    `continue` here would make the gate ceremonial: a supervisor would have refused an action and
    watched it happen anyway. Routing forward on rejection is the single most damaging thing an
    approval gate can do, and it is a one-word mistake.
    """
    rejected = _state(
        rca=_rca(0.2),
        approvals=[_approval(ApprovalKind.LOW_CONFIDENCE_RCA, ApprovalStatus.REJECTED)],
    )
    assert DECISIONS["D06"].route(rejected) == "retry_diagnosis"

    approved = _state(
        rca=_rca(0.2),
        approvals=[_approval(ApprovalKind.LOW_CONFIDENCE_RCA, ApprovalStatus.APPROVED)],
    )
    assert DECISIONS["D06"].route(approved) == "continue"


def test_an_incident_that_reached_the_confidence_gate_with_no_analysis_asks_a_human() -> None:
    """Fail-closed at D06 means *ask*, not stop. No root cause at the confidence gate is exactly the
    case L2 review exists for, and stopping would put it in a queue instead of in front of someone.
    """
    assert DECISIONS["D06"].route(_state()) == "approve_low_confidence"


def test_an_answered_gate_stays_shut_until_policy_asks_again() -> None:
    """`approval_outstanding` compares timestamps because both lists are append-only.

    "Has this ever been demanded?" stays true for the life of the incident, so a gate built on it
    would re-ask on every pass and never terminate. "Was the *latest* demand answered?" closes once
    -- and re-opens exactly when a later evaluation demands it again, which is what a second
    dispatch proposed after the first was rejected has to do.
    """
    kind = ApprovalKind.DISPATCH
    demand = _policy(outcome=PolicyOutcome.REQUIRES_APPROVAL, kind=kind, at=AT)

    answered = _state(
        policy_decisions=[demand], approvals=[_approval(kind, ApprovalStatus.APPROVED, at=LATER)]
    )
    assert not approval_outstanding(answered, kind)
    assert DECISIONS["D15"].route(answered) == "commit"

    asked_again = _state(
        policy_decisions=[
            demand,
            _policy(
                outcome=PolicyOutcome.REQUIRES_APPROVAL, kind=kind, at=LATEST, decision_id="POL-2"
            ),
        ],
        approvals=[_approval(kind, ApprovalStatus.APPROVED, at=LATER)],
    )
    assert approval_outstanding(asked_again, kind)
    assert DECISIONS["D15"].route(asked_again) == "approve_dispatch"


def test_a_demand_nobody_has_answered_is_outstanding_and_an_undemanded_kind_is_not() -> None:
    """Both directions, because a predicate that answered `True` unconditionally would satisfy the
    re-opening test above on its own."""
    kind = ApprovalKind.HIGH_BLAST_RADIUS_ACTION
    demanded = _state(
        policy_decisions=[_policy(outcome=PolicyOutcome.REQUIRES_APPROVAL, kind=kind)]
    )
    assert approval_outstanding(demanded, kind)
    assert not approval_outstanding(demanded, ApprovalKind.DISPATCH)
    assert not approval_outstanding(_state(), kind)


# ------------------------------------------------------------------------------------------------
# Stage 3
# ------------------------------------------------------------------------------------------------


def test_a_refused_high_blast_radius_action_escalates_rather_than_proceeding() -> None:
    assert (
        DECISIONS["D07"].route(
            _state(
                approvals=[
                    _approval(ApprovalKind.HIGH_BLAST_RADIUS_ACTION, ApprovalStatus.APPROVED)
                ]
            )
        )
        == "continue"
    )
    for refusal in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.WITHDRAWN):
        state = _state(approvals=[_approval(ApprovalKind.HIGH_BLAST_RADIUS_ACTION, refusal)])
        assert DECISIONS["D07"].route(state) == "escalate", refusal


def test_a_plan_whose_every_remaining_option_is_blocked_goes_to_a_human() -> None:
    """There is nothing left for the graph to try, and `continue` would walk it into D09 and D11
    finding no eligible option either, then into field planning for a fault policy has refused."""
    blocked = _state(
        resolution_plan=_plan([_option()]),
        policy_decisions=[
            _policy(action_type=ActionType.CPE_REBOOT, outcome=PolicyOutcome.BLOCKED)
        ],
    )
    assert DECISIONS["D07"].route(blocked) == "escalate"

    one_survivor = _state(
        resolution_plan=_plan(
            [_option(), _option(option_id="OPT-2", action_type=ActionType.CPE_RESYNC)]
        ),
        policy_decisions=[
            _policy(action_type=ActionType.CPE_REBOOT, outcome=PolicyOutcome.BLOCKED),
            _policy(
                action_type=ActionType.CPE_RESYNC,
                outcome=PolicyOutcome.ALLOWED,
                decision_id="POL-2",
            ),
        ],
    )
    assert DECISIONS["D07"].route(one_survivor) == "continue"


def test_an_incident_with_no_policy_evaluation_at_all_is_not_declared_unsafe() -> None:
    """D07 asks whether a blocking condition was *found*, not whether one was looked for.

    Escalating on silence would turn every gap in an upstream node into a supervisor's queue item
    that says nothing useful, and would hide the actual defect. The actions that matter are gated
    individually at D15 and by `policies.engine` before execution.
    """
    assert DECISIONS["D07"].route(_state(resolution_plan=_plan([_option()]))) == "continue"


def test_a_fault_at_the_tap_is_not_sent_down_the_plant_path() -> None:
    """D08's one interesting case, and the reason it asks `crew_for` rather than `is_plant_side`.

    The tap and the ODP are plant-side by the responsibility boundary but their remedy is a joint
    dispatch. Diverting them here would skip the Clean Boots half of that visit and leave the
    customer's side of the fault untouched, which is the failure Scenario 11 exists to catch.
    """
    assert DECISIONS["D08"].route(_state(fault_domain=FaultDomain.TAP_OR_ODP)) == "continue"


def test_d08_diverts_exactly_the_domains_that_need_no_premises_visit() -> None:
    """A hand-written table over all fifteen domains. `crew_for` is the owner of the split and
    `test_boundaries.py` checks it; what is checked here is the *use* -- which side of D08 each
    domain lands on, including the three that dispatch nobody at all."""
    expected: dict[FaultDomain, str] = {
        FaultDomain.CPE: "continue",
        FaultDomain.INSIDE_HOME_WIRING: "continue",
        FaultDomain.DROP: "continue",
        FaultDomain.CUSTOMER_ENVIRONMENT: "continue",
        FaultDomain.TAP_OR_ODP: "continue",
        FaultDomain.DISTRIBUTION: "plant_path",
        FaultDomain.FEEDER: "plant_path",
        FaultDomain.NODE_OR_OLT: "plant_path",
        FaultDomain.HEADEND_OR_CO: "plant_path",
        FaultDomain.POWER: "plant_path",
        FaultDomain.SERVICE_PLATFORM: "plant_path",
        FaultDomain.PROVISIONING: "plant_path",
        FaultDomain.NO_FAULT_FOUND: "continue",
        FaultDomain.MULTIPLE: "continue",
        FaultDomain.UNKNOWN: "continue",
    }
    assert set(expected) == set(FaultDomain), "a fault domain was added without a D08 answer"
    for domain, branch in expected.items():
        assert DECISIONS["D08"].route(_state(fault_domain=domain)) == branch, domain


def test_d13_names_a_crew_for_every_domain_or_refuses_to_guess() -> None:
    """The same fifteen domains, the other decision. Written out rather than derived from
    `crew_for`, so that a change to the boundary has to be restated here to pass -- which is the
    point at which somebody re-reads whether a truck should now go somewhere new."""
    expected: dict[FaultDomain, str] = {
        FaultDomain.CPE: "clean",
        FaultDomain.INSIDE_HOME_WIRING: "clean",
        FaultDomain.DROP: "clean",
        FaultDomain.CUSTOMER_ENVIRONMENT: "clean",
        FaultDomain.TAP_OR_ODP: "joint",
        FaultDomain.DISTRIBUTION: "dirty",
        FaultDomain.FEEDER: "dirty",
        FaultDomain.NODE_OR_OLT: "dirty",
        FaultDomain.HEADEND_OR_CO: "dirty",
        FaultDomain.POWER: "dirty",
        FaultDomain.SERVICE_PLATFORM: "escalate",
        FaultDomain.PROVISIONING: "escalate",
        FaultDomain.NO_FAULT_FOUND: "escalate",
        FaultDomain.MULTIPLE: "escalate",
        FaultDomain.UNKNOWN: "escalate",
    }
    assert set(expected) == set(FaultDomain), "a fault domain was added without a D13 answer"
    for domain, branch in expected.items():
        assert DECISIONS["D13"].route(_state(fault_domain=domain)) == branch, domain


def test_an_unknown_fault_domain_never_becomes_a_default_crew() -> None:
    """The mistake D13 is written to prevent, stated on its own so it cannot be lost in the table:
    `unknown` silently becoming `clean` is a truck sent to a customer whose fault nobody located."""
    assert DECISIONS["D13"].route(_state()) == "escalate"


def test_the_three_option_shapes_partition_every_combination_of_what_an_option_needs() -> None:
    """Exactly one shape for each of the four (truck, customer) combinations.

    "In two" would make D09 and D11 order-dependent -- a self-help option answering D09 first would
    be executed by a console nobody is sitting at. "In none" would make an option invisible to every
    branch and leave it in the plan forever, which reads as an exhausted plan at D12.
    """
    for truck in (False, True):
        for customer in (False, True):
            option = _option(truck=truck, customer=customer)
            shapes = [
                name
                for name, predicate in (
                    ("remote", is_remote_option),
                    ("self_help", is_self_help_option),
                    ("field", is_field_option),
                )
                if predicate(option)
            ]
            assert shapes == [
                "remote" if not truck and not customer else "self_help" if not truck else "field"
            ], (truck, customer)


def test_an_option_policy_has_blocked_is_skipped_but_one_needing_approval_is_not() -> None:
    """Skipping to self-help because a supervisor would have to be asked is a silent downgrade of
    the remedy: the customer gets instructions instead of the fix, and nothing records that a policy
    gate caused it."""
    needs_approval = _state(
        resolution_plan=_plan([_option()]),
        policy_decisions=[
            _policy(
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                kind=ApprovalKind.HIGH_RISK_REMOTE_ACTION,
            )
        ],
    )
    assert DECISIONS["D09"].route(needs_approval) == "remote"

    blocked = _state(
        resolution_plan=_plan([_option()]),
        policy_decisions=[_policy(outcome=PolicyOutcome.BLOCKED)],
    )
    assert DECISIONS["D09"].route(blocked) == "self_help_check"


def test_an_option_already_attempted_is_not_offered_again() -> None:
    """ "Prior failed attempts" is one of D09's stated eligibility inputs, and `untried()` owns it."""
    exhausted = _state(resolution_plan=_plan([_option()], attempted=["OPT-1"]))
    assert DECISIONS["D09"].route(exhausted) == "self_help_check"


def test_a_blocked_self_help_option_is_skipped_the_same_way_a_blocked_remote_one_is() -> None:
    """D11's half of the policy check, which the reachability table alone does not exercise.

    Written because the sweep found it missing: deleting the policy filter from D11 left every test
    green, because the only state reaching `self_help` had no policy decision to filter on. A remote
    action and a self-help message are both actions the engine can refuse -- sending a customer
    instructions for a procedure policy has blocked is the same failure as executing it, minus the
    audit record that an action was taken.
    """
    blocked = _state(
        resolution_plan=_plan([_option(action_type=ActionType.SEND_SELF_HELP, customer=True)]),
        policy_decisions=[
            _policy(action_type=ActionType.SEND_SELF_HELP, outcome=PolicyOutcome.BLOCKED)
        ],
    )
    assert DECISIONS["D11"].route(blocked) == "field_planning"

    needs_approval = _state(
        resolution_plan=_plan([_option(action_type=ActionType.SEND_SELF_HELP, customer=True)]),
        policy_decisions=[
            _policy(
                action_type=ActionType.SEND_SELF_HELP,
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                kind=ApprovalKind.HIGH_RISK_REMOTE_ACTION,
            )
        ],
    )
    assert DECISIONS["D11"].route(needs_approval) == "self_help"


def test_an_unverified_remote_success_is_not_a_restoration() -> None:
    """`RemoteAction.fixed_it` needs `verification_passed is True`. A reboot that returned 200 and
    was never checked is the commonest way an incident gets closed on a service still down."""
    unverified = RemoteAction(
        action_id="ACT-1",
        action_type=ActionType.CPE_REBOOT,
        target_ref="CPE-1",
        idempotency_key="KEY-1",
        requested_at=AT,
        outcome=ActionOutcome.SUCCEEDED,
    )
    assert DECISIONS["D10"].route(_state(remote_actions=[unverified])) == "retry_diagnosis"
    assert DECISIONS["D10"].route(_state(remote_actions=[_remote_action(fixed=True)])) == "verify"


def test_self_help_stops_re_diagnosing_once_the_plan_is_exhausted() -> None:
    """ "Return to diagnosis or proceed to field planning according to policy", answered by a
    recorded fact rather than a threshold.

    While untried options remain another pass can still change which one is chosen -- that is
    "re-diagnose before repeating work". Once every option has been attempted the same evidence
    produces the same plan, and looping would spend the step budget to arrive at field planning
    anyway, several minutes later, with the customer still down.
    """
    failed = _session("not_resolved")
    assert (
        DECISIONS["D12"].route(_state(self_help_session=failed, resolution_plan=_plan([_option()])))
        == "retry_diagnosis"
    )
    assert (
        DECISIONS["D12"].route(
            _state(
                self_help_session=failed,
                resolution_plan=_plan([_option()], attempted=["OPT-1"]),
            )
        )
        == "field_planning"
    )


def test_a_plan_that_lost_a_requirement_is_queued_even_with_nothing_unassigned() -> None:
    """The complementary question to `unassigned`, and the reason D14 asks both.

    `DispatchPlan` refuses to carry an unexplained `unassigned` entry, so an optimiser that dropped
    a requirement *silently* -- neither assigning it nor listing it -- would produce a plan that
    passes its own validator and leaves a crew unscheduled. Coverage catches that; `unassigned`
    cannot.
    """
    lost = _state(
        dispatch_requirements=[_requirement("REQ-1"), _requirement("REQ-2")],
        dispatch_plan=_dispatch_plan(assigned=["REQ-1"]),
    )
    assert DECISIONS["D14"].route(lost) == "queue_for_dispatcher"

    covered = _state(
        dispatch_requirements=[_requirement("REQ-1"), _requirement("REQ-2")],
        dispatch_plan=_dispatch_plan(assigned=["REQ-1", "REQ-2"]),
    )
    assert DECISIONS["D14"].route(covered) == "continue"


def test_dispatch_commits_without_a_gate_only_on_an_explicit_policy_allowance() -> None:
    """ "By default, require human approval before committing a field slot" makes silence mean ask.

    The only path straight to `commit` is a recorded, versioned `PolicyDecision` for
    `create_work_order` that is allowed and demands no approval -- a statement somebody made, rather
    than the absence of one saying otherwise. A decision for a *different* action does not carry.
    """
    assert DECISIONS["D15"].route(_state()) == "approve_dispatch"

    allowed = _state(
        policy_decisions=[_policy(action_type=ActionType.CREATE_WORK_ORDER)],
    )
    assert DECISIONS["D15"].route(allowed) == "commit"

    someone_elses_allowance = _state(
        policy_decisions=[_policy(action_type=ActionType.CPE_REBOOT)],
    )
    assert DECISIONS["D15"].route(someone_elses_allowance) == "approve_dispatch"


def test_a_dispatcher_who_refuses_a_slot_gets_a_new_plan_not_the_same_question() -> None:
    """`replan`, not `commit` and not the gate again. Re-asking an unchanged question is how an
    incident spends its whole step budget in front of the same person."""
    refused = _state(approvals=[_approval(ApprovalKind.DISPATCH, ApprovalStatus.REJECTED)])
    assert DECISIONS["D15"].route(refused) == "replan"


def test_the_two_approval_readers_agree_about_which_answer_is_the_current_one() -> None:
    """`approval_outstanding` and `latest_decision_of` are read one after the other, so they have to
    order the list the same way.

    Every gate asks the pair in sequence -- "is the newest demand still unanswered?", then "what was
    the answer?" -- and `approval_outstanding` orders by `decided_at`. If `latest_decision_of` took
    the list tail instead, a state where write order and decision order disagree would close the
    gate on one decision and then act on a different one. `approvals` is de-duplicated but not
    sorted: `append_unique` preserves write order, and two nodes writing in the same super-step
    merge in whatever order the runtime ran them.

    Here the rejection was decided last and appended first. The gate must replan.
    """
    out_of_order = [
        _approval(ApprovalKind.DISPATCH, ApprovalStatus.REJECTED, at=LATEST, approval_id="APR-b"),
        _approval(ApprovalKind.DISPATCH, ApprovalStatus.APPROVED, at=LATER, approval_id="APR-a"),
    ]
    state = _state(
        approvals=out_of_order,
        policy_decisions=[
            _policy(kind=ApprovalKind.DISPATCH, outcome=PolicyOutcome.REQUIRES_APPROVAL)
        ],
    )
    assert not approval_outstanding(state, ApprovalKind.DISPATCH)
    answer = latest_decision_of(state, ApprovalKind.DISPATCH)
    assert answer is not None and answer.status is ApprovalStatus.REJECTED
    assert DECISIONS["D15"].route(state) == "replan"


# ------------------------------------------------------------------------------------------------
# Stage 4
# ------------------------------------------------------------------------------------------------


def test_the_latest_field_finding_wins_over_the_first() -> None:
    """A second visit that discovers plant work overrides the first visit's "resolved here".

    `any(...)` across the list -- the obvious implementation -- would keep answering with whichever
    finding happened to be listed first, so an incident that was closed at the premises and then
    reopened with a tap fault would route to validation forever.
    """
    findings = [
        _finding("FF-1", work_completed=True),
        _finding(
            "FF-2",
            work_completed=False,
            requires_plant_work=True,
            fault_domain=FaultDomain.TAP_OR_ODP,
        ),
    ]
    assert latest_field_finding(_state(field_findings=findings)) is findings[-1]
    assert DECISIONS["D16"].route(_state(field_findings=findings)) == "delimit"
    assert DECISIONS["D16"].route(_state(field_findings=findings[:1])) == "validate"


def test_work_finished_at_the_premises_but_naming_plant_work_is_not_resolved_here() -> None:
    """Both clauses, because reading only `work_completed` is the plausible half-implementation: a
    technician who replaced the drop *and* recorded that the tap needs work has not finished."""
    both = _finding(work_completed=True, requires_plant_work=True, fault_domain=FaultDomain.FEEDER)
    assert DECISIONS["D16"].route(_state(field_findings=[both])) == "delimit"


def test_an_mr_is_not_raised_until_the_boundary_is_actually_named() -> None:
    """ "Do not create an incomplete MR": each clause of D17 is one way an MR can be incomplete.

    Parametrised over the three ways a finding can fall short of placing the fault beyond the
    boundary, because a router checking any two of them would still pass a single spot-check.
    """
    complete = {
        "fault_domain": FaultDomain.TAP_OR_ODP,
        "requires_plant_work": True,
        "delimiter_kind": DelimiterKind.TAP,
        "delimiter_ref": "TAP-1",
    }
    assert DECISIONS["D17"].route(_state(field_findings=[_finding(**complete)])) == "handover"

    for gap in (
        {"requires_plant_work": False, "fault_domain": FaultDomain.CPE},
        {"delimiter_ref": None},
        {"delimiter_kind": DelimiterKind.UNKNOWN},
        {"fault_domain": FaultDomain.CPE, "requires_plant_work": False},
    ):
        short = {**complete, **gap}
        assert DECISIONS["D17"].route(_state(field_findings=[_finding(**short)])) == "more_tests", (
            gap
        )


def test_an_incomplete_handover_contract_is_rejected_by_its_own_audit() -> None:
    """`HandoverContract.complete` enumerates the missing items; D18 does not re-check fields.

    A router with its own shorter list would be a second version of the twenty-four-item contract,
    and the shorter one always wins by accident.
    """
    assert DECISIONS["D18"].route(_state(handover_contract=_contract())) == "request_approval"
    for gap in ({"ruled_out": []}, {"field_finding_ids": []}, {"measurements": {}}):
        assert DECISIONS["D18"].route(_state(handover_contract=_contract(**gap))) == "reject", gap


def test_a_complete_contract_the_receiving_owner_refused_is_still_rejected() -> None:
    """Completeness and acceptance fail for different reasons and carry different remedies: a
    complete contract can still be duplicative, and the rejection carries the reason code the
    diagnosis path needs."""
    refused = _contract(accepted=False, rejection_reason=ReasonCode.POLICY_DUPLICATE_SUPPRESSED)
    assert refused.complete
    assert DECISIONS["D18"].route(_state(handover_contract=refused)) == "reject"


def test_an_mr_still_with_osp_is_not_read_as_a_failed_repair() -> None:
    """Three outcomes at D19, because "not restored" and "not finished" are different states.

    Collapsing them would re-open diagnosis while a crew is at the pole -- and the specification is
    explicit that the response to a failure is to re-diagnose *and* not duplicate the MR, which is
    what a diagnosis pass fired against an in-flight MR eventually does.
    """
    expected = {
        MRStatus.DRAFT: "retry_diagnosis",
        MRStatus.SUBMITTED: "await_plant",
        MRStatus.ACCEPTED: "await_plant",
        MRStatus.REJECTED: "retry_diagnosis",
        MRStatus.IN_PROGRESS: "await_plant",
        MRStatus.PLANNED: "await_plant",
        MRStatus.COMPLETED: "restored",
        MRStatus.CLOSED: "restored",
        MRStatus.CANCELLED: "retry_diagnosis",
    }
    assert set(expected) == set(MRStatus), "an MR status was added without a D19 answer"
    for status, branch in expected.items():
        assert DECISIONS["D19"].route(_state(mr_records=[_mr(status)])) == branch, status


def test_the_current_mr_revision_decides_not_the_first_one_recorded() -> None:
    """`mr_records` is an `append_revision` list: a submitted MR that later completed appears twice
    and the second entry is the truth. Reading the list head would leave every completed repair
    waiting on OSP forever."""
    revisions = [
        _mr(MRStatus.SUBMITTED, updated_at=AT),
        _mr(MRStatus.COMPLETED, updated_at=LATER),
    ]
    assert DECISIONS["D19"].route(_state(mr_records=revisions)) == "restored"


def test_with_two_mrs_open_the_most_recently_updated_one_answers() -> None:
    """Two *different* MRs, which is the case the revision test above cannot reach.

    `current_mr_records` collapses revisions of one `mr_id`, so a state holding several revisions of
    a single MR has one entry and reading the dict head is indistinguishable from reading the newest.
    A rejected MR followed by a second, successful one gives the dict two entries in insertion order,
    and there the two readings part company: the head still says the first attempt is in flight.
    That is a plant repair that finished and an incident that waits on OSP until the guard escalates
    it.
    """
    two_mrs = [
        _mr(MRStatus.IN_PROGRESS, mr_id="MR-1", updated_at=AT),
        _mr(MRStatus.COMPLETED, mr_id="MR-2", updated_at=LATER),
    ]
    assert list(current_mr_records(_state(mr_records=two_mrs))) == ["MR-1", "MR-2"]
    assert DECISIONS["D19"].route(_state(mr_records=two_mrs)) == "restored"


def test_a_test_that_could_not_run_does_not_send_a_second_truck() -> None:
    """D20 reads the latest *conclusive* result.

    An `inconclusive` or `unavailable` probe is not evidence that the service is broken. Treating it
    as a failure would order a reverse handover -- a second work order and a second visit -- every
    time a probe timed out after a plant repair.
    """
    assert (
        DECISIONS["D20"].route(
            _state(test_results=[_test(TestStatus.FAILED), _test(TestStatus.PASSED, result_id="B")])
        )
        == "verify"
    )
    assert (
        DECISIONS["D20"].route(
            _state(
                test_results=[
                    _test(TestStatus.FAILED),
                    _test(TestStatus.INCONCLUSIVE, result_id="B"),
                ]
            )
        )
        == "reverse_handover"
    )
    assert DECISIONS["D20"].route(_state(test_results=[_test(TestStatus.UNAVAILABLE)])) == "verify"


# ------------------------------------------------------------------------------------------------
# Stage 5
# ------------------------------------------------------------------------------------------------


def test_a_metric_that_got_worse_is_not_waited_out() -> None:
    """Regression is checked before window completeness, and the order is the assertion.

    "Continue observation when evidence is improving but incomplete" is the branch for an incomplete
    window. A regression is degradation remaining, and no amount of further observation improves it
    -- checking the window first would hold a worsening service open for the whole stability period.
    """
    regressed_early = _open_window(regressed_metrics=("snr",))
    assert not regressed_early.window_complete
    assert DECISIONS["D21"].route(_state(validation=regressed_early)) == "retry_diagnosis"

    improving = _open_window(improved_metrics=("snr",))
    assert not improving.window_complete
    assert DECISIONS["D21"].route(_state(validation=improving)) == "continue_observation"


def test_a_completed_window_that_did_not_pass_goes_back_to_diagnosis() -> None:
    """The fallthrough: window complete, nothing regressed, still not passing. A fix that did not
    take. `continue_observation` here would loop until the guard escalated, which reports a budget
    problem rather than the diagnosis problem it actually is."""
    stalled = _validation(samples_in_window=4)
    assert stalled.window_complete
    assert not stalled.passed
    assert DECISIONS["D21"].route(_state(validation=stalled)) == "retry_diagnosis"


def test_a_customer_who_was_never_asked_is_not_a_customer_who_said_no() -> None:
    """`customer_confirmed is False`, not falsiness.

    `None` means P23 judged telemetry sufficient and nobody was called -- the specification's own
    rule for when to ask. Reading it as a denial would send every incident that did not need a phone
    call back to diagnosis, which is most of them.
    """
    passed = _validation(passed=True)
    assert passed.customer_confirmed is None
    assert DECISIONS["D22"].route(_state(validation=passed)) == "reconcile"

    denied = _validation(passed=True, customer_confirmed=False)
    assert DECISIONS["D22"].route(_state(validation=denied)) == "retry_diagnosis"

    confirmed = _validation(passed=True, customer_confirmed=True)
    assert DECISIONS["D22"].route(_state(validation=confirmed)) == "reconcile"


def test_a_system_nobody_could_reach_holds_closure_open() -> None:
    """`ReconciliationResult.consistent` counts an unreachable system as inconsistent, and D23 takes
    that answer. Closing an incident whose ticket may still be open in a system that timed out is
    the premature closure Stage 5 exists to prevent."""
    unreachable = _reconciliation(systems_unreachable=["jtrack"])
    assert not unreachable.consistent
    assert DECISIONS["D23"].route(_state(reconciliation=unreachable)) == "reconcile_retry"
    assert DECISIONS["D23"].route(_state(reconciliation=_reconciliation())) == "close"


def test_reconciliation_that_never_ran_does_not_close_the_incident() -> None:
    assert DECISIONS["D23"].route(_state()) == "reconcile_retry"


def test_a_repeat_is_a_repeat_by_any_of_its_four_signals() -> None:
    """OR-ed rather than scored: "do not hide chronic problems by treating every recurrence as
    isolated". A case filed as a repeat visit is chronic even when this particular visit went
    perfectly, and a second truck roll is chronic even when nobody labelled it."""
    two_visits = _state(
        work_orders=[
            _work_order(WorkOrderStatus.COMPLETED, work_order_id="WO-1"),
            _work_order(WorkOrderStatus.ON_SITE, work_order_id="WO-2"),
        ]
    )
    assert DECISIONS["D24"].route(two_visits) == "chronic"
    assert DECISIONS["D24"].route(_state(case_type=CaseType.REPEAT_VISIT)) == "chronic"
    assert DECISIONS["D24"].route(_state(mr_attempt_count=2)) == "chronic"
    assert DECISIONS["D24"].route(_state(linked_records={"prior_incidents": "INC-0"})) == "chronic"


def test_one_clean_visit_is_not_a_chronic_pattern() -> None:
    """The control. Without it, `return "chronic"` unconditionally passes every assertion above."""
    single_visit = _state(
        case_type=CaseType.CUSTOMER_REPORTED,
        work_orders=[_work_order(WorkOrderStatus.COMPLETED)],
        mr_attempt_count=1,
        linked_records={"crm_ticket": "TT-1"},
    )
    assert DECISIONS["D24"].route(single_visit) == "done"


def test_a_scheduled_visit_nobody_travelled_to_is_not_a_truck_roll() -> None:
    """Two draft work orders are not two visits. `truck_roll_count` owns the rule; D24 reads it
    rather than counting rows, which is how two dashboards end up disagreeing."""
    never_left = _state(
        work_orders=[
            _work_order(WorkOrderStatus.SCHEDULED, work_order_id="WO-1"),
            _work_order(WorkOrderStatus.CANCELLED, work_order_id="WO-2"),
        ]
    )
    assert DECISIONS["D24"].route(never_left) == "done"


# ------------------------------------------------------------------------------------------------
# Purity
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("decision_id", list(DECISIONS))
def test_a_router_does_not_write_to_the_state_it_is_given(decision_id: str) -> None:
    """A conditional edge's mutations are not merged through the reducers, so a router that wrote
    into the state dict would be changing an incident by a route no checkpoint records and no
    reducer validates -- `advance_status` and `write_once` would both be bypassed."""
    decision = DECISIONS[decision_id]
    for state in CORPUS:
        before = dict(state)
        decision.route(state)
        assert dict(state) == before


@pytest.mark.parametrize("decision_id", list(DECISIONS))
def test_a_router_gives_the_same_answer_twice(decision_id: str) -> None:
    """Determinism is the specification's requirement for these transitions, and it is also what
    makes a resume safe: the router runs again on every replay of the super-step, and a second
    answer would send the resumed incident somewhere the first pass did not go."""
    decision = DECISIONS[decision_id]
    for state in CORPUS:
        assert decision.route(state) == decision.route(state)


def test_the_decision_type_is_what_the_builder_will_iterate() -> None:
    """A shape assertion, so `graph.builder` can be written against it before it exists."""
    decision = DECISIONS["D01"]
    assert isinstance(decision, Decision)
    assert callable(decision.route)
    assert isinstance(decision.branches, tuple)
