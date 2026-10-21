"""Turn a bare retailer URL into a retailer identity - the "paste a listing URL" path
(issue #94: someone has a product page open, has no interest in typing a retailer name
or picking one from a list, and just wants to hand over the address).

Deliberately does not import anything from deploy/research-runner.py, even though that
module already has near-identical domain-to-display-name logic for its own "discover
retailers" feature. That script runs host-side, outside this container, on its own
venv; importing across that boundary would tie this container's behaviour to a file
that is not even guaranteed to be present at runtime. A dozen lines of duplicated regex
here is cheaper than that coupling.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from . import retailers
from .urls import public_http_url


def bare_domain(url: str) -> str | None:
    """Lowercased host with any leading www. stripped, or None for an unusable url.

    Deliberately strict about scheme: something that merely contains a dot is not
    evidence it is a URL at all, and a scheme this app cannot fetch later (ftp:,
    mailto:, javascript:) must not be accepted just because urlparse tolerates it.
    """
    if not public_http_url(url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    netloc = parsed.netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def adapter_for_domain(domain: str) -> type[retailers.RetailerAdapter] | None:
    """The registered adapter whose declared homepage matches this domain, or None.

    Matching on the adapter's own `homepage` class attribute rather than a separate
    domain table: that attribute is already the single source of truth other code
    trusts (the wizard's retailer list, price_watch), so a retailer added here lines
    up with the same adapter price_watch would pick up on its next scheduled pass.
    """
    for cls in retailers.available_adapters().values():
        home = bare_domain(cls.homepage) if cls.homepage else None
        if home and home == domain:
            return cls
    return None


#: TLD suffixes stripped before turning a domain into a readable name. Not exhaustive -
#: an unrecognised TLD is left in place, which is a worse-looking name than a wrong one.
_TLD_RE = re.compile(r"\.(com\.au|net\.au|org\.au|com|net|org|io|co)$")


def display_name_for_domain(domain: str) -> str:
    """A readable starting-point retailer name derived purely from the domain, e.g.
    "centrecom.com.au" -> "Centrecom". Approximate by construction: a domain rarely
    spells a brand's actual styling ("jbhifi.com.au" vs "JB Hi-Fi"). That is an
    acceptable trade-off here, not a defect to fix, because store.ensure_retailer
    matches on name case-insensitively and the wizard already lets a mis-cased or
    mis-spaced retailer be renamed later - this only has to be a reasonable label to
    show while the listing itself lands, not the final word on how the retailer is
    styled everywhere.
    """
    base = _TLD_RE.sub("", domain)
    parts = re.split(r"[.\-_]+", base)
    return " ".join(p.capitalize() for p in parts if p) or domain


def is_marketplace_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return (host == "amazon.com.au" or host.endswith(".amazon.com.au")
            or host == "temu.com" or host.endswith(".temu.com"))


def marketplace_identity(url: str, payload: dict) -> dict[str, str | None]:
    """Extract explicit or URL-published seller and offer identifiers."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    amazon = host == "amazon.com.au" or host.endswith(".amazon.com.au")
    temu = host == "temu.com" or host.endswith(".temu.com")
    seller_keys = (("seller_id", "sellerId", "merchantId", "seller", "merchant", "m")
                   if amazon else ("seller_id", "sellerId", "merchantId"))
    offer_keys = ("offer_id", "offerId", "offerListingID", "listingId")

    def first(names: tuple[str, ...]) -> str | None:
        for name in names:
            value = payload.get(name)
            if value is None and (amazon or temu):
                value = next((items[0] for key, items in query.items()
                              if key.lower() == name.lower() and items), None)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        return None

    seller_id = first(seller_keys)
    offer_id = first(offer_keys)
    seller_name = payload.get("seller_name")
    if seller_name is not None and not isinstance(seller_name, str):
        raise ValueError("seller_name must be text")
    seller_name = seller_name.strip()[:200] if seller_name and seller_name.strip() else None
    if not (amazon or temu):
        seller_name = seller_id = offer_id = None
    identity_parts = [part for part in (
        f"seller:{seller_id}" if seller_id else None,
        f"offer:{offer_id}" if offer_id else None,
    ) if part]
    if not identity_parts and seller_name:
        identity_parts.append(f"name:{seller_name.casefold()}")
    key = "|".join(identity_parts)
    return {"seller_name": seller_name, "seller_id": seller_id,
            "offer_id": offer_id, "offer_key": key}
