"""SQLite access layer: connections, WAL, and forward-only migrations.

Startup never wipes an existing database. Migrations are applied in filename order
and recorded in schema_migrations; an already-applied file is skipped.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def db_path() -> Path:
    """Resolved database path. Read from the environment each call so tests can redirect it."""
    return Path(os.environ.get("SHOPWATCH_DB", "data/shopwatch.db"))


def utcnow() -> str:
    """Timestamp format used everywhere: UTC, second resolution, sortable as text."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection. Commits on clean exit, rolls back on exception."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}


def migrate(path: Path | None = None) -> list[str]:
    """Apply any unapplied migration files. Returns the filenames applied this call."""
    applied_now: list[str] = []
    conn = connect(path)
    try:
        done = _applied(conn)
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in done:
                continue
            conn.executescript(sql_file.read_text())
            violation = conn.execute("PRAGMA foreign_key_check").fetchone()
            if violation:
                raise sqlite3.IntegrityError(
                    f"foreign-key violation after {sql_file.name}: {tuple(violation)}"
                )
            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (sql_file.name, utcnow()),
            )
            conn.commit()
            applied_now.append(sql_file.name)
    finally:
        conn.close()
    return applied_now


def backup(destination: Path | None = None, path: Path | None = None) -> Path:
    """Consistent online backup via SQLite's own backup API (safe while the app runs)."""
    source = Path(path) if path is not None else db_path()
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = source.parent / "backups" / f"shopwatch-{stamp}.db"
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return destination


def restore(backup_file: Path, path: Path | None = None) -> Path:
    """Restore a backup over the live database, keeping a copy of what was replaced.

    Stop the application first. SQLite will not notice the file underneath an open
    connection changing, and a process still holding the old handle reports a disk I/O
    error rather than serving stale data, which is the better failure but still a failure.
    """
    target = Path(path) if path is not None else db_path()
    backup_file = Path(backup_file)
    if not backup_file.exists():
        raise FileNotFoundError(f"no such backup: {backup_file}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(target, target.with_suffix(f".replaced-{stamp}.db"))
    shutil.copy2(backup_file, target)
    # A stale -wal/-shm pair from the replaced database would shadow the restored file.
    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists():
            side.unlink()
    return target
