"""Product lifecycle and purchase records.

The behaviour that matters: buying something stops it nagging you, without deleting
anything you learned while hunting it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import alerts, price_watch, provenance, store
from app.db import connect


def day(offset: int) -> str:
    return (datetime.now(UTC) + timedelta(days=offset)).strftime("%Y-%m-%d")


def crowdshop(conn):
    product = store.product_by_model(conn, "HW-Q930H/XY")
    return product, store.find_listing(
        conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
    )


@pytest.fixture()
def armed(seeded):
    """Seeded board with Crowdshop's freight confirmed, so its rule would fire."""
    with connect(seeded) as conn:
        _, listing = crowdshop(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        conn.commit()
    return seeded


def test_the_control_an_active_product_does_alert(armed):
    with connect(armed) as conn:
        assert len(alerts.evaluate_all(conn)) == 1


def test_a_new_product_starts_active(conn):
    pid = store.create_product(conn, {"name": "Thing", "model": "THING-1"})
    assert store.get_product(conn, pid)["status"] == "ACTIVE"


def test_recording_a_purchase_moves_the_product_and_keeps_history(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        before = len(store.price_history(conn, product["id"]))
        purchase = store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "advertised_paid": 869,
             "freight_paid": 25, "order_reference": "CS-12345"},
        )
        conn.commit()
        assert purchase["retailer_name"] == "Crowdshop", "retailer copied from the listing"
        assert store.get_product(conn, product["id"])["status"] == "PURCHASED"
        history = store.price_history(conn, product["id"])
        assert len(history) == before + 1
        assert history[0]["source"] == "purchase"
        assert history[0]["delivered_price"] == 894
        assert history[0]["freight_resolved"] == 1, "a price you paid is a confirmed price"


def test_a_purchase_needs_a_delivered_price(conn):
    pid = store.create_product(conn, {"name": "Thing", "model": "THING-1"})
    with pytest.raises(ValueError, match="price_paid"):
        store.record_purchase(conn, pid, {"order_reference": "X"})


def test_a_purchase_can_name_a_retailer_that_was_never_on_the_board(armed):
    with connect(armed) as conn:
        product, _ = crowdshop(conn)
        purchase = store.record_purchase(
            conn, product["id"], {"price_paid": 850, "retailer_name": "A bloke on Marketplace"}
        )
        assert purchase["retailer_name"] == "A bloke on Marketplace"
        assert purchase["listing_id"] is None


def test_a_purchased_product_keeps_normal_alerts(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(conn, product["id"], {"listing_id": listing["id"], "price_paid": 894})
        conn.commit()
        assert len(alerts.evaluate_all(conn)) == 1


def test_a_parked_product_stops_alerting_too(armed):
    with connect(armed) as conn:
        product, _ = crowdshop(conn)
        store.update_product(conn, product["id"], {"status": "PARKED"})
        conn.commit()
        assert alerts.evaluate_all(conn) == []


def test_the_watch_keeps_checking_a_purchased_product(armed):
    with connect(armed) as conn:
        conn.execute("UPDATE listings SET url = 'https://example.invalid/x'")
        conn.commit()
    summary = price_watch.run(send_alerts=False)
    assert summary["checked"] == 5, "control: all five are checked while ACTIVE"

    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(conn, product["id"], {"listing_id": listing["id"], "price_paid": 894})
        conn.commit()
    assert price_watch.run(send_alerts=False)["checked"] == 5


# ------------------------------------------------------------------- price protection


def test_price_protection_keeps_the_watch_running(armed):
    with connect(armed) as conn:
        conn.execute("UPDATE listings SET url = 'https://example.invalid/x'")
        product, listing = crowdshop(conn)
        store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "price_protection_until": day(14)},
        )
        conn.commit()
    assert price_watch.run(send_alerts=False)["checked"] == 5


def test_protection_alerts_when_it_drops_below_what_you_paid(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "price_protection_until": day(14)},
        )
        conn.commit()

        assert all(a.rule_name != "Price protection" for a in alerts.evaluate_all(conn)), (
            "normal target alerts remain armed before the price beats the purchase"
        )

        provenance.set_field(conn, listing["id"], "advertised_price", 780.0,
                             state=provenance.MANUAL)
        conn.commit()
        raised = alerts.evaluate_all(conn)
        assert len(raised) == 1
        assert raised[0].rule_name == "Price protection"
        assert raised[0].delivered == 805.0
        assert "below the $894 you paid" in raised[0].reason


def test_protection_ignores_an_unconfirmed_cheaper_price(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "price_protection_until": day(14)},
        )
        # Harvey Norman marked far cheaper but with freight unresolved.
        hn = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Harvey Norman")["id"]
        )
        provenance.set_field(conn, hn["id"], "advertised_price", 700.0, state=provenance.MANUAL)
        conn.commit()
        assert all(a.rule_name != "Price protection" for a in alerts.evaluate_all(conn)), (
            "chasing a price guarantee on an unconfirmed figure wastes a trip"
        )


def test_protection_stops_at_the_end_of_the_window(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "price_protection_until": day(-1)},
        )
        provenance.set_field(conn, listing["id"], "advertised_price", 780.0,
                             state=provenance.MANUAL)
        conn.commit()
        assert all(a.rule_name != "Price protection" for a in alerts.evaluate_all(conn))


def test_protection_does_not_re_alert_on_a_trivial_move(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(
            conn, product["id"],
            {"listing_id": listing["id"], "price_paid": 894, "price_protection_until": day(14)},
        )
        provenance.set_field(conn, listing["id"], "advertised_price", 780.0,
                             state=provenance.MANUAL)
        conn.commit()
        first = [a for a in alerts.evaluate_all(conn) if a.rule_name == "Price protection"]
        assert len(first) == 1
        alerts.record_fired(conn, first[0])
        conn.commit()

        provenance.set_field(conn, listing["id"], "advertised_price", 775.0,
                             state=provenance.MANUAL)
        conn.commit()
        assert alerts.evaluate_all(conn) == []

        provenance.set_field(conn, listing["id"], "advertised_price", 700.0,
                             state=provenance.MANUAL)
        conn.commit()
        assert len(alerts.evaluate_all(conn)) == 1


def test_movement_since_purchase_is_reported(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(conn, product["id"], {"listing_id": listing["id"], "price_paid": 894})
        provenance.set_field(conn, listing["id"], "advertised_price", 800.0,
                             state=provenance.MANUAL)
        conn.commit()
        view = store.product_view(conn, product["id"])
        assert view["moved_since_purchase"] == -69.0, "825 delivered vs 894 paid"
        assert view["protection_open"] is False


# --------------------------------------------------------------------------- undo


def test_undo_restores_active_but_keeps_the_history(armed):
    with connect(armed) as conn:
        product, listing = crowdshop(conn)
        store.record_purchase(conn, product["id"], {"listing_id": listing["id"], "price_paid": 894})
        conn.commit()
        before = len(store.price_history(conn, product["id"]))

        assert store.undo_purchase(conn, product["id"]) is True
        conn.commit()
        assert store.get_product(conn, product["id"])["status"] == "ACTIVE"
        assert store.latest_purchase(conn, product["id"]) is None
        assert len(store.price_history(conn, product["id"])) == before
        assert len(alerts.evaluate_all(conn)) == 1, "alerts come back"


def test_undo_on_a_product_that_was_never_bought(conn):
    pid = store.create_product(conn, {"name": "Thing", "model": "THING-1"})
    assert store.undo_purchase(conn, pid) is False



def test_paused_and_archived_purchases_stop_checks_and_alerts(armed):
    with connect(armed) as conn:
        conn.execute("UPDATE listings SET url = 'https://example.invalid/x'")
        product, listing = crowdshop(conn)
        store.record_purchase(conn, product["id"], {"listing_id": listing["id"], "price_paid": 894})
        store.update_product(conn, product["id"], {"status": "PARKED"})
        conn.commit()
        assert alerts.evaluate_all(conn) == []

    assert price_watch.run(send_alerts=False)["checked"] == 0

    with connect(armed) as conn:
        product, _ = crowdshop(conn)
        store.update_product(conn, product["id"], {"status": "PURCHASED"})
        conn.commit()
        assert len(alerts.evaluate_all(conn)) == 1
    assert price_watch.run(send_alerts=False)["checked"] == 5

    with connect(armed) as conn:
        product, _ = crowdshop(conn)
        store.update_product(conn, product["id"], {"archived": 1})
        conn.commit()
        assert alerts.evaluate_all(conn) == []
    assert price_watch.run(send_alerts=False)["checked"] == 0


# ------------------------------------------------------------------ many products


def test_new_products_do_not_disturb_the_existing_ones(seeded):
    """Adding research targets must not bury or change what is already being hunted."""
    with connect(seeded) as conn:
        store.create_product(conn, {"name": "LG C4 65", "model": "OLED65C4PSA", "category": "tv"})
        store.create_product(conn, {"name": "Makita kit", "model": "DLX2414ST",
                                    "category": "power_tool"})
        conn.commit()

        active = store.list_products(conn, status="ACTIVE")
        assert len(active) == 3
        assert [p["name"] for p in active] == sorted(p["name"] for p in active)

        q930h = next(p for p in active if p["model"] == "HW-Q930H/XY")
        assert len(q930h["listings"]) == 5, "the existing board is untouched"
        assert q930h["trigger_price"] == 900


def test_status_filter_separates_the_bought_from_the_hunted(seeded):
    with connect(seeded) as conn:
        tv = store.create_product(conn, {"name": "LG C4 65", "model": "OLED65C4PSA"})
        store.record_purchase(conn, tv, {"price_paid": 2100, "retailer_name": "Bing Lee"})
        conn.commit()

        assert [p["model"] for p in store.list_products(conn, status="ACTIVE")] == ["HW-Q930H/XY"]
        assert [p["model"] for p in store.list_products(conn, status="PURCHASED")] == ["OLED65C4PSA"]
        assert len(store.list_products(conn)) == 2
        # ACTIVE sorts ahead of PURCHASED so the hunt stays at the top of the board.
        assert [p["status"] for p in store.list_products(conn)] == ["ACTIVE", "PURCHASED"]
