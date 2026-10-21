"""Product seller discovery through HTTP, rendering and a controlled scheduler clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


def add_product(client, name="Soundbar", model="HW-Q9999H/XY"):
    response = client.post("/api/products", json={"name": name, "model": model})
    assert response.status_code == 201
    return response.json()


def candidate(model="HW-Q9999H/XY"):
    return {
        "name": "Example Store", "homepage": "https://example.com.au",
        "source_url": "https://example.com.au/" + model.lower().replace("/", "-"),
        "source_title": f"Samsung {model} Soundbar",
    }


def complete(client, job_id, retailers=None, model="HW-Q9999H/XY"):
    result = json.dumps({"retailers": retailers if retailers is not None else [candidate(model)]})
    response = client.post(
        f"/api/retailer-search-jobs/{job_id}/complete",
        json={"status": "DONE", "result": result},
    )
    assert response.status_code == 200
    return response.json()


def test_product_creation_discovers_exact_variant_and_renders_unpriced_source(client):
    product = add_product(client)
    job = product["discovery_job"]
    assert job["status"] == "QUEUED"
    assert job["trigger"] == "initial"
    assert json.loads(client.get(f"/api/retailer-search-jobs/{job['id']}").json()["query"])["model"] == "HW-Q9999H/XY"

    completed = complete(client, job["id"])
    assert json.loads(completed["result"])["retailers"][0]["source_url"] == candidate()["source_url"]
    product = client.get(f"/api/products/{product['id']}").json()
    listing = next(item for item in product["listings"] if item["url"] == candidate()["source_url"])
    assert listing["advertised_price"] is None
    assert listing["provenance"]["advertised_price"]["manual_locked"] == 0

    page = client.get(f"/products/{product['id']}").text
    assert candidate()["source_url"] in page
    assert candidate()["source_title"] in page
    assert "Find more shops" in page


def test_manual_and_repeated_discovery_preserve_a_locked_price_and_deduplicate(client):
    product = add_product(client, "Soundbar manual", "HW-Q930H/M")
    complete(client, product["discovery_job"]["id"], model="HW-Q930H/M")
    listing = next(item for item in client.get(f"/api/products/{product['id']}").json()["listings"]
                   if item["url"] == candidate("HW-Q930H/M")["source_url"])
    client.patch(f"/api/retailers/{listing['id']}", json={"advertised_price": 123.45})

    for _ in range(2):
        queued = client.post(f"/api/products/{product['id']}/discover-retailers")
        assert queued.status_code == 202
        result = complete(client, queued.json()["id"], model="HW-Q930H/M")
        assert result["status"] == "DONE"

    saved = client.get(f"/api/products/{product['id']}").json()
    matching = [item for item in saved["listings"]
                if item["url"] == candidate("HW-Q930H/M")["source_url"]]
    assert len(matching) == 1
    assert matching[0]["advertised_price"] == 123.45
    assert matching[0]["provenance"]["advertised_price"]["manual_locked"] == 1


def test_late_initial_discovery_does_not_attach_results_to_paused_or_archived_product(client):
    for suffix, status_change in (("P", {"status": "PARKED"}), ("A", {"archived": 1})):
        model = f"HW-Q930H/{suffix}"
        product = add_product(client, "Paused soundbar", model)
        updated = client.patch(f"/api/products/{product['id']}", json=status_change)
        assert updated.status_code == 200
        job = complete(client, product["discovery_job"]["id"], model=model)
        assert job["status"] == "FAILED"
        assert "inactive" in job["error"]
        assert client.get(f"/api/products/{product['id']}").json()["listings"] == []


def test_parked_or_archived_product_creation_skips_automatic_discovery(client):
    for model, extra in (("HW-Q9999H/P", {"status": "PARKED"}),
                         ("HW-Q9999H/A", {"archived": 1})):
        response = client.post("/api/products", json={"name": "Inactive soundbar", "model": model, **extra})
        assert response.status_code == 201
        assert response.json()["discovery_job"] is None


def test_initial_result_reuses_retailer_added_after_the_search_was_queued(client, conn):
    from app import store

    product = add_product(client, "Already selected", "HW-Q9999H/D")
    retailer = store.ensure_retailer(conn, "Example Store", homepage="https://example.com.au")
    store.create_listing(conn, {
        "product_id": product["id"], "retailer_id": retailer["id"],
        "url": "https://example.com.au/hand-picked",
    })
    conn.commit()

    completed = complete(client, product["discovery_job"]["id"], model="HW-Q9999H/D")

    assert json.loads(completed["result"])["retailers"] == []
    listings = client.get(f"/api/products/{product['id']}").json()["listings"]
    assert [row["url"] for row in listings] == ["https://example.com.au/hand-picked"]


def test_marketplace_candidate_stays_a_review_suggestion_without_seller_identity(client):
    product = add_product(client, "Marketplace model", "HW-Q9999H/A")
    result = complete(client, product["discovery_job"]["id"], model="HW-Q9999H/A", retailers=[{
        "name": "Amazon", "homepage": "https://amazon.com.au",
        "source_url": "https://www.amazon.com.au/dp/B123", "source_title": "HW-Q9999H/A Soundbar",
    }])

    assert result["status"] == "DONE"
    assert json.loads(result["result"])["retailers"][0]["needs_review"] is True
    assert client.get(f"/api/products/{product['id']}").json()["listings"] == []


def test_candidates_for_the_same_store_domain_are_coalesced(client):
    product = add_product(client, "Same store", "HW-Q9999H/S")
    rows = [
        {**candidate("HW-Q9999H/S"), "source_title": "HW-Q9999H/S Soundbar | Store One"},
        {**candidate("HW-Q9999H/S"), "name": "Store Two",
         "source_url": "https://www.example.com.au/second",
         "source_title": "HW-Q9999H/S Soundbar | Store Two"},
    ]

    result = complete(client, product["discovery_job"]["id"], retailers=rows)

    assert len(json.loads(result["result"])["retailers"]) == 1
    assert len(client.get(f"/api/products/{product['id']}").json()["listings"]) == 1


@pytest.mark.parametrize("bad_result", ["not-json", "[]", "{}"])
def test_invalid_product_discovery_result_fails_the_job(client, bad_result):
    product = add_product(client, "Malformed result", f"HW-Q9999H/{len(bad_result)}")
    response = client.post(
        f"/api/retailer-search-jobs/{product['discovery_job']['id']}/complete",
        json={"status": "DONE", "result": bad_result},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["result"] is None
    assert "invalid retailer discovery result" in response.json()["error"]


def test_weekly_discovery_uses_its_own_due_date_not_the_price_check_cadence(
    client, conn, monkeypatch,
):
    from app import schedules

    product = add_product(client, "Weekly soundbar", "HW-Q930H/W")
    complete(client, product["discovery_job"]["id"], retailers=[])
    conn.execute(
        "UPDATE product_check_schedules SET cadence = 'monthly', next_due_at = '2027-01-01T00:00:00Z'"
        " WHERE product_id = ?", (product["id"],)
    )
    conn.execute(
        "UPDATE product_discovery_schedules SET next_due_at = '2026-10-20T00:00:00Z'"
        " WHERE product_id = ?", (product["id"],)
    )
    conn.commit()
    monkeypatch.setattr(schedules.price_watch, "run", lambda **_: {
        "checked": 0, "skipped": 0, "errors": 0, "status": "ok",
    })

    schedules.run_due(datetime(2026, 10, 21, tzinfo=UTC))

    updated = client.get(f"/api/products/{product['id']}").json()
    assert updated["discovery_job"]["trigger"] == "weekly"
    assert updated["discovery_job"]["status"] == "QUEUED"
    assert updated["discovery_schedule"]["next_due_at"] == "2026-10-28T00:00:00Z"
    assert updated["check_schedule"]["cadence"] == "monthly"
