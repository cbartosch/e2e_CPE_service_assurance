"""The bounded-loop guard and the escalation it produces.

The specification requires "a bounded loop and escalation strategy". This module is the whole of it,
kept in one file because a budget enforced in three places is a budget with three different numbers.

Four bounds are checked, each on a **different quantity**:

| Bound | Quantity | Question |
| --- | --- | --- |
| total steps | `total_steps(state)` | Is this incident consuming an unreasonable amount of work? |
| node re-entries | `visit_count(state, node)` | Is one node being re-entered in a cycle? |
| diagnostic cycles | `state["diagnostic_cycles"]` | Is evidence being re-gathered pointlessly? |
| resolution cycles | `state["resolution_cycles"]` | Are we still working through the option list? |

"Different quantity" is the load-bearing word. An earlier draft of this module had a fourth check
too, because `settings` and the policy pack each name a limit for two of them. Measured against a
real sweep, that one was dead: the per-node ceiling of 8 sat behind a re-entry ceiling of 6 on the
same counter, so the graph always stopped at 6 and the 8 could never be reached. A bound that cannot
fire is worse than no bound, because it reads like protection.

The last two are the same shape and are still two bounds, which is the one place that claim has to
be earned rather than asserted. It was earned by walking the tables: the parent graph has five
cycles, and **`D12:retry_diagnosis` -- P10 -> P11 -> self_help -> P10 -- contains P11 and not P07**.
`diagnostic_cycles` is bumped at P07, so on that loop it does not move at all, and a single counter
was not bounding the option list on the self-help side; it was blind to it. `node_reentries` is not
that bound either: it stops a node on *re-entry*, one super-step later than a stage bound stops the
incident wherever it currently is, and it fires at the same 6 for reasons that have nothing to do
with how many options a plan holds.

So where two owners bound one quantity, they are resolved into a single check that takes the
**tighter** limit and records **which owner supplied it** -- never into two checks:

* total steps -- `settings.max_graph_steps` (60), an engineering circuit breaker against a routing
  bug, versus `policy.attempt_limits.total_steps` (200), an operational budget. At the shipped
  defaults the setting binds; raising `LPR_MAX_GRAPH_STEPS` past 200 hands the decision to the pack.
* node re-entries -- `policy.attempt_limits.max_subgraph_reentries` (6) versus a per-node override
  from `GraphContext`, which exists so a scenario test can tighten one node without touching the
  pack every other assertion reads.

Both directions of both resolutions are asserted in the tests, so neither owner is silently dead.
`BudgetVerdict.owner` is what makes this honest at runtime: an escalation says which number stopped
it and where to go and read that number.

Determinism under replay
------------------------
Every id this module mints is derived from its inputs rather than from `uuid4`. That is not a
style preference. The interrupted node re-runs from its start on resume (see
`tests/unit/test_langgraph_replay_contract.py`), so a guard that stamped a fresh uuid would emit a
second audit event with a different natural key, and `append_unique` -- which de-duplicates on that
key -- would keep both. The escalation would then appear to have happened twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from lpr_cpe.domain.enums import IncidentStatus, ReasonCode
from lpr_cpe.domain.governance import AuditEvent
from lpr_cpe.graph.state import IncidentState, total_steps, visit_count

if TYPE_CHECKING:
    from lpr_cpe.graph.context import GraphContext


class BudgetKind(StrEnum):
    """Which bound was exceeded. Carried into the audit trail, so it is a value, not a string."""

    TOTAL_STEPS = "total_steps"
    NODE_REENTRIES = "node_reentries"
    DIAGNOSTIC_CYCLES = "diagnostic_cycles"
    RESOLUTION_CYCLES = "resolution_cycles"


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether the graph may continue, and if not, exactly which bound stopped it."""

    within_budget: bool
    kind: BudgetKind | None = None
    observed: int = 0
    limit: int = 0
    #: The setting or policy field that supplied `limit`, spelled as a path a reader can go and
    #: look at. Without this, an escalation naming a number nobody can locate is a dead end.
    owner: str = ""

    @property
    def reason(self) -> str:
        if self.within_budget:
            return ""
        return (
            f"{self.kind} budget exhausted: observed {self.observed}, "
            f"limit {self.limit} (from {self.owner})"
        )


def step_budget(ctx: GraphContext) -> tuple[int, str]:
    """The binding total-step limit and the name of whichever owner supplied it.

    Returns the tighter of the engineering circuit breaker and the operational budget. See the
    module docstring for why both exist.
    """
    engineering = ctx.max_graph_steps
    operational = ctx.policy.pack.attempt_limits.total_steps
    if engineering <= operational:
        source = (
            "settings.max_graph_steps"
            if ctx.step_budget_override is None
            else "GraphContext.step_budget_override"
        )
        return engineering, source
    return operational, "policy.attempt_limits.total_steps"


def reentry_budget(ctx: GraphContext, node: str) -> tuple[int, str]:
    """The re-entry ceiling for one node, and the name of whichever owner supplied it.

    Same resolution as `step_budget`: the tighter of the pack's ceiling and any per-node override
    wins, and the caller is told which. The override is allowed to be *looser* as well as tighter,
    because a node that legitimately cycles more than the general ceiling should be able to say so
    in one place rather than force the pack's number up for every node.
    """
    pack_limit = ctx.policy.pack.attempt_limits.max_subgraph_reentries
    override = ctx.node_visit_budget.get(node)
    if override is None:
        return pack_limit, "policy.attempt_limits.max_subgraph_reentries"
    return override, f"GraphContext.node_visit_budget[{node!r}]"


def check_budgets(state: IncidentState, ctx: GraphContext, *, node: str) -> BudgetVerdict:
    """Evaluate every bound for one node entry. Pure -- reads state, writes nothing.

    Called *before* a node does its work, so the guard stops the incident at the boundary rather
    than after a further external call. The comparison is `>=` and not `>`: `node_visits` records
    visits already completed, so a node entering for the (limit + 1)th time sees `limit` recorded
    and must be stopped now.

    The order is widest first, and it decides which bound is *named* when two are spent at once.
    That is not arbitrary: on the remote loop -- P07 through P11 and back via `D10:retry_diagnosis`
    -- both cycle counters advance once per lap, but P07 runs earlier in the lap, so the diagnostic
    bound is the one the incident actually reached first and naming it is the true answer.
    """
    steps_limit, steps_owner = step_budget(ctx)
    steps_seen = total_steps(state)
    if steps_seen >= steps_limit:
        return BudgetVerdict(False, BudgetKind.TOTAL_STEPS, steps_seen, steps_limit, steps_owner)

    reentry_limit, reentry_owner = reentry_budget(ctx, node)
    visits_seen = visit_count(state, node)
    if visits_seen >= reentry_limit:
        return BudgetVerdict(
            False, BudgetKind.NODE_REENTRIES, visits_seen, reentry_limit, reentry_owner
        )

    diagnostic_seen = state.get("diagnostic_cycles", 0)
    if diagnostic_seen >= ctx.max_diagnostic_cycles:
        return BudgetVerdict(
            False,
            BudgetKind.DIAGNOSTIC_CYCLES,
            diagnostic_seen,
            ctx.max_diagnostic_cycles,
            "settings.max_diagnostic_cycles",
        )

    resolution_seen = state.get("resolution_cycles", 0)
    if resolution_seen >= ctx.max_resolution_cycles:
        return BudgetVerdict(
            False,
            BudgetKind.RESOLUTION_CYCLES,
            resolution_seen,
            ctx.max_resolution_cycles,
            "settings.max_resolution_cycles",
        )

    return BudgetVerdict(True)


def _escalation_event_id(incident_id: str, node: str, verdict: BudgetVerdict) -> str:
    """A stable id for one escalation, so a replayed node does not double-record it.

    Keyed on the observed count as well as the node: an incident that escalates, is resumed by a
    supervisor and then exhausts a *different* bound is two escalations and must appear as two.
    """
    material = f"{incident_id}\x1f{node}\x1f{verdict.kind}\x1f{verdict.observed}"
    return f"AUD-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def escalation_update(
    state: IncidentState, ctx: GraphContext, verdict: BudgetVerdict, *, node: str
) -> dict[str, Any]:
    """The partial state update that escalates an incident to a human.

    Raises on a passing verdict rather than returning an empty dict. A caller that reached here with
    a healthy incident has a routing bug, and quietly returning `{}` would let the graph continue
    while the caller believed it had stopped it.

    `escalated` is set but the incident is **not** terminated: `IncidentStatus.ESCALATED` moves
    onward to nine other statuses, because a supervisor who takes an incident over resumes it rather
    than filing it. The guard's job is to stop the machine, not to close the case.
    """
    if verdict.within_budget:
        raise ValueError(
            f"escalation_update called for node {node!r} on a passing verdict. The guard stops an "
            "incident only when a bound fired; reaching here otherwise means the caller routed to "
            "escalation without checking."
        )

    incident_id = state.get("incident_id") or ""
    now = ctx.clock.now()
    event = AuditEvent(
        event_id=_escalation_event_id(incident_id, node, verdict),
        incident_id=incident_id,
        occurred_at=now,
        actor=ctx.automation_actor,
        action="escalate",
        node=node,
        reason_code=ReasonCode.LOOP_LIMIT_REACHED,
        outcome="escalated",
        detail={
            "budget": str(verdict.kind),
            "observed": verdict.observed,
            "limit": verdict.limit,
            "owner": verdict.owner,
        },
        policy_version=ctx.policy.policy_version,
        correlation_id=state.get("correlation_id", ""),
    )
    return {
        "escalated": True,
        "escalation_reason": verdict.reason,
        "status": IncidentStatus.ESCALATED,
        "audit_events": [event],
        "updated_at": now,
    }


# ------------------------------------------------------------------------------------------------
# How the escalation reaches the edges
# ------------------------------------------------------------------------------------------------
#
# `escalation_update` stops a *node*; it does not stop the *graph*. Only D02 and D05 read
# `escalated`, so an incident that exhausted its budget at P04 walked five further super-steps
# before being diverted -- five further checkpoint writes and a recorded `total_steps` five past
# the limit that was supposed to have stopped it (measured; see `graph.builder`'s docstring).
#
# The remedy is to wire the flag onto every edge, and these three names are how. They live here
# rather than in `graph.builder` because the parent is no longer the only graph that needs them:
# each subgraph wires its own edges and would otherwise either import the builder -- a cycle, once
# the builder imports the subgraph -- or keep its own copy of the sentinel, which is how one half
# of the graph comes to have a guard the other half does not.


#: The branch every guarded edge takes when `escalated` is set. Spelled in LangGraph's own sentinel
#: style so it provably cannot collide with a specification answer; `builder._check_tables`
#: subtracts exactly this key before comparing a `path_map` against `Decision.branches`.
ESCALATED = "__escalated__"

#: The branch a plain edge takes when the guard has not fired. A plain edge has one destination, so
#: this exists only to give the two-way conditional edge a second key.
ONWARD = "__onward__"


def guarded(answer: Callable[[IncidentState], str]) -> Callable[[IncidentState], str]:
    """Wrap a router so the guard's verdict is read before the specification's question.

    Order matters, and it is the same order `route_identity_resolution` uses for the same reason: an
    incident that exhausted its budget and *then* produced a usable answer on its final pass has
    still exhausted its budget. Asking the question first would leave the escalation recorded in the
    audit trail but not acted on -- state and graph disagreeing about whether a human was involved.
    """

    def route(state: IncidentState) -> str:
        if state.get("escalated"):
            return ESCALATED
        return answer(state)

    return route


def straight_on(state: IncidentState) -> str:
    """The answer a plain edge gives. Total, like every router: reads nothing, cannot raise."""
    return ONWARD
