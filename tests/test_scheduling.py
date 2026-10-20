import multiprocessing
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import schedules, store


def test_due_dates_follow_household_local_calendar_boundaries(conn):
    zone = ZoneInfo("Australia/Brisbane")
    checked = datetime(2026, 1, 31, 9, 0, tzinfo=zone)

    assert schedules.next_due(checked, "monthly", zone).isoformat() == (
        "2026-02-28T09:00:00+10:00"
    )
    assert schedules.next_due(checked, "twice_weekly", zone).isoformat() == (
        "2026-02-02T09:00:00+10:00"
    )
    sydney = ZoneInfo("Australia/Sydney")
    before_daylight_change = datetime(2026, 4, 4, 9, 0, tzinfo=sydney)
    assert schedules.next_due(
        before_daylight_change, "daily", sydney
    ).isoformat() == "2026-04-05T09:00:00+10:00"


def test_household_timezone_is_saved_and_invalid_timezone_rejected(conn):
    assert store.household_settings(conn)["timezone"] == "Australia/Brisbane"
    assert store.update_household_settings(
        conn, {"postcode": None, "pickup_radius_km": 25, "timezone": "Pacific/Auckland"}
    )["timezone"] == "Pacific/Auckland"

    try:
        store.update_household_settings(
            conn, {"postcode": None, "pickup_radius_km": 25, "timezone": "Mars/Olympus"}
        )
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("invalid timezone was accepted")


def test_due_products_catch_up_once_and_keep_success_state(conn, seeded, monkeypatch):
    from datetime import UTC

    from app.db import connect

    product_id = conn.execute("SELECT id FROM products LIMIT 1").fetchone()["id"]
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    conn.execute(
        "UPDATE product_check_schedules SET next_due_at = '2026-01-01T00:00:00Z',"
        " locked_until = '2026-02-28T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    conn.commit()
    calls = []
    nested = []

    def completed(**kwargs):
        calls.append(kwargs["product_id"])
        nested.append(schedules.run_due(now + timedelta(minutes=31))["checked_products"])
        return {"errors": 0, "checked": 1, "status": "ok"}

    monkeypatch.setattr(schedules.price_watch, "run", completed)
    first = schedules.run_due(now)
    second = schedules.run_due(now)

    assert first["checked_products"] == 1
    assert second["checked_products"] == 0
    assert calls == [product_id]
    assert nested == [0], "the process lock prevents overlap after the database lease expires"
    with connect(seeded) as read:
        state = read.execute(
            "SELECT last_attempt_at, last_success_at, next_due_at, failures"
            " FROM product_check_schedules WHERE product_id = ?", (product_id,)
        ).fetchone()
    assert state["last_attempt_at"] == "2026-03-01T00:00:00Z"
    assert state["last_success_at"] == "2026-03-01T00:00:00Z"
    assert state["next_due_at"] > state["last_attempt_at"]
    assert state["failures"] == 0


def _try_product_lock(product_id, result):
    with schedules._product_lock(product_id) as acquired:
        result.put(acquired)


def _can_acquire_product_lock(product_id):
    with schedules._product_lock(product_id) as acquired:
        return acquired


def test_product_lock_blocks_threads_and_processes(conn, seeded):
    product_id = conn.execute("SELECT id FROM products LIMIT 1").fetchone()["id"]
    with schedules._product_lock(product_id) as acquired:
        assert acquired
        thread_result = []
        thread = threading.Thread(target=lambda: thread_result.append(
            _can_acquire_product_lock(product_id)
        ))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert thread_result == [False]

        context = multiprocessing.get_context("spawn")
        result = context.Queue()
        process = context.Process(target=_try_product_lock, args=(product_id, result))
        process.start()
        assert result.get(timeout=5) is False
        process.join(timeout=5)
        assert process.exitcode == 0


def test_price_watch_cli_uses_schedule_locks_and_preserves_options(monkeypatch, capsys):
    from app import price_watch

    calls = []
    monkeypatch.setattr(schedules, "check_now", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"status": "ok", "run_id": 9, "checked": 1,
                                        "ok": 1, "unresolved": 0, "skipped": 0,
                                        "errors": 0, "alerts": [],
                                        "delivery_failures": []}
    ))
    monkeypatch.setattr(schedules, "check_all_now", lambda **kwargs: (
        calls.append(((), kwargs)) or {"status": "ok", "run_id": 10, "checked": 2,
                                       "ok": 2, "unresolved": 0, "skipped": 0,
                                       "errors": 0, "alerts": [],
                                       "delivery_failures": []}
    ))
    monkeypatch.setattr(price_watch, "migrate", lambda: None)

    assert price_watch.main([
        "--product", "12", "--no-alerts", "--trigger", "batch", "--json"
    ]) == 0
    assert '"run_id": 9' in capsys.readouterr().out
    assert calls.pop(0) == ((12,), {"trigger": "batch", "send_alerts": False})

    assert price_watch.main(["--no-alerts", "--trigger", "batch", "--json"]) == 0
    assert '"run_id": 10' in capsys.readouterr().out
    assert calls.pop(0) == ((), {"trigger": "batch", "send_alerts": False})


def test_scheduled_failures_use_bounded_retries_and_retain_error(conn, seeded, monkeypatch):
    from datetime import UTC, timedelta

    product_id = conn.execute("SELECT id FROM products LIMIT 1").fetchone()["id"]
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    conn.execute(
        "UPDATE product_check_schedules SET next_due_at = '2026-01-01T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    conn.commit()
    monkeypatch.setattr(schedules.price_watch, "run", lambda **_: {
        "errors": 1, "status": "failed", "error": "retailer timeout"
    })

    schedules.run_due(now)
    row = conn.execute(
        "SELECT failures, last_error, next_due_at FROM product_check_schedules"
        " WHERE product_id = ?", (product_id,)
    ).fetchone()
    assert row["failures"] == 1
    assert row["last_error"] == "retailer timeout"
    assert row["next_due_at"] == "2026-03-01T01:00:00Z"

    now += timedelta(hours=1)
    schedules.run_due(now)
    now += timedelta(hours=6)
    schedules.run_due(now)
    now += timedelta(hours=24)
    schedules.run_due(now)
    row = conn.execute(
        "SELECT failures, next_due_at FROM product_check_schedules WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    assert row["failures"] == 4
    assert row["next_due_at"] == "2026-03-09T07:00:00Z"


def test_product_page_and_api_expose_schedule_and_retain_it_when_product_paused(client):
    product = client.get("/api/products").json()[0]
    page = client.get(f"/products/{product['id']}").text
    assert "Last attempt never" in page
    assert 'value="twice_weekly"' in page

    saved = client.patch(
        f"/api/products/{product['id']}/check-schedule", json={"cadence": "monthly"}
    ).json()
    client.patch(f"/api/products/{product['id']}", json={"status": "PARKED"})
    paused = client.get(f"/api/products/{product['id']}").json()["check_schedule"]
    assert paused["cadence"] == "monthly" and paused["next_due_at"] == saved["next_due_at"]
    client.patch(f"/api/products/{product['id']}", json={"status": "ACTIVE"})
    resumed = client.get(f"/api/products/{product['id']}").json()["check_schedule"]
    assert resumed["cadence"] == "monthly" and resumed["next_due_at"] == saved["next_due_at"]


def test_household_timezone_api_and_settings_form(client):
    response = client.patch(
        "/api/household-settings", json={"postcode": None, "pickup_radius_km": 25,
                                        "timezone": "Australia/Perth"}
    )
    assert response.json()["timezone"] == "Australia/Perth"
    assert "Australia/Perth" in client.get("/settings").text
    assert client.patch(
        "/api/household-settings", json={"postcode": None, "pickup_radius_km": 25,
                                         "timezone": "No/Such_Zone"}
    ).status_code == 400


def test_scheduler_includes_purchased_but_skips_parked_and_archived(
    conn, seeded, monkeypatch
):
    from datetime import UTC

    product_id = conn.execute("SELECT id FROM products LIMIT 1").fetchone()["id"]
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    conn.execute("UPDATE products SET status = 'PURCHASED' WHERE id = ?", (product_id,))
    conn.execute(
        "UPDATE product_check_schedules SET next_due_at = '2026-01-01T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    conn.commit()
    seen = []
    monkeypatch.setattr(schedules.price_watch, "run", lambda **kw: (
        seen.append(kw["product_id"]) or {"errors": 0, "checked": 1, "status": "ok"}
    ))

    assert schedules.run_due(now)["checked_products"] == 1
    conn.execute("UPDATE products SET status = 'PARKED' WHERE id = ?", (product_id,))
    conn.execute(
        "UPDATE product_check_schedules SET next_due_at = '2026-01-01T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    conn.commit()
    assert schedules.run_due(now)["checked_products"] == 0
    conn.execute("UPDATE products SET status = 'ACTIVE' WHERE id = ?", (product_id,))
    conn.execute("UPDATE products SET archived = 1 WHERE id = ?", (product_id,))
    conn.commit()
    assert schedules.run_due(now)["checked_products"] == 0
    conn.execute("UPDATE products SET archived = 0 WHERE id = ?", (product_id,))
    assert seen == [product_id]


def test_skipped_only_run_does_not_claim_a_success(conn, seeded, monkeypatch):
    from datetime import UTC

    product_id = conn.execute("SELECT id FROM products LIMIT 1").fetchone()["id"]
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    conn.execute(
        "UPDATE product_check_schedules SET next_due_at = '2026-01-01T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    conn.commit()
    monkeypatch.setattr(schedules.price_watch, "run", lambda **_: {
        "errors": 0, "checked": 5, "skipped": 5, "status": "ok"
    })

    schedules.run_due(now)
    schedule = conn.execute(
        "SELECT last_success_at, last_error FROM product_check_schedules WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    assert schedule["last_success_at"] is None
    assert schedule["last_error"] == "No retailer listings were eligible for an automatic check."
