"""Research jobs: the wizard's "let's go" step.

A job's own lifecycle lives here, in this container. The actual research (a
credentialed subprocess call to `claude`, real internet egress, real shared quota) never
runs in this process or this container - see `deploy/research-runner.py` for the
host-level script that claims a job over this same API, invokes `claude`, and reports
back, exactly the way `app/mailwatch.py` already does for retailer marketing email. The
token never enters this container; this module only ever talks to sqlite and answers
HTTP calls from that host script.

Whatever a `kind="price"` job finds still lands through the ordinary import path
(`app/ingest.py`), under the ordinary IMPORTED provenance state - a research job is not
a second, parallel way for prices to enter the database, only a new way to trigger
`POST /api/import`.

A `kind="historical_low"` job (issue #93) asks a different question - "has this ever
been cheaper, before tracking started" - and is answered once per job rather than once
per retailer. Its finding is never imported and never written to a product directly: it
sits on the job row (`historical_low_*`) purely as a suggestion for a human to copy into
the ordinary product-edit form, or discard.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlparse

from . import store
from .db import utcnow

#: Best-effort cap on how many "other retailers noticed" candidates a historical-low
#: finding can carry - matches deploy/research-runner.py's FIRECRAWL_MAX_CANDIDATES,
#: so neither side of the pipeline can be the one that lets an unbounded list through.
MAX_OTHER_RETAILERS = 8

STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED")
RESULT_STATUSES = ("PENDING", "FOUND", "NEEDS_MANUAL_CHECK", "BLOCKED", "TIMED_OUT")

#: price: the original question, "what's this retailer selling it for today", answered
#: once per selected retailer via research_job_results. historical_low: "has this ever
#: been cheaper", a single whole-product question with no retailer selection, answered
#: via the historical_low_* columns on the job itself - see migrations/0010.
KINDS = ("price", "historical_low")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")

#: Hard backstop for a job that never reports back (host script crashed, opti rebooted
#: mid-run, network partition). Matches the per-job ceiling the host-level script is
#: expected to enforce on itself; this is the container-side proof of liveness for the
#: case where the host script cannot report its own failure at all.
JOB_CEILING_SECONDS = 600


class JobAlreadyRunning(RuntimeError):
    """Raised when a product already has an active (queued or running) job."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"a research job is already active for this product: {job_id}")
        self.job_id = job_id


def _seconds_since(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    from datetime import UTC, datetime

    then = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


def reconcile_stale(conn: sqlite3.Connection) -> list[int]:
    """Fail any job that has been RUNNING longer than the ceiling with nothing reporting.

    Called on app startup (catches a job orphaned by a crash/redeploy while nothing was
    running to update it) and opportunistically whenever a job is read, so a wedged host
    script is caught within one ceiling window rather than only at the next restart.
    """
    stale = conn.execute(
        "SELECT id FROM research_jobs WHERE status = 'RUNNING'"
    ).fetchall()
    reconciled = []
    for row in stale:
        job = conn.execute(
            "SELECT started_at FROM research_jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        if _seconds_since(job["started_at"]) > JOB_CEILING_SECONDS:
            conn.execute(
                "UPDATE research_jobs SET status = 'FAILED', finished_at = ?,"
                " error = 'stale: no completion reported within the job ceiling'"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            reconciled.append(int(row["id"]))
    return reconciled


def create_job(
    conn: sqlite3.Connection,
    product_id: int,
    retailer_ids: list[int],
    kind: str = "price",
    urls: dict[int, str] | None = None,
) -> int:
    """Queue a job for one product.

    For kind="price" (the default, and the only kind this accepted before issue #93):
    a fixed set of retailers, one result row each. Default-deny by construction - a
    retailer id that is not already in the `retailers` table is silently dropped
    rather than passed through, so this can only ever ask about a retailer shopwatch
    already knows, never wherever a typo or a scraped page might lead the research
    pass.

    For kind="historical_low": no retailer selection at all. "Has this ever been
    cheaper" is one question about the product as a whole, not one answer per
    retailer, so retailer_ids is ignored and no research_job_results rows are created -
    the finding lands on the job itself (report_historical_low) for a human to review.

    `urls` is optional and keyed by retailer_id: when a retailer id has an entry, the
    runner reads that exact page instead of searching generically for the product at
    that retailer (see deploy/research-runner.py's `research_one_retailer`). This is
    the "paste a listing URL" path (issue #94) - the whole point of having a URL in
    hand is that the runner does not have to guess which page is the right one. A
    retailer id with no entry behaves exactly as before.

    Refuses outright for a paused or archived product - a stray research job against
    something nobody is currently watching is exactly the wasted spend this prevents.

    At most one active (QUEUED or RUNNING) job per product regardless of kind: the
    host runner processes one job at a time per product anyway, and a price job and a
    historical-low job both spend the same shared, credentialed CLI quota.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    row = conn.execute(
        "SELECT archived, status FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is not None and (row["archived"] or row["status"] == "PARKED"):
        raise ValueError("cannot start research on a paused or archived product")

    known_ids: list[int] = []
    if kind == "price":
        if not retailer_ids:
            raise ValueError("at least one retailer is required")
        placeholders = ",".join("?" * len(retailer_ids))
        known_ids = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM retailers WHERE id IN ({placeholders})", retailer_ids
            )
        ]
        if not known_ids:
            raise ValueError("no known retailers in the request")

    try:
        cur = conn.execute(
            "INSERT INTO research_jobs (product_id, status, kind) VALUES (?, 'QUEUED', ?)",
            (product_id, kind),
        )
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            "SELECT id FROM research_jobs WHERE product_id = ?"
            " AND status IN ('QUEUED', 'RUNNING')",
            (product_id,),
        ).fetchone()
        if existing:
            raise JobAlreadyRunning(int(existing["id"])) from exc
        raise
    job_id = int(cur.lastrowid)
    urls = urls or {}
    for retailer_id in known_ids:
        conn.execute(
            "INSERT INTO research_job_results (job_id, retailer_id, status, url)"
            " VALUES (?, ?, 'PENDING', ?)",
            (job_id, retailer_id, urls.get(retailer_id)),
        )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    reconcile_stale(conn)
    row = conn.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["results"] = [
        dict(r)
        for r in conn.execute(
            "SELECT rjr.*, r.name AS retailer_name, r.homepage AS retailer_homepage"
            " FROM research_job_results rjr"
            " JOIN retailers r ON r.id = rjr.retailer_id"
            " WHERE rjr.job_id = ? ORDER BY r.name",
            (job_id,),
        )
    ]
    return job


def get_latest_job_for_product(conn: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    """The most recent job for a product, terminal or not, so a caller can pre-fill a
    retry with whichever retailers did not come back FOUND last time - the whole point
    being that a retailer selection the wizard already made once is never something the
    person has to reconstruct from memory."""
    row = conn.execute(
        "SELECT id FROM research_jobs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    return get_job(conn, row["id"]) if row else None


def claim_next_queued(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Atomically claim the oldest QUEUED job for the host-level runner.

    Flips it to RUNNING in the same statement a caller commits, so two overlapping
    polls from the host script (or a second host script) cannot both claim the same
    job - the UPDATE only affects a row still QUEUED, and `changes()` proves whether
    this call actually won it.
    """
    row = conn.execute(
        "SELECT j.id FROM research_jobs j JOIN products p ON p.id = j.product_id"
        " WHERE j.status = 'QUEUED' AND p.archived = 0 AND p.status != 'PARKED'"
        " ORDER BY j.id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE research_jobs SET status = 'RUNNING', started_at = ?"
        " WHERE id = ? AND status = 'QUEUED'",
        (utcnow(), row["id"]),
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        return None  # lost the race to another claimer
    return get_job(conn, int(row["id"]))


def report_result(
    conn: sqlite3.Connection,
    job_id: int,
    retailer_id: int,
    status: str,
    listing_id: int | None = None,
    note: str | None = None,
) -> None:
    if status not in RESULT_STATUSES:
        raise ValueError(f"{status!r} is not a valid research-job result status")
    conn.execute(
        "UPDATE research_job_results SET status = ?, listing_id = ?, note = ?,"
        " updated_at = ? WHERE job_id = ? AND retailer_id = ?",
        (status, listing_id, note, utcnow(), job_id, retailer_id),
    )


def _safe_http_url(url: str | None) -> str | None:
    """http(s)-only allowlist, same reasoning and same check deploy/research-runner.py
    already applies before it ever reports a candidate to this endpoint: the URL
    ultimately came out of an LLM's reply to a "search the web" prompt, so it is
    untrusted content relayed through the model, not content safe to store or later
    render into an href unexamined. Re-checked here rather than trusting the
    runner's own filtering, since this is the actual boundary that writes to the
    database and re-exposes the value through the API - the runner is a separate
    process this code must not simply assume behaved.
    """
    if not url:
        return None
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def _is_excluded_retailer_name(conn: sqlite3.Connection, name: str) -> bool:
    """Same case-insensitive name/slug matching store.ensure_retailer already uses,
    without creating anything - a research candidate is not yet a real retailer, and
    checking exclusion must not be the thing that first brings it into existence."""
    row = conn.execute(
        "SELECT excluded FROM retailers WHERE lower(name) = lower(?) OR slug = ?",
        (name, store.slugify(name)),
    ).fetchone()
    return bool(row and row["excluded"])


def _clean_other_retailers(
    conn: sqlite3.Connection, other_retailers: list[Any] | None
) -> str | None:
    """Best-effort coercion of "other retailers noticed along the way" (issue #98)
    into a JSON string, same storage convention retailer_search_jobs.result and
    llm_jobs.result already use. An item with no usable name is dropped rather than
    raising - this rides along with the historical-low answer as incidental
    information, not something worth failing the whole report over. Capped at
    MAX_OTHER_RETAILERS in case a future prompt/model version returns more than the
    product page should ever render as a one-shot checkbox list.

    Issue #112: a candidate matching an excluded retailer's name is dropped here too,
    right next to the existing http(s)-only URL allowlist below - this is the actual
    enforcement boundary (the one that writes to the database and re-exposes the
    value through the API), so it re-checks rather than trusting the runner's own
    prompt-level instruction (deploy/research-runner.py) to have kept the model from
    finding it in the first place.
    """
    if not other_retailers:
        return None
    cleaned: list[dict[str, Any]] = []
    for item in other_retailers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name or _is_excluded_retailer_name(conn, name):
            continue
        url = item.get("url")
        url = url.strip() if isinstance(url, str) and url.strip() else None
        cleaned.append({"name": name, "url": _safe_http_url(url)})
        if len(cleaned) >= MAX_OTHER_RETAILERS:
            break
    return json.dumps(cleaned) if cleaned else None


def report_historical_low(
    conn: sqlite3.Connection,
    job_id: int,
    price: float | None = None,
    date: str | None = None,
    retailer: str | None = None,
    notes: str | None = None,
    confidence: str | None = None,
    other_retailers: list[Any] | None = None,
) -> None:
    """Record a kind="historical_low" job's finding on the job itself.

    Deliberately does NOT touch products.lowest_known_price or any other product
    column - see migrations/0010 and app/main.py's product-edit endpoint. This is
    called once by the host runner (deploy/research-runner.py) before it marks the
    job DONE, whether or not it actually found anything: price=None with a note is a
    legitimate, honest outcome ("could not find a confident historical low"), not an
    error.

    `other_retailers` (issue #98): retailers the model noticed selling this product
    while researching the historical low - genuinely incidental, since the prompt
    already asks it to search broadly across "any legitimate Australian retailer".
    Stored via _clean_other_retailers, never applied to a retailer or listing here -
    see the product page's checkbox confirm step, which calls the ordinary
    POST /api/products/{id}/retailers / .../retailers/from-url endpoints itself.
    """
    if confidence is not None and confidence not in CONFIDENCES:
        raise ValueError(f"confidence must be one of {CONFIDENCES}")
    conn.execute(
        "UPDATE research_jobs SET historical_low_price = ?, historical_low_date = ?,"
        " historical_low_retailer = ?, historical_low_notes = ?,"
        " historical_low_confidence = ?, historical_low_other_retailers = ? WHERE id = ?",
        (price, date, retailer, notes, confidence,
         _clean_other_retailers(conn, other_retailers), job_id),
    )


def complete_job(
    conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None
) -> None:
    if status not in ("DONE", "FAILED"):
        raise ValueError("a job can only complete as DONE or FAILED")
    conn.execute(
        "UPDATE research_jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
        (status, utcnow(), error, job_id),
    )
