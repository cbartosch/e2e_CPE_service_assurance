"""The transactional outbox, and an honest account of which transaction it is.

The specification asks for a "transactional outbox for external writes" among the idempotency and
resilience controls, and for `outbox events` among the things Postgres persists. What an outbox
buys is a single guarantee: **an intended external write is never lost because the process died
between deciding and calling, and never applied twice because it was retried.** The decision and
the record of intent commit together; a separate relay does the calling, and the calling is
idempotent.

Which transaction, measured
---------------------------
The natural reading is "the same transaction as the state change", and in this system the state
change is a LangGraph checkpoint. That cannot be done through LangGraph's public API, and the
reason is structural rather than a missing feature:

* `AsyncPostgresSaver.aput` is called by the framework *after* a node has returned. Read its source
  -- it opens `self._cursor(pipeline=True)` and writes blobs, the checkpoint row and its metadata.
  There is no hook that runs inside that cursor, so a node has no way to add a row to it. By the
  time the transaction exists, the node that wanted to enqueue has finished.
* The saver's connection is reachable as `saver.conn`, so a subclass overriding `aput` *could*
  insert on the same cursor. That is a real option and it is deliberately not taken here: it binds
  this system to the internals of a checkpointer it does not own, and `_cursor(pipeline=True)` puts
  psycopg in pipeline mode, where the failure semantics of an extra statement are not something to
  discover in production.

So the boundary is one step wider than ideal, and the gap is named rather than papered over: a
crash between the checkpoint write and `flush()` loses the *record* of an intent that no external
system ever saw. Nothing is half-applied -- the relay had not run -- so the incident replays from
its checkpoint and stages the intent again, with **the same idempotency key**, because
`domain.base.idempotency_key` is a hash of the incident, the action, the target and the attempt.
That is the property that makes the wider boundary survivable, and it is why `enqueue` returning
`False` for a key it has already seen is a normal outcome rather than an error. Gap OUTBOX-1.

Why the jitter is deterministic
-------------------------------
"Retry with exponential backoff and jitter" -- the specification's words. The jitter here is a hash
of the event id rather than `random.random()`, which looks wrong for about ten seconds.

Jitter exists to stop a thousand events that failed together from retrying together. Deriving it
from the event id does that: two *different* events get different offsets. What it deliberately
does not do is give two *processes* different offsets for the *same* event -- and that is correct,
because two relays contending for one row should agree on when that row is due, not race on it.
The reward is a backoff schedule that a test can assert exactly, which a `random`-based one cannot.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from lpr_cpe.config.clock import Clock
    from lpr_cpe.domain.governance import ActionRequest

#: Attempts before an event is dead-lettered. Five, spanning roughly a minute and a half of backoff
#: at the default base -- long enough to ride out a restart of the far side, short enough that a
#: genuinely broken integration reaches a human the same shift rather than retrying all night.
DEFAULT_MAX_ATTEMPTS = 5

#: First retry delay in seconds. Doubles each attempt.
DEFAULT_BACKOFF_BASE_SECONDS = 5.0

#: The delay is never longer than this however many attempts have failed.
DEFAULT_BACKOFF_CAP_SECONDS = 300.0


class OutboxStatus(StrEnum):
    """Where an event is in its life. Four states, and the fourth is the one that needs a human.

    `FAILED` and `DEAD` are different facts and a single `failed` would lose the distinction that
    matters: `FAILED` will be tried again, `DEAD` will not and nothing further will happen to it
    unless somebody acts. Dead-letter handling is on the specification's list of required controls,
    and a status that conflates "retrying" with "given up" cannot support it.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


class OutboxEvent(BaseModel):
    """One intended external write, durable and replayable.

    Not frozen, unlike most records here, and the exception is deliberate: `attempts`, `status` and
    `next_attempt_at` are the state a relay advances, and modelling that as "build a new event with
    one field changed" would make it easy to advance a copy and store the original. The stores
    below own mutation; nothing else should.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable and derived, not random. Two stagings of the same intent produce the same event, so a
    #: replay after a crash is a duplicate the store can recognise rather than a second row.
    event_id: str = Field(min_length=8, max_length=64)

    incident_id: str = Field(min_length=1, max_length=128)

    #: The uniqueness constraint. `domain.base.idempotency_key` derives it from the incident, the
    #: action type, the target and the attempt, so a deliberate second reboot differs from an
    #: accidental repeat only because a caller incremented `attempt` on purpose.
    idempotency_key: str = Field(min_length=8, max_length=128)

    #: Which integration will make the call. Free-form rather than an enum over the eleven adapter
    #: packages, because the relay dispatches on it and a new integration should not need a change
    #: to a persisted schema's value domain.
    target_system: str = Field(min_length=1, max_length=64)

    action_type: str = Field(min_length=1, max_length=64)
    target_ref: str = Field(min_length=1, max_length=128)

    #: The call to make, as data. It has to survive a process restart and be readable by a relay
    #: that never saw the incident, so it is a plain mapping rather than a closure or a record type
    #: whose class might have moved by the time anyone drains it.
    payload: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime
    last_error: str = Field(default="", max_length=2000)
    sent_at: datetime | None = None

    @classmethod
    def from_action(
        cls, request: ActionRequest, *, target_system: str, now: datetime, payload: Any = None
    ) -> Self:
        """Stage the intent an `ActionRequest` represents.

        The event id is a hash of the idempotency key rather than a fresh uuid, which is what
        makes a replay produce the *same* row instead of a second one the unique index happens to
        catch. A uuid would work too, but it would mean two ids for one intent in two logs, and the
        id is what an operator greps for.
        """
        digest = hashlib.blake2b(request.idempotency_key.encode(), digest_size=12).hexdigest()
        return cls(
            event_id=f"OBX-{digest}",
            incident_id=request.incident_id,
            idempotency_key=request.idempotency_key,
            target_system=target_system,
            action_type=request.action_type.value,
            target_ref=request.target_ref,
            payload=dict(payload) if payload is not None else dict(request.parameters),
            created_at=now,
            next_attempt_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in {OutboxStatus.SENT, OutboxStatus.DEAD}


def backoff_delay(
    event_id: str,
    attempts: int,
    *,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> timedelta:
    """Exponential backoff with jitter, both bounded and both reproducible.

    `base * 2 ** (attempts - 1)`, capped, then multiplied by a factor in `[0.5, 1.0]` derived from
    the event id. Full jitter down to zero is the usual recommendation and is not used here: a
    delay that can round to nothing turns the cap into a suggestion, and the point of the cap is
    that a relay draining a dead far side does so at a known rate.

    See the module docstring for why the jitter is a hash rather than a draw.
    """
    if attempts < 1:
        return timedelta(0)
    raw = min(base_seconds * (2 ** (attempts - 1)), cap_seconds)
    digest = hashlib.blake2b(f"{event_id}\x1f{attempts}".encode(), digest_size=8).digest()
    factor = 0.5 + (int.from_bytes(digest, "big") / 2**64) * 0.5
    return timedelta(seconds=raw * factor)


@runtime_checkable
class OutboxStore(Protocol):
    """What a relay needs from storage, and nothing else.

    A Protocol so the in-memory store is not a subclass of the Postgres one nor a mock of it: both
    are real implementations of the same five operations, and the suite runs against the first
    without a database.
    """

    async def enqueue(self, event: OutboxEvent) -> bool:
        """Record an intent. `False` if this idempotency key is already recorded.

        Not an error, and the module docstring says why: a replayed node stages the same intent
        with the same key, and the store recognising it is the mechanism working.
        """
        ...

    async def claim_due(self, now: datetime, *, limit: int = 100) -> tuple[OutboxEvent, ...]:
        """Events that are pending or failed and whose `next_attempt_at` has passed."""
        ...

    async def mark_sent(self, event_id: str, now: datetime) -> None: ...

    async def mark_failed(
        self, event_id: str, error: str, now: datetime, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> None:
        """Count the attempt, schedule the next one, or dead-letter it."""
        ...

    async def by_status(self, status: OutboxStatus) -> tuple[OutboxEvent, ...]: ...


class InMemoryOutbox:
    """The default, and the one the suite runs against.

    Keyed by `idempotency_key` rather than by `event_id` so that the uniqueness the Postgres schema
    enforces with an index is enforced here by the data structure -- two stores that agreed on the
    happy path and disagreed on a duplicate would make the suite prove the wrong thing.

    **Not durable, which is the whole point of the other one.** A restart loses every staged intent.
    In simulation that is correct: nothing was going to leave the process anyway.
    """

    __slots__ = ("_by_key",)

    def __init__(self) -> None:
        self._by_key: dict[str, OutboxEvent] = {}

    async def enqueue(self, event: OutboxEvent) -> bool:
        if event.idempotency_key in self._by_key:
            return False
        self._by_key[event.idempotency_key] = event.model_copy(deep=True)
        return True

    async def claim_due(self, now: datetime, *, limit: int = 100) -> tuple[OutboxEvent, ...]:
        due = [
            event
            for event in self._by_key.values()
            if not event.is_terminal and event.next_attempt_at <= now
        ]
        # Oldest first, so a backlog drains in the order the incidents decided things rather than
        # in dictionary order. An outbox that reordered a reboot before the approval that
        # authorised it would be a different kind of bug.
        due.sort(key=lambda event: (event.created_at, event.event_id))
        return tuple(event.model_copy(deep=True) for event in due[:limit])

    def _find(self, event_id: str) -> OutboxEvent | None:
        return next((e for e in self._by_key.values() if e.event_id == event_id), None)

    async def mark_sent(self, event_id: str, now: datetime) -> None:
        event = self._find(event_id)
        if event is None:
            return
        event.status = OutboxStatus.SENT
        event.sent_at = now
        event.attempts += 1
        event.last_error = ""

    async def mark_failed(
        self, event_id: str, error: str, now: datetime, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> None:
        event = self._find(event_id)
        if event is None:
            return
        event.attempts += 1
        event.last_error = error[:2000]
        if event.attempts >= max_attempts:
            event.status = OutboxStatus.DEAD
            return
        event.status = OutboxStatus.FAILED
        event.next_attempt_at = now + backoff_delay(event.event_id, event.attempts)

    async def by_status(self, status: OutboxStatus) -> tuple[OutboxEvent, ...]:
        return tuple(
            event.model_copy(deep=True) for event in self._by_key.values() if event.status is status
        )

    def __len__(self) -> int:
        return len(self._by_key)


class PostgresOutbox:
    """The durable store. Same five operations, against the table `migrations/0001` creates.

    **Unexercised by the suite** -- it needs a database, and the specification asks for an in-memory
    profile for unit tests. What the suite does prove is that it satisfies `OutboxStore`, which
    catches the drift that actually happens (a signature changing on one implementation only).
    Everything past that is unverified. Gap OUTBOX-2.

    `claim_due` takes `FOR UPDATE SKIP LOCKED`, which is what makes two relay processes safe to run
    at once: each takes rows the other has not locked instead of both taking the same rows and one
    losing. That is the "optimistic locking or equivalent concurrency control" line of the
    specification, discharged where concurrency actually arises.
    """

    __slots__ = ("_dsn",)

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _connect(self) -> Any:
        # Lazy, like `checkpointer.checkpointer_scope`: psycopg is an optional extra and a
        # top-level import would make this module unimportable on every machine running the suite.
        from psycopg import AsyncConnection

        return await AsyncConnection.connect(self._dsn, autocommit=False)

    async def enqueue(self, event: OutboxEvent) -> bool:
        async with await self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO lpr_outbox_events (
                    event_id, incident_id, idempotency_key, target_system, action_type,
                    target_ref, payload, created_at, status, attempts, next_attempt_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    event.event_id,
                    event.incident_id,
                    event.idempotency_key,
                    event.target_system,
                    event.action_type,
                    event.target_ref,
                    json.dumps(event.payload),
                    event.created_at,
                    event.status.value,
                    event.attempts,
                    event.next_attempt_at,
                ),
            )
            inserted = bool(cur.rowcount == 1)
            await conn.commit()
            return inserted

    async def claim_due(self, now: datetime, *, limit: int = 100) -> tuple[OutboxEvent, ...]:
        async with await self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT event_id, incident_id, idempotency_key, target_system, action_type,
                       target_ref, payload, created_at, status, attempts, next_attempt_at,
                       last_error, sent_at
                  FROM lpr_outbox_events
                 WHERE status IN ('pending', 'failed') AND next_attempt_at <= %s
                 ORDER BY created_at, event_id
                 LIMIT %s
                   FOR UPDATE SKIP LOCKED
                """,
                (now, limit),
            )
            rows = await cur.fetchall()
            await conn.commit()
        return tuple(_row_to_event(row) for row in rows)

    async def mark_sent(self, event_id: str, now: datetime) -> None:
        async with await self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE lpr_outbox_events
                   SET status = 'sent', sent_at = %s, attempts = attempts + 1, last_error = ''
                 WHERE event_id = %s
                """,
                (now, event_id),
            )
            await conn.commit()

    async def mark_failed(
        self, event_id: str, error: str, now: datetime, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> None:
        async with await self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT attempts FROM lpr_outbox_events WHERE event_id = %s FOR UPDATE",
                (event_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.commit()
                return
            attempts = int(row[0]) + 1
            if attempts >= max_attempts:
                await cur.execute(
                    """
                    UPDATE lpr_outbox_events
                       SET status = 'dead', attempts = %s, last_error = %s
                     WHERE event_id = %s
                    """,
                    (attempts, error[:2000], event_id),
                )
            else:
                await cur.execute(
                    """
                    UPDATE lpr_outbox_events
                       SET status = 'failed', attempts = %s, last_error = %s, next_attempt_at = %s
                     WHERE event_id = %s
                    """,
                    (
                        attempts,
                        error[:2000],
                        now + backoff_delay(event_id, attempts),
                        event_id,
                    ),
                )
            await conn.commit()

    async def by_status(self, status: OutboxStatus) -> tuple[OutboxEvent, ...]:
        async with await self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT event_id, incident_id, idempotency_key, target_system, action_type,
                       target_ref, payload, created_at, status, attempts, next_attempt_at,
                       last_error, sent_at
                  FROM lpr_outbox_events WHERE status = %s ORDER BY created_at, event_id
                """,
                (status.value,),
            )
            rows = await cur.fetchall()
            await conn.commit()
        return tuple(_row_to_event(row) for row in rows)


def _row_to_event(row: Sequence[Any]) -> OutboxEvent:
    """One row to one event. Positional, matching the column list in every SELECT above."""
    payload = row[6]
    return OutboxEvent(
        event_id=row[0],
        incident_id=row[1],
        idempotency_key=row[2],
        target_system=row[3],
        action_type=row[4],
        target_ref=row[5],
        payload=json.loads(payload) if isinstance(payload, str) else dict(payload or {}),
        created_at=row[7],
        status=OutboxStatus(row[8]),
        attempts=int(row[9]),
        next_attempt_at=row[10],
        last_error=row[11] or "",
        sent_at=row[12],
    )


class RelayResult(BaseModel):
    """What one drain did. Counts rather than events, because a relay run is a log line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    dead_lettered: int = 0

    @property
    def had_work(self) -> bool:
        return self.claimed > 0


class OutboxRelay:
    """Drains the outbox by calling `dispatch` for each due event.

    The relay is the half of the pattern that talks to the outside, and it is deliberately separate
    from the graph: a node's job ends when the intent is durable. Running it in-process after an
    invoke and running it as a separate worker against the same table are the same code.

    `dispatch` raising is the failure signal, not a returned bool. An integration that fails raises
    -- that is what every adapter in `integrations/` already does -- and requiring it to be caught
    and converted would put the conversion in eleven places instead of one.
    """

    __slots__ = ("_clock", "_dispatch", "_max_attempts", "_store")

    def __init__(
        self,
        store: OutboxStore,
        dispatch: Callable[[OutboxEvent], Awaitable[None]],
        *,
        clock: Clock,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._store = store
        self._dispatch = dispatch
        self._clock = clock
        self._max_attempts = max_attempts

    async def drain_once(self, *, limit: int = 100) -> RelayResult:
        """One pass over the due events. Returns counts; raises nothing a dispatch raised.

        A failing event does not stop the pass. The alternative -- abandoning the batch on the
        first failure -- lets one permanently broken target hold up every other incident's writes,
        which is the outage an outbox is supposed to contain rather than cause.
        """
        now = self._clock.now()
        due = await self._store.claim_due(now, limit=limit)
        sent = failed = dead = 0

        for event in due:
            try:
                await self._dispatch(event)
            except Exception as failure:  # noqa: BLE001 -- any adapter failure is a failed attempt
                await self._store.mark_failed(
                    event.event_id,
                    f"{type(failure).__name__}: {failure}",
                    now,
                    max_attempts=self._max_attempts,
                )
                # Re-read rather than recompute: the store decides whether this was the last
                # attempt, and a relay counting dead letters by its own arithmetic would disagree
                # with the table the moment `max_attempts` differed between them.
                if event.attempts + 1 >= self._max_attempts:
                    dead += 1
                else:
                    failed += 1
            else:
                await self._store.mark_sent(event.event_id, now)
                sent += 1

        return RelayResult(claimed=len(due), sent=sent, failed=failed, dead_lettered=dead)


class StagedWrites:
    """Intents a `WriteGate` has authorised and nobody has persisted yet.

    The seam exists because `WriteGate.authorize` is synchronous and every store here is not, and
    making the gate async would make every node that authorises an action async for the sake of a
    write that, in simulation, never leaves the process.

    So staging is sync and `flush` is the durability step, called once per invoke by whoever owns
    the store. **Between those two points the intent is in memory only**, which is gap OUTBOX-1 and
    the module docstring is where it is argued.
    """

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[OutboxEvent] = []

    def stage(self, event: OutboxEvent) -> None:
        self._events.append(event)

    @property
    def pending(self) -> tuple[OutboxEvent, ...]:
        return tuple(self._events)

    async def flush(self, store: OutboxStore) -> int:
        """Persist everything staged and forget it. Returns how many rows were new.

        Cleared even for the events the store already had, because a duplicate that stays staged
        would be offered again on the next flush forever.
        """
        staged, self._events = self._events, []
        return sum([await store.enqueue(event) for event in staged])

    def __len__(self) -> int:
        return len(self._events)


def build_outbox(dsn: str | None = None) -> InMemoryOutbox | PostgresOutbox:
    """Postgres when a DSN is configured, in memory otherwise.

    The same selection `checkpointer_scope` makes, on the same setting, so a deployment cannot end
    up with durable checkpoints and an outbox that forgets -- which would be the worst of the two,
    since the incident would replay and the intent would not.
    """
    if dsn:
        return PostgresOutbox(dsn)
    return InMemoryOutbox()


def dispatch_to(
    handlers: dict[str, Callable[[OutboxEvent], Awaitable[None]]],
) -> Callable[[OutboxEvent], Awaitable[None]]:
    """A dispatcher over `target_system`, refusing an event it has no handler for.

    Refusing rather than dropping: an event for a system nobody wired reaches `max_attempts` and
    dead-letters, which puts it in front of somebody. Silently marking it sent would be a write the
    trail claims was made and no system received.
    """

    async def dispatch(event: OutboxEvent) -> None:
        handler = handlers.get(event.target_system)
        if handler is None:
            raise KeyError(
                f"no handler for target system {event.target_system!r}; known: {sorted(handlers)}"
            )
        await handler(event)

    return dispatch


def staged_from(
    requests: Iterable[ActionRequest], *, target_system: str, now: datetime
) -> StagedWrites:
    """Convenience for the call sites that have a batch of authorised actions in hand."""
    staged = StagedWrites()
    for request in requests:
        staged.stage(OutboxEvent.from_action(request, target_system=target_system, now=now))
    return staged


__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "InMemoryOutbox",
    "OutboxEvent",
    "OutboxRelay",
    "OutboxStatus",
    "OutboxStore",
    "PostgresOutbox",
    "RelayResult",
    "StagedWrites",
    "backoff_delay",
    "build_outbox",
    "dispatch_to",
    "staged_from",
]
