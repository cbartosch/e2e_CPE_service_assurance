"""The eleven parent nodes, run end to end against the simulator.

These are not unit tests of each node in isolation. P01-P11 is a pipeline whose interesting
properties are only visible once a real signal has been through all of it -- what the evidence
stage hands the diagnosis stage, whether anything was written, what the routers downstream will do
with the plan that comes out. Each test below drives the whole chain and then asserts one property
of the result.

The nodes are invoked through `fn.__wrapped__`, so the `@node` decorator's wrapper does not run --
it reaches for the LangGraph runtime to find `GraphContext`, and there is no runtime here. That
path, together with the budget guard and the conditional edges that read what the guard writes, is
covered by `tests/unit/test_builder.py`, which drives these same eleven nodes through the compiled
graph.

Both modules are worth having because they localise different faults. Driving the chain by hand in
a fixed order means no routing table decides what runs next, so a failure here is a failure of a
node; the builder's tests run the same nodes through the real edges, so a failure there that is
green here points at the wiring. One test below does call routers, but on the finished state and as
assertions about the plan -- D08, D09 and D11 belong to Stage 3 and wire no parent edge, so nothing
here depends on `builder.DECISION_AFTER`. `preview` folds each update into the state
exactly as the reducers will, and `conftest.build_context` assembles a detector snapshot by hand,
so that neither a graph nor a live adapter is needed to reach P11.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lpr_cpe.domain.enums import (
    ActionType,
    CaseType,
    EventSource,
    FaultDomain,
    Severity,
    Technology,
)
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.nodes import PARENT_NODES
from lpr_cpe.graph.nodes._runtime import preview
from lpr_cpe.graph.routing import (
    route_remote_eligibility,
    route_self_help_suitability,
    route_shared_or_plant,
)
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.policies.engine import PolicyEngine

#: Fixed so that every derived id and every KPI duration is reproducible. The simulator's fixtures
#: are dated relative to nothing, so any instant works; this one is written down rather than
#: `now()` so a failure is the same failure tomorrow.
NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The four fixture services these tests drive, by their `health` label. One degraded HFC service
#: behind a degraded tap, one degraded PON service, one PON service behind a power cut, and one
#: healthy service that nonetheless sits behind the same degraded tap as the first.
HEALTHS = ("hfc_degraded_upstream", "pon_degraded_optical", "pon_power_affected", "hfc_healthy")


class _Frozen:
    """A clock that only moves when a test moves it.

    Each node is given a distinct instant so that KPI durations are non-zero and ordering
    assertions mean something. A real `Clock` is a protocol; this satisfies it structurally.
    """

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def _event(service: dict[str, Any]) -> AssuranceEvent:
    return AssuranceEvent(
        event_id=f"EVT-{service['service_ref']}",
        source=EventSource.NXT,
        case_type=CaseType.PROACTIVE_ALARM,
        technology=Technology(service["technology"]),
        severity=Severity.HIGH,
        occurred_at=NOW - timedelta(minutes=6),
        received_at=NOW - timedelta(minutes=5),
        customer_ref=service["customer_ref"],
        service_ref=service["service_ref"],
        cpe_ref=service["cpe_ref"],
        summary=f"degraded service on {service['service_ref']}",
    )


async def _run_parent(service: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """P01 through P11 over one fixture service. Returns the final state and the context used."""
    clock = _Frozen(NOW)
    ctx = build_context(clock=clock)  # type: ignore[arg-type]
    state = make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=_event(service),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )
    for _, fn in PARENT_NODES:
        clock.advance(timedelta(seconds=3))
        update = await fn.__wrapped__(state, ctx)
        state = preview(state, update)
    return state, ctx


def _service(fixtures: Any, health: str) -> dict[str, Any]:
    return next(s for s in fixtures.services.values() if s["health"] == health)


# ------------------------------------------------------------------------------------------------
# The read-only guarantee
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("health", HEALTHS)
async def test_the_first_eleven_nodes_write_nothing(fixtures: Any, health: str) -> None:
    """P01-P11 is diagnosis, and diagnosis does not change the network.

    Asserted against `WriteGate.recorded` rather than against each adapter, because the gate is the
    single place every outbound write must pass through -- an adapter that wrote without asking
    would be a defect the gate's own tests catch, and one that asked would be recorded here. The
    specification puts the first write behind D07's approval and P12's execution, both of which are
    downstream of everything these nodes do.
    """
    _, ctx = await _run_parent(_service(fixtures, health))
    # `list(...)` because `recorded` is a tuple; comparing to `[]` fails on the type alone and
    # would pass this test off as a real failure. The empty sequence is the assertion.
    assert list(ctx.adapters.gate.recorded) == []


# ------------------------------------------------------------------------------------------------
# What the evidence stage hands the diagnosis stage
# ------------------------------------------------------------------------------------------------


async def test_p10_reclassifies_instead_of_reusing_the_evidence_stage_verdict(
    fixtures: Any,
) -> None:
    """P07 and P10 disagree about this service, and P10's answer is the one that is right.

    P07 runs the detectors over the evidence as assembled and its fault-domain classifier says
    `distribution`. P10 re-runs the classifier over the *folded* findings -- which by then include
    the test results P09 produced -- and says `tap_or_odp`. The fixture's ground truth is that five
    of the eight services behind `TAP-SJ-011-A` are degraded, so the tap is the common element and
    `distribution` is a level too far upstream.

    This is the test that would fail if somebody "optimised" P10 by reusing P07's finding, which
    looks like an obvious saving and quietly costs the diagnosis its accuracy.
    """
    service = _service(fixtures, "hfc_degraded_upstream")
    state, _ = await _run_parent(service)

    rca = state["rca"]
    assert rca.fault_domain is FaultDomain.TAP_OR_ODP
    assert rca.delimiter_ref == "TAP-SJ-011-A"

    # The evidence stage's own classifier finding is still in state, and still says otherwise.
    evidence_stage_verdict = [
        f.suspected_domain
        for f in state["anomaly_findings"]
        if f.detector_name == "fault_domain_classifier" and f.suspected_domain is not None
    ]
    assert FaultDomain.DISTRIBUTION in evidence_stage_verdict, (
        "P07's classifier no longer says `distribution` for this fixture, so this test is no "
        "longer demonstrating that P10 re-classifies. Check the fixture before relaxing it."
    )


# ------------------------------------------------------------------------------------------------
# What the routers downstream do with the plan
# ------------------------------------------------------------------------------------------------


async def test_a_tap_fault_reaches_field_planning_with_both_halves_of_a_joint_dispatch(
    fixtures: Any,
) -> None:
    """The tap is the one plant domain D08 does not divert, and that has to end somewhere sensible.

    `boundaries.crew_for(TAP_OR_ODP)` is `JOINT`, so D08 returns `continue` deliberately: diverting
    it down the plant path would skip the Clean Boots half of a joint visit. That is only correct
    if the plan actually contains both halves and if the intervening routers decline it.

    This has been wrong. With `raise_mr` as the tap's only catalogued option and `truck_roll=False`
    on it, `is_remote_option` -- "no truck and no customer" -- was true, D09 returned `remote`, and
    a day-long plant maintenance request was handed to the remote-repair stage as though it were a
    reboot. Both halves of the fix are asserted here: the plan has a Dirty Boots action and a Clean
    Boots one, and the route runs D08 -> D09 -> D11 -> field planning without either stage claiming
    it.
    """
    state, _ = await _run_parent(_service(fixtures, "hfc_degraded_upstream"))
    assert state["fault_domain"] is FaultDomain.TAP_OR_ODP

    plan = state["resolution_plan"]
    offered = {o.action_type for o in plan.options}
    assert ActionType.RAISE_MR in offered, "the Dirty Boots half"
    assert ActionType.CREATE_WORK_ORDER in offered, "the Clean Boots half"

    assert route_shared_or_plant(state) == "continue"
    assert route_remote_eligibility(state) == "self_help_check", (
        "a plant maintenance request is not a remote repair; if this says `remote` then an option "
        "in the tap's plan looks console-executable to `is_remote_option` again"
    )
    assert route_self_help_suitability(state) == "field_planning"


async def test_every_offered_option_carries_the_packs_risk_and_approval(fixtures: Any) -> None:
    """The pack's `ActionRule` is read to decide whether to offer an option; it is also recorded.

    A plan is checkpointed and read back during an audit, so an option that named only its action
    type would tell a reader what today's pack says rather than what applied when the plan was
    made. Each option's copy is checked against the pack it was built from, field by field, so a
    propagation that silently stopped copying would fail here rather than in a stale audit.
    """
    pack = PolicyEngine.load().pack
    state, _ = await _run_parent(_service(fixtures, "hfc_degraded_upstream"))
    options = state["resolution_plan"].options
    assert options, "this fixture must produce a non-empty plan for this test to mean anything"

    for option in options:
        rule = pack.remote_actions[option.action_type]
        assert option.risk == rule.risk
        assert option.required_approval == rule.approval_kind

    # And specifically: the joint dispatch's two halves need two different approvals.
    by_action = {o.action_type: o for o in options}
    assert by_action[ActionType.RAISE_MR].required_approval is not None
    assert by_action[ActionType.CREATE_WORK_ORDER].required_approval is not None
    assert (
        by_action[ActionType.RAISE_MR].required_approval
        != by_action[ActionType.CREATE_WORK_ORDER].required_approval
    )


# ------------------------------------------------------------------------------------------------
# The registry
# ------------------------------------------------------------------------------------------------


def test_the_parent_registry_is_the_eleven_nodes_in_specification_order() -> None:
    """`PARENT_NODES` is a tuple because the builder wires edges between consecutive entries.

    `nodes/__init__` already checks at import that each registry key matches the name its `@node`
    decorator stamps into the audit trail. What it cannot check is that the order is the one the
    specification numbers, because nothing in the code knows what P01 means. That is written here.
    """
    assert [name for name, _ in PARENT_NODES] == [
        "receive_signal",
        "normalize_event",
        "resolve_identity_and_topology",
        "deduplicate_and_correlate",
        "assess_impact_and_priority",
        "create_or_attach_incident",
        "assemble_case_evidence",
        "create_diagnostic_test_plan",
        "execute_read_only_tests",
        "determine_root_cause",
        "generate_resolution_options",
    ]
