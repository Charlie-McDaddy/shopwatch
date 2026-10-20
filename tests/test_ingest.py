"""Import merge behaviour, model matching and snapshot round trips."""

from __future__ import annotations

import pytest

from app import ingest, provenance, store
from app.db import connect


def get(conn, listing_id, field):
    return conn.execute(f"SELECT {field} FROM listings WHERE id = ?", (listing_id,)).fetchone()[0]


def test_model_normalisation_matches_punctuation_and_case_only():
    assert store.normalise_model("HW-Q930H/XY") == store.normalise_model("hw q930h/xy")
    assert store.normalise_model("HW-Q930H/XY") == store.normalise_model("HWQ930H/XY")
    assert store.normalise_model("HW-Q930H/XY") != store.normalise_model("HW-Q990H/XY")
    assert store.normalise_model("HW-Q930H/XY") != store.normalise_model("HW-Q930H/XU"), (
        "region suffix is genuinely different stock"
    )


def test_import_matches_an_existing_product_by_model(seeded):
    with connect(seeded) as conn:
        report = ingest.import_finding(
            conn,
            {
                "model": "hw-q930h/xy",
                "retailer": "JB Hi-Fi",
                "price": 1599,
                "stock": "Available",
                "condition": "New",
            },
        )
        assert report["listing_created"] is False, "JB Hi-Fi is already on the seeded board"
        assert get(conn, report["listing_id"], "advertised_price") == 1599


def test_import_creates_a_listing_for_a_new_retailer(seeded):
    with connect(seeded) as conn:
        report = ingest.import_finding(
            conn, {"model": "HW-Q930H/XY", "retailer": "Bing Lee", "price": 1499}
        )
        assert report["listing_created"] is True
        assert report["retailer"] == "Bing Lee"


def test_import_refuses_an_unknown_model_rather_than_guessing(seeded):
    with connect(seeded) as conn:
        with pytest.raises(ingest.ImportError_):
            ingest.import_finding(conn, {"model": "HW-Q990H/XY", "retailer": "JB Hi-Fi", "price": 1})


def test_import_preserves_a_manual_override(seeded):
    """The headline behaviour: automated data must not clobber hand-confirmed values."""
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(
            conn, listing["id"], "freight", 89.0, state=provenance.MANUAL, source="phone call"
        )
        conn.commit()

        report = ingest.import_finding(
            conn,
            {"model": "HW-Q930H/XY", "retailer": "Crowdshop", "price": 879, "freight": 0,
             "stock": "In Stock"},
        )
        assert "freight" in report["preserved_manual"]
        assert get(conn, listing["id"], "freight") == 89.0
        assert get(conn, listing["id"], "advertised_price") == 879, "unlocked fields still update"


def test_import_records_history_and_last_checked(seeded):
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        before = len(store.price_history(conn, product["id"]))
        ingest.import_finding(
            conn,
            {"model": "HW-Q930H/XY", "retailer": "JB Hi-Fi", "price": 1599,
             "checked_at": "2026-09-11T02:00:00Z"},
        )
        after = store.price_history(conn, product["id"])
        assert len(after) == before + 1
        assert after[0]["advertised_price"] == 1599


def test_import_applies_verification_flags(seeded):
    with connect(seeded) as conn:
        report = ingest.import_finding(
            conn,
            {"model": "HW-Q930H/XY", "retailer": "JB Hi-Fi", "price": 1699,
             "verification": {"model": True, "price": True, "stock": False}},
        )
        verification = provenance.verification_map(conn, report["listing_id"])
        assert verification["model"]["status"] == provenance.VERIFIED
        assert verification["stock"]["status"] == provenance.UNVERIFIED


def test_batch_import_reports_failures_without_losing_the_good_rows(seeded):
    with connect(seeded) as conn:
        result = ingest.import_findings(
            conn,
            [
                {"model": "HW-Q930H/XY", "retailer": "JB Hi-Fi", "price": 1599},
                {"retailer": "JB Hi-Fi", "price": 1599},
                {"model": "NOT-A-MODEL", "retailer": "JB Hi-Fi", "price": 1},
            ],
        )
        assert result["applied"] == 1
        assert result["failed"] == 2


def test_snapshot_round_trip_into_an_empty_database(seeded, tmp_path, monkeypatch):
    with connect(seeded) as conn:
        store.update_household_settings(conn, {"postcode": "4000", "pickup_radius_km": 40})
        snapshot = ingest.export_all(conn)
    assert snapshot["format"] == "shopwatch-snapshot"
    assert snapshot["household_settings"] == {"postcode": "4000", "pickup_radius_km": 40}
    assert any(p["model"] == "HW-Q930H/XY" for p in snapshot["products"])

    from app.db import migrate

    target = tmp_path / "restored.db"
    monkeypatch.setenv("SHOPWATCH_DB", str(target))
    migrate(target)
    with connect(target) as conn:
        report = ingest.import_snapshot(conn, snapshot)
        conn.commit()
        product = store.product_by_model(conn, "HW-Q930H/XY")
        assert product is not None
        view = store.product_view(conn, product["id"])
        assert report["products"] == 1
        assert store.household_settings(conn) == {"postcode": "4000", "pickup_radius_km": 40}
        assert len(view["listings"]) == 5
        assert view["trigger_price"] == 900


def test_snapshot_import_carries_the_lock_with_the_value(seeded, tmp_path, monkeypatch):
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(conn, listing["id"], "freight", 89.0, state=provenance.MANUAL)
        conn.commit()
        snapshot = ingest.export_all(conn)

    from app.db import migrate

    target = tmp_path / "restored.db"
    monkeypatch.setenv("SHOPWATCH_DB", str(target))
    migrate(target)
    with connect(target) as conn:
        ingest.import_snapshot(conn, snapshot)
        conn.commit()
        product = store.product_by_model(conn, "HW-Q930H/XY")
        restored = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        prov = provenance.provenance_map(conn, restored["id"])["freight"]
        assert prov["manual_locked"] == 1
        assert get(conn, restored["id"], "freight") == 89.0


def test_local_manual_lock_beats_an_incoming_snapshot_value(seeded, tmp_path, monkeypatch):
    with connect(seeded) as conn:
        snapshot = ingest.export_all(conn)
        for listing in snapshot["listings"]:
            listing["advertised_price"] = 1.0

        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(
            conn, listing["id"], "advertised_price", 869.0, state=provenance.MANUAL
        )
        conn.commit()

        report = ingest.import_snapshot(conn, snapshot)
        assert report["preserved_manual"] >= 1
        assert get(conn, listing["id"], "advertised_price") == 869.0


def test_csv_export_has_one_row_per_listing(seeded):
    with connect(seeded) as conn:
        csv_text = ingest.csv_export(conn)
    lines = [line for line in csv_text.splitlines() if line.strip()]
    assert len(lines) == 6, "header plus the five seeded listings"
    assert "HW-Q930H/XY" in csv_text
    assert "promo_ends" in lines[0]


def test_csv_export_includes_a_set_promo_end_date(seeded):
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(
            conn, listing["id"], "price_valid_until", "2026-09-21", state=provenance.MANUAL
        )
        conn.commit()
        csv_text = ingest.csv_export(conn)
    assert "2026-09-21" in csv_text


def test_import_finding_ignores_a_promo_end_date_deliberately(seeded):
    """Issue #97 keeps the promo end-date manual-entry only: the research-job pipeline
    (which lands here, through import_finding/FINDING_FIELD_MAP) must not be able to
    set it just because a finding happens to carry that key."""
    with connect(seeded) as conn:
        report = ingest.import_finding(
            conn,
            {"model": "HW-Q930H/XY", "retailer": "Crowdshop", "price": 700,
             "price_valid_until": "2026-09-21"},
        )
        assert "price_valid_until" not in report["updated"]
        assert get(conn, report["listing_id"], "price_valid_until") is None


def test_import_stores_hostile_text_verbatim_so_the_view_layer_must_escape(seeded):
    """Pins the contract between the layers.

    Findings are pasted in from wherever research came from, so retailer names and source
    labels are untrusted text. The store deliberately does NOT strip or sanitise them:
    mangling a retailer's real name to dodge a rendering bug is the wrong fix, and a
    half-sanitised value is worse than an honest one. Escaping is the renderer's job,
    which is why the templates autoescape and app.js builds nodes rather than markup.
    """
    payload = '<img src=x onerror="alert(1)">Evil Co'
    with connect(seeded) as conn:
        report = ingest.import_finding(
            conn,
            {"model": "HW-Q930H/XY", "retailer": payload, "price": 700, "freight": 0,
             "source": "<script>alert(2)</script>"},
        )
        assert report["retailer"] == payload, "stored exactly as given, not silently rewritten"
        prov = provenance.provenance_map(conn, report["listing_id"])["advertised_price"]
        assert prov["source"] == "<script>alert(2)</script>"
