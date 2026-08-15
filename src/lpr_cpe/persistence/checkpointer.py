"""Where an incident's state is kept between super-steps, and how the two backends are chosen.

The specification asks for in-memory persistence for local tests and PostgreSQL for production,
behind one selection point. This is that point.

The Postgres import is deferred into the function body and that is not tidiness. Importing
`langgraph.checkpoint.postgres` raises `ImportError: no pq wrapper available` unless `libpq` is
present -- the bare `psycopg` wheel is not enough, `psycopg[binary]` is. A module-level import would
make the *in-memory* path unusable on any machine without Postgres client libraries, including CI,
for a backend it was never going to touch.

Both backends are built with the same serialiser, from `serde.build_serde`. That is load-bearing
rather than symmetric-looking: the in-memory saver is what every test resumes through, so if it were
constructed with a laxer serde than production, the tests would pass on objects that arrive back
from Postgres as bare dicts. The one place a type could quietly stop round-tripping is the one place
both backends must agree.

Why this is a scope and not a factory
-------------------------------------
An earlier version of this module was a plain function returning a saver. It could not work, and
the reason is worth keeping. Measured on langgraph-checkpoint-postgres 3.1.2:

    AsyncPostgresSaver.from_conn_string(dsn, serde=...)
        -> contextlib._AsyncGeneratorContextManager   # NOT a saver
        isinstance(result, AsyncPostgresSaver)  is False
    AsyncPostgresSaver.__init__(conn, pipe=None, serde=None)   # wants a live connection
    AsyncPostgresSaver.setup()  -> coroutine; version-tracked DDL; its own docstring says it
                                   "MUST be called directly by the user the first time"

`from_conn_string` is an `@asynccontextmanager`, so it *has* to be entered -- the connection is
opened on `__aenter__` and closed on `__aexit__`. A synchronous factory has nowhere to put either
half. Returning the unentered context manager, as the earlier version did, would have handed
`StateGraph.compile(checkpointer=...)` an object with no `aget_tuple`, and every incident in
production would have failed to checkpoint while every test kept passing on the in-memory path.

So the caller that owns the application lifespan owns the connection, and says so with `async with`.

How the mistake survived review
-------------------------------
The bad return carried `# type: ignore[return-value]`, which reads like a considered decision and
was not one. `langgraph.*` is under `ignore_missing_imports` and the postgres extra is optional, so
on a machine without it `AsyncPostgresSaver` is `Any` -- and an ignore over an `Any` silences
nothing, because there was no `return-value` error to silence. mypy said exactly that, twice, and
it was not read:

    error: Unused "type: ignore" comment  [unused-ignore]
    error: Returning Any from function declared to return "BaseCheckpointSaver[Any]"

A suppression whose error code does not match the error being reported is a suppression pointed at
a bug the author has not identified. `unused-ignore` is the only warning a type checker can give
for that, and it is worth treating as a failure rather than as noise.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import InMemorySaver

from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.persistence.serde import build_serde

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from langgraph.checkpoint.base import BaseCheckpointSaver


def build_memory_checkpointer() -> InMemorySaver:
    """The local and test backend. Explicit rather than reached only through `checkpointer_scope`.

    Tests want an in-memory saver regardless of what `LPR_POSTGRES_DSN` happens to hold in the
    environment they run in, and routing them through the DSN-sniffing scope would make the suite
    depend on an environment variable it never sets. It needs no lifecycle: there is no connection
    to open, so a bare constructor is the honest shape for this one.
    """
    return InMemorySaver(serde=build_serde())


@asynccontextmanager
async def checkpointer_scope(
    settings: Settings | None = None, *, setup: bool = True
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Open the configured checkpointer for the lifetime of the block, and close it after.

    Postgres when `LPR_POSTGRES_DSN` is set, in-memory otherwise -- the one selection point the
    specification asks for. This is an async context manager because the Postgres saver genuinely
    has a lifecycle (see the module docstring); the in-memory branch yields immediately and has
    nothing to unwind, which keeps the two backends interchangeable at every call site.

    `setup=False` is for a deployment that runs DDL separately -- the `migrations/` path, or a
    database whose application role has no `CREATE TABLE` right. The default is `True` because
    `setup()` is version-tracked: it reads `checkpoint_migrations` and applies only what is newer,
    so calling it on every start is a query, not a re-migration.
    """
    resolved = settings or get_settings()
    if not resolved.postgres_dsn:
        yield build_memory_checkpointer()
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(
        resolved.postgres_dsn, serde=build_serde()
    ) as saver:
        if setup:
            await saver.setup()
        yield saver
