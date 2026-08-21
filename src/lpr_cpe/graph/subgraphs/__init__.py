"""The branch stages, each compiled as its own graph.

A stage lives here rather than in `graph.nodes` for one of two reasons, and the first is the
interrupt. A paused graph checkpoints *its own* state, and on resume LangGraph re-enters the task
that raised. Put a gate directly in the parent and the parent is what pauses, so the parent's
checkpoint becomes the resume point for a question that belongs to one stage of one branch -- and
every later stage inherits a parent whose status field says `awaiting_approval` about a decision it
does not own. `remote_resolution`, `self_help`, `field_planning` and `field_execution` are here for
that reason.

The second is branching. `graph.nodes.PARENT_NODES` is a *sequence*: `builder._plain_edges` draws an
edge between consecutive entries, so a stage written there is a stage on the main line. A stage that
is reached from one answer of one decision and fans out internally has no place in that sequence at
all -- listing it would invite the plain edge that its position implies. `preventive_maintenance` is
here for that reason and holds no interrupt at all, which is why this docstring no longer says every
module here has one.

A stage may have a plain successor without being in that sequence, which is what
`builder.SUBGRAPH_SUCCESSOR` is for: `field_planning` runs into `field_execution` because the
specification puts no decision between P16 and P17. The edge fires on all three of planning's exits,
and `field_execution.route_visit_gate` is what makes that correct rather than merely convenient --
only `commit_field_dispatch` books a work order, and the arm for finding none is a real arm with a
real test rather than a case the parent quietly excluded.

Nesting also buys the property `graph.interrupts` documents at length and `graph.inspect` exists to
work around: a paused subgraph's writes have not reached the parent, so the parent alone understates
what is happening. That is a cost, not a benefit, but it is the cost of having each stage own its
own resume point, and the alternative -- one flat graph with six interrupts in it -- pays it at the
parent instead, where it is worse.

Each module here exports a `build_*` returning the uncompiled `StateGraph` and a `compile_*`. The
parent wires the compiled form as a single node; `graph.builder.PENDING_STAGES` names every stage
that is written but not yet wired, and fails the build in both directions so neither half can be
forgotten.
"""

from __future__ import annotations

from lpr_cpe.graph.subgraphs.field_execution import (
    FIELD_EXECUTION_NODES,
    build_field_execution_graph,
    compile_field_execution_graph,
)
from lpr_cpe.graph.subgraphs.field_planning import (
    FIELD_PLANNING_NODES,
    build_field_planning_graph,
    compile_field_planning_graph,
)
from lpr_cpe.graph.subgraphs.plant_execution import (
    PLANT_EXECUTION_NODES,
    build_plant_execution_graph,
    compile_plant_execution_graph,
)
from lpr_cpe.graph.subgraphs.plant_referral import (
    PLANT_REFERRAL_NODES,
    build_plant_referral_graph,
    compile_plant_referral_graph,
)
from lpr_cpe.graph.subgraphs.preventive_maintenance import (
    PREVENTIVE_MAINTENANCE_NODES,
    build_preventive_maintenance_graph,
    compile_preventive_maintenance_graph,
)
from lpr_cpe.graph.subgraphs.reconciliation_closure import (
    RECONCILIATION_CLOSURE_NODES,
    build_reconciliation_closure_graph,
    compile_reconciliation_closure_graph,
)
from lpr_cpe.graph.subgraphs.remote_resolution import (
    REMOTE_RESOLUTION_NODES,
    build_remote_resolution_graph,
    compile_remote_resolution_graph,
)
from lpr_cpe.graph.subgraphs.restoration_validation import (
    RESTORATION_VALIDATION_NODES,
    build_restoration_validation_graph,
    compile_restoration_validation_graph,
)
from lpr_cpe.graph.subgraphs.self_help import (
    SELF_HELP_NODES,
    build_self_help_graph,
    compile_self_help_graph,
)

__all__ = [
    "FIELD_EXECUTION_NODES",
    "FIELD_PLANNING_NODES",
    "PLANT_EXECUTION_NODES",
    "PLANT_REFERRAL_NODES",
    "PREVENTIVE_MAINTENANCE_NODES",
    "RECONCILIATION_CLOSURE_NODES",
    "REMOTE_RESOLUTION_NODES",
    "RESTORATION_VALIDATION_NODES",
    "SELF_HELP_NODES",
    "build_field_execution_graph",
    "build_field_planning_graph",
    "build_plant_execution_graph",
    "build_plant_referral_graph",
    "build_preventive_maintenance_graph",
    "build_reconciliation_closure_graph",
    "build_remote_resolution_graph",
    "build_restoration_validation_graph",
    "build_self_help_graph",
    "compile_field_execution_graph",
    "compile_field_planning_graph",
    "compile_plant_execution_graph",
    "compile_plant_referral_graph",
    "compile_preventive_maintenance_graph",
    "compile_reconciliation_closure_graph",
    "compile_remote_resolution_graph",
    "compile_restoration_validation_graph",
    "compile_self_help_graph",
]
