"""Where an incident's state is kept between super-steps, and how the two backends are chosen.

The specification asks for in-memory persistence for local tests and PostgreSQL for production,
behind one selection point. This is that point.

The Postgres import is deferred into the factory body and that is not tidiness. Importing
`langgraph.checkpoint.postgres` raises `ImportError: no pq wrapper available` unless `libpq` is
present -- the bare `psycopg` wheel is not enough, `psycopg[binary]` is. A module-level import would
make the *in-memory* path unusable on any machine without Postgres client libraries, including CI,
for a backend it was never going to touch.

Both backends are built with the same serialiser, from `serde.build_serde`. That is load-bearing
rather than symmetric-looking: the in-memory saver is what every test resumes through, so if it were
constructed with a laxer serde than production, the tests would pass on objects that arrive back
from Postgres as bare dicts. The one place a type could quietly stop round-tripping is the one place
both backends must agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import InMemorySaver

from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.persistence.serde import build_serde

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


def build_memory_checkpointer() -> InMemorySaver:
    """The local and test backend. Explicit rather than reached only through `build_checkpointer`.

    Tests want an in-memory saver regardless of what `LPR_POSTGRES_DSN` happens to hold in the
    environment they run in, and routing them through the DSN-sniffing factory would make the suite
    depend on an environment variable it never sets.
    """
    return InMemorySaver(serde=build_serde())


def build_checkpointer(settings: Settings | None = None) -> BaseCheckpointSaver:  # type: ignore[type-arg]
    """Select a backend from settings: Postgres when a DSN is configured, in-memory otherwise.

    Returns the saver **unopened**. `AsyncPostgresSaver` needs `await saver.setup()` and a live
    connection, and doing that here would make a synchronous factory perform I/O -- so the caller
    that owns the application lifespan owns the connection too.
    """
    resolved = settings or get_settings()
    if not resolved.postgres_dsn:
        return build_memory_checkpointer()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver.from_conn_string(  # type: ignore[return-value]
        resolved.postgres_dsn, serde=build_serde()
    )
