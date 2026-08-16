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


async def awaiting_node_path(
    app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig
) -> tuple[str, ...]:
    """Which node is asking, named from the outside in -- `("gate", "ask")`. Empty when none is.

    The question a console asks first and the one the interrupt itself cannot answer. Measured on
    langgraph 1.2.11: `Interrupt` carries `id` and `value` and nothing else. The `id` is an opaque
    digest (`5ed6ae3c455c6fcdc2eee48356fbbf12`) that names no node, and `Interrupt.from_ns` -- which
    reads like the missing field -- is a **classmethod**, so `i.from_ns` yields a bound method
    rather than a namespace. There is no node name anywhere on the object.

    It is on the *tasks*, which is why this walks them rather than reusing `_snapshots`. That helper
    merges `values` and drops the task it took them from; the name is exactly what it discards. Two
    small walks answering two different questions is the honest arrangement here -- widening
    `_snapshots` to carry names would make every caller pay for a field only this one reads.

    The walk follows the task that **carries the interrupt** rather than the snapshot's `next`.
    `next` names what would run, which is also populated for a graph stopped for some other reason;
    following the interrupt means a non-empty path means *a human is being waited on* and nothing
    else. So `bool(await awaiting_node_path(...))` equals `await is_awaiting_human(...)`, and that
    equivalence is asserted rather than assumed -- two spellings of one predicate is precisely the
    pair that drifts.

    **If two gates were ever outstanding at once this names one of them.** `interrupt_payloads`
    stays the complete list. Today the graph cannot produce two: the resolution fork is unwired, and
    each subgraph has a single gate. When it can, this returns a path per interrupt or it lies.
    """
    snapshot: Any = await app.aget_state(config, subgraphs=True)
    path: list[str] = []
    while snapshot is not None:
        asking = next((t for t in snapshot.tasks if getattr(t, "interrupts", ())), None)
        if asking is None:
            break
        path.append(str(asking.name))
        child = getattr(asking, "state", None)
        snapshot = child if child is not None and hasattr(child, "tasks") else None
    return tuple(path)


async def is_awaiting_human(app: CompiledStateGraph[Any, Any, Any], config: RunnableConfig) -> bool:
    """Whether the incident is paused on an approval.

    Defined on the presence of an interrupt rather than on `status == AWAITING_APPROVAL`. The status
    is a field some node wrote and a later node will overwrite; the interrupt is the thing that
    actually stops the graph, and the two can disagree if a gate is added without its `prepare`.
    """
    return bool(await interrupt_payloads(app, config))
