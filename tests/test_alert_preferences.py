"""Per-product alerts use the best confirmed comparable delivered offer."""

from app import alerts, provenance, store
from app.db import connect


def _armed(conn, target=None, drop=False):
    product = store.product_by_model(conn, "HW-Q930H/XY")
    listing = store.find_listing(
        conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
    )
    for aspect in ("model", "price", "stock"):
        provenance.set_verification(conn, listing["id"], aspect, provenance.VERIFIED)
    provenance.set_field(conn, listing["id"], "freight", 25.0, state=provenance.MANUAL)
    conn.execute(
        "INSERT INTO product_alert_preferences (product_id, target_price, drop_enabled)"
        " VALUES (?, ?, ?)", (product["id"], target, int(drop)),
    )
    conn.execute("UPDATE alert_rules SET enabled = 0 WHERE product_id = ?", (product["id"],))
    conn.commit()
    return product["id"], listing["id"]


def test_target_alert_is_deduplicated_across_connections(seeded):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, target=900)
        view = store.product_view(conn, product_id)
        raised = alerts.evaluate_product(conn, view)
        assert [(alert.rule_name, alert.delivered) for alert in raised] == [("Target price", 894.0)]
        alerts.record_fired(conn, raised[0])
        conn.commit()

    with connect(seeded) as conn:
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []


def test_target_alert_fires_again_after_price_leaves_and_reenters_target(seeded):
    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, target=900)
        first = alerts.evaluate_product(conn, store.product_view(conn, product_id))
        alerts.record_fired(conn, first[0])
        provenance.set_field(conn, listing_id, "advertised_price", 969.0,
                             state=provenance.MANUAL)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        conn.commit()
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []

        provenance.set_field(conn, listing_id, "advertised_price", 869.0,
                             state=provenance.MANUAL)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert [a.rule_name for a in alerts.evaluate_product(conn, view)] == ["Target price"]


def test_drop_watch_seeds_then_alerts_only_on_a_further_confirmed_best_price(seeded):
    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, drop=True)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        conn.commit()
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []

        provenance.set_field(conn, listing_id, "advertised_price", 829.0,
                             state=provenance.MANUAL)
        conn.commit()
        raised = alerts.evaluate_product(conn, store.product_view(conn, product_id))
        assert [(alert.rule_name, alert.delivered) for alert in raised] == [("Price drop", 854.0)]
        alerts.record_fired(conn, raised[0])
        conn.commit()

    with connect(seeded) as conn:
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []


def test_zero_drop_threshold_still_requires_a_strict_price_reduction(seeded):
    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, drop=True)
        conn.execute(
            "UPDATE product_alert_preferences SET drop_min_change = 0 WHERE product_id = ?",
            (product_id,),
        )
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        conn.commit()
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []

        provenance.set_field(conn, listing_id, "advertised_price", 868.0,
                             state=provenance.MANUAL)
        assert [a.rule_name for a in alerts.evaluate_product(
            conn, store.product_view(conn, product_id)
        )] == ["Price drop"]


def test_delivery_failure_is_kept_in_durable_alert_history(seeded, monkeypatch):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, target=900)
        raised = alerts.evaluate_product(conn, store.product_view(conn, product_id))
        monkeypatch.setattr(
            alerts, "send",
            lambda _alert, _config: alerts.DeliveryReport(
                failures=["ntfy delivery failed: ConnectionError"]
            ),
        )
        result = alerts.dispatch(conn, raised)
        conn.commit()
        assert result["delivery_failures"] == ["ntfy delivery failed: ConnectionError"]

    with connect(seeded) as conn:
        history = alerts.get_alert_history(conn, product_id)
        assert len(history) == 1
        assert history[0]["delivery_status"] == "failed"
        assert "ConnectionError" in history[0]["delivery_error"]


def test_saving_unchanged_preferences_preserves_both_dedupe_states(seeded):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, target=900, drop=True)
        conn.execute(
            "UPDATE product_alert_preferences SET target_last_fired_price = 894,"
            " target_above_target = 0, drop_baseline_price = 950,"
            " drop_baseline_context = 'ctx', drop_last_fired_price = 894"
            " WHERE product_id = ?", (product_id,),
        )
        alerts.update_preferences(conn, product_id, {
            "target_price": 900, "drop_enabled": True, "drop_min_change": 20,
        })
        prefs = conn.execute(
            "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product_id,)
        ).fetchone()
        assert (prefs["target_last_fired_price"], prefs["target_above_target"]) == (894, 0)
        assert (prefs["drop_baseline_price"], prefs["drop_baseline_context"],
                prefs["drop_last_fired_price"]) == (950, "ctx", 894)


def test_changing_one_preference_mode_preserves_the_other_mode_state(seeded):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, target=900, drop=True)
        conn.execute(
            "UPDATE product_alert_preferences SET target_last_fired_price = 894,"
            " target_above_target = 0, drop_baseline_price = 950,"
            " drop_baseline_context = 'ctx', drop_last_fired_price = 894"
            " WHERE product_id = ?", (product_id,),
        )
        alerts.update_preferences(conn, product_id, {
            "target_price": 850, "drop_enabled": True, "drop_min_change": 20,
        })
        prefs = conn.execute(
            "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product_id,)
        ).fetchone()
        assert (prefs["target_last_fired_price"], prefs["target_above_target"]) == (None, 1)
        assert (prefs["drop_baseline_price"], prefs["drop_baseline_context"],
                prefs["drop_last_fired_price"]) == (950, "ctx", 894)

        conn.execute(
            "UPDATE product_alert_preferences SET target_last_fired_price = 800,"
            " target_above_target = 0 WHERE product_id = ?", (product_id,),
        )
        alerts.update_preferences(conn, product_id, {
            "target_price": 850, "drop_enabled": True, "drop_min_change": 30,
        })
        prefs = conn.execute(
            "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product_id,)
        ).fetchone()
        assert (prefs["target_last_fired_price"], prefs["target_above_target"]) == (800, 0)
        assert (prefs["drop_baseline_price"], prefs["drop_baseline_context"],
                prefs["drop_last_fired_price"]) == (None, None, None)


def test_inferior_seller_price_change_does_not_trigger_the_product_drop_watch(seeded):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, drop=True)
        alerts.refresh_drop_baseline(conn, store.product_view(conn, product_id))
        product = store.get_product(conn, product_id)
        retailer = store.ensure_retailer(conn, "Dear Store")
        listing_id = store.create_listing(conn, {
            "product_id": product_id, "retailer_id": retailer["id"],
            "advertised_price": 1200, "freight": 0, "stock_status": "In Stock",
            "model_on_page": product["model"], "condition": "NEW",
        })
        for field in ("advertised_price", "freight", "stock_status", "model_on_page"):
            provenance.set_field(conn, listing_id, field,
                                 {"advertised_price": 1200, "freight": 0,
                                  "stock_status": "In Stock", "model_on_page": product["model"]}[field],
                                 state=provenance.MANUAL)
        for aspect in ("model", "price", "stock"):
            provenance.set_verification(conn, listing_id, aspect, provenance.VERIFIED)
        conn.commit()
        provenance.set_field(conn, listing_id, "advertised_price", 1100,
                             state=provenance.MANUAL)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert alerts.evaluate_product(conn, view) == []


def test_changed_postcode_reseeds_drop_baseline_without_alerting(seeded):
    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, drop=True)
        alerts.refresh_drop_baseline(conn, store.product_view(conn, product_id))
        conn.commit()
        store.update_household_settings(conn, {"postcode": "5000"})
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert alerts.evaluate_product(conn, view) == []
        assert conn.execute(
            "SELECT drop_baseline_price FROM product_alert_preferences WHERE product_id = ?",
            (product_id,),
        ).fetchone()[0] is None

        store.confirm_freight_for_household(conn, listing_id)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert alerts.evaluate_product(conn, view) == []


def test_changed_pack_and_new_discount_eligibility_reseed_baseline(seeded):
    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, drop=True)
        alerts.refresh_drop_baseline(conn, store.product_view(conn, product_id))
        store.update_product(conn, product_id, {"quantity": 2, "quantity_unit": "each"})
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert alerts.evaluate_product(conn, view) == []

        provenance.set_field(conn, listing_id, "conditional_requirements", "Member price",
                             state=provenance.MANUAL)
        provenance.set_field(conn, listing_id, "eligibility_confirmed", True,
                             state=provenance.MANUAL)
        provenance.set_field(conn, listing_id, "cashback", 100.0,
                             state=provenance.MANUAL)
        provenance.sync_verification_from_provenance(conn, listing_id)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        assert alerts.evaluate_product(conn, view) == []


def test_imported_freight_is_not_a_confirmed_alert_price(seeded):
    with connect(seeded) as conn:
        product_id, _ = _armed(conn, target=600)
        product = store.get_product(conn, product_id)
        retailer = store.ensure_retailer(conn, "Unconfirmed Freight Shop")
        listing_id = store.create_listing(conn, {
            "product_id": product_id, "retailer_id": retailer["id"],
            "advertised_price": 400, "freight": 0, "stock_status": "In Stock",
            "model_on_page": product["model"], "condition": "NEW",
        })
        for field, value in (("advertised_price", 400), ("freight", 0),
                             ("stock_status", "In Stock"),
                             ("model_on_page", product["model"])):
            provenance.set_field(conn, listing_id, field, value, state=provenance.IMPORTED)
        for aspect in ("model", "price", "stock"):
            provenance.set_verification(conn, listing_id, aspect, provenance.VERIFIED)
        provenance.sync_verification_from_provenance(conn, listing_id)
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []


def test_price_protection_coalesces_and_suppresses_preference_event(seeded):
    from datetime import UTC, datetime, timedelta

    with connect(seeded) as conn:
        product_id, listing_id = _armed(conn, target=900, drop=True)
        view = store.product_view(conn, product_id)
        alerts.refresh_drop_baseline(conn, view)
        conn.execute(
            "UPDATE product_alert_preferences SET drop_baseline_price = 950 WHERE product_id = ?",
            (product_id,),
        )
        store.record_purchase(conn, product_id, {
            "listing_id": listing_id, "price_paid": 1000,
            "price_protection_until": (datetime.now(UTC) + timedelta(days=14)).date().isoformat(),
        })
        view = store.product_view(conn, product_id)
        raised = alerts.evaluate_product(conn, view)
        assert [alert.rule_name for alert in raised] == ["Price protection"]
        alerts.record_fired(conn, raised[0])
        conn.commit()
        assert alerts.evaluate_product(conn, store.product_view(conn, product_id)) == []
