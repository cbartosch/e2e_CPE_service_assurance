"""The thirteen detectors, verified by execution against the fixture-backed adapters.

The standard here is falsification, not coverage. For each detector the suite must show BOTH that it
fires on a profile that should trigger it AND that it stays clean on one that should not -- a
detector that can never fire is decoration, and one that can never stay clean is noise. Neither
shows up in a test that only asserts "no exception was raised", and neither shows up in a test that
checks the three or four profiles hand-picked to suit it, which is why the fire/clean question is
answered by a sweep over every fixture service rather than over a chosen few.

Four of the tests below exist because they caught a real defect during development, and each says
so at its definition:

* technology exclusivity reported as a data-quality defect, because `requires` was checked before
  the technology gate and the `not_applicable` return was therefore unreachable;
* the Wi-Fi detector scoring a TR-069 device read it could not parse, and a verdict that called
  Wi-Fi HEALTHY while listing the breaches that made it not;
* a non-accumulating classifier pass, in which the localiser found a degraded tap that the
  no-fault-found scorer could not see and therefore reported an 85% wasted-visit risk against;
* a weather advisory carrying a fault domain, so lightning over a healthy premises read as evidence
  that the problem was inside the home.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from lpr_cpe.detectors import (
    CPEWiFiAnomalyDetector,
    DelimiterLocaliser,
    DetectionContext,
    DetectorResult,
    FaultDomainClassifier,
    NoFaultFoundRiskScorer,
    all_detectors,
    normalise_wifi_snapshot,
    run_detectors,
    wifi_health_verdict,
)
from lpr_cpe.domain.enums import DataQualityFlag, FaultDomain, HealthBand, Severity, Technology
from lpr_cpe.simulation.loader import build_simulated_adapters

DETECTOR_NAMES = [d.name for d in all_detectors()]

#: Fed by incident state rather than by an adapter, so the telemetry sweep cannot exercise them.
#: They get their own section instead -- excluding them from the fire/clean sweep is a statement
#: about where their input comes from, not an exemption from having to fire.
HISTORY_FED = {"repeat_visit_risk", "handover_quality", "post_fix_stability"}

#: The one detector for which a clean result would be a defect. "No fault found" is one of its
#: answers, not an absence of one, and a silent classifier would leave the graph with no domain.
ALWAYS_CLASSIFIES = {"fault_domain_classifier"}

#: One fixture service per health profile. The `health` label on each is asserted in
#: `test_subjects_still_have_the_profiles_these_tests_name`, so a fixture edit that moves a service
#: to a different profile fails there rather than silently making every test below vacuous.
SUBJECTS = {
    "hfc_healthy": "SVC-SJ-011-A-04",
    "hfc_marginal": "SVC-PO-042-A-04",
    "hfc_degraded_upstream": "SVC-SJ-011-A-01",
    "pon_healthy": "SVC-UT-001-A-01",
    "pon_degraded_optical": "SVC-UT-001-A-03",
    "pon_power_affected": "SVC-VQ-002-A-01",
}
HFC_SUBJECTS = [r for p, r in SUBJECTS.items() if p.startswith("hfc_")]
PON_SUBJECTS = [r for p, r in SUBJECTS.items() if p.startswith("pon_")]

#: The quietest healthy HFC service. See `test_healthy_service_produces_no_false_fault`.
QUIET_HEALTHY_SERVICE = "SVC-SJ-011-B-02"

#: What `BaseDetector.detect` writes into `unavailable_reason` when `_detect` raised: the exception
#: type followed by its message. No deliberate return path produces that shape, so matching it is
#: how the poisoned-payload test tells a handled refusal from a swallowed crash.
_CRASH_REASON = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(Error|Exception|Warning):")


# -- the sweep -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sweep:
    """Every detector run against every fixture service, once, for the whole module."""

    #: detector name -> {"fired" | "clean" | "n/a" | "unavailable"} -> count.
    counts: dict[str, dict[str, int]]
    #: service_ref -> detector name -> result.
    by_service: dict[str, dict[str, DetectorResult]]
    #: Results that ran and found nothing, for building a "the detectors looked and were clean"
    #: prior. Collected from the sweep rather than hand-picked: a hand-picked detector list off a
    #: subject whose Wi-Fi happened to be congested would make the classifier tests assert the
    #: opposite of what they claim to.
    clean: list[DetectorResult] = field(default_factory=list)

    def results_for(self, profile: str) -> dict[str, DetectorResult]:
        return self.by_service[SUBJECTS[profile]]


async def _run_sweep(fixtures: Any, make_context: Any, state_of: Any) -> Sweep:
    # Its own adapters, not the `adapters` fixture: that one is function-scoped because the ten
    # simulators share a mutable `WriteGate`, and a session-scoped set would let one test's writes
    # turn up in another test's audit. The sweep only reads, so a private set costs nothing.
    adapters = build_simulated_adapters(fixtures=fixtures)
    now = datetime.now(UTC)
    counts = {n: {"fired": 0, "clean": 0, "n/a": 0, "unavailable": 0} for n in DETECTOR_NAMES}
    by_service: dict[str, dict[str, DetectorResult]] = {}
    clean: list[DetectorResult] = []
    for service_ref, service in fixtures.services.items():
        results = await run_detectors(await make_context(adapters, service, now=now))
        by_service[service_ref] = {r.detector_name: r for r in results}
        for result in results:
            counts[result.detector_name][state_of(result)] += 1
            if result.clean:
                clean.append(result)
    return Sweep(counts=counts, by_service=by_service, clean=clean)


@pytest.fixture(scope="module")
def sweep(fixtures: Any, make_context: Any, state_of: Any) -> Sweep:
    """All thirteen detectors over all forty-one fixture services.

    Module-scoped and synchronous. Thirteen detectors times forty-one services is 533 results, and
    a dozen tests below each want a different slice of them; recomputing that per test would be the
    same work over and over for an answer that cannot change between them.
    """
    return asyncio.run(_run_sweep(fixtures, make_context, state_of))


# -- the registry ----------------------------------------------------------------------------


def test_thirteen_detectors_registered_with_unique_names() -> None:
    assert len(DETECTOR_NAMES) == 13, DETECTOR_NAMES
    assert len(set(DETECTOR_NAMES)) == 13, "two detectors share a name"


def test_subjects_still_have_the_profiles_these_tests_name(fixtures: Any) -> None:
    for profile, service_ref in SUBJECTS.items():
        assert fixtures.services[service_ref]["health"] == profile
    assert fixtures.services[QUIET_HEALTHY_SERVICE]["health"] == "hfc_healthy"


def test_every_service_yields_exactly_thirteen_results(sweep: Sweep) -> None:
    for service_ref, results in sweep.by_service.items():
        assert sorted(results) == sorted(DETECTOR_NAMES), service_ref


# -- falsification: every detector must be able to fire, and to stay quiet --------------------


@pytest.mark.parametrize("name", [n for n in DETECTOR_NAMES if n not in HISTORY_FED])
def test_detector_fires_on_at_least_one_fixture_service(name: str, sweep: Sweep) -> None:
    """A detector that cannot fire anywhere in the fixture set is decoration."""
    assert sweep.counts[name]["fired"] > 0, sweep.counts[name]


@pytest.mark.parametrize(
    "name", [n for n in DETECTOR_NAMES if n not in HISTORY_FED | ALWAYS_CLASSIFIES]
)
def test_detector_stays_clean_on_at_least_one_fixture_service(name: str, sweep: Sweep) -> None:
    """A detector that fires on everything is noise, and says nothing when it fires."""
    assert sweep.counts[name]["clean"] > 0, sweep.counts[name]


def test_classifier_never_stays_silent(sweep: Sweep) -> None:
    assert sweep.counts["fault_domain_classifier"]["clean"] == 0


# -- technology exclusivity --------------------------------------------------------------------
#
# REGRESSION. `BaseDetector.detect` used to check `requires` before the technology gate, which made
# the `not_applicable` return unreachable for exactly the services it existed to exempt: a PON
# service has no `nxt` payload, so the HFC detector reported MISSING_FIELD -- a data-quality defect
# -- for a reading that does not exist on that plant and was never meant to. Every PON incident
# accumulated a phantom defect from the HFC detector and every HFC incident one from the PON
# detector, which drags the policy pack's evidence checks towards blocking on healthy services.


@pytest.mark.parametrize("service_ref", PON_SUBJECTS)
def test_hfc_detector_on_pon_is_not_applicable_not_a_defect(service_ref: str, sweep: Sweep) -> None:
    result = sweep.by_service[service_ref]["hfc_rf_pnm_degradation"]
    assert not result.ran
    assert result.data_quality_warnings == [], result.unavailable_reason


@pytest.mark.parametrize("service_ref", HFC_SUBJECTS)
def test_pon_detector_on_hfc_is_not_applicable_not_a_defect(service_ref: str, sweep: Sweep) -> None:
    result = sweep.by_service[service_ref]["pon_optical_degradation"]
    assert not result.ran
    assert result.data_quality_warnings == [], result.unavailable_reason


async def test_unknown_technology_does_not_exempt_the_physical_detectors(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    """A missing metadata label must not become a diagnosis.

    Skipping both physical detectors on an unlabelled service would leave zero physical evidence,
    which `NoFaultFoundRiskScorer` reads as an argument against dispatch -- so an inventory gap
    would quietly turn into "there is nothing at the premises to repair".
    """
    context = await make_context(
        adapters, fixtures.services[SUBJECTS["hfc_degraded_upstream"]], now=now
    )
    unlabelled = replace(context, technology=Technology.UNKNOWN)
    results = {r.detector_name: r for r in await run_detectors(unlabelled)}
    assert results["hfc_rf_pnm_degradation"].ran
    assert results["hfc_rf_pnm_degradation"].findings


# -- the physical detectors --------------------------------------------------------------------


def test_hfc_detector_fires_on_degraded_upstream_and_not_on_healthy(sweep: Sweep) -> None:
    assert sweep.results_for("hfc_degraded_upstream")["hfc_rf_pnm_degradation"].findings
    assert sweep.results_for("hfc_healthy")["hfc_rf_pnm_degradation"].clean


def test_pon_detector_fires_on_degraded_optical_and_not_on_healthy(sweep: Sweep) -> None:
    assert sweep.results_for("pon_degraded_optical")["pon_optical_degradation"].findings
    assert sweep.results_for("pon_healthy")["pon_optical_degradation"].clean


def test_power_loss_is_diagnosed_as_power_not_as_a_fibre_fault(sweep: Sweep) -> None:
    """Dying-gasp before the optical levels, or a dark ONT reads as a broken fibre.

    A powered-down ONT reports no light. Ranking the levels first would send a fibre crew to a house
    whose electricity is out, which is the single most expensive way to get this wrong.
    """
    results = sweep.results_for("pon_power_affected")

    optical = results["pon_optical_degradation"]
    assert optical.ran, optical.unavailable_reason
    assert [f.suspected_domain for f in optical.findings] == [FaultDomain.POWER]

    classified = results["fault_domain_classifier"]
    assert [f.suspected_domain for f in classified.findings] == [FaultDomain.POWER]

    risk = results["no_fault_found_risk"]
    assert risk.findings and risk.findings[0].score >= 0.9


# -- a healthy service ---------------------------------------------------------------------------


def test_healthy_service_produces_no_false_fault(sweep: Sweep) -> None:
    """The literal acceptance criterion -- "all thirteen clean" -- cannot hold, and should not.

    Three structural reasons, none of them a gap: the HFC and PON detectors are mutually exclusive
    so one is always not-applicable; the domain classifier always emits a classification because
    "no fault found" is one of its answers; and the handover and post-fix detectors are lifecycle
    checks with nothing to check on a fresh incident.

    What the criterion is *for* is that a healthy service produces no false fault, and that is
    checkable. Every finding on the quietest healthy service must be a statement that there is no
    fault rather than a fault, no detector may report a data-quality defect, and all thirteen must
    be accounted for -- so a detector cannot go missing behind the relaxation.
    """
    results = sweep.by_service[QUIET_HEALTHY_SERVICE]

    faults = [
        f"{name}: {finding.suspected_domain}/{finding.severity}"
        for name, result in results.items()
        for finding in result.findings
        if not (
            finding.suspected_domain is FaultDomain.NO_FAULT_FOUND
            or name == "no_fault_found_risk"
            or finding.severity is Severity.INFO
        )
    ]
    assert faults == []

    defects = [n for n, r in results.items() if not r.ran and r.data_quality_warnings]
    assert defects == []

    accounted = [
        n
        for n in DETECTOR_NAMES
        if results[n].clean
        or not results[n].ran
        or n in {"fault_domain_classifier", "no_fault_found_risk"}
    ]
    assert len(accounted) == 13, sorted(set(DETECTOR_NAMES) - set(accounted))


# -- the classifiers must not guess ---------------------------------------------------------------


async def test_classifier_with_no_prior_results_does_not_run(now: datetime) -> None:
    """ "Nobody looked" is not "nothing was found", and the difference decides a dispatch."""
    bare = DetectionContext(incident_id="INC-BARE", now=now, technology=Technology.HFC)

    classified = await FaultDomainClassifier().detect(bare)
    assert not classified.ran, classified.unavailable_reason
    assert not classified.findings
    assert DataQualityFlag.MISSING_FIELD in classified.data_quality_warnings

    risk = await NoFaultFoundRiskScorer().detect(bare)
    assert not risk.ran, risk.unavailable_reason


async def test_classifier_with_a_clean_prior_says_no_fault_found(
    now: datetime, sweep: Sweep
) -> None:
    assert len(sweep.clean) >= 4, "the sweep produced too few clean results to build a prior from"
    context = DetectionContext(
        incident_id="INC-CLEAN",
        now=now,
        technology=Technology.HFC,
        prior=sweep.clean[:4],
    )
    result = await FaultDomainClassifier().detect(context)
    assert result.ran, result.unavailable_reason
    assert [f.suspected_domain for f in result.findings] == [FaultDomain.NO_FAULT_FOUND]


async def test_classifier_with_an_all_unavailable_prior_does_not_claim_no_fault_found(
    now: datetime,
) -> None:
    """Thirteen dead adapters must not read as thirteen healthy readings."""
    context = DetectionContext(
        incident_id="INC-DEAD",
        now=now,
        technology=Technology.HFC,
        prior=[DetectorResult.unavailable("x", "1.0.0", "adapter down")],
    )
    result = await FaultDomainClassifier().detect(context)
    assert not result.ran, result.unavailable_reason
    assert not result.findings


# -- no detector may raise -------------------------------------------------------------------------


async def test_no_detector_raises_however_broken_the_payload(now: datetime) -> None:
    """One malformed field must not cost the other twelve detectors their run."""
    poison = DetectionContext(
        incident_id="INC-POISON",
        now=now,
        technology=Technology.HFC,
        nxt={"rf": "not-a-dict"},
        plant={"delimiter": {"services_in_service": "eight"}},
        cpe_raw={"online": "maybe"},
        wifi={"utilization_2g_pct": "high"},
        service_platform={"download_speed": {"sold_mbps": None}},
        recent_changes=[{"changed_at": "not-a-timestamp"}],
        power_outages=[{"open": True, "distance_km": "near"}],
        weather={},
        history={"previous_incidents": ["not-a-dict", 7], "post_fix_samples": [None]},
    )
    results = await run_detectors(poison)
    assert len(results) == 13
    crashed = [r.detector_name for r in results if _CRASH_REASON.match(r.unavailable_reason)]
    assert crashed == []


# -- the three history-fed risk detectors ---------------------------------------------------------


@pytest.fixture
def incident_history() -> dict[str, Any]:
    """One incident's state: two visits inside the window, a thin handover, a fix that slipped."""
    return {
        "previous_incidents": [
            {"closed_days_ago": 3, "dispatched": True, "closure_reason": "no_fault_found"},
            {"closed_days_ago": 11, "dispatched": True, "closure_reason": "no_fault_found"},
            # Outside the 30-day window. Counting it would make every long-lived service look like
            # a repeat-visit case.
            {"closed_days_ago": 400, "dispatched": True},
        ],
        "handover_package": {"fault_domain": "drop", "evidence_refs": ["E1"]},
        "post_fix_samples": [
            {"minutes_since_fix": 5, "healthy": True},
            {"minutes_since_fix": 20, "healthy": False},
            {"minutes_since_fix": 35, "healthy": True},
        ],
    }


@pytest.fixture
async def history_results(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any, incident_history: dict[str, Any]
) -> dict[str, DetectorResult]:
    context = await make_context(
        adapters,
        fixtures.services[SUBJECTS["hfc_healthy"]],
        now=now,
        history=incident_history,
    )
    return {r.detector_name: r for r in await run_detectors(context)}


def test_repeat_visit_counts_only_visits_inside_the_window(
    history_results: dict[str, DetectorResult],
) -> None:
    result = history_results["repeat_visit_risk"]
    assert result.findings, result.unavailable_reason
    assert result.findings[0].contributing_features["visits_in_window"] == 2.0


def test_handover_reports_exactly_the_fields_a_crew_would_have_to_ask_for(
    history_results: dict[str, DetectorResult],
) -> None:
    result = history_results["handover_quality"]
    assert result.findings, result.unavailable_reason
    assert result.findings[0].contributing_features["missing_fields"] == 3.0


def test_post_fix_fires_when_the_fix_slipped(history_results: dict[str, DetectorResult]) -> None:
    result = history_results["post_fix_stability"]
    assert result.findings, result.unavailable_reason
    assert result.findings[0].contributing_features["healthy_samples"] == 2.0


async def test_post_fix_is_clean_when_the_fix_held(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    context = await make_context(
        adapters,
        fixtures.services[SUBJECTS["hfc_healthy"]],
        now=now,
        history={
            "post_fix_samples": [
                {"minutes_since_fix": 10, "healthy": True},
                {"minutes_since_fix": 25, "healthy": True},
                {"minutes_since_fix": 40, "healthy": True},
            ]
        },
    )
    results = {r.detector_name: r for r in await run_detectors(context)}
    assert results["post_fix_stability"].clean


async def test_post_fix_reports_an_incomplete_window_rather_than_passing_it(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    """Not yet a failure, but not a pass either -- and closing on it produces the repeat visit."""
    context = await make_context(
        adapters,
        fixtures.services[SUBJECTS["hfc_healthy"]],
        now=now,
        history={"post_fix_samples": [{"minutes_since_fix": 4, "healthy": True}]},
    )
    results = {r.detector_name: r for r in await run_detectors(context)}
    result = results["post_fix_stability"]
    assert result.findings
    assert DataQualityFlag.LOW_SAMPLE_COUNT in result.data_quality_warnings


def test_lifecycle_detectors_are_not_applicable_on_a_fresh_incident(sweep: Sweep) -> None:
    """No handover to validate and no fix to hold is not a defect in the data."""
    for name in ("handover_quality", "post_fix_stability"):
        assert sweep.counts[name]["n/a"] == len(sweep.by_service), sweep.counts[name]
        assert sweep.counts[name]["unavailable"] == 0, sweep.counts[name]


# -- Wi-Fi: one owner of the score and the band ---------------------------------------------------
#
# REGRESSION. The detector was written against the fixture Wi-Fi *profile* shape while the CPE
# adapter returns a TR-069 `Device.WiFi.*` device read, so it scored a payload it could not parse
# and reported "radios returned no measurements" on every service. Separately, the band was graded
# off the score alone, which let a verdict call the Wi-Fi HEALTHY while listing the breaches that
# made it not -- and the band is the half the customer narrative reads.


def test_wifi_verdict_is_none_when_the_radios_reported_nothing() -> None:
    """`None`, not a zero score: zero would read as the worst possible Wi-Fi, not as no reading."""
    assert wifi_health_verdict({}) is None
    assert (
        wifi_health_verdict(
            {"utilization_2g_pct": None, "worst_rssi_dbm": None, "throughput_mbps": None}
        )
        is None
    )


def test_wifi_verdict_separates_the_fixture_profiles(fixtures: Any) -> None:
    clean = wifi_health_verdict(fixtures.wifi_profiles["clean"])
    congested = wifi_health_verdict(fixtures.wifi_profiles["congested_2g"])
    weak = wifi_health_verdict(fixtures.wifi_profiles["weak_coverage"])
    assert clean is not None and clean.healthy, clean
    assert congested is not None and not congested.healthy, congested
    assert weak is not None and not weak.healthy, weak


def test_wifi_verdict_is_deterministic(fixtures: Any) -> None:
    """A score that moves between identical inputs makes every threshold below it meaningless."""
    profile = fixtures.wifi_profiles["congested_2g"]
    assert wifi_health_verdict(profile) == wifi_health_verdict(profile)


@pytest.mark.parametrize("profile_name", ["clean", "congested_2g", "weak_coverage"])
def test_healthy_never_coexists_with_a_breach(fixtures: Any, profile_name: str) -> None:
    verdict = wifi_health_verdict(fixtures.wifi_profiles[profile_name])
    assert verdict is not None
    assert (verdict.band is HealthBand.HEALTHY) == (verdict.breaches == ())


async def test_wifi_verdict_reads_the_adapter_s_own_tr069_payload(
    adapters: Any, fixtures: Any
) -> None:
    """The detector must score what the CPE adapter actually returns, not a summary shape."""
    congested = next(
        service
        for service in fixtures.services.values()
        if fixtures.cpe_devices[service["cpe_ref"]]["wifi_profile"] == "congested_2g"
        and not fixtures.cpe_devices[service["cpe_ref"]]["offline"]
    )
    payload = await adapters.cpe.read_wifi_status(congested["cpe_ref"])
    assert "radios" in payload, "fixture no longer returns a TR-069 device read"

    flat = normalise_wifi_snapshot(payload)
    assert "utilization_2g_pct" in flat
    assert "worst_rssi_dbm" in flat

    verdict = wifi_health_verdict(payload)
    assert verdict is not None, "the TR-069 payload scored as no measurement at all"
    assert not verdict.healthy
    assert verdict.breaches


def test_normalise_passes_an_already_flat_payload_through_untouched() -> None:
    """A caller holding a summary must not have to fabricate a TR-069 envelope to be scored."""
    flat = {"utilization_2g_pct": 81.0, "worst_rssi_dbm": -60.0}
    assert normalise_wifi_snapshot(flat) == flat


def test_normalise_ignores_associated_but_inactive_clients() -> None:
    """A forgotten device parked in the garage is not the fault the customer is reporting."""
    payload = {
        "radios": [],
        "access_points": [
            {
                "Device.WiFi.AccessPoint.AssociatedDevice": [
                    {
                        "Device.WiFi.AccessPoint.AssociatedDevice.Active": True,
                        "Device.WiFi.AccessPoint.AssociatedDevice.SignalStrength": -55.0,
                    },
                    {
                        "Device.WiFi.AccessPoint.AssociatedDevice.Active": False,
                        "Device.WiFi.AccessPoint.AssociatedDevice.SignalStrength": -91.0,
                    },
                ]
            }
        ],
    }
    flat = normalise_wifi_snapshot(payload)
    assert flat["worst_rssi_dbm"] == -55.0
    assert flat["client_count"] == 1.0


async def test_wifi_anomaly_score_is_the_complement_of_the_health_score(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    """Publishing health as the anomaly score would invert every downstream comparison."""
    detector = CPEWiFiAnomalyDetector()
    checked = 0
    for service in fixtures.services.values():
        context = await make_context(adapters, service, now=now)
        result = await detector.detect(context)
        verdict = wifi_health_verdict(context.wifi or {}, context.thresholds)
        if not result.findings or verdict is None or verdict.healthy:
            continue
        assert result.findings[0].score == pytest.approx(1.0 - verdict.score, abs=1e-4)
        checked += 1
    assert checked > 0, "no service exercised the Wi-Fi scoring path"


# -- thresholds belong to the policy pack, not to the detector -------------------------------------


async def test_a_threshold_override_changes_the_verdict_on_a_healthy_service(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    """A detector holding its own literal would ignore the pack and this would stay clean."""
    context = await make_context(
        adapters,
        fixtures.services[SUBJECTS["hfc_healthy"]],
        now=now,
        thresholds={"hfc.upstream_power_max_dbmv": 10.0},
    )
    results = {r.detector_name: r for r in await run_detectors(context)}
    assert results["hfc_rf_pnm_degradation"].findings


# -- the classifying pass accumulates ---------------------------------------------------------------
#
# REGRESSION. All six classifiers were handed the same telemetry-only `prior`, which made their
# declared order decorative and produced a specific wrong answer: `DelimiterLocaliser` would find a
# degraded tap and `NoFaultFoundRiskScorer`, unable to see it, would report no physical evidence and
# an 85% chance of a wasted visit -- for the same incident, in the same pass.


#: What `NoFaultFoundRiskScorer` counts as something a crew could physically repair, and what it
#: counts as pointing at the home instead. Restated here rather than imported, deliberately: these
#: are the test's own statement of the two categories, so moving a domain between them in the
#: scorer has to be a decision taken twice rather than a rename that silently agrees with itself.
PHYSICAL_DOMAINS = {
    FaultDomain.DROP,
    FaultDomain.TAP_OR_ODP,
    FaultDomain.DISTRIBUTION,
    FaultDomain.FEEDER,
    FaultDomain.NODE_OR_OLT,
    FaultDomain.HEADEND_OR_CO,
}
SOFT_DOMAINS = {
    FaultDomain.CUSTOMER_ENVIRONMENT,
    FaultDomain.INSIDE_HOME_WIRING,
    FaultDomain.CPE,
    FaultDomain.SERVICE_PLATFORM,
    FaultDomain.PROVISIONING,
}

#: Everything `all_detectors()` orders ahead of the risk scorer. The order *is* the contract -- see
#: the `run_detectors` docstring -- so reading it back off the registry is the point rather than a
#: shortcut.
BEFORE_RISK_SCORER = set(DETECTOR_NAMES[: DETECTOR_NAMES.index("no_fault_found_risk")])


def _evidence_weight(results: dict[str, DetectorResult], domains: set[FaultDomain]) -> float:
    """What the risk scorer should have counted, recomputed from the results it was handed.

    Mirrors `DetectionContext.findings_from`: results that ran, minus the derived summaries, since
    a classifier's finding restates the findings it read rather than adding evidence of its own.
    """
    return sum(
        f.score * f.confidence
        for name, result in results.items()
        if name in BEFORE_RISK_SCORER and result.ran and not result.derived
        for f in result.findings
        if f.suspected_domain in domains
    )


@pytest.mark.parametrize(
    ("feature", "domains"),
    [("physical_evidence", PHYSICAL_DOMAINS), ("soft_evidence", SOFT_DOMAINS)],
)
def test_the_risk_scorer_counts_every_finding_made_before_it(
    sweep: Sweep, feature: str, domains: set[FaultDomain]
) -> None:
    """An exact sum, over every service, not a "greater than zero" on one of them.

    The first version of this test asserted only that `physical_evidence > 0` on the first service
    with a localised delimiter fault -- and that service already carried 1.54 of physical evidence
    from the telemetry detectors, so it passed with the non-accumulating pass reinstated. The
    localiser's contribution is only visible where it is the *sole* source, so the invariant has to
    be the total rather than its sign.
    """
    compared = 0
    for service_ref, results in sweep.by_service.items():
        risk = results["no_fault_found_risk"]
        if not risk.findings:
            continue
        reported = risk.findings[0].contributing_features[feature]
        assert reported == pytest.approx(_evidence_weight(results, domains), abs=1e-4), service_ref
        compared += 1
    assert compared > 0, "the risk scorer reported on no service, so nothing was compared"


def test_the_localiser_alone_is_enough_physical_evidence(sweep: Sweep) -> None:
    """REGRESSION, and the case the exact-sum test above exists to reach.

    On these services no telemetry detector named a physical domain -- the only evidence that there
    is something at the premises to repair comes from `DelimiterLocaliser`, which runs in the
    classifying pass. Handing the scorer a telemetry-only `prior` made it report "no physical-plant
    evidence was found at all" and an 85% chance of a wasted visit, for an incident in which a
    degraded tap had already been localised in the same pass.
    """
    telemetry_only = BEFORE_RISK_SCORER - {"delimiter_localiser", "fault_domain_classifier"}
    discriminating = []
    for service_ref, results in sweep.by_service.items():
        from_telemetry = sum(
            f.score * f.confidence
            for name, result in results.items()
            if name in telemetry_only and result.ran and not result.derived
            for f in result.findings
            if f.suspected_domain in PHYSICAL_DOMAINS
        )
        localiser = results["delimiter_localiser"]
        from_localiser = sum(
            f.score * f.confidence
            for f in localiser.findings
            if f.suspected_domain in PHYSICAL_DOMAINS
        )
        if from_telemetry == 0.0 and from_localiser > 0.0:
            discriminating.append((service_ref, results))

    assert discriminating, "no service isolates the localiser as the only physical evidence"
    for service_ref, results in discriminating:
        risk = results["no_fault_found_risk"]
        if not risk.findings:
            continue
        finding = risk.findings[0]
        assert finding.contributing_features["physical_evidence"] > 0.0, service_ref
        assert "no physical-plant evidence was found at all" not in finding.explanation, service_ref


@pytest.fixture
def localised_fault(sweep: Sweep) -> tuple[str, dict[str, DetectorResult]]:
    """A service the localiser pinned to a physical delimiter, and its full result set."""
    for service_ref, results in sweep.by_service.items():
        localiser = results["delimiter_localiser"]
        if localiser.findings and localiser.findings[0].suspected_domain in {
            FaultDomain.TAP_OR_ODP,
            FaultDomain.DROP,
        }:
            return service_ref, results
    pytest.fail("no fixture service produced a localised physical delimiter fault")


def test_derived_results_are_marked_and_excluded_from_the_evidence_count(
    now: datetime, localised_fault: tuple[str, dict[str, DetectorResult]]
) -> None:
    """A classifier's finding *is* the telemetry findings folded.

    Counting both hands whichever domain the classifier picked a second vote, cast by the count of
    the first -- so accumulation, which is what makes the summaries visible at all, has to come with
    the flag that stops them being weighed twice.
    """
    _service_ref, results = localised_fault
    assert results["fault_domain_classifier"].derived
    assert not results["delimiter_localiser"].derived, "the localiser is evidence, not a summary"
    assert not results["cpe_wifi_anomaly"].derived

    probe = DetectionContext(
        incident_id="INC-DERIVED",
        now=now,
        technology=Technology.HFC,
        prior=list(results.values()),
    )
    assert len(probe.findings_from()) < len(probe.findings_from(include_derived=True))


# -- weather constrains dispatch; it is not a place a fault can be -----------------------------------
#
# REGRESSION. The weather advisory carried `FaultDomain.CUSTOMER_ENVIRONMENT`, which made it count
# as soft evidence in the no-fault-found scorer: a lightning advisory over a healthy premises read
# as "the problem is inside the home" and argued against dispatching to a plant fault the localiser
# had already found. Dropping the domain took the count of services scored as no-fault-found from
# 32 to 25.


@pytest.fixture
def weather_advisory(sweep: Sweep) -> DetectorResult:
    for results in sweep.by_service.values():
        correlation = results["power_weather_correlation"]
        if any("unsafe for field work" in f.explanation for f in correlation.findings):
            return correlation
    pytest.fail("no fixture service sits under a weather advisory")


def test_the_weather_finding_names_no_fault_domain(weather_advisory: DetectorResult) -> None:
    advisories = [f for f in weather_advisory.findings if "unsafe for field work" in f.explanation]
    assert advisories, "the safety constraint must still reach the dispatch layer"
    assert all(f.suspected_domain is None for f in advisories)


def test_a_power_outage_unlike_the_weather_does_name_a_domain(
    weather_advisory: DetectorResult,
) -> None:
    """The distinction has to survive, not be flattened in either direction."""
    domained = [f for f in weather_advisory.findings if f.suspected_domain is not None]
    assert all(f.suspected_domain is FaultDomain.POWER for f in domained)


def test_power_outages_are_still_diagnosed_as_power(sweep: Sweep) -> None:
    domains = {
        f.suspected_domain
        for results in sweep.by_service.values()
        for f in results["power_weather_correlation"].findings
        if f.suspected_domain is not None
    }
    assert domains == {FaultDomain.POWER}


# -- determinism -------------------------------------------------------------------------------------


async def test_the_same_snapshot_produces_the_same_scores(
    adapters: Any, fixtures: Any, now: datetime, make_context: Any
) -> None:
    """A score that moves between identical inputs makes every threshold in the pack moot."""
    context = await make_context(
        adapters, fixtures.services[SUBJECTS["hfc_degraded_upstream"]], now=now
    )

    def signature(results: list[DetectorResult]) -> list[Any]:
        return [
            (r.detector_name, r.ran, [(f.score, f.confidence) for f in r.findings]) for r in results
        ]

    assert signature(await run_detectors(context)) == signature(await run_detectors(context))


# -- the parent-escalation branch ----------------------------------------------------------------------


def test_the_fixtures_cannot_reach_parent_escalation(sweep: Sweep, fixtures: Any) -> None:
    """Measured, not assumed -- and the branch is then exercised synthetically below.

    Whether the fixture set can put two degraded delimiters under one parent is a property of the
    fixtures, so it is checked rather than believed. If a future fixture edit makes it reachable
    this test fails, which is the signal to assert on the real path instead of the synthetic one.
    """
    reached = [
        service_ref
        for service_ref, results in sweep.by_service.items()
        if results["delimiter_localiser"].findings
        and results["delimiter_localiser"].findings[0].suspected_domain is FaultDomain.NODE_OR_OLT
    ]
    assert reached == [], f"{len(reached)} services now reach it; assert on those instead"
    assert len(fixtures.services) == 41


async def test_two_degraded_delimiters_under_one_parent_localise_above_both(
    now: datetime,
) -> None:
    """Sending a crew to one tap when the fault is above both is the error this prevents."""
    context = DetectionContext(
        incident_id="INC-ESC",
        now=now,
        technology=Technology.PON,
        plant={
            "port": {
                "pon_port_ref": "OLT-SYN-1/1/1",
                "degraded_by_delimiter": {"ODP-SYN-A": 3, "ODP-SYN-B": 2},
            },
            "delimiter": {
                "delimiter_ref": "ODP-SYN-A",
                "degraded_count": 3,
                "services_in_service": 8,
            },
        },
    )
    result = await DelimiterLocaliser().detect(context)
    assert result.ran, result.unavailable_reason
    assert [f.suspected_domain for f in result.findings] == [FaultDomain.NODE_OR_OLT]
    assert result.findings[0].suspected_delimiter_ref is None
