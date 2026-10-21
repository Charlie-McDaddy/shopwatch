"""Per-product check schedules, evaluated in the household's local timezone."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import price_watch, retailer_search
from .db import db_path, migrate, session

CADENCES = {"daily", "twice_weekly", "weekly", "monthly"}
RETRIES = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))


@contextmanager
def _product_lock(product_id: int):
    """Hold a process-wide product lock while a check runs; the OS releases it on crash."""
    database = db_path()
    lock_path = database.with_name(f"{database.name}.schedule-{product_id}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def next_due(after: datetime, cadence: str, zone: ZoneInfo) -> datetime:
    """Advance on local calendar boundaries, including DST and month-end clamping."""
    local = after.astimezone(zone)
    if cadence == "daily":
        return local + timedelta(days=1)
    if cadence == "twice_weekly":
        target_weekday = 3 if local.weekday() < 3 else 0  # Thursday and Monday
        days = (target_weekday - local.weekday()) % 7 or 7
        return local + timedelta(days=days)
    if cadence == "weekly":
        return local + timedelta(days=7)
    if cadence == "monthly":
        import calendar

        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        day = min(local.day, calendar.monthrange(year, month)[1])
        return local.replace(year=year, month=month, day=day)
    raise ValueError("unsupported check cadence")


def retry_due(after: datetime, failures: int) -> datetime:
    return after + RETRIES[min(failures - 1, len(RETRIES) - 1)]


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(product_id: int, now: datetime, force: bool = False) -> bool:
    """Atomically reserve a product; a committed claim survives a worker restart."""
    with session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT s.*, h.timezone, p.archived, p.status,"
            " (s.locked_until IS NOT NULL AND datetime(s.locked_until) > datetime(?))"
            " AS lease_active, datetime(s.next_due_at) > datetime(?) AS not_due"
            " FROM product_check_schedules s"
            " JOIN products p ON p.id = s.product_id CROSS JOIN household_settings h"
            " WHERE s.product_id = ? AND h.id = 1", (_stamp(now), _stamp(now), product_id)
        ).fetchone()
        if (row is None or row["archived"]
                or row["status"] == "PARKED" or row["status"] not in {"ACTIVE", "PURCHASED"}
                or row["lease_active"] or (not force and row["not_due"])):
            return False
        # shortcut: the lease is a 30-minute crash-recovery cooldown; the OS lock prevents
        # overlap during long runs, and the overdue date remains intact for catch-up.
        conn.execute(
            "UPDATE product_check_schedules SET last_attempt_at = ?,"
            " locked_until = ? WHERE product_id = ?",
            (_stamp(now), _stamp(now + timedelta(minutes=30)), product_id),
        )
        return True


def _finish(product_id: int, now: datetime, summary: dict) -> None:
    attempted = summary.get("checked", 0) - summary.get("skipped", 0)
    failed = summary.get("errors", 0) > 0
    with session() as conn:
        row = conn.execute(
            "SELECT s.failures, s.cadence, h.timezone FROM product_check_schedules s"
            " CROSS JOIN household_settings h WHERE s.product_id = ? AND h.id = 1",
            (product_id,),
        ).fetchone()
        if row is None:
            return
        failures = row["failures"] + 1 if failed else 0
        zone = ZoneInfo(row["timezone"])
        succeeded = attempted > 0 and not failed
        if failed:
            due = retry_due(now, failures) if failures <= len(RETRIES) else next_due(
                now.astimezone(zone), row["cadence"], zone
            )
        else:
            due = next_due(now.astimezone(zone), row["cadence"], zone)
        error = None
        if failed:
            error = summary.get("error")
            if not error and summary.get("run_id"):
                messages = conn.execute(
                    "SELECT message FROM scrape_results WHERE run_id = ? AND status = 'error'",
                    (summary["run_id"],),
                ).fetchall()
                error = "; ".join(row["message"] for row in messages)
            error = (error or f"{summary['errors']} listing check(s) failed")[:500]
        elif not attempted:
            error = "No retailer listings were eligible for an automatic check."
        conn.execute(
            "UPDATE product_check_schedules SET last_success_at = CASE WHEN ? THEN ?"
            " ELSE last_success_at END, failures = ?, last_error = ?, next_due_at = ?,"
            " locked_until = NULL WHERE product_id = ?",
            (int(succeeded), _stamp(now), failures, error, _stamp(due), product_id),
        )


def check_now(
    product_id: int, now: datetime | None = None, send_alerts: bool = True,
    trigger: str = "manual",
) -> dict:
    now = now or datetime.now(UTC)
    with _product_lock(product_id) as acquired:
        if not acquired or not _claim(product_id, now, force=True):
            raise ValueError("scheduled check is already running or this product is paused")
        try:
            result = price_watch.run(
                product_id=product_id, trigger=trigger, send_alerts=send_alerts
            )
            _finish(product_id, now, result)
            return result
        except Exception:
            _finish(product_id, now, {"errors": 1, "status": "failed"})
            raise


def check_all_now(
    now: datetime | None = None, send_alerts: bool = True, trigger: str = "api"
) -> dict:
    """Manually check active cards through the same per-product overlap guard."""
    with session() as conn:
        product_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM products WHERE archived = 0 AND status IN ('ACTIVE', 'PURCHASED')"
        )]
    combined = {
        "run_id": None, "checked": 0, "ok": 0, "unresolved": 0, "skipped": 0,
        "errors": 0, "stale_marked": 0, "alerts": [], "delivery_failures": [],
        "results": [],
    }
    for product_id in product_ids:
        try:
            result = check_now(product_id, now, send_alerts, trigger)
        except ValueError as exc:
            result = {"checked": 0, "ok": 0, "unresolved": 0, "skipped": 0,
                      "errors": 1, "status": "failed", "error": str(exc)}
        for key in ("checked", "ok", "unresolved", "skipped", "errors", "stale_marked"):
            combined[key] += result.get(key, 0)
        combined["run_id"] = result.get("run_id", combined["run_id"])
        combined["alerts"].extend(result.get("alerts", []))
        combined["delivery_failures"].extend(result.get("delivery_failures", []))
        combined["results"].append({"product_id": product_id, **result})
    attempted = combined["checked"] - combined["skipped"]
    combined["status"] = (
        "failed" if combined["errors"] and (not attempted or combined["errors"] >= attempted)
        else "partial" if combined["errors"] else "ok"
    )
    return combined


def run_due(now: datetime | None = None) -> dict:
    """Run each overdue active product once, keeping failures isolated by product."""
    now = now or datetime.now(UTC)
    migrate()
    with session() as conn:
        discovery_jobs = retailer_search.queue_due(conn, _stamp(now))
        due_ids = [r["product_id"] for r in conn.execute(
            "SELECT s.product_id FROM product_check_schedules s JOIN products p"
            " ON p.id = s.product_id WHERE datetime(s.next_due_at) <= datetime(?)"
            " AND (s.locked_until IS NULL OR datetime(s.locked_until) <= datetime(?))"
            " AND p.archived = 0 AND p.status IN ('ACTIVE', 'PURCHASED') ORDER BY s.next_due_at",
            (_stamp(now), _stamp(now)),
        )]
    results = []
    for product_id in due_ids:
        with _product_lock(product_id) as acquired:
            if not acquired or not _claim(product_id, now):
                continue
            try:
                result = price_watch.run(product_id=product_id, trigger="schedule")
            except Exception as exc:
                result = {"errors": 1, "status": "failed", "error": str(exc)}
            _finish(product_id, now, result)
            results.append({"product_id": product_id, **result})
    return {"checked_products": len(results), "results": results,
            "discovery_jobs_queued": len(discovery_jobs)}


if __name__ == "__main__":
    outcome = run_due()
    print(json.dumps(outcome, indent=2))
    raise SystemExit(1 if any(r.get("status") == "failed" for r in outcome["results"]) else 0)
