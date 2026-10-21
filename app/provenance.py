"""Per-field provenance and manual overrides.

Every write to a listing field goes through this module. That is the whole point: a
manual value must survive an automated refresh, and the only way to guarantee that is
to have one write path that knows about locks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .db import utcnow

LIVE = "LIVE"
MANUAL = "MANUAL"
UNVERIFIED = "UNVERIFIED"
STALE = "STALE"
IMPORTED = "IMPORTED"

STATES = [LIVE, MANUAL, UNVERIFIED, STALE, IMPORTED]

# Listing columns that carry provenance and can be edited inline from the matrix.
TRACKED_FIELDS = [
    "advertised_price",
    "freight",
    "cashback",
    "rebate",
    "coupon_discount",
    "stock_status",
    "pickup_status",
    "pickup_location",
    "warranty",
    "condition",
    "included_components",
    "model_on_page",
    "price_guide",
    "seller_notes",
    "url",
    "price_valid_until",
    "conditional_requirements",
    "eligibility_confirmed",
]

NUMERIC_FIELDS = {"advertised_price", "freight", "cashback", "rebate", "coupon_discount"}

#: Fields holding a bare calendar date (YYYY-MM-DD), validated but not reformatted -
#: the retailer statement they come from ("ends Monday 21/09") is a day, not a time.
DATE_FIELDS = {"price_valid_until"}
BOOLEAN_FIELDS = {"eligibility_confirmed"}

VERIFICATION_ASPECTS = [
    "model",
    "price",
    "stock",
    "freight",
    "condition",
    "warranty",
    "contents",
]

# Which listing field, if any, backs each verification aspect.
ASPECT_FIELD = {
    "model": "model_on_page",
    "price": "advertised_price",
    "stock": "stock_status",
    "freight": "freight",
    "condition": "condition",
    "warranty": "warranty",
    "contents": "included_components",
}

VERIFIED = "VERIFIED"
FLAGGED = "FLAGGED"


class FieldError(ValueError):
    """Raised when a caller tries to write a field that does not carry provenance."""


def coerce(field: str, value: Any) -> Any:
    """Normalise an inbound value for a tracked field. Empty string means 'clear it'."""
    if field not in TRACKED_FIELDS:
        raise FieldError(f"{field!r} is not an editable listing field")
    if field in BOOLEAN_FIELDS:
        if value in (True, 1, "1", "true", "TRUE", "yes", "YES"):
            return 1
        if value in (None, False, 0, "0", "false", "FALSE", "no", "NO"):
            return 0
        raise FieldError(f"{field} must be true or false")
    if value is None:
        return None
    if field in NUMERIC_FIELDS:
        if isinstance(value, str):
            cleaned = value.strip().replace("$", "").replace(",", "").replace(" ", "")
            if cleaned == "":
                return None
            value = cleaned
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FieldError(f"{field} must be a number, got {value!r}") from exc
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        if field == "condition":
            value = value.upper().replace(" ", "_").replace("-", "_")
        if field in DATE_FIELDS:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise FieldError(
                    f"{field} must be a date in YYYY-MM-DD form, got {value!r}"
                ) from exc
    return value


def provenance_map(conn: sqlite3.Connection, listing_id: int) -> dict[str, dict[str, Any]]:
    """Current provenance for every tracked field of a listing, defaults filled in."""
    rows = conn.execute(
        "SELECT field, state, source, manual_locked, note, updated_at"
        " FROM field_provenance WHERE listing_id = ?",
        (listing_id,),
    ).fetchall()
    found = {r["field"]: dict(r) for r in rows}
    result: dict[str, dict[str, Any]] = {}
    for field in TRACKED_FIELDS:
        result[field] = found.get(
            field,
            {
                "field": field,
                "state": UNVERIFIED,
                "source": None,
                "manual_locked": 0,
                "note": None,
                "updated_at": None,
            },
        )
    return result


def locked_fields(conn: sqlite3.Connection, listing_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT field FROM field_provenance WHERE listing_id = ? AND manual_locked = 1",
        (listing_id,),
    ).fetchall()
    return {r["field"] for r in rows}


def _write_provenance(
    conn: sqlite3.Connection,
    listing_id: int,
    field: str,
    state: str,
    source: str | None,
    manual_locked: int,
    note: str | None,
) -> None:
    conn.execute(
        "INSERT INTO field_provenance"
        " (listing_id, field, state, source, manual_locked, note, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(listing_id, field) DO UPDATE SET"
        "   state = excluded.state, source = excluded.source,"
        "   manual_locked = excluded.manual_locked, note = excluded.note,"
        "   updated_at = excluded.updated_at",
        (listing_id, field, state, source, manual_locked, note, utcnow()),
    )


def set_field(
    conn: sqlite3.Connection,
    listing_id: int,
    field: str,
    value: Any,
    *,
    state: str,
    source: str | None = None,
    note: str | None = None,
    lock: bool | None = None,
) -> bool:
    """Write one field plus its provenance.

    Returns True if the value was written, False if a manual lock blocked it.
    `lock=None` means: manual edits lock, automated writes leave the lock as-is.
    An automated write (any state other than MANUAL) is refused on a locked field.
    """
    if field not in TRACKED_FIELDS:
        raise FieldError(f"{field!r} is not an editable listing field")

    existing = conn.execute(
        "SELECT manual_locked FROM field_provenance WHERE listing_id = ? AND field = ?",
        (listing_id, field),
    ).fetchone()
    currently_locked = bool(existing["manual_locked"]) if existing else False

    if currently_locked and state != MANUAL and lock is not True:
        return False

    if lock is None:
        should_lock = 1 if state == MANUAL else int(currently_locked)
    else:
        should_lock = int(lock)

    value = coerce(field, value)
    conn.execute(
        f"UPDATE listings SET {field} = ?, updated_at = ? WHERE id = ?",
        (value, utcnow(), listing_id),
    )
    _write_provenance(conn, listing_id, field, state, source, should_lock, note)
    return True


def apply_values(
    conn: sqlite3.Connection,
    listing_id: int,
    values: Mapping[str, Any],
    *,
    state: str,
    source: str | None = None,
    note: str | None = None,
    lock: bool | None = None,
) -> dict[str, list[str]]:
    """Apply several fields at once. Reports which were written and which locks blocked."""
    written: list[str] = []
    blocked: list[str] = []
    for field, value in values.items():
        if field not in TRACKED_FIELDS:
            continue
        if set_field(
            conn, listing_id, field, value, state=state, source=source, note=note, lock=lock
        ):
            written.append(field)
        else:
            blocked.append(field)
    return {"written": written, "blocked": blocked}


def clear_override(conn: sqlite3.Connection, listing_id: int, field: str) -> None:
    """Return a field to automated updates. The value stays; only the lock is released.

    The state drops to UNVERIFIED rather than staying MANUAL, because nothing has
    confirmed the value since the human did and the next watch run now owns it.
    """
    if field not in TRACKED_FIELDS:
        raise FieldError(f"{field!r} is not an editable listing field")
    conn.execute(
        "UPDATE field_provenance SET manual_locked = 0, state = ?, updated_at = ?"
        " WHERE listing_id = ? AND field = ?",
        (UNVERIFIED, utcnow(), listing_id, field),
    )


def mark_stale(conn: sqlite3.Connection, listing_id: int, stale_after_days: int) -> list[str]:
    """Downgrade LIVE/IMPORTED fields that have not refreshed inside the window.

    Manual fields are never marked stale: a human asserted them deliberately and a clock
    does not un-assert that. Returns the fields downgraded.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=stale_after_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = conn.execute(
        "SELECT field FROM field_provenance"
        " WHERE listing_id = ? AND manual_locked = 0 AND state IN (?, ?)"
        "   AND (updated_at IS NULL OR updated_at < ?)",
        (listing_id, LIVE, IMPORTED, cutoff),
    ).fetchall()
    fields = [r["field"] for r in rows]
    for field in fields:
        conn.execute(
            "UPDATE field_provenance SET state = ?, updated_at = updated_at"
            " WHERE listing_id = ? AND field = ?",
            (STALE, listing_id, field),
        )
    return fields


def verification_map(conn: sqlite3.Connection, listing_id: int) -> dict[str, dict[str, Any]]:
    """Verification status per aspect, defaulting to UNVERIFIED."""
    rows = conn.execute(
        "SELECT aspect, status, note, updated_at FROM listing_verification WHERE listing_id = ?",
        (listing_id,),
    ).fetchall()
    found = {r["aspect"]: dict(r) for r in rows}
    return {
        aspect: found.get(
            aspect,
            {"aspect": aspect, "status": UNVERIFIED, "note": None, "updated_at": None},
        )
        for aspect in VERIFICATION_ASPECTS
    }


def set_verification(
    conn: sqlite3.Connection,
    listing_id: int,
    aspect: str,
    status: str,
    note: str | None = None,
) -> None:
    if aspect not in VERIFICATION_ASPECTS:
        raise FieldError(f"{aspect!r} is not a verification aspect")
    conn.execute(
        "INSERT INTO listing_verification (listing_id, aspect, status, note, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(listing_id, aspect) DO UPDATE SET"
        "   status = excluded.status, note = excluded.note, updated_at = excluded.updated_at",
        (listing_id, aspect, status, note, utcnow()),
    )


def sync_verification_from_provenance(
    conn: sqlite3.Connection, listing_id: int, aspects: Iterable[str] | None = None
) -> None:
    """Reflect field provenance onto the aspects it backs.

    A field a human has confirmed reads MANUAL; a freshly scraped one reads LIVE. An
    aspect already marked VERIFIED or FLAGGED by hand is left alone: those are human
    judgements about truth, not statements about where a value came from.
    """
    prov = provenance_map(conn, listing_id)
    current = verification_map(conn, listing_id)
    for aspect in aspects or VERIFICATION_ASPECTS:
        field = ASPECT_FIELD.get(aspect)
        if not field:
            continue
        if current[aspect]["status"] in {VERIFIED, FLAGGED}:
            continue
        set_verification(conn, listing_id, aspect, prov[field]["state"], current[aspect]["note"])
