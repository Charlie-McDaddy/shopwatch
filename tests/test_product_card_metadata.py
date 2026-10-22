from __future__ import annotations


def test_product_metadata_persists_and_renders_on_the_watchlist(client):
    response = client.post("/api/products", json={
        "name": "Mixer",
        "model": "MX-42/AU",
        "variant": "2 L, stainless steel",
        "description": "Two litre stand mixer",
        "image_url": "https://images.example/mx-42.jpg",
    })
    assert response.status_code == 201
    product_id = response.json()["id"]

    reloaded = client.get(f"/api/products/{product_id}").json()
    assert reloaded["variant"] == "2 L, stainless steel"
    assert reloaded["description"] == "Two litre stand mixer"
    assert reloaded["image_url"] == "https://images.example/mx-42.jpg"

    card = client.get("/").text
    assert "Two litre stand mixer" in card
    assert "2 L, stainless steel" in card
    assert 'src="https://images.example/mx-42.jpg"' in card


def test_missing_card_metadata_stays_missing(client):
    response = client.post("/api/products", json={"name": "Mixer", "model": "MX-43/AU"})
    assert response.status_code == 201
    product = response.json()
    assert product["image_url"] is None
    assert product["description"] is None
    assert product["variant"] is None


def test_product_image_url_rejects_non_public_or_non_https_urls(client):
    for image_url in ("http://images.example/item.jpg", "https://127.0.0.1/item.jpg", "https://localhost/item.jpg"):
        response = client.post("/api/products", json={
            "name": "Mixer", "model": f"MX-{len(image_url)}/AU", "image_url": image_url,
        })
        assert response.status_code == 400


def test_exact_model_duplicate_is_rejected_but_regional_variant_is_allowed(client):
    first = client.post("/api/products", json={"name": "Soundbar", "model": "ZX-Q930H/XY"})
    assert first.status_code == 201
    duplicate = client.post("/api/products", json={"name": "Soundbar", "model": "zx q930h/xy"})
    assert duplicate.status_code == 409
    regional = client.post("/api/products", json={"name": "Soundbar", "model": "ZX-Q930H/XX"})
    assert regional.status_code == 201


def test_card_metadata_survives_snapshot_import(db, tmp_path):
    from app import ingest, store
    from app.db import connect, migrate

    source = connect(db)
    try:
        product_id = store.create_product(source, {
            "name": "Mixer", "model": "SNAP-MX-42", "variant": "2 L",
            "description": "Stainless steel bowl", "image_url": "https://images.example/mixer.jpg",
        })
        snapshot = ingest.export_all(source)
    finally:
        source.close()

    target_path = tmp_path / "restored.db"
    migrate(target_path)
    target = connect(target_path)
    try:
        ingest.import_snapshot(target, snapshot)
        restored = store.get_product(target, product_id)
        assert restored["variant"] == "2 L"
        assert restored["description"] == "Stainless steel bowl"
        assert restored["image_url"] == "https://images.example/mixer.jpg"
    finally:
        target.close()
