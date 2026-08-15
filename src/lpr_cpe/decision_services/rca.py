"""Turning detector findings into a hypothesis set, with what would tell them apart.

This module does *not* decide which fault domain leads.
`detectors.localisation.FaultDomainClassifier` does, it has rules this module does not (the power
short-circuit, the outward-reach tiebreak), and its answer is passed in. What happens here is the
other half: building the candidate explanations
that the chosen domain and its rivals correspond to, scoring how much of the evidence stands behind
each, and handing the set to `RCAResult.derive`, which computes how contested the set is.

Three facts, three owners, and the split is what keeps them from disagreeing:

* which domain leads -- the classifier, from `domain_weights`
* how strongly each domain is evidenced -- here, from the same `domain_weights`
* how confident the conclusion as a whole is -- `RCAResult.derive`, from the posteriors

The import of `domain_weights` rather than a local `score * confidence` is the load-bearing part.
`RCAResult`'s own validator refuses a result whose primary hypothesis is not in the stated fault
domain, so a second weighting formula here would not quietly disagree with the classifier -- it
would raise a validation error part-way through an incident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from lpr_cpe.decision_services.delimiter import delimiter_kind_for
from lpr_cpe.detectors.localisation import domain_weights
from lpr_cpe.domain.diagnosis import AnomalyFinding, RCAHypothesis, RCAResult
from lpr_cpe.domain.enums import FaultDomain, Technology, TestKind
from lpr_cpe.policies.models import EvidencePolicy, RCAPolicy

#: No hypothesis reaches 1.0. A posterior of exactly one asserts that no other explanation is
#: possible, which a set of detector findings cannot establish -- there is always the fault nobody
#: has a detector for. The number also keeps `RCAResult.derive` from returning a confidence of
#: exactly 1.0, which a reader takes as a measurement rather than as an inference.
_CERTAINTY_CEILING = 0.95


def build_hypotheses(
    findings: Sequence[AnomalyFinding],
    *,
    evidence: EvidencePolicy,
    rejected: Mapping[FaultDomain, str] | None = None,
) -> list[RCAHypothesis]:
    """One hypothesis per domain any finding suspects, ranked by how much evidence stands behind it.

    The posterior is `share x corroboration`, and it is two factors rather than one because either
    alone produces a number that is wrong in a way that matters:

    * **Share** alone -- the domain's weight over the total -- makes a lone weak finding certain.
      One detector scoring 0.3 at confidence 0.4 is the only suspicion on the board, so its share is
      100%, and a diagnosis built on it would be reported as conclusive.
    * **Corroboration** alone ignores that the evidence disagrees. Four sources split evenly across
      two domains corroborate both.

    Corroboration counts *distinct evidence references*, against
    `evidence.min_sources_for_diagnosis` -- which the pack sets to 2 and explains as "a single
    source that agrees with itself is not corroboration". Where no finding carries evidence
    references, distinct detector names are counted instead and the hypothesis says so in its
    statement: two detectors reading one payload are not two sources, so that fallback overstates
    independence and must be visible where it is used.

    `rejected` carries domains a previous cycle ruled out, with the reason. They stay in the set as
    rejected hypotheses rather than being dropped -- `RCAResult.ruled_out` is what the reviewer at
    the low-confidence interrupt reads to tell "considered and rejected" from "never considered".
    """
    weights = domain_weights(findings)
    if not weights:
        return []
    total = sum(weights.values()) or 1.0
    ruled_out = dict(rejected or {})

    hypotheses: list[RCAHypothesis] = []
    for domain in sorted(weights, key=lambda d: (-weights[d], d.value)):
        supporting = [f for f in findings if f.suspected_domain is domain]
        opposing = [
            f
            for f in findings
            if f.suspected_domain is not None and f.suspected_domain is not domain
        ]

        sources = {ref for f in supporting for ref in f.evidence_refs}
        detectors = {f.detector_name for f in supporting}
        counted, basis = (len(sources), "sources") if sources else (len(detectors), "detectors")
        corroboration = min(1.0, counted / evidence.min_sources_for_diagnosis)
        share = weights[domain] / total
        posterior = min(_CERTAINTY_CEILING, share * corroboration)

        caveat = (
            ""
            if basis == "sources"
            else " No finding carried an evidence reference, so this counts detectors instead, "
            "which overstates their independence."
        )
        hypotheses.append(
            RCAHypothesis(
                hypothesis_id=f"hyp-{domain.value}",
                fault_domain=domain,
                statement=(
                    f"The fault is in {domain.value.replace('_', ' ')}: "
                    f"{len(supporting)} finding(s) point there, on {counted} {basis}, "
                    f"which is {share:.0%} of the weighted evidence.{caveat}"
                ),
                # Uniform across the domains anything suspects. Before the evidence was read we had
                # no reason to prefer one over another, and a prior shaped by, say, historical fault
                # rates would make this diagnosis partly a report on last quarter's faults.
                prior=round(1.0 / len(weights), 4),
                posterior=round(posterior, 4),
                supporting_evidence_refs=tuple(sorted(sources)),
                # Evidence pointing at a different domain is evidence against this one. That is what
                # makes the contradicting list real rather than decorative: with a single suspected
                # domain it is genuinely empty, and the hypothesis is then one nothing competes with
                # rather than one nobody tried to falsify.
                contradicting_evidence_refs=tuple(
                    sorted({ref for f in opposing for ref in f.evidence_refs})
                ),
                discriminating_tests=_discriminating_tests(supporting, opposing),
                suspected_delimiter_ref=next(
                    (f.suspected_delimiter_ref for f in supporting if f.suspected_delimiter_ref),
                    None,
                ),
                rejected=domain in ruled_out,
                rejection_reason=ruled_out.get(domain, ""),
            )
        )
    return hypotheses


def _discriminating_tests(
    supporting: Sequence[AnomalyFinding],
    opposing: Sequence[AnomalyFinding],
) -> tuple[TestKind, ...]:
    """The tests worth running to separate this hypothesis from its rivals.

    Rivals' recommended tests, because a test that would confirm a competing explanation is what
    discriminates -- running this hypothesis's own tests can only confirm what is already believed.
    When there are no rivals there is nothing to discriminate from, so the hypothesis's own tests
    stand in; `TestRequest.expected_discrimination` is where that distinction is written down for
    the test actually requested.
    """
    source = opposing or supporting
    return tuple(sorted({t for f in source for t in f.recommended_tests}, key=lambda t: t.value))


def conclude(
    findings: Sequence[AnomalyFinding],
    *,
    concluded_at: datetime,
    fault_domain: FaultDomain,
    rca_policy: RCAPolicy,
    evidence: EvidencePolicy,
    technology: Technology = Technology.UNKNOWN,
    delimiter_ref: str | None = None,
    rejected: Mapping[FaultDomain, str] | None = None,
    cycles_used: int = 1,
) -> RCAResult:
    """Build the `RCAResult` for a diagnosis cycle.

    `fault_domain` is the classifier's answer and is not second-guessed here, with one exception
    that `RCAResult.derive` owns: a domain no live hypothesis supports is reported as `UNKNOWN`
    rather than asserted at zero confidence, because routing reads the domain and would otherwise
    send a crew to a plant element nothing in the evidence implicates.

    The confidence bar comes from `rca.review_below` rather than from any of the action bars. The
    reason code this function produces is what the low-confidence-RCA interrupt fires on, and
    `review_below` is the pack's threshold for exactly that interrupt -- using `min_for_dispatch`
    here would label a result low-confidence because it cannot justify a truck roll, which is a
    different question and one `policies.engine` already asks separately.
    """
    hypotheses = build_hypotheses(findings, evidence=evidence, rejected=rejected)
    return RCAResult.derive(
        concluded_at=concluded_at,
        fault_domain=fault_domain,
        hypotheses=hypotheses,
        delimiter_kind=delimiter_kind_for(technology),
        delimiter_ref=delimiter_ref,
        evidence_refs=sorted({ref for f in findings for ref in f.evidence_refs}),
        summary=_summarise(findings, fault_domain, hypotheses),
        cycles_used=cycles_used,
        confident_at=rca_policy.review_below,
        ambiguity_margin=rca_policy.ambiguity_margin,
    )


def _summarise(
    findings: Sequence[AnomalyFinding],
    fault_domain: FaultDomain,
    hypotheses: Sequence[RCAHypothesis],
) -> str:
    """One sentence an operator reads first, built from counts rather than from adjectives."""
    if not hypotheses:
        return (
            f"{len(findings)} finding(s) and no fault domain among them, so there is no hypothesis "
            "set to rank."
        )
    live = [h for h in hypotheses if not h.rejected]
    ruled_out = len(hypotheses) - len(live)
    tail = f" {ruled_out} hypothesis(es) previously ruled out are retained." if ruled_out else ""
    return (
        f"{len(findings)} finding(s) across {len(hypotheses)} candidate domain(s); "
        f"{fault_domain.value} leads.{tail}"
    )


__all__ = ["build_hypotheses", "conclude"]
