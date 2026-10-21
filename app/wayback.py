"""Small, bounded Wayback CDX lookup for existing product listing URLs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from . import store
from .urls import public_http_url as valid_public_http_url
from .urls import resolve_public_http_url

CDX_URL = "https://web.archive.org/cdx/search/cdx"
ARCHIVE_HOST = "web.archive.org"
MAX_LISTINGS = 8
MAX_SNAPSHOTS_PER_LISTING = 4
MAX_RESPONSE_BYTES = 750_000
MAX_TOTAL_REQUESTS = 16
MAX_TOTAL_SECONDS = 30.0
MAX_SINGLE_TIMEOUT_SECONDS = 4.0


@dataclass
class RequestBudget:
    """Shared hard cap for one GUI check, across all listing URLs and captures."""

    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_TOTAL_SECONDS)
    remaining_requests: int = MAX_TOTAL_REQUESTS

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.remaining_requests <= 0 or remaining <= 0:
            raise TimeoutError("Wayback request budget was exhausted")
        self.remaining_requests -= 1
        # Requests applies the scalar to connection and read waits; half the time left
        # bounds both phases together, with the iterator checking the same deadline.
        return max(0.1, min(MAX_SINGLE_TIMEOUT_SECONDS, remaining / 2))

    def check_time(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Wayback request budget was exhausted")


def public_http_url(url: str) -> bool:
    """Use the shared lexical and DNS guard before sending a listing URL to Wayback."""
    return resolve_public_http_url(url)


def _bounded_get(url: str, budget: RequestBudget, *, params: dict | None = None) -> bytes:
    parsed = urlparse(url)
    is_cdx = url == CDX_URL
    is_snapshot = (
        parsed.scheme == "https"
        and parsed.hostname == ARCHIVE_HOST
        and parsed.port is None
        and re.match(r"^/web/\d{14}id_/https?://", parsed.path)
    )
    timeout = budget.timeout()
    if not (is_cdx or is_snapshot) or not public_http_url(url):
        raise ValueError("archive URL is not an approved public Wayback endpoint")
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        headers={"User-Agent": "Shopwatch/1.0 (Wayback price evidence)"},
    )
    try:
        if response.status_code != 200:
            raise ValueError("archive response was unavailable")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError("archive response exceeded the size limit")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(16_384):
            budget.check_time()
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("archive response exceeded the size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def _captured_rows(original_url: str, budget: RequestBudget) -> list[tuple[str, str]]:
    payload = _bounded_get(
        CDX_URL,
        budget,
        params={
            "url": original_url,
            "output": "json",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "fl": "timestamp,original,statuscode,mimetype",
            "collapse": "timestamp:4",
            "limit": f"-{MAX_SNAPSHOTS_PER_LISTING}",
        },
    )
    rows = json.loads(payload)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return []
    header = rows[0]
    try:
        timestamp_i = header.index("timestamp")
        original_i = header.index("original")
        status_i = header.index("statuscode")
        mimetype_i = header.index("mimetype")
    except ValueError:
        return []
    captures = []
    for row in rows[1 : MAX_SNAPSHOTS_PER_LISTING + 1]:
        if not isinstance(row, list) or len(row) <= max(
            timestamp_i, original_i, status_i, mimetype_i
        ):
            continue
        timestamp, matched_url = row[timestamp_i], row[original_i]
        if row[status_i] != "200" or row[mimetype_i] != "text/html":
            continue
        try:
            datetime.strptime(str(timestamp), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if matched_url == original_url:
            captures.append((str(timestamp), str(matched_url)))
    return captures


def _model_price_from_html(html: bytes, expected_model: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")
    expected = store.normalise_model(expected_model)
    if not expected:
        return None

    def products(value):
        if isinstance(value, list):
            for item in value:
                yield from products(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(item).lower() == "product" for item in kinds):
                yield value
            graph = value.get("@graph")
            if graph:
                yield from products(graph)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in products(data):
            identifiers = [item.get(key) for key in ("model", "mpn")]
            if not any(
                isinstance(value, str) and store.normalise_model(value) == expected
                for value in identifiers
            ):
                continue
            offers = item.get("offers", [])
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if (
                    not isinstance(offer, dict)
                    or str(offer.get("priceCurrency", "")).upper() != "AUD"
                ):
                    continue
                try:
                    if isinstance(offer.get("price"), bool):
                        continue
                    price = float(offer.get("price"))
                except (TypeError, ValueError):
                    continue
                if 0 < price < 10_000_000:
                    return round(price, 2)

    # OpenGraph metadata is accepted only with an explicit AUD currency, and the exact
    # variant must occur on the captured page. Bare dollar signs are ambiguous.
    currency = soup.find("meta", attrs={"property": "product:price:currency"})
    amount = soup.find("meta", attrs={"property": "product:price:amount"})
    page_text = soup.get_text(" ", strip=True)
    if currency and amount and currency.get("content", "").upper() == "AUD":
        tokens = page_text.split()
        exact_variant_on_page = any(
            store.normalise_model("".join(tokens[start : start + width])) == expected
            for start in range(len(tokens))
            for width in range(1, 4)
            if start + width <= len(tokens)
        )
        if exact_variant_on_page:
            try:
                price = float(amount.get("content", ""))
                if 0 < price < 10_000_000:
                    return round(price, 2)
            except ValueError:
                pass
    return None


def lookup_listing(
    original_url: str, expected_model: str, budget: RequestBudget | None = None
) -> dict:
    """Return only exact-URL, exact-model AUD advertised price evidence."""
    budget = budget or RequestBudget()
    if not public_http_url(original_url):
        return {
            "status": "blocked",
            "note": "Listing URL is not a validated public HTTP(S) reference.",
            "snapshots": [],
        }
    try:
        captures = _captured_rows(original_url, budget)
    except TimeoutError:
        return {
            "status": "blocked",
            "note": "Wayback request/time budget expired; archive history may be incomplete.",
            "snapshots": [],
        }
    except (requests.RequestException, ValueError, OSError, json.JSONDecodeError):
        return {
            "status": "blocked",
            "note": "Wayback archive history could not be read safely.",
            "snapshots": [],
        }
    if not captures:
        return {
            "status": "no_snapshot",
            "note": "Wayback has no matching public page snapshots.",
            "snapshots": [],
        }

    snapshots = []
    saw_page = False
    budget_exhausted = False
    for timestamp, matched_url in captures:
        snapshot_url = (
            f"https://{ARCHIVE_HOST}/web/{timestamp}id_/{quote(matched_url, safe=':/?=&%')}"
        )
        try:
            html = _bounded_get(snapshot_url, budget)
            saw_page = True
            price = _model_price_from_html(html, expected_model)
        except TimeoutError:
            budget_exhausted = True
            break
        except (requests.RequestException, ValueError, OSError, json.JSONDecodeError):
            continue
        if price is None:
            continue
        captured = datetime.strptime(timestamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshots.append(
            {
                "snapshot_url": snapshot_url,
                "captured_at": captured,
                "model": expected_model,
                "advertised_price": price,
                "currency": "AUD",
                "price_context": (
                    "Archived advertised price; freight and discount eligibility unknown."
                ),
            }
        )
    if snapshots:
        return {
            "status": "evidence",
            "note": (
                "Some captures were skipped at the Wayback request/time budget. "
                "Archived advertised prices; freight and discounts are not confirmed."
                if budget_exhausted
                else "Archived advertised prices; freight and discounts are not confirmed."
            ),
            "snapshots": snapshots,
        }
    if not saw_page:
        return {
            "status": "blocked",
            "note": (
                "Wayback request/time budget expired; archive history may be incomplete."
                if budget_exhausted
                else "Wayback snapshots were unavailable or blocked."
            ),
            "snapshots": [],
        }
    if budget_exhausted:
        return {
            "status": "blocked",
            "note": "Wayback request/time budget expired; archive history may be incomplete.",
            "snapshots": [],
        }
    return {
        "status": "no_usable_price",
        "note": "Snapshots exist, but none confirms this exact model and an AUD price.",
        "snapshots": [],
    }


def valid_archive_snapshot(original_url: str, expected_model: str, item: dict) -> dict | None:
    """Return a normalized capture only when its URL, date, variant and AUD amount agree."""
    if not isinstance(item, dict) or not valid_public_http_url(original_url):
        return None
    snapshot_url = item.get("snapshot_url")
    try:
        parsed = urlparse(snapshot_url)
        if isinstance(item.get("advertised_price"), bool):
            return None
        price = float(item.get("advertised_price"))
        captured = datetime.strptime(item.get("captured_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    timestamp = captured.strftime("%Y%m%d%H%M%S")
    expected_snapshot = (
        f"https://{ARCHIVE_HOST}/web/{timestamp}id_/"
        f"{quote(original_url, safe=':/?=&%')}"
    )
    if (
        snapshot_url != expected_snapshot
        or parsed.scheme != "https"
        or parsed.hostname != ARCHIVE_HOST
        or parsed.port
        or parsed.username
        or parsed.password
    ):
        return None
    if item.get("currency") != "AUD" or not (0 < price < 10_000_000):
        return None
    matched_model = item.get("model")
    if not isinstance(matched_model, str) or store.normalise_model(
        matched_model
    ) != store.normalise_model(expected_model):
        return None
    return {
        "snapshot_url": snapshot_url,
        "captured_at": captured.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": matched_model,
        "advertised_price": round(price, 2),
        "currency": "AUD",
        "price_context": str(
            item.get("price_context") or "Archived advertised price; freight unknown."
        )[:300],
    }


def persist_lookup(
    conn,
    product_id: int,
    listing_id: int | None,
    original_url: str,
    expected_model: str,
    result: dict,
    checked_at: str,
) -> int:
    """Persist a coverage result and only independently validated evidence fields."""
    status = result.get("status") if isinstance(result, dict) else None
    note = result.get("note") if isinstance(result, dict) else None
    status = (
        status if status in {"evidence", "no_snapshot", "no_usable_price", "blocked"} else "blocked"
    )
    note = str(note or "Wayback returned no usable evidence.")[:500]
    raw_snapshots = result.get("snapshots", []) if isinstance(result, dict) else []
    good = []
    if isinstance(raw_snapshots, list):
        for item in raw_snapshots[:MAX_SNAPSHOTS_PER_LISTING]:
            clean = valid_archive_snapshot(original_url, expected_model, item)
            if clean:
                good.append(clean)

    if good:
        status = "evidence"
    elif status == "evidence":
        status = "no_usable_price"
        note = "Archive response contained no safely validated exact-model AUD price."
    cursor = conn.execute(
        "INSERT INTO wayback_checks (product_id, listing_id, original_url, checked_at, status,"
        " note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, listing_id, original_url, checked_at, status, note),
    )
    check_id = int(cursor.lastrowid)
    for item in good:
        conn.execute(
            "INSERT INTO wayback_price_evidence"
            " (check_id, product_id, listing_id, original_url, snapshot_url, captured_at,"
            " matched_model, advertised_price, currency, price_context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AUD', ?)",
            (
                check_id,
                product_id,
                listing_id,
                original_url,
                item["snapshot_url"],
                item["captured_at"],
                item["model"],
                item["advertised_price"],
                item["price_context"],
            ),
        )
    return check_id


def history(conn, product_id: int) -> tuple[list[dict], list[dict]]:
    evidence = [
        dict(row)
        for row in conn.execute(
            "SELECT e.*, r.name AS retailer_name FROM wayback_price_evidence e"
            " LEFT JOIN listings l ON l.id = e.listing_id"
            " LEFT JOIN retailers r ON r.id = l.retailer_id"
            " WHERE e.product_id = ? ORDER BY e.captured_at DESC, e.id DESC",
            (product_id,),
        )
    ]
    rows = conn.execute(
        "SELECT c.*, r.name AS retailer_name FROM wayback_checks c"
        " LEFT JOIN listings l ON l.id = c.listing_id"
        " LEFT JOIN retailers r ON r.id = l.retailer_id"
        " WHERE c.product_id = ? ORDER BY c.id DESC",
        (product_id,),
    )
    latest_by_listing = {}
    for row in rows:
        key = row["listing_id"]
        if key not in latest_by_listing:
            latest_by_listing[key] = dict(row)
    return evidence, list(latest_by_listing.values())
