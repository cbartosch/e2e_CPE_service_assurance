"""Reading an incident's real state, including the part of it that lives inside a paused subgraph.

The specification asks for state-inspection endpoints. The naive implementation of one --
`(await app.aget_state(config)).values` -- is wrong in the single case anybody actually calls it
for. Measured on langgraph 1.2.11 with the incident paused at a nested approval gate:

    parent  .values                                       status=dispatch_planning  pending=None
    task    .tasks[0].state.values (subgraphs=True)        status=awaiting_approval  pending=set

A subgraph's writes reach the parent when the subgraph node completes. A paused one has not
completed, so for exactly as long as a human is being waited on, the parent's state understates
what is happening. An endpoint built on the parent alone would report "dispatch planning" for an
incident that has been sitting on someone's approval queue since Tuesday.

Every function here therefore reads through the boundary. They take the compiled app rather than a
state mapping, because the information simply is not in the mapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from langchain_core.runnables import RunnableConfig

from lpr_cpe.domain.governance import ApprovalRequest
from lpr_cpe.graph.state import IncidentState

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.pregel.types import StateSnapshot


async def _snapshots(
    app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig
) -> list[StateSnapshot]:
    """The parent snapshot followed by each paused child's, outermost first.

    Order matters: callers merge these, and a child that is currently running holds the newer view
    of any field both wrote. Outermost first means the merge overwrites with the innermost value.
    """
    root = await app.aget_state(config, subgraphs=True)
    out = [root]
    pending = list(root.tasks)
    while pending:
        task = pending.pop(0)
        child = getattr(task, "state", None)
        if child is not None and hasattr(child, "values"):
            out.append(child)
            pending.extend(getattr(child, "tasks", ()) or ())
    return out


async def effective_state(
    app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig
) -> IncidentState:
    """The incident's state as it actually stands, merged across any paused subgraph.

    This is what a state-inspection endpoint should return. Fields written by a paused subgraph
    override the parent's, because the subgraph holds the more recent value -- the parent's copy is
    simply the one from before the stage was entered.

    Note the asymmetry with `graph.state`'s reducers: this is a *read-side* merge for display and
    routing decisions made outside the graph. It does not run the reducers and must not be written
    back into the graph, which would replay writes the checkpoint already holds.
    """
    merged: dict[str, Any] = {}
    for snapshot in await _snapshots(app, config):
        merged.update(snapshot.values or {})
    return cast(IncidentState, merged)


async def pending_approval_for(
    app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig
) -> ApprovalRequest | None:
    """The approval this incident is currently blocked on, or `None` if it is not blocked.

    `None` means *not waiting*, not *unknown* -- there is no third answer, because a paused gate
    always wrote `pending_approval` before pausing (`interrupts.prepare_approval`).
    """
    for snapshot in reversed(await _snapshots(app, config)):
        found = (snapshot.values or {}).get("pending_approval")
        if isinstance(found, ApprovalRequest):
            return found
    return None


async def interrupt_payloads(
    app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig
) -> list[dict[str, Any]]:
    """Every outstanding question, as `{"id": ..., "value": ...}`.

    The `id` is what `Command(resume={id: answer})` needs, so this is the shape the API returns to
    whoever will answer. Read from the parent snapshot: LangGraph surfaces a nested interrupt on the
    parent even though it does not surface the nested *state*, which is the one asymmetry in this
    area that works in a caller's favour.
    """
    root = await app.aget_state(config, subgraphs=True)
    return [{"id": i.id, "value": i.value} for i in root.interrupts]


async def is_awaiting_human(app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig) -> bool:
    """Whether the incident is paused on an approval.

    Defined on the presence of an interrupt rather than on `status == AWAITING_APPROVAL`. The status
    is a field some node wrote and a later node will overwrite; the interrupt is the thing that
    actually stops the graph, and the two can disagree if a gate is added without its `prepare`.
    """
    return bool(await interrupt_payloads(app, config))
