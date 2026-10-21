"""Delivered-price arithmetic, threshold classification and ranking.

Delivered price is the primary ranking metric everywhere in this application.
Headline price is informational only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Deal classifications, best to worst.
HISTORICAL_LOW = "HISTORICAL_LOW"
EXCELLENT = "EXCELLENT"
TRIGGER_MET = "TRIGGER_MET"
ABOVE_TARGET = "ABOVE_TARGET"
UNRESOLVED = "UNRESOLVED"

CLASS_ORDER = [HISTORICAL_LOW, EXCELLENT, TRIGGER_MET, ABOVE_TARGET, UNRESOLVED]

CLASS_LABELS = {
    HISTORICAL_LOW: "HISTORICAL LOW TERRITORY",
    EXCELLENT: "EXCELLENT",
    TRIGGER_MET: "TRIGGER MET",
    ABOVE_TARGET: "ABOVE TARGET",
    UNRESOLVED: "UNRESOLVED",
}

def unit_price(
    price: float | None, quantity: float | None, unit: str | None
) -> tuple[float, str] | None:
    """Return AUD per kg, L, or each; unknown and unsupported quantities stay unknown."""
    try:
        amount, cost = float(quantity), float(price)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or cost < 0 or not amount < float("inf") or not cost < float("inf"):
        return None
    factors = {"g": (1000, "kg"), "kg": (1, "kg"), "ml": (1000, "L"),
               "L": (1, "L"), "each": (1, "each")}
    if unit not in factors:
        return None
    factor, label = factors[unit]
    return round(cost * factor / amount, 2), label


CONDITIONS = [
    "NEW",
    "FACTORY_SECOND",
    "CARTON_DAMAGED",
    "EX_DISPLAY",
    "REFURBISHED",
    "USED",
]


@dataclass(frozen=True)
class Delivered:
    """The result of a delivered-price calculation.

    `value` is the best current estimate. `resolved` says whether freight was actually
    confirmed; an unresolved value is provisional and must be presented as such.
    """

    value: float | None
    resolved: bool
    freight_known: bool
    advertised: float | None
    adjustments: float

    @property
    def known(self) -> bool:
        return self.value is not None


def _num(value: Any) -> float | None:
    """Tolerant numeric read. Accepts "$1,059.00" as well as 1059.0, None on anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "").replace(" ", "")
        if value == "":
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def delivered_price(
    advertised: Any,
    freight: Any = None,
    cashback: Any = None,
    rebate: Any = None,
    coupon_discount: Any = None,
) -> Delivered:
    """advertised + freight - cashback - rebate - coupon.

    A NULL freight means UNRESOLVED, never free. The provisional figure omits freight
    but the result is flagged unresolved so callers can rank and label it honestly.
    Negative results are clamped to zero: a cashback larger than the price is a data
    error, not a retailer paying you.
    """
    adv = _num(advertised)
    fre = _num(freight)
    adjustments = (_num(cashback) or 0.0) + (_num(rebate) or 0.0) + (_num(coupon_discount) or 0.0)

    if adv is None:
        return Delivered(None, False, fre is not None, None, adjustments)

    total = adv + (fre or 0.0) - adjustments
    if total < 0:
        total = 0.0
    return Delivered(round(total, 2), fre is not None, fre is not None, adv, adjustments)


def classify(delivered: Delivered | float | None, targets: Mapping[str, Any]) -> str:
    """Classify a delivered price against a product's thresholds.

    Thresholds are inclusive and evaluated best-first. An unknown price, or a price with
    unresolved freight that would otherwise rate at or better than the trigger, returns
    UNRESOLVED: an unconfirmed bargain is not a bargain yet. An unresolved price that is
    above target is still reported as ABOVE_TARGET, because freight can only make it worse.
    """
    if isinstance(delivered, Delivered):
        value, resolved = delivered.value, delivered.resolved
    else:
        value, resolved = _num(delivered), True

    if value is None:
        return UNRESOLVED

    historical = _num(targets.get("historical_low_price"))
    excellent = _num(targets.get("excellent_price"))
    trigger = _num(targets.get("trigger_price"))

    if historical is not None and value <= historical:
        rating = HISTORICAL_LOW
    elif excellent is not None and value <= excellent:
        rating = EXCELLENT
    elif trigger is not None and value <= trigger:
        rating = TRIGGER_MET
    else:
        rating = ABOVE_TARGET

    if not resolved and rating != ABOVE_TARGET:
        return UNRESOLVED
    return rating


def rank_key(delivered: Delivered, penalty: float = 60.0) -> tuple[int, float]:
    """Sort key for the retailer matrix. Lower is better.

    An unresolved-freight listing is ranked as if freight cost `penalty`, so it cannot
    automatically outrank a slightly dearer listing whose delivered price is confirmed.
    The penalty is a ranking device only and is never stored or displayed as a price.
    Listings with no price at all sort last.
    """
    if delivered.value is None:
        return (1, float("inf"))
    if delivered.resolved:
        return (0, delivered.value)
    return (0, delivered.value + penalty)


def difference_from_low(delivered: Delivered | float | None, lowest_known: Any) -> float | None:
    """Delivered price minus the lowest price ever recorded. Positive means dearer than the low."""
    value = delivered.value if isinstance(delivered, Delivered) else _num(delivered)
    low = _num(lowest_known)
    if value is None or low is None:
        return None
    return round(value - low, 2)


def listing_is_comparable(listing: Mapping[str, Any]) -> bool:
    """Whether the listing was not rejected and its model is not flagged."""
    verification = listing.get("verification") or {}
    model = verification.get("model") or {}
    return not listing.get("ruled_out") and model.get("status") != "FLAGGED"


def listing_eligibility(
    listing: Mapping[str, Any], *, require_new: bool = True, require_delivery: bool = True
) -> tuple[bool, list[str]]:
    """Whether this is a confirmed, current, unconditional buying option."""
    verification = listing.get("verification") or {}
    reasons = []
    if not listing_is_comparable(listing):
        reasons.append("model is not confirmed or listing is ruled out")
    if listing.get("model_match") is False:
        reasons.append("listed variant does not match the product")
    for aspect in ("model", "price", "stock"):
        if (verification.get(aspect) or {}).get("status") not in {
            "VERIFIED", "LIVE", "MANUAL",
        }:
            reasons.append(f"{aspect} is unverified")
    if require_new and (listing.get("condition") or "").upper() != "NEW":
        reasons.append("condition is not new")
    stock = (listing.get("stock_status") or "").casefold()
    available = (
        stock.startswith("in stock")
        or stock in {"available", "available now", "available online", "limited"}
        or stock.startswith(("limited stock", "limited availability"))
    ) and not any(
        word in stock
        for word in (
            "out of stock", "unavailable", "sold out", "preorder", "pre-order", "backorder",
        )
    )
    if not available:
        reasons.append("availability is unknown or unavailable")
    if require_delivery and not listing.get("delivered_resolved"):
        reasons.append("delivery total is unconfirmed")
    if not listing.get("freight_current", True):
        reasons.append("delivery quote is for another location")
    requirements = (listing.get("conditional_requirements") or "").strip()
    if requirements and listing.get("eligibility_confirmed"):
        confirmation = (listing.get("provenance") or {}).get("eligibility_confirmed") or {}
        if confirmation.get("state") != "MANUAL" or not confirmation.get("manual_locked"):
            reasons.append("eligibility confirmation was not set by hand")
    elif requirements:
        reasons.append(requirements)
    return not reasons, reasons


def best_listing(rows: list[Mapping[str, Any]], penalty: float = 60.0) -> Mapping[str, Any] | None:
    """The current best new buy among verified, available, eligible delivered offers.

    A ruled-out listing or one with a flagged model is never the answer, however cheap.
    It stays visible on the board and axis so the operator can review it, but it cannot
    be nominated. Provisional prices remain on their rows, but do not establish a best.
    """
    priced = [
        r for r in rows
        if r.get("delivered") and r["delivered"].known and listing_eligibility(r)[0]
    ]
    if not priced:
        return None
    return min(priced, key=lambda r: rank_key(r["delivered"], penalty))


# --------------------------------------------------------------------------- scale


def threshold_scale(
    targets: Mapping[str, Any],
    best: Any = None,
    listings: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Positions (0-100) for plotting targets, every contender, and the current best.

    The three price targets are the whole point of the product, so they get shown as a
    scale rather than as three numbers in a row: "how far off are we" is the question,
    and a distance is easier to see than to calculate. Returns None when there is nothing
    to plot, so the caller can leave the space empty instead of drawing an empty axis.

    `listings` puts every priced contender on the same axis. That widens the domain a
    long way, because the dearest retailer is usually double the cheapest, which is
    exactly why the axis is zoomable in the browser: at full extent the three targets
    sit on top of each other. `lo`/`hi` are returned so the client can re-map a zoomed
    window from each item's raw value rather than from a position it would have to
    un-project first.
    """
    # Two targets landing on the exact same value is no longer rare: issue #101's
    # auto-derived defaults set excellent_price = historical_low_price = lowest_known_price
    # exactly, so a product that has never had its targets hand-tuned routinely has two
    # (or all three) marks at one price. Each mark is plotted as its own absolutely
    # positioned label centered on its axis position - two at the same position render
    # directly on top of each other, illegible. Group by value first so same-position
    # marks share one label ("hist low + excellent") instead of two marks fighting for
    # the same pixels.
    marks_by_value: dict[float, dict[str, Any]] = {}
    for key, label in (
        ("historical_low_price", "hist low"),
        ("excellent_price", "excellent"),
        ("trigger_price", "trigger"),
    ):
        value = _num(targets.get(key))
        if value is None:
            continue
        existing = marks_by_value.get(value)
        if existing is None:
            marks_by_value[value] = {"key": key, "label": label, "value": value}
        else:
            existing["key"] = f"{existing['key']}+{key}"
            existing["label"] = f"{existing['label']} + {label}"
    marks = list(marks_by_value.values())

    best_value = best.value if isinstance(best, Delivered) else _num(best)

    # Every contender that has a price goes on the axis. One without a price is not
    # plotted at all rather than being pinned somewhere arbitrary: a dot on a price
    # axis is a claim about a number, and there is no number.
    points = []
    for listing in listings or ():
        value = _num(listing.get("delivered_price"))
        if value is None:
            continue
        points.append({
            "id": listing.get("id"),
            "retailer": listing.get("retailer_name") or "?",
            "value": value,
            "resolved": bool(listing.get("delivered_resolved")),
            "classification": listing.get("classification"),
            # Still plotted, deliberately. Ruling something out is a decision that can
            # be revisited, and it is easier to revisit one you can see sitting there.
            "ruled_out": not listing_is_comparable(listing),
            # Set by store.product_view() (issue #100), not computed here - this
            # module stays about positions, not presentation. Copied through rather
            # than dropped so the dot and its listing row below share one value.
            "retailer_color": listing.get("retailer_color"),
        })
    points.sort(key=lambda pt: pt["value"])

    # Nothing to plot at all, so the caller leaves the space empty rather than drawing
    # an empty axis. This used to trigger on "no targets", which was right when the
    # axis carried only targets: once it carries every contender, a product with three
    # priced listings and no trigger decided yet has plenty worth comparing.
    if not marks and not points:
        return None

    values = [m["value"] for m in marks] + ([best_value] if best_value is not None else [])
    values += [pt["value"] for pt in points]
    lo, hi = min(values), max(values)
    span = hi - lo
    # A flat span (single threshold, or best exactly on it) would divide by zero and
    # stack every mark on one pixel. Give it an arbitrary but proportionate width.
    pad = span * 0.14 if span else max(hi * 0.06, 1.0)
    lo, hi = lo - pad, hi + pad

    def pos(value: float) -> float:
        return round((value - lo) / (hi - lo) * 100, 2)

    for mark in marks:
        mark["pos"] = pos(mark["value"])
    for point in points:
        point["pos"] = pos(point["value"])

    return {
        "marks": marks,
        "points": points,
        "bands": _bands(targets, pos, lo, hi),
        "best": {"value": best_value, "pos": pos(best_value)} if best_value is not None else None,
        "lo": round(lo, 2),
        "hi": round(hi, 2),
    }


#: What each stretch of the axis means. Only the two that would change a decision are
#: toned; everything above the trigger is the ordinary state and stays grey, which is
#: the same rule the stylesheet follows: colour here means act, never decoration.
#: Ascending. Each band runs from the previous PRESENT threshold to its own, which is
#: not the same as running from a fixed predecessor: every target column is nullable
#: and all three inputs are optional, so naming a predecessor that is NULL made the
#: band fall back to the left edge and paint straight over its neighbour.
BAND_SPEC = (
    ("historical_low_price", "historical low", "act"),
    ("excellent_price", "excellent", "act"),
    ("trigger_price", "on target", "close"),
)


def _bands(
    targets: Mapping[str, Any],
    pos: Any,
    lo: float,
    hi: float,
) -> list[dict[str, Any]]:
    """Contiguous regions of the axis, left to right, each with what it means.

    A band is drawn from the previous threshold (or the left edge) up to its own
    threshold, so the bands tile the axis without gaps. A missing target simply does
    not produce a band rather than collapsing the ones around it.
    """
    bands = []
    previous = None  # the last threshold that actually exists, not the nominal one
    for key, label, tone in BAND_SPEC:
        upper = _num(targets.get(key))
        if upper is None:
            continue
        start = pos(previous) if previous is not None else 0.0
        end = pos(upper)
        previous = upper
        if end <= start:
            continue
        bands.append({
            "key": key, "label": label, "tone": tone,
            "from": round(start, 2), "to": round(end, 2),
            "width": round(end - start, 2),
        })
    if bands:
        last = max(b["to"] for b in bands)
        if last < 100:
            bands.append({
                "key": "above", "label": "above target", "tone": "quiet",
                "from": last, "to": 100.0, "width": round(100 - last, 2),
            })
    return bands


def merge_price_points(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Position a flat list of prices from DIFFERENT products onto one shared 0-100 scale.

    Deliberately carries no marks or bands: unlike threshold_scale, these points come
    from genuinely different products (a watch group's candidates), which do not share
    a trigger/excellent/historical-low - each candidate keeps its own, shown per-row
    instead. This is a comparison axis only, never a judged one.
    """
    if not points:
        return None
    values = [p["value"] for p in points]
    lo, hi = min(values), max(values)
    span = hi - lo
    pad = span * 0.14 if span else max(hi * 0.06, 1.0)
    lo, hi = lo - pad, hi + pad

    def pos(value: float) -> float:
        return round((value - lo) / (hi - lo) * 100, 2)

    positioned = [{**p, "pos": pos(p["value"])} for p in points]
    # Empty marks/bands rather than omitted keys: the axis template's ruler() macro
    # expects both, and a group's candidates share no thresholds to draw either from.
    return {"points": positioned, "marks": [], "bands": [], "lo": round(lo, 2), "hi": round(hi, 2)}


def suggest_price_target(
    listings: Iterable[Mapping[str, Any]], floor: int = 2
) -> dict[str, Any] | None:
    """A same-day price-target suggestion from whatever real listings a wizard found.

    Refuses below `floor` priced listings rather than printing a number from one data
    point dressed up as a target - the wizard's "work it out for me" step needs to say
    "not enough data yet" instead. When it does return a suggestion, the caller must
    render it with wording distinct from CLASS_LABELS (never "historical low territory"
    or "excellent"): those are earned through real tracking over time, and a same-day
    guess wearing the same words would be indistinguishable from an earned one.
    """
    prices = sorted(
        v for v in (_num(listing.get("delivered_price")) for listing in listings)
        if v is not None
    )
    if len(prices) < floor:
        return None
    return {
        "suggested_trigger": prices[0],
        "based_on_count": len(prices),
        "note": f"estimate, from {len(prices)} listing{'s' if len(prices) != 1 else ''} - "
                "not an earned historical low",
    }


def verdict_line(product: Mapping[str, Any]) -> tuple[str, str]:
    """The answer, in a sentence, before any data. Returns (tone, sentence).

    Tone is one of quiet / close / act / bought and drives how loudly it is shown. The
    wording states what to do and why, because "ABOVE TARGET" on a badge does not tell
    you whether you are ten dollars off or eight hundred.
    """
    purchase = product.get("purchase")
    if product.get("status") == "PURCHASED" and purchase:
        paid = purchase["price_paid"]
        moved = product.get("moved_since_purchase")
        when = str(purchase["purchased_at"])[:10]
        where = purchase.get("retailer_name") or "an unrecorded seller"
        if moved is not None and moved < 0 and product.get("protection_open"):
            return ("act", f"Now ${abs(moved):,.0f} cheaper than the ${paid:,.0f} you paid on "
                           f"{when}. Price protection is still open.")
        return ("bought", f"Bought {when} from {where} for ${paid:,.0f} delivered.")

    if product.get("status") == "PARKED":
        return ("quiet", "Parked. Nothing is being checked or alerted on.")

    listings = product.get("listings") or []
    if not listings:
        return ("quiet", "No retailers yet. Add one to start tracking a price.")

    best = product.get("best_listing")
    if best is None or product.get("best_delivered") is None:
        return ("quiet", "No prices recorded yet across "
                         f"{len(listings)} retailer{'s' if len(listings) != 1 else ''}.")

    price = product["best_delivered"]
    where = product["best_retailer"]
    rating = product["best_classification"]

    if not product.get("best_resolved"):
        return ("close", f"Unconfirmed. {where} shows ${price:,.0f} but freight is unknown, "
                         f"so the real figure could land anywhere above it.")

    trigger = _num(product.get("trigger_price"))
    if rating == HISTORICAL_LOW:
        return ("act", f"Buy. ${price:,.0f} delivered from {where} is historical-low territory.")
    if rating == EXCELLENT:
        return ("act", f"Buy. ${price:,.0f} delivered from {where}, under your "
                       f"${_num(product.get('excellent_price')):,.0f} excellent mark.")
    if rating == TRIGGER_MET:
        return ("act", f"Worth acting on. ${price:,.0f} delivered from {where}, under your "
                       f"${trigger:,.0f} trigger.")
    if trigger is not None:
        return ("quiet", f"Not yet. Best is ${price:,.0f} from {where}, "
                         f"${price - trigger:,.0f} over your ${trigger:,.0f} trigger.")
    return ("quiet", f"Best is ${price:,.0f} delivered from {where}. No trigger set.")
