"""Scoring one candidate assignment, from the pack's weights.

**The weights are not comparable to each other, and that is the trap this module exists to
document.** `dispatch.objective_weights` reads `sla_risk: 100, travel_minutes: 1`, which looks like
SLA is a hundred times more important than travel. It is not: five of the six terms are fractions
in `[0, 1]` and `travel_minutes` multiplies a raw minute count. A 40-minute drive contributes 40,
against a maximum of 100 from a fully breached SLA. The weights encode a *unit conversion* as much
as a priority, and anyone editing them needs to know which.

Making travel a fraction too -- dividing by some maximum journey -- was the alternative, and it is
worse: the divisor would be a fifth number nobody could justify, and it would make the objective's
travel term depend on the longest journey in the batch, so adding a Vieques job to a metro run would
silently reprice every metro leg. A raw minute count is at least a thing that exists.

`score_candidate` returns a *benefit* -- higher is better -- with travel subtracted. Costs and
benefits in one signed number keeps the greedy pass a simple `max`, and keeps the sign convention
in one place rather than at each comparison site.
"""

from __future__ import annotations

from dataclasses import dataclass

from lpr_cpe.dispatch.constraints import Candidate
from lpr_cpe.policies.models import BlastRadiusPolicy, DispatchPolicy


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The six terms and their total, kept separate so a plan can be argued with.

    A single objective number is unauditable: "why did the Bayamón job go first" has no answer from
    a float. Every term is retained, weighted, and reported, which is also what makes the weights
    testable -- setting one to zero must move exactly one term.
    """

    sla_risk: float = 0.0
    blast_radius: float = 0.0
    travel_cost: float = 0.0
    crew_skill_match: float = 0.0
    appointment_window: float = 0.0
    vulnerable_customer: float = 0.0

    @property
    def total(self) -> float:
        """Benefits minus the one cost. Travel is already stored positive."""
        return (
            self.sla_risk
            + self.blast_radius
            + self.crew_skill_match
            + self.appointment_window
            + self.vulnerable_customer
            - self.travel_cost
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "sla_risk": round(self.sla_risk, 4),
            "blast_radius": round(self.blast_radius, 4),
            "travel_cost": round(self.travel_cost, 4),
            "crew_skill_match": round(self.crew_skill_match, 4),
            "appointment_window": round(self.appointment_window, 4),
            "vulnerable_customer": round(self.vulnerable_customer, 4),
            "total": round(self.total, 4),
        }


def sla_urgency(remaining_minutes: float | None, *, at_risk: bool, job_minutes: float) -> float:
    """0.0 for comfortable, 1.0 for already breached or unfinishable in time.

    Measured against the job's own duration rather than against a fixed horizon. Ninety minutes of
    remaining budget is relaxed for a fifteen-minute reset and impossible for a two-hour plant
    repair, and a horizon that did not know the job length would rank those two identically.

    `None` remaining -- no SLA context supplied -- returns the `at_risk` flag as 1.0 or 0.0 rather
    than guessing a number. An invented middling urgency would sort real deadlines below imaginary
    ones.
    """
    if remaining_minutes is None:
        return 1.0 if at_risk else 0.0
    if remaining_minutes <= 0:
        return 1.0
    headroom = remaining_minutes / max(job_minutes, 1.0)
    if headroom <= 1.0:
        # Cannot be finished inside the budget however it is scheduled. Still ranked at the top,
        # because "already lost" incidents are exactly the ones an operations review asks why
        # nobody attended.
        return 1.0
    # Falls from 1.0 at one job-length of headroom towards 0.0 as headroom grows. Reciprocal rather
    # than linear so the curve is steep where decisions are close and flat where they are not.
    return max(0.0, min(1.0, 1.0 / headroom))


def blast_fraction(affected: int, blast: BlastRadiusPolicy) -> float:
    """Customers affected, as a fraction of the pack's network-action threshold.

    That threshold is reused rather than a new divisor invented, because the pack already answers
    "how many customers makes this a network-scale problem" once, for the blast-radius gate. A
    second scale here would drift from it, and the dispatch queue would disagree with the gate about
    which incidents are large. Clamped at 1.0: past the threshold everything is network-scale, and
    the ranking between a 500-customer and a 900-customer outage belongs to severity, not here.
    """
    return max(0.0, min(1.0, affected / max(blast.network_action_threshold, 1)))


def skill_overlap(required: list[str], held: list[str]) -> float:
    """Fraction of the required skills the crew holds.

    Only reachable at 1.0 for a feasible candidate -- `check_skill` refuses anything less -- so this
    term does not discriminate between qualified crews. It is kept because it is what makes the
    weight testable, and because it stops meaning "always 1.0" the moment
    `require_crew_type_match` or the skill check is relaxed for a soft-constraint mode.

    No required skills scores 1.0. A job needing nothing specific is not a job every crew is equally
    unsuited to.
    """
    if not required:
        return 1.0
    return len(set(required) & set(held)) / len(set(required))


def appointment_fit(c: Candidate) -> float:
    """1.0 inside a stated customer window, 0.0 outside, 0.5 when none was stated.

    The middle value is doing real work. A job with no window is neither honouring a preference nor
    breaking one, and scoring it 0.0 would sort every no-preference customer behind everyone who
    named a time -- which over a queue means the customers who ask for nothing are served last.
    """
    windows = c.requirement.customer_availability_windows
    if not windows:
        return 0.5
    return 1.0 if any(s <= c.start and c.end <= e for s, e in windows) else 0.0


def score_candidate(
    c: Candidate, *, policy: DispatchPolicy, blast: BlastRadiusPolicy
) -> ScoreBreakdown:
    """Weight the six terms for one candidate.

    Deterministic and side-effect free: the same candidate scores the same on every run and on
    every machine, which is what lets a plan be recomputed during an approval replay and compared
    against the one the approver saw.
    """
    weights = policy.objective_weights
    job_minutes = c.requirement.estimated_duration.total_seconds() / 60.0
    return ScoreBreakdown(
        sla_risk=weights.sla_risk
        * sla_urgency(
            c.context.sla_remaining_minutes,
            at_risk=c.context.sla_at_risk,
            job_minutes=job_minutes,
        ),
        blast_radius=weights.blast_radius * blast_fraction(c.context.affected_customers, blast),
        travel_cost=weights.travel_minutes * c.travel.minutes,
        crew_skill_match=weights.crew_skill_match
        * skill_overlap(c.requirement.skills_required, c.crew.skills),
        appointment_window=weights.appointment_window * appointment_fit(c),
        vulnerable_customer=(weights.vulnerable_customer if c.context.vulnerable_customer else 0.0),
    )


def urgency_rank(
    requirement_priority: float,
    context_sla: float | None,
    *,
    at_risk: bool,
    affected: int,
    job_minutes: float,
    blast: BlastRadiusPolicy,
) -> float:
    """How early a requirement should be *considered*, before any crew is chosen.

    Deliberately excludes travel. Travel is a property of an assignment, not of a job, so ordering
    the queue by it would let a job's position depend on which crew happened to be evaluated first
    -- and the greedy pass would then produce different plans for different input orderings, which
    is the one thing a deterministic optimizer may not do.
    """
    return (
        2.0 * sla_urgency(context_sla, at_risk=at_risk, job_minutes=job_minutes)
        + blast_fraction(affected, blast)
        + requirement_priority
    )


__all__ = [
    "ScoreBreakdown",
    "appointment_fit",
    "blast_fraction",
    "score_candidate",
    "skill_overlap",
    "sla_urgency",
    "urgency_rank",
]
