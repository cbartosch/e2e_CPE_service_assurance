"""The stages that contain an interrupt, each compiled as its own graph.

Everything from stage 3 onwards lives here rather than in `graph.nodes`, and the reason is the
interrupt. A paused graph checkpoints *its own* state, and on resume LangGraph re-enters the task
that raised. Put a gate directly in the parent and the parent is what pauses, so the parent's
checkpoint becomes the resume point for a question that belongs to one stage of one branch -- and
every later stage inherits a parent whose status field says `awaiting_approval` about a decision it
does not own.

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

from lpr_cpe.graph.subgraphs.remote_resolution import (
    REMOTE_RESOLUTION_NODES,
    build_remote_resolution_graph,
    compile_remote_resolution_graph,
)
from lpr_cpe.graph.subgraphs.self_help import (
    SELF_HELP_NODES,
    build_self_help_graph,
    compile_self_help_graph,
)

__all__ = [
    "REMOTE_RESOLUTION_NODES",
    "SELF_HELP_NODES",
    "build_remote_resolution_graph",
    "build_self_help_graph",
    "compile_remote_resolution_graph",
    "compile_self_help_graph",
]
