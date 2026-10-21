"""Alert rules fire on delivered price, on meaningful change, and on complete systems only."""

from __future__ import annotations

from app import alerts, provenance, store
from app.db import connect


def crowdshop_listing(conn):
    product = store.product_by_model(conn, "HW-Q930H/XY")
    listing = store.find_listing(
        conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
    )
    for aspect in ("model", "price", "stock"):
        provenance.set_verification(conn, listing["id"], aspect, provenance.VERIFIED)
    return product, listing


def test_nothing_fires_while_freight_is_unresolved(seeded):
    with connect(seeded) as conn:
        assert alerts.evaluate_all(conn) == [], (
            "the seeded Crowdshop price is under trigger but freight is unconfirmed"
        )


def test_unverified_import_is_not_actionable_even_after_freight_is_entered(seeded):
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(conn, listing["id"], "freight", 0, state=provenance.MANUAL)
        conn.commit()
        assert alerts.evaluate_all(conn) == []


def test_confirming_freight_under_trigger_fires_the_new_stock_rule(seeded):
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        conn.commit()
        raised = alerts.evaluate_all(conn)
        assert len(raised) == 1
        assert raised[0].delivered == 894.0
        assert raised[0].classification == "TRIGGER_MET"


def test_an_incomplete_system_does_not_fire(seeded):
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        provenance.set_field(
            conn, listing["id"], "included_components", "", state=provenance.MANUAL
        )
        conn.commit()
        assert alerts.evaluate_all(conn) == []


def test_a_trivial_price_move_does_not_re_alert(seeded):
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        conn.commit()
        first = alerts.evaluate_all(conn)
        alerts.record_fired(conn, first[0])
        conn.commit()

        provenance.set_field(conn, listing["id"], "advertised_price", 864.0,
                             state=provenance.MANUAL)
        conn.commit()
        assert alerts.evaluate_all(conn) == [], "a $5 move is not a new buying decision"

        provenance.set_field(conn, listing["id"], "advertised_price", 820.0,
                             state=provenance.MANUAL)
        conn.commit()
        again = alerts.evaluate_all(conn)
        assert len(again) == 1
        assert again[0].classification == "EXCELLENT"


def test_secondary_stock_rule_has_its_own_threshold(seeded):
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 0.0, state=provenance.MANUAL)
        provenance.set_field(conn, listing["id"], "condition", "CARTON_DAMAGED",
                             state=provenance.MANUAL)
        conn.commit()
        # $869 carton damaged is above the $800 secondary threshold and the NEW rule
        # does not apply to it, so nothing fires.
        assert alerts.evaluate_all(conn) == []

        provenance.set_field(conn, listing["id"], "advertised_price", 780.0,
                             state=provenance.MANUAL)
        conn.commit()
        raised = alerts.evaluate_all(conn)
        assert len(raised) == 1
        assert "Secondary" in raised[0].rule_name


def test_alert_body_states_when_a_delivered_price_is_provisional():
    alert = alerts.Alert(
        product_name="Bar", model="M", retailer="R", delivered=800.0,
        classification="EXCELLENT", condition="NEW", rule_id=1, rule_name="rule",
        url=None, listing_id=1, reason="under target", resolved=False,
    )
    assert "freight unresolved" in alert.body()


# ------------------------------------------------------- ruled out never alerts


def test_a_ruled_out_listing_does_not_alert(seeded):
    """The board refuses to nominate a ruled-out listing. The phone must agree.

    An alert engine that disagrees with the board is worse than either on its own:
    the page shows the price struck through while ntfy says act on it, and you have
    no way to tell which one is lying.
    """
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        conn.commit()
        assert len(alerts.evaluate_all(conn)) == 1, "control: it alerts while in the running"

        conn.execute(
            "UPDATE listings SET ruled_out = 1, ruled_out_reason = ? WHERE id = ?",
            ("group-buy, freight never quotable", listing["id"]),
        )
        conn.commit()
        assert alerts.evaluate_all(conn) == [], "ruled out must not reach the phone"


def test_putting_it_back_in_the_running_alerts_again(seeded):
    """Ruling out is reversible, so the suppression has to be too."""
    with connect(seeded) as conn:
        _, listing = crowdshop_listing(conn)
        provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
        conn.execute("UPDATE listings SET ruled_out = 1 WHERE id = ?", (listing["id"],))
        conn.commit()
        assert alerts.evaluate_all(conn) == []

        conn.execute("UPDATE listings SET ruled_out = 0 WHERE id = ?", (listing["id"],))
        conn.commit()
        assert len(alerts.evaluate_all(conn)) == 1
