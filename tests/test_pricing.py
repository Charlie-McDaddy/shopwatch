"""Delivered-price arithmetic, classification and ranking."""

from __future__ import annotations

from app import pricing

TARGETS = {"trigger_price": 900, "excellent_price": 850, "historical_low_price": 800}


def test_delivered_adds_freight_and_subtracts_every_discount():
    result = pricing.delivered_price(1000, freight=50, cashback=100, rebate=25, coupon_discount=15)
    assert result.value == 910.0
    assert result.resolved is True


def test_zero_freight_is_confirmed_free_shipping():
    result = pricing.delivered_price(869, freight=0)
    assert result.value == 869.0
    assert result.resolved is True


def test_null_freight_is_unresolved_not_free():
    result = pricing.delivered_price(869, freight=None)
    assert result.value == 869.0, "the provisional figure still shows"
    assert result.resolved is False, "but it must not claim to be a delivered price"


def test_no_advertised_price_means_no_delivered_price():
    assert pricing.delivered_price(None, freight=0).value is None


def test_price_strings_and_cashback_larger_than_price():
    assert pricing.delivered_price("1,059.00", freight="0").value == 1059.0
    assert pricing.delivered_price(100, freight=0, cashback=250).value == 0.0


def test_classification_thresholds_are_inclusive_and_ordered():
    assert pricing.classify(pricing.delivered_price(800, 0), TARGETS) == pricing.HISTORICAL_LOW
    assert pricing.classify(pricing.delivered_price(799, 0), TARGETS) == pricing.HISTORICAL_LOW
    assert pricing.classify(pricing.delivered_price(850, 0), TARGETS) == pricing.EXCELLENT
    assert pricing.classify(pricing.delivered_price(899, 0), TARGETS) == pricing.TRIGGER_MET
    assert pricing.classify(pricing.delivered_price(900, 0), TARGETS) == pricing.TRIGGER_MET
    assert pricing.classify(pricing.delivered_price(901, 0), TARGETS) == pricing.ABOVE_TARGET


def test_unresolved_freight_cannot_claim_a_good_deal():
    """An 869 headline with unknown freight is not an EXCELLENT buy, it is unresolved."""
    assert pricing.classify(pricing.delivered_price(869, None), TARGETS) == pricing.UNRESOLVED
    # Above target stays above target: freight can only make it worse.
    assert pricing.classify(pricing.delivered_price(1699, None), TARGETS) == pricing.ABOVE_TARGET


def test_unresolved_listing_does_not_outrank_slightly_dearer_confirmed_one():
    unresolved = pricing.delivered_price(869, None)
    confirmed = pricing.delivered_price(880, 0)
    assert pricing.rank_key(unresolved, 60) > pricing.rank_key(confirmed, 60)


def test_unresolved_listing_still_wins_when_it_is_far_cheaper():
    unresolved = pricing.delivered_price(700, None)
    confirmed = pricing.delivered_price(880, 0)
    assert pricing.rank_key(unresolved, 60) < pricing.rank_key(confirmed, 60)


def test_unpriced_listings_sort_last():
    assert pricing.rank_key(pricing.delivered_price(None), 60) > pricing.rank_key(
        pricing.delivered_price(9999, 0), 60
    )


def test_difference_from_historical_low():
    assert pricing.difference_from_low(pricing.delivered_price(869, 0), 800) == 69.0
    assert pricing.difference_from_low(pricing.delivered_price(869, 0), None) is None


# --------------------------------------------------------------- the headline sentence

BOARD = {
    "name": "Bar", "model": "M-1", "status": "ACTIVE",
    "trigger_price": 900, "excellent_price": 850, "historical_low_price": 800,
}


def product(**over):
    base = dict(BOARD, listings=[{}], best_listing={"id": 1}, best_delivered=None,
                best_retailer="Crowdshop", best_resolved=True,
                best_classification=pricing.ABOVE_TARGET)
    base.update(over)
    return base


def test_verdict_says_what_to_do_and_how_far_off():
    tone, line = pricing.verdict_line(product(best_delivered=990, best_classification=pricing.ABOVE_TARGET))
    assert tone == "quiet"
    assert "$90 over your $900 trigger" in line, line


def test_verdict_calls_a_crossed_trigger_worth_acting_on():
    tone, line = pricing.verdict_line(product(best_delivered=894, best_classification=pricing.TRIGGER_MET))
    assert tone == "act"
    assert line.startswith("Worth acting on.")


def test_verdict_says_buy_at_a_historical_low():
    tone, line = pricing.verdict_line(product(best_delivered=790, best_classification=pricing.HISTORICAL_LOW))
    assert tone == "act" and "historical-low" in line


def test_verdict_refuses_to_celebrate_an_unconfirmed_price():
    tone, line = pricing.verdict_line(
        product(best_delivered=869, best_resolved=False, best_classification=pricing.UNRESOLVED))
    assert tone == "close"
    assert "freight is unknown" in line


def test_verdict_for_an_empty_or_priceless_board():
    assert pricing.verdict_line(product(listings=[]))[1].startswith("No retailers yet")
    assert "No prices recorded" in pricing.verdict_line(product(best_listing=None))[1]


def test_verdict_for_something_already_bought():
    tone, line = pricing.verdict_line(product(
        status="PURCHASED",
        purchase={"price_paid": 629, "purchased_at": "2026-09-11", "retailer_name": "Sydney Tools"},
        moved_since_purchase=0, protection_open=False))
    assert tone == "bought" and "$629" in line


def test_verdict_shouts_when_a_bought_item_drops_inside_protection():
    tone, line = pricing.verdict_line(product(
        status="PURCHASED",
        purchase={"price_paid": 629, "purchased_at": "2026-09-11", "retailer_name": "Sydney Tools"},
        moved_since_purchase=-80, protection_open=True))
    assert tone == "act"
    assert "$80 cheaper" in line and "protection" in line.lower()


# ------------------------------------------------------------------------- the ruler


def test_scale_places_marks_in_order_and_within_bounds():
    scale = pricing.threshold_scale(BOARD, 869)
    assert [m["key"] for m in scale["marks"]] == [
        "historical_low_price", "excellent_price", "trigger_price"]
    positions = [m["pos"] for m in scale["marks"]]
    assert positions == sorted(positions)
    assert all(0 <= p <= 100 for p in positions + [scale["best"]["pos"]])


def test_a_cheaper_best_sits_left_of_a_dearer_one():
    cheap = pricing.threshold_scale(BOARD, 820)["best"]["pos"]
    dear = pricing.threshold_scale(BOARD, 890)["best"]["pos"]
    assert cheap < dear


def test_scale_without_a_price_still_plots_the_targets():
    scale = pricing.threshold_scale(BOARD, None)
    assert scale["best"] is None and len(scale["marks"]) == 3


def test_no_targets_means_no_axis_rather_than_an_empty_one():
    assert pricing.threshold_scale({}, 500) is None


def test_a_single_target_does_not_divide_by_zero():
    scale = pricing.threshold_scale({"trigger_price": 900}, 900)
    assert 0 <= scale["marks"][0]["pos"] <= 100
    assert 0 <= scale["best"]["pos"] <= 100


def test_marks_sharing_a_value_merge_into_one_labelled_mark():
    """Issue #101's auto-derive sets excellent_price = historical_low_price exactly,
    so two targets landing on the same value is now routine, not a rare coincidence.
    Each mark renders as its own absolutely-positioned label on the axis - two at the
    same position previously overlapped into illegible text. One merged mark with a
    combined label is the fix, not two marks fighting for the same pixels."""
    scale = pricing.threshold_scale(
        {"historical_low_price": 69.30, "excellent_price": 69.30, "trigger_price": 76.23}, 88.95
    )
    assert [m["value"] for m in scale["marks"]] == [69.30, 76.23]
    hist_and_excellent = scale["marks"][0]
    assert hist_and_excellent["label"] == "hist low + excellent"
    assert hist_and_excellent["key"] == "historical_low_price+excellent_price"


def test_all_three_targets_sharing_a_value_merge_into_one_mark():
    scale = pricing.threshold_scale(
        {"historical_low_price": 55, "excellent_price": 55, "trigger_price": 55}, 55
    )
    assert len(scale["marks"]) == 1
    assert scale["marks"][0]["label"] == "hist low + excellent + trigger"


# ------------------------------------------------------- every contender on the axis

CONTENDERS = [
    {"id": 1, "retailer_name": "Crowdshop", "delivered_price": 869,
     "delivered_resolved": False, "classification": "UNRESOLVED"},
    {"id": 2, "retailer_name": "Appliance Central", "delivered_price": 990,
     "delivered_resolved": True, "classification": "ABOVE_TARGET"},
    {"id": 3, "retailer_name": "JB Hi-Fi", "delivered_price": 1699,
     "delivered_resolved": True, "classification": "ABOVE_TARGET"},
    {"id": 4, "retailer_name": "No price yet", "delivered_price": None,
     "delivered_resolved": False, "classification": "UNRESOLVED"},
]


def test_every_priced_contender_lands_on_the_axis():
    scale = pricing.threshold_scale(BOARD, 869, CONTENDERS)
    plotted = [p["retailer"] for p in scale["points"]]
    assert plotted == ["Crowdshop", "Appliance Central", "JB Hi-Fi"], "cheapest first"
    assert all(0 <= p["pos"] <= 100 for p in scale["points"])


def test_a_contender_without_a_price_is_not_plotted_anywhere():
    """A dot on a price axis claims a number. There is no number, so there is no dot."""
    scale = pricing.threshold_scale(BOARD, 869, CONTENDERS)
    assert "No price yet" not in [p["retailer"] for p in scale["points"]]


def test_the_dearest_contender_widens_the_domain():
    """The reason the axis needs a zoom: one $1,699 listing crushes the targets."""
    narrow = pricing.threshold_scale(BOARD, 869)
    wide = pricing.threshold_scale(BOARD, 869, CONTENDERS)
    assert wide["hi"] > narrow["hi"] * 1.5
    spread = max(m["pos"] for m in wide["marks"]) - min(m["pos"] for m in wide["marks"])
    assert spread < 15, "targets occupy a sliver once the dear end is on the same axis"


def test_bands_tile_the_axis_without_gaps_or_overlaps():
    scale = pricing.threshold_scale(BOARD, 869, CONTENDERS)
    bands = scale["bands"]
    assert bands[0]["from"] == 0 and bands[-1]["to"] == 100
    for earlier, later in zip(bands, bands[1:], strict=False):
        assert earlier["to"] == later["from"], "a gap would read as meaningless territory"


def test_only_the_bands_that_change_a_decision_are_toned():
    scale = pricing.threshold_scale(BOARD, 869, CONTENDERS)
    tones = {b["label"]: b["tone"] for b in scale["bands"]}
    assert tones["above target"] == "quiet", "the ordinary state is never coloured"
    assert tones["on target"] == "close"
    assert tones["historical low"] == "act" and tones["excellent"] == "act"


def test_a_missing_target_drops_its_band_rather_than_collapsing_the_others():
    scale = pricing.threshold_scale(
        {"excellent_price": 850, "trigger_price": 900}, 869, CONTENDERS)
    labels = [b["label"] for b in scale["bands"]]
    assert "historical low" not in labels
    assert labels[0] == "excellent" and scale["bands"][0]["from"] == 0


# ----------------------------------------------------------------- ruled out

def _delivered(rows):
    """Attach the Delivered objects best_listing reads, as store.enrich_listing does."""
    for row in rows:
        row["delivered"] = pricing.delivered_price(
            row["advertised_price"], freight=row.get("freight"))
        row.setdefault("delivered_resolved", row["delivered"].resolved)
    return rows


def test_a_ruled_out_listing_is_never_the_answer_however_cheap():
    """The board headlined a rejected $869 for hours. Cheapest and best are not the
    same question once a decision has been made about one of them."""
    rows = _delivered([
        {"retailer_name": "Crowdshop", "advertised_price": 869, "freight": 0,
         "ruled_out": True, "condition": "NEW", "stock_status": "In Stock",
         "verification": {k: {"status": "VERIFIED"} for k in ("model", "price", "stock")}},
        {"retailer_name": "Appliance Central", "advertised_price": 990, "freight": 0,
         "ruled_out": False, "condition": "NEW", "stock_status": "In Stock",
         "verification": {k: {"status": "VERIFIED"} for k in ("model", "price", "stock")}},
    ])
    best = pricing.best_listing(rows)
    assert best["retailer_name"] == "Appliance Central"


def test_ruling_everything_out_leaves_no_answer_rather_than_a_rejected_one():
    rows = _delivered([
        {"retailer_name": "Crowdshop", "advertised_price": 869, "freight": 0,
         "ruled_out": True, "condition": "NEW", "stock_status": "In Stock",
         "verification": {k: {"status": "VERIFIED"} for k in ("model", "price", "stock")}},
    ])
    assert pricing.best_listing(rows) is None


def test_best_listing_requires_a_confirmed_new_available_exact_offer():
    base = {
        "advertised_price": 800, "freight": 0, "condition": "NEW",
        "stock_status": "In Stock", "delivered_resolved": True,
        "verification": {key: {"status": "VERIFIED"} for key in ("model", "price", "stock")},
    }
    rows = _delivered([
        {**base, "retailer_name": "Confirmed", "freight": 20},
        {**base, "retailer_name": "Search snippet", "verification": {}},
        {**base, "retailer_name": "Used", "condition": "USED"},
        {**base, "retailer_name": "Unavailable", "stock_status": "Out of stock"},
        {**base, "retailer_name": "Preorder", "stock_status": "Available for preorder"},
        {**base, "retailer_name": "Unknown condition", "condition": None},
        {**base, "retailer_name": "Unknown freight", "freight": None},
    ])
    assert pricing.best_listing(rows)["retailer_name"] == "Confirmed"


def test_conditional_discount_requires_explicit_eligibility():
    row = {
        "advertised_price": 900, "freight": 10, "coupon_discount": 100,
        "conditional_requirements": "new members only", "eligibility_confirmed": False,
        "delivered_resolved": True,
        "provenance": {"eligibility_confirmed": {"state": "MANUAL", "manual_locked": 1}},
        "condition": "NEW", "stock_status": "In Stock",
        "verification": {key: {"status": "VERIFIED"} for key in ("model", "price", "stock")},
    }
    assert pricing.listing_eligibility(row) == (False, ["new members only"])
    row["eligibility_confirmed"] = True
    assert pricing.listing_eligibility(row) == (True, [])


def test_a_ruled_out_listing_is_still_plotted():
    """It stays visible on purpose: a decision you can see is easier to revisit."""
    scale = pricing.threshold_scale(BOARD, 990, [
        {"id": 1, "retailer_name": "Crowdshop", "delivered_price": 869,
         "delivered_resolved": False, "ruled_out": True},
        {"id": 2, "retailer_name": "Appliance Central", "delivered_price": 990,
         "delivered_resolved": True, "ruled_out": False},
    ])
    plotted = {p["retailer"]: p["ruled_out"] for p in scale["points"]}
    assert plotted == {"Crowdshop": True, "Appliance Central": False}


# ------------------------------------- bands tile whatever targets actually exist


def test_bands_still_tile_when_the_middle_target_is_missing():
    """Every target column is nullable and all three inputs are optional.

    Naming a fixed predecessor meant a band whose predecessor was NULL fell back to
    the left edge and painted over its neighbour, so a price in historical-low
    territory rendered as merely on target.
    """
    scale = pricing.threshold_scale(
        {"historical_low_price": 800, "trigger_price": 900}, 869, CONTENDERS)
    bands = scale["bands"]
    assert [b["label"] for b in bands] == ["historical low", "on target", "above target"]
    assert bands[0]["from"] == 0 and bands[-1]["to"] == 100
    for earlier, later in zip(bands, bands[1:], strict=False):
        assert earlier["to"] == later["from"], "an overlap reads as the wrong territory"


def test_bands_tile_when_only_the_dearest_target_is_set():
    scale = pricing.threshold_scale({"trigger_price": 900}, 869, CONTENDERS)
    bands = scale["bands"]
    assert bands[0]["label"] == "on target" and bands[0]["from"] == 0
    for earlier, later in zip(bands, bands[1:], strict=False):
        assert earlier["to"] == later["from"]


# ------------------------------- an axis needs something to plot, not a target


def test_prices_without_any_target_still_draw_an_axis():
    """Adding a product and its listings before deciding a trigger is a normal order
    of work. Three prices are worth comparing against each other even with no line
    drawn yet."""
    scale = pricing.threshold_scale({}, None, CONTENDERS)
    assert scale is not None, "prices with no targets must still plot"
    assert len(scale["points"]) == 3
    assert scale["marks"] == [] and scale["bands"] == []


def test_nothing_at_all_still_means_no_axis():
    """The early return has to survive: an empty axis is worse than no axis."""
    assert pricing.threshold_scale({}, None, []) is None
    assert pricing.threshold_scale({}, None) is None


# ------------------------------------------------- wizard price-target suggestions


def test_suggestion_refuses_below_the_floor():
    """One listing is not enough to call anything a target - the honest answer is
    'not enough data yet', not a guess wearing a confident number."""
    one = [{"delivered_price": 999.0}]
    assert pricing.suggest_price_target(one) is None


def test_suggestion_appears_once_the_floor_is_met():
    two = [{"delivered_price": 999.0}, {"delivered_price": 899.0}]
    suggestion = pricing.suggest_price_target(two)
    assert suggestion is not None
    assert suggestion["suggested_trigger"] == 899.0
    assert suggestion["based_on_count"] == 2


def test_suggestion_note_never_uses_class_label_wording():
    """A same-day guess must never wear the words earned by real historical tracking."""
    two = [{"delivered_price": 999.0}, {"delivered_price": 899.0}]
    suggestion = pricing.suggest_price_target(two)
    for label in pricing.CLASS_LABELS.values():
        assert label.lower() not in suggestion["note"].lower()


def test_suggestion_ignores_listings_with_no_price():
    listings = [{"delivered_price": None}, {"delivered_price": 899.0}]
    assert pricing.suggest_price_target(listings) is None  # only 1 real price


def test_suggestion_floor_is_configurable():
    one = [{"delivered_price": 899.0}]
    assert pricing.suggest_price_target(one, floor=1) is not None
