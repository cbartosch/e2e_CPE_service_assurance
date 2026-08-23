"""D04's preventive arm, compiled and run against every fixture that can reach it.

Unlike its two siblings this stage has no interrupt, so there is no `Command(resume=...)` anywhere
below and no paused-graph fixture. What replaces them is a sweep: the stage's whole job is to
*choose*, the choice is a function of what the network said, and the only way to know the three arms
are three real answers rather than one answer and two decorations is to run all 41 services and
count. `_sweep` does that once for the module.

The measurement, so it does not have to be re-derived
-----------------------------------------------------
Of the 41 fixture services, **17** answer `D04:preventive` -- the other 24 are `active`, and
`route_predictive_or_active` is the reason: only `predictive_maintenance` and
`post_install_baseline` are preventive case types at all. Those 17 split **3 field work / 2 remote
prevention / 12 monitoring**, and every one of the five acting cases is named in the tests below
rather than counted, because a count that changed would not say which service stopped being seen.

Two of those numbers are the ones the design turned on. The three field-work cases are found by the
access-layer detectors and are *invisible* to the Wi-Fi forecast -- see the subgraph's module
docstring for the earlier draft that keyed on `should_dispatch` and could not fire. And twelve
monitoring cases is the intended shape rather than a stage that does nothing: a predictive sweep
whose usual answer is "send a crew" has its threshold in the wrong place.

What is asserted here and not in `test_builder.py`
--------------------------------------------------
`route_preventive_disposition` is asked *inside* this graph, so unlike D10 and D12 it is exercised
here rather than on the parent's edge. The parent's side of the wiring -- that `D04:preventive`
reaches this subgraph at all, and that the subgraph is terminal -- is `test_builder.py`'s, and the
one test below that runs the parent end to end is here only because it is the claim that the two
halves meet.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END, START

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.decision_services.forecast import should_dispatch
from lpr_cpe.domain.boundaries import crew_for
from lpr_cpe.domain.enums import (
    CaseType,
    CrewType,
    EventSource,
    FaultDomain,
    HealthBand,
    IncidentStatus,
    KPIName,
    Severity,
    Technology,
)
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import PENDING_STAGES, build_parent_graph, compile_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.guards import ESCALATED, ONWARD
from lpr_cpe.graph.nodes._runtime import check_node_registry
from lpr_cpe.graph.routing import route_predictive_or_active
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.graph.subgraphs._shared import evidence_support
from lpr_cpe.graph.subgraphs.preventive_maintenance import (
    DISPOSITION_TARGETS,
    INSUFFICIENT_EVIDENCE,
    PREVENTIVE_MAINTENANCE_NODES,
    apply_remote_prevention,
    build_preventive_maintenance_graph,
    physical_findings,
    plan_preventive_field_work,
    record_monitoring,
    route_preventive_disposition,
    sources_for,
)
from lpr_cpe.observability.kpi import NOT_DERIVABLE_FROM_STATE
from lpr_cpe.policies.engine import PolicyEngine
from lpr_cpe.policies.loader import load_pack
from lpr_cpe.policies.models import EvidencePolicy

NOW = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The one PON service with a degraded optical *reading*, and the worked example for the field-work
#: arm. Not the one service that fires `pon_optical_degradation`, which this line used to say:
#: `SVC-VQ-002-A-01` raises the same detector on a powered-off ONT and a dying gasp, with no optical
#: measurement in the window at all. Both are PON and both plan field work.
DEGRADED_OPTICAL_SERVICE = "SVC-UT-001-A-03"

#: An HFC service whose radios band `at_risk` -- not `healthy`, which this line used to say -- and
#: name two levers anyway. The worked example for remote prevention, and the proof that "below the
#: band that would send anyone" and "nothing to do" are different facts: `should_dispatch` is
#: `False` here, against the shipped pack's `critical`.
LEVERED_WIFI_SERVICE = "SVC-SJ-011-B-01"

#: A service with nothing wrong with it at all. The worked example for monitoring.
QUIET_SERVICE = "SVC-UT-001-B-02"

#: Every service that reaches a disposition, by the arm it reaches. Named rather than counted; see
#: the module docstring.
EXPECTED_DISPOSITIONS: dict[str, str] = {
    "SVC-PO-042-A-04": "field_work",
    "SVC-UT-001-A-03": "field_work",
    "SVC-VQ-002-A-01": "field_work",
    "SVC-SJ-011-B-01": "remote_prevention",
    "SVC-VQ-002-B-03": "remote_prevention",
    "SVC-PO-042-B-01": "monitoring",
    "SVC-PO-042-B-02": "monitoring",
    "SVC-PO-042-B-03": "monitoring",
    "SVC-SJ-011-B-02": "monitoring",
    "SVC-SJ-011-B-03": "monitoring",
    "SVC-SJ-011-B-04": "monitoring",
    "SVC-UT-001-B-01": "monitoring",
    "SVC-UT-001-B-02": "monitoring",
    "SVC-UT-001-B-03": "monitoring",
    "SVC-UT-001-B-04": "monitoring",
    "SVC-VQ-002-B-01": "monitoring",
    "SVC-VQ-002-B-02": "monitoring",
}


class _Ticking(FrozenClock):
    """The advance-on-read clock both sibling modules use, and for the same reason.

    Inside a compiled graph the test cannot advance the clock between nodes, so a frozen instant
    would make every `observed_at` identical and `evidence_support`'s age zero by construction.
    Subclassed off `FrozenClock` so `local_now()` and `timezone` stay the production ones.
    """

    def now(self) -> datetime:
        return self.advance(timedelta(seconds=3))


def _initial(service: dict[str, Any], *, case_type: CaseType) -> Any:
    """One incident, filed as whatever `case_type` says.

    The case type is a parameter and not a constant because D04 reads it and nothing else here
    does: `test_only_a_preventive_case_type_reaches_this_stage` is the test that needs the other
    value, and hand-building a second initial-state helper for it would be two helpers to keep in
    step.
    """
    return make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=AssuranceEvent(
            event_id=f"EVT-{service['service_ref']}",
            source=EventSource.NXT,
            case_type=case_type,
            technology=Technology(service["technology"]),
            severity=Severity.HIGH,
            occurred_at=NOW - timedelta(minutes=6),
            received_at=NOW - timedelta(minutes=5),
            customer_ref=service["customer_ref"],
            service_ref=service["service_ref"],
            cpe_ref=service["cpe_ref"],
            summary=f"predictive risk forecast for {service['service_ref']}",
        ),
        sla=SLAContext(
            clock_started_at=NOW - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=NOW,
    )


async def _to_d04(service: dict[str, Any], *, case_type: CaseType) -> Any:
    """Run the parent as far as D04 and hand back the state the subgraph would be entered with.

    `interrupt_after` rather than a hand-built state, for the reason
    `test_subgraph_remote_resolution.py` gives about its own fixture: the five intake steps are what
    put `evidence`, `impact` and `topology` on the state, and a constructed substitute would let
    this module pass while intake stopped producing one of them. The evidence floor measured in
    `test_no_fixture_can_miss_the_shipped_evidence_bar` is exactly a fact about those five steps,
    and asserting it against a fabricated state would be asserting it against this file.
    """
    parent = build_parent_graph().compile(
        name="lpr_cpe_parent", interrupt_after=["assess_impact_and_priority"]
    )
    return await parent.ainvoke(
        _initial(service, case_type=case_type), context=build_context(clock=_Ticking(NOW))
    )


async def _through(
    service: dict[str, Any],
    *,
    policy: PolicyEngine | None = None,
    node_visit_budget: dict[str, int] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Parent to D04, then the whole subgraph. Returns the final state and the context it ran in.

    A fresh context for the subgraph so that `ctx.adapters.gate.recorded` reports what *this stage*
    asked for rather than what intake did -- the write assertion below would otherwise be reading a
    gate five other nodes had already used.
    """
    entering = await _to_d04(service, case_type=CaseType.PREDICTIVE_MAINTENANCE)
    ctx = build_context(  # type: ignore[arg-type]
        clock=_Ticking(NOW), policy=policy, node_visit_budget=node_visit_budget
    )
    graph = build_preventive_maintenance_graph().compile(name="lpr_cpe_preventive_maintenance")
    final = await graph.ainvoke(entering, context=ctx)
    return final, ctx


@pytest.fixture(scope="module")
def sweep(fixtures: Any) -> dict[str, dict[str, Any]]:
    """Every service that answers `D04:preventive`, run through the whole subgraph. Keyed by ref.

    Module-scoped and synchronous, driving `asyncio.run` itself, because
    `asyncio_default_fixture_loop_scope` is `function`: an async fixture wanting to outlive one test
    would be asking for a loop that is already closed. The sweep is about a second and a half, which
    is cheap enough that this is a convenience rather than a necessity -- but every test below reads
    it, and running the parent 41 times per test would not be.

    `disposition` is read off `node_visits` -- which arm actually ran -- and *not* by calling
    `route_preventive_disposition(final)`, which would be wrong. The router reads `case.status`, and
    the arm it selects then overwrites `case.status` with its own disposition, so asking the router
    again after the run is asking it about a state it never saw. Measured on `SVC-UT-001-A-03` with
    the evidence bar raised to 8: the run visits `record_monitoring`, and the re-read answers
    `field_work`. The router is correct; re-reading it is not. This fixture had that bug and
    `test_raising_the_bar_overrules_an_actionable_finding` is what caught it.

    The exclusivity assertion is not decoration: it is what makes `disposition` a single answer
    rather than the first of several. Shown red by pointing `DISPOSITION_TARGETS["monitoring"]` at
    `assess_predictive_risk`, a node every run visits -- verbatim:

        E   AssertionError: SVC-PO-042-A-04 visited ['plan_preventive_field_work',
            'assess_predictive_risk'], and the arms are meant to be exclusive
        E   assert 2 == 1

    It fails in the fixture, so every test that reads the sweep errors at once. That is the intended
    blast radius. A `DISPOSITION_TARGETS` naming a node on the main path is not one broken test.
    """
    arm_disposition = {node: answer for answer, node in DISPOSITION_TARGETS.items()}

    async def run_all() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for ref, service in sorted(fixtures.services.items()):
            entering = await _to_d04(service, case_type=CaseType.PREDICTIVE_MAINTENANCE)
            if route_predictive_or_active(entering) != "preventive":
                continue
            graph = build_preventive_maintenance_graph().compile(
                name="lpr_cpe_preventive_maintenance"
            )
            final = await graph.ainvoke(
                entering,
                context=build_context(clock=_Ticking(NOW)),  # type: ignore[arg-type]
            )
            arms = [node for node in arm_disposition if final["node_visits"].get(node)]
            assert len(arms) == 1, f"{ref} visited {arms}, and the arms are meant to be exclusive"
            out[ref] = {
                "entering": entering,
                "final": final,
                "disposition": arm_disposition[arms[0]],
            }
        return out

    return asyncio.run(run_all())


# ------------------------------------------------------------------------------------------------
# The shape LangGraph received
# ------------------------------------------------------------------------------------------------


def test_the_subgraph_holds_five_nodes_and_the_edges_the_dispositions_imply() -> None:
    """Read back out of the `StateGraph`, not off `DISPOSITION_TARGETS`, which equals itself.

    The asymmetry is the thing to read. Two conditional edges carry `__escalated__` and three plain
    edges do not, and that is not an oversight: `guarded` exists to divert an escalated thread away
    from the next node, and an edge whose only destination is already `END` has nothing to divert it
    from. Wrapping those three would add a branch both of whose arms are the same -- which is
    exactly what the parent's own terminal edge into `END` does, and `test_builder.py` says there
    why the parent does it anyway. The difference is that the parent draws one kind of edge from a
    table and this file draws each one by hand.
    """
    graph = build_preventive_maintenance_graph()
    ends = {
        source: dict(branch.ends or {})
        for source, branches in graph.branches.items()
        for branch in branches.values()
    }

    assert ends == {
        "assess_predictive_risk": {ONWARD: "open_preventive_case", ESCALATED: END},
        "open_preventive_case": {**DISPOSITION_TARGETS, ESCALATED: END},
    }
    assert sorted(graph.edges) == [
        (START, "assess_predictive_risk"),
        ("apply_remote_prevention", END),
        ("plan_preventive_field_work", END),
        ("record_monitoring", END),
    ]
    assert set(graph.nodes) == {name for name, _ in PREVENTIVE_MAINTENANCE_NODES}


def test_every_disposition_names_a_node_that_exists() -> None:
    """`DISPOSITION_TARGETS` is a second table and this is the check that keeps it honest.

    The router returns a `Literal` of three strings and the map turns each into a node name. Neither
    half knows about the other, so a renamed arm would leave a path map pointing at a node LangGraph
    does not have -- which `add_conditional_edges` accepts and only fails on at run time, and only
    for the one incident unlucky enough to take that arm.
    """
    registered = {name for name, _ in PREVENTIVE_MAINTENANCE_NODES}
    assert set(DISPOSITION_TARGETS.values()) <= registered
    assert set(DISPOSITION_TARGETS) == {"field_work", "remote_prevention", "monitoring"}


def test_a_registry_key_that_is_not_the_stamped_name_is_refused() -> None:
    """`check_node_registry` shown red on this registry, in isolation, for its own reason.

    It runs at import of the subgraph module, so it cannot be provoked in place without breaking
    the import every other test needs. Handed a copy with one key misspelt it reports, verbatim:

        the preventive-maintenance node registry disagrees with the @node decorators:
        {'assess_predictive_risks': 'assess_predictive_risk'}. The key is the LangGraph node name
        and the decorator's name is what appears in the audit trail; they must be the same string.

    Which is the failure that matters: LangGraph would route to `assess_predictive_risks` and every
    audit event the node wrote would say `assess_predictive_risk`, so an incident could not be
    traced through the stage that handled it.
    """
    misspelt = tuple(
        ("assess_predictive_risks", fn) if name == "assess_predictive_risk" else (name, fn)
        for name, fn in PREVENTIVE_MAINTENANCE_NODES
    )
    with pytest.raises(RuntimeError, match="disagrees with the @node decorators"):
        check_node_registry(misspelt, "the preventive-maintenance node registry")


def test_a_stage_pasted_twice_is_refused() -> None:
    """The other half of the same guard, red for its own separate reason.

    Duplicated rather than misspelt, so the message is the other branch of `check_node_registry`:

        the preventive-maintenance node registry has duplicate names: ['assess_predictive_risk']

    `add_node` would raise on this too, but only once a builder ran against a real graph. A tuple
    entry pasted twice is a plausible edit and this catches it at import.
    """
    with pytest.raises(RuntimeError, match="has duplicate names"):
        check_node_registry(
            (*PREVENTIVE_MAINTENANCE_NODES, PREVENTIVE_MAINTENANCE_NODES[0]),
            "the preventive-maintenance node registry",
        )


# ------------------------------------------------------------------------------------------------
# What the stage reads
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("technology", "expected"),
    [
        (Technology.HFC, ("cpe.wifi", "nxt.rf", "nxt.pnm")),
        (Technology.PON, ("cpe.wifi", "plant.optical")),
        (Technology.UNKNOWN, ("cpe.wifi",)),
    ],
)
def test_the_read_is_narrow_and_technology_specific(
    technology: Technology, expected: tuple[str, ...]
) -> None:
    """Four of `evidence.SOURCES`' twelve, and never the wrong plant for the technology.

    `UNKNOWN` is the case worth stating. It reads the radios only, because `nxt.rf` on a PON service
    resolves to an adapter with nothing to say and the `AdapterUnavailableError` that follows would
    be recorded as a data-quality defect against a service whose only defect is that P03 could not
    work out what it was.
    """
    assert sources_for(technology) == expected


def test_the_stage_reads_what_the_forecast_and_the_detectors_actually_need() -> None:
    """The narrow list is not arbitrary: each name feeds something, and nothing feeds nothing.

    Asserted as a partition rather than a list, so that adding a source without a consumer fails
    here rather than costing every predictive scan an adapter call whose result nothing reads.
    """
    forecast_input = {"cpe.wifi"}
    hfc_detector_input = {"nxt.rf", "nxt.pnm"}
    pon_detector_input = {"plant.optical"}

    every_source = set(sources_for(Technology.HFC)) | set(sources_for(Technology.PON))
    assert every_source == forecast_input | hfc_detector_input | pon_detector_input
    assert set(sources_for(Technology.HFC)) == forecast_input | hfc_detector_input
    assert set(sources_for(Technology.PON)) == forecast_input | pon_detector_input


# ------------------------------------------------------------------------------------------------
# The three arms, over the fixture set
# ------------------------------------------------------------------------------------------------


def test_all_three_dispositions_are_reached_and_by_these_services(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """The measurement the design rests on: 17 services, three arms, none of them empty.

    By name rather than by count. A count of three field-work cases would still read as three if
    `SVC-UT-001-A-03` stopped being seen and some healthy service started being dispatched on, and
    that is the substitution this stage exists to prevent.
    """
    assert {ref: row["disposition"] for ref, row in sweep.items()} == EXPECTED_DISPOSITIONS

    reached = set(EXPECTED_DISPOSITIONS.values())
    assert reached == set(DISPOSITION_TARGETS), (
        f"only {sorted(reached)} of the three dispositions can be reached from the fixture set; an "
        "arm no fixture takes is an arm nothing has ever run"
    )


def test_the_field_work_arm_is_taken_for_a_fault_the_radios_cannot_see(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """The measurement that killed the `should_dispatch` draft, asserted rather than remembered.

    All three field-work cases have a healthy or absent Wi-Fi band. An arm keyed on the forecast
    would therefore have dispatched on none of them, and -- since no fixture produces a `critical`
    band at all -- on nothing else either. The fault is in the fibre and the radios are the wrong
    instrument for it.
    """
    dispatched = [ref for ref, row in sweep.items() if row["disposition"] == "field_work"]
    assert dispatched == ["SVC-PO-042-A-04", "SVC-UT-001-A-03", "SVC-VQ-002-A-01"]

    for ref in dispatched:
        prediction = sweep[ref]["final"].get("prediction")
        band = prediction.band if prediction is not None else None
        assert band is None or band.value != "critical", (
            f"{ref} has Wi-Fi band {band}; if a fixture ever does read critical, the claim that "
            "the forecast is blind to these faults needs re-measuring rather than re-asserting"
        )
        assert physical_findings(sweep[ref]["final"]), (
            f"{ref} took the field-work arm with no actionable access-layer finding behind it"
        )


def test_the_field_work_arm_records_the_domain_and_the_crew_and_stops(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """What `plan_preventive_field_work` hands to P14, and what it deliberately does not do.

    The crew is `crew_for`'s answer and not this stage's, which is the whole argument for collapsing
    Clean Boots and Dirty Boots into one arm: the fact has an owner, and this node reads it rather
    than minting a second copy. `within_24_hours` follows from the finding's `critical` severity,
    and it is a named window rather than a date because P14 holds the calendar.
    """
    final = sweep[DEGRADED_OPTICAL_SERVICE]["final"]
    case = final["pm_case"]

    assert case.status == "planned_field_work"
    assert case.recommended_window == "within_24_hours"
    assert case.priority_score == 1.0

    (event,) = [a for a in final["audit_events"] if a.node == "plan_preventive_field_work"]
    assert event.detail["suspected_domain"] == FaultDomain.DISTRIBUTION.value
    assert event.detail["crew"] == CrewType.DIRTY.value
    assert event.detail["detector"] == "pon_optical_degradation"

    assert final.get("work_order") is None, "P14 owns the work order; this stage only recommends"
    assert case.linked_incident_id is None, "P06 is on D04's other arm; no incident exists here"


def test_no_fixture_produces_a_clean_boots_crew(sweep: dict[str, dict[str, Any]]) -> None:
    """The measurement behind the deviation recorded in `docs/vendor-integration-gaps.md`.

    The specification names planned Clean Boots work and planned Dirty Boots work as two separate
    dispositions. Every physical finding this stage can produce classifies to `CrewType.DIRTY`, so
    splitting the arm would have added a branch no fixture takes -- to answer a question D13 owns.

    This is the test to read if the fixture set grows a Clean Boots case. It will not fail; it will
    stop being able to make its point, and the assertion message says what to do about that.
    """
    crews = {
        finding.suspected_domain
        for row in sweep.values()
        for finding in physical_findings(row["final"])
    }
    assert crews, "no fixture produced a physical finding at all, so this measures nothing"
    assert {crew_for(domain) for domain in crews} == {CrewType.DIRTY}, (
        "a fixture now produces a CLEAN-crew finding. The single field-work arm still records the "
        "crew correctly -- `crew_for` is doing the work -- but the vendor-integration-gaps entry "
        "claiming the split is unmeasurable is now out of date and should be re-measured."
    )


def test_the_remote_prevention_arm_selects_levers_and_executes_nothing(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """Two services, both banding `at_risk`, both with something worth changing anyway.

    That combination is the reason the arm keys on `recommended_actions` rather than on the band:
    a band is a summary and a lever is a specific breached metric with a specific remedy. A verdict
    breaching only `throughput_mbps` names no lever and correctly falls through to monitoring.

    This docstring said "a healthy band" until the bands were measured rather than assumed, and the
    corrected reading is the stronger one: `at_risk` is *worse* than healthy and `should_dispatch`
    still refuses it, so the arm is not rescuing services the band called fine -- it is acting on
    two the band called degrading and declined to send anyone to.
    `test_no_band_in_the_sweep_would_dispatch_anyone` is where that now has an owner.
    """
    selected = [ref for ref, row in sweep.items() if row["disposition"] == "remote_prevention"]
    assert selected == ["SVC-SJ-011-B-01", "SVC-VQ-002-B-03"]

    final = sweep[LEVERED_WIFI_SERVICE]["final"]
    case = final["pm_case"]
    assert case.status == "remote_prevention_selected"
    assert case.recommended_window == "next_maintenance_window"

    (event,) = [a for a in final["audit_events"] if a.node == "apply_remote_prevention"]
    assert event.detail["recommended_actions"] == ["wifi_channel_change", "cpe_resync"]
    assert "Selection only" in event.detail["note"], (
        "the note is what a queue reader sees; if it stops saying the action was not taken, this "
        "stage starts looking like it rebooted somebody's router"
    )


def _band_of(final: dict[str, Any]) -> str:
    """The Wi-Fi band behind one disposition, with the two ways of having none kept apart.

    `forecast_wifi` returns `None` for a CPE that reported no readable metric, and a
    `PredictionResult` carrying `band=None` would be a different thing entirely -- a forecast that
    ran and declined to band its own score. Only the first happens here. They are labelled
    differently so that if the second ever starts happening the assertion below says so, rather
    than absorbing it into the same bucket the way `assess_predictive_risk`'s audit detail does.
    """
    prediction = final["pm_case"].prediction
    if prediction is None:
        return "no radio data"
    return prediction.band.value if prediction.band is not None else "banded nothing"


def test_no_band_in_the_sweep_would_dispatch_anyone(sweep: dict[str, dict[str, Any]]) -> None:
    """The measurement behind the subgraph docstring's "the arm could not be taken".

    An earlier draft keyed field work on `forecast.should_dispatch`. Nothing pinned the bands it
    would have read, so two comments in this module described them from memory and both said
    `healthy` where the fixtures say `at_risk`. `EXPECTED_DISPOSITIONS` pins which arm each service
    takes; this pins what the forecast said while that arm was being chosen, which is the other
    half of every argument the stage makes about why those are not the same question.

    The cross-tab is asserted whole rather than per service, because the shape is the point:
    `healthy` appears against `field_work` as well as `monitoring`, and the only services that are
    *not* healthy are the two the band would have done nothing for. The band predicts the arm in
    neither direction.
    """
    observed = Counter((_band_of(row["final"]), row["disposition"]) for row in sweep.values())
    assert dict(observed) == {
        ("healthy", "field_work"): 2,
        ("healthy", "monitoring"): 10,
        ("at_risk", "remote_prevention"): 2,
        ("no radio data", "field_work"): 1,
        ("no radio data", "monitoring"): 2,
    }

    bands = load_pack().health_bands
    dispatchable = sorted(
        ref
        for ref, row in sweep.items()
        if (band := _band_of(row["final"])) in set(HealthBand)
        and should_dispatch(HealthBand(band), bands)
    )
    assert dispatchable == [], (
        f"{dispatchable} would now be dispatched on the Wi-Fi band alone, against a threshold of "
        f"{bands.dispatch_threshold_band.value}. The subgraph's module docstring argues the "
        "field-work arm could not have been keyed on `should_dispatch` because no fixture crosses "
        "that line. That paragraph needs re-measuring rather than this assertion relaxing."
    )


def test_the_monitoring_arm_distinguishes_quiet_from_blind(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """ "We looked and it was fine" and "we could not see enough" are the same arm and different
    facts, and only one of them is a reason to scan again sooner.

    The distinction is carried in the case note rather than in a fourth disposition, because the
    disposition really is the same -- watch it -- and a branch with nothing different at the end of
    it would be a branch that exists to be counted.
    """
    final = sweep[QUIET_SERVICE]["final"]
    case = final["pm_case"]

    assert case.status == "monitoring"
    assert case.recommended_window == "", "nothing is scheduled, and an empty window says so"
    assert case.priority_score == 0.0
    assert case.notes == [
        "monitoring: Wi-Fi band healthy, no actionable access-layer finding and no remote lever "
        "indicated"
    ]


def test_monitoring_is_the_usual_answer_and_that_is_the_point(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """Twelve of seventeen, and a stage whose usual answer was "dispatch" would be miscalibrated.

    Asserted as a majority rather than as the number twelve, so that a fixture set which grew a
    genuinely sick service does not fail a test about calibration.
    """
    dispositions = [row["disposition"] for row in sweep.values()]
    assert dispositions.count("monitoring") > len(dispositions) / 2


# ------------------------------------------------------------------------------------------------
# The evidence bar
# ------------------------------------------------------------------------------------------------


def test_no_fixture_can_miss_the_shipped_evidence_bar(sweep: dict[str, dict[str, Any]]) -> None:
    """The honest statement about a bound that cannot fire at the defaults, and why it is kept.

    Measured: the fewest distinct source systems any of the 17 arrives at D04 with is **3**, against
    a shipped `min_sources_for_diagnosis` of **2** -- and the count is taken over the whole evidence
    list, so this stage's own reads can only push it up. Failing every adapter this stage calls
    would not trip the bar.

    The floor is structural rather than lucky. P02 emits one evidence item unconditionally under
    `event.source.value`, P03 emits one unconditionally under the plant adapter's name or the
    literal `"topology"`, and no `EventSource` value collides with `pon`, `hfc` or `topology`. So the
    count is at least 2 before this stage exists.

    `graph.guards` deleted a bound for being unable to fire, so keeping this one needs a reason and
    the reason is that the two are different shapes. That one was dominated by another bound on the
    same counter -- dead whatever anyone configured. This one is dead only at the shipped number,
    which is an operator's to change, and the sibling test raises it and shows the arm firing. Both
    directions asserted is the same resolution `step_budget` uses for its two owners.
    """
    bar = load_pack().evidence.min_sources_for_diagnosis
    floor = min(evidence_support(row["entering"], NOW)[0] for row in sweep.values())

    assert bar == 2, "the docstring's arithmetic is against 2; re-measure the floor if it moves"
    assert floor >= bar, "the floor fell below the bar, so the bound is live -- delete this test"
    assert floor == 3

    assert not [
        row for row in sweep.values() if row["final"]["pm_case"].status == INSUFFICIENT_EVIDENCE
    ], (
        "a fixture now falls below the shipped bar. That is the bound becoming live, which is "
        "good -- but this test and the module docstring both claim it cannot, and they are wrong."
    )


async def test_raising_the_bar_overrules_an_actionable_finding(fixtures: Any) -> None:
    """The evidence bar shown red, in isolation, for its own reason.

    Run against `SVC-UT-001-A-03` -- the case that would otherwise be planned field work off a
    `critical` optical finding with a priority score of 1.0 -- with the pack's bar raised from 2 to
    8, above the 6 sources it actually has. The same service at the two bars, measured verbatim:

        --- shipped bar=2 ---
          status  : planned_field_work
          findings: 1
          window  : 'within_24_hours'
          arm run : ['plan_preventive_field_work']
          audit   : open_preventive_case create_pm_case opened
                    {..., 'priority_score': 1.0, 'findings': 1, 'sources_read': 6,
                     'minimum_sources': 2}
        --- raised bar=8 ---
          status  : monitoring
          findings: 1
          window  : ''
          arm run : ['record_monitoring']
          audit   : open_preventive_case create_pm_case insufficient_evidence
                    {..., 'priority_score': 1.0, 'findings': 1, 'sources_read': 6,
                     'minimum_sources': 8}

    Two things are proved and the second is the one worth having. The bar changes the *status*,
    which a test of `open_preventive_case` alone would show; and it changes *which arm runs*, which
    only running the graph can show. A bar that marked the case thin and then scheduled the visit
    anyway would be a bar in name only, and the finding is still there in `case.findings` -- it was
    overruled, not hidden. Nothing else moved: same case id, same priority score, same one finding,
    same six sources read. The bar is the only difference between the two runs.

    Shown red by deleting question 1 from `route_preventive_disposition` -- the two lines that read
    `INSUFFICIENT_EVIDENCE` -- so that `open_preventive_case` still marks the case thin and the router
    schedules the visit regardless. Verbatim:

        >       assert "plan_preventive_field_work" not in final["node_visits"], (
        E       AssertionError: the bar did not overrule the finding: the case was marked thin and
                the visit was scheduled anyway, which is a bar in name only
        E       assert 'plan_preventive_field_work' not in {'assess_impact_and_priority': 1,
                'assess_predictive_risk': 1, 'deduplicate_and_correlate': 1, 'normalize_event': 1,
                ...}

    That assertion is deliberately first. Neutering the bar the other way -- hardcoding
    `status="open"` in `open_preventive_case` -- fails instead on `case.status`, with
    `assert 'planned_field_work' == 'monitoring'`, and so does the router perturbation if the status
    check is asked first, because the arm overwrites `status` on its way past. Both reds are real,
    but `'planned_field_work' == 'monitoring'` reports a symptom two writes downstream of the fault
    while `node_visits` names the arm that should not have run.

    Which arm ran is read off `node_visits` and not by calling `route_preventive_disposition(final)`.
    That re-read answers `field_work` at *both* bars, because the arm overwrites the `case.status`
    the router reads -- see the router's docstring, and the `sweep` fixture, which had this bug until
    this test caught it. The lost `''` window is worth noticing too: `open_preventive_case` had
    computed `within_24_hours`, and `record_monitoring` is the arm that declines to commit to one.

    The pack is rebuilt through `EvidencePolicy(**{...})` rather than `model_copy`, so the modified
    policy goes through validation exactly as a loaded one does. `model_copy` would accept a bar of
    `-1` and this test would then be asserting against a pack the loader would have rejected.
    """
    pack = load_pack()
    strict = pack.model_copy(
        update={
            "evidence": EvidencePolicy(
                **{**pack.evidence.model_dump(), "min_sources_for_diagnosis": 8}
            )
        }
    )
    clock = _Ticking(NOW)
    final, _ctx = await _through(
        fixtures.services[DEGRADED_OPTICAL_SERVICE],
        policy=PolicyEngine(strict, clock=clock),  # type: ignore[arg-type]
    )
    case = final["pm_case"]

    assert "plan_preventive_field_work" not in final["node_visits"], (
        "the bar did not overrule the finding: the case was marked thin and the visit was "
        "scheduled anyway, which is a bar in name only"
    )
    assert final["node_visits"].get("record_monitoring") == 1
    assert case.status == "monitoring"
    assert case.recommended_window == ""
    assert case.findings, "the finding is overruled, not deleted; the queue still has to see it"
    assert case.notes == [
        "monitoring: 6 source(s) read against a minimum of 8, which is too thin to schedule work on"
    ]

    (opened,) = [a for a in final["audit_events"] if a.node == "open_preventive_case"]
    assert opened.outcome == INSUFFICIENT_EVIDENCE
    assert opened.detail["sources_read"] == 6
    assert opened.detail["minimum_sources"] == 8


# ------------------------------------------------------------------------------------------------
# What cannot happen, and what happens instead
# ------------------------------------------------------------------------------------------------


async def test_an_escalation_before_the_case_exists_enters_no_arm(fixtures: Any) -> None:
    """Why `route_preventive_disposition`'s `case is None` branch is totality and not a path.

    `open_preventive_case` is given a re-entry budget of zero, so `check_budgets` fires on entry and
    the `@node` wrapper returns the escalation *without running the body*. No `pm_case` is written --
    and `guarded` then answers `__escalated__` before the router is called at all, so the branch
    that would have handled the missing case is never reached.

    That is the measurement the three arms take the opposite view of: they raise rather than record,
    because a node may raise and a router may not.

    The branch is then called directly, because a branch no run can reach is a branch no run can
    check, and the alternative to pinning it here is leaving the documented answer to be discovered
    by whoever eventually rewires the edge. Calling it is the only way to see it: the state below is
    one the graph cannot produce. Shown red by changing that `return` to `"field_work"`, verbatim:

        >       assert route_preventive_disposition(final) == "monitoring", (
        E       AssertionError: the router raised or picked an arm on a state with no case; it may
                do neither, because LangGraph would surface the first as an edge failure with no
                node to blame it on, and the second would schedule work off a case that was never
                opened
        E       assert 'field_work' == 'monitoring'

    Note what stayed green under that change: every other test in this module, including the whole
    41-service sweep. That is the point of the assertion. The branch is unreachable, so nothing else
    can notice it is wrong, and an unreachable branch that no test pins is a decision recorded only
    in a comment.
    """
    final, _ctx = await _through(
        fixtures.services[DEGRADED_OPTICAL_SERVICE],
        node_visit_budget={"open_preventive_case": 0},
    )

    assert final["escalated"] is True
    assert "node_reentries budget exhausted" in final["escalation_reason"]
    assert final.get("pm_case") is None
    assert set(final["node_visits"]) & set(DISPOSITION_TARGETS.values()) == set(), (
        "an arm ran after the guard stopped the stage, so the escalation edge is not wired"
    )

    assert route_preventive_disposition(final) == "monitoring", (
        "the router raised or picked an arm on a state with no case; it may do neither, because "
        "LangGraph would surface the first as an edge failure with no node to blame it on, and "
        "the second would schedule work off a case that was never opened"
    )


@pytest.mark.parametrize(
    "arm", [plan_preventive_field_work, apply_remote_prevention, record_monitoring]
)
async def test_an_arm_entered_without_a_case_says_so_loudly(arm: Any) -> None:
    """`_case_or_raise` shown red, in isolation, for each of the three callers.

    Called through `__wrapped__` so the guard check is skipped and the body is entered directly,
    which is the only way to reach a state the wired graph cannot produce. Verbatim:

        plan_preventive_field_work was entered with no `pm_case`. Only `open_preventive_case`
        writes one and only its conditional edge reaches this node, so either that edge has been
        rewired or an arm has been given a second predecessor. There is nothing to record a
        disposition against.

    Raising rather than recording follows `guards.escalation_update`, which raises on a passing
    verdict for the same reason: quietly writing a disposition against nothing would leave a case in
    the queue that no assessment produced, and nothing downstream would be able to tell.

    `ctx` is `None` and that is safe here -- `_case_or_raise` is the first statement in each body, so
    nothing touches the context before it raises. A body that grew a clock read above it would fail
    this test with an `AttributeError`, which is the right complaint about the wrong ordering.
    """
    with pytest.raises(ValueError, match="was entered with no `pm_case`"):
        await arm.__wrapped__({"incident_id": "INC-NO-CASE"}, None)


async def test_only_a_preventive_case_type_reaches_this_stage(fixtures: Any) -> None:
    """The premise every test above rests on, asserted where it will name itself if it breaks.

    `route_predictive_or_active` reads `case_type` first, so a service filed as a proactive alarm
    goes down the active line no matter how sick it is. Every fixture in the sweep is filed as
    `PREDICTIVE_MAINTENANCE` for that reason, and without this the sweep could quietly become empty
    and every count above would pass against nothing.
    """
    service = fixtures.services[DEGRADED_OPTICAL_SERVICE]

    preventive = await _to_d04(service, case_type=CaseType.PREDICTIVE_MAINTENANCE)
    active = await _to_d04(service, case_type=CaseType.PROACTIVE_ALARM)

    assert route_predictive_or_active(preventive) == "preventive"
    assert route_predictive_or_active(active) == "active"


# ------------------------------------------------------------------------------------------------
# What the stage does not emit
# ------------------------------------------------------------------------------------------------


async def test_the_stage_asks_no_adapter_to_write(fixtures: Any) -> None:
    """A predictive scan is a read. Every arm records a decision; none of them acts on it.

    The context is fresh for the subgraph, so the gate reports what this stage asked for rather than
    what the five intake steps did before it. `apply_remote_prevention` is the arm this is really
    about: it is the one holding a list of executable actions.
    """
    for ref in (DEGRADED_OPTICAL_SERVICE, LEVERED_WIFI_SERVICE, QUIET_SERVICE):
        _final, ctx = await _through(fixtures.services[ref])
        assert list(ctx.adapters.gate.recorded) == [], f"{ref} wrote to an adapter"


def test_the_stage_emits_no_kpi(sweep: dict[str, dict[str, Any]]) -> None:
    """No KPI is emitted here, and the two that name this stage provably cannot be.

    `PREDICTIVE_SCANS_RUN` and `PREDICTIVE_TRUE_POSITIVE_RATE` are both in
    `observability.kpi.NOT_DERIVABLE_FROM_STATE`: the twice-daily scan is a batch job rather than an
    incident thread, and a true-positive rate read off one incident's state is 1.0 by construction.

    Asserted as a difference across the subgraph rather than as an empty list, because intake emits
    `DATA_QUALITY_DEFECT_RATE` before D04 and an equality against `[]` would be asserting that P02
    had stopped working.
    """
    assert KPIName.PREDICTIVE_SCANS_RUN in NOT_DERIVABLE_FROM_STATE
    assert KPIName.PREDICTIVE_TRUE_POSITIVE_RATE in NOT_DERIVABLE_FROM_STATE

    for ref, row in sweep.items():
        before = {e.event_id for e in row["entering"].get("kpi_events", [])}
        after = {e.event_id for e in row["final"].get("kpi_events", [])}
        assert after == before, f"{ref} emitted a KPI from the preventive stage"


def test_the_stage_creates_no_incident(sweep: dict[str, dict[str, Any]]) -> None:
    """The one thing this branch is defined by not doing.

    P06 is on D04's other arm. The case is linked through `linked_records["pm_case"]` instead, which
    is what a later thread reads to find it -- the link is made from the incident's side because the
    incident is the thing that arrives later.
    """
    for ref, row in sweep.items():
        final = row["final"]
        assert final["pm_case"].linked_incident_id is None, f"{ref} claims an incident"
        assert final["linked_records"]["pm_case"] == final["pm_case"].case_id, (
            f"{ref} has no way back to its own case"
        )
        assert "create_or_attach_incident" not in final["node_visits"], f"{ref} ran P06"


def test_the_stage_records_diagnosing_and_no_arm_moves_it_on(
    sweep: dict[str, dict[str, Any]],
) -> None:
    """`assess_predictive_risk` sets the status and the three arms leave it alone.

    `DIAGNOSING` is legal from `TRIAGING` and true of what that node does -- it reads telemetry and
    runs detectors. What none of the arms does is set `DISPATCH_PLANNING`, not even the one that
    plans a visit: recommending a visit is not entering the dispatch stage, and a status saying
    otherwise would record the incident as having reached a stage it never reached.
    """
    for ref, row in sweep.items():
        assert row["final"]["status"] is IncidentStatus.DIAGNOSING, ref


# ------------------------------------------------------------------------------------------------
# Where this meets the parent
# ------------------------------------------------------------------------------------------------


async def test_the_parent_runs_the_whole_branch_in_one_pass(fixtures: Any) -> None:
    """Five intake steps, then three inside the subgraph, then the run ends. No interrupt.

    The claim `test_builder.py` cannot make: it asserts the *edge* into this subgraph exists, and
    this asserts that a thread crossing it comes out the other side. `node_visits` is asserted
    exactly rather than by count, so a run that entered two arms would fail here rather than total
    eight either way.

    Ending is the correct behaviour and `PENDING_STAGES` is where it is explained, which is asserted
    alongside so that "the run stopped" and "we know why the run stopped" cannot drift apart.
    """
    ctx = build_context(clock=_Ticking(NOW))  # type: ignore[arg-type]
    final = await compile_parent_graph().ainvoke(
        _initial(
            fixtures.services[DEGRADED_OPTICAL_SERVICE],
            case_type=CaseType.PREDICTIVE_MAINTENANCE,
        ),
        context=ctx,
    )

    assert final["node_visits"] == {
        "receive_signal": 1,
        "normalize_event": 1,
        "resolve_identity_and_topology": 1,
        "deduplicate_and_correlate": 1,
        "assess_impact_and_priority": 1,
        "assess_predictive_risk": 1,
        "open_preventive_case": 1,
        "plan_preventive_field_work": 1,
    }
    assert final["escalated"] is False
    assert final["pm_case"].status == "planned_field_work"
    assert list(ctx.adapters.gate.recorded) == []

    assert f"{ONWARD}:preventive_maintenance" in PENDING_STAGES, (
        "the run ends here on purpose and the reason -- P14 owns the work order -- has to be "
        "written down where the builder checks it, or a finished-looking run is all anybody sees"
    )
