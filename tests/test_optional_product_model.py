import json

import pytest


@pytest.mark.parametrize("model", [None, "", "   "])
def test_product_can_be_created_without_a_model_and_remains_distinct(client, model):
    first = client.post("/api/products", json={"name": "Greek yoghurt", "model": model})
    assert first.status_code == 201
    assert first.json()["model"] == ""
    second = client.post("/api/products", json={"name": "Rolled oats"})
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    duplicate = client.post("/api/products", json={"name": "greek yoghurt"})
    assert duplicate.status_code == 409
    check = client.get("/api/products/check-model", params={"name": "greek yoghurt"}).json()
    assert check["duplicate_of"]["id"] == first.json()["id"]
    variant = client.post("/api/products", json={"name": "Greek yoghurt", "variant": "500 g"})
    assert variant.status_code == 201
    assert "Model (optional)" in client.get("/").text


def test_existing_model_can_be_cleared_without_affecting_other_products(client):
    product = client.post("/api/products", json={"name": "Milk", "model": "TEMP-1"}).json()
    response = client.patch(f"/api/products/{product['id']}", json={"model": None})
    assert response.status_code == 200
    assert response.json()["model"] == ""
    assert client.get("/api/products/check-model", params={"model": ""}).json()["duplicate_of"] is None


def test_model_less_snapshot_round_trip_preserves_separate_products(conn):
    from app import ingest, store

    for name in ("Greek yoghurt", "Rolled oats"):
        store.create_product(conn, {"name": name})
    snapshot = ingest.export_all(conn)
    report = ingest.import_snapshot(conn, snapshot)
    assert report["errors"] == []
    assert len(ingest.export_all(conn)["products"]) == 2


def test_model_less_research_finding_requires_explicit_product_scope(conn):
    from app import ingest, store

    first = store.create_product(conn, {"name": "Greek yoghurt"})
    second = store.create_product(conn, {"name": "Rolled oats"})
    finding = {"product_id": second, "retailer": "Example Shop", "price": 5}
    result = ingest.import_finding(conn, finding)
    listing = conn.execute("SELECT product_id FROM listings WHERE id = ?",
                           (result["listing_id"],)).fetchone()
    assert listing["product_id"] == second
    assert listing["product_id"] != first
    with pytest.raises(ingest.ImportError_, match="positive integer"):
        ingest.import_finding(conn, {**finding, "product_id": [second]})
    with pytest.raises(ingest.ImportError_, match="model or product_id"):
        ingest.import_finding(conn, {"retailer": "Example Shop", "price": 4})


def test_search_keeps_sourced_product_candidates_without_model_numbers(client):
    job = client.post("/api/llm-jobs", json={"query": "Greek yoghurt"}).json()
    candidate = {"label": "Greek yoghurt", "model": None, "variant": "500 g",
                 "source": "https://shop.example/yoghurt"}
    result = client.post(f"/api/llm-jobs/{job['id']}/complete", json={
        "status": "DONE", "result": json.dumps({"candidates": [candidate]}),
    })
    assert result.status_code == 200
    saved = json.loads(result.json()["result"])["candidates"][0]
    assert saved["model"] == ""
    assert saved["label"] == "Greek yoghurt"
    assert saved["variant"] == "500 g"


def test_model_less_supported_url_is_manual_and_never_scraped(client, monkeypatch):
    from app import price_watch
    from app.retailers.base import RetailerAdapter

    monkeypatch.setattr(RetailerAdapter, "check",
                        lambda *args, **kwargs: pytest.fail("cannot verify a missing model"))
    product = client.post("/api/products", json={"name": "Milk"}).json()
    response = client.post(f"/api/products/{product['id']}/retailers/from-url",
                           json={"url": "https://www.jbhifi.com.au/products/milk"})
    assert response.status_code == 201
    assert response.json()["scraped"] is False
    assert response.json()["research_job"] is None
    refreshed = client.get(f"/api/products/{product['id']}").json()
    assert refreshed["listings"][0]["check_capability"] == "MANUAL"
    summary = price_watch.run(product_id=product["id"], send_alerts=False)
    assert summary["skipped"] == 1


def test_model_less_archive_does_not_fetch_or_invent_matching_evidence(monkeypatch):
    from app import wayback

    monkeypatch.setattr(wayback, "_captured_rows",
                        lambda *args: pytest.fail("cannot verify archived model identity"))
    result = wayback.lookup_listing("https://shop.example/yoghurt", "")
    assert result["status"] == "no_usable_price"
    assert result["snapshots"] == []
    assert "model number" in result["note"]
