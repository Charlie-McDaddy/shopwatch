"""Watch preferences survive a portable snapshot without copying running jobs."""

import pytest

from app import alerts, ingest, store


def test_snapshot_preserves_alert_preferences_and_discovery_dates(conn):
    product_id = store.create_product(conn, {"name": "Protein 1 kg", "model": "P-1KG"})
    conn.execute(
        "INSERT INTO product_alert_preferences"
        " (product_id, target_price, drop_enabled, drop_min_change, target_last_fired_price,"
        " target_above_target) VALUES (?, 45, 1, 5, 40, 0)",
        (product_id,),
    )
    conn.execute(
        "UPDATE product_discovery_schedules SET next_due_at = '2026-11-01T00:00:00Z',"
        " last_attempt_at = '2026-10-25T00:00:00Z' WHERE product_id = ?", (product_id,),
    )
    snapshot = ingest.export_all(conn)
    store.delete_product(conn, product_id)
    report = ingest.import_snapshot(conn, snapshot)
    assert report["errors"] == []
    restored = ingest.export_all(conn)
    assert restored["product_alert_preferences"][0]["target_price"] == 45
    assert restored["product_alert_preferences"][0]["drop_enabled"] == 1
    assert restored["product_alert_preferences"][0]["drop_min_change"] == 5
    assert restored["product_alert_preferences"][0]["target_last_fired_price"] == 40
    assert restored["product_alert_preferences"][0]["target_above_target"] == 0
    assert restored["product_discovery_schedules"][0]["next_due_at"] == "2026-11-01T00:00:00Z"
    assert restored["product_discovery_schedules"][0]["last_attempt_at"] == "2026-10-25T00:00:00Z"


@pytest.mark.parametrize("values", [
    {"target_price": float("nan")}, {"drop_min_change": -1}, {"drop_enabled": "yes"},
])
def test_snapshot_rejects_invalid_alert_preferences(conn, values):
    store.create_product(conn, {"name": "Protein", "model": "P-1KG"})
    snapshot = ingest.export_all(conn)
    snapshot["product_alert_preferences"] = [
        {"product_id": snapshot["products"][0]["id"], **values}
    ]
    report = ingest.import_snapshot(conn, snapshot)
    assert any("product_alert_preferences" in error for error in report["errors"])
    assert ingest.export_all(conn)["product_alert_preferences"] == []


def test_snapshot_retains_in_app_alert_history(conn):
    pid = store.create_product(conn, {"name": "Protein", "model": "P-1KG"})
    retailer = store.ensure_retailer(conn, "Example shop")
    lid = store.create_listing(conn, {"product_id": pid, "retailer_id": retailer["id"]})
    alert = alerts.Alert(
        product_name="Protein", model="P-1KG", retailer="Example shop", delivered=40,
        classification="EXCELLENT", condition="NEW", rule_id=pid, rule_name="Target price",
        url=None, listing_id=lid, reason="Target reached", preference_kind="target",
    )
    alerts.dispatch(conn, [alert])
    snapshot = ingest.export_all(conn)
    store.delete_product(conn, pid)
    assert ingest.import_snapshot(conn, snapshot)["errors"] == []
    history = alerts.get_alert_history(conn)
    assert len(history) == 1
    assert history[0]["delivered_price"] == 40
    assert history[0]["delivery_status"] == "not_sent"
