from __future__ import annotations

import pytest


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
    for image_url in (
        "http://images.example/item.jpg", "https://127.0.0.1/item.jpg",
        "https://localhost/item.jpg", "https://2130706433/item.jpg",
        "https://0x7f000001/item.jpg", "https://images.example:8443/item.jpg",
        "https://user:secret@images.example/item.jpg",
    ):
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


def test_same_model_with_distinct_variants_is_allowed_but_same_identity_is_rejected(client):
    base = {"name": "Protein", "model": "WHEY-9000", "variant": "Vanilla 1 kg"}
    assert client.post("/api/products", json=base).status_code == 201
    chocolate = {**base, "variant": "Chocolate 1 kg"}
    assert client.post("/api/products", json=chocolate).status_code == 201
    duplicate = {**base, "variant": "vanilla 1 kg"}
    assert client.post("/api/products", json=duplicate).status_code == 409


def test_product_metadata_rejects_non_string_values_cleanly(client):
    for key, value in (("variant", ["Vanilla"]), ("description", {"text": "bad"}),
                       ("image_url", 42)):
        response = client.post("/api/products", json={
            "name": "Protein", "model": f"WHEY-9001-{key}", key: value,
        })
        assert response.status_code == 400


def test_import_finding_keeps_same_model_variants_separate(seeded):
    from app import ingest, store
    from app.db import connect

    with connect(seeded) as conn:
        first_id = store.create_product(conn, {
            "name": "Protein", "model": "WHEY-9000", "variant": "Vanilla 1 kg",
        })
        second_id = store.create_product(conn, {
            "name": "Protein", "model": "WHEY-9000", "variant": "Chocolate 1 kg",
        })
        report = ingest.import_finding(conn, {
            "model": "WHEY-9000", "variant": "Chocolate 1 kg",
            "retailer": "Example Shop", "price": 50,
        })
        listing = conn.execute(
            "SELECT product_id FROM listings WHERE id = ?", (report["listing_id"],)
        ).fetchone()
        assert listing["product_id"] == second_id
        assert listing["product_id"] != first_id
        with pytest.raises(ingest.ImportError_, match="multiple variants"):
            ingest.import_finding(conn, {
                "model": "WHEY-9000", "retailer": "Other Shop", "price": 45,
            })
        with pytest.raises(ingest.ImportError_, match="variant must be a string"):
            ingest.import_finding(conn, {
                "model": "WHEY-9000", "variant": ["Vanilla"],
                "retailer": "Other Shop", "price": 45,
            })


def test_model_only_check_reports_duplicate_only_when_unambiguous(client):
    base = {"name": "Protein", "model": "WHEY-CHECK", "variant": "Vanilla"}
    assert client.post("/api/products", json=base).status_code == 201
    checked = client.get("/api/products/check-model?model=WHEY-CHECK").json()
    assert checked["duplicate_of"]["variant"] == "Vanilla"
    assert client.post("/api/products", json={**base, "variant": "Chocolate"}).status_code == 201
    ambiguous = client.get("/api/products/check-model?model=WHEY-CHECK").json()
    assert ambiguous["duplicate_of"] is None
    variant = client.get("/api/products/check-model?model=WHEY-CHECK&variant=Chocolate").json()
    assert variant["duplicate_of"]["variant"] == "Chocolate"


def test_snapshot_preserves_multiple_variants_for_one_model(db, tmp_path):
    from app import ingest, store
    from app.db import connect, migrate

    source = connect(db)
    try:
        for variant in ("Vanilla 1 kg", "Chocolate 1 kg"):
            store.create_product(source, {"name": "Protein", "model": "SNAP-WHEY", "variant": variant})
        snapshot = ingest.export_all(source)
    finally:
        source.close()

    target_path = tmp_path / "variant-restored.db"
    migrate(target_path)
    target = connect(target_path)
    try:
        ingest.import_snapshot(target, snapshot)
        restored = target.execute(
            "SELECT variant FROM products WHERE model = 'SNAP-WHEY' ORDER BY variant"
        ).fetchall()
        assert [row["variant"] for row in restored] == ["Chocolate 1 kg", "Vanilla 1 kg"]
    finally:
        target.close()


def test_pre_metadata_upgrade_preserves_linked_product_data_and_id_high_water(tmp_path):

    from app import store
    from app.db import MIGRATIONS_DIR, connect, migrate

    path = tmp_path / "pre-metadata.db"
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0030_product_card_metadata.sql":
            break
        conn.executescript(migration.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, 'test')",
            (migration.name,),
        )
    retailer = store.ensure_retailer(conn, "Upgrade Shop")
    conn.execute("INSERT INTO products (name, model, archived) VALUES ('Mixer', 'UP-MX', 1)")
    product_id = conn.execute("SELECT id FROM products WHERE model = 'UP-MX'").fetchone()[0]
    conn.execute("INSERT INTO products (name, model) VALUES ('Deleted', 'UP-DEL')")
    conn.execute("DELETE FROM products WHERE model = 'UP-DEL'")
    conn.execute("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'products'")
    conn.execute(
        "INSERT INTO listings (product_id, retailer_id, url) VALUES (?, ?, ?)",
        (product_id, retailer["id"], "https://shop.example/mixer"),
    )
    listing_id = conn.execute("SELECT id FROM listings WHERE product_id = ?", (product_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO price_history (product_id, listing_id, observed_at, advertised_price)"
        " VALUES (?, ?, '2026-01-01T00:00:00Z', 80)",
        (product_id, listing_id),
    )
    conn.execute(
        "INSERT INTO purchases (product_id, listing_id, purchased_at, price_paid)"
        " VALUES (?, ?, '2026-01-02T00:00:00Z', 90)",
        (product_id, listing_id),
    )
    conn.execute(
        "UPDATE product_check_schedules SET cadence = 'monthly' WHERE product_id = ?",
        (product_id,),
    )
    conn.commit()
    conn.close()

    assert migrate(path) == [
        "0030_product_card_metadata.sql", "0031_product_model_variant_identity.sql",
    ]
    conn = connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        product = store.get_product(conn, product_id)
        assert product["archived"] == 1
        assert conn.execute("SELECT product_id FROM listings WHERE id = ?", (listing_id,)).fetchone()[0] == product_id
        assert conn.execute("SELECT product_id FROM price_history WHERE listing_id = ?", (listing_id,)).fetchone()[0] == product_id
        assert conn.execute("SELECT product_id FROM purchases WHERE listing_id = ?", (listing_id,)).fetchone()[0] == product_id
        assert conn.execute("SELECT cadence FROM product_check_schedules WHERE product_id = ?", (product_id,)).fetchone()[0] == "monthly"
        new_id = store.create_product(conn, {"name": "New mixer", "model": "AFTER-UP"})
        assert new_id > 99
        assert conn.execute(
            "SELECT product_id FROM product_check_schedules WHERE product_id = ?", (new_id,)
        ).fetchone()[0] == new_id
        assert conn.execute(
            "SELECT product_id FROM product_discovery_schedules WHERE product_id = ?", (new_id,)
        ).fetchone()[0] == new_id
    finally:
        conn.close()


def test_legacy_snapshot_without_variant_is_reported_when_model_is_ambiguous(db):
    from app import ingest, store
    from app.db import connect

    with connect(db) as conn:
        for variant in ("Vanilla", "Chocolate"):
            store.create_product(conn, {
                "name": "Protein", "model": "LEGACY-WHEY", "variant": variant,
            })
        before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        report = ingest.import_snapshot(conn, {
            "products": [{"id": 500, "name": "Protein", "model": "LEGACY-WHEY"}],
            "retailers": [],
        })
        after = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        assert after == before
        assert any("matches multiple variants" in error for error in report["errors"])


def test_legacy_model_only_finding_still_matches_one_unambiguous_variant(db):
    from app import ingest, store
    from app.db import connect

    with connect(db) as conn:
        product_id = store.create_product(conn, {
            "name": "Protein", "model": "UNAMB-WHEY", "variant": "Vanilla",
        })
        result = ingest.import_finding(conn, {
            "model": "UNAMB-WHEY", "retailer": "Example Shop", "price": 50,
        })
        listing = conn.execute(
            "SELECT product_id FROM listings WHERE id = ?", (result["listing_id"],)
        ).fetchone()
        assert listing["product_id"] == product_id
