"""The parent graph: seventeen steps, nine subgraphs, seventeen decisions, and the edges between.

Those counts are measured rather than remembered -- 26 nodes read off
`compile_parent_graph().get_graph()` on 2026-08-23, and 17 questions off `DECISION_AFTER` followed
through `chain_from`. The line read "eleven steps, three subgraphs, twelve decisions" through six
stages being added. Seventeen is the count *this module* wires; the other seven of the
specification's twenty-four sit on a subgraph's own `add_conditional_edges` and are invisible from
here, which is what `chain_from`'s docstring means by "complete for the parent and silent about the
rest".

This module is the only place that knows what the workflow's *shape* is. `graph.nodes` knows what
each step does, `graph.routing` knows what each question asks, and neither knows what follows what.
That separation is what lets `routing.py` say -- as its docstring does -- that a branch "names the
answer, never the node the builder happens to wire it to".

Four tables and nothing else
----------------------------
The topology is data, not control flow, and it is spread over exactly four places:

* **`graph.nodes.PARENT_NODES`** -- the seventeen steps in specification order. Consecutive entries
  with no decision between them are joined by a plain edge, so the registry's *order* is what draws
  P01 -> P02, P06 -> P07, P08 -> P09 and P09 -> P10. Nothing here restates them.
* **`SUBGRAPH_NODES`** -- the stages compiled as graphs rather than written as functions.
  Deliberately *not* in `PARENT_NODES`, and not merely because `graph.nodes` says nothing beyond P11
  lives there: they have no order to be in. Each is reached by name from a branch, never by falling
  off the end of the one before, so listing them in sequence would invite the plain edge that
  `_plain_edges` draws between consecutive registry entries. Most are here because they own an
  interrupt and a paused stage should checkpoint its own resume point; `preventive_maintenance` has
  none and is here because it fans out internally from one answer of one decision, which is a shape
  the sequence cannot hold. `graph.subgraphs` states both reasons.
* **`DECISION_AFTER`** -- which decision is asked after which node.
* **`BRANCH_TARGETS`** -- where each answer goes.

A reader wanting the diagram reads those four; a reader wanting the behaviour reads the other two
modules. `_check_tables` refuses to build a graph whose tables disagree with either.

Why the answer-to-node mapping lives here and not on `Decision`
---------------------------------------------------------------
`Decision.branches` is the set of answers a router may give. Putting the destinations on it too
would make `routing.py` import node names, and a router that knows node names is one refactor away
from being written in terms of them. Instead the mapping is here and `_check_tables` asserts each
`path_map`'s keys against `Decision.branches` -- the check `routing.py` delegates by name. It fires
in both directions: an answer with no destination and a destination for an answer no router gives
are both build-time errors, not runtime surprises.

An answer may name another decision
-----------------------------------
`BRANCH_TARGETS["D07"]["continue"]` is `"D08"` rather than a node, and the specification is why. D07
ends "If no, continue to D08"; D08 ends "If no, evaluate remote repair"; D09 ends "If yes, continue
to P12. If no, continue to D11." Four questions in a row, with no step between any two of them.

LangGraph attaches a conditional edge to a *node*, so those four cannot be four
`add_conditional_edges` calls -- there is nothing to attach the second one to. `_cascade` composes
them instead: it asks D07, and while the answer names a decision it asks that one too, stopping at
the first answer that names a node or `END`. `_terminal_targets` is the union of those answers, and
it is the path map.

The composition is exact rather than an approximation, which is the whole of its justification.
**No node runs between the four**, so they read a state that is identical by construction -- and a
router's return value is never checkpointed, which `routing.py` opens by explaining, so four
separate edges would have recorded no more than this one does. Nothing is lost, and what is gained
is that the table can say what the specification says.

The failure mode it introduces instead is a chain that never terminates, which `_check_chains`
refuses at build time rather than letting `_cascade` spin.

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
Nothing, as of 2026-08-23. `PENDING_STAGES` is empty for the first time.

The mechanism stays, because it is the thing that made the list shrink rather than rot. An exit that
leaves this graph for a stage that does not exist goes to `END`, which is indistinguishable from a
successful run, so each such exit is named in `PENDING_STAGES` and `_check_tables` holds the two in
agreement in both directions: an exit that goes to `END` without an entry there, or an entry whose
exit no longer goes to `END`, both fail the build. Wiring a subgraph therefore cannot be done
without deleting its line, and deleting its line cannot be done without wiring it.

An empty table and a table nobody consults look identical from the outside, which is why
`_check_pending_stages` is asserted in both directions by a test that monkeypatches a gap into
existence rather than by the table's own contents.

Three nodes now end the workflow, and all three are declared in `DELIBERATE_TERMINALS` rather than
confessed in `PENDING_STAGES`. The last entry to leave was `__onward__:preventive_maintenance`, and
it left by being **disproved rather than built**: see that table for the measurement.
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
from lpr_cpe.graph.subgraphs import (
    compile_field_execution_graph,
    compile_field_planning_graph,
    compile_plant_execution_graph,
    compile_plant_referral_graph,
    compile_preventive_maintenance_graph,
    compile_reconciliation_closure_graph,
    compile_remote_resolution_graph,
    compile_restoration_validation_graph,
    compile_self_help_graph,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

# `ESCALATED`, `ONWARD`, `guarded` and `straight_on` used to be defined here. They moved to
# `graph.guards` when the first subgraph needed them: the guard owns the escalation, and these are
# how that escalation reaches an edge. Importing them here keeps them readable as
# `builder.ESCALATED` for anything that already refers to them that way.

# ------------------------------------------------------------------------------------------------
# The topology
# ------------------------------------------------------------------------------------------------

#: The stages that are compiled graphs rather than functions, and the factory for each. Added as
#: nodes exactly like `PARENT_NODES`' entries; the parent cannot tell the difference, and that is
#: the point of `compile_*` taking no checkpointer -- a subgraph node shares the parent's.
#:
#: Called at build time rather than held as already-compiled objects, so that two parent graphs get
#: two subgraphs. Sharing one compiled instance across parents would work today and is the kind of
#: thing that stops working the moment anything is cached on the compiled object.
SUBGRAPH_NODES: Mapping[str, Callable[[], Any]] = {
    "field_execution": compile_field_execution_graph,
    "field_planning": compile_field_planning_graph,
    "plant_execution": compile_plant_execution_graph,
    "plant_referral": compile_plant_referral_graph,
    "preventive_maintenance": compile_preventive_maintenance_graph,
    "reconciliation_closure": compile_reconciliation_closure_graph,
    "remote_resolution": compile_remote_resolution_graph,
    "restoration_validation": compile_restoration_validation_graph,
    "self_help": compile_self_help_graph,
}

#: The subgraphs a plain edge leaves, and where it goes. The third way a stage acquires a successor,
#: alongside a position in `PARENT_NODES` and an entry in `DECISION_AFTER`.
#:
#: Two entries, and the specification is why each exists rather than a `BRANCH_TARGETS` line: a
#: subgraph is not in `PARENT_NODES`, so it has no consecutive neighbour for `_plain_edges` to find,
#: and neither pair has a decision asked between them for `DECISION_AFTER` to name.
#:
#: The first is P16 into P17: commit the field action, then brief the technician. The edge fires on
#: **all** of `field_planning`'s exits, including the two that book nothing, and that is the design
#: rather than a tolerated imprecision. `field_execution.route_visit_gate` asks whether a work order
#: is open and answers `no_visit` when none is; only `commit_field_dispatch` writes `work_orders`,
#: so `queue_for_dispatcher` and `abandon_field_planning` arrive, record that there was nothing to
#: visit, and stop. Gating here instead would leave that arm unreachable, and an arm no state can
#: enter is an arm no test can hold to account.
#:
#: The second is P20 into P21 on D08's arm: raise the MR, then track it to repair. It fires on all
#: three of `plant_referral`'s exits for the same reason the first one does, and the receiving gate
#: is again what makes that right. `plant_execution` opens on `route_plant_gate`, which reads the MR
#: records; the referral's `abandon_plant_referral` files none, so an abandoned referral arrives
#: holding nothing -- but it also writes `IncidentStatus.ESCALATED`, and `guarded` sends an
#: escalated exit to `END` before this edge is consulted. The arm that survives to `plant_execution`
#: is therefore the one that filed, plus `already_referred`, which is an incident that already held
#: an MR and is exactly what that stage is for.
SUBGRAPH_SUCCESSOR: Mapping[str, str] = {
    "field_planning": "field_execution",
    "plant_referral": "plant_execution",
}

#: Which decision is asked after which node. A node absent from this mapping is followed by a plain
#: edge to whatever comes next in `PARENT_NODES`.
#:
#: The one entry worth checking against the specification is the first: **D01 follows P02, not
#: P01**. "Is the event valid and actionable?" is a question about the normalised event and its
#: data-quality score, and P02 is what computes that score -- asking before P02 would be asking
#: before there was anything to read. The specification's heading order says so and this table is
#: the only place the code does.
#:
#: The last three are Stage 3. **D07 is the head of a chain**, not the only question asked after
#: P11: D08, D09 and D11 follow it through `BRANCH_TARGETS` because nothing runs in between. See the
#: module docstring. D10 and D12 are asked after the two subgraph nodes, and they are asked *here*
#: rather than inside those subgraphs because every destination they have is a sibling the subgraph
#: does not contain -- which both subgraph modules say in their own docstrings.
DECISION_AFTER: Mapping[str, str] = {
    "normalize_event": "D01",
    "resolve_identity_and_topology": "D02",
    "deduplicate_and_correlate": "D03",
    "assess_impact_and_priority": "D04",
    "assemble_case_evidence": "D05",
    "determine_root_cause": "D06",
    "generate_resolution_options": "D07",
    "remote_resolution": "D10",
    "self_help": "D12",
    # Stage 4. D16 is asked *twice* -- once inside `field_execution` to pick its own ending, and
    # again here to pick the parent's. That is not a duplicated question but the only expressible
    # one: the subgraph has one exit and four dispositions to say through it, and `_check_tables`
    # admits only decisions that are in `routing.DECISIONS`, so no local gate could separate them
    # on this edge. The second reading is the first one's answer by construction -- of the four
    # nodes that can end the stage, none writes a `FieldFinding`, which is the only thing
    # `route_clean_boots_outcome` reads.
    "field_execution": "D16",
    "plant_execution": "D19",
    # Stage 5. D21 is asked here for the same reason D10 and D12 are: `continue_observation` re-runs
    # the window, but `retry_diagnosis` and `confirm_outcome` both land on parent nodes the subgraph
    # does not contain.
    "restoration_validation": "D21",
    "confirm_customer_outcome": "D22",
    # The two parent gates re-ask the decision that sent them the question. That is what closes
    # each loop: `approval_outstanding` is false once the answer is recorded, so the second pass
    # falls through to the clause that reads the answer, and the router picks the arm from it.
    "request_low_confidence_review": "D06",
    "request_blast_radius_approval": "D07",
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
#: * **D07 `continue` -> D08, D08 `continue` -> D09, D09 `self_help_check` -> D11.** A target that
#:   names a decision means "ask that next"; see the module docstring for why Stage 3's opening is a
#:   chain rather than four edges.
#: * **D10 `retry_diagnosis` -> P07 and D12 `retry_diagnosis` -> P10.** Not the same destination,
#:   and the specification asks for both. D10's remedy is "return to evidence assembly and
#:   root-cause analysis (P07 and P10)" -- a remote repair that did not hold means the device has
#:   changed since the evidence was gathered, so the evidence is re-gathered. D12's is "return to
#:   diagnosis (P10)": self-help changes nothing the diagnostic reads unless it worked, and it did
#:   not, so the same evidence supports a second opinion.
#: * **D08 `plant_path` -> `plant_referral`, and not to `plant_execution` directly.** The tempting
#:   edge is the short one: the fault is in a plant domain, so send it to the stage that owns plant
#:   work. It would arrive with no MR to chase. `route_plant_gate` reads `outstanding_plant_mr` and
#:   would answer `no_plant_action` on every one of these, so the arm would cross a stage that does
#:   nothing and reach D19 -- which is what it did before this edge existed, except that the arm
#:   ended at `END` instead. What is missing between D08 and P21 is P19 and P20: the MR does not
#:   exist yet, and P19's approval is what decides whether it may. `plant_referral` is those two
#:   steps, and `SUBGRAPH_SUCCESSOR` runs it into `plant_execution` once there is an MR to track.
#: * **D16 `delimit` -> `plant_execution` for three of `field_execution`'s four endings.** Only
#:   `close_clean_boots_visit` answers `validate`; the MR that was just filed, the handover that was
#:   abandoned and the visit that never opened all arrive at the plant stage. That is correct rather
#:   than merely total, because `route_plant_gate` asks whether there is an MR with OSP at all and
#:   answers `no_plant_action` for the two that filed none -- so they cross a stage that does
#:   nothing and reach D19, which routes them to P10 by the same `retry_diagnosis` arm that a
#:   rejected MR takes. The alternative was a local gate on this edge, and `_check_tables` refuses
#:   one: only decisions in `routing.DECISIONS` may sit on a parent edge.
#: * **D19 `await_plant` -> `plant_execution`, a self-loop.** The stage chases the MR, records what
#:   OSP said and is re-entered while the answer is still "with OSP". The bound is not here: every
#:   node in the subgraph calls `check_budgets` on entry, so the loop ends at the guard's ceiling.
#: * **D20 `reverse_handover` -> `field_planning`, not `field_execution`.** The specification says
#:   "returning to P17", which is inside `field_execution` -- but it also says, in the same list,
#:   "create or update a linked Clean Boots work order", and P17 books none. `open_field_visit`
#:   reads `open_work_order` and returns `no_visit` when there is none, and `file_plant_mr` has
#:   already completed the previous order, so an edge straight to P17 would arrive at a stage with
#:   nothing to open and leave through the arm for having nothing to do. `field_planning` books the
#:   order and `SUBGRAPH_SUCCESSOR` runs it into `field_execution`, which satisfies both lines.
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
        "preventive": "preventive_maintenance",
        "active": "create_or_attach_incident",
    },
    "D05": {
        "gather_more": "assemble_case_evidence",
        "manual_review": END,
        "continue": "create_diagnostic_test_plan",
    },
    "D06": {
        "approve_low_confidence": "prepare_low_confidence_review",
        "retry_diagnosis": "assemble_case_evidence",
        "continue": "generate_resolution_options",
    },
    "D07": {
        "approve_high_blast_radius": "prepare_blast_radius_approval",
        "escalate": "record_escalation",
        "continue": "D08",
    },
    "D08": {
        "plant_path": "plant_referral",
        "continue": "D09",
    },
    "D09": {
        "remote": "remote_resolution",
        "self_help_check": "D11",
    },
    "D10": {
        "verify": "restoration_validation",
        "retry_diagnosis": "assemble_case_evidence",
    },
    "D11": {
        "self_help": "self_help",
        "field_planning": "field_planning",
    },
    "D12": {
        "verify": "restoration_validation",
        "retry_diagnosis": "determine_root_cause",
        "field_planning": "field_planning",
    },
    "D16": {
        "validate": "restoration_validation",
        "delimit": "plant_execution",
    },
    "D19": {
        "restored": "D20",
        "await_plant": "plant_execution",
        "retry_diagnosis": "determine_root_cause",
    },
    "D20": {
        "reverse_handover": "field_planning",
        "verify": "restoration_validation",
    },
    "D21": {
        "continue_observation": "restoration_validation",
        "retry_diagnosis": "determine_root_cause",
        "confirm_outcome": "confirm_customer_outcome",
    },
    "D22": {
        "reconcile": "reconciliation_closure",
        "retry_diagnosis": "determine_root_cause",
    },
}

#: Every exit that reaches `END` because the work beyond it has not been written, spelled
#: `<source>:<answer>` and explained. `_check_tables` keeps this exactly in step with the tables
#: above; see the module docstring for why that is enforced rather than trusted.
#:
#: **Empty since 2026-08-23**, and the way the last entry left is worth more than the fact that it
#: did. `__onward__:preventive_maintenance` claimed the preventive stage owed an edge into
#: `field_planning` and was waiting only on somebody deciding what a preventive `ResolutionOption`
#: is. Measured, that edge cannot exist:
#:
#: * `field_planning` commits one action type. `is_dispatchable_option` is
#:   `requires_truck_roll and action_type is CREATE_WORK_ORDER`, and its own docstring says why the
#:   narrowing is load-bearing -- `wfm.create_work_order` refuses anything else by name.
#: * Across all fifteen `FaultDomain` members, **every domain `boundaries.crew_for` calls `DIRTY`
#:   offers no `CREATE_WORK_ORDER` at all**, and every domain that offers one is `CLEAN` or `JOINT`.
#:   The correspondence is exact, and it is the Clean/Dirty delimiter itself rather than a property
#:   of the catalogue: work upstream of the tap or ODP is an MR to OSP, and an MR is not a WFM work
#:   order.
#: * `plan_preventive_field_work` produces a `DIRTY` crew and nothing else -- measured over all 41
#:   fixtures by gap PREVENTIVE-1, and again here through the parent: three services take the arm,
#:   `SVC-PO-042-A-04` and `SVC-UT-001-A-03` on `distribution` and `SVC-VQ-002-A-01` on `power`.
#:
#: So P14 would find nothing to select for any arrival this arm can produce, `route_field_gate`
#: would answer `escalate`, and the edge would land every preventive case in
#: `abandon_field_planning` -- which writes `diagnosing`, and would then carry a service whose
#: disposition was "monitor it" onward through field execution, restoration validation and closure.
#: The parent cannot hold the other two dispositions back either: a conditional exit from a subgraph
#: needs a `routing.DECISIONS` member, the specification declares twenty-four, and none of them
#: follows D04's preventive arm.
#:
#: The disposition is therefore the end of the thread and `preventive_maintenance` is declared in
#: `DELIBERATE_TERMINALS`. What is genuinely missing is a preventive-maintenance queue that re-reads
#: the case -- gap PREVENTIVE-2, which no edge in this graph would have closed.
#:
#: `quarantine` and the two `manual_review`s were never here either. Those genuinely end the run:
#: D01's remedy is "do not create an incident", and `manual_review` is only ever reached after
#: `escalation_update` has already recorded the escalation and set `IncidentStatus.ESCALATED`. An
#: incident may be resumed from either by a supervisor re-invoking the thread; neither is waiting on
#: a stage that is missing.
PENDING_STAGES: Mapping[str, str] = {}


# ------------------------------------------------------------------------------------------------
# Build-time checks
# ------------------------------------------------------------------------------------------------


class GraphTopologyError(RuntimeError):
    """The tables above disagree with the node registry, the decision table, or each other."""


def _node_names() -> tuple[str, ...]:
    return tuple(name for name, _ in PARENT_NODES)


def _wired_node_names() -> frozenset[str]:
    """Every name the parent graph has a node under, ordered steps and subgraphs alike.

    What "is that a real destination?" means, and it is deliberately a set rather than a sequence:
    `_plain_edges` needs the registry's *order* to draw the edges between consecutive steps, and a
    subgraph has none. Keeping the two separate is what stops a subgraph from acquiring a plain edge
    into it by being written down next to something.
    """
    return frozenset(_node_names()) | frozenset(SUBGRAPH_NODES)


def _plain_edges() -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Every pair joined by a plain edge, and the nodes with no successor at all.

    Derived rather than listed. An eighteenth node inserted into the registry is wired by that edit
    alone; a hand-written edge list would have had to be found and updated, and the failure mode of
    forgetting is a node that is present in the graph and unreachable.

    Two things produce a pair, and only the first is positional. Consecutive entries in
    `PARENT_NODES` with no decision between them are the main line. `SUBGRAPH_SUCCESSOR` is the
    other, and it exists because a subgraph has no position for `pairwise` to read -- see
    `_wired_node_names` for why that set is a set -- so `field_planning -> field_execution` cannot
    be expressed by writing the two down next to each other, which is the whole point of keeping
    them out of the sequence.

    **A subgraph may also be terminal, and the two kinds of terminal are found differently.** An
    ordered step is terminal by being last in the sequence with no decision after it. A subgraph is
    terminal by having neither a `DECISION_AFTER` entry nor a `SUBGRAPH_SUCCESSOR` one. Two
    subgraphs are in this set, and re-measured on 2026-08-21 they are a different two than when this
    was written. `preventive_maintenance` is still one: D04's preventive arm creates a case and
    picks a disposition, and every disposition is the end of that thread's automated work.
    `reconciliation_closure` is the other, and it joined by being built rather than by lacking
    anything. `field_execution` has left: it answers D16, whose `validate` and `delimit` arms reach
    `restoration_validation` and `plant_execution`, and it was called terminal here because those
    two were unwritten, not because it ends anything. Five subgraphs are now kept out of the set by
    a `DECISION_AFTER` entry -- D10, D12, D16, D19 and D21 -- and two by `SUBGRAPH_SUCCESSOR`.

    Nothing about that absence is silent. A subgraph reaching `END` because somebody *forgot* its
    decision looks identical here to one that ends deliberately, so the distinction is not drawn
    here at all -- it is drawn between `PENDING_STAGES` and `DELIBERATE_TERMINALS`, which
    `_check_pending_stages` requires an entry in one or the other of for every terminal node, and
    refuses to let either go stale.

    **Both of the two are now in the second table, and that is new on 2026-08-23.** The sentence
    above about `preventive_maintenance` -- "every disposition is the end of that thread's automated
    work" -- was written while `PENDING_STAGES` simultaneously claimed the stage owed an edge into
    `field_planning`. The two readings sat a hundred lines apart in this file and contradicted each
    other for four revisions. `PENDING_STAGES` records which one measurement supports.
    """
    names = _node_names()
    pairs = tuple((left, right) for left, right in pairwise(names) if left not in DECISION_AFTER)
    last: tuple[str, ...] = (names[-1],) if names[-1] not in DECISION_AFTER else ()
    onward = tuple(SUBGRAPH_SUCCESSOR.items())
    subgraphs = tuple(
        name
        for name in SUBGRAPH_NODES
        if name not in DECISION_AFTER and name not in SUBGRAPH_SUCCESSOR
    )
    return pairs + onward, last + subgraphs


def chain_from(identifier: str) -> tuple[str, ...]:
    """Every decision reachable from this one without a node in between, the head first.

    A target that is itself a key of `BRANCH_TARGETS` names a question rather than a destination, so
    the chain is read off the table by following exactly those. `("D07", "D08", "D09", "D11")` for
    Stage 3's opening; `("D01",)` for a decision that chains to nothing, which is most of them.
    There are two chains rather than one: `("D19", "D20")` joined when the plant stages landed.

    Breadth-first and de-duplicating, because a chain is a graph and not a list: two answers of one
    decision may name the same next question, and D09's two arms already diverge. The de-duplication
    is not what makes a cycle safe -- `_check_chains` refuses those -- it is what keeps a diamond
    from being walked twice.

    Public, unlike the rest of this section, because chaining made `DECISION_AFTER` an incomplete
    answer to "which questions does this graph ask?" -- re-measured on 2026-08-21 it names thirteen
    of the seventeen wired here, and chaining recovers exactly D08, D09, D11 and D20. It no longer
    finishes that sentence, which is worth stating rather than renumbering past: seven of the
    twenty-four declared decisions -- D13 to D15, D17, D18, D23 and D24 -- are wired on a subgraph's
    own `add_conditional_edges` and appear in neither table, so `DECISION_AFTER` plus this function
    is complete for the parent and silent about the rest. `cli.report_topology` is the first reader
    and says so by printing both counts. The module docstring's "a reader wanting the diagram reads
    those four" now means: reads those four, following the targets that name decisions, and then
    reads the nine subgraphs.
    """
    order: list[str] = []
    pending = [identifier]
    while pending:
        current = pending.pop(0)
        if current in order or current not in BRANCH_TARGETS:
            continue
        order.append(current)
        pending.extend(t for t in BRANCH_TARGETS[current].values() if t in BRANCH_TARGETS)
    return tuple(order)


def _terminal_targets(identifier: str) -> dict[str, str]:
    """The path map for a chain: every answer that names a node or `END`, and where it goes.

    Answers that name another decision are not in it, because they are not destinations -- they are
    the chain itself, and `_cascade` has already followed them by the time an edge sees a return
    value.

    An answer appearing twice in one chain is a build-time error rather than a last-write-wins, and
    this is the one hazard the composition introduces that four separate edges would not have had.
    D10 and D12 both answer `retry_diagnosis` to different nodes and both are fine, because they are
    asked after different nodes and are therefore different edges. Two decisions *in one chain*
    answering `retry_diagnosis` would share one edge, and the branch could no longer say which
    question had been asked -- which is the property `routing.py` is written around.
    """
    out: dict[str, str] = {}
    origin: dict[str, str] = {}
    for current in chain_from(identifier):
        for answer, target in BRANCH_TARGETS[current].items():
            if target in BRANCH_TARGETS:
                continue
            if answer in out:
                raise GraphTopologyError(
                    f"{current} and {origin[answer]} are chained after one node and both answer "
                    f"'{answer}'. One conditional edge carries both, so the branch would no longer "
                    "name which question was asked."
                )
            out[answer] = target
            origin[answer] = current
    return out


def _cascade(identifier: str) -> Callable[[IncidentState], str]:
    """The edge function for a chain: ask each question in turn, return the first real answer.

    The composed router. It asks `identifier`, and while the answer names another decision it asks
    that one too, so what reaches the edge is always a key of `_terminal_targets(identifier)`.

    Composing is exact here, not an approximation, and the module docstring says why: no node runs
    between the questions, so each reads the state the one before it read, and a router's return
    value is never checkpointed. What the trace loses by having one edge instead of four is nothing
    it ever held.

    Wrapped in `guarded` by the caller, once, at the head. The guard's job is to stop the graph when
    the budget is spent, and asking the same guard again between two questions that share a state
    could only ever get the same answer.
    """

    def route(state: IncidentState) -> str:
        current = identifier
        while True:
            answer = DECISIONS[current].route(state)
            target = BRANCH_TARGETS[current][answer]
            if target not in BRANCH_TARGETS:
                return answer
            current = target

    return route


def _check_chains() -> None:
    """No chain loops back on itself, because `_cascade` would not return if one did.

    The failure this exists to convert. `_cascade`'s loop terminates on the first answer that names
    a node, so a table in which D09 answered back to D07 is not a wrong edge or a missing
    destination -- it is an edge function that spins, inside a super-step, with no checkpoint
    written and nothing in the log. Refusing it at build time costs one walk of a table with twelve
    entries in it.

    Depth-first over paths rather than a visited set, because the question is whether a decision can
    reach *itself*, and a shared decision that two arms both reach is legitimate.
    """
    for head in sorted(set(DECISION_AFTER.values())):
        stack: list[tuple[str, tuple[str, ...]]] = [(head, ())]
        while stack:
            current, path = stack.pop()
            if current in path:
                raise GraphTopologyError(
                    f"the decision chain after {head} loops: {' -> '.join([*path, current])}. "
                    "`_cascade` follows answers until one names a node, so this is an edge "
                    "function that never returns."
                )
            walked = (*path, current)
            stack.extend(
                (t, walked) for t in BRANCH_TARGETS[current].values() if t in BRANCH_TARGETS
            )


def _check_tables() -> None:
    """Refuse to build a graph whose four tables disagree. Every check fires in both directions.

    Called from `build_parent_graph` rather than at import. Import-time is right for a check whose
    inputs are all constants -- `graph.nodes` does one -- but this one reads `DECISIONS`, and a
    circular-import order in which `routing` is half-initialised would turn a topology error into an
    `AttributeError` from somewhere unhelpful.
    """
    known = _wired_node_names()

    unknown_sources = sorted(set(DECISION_AFTER) - known)
    if unknown_sources:
        raise GraphTopologyError(
            f"DECISION_AFTER names nodes that are not in the registry: {unknown_sources}"
        )

    not_subgraphs = sorted(set(SUBGRAPH_SUCCESSOR) - set(SUBGRAPH_NODES))
    if not_subgraphs:
        raise GraphTopologyError(
            f"SUBGRAPH_SUCCESSOR leaves nodes that are not subgraphs: {not_subgraphs}. An ordered "
            "step gets its successor from its position in PARENT_NODES; this table is only for the "
            "stages that have no position."
        )

    unknown_successors = sorted(set(SUBGRAPH_SUCCESSOR.values()) - known)
    if unknown_successors:
        raise GraphTopologyError(
            f"SUBGRAPH_SUCCESSOR routes to nodes that are not in the registry: {unknown_successors}"
        )

    # Both tables would draw an edge from the same node, and LangGraph would keep both: the plain
    # one, and the decision's path map. The run would take whichever the second call installed.
    doubly_wired = sorted(set(SUBGRAPH_SUCCESSOR) & set(DECISION_AFTER))
    if doubly_wired:
        raise GraphTopologyError(
            "these subgraphs have both a plain successor and a decision after them: "
            f"{doubly_wired}. Pick one -- a stage whose exit is conditional belongs in "
            "DECISION_AFTER, and one whose exit is unconditional belongs in SUBGRAPH_SUCCESSOR."
        )

    self_loops = sorted(name for name, target in SUBGRAPH_SUCCESSOR.items() if name == target)
    if self_loops:
        raise GraphTopologyError(
            f"SUBGRAPH_SUCCESSOR sends a subgraph to itself: {self_loops}. The guard's re-entry "
            "budget would be the only thing stopping the run."
        )

    # Both tables, not just `DECISION_AFTER`. A chained decision follows no node, so it reaches
    # `DECISIONS[identifier]` below without this having looked at it, and the error would be a
    # `KeyError` from the check rather than a sentence saying which table is wrong.
    wired_decisions = set(BRANCH_TARGETS) | set(DECISION_AFTER.values())
    missing_decisions = sorted(wired_decisions - set(DECISIONS))
    if missing_decisions:
        raise GraphTopologyError(
            f"decisions are wired here but are not in routing.DECISIONS: {missing_decisions}"
        )

    untargeted = sorted(set(DECISION_AFTER.values()) - set(BRANCH_TARGETS))
    if untargeted:
        raise GraphTopologyError(
            f"decisions are wired after a node but have no BRANCH_TARGETS entry: {untargeted}. "
            "A decision with no destinations cannot be an edge."
        )

    _check_chains()

    # Reachable, not just wired: D08 follows no node, and is not an orphan -- D07 answers to it.
    # Computed from `chain_from` rather than from `BRANCH_TARGETS`' values directly, so that a
    # chain hanging off nothing is caught however long it is.
    reachable = {d for head in DECISION_AFTER.values() for d in chain_from(head)}
    orphan_targets = sorted(set(BRANCH_TARGETS) - reachable)
    if orphan_targets:
        raise GraphTopologyError(
            f"BRANCH_TARGETS has destinations for decisions nothing asks: {orphan_targets}. Either "
            "wire the decision in DECISION_AFTER, answer to it from one that is, or delete its "
            "destinations."
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
        # `- set(BRANCH_TARGETS)`: a target naming a decision is a chain link, checked above by
        # `_check_chains` and resolved by `_cascade`, not a node that has gone missing.
        strangers = sorted(set(targets.values()) - known - {END} - set(BRANCH_TARGETS))
        if strangers:
            raise GraphTopologyError(
                f"{identifier} routes to nodes that are not in the registry: {strangers}"
            )

    for head in set(DECISION_AFTER.values()):
        _terminal_targets(head)

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
    gaps |= {f"{ONWARD}:{name}" for name in terminal_nodes if name not in DELIBERATE_TERMINALS}

    undeclared = sorted(gaps - set(PENDING_STAGES))
    if undeclared:
        raise GraphTopologyError(
            f"these exits reach END with nothing to explain them: {undeclared}. A run that stops "
            "there looks like a run that finished. Add a PENDING_STAGES entry saying what is "
            "missing, or -- if the run really does end there -- declare it: an entry named "
            f"`Dnn:answer` belongs in _DELIBERATE_ENDINGS, one named `{ONWARD}:node` in "
            "DELIBERATE_TERMINALS."
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

#: Nodes whose `END` is the real end of the workflow. The node-shaped half of `_DELIBERATE_ENDINGS`.
#:
#: Needed because the two halves of `_check_pending_stages` derive their gaps differently. A branch
#: answer is excused by name; a *terminal node* -- one `_plain_edges` finds no successor for -- had
#: no way to be excused at all, so the only way to declare one was a `PENDING_STAGES` line saying
#: work was owed. For `record_escalation` that would be false: nothing is missing after it.
#:
#: Public, unlike `_DELIBERATE_ENDINGS`, because with `PENDING_STAGES` empty this is the only table
#: that answers "where does a run legitimately stop?" -- `cli.report_topology` prints it, and the
#: three entries are three different ways to stop rather than one repeated.
DELIBERATE_TERMINALS: frozenset[str] = frozenset(
    {
        # The incident is a human's now. `IncidentStatus.ESCALATED` moves onward to nine other
        # statuses, so a supervisor resumes the thread; there is no next node for the graph to run.
        "record_escalation",
        # The last stage of the lifecycle. Its main line ends at `update_kpis_and_learning`, which
        # writes `IncidentStatus.CLOSED` -- and `domain.lifecycle` gives `closed` no outward
        # transition, so there is not merely no next node but no legal one. Its other two exits end
        # for reasons of their own: `abandon_closure` has escalated, and D23's exhausted-retry arm
        # escalates through the same guard.
        "reconciliation_closure",
        # The third way, and the only one that is neither an escalation nor a closure: D04's
        # preventive arm never opens an incident, so there is no incident here to escalate or to
        # close. It creates a preventive-maintenance case, picks a disposition, and the disposition
        # is the end of that thread's automated work -- which is the whole of what the
        # specification's D04 asks for ("create or update", "select", "keep it linked").
        #
        # This entry replaced the last `PENDING_STAGES` line rather than being added beside it. The
        # line claimed the arm owed an edge into `field_planning`; that table records why no such
        # edge can exist, and D8 in IMPLEMENTATION_PLAN.md is the same argument from the other end
        # -- a case whose `recommended_window` is `next_maintenance_window` must not hold a
        # LangGraph thread open for a week waiting for it.
        "preventive_maintenance",
    }
)


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

    # A compiled graph is a runnable, so `add_node` takes one exactly as it takes a function and the
    # parent has no way to tell which it got. What the subgraph does *not* get here is a
    # checkpointer: a node compiled with its own would write its state somewhere the parent's thread
    # cannot reach, and `graph.inspect` reads a paused stage through `subgraphs=True` on the
    # parent's.
    for name, compile_subgraph in SUBGRAPH_NODES.items():
        graph.add_node(name, compile_subgraph())

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
        #
        # `_terminal_targets` and `_cascade` rather than the table and the router: for the nine
        # decisions that chain to nothing these are the table and the router, and for D07 they are
        # the four questions the specification asks after P11 with no step between them.
        path_map: dict[Hashable, str] = dict(_terminal_targets(identifier).items())
        path_map[ESCALATED] = END
        graph.add_conditional_edges(source, guarded(_cascade(identifier)), path_map)

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
