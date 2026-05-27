"""Apply pending SQL migrations from migrations/ directory.

Reads files matching `NNN_*.sql` (zero-padded number, underscore, anything,
.sql), applies any not yet recorded in the `schema_migrations` tracking
table, in numeric order. Each migration runs in a transaction by default;
add a magic comment on the file's first line to opt out:

    -- migration:notransaction

The runner takes a Postgres advisory lock at startup so concurrent invocations
serialize (the second invocation waits for the first to finish, then sees
all migrations applied and exits).

Designed to be invoked as a Railway pre-start command:

    python scripts/apply_migrations.py && uvicorn ...

The runner exits 0 on success (including "nothing to do") and non-zero on
any failure. If a migration fails, the API does NOT start — the failed
deploy makes the problem visible immediately.

Manual escape hatches:
  - To re-run a migration: DELETE FROM schema_migrations WHERE
    filename = 'NNN_xxx.sql', then redeploy.
  - To bootstrap a new tracker against an already-deployed schema:
    set BOOTSTRAP_MIGRATIONS=true on the next deploy. The runner will
    mark all migrations EXCEPT those in BOOTSTRAP_EXCLUDE (default:
    the latest unmigrated one) as already-applied without running them.

See docs/decisions/ADR-0005-automated-migration-application.md for the
design rationale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg

# Path to the migrations directory, resolved relative to this script.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Postgres advisory lock key. Arbitrary but stable across the project.
# Chosen so it doesn't collide with anything pgvector or asyncpg uses
# internally. If you reuse advisory locks elsewhere in this codebase,
# pick a different key there.
ADVISORY_LOCK_KEY = 728304917

# Migrations matching this filename pattern are run. Files in
# migrations/ that don't match (READMEs, archived files) are ignored.
MIGRATION_FILENAME_RE = re.compile(r"^(\d{3,})_.+\.sql$")

# First-line magic comment to opt out of transaction wrapping.
NO_TRANSACTION_MARKER = "-- migration:notransaction"

# Migrations to mark as already-applied during a bootstrap. Override with
# the BOOTSTRAP_EXCLUDE env var (comma-separated filenames) if needed.
# Defaults to ["019_wcs_entity_substrate.sql"] because that's the only
# migration that has never been applied to production at the time the
# runner is introduced.
DEFAULT_BOOTSTRAP_EXCLUDE = ("019_wcs_entity_substrate.sql",)

logger = logging.getLogger("apply_migrations")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _normalize_database_url(raw_url: str) -> str:
    """Strip the SQLAlchemy `+asyncpg` driver suffix that asyncpg's connect
    function doesn't accept. The API code uses `postgresql+asyncpg://...`
    via SQLAlchemy; we want plain `postgresql://...` for asyncpg's connect.
    """
    if raw_url.startswith("postgresql+asyncpg://"):
        return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql://", 1)
    return raw_url


def _list_migration_files() -> list[Path]:
    """Return migration files in numeric order. Files not matching the
    NNN_*.sql pattern are ignored (with a warning if any are present)."""
    if not MIGRATIONS_DIR.exists():
        raise FileNotFoundError(f"Migrations directory not found: {MIGRATIONS_DIR}")

    files: list[Path] = []
    skipped: list[str] = []
    for path in MIGRATIONS_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix != ".sql":
            continue
        if MIGRATION_FILENAME_RE.match(path.name):
            files.append(path)
        else:
            skipped.append(path.name)

    if skipped:
        logger.warning(
            "Ignoring %d file(s) in migrations/ that don't match NNN_*.sql: %s",
            len(skipped),
            ", ".join(skipped),
        )

    # Sort by the numeric prefix to handle e.g. 001 vs 010 correctly.
    def _key(p: Path) -> int:
        m = MIGRATION_FILENAME_RE.match(p.name)
        assert m is not None  # filtered above
        return int(m.group(1))

    files.sort(key=_key)
    return files


def _is_transactional(sql: str) -> bool:
    """Inspect the migration's first line for the no-transaction marker."""
    first_line = sql.lstrip().split("\n", 1)[0].strip()
    return first_line != NO_TRANSACTION_MARKER


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    """Create the schema_migrations tracking table if it doesn't exist."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            duration_ms INTEGER
        )
        """
    )


async def _get_applied(conn: asyncpg.Connection) -> set[str]:
    """Return the set of filenames already recorded as applied."""
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {row["filename"] for row in rows}


async def _bootstrap(
    conn: asyncpg.Connection,
    all_migrations: list[Path],
    exclude: set[str],
) -> None:
    """Mark all migrations as applied EXCEPT those in `exclude`. Used
    when introducing the runner against a database whose schema is
    already at the latest version (minus the excluded migrations)."""
    to_mark = [m for m in all_migrations if m.name not in exclude]

    if not to_mark:
        logger.warning("Bootstrap requested but no migrations to mark.")
        return

    logger.info("Bootstrap: marking %d migration(s) as applied", len(to_mark))
    for m in to_mark:
        logger.info("  bootstrap-mark: %s", m.name)

    # Use INSERT ... ON CONFLICT DO NOTHING so re-running bootstrap is safe.
    await conn.executemany(
        "INSERT INTO schema_migrations (filename, duration_ms) VALUES ($1, NULL) "
        "ON CONFLICT (filename) DO NOTHING",
        [(m.name,) for m in to_mark],
    )

    skipped = [m.name for m in all_migrations if m.name in exclude]
    if skipped:
        logger.info(
            "Bootstrap: %d migration(s) NOT marked (will run on next normal apply): %s",
            len(skipped),
            ", ".join(skipped),
        )


async def _apply_one(conn: asyncpg.Connection, migration: Path) -> int:
    """Apply a single migration. Returns elapsed milliseconds.

    Transaction handling:
      - Default: wrap the migration body in a transaction, then INSERT
        into schema_migrations in the same transaction. Atomic.
      - If the file starts with `-- migration:notransaction`: run the
        body without a transaction, then INSERT the tracking row in a
        separate explicit transaction.
    """
    sql = migration.read_text()
    transactional = _is_transactional(sql)

    logger.info(
        "Applying %s (transactional=%s)",
        migration.name,
        transactional,
    )

    start = asyncio.get_event_loop().time()

    if transactional:
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (filename, duration_ms) VALUES ($1, $2)",
                migration.name,
                None,  # filled in after; we want the row inside the txn
            )
    else:
        # Run body outside any transaction (caller must not be in a txn).
        await conn.execute(sql)
        # Now record the application in its own transaction.
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO schema_migrations (filename, duration_ms) VALUES ($1, $2)",
                migration.name,
                None,
            )

    elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)

    # Update the duration_ms now that we know it.
    await conn.execute(
        "UPDATE schema_migrations SET duration_ms = $1 WHERE filename = $2",
        elapsed_ms,
        migration.name,
    )

    logger.info("Applied %s (%dms)", migration.name, elapsed_ms)
    return elapsed_ms


async def _run(database_url: str, bootstrap: bool, bootstrap_exclude: set[str]) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        # Serialize concurrent invocations via Postgres advisory lock.
        # pg_advisory_lock blocks until the lock is acquired; if a
        # previous deploy's runner is still applying migrations, we wait.
        logger.info("Acquiring advisory lock %d...", ADVISORY_LOCK_KEY)
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        logger.info("Advisory lock acquired.")

        try:
            await _ensure_tracking_table(conn)
            all_migrations = _list_migration_files()
            logger.info("Found %d migration file(s)", len(all_migrations))

            if bootstrap:
                logger.warning("BOOTSTRAP_MIGRATIONS=true — entering bootstrap mode")
                await _bootstrap(conn, all_migrations, bootstrap_exclude)
                # After bootstrap, fall through to normal apply: any migrations
                # NOT in bootstrap_exclude are now marked applied, anything
                # else will run normally.

            applied = await _get_applied(conn)
            pending = [m for m in all_migrations if m.name not in applied]

            if not pending:
                logger.info("No pending migrations.")
                return 0

            logger.info("%d pending migration(s):", len(pending))
            for m in pending:
                logger.info("  pending: %s", m.name)

            total_ms = 0
            for m in pending:
                total_ms += await _apply_one(conn, m)

            logger.info(
                "Applied %d migration(s) in %dms total.",
                len(pending),
                total_ms,
            )
            return 0
        finally:
            # Release the lock even if we error out.
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
            logger.info("Advisory lock released.")
    finally:
        await conn.close()


def _parse_bootstrap_exclude(raw: str | None) -> set[str]:
    """Parse BOOTSTRAP_EXCLUDE env var. Comma-separated filenames.
    Empty/missing -> use DEFAULT_BOOTSTRAP_EXCLUDE."""
    if not raw or not raw.strip():
        return set(DEFAULT_BOOTSTRAP_EXCLUDE)
    return {item.strip() for item in raw.split(",") if item.strip()}


def main() -> int:
    _setup_logging()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        logger.error("DATABASE_URL environment variable is not set.")
        return 2

    database_url = _normalize_database_url(raw_url)

    bootstrap = os.environ.get("BOOTSTRAP_MIGRATIONS", "").lower() in {
        "true",
        "1",
        "yes",
    }
    bootstrap_exclude = _parse_bootstrap_exclude(os.environ.get("BOOTSTRAP_EXCLUDE"))

    try:
        return asyncio.run(_run(database_url, bootstrap, bootstrap_exclude))
    except Exception as exc:  # pragma: no cover - error path
        logger.exception("Migration runner failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
