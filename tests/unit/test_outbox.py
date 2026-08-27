"""The outbox, the relay and the migration runner.

Three claims are worth more than the rest and the module is organised around them.

**A replay does not produce a second write.** That is the entire reason an outbox exists here, and
it is asserted by staging the same intent twice and reading the store, not by inspecting a key.

**A permanently broken target does not hold up everybody else's writes.** The failure an outbox is
supposed to contain is one integration being down; a relay that abandoned the batch on the first
exception would turn that into every incident's writes stopping.

**An edited migration is refused.** A version-number-only runner cannot see it, and the symptom --
the change is present where it was written and missing everywhere else -- is invisible until the
next deploy fails somewhere unrelated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.config.settings import AppMode, Settings
from lpr_cpe.domain.base import idempotency_key
from lpr_cpe.domain.enums import ActionType, PolicyOutcome, ReasonCode
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import WriteGate
from lpr_cpe.persistence.migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationTamperedError,
    checksum_of,
    discover,
    plan,
)
from lpr_cpe.persistence.outbox import (
    DEFAULT_MAX_ATTEMPTS,
    InMemoryOutbox,
    OutboxEvent,
    OutboxRelay,
    OutboxStatus,
    OutboxStore,
    PostgresOutbox,
    StagedWrites,
    backoff_delay,
    build_outbox,
    dispatch_to,
)

NOW = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)


def _request(**over: Any) -> ActionRequest:
    fields: dict[str, Any] = {
        "action_id": "ACT-1",
        "incident_id": "INC-SVC-1",
        "action_type": ActionType.CPE_REBOOT,
        "target_ref": "CPE-1",
        "requested_at": NOW,
        "idempotency_key": idempotency_key("INC-SVC-1", "cpe_reboot", "CPE-1"),
        "actor": "lpr-cpe-automation",
        "reason_code": ReasonCode.POLICY_ALLOWED,
        "correlation_id": "COR-1",
        "policy_outcome": PolicyOutcome.ALLOWED,
    }
    fields.update(over)
    return ActionRequest(**fields)


def _event(**over: Any) -> OutboxEvent:
    return OutboxEvent.from_action(_request(**over), target_system="cpe", now=NOW)


# ------------------------------------------------------------------------------------------------
# The claim the pattern exists for
# ------------------------------------------------------------------------------------------------


async def test_staging_the_same_intent_twice_leaves_one_row() -> None:
    """A replayed node must not become a second reboot.

    LangGraph re-executes nodes on resume, so this is the ordinary case and not an edge one: a
    supervisor answering an approval three hours later replays every node in the super-step that
    interrupted. `domain.base.idempotency_key` makes both stagings agree on a key, and the store
    recognising it is the mechanism working -- so `enqueue` answers `False` rather than raising.

    Watched red by keying `InMemoryOutbox` on `event_id` and generating that from a uuid::

        AssertionError: a replay staged a second write for the same intent: 2 rows
    """
    store = InMemoryOutbox()

    assert await store.enqueue(_event()) is True
    assert await store.enqueue(_event()) is False, (
        "the second staging must be recognised, not raise"
    )

    pending = await store.by_status(OutboxStatus.PENDING)
    assert len(pending) == 1, f"a replay staged a second write for the same intent: {len(pending)}"


async def test_a_deliberate_second_attempt_is_a_different_row() -> None:
    """The positive control. Without it the test above passes on a store that swallows everything.

    `attempt` is what separates "the operator asked for another reboot" from "the node ran twice",
    and it is an argument a caller has to increment on purpose.
    """
    store = InMemoryOutbox()
    first = idempotency_key("INC-SVC-1", "cpe_reboot", "CPE-1", 1)
    second = idempotency_key("INC-SVC-1", "cpe_reboot", "CPE-1", 2)
    assert first != second

    assert await store.enqueue(_event(idempotency_key=first)) is True
    assert await store.enqueue(_event(idempotency_key=second)) is True
    assert len(await store.by_status(OutboxStatus.PENDING)) == 2


# ------------------------------------------------------------------------------------------------
# The relay
# ------------------------------------------------------------------------------------------------


async def test_one_broken_target_does_not_stop_the_others() -> None:
    """The outage an outbox contains, rather than causes.

    A relay that abandoned the batch on the first exception would let one dead integration stop
    every other incident's writes -- which is worse than the original fault and is the failure mode
    this pattern is bought to prevent.

    Watched red by re-raising instead of recording the failure::

        KeyError: 'no handler for target system ...'
    """
    store = InMemoryOutbox()
    for index, system in enumerate(("cpe", "wfm", "cpe")):
        event = _event(idempotency_key=idempotency_key("INC-SVC-1", "cpe_reboot", f"CPE-{index}"))
        event.target_system = system
        event.target_ref = f"CPE-{index}"
        await store.enqueue(event)

    seen: list[str] = []

    async def working(event: OutboxEvent) -> None:
        seen.append(event.target_ref)

    async def broken(event: OutboxEvent) -> None:
        raise RuntimeError("wfm is down")

    relay = OutboxRelay(store, dispatch_to({"cpe": working, "wfm": broken}), clock=FrozenClock(NOW))
    result = await relay.drain_once()

    assert result.claimed == 3
    assert result.sent == 2, "the two healthy events had to go out regardless of the third"
    assert result.failed == 1
    assert sorted(seen) == ["CPE-0", "CPE-2"]


async def test_a_failure_is_rescheduled_and_the_fifth_is_dead_lettered() -> None:
    """Retry, backoff, and then a human. Dead-letter handling is on the specification's list.

    Asserted by driving the relay five times rather than by calling `mark_failed` in a loop, so the
    thing under test is the arrangement and not one method. The clock does not advance between
    drains and the event is still claimed each time, which is deliberate: `claim_due` compares
    `next_attempt_at <= now`, so a frozen clock proves the *counting* without the scheduling. The
    scheduling has its own test below.
    """
    store = InMemoryOutbox()
    await store.enqueue(_event())

    async def always_fails(event: OutboxEvent) -> None:
        raise RuntimeError("the far side returned 503")

    clock = FrozenClock(NOW)
    relay = OutboxRelay(store, always_fails, clock=clock)

    for attempt in range(1, DEFAULT_MAX_ATTEMPTS):
        clock.advance(timedelta(hours=1))
        result = await relay.drain_once()
        assert result.claimed == 1, f"attempt {attempt} claimed nothing"
        assert result.failed == 1
        assert result.dead_lettered == 0
        (event,) = await store.by_status(OutboxStatus.FAILED)
        assert event.attempts == attempt
        assert "503" in event.last_error

    clock.advance(timedelta(hours=1))
    final = await relay.drain_once()
    assert final.dead_lettered == 1, "the fifth attempt has to stop retrying and wait for a human"

    (dead,) = await store.by_status(OutboxStatus.DEAD)
    assert dead.attempts == DEFAULT_MAX_ATTEMPTS
    assert not await store.claim_due(clock.now()), "a dead letter must not be claimed again"


async def test_a_failed_event_is_not_due_until_its_backoff_has_passed() -> None:
    """The scheduling half, which the test above deliberately does not cover.

    Watched red by leaving `next_attempt_at` alone in `mark_failed`: the event is claimed again
    immediately and five attempts burn in one drain loop with no delay at all.
    """
    store = InMemoryOutbox()
    await store.enqueue(_event())

    async def fails(event: OutboxEvent) -> None:
        raise RuntimeError("nope")

    clock = FrozenClock(NOW)
    await OutboxRelay(store, fails, clock=clock).drain_once()

    (event,) = await store.by_status(OutboxStatus.FAILED)
    assert event.next_attempt_at > NOW, "a failure with no delay is a hot retry loop"

    assert not await store.claim_due(NOW), "claimed again before its backoff elapsed"
    assert await store.claim_due(event.next_attempt_at), "never became due"


def test_the_backoff_grows_is_capped_and_is_reproducible() -> None:
    """Exponential, jittered, bounded -- and the same twice, which `random` could not give.

    The jitter is a hash of the event id, so the schedule is assertable. See the module docstring in
    `persistence.outbox` for why that is the right trade rather than a shortcut.
    """
    delays = [backoff_delay("OBX-abc", attempt) for attempt in range(1, 9)]

    assert backoff_delay("OBX-abc", 0) == timedelta(0), "attempt zero has not failed yet"
    assert delays == [backoff_delay("OBX-abc", n) for n in range(1, 9)], "not reproducible"
    assert delays[0] < delays[3], "the delay has to grow with the attempt"
    assert all(delay <= timedelta(seconds=300) for delay in delays), "the cap is not holding"
    assert backoff_delay("OBX-abc", 4) != backoff_delay("OBX-xyz", 4), (
        "two events must not retry in lockstep; that is what the jitter is for"
    )


async def test_an_event_for_a_system_nobody_wired_dead_letters_rather_than_vanishing() -> None:
    """Refusing beats dropping. A write the trail claims was made and nobody received is worse."""
    store = InMemoryOutbox()
    event = _event()
    event.target_system = "a-system-that-does-not-exist"
    await store.enqueue(event)

    clock = FrozenClock(NOW)
    relay = OutboxRelay(store, dispatch_to({}), clock=clock, max_attempts=1)
    result = await relay.drain_once()

    assert result.dead_lettered == 1
    (dead,) = await store.by_status(OutboxStatus.DEAD)
    assert "no handler" in dead.last_error


async def test_the_backlog_drains_oldest_first() -> None:
    """Ordering is a correctness property, not a nicety.

    An outbox that sent a reboot before the approval that authorised it, because a dictionary
    iterated that way, would be a system whose external record disagrees with its own trail.
    """
    store = InMemoryOutbox()
    for minute in (30, 0, 15):
        event = _event(idempotency_key=idempotency_key("INC-SVC-1", "cpe_reboot", f"CPE-{minute}"))
        event.created_at = NOW + timedelta(minutes=minute)
        event.next_attempt_at = event.created_at
        await store.enqueue(event)

    due = await store.claim_due(NOW + timedelta(hours=1))
    assert [event.created_at.minute for event in due] == [0, 15, 30]


# ------------------------------------------------------------------------------------------------
# The gate stages, and both stores answer the same Protocol
# ------------------------------------------------------------------------------------------------


async def test_every_authorised_action_is_staged_by_the_gate() -> None:
    """The gate is the choke point, so it is where staging belongs.

    Staged in simulation too, and that is the point rather than an oversight: the outbox in
    simulation is the record of what *would* have been sent, which is the artefact somebody reads
    before turning writes on. One code path means the thing reviewed is the thing that runs.
    """
    gate = WriteGate(Settings(app_mode=AppMode.SIMULATION), clock=FrozenClock(NOW))
    verdict = gate.authorize(_request())

    assert verdict.simulated is True, "this test needs the simulated arm"
    assert len(gate.staged) == 1
    (staged,) = gate.staged.pending
    assert staged.incident_id == "INC-SVC-1"
    assert staged.target_system == "cpe"
    assert staged.created_at == NOW, "the gate must stamp from its clock, not the wall clock"

    store = InMemoryOutbox()
    assert await gate.staged.flush(store) == 1
    assert len(gate.staged) == 0, "a flushed intent must not be offered again"


async def test_a_flush_that_finds_a_duplicate_still_clears_the_stage() -> None:
    """Otherwise a duplicate is re-offered on every flush, forever.

    Watched red by clearing only the events that returned `True`: the second flush re-offers the
    same event and `len(gate.staged)` never reaches zero.
    """
    store = InMemoryOutbox()
    staged = StagedWrites()
    staged.stage(_event())
    assert await staged.flush(store) == 1

    staged.stage(_event())
    assert await staged.flush(store) == 0, "the store already had it"
    assert len(staged) == 0


def test_the_gate_routes_each_action_to_the_system_that_performs_it() -> None:
    """A work order posted to the CPE adapter is a work order nobody in the field ever sees.

    The unlisted actions falling through to `cpe` is asserted too, because the default is what makes
    the map short and is therefore what a wrong entry hides behind.
    """
    expected = {
        ActionType.CPE_REBOOT: "cpe",
        ActionType.WIFI_CHANNEL_CHANGE: "cpe",
        ActionType.CREATE_WORK_ORDER: "wfm",
        ActionType.RAISE_MR: "jtrack",
        ActionType.NOTIFY_CUSTOMER: "communications",
        ActionType.OLT_PORT_RESET: "pon",
        ActionType.NODE_LEVEL_RESET: "hfc",
        ActionType.REPROVISION: "inventory",
        ActionType.CLOSE_INCIDENT: "nxt",
    }
    gate = WriteGate(Settings(), clock=FrozenClock(NOW))
    for action in expected:
        gate.authorize(
            _request(
                action_type=action,
                idempotency_key=idempotency_key("INC-SVC-1", action.value, "T-1"),
            )
        )

    routed = {event.action_type: event.target_system for event in gate.staged.pending}
    assert routed == {action.value: system for action, system in expected.items()}


def test_both_stores_satisfy_the_protocol() -> None:
    """The drift that actually happens is a signature changing on one implementation only.

    `PostgresOutbox` is never called by the suite -- it needs a database, gap OUTBOX-2 -- so this is
    the only thing standing between it and a rename that leaves it uncallable.
    """
    assert isinstance(InMemoryOutbox(), OutboxStore)
    assert isinstance(PostgresOutbox("postgresql://unused"), OutboxStore)


def test_the_store_is_chosen_by_the_same_setting_as_the_checkpointer() -> None:
    """Durable checkpoints with a forgetful outbox is the worst of the two.

    The incident would replay from its checkpoint and the record of its intent would be gone, so the
    two have to be selected together or not at all.
    """
    assert isinstance(build_outbox(None), InMemoryOutbox)
    assert isinstance(build_outbox(""), InMemoryOutbox)
    assert isinstance(build_outbox("postgresql://host/db"), PostgresOutbox)


# ------------------------------------------------------------------------------------------------
# Migrations
# ------------------------------------------------------------------------------------------------


def test_the_committed_migrations_are_discoverable_and_ordered() -> None:
    """Against the real directory, because a runner that cannot find the files is the failure."""
    found = discover()
    assert found, f"no migrations discovered in {MIGRATIONS_DIR}"
    assert [migration.version for migration in found] == sorted(
        migration.version for migration in found
    )
    assert any("lpr_outbox_events" in migration.sql for migration in found), (
        "the outbox table has no migration, so PostgresOutbox has nothing to write to"
    )


def test_a_migration_that_has_been_edited_since_it_was_applied_is_refused() -> None:
    """The failure a version-number-only runner cannot see.

    An edited migration is present on the machine where it was written and absent everywhere else,
    and nothing notices until a later deploy fails for an unrelated-looking reason. The checksum is
    the whole reason `lpr_schema_migrations` has a third column.

    Watched red by comparing versions only in `plan`::

        Failed: DID NOT RAISE <class 'MigrationTamperedError'>
    """
    migration = Migration(
        version="0001",
        slug="persistence",
        path=Path("migrations/0001_persistence.sql"),
        sql="CREATE TABLE x ();",
        checksum=checksum_of("CREATE TABLE x ();"),
    )

    assert plan({}, [migration]) == (migration,), "an unapplied migration is pending"
    assert plan({"0001": migration.checksum}, [migration]) == (), "an applied one is not"

    with pytest.raises(MigrationTamperedError, match="has been edited"):
        plan({"0001": checksum_of("CREATE TABLE y ();")}, [migration])


def test_the_checksum_ignores_line_endings() -> None:
    """Developed on Windows, deployed on Linux. A checksum that moved with CRLF would fire always."""
    assert checksum_of("a\r\nb\r\n") == checksum_of("a\nb\n")


def test_two_migrations_cannot_share_a_version(tmp_path: Path) -> None:
    """Ambiguous about which ran, and the version table can only record one.

    Caught in `discover` rather than at the INSERT, where the error would name a primary key
    violation instead of the two files that caused it.
    """
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_second.sql").write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(ValueError, match="share a version number"):
        discover(tmp_path)


def test_a_file_that_is_not_a_migration_is_not_applied(tmp_path: Path) -> None:
    """A scratch `.sql` in the directory must not reach production because it was sitting there."""
    (tmp_path / "0001_real.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "scratch.sql").write_text("DROP TABLE lpr_outbox_events;", encoding="utf-8")
    (tmp_path / "0002-wrong-separator.sql").write_text("SELECT 2;", encoding="utf-8")

    found = discover(tmp_path)
    assert [migration.slug for migration in found] == ["real"]


def test_a_missing_migrations_directory_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    """An installed wheel has no `migrations/` beside it, and importing the runner must still work."""
    assert discover(tmp_path / "nothing-here") == ()
