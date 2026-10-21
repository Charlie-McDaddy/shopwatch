"""Retailer search jobs: "who else sells this, beyond what's already selected" -
answered by a LIVE web search, for the "Track something new" wizard's retailers step.

Same job-lifecycle shape as app/llm_jobs.py, but a different claimant. llm_jobs is
claimed by WHOEVER's own machine runs tools/llm-helper.py, using their own CLI login -
appropriate for a knowledge-only question an LLM can answer without browsing. This
queue needs an actual live search, which means the self-hosted Firecrawl instance on
opti (127.0.0.1:3002) - reachable only from opti's own host network, not from this
container's docker network. So this queue is claimed by deploy/research-runner.py, the
same fixed host-level script (and systemd timer) that already claims research_jobs,
extended to also poll this queue each cycle. Nothing about Firecrawl's location or
credentials ever needs to exist in this container.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .db import utcnow

STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED")

#: Same order of magnitude as llm_jobs' ceiling: a live search plus a handful of page
#: reads, not a multi-retailer research pass - a runner that hasn't reported back
#: inside two minutes is almost certainly gone, not merely slow.
JOB_CEILING_SECONDS = 120
DISCOVERY_INTERVAL_SECONDS = 7 * 24 * 60 * 60


def _stamp(value: str | None = None) -> str:
    if value is not None:
        return value
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _after_week(value: str) -> str:
    from datetime import UTC, datetime, timedelta

    then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (then + timedelta(seconds=DISCOVERY_INTERVAL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def matches_exact_model(model: str, text: str) -> bool:
    """Match the complete model token sequence, including regional suffixes."""
    from .store import normalise_model

    words = re.findall(r"[A-Za-z0-9]+", text)
    model_key = normalise_model(model).replace("/", "")
    return bool(model_key) and any(
        normalise_model("".join(words[start:end])).replace("/", "") == model_key
        for start in range(len(words)) for end in range(start + 1, min(len(words), start + 6) + 1)
    )


def _seconds_since(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    from datetime import UTC, datetime

    then = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


def reconcile_stale(conn: sqlite3.Connection) -> list[int]:
    """Fail any job that has been RUNNING longer than the ceiling with nothing reporting.

    Same discipline as llm_jobs.reconcile_stale: called on every read, so a wedged or
    killed runner is caught within one ceiling window rather than left QUEUED-looking
    forever from the wizard's point of view.
    """
    stale = conn.execute(
        "SELECT id, started_at FROM retailer_search_jobs WHERE status = 'RUNNING'"
    ).fetchall()
    reconciled = []
    for row in stale:
        if _seconds_since(row["started_at"]) > JOB_CEILING_SECONDS:
            conn.execute(
                "UPDATE retailer_search_jobs SET status = 'FAILED', finished_at = ?,"
                " error = 'stale: no completion reported within the job ceiling'"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            reconciled.append(int(row["id"]))
    return reconciled


def create_job(
    conn: sqlite3.Connection, query: str, *, product_id: int | None = None,
    trigger: str = "manual",
) -> int:
    query = query.strip() if isinstance(query, str) else ""
    if not query:
        raise ValueError("query is required")
    cur = conn.execute(
        "INSERT INTO retailer_search_jobs (query, status, product_id, trigger)"
        " VALUES (?, 'QUEUED', ?, ?)", (query, product_id, trigger)
    )
    return int(cur.lastrowid)


def request_product_discovery(
    conn: sqlite3.Connection, product_id: int, *, trigger: str = "manual",
    now: str | None = None,
) -> int:
    """Queue one search for this product's exact model, excluding current retailers."""
    if trigger not in {"initial", "weekly", "manual"}:
        raise ValueError("trigger must be initial, weekly, or manual")
    product = conn.execute(
        "SELECT name, model, archived, status FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        raise ValueError("unknown product")
    if product["archived"] or product["status"] not in {"ACTIVE", "PURCHASED"}:
        raise ValueError("product is inactive")
    active = conn.execute(
        "SELECT id FROM retailer_search_jobs WHERE product_id = ?"
        " AND status IN ('QUEUED', 'RUNNING') ORDER BY id DESC LIMIT 1", (product_id,)
    ).fetchone()
    if active:
        return int(active["id"])
    rows = conn.execute(
        "SELECT DISTINCT r.name, r.homepage FROM listings l JOIN retailers r"
        " ON r.id = l.retailer_id WHERE l.product_id = ?", (product_id,)
    ).fetchall()
    query = json.dumps({
        "product_name": product["name"], "model": product["model"],
        "excluded_names": [r["name"] for r in rows],
        "excluded_homepages": [r["homepage"] for r in rows if r["homepage"]],
    })
    job_id = create_job(conn, query, product_id=product_id, trigger=trigger)
    stamped = _stamp(now)
    conn.execute(
        "UPDATE product_discovery_schedules SET last_attempt_at = ?, last_error = NULL"
        " WHERE product_id = ?", (stamped, product_id)
    )
    if trigger in {"initial", "weekly"}:
        conn.execute(
            "UPDATE product_discovery_schedules SET next_due_at = ? WHERE product_id = ?",
            (_after_week(stamped), product_id),
        )
    return job_id


def queue_due(conn: sqlite3.Connection, now: str | None = None) -> list[int]:
    """Queue overdue weekly searches for active, unarchived products."""
    reconcile_stale(conn)
    stamped = _stamp(now)
    rows = conn.execute(
        "SELECT s.product_id FROM product_discovery_schedules s JOIN products p"
        " ON p.id = s.product_id WHERE datetime(s.next_due_at) <= datetime(?)"
        " AND p.archived = 0 AND p.status IN ('ACTIVE', 'PURCHASED')"
        " AND NOT EXISTS (SELECT 1 FROM retailer_search_jobs j WHERE j.product_id = p.id"
        " AND j.status IN ('QUEUED', 'RUNNING')) ORDER BY s.next_due_at", (stamped,)
    ).fetchall()
    return [request_product_discovery(conn, row["product_id"], trigger="weekly", now=stamped)
            for row in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    reconcile_stale(conn)
    row = conn.execute("SELECT * FROM retailer_search_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def claim_next_queued(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Atomically claim the oldest QUEUED job for a runner.

    Same race-safety as llm_jobs.claim_next_queued: the UPDATE only affects a row still
    QUEUED, and changes() proves whether this call actually won it.
    """
    conn.execute(
        "UPDATE retailer_search_jobs SET status = 'FAILED', finished_at = ?,"
        " error = 'product became inactive before discovery started' WHERE status = 'QUEUED'"
        " AND product_id IS NOT NULL AND product_id IN (SELECT id FROM products"
        " WHERE archived = 1 OR status NOT IN ('ACTIVE', 'PURCHASED'))", (utcnow(),)
    )
    row = conn.execute(
        "SELECT j.id FROM retailer_search_jobs j LEFT JOIN products p ON p.id = j.product_id"
        " WHERE j.status = 'QUEUED' AND (j.product_id IS NULL OR"
        " (p.archived = 0 AND p.status IN ('ACTIVE', 'PURCHASED'))) ORDER BY j.id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE retailer_search_jobs SET status = 'RUNNING', started_at = ?"
        " WHERE id = ? AND status = 'QUEUED'",
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
    job = conn.execute(
        "SELECT product_id, status FROM retailer_search_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        return
    if job["status"] not in {"QUEUED", "RUNNING"}:
        raise ValueError("job is already complete")
    if status == "DONE" and job["product_id"] is not None:
        product = conn.execute(
            "SELECT archived, status, model FROM products WHERE id = ?", (job["product_id"],)
        ).fetchone()
        if (product is None or product["archived"]
                or product["status"] not in {"ACTIVE", "PURCHASED"}):
            status, result, error = (
                "FAILED", None, "product became inactive before discovery completed"
            )
        else:
            try:
                result = _retain_discovered_listings(
                    conn, job["product_id"], product["model"], result
                )
            except ValueError as exc:
                status, result, error = "FAILED", None, str(exc)
    conn.execute(
        "UPDATE retailer_search_jobs SET status = ?, result = ?, error = ?, finished_at = ?"
        " WHERE id = ?",
        (status, result, error, utcnow(), job_id),
    )
    if job["product_id"] is not None:
        conn.execute(
            "UPDATE product_discovery_schedules SET last_error = ? WHERE product_id = ?",
            (error if status == "FAILED" else None, job["product_id"]),
        )


def _retain_discovered_listings(
    conn: sqlite3.Connection, product_id: int, model: str, result: str | None,
) -> str:
    """Retain only public, exact-model product pages as empty, unverified listings."""
    from . import store
    from .urls import public_http_url

    try:
        payload = json.loads(result or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid retailer discovery result: malformed JSON") from exc
    candidates = payload.get("retailers") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("invalid retailer discovery result: retailers must be a list")
    from .url_intake import bare_domain, is_marketplace_url

    existing = conn.execute(
        "SELECT DISTINCT r.name, r.homepage FROM listings l JOIN retailers r"
        " ON r.id = l.retailer_id WHERE l.product_id = ?", (product_id,)
    ).fetchall()
    existing_names = {row["name"].strip().casefold() for row in existing}
    existing_domains = {bare_domain(row["homepage"]) for row in existing if row["homepage"]}
    existing_domains.discard(None)
    cleaned = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = candidate.get("name")
        name = name.strip()[:200] if isinstance(name, str) else ""
        homepage = candidate.get("homepage")
        homepage = homepage.strip() if isinstance(homepage, str) else ""
        source_url = candidate.get("source_url")
        source_url = source_url.strip() if isinstance(source_url, str) else ""
        source_title = candidate.get("source_title")
        source_title = source_title.strip() if isinstance(source_title, str) else ""
        if not name:
            continue
        if homepage and not public_http_url(homepage):
            homepage = ""
        exact_page = bool(source_url and public_http_url(source_url)
                          and matches_exact_model(model, source_title))
        domain = bare_domain(homepage) if homepage else None
        marketplace = bool(exact_page and is_marketplace_url(source_url))
        if (not marketplace and
                (name.casefold() in existing_names or (domain and domain in existing_domains))):
            continue
        item = {"name": name, "homepage": homepage or None,
                "source_url": source_url if exact_page else None,
                "source_title": source_title[:500] if exact_page else None,
                "needs_review": marketplace}
        if exact_page and not marketplace:
            retailer = store.ensure_retailer(conn, name, homepage=homepage or None)
            existing = conn.execute(
                "SELECT id FROM listings WHERE product_id = ? AND retailer_id = ?",
                (product_id, retailer["id"]),
            ).fetchone()
            if existing is None:
                store.create_listing(conn, {"product_id": product_id,
                                            "retailer_id": retailer["id"], "url": source_url})
        cleaned.append(item)
        if not marketplace:
            existing_names.add(name.casefold())
            if domain:
                existing_domains.add(domain)
    payload["retailers"] = cleaned
    return json.dumps(payload)
