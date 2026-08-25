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
from pathlib import Path
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
    CommunicationChannel,
    IncidentStatus,
    KPIName,
    ReasonCode,
    SelfHelpOutcome,
    Technology,
)
from lpr_cpe.domain.governance import ApprovalDecision, ApprovalRequest, AuditEvent, KPIEvent
from lpr_cpe.domain.resolution import SelfHelpSession
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer, checkpointer_scope
from lpr_cpe.persistence.serde import allowlisted_types, build_serde

AT = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)


def _populated() -> dict[str, Any]:
    """A state fragment covering each shape the reducers store: scalar enum, model, list of models.

    Not an exhaustive state. The point is one representative of each *serialisation* shape -- a bare
    enum, a model in a scalar field, models inside a list, and a model with an enum *inside* it --
    since the allowlist works per type and the nested case is the one where a degradation is
    easiest to miss.
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
        "self_help_session": SelfHelpSession(
            session_id="SHS-1",
            incident_id="INC-1",
            channel=CommunicationChannel.SMS,
            started_at=AT,
            outcome=SelfHelpOutcome.RESOLVED,
        ),
        "kpi_events": [
            KPIEvent(
                event_id="KPI-1",
                kpi_name=KPIName.SELF_HELP_SUCCESS_RATE,
                emitted_at=AT,
                value=0.0,
                unit="rate",
                numerator=0.0,
                denominator=1.0,
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


async def test_an_enum_inside_a_model_survives_as_the_member_not_an_equal_string() -> None:
    """`KPIEvent.kpi_name` comes back as `KPIName`, so an identity comparison against it holds.

    `_degraded_fields` cannot make this claim and never could: it compares the type of the *outer*
    value, which is `KPIEvent` either way. An enum field flattened to `str` inside a model that is
    itself restored perfectly passes every other test in this file.

    Worth pinning because the failure is silent and asymmetric. Declared `str`, the field still
    accepted a `KPIName` -- pydantic coerced it down -- and `e.kpi_name is KPIName.X` was then never
    true. A filter written that way and used *positively* fails loudly; used negatively,
    `assert not [e for e in events if e.kpi_name is KPIName.X]` passes on the empty list it always
    produces and proves nothing for as long as it exists. That is not hypothetical: it was written,
    in the self-help subgraph tests, and the absence assertion was green.

    Seen to go red: with `kpi_name` reinstated as `str` on `KPIEvent`, this fails on the `is` --
    `assert 'self_help_success_rate' is <KPIName.SELF_HELP_SUCCESS_RATE>` -- while every other test
    in this file still passes.
    """
    restored = await _round_trip(build_memory_checkpointer())
    kpi = restored["kpi_events"][0]

    assert kpi.kpi_name is KPIName.SELF_HELP_SUCCESS_RATE, (
        "the enum member did not survive the checkpoint, so `is` comparisons on kpi_name are "
        "silently false everywhere and any negative filter built on one proves nothing"
    )
    # The StrEnum half of the contract, which the aggregation layer and `derive_id` both lean on:
    # `str(member)` is the value, so a derived event id is unchanged by the field being an enum.
    assert kpi.kpi_name == "self_help_success_rate"


async def test_a_restored_self_help_outcome_is_the_member_that_d12_routes_on() -> None:
    """`SelfHelpSession.outcome` comes back as `SelfHelpOutcome`, because D12 compares with `is`.

    Separate from the `kpi_name` case above even though the mechanism is identical, because the
    consequence is: `route_self_help_outcome` sends `RESOLVED` to validation and everything else
    back round the loop. An outcome flattened to `str` matches no member, so a session the customer
    *did* resolve is re-diagnosed or sent to field planning instead of being validated and closed --
    a truck for an incident that was already fixed. Nothing raises on that path.

    The `==` assertion is the other half: `outcome` is a `StrEnum`, so a checkpoint written before
    the field was typed still reads back as the member rather than needing a migration.

    Seen to go red: with `outcome` reinstated as `str` on `SelfHelpSession`, this fails on the `is`
    -- `assert 'resolved' is <SelfHelpOutcome.RESOLVED>` -- and every other test in this file still
    passes, including `test_our_checkpointer_restores_every_type_it_was_given`. `_degraded_fields`
    compares the type of the *outer* value, which is `SelfHelpSession` either way, so nothing here
    could see it. The only other test in the suite that notices is the D12 branch-reachability case
    in `test_routing.py`, which stops reaching `verify` at all -- the truck-roll consequence above,
    observed rather than argued.
    """
    restored = await _round_trip(build_memory_checkpointer())
    session = restored["self_help_session"]

    assert session.outcome is SelfHelpOutcome.RESOLVED, (
        "the outcome did not survive the checkpoint as its member, so every `is` comparison in "
        "route_self_help_outcome, route_customer_answer and self_help_success_rate is silently "
        "false and a resolved session never reaches validation"
    )
    assert session.outcome == "resolved"


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
    """The in-memory saver is what tests resume through; a laxer serde there hides Postgres bugs.

    True of the *factory*, and it was false of the tests until 2026-08-25 — see
    `test_every_stage_test_resumes_through_the_production_serde`, which is the half of this claim
    that has to be checked against the suite rather than against the constructor.
    """
    assert (
        build_serde()._allowed_msgpack_modules
        == build_memory_checkpointer().serde._allowed_msgpack_modules
    )  # type: ignore[attr-defined]


#: The two modules allowed to construct a saver by hand, and why each one is.
#:
#: This module is the first: it tests the serde itself, including three plausibly-wrong allowlists
#: asserted to degrade every field, and handing it the production serde would delete its controls.
#: `test_langgraph_replay_contract` is the second: it probes LangGraph's own replay semantics over
#: reducer state that holds no domain models at all, so the allowlist would cover none of it.
_MAY_BUILD_A_SAVER_BY_HAND = {"test_persistence.py", "test_langgraph_replay_contract.py"}


def test_every_stage_test_resumes_through_the_production_serde() -> None:
    """No test may drive a graph through a bare `InMemorySaver`, and 21 of them did.

    `build_memory_checkpointer`'s own docstring calls itself "the local and **test** backend", and
    until 2026-08-25 no test that drove a graph across an interrupt called it. Every stage module
    built `InMemorySaver()` by hand instead, which is the permissive default serde — so the
    allowlist this module spends 130 lines establishing was applied by the factory and by nothing
    that used it.

    It was not hypothetical. Running one incident to closure outside pytest emitted **45** distinct
    `Deserializing unregistered type … This will be blocked in a future version` warnings, covering
    `IncidentStatus`, `ApprovalRequest`, `WorkOrder`, `ClosureRecord` and forty-one others — every
    one of them on the allowlist, and none of them reaching it. The same run through the factory
    emits zero and reaches the same `closed`.

    That is precisely the failure `serde.py` opens by describing: types off the allowlist are
    degraded to primitives *silently*, so `advance_status` compares a `str` against an
    `IncidentStatus`, finds no match, and the lifecycle check stops guarding anything. The suite
    would not have noticed, because a green run over degraded state is exactly what the degradation
    produces.

    Watched red by reverting one call site::

        AssertionError: these tests drive a graph through a bare InMemorySaver, so they resume
        through the permissive default serde rather than the allowlist:
          ['test_builder.py:777']
    """
    offenders: list[str] = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        if path.name in _MAY_BUILD_A_SAVER_BY_HAND:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "InMemorySaver(" in line:
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        "these tests drive a graph through a bare InMemorySaver, so they resume through the "
        "permissive default serde rather than the allowlist:\n  " + "\n  ".join(offenders)
    )


def test_the_hand_built_saver_exemptions_are_all_still_used() -> None:
    """The other direction: an exemption that stops being needed is one nobody will remove.

    Same rule as `builder._check_pending_stages` and `_DECLARED_MISSING` — a table that excuses
    something has to fail when the excuse expires, or it silently widens.

    Watched red by adding `test_builder.py` to the set::

        AssertionError: these modules are exempted from the serde rule and no longer build a saver
        by hand: ['test_builder.py']
    """
    stale = [
        name
        for name in sorted(_MAY_BUILD_A_SAVER_BY_HAND)
        if "InMemorySaver(" not in (Path(__file__).parent / name).read_text(encoding="utf-8")
    ]
    assert not stale, (
        "these modules are exempted from the serde rule and no longer build a saver by hand: "
        f"{stale}. Delete the exemption."
    )


async def test_no_dsn_selects_the_in_memory_backend() -> None:
    async with checkpointer_scope(Settings(postgres_dsn="")) as saver:
        assert isinstance(saver, InMemorySaver)


async def test_whatever_the_scope_yields_can_actually_checkpoint() -> None:
    """The assertion that would have caught the bug this scope was written to fix.

    `isinstance(..., InMemorySaver)` -- the only check this file used to make -- passes on the one
    branch that was never broken. The Postgres branch returned an *unentered* async context manager
    where a saver belonged, which has no `aget_tuple` and would have failed on the first checkpoint
    write in production while every test stayed green.

    So the claim here is not "the right class came back", it is "the thing that came back can store
    and return a checkpoint". That is backend-agnostic: point `LPR_POSTGRES_DSN` at a live database
    and the same assertion tests the branch that was broken.
    """

    def bump(state: dict[str, Any]) -> dict[str, Any]:
        return {"n": state["n"] + 1}

    builder: StateGraph[Any, Any, Any] = StateGraph(dict)
    builder.add_node("bump", bump)
    builder.add_edge(START, "bump")
    builder.add_edge("bump", END)

    async with checkpointer_scope(Settings(postgres_dsn="")) as saver:
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "scope-round-trip"}}
        assert (await graph.ainvoke({"n": 1}, config))["n"] == 2

        # Read it back through the saver rather than trusting the return value: a checkpointer that
        # accepted writes and stored nothing would satisfy the line above.
        assert (await graph.aget_state(config)).values["n"] == 2


async def test_the_postgres_branch_enters_the_saver_and_closes_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres branch, exercised with no Postgres.

    The round-trip test above cannot reach this branch -- it needs a DSN, and a DSN needs a
    database -- so the branch that was actually broken would still be the untested one. A stand-in
    module is injected under the name the deferred import uses, which lets the *sequence* be
    asserted: open, setup, hand over the entered saver, close. The original defect returned the
    unentered helper, so `saver` would be the context manager itself and `close` would never appear.

    The stand-in mirrors the real `from_conn_string`, and
    `test_the_postgres_saver_has_a_lifecycle_a_plain_factory_could_not_have` is what stops that
    mirror from drifting: it asserts the same shape against the installed library. A fake nobody
    checks against the real thing is a test of the fake.
    """
    import sys
    import types
    from contextlib import asynccontextmanager

    events: list[Any] = []

    class FakeSaver:
        def __init__(self, serde: Any) -> None:
            self.serde = serde

        async def setup(self) -> None:
            events.append("setup")

    @asynccontextmanager
    async def from_conn_string(conn_string: str, *, serde: Any = None) -> Any:
        events.append(("open", conn_string))
        try:
            yield FakeSaver(serde)
        finally:
            events.append("close")

    module = types.ModuleType("langgraph.checkpoint.postgres.aio")
    module.AsyncPostgresSaver = types.SimpleNamespace(  # type: ignore[attr-defined]
        from_conn_string=from_conn_string
    )
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", module)

    dsn = "postgresql://user@example.invalid/lpr"
    async with checkpointer_scope(Settings(postgres_dsn=dsn)) as saver:
        assert isinstance(saver, FakeSaver), (
            "the scope handed back the connection helper instead of the saver inside it"
        )
        assert saver.serde is not None, "the Postgres saver was built without the shared serde"
        assert events == [("open", dsn), "setup"]

    assert events == [("open", dsn), "setup", "close"], (
        "the connection was not closed when the scope exited"
    )


async def test_setup_can_be_left_to_a_separate_migration_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`setup=False` exists for a role with no DDL right, so it must really skip the DDL.

    Paired with the test above rather than merged into it: together they show the flag has both of
    its effects, which a single test of the default would not.
    """
    import sys
    import types
    from contextlib import asynccontextmanager

    events: list[str] = []

    class FakeSaver:
        def __init__(self, serde: Any) -> None:
            self.serde = serde

        async def setup(self) -> None:
            events.append("setup")

    @asynccontextmanager
    async def from_conn_string(conn_string: str, *, serde: Any = None) -> Any:
        yield FakeSaver(serde)

    module = types.ModuleType("langgraph.checkpoint.postgres.aio")
    module.AsyncPostgresSaver = types.SimpleNamespace(  # type: ignore[attr-defined]
        from_conn_string=from_conn_string
    )
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", module)

    settings = Settings(postgres_dsn="postgresql://user@example.invalid/lpr")
    async with checkpointer_scope(settings, setup=False) as saver:
        assert isinstance(saver, FakeSaver)
    assert events == [], "setup() ran despite setup=False"


def test_the_postgres_saver_has_a_lifecycle_a_plain_factory_could_not_have() -> None:
    """Pins the library shape this module's design depends on, without needing a database.

    `from_conn_string` is an `@asynccontextmanager`: it returns a helper that opens the connection
    on `__aenter__` and closes it on `__aexit__`, and is not a saver itself. That is the whole
    reason `checkpointer_scope` is a scope. If a future release of the extra returned the saver
    directly, `async with` would stop being correct -- and this test would say so, rather than the
    failure surfacing as a production incident with no checkpoints.

    Skipped rather than failed where the optional extra is absent, which is the normal case: the
    in-memory path is designed to work on a machine with no libpq at all.
    """
    aio = pytest.importorskip(
        "langgraph.checkpoint.postgres.aio",
        reason="the postgres extra is optional; install .[postgres] to check this contract",
    )
    from langgraph.checkpoint.base import BaseCheckpointSaver

    # No connection is opened: the body of an async generator context manager does not run until
    # it is entered, so an unreachable DSN is safe here.
    handle = aio.AsyncPostgresSaver.from_conn_string("postgresql://unused:unused@127.0.0.1:1/none")
    assert not isinstance(handle, BaseCheckpointSaver), (
        "from_conn_string now returns a saver directly; checkpointer_scope's `async with` is no "
        "longer the right shape and the connection would be opened and closed by nobody"
    )
    assert hasattr(handle, "__aenter__") and hasattr(handle, "__aexit__")


def test_importing_the_module_does_not_import_the_postgres_driver() -> None:
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
        f"importing the checkpointer module alone failed, which is itself the bug this test is "
        f"about:\n{result.stderr}"
    )
    assert result.stdout.strip() == "[]", (
        f"importing lpr_cpe.persistence.checkpointer pulled in {result.stdout.strip()}. The "
        "Postgres import must stay inside checkpointer_scope, or the in-memory path stops working "
        "on machines without libpq."
    )
