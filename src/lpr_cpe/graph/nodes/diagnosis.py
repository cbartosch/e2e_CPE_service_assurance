"""Stage 2, second half: name the fault and say what could be done about it (P10-P11).

Two nodes, and between them the incident stops being a pile of readings and becomes a decision.
P10 says *where the fault is*; P11 says *what actions address a fault there*. Neither picks an
action -- selection is `policies.engine`'s and stage 3's, and the specification says so
explicitly ("the final selection must be made by deterministic policy and scoring services").

Why P10 classifies again instead of reading P07's answer
--------------------------------------------------------
P07 runs all thirteen detectors, `FaultDomainClassifier` among them, so a classification already
exists by the time this module runs. Reusing it would be wrong, and measurably so.

P08 plans the tests that discriminate between the live hypotheses and P09 runs them, which is the
whole point of the cycle -- so by P10 the finding set has grown by everything P09 scored. On the
`hfc_degraded_upstream` fixture `SVC-SJ-011-A-01` that changes the answer: P07's classifier sees
`distribution` leading and P10's sees `tap_or_odp`, which is the correct one (five of the eight
services behind `TAP-SJ-011-A` are degraded). Handing `conclude()` P07's stale `distribution`
alongside a hypothesis set that now leads on `tap_or_odp` produces a result whose confidence is
`leader/(leader+rival) * leader` across two different domains -- 0.119 on that fixture -- and fires
the low-confidence gate on a case the evidence settles.

So the classifier is re-run here over `live_findings(state)`. It is re-run rather than replaced by
"take the top hypothesis's domain" because `detectors.localisation.domain_weights` is the one owner
of how findings fold into a domain, and its own docstring warns that deriving the domain from the
posterior would be a second formula that disagrees with the first at the margin -- notably on
`POWER`, which the classifier short-circuits and the posterior does not.

Where the specification asks for a field the model does not have
----------------------------------------------------------------
`RCAResult` is asked to carry "whether the cause appears common or individual" and "whether
customer access is required". It carries neither, and neither is invented here.

* **Common or individual** already has an owner: `blast_radius.scope_for_fault_domain`. Anything
  wider than `SINGLE_PREMISES` is a common cause by construction. P10 records the answer in its
  audit detail rather than as a field, so there is one table and not two.
* **Customer access required** is a property of the remedy, not of the fault -- a CPE fault is
  fixed remotely by a reboot and in the home by a swap. `ResolutionOption.requires_customer_present`
  is where it is decided, per option, from `resolution._CATALOGUE`. P11 reports it.

The specification's twelve fault-domain labels against this repository's fifteen
-------------------------------------------------------------------------------
`domain.enums.FaultDomain` was drawn from the operational boundary (who attends, and where the
responsibility changes hands) rather than from the specification's list, and the two do not line up
one-to-one. The mapping, so that a reader of the specification can find each of its labels here:

| Specification | `FaultDomain` | Note |
| --- | --- | --- |
| `cpe` | `CPE` | |
| `wifi_or_home_network` | `CPE` | scored separately by `CPEWiFiAnomalyDetector`, not a domain |
| `premise_wiring` | `INSIDE_HOME_WIRING` | |
| `drop` | `DROP` | |
| `hfc_tap` | `TAP_OR_ODP` | one domain: the delimiter. `DelimiterKind` carries which |
| `pon_odp` | `TAP_OR_ODP` | as above |
| `shared_access_network` | `DISTRIBUTION`, `FEEDER` | split, because a crew attends them alike |
| `plant` | `NODE_OR_OLT`, `HEADEND_OR_CO` | split, because the blast radius differs by an order |
| `provisioning` | `PROVISIONING` | |
| `service_platform` | `SERVICE_PLATFORM` | |
| `commercial_power` | `POWER` | |
| `unknown` | `UNKNOWN` | |

Three members have no counterpart in the specification's list and are kept because a decision
depends on each: `CUSTOMER_ENVIRONMENT` (the fault is real but ours to advise on, not to fix),
`NO_FAULT_FOUND` (the KPI the whole workflow exists to move), and `MULTIPLE` (an incident with two
faults must not be closed on one).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from lpr_cpe.decision_services.blast_radius import BlastRadiusScope, scope_for_fault_domain
from lpr_cpe.decision_services.rca import conclude
from lpr_cpe.decision_services.resolution import plan_resolution
from lpr_cpe.detectors.base import DetectionContext, DetectorResult
from lpr_cpe.detectors.localisation import DelimiterLocaliser, FaultDomainClassifier
from lpr_cpe.domain.diagnosis import AnomalyFinding, RCAResult
from lpr_cpe.domain.enums import (
    FaultDomain,
    KPIName,
    Technology,
)
from lpr_cpe.domain.records import TopologyContext
from lpr_cpe.domain.resolution import ResolutionPlan
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.nodes._runtime import (
    NodeUpdate,
    audit,
    derive_id,
    emit_kpi,
    node,
    preview,
)
from lpr_cpe.graph.nodes.evidence import live_findings
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.observability.kpi import MetricTimestamp, mark

#: The synthetic detector name P10 files its re-classification input under.
#:
#: `DetectionContext.findings_from` reads `prior` and filters on `DetectorResult.ran` and
#: `.derived`, so the folded findings have to arrive as a result rather than as a list. Naming it
#: after this node rather than after any real detector keeps the audit trail honest: nothing called
#: `assemble_case_evidence` produced this, it was assembled here from what the cycle left behind.
_REPLAY_DETECTOR = "graph.determine_root_cause.replay"


# ----------------------------------------------------------------------------------------------
# P10 -- determine likely root cause and fault domain
# ----------------------------------------------------------------------------------------------


def _replay(findings: Sequence[AnomalyFinding]) -> DetectorResult:
    """The cycle's findings, packaged as the prior a classifier expects.

    `ran=True` and `derived=False` are both load-bearing. `findings_from` skips results that did
    not run, so `ran=False` would hand the classifier an empty evidence base and it would report
    `unavailable` -- which `conclude` would then turn into an `UNKNOWN` domain on a case that has
    plenty of evidence. `derived=True` would have it skipped for the opposite reason: derived
    results restate other findings and are excluded from weighting by default. These *are* the
    other findings, already folded by `live_findings`, so they are the primary input and not a
    summary of one.
    """
    return DetectorResult(
        detector_name=_REPLAY_DETECTOR,
        detector_version="1.0.0",
        ran=True,
        findings=list(findings),
    )


def _delimiter_ref_from(
    findings: Sequence[AnomalyFinding], state: IncidentState, topology: TopologyContext | None
) -> str | None:
    """The suspected delimiter, preferring what a detector suspected over what topology recorded.

    `DelimiterLocaliser` names a delimiter only when the readings behind it implicate one; the
    topology's `delimiter_ref` is simply the tap or ODP this service happens to hang off. They are
    usually the same string and mean different things -- "the evidence points here" against "the
    customer is connected here" -- and the first is what a crew is dispatched on, so it wins.
    """
    suspected = [
        finding.suspected_delimiter_ref
        for finding in findings
        if finding.detector_name == DelimiterLocaliser.name and finding.suspected_delimiter_ref
    ]
    if suspected:
        return suspected[0]
    return state.get("delimiter_ref") or (topology.delimiter_ref if topology else None)


def _rejected_before(previous: RCAResult | None) -> dict[FaultDomain, str]:
    """What an earlier cycle ruled out, so this one does not re-open it silently.

    A domain rejected on the first pass stays rejected on the second unless new evidence revives
    it, and `build_hypotheses` takes the map to mark them. Without this the loop re-litigates the
    same discarded hypotheses every cycle and the ranked list an operator reads never converges.
    """
    if previous is None:
        return {}
    return {
        hypothesis.fault_domain: hypothesis.rejection_reason or "rejected in an earlier cycle"
        for hypothesis in previous.ruled_out
    }


@node("determine_root_cause")
async def determine_root_cause(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P10. Fold the cycle's findings into one fault domain and one ranked hypothesis set.

    The order is: fold the findings (`live_findings`), classify them (`FaultDomainClassifier`),
    then rank them (`conclude`). Each step has exactly one owner elsewhere in the repository and
    this node supplies the inputs; nothing here decides what a bad reading is, which domain a
    finding implicates, or how a posterior is computed.

    `fault_domain` is written from `rca.fault_domain` rather than from the classifier, because
    `RCAResult.derive` downgrades a domain no live hypothesis supports to `UNKNOWN` and D08 routes
    on the domain. Writing the classifier's answer would send an incident down the plant path on a
    classification the hypothesis set does not back.

    This node is on **three** of the parent graph's five cycles and neither counter separates its
    runs alone, which is why the audit discriminator is the pair and `cycles_used` is not. Walked
    from the tables: D06's and D10's `retry_diagnosis` both return to P07 and move
    `diagnostic_cycles` while P11 may not run at all; D12's returns to P10 and moves only
    `resolution_cycles`, because that lap is P10 -> P11 -> self_help -> P10 and never reaches P07.
    Keyed on `diagnostic_cycles` alone, a re-diagnosis on the self-help lap derived the same
    `event_id` as the first, and `append_unique` -- first-write-wins -- kept the first: the second
    diagnosis left no trace at all. Measured on `SVC-SJ-011-A-01`, both `AUD-0dafcf9872465b830461`.

    `cycles_used` stays the diagnostic count on purpose. It answers "how much evidence-gathering
    did this conclusion take", which is a question about P07's loop; widening it to the pair would
    make an RCA reached on the first cycle report two because a plan had been drawn up in between.
    """
    now = ctx.clock.now()
    findings = live_findings(state)
    topology = state.get("topology")
    technology = state.get("technology", Technology.UNKNOWN)
    cycle = state.get("diagnostic_cycles", 1)
    lap = state.get("resolution_cycles", 0)

    context = DetectionContext(
        incident_id=state.get("incident_id") or "",
        now=now,
        technology=technology,
        topology=topology,
        # Empty for the same reason P07 runs the detectors against an empty evidence list: the
        # classifier's finding would otherwise cite every source in the case, and corroboration is
        # counted in distinct refs. The refs that matter are already on the findings being folded.
        evidence=[],
        prior=[_replay(findings)],
        thresholds=dict(ctx.policy.pack.detector_thresholds),
    )
    classified = await FaultDomainClassifier().detect(context)
    domain = next(
        (f.suspected_domain for f in classified.findings if f.suspected_domain is not None),
        FaultDomain.UNKNOWN,
    )

    rca = conclude(
        findings,
        concluded_at=now,
        fault_domain=domain,
        rca_policy=ctx.policy.pack.rca,
        evidence=ctx.policy.pack.evidence,
        technology=technology,
        delimiter_ref=_delimiter_ref_from(findings, state, topology),
        rejected=_rejected_before(state.get("rca")),
        cycles_used=cycle,
    )

    scope = scope_for_fault_domain(rca.fault_domain)
    common = scope is not BlastRadiusScope.SINGLE_PREMISES
    update: NodeUpdate = {
        "rca": rca,
        "fault_domain": rca.fault_domain,
        "delimiter": rca.delimiter_kind,
        "delimiter_ref": rca.delimiter_ref,
        # No `EvidenceItem` is filed for the conclusion, and that is deliberate. `EvidenceKind` has
        # eighteen members and every one of them names an observation; a conclusion filed among
        # them would be corroborating evidence for itself on the next cycle, because
        # `build_hypotheses` counts distinct refs. `rca.evidence_refs` already carries what this
        # was concluded from, which is what a work order or an MR needs to cite.
        # Derived, so `live_findings` will drop it on the next cycle rather than let the winning
        # domain vote twice. Kept because it is the record of what was decided and why.
        "anomaly_findings": list(classified.findings),
        "audit_events": [
            audit(
                state,
                ctx,
                node="determine_root_cause",
                action="determine_root_cause",
                outcome=rca.fault_domain.value,
                subject_ref=rca.delimiter_ref or state.get("service_ref") or "",
                reason_code=rca.reason_code,
                detail={
                    "cycle": cycle,
                    # Printed as well as hashed. Two records distinguished only by an opaque id
                    # are two records a reader cannot order or tell apart.
                    "resolution_lap": lap,
                    "confidence": rca.confidence,
                    "classified_as": domain.value,
                    "delimiter_kind": rca.delimiter_kind.value,
                    "delimiter_ref": rca.delimiter_ref,
                    "findings_folded": len(findings),
                    "hypotheses": [
                        {"domain": h.fault_domain.value, "posterior": h.posterior} for h in rca.live
                    ],
                    "ruled_out": [h.fault_domain.value for h in rca.ruled_out],
                    "next_tests": sorted(
                        {t.value for h in rca.live for t in h.discriminating_tests}
                    ),
                    # The specification's "common or individual", answered from the one table that
                    # already knows -- see the module docstring.
                    "cause_appears": "common" if common else "individual",
                    "blast_radius_scope": scope.value,
                },
                discriminator=f"{cycle}.{lap}",
            )
        ],
        **mark(MetricTimestamp.DIAGNOSED_AT, now),
    }

    # Same shape as P06: `mark` writes the timestamp the KPI is computed from, so the calculator
    # has to see the state as the reducers will leave it rather than as it was on entry.
    update["kpi_events"] = emit_kpi(
        preview(state, update),
        ctx,
        KPIName.TIME_TO_DIAGNOSE_SECONDS,
        node="determine_root_cause",
    )
    return update


# ----------------------------------------------------------------------------------------------
# P11 -- generate resolution options
# ----------------------------------------------------------------------------------------------


#: One candidate reference: read it out of the state and the topology, or say it is not there.
type _Ref = Callable[[IncidentState, TopologyContext | None], str | None]

#: Where the reference for an action of each blast-radius scope comes from, in preference order.
#:
#: New here, because nothing else in the repository maps a scope to a reference: `blast_radius`
#: maps a domain to a scope and *sizes* it, and stops there. The pairing matters because
#: `ResolutionOption.target_ref` is what an adapter is eventually called with -- offering "replace
#: the tap" against a CPE reference is an option that cannot be executed, and it would fail at the
#: adapter with a reference error rather than here with a planning one.
#:
#: A table rather than a `match`, so that `_every_scope_has_a_target` below can check it is
#: complete. A `match` looks exhaustive and is not: deleting a `case` from one was measured to
#: leave mypy silent, which is why the check is data and not syntax.
#:
#: Each chain falls back **inward**. A missing node reference degrades to the service -- wrong, but
#: small. Degrading outward would aim a plant action at a headend on the strength of a topology gap.
_TARGET_CHAINS: Mapping[BlastRadiusScope, tuple[tuple[str, _Ref], ...]] = {
    BlastRadiusScope.SINGLE_PREMISES: (
        ("cpe_ref", lambda s, t: s.get("cpe_ref")),
        ("service_ref", lambda s, t: s.get("service_ref")),
    ),
    BlastRadiusScope.DELIMITER: (
        ("delimiter_ref", lambda s, t: s.get("delimiter_ref")),
        ("topology.delimiter_ref", lambda s, t: t.delimiter_ref if t else None),
    ),
    # An amplifier or the primary splitter is the distribution leg's own object. Falling back to
    # the node or port names the thing upstream of the leg, which is the smallest honest
    # over-statement available when the leg itself was never resolved.
    BlastRadiusScope.DISTRIBUTION: (
        ("topology.amplifier_refs", lambda s, t: next(iter(t.amplifier_refs), None) if t else None),
        ("topology.primary_splitter_ref", lambda s, t: t.primary_splitter_ref if t else None),
        ("topology.node_ref", lambda s, t: t.node_ref if t else None),
        ("topology.pon_port_ref", lambda s, t: t.pon_port_ref if t else None),
    ),
    BlastRadiusScope.NODE_OR_PORT: (
        ("topology.node_ref", lambda s, t: t.node_ref if t else None),
        ("topology.pon_port_ref", lambda s, t: t.pon_port_ref if t else None),
    ),
    BlastRadiusScope.HEADEND_OR_OLT: (
        ("topology.headend_ref", lambda s, t: t.headend_ref if t else None),
        ("topology.cmts_ref", lambda s, t: t.cmts_ref if t else None),
        ("topology.olt_ref", lambda s, t: t.olt_ref if t else None),
    ),
}


def _every_scope_has_a_target() -> None:
    """A scope with no reference chain would silently aim every action at the service."""
    missing = sorted(s.value for s in BlastRadiusScope if s not in _TARGET_CHAINS)
    if missing:
        raise RuntimeError(
            f"_TARGET_CHAINS has no reference for blast-radius scope(s) {missing}. Every scope "
            "needs one, or an action planned at that scope is aimed at whatever the fallback "
            "happens to be."
        )


_every_scope_has_a_target()


def _target_for(scope: BlastRadiusScope, state: IncidentState) -> tuple[str, str]:
    """The object an action against this scope would touch, and which field it came from."""
    topology = state.get("topology")
    for name, read in _TARGET_CHAINS.get(scope, ()):
        ref = read(state, topology)
        if ref:
            return name, ref
    return "service_ref", state.get("service_ref") or state.get("incident_id") or ""


def _access_note(plan: ResolutionPlan) -> str:
    """The specification's "whether customer access is required", answered per plan.

    Read off the options rather than off the fault domain, because that is where it is decided --
    `resolution._CATALOGUE` sets `requires_customer_present` per candidate action, and the same
    domain can hold both kinds. A CPE fault offers a remote reboot that needs nobody at home and a
    swap that needs somebody at home; saying "cpe faults require access" would be false of the
    first option and would suppress the cheapest remedy in the catalogue.
    """
    needing = [o.label for o in plan.options if o.requires_customer_present]
    if not needing:
        return "no option in this plan needs the customer at home"
    if len(needing) == len(plan.options):
        return "every option in this plan needs the customer at home"
    return f"{len(needing)} of {len(plan.options)} options need the customer at home: " + ", ".join(
        needing
    )


@node("generate_resolution_options")
async def generate_resolution_options(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """P11. Offer every catalogued action for this fault domain that the pack allows.

    Offers, and does not choose. `selected_option_id` is left unset: the specification requires the
    final selection to be made by deterministic policy and scoring services, and those run in stage
    3 where an action can be evaluated against blast radius, attempt limits, the maintenance
    window and the customer-contact budget -- none of which this node has any business
    second-guessing.

    Options already attempted are passed to `plan_resolution` rather than filtered out here, so
    `ResolutionPlan.exhausted` can tell "nothing addresses this domain" apart from "everything that
    does has been tried", which route to different places.

    An empty plan is a valid outcome and is recorded rather than escalated from here. `UNKNOWN`,
    `MULTIPLE` and `NO_FAULT_FOUND` all produce one, all mean something different, and the routing
    that tells them apart is D06's and D09's.

    The audit event for an empty plan carries no `ReasonCode`, and that is a stated gap rather than
    an oversight -- gap RESOLUTION-2. An empty plan has two causes -- the catalogue has nothing for
    this domain, or the pack disallowed everything it had -- and no `ReasonCode` distinguishes them:
    `POLICY_NO_MATCHING_RULE` means the pack had no rule, which is a third thing again. Both causes
    are stated in prose by `plan_resolution`, whose `notes` are passed through verbatim as
    `detail["withheld"]`. Labelling them with a code that means something else would be worse than
    leaving the code off, because a code is what a dashboard counts.

    The cycle counter is this node's own and not `diagnostic_cycles`, because this node re-runs on a
    loop that one does not count. D12's `retry_diagnosis` returns to P10, so the self-help loop is
    P10 -> P11 -> self_help -> P10 and never touches P07, which is where `diagnostic_cycles` is
    bumped. Keyed on that counter, a second lap derived the *same* `plan_id` and the same audit
    `event_id`; `append_unique` is first-write-wins, so the second plan was dropped and the trail
    kept a record reading `already_attempted: []` for an incident that had already tried and failed
    a repair. Measured on `SVC-SJ-011-A-01`, both laps deriving `RPLAN-228a28c9ba86d6de6d43`.
    """
    now = ctx.clock.now()
    domain = state.get("fault_domain", FaultDomain.UNKNOWN)
    cycle = state.get("resolution_cycles", 0) + 1
    scope = scope_for_fault_domain(domain)
    target_field, target_ref = _target_for(scope, state)
    plan_id = derive_id("RPLAN", state.get("incident_id") or "", cycle)

    plan = plan_resolution(
        plan_id=plan_id,
        created_at=now,
        fault_domain=domain,
        target_ref=target_ref,
        allowlist=ctx.policy.pack.remote_actions,
        blast_radius_policy=ctx.policy.pack.blast_radius,
        topology=state.get("topology"),
        attempted_option_ids=_attempted(state, plan_id),
    )

    return {
        "resolution_cycles": cycle,
        "resolution_plan": plan,
        "resolution_options": list(plan.options),
        "audit_events": [
            audit(
                state,
                ctx,
                node="generate_resolution_options",
                action="generate_resolution_options",
                outcome="offered" if plan.options else "no_option_addresses_this_domain",
                subject_ref=target_ref,
                detail={
                    "cycle": cycle,
                    "fault_domain": domain.value,
                    "target_ref": target_ref,
                    "target_from": target_field,
                    "blast_radius_scope": scope.value,
                    "options": [
                        {
                            "action": o.action_type.value,
                            "success": o.estimated_success_probability,
                            "minutes": o.estimated_duration.total_seconds() / 60.0,
                            "truck_roll": o.requires_truck_roll,
                            "reversible": o.reversible,
                            "blast_radius": o.blast_radius,
                        }
                        for o in plan.options
                    ],
                    "already_attempted": list(plan.attempted_option_ids),
                    "exhausted": plan.exhausted,
                    "escalation_path": plan.escalation_path,
                    "customer_access": _access_note(plan),
                    "withheld": plan.notes,
                },
                discriminator=cycle,
            )
        ],
    }


def _attempted(state: IncidentState, plan_id: str) -> list[str]:
    """Option ids already tried this incident, expressed in *this* plan's id space.

    `ResolutionOption.option_id` is `f"{plan_id}-{action_type.value}"` and `plan_id` carries the
    resolution cycle, so the same action offered in cycle 1 and cycle 2 has two different ids.
    `ResolutionPlan.untried` matches on the id, so passing the ids recorded in cycle 1 would match
    nothing in cycle 2 and every loop would re-offer a reboot that has already failed twice --
    `exhausted` would never become true and the incident would circle instead of escalating. So
    the history is read as a set of action *types* and re-expressed against this plan's id.

    "Already tried" is `ActionRecord.was_attempted` rather than a set of outcomes kept here. The
    subgraph that executes these options needs the same notion for the policy engine's attempt
    limit, and two copies of the set would drift; the record is the one place that owns it.
    """
    tried = {
        record.action_type.value
        for record in state.get("action_history", [])
        if record.was_attempted
    }
    return [f"{plan_id}-{action}" for action in sorted(tried)]


DIAGNOSIS_NODES: Sequence[tuple[str, Any]] = (
    ("determine_root_cause", determine_root_cause),
    ("generate_resolution_options", generate_resolution_options),
)


__all__ = [
    "DIAGNOSIS_NODES",
    "determine_root_cause",
    "generate_resolution_options",
]
