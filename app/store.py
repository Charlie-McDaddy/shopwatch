"""Query and assembly layer: everything the API and templates read goes through here."""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import pricing, provenance
from .config import load_config
from .db import utcnow
from .urls import public_http_url

PRODUCT_FIELDS = [
    "name", "model", "variant", "description", "image_url", "brand", "category",
    "generation", "verdict", "status", "buying_mode", "notes",
    "trigger_price", "excellent_price", "historical_low_price",
    "lowest_known_price", "lowest_known_date", "lowest_known_retailer", "lowest_known_notes",
    "quantity", "quantity_unit",
    "width_mm", "height_mm", "depth_mm", "weight_kg",
    "packaged_width_mm", "packaged_height_mm", "packaged_depth_mm", "packaged_weight_kg",
    "mounting_notes", "vesa", "fit_notes", "archived",
]

LISTING_FIELDS = provenance.TRACKED_FIELDS + [
    "product_id", "retailer_id", "active", "seller_name", "seller_id", "offer_id", "offer_key",
    "freight_postcode", "pickup_postcode",
]

VERDICTS = ["BUY", "MAYBE", "IGNORE"]

#: Where a product sits in the process, as distinct from the verdict on it.
#: ACTIVE    - being researched or hunted, alerts armed
#: PURCHASED - bought; the active card remains watched
#: PARKED    - set aside without deleting anything
STATUSES = ["ACTIVE", "PURCHASED", "PARKED"]

#: Matches style.css's --candidate-1..8 custom properties exactly, in order. Only the
#: count matters here (for the modulo cycle); the actual colour values live in CSS,
#: including their dark-mode variants, so this list is never rendered directly.
#: Shared by two features: a watch group's per-model identity colour (group_view,
#: assigned by list position) and, since issue #100, a single product's per-retailer
#: identity colour (retailer_color, hashed from the retailer's id). The two never
#: appear on the same page, so reusing one palette is not a collision risk.
CANDIDATE_PALETTE = ["blue", "amber", "green", "violet", "orange", "teal", "pink", "taupe"]
DEFAULT_PICKUP_RADIUS_KM = 25
PRICE_CHANGE_LABELS = {
    "INITIAL": "Initial observation",
    "CONTEXT_CHANGED": "Buying context changed; not a like-for-like comparison",
    "RETAILER_PRICE_REDUCTION": "Retailer price reduced in a comparable context",
    "NO_PRICE_REDUCTION": "No retailer price reduction",
    "UNKNOWN": "Comparison context unknown",
}


def normalise_model(model: str | None) -> str:
    """Model-matching key: case and separator insensitive.

    HW-Q930H/XY, hw q930h/xy and HWQ930H/XY are the same product. Region suffixes are
    kept, because HW-Q930H/XY and HW-Q930H/XU are genuinely different stock.
    """
    if not isinstance(model, str) or not model:
        return ""
    return re.sub(r"[^A-Z0-9/]", "", model.upper())


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --------------------------------------------------------------------------- retailers


def ensure_retailer(conn: sqlite3.Connection, name: str, **extra: Any) -> sqlite3.Row:
    """Find a retailer by name (case insensitive) or create it."""
    name = name.strip()
    row = conn.execute(
        "SELECT * FROM retailers WHERE lower(name) = lower(?) OR slug = ?",
        (name, slugify(name)),
    ).fetchone()
    if row:
        # Backfill an adapter/homepage onto a retailer first created by a bare import.
        updates = {
            k: v for k, v in (("adapter", extra.get("adapter")),
                              ("homepage", extra.get("homepage")))
            if v and not row[k]
        }
        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE retailers SET {assignments} WHERE id = ?",
                [*updates.values(), row["id"]],
            )
            row = conn.execute("SELECT * FROM retailers WHERE id = ?", (row["id"],)).fetchone()
        return row
    conn.execute(
        "INSERT INTO retailers (name, slug, adapter, homepage, notes) VALUES (?, ?, ?, ?, ?)",
        (name, slugify(name), extra.get("adapter"), extra.get("homepage"), extra.get("notes")),
    )
    return conn.execute("SELECT * FROM retailers WHERE slug = ?", (slugify(name),)).fetchone()


def list_retailers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every retailer, plus how many of its listings are currently active.

    `listing_count` (issue #112) is a cheap join, not a second endpoint - the new
    Retailers page (app/templates/retailers.html) needs it to tell someone what
    excluding a retailer is actually about to switch off, before they do it, and
    nothing else currently reading this list has a reason to mind an extra column.
    """
    rows = conn.execute(
        "SELECT r.*, COUNT(CASE WHEN l.active = 1 THEN 1 END) AS listing_count"
        " FROM retailers r LEFT JOIN listings l ON l.retailer_id = r.id"
        " GROUP BY r.id ORDER BY r.name"
    )
    return [dict(r) for r in rows]


def set_retailer_excluded(
    conn: sqlite3.Connection, retailer_id: int, excluded: bool
) -> dict[str, Any] | None:
    """Toggle a retailer's excluded flag (issue #112) - "never suggest this retailer
    again," enforced everywhere a retailer could enter the system. Returns the
    updated retailer row, or None if retailer_id does not exist.

    Excluding cascades: every currently active listing under this retailer, across
    every product, is deactivated the same way a single listing's own soft-delete
    already works (`UPDATE listings SET active = 0 ...`, app/main.py's
    api_deactivate_listing) - just scoped by retailer_id instead of one listing id.
    price_watch.py already only ever queries `WHERE l.active = 1`, so nothing else
    has to change for an excluded retailer's listings to stop being price-checked.
    Idempotent: re-excluding an already-excluded retailer just re-runs a no-op
    UPDATE (WHERE active = 1 matches nothing already deactivated).

    Un-excluding does NOT reactivate anything - deliberately (issue #112 left this
    as an open question; resolved here as "stay off"). A listing switched off by
    exclusion might also have been switched off for an unrelated reason, and
    silently resuming it on nothing more than "the exclusion lifted" would be a
    surprise, not a convenience. Reactivate by hand if that is actually wanted.
    """
    row = conn.execute("SELECT id FROM retailers WHERE id = ?", (retailer_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE retailers SET excluded = ? WHERE id = ?", (1 if excluded else 0, retailer_id)
    )
    if excluded:
        conn.execute(
            "UPDATE listings SET active = 0, updated_at = ?"
            " WHERE retailer_id = ? AND active = 1",
            (utcnow(), retailer_id),
        )
    return dict(conn.execute("SELECT * FROM retailers WHERE id = ?", (retailer_id,)).fetchone())


def retailer_color(retailer_id: int | None) -> str | None:
    """A stable colour for a retailer (issue #100), so its dot on a product's price
    axis and its row in the listing list below are recognisably the same retailer at
    a glance, without hovering or reading names.

    Hashed from the retailer's own id, not assigned by position in any one product's
    listing set - deliberately, so the same retailer keeps the same colour on every
    product it appears on, not just within one page. crc32 rather than the builtin
    hash(): PYTHONHASHSEED randomises str/int hashing per process by default, which
    would reshuffle every retailer's colour on each server restart. No schema change:
    reuses group_view's existing --candidate-1..8 palette (already colourblind-
    considered and dark-mode aware) rather than a second one just for this.
    """
    if retailer_id is None:
        return None
    index = zlib.crc32(str(retailer_id).encode()) % len(CANDIDATE_PALETTE)
    return f"var(--candidate-{index + 1})"


def household_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT postcode, pickup_radius_km, timezone FROM household_settings WHERE id = 1"
    ).fetchone()
    return dict(row)


def effective_freight_for_listing(
    listing: Mapping[str, Any], household_postcode: str | None
) -> tuple[Any, bool]:
    """Return the freight value valid for this postcode and whether it is current.

    Unmarked legacy quotes retain their behavior when no household postcode is set. A
    location-tagged quote counts only when its postcode matches the current setting. The
    saved freight value and its provenance remain untouched when the postcode moves.
    """
    freight = listing["freight"]
    quote_postcode = listing["freight_postcode"]
    stale = freight is not None and quote_postcode != household_postcode
    return (None if stale else freight), not stale


def pickup_current_for_listing(listing: Mapping[str, Any], household_postcode: str | None) -> bool:
    """Whether saved pickup information is confirmed for the current postcode."""
    has_info = listing["pickup_status"] is not None or listing["pickup_location"] is not None
    return not has_info or listing["pickup_postcode"] == household_postcode


def confirm_pickup_for_household(conn: sqlite3.Connection, listing_id: int) -> None:
    postcode = household_settings(conn)["postcode"]
    conn.execute("UPDATE listings SET pickup_postcode = ? WHERE id = ?", (postcode, listing_id))


def clear_pickup_location_confirmation(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET pickup_postcode = NULL WHERE id = ?", (listing_id,))


def confirm_freight_for_household(conn: sqlite3.Connection, listing_id: int) -> None:
    """Associate a manually entered freight value with the current household postcode."""
    postcode = household_settings(conn)["postcode"]
    conn.execute(
        "UPDATE listings SET freight_postcode = ? WHERE id = ?", (postcode, listing_id)
    )


def clear_freight_location_confirmation(conn: sqlite3.Connection, listing_id: int) -> None:
    """Do not treat a non-manual freight update as checked for the current postcode."""
    conn.execute("UPDATE listings SET freight_postcode = NULL WHERE id = ?", (listing_id,))


def update_household_settings(
    conn: sqlite3.Connection, data: Mapping[str, Any]
) -> dict[str, Any]:
    current = household_settings(conn)
    postcode = data.get("postcode", current["postcode"])
    if postcode is not None:
        if not isinstance(postcode, str):
            raise ValueError("postcode must be a four-digit string or blank")
        postcode = postcode.strip()
        if postcode and (len(postcode) != 4 or not postcode.isascii() or not postcode.isdigit()):
            raise ValueError("postcode must be a four-digit string")
        postcode = postcode or None

    radius = data.get("pickup_radius_km", current["pickup_radius_km"])
    if isinstance(radius, bool) or not isinstance(radius, int) or not 1 <= radius <= 500:
        raise ValueError("pickup_radius_km must be a whole number from 1 to 500")
    timezone = data.get("timezone", current["timezone"])
    if not isinstance(timezone, str):
        raise ValueError("timezone must be an IANA timezone")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be an IANA timezone") from exc
    conn.execute(
        "UPDATE household_settings SET postcode = ?, pickup_radius_km = ?, timezone = ?"
        " WHERE id = 1",
        (postcode, radius, timezone),
    )
    return household_settings(conn)


# --------------------------------------------------------------------------- products


def normalise_variant(variant: str | None) -> str:
    """Variant identity ignores case and repeated whitespace, while keeping wording."""
    return " ".join(variant.split()).casefold() if isinstance(variant, str) else ""


_UNSPECIFIED_VARIANT = object()


def products_by_model(conn: sqlite3.Connection, model: str) -> list[sqlite3.Row]:
    target = normalise_model(model)
    if not target:
        return []
    return [row for row in conn.execute("SELECT * FROM products")
            if normalise_model(row["model"]) == target]


def product_by_model(
    conn: sqlite3.Connection, model: str, variant: str | None | object = _UNSPECIFIED_VARIANT
) -> sqlite3.Row | None:
    """Match model plus variant, or a model-only lookup only when it is unambiguous."""
    matches = products_by_model(conn, model)
    if variant is _UNSPECIFIED_VARIANT:
        return matches[0] if len(matches) == 1 else None
    variant_key = normalise_variant(variant)
    return next((row for row in matches if normalise_variant(row["variant"]) == variant_key), None)


def get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def product_by_identity(
    conn: sqlite3.Connection, model: str | None, name: str, variant: str | None = None,
) -> sqlite3.Row | None:
    if model and model.strip():
        return product_by_model(conn, model, variant)
    name_key = normalise_variant(name)
    return next((row for row in conn.execute("SELECT * FROM products WHERE model = ''")
                 if normalise_variant(row["name"]) == name_key
                 and normalise_variant(row["variant"]) == normalise_variant(variant)), None)


def validate_product_metadata(data: Mapping[str, Any]) -> None:
    if data.get("model") is not None and not isinstance(data["model"], str):
        raise ValueError("model must be text")
    for field, limit in (("variant", 200), ("description", 5000), ("image_url", 2048)):
        value = data.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(
                f"{field.replace('_', ' ')} must be a string of at most {limit} characters"
            )
        if field == "image_url" and (
            value[:8].lower() != "https://" or not public_http_url(value)
        ):
            raise ValueError("image URL must be HTTPS on a public host")


def create_product(conn: sqlite3.Connection, data: Mapping[str, Any]) -> int:
    validate_product_metadata(data)
    _validate_quantity(data)
    _validate_buying_mode(data)
    payload = {k: v for k, v in data.items() if k in PRODUCT_FIELDS}
    for field in ("variant", "description", "image_url"):
        if isinstance(payload.get(field), str):
            payload[field] = payload[field].strip() or None
    payload.setdefault("category", "general")
    payload.setdefault("verdict", "MAYBE")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ValueError("name is required")
    payload["model"] = (payload.get("model") or "").strip()
    if product_by_identity(conn, payload["model"], payload["name"], payload.get("variant")):
        raise ValueError("this product and variant already exists")
    payload["specs_json"] = json.dumps(data.get("specs") or {})
    payload["components_json"] = json.dumps(data.get("components") or {})
    payload["created_at"] = payload["updated_at"] = utcnow()
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    cur = conn.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", list(payload.values()))
    product_id = int(cur.lastrowid)
    # Covers a caller that creates a product with lowest_known_price already known
    # (a wizard step, an import) but no targets typed in - the ordinary PATCH path
    # below is not the only way a product can start life with a low and no targets.
    maybe_derive_price_targets(conn, product_id)
    if not payload.get("archived") and payload.get("status", "ACTIVE") in {"ACTIVE", "PURCHASED"}:
        from . import retailer_search
        retailer_search.request_product_discovery(conn, product_id, trigger="initial")
    return product_id


def update_product(conn: sqlite3.Connection, product_id: int, data: Mapping[str, Any]) -> None:
    validate_product_metadata(data)
    current = get_product(conn, product_id)
    _validate_quantity(data, current=current)
    if "name" in data and (not isinstance(data["name"], str) or not data["name"].strip()):
        raise ValueError("name is required")
    model = (data.get("model", current["model"] if current else None) or "").strip()
    variant = data.get("variant", current["variant"] if current else None)
    name = data.get("name", current["name"] if current else "")
    duplicate = product_by_identity(conn, model, name, variant)
    if duplicate and duplicate["id"] != product_id:
        raise ValueError("this exact product model and variant already exists")
    _validate_buying_mode(data)
    payload = {k: v for k, v in data.items() if k in PRODUCT_FIELDS}
    if "model" in payload:
        payload["model"] = model
    if "specs" in data:
        payload["specs_json"] = json.dumps(data["specs"] or {})
    if "components" in data:
        payload["components_json"] = json.dumps(data["components"] or {})
    if not payload:
        return
    # A genuine hand-typed change to a derived target locks it - the same "a manual
    # edit beats an automated write" discipline provenance.py enforces for listings,
    # just without the full state machine (see migrations/0014 for why three booleans
    # are enough here). "Genuine" matters: the edit dialog's Save always resubmits
    # every field on the form (app.js ep-save), whether or not the user touched it,
    # so "present in the payload" cannot mean "the user changed this" - only a value
    # that actually differs from what is stored does. Clearing a target back to null
    # is a deliberate action too, but it reads as "let shopwatch guess again", not as
    # a value worth protecting, so it unlocks the field instead of locking it at null.
    lockable = {"trigger_price", "excellent_price", "historical_low_price"} & payload.keys()
    if lockable:
        current = get_product(conn, product_id)
        if current is not None:
            for field in lockable:
                if payload[field] != current[field]:
                    payload[f"{field}_auto"] = 0
    payload["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = ?" for k in payload)
    conn.execute(
        f"UPDATE products SET {assignments} WHERE id = ?", [*payload.values(), product_id]
    )
    maybe_derive_price_targets(conn, product_id)



def _validate_buying_mode(data: Mapping[str, Any]) -> None:
    if "buying_mode" in data and data["buying_mode"] not in {"just_watching", "buy_asap"}:
        raise ValueError("buying_mode must be just_watching or buy_asap")

def _validate_quantity(data: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> None:
    quantity = data.get("quantity", current["quantity"] if current else None)
    unit = data.get("quantity_unit", current["quantity_unit"] if current else None)
    if quantity is None and unit in (None, ""):
        return
    if not isinstance(unit, str) or unit not in {"g", "kg", "ml", "L", "each"}:
        raise ValueError("quantity unit must be g, kg, ml, L, or each")
    if isinstance(quantity, bool):
        raise ValueError("quantity must be a positive number")
    try:
        amount = float(quantity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("quantity must be a positive number") from exc
    if not 0 < amount < float("inf"):
        raise ValueError("quantity must be a positive number")


def delete_product(conn: sqlite3.Connection, product_id: int) -> None:
    """Permanently remove a product and everything under it - listings, price
    history, purchases, research/llm jobs, offer matches - via the schema's own
    ON DELETE CASCADE (app/db.py turns PRAGMA foreign_keys on, so this is a real
    cascade, not orphaned rows). Distinct from update_product({"archived": 1}),
    which is what "Give up on this" uses and keeps every row: this one has no
    undo, which is the whole reason it needs its own explicit call rather than
    being a flag flip like archiving."""
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


# --------------------------------------------------------------------------- listings


def create_listing(conn: sqlite3.Connection, data: Mapping[str, Any]) -> int:
    payload = {k: v for k, v in data.items() if k in LISTING_FIELDS}
    if not payload.get("product_id") or not payload.get("retailer_id"):
        raise ValueError("product_id and retailer_id are required")
    payload.setdefault("condition", "NEW")
    payload["offer_key"] = payload.get("offer_key") or ""
    payload["created_at"] = payload["updated_at"] = utcnow()
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    cur = conn.execute(f"INSERT INTO listings ({cols}) VALUES ({marks})", list(payload.values()))
    return int(cur.lastrowid)


def find_listing(
    conn: sqlite3.Connection, product_id: int, retailer_id: int, url: str | None = None,
    offer_key: str | None = None,
) -> sqlite3.Row | None:
    if url:
        if offer_key is not None:
            return conn.execute(
                "SELECT * FROM listings WHERE product_id = ? AND retailer_id = ? AND url = ?"
                " AND offer_key = ?", (product_id, retailer_id, url, offer_key),
            ).fetchone()
        row = conn.execute(
            "SELECT * FROM listings WHERE product_id = ? AND retailer_id = ? AND url = ?",
            (product_id, retailer_id, url),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM listings WHERE product_id = ? AND retailer_id = ? ORDER BY id LIMIT 1",
        (product_id, retailer_id),
    ).fetchone()


def enrich_listing(
    conn: sqlite3.Connection, row: sqlite3.Row, product: Mapping[str, Any], penalty: float,
    household_postcode: str | None,
) -> dict[str, Any]:
    """Attach delivered price, classification, provenance and verification to a listing row."""
    listing = dict(row)
    listing["provenance"] = provenance.provenance_map(conn, listing["id"])
    stale = any(value["state"] == provenance.STALE
                for value in listing["provenance"].values())
    from . import url_intake
    domain = url_intake.bare_domain(listing.get("url") or "")
    adapter_cls = url_intake.adapter_for_domain(domain) if domain else None
    automatic = bool(product["model"] and adapter_cls
                     and adapter_cls.slug == listing.get("retailer_adapter"))
    listing["check_capability"] = "AUTOMATIC" if automatic else "MANUAL"
    listing["check_status"] = (
        "BLOCKED" if listing.get("check_error") else
        "STALE" if stale else listing["check_capability"]
    )
    effective_freight, freight_current = effective_freight_for_listing(
        listing, household_postcode
    )
    provisional = pricing.delivered_price(
        listing["advertised_price"],
        effective_freight,
        listing["cashback"],
        listing["rebate"],
        listing["coupon_discount"],
    )
    conditional = bool((listing.get("conditional_requirements") or "").strip())
    eligibility_provenance = listing["provenance"]["eligibility_confirmed"]
    eligible = bool(listing.get("eligibility_confirmed")) and (
        eligibility_provenance["state"] == provenance.MANUAL
        and eligibility_provenance["manual_locked"]
    )
    listing["eligibility_confirmed"] = bool(eligible)
    delivered = pricing.delivered_price(
        listing["advertised_price"], effective_freight,
        listing["cashback"] if not conditional or eligible else None,
        listing["rebate"] if not conditional or eligible else None,
        listing["coupon_discount"] if not conditional or eligible else None,
    )
    listing["delivered"] = delivered
    listing["delivered_price"] = delivered.value
    listing["delivered_resolved"] = delivered.resolved
    listing["freight_current"] = freight_current
    listing["pickup_current"] = pickup_current_for_listing(listing, household_postcode)
    listing["provisional_delivered_price"] = (
        provisional.value if conditional and not eligible else None
    )
    listing["classification"] = pricing.classify(delivered, product)
    listing["classification_label"] = pricing.CLASS_LABELS[listing["classification"]]
    listing["diff_from_low"] = pricing.difference_from_low(
        delivered, product.get("lowest_known_price")
    )
    listing["verification"] = provenance.verification_map(conn, listing["id"])
    if product["model"] and listing.get("model_on_page"):
        listing["model_match"] = (
            normalise_model(listing["model_on_page"]) == normalise_model(product["model"])
        )
    if not freight_current and listing["freight"] is not None:
        listing["verification"]["freight"] = {
            **listing["verification"]["freight"],
            "status": provenance.STALE,
            "note": (
                f"Previous quote is for postcode {listing['freight_postcode'] or 'unknown'}; "
                f"current postcode is {household_postcode}. Reconfirm freight."
            ),
        }
    listing["manual_fields"] = sorted(
        f for f, p in listing["provenance"].items() if p["manual_locked"]
    )
    listing["rank"] = pricing.rank_key(delivered, penalty)
    listing["offer_eligible"], listing["eligibility_reasons"] = pricing.listing_eligibility(
        listing
    )
    # Display-only: whether a retailer-stated promo end-date has already passed. This
    # never touches classify() or rank_key() above - issue #97 leaves "should a lapsed
    # promo affect classification" as a separate, undecided question, so a lapsed date
    # is shown, not acted on.
    listing["promo_lapsed"] = (
        listing["price_valid_until"] is not None
        and date.fromisoformat(listing["price_valid_until"]) < datetime.now(UTC).date()
    )
    return listing


SORT_KEYS = {
    "delivered": lambda r: r["rank"],
    "headline": lambda r: (r["advertised_price"] is None, r["advertised_price"] or 0.0),
    "retailer": lambda r: (r["retailer_name"] or "").lower(),
    "stock": lambda r: (r["stock_status"] or "zzz").lower(),
    "condition": lambda r: (
        pricing.CONDITIONS.index(r["condition"]) if r["condition"] in pricing.CONDITIONS else 99
    ),
    "verification": lambda r: -sum(
        1 for v in r["verification"].values() if v["status"] in {"VERIFIED", "LIVE", "MANUAL"}
    ),
    "diff": lambda r: (r["diff_from_low"] is None, r["diff_from_low"] or 0.0),
}


def listings_for_product(
    conn: sqlite3.Connection,
    product: Mapping[str, Any],
    sort: str = "delivered",
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    penalty = load_config().unresolved_freight_penalty
    where = "WHERE l.product_id = ?" + ("" if include_inactive else " AND l.active = 1")
    rows = conn.execute(
        "SELECT l.*, r.name AS retailer_name, r.slug AS retailer_slug,"
        " r.adapter AS retailer_adapter"
        " FROM listings l JOIN retailers r ON r.id = l.retailer_id "
        + where,
        (product["id"],),
    ).fetchall()
    household_postcode = household_settings(conn)["postcode"]
    enriched = [
        enrich_listing(conn, r, product, penalty, household_postcode) for r in rows
    ]
    key = SORT_KEYS.get(sort, SORT_KEYS["delivered"])
    enriched.sort(key=key)
    return enriched


def product_view(
    conn: sqlite3.Connection, product_id: int, sort: str = "delivered"
) -> dict[str, Any] | None:
    """The full comparison view for one product: summary plus the retailer matrix."""
    row = get_product(conn, product_id)
    if row is None:
        return None
    product = dict(row)
    schedule_row = conn.execute(
        "SELECT cadence, last_attempt_at, last_success_at, next_due_at, last_error"
        " FROM product_check_schedules WHERE product_id = ?", (product_id,)
    ).fetchone()
    schedule = dict(schedule_row) if schedule_row else None
    if schedule:
        schedule["timezone"] = household_settings(conn)["timezone"]
        zone = ZoneInfo(schedule["timezone"])
        for key in ("last_attempt_at", "last_success_at", "next_due_at"):
            value = schedule[key]
            if value:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=UTC)
                local = instant.astimezone(zone)
                schedule[key.replace("_at", "_local")] = local.strftime("%d %b %Y %H:%M")
    product["check_schedule"] = schedule
    discovery_schedule = conn.execute(
        "SELECT next_due_at, last_attempt_at, last_error FROM product_discovery_schedules"
        " WHERE product_id = ?", (product_id,)
    ).fetchone()
    product["discovery_schedule"] = dict(discovery_schedule) if discovery_schedule else None
    discovery_job = conn.execute(
        "SELECT id, status, trigger, result, error, requested_at, finished_at"
        " FROM retailer_search_jobs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if discovery_job:
        discovery_job = dict(discovery_job)
        try:
            discovery_job["result"] = json.loads(discovery_job["result"] or "{}")
        except (TypeError, ValueError):
            discovery_job["result"] = {}
        if not isinstance(discovery_job["result"], dict):
            discovery_job["result"] = {}
    product["discovery_job"] = discovery_job
    product["specs"] = json.loads(product.pop("specs_json") or "{}")
    product["components"] = json.loads(product.pop("components_json") or "{}")
    listings = listings_for_product(conn, product, sort=sort)
    # Set once here so the axis (via threshold_scale, which copies it onto each point
    # below) and the listing rows read the same value off the same dict - never
    # derived twice, which is how the two views could quietly drift apart.
    for listing in listings:
        listing["retailer_color"] = retailer_color(listing.get("retailer_id"))

    best = pricing.best_listing(listings, load_config().unresolved_freight_penalty)
    product["best_listing"] = best
    product["best_delivered"] = best["delivered_price"] if best else None
    product["best_unit_price"] = pricing.unit_price(
        product["best_delivered"], product.get("quantity"), product.get("quantity_unit"))
    product["best_retailer"] = best["retailer_name"] if best else None
    product["best_classification"] = best["classification"] if best else pricing.UNRESOLVED
    product["best_resolved"] = bool(best and best["delivered_resolved"])
    product["diff_from_low"] = (
        pricing.difference_from_low(best["delivered_price"], product["lowest_known_price"])
        if best
        else None
    )
    product["listings"] = listings
    product["category_fields"] = category_fields(conn, product["category"])

    product["scale"] = pricing.threshold_scale(product, product["best_delivered"], listings)

    purchase = latest_purchase(conn, product_id)
    product["purchase"] = purchase
    if purchase:
        # Did it get cheaper after you bought it? The whole reason to keep watching.
        product["moved_since_purchase"] = (
            round(product["best_delivered"] - purchase["price_paid"], 2)
            if product["best_delivered"] is not None
            else None
        )
        product["protection_open"] = protection_open(purchase)
    else:
        product["moved_since_purchase"] = None
        product["protection_open"] = False

    # Live offers that could touch this product. Imported lazily to keep the module
    # graph acyclic: offers reads the product view, the product view lists offers.
    from . import offers as _offers
    product["offers"] = _offers.offers_for_product(conn, product_id)

    product["tone"], product["verdict_line"] = pricing.verdict_line(product)
    # Contenders are what you choose between; the rest are folded away by default.
    product["contenders"] = [
        listing for listing in listings
        if listing["classification"] != pricing.ABOVE_TARGET
    ][:4]
    if not product["contenders"]:
        product["contenders"] = listings[:2]
    contender_ids = {listing["id"] for listing in product["contenders"]}
    product["also_ran"] = [listing for listing in listings if listing["id"] not in contender_ids]
    return product


# -------------------------------------------------------------------------- purchases


def protection_open(purchase: Mapping[str, Any] | None) -> bool:
    """Is a price-protection window still running? Dates are plain YYYY-MM-DD."""
    if not purchase or not purchase.get("price_protection_until"):
        return False
    return str(purchase["price_protection_until"])[:10] >= utcnow()[:10]


def latest_purchase(conn: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM purchases WHERE product_id = ? ORDER BY purchased_at DESC, id DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    return dict(row) if row else None


def record_purchase(
    conn: sqlite3.Connection, product_id: int, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Record a purchase and move the product to PURCHASED.

    The listing is optional: plenty of things get bought in a shop, over the phone, or
    from somewhere that was never on the board. `price_paid` is the delivered figure and
    is the only required number, because it is the only one that settles the question.
    """
    price_paid = data.get("price_paid")
    if price_paid in (None, ""):
        raise ValueError("price_paid (delivered) is required")
    try:
        price_paid = float(price_paid)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"price_paid must be a number, got {price_paid!r}") from exc

    listing_id = data.get("listing_id")
    retailer_id, retailer_name = None, data.get("retailer_name")
    if listing_id:
        row = conn.execute(
            "SELECT l.retailer_id, r.name FROM listings l JOIN retailers r ON r.id = l.retailer_id"
            " WHERE l.id = ?",
            (listing_id,),
        ).fetchone()
        if row:
            retailer_id, retailer_name = row["retailer_id"], row["name"]
    elif retailer_name:
        retailer_id = ensure_retailer(conn, retailer_name)["id"]

    cur = conn.execute(
        "INSERT INTO purchases (product_id, listing_id, retailer_id, retailer_name,"
        " purchased_at, price_paid, advertised_paid, freight_paid, condition,"
        " order_reference, warranty_months, price_protection_until, notes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            product_id, listing_id, retailer_id, retailer_name,
            data.get("purchased_at") or utcnow()[:10], price_paid,
            data.get("advertised_paid"), data.get("freight_paid"),
            (data.get("condition") or "NEW").upper(), data.get("order_reference"),
            data.get("warranty_months"), data.get("price_protection_until"),
            data.get("notes"), utcnow(),
        ),
    )
    conn.execute(
        "UPDATE products SET status = 'PURCHASED', updated_at = ? WHERE id = ?",
        (utcnow(), product_id),
    )
    # The purchase is itself an observation, and the best one: it is a price someone
    # actually paid rather than one a page advertised.
    conn.execute(
        "INSERT INTO price_history (product_id, listing_id, retailer_id, observed_at,"
        " advertised_price, delivered_price, freight, freight_resolved, condition, source, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'purchase', ?)",
        (
            product_id, listing_id, retailer_id, data.get("purchased_at") or utcnow(),
            data.get("advertised_paid"), price_paid, data.get("freight_paid"),
            (data.get("condition") or "NEW").upper(),
            data.get("notes") or "purchase recorded",
        ),
    )
    return dict(conn.execute("SELECT * FROM purchases WHERE id = ?", (cur.lastrowid,)).fetchone())


def undo_purchase(conn: sqlite3.Connection, product_id: int) -> bool:
    """Delete the most recent purchase and return the product to ACTIVE.

    The price_history row is deliberately left behind: it records a price that really was
    paid, and history is append-only even when the bookkeeping was wrong.

    Group siblings remain in their existing watch state; undoing a purchase only changes
    the purchased product and leaves its append-only price observation behind.
    """
    purchase = latest_purchase(conn, product_id)
    if not purchase:
        return False
    conn.execute("DELETE FROM purchases WHERE id = ?", (purchase["id"],))
    conn.execute(
        "UPDATE products SET status = 'ACTIVE', updated_at = ? WHERE id = ?",
        (utcnow(), product_id),
    )
    return True


STATUS_ORDER = {"ACTIVE": 0, "PURCHASED": 1, "PARKED": 2}


# ----------------------------------------------------------------------- watch groups
#
# Several genuinely different products that satisfy the same want, tracked together
# because you will buy whichever wins, not all of them. A group carries no price
# targets of its own - a $500 GPU and a $2,000 TV would never share a trigger - each
# member keeps its own.

GROUP_FIELDS = ["name", "notes"]


def create_group(conn: sqlite3.Connection, data: Mapping[str, Any]) -> int:
    payload = {k: v for k, v in data.items() if k in GROUP_FIELDS}
    if not payload.get("name"):
        raise ValueError("name is required")
    payload["created_at"] = payload["updated_at"] = utcnow()
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    cur = conn.execute(
        f"INSERT INTO watch_groups ({cols}) VALUES ({marks})", list(payload.values())
    )
    return int(cur.lastrowid)


def get_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watch_groups WHERE id = ?", (group_id,)).fetchone()


def update_group(conn: sqlite3.Connection, group_id: int, data: Mapping[str, Any]) -> None:
    payload = {k: v for k, v in data.items() if k in GROUP_FIELDS}
    if not payload:
        return
    payload["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = ?" for k in payload)
    conn.execute(
        f"UPDATE watch_groups SET {assignments} WHERE id = ?", [*payload.values(), group_id]
    )


def set_product_group(
    conn: sqlite3.Connection, product_id: int, group_id: int | None
) -> None:
    """Attach a product to a group, or detach it (group_id=None)."""
    conn.execute(
        "UPDATE products SET group_id = ?, updated_at = ? WHERE id = ?",
        (group_id, utcnow(), product_id),
    )


def archive_other_group_members(conn: sqlite3.Connection, purchased_product_id: int) -> int:
    """Explicitly archive every alternative to a chosen group candidate.

    This lifecycle operation is separate from recording a purchase: purchase does not
    determine whether the household wants to keep watching the other candidates. Archives
    every OTHER product in the same group and marks the group itself archived. Returns
    how many products were archived.
    """
    product = get_product(conn, purchased_product_id)
    if product is None or product["group_id"] is None:
        return 0
    group_id = product["group_id"]
    cur = conn.execute(
        "UPDATE products SET archived = 1, updated_at = ?"
        " WHERE group_id = ? AND id != ? AND archived = 0",
        (utcnow(), group_id, purchased_product_id),
    )
    conn.execute(
        "UPDATE watch_groups SET archived = 1, updated_at = ? WHERE id = ?",
        (utcnow(), group_id),
    )
    return cur.rowcount


def group_view(
    conn: sqlite3.Connection, group_id: int, sort: str = "delivered"
) -> dict[str, Any] | None:
    """The full comparison view for one group: every member product plus a merged axis."""
    row = get_group(conn, group_id)
    if row is None:
        return None
    group = dict(row)
    member_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM products WHERE group_id = ? ORDER BY name", (group_id,)
        )
    ]
    members = [p for p in (product_view(conn, pid, sort=sort) for pid in member_ids) if p]
    # Assigned once here, by list position, so a candidate's row and its axis point(s)
    # always agree on which colour is "theirs" without the template re-deriving it.
    # Deliberately not the act/close/fault/manual palette: this answers "which model",
    # not "should I act" - the two systems must never be confusable if they ever
    # appear on the same page together.
    for index, member in enumerate(members):
        member["candidate_color"] = f"var(--candidate-{(index % len(CANDIDATE_PALETTE)) + 1})"
    group["members"] = members

    points = []
    for member in members:
        for listing in member["listings"]:
            if listing["delivered_price"] is None:
                continue
            points.append({
                # Matches _product.html's ruler() macro field names (pt.id, pt.retailer,
                # ...) so the group page can reuse that macro unmodified.
                "id": listing["id"],
                "product_id": member["id"],
                "product_name": member["name"],
                "retailer": f"{member['name']} - {listing['retailer_name']}",
                # Plain retailer name, no product prefix - the hover callout sits next
                # to a row that already names the candidate, so repeating it would be
                # noise. "retailer" above stays as-is for the existing title/label text
                # and the visually-hidden list, which need the candidate named.
                "retailer_name": listing["retailer_name"],
                "value": listing["delivered_price"],
                "resolved": bool(listing["delivered_resolved"]),
                "ruled_out": not pricing.listing_is_comparable(listing),
                "offer_eligible": listing["offer_eligible"],
                "verification": listing.get("verification"),
                "candidate_color": member["candidate_color"],
            })
    group["scale"] = pricing.merge_price_points(points)
    # Same rule as a single product's best_listing: a ruled-out candidate never wins,
    # however cheap.
    contenders = [p for p in points if p["offer_eligible"]]
    group["best"] = min(contenders, key=lambda p: p["value"]) if contenders else None
    return group


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Permanently remove a group. Members are never touched - the FK's own
    ON DELETE SET NULL (migration 0006) detaches any remaining product from it
    automatically, the same "the group is just a grouping" reasoning that migration's
    own comment gives for not cascading. There was no way to do this at all before -
    "nothing in the API actually does today" was true until this existed."""
    conn.execute("DELETE FROM watch_groups WHERE id = ?", (group_id,))


def delete_group_if_empty(conn: sqlite3.Connection, group_id: int) -> bool:
    """Delete a group once nothing points at it any more, and only then.

    "Empty" means no product's group_id references it - not "every member is
    archived". An explicitly resolved group keeps every member's group_id exactly as it
    was: "these were the candidates" is useful history, the same reason a product is
    archived rather than deleted. Auto-deleting on that would erase it.
    This only fires from an action that actually detaches a product from the group -
    "Leave group", or a permanent product delete - never from archiving.

    Returns whether it actually deleted anything, so a caller can report it.
    """
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM products WHERE group_id = ?", (group_id,)
    ).fetchone()["n"]
    if remaining:
        return False
    delete_group(conn, group_id)
    return True


def list_groups(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    where = "" if include_archived else "WHERE archived = 0"
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM watch_groups {where} ORDER BY name")]
    return [g for g in (group_view(conn, gid) for gid in ids) if g]


def list_products(
    conn: sqlite3.Connection,
    include_archived: bool = False,
    status: str | None = None,
    exclude_grouped: bool = False,
) -> list[dict[str, Any]]:
    """Products with their matrices assembled. ACTIVE first, so the hunt stays on top.

    `exclude_grouped` is for the board's HTML view only: a grouped product's card is
    the group's, not its own, so the board asks for the ungrouped set and renders
    groups separately. The JSON API (GET /api/products) leaves this off by default, so
    an API consumer still sees every product, grouped or not.

    Only excludes membership in a STILL-ACTIVE group. If a group is explicitly archived,
    the product's own group_id is left untouched as a historical fact - "which candidates
    were considered" - but
    that must never make the product itself invisible: its own group's card has
    stopped rendering (list_groups excludes archived groups), so if this exclusion
    still hid it too, a purchased, non-archived product would vanish from the board
    entirely.
    """
    clauses = ["archived = 1"] if status and status.upper() == "ARCHIVED" else (
        [] if include_archived else ["archived = 0"]
    )
    params: list[Any] = []
    if status and status.upper() not in ("ALL", "ARCHIVED"):
        clauses.append("status = ?")
        params.append(status.upper())
    if exclude_grouped:
        clauses.append(
            "(group_id IS NULL OR group_id NOT IN (SELECT id FROM watch_groups WHERE archived = 0))"
        )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM products{where} ORDER BY name", params)]
    products = [p for p in (product_view(conn, pid) for pid in ids) if p]
    products.sort(key=lambda p: (STATUS_ORDER.get(p["status"], 9), p["name"].lower()))
    return products


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Per-status tallies for the board's tabs, kept honest with what the tabs
    actually show.

    An ACTIVE member of a still-active group is folded into the group's card
    (see list_products' exclude_grouped), but the group card discloses it - "N
    candidates" - so counting it under ACTIVE still reconciles with what is on
    screen across the group card plus any standalone ones. A PURCHASED (or
    PARKED) member of a still-active group has no such stand-in: nothing on the
    board shows it as bought, so counting it there produced a "Bought 1" badge
    that opened onto an empty tab - a resolved-looking number with nothing behind
    it. It is excluded from status tallies while its active group represents it on the
    group card. A purchase leaves the active group and its alternatives intact.
    """
    rows = conn.execute(
        """
        SELECT status, COUNT(*) n FROM products
        WHERE archived = 0
          AND NOT (
            status != 'ACTIVE'
            AND group_id IS NOT NULL
            AND group_id IN (SELECT id FROM watch_groups WHERE archived = 0)
          )
        GROUP BY status
        """
    )
    counts = {row["status"]: row["n"] for row in rows}
    counts["ARCHIVED"] = conn.execute(
        "SELECT COUNT(*) FROM products WHERE archived = 1"
    ).fetchone()[0]
    counts["ALL"] = sum(count for status, count in counts.items() if status != "ARCHIVED")
    return counts


# --------------------------------------------------------------------------- history


def record_observation(
    conn: sqlite3.Connection,
    listing_id: int,
    source: str,
    note: str | None = None,
) -> int | None:
    """Append a price-history row from a listing's current state.

    Skipped when there is no price to record: a row of nulls is noise, not history.
    """
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None or row["advertised_price"] is None:
        return None
    current_freight, _freight_current = effective_freight_for_listing(
        row, household_settings(conn)["postcode"]
    )
    confirmation = conn.execute(
        "SELECT state, manual_locked FROM field_provenance"
        " WHERE listing_id = ? AND field = 'eligibility_confirmed'", (listing_id,)
    ).fetchone()
    eligibility_confirmed = bool(row["eligibility_confirmed"]) and bool(
        confirmation and confirmation["state"] == provenance.MANUAL
        and confirmation["manual_locked"]
    )
    apply_discounts = not (row["conditional_requirements"] or "").strip() or eligibility_confirmed
    delivered = pricing.delivered_price(
        row["advertised_price"], current_freight,
        row["cashback"] if apply_discounts else None,
        row["rebate"] if apply_discounts else None,
        row["coupon_discount"] if apply_discounts else None,
    )
    settings = household_settings(conn)
    verification = provenance.verification_map(conn, listing_id)
    price_context = json.dumps(
        {
            "postcode": settings["postcode"],
            "condition": row["condition"],
            "stock_status": row["stock_status"],
            "conditional_requirements": row["conditional_requirements"],
            "eligibility_confirmed": eligibility_confirmed,
            "delivery": {
                "freight": current_freight,
                "resolved": delivered.resolved,
            },
            "cashback": row["cashback"] if apply_discounts else None,
            "rebate": row["rebate"] if apply_discounts else None,
            "coupon_discount": row["coupon_discount"] if apply_discounts else None,
            "pickup": {
                "status": row["pickup_status"],
                "location": row["pickup_location"],
                "current": pickup_current_for_listing(row, settings["postcode"]),
            },
            "verification": {
                aspect: verification[aspect]["status"]
                for aspect in ("model", "price", "stock", "freight", "condition")
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    previous = conn.execute(
        "SELECT advertised_price, price_context FROM price_history"
        " WHERE listing_id = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    if previous is None:
        change_reason = "INITIAL"
    elif previous["price_context"] is None:
        change_reason = "UNKNOWN"
    elif previous["price_context"] != price_context:
        change_reason = "CONTEXT_CHANGED"
    elif any(
        verification[aspect]["status"] != provenance.VERIFIED
        for aspect in ("model", "price")
    ):
        change_reason = "UNKNOWN"
    elif (
        previous["advertised_price"] is not None
        and row["advertised_price"] is not None
        and row["advertised_price"] < previous["advertised_price"]
    ):
        change_reason = "RETAILER_PRICE_REDUCTION"
    else:
        change_reason = "NO_PRICE_REDUCTION"
    cur = conn.execute(
        "INSERT INTO price_history (product_id, listing_id, retailer_id, observed_at,"
        " advertised_price, delivered_price, freight, freight_resolved, cashback,"
        " stock_status, condition, source, note, price_context, price_change_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["product_id"], listing_id, row["retailer_id"], utcnow(),
            row["advertised_price"], delivered.value, row["freight"], int(delivered.resolved),
            row["cashback"], row["stock_status"], row["condition"], source, note,
            price_context, change_reason,
        ),
    )
    maybe_lower_known_low(conn, row["product_id"], delivered, listing_id)
    return int(cur.lastrowid)


def maybe_lower_known_low(
    conn: sqlite3.Connection, product_id: int, delivered: pricing.Delivered, listing_id: int
) -> bool:
    """Lower the product's recorded historical low when a confirmed price beats it.

    Only confirmed-freight prices qualify. The low is never raised automatically: the
    whole point of a historical low is that it survives the market moving.
    """
    if not delivered.resolved or delivered.value is None:
        return False
    product = get_product(conn, product_id)
    if product is None:
        return False
    current = product["lowest_known_price"]
    if current is not None and delivered.value >= current:
        return False
    retailer = conn.execute(
        "SELECT r.name FROM listings l JOIN retailers r ON r.id = l.retailer_id WHERE l.id = ?",
        (listing_id,),
    ).fetchone()
    conn.execute(
        "UPDATE products SET lowest_known_price = ?, lowest_known_date = ?,"
        " lowest_known_retailer = ?, updated_at = ? WHERE id = ?",
        (
            delivered.value,
            utcnow()[:10],
            retailer["name"] if retailer else None,
            utcnow(),
            product_id,
        ),
    )
    maybe_derive_price_targets(conn, product_id)
    return True


#: How far above the confirmed low a target sits, expressed as a multiplier. excellent_price
#: matches the low exactly - matching the best price ever seen is what "excellent" means, not
#: some looser band under it. trigger_price sits 10% above it: loose enough that a genuine
#: improving trend crosses it before the record itself gets broken, tight enough that "worth
#: acting on" still means something. Both are candidates issue #101 itself raised; picked over
#: a wider band because a derived default's whole job is to stop a product sitting un-actionable,
#: not to already be the ideal number - a human who wants tighter or looser can always type over
#: it, and doing so locks the field (see update_product).
TRIGGER_MARGIN = 1.10


def maybe_derive_price_targets(conn: sqlite3.Connection, product_id: int) -> bool:
    """Fill trigger_price/excellent_price/historical_low_price from lowest_known_price
    when they are unset.

    "Unset" is the whole trigger: this only ever writes into a NULL field, so a value
    the user typed in by hand - or a previous auto-fill they have not cleared - is
    never touched. It is a one-shot fill, not a standing link: once written, the value
    sits still (still labelled "auto" via *_price_auto) until either a human overwrites
    it, which locks it per update_product, or clears it back to null, which puts it
    back in scope for this function to fill again on the next call.

    historical_low_price = lowest_known_price exactly, same reasoning as excellent_price
    below: matching the actual recorded low IS what "historical low territory" means.
    Unlike the other two, this one is not just a display default - pricing.classify()
    reads historical_low_price directly, checked before excellent_price and trigger_price
    (app/pricing.py, HISTORICAL_LOW is checked first and wins ties). So on a product that
    already has a lowest_known_price and no historical_low_price set, this can change a
    listing's classification the moment it runs - a listing sitting at or under the known
    low newly rates HISTORICAL LOW TERRITORY instead of whatever it rated before. That is
    the intended behaviour (the classification was arguably wrong before, given the price
    data already on hand), not a side effect to guard against, but it is a real behavioural
    change on existing products the first time this ships, not only a cosmetic UI default
    the way the other two fields are. One consequence worth knowing: because both default
    to exactly lowest_known_price, the EXCELLENT tier has no width under pure auto-derived
    defaults - a price at or under the low always resolves to the better HISTORICAL_LOW
    rating instead. A human who wants a distinct EXCELLENT band back can always type over
    either threshold, which locks it.

    Called from every place lowest_known_price can change: update_product (a human
    typing it into the edit dialog, or confirming a historical-low research finding
    the same way), maybe_lower_known_low (auto-tracking beating the known low), and
    create_product (a product created with a low already known and no targets typed).
    """
    product = get_product(conn, product_id)
    if product is None:
        return False
    low = product["lowest_known_price"]
    if low is None:
        return False
    updates: dict[str, Any] = {}
    if product["historical_low_price"] is None:
        updates["historical_low_price"] = low
        updates["historical_low_price_auto"] = 1
    if product["excellent_price"] is None:
        updates["excellent_price"] = low
        updates["excellent_price_auto"] = 1
    if product["trigger_price"] is None:
        updates["trigger_price"] = round(low * TRIGGER_MARGIN, 2)
        updates["trigger_price_auto"] = 1
    if not updates:
        return False
    updates["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE products SET {assignments} WHERE id = ?", [*updates.values(), product_id]
    )
    return True


#: Name of the one-off backfill row in python_backfills (see run_price_target_backfill).
#: Numbered like the SQL migrations for the same "forward-only, applied once" reason,
#: but kept out of schema_migrations itself - see that function's docstring.
PRICE_TARGET_BACKFILL_NAME = "0001_price_targets_from_lowest_known_low"


def run_price_target_backfill(conn: sqlite3.Connection) -> int:
    """One-off sweep: run maybe_derive_price_targets against every existing product once.

    maybe_derive_price_targets (above) only ever runs when create_product, update_product,
    or maybe_lower_known_low is called - so a product that already had a lowest_known_price
    before #106 shipped, and has not been touched by any of those three paths since, sits
    with trigger_price/excellent_price/historical_low_price still NULL until some unrelated
    future edit happens to fire one of them. Confirmed live: a product sat at "No trigger
    set" until a no-op Edit-Save was performed on it, at which point targets appeared
    correctly. This sweep catches every such product up on the next deploy instead of
    leaving it to chance.

    Tracked the same way app/migrations/*.sql tracks schema changes in schema_migrations -
    forward-only, applied exactly once, self-marking - but in its own python_backfills
    table rather than schema_migrations itself. schema_migrations' rows are meant to
    correspond 1:1 with the *.sql files db.migrate() finds under app/migrations; this is a
    one-off Python sweep with business logic (maybe_derive_price_targets), not a DDL file,
    and a synthetic entry in schema_migrations would break that correspondence for anyone
    auditing it against the files on disk. A dedicated table gets the same "applied once,
    tracked" discipline without that confusion.

    Safe to run against a large, real production table: every write goes through
    maybe_derive_price_targets, which only ever fills a NULL field and never overwrites a
    hand-set one (or an earlier auto-fill), so a second run - or a first run against rows
    already fixed by an incidental edit - is a no-op for those rows regardless of whether
    the sweep itself has already been marked done. The marker row exists purely to avoid
    re-scanning every product on every startup, not to guarantee correctness; correctness
    comes from maybe_derive_price_targets' own null-only-fill rule either way.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS python_backfills ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, rows_updated INTEGER NOT NULL)"
    )
    already = conn.execute(
        "SELECT 1 FROM python_backfills WHERE name = ?", (PRICE_TARGET_BACKFILL_NAME,)
    ).fetchone()
    if already is not None:
        return 0
    ids = [r["id"] for r in conn.execute("SELECT id FROM products")]
    updated = sum(1 for product_id in ids if maybe_derive_price_targets(conn, product_id))
    conn.execute(
        "INSERT INTO python_backfills (name, applied_at, rows_updated) VALUES (?, ?, ?)",
        (PRICE_TARGET_BACKFILL_NAME, utcnow(), updated),
    )
    return updated


def price_history(
    conn: sqlite3.Connection, product_id: int, limit: int = 500
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT h.*, r.name AS retailer_name, l.url AS listing_url FROM price_history h"
        " LEFT JOIN retailers r ON r.id = h.retailer_id"
        " LEFT JOIN listings l ON l.id = h.listing_id"
        " WHERE h.product_id = ? ORDER BY h.observed_at DESC, h.id DESC LIMIT ?",
        (product_id, limit),
    ).fetchall()
    result = [dict(r) for r in rows]
    for row in result:
        reason = row.get("price_change_reason") or "UNKNOWN"
        row["price_change_label"] = PRICE_CHANGE_LABELS.get(reason, PRICE_CHANGE_LABELS["UNKNOWN"])
    return result


# --------------------------------------------------------------------------- categories


def category_fields(conn: sqlite3.Connection, category: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM category_specifications WHERE category = ? ORDER BY sort, label",
        (category,),
    ).fetchall()
    return [dict(r) for r in rows]


def categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM category_specifications ORDER BY category"
    ).fetchall()
    known = [r["category"] for r in rows]
    used = [
        r["category"]
        for r in conn.execute("SELECT DISTINCT category FROM products ORDER BY category")
    ]
    return sorted(set(known) | set(used))
