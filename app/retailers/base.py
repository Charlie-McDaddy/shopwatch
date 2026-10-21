"""Retailer adapter framework.

One adapter per retailer. An adapter's only job is to turn a product page into a
normalised `Observation`. It must never guess: a value it could not determine stays
None, which the watcher records as unresolved. A None freight means "unknown", it does
not mean free shipping.

Adapters do not bypass CAPTCHAs or anti-bot measures. If a retailer blocks automated
requests, the adapter reports that and the value is maintained manually instead.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import load_config
from ..urls import resolve_public_http_url

try:  # pragma: no cover - exercised implicitly by the live watcher
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:  # pragma: no cover
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]


class FetchError(RuntimeError):
    """The page could not be retrieved. The previous stored value must survive this."""


@dataclass
class Observation:
    """A normalised reading of one retailer page. None means unresolved, never zero."""

    model_on_page: str | None = None
    #: Which schema.org key supplied model_on_page. A merchant stock number and a
    #: manufacturer part number are both strings; only the key says which you have.
    model_on_page_key: str | None = None
    advertised_price: float | None = None
    freight: float | None = None
    cashback: float | None = None
    stock_status: str | None = None
    pickup_status: str | None = None
    pickup_location: str | None = None
    warranty: str | None = None
    condition: str | None = None
    included_components: str | None = None
    price_guide: str | None = None
    notes: str | None = None
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    VALUE_FIELDS = (
        "model_on_page", "advertised_price", "freight", "cashback", "stock_status",
        "pickup_status", "pickup_location", "warranty", "condition",
        "included_components", "price_guide",
    )

    def to_values(self) -> dict[str, Any]:
        """Only the fields actually determined. Unresolved fields are simply absent."""
        return {
            name: getattr(self, name)
            for name in self.VALUE_FIELDS
            if getattr(self, name) is not None
        }

    def mark_unresolved(self, *fields: str) -> None:
        for name in fields:
            if name not in self.unresolved:
                self.unresolved.append(name)


# Comma-grouped form first, but only when a group is actually present, otherwise
# "1699.00" would match as "169".
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def parse_price(text: Any) -> float | None:
    """Pull a price out of messy page text. Returns None rather than a wrong number."""
    if text is None:
        return None
    if isinstance(text, int | float):
        return float(text)
    cleaned = str(text).replace("\xa0", " ").strip()
    if not cleaned:
        return None
    match = PRICE_RE.search(cleaned.replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def model_matches(expected: str | None, found: str | None) -> bool:
    """Punctuation-insensitive comparison, tolerant of a dropped regional suffix.

    This used to accept a substring in either direction, which let a genuinely
    different product through: "HW-Q930" matched "HW-Q930H/XY", and so did "XY",
    "930" and "Q9". Two characters that happen to appear in the expected model were
    enough. With merchant stock numbers now excluded from comparison entirely, this
    rule is the whole of the remaining check, so it has to actually discriminate.

    Two things genuinely vary. A page may wrap the model in a longer title
    ("Samsung HW-Q930H/XY Soundbar"), and it may drop the regional suffix after the
    slash ("HW-Q930H"). Both are the same product.

    The containment is deliberately one-directional. The expected model appearing
    inside a longer page string is a title; a SHORTER page string appearing inside
    the expected model is a truncation, and "HW-Q930" is a different soundbar from
    "HW-Q930H". Accepting both directions is what let "XY" and "930" through.
    """
    if not expected or not found:
        return False
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())  # noqa: E731
    forms = lambda s: {norm(s), norm(s.split("/")[0])}  # noqa: E731
    if forms(expected) & forms(found):
        return True
    whole = norm(expected)
    return bool(whole) and whole in norm(found)


#: schema.org keys that name the manufacturer's product rather than the shop's
#: own stock numbering. Only these are worth comparing against an expected model.
MANUFACTURER_KEYS = ("mpn", "model")


def json_ld_blocks(html: str) -> list[dict[str, Any]]:
    """Every schema.org JSON-LD object on the page, flattened out of @graph wrappers."""
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            blocks.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                blocks.extend(g for g in graph if isinstance(g, dict))
    return blocks


AVAILABILITY = {
    "instock": "In Stock",
    "in_stock": "In Stock",
    "outofstock": "Out of Stock",
    "out_of_stock": "Out of Stock",
    "preorder": "Pre-order",
    "backorder": "Backorder",
    "limitedavailability": "Limited",
    "discontinued": "Discontinued",
    "soldout": "Sold Out",
}


def _read_product_block(block: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we care about out of one schema.org Product block."""
    found: dict[str, Any] = {"identifier": None, "key": None, "price": None, "stock": None}

    # Ordered so a manufacturer identifier wins when the page publishes both.
    # schema.org already draws the line this code needs: mpn and model name the
    # manufacturer's product, sku and productID are the merchant's own numbering.
    for key in ("mpn", "model", "productID", "sku"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            found["identifier"] = value.strip()
            found["key"] = key
            break

    offers = block.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        price = parse_price(offer.get("price") or offer.get("lowPrice"))
        if price is not None and found["price"] is None:
            found["price"] = price
        availability = str(offer.get("availability") or "").split("/")[-1].lower()
        if availability and found["stock"] is None:
            found["stock"] = AVAILABILITY.get(availability, availability)
    return found


def observation_from_json_ld(html: str, expected_model: str | None = None) -> Observation:
    """Generic schema.org Product/Offer reader, which most AU retailers still publish.

    Picks a block rather than taking the first one and stopping. Retailers commonly
    emit a recommendation or carousel Product before the page's own, and the old loop
    broke after the first Product whether or not it had yielded anything: a leading
    stub with no offers left the price unresolved forever while the real block below
    was never examined, and a leading carousel entry had ITS price written to the
    listing. With merchant stock numbers no longer compared, that could happen without
    raising a mismatch, so the wrong product's price would land silently.

    Preference order: a block whose manufacturer identifier matches what we expect,
    then the first block that actually carries a price, then the first Product at all
    so its identifier is still recorded.
    """
    obs = Observation()
    candidates = []
    for block in json_ld_blocks(html):
        types = block.get("@type")
        types = types if isinstance(types, list) else [types]
        if not any(str(t).lower() == "product" for t in types if t):
            continue
        candidates.append(_read_product_block(block))

    chosen = None
    if expected_model:
        chosen = next(
            (c for c in candidates
             if c["key"] in MANUFACTURER_KEYS and model_matches(expected_model, c["identifier"])),
            None,
        )
    if chosen is None:
        chosen = next((c for c in candidates if c["price"] is not None), None)
    if chosen is None and candidates:
        chosen = candidates[0]

    if chosen:
        obs.model_on_page = chosen["identifier"]
        obs.model_on_page_key = chosen["key"]
        obs.advertised_price = chosen["price"]
        obs.stock_status = chosen["stock"]

    if obs.advertised_price is None:
        obs.mark_unresolved("advertised_price")

    # Only compare an identifier that claims to be the manufacturer's. A merchant
    # stock number is not a wrong model, it is a different kind of number, and
    # comparing it produced a permanent red fault tag on two correct listings.
    #
    # Deciding this by the schema.org key rather than by the shape of the string
    # matters in both directions: a retailer publishing an alphanumeric stock number
    # would still have raised a false mismatch under a digits-only test, and skipping
    # every numeric identifier disabled the check outright for the two retailers it
    # was meant to help.
    mismatch = (
        expected_model
        and obs.model_on_page
        and obs.model_on_page_key in MANUFACTURER_KEYS
        and not model_matches(expected_model, obs.model_on_page)
    )
    if mismatch:
        obs.warnings.append(
            f"model mismatch: page shows {obs.model_on_page!r}, expected {expected_model!r}"
        )
    return obs


#: Markers of a bot-protection interstitial. These pages return HTTP 200, so without
#: this check a block is indistinguishable from a product page that has no price, and
#: the run reports "unresolved" for a retailer that never answered at all.
BLOCK_MARKERS = (
    "pardon our interruption",          # Imperva / Incapsula
    "_incapsula_resource",
    "request unsuccessful. incapsula",
    "attention required! | cloudflare",  # Cloudflare
    "cf-browser-verification",
    "checking your browser before accessing",
    "just a moment...",
    "px-captcha",                        # PerimeterX
    "/_sec/cp_challenge/",               # Akamai
    "enable javascript and cookies to continue",
)


def detect_block(html: str) -> str | None:
    """Return the marker that identifies an interstitial, or None for a real page.

    Deliberately narrow. A false positive here hides a genuine price, so only markers
    that belong to a challenge page's own chrome are listed.
    """
    lowered = html[:20000].lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return marker
    return None


class RetailerAdapter:
    """Base adapter. Subclasses normally only override `parse`."""

    slug = "base"
    name = "Base"
    homepage = ""
    #: Fields this retailer structurally cannot expose to a scraper (freight behind a
    #: postcode, stock behind a store picker). Documented, not guessed at.
    never_scrapable: tuple[str, ...] = ()

    def __init__(self, config=None) -> None:
        self.config = config or load_config()

    def fetch(self, url: str) -> str:
        if requests is None:  # pragma: no cover
            raise FetchError("requests is not installed")
        if not self.config.scraping_enabled:
            raise FetchError("scraping disabled by configuration")
        current_url = url
        for redirects in range(6):
            if not resolve_public_http_url(current_url):
                raise FetchError("unsafe URL or redirect target")
            time.sleep(max(self.config.request_delay, 0))
            try:
                response = requests.get(
                    current_url,
                    timeout=self.config.request_timeout,
                    allow_redirects=False,
                    headers={
                        "User-Agent": self.config.user_agent,
                        "Accept-Language": "en-AU,en;q=0.9",
                    },
                )
            except Exception as exc:  # network errors of every flavour
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirects == 5:
                    raise FetchError("too many or invalid redirects")
                from urllib.parse import urljoin
                current_url = urljoin(current_url, location)
                if not resolve_public_http_url(current_url):
                    raise FetchError("unsafe URL or redirect target")
                continue
            if response.status_code == 403:
                raise FetchError("403: retailer blocked the request (bot protection)")
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}")
            marker = detect_block(response.text)
            if marker:
                raise FetchError(f"bot-protection interstitial (matched {marker!r})")
            return response.text
        raise FetchError("too many redirects")

    def parse(self, html: str, expected_model: str | None = None) -> Observation:
        return observation_from_json_ld(html, expected_model)

    def check(self, url: str, expected_model: str | None = None) -> Observation:
        """Fetch and parse. Raises FetchError; never returns invented values."""
        obs = self.parse(self.fetch(url), expected_model)
        obs.mark_unresolved(*self.never_scrapable)
        return obs


_REGISTRY: dict[str, type[RetailerAdapter]] = {}


def register(cls: type[RetailerAdapter]) -> type[RetailerAdapter]:
    _REGISTRY[cls.slug] = cls
    return cls


def get_adapter(slug: str | None) -> RetailerAdapter | None:
    if not slug:
        return None
    cls = _REGISTRY.get(slug)
    return cls() if cls else None


def available_adapters() -> dict[str, type[RetailerAdapter]]:
    return dict(_REGISTRY)
