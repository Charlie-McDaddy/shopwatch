"""Import and export.

Two shapes are accepted:

* a list of normalised *findings* (one retailer observation each), which is what external
  research produces, and
* a full *snapshot* as produced by `export_all`, for backup and hand-off.

Both paths obey the same rule: a manually locked field is never overwritten.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from . import provenance, store, url_intake
from .db import utcnow

# Finding keys that map onto listing fields.
FINDING_FIELD_MAP = {
    "price": "advertised_price",
    "advertised_price": "advertised_price",
    "freight": "freight",
    "shipping": "freight",
    "cashback": "cashback",
    "rebate": "rebate",
    "coupon": "coupon_discount",
    "coupon_discount": "coupon_discount",
    "stock": "stock_status",
    "stock_status": "stock_status",
    "pickup": "pickup_status",
    "pickup_status": "pickup_status",
    "pickup_location": "pickup_location",
    "condition": "condition",
    "warranty": "warranty",
    "contents": "included_components",
    "included_components": "included_components",
    "model_on_page": "model_on_page",
    "notes": "seller_notes",
    "seller_notes": "seller_notes",
    "price_guide": "price_guide",
    "source_url": "url",
    "url": "url",
}


class ImportError_(ValueError):
    """A finding that cannot be applied, with a reason worth showing the user."""


def import_finding(
    conn: sqlite3.Connection,
    finding: Mapping[str, Any],
    *,
    state: str = provenance.IMPORTED,
    source: str | None = None,
    create_missing_product: bool = False,
) -> dict[str, Any]:
    """Apply one normalised finding. Returns a per-finding report."""
    model = finding.get("model") or finding.get("model_number")
    retailer_name = finding.get("retailer")
    if not model:
        raise ImportError_("finding has no model")
    if not retailer_name:
        raise ImportError_("finding has no retailer")

    product = store.product_by_model(conn, model)
    if product is None:
        if not create_missing_product:
            raise ImportError_(f"no product matches model {model!r}")
        pid = store.create_product(
            conn,
            {
                "name": finding.get("product_name") or model,
                "model": model,
                "brand": finding.get("brand"),
                "category": finding.get("category") or "general",
            },
        )
        product = store.get_product(conn, pid)

    retailer = store.ensure_retailer(conn, retailer_name)
    url = finding.get("source_url") or finding.get("url")
    identity = url_intake.marketplace_identity(url, finding) if url else {
        "seller_name": None, "seller_id": None, "offer_id": None, "offer_key": ""
    }
    offer_key = identity["offer_key"] if url and url_intake.is_marketplace_url(url) else None
    listing = store.find_listing(
        conn, product["id"], retailer["id"], url, offer_key=offer_key
    )
    created = False
    if listing is None:
        listing_id = store.create_listing(conn, {
            "product_id": product["id"], "retailer_id": retailer["id"], "url": url,
            **identity,
        })
        created = True
    else:
        listing_id = listing["id"]

    values: dict[str, Any] = {}
    for key, field in FINDING_FIELD_MAP.items():
        if key in finding and finding[key] is not None:
            values[field] = finding[key]
    # A model reported for the listing but not echoed separately still belongs on the row.
    values.setdefault("model_on_page", finding.get("model_on_page") or model)

    applied = provenance.apply_values(
        conn,
        listing_id,
        values,
        state=state,
        source=source or finding.get("source") or "import",
    )

    checked_at = finding.get("checked_at") or utcnow()
    conn.execute(
        "UPDATE listings SET last_checked_at = ?, updated_at = ? WHERE id = ?",
        (checked_at, utcnow(), listing_id),
    )

    verification = finding.get("verification") or {}
    for aspect, value in verification.items():
        if aspect not in provenance.VERIFICATION_ASPECTS:
            continue
        if isinstance(value, bool):
            status = provenance.VERIFIED if value else provenance.UNVERIFIED
        else:
            status = str(value).upper()
        provenance.set_verification(conn, listing_id, aspect, status)
    provenance.sync_verification_from_provenance(conn, listing_id)

    history_id = None
    if finding.get("record_history", True):
        history_id = store.record_observation(
            conn, listing_id, source=source or "import", note=finding.get("history_note")
        )

    return {
        "model": model,
        "retailer": retailer["name"],
        "listing_id": listing_id,
        "listing_created": created,
        "updated": applied["written"],
        "preserved_manual": applied["blocked"],
        "history_id": history_id,
    }


def import_findings(
    conn: sqlite3.Connection,
    findings: Iterable[Mapping[str, Any]],
    *,
    state: str = provenance.IMPORTED,
    source: str | None = None,
    create_missing_product: bool = False,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for finding in findings:
        try:
            results.append(
                import_finding(
                    conn,
                    finding,
                    state=state,
                    source=source,
                    create_missing_product=create_missing_product,
                )
            )
        except (ImportError_, ValueError, provenance.FieldError) as exc:
            errors.append({"finding": dict(finding), "error": str(exc)})
    return {
        "applied": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


# --------------------------------------------------------------------------- snapshots


def export_all(conn: sqlite3.Connection, include_history: bool = True) -> dict[str, Any]:
    """Full JSON snapshot: products, retailers, listings with provenance, history, rules."""

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql)]

    data: dict[str, Any] = {
        "format": "shopwatch-snapshot",
        "version": 1,
        "exported_at": utcnow(),
        "retailers": rows("SELECT * FROM retailers ORDER BY id"),
        "products": rows("SELECT * FROM products ORDER BY id"),
        "listings": rows("SELECT * FROM listings ORDER BY id"),
        "field_provenance": rows("SELECT * FROM field_provenance ORDER BY listing_id, field"),
        "listing_verification": rows("SELECT * FROM listing_verification ORDER BY listing_id"),
        "alert_rules": rows("SELECT * FROM alert_rules ORDER BY id"),
        "category_specifications": rows("SELECT * FROM category_specifications ORDER BY id"),
        "household_settings": store.household_settings(conn),
    }
    if include_history:
        data["price_history"] = rows("SELECT * FROM price_history ORDER BY id")
    return data


def import_snapshot(conn: sqlite3.Connection, data: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a snapshot into the current database.

    Products and retailers match on model/name, never on the exporting database's ids.
    Listing fields are written through the provenance layer so a local manual lock beats
    an imported value; a field the snapshot itself marks MANUAL arrives locked.
    History rows are appended, de-duplicated on (listing, timestamp, price).
    """
    report = {"products": 0, "listings": 0, "history": 0, "preserved_manual": 0, "errors": []}

    if isinstance(data.get("household_settings"), Mapping):
        try:
            store.update_household_settings(conn, data["household_settings"])
        except ValueError as exc:
            report["errors"].append(f"household_settings: {exc}")

    for spec in data.get("category_specifications", []):
        conn.execute(
            "INSERT OR IGNORE INTO category_specifications"
            " (category, field_key, label, kind, options, sort)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                spec.get("category"), spec.get("field_key"), spec.get("label"),
                spec.get("kind", "text"), spec.get("options"), spec.get("sort", 0),
            ),
        )

    old_retailer_names = {r["id"]: r["name"] for r in data.get("retailers", [])}
    retailer_ids: dict[Any, int] = {}
    for old_id, name in old_retailer_names.items():
        retailer_ids[old_id] = store.ensure_retailer(conn, name)["id"]

    product_ids: dict[Any, int] = {}
    for snap in data.get("products", []):
        existing = store.product_by_model(conn, snap.get("model", ""))
        payload = {k: v for k, v in snap.items() if k in store.PRODUCT_FIELDS}
        payload["specs"] = json.loads(snap.get("specs_json") or "{}")
        payload["components"] = json.loads(snap.get("components_json") or "{}")
        if existing:
            store.update_product(conn, existing["id"], payload)
            product_ids[snap["id"]] = existing["id"]
        else:
            product_ids[snap["id"]] = store.create_product(conn, payload)
        report["products"] += 1

    prov_index: dict[tuple[Any, str], dict[str, Any]] = {
        (p["listing_id"], p["field"]): p for p in data.get("field_provenance", [])
    }

    listing_ids: dict[Any, int] = {}
    for snap in data.get("listings", []):
        pid = product_ids.get(snap.get("product_id"))
        rid = retailer_ids.get(snap.get("retailer_id"))
        if pid is None or rid is None:
            report["errors"].append(f"listing {snap.get('id')}: unknown product or retailer")
            continue
        offer_key = snap.get("offer_key", "")
        snap_url = snap.get("url")
        lookup_key = offer_key if snap_url and url_intake.is_marketplace_url(snap_url) else None
        existing = store.find_listing(conn, pid, rid, snap_url, offer_key=lookup_key)
        listing_id = existing["id"] if existing else store.create_listing(conn, {
            "product_id": pid, "retailer_id": rid, "url": snap.get("url"),
            **{key: snap.get(key) for key in ("seller_name", "seller_id", "offer_id")},
            "offer_key": offer_key,
        })
        listing_ids[snap["id"]] = listing_id

        for field in provenance.TRACKED_FIELDS:
            if field not in snap:
                continue
            incoming = prov_index.get((snap["id"], field), {})
            state = incoming.get("state", provenance.IMPORTED)
            locked = bool(incoming.get("manual_locked"))
            wrote = provenance.set_field(
                conn, listing_id, field, snap[field],
                state=provenance.MANUAL if locked else state,
                source=incoming.get("source") or "snapshot",
                note=incoming.get("note"),
                lock=True if locked else None,
            )
            if not wrote:
                report["preserved_manual"] += 1
        conn.execute(
            "UPDATE listings SET last_checked_at = COALESCE(?, last_checked_at), updated_at = ?"
            " WHERE id = ?",
            (snap.get("last_checked_at"), utcnow(), listing_id),
        )
        report["listings"] += 1

    for ver in data.get("listing_verification", []):
        lid = listing_ids.get(ver.get("listing_id"))
        if lid and ver.get("aspect") in provenance.VERIFICATION_ASPECTS:
            provenance.set_verification(
                conn, lid, ver["aspect"], ver.get("status", "UNVERIFIED"), ver.get("note")
            )

    for hist in data.get("price_history", []):
        pid = product_ids.get(hist.get("product_id"))
        lid = listing_ids.get(hist.get("listing_id"))
        if pid is None:
            continue
        duplicate = conn.execute(
            "SELECT 1 FROM price_history WHERE product_id = ? AND observed_at = ?"
            " AND IFNULL(listing_id, -1) = IFNULL(?, -1)"
            " AND IFNULL(advertised_price, -1) = IFNULL(?, -1)",
            (pid, hist.get("observed_at"), lid, hist.get("advertised_price")),
        ).fetchone()
        if duplicate:
            continue
        conn.execute(
            "INSERT INTO price_history (product_id, listing_id, retailer_id, observed_at,"
            " advertised_price, delivered_price, freight, freight_resolved, cashback,"
            " stock_status, condition, source, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid, lid, retailer_ids.get(hist.get("retailer_id")), hist.get("observed_at"),
                hist.get("advertised_price"), hist.get("delivered_price"), hist.get("freight"),
                int(hist.get("freight_resolved") or 0), hist.get("cashback"),
                hist.get("stock_status"), hist.get("condition"),
                hist.get("source") or "snapshot", hist.get("note"),
            ),
        )
        report["history"] += 1

    return report


def csv_export(conn: sqlite3.Connection) -> str:
    """Flat CSV of the current comparison, one row per active listing."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "product", "model", "verdict", "retailer", "advertised", "freight", "freight_resolved",
        "cashback", "rebate", "coupon", "delivered", "classification", "condition", "stock",
        "pickup", "warranty", "included_components", "promo_ends", "manual_fields",
        "last_checked", "url",
    ])
    for product in store.list_products(conn):
        for listing in product["listings"]:
            writer.writerow([
                product["name"], product["model"], product["verdict"], listing["retailer_name"],
                listing["advertised_price"], listing["freight"],
                int(listing["delivered_resolved"]), listing["cashback"], listing["rebate"],
                listing["coupon_discount"], listing["delivered_price"],
                listing["classification"], listing["condition"], listing["stock_status"],
                listing["pickup_status"], listing["warranty"], listing["included_components"],
                listing["price_valid_until"],
                "|".join(listing["manual_fields"]), listing["last_checked_at"], listing["url"],
            ])
    return buf.getvalue()
