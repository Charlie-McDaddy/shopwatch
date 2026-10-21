"""LLM helper jobs: turn a rough product description into a short list of real
candidate model numbers, for the "Track something new" wizard's model field.

A job's lifecycle lives here, in this container - same split as app/research.py. The
actual CLI call runs on the host research runner, outside this container, through this
same HTTP API. No CLI credential enters the web container.

"Exact model" is used to match price imports (store.normalise_model), so a wrong
candidate silently accepted would corrupt matching later. Every candidate is therefore
presented to the person as something to verify, never applied automatically - this
module only ever returns suggestions into the wizard's own text field.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import utcnow

STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED")

#: model_suggestion: rough description -> candidate model numbers, for the wizard's
#: "basics" step. retailer_discovery: product name/model + retailers already checked ->
#: other real AU retailers likely selling it, for the wizard's "retailers" step. Both
#: are answered by the same tools/llm-helper.py; `kind` is how it picks the right
#: prompt template and reply schema.
KINDS = ("model_suggestion", "retailer_discovery")

#: Much shorter than research_jobs' ceiling (600s): this is one small text completion,
#: not a multi-retailer web research pass, so a runner that hasn't reported back inside
#: two minutes is almost certainly gone (killed terminal, network drop), not merely slow.
JOB_CEILING_SECONDS = 120


def _seconds_since(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    from datetime import UTC, datetime

    then = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


def reconcile_stale(conn: sqlite3.Connection) -> list[int]:
    """Fail any job that has been RUNNING longer than the ceiling with nothing reporting.

    Same discipline as research.reconcile_stale: called on every read, so a wedged or
    killed runner is caught within one ceiling window rather than left QUEUED-looking
    forever from the wizard's point of view.
    """
    stale = conn.execute("SELECT id, started_at FROM llm_jobs WHERE status = 'RUNNING'").fetchall()
    reconciled = []
    for row in stale:
        if _seconds_since(row["started_at"]) > JOB_CEILING_SECONDS:
            conn.execute(
                "UPDATE llm_jobs SET status = 'FAILED', finished_at = ?,"
                " error = 'stale: no completion reported within the job ceiling'"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            reconciled.append(int(row["id"]))
    return reconciled


def create_job(conn: sqlite3.Connection, query: str, kind: str = "model_suggestion") -> int:
    query = query.strip() if isinstance(query, str) else ""
    if not query:
        raise ValueError("query is required")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    cur = conn.execute(
        "INSERT INTO llm_jobs (query, status, kind) VALUES (?, 'QUEUED', ?)", (query, kind)
    )
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    reconcile_stale(conn)
    row = conn.execute("SELECT * FROM llm_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def claim_next_queued(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Atomically claim the oldest QUEUED job for a runner.

    Same race-safety as research.claim_next_queued: the UPDATE only affects a row still
    QUEUED, and changes() proves whether this call actually won it, so two runners (or
    one runner polling twice in quick succession) can never both claim the same job.
    """
    row = conn.execute(
        "SELECT id FROM llm_jobs WHERE status = 'QUEUED' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE llm_jobs SET status = 'RUNNING', started_at = ? WHERE id = ? AND status = 'QUEUED'",
        (utcnow(), row["id"]),
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        return None  # lost the race to another claimer
    return get_job(conn, int(row["id"]))


def complete_job(
    conn: sqlite3.Connection, job_id: int, status: str,
    result: str | None = None, error: str | None = None,
) -> None:
    if status not in ("DONE", "FAILED"):
        raise ValueError("a job can only complete as DONE or FAILED")
    conn.execute(
        "UPDATE llm_jobs SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, result, error, utcnow(), job_id),
    )
