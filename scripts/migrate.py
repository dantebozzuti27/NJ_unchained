"""Idempotent SQL migration runner.

Discovers ``db/migrations/NNN_*.sql`` and ``db/seeds/NNN_*.sql`` files in
the repository, applies them in lexical (== numeric) order against the
``$PG_DSN`` Postgres database, and records each application in
``governance.schema_migrations``.

Idempotency contract
--------------------
1. A migration whose ``migration_id`` already appears in
   ``governance.schema_migrations`` AND whose sha256 matches the recorded
   one is silently skipped.
2. A migration whose ``migration_id`` already appears AND whose sha256
   *differs* from the recorded one **fails loudly**. This catches edits
   to a previously-shipped migration -- which would silently desync any
   already-deployed environment from the source tree.
3. A new migration (no ledger entry) is applied inside a single
   transaction; on failure the transaction is rolled back and the ledger
   is untouched.

Bootstrap
---------
``governance.schema_migrations`` is created by migration ``001_initial_schema``.
For a brand-new database, this runner first checks whether the table
exists; if not, it applies migration 001 unconditionally (and records it
afterwards).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
SEEDS_DIR = REPO_ROOT / "db" / "seeds"
_MIGRATION_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------


def discover(directory: Path) -> list[Path]:
    """Return migration / seed files in *directory*, in lexical (numeric) order.

    Files that do not match the ``NNN_<snake_case>.sql`` pattern are
    silently ignored (so editor backup files do not break the runner).
    """
    if not directory.exists():
        return []
    matched: list[Path] = []
    for f in sorted(directory.glob("*.sql")):
        if _MIGRATION_FILE_RE.match(f.name):
            matched.append(f)
        else:
            log.warning("Ignoring %s -- does not match NNN_<name>.sql", f)
    return matched


def migration_id(path: Path) -> str:
    """Strip the .sql suffix to produce the canonical migration_id."""
    return path.stem


def sha256_text(path: Path) -> str:
    """SHA-256 of *path* contents (text, but we hash bytes for determinism)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------------


def _ledger_exists(conn: psycopg.Connection) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'governance' AND table_name = 'schema_migrations'"
    )
    return cur.fetchone() is not None


def _apply_one(conn: psycopg.Connection, path: Path) -> int:
    """Apply a single migration file. Returns wall-clock duration in ms.

    Each migration is responsible for its own BEGIN/COMMIT (every migration
    in this repository wraps its DDL in a transaction). We do NOT wrap the
    file in another transaction here because some DDL (e.g. CREATE INDEX
    CONCURRENTLY in future migrations) cannot run inside a transaction.
    """
    sql = path.read_text(encoding="utf-8")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(sql)
    return int((time.monotonic() - t0) * 1000)


def _record(conn: psycopg.Connection, mig_id: str, sha: str, duration_ms: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO governance.schema_migrations "
            "(migration_id, sha256, duration_ms) VALUES (%s, %s, %s)",
            (mig_id, sha, duration_ms),
        )


def _check_existing(conn: psycopg.Connection, mig_id: str, sha: str) -> str | None:
    """Return the recorded sha256 if *mig_id* is already applied, else None."""
    cur = conn.execute(
        "SELECT sha256 FROM governance.schema_migrations WHERE migration_id = %s",
        (mig_id,),
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])


# ----------------------------------------------------------------------------
# Public entrypoints
# ----------------------------------------------------------------------------


def apply_migrations(conn: psycopg.Connection, files: list[Path]) -> list[tuple[str, str]]:
    """Apply each migration in *files* in order. Returns [(migration_id, action)].

    ``action`` is one of ``"applied"`` or ``"skipped"``. Raises immediately
    on sha256 mismatch (action would be ``"drift"``).
    """
    out: list[tuple[str, str]] = []
    bootstrap = not _ledger_exists(conn)

    for path in files:
        mig_id = migration_id(path)
        sha = sha256_text(path)

        if bootstrap:
            # First migration ever: ledger does not yet exist. Apply 001
            # unconditionally; it will create the ledger; then record.
            log.info("[bootstrap] applying %s", mig_id)
            duration_ms = _apply_one(conn, path)
            conn.commit()
            _record(conn, mig_id, sha, duration_ms)
            conn.commit()
            out.append((mig_id, "applied"))
            bootstrap = False
            continue

        recorded = _check_existing(conn, mig_id, sha)
        if recorded is None:
            log.info("applying %s", mig_id)
            duration_ms = _apply_one(conn, path)
            conn.commit()
            _record(conn, mig_id, sha, duration_ms)
            conn.commit()
            out.append((mig_id, "applied"))
        elif recorded == sha:
            log.info("skipping %s (already applied)", mig_id)
            out.append((mig_id, "skipped"))
        else:
            raise RuntimeError(
                f"DRIFT DETECTED: migration {mig_id} was previously applied with "
                f"sha256={recorded!r} but its current file sha256 is {sha!r}. "
                "The migration file was edited after deployment. Either revert "
                "the edit or roll a new migration with a higher number."
            )
    return out


def status(conn: psycopg.Connection, files: list[Path]) -> list[tuple[str, str]]:
    """Return [(migration_id, status)] for each file.

    ``status`` is one of ``"applied"``, ``"pending"``, or ``"drift"``.
    """
    out: list[tuple[str, str]] = []
    if not _ledger_exists(conn):
        return [(migration_id(p), "pending") for p in files]
    for path in files:
        mig_id = migration_id(path)
        sha = sha256_text(path)
        recorded = _check_existing(conn, mig_id, sha)
        if recorded is None:
            out.append((mig_id, "pending"))
        elif recorded == sha:
            out.append((mig_id, "applied"))
        else:
            out.append((mig_id, "drift"))
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Idempotent SQL migration runner."""


def _connect(dsn: str) -> psycopg.Connection:
    import psycopg

    return psycopg.connect(dsn)


@cli.command("apply")
@click.option("--dsn", envvar="PG_DSN", required=True)
def apply_cmd(dsn: str) -> None:
    """Apply all pending migrations in db/migrations/."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    files = discover(MIGRATIONS_DIR)
    if not files:
        click.echo("No migrations found.")
        return
    with _connect(dsn) as conn:
        try:
            results = apply_migrations(conn, files)
        except RuntimeError as exc:
            log.error(str(exc))
            sys.exit(2)
    for mig_id, action in results:
        click.echo(f"{action:>8s}  {mig_id}")


@cli.command("seed")
@click.option("--dsn", envvar="PG_DSN", required=True)
def seed_cmd(dsn: str) -> None:
    """Apply all pending seeds in db/seeds/."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    files = discover(SEEDS_DIR)
    if not files:
        click.echo("No seeds found.")
        return
    with _connect(dsn) as conn:
        try:
            results = apply_migrations(conn, files)
        except RuntimeError as exc:
            log.error(str(exc))
            sys.exit(2)
    for mig_id, action in results:
        click.echo(f"{action:>8s}  {mig_id}")


@cli.command("status")
@click.option("--dsn", envvar="PG_DSN", required=True)
def status_cmd(dsn: str) -> None:
    """Show pending / applied / drift for each migration."""
    files = discover(MIGRATIONS_DIR)
    with _connect(dsn) as conn:
        results = status(conn, files)
    for mig_id, action in results:
        marker = {"applied": "OK ", "pending": "..", "drift": "!! "}[action]
        click.echo(f"{marker} {mig_id:40s} {action}")


if __name__ == "__main__":
    cli()
