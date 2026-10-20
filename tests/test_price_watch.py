"""Watcher behaviour: failures are survivable, locks hold, nothing is invented."""

from __future__ import annotations

import pytest

from app import price_watch, provenance, retailers, store
from app.db import connect
from app.retailers.base import FetchError, Observation


@pytest.fixture()
def wired(seeded, monkeypatch):
    """Seeded board with URLs on every listing, so adapters are actually attempted."""
    with connect(seeded) as conn:
        rows = conn.execute(
            "SELECT l.id, r.adapter FROM listings l JOIN retailers r ON r.id = l.retailer_id"
        ).fetchall()
        for row in rows:
            adapter = retailers.get_adapter(row["adapter"])
            conn.execute("UPDATE listings SET url = ? WHERE id = ?",
                         (adapter.homepage + "/product", row["id"]))
        conn.commit()
    return seeded


def get(conn, retailer_name, field):
    row = conn.execute(
        "SELECT l.* FROM listings l JOIN retailers r ON r.id = l.retailer_id"
        " WHERE lower(r.name) = lower(?)",
        (retailer_name,),
    ).fetchone()
    return row[field]


def stub_adapters(monkeypatch, behaviour):
    """Replace every adapter's check() with a function of the adapter slug."""

    def fake_check(self, url, expected_model=None):
        return behaviour(self.slug)

    monkeypatch.setattr(retailers.base.RetailerAdapter, "check", fake_check, raising=True)


def test_successful_run_writes_live_values_and_history(wired, monkeypatch):
    stub_adapters(
        monkeypatch,
        lambda slug: Observation(advertised_price=1234.0, stock_status="In Stock",
                                 model_on_page="HW-Q930H/XY"),
    )
    summary = price_watch.run(send_alerts=False)
    assert summary["status"] == "ok"
    assert summary["checked"] == 5
    assert summary["errors"] == 0
    with connect(wired) as conn:
        assert get(conn, "JB Hi-Fi", "advertised_price") == 1234.0
        product = store.product_by_model(conn, "HW-Q930H/XY")
        history = store.price_history(conn, product["id"])
        assert len([h for h in history if h["source"] != "seed"]) == 5


def test_a_fetch_failure_never_destroys_the_last_good_value(wired, monkeypatch):
    with connect(wired) as conn:
        before = get(conn, "JB Hi-Fi", "advertised_price")
    assert before == 1699.0

    stub_adapters(monkeypatch, lambda slug: (_ for _ in ()).throw(FetchError("403: blocked")))
    summary = price_watch.run(send_alerts=False)

    assert summary["status"] == "failed", "every adapter failed, so the run must not report ok"
    assert summary["errors"] == 5
    with connect(wired) as conn:
        assert get(conn, "JB Hi-Fi", "advertised_price") == before


def test_one_retailer_failing_does_not_abort_the_others(wired, monkeypatch):
    def behaviour(slug):
        if slug == "jb_hifi":
            raise FetchError("403: blocked")
        if slug == "harvey_norman":
            raise RuntimeError("adapter bug")
        return Observation(advertised_price=999.0)

    stub_adapters(monkeypatch, behaviour)
    summary = price_watch.run(send_alerts=False)

    assert summary["errors"] == 2
    assert summary["ok"] == 3
    assert summary["status"] == "partial"
    with connect(wired) as conn:
        assert get(conn, "The Good Guys", "advertised_price") == 999.0
        assert get(conn, "JB Hi-Fi", "advertised_price") == 1699.0


def test_manual_override_survives_an_automated_run(wired, monkeypatch):
    with connect(wired) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(
            conn, listing["id"], "freight", 89.0, state=provenance.MANUAL, source="checkout"
        )
        conn.commit()

    stub_adapters(monkeypatch, lambda slug: Observation(advertised_price=869.0, freight=0.0))
    price_watch.run(send_alerts=False)

    with connect(wired) as conn:
        assert get(conn, "Crowdshop", "freight") == 89.0
        assert get(conn, "Crowdshop", "advertised_price") == 869.0


def test_a_listing_without_a_url_is_skipped_not_errored(seeded, monkeypatch):
    stub_adapters(monkeypatch, lambda slug: Observation(advertised_price=1.0))
    summary = price_watch.run(send_alerts=False)
    assert summary["skipped"] == 5
    assert summary["errors"] == 0
    assert summary["status"] == "ok"


def test_a_retailer_with_no_adapter_is_skipped(wired, monkeypatch):
    with connect(wired) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        retailer = store.ensure_retailer(conn, "Bing Lee")
        store.create_listing(
            conn,
            {"product_id": product["id"], "retailer_id": retailer["id"],
             "url": "https://example.invalid/bing"},
        )
        conn.commit()
    stub_adapters(monkeypatch, lambda slug: Observation(advertised_price=1.0))
    summary = price_watch.run(send_alerts=False)
    assert summary["skipped"] == 1
    assert summary["checked"] == 6


def test_a_model_mismatch_flags_verification(wired, monkeypatch):
    stub_adapters(
        monkeypatch,
        lambda slug: Observation(
            advertised_price=899.0, warnings=["model mismatch: page shows 'HW-Q990H/XY'"]
        ),
    )
    price_watch.run(send_alerts=False)
    with connect(wired) as conn:
        row = conn.execute(
            "SELECT l.id FROM listings l JOIN retailers r ON r.id = l.retailer_id"
            " WHERE r.adapter = 'jb_hifi'"
        ).fetchone()
        verification = provenance.verification_map(conn, row["id"])
        assert verification["model"]["status"] == provenance.FLAGGED


# ------------------------------------ a model flag must reflect the latest run


def _flag_model(db, retailer, note):
    with connect(db) as conn:
        row = conn.execute(
            "SELECT l.id FROM listings l JOIN retailers r ON r.id = l.retailer_id"
            " WHERE lower(r.name) = lower(?)", (retailer,)).fetchone()
        provenance.set_verification(conn, row["id"], "model", provenance.FLAGGED, note)
        conn.commit()
        return row["id"]


def _model_status(db, listing_id):
    with connect(db) as conn:
        return provenance.verification_map(conn, listing_id)["model"]["status"]


def test_a_matching_run_clears_a_stale_model_flag(wired, monkeypatch):
    """sync_verification_from_provenance never touches a FLAGGED aspect, so a listing
    flagged by the old shape-based comparison stayed red forever, which is exactly the
    outcome that fix existed to prevent. Same for any one-off bad reading."""
    listing_id = _flag_model(wired, "JB Hi-Fi", "stale flag from an old run")
    assert _model_status(wired, listing_id) == provenance.FLAGGED, "control: starts flagged"

    stub_adapters(monkeypatch, lambda slug: Observation(
        advertised_price=1699.0, model_on_page="HW-Q930H/XY", model_on_page_key="mpn"))
    price_watch.run(send_alerts=False)

    assert _model_status(wired, listing_id) != provenance.FLAGGED, (
        "a run that compared a manufacturer identifier and matched must clear it"
    )


def test_a_run_that_could_not_compare_leaves_the_flag_alone(wired, monkeypatch):
    """A page publishing only a merchant stock number is no evidence either way.
    Absence of a mismatch is not a clean bill of health (hardening rule 12)."""
    listing_id = _flag_model(wired, "JB Hi-Fi", "a real mismatch someone saw")

    stub_adapters(monkeypatch, lambda slug: Observation(
        advertised_price=1699.0, model_on_page="893039", model_on_page_key="sku"))
    price_watch.run(send_alerts=False)

    assert _model_status(wired, listing_id) == provenance.FLAGGED, (
        "no comparison happened, so there is nothing to clear it on"
    )


def test_the_flag_note_is_the_warning_that_caused_it(wired, monkeypatch):
    """The note used to be warnings[0] regardless. An adapter appending its own
    warning first would file unrelated text under the model mismatch fault tag."""
    obs = Observation(advertised_price=1699.0, model_on_page="HW-Q990H/XY",
                      model_on_page_key="mpn")
    obs.warnings = ["freight could not be read from this page",
                    "model mismatch: page shows 'HW-Q990H/XY', expected 'HW-Q930H/XY'"]
    stub_adapters(monkeypatch, lambda slug: obs)
    price_watch.run(send_alerts=False)

    with connect(wired) as conn:
        row = conn.execute(
            "SELECT l.id FROM listings l JOIN retailers r ON r.id = l.retailer_id"
            " WHERE lower(r.name) = 'jb hi-fi'").fetchone()
        note = provenance.verification_map(conn, row["id"])["model"]["note"]
    assert "model mismatch" in note, f"the note must explain the flag, got {note!r}"
