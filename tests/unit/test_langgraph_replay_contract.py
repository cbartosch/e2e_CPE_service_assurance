"""What LangGraph does to already-committed writes when an interrupt is resumed.

Every approval gate in this system sits inside a subgraph, and each one resumes a thread whose
upstream nodes have already written to state. Whether those writes are applied once or twice is
therefore not a curiosity -- it decides whether `field_visit_count` is a count or a fiction, and
whether the bounded-loop guard can be trusted to fire.

The behaviour is measured here rather than recalled, and asserted rather than commented, for three
reasons:

1. **It is load-bearing and invisible.** `state.py` chose de-duplicating and absolute-valued
   reducers over the obvious `operator.add` *because* of what is measured below. Nothing in that
   file explains itself by failing; if LangGraph's semantics changed, `state.py` would keep passing
   its own unit tests while the graph quietly double-counted. This file is the tripwire.
2. **It is a third-party contract, not our code.** A dependency upgrade is exactly the moment such
   an assumption breaks, and exactly the moment nobody re-derives it by hand.
3. **The surprising half needs its own control.** The replay is specific to interrupts raised
   *inside a subgraph*; the same graph flattened does not replay. Asserting only the subgraph case
   would leave "LangGraph replays parent writes" as the remembered lesson, which is false and would
   justify defensive code where none is needed. Both are asserted together.

`operator.add` appears in these graphs as a **positive control**: it is the reducer a reasonable
person would reach for, and watching it go wrong is what makes the assertions about the real
reducers mean something. A test in which every reducer survives proves only that the graph never
replayed anything.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from lpr_cpe.domain.enums import IncidentStatus
from lpr_cpe.graph.state import (
    advance_status,
    append_revision,
    append_unique,
    merge_retries,
    take_max,
    write_once,
)


class ReplayProbeState(TypedDict, total=False):
    """One field per reducer the real state contract uses, plus the control.

    Deliberately a separate TypedDict rather than `IncidentState`: the point is to compare reducers
    against each other under identical replay conditions, which needs them side by side on fields
    that one node writes in one go.
    """

    #: The control. What `state.py` would have used if replay were not a concern.
    naive_count: Annotated[int, operator.add]
    #: The real reducers, each fed the same replayed write as the control.
    absolute_count: Annotated[int, take_max]
    findings: Annotated[list[dict[str, Any]], append_unique]
    revisions: Annotated[list[dict[str, Any]], append_revision]
    retries: Annotated[dict[str, int], merge_retries]
    clock: Annotated[str | None, write_once]
    status: Annotated[IncidentStatus | None, advance_status]
    #: Written only by the gate, so it distinguishes "resumed" from "paused".
    answer: Annotated[list[str], operator.add]


def _upstream_write(attempt: int) -> dict[str, Any]:
    """Exactly what a real upstream node returns: absolute counters, keyed records, one clock.

    `attempt` is the node's own invocation number. A replay re-applies the write produced by
    invocation 1 -- it does not re-run the node to produce a second one -- so a genuine second
    invocation and a replayed first are distinguishable in the recorded values.
    """
    return {
        "naive_count": 1,
        "absolute_count": 1,
        "findings": [{"finding_id": "f-1", "attempt": attempt}],
        "revisions": [{"ref": "wo-1", "state": "scheduled"}],
        "retries": {"cpe_read": 1},
        "clock": "2026-08-15T07:00:00Z",
        "status": IncidentStatus.DIAGNOSING,
    }


def _build(*, gate_in_subgraph: bool, invocations: list[str]) -> Any:
    """The same two-node graph twice, differing only in whether the gate is nested.

    Returned compiled; the caller supplies the thread id.
    """

    async def upstream(state: ReplayProbeState) -> dict[str, Any]:
        invocations.append("upstream")
        return _upstream_write(attempt=invocations.count("upstream"))

    async def gate(state: ReplayProbeState) -> dict[str, Any]:
        invocations.append("gate")
        decision = interrupt({"question": "approve?"})
        # Anything after `interrupt()` runs only on the resumed pass; anything before it runs on
        # both. D3 in IMPLEMENTATION_PLAN.md turns that into a rule: gates ask and return, and a
        # separate downstream node performs the non-idempotent write.
        return {"answer": [decision]}

    def _gate_node() -> Any:
        if not gate_in_subgraph:
            return gate
        inner = StateGraph(ReplayProbeState)
        inner.add_node("gate", gate)
        inner.add_edge(START, "gate")
        inner.add_edge("gate", END)
        return inner.compile()

    outer = StateGraph(ReplayProbeState)
    outer.add_node("upstream", upstream)
    outer.add_node("gate", _gate_node())
    outer.add_edge(START, "upstream")
    outer.add_edge("upstream", "gate")
    outer.add_edge("gate", END)
    return outer.compile(checkpointer=InMemorySaver())


async def _run_to_completion(
    *, gate_in_subgraph: bool, thread: str
) -> tuple[dict[str, Any], list[str]]:
    """Drive the graph to its pause, resume it, and return the final state and the call log."""
    invocations: list[str] = []
    app = _build(gate_in_subgraph=gate_in_subgraph, invocations=invocations)
    config = {"configurable": {"thread_id": thread}}
    paused = await app.ainvoke({}, config)

    assert "__interrupt__" in paused, "the graph did not pause, so nothing below tests a replay"
    assert paused["naive_count"] == 1, (
        "the upstream write was not committed before the pause, so the resumed pass has nothing "
        f"to replay and this test would pass vacuously; got {paused['naive_count']}"
    )

    resumed = await app.ainvoke(
        Command(resume={i.id: "approved" for i in paused["__interrupt__"]}), config
    )
    assert resumed["answer"] == ["approved"], "the graph did not actually resume"
    return resumed, invocations


async def test_the_interrupted_node_reruns_from_its_start_which_is_why_gates_do_not_write() -> None:
    """D3's premise. The gate body before `interrupt()` executes on both passes.

    This is the cheap half of the contract and the one most likely to be assumed rather than
    checked. It holds whether or not the gate is nested, so both are asserted -- if it ever became
    true in only one arrangement, the rule "gates ask and return" would need to name which.
    """
    for gate_in_subgraph in (True, False):
        _, invocations = await _run_to_completion(
            gate_in_subgraph=gate_in_subgraph, thread=f"rerun-{gate_in_subgraph}"
        )
        assert invocations.count("gate") == 2, (
            f"gate_in_subgraph={gate_in_subgraph}: expected the interrupted node to run twice, "
            f"once per pass; got {invocations}"
        )


async def test_a_committed_upstream_write_is_replayed_only_when_the_gate_is_nested() -> None:
    """The finding, with the flat graph as its control.

    Measured on langgraph 1.2.11: resuming an interrupt raised inside a subgraph re-applies the
    parent's already-committed write, **without** re-running the parent node. Flattening the same
    graph does not. The asymmetry is the whole point -- it is why the defence belongs in the
    reducers, which cannot tell where the interrupt came from, rather than in the nodes.

    `naive_count` is read rather than the real reducers precisely because it is the one field that
    *shows* the replay; a de-duplicating reducer would hide it.
    """
    nested, nested_calls = await _run_to_completion(gate_in_subgraph=True, thread="nested")
    flat, flat_calls = await _run_to_completion(gate_in_subgraph=False, thread="flat")

    # Neither node re-ran. Whatever happened to the value, it was not recomputed.
    assert nested_calls.count("upstream") == 1, nested_calls
    assert flat_calls.count("upstream") == 1, flat_calls

    assert nested["naive_count"] == 2, (
        "expected the nested-gate resume to re-apply the upstream write to an `operator.add` "
        f"field, doubling it; got {nested['naive_count']}. If this is now 1, LangGraph stopped "
        "replaying parent writes -- state.py's reducers are then belt-and-braces rather than "
        "load-bearing, and this file should say so."
    )
    assert flat["naive_count"] == 1, (
        "expected no replay when the interrupt is raised in the parent graph itself; got "
        f"{flat['naive_count']}. If this is now 2, the replay is universal rather than specific to "
        "subgraphs, and any future non-nested interrupt needs the same care as a nested one."
    )

    # Stated as a relation as well as as literals: the two arrangements must differ, which is the
    # claim that survives a change in how many times a replay applies.
    assert nested["naive_count"] > flat["naive_count"]


@pytest.mark.parametrize(
    ("field", "expected", "why"),
    [
        pytest.param(
            "absolute_count",
            1,
            "counters are absolute, so `take_max` of 1 and 1 is 1 -- this is the field the "
            "bounded-loop guard reads, and the control above shows what it would have been",
            id="take_max_survives_the_replay_that_breaks_operator_add",
        ),
        pytest.param(
            "findings",
            [{"finding_id": "f-1", "attempt": 1}],
            "`append_unique` keys on finding_id, so the replayed copy is recognised as the same "
            "finding; attempt=1 confirms it is the original write re-applied, not a re-run",
            id="append_unique_keeps_one_copy",
        ),
        pytest.param(
            "revisions",
            [{"ref": "wo-1", "state": "scheduled"}],
            "`append_revision` keeps genuine revisions and drops an identical one wherever it "
            "sits, which is exactly the shape a replay produces",
            id="append_revision_drops_the_duplicate",
        ),
        pytest.param(
            "retries",
            {"cpe_read": 1},
            "`merge_retries` is per-key max for the same reason `take_max` exists",
            id="merge_retries_is_per_key_max",
        ),
        pytest.param(
            "clock",
            "2026-08-15T07:00:00Z",
            "`write_once` permits an identical re-write; if it refused, every interrupt "
            "downstream of the intake clock would be fatal",
            id="write_once_tolerates_an_identical_rewrite",
        ),
        pytest.param(
            "status",
            IncidentStatus.DIAGNOSING,
            "`advance_status` reaches `can_transition(x, x)`, which is legal by explicit "
            "special-case; DIAGNOSING -> DIAGNOSING is absent from the transition table, so "
            "without that case the replay would raise",
            id="advance_status_allows_the_no_op_self_transition",
        ),
    ],
)
async def test_each_real_reducer_absorbs_the_replay_that_corrupts_operator_add(
    field: str, expected: Any, why: str
) -> None:
    """The bridge from the library's behaviour to this repo's defence against it.

    Each reducer is driven through the *same* replaying graph as the control, one field at a time,
    so a failure names the reducer rather than the state contract. The control is re-asserted in
    every case: if the graph stopped replaying, these would all pass for the wrong reason.
    """
    final, _ = await _run_to_completion(gate_in_subgraph=True, thread=f"reducer-{field}")

    assert final["naive_count"] == 2, (
        f"the replay did not occur on this run, so `{field}` was never tested against one"
    )
    assert final[field] == expected, f"{field}: {why}"


def test_append_revision_does_not_double_when_handed_its_own_list() -> None:
    """The 49,152-entry defect, reduced to the two lines that caused it.

    A subgraph that shares a channel with its parent is given the parent's accumulated list on entry
    and returns the whole thing on exit, so the parent's reducer is handed its own list as an
    update. When `append_revision` dropped only a duplicate of `out[-1]`, none of the incoming items
    matched except the last, and the channel **doubled on every re-entry**. Measured end to end
    before the fix: `SVC-SJ-011-A-01` finished with 49,152 copies of one work order, `mr_records`
    reached 524,288 across the fixture sweep, and 57 of 164 runs held a channel over 100 entries.

    Nothing decided wrongly, which is why nothing caught it -- `latest_by_id` collapses by id, so
    `current_work_orders` and `truck_roll_count` both answered 1 and the gate was called once. What
    was wrong was the state: every checkpoint carrying fifty thousand copies of one record.

    Watched red by restoring `if out and out[-1] == item`::

        AssertionError: handing the reducer its own list doubled the channel: 3 -> 6
    """
    revisions = [
        {"ref": "wo-1", "state": "scheduled"},
        {"ref": "wo-1", "state": "dispatched"},
        {"ref": "wo-1", "state": "on_site"},
    ]

    same = append_revision(revisions, list(revisions))
    assert len(same) == len(revisions), (
        f"handing the reducer its own list doubled the channel: {len(revisions)} -> {len(same)}"
    )
    assert same == revisions, "the order of the genuine revisions has to survive"

    # Twelve re-entries is what the dispatch fixture actually did. Doubling would give 12,288.
    grown = list(revisions)
    for _ in range(12):
        grown = append_revision(grown, list(grown))
    assert len(grown) == 3, f"twelve re-entries grew the channel to {len(grown)}"

    # And the property this reducer exists for is intact: a genuine new revision still appends.
    moved = append_revision(revisions, [{"ref": "wo-1", "state": "completed"}])
    assert len(moved) == 4, "a real status change must still be recorded"
    assert moved[-1]["state"] == "completed"


def test_append_revision_still_keeps_a_repeat_that_differs() -> None:
    """The positive control, without which the test above passes on `append_unique`.

    These channels hold successive states of one object, so de-duplicating *by id* would collapse
    the sequence that is the record. Only byte-identical entries are dropped.
    """
    out = append_revision(
        [{"ref": "wo-1", "state": "scheduled", "at": "T1"}],
        [
            {"ref": "wo-1", "state": "scheduled", "at": "T2"},
            {"ref": "wo-1", "state": "dispatched", "at": "T3"},
        ],
    )
    assert len(out) == 3, "two entries differing only by timestamp are two facts, not one"
