"""Evidence-based buying advice from the product's recorded prices."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any


def next_sale_dates(today: date) -> list[dict[str, str]]:
    """Australian retail calendar dates; these are planning dates, not price claims."""
    dates = []
    for year in (today.year, today.year + 1):
        first_thursday = date(year, 11, 1)
        first_thursday = date(year, 11, 1 + (3 - first_thursday.weekday()) % 7)
        black_friday = date(year, 11, first_thursday.day + 22)
        dates.extend([
            {"name": "Black Friday", "date": black_friday.isoformat()},
            {"name": "Boxing Day", "date": date(year, 12, 26).isoformat()},
        ])
    return [item for item in dates if item["date"] >= today.isoformat()][:2]


def assess(
    product: Mapping[str, Any],
    history: list[Mapping[str, Any]],
    archive: list[Mapping[str, Any]],
    today: date,
) -> dict[str, Any]:
    """State a limited, dated recommendation from confirmed offers and recorded evidence."""
    offers = [
        listing for listing in product.get("listings", [])
        if listing.get("offer_eligible") and listing.get("delivered_resolved")
    ]
    best = product.get("best_listing")
    mode = product.get("buying_mode", "just_watching")
    delivered_history = [
        item for item in history
        if item.get("freight_resolved") and item.get("delivered_price") is not None
        and item.get("source") != "seed"
    ]
    historical_best = min(delivered_history, key=lambda item: item["delivered_price"], default=None)
    historical_signal = None
    if historical_best:
        historical_signal = (
            f"A delivered amount was recorded as ${historical_best['delivered_price']:,.2f} "
            f"at {historical_best.get('retailer_name') or 'an unrecorded retailer'} on "
            f"{str(historical_best['observed_at'])[:10]} (source: "
            f"{historical_best.get('source') or 'unspecified'}). The record does not confirm "
            "the exact model, condition, availability, location or purchase eligibility then, "
            "so it is not a comparable price signal."
        )
    history_limit = (
        "Dated delivered observations exist, but their model, condition, availability, location "
        "and purchase eligibility are not preserved well enough to establish comparable history."
        if historical_best
        else "No dated delivered observation is available; the assessment relies on current offers "
        "and your saved price mark."
    )
    saved_low_signal = None
    if product.get("lowest_known_price") is not None:
        details = []
        if product.get("lowest_known_retailer"):
            details.append(f"from {product['lowest_known_retailer']}")
        if product.get("lowest_known_date"):
            details.append(f"dated {product['lowest_known_date']}")
        note = product.get("lowest_known_notes")
        status = (
            "The saved note marks this as an unconfirmed estimate."
            if note and any(term in note.lower() for term in ("unconfirmed", "estimate"))
            else "Check the source note before treating it as comparable buying evidence."
        )
        saved_low_signal = (
            f"Saved historical-low record: ${product['lowest_known_price']:,.2f}"
            f"{'; ' + '; '.join(details) if details else ''}. "
            f"Source / confirmation note: {note or 'none recorded'}. {status}"
        )
    archive_signal = None
    if archive:
        item = min(archive, key=lambda row: row["advertised_price"])
        archive_signal = (
            f"Wayback captured an advertised ${item['advertised_price']:,.2f} at "
            f"{item.get('retailer_name') or 'a retailer'} on {str(item['captured_at'])[:10]}; "
            "it is not comparable to a delivered price because freight and eligibility "
            "are unknown."
        )

    if not offers or best is None:
        recommendation = (
            "There is not enough confirmed current offer evidence to say whether to buy now "
            "or wait."
        )
    elif mode == "buy_asap":
        if product.get("best_classification") in {"HISTORICAL_LOW", "EXCELLENT", "TRIGGER_MET"}:
            recommendation = (
                f"Buy now if the terms suit you: {best['retailer_name']} has a confirmed "
                f"${best['delivered_price']:,.2f} delivered offer that meets your price mark."
            )
        else:
            recommendation = (
                f"Your intent is to buy ASAP. The best confirmed offer is "
                f"${best['delivered_price']:,.2f} delivered at {best['retailer_name']}; "
                "the recorded evidence does not establish that waiting will improve it."
            )
    elif product.get("best_classification") in {"HISTORICAL_LOW", "EXCELLENT", "TRIGGER_MET"}:
        recommendation = (
            f"You are just watching, and the best confirmed offer is "
            f"${best['delivered_price']:,.2f} delivered at {best['retailer_name']}, "
            "within your price mark. Consider it if you are ready to buy."
        )
    else:
        recommendation = (
            f"You are just watching. The best confirmed offer is "
            f"${best['delivered_price']:,.2f} delivered at {best['retailer_name']}; "
            "waiting for more evidence is reasonable."
        )

    return {
        "mode": mode,
        "recommendation": recommendation,
        "offers": offers,
        "history": history,
        "archive": archive,
        "historical_signal": historical_signal,
        "history_limit": history_limit,
        "saved_low_signal": saved_low_signal,
        "archive_signal": archive_signal,
        "sale_dates": next_sale_dates(today),
        "uncertainty": (
            "Black Friday and Boxing Day are common Australian sale periods, but neither "
            "guarantees a discount. Past prices do not predict future prices, and the "
            "recorded history does not establish that an old offer is comparable today."
        ),
    }
