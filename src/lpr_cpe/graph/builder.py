"""The parent graph: eleven nodes, six decisions, and the edges between them.

This module is the only place that knows what the workflow's *shape* is. `graph.nodes` knows what
each step does, `graph.routing` knows what each question asks, and neither knows what follows what.
That separation is what lets `routing.py` say -- as its docstring does -- that a branch "names the
answer, never the node the builder happens to wire it to".

Three tables and nothing else
-----------------------------
The topology is data, not control flow, and it is spread over exactly three places:

* **`graph.nodes.PARENT_NODES`** -- the eleven steps in specification order. Consecutive entries
  with no decision between them are joined by a plain edge, so the registry's *order* is what draws
  P01 -> P02, P06 -> P07, P08 -> P09 and P09 -> P10. Nothing here restates them.
* **`DECISION_AFTER`** -- which of the six parent decisions follows which node.
* **`BRANCH_TARGETS`** -- where each answer goes.

A reader wanting the diagram reads those three; a reader wanting the behaviour reads the other two
modules. `_check_tables` refuses to build a graph whose tables disagree with either.

Why the answer-to-node mapping lives here and not on `Decision`
---------------------------------------------------------------
`Decision.branches` is the set of answers a router may give. Putting the destinations on it too
would make `routing.py` import node names, and a router that knows node names is one refactor away
from being written in terms of them. Instead the mapping is here and `_check_tables` asserts each
`path_map`'s keys against `Decision.branches` -- the check `routing.py` delegates by name. It fires
in both directions: an answer with no destination and a destination for an answer no router gives
are both build-time errors, not runtime surprises.

The escalation edge
-------------------
`routing.py`: *"the escalation edge is wired by `graph.builder` from the guard rather than chosen
here."* This is it, and it is wired on **every** edge out of a node, plain or conditional.

Uniform, because the alternative was measured and is worse. `escalation_update` sets `escalated`
and stops the node's body from running, but on its own that does not stop the *graph*: only D02 and
D05 read the flag, so an incident whose budget ran out at P04 continued through D03, P05, D04, P06
and P07 before D05 diverted it -- five further super-steps, five further checkpoint writes, and a
recorded `total_steps` five past the limit that was supposed to have stopped it. An operator reading
"limit 60, observed 65" is entitled to ask what the limit was for.

`ESCALATED` is spelled `__escalated__`, in LangGraph's own sentinel style, so that it provably
cannot collide with a specification answer -- `_check_tables` subtracts exactly this one key before
comparing against `Decision.branches`, and a router that somehow returned it would be routed to the
same place the guard sends everything else.

What is not wired yet
---------------------
Three exits leave this graph for stages that do not exist. They go to `END`, which would otherwise
be indistinguishable from a successful run, so each is named in `PENDING_STAGES` and `_check_tables`
holds the two in agreement in both directions: an exit that goes to `END` without an entry there,
or an entry whose exit no longer goes to `END`, both fail the build. Wiring a subgraph therefore
cannot be done without deleting its line, and deleting its line cannot be done without wiring it.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.guards import ESCALATED, ONWARD, guarded, straight_on
from lpr_cpe.graph.nodes import PARENT_NODES
from lpr_cpe.graph.routing import DECISIONS
from lpr_cpe.graph.state import IncidentState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

# `ESCALATED`, `ONWARD`, `guarded` and `straight_on` used to be defined here. They moved to
# `graph.guards` when the first subgraph needed them: the guard owns the escalation, and these are
# how that escalation reaches an edge. Importing them here keeps them readable as
# `builder.ESCALATED` for anything that already refers to them that way.

# ------------------------------------------------------------------------------------------------
# The topology
# ------------------------------------------------------------------------------------------------

#: Which decision follows which node. A node absent from this mapping is followed by a plain edge to
#: whatever comes next in `PARENT_NODES`.
#:
#: The one entry worth checking against the specification is the first: **D01 follows P02, not
#: P01**. "Is the event valid and actionable?" is a question about the normalised event and its
#: data-quality score, and P02 is what computes that score -- asking before P02 would be asking
#: before there was anything to read. The specification's heading order says so and this table is
#: the only place the code does.
DECISION_AFTER: Mapping[str, str] = {
    "normalize_event": "D01",
    "resolve_identity_and_topology": "D02",
    "deduplicate_and_correlate": "D03",
    "assess_impact_and_priority": "D04",
    "assemble_case_evidence": "D05",
    "determine_root_cause": "D06",
}

#: Where each answer goes. Keys are checked against `Decision.branches`; values against the node
#: registry. Notes on the four that are not simply "the next step":
#:
#: * **D02 `enrich` -> P03.** The bounded enrichment retry. The bound is not here and not in the
#:   router: P03 calls `check_budgets` on entry, so the loop terminates at the guard's ceiling and
#:   surfaces as `manual_review` on the next pass.
#: * **D03 `associate` and `continue` both -> P05.** Not a mistake and not a redundant branch. The
#:   specification's "if yes" remedy ends "continue to impact assessment for the affected customer",
#:   which is the same step the "no" arm reaches. The two answers differ in what P04 *recorded*, not
#:   in where the graph goes: `associate` means P04 wrote a parent record into `linked_records`, and
#:   P06 reads that to attach rather than create. Collapsing them into one plain edge would delete
#:   the distinction from the graph, and D03 would stop appearing in the trace at all.
#: * **D05 `gather_more` -> P07 and D06 `retry_diagnosis` -> P07.** Both re-enter the evidence stage
#:   rather than jumping to P08 or P09, because both mean the evidence itself is inadequate; a
#:   re-test over the same stale snapshots would return the same answer.
#: * **D06 `retry_diagnosis` -> P07, not P10.** A rejected low-confidence RCA is not re-derivable
#:   from the evidence that produced it. See `route_rca_confidence`.
BRANCH_TARGETS: Mapping[str, Mapping[str, str]] = {
    "D01": {
        "quarantine": END,
        "continue": "resolve_identity_and_topology",
    },
    "D02": {
        "enrich": "resolve_identity_and_topology",
        "manual_review": END,
        "continue": "deduplicate_and_correlate",
    },
    "D03": {
        "associate": "assess_impact_and_priority",
        "continue": "assess_impact_and_priority",
    },
    "D04": {
        "preventive": END,
        "active": "create_or_attach_incident",
    },
    "D05": {
        "gather_more": "assemble_case_evidence",
        "manual_review": END,
        "continue": "create_diagnostic_test_plan",
    },
    "D06": {
        "approve_low_confidence": END,
        "retry_diagnosis": "assemble_case_evidence",
        "continue": "generate_resolution_options",
    },
}

#: Every exit that reaches `END` because the work beyond it has not been written, spelled
#: `<source>:<answer>` and explained. `_check_tables` keeps this exactly in step with the tables
#: above; see the module docstring for why that is enforced rather than trusted.
#:
#: `quarantine` and the two `manual_review`s are **not** here. Those genuinely end the run: D01's
#: remedy is "do not create an incident", and `manual_review` is only ever reached after
#: `escalation_update` has already recorded the escalation and set `IncidentStatus.ESCALATED`. An
#: incident may be resumed from either by a supervisor re-invoking the thread; neither is waiting on
#: a stage that is missing.
PENDING_STAGES: Mapping[str, str] = {
    "D04:preventive": (
        "the preventive-maintenance subgraph -- D04's 'predictive risk without current service "
        "impact' arm, which creates or updates a PM case and selects remote prevention, planned "
        "Clean Boots work, planned Dirty Boots work, or monitoring"
    ),
    "D06:approve_low_confidence": (
        "the L2/SME review interrupt -- a human-approval interruption that resumes the same "
        "incident thread with the reviewer's structured response"
    ),
    f"{ONWARD}:generate_resolution_options": (
        "Stage 3, select and execute the resolution, which begins at D07"
    ),
}


# ------------------------------------------------------------------------------------------------
# Build-time checks
# ------------------------------------------------------------------------------------------------


class GraphTopologyError(RuntimeError):
    """The tables above disagree with the node registry, the decision table, or each other."""


def _node_names() -> tuple[str, ...]:
    return tuple(name for name, _ in PARENT_NODES)


def _plain_edges() -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """The consecutive pairs `PARENT_NODES` joins directly, and the nodes with no successor at all.

    Derived rather than listed. A twelfth node inserted into the registry is wired by that edit
    alone; a hand-written edge list would have had to be found and updated, and the failure mode of
    forgetting is a node that is present in the graph and unreachable.
    """
    names = _node_names()
    pairs = tuple((left, right) for left, right in pairwise(names) if left not in DECISION_AFTER)
    terminal = (names[-1],) if names[-1] not in DECISION_AFTER else ()
    return pairs, terminal


def _check_tables() -> None:
    """Refuse to build a graph whose three tables disagree. Every check fires in both directions.

    Called from `build_parent_graph` rather than at import. Import-time is right for a check whose
    inputs are all constants -- `graph.nodes` does one -- but this one reads `DECISIONS`, and a
    circular-import order in which `routing` is half-initialised would turn a topology error into an
    `AttributeError` from somewhere unhelpful.
    """
    known = set(_node_names())

    unknown_sources = sorted(set(DECISION_AFTER) - known)
    if unknown_sources:
        raise GraphTopologyError(
            f"DECISION_AFTER names nodes that are not in the registry: {unknown_sources}"
        )

    missing_decisions = sorted(set(DECISION_AFTER.values()) - set(DECISIONS))
    if missing_decisions:
        raise GraphTopologyError(
            f"DECISION_AFTER names decisions that are not in routing.DECISIONS: {missing_decisions}"
        )

    untargeted = sorted(set(DECISION_AFTER.values()) - set(BRANCH_TARGETS))
    if untargeted:
        raise GraphTopologyError(
            f"decisions are wired after a node but have no BRANCH_TARGETS entry: {untargeted}. "
            "A decision with no destinations cannot be an edge."
        )

    orphan_targets = sorted(set(BRANCH_TARGETS) - set(DECISION_AFTER.values()))
    if orphan_targets:
        raise GraphTopologyError(
            f"BRANCH_TARGETS has destinations for decisions that follow no node: {orphan_targets}. "
            "Either wire the decision in DECISION_AFTER or delete its destinations."
        )

    # The check `routing.py` delegates by name: "graph.builder owns the answer-to-node mapping and
    # asserts each path_map's keys against Decision.branches below, so the two cannot drift."
    for identifier, targets in BRANCH_TARGETS.items():
        declared = set(DECISIONS[identifier].branches)
        wired = set(targets)
        if wired != declared:
            raise GraphTopologyError(
                f"{identifier} wires {sorted(wired)} but routing.DECISIONS declares "
                f"{sorted(declared)}. An answer with no destination is an unreachable branch; a "
                "destination for an answer no router gives is a dead edge."
            )
        strangers = sorted(set(targets.values()) - known - {END})
        if strangers:
            raise GraphTopologyError(
                f"{identifier} routes to nodes that are not in the registry: {strangers}"
            )

    _check_pending_stages()


def _check_pending_stages() -> None:
    """`PENDING_STAGES` names every `END` that is a gap, and only those.

    Both directions, because only one of them is the direction a future edit gets wrong. Forgetting
    to *add* an entry is caught the moment somebody routes a new answer to `END`; forgetting to
    *remove* one is caught when a subgraph is finally wired and its line here goes stale. The second
    is the one that would otherwise survive, because nothing else in the codebase would notice.
    """
    _, terminal_nodes = _plain_edges()
    gaps = {
        f"{identifier}:{answer}"
        for identifier, targets in BRANCH_TARGETS.items()
        for answer, destination in targets.items()
        if destination == END and answer not in _DELIBERATE_ENDINGS.get(identifier, frozenset())
    }
    gaps |= {f"{ONWARD}:{name}" for name in terminal_nodes}

    undeclared = sorted(gaps - set(PENDING_STAGES))
    if undeclared:
        raise GraphTopologyError(
            f"these exits reach END with nothing to explain them: {undeclared}. A run that stops "
            "there looks like a run that finished. Add a PENDING_STAGES entry saying what is "
            "missing, or add the entry to _DELIBERATE_ENDINGS if the run really does end there."
        )

    stale = sorted(set(PENDING_STAGES) - gaps)
    if stale:
        raise GraphTopologyError(
            f"PENDING_STAGES still lists exits that no longer reach END: {stale}. The stage was "
            "wired; delete its line."
        )


#: The answers whose `END` is the real end of the workflow rather than a missing stage. Kept as a
#: table so that the reason for each is stated once, next to the check that reads it, rather than
#: inferred from the absence of a `PENDING_STAGES` line.
_DELIBERATE_ENDINGS: Mapping[str, frozenset[str]] = {
    # "Quarantine it. [...] Do not create an incident." There is no onward step to build.
    "D01": frozenset({"quarantine"}),
    # Only reachable when the guard has already escalated and recorded it; a supervisor resumes the
    # thread rather than the graph carrying on.
    "D02": frozenset({"manual_review"}),
    "D05": frozenset({"manual_review"}),
}


# ------------------------------------------------------------------------------------------------
# The builder
# ------------------------------------------------------------------------------------------------


def build_parent_graph() -> StateGraph[IncidentState, GraphContext, IncidentState, IncidentState]:
    """Assemble the parent graph, uncompiled.

    Returned uncompiled because two callers need it that way: `compile_parent_graph` adds a
    checkpointer, and the documentation build renders the topology without wanting a runtime. A
    single function returning a compiled graph would force the second to compile one and throw it
    away.

    `context_schema=GraphContext` is what makes `get_runtime(GraphContext).context` work inside a
    node. Without it every node raises on its first line, which is the failure the `@node`
    decorator's tests could not see: they call `fn.__wrapped__` and never enter the wrapper.
    """
    _check_tables()

    graph: StateGraph[IncidentState, GraphContext, IncidentState, IncidentState] = StateGraph(
        IncidentState, context_schema=GraphContext
    )

    for name, fn in PARENT_NODES:
        graph.add_node(name, fn)

    graph.add_edge(START, _node_names()[0])

    plain, terminal = _plain_edges()
    for left, right in plain:
        graph.add_conditional_edges(left, guarded(straight_on), {ESCALATED: END, ONWARD: right})
    for name in terminal:
        graph.add_conditional_edges(name, guarded(straight_on), {ESCALATED: END, ONWARD: END})

    for source, identifier in DECISION_AFTER.items():
        # `dict[Hashable, str]` and not `dict[str, str]`, because `dict` is invariant in its key
        # type and `add_conditional_edges` declares the parameter as `dict[Hashable, str]`. Every
        # key here really is a `str`; the wider annotation is for the call, not the contents.
        # Built through `.items()` rather than `{**table, ...}` for the same invariance: `**`
        # unpacking asks for a `SupportsKeysAndGetItem[Hashable, str]`, which a `Mapping[str, str]`
        # is not, while `Iterable[tuple[str, str]]` is an `Iterable[tuple[Hashable, str]]`.
        path_map: dict[Hashable, str] = dict(BRANCH_TARGETS[identifier].items())
        path_map[ESCALATED] = END
        graph.add_conditional_edges(source, guarded(DECISIONS[identifier].route), path_map)

    return graph


def compile_parent_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[IncidentState, GraphContext, IncidentState, IncidentState]:
    """Compile the parent graph, with a checkpointer if one is supplied.

    `None` is a real option and not just a test convenience: the graph is invoked in-process by the
    CLI and by the documentation examples, neither of which resumes anything. What a missing
    checkpointer costs is exactly the resumable half of the system -- no interrupt can be resumed,
    no thread can be re-read -- so the API always supplies one.
    """
    return build_parent_graph().compile(checkpointer=checkpointer, name="lpr_cpe_parent")
