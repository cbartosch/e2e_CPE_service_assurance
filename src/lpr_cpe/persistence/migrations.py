"""Applying `migrations/*.sql` in order, once each, and noticing when one has been edited.

"Provide database migrations" is one line of the specification and about forty of anything that
works. What is here is deliberately small -- forward-only SQL files, a version table, a checksum --
because the alternative is a dependency on Alembic for a schema with no ORM behind it, and Alembic's
value is autogeneration from models this system does not have.

Forward-only, and no `down`
---------------------------
There is no rollback path and that is a decision rather than an omission. A `down` migration is
written when the schema is designed and run, if ever, in an incident at 3am against a database whose
contents nobody predicted; the ones that drop a column destroy the data the rollback was meant to
save. Restoring from a backup and rolling the application back is the recovery procedure, and it is
the one that gets rehearsed.

The checksum is the interesting part
------------------------------------
`lpr_schema_migrations` records a sha256 of each file's text. Applying a migration whose recorded
checksum differs from the file on disk **raises** rather than skipping quietly, because that
divergence has exactly one cause -- somebody edited an applied migration -- and exactly one
symptom, which is that the change is present on the machine where it was written and absent
everywhere else. It is the failure a version-number-only scheme cannot see.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

#: `migrations/` at the repository root, three parents up from this file. Resolved rather than
#: assumed relative to the working directory: a migration runner invoked from a container's `/` has
#: to find the same files as one invoked from a checkout.
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

#: `0001_persistence.sql` -> version `0001`. A file that does not match is not a migration, which is
#: what keeps a stray `.sql` scratch file from being applied to production because it was in the
#: directory.
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")


class Migration(NamedTuple):
    """One file, with the two things the version table stores about it."""

    version: str
    slug: str
    path: Path
    sql: str
    checksum: str


def checksum_of(sql: str) -> str:
    """sha256 of the file's text, newline-normalised.

    Normalised because this repository is developed on Windows and deployed on Linux, and a
    checksum that changed with the line endings would fire the tamper check on every checkout
    rather than on an edit -- an alarm that goes off constantly is one nobody reads.
    """
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: Path | None = None) -> tuple[Migration, ...]:
    """Every migration in the directory, in version order.

    Sorted by the version string rather than by filename so that the ordering is the one the numbers
    declare. They coincide today; they stop coinciding the moment somebody renames a slug.
    """
    root = directory or MIGRATIONS_DIR
    if not root.is_dir():
        return ()

    found: list[Migration] = []
    for path in sorted(root.iterdir()):
        match = _FILENAME.match(path.name)
        if match is None:
            continue
        sql = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=match.group("version"),
                slug=match.group("slug"),
                path=path,
                sql=sql,
                checksum=checksum_of(sql),
            )
        )

    versions = [migration.version for migration in found]
    duplicates = {version for version in versions if versions.count(version) > 1}
    if duplicates:
        # Two files claiming one version is ambiguous about which ran, and the version table cannot
        # record both. Caught here rather than at the INSERT, where the error would name a primary
        # key rather than the two files.
        raise ValueError(f"two migrations share a version number: {sorted(duplicates)}")

    return tuple(sorted(found, key=lambda migration: migration.version))


class MigrationTamperedError(RuntimeError):
    """An applied migration's file no longer matches what was applied.

    Its own exception because the operator response is specific and is not "re-run the migration":
    the change in the file has not been applied anywhere it was already run, so it needs a *new*
    migration, and the edit needs reverting.
    """


def plan(applied: dict[str, str], available: Sequence[Migration]) -> tuple[Migration, ...]:
    """What still needs applying, having checked what already was.

    Separated from the database so it is testable without one -- which matters, because the tamper
    check is the part most worth a test and the part a Postgres-only implementation would leave
    unexercised.

    `applied` maps version to the checksum recorded at the time.
    """
    pending: list[Migration] = []
    for migration in available:
        recorded = applied.get(migration.version)
        if recorded is None:
            pending.append(migration)
            continue
        if recorded != migration.checksum:
            raise MigrationTamperedError(
                f"migration {migration.version} ({migration.path.name}) was applied with checksum "
                f"{recorded[:12]}... and the file on disk now hashes to {migration.checksum[:12]}"
                "... An applied migration has been edited, so this change is present where it was "
                "written and missing everywhere else. Revert the edit and add a new migration."
            )
    return tuple(pending)


_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS lpr_schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT        NOT NULL
)
"""


async def applied_versions(conn: Any) -> dict[str, str]:
    """Version to checksum, for what this database has already run."""
    async with conn.cursor() as cur:
        await cur.execute(_BOOTSTRAP)
        await cur.execute("SELECT version, checksum FROM lpr_schema_migrations")
        rows = await cur.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


async def apply_all(dsn: str, *, directory: Path | None = None) -> tuple[str, ...]:
    """Apply every pending migration and return the versions applied.

    **Each migration and its version row commit together.** That is the one thing a migration runner
    has to get right: a schema change that committed without its version row would be re-applied on
    the next start, and `CREATE TABLE IF NOT EXISTS` makes that survivable only until the first
    migration that does something else.

    Unexercised against a real database -- gap OUTBOX-2 covers it along with `PostgresOutbox`. What
    the suite does prove is `discover` and `plan`, which is where the logic is; this function is the
    part that is only a transaction boundary.
    """
    from psycopg import AsyncConnection

    available = discover(directory)
    async with await AsyncConnection.connect(dsn, autocommit=False) as conn:
        pending = plan(await applied_versions(conn), available)
        for migration in pending:
            async with conn.cursor() as cur:
                await cur.execute(migration.sql)
                await cur.execute(
                    "INSERT INTO lpr_schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
            await conn.commit()
    return tuple(migration.version for migration in pending)


__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationTamperedError",
    "applied_versions",
    "apply_all",
    "checksum_of",
    "discover",
    "plan",
]
