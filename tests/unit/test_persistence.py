"""That state survives a checkpoint round trip as the types it went in as.

LangGraph's msgpack serialiser degrades types it is not told about instead of rejecting them: an
`ApprovalRequest` comes back a `dict`, an `IncidentStatus` comes back a `str`, and nothing raises.
The graph resumes and keeps running on values whose methods no longer exist. That is the failure
this file is here to make loud.

Every test below is paired with a control, because "the value survived" is not the claim -- the
claim is "the value survived *because of our allowlist*", and the permissive default satisfies the
first without satisfying the second. `test_a_mismatched_allowlist_degrades_every_type` is the
control: it demonstrates the mechanism biting, so a pass elsewhere means the allowlist did work
rather than that nothing was ever at risk.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

import lpr_cpe.domain as domain
from lpr_cpe.config.settings import Settings
from lpr_cpe.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    IncidentStatus,
    ReasonCode,
    Technology,
)
from lpr_cpe.domain.governance import ApprovalDecision, ApprovalRequest, AuditEvent
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.persistence.checkpointer import build_checkpointer, build_memory_checkpointer
from lpr_cpe.persistence.serde import allowlisted_types, build_serde

AT = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)


def _populated() -> dict[str, Any]:
    """A state fragment covering each shape the reducers store: scalar enum, model, list of models.

    Not an exhaustive state. The point is one representative of each *serialisation* shape -- a bare
    enum, a model in a scalar field, models inside a list -- since the allowlist works per type and
    the list case is the one where a degradation is easiest to miss.
    """
    return {
        "status": IncidentStatus.TRIAGING,
        "technology": Technology.PON,
        "pending_approval": ApprovalRequest(
            approval_id="APR-1",
            incident_id="INC-1",
            kind=ApprovalKind.DISPATCH,
            requested_at=AT,
            question="Send a crew?",
        ),
        "approvals": [
            ApprovalDecision(
                approval_id="APR-1",
                incident_id="INC-1",
                kind=ApprovalKind.DISPATCH,
                status=ApprovalStatus.APPROVED,
                decided_at=AT,
                decided_by="alice",
            )
        ],
        "audit_events": [
            AuditEvent(
                event_id="AUD-1", incident_id="INC-1", occurred_at=AT, actor="bot", action="t"
            )
        ],
    }


async def _round_trip(saver: Any) -> dict[str, Any]:
    """Write a populated state, pause at an interrupt, resume, and return what came back.

    The pause is what forces the round trip. Without it the values may never leave the process and
    the test would assert nothing about serialisation at all.
    """
    payload = _populated()

    async def write(state: IncidentState) -> dict[str, Any]:
        return dict(payload)

    async def gate(state: IncidentState) -> dict[str, Any]:
        interrupt({"question": "pause here"})
        return {}

    graph = StateGraph(IncidentState)
    graph.add_node("write", write)
    graph.add_node("gate", gate)
    graph.add_edge(START, "write")
    graph.add_edge("write", "gate")
    graph.add_edge("gate", END)
    app = graph.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": "serde"}}
    paused = await app.ainvoke({}, config)
    assert "__interrupt__" in paused, "no pause, so nothing round-tripped through the checkpointer"
    return dict(
        await app.ainvoke(Command(resume={i.id: "x" for i in paused["__interrupt__"]}), config)
    )


def _degraded_fields(restored: dict[str, Any]) -> list[str]:
    """Field names whose restored value has a different type from what was written."""
    out = []
    for name, written in _populated().items():
        got = restored.get(name)
        want_type = type(written[0]) if isinstance(written, list) else type(written)
        got_type = type(got[0]) if isinstance(got, list) and got else type(got)
        if want_type is not got_type:
            out.append(name)
    return out


async def test_our_checkpointer_restores_every_type_it_was_given() -> None:
    """The claim. Read together with the control below, which proves it is not free."""
    assert _degraded_fields(await _round_trip(build_memory_checkpointer())) == []


@pytest.mark.parametrize(
    ("label", "allowlist"),
    [
        pytest.param("empty", [], id="an_empty_allowlist"),
        pytest.param("unrelated", [ReasonCode], id="an_allowlist_of_unrelated_types"),
        pytest.param("strings", ["lpr_cpe"], id="a_package_name_instead_of_the_classes"),
    ],
)
async def test_a_mismatched_allowlist_degrades_every_type_without_raising(
    label: str, allowlist: list[Any]
) -> None:
    """The control. Each of these is a plausible mistake, and each one fails silently.

    `a_package_name_instead_of_the_classes` is the one worth dwelling on: the allowlist accepts
    `(module, name)` tuples as well as classes, so `["lpr_cpe"]` looks like it should mean "trust
    everything under lpr_cpe". It matches nothing, and the graph keeps running.

    The assertion is that *nothing raised* as well as that the types degraded. If a future LangGraph
    started raising, that would be strictly better and this test should be rewritten to expect it --
    but it must not be allowed to change without anyone noticing.
    """
    restored = await _round_trip(
        InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=allowlist))
    )
    assert sorted(_degraded_fields(restored)) == sorted(_populated()), (
        f"the {label} allowlist did not degrade every field, so this control no longer demonstrates "
        "the failure that test_our_checkpointer_restores_every_type_it_was_given claims to prevent"
    )
    assert isinstance(restored["status"], str)
    assert isinstance(restored["pending_approval"], dict)


def test_the_allowlist_is_derived_from_the_domain_api_so_a_new_model_cannot_be_missed() -> None:
    """Every model and enum `lpr_cpe.domain` exports is allowlisted, by construction not by hand.

    Asserted as set equality against a *separately computed* expectation rather than against a
    count. A count would pass the day someone swapped one model for another.
    """
    expected = {
        obj
        for name in domain.__all__
        if isinstance(obj := getattr(domain, name), type) and issubclass(obj, BaseModel | enum.Enum)
    }
    assert set(allowlisted_types()) == expected
    assert expected, "the domain package exported no models or enums, which cannot be right"


def test_the_allowlist_holds_classes_rather_than_string_paths() -> None:
    """A rename should be a rename. String paths are the form that degrades silently when stale."""
    assert all(isinstance(t, type) for t in allowlisted_types())


def test_the_allowlist_order_does_not_depend_on_set_iteration() -> None:
    """Two calls agree. `allowlisted_types` builds from a set, and sets do not order themselves."""
    assert allowlisted_types() == allowlisted_types()


def test_both_backends_are_built_with_the_same_serde() -> None:
    """The in-memory saver is what tests resume through; a laxer serde there hides Postgres bugs."""
    assert (
        build_serde()._allowed_msgpack_modules
        == build_memory_checkpointer().serde._allowed_msgpack_modules
    )  # type: ignore[attr-defined]


def test_no_dsn_selects_the_in_memory_backend() -> None:
    assert isinstance(build_checkpointer(Settings(postgres_dsn="")), InMemorySaver)


def test_importing_the_factory_does_not_import_the_postgres_driver() -> None:
    """The measured reason the Postgres import is deferred, checked in a clean interpreter.

    `langgraph.checkpoint.postgres` raises `ImportError: no pq wrapper available` unless `libpq` is
    installed -- the bare `psycopg` wheel is not enough. A module-level import would therefore make
    the *in-memory* path unusable on any machine without Postgres client libraries, CI included, for
    a backend it was never going to touch.

    A subprocess is used rather than inspecting `sys.modules` in-process: by the time this file
    runs, pytest has imported most of the tree, so an in-process check would report on what the
    suite loaded rather than on what this module loads. The interpreter is re-entered so the
    question asked is the real one -- what does importing *only* this module pull in?
    """
    import os
    import pathlib
    import subprocess
    import sys

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    probe = (
        "import sys; import lpr_cpe.persistence.checkpointer as m; "
        "print(sorted(n for n in sys.modules if 'checkpoint.postgres' in n or n == 'psycopg'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ},
        check=False,
    )
    assert result.returncode == 0, (
        f"importing the checkpointer factory alone failed, which is itself the bug this test is "
        f"about:\n{result.stderr}"
    )
    assert result.stdout.strip() == "[]", (
        f"importing lpr_cpe.persistence.checkpointer pulled in {result.stdout.strip()}. The "
        "Postgres import must stay inside build_checkpointer, or the in-memory path stops working "
        "on machines without libpq."
    )
