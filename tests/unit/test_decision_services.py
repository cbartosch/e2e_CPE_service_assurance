"""The eight decision services, verified by execution against the fixture-backed adapters.

These functions are where a fault becomes a number: how many customers it reaches, when the clock
runs out, which explanations survive, what could be done, and whether the fix held. None of them
call a model or a network, so every test here runs the real function over real fixture payloads --
there is nothing to mock and mocking anything would be testing the mock.

The standard is the same as `test_detectors.py`: falsification, not coverage. For a scoring function
that means showing it can produce *both* answers over the fixture set. Three of the tests below
exist for that reason alone and would pass against a stub that always returned one branch, so they
assert the other branch is reachable too:

* `test_both_sla_branches_are_reachable` -- `_tighter` returning the pack every time satisfies every
  other SLA assertion in this file.
* `test_forecast_recommends_at_least_one_action_across_the_profiles` -- see the regression below.
* `test_every_fault_domain_yields_a_plan_or_says_why_not` -- an empty plan is a real answer for
  three domains and a silent failure for the other twelve.

Four tests are marked REGRESSION and each names the defect it was written against:

* the Wi-Fi breach-to-action map keyed on prose, which matched nothing and silently recommended no
  action for every verdict;
* `homes_behind_delimiter` being defaulted at resolution time, which would have made every estimate
  look like a measurement;
* corroboration promoting the blast-radius scope, hiding a diagnosis that disagrees with what is
  being observed;
* an unrecorded fix taking the short stability window, which lets a plant repair be signed off in
  half the time the pack asks for.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lpr_cpe.decision_services import blast_radius as br
from lpr_cpe.decision_services import forecast, impact, rca, resolution, restoration, sla
from lpr_cpe.decision_services.delimiter import resolve_topology
from lpr_cpe.detectors import DetectorResult, run_detectors, wifi_health_verdict
from lpr_cpe.domain.diagnosis import AnomalyFinding
from lpr_cpe.domain.enums import (
    ActionType,
    FaultDomain,
    HealthBand,
    ReasonCode,
    Severity,
    Technology,
)
from lpr_cpe.policies import PolicyPack, load_pack
from lpr_cpe.simulation.loader import build_simulated_adapters

#: The three domains for which no remote action exists, and an empty resolution plan is correct.
#: `UNKNOWN` and `MULTIPLE` mean diagnosis has not landed, and `NO_FAULT_FOUND` means there is
#: nothing to fix -- in all three the answer is the escalation path, not an action.
NO_REMOTE_ACTION = {FaultDomain.UNKNOWN, FaultDomain.MULTIPLE, FaultDomain.NO_FAULT_FOUND}


@pytest.fixture(scope="module")
def pack() -> PolicyPack:
    return load_pack()


# -- the sweep -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sweep:
    """Topology, findings and the classifier's domain for every fixture service, computed once."""

    #: service_ref -> resolved topology
    topology: dict[str, Any] = field(default_factory=dict)
    #: service_ref -> every finding from every detector
    findings: dict[str, list[AnomalyFinding]] = field(default_factory=dict)
    #: service_ref -> the domain the classifier chose
    domain: dict[str, FaultDomain] = field(default_factory=dict)
    #: service_ref -> the service fixture
    services: dict[str, dict[str, Any]] = field(default_factory=dict)

    def with_findings(self) -> list[str]:
        return [ref for ref, fs in self.findings.items() if fs]


async def _run_sweep(fixtures: Any, make_context: Any, now: datetime) -> Sweep:
    adapters = build_simulated_adapters(fixtures=fixtures)
    sweep = Sweep()
    for ref, svc in fixtures.services.items():
        tech = Technology(svc["technology"])
        payload = (
            await adapters.hfc.fetch_tap_view(svc["delimiter_ref"])
            if tech is Technology.HFC
            else await adapters.pon.fetch_odp_view(svc["delimiter_ref"])
        )
        sweep.topology[ref] = resolve_topology(payload, technology=tech, resolved_at=now)
        sweep.services[ref] = svc

        results: list[DetectorResult] = await run_detectors(
            await make_context(adapters, svc, now=now)
        )
        sweep.findings[ref] = [f for r in results for f in r.findings]
        classifier = {r.detector_name: r for r in results}["fault_domain_classifier"]
        sweep.domain[ref] = next(
            (f.suspected_domain for f in classifier.findings if f.suspected_domain is not None),
            FaultDomain.UNKNOWN,
        )
    return sweep


@pytest.fixture(scope="module")
def sweep(fixtures: Any, make_context: Any) -> Sweep:
    """Every fixture service resolved, detected and classified once, for the whole module."""
    return asyncio.run(_run_sweep(fixtures, make_context, datetime.now(UTC)))


@pytest.fixture(scope="module")
def now() -> datetime:
    return datetime.now(UTC)


def _finding(at: datetime, score: float, severity: Severity, **kw: Any) -> AnomalyFinding:
    return AnomalyFinding(
        detector_name=kw.pop("detector_name", "test_detector"),
        detector_version="1",
        observed_at=at,
        score=score,
        confidence=kw.pop("confidence", 0.9),
        severity=severity,
        explanation="a finding written by the test",
        **kw,
    )


# -- topology resolution ---------------------------------------------------------------------


def test_every_fixture_service_resolves_a_delimiter_of_its_technology(sweep: Sweep) -> None:
    for ref, resolved in sweep.topology.items():
        expected = "tap" if sweep.services[ref]["technology"] == "hfc" else "odp"
        assert resolved.context.delimiter_kind.value == expected, ref


def test_resolution_leaves_the_homes_count_missing_rather_than_defaulting_it(sweep: Sweep) -> None:
    """REGRESSION. `homes_behind_delimiter` must stay `None` here.

    `TopologyContext` documents the field as unknown-rather-than-defaulted, and the substitution
    belongs in `blast_radius.size_of`, where it is recorded as an estimate with a stated basis. If
    resolution filled it in, `BlastRadius.measured` would be `True` for every delimiter in the
    fixture set and `ImpactAssessment.count_is_estimated` would be `False` on a number that came
    from a policy default -- an estimate wearing a measurement's label, in the one field a reviewer
    would check to tell them apart.
    """
    assert sweep.topology, "the sweep found no services"
    for ref, resolved in sweep.topology.items():
        assert resolved.context.homes_behind_delimiter is None, ref


# -- blast radius: the two questions -----------------------------------------------------------


def test_a_cpe_action_is_one_premises_whatever_the_fault_around_it_is(
    sweep: Sweep, pack: PolicyPack
) -> None:
    """The reason `impact_radius` and `action_radius` are two functions.

    A reboot requested for a customer sitting inside a node outage still reaches one modem. If the
    fault's radius were used to gate the action, `network_action_threshold` would hold the cheapest
    remedy behind an approval from an operator who is already busy with the outage.
    """
    topology = next(iter(sweep.topology.values())).context
    action = br.action_radius(topology, action=ActionType.CPE_REBOOT, policy=pack.blast_radius)
    fault = br.impact_radius(
        topology, fault_domain=FaultDomain.NODE_OR_OLT, policy=pack.blast_radius
    )
    assert action.count == 1, action
    assert fault.count > 1, fault


@pytest.mark.parametrize("domain", list(FaultDomain))
def test_impact_radius_sizes_every_fault_domain_and_states_its_basis(
    domain: FaultDomain, sweep: Sweep, pack: PolicyPack
) -> None:
    topology = next(iter(sweep.topology.values())).context
    radius = br.impact_radius(topology, fault_domain=domain, policy=pack.blast_radius)
    assert radius.count > 0, radius
    assert radius.basis.strip(), radius


@pytest.mark.parametrize("action", list(ActionType))
def test_action_radius_sizes_every_action(
    action: ActionType, sweep: Sweep, pack: PolicyPack
) -> None:
    topology = next(iter(sweep.topology.values())).context
    assert br.action_radius(topology, action=action, policy=pack.blast_radius).count > 0


def test_corroboration_can_raise_the_count_but_never_lower_it(
    sweep: Sweep, pack: PolicyPack
) -> None:
    """Three of a tap's eight peers complaining does not mean five are fine."""
    topology = next(iter(sweep.topology.values())).context
    kw = {"fault_domain": FaultDomain.TAP_OR_ODP, "policy": pack.blast_radius}
    base = br.impact_radius(topology, **kw)
    assert br.impact_radius(topology, corroborating_services=1, **kw).count == base.count
    assert br.impact_radius(topology, corroborating_services=300, **kw).count == 300


def test_corroboration_above_the_population_keeps_the_diagnosed_scope(
    sweep: Sweep, pack: PolicyPack
) -> None:
    """REGRESSION. The scope must not be promoted to fit the observation.

    Thirty services down behind a tap the pack sizes at eight means the diagnosis is too small for
    what is being seen. Quietly widening `scope` to `node_or_port` would make the record
    self-consistent and destroy the evidence of the disagreement -- and `scope` is the field a
    reviewer reads to notice it. The count moves, the scope does not, and a note says so.
    """
    topology = next(iter(sweep.topology.values())).context
    kw = {"fault_domain": FaultDomain.TAP_OR_ODP, "policy": pack.blast_radius}
    base = br.impact_radius(topology, **kw)
    stretched = br.impact_radius(topology, corroborating_services=300, **kw)
    assert stretched.scope is base.scope, stretched
    assert stretched.notes, "a count that outgrew its scope must say so"


def test_a_blast_radius_cannot_be_built_without_a_basis() -> None:
    with pytest.raises(ValueError, match="basis"):
        br.BlastRadius(
            count=8, measured=False, basis="   ", scope=br.BlastRadiusScope.DELIMITER
        )


def test_scope_ranking_is_the_outward_nesting_not_alphabetical() -> None:
    """`StrEnum` comparison would put `delimiter` before `distribution` before `headend`."""
    ordered = [
        br.BlastRadiusScope.SINGLE_PREMISES,
        br.BlastRadiusScope.DELIMITER,
        br.BlastRadiusScope.DISTRIBUTION,
        br.BlastRadiusScope.NODE_OR_PORT,
        br.BlastRadiusScope.HEADEND_OR_OLT,
    ]
    assert [s.rank() for s in ordered] == sorted(s.rank() for s in ordered)


# -- SLA -------------------------------------------------------------------------------------


def test_the_resolved_target_is_never_looser_than_the_contract(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    adapters = build_simulated_adapters(fixtures=fixtures)

    async def check() -> None:
        for severity in Severity:
            for ref in fixtures.services:
                payload = await adapters.tmf.fetch_sla(ref)
                resolved = sla.resolve_sla(
                    payload, severity=severity, clock_started_at=now, policy=pack.sla
                )
                contract = timedelta(hours=float(payload["restore_target_hours"]))
                assert resolved.context.restore_target <= contract, (ref, severity)

    asyncio.run(check())


def test_both_sla_branches_are_reachable(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """Falsification. A `_tighter` that always returned the pack passes every other SLA test here.

    The fixture set holds residential contracts looser than the pack at high severity and business
    contracts tighter than it at low severity, so both branches must appear across the sweep. If
    only one does, the function is not choosing.
    """
    adapters = build_simulated_adapters(fixtures=fixtures)
    seen: set[str] = set()

    async def collect() -> None:
        for severity in Severity:
            for ref in fixtures.services:
                resolved = sla.resolve_sla(
                    await adapters.tmf.fetch_sla(ref),
                    severity=severity,
                    clock_started_at=now,
                    policy=pack.sla,
                )
                seen.add(resolved.response_bound_by)
                seen.add(resolved.restore_bound_by)

    asyncio.run(collect())
    assert {"contract", "policy"} <= seen, seen


def test_a_failed_sla_read_still_produces_a_clock(pack: PolicyPack, now: datetime) -> None:
    """An incident with no clock is an incident that can never be late.

    Leaving the target unknown would let a CRM outage silently suspend the SLA on every incident
    opened during it. The pack's severity target stands in, and the flag says the read failed.
    """
    resolved = sla.resolve_sla(None, severity=Severity.HIGH, clock_started_at=now, policy=pack.sla)
    assert resolved.context.restore_target > timedelta(0)
    assert resolved.flags, "a substituted target must be flagged"


def test_the_clock_is_not_breached_or_at_risk_the_moment_it_starts(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    adapters = build_simulated_adapters(fixtures=fixtures)

    async def check() -> None:
        ref = next(iter(fixtures.services))
        for severity in Severity:
            resolved = sla.resolve_sla(
                await adapters.tmf.fetch_sla(ref),
                severity=severity,
                clock_started_at=now,
                policy=pack.sla,
            )
            status = sla.sla_status(resolved.context, now=now, escalation=pack.escalation)
            assert not status.restore_breached, severity
            assert not status.at_risk, severity

    asyncio.run(check())


def test_at_risk_fires_at_the_warning_fraction_of_the_budget(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """`sla_breach_warning_fraction` is budget *consumed*, so `at_risk` needs its complement.

    Handing the fraction straight to `SLAContext.at_risk`, which asks how much time is *left*,
    inverts the warning: a 0.75 pack setting would fire the alert in the first quarter of the
    incident and go quiet for the three quarters where it matters.
    """
    adapters = build_simulated_adapters(fixtures=fixtures)

    async def check() -> None:
        resolved = sla.resolve_sla(
            await adapters.tmf.fetch_sla(next(iter(fixtures.services))),
            severity=Severity.HIGH,
            clock_started_at=now,
            policy=pack.sla,
        )
        budget = resolved.context.restore_target
        fraction = pack.escalation.sla_breach_warning_fraction
        early = sla.sla_status(
            resolved.context, now=now + budget * (fraction / 2), escalation=pack.escalation
        )
        late = sla.sla_status(
            resolved.context,
            now=now + budget * fraction + timedelta(seconds=1),
            escalation=pack.escalation,
        )
        assert not early.at_risk, "warned in the quiet part of the budget"
        assert late.at_risk, "did not warn approaching the deadline"

    asyncio.run(check())


def test_consumed_fraction_is_not_clamped_at_one(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """A three-hour overrun and a one-minute overrun must not both read as 100%."""
    adapters = build_simulated_adapters(fixtures=fixtures)

    async def check() -> None:
        resolved = sla.resolve_sla(
            await adapters.tmf.fetch_sla(next(iter(fixtures.services))),
            severity=Severity.HIGH,
            clock_started_at=now,
            policy=pack.sla,
        )
        overrun = sla.sla_status(
            resolved.context,
            now=now + resolved.context.restore_target * 2,
            escalation=pack.escalation,
        )
        assert overrun.restore_breached
        assert overrun.fraction_consumed > 1.0, overrun.fraction_consumed

    asyncio.run(check())


# -- RCA -------------------------------------------------------------------------------------


def test_rca_produces_ranked_hypotheses_for_every_service_with_findings(
    sweep: Sweep, pack: PolicyPack, now: datetime
) -> None:
    refs = sweep.with_findings()
    assert refs, "no fixture service produced a finding"
    for ref in refs:
        result = rca.conclude(
            sweep.findings[ref],
            concluded_at=now,
            fault_domain=sweep.domain[ref],
            rca_policy=pack.rca,
            evidence=pack.evidence,
            technology=Technology(sweep.services[ref]["technology"]),
            delimiter_ref=sweep.services[ref]["delimiter_ref"],
        )
        assert result.hypotheses, ref
        assert 0.0 <= result.confidence <= 1.0, (ref, result.confidence)
        posteriors = [h.posterior for h in result.hypotheses]
        assert posteriors == sorted(posteriors, reverse=True), ref


def test_no_hypothesis_is_ever_certain(sweep: Sweep, pack: PolicyPack, now: datetime) -> None:
    """A posterior of 1.0 asserts no other explanation is possible, which findings cannot show.

    There is always the fault nobody has a detector for, and a confidence of exactly 1.0 reads to
    an operator as a measurement rather than as an inference from the evidence to hand.
    """
    for ref in sweep.with_findings():
        result = rca.conclude(
            sweep.findings[ref],
            concluded_at=now,
            fault_domain=sweep.domain[ref],
            rca_policy=pack.rca,
            evidence=pack.evidence,
        )
        for hypothesis in result.hypotheses:
            assert hypothesis.posterior <= 0.95, (ref, hypothesis)


def test_a_lone_weak_finding_does_not_become_a_confident_diagnosis(
    pack: PolicyPack, now: datetime
) -> None:
    """Share alone would make this 100%: it is the only suspicion on the board.

    Corroboration is the second factor precisely so that one detector scoring 0.3 at confidence 0.4,
    carrying a single evidence reference, cannot be reported as a conclusive diagnosis.
    """
    lone = _finding(
        now, 0.3, Severity.LOW, confidence=0.4, suspected_domain=FaultDomain.CPE,
        evidence_refs=("ev-1",),
    )
    hypotheses = rca.build_hypotheses([lone], evidence=pack.evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].posterior < 1.0
    assert hypotheses[0].posterior <= 1.0 / pack.evidence.min_sources_for_diagnosis


def test_evidence_for_another_domain_is_recorded_as_contradicting(
    pack: PolicyPack, now: datetime
) -> None:
    """What makes the contradicting list real rather than decorative."""
    findings = [
        _finding(now, 0.8, Severity.HIGH, suspected_domain=FaultDomain.CPE,
                 evidence_refs=("ev-cpe",)),
        _finding(now, 0.6, Severity.MEDIUM, suspected_domain=FaultDomain.TAP_OR_ODP,
                 evidence_refs=("ev-tap",)),
    ]
    by_domain = {h.fault_domain: h for h in rca.build_hypotheses(findings, evidence=pack.evidence)}
    assert by_domain[FaultDomain.CPE].contradicting_evidence_refs == ("ev-tap",)
    assert by_domain[FaultDomain.TAP_OR_ODP].contradicting_evidence_refs == ("ev-cpe",)


def test_counting_detectors_instead_of_sources_says_so_in_the_statement(
    pack: PolicyPack, now: datetime
) -> None:
    """Two detectors reading one payload are not two sources, so the fallback must be visible."""
    findings = [
        _finding(now, 0.8, Severity.HIGH, detector_name="a", suspected_domain=FaultDomain.CPE),
        _finding(now, 0.7, Severity.HIGH, detector_name="b", suspected_domain=FaultDomain.CPE),
    ]
    [hypothesis] = rca.build_hypotheses(findings, evidence=pack.evidence)
    assert "detectors" in hypothesis.statement
    assert "overstates their independence" in hypothesis.statement


def test_a_rejected_domain_stays_in_the_set_with_its_reason(
    pack: PolicyPack, now: datetime
) -> None:
    """`RCAResult.ruled_out` is how a reviewer tells "considered and rejected" from "never
    considered". Dropping the hypothesis would make those two look identical."""
    findings = [
        _finding(now, 0.8, Severity.HIGH, suspected_domain=FaultDomain.CPE),
        _finding(now, 0.6, Severity.MEDIUM, suspected_domain=FaultDomain.DROP),
    ]
    hypotheses = rca.build_hypotheses(
        findings,
        evidence=pack.evidence,
        rejected={FaultDomain.DROP: "the drop was replaced last week"},
    )
    dropped = next(h for h in hypotheses if h.fault_domain is FaultDomain.DROP)
    assert dropped.rejected
    assert dropped.rejection_reason == "the drop was replaced last week"


def test_findings_with_no_domain_produce_no_hypotheses(pack: PolicyPack, now: datetime) -> None:
    assert rca.build_hypotheses([_finding(now, 0.8, Severity.HIGH)], evidence=pack.evidence) == []


# -- impact ----------------------------------------------------------------------------------


def test_impact_counts_every_service_and_states_when_the_count_is_estimated(
    sweep: Sweep, pack: PolicyPack, now: datetime
) -> None:
    for ref in sweep.with_findings():
        assessment = impact.assess_impact(
            assessed_at=now,
            subject=impact.AffectedService(
                service_ref=ref, delimiter_ref=sweep.services[ref]["delimiter_ref"]
            ),
            fault_domain=sweep.domain[ref],
            topology=sweep.topology[ref].context,
            policy=pack.blast_radius,
            findings=sweep.findings[ref],
        )
        assert assessment.affected_customer_count >= 1, ref
        if assessment.count_is_estimated:
            assert assessment.estimation_basis.strip(), ref


def test_scale_raises_severity_above_what_one_modem_scored(
    sweep: Sweep, pack: PolicyPack, now: datetime
) -> None:
    """Detector severity alone leaves a node outage at whatever one household's symptoms scored."""
    mild = [_finding(now, 0.3, Severity.LOW, suspected_domain=FaultDomain.NODE_OR_OLT)]
    small = impact.incident_severity(mild, affected_count=1, policy=pack.blast_radius)
    large = impact.incident_severity(
        mild, affected_count=pack.blast_radius.network_action_threshold, policy=pack.blast_radius
    )
    assert large != small
    assert large in {Severity.HIGH, Severity.CRITICAL}, large


def test_a_single_premises_fibre_cut_is_critical_at_a_count_of_one(
    pack: PolicyPack, now: datetime
) -> None:
    """The other half of the same rule: a scale-only severity would file this as low."""
    severe = [_finding(now, 0.95, Severity.CRITICAL, suspected_domain=FaultDomain.DROP)]
    assert (
        impact.incident_severity(severe, affected_count=1, policy=pack.blast_radius)
        is Severity.CRITICAL
    )


# -- resolution ------------------------------------------------------------------------------


def test_every_fault_domain_yields_a_plan_or_says_why_not(pack: PolicyPack, now: datetime) -> None:
    """An empty plan is correct for three domains and a silent failure for the other twelve.

    Where nothing is catalogued the plan must still carry the note explaining that and the
    escalation path that replaces the action, because an empty `options` list with nothing beside it
    is indistinguishable from a planner that crashed.
    """
    for domain in FaultDomain:
        plan = resolution.plan_resolution(
            plan_id="PLAN-1",
            created_at=now,
            fault_domain=domain,
            target_ref="SVC-TEST",
            allowlist=pack.remote_actions,
            blast_radius_policy=pack.blast_radius,
        )
        if domain in NO_REMOTE_ACTION:
            assert not plan.options, domain
            assert plan.notes, domain
            assert plan.escalation_path, domain
        else:
            assert plan.options, domain


def test_a_plan_never_offers_an_action_the_pack_forbids(pack: PolicyPack, now: datetime) -> None:
    """`bulk_config_push` is `allowed: false` in the pack and must never reach a plan."""
    for domain in FaultDomain:
        plan = resolution.plan_resolution(
            plan_id="PLAN-1",
            created_at=now,
            fault_domain=domain,
            target_ref="SVC-TEST",
            allowlist=pack.remote_actions,
            blast_radius_policy=pack.blast_radius,
        )
        for option in plan.options:
            rule = pack.remote_actions.get(option.action_type)
            assert rule is not None and rule.allowed, (domain, option.action_type)


def test_an_already_attempted_option_is_not_offered_again(
    pack: PolicyPack, now: datetime
) -> None:
    first = resolution.plan_resolution(
        plan_id="PLAN-1",
        created_at=now,
        fault_domain=FaultDomain.CPE,
        target_ref="SVC-TEST",
        allowlist=pack.remote_actions,
        blast_radius_policy=pack.blast_radius,
    )
    attempted = first.options[0].option_id
    again = resolution.plan_resolution(
        plan_id="PLAN-2",
        created_at=now,
        fault_domain=FaultDomain.CPE,
        target_ref="SVC-TEST",
        allowlist=pack.remote_actions,
        blast_radius_policy=pack.blast_radius,
        attempted_option_ids=(attempted,),
    )
    assert attempted not in {o.option_id for o in again.options}


# -- forecast --------------------------------------------------------------------------------


def test_forecast_delegates_the_score_and_band_to_the_detector(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """The one rule this module is under: no second implementation of the Wi-Fi health score.

    `PredictionResult` declares 0-100 and the verdict is 0-1, so the only thing done here is the
    conversion. A recomputation would be discovered by a customer told two different things about
    the same week, not by a test -- unless this one holds the two together.
    """
    for name, profile in fixtures.wifi_profiles.items():
        verdict = wifi_health_verdict(profile)
        prediction = forecast.forecast_wifi(
            profile, predicted_at=now, subject_ref="SVC-TEST", bands=pack.health_bands
        )
        if verdict is None:
            assert prediction is None, name
            continue
        assert prediction is not None, name
        assert prediction.band is verdict.band, name
        assert prediction.wifi_health_score == pytest.approx(verdict.score * 100.0, abs=0.011), name


def test_unread_radios_produce_no_prediction_rather_than_a_zero_score(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """A predictive sweep that turned unreadable CPEs into critical results would fill the dispatch
    queue with houses whose only fault is that the ACS did not answer."""
    assert (
        forecast.forecast_wifi(
            fixtures.wifi_profiles["no_radio_data"],
            predicted_at=now,
            subject_ref="SVC-TEST",
            bands=pack.health_bands,
        )
        is None
    )


def test_forecast_recommends_at_least_one_action_across_the_profiles(fixtures: Any) -> None:
    """REGRESSION. `_ACTION_FOR_BREACH` was keyed on prose and matched nothing.

    The breach reads "airtime utilisation 82%" and the key was `"utilization"` -- an American
    spelling the detector never emits. Every substring lookup missed, so `recommended_actions`
    returned an empty tuple for every verdict ever produced, while its docstring described the
    breach-to-action link as the thing it added over the band. The map is now keyed on
    `WifiVerdict.breached_metrics`, which is a named vocabulary the detector owns.

    Asserting a non-empty result *somewhere* is the part that fails against the old code. Asserting
    per-profile would not: `clean` correctly recommends nothing.
    """
    recommended: set[ActionType] = set()
    for profile in fixtures.wifi_profiles.values():
        verdict = wifi_health_verdict(profile)
        if verdict is not None:
            recommended |= set(forecast.recommended_actions(verdict))
    assert recommended, "no breach in any fixture profile mapped to an action"


def test_every_key_in_the_action_map_is_a_metric_the_detector_emits(fixtures: Any) -> None:
    """The check that makes the regression above impossible to reintroduce silently.

    A key naming no real metric is dead weight that looks like coverage, which is exactly how the
    original defect survived review.
    """
    emitted: set[str] = set()
    for profile in fixtures.wifi_profiles.values():
        verdict = wifi_health_verdict(profile)
        if verdict is not None:
            emitted |= set(verdict.features)
    unknown = set(forecast._ACTION_FOR_BREACH) - emitted
    assert not unknown, f"these keys name no metric the detector produces: {sorted(unknown)}"


def test_breached_metrics_are_always_features(fixtures: Any) -> None:
    """A metric can be read and be fine, but it cannot breach without having been read."""
    for name, profile in fixtures.wifi_profiles.items():
        verdict = wifi_health_verdict(profile)
        if verdict is not None:
            assert set(verdict.breached_metrics) <= set(verdict.features), name


def test_a_healthy_verdict_recommends_nothing(fixtures: Any) -> None:
    """The other half of the falsification: the map must not fire on everything."""
    verdict = wifi_health_verdict(fixtures.wifi_profiles["clean"])
    assert verdict is not None and verdict.band is HealthBand.HEALTHY
    assert forecast.recommended_actions(verdict) == ()


def test_failure_probability_is_damped_below_certainty(fixtures: Any) -> None:
    """Terrible Wi-Fi now is a weak statement about the next seven days.

    Reporting the complement of the health score directly would put a congested 2.4 GHz radio above
    a confirmed optical degradation in any queue that sorted on this field.
    """
    for profile in fixtures.wifi_profiles.values():
        verdict = wifi_health_verdict(profile)
        if verdict is not None:
            assert 0.0 <= forecast.failure_probability(verdict) <= 0.5


def test_confidence_falls_when_less_of_the_radio_was_readable(
    fixtures: Any, pack: PolicyPack, now: datetime
) -> None:
    """A score from a quarter of the picture is reported on the same scale as a complete one, which
    is precisely what `confidence` exists to distinguish."""
    full = forecast.forecast_wifi(
        fixtures.wifi_profiles["congested_2g"],
        predicted_at=now,
        subject_ref="SVC-TEST",
        bands=pack.health_bands,
    )
    partial = forecast.forecast_wifi(
        {"utilization_2g_pct": 95.0},
        predicted_at=now,
        subject_ref="SVC-TEST",
        bands=pack.health_bands,
    )
    assert full is not None and partial is not None
    assert partial.confidence < full.confidence


def test_event_and_dispatch_thresholds_are_separate_lines(pack: PolicyPack) -> None:
    """Everything crossing the event line gets a case; only what crosses this one gets a van."""
    raised = [b for b in HealthBand if forecast.should_raise_event(b, pack.health_bands)]
    dispatched = [b for b in HealthBand if forecast.should_dispatch(b, pack.health_bands)]
    assert dispatched, "no band is ever worth a visit"
    assert set(dispatched) < set(raised), (dispatched, raised)


# -- restoration -----------------------------------------------------------------------------


def test_anomaly_reduction_uses_the_peak_not_the_mean(now: datetime) -> None:
    """A fix that cleared four minor findings and left the severe one has not restored service.

    The numbers are chosen so peak and mean give *different, unclamped* answers. An earlier version
    of this test used a case where both produced `0.0`, which is the floor `max(0.0, ...)` imposes
    -- so it passed against a mean, against a peak, and against several other wrong things besides.
    Here the peak reports 50% cleared while any mean-based reading falls to the floor, so only one
    implementation returns 0.5.
    """
    before = [
        _finding(now, 0.9, Severity.HIGH),
        *[_finding(now, 0.1, Severity.LOW) for _ in range(3)],
    ]
    assert restoration.anomaly_reduction(before, [_finding(now, 0.45, Severity.MEDIUM)]) == 0.5

    # And the narrative case: the severe finding is untouched, so nothing has been restored.
    assert restoration.anomaly_reduction(before, [_finding(now, 0.9, Severity.HIGH)]) == 0.0


def test_no_anomaly_before_the_fix_is_not_a_reduction_of_zero(now: datetime) -> None:
    """`0.0` would read as "the anomaly is entirely still present" for an incident that never
    scored one, and would block every closure whose detectors found nothing."""
    assert restoration.anomaly_reduction([], [_finding(now, 0.1, Severity.LOW)]) is None


def test_an_unrecorded_fix_takes_the_plant_window(pack: PolicyPack) -> None:
    """REGRESSION. `None` must not fall through to the shorter window.

    An unrecorded action is not evidence of a small action, and the two errors are not
    symmetrical: a premature all-clear on a plant repair is multiplied by everyone behind the
    element, while an unnecessarily long window costs one incident some waiting.
    """
    cpe = restoration.stability_window(ActionType.CPE_REBOOT, pack.validation)
    plant = restoration.stability_window(ActionType.NODE_LEVEL_RESET, pack.validation)
    assert plant > cpe, (cpe, plant)
    assert restoration.stability_window(None, pack.validation) == plant


def test_kpi_direction_is_never_guessed(now: datetime) -> None:
    """`rx_power_dbm` is bad in both directions because an overloaded receiver saturates.

    A metric with no stated direction lands in `undirected` and counts neither way. Guessing would
    not merely mislabel it: `ValidationResult._pass_requires_a_window` refuses a pass with regressed
    metrics, so a misclassified regression lets a closure through.
    """
    improved, regressed, undirected = restoration.compare_kpis(
        {"snr_db": 25.0, "rx_power_dbm": -20.0},
        {"snr_db": 34.0, "rx_power_dbm": -12.0},
        {"snr_db": True},
    )
    assert improved == ("snr_db",)
    assert regressed == ()
    assert undirected == ("rx_power_dbm",)


def test_validation_passes_only_when_every_criterion_is_met(
    pack: PolicyPack, now: datetime
) -> None:
    window = restoration.stability_window(ActionType.CPE_REBOOT, pack.validation)
    result = restoration.validate_restoration(
        validation_id="VAL-1",
        incident_id="INC-1",
        validated_at=now + window + timedelta(minutes=1),
        window_start=now,
        fault_domain=FaultDomain.TAP_OR_ODP,
        policy=pack.validation,
        action_taken=ActionType.CPE_REBOOT,
        samples_in_window=pack.validation.min_post_fix_samples,
        findings_before=[_finding(now, 0.9, Severity.HIGH)],
        findings_after=[_finding(now, 0.1, Severity.LOW)],
        kpi_before={"snr_db": 25.0},
        kpi_after={"snr_db": 34.0},
        higher_is_better={"snr_db": True},
    )
    assert result.passed
    assert result.reason_code is ReasonCode.VALIDATED_STABLE


def test_a_window_that_has_not_elapsed_is_pending_not_failed(
    pack: PolicyPack, now: datetime
) -> None:
    """Collapsing pending into failed sends every incident round again the moment it is fixed."""
    result = restoration.validate_restoration(
        validation_id="VAL-1",
        incident_id="INC-1",
        validated_at=now + timedelta(minutes=1),
        window_start=now,
        fault_domain=FaultDomain.TAP_OR_ODP,
        policy=pack.validation,
        action_taken=ActionType.CPE_REBOOT,
        samples_in_window=0,
        findings_before=[_finding(now, 0.9, Severity.HIGH)],
        findings_after=[_finding(now, 0.1, Severity.LOW)],
    )
    assert not result.passed
    assert result.reason_code is ReasonCode.STABILITY_WINDOW_PENDING


def test_a_regressed_metric_fails_rather_than_pends(pack: PolicyPack, now: datetime) -> None:
    """The other direction: a fix that made things worse must not sit in a window waiting."""
    window = restoration.stability_window(ActionType.CPE_REBOOT, pack.validation)
    result = restoration.validate_restoration(
        validation_id="VAL-1",
        incident_id="INC-1",
        validated_at=now + window + timedelta(minutes=1),
        window_start=now,
        fault_domain=FaultDomain.TAP_OR_ODP,
        policy=pack.validation,
        action_taken=ActionType.CPE_REBOOT,
        samples_in_window=pack.validation.min_post_fix_samples,
        findings_before=[_finding(now, 0.9, Severity.HIGH)],
        findings_after=[_finding(now, 0.1, Severity.LOW)],
        kpi_before={"snr_db": 34.0},
        kpi_after={"snr_db": 25.0},
        higher_is_better={"snr_db": True},
    )
    assert result.reason_code is ReasonCode.VALIDATION_FAILED


def test_a_coverage_complaint_waits_for_the_customer(pack: PolicyPack, now: datetime) -> None:
    """Perfect readings on every metric while the customer still cannot use the far bedroom.

    For the domains the pack names, telemetry cannot answer the question that was asked, so an
    unanswered customer is pending rather than passed.
    """
    domain = next(iter(pack.validation.require_customer_confirmation_for_domains))
    window = restoration.stability_window(None, pack.validation)
    kw: dict[str, Any] = {
        "validation_id": "VAL-1",
        "incident_id": "INC-1",
        "validated_at": now + window + timedelta(minutes=1),
        "window_start": now,
        "fault_domain": domain,
        "policy": pack.validation,
        "samples_in_window": pack.validation.min_post_fix_samples,
        "findings_before": [_finding(now, 0.9, Severity.HIGH)],
        "findings_after": [_finding(now, 0.1, Severity.LOW)],
    }
    assert (
        restoration.validate_restoration(customer_confirmed=None, **kw).reason_code
        is ReasonCode.STABILITY_WINDOW_PENDING
    )
    assert (
        restoration.validate_restoration(customer_confirmed=False, **kw).reason_code
        is ReasonCode.VALIDATION_FAILED
    )
    assert restoration.validate_restoration(customer_confirmed=True, **kw).passed
