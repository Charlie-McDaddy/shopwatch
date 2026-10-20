"""The price-watch subsystem.

Runnable four ways, all the same code path:

    python -m app.price_watch              # CLI / cron / systemd timer
    POST /api/price-watch/run              # UI button or external automation

For every active listing with an adapter and a URL it fetches the page, normalises it,
writes the fields that are not manually locked, and appends a history observation.

Non-negotiables, each of which has its own test:
  * a fetch failure never destroys the last good value;
  * a manually locked field is never overwritten;
  * one retailer failing does not abort the run;
  * an unresolved value is stored as unresolved, never as zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from typing import Any

from . import alerts, provenance, retailers, store
from .config import Config, load_config
from .db import migrate, session, utcnow

log = logging.getLogger("shopwatch.price_watch")

#: check_listing status -> summary counter key
STATUS_COUNTER = {"ok": "ok", "unresolved": "unresolved", "skipped": "skipped",
                  "error": "errors"}


def _open_run(conn: sqlite3.Connection, trigger: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, trigger, status) VALUES (?, ?, 'running')",
        (utcnow(), trigger),
    )
    return int(cur.lastrowid)


def _record(
    conn: sqlite3.Connection,
    run_id: int,
    listing_id: int | None,
    status: str,
    message: str,
    payload: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO scrape_results (run_id, listing_id, status, message, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, listing_id, status, message, json.dumps(payload) if payload else None),
    )


def check_listing(
    conn: sqlite3.Connection,
    listing: sqlite3.Row,
    product: sqlite3.Row,
    run_id: int,
    config: Config,
) -> str:
    """Check one listing. Returns 'ok', 'unresolved', 'skipped' or 'error'."""
    adapter = retailers.get_adapter(listing["retailer_adapter"])
    from . import url_intake
    domain = url_intake.bare_domain(listing["url"] or "")
    scoped_adapter = url_intake.adapter_for_domain(domain) if domain else None
    if adapter is None or scoped_adapter is None or scoped_adapter.slug != adapter.slug:
        _record(conn, run_id, listing["id"], "skipped", "no supported automatic check for this URL")
        return "skipped"
    if not listing["url"]:
        _record(conn, run_id, listing["id"], "skipped", "listing has no URL")
        return "skipped"

    try:
        observation = adapter.check(listing["url"], product["model"])
    except retailers.FetchError as exc:
        # Deliberately leaves every stored value alone. The previous reading is still the
        # best information available; an error is not evidence that the price changed.
        _record(conn, run_id, listing["id"], "error", str(exc))
        conn.execute("UPDATE listings SET check_error = ? WHERE id = ?", (str(exc), listing["id"]))
        log.warning("%s: %s", adapter.name, exc)
        return "error"
    except Exception as exc:  # an adapter bug must not abort the whole run
        _record(
            conn, run_id, listing["id"], "error",
            f"adapter crashed: {type(exc).__name__}: {exc}",
        )
        conn.execute(
            "UPDATE listings SET check_error = ? WHERE id = ?",
            (f"adapter crashed: {type(exc).__name__}: {exc}", listing["id"]),
        )
        log.exception("adapter %s crashed", adapter.slug)
        return "error"

    for warning in observation.warnings:
        _record(conn, run_id, listing["id"], "warning", warning)

    mismatch = next((w for w in observation.warnings if "model mismatch" in w), None)
    current_verification = provenance.verification_map(conn, listing["id"])
    model_flagged = current_verification["model"]["status"] == provenance.FLAGGED
    manufacturer_match = observation.model_on_page_key in retailers.MANUFACTURER_KEYS
    if mismatch:
        provenance.set_verification(conn, listing["id"], "model", provenance.FLAGGED, mismatch)
        conn.execute(
            "UPDATE listings SET last_checked_at = ?, updated_at = ?, check_error = ? WHERE id = ?",
            (utcnow(), utcnow(), mismatch, listing["id"]),
        )
        _record(conn, run_id, listing["id"], "error", mismatch)
        return "error"
    if model_flagged and not manufacturer_match:
        # A previous model mismatch stays out of comparison until a manufacturer
        # identifier confirms that this page is the expected product again.
        note = current_verification["model"]["note"] or "model identity remains unverified"
        conn.execute(
            "UPDATE listings SET last_checked_at = ?, updated_at = ?, check_error = ? WHERE id = ?",
            (utcnow(), utcnow(), note, listing["id"]),
        )
        _record(conn, run_id, listing["id"], "error", note)
        return "error"

    if model_flagged and manufacturer_match:
        provenance.set_verification(
            conn, listing["id"], "model", provenance.LIVE,
            f"cleared: {observation.model_on_page!r} matched on this run",
        )

    values = observation.to_values()
    applied = provenance.apply_values(
        conn, listing["id"], values, state=provenance.LIVE, source=adapter.slug
    )
    conn.execute(
        "UPDATE listings SET last_checked_at = ?, updated_at = ?, check_error = NULL WHERE id = ?",
        (utcnow(), utcnow(), listing["id"]),
    )
    provenance.sync_verification_from_provenance(conn, listing["id"])
    store.record_observation(conn, listing["id"], source=adapter.slug)

    payload = {
        "updated": applied["written"],
        "preserved_manual": applied["blocked"],
        "unresolved": observation.unresolved,
        "warnings": observation.warnings,
    }
    status = "ok" if observation.advertised_price is not None else "unresolved"
    _record(conn, run_id, listing["id"], status, observation.notes or "", payload)
    return status


def run(
    product_id: int | None = None,
    trigger: str = "cli",
    send_alerts: bool = True,
    config: Config | None = None,
) -> dict[str, Any]:
    """Execute one watch pass. Returns a summary suitable for the API and the CLI."""
    config = config or load_config()
    summary: dict[str, Any] = {
        "run_id": None, "checked": 0, "ok": 0, "unresolved": 0, "skipped": 0,
        "errors": 0, "stale_marked": 0, "alerts": [], "delivery_failures": [],
    }

    with session() as conn:
        run_id = _open_run(conn, trigger)
        summary["run_id"] = run_id

        # A purchase keeps the card's watch active; pausing or archiving it stops checks.
        where = (
            "WHERE l.active = 1 AND p.archived = 0"
            " AND p.status IN ('ACTIVE', 'PURCHASED')"
        )
        params: list = []
        if product_id:
            where += " AND l.product_id = ?"
            params.append(product_id)
        listings = conn.execute(
            "SELECT l.*, r.adapter AS retailer_adapter, r.name AS retailer_name"
            " FROM listings l"
            " JOIN retailers r ON r.id = l.retailer_id"
            " JOIN products p ON p.id = l.product_id " + where,
            params,
        ).fetchall()

        for listing in listings:
            product = store.get_product(conn, listing["product_id"])
            if product is None:
                continue
            status = check_listing(conn, listing, product, run_id, config)
            summary["checked"] += 1
            summary[STATUS_COUNTER[status]] += 1

        for listing in listings:
            summary["stale_marked"] += len(
                provenance.mark_stale(conn, listing["id"], config.stale_after_days)
            )

        attempted = summary["checked"] - summary["skipped"]
        if attempted and summary["errors"] == attempted:
            status = "failed"
        elif summary["errors"]:
            status = "partial"
        else:
            status = "ok"

        conn.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ?, checked = ?, updated = ?,"
            " errors = ? WHERE id = ?",
            (utcnow(), status, summary["checked"], summary["ok"], summary["errors"], run_id),
        )
        summary["status"] = status

        if send_alerts:
            if product_id:
                view = store.product_view(conn, product_id)
                raised = alerts.evaluate_product(conn, view) if view else []
            else:
                raised = alerts.evaluate_all(conn)
            dispatched = alerts.dispatch(conn, raised, config)
            summary["alerts"] = dispatched["payload"]
            summary["delivery_failures"] = dispatched["delivery_failures"]

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.price_watch")
    parser.add_argument("--product", type=int, help="limit the run to one product id")
    parser.add_argument("--no-alerts", action="store_true", help="check prices, send nothing")
    parser.add_argument("--trigger", default="cli", help="label recorded against the run")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    migrate()
    summary = run(
        product_id=args.product, trigger=args.trigger, send_alerts=not args.no_alerts
    )

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            f"run {summary['run_id']}: {summary['status']} - checked {summary['checked']}, "
            f"ok {summary['ok']}, unresolved {summary['unresolved']}, "
            f"skipped {summary['skipped']}, errors {summary['errors']}, "
            f"alerts {len(summary['alerts'])}"
        )

    # Fail loudly for cron: a run where everything errored, or where an alert could not be
    # delivered, must not exit 0 and look healthy.
    if summary["status"] == "failed" or summary["delivery_failures"]:
        for failure in summary["delivery_failures"]:
            print(f"[ALERT-FAILURE] {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
