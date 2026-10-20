"""A price-history change needs a comparable buying context."""

from app import provenance, store


def _listing(conn, *, price=500, conditional_requirements=None):
    product_id = store.create_product(
        conn, {"name": "Widget", "model": "W-1", "category": "general"}
    )
    retailer_id = store.ensure_retailer(conn, "Example Shop")["id"]
    listing_id = store.create_listing(conn, {
        "product_id": product_id,
        "retailer_id": retailer_id,
        "url": "https://example.test/widget",
        "advertised_price": price,
        "condition": "NEW",
        "stock_status": "IN_STOCK",
        "freight": 10,
        "freight_postcode": None,
        "conditional_requirements": conditional_requirements,
    })
    return product_id, listing_id


def test_history_marks_a_lower_price_only_when_comparison_context_is_stable(conn):
    product_id, listing_id = _listing(conn)
    provenance.set_verification(conn, listing_id, "model", "VERIFIED")
    provenance.set_verification(conn, listing_id, "price", "VERIFIED")
    first_id = store.record_observation(conn, listing_id, source="test")
    conn.execute("UPDATE listings SET advertised_price = 450 WHERE id = ?", (listing_id,))
    second_id = store.record_observation(conn, listing_id, source="test")

    history = {row["id"]: row for row in store.price_history(conn, product_id)}
    assert history[first_id]["price_change_reason"] == "INITIAL"
    assert history[second_id]["price_change_reason"] == "RETAILER_PRICE_REDUCTION"


def test_unverified_lower_advertised_price_is_not_claimed_as_a_retailer_reduction(conn):
    product_id, listing_id = _listing(conn)
    store.record_observation(conn, listing_id, source="test")
    conn.execute("UPDATE listings SET advertised_price = 450 WHERE id = ?", (listing_id,))
    second_id = store.record_observation(conn, listing_id, source="test")

    history = {row["id"]: row for row in store.price_history(conn, product_id)}
    assert history[second_id]["price_change_reason"] == "UNKNOWN"


def test_history_marks_eligibility_and_location_changes_as_context_changes(conn):
    product_id, listing_id = _listing(conn, conditional_requirements="Member price")
    store.record_observation(conn, listing_id, source="test")
    provenance.apply_values(
        conn, listing_id, {"eligibility_confirmed": True},
        state=provenance.MANUAL, source="ui",
    )
    after_eligibility = store.record_observation(conn, listing_id, source="ui")
    store.update_household_settings(conn, {"postcode": "4000"})
    after_location = store.record_observation(conn, listing_id, source="test")
    provenance.set_verification(conn, listing_id, "stock", "VERIFIED")
    after_verification = store.record_observation(conn, listing_id, source="test")

    history = {row["id"]: row for row in store.price_history(conn, product_id)}
    assert history[after_eligibility]["price_change_reason"] == "CONTEXT_CHANGED"
    assert history[after_location]["price_change_reason"] == "CONTEXT_CHANGED"
    assert history[after_verification]["price_change_reason"] == "CONTEXT_CHANGED"


def test_legacy_history_has_unknown_context_and_does_not_claim_a_reduction(conn):
    product_id, listing_id = _listing(conn)
    legacy = conn.execute(
        "INSERT INTO price_history (product_id, listing_id, advertised_price, source)"
        " VALUES (?, ?, 500, 'legacy')", (product_id, listing_id),
    )
    conn.execute("UPDATE listings SET advertised_price = 450 WHERE id = ?", (listing_id,))
    new_id = store.record_observation(conn, listing_id, source="test")

    history = {row["id"]: row for row in store.price_history(conn, product_id)}
    assert history[new_id]["price_change_reason"] == "UNKNOWN"
    assert history[new_id]["price_context"] is not None
    assert history[legacy.lastrowid]["price_change_reason"] == "UNKNOWN"


def test_clearing_a_listing_price_does_not_create_a_price_observation(conn):
    _product_id, listing_id = _listing(conn)
    first_id = store.record_observation(conn, listing_id, source="test")
    conn.execute("UPDATE listings SET advertised_price = NULL WHERE id = ?", (listing_id,))

    assert store.record_observation(conn, listing_id, source="test") is None
    assert [row["id"] for row in store.price_history(conn, _product_id)] == [first_id]


def test_price_history_api_and_page_include_price_comparison_reason(client):
    product = next(p for p in client.get("/api/products").json()
                   if p["model"] == "HW-Q930H/XY")
    history = client.get(f"/api/price-history/{product['id']}").json()
    assert history
    assert all(row["price_change_reason"] == "INITIAL" for row in history)
    page = client.get(f"/products/{product['id']}").text
    assert "Initial observation" in page
