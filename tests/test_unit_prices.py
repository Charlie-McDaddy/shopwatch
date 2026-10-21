from app import pricing, store


def test_unit_price_converts_grams_to_price_per_kilogram():
    assert pricing.unit_price(12.50, 500, "g") == (25.0, "kg")


def test_unit_price_does_not_guess_unknown_or_incompatible_quantities():
    assert pricing.unit_price(12.50, None, "g") is None
    assert pricing.unit_price(12.50, 500, "ml") == (25.0, "L")
    assert pricing.unit_price(12.50, 2, "each") == (6.25, "each")


def test_unit_price_is_exposed_for_each_separate_product(client):
    small = client.post("/api/products", json={
        "name": "Protein powder vanilla 500 g", "model": "PROTEIN-VAN-500",
        "category": "food", "quantity": 500, "quantity_unit": "g",
    }).json()
    large = client.post("/api/products", json={
        "name": "Protein powder vanilla 1 kg", "model": "PROTEIN-VAN-1000",
        "category": "food", "quantity": 1, "quantity_unit": "kg",
    }).json()
    assert small["id"] != large["id"]
    client.post(f"/api/products/{small['id']}/retailers", json={
        "retailer": "Small-pack shop", "advertised_price": 10, "freight": 0,
    })
    client.post(f"/api/products/{large['id']}/retailers", json={
        "retailer": "Large-pack shop", "advertised_price": 15, "freight": 0,
    })
    small = client.get(f"/api/products/{small['id']}").json()
    large = client.get(f"/api/products/{large['id']}").json()
    assert small["best_unit_price"] == [20.0, "kg"]
    assert large["best_unit_price"] == [15.0, "kg"]
    assert small["best_delivered"] == 10 and large["best_delivered"] == 15
    assert small["best_unit_price"] != large["best_unit_price"]
    html = client.get(f"/products/{small['id']}").text
    assert "Pack: 500.0 g" in html and "Best: $20 per kg" in html


def test_quantity_is_validated_at_http_boundary_and_can_be_cleared(client):
    product = client.post("/api/products", json={"name": "Test", "model": "TEST-Q"}).json()
    for quantity in (0, -1, "nan", "inf", True):
        response = client.patch(f"/api/products/{product['id']}", json={
            "quantity": quantity, "quantity_unit": "g",
        })
        assert response.status_code == 400
    for unit in ([], {}):
        response = client.patch(f"/api/products/{product['id']}", json={
            "quantity": 500, "quantity_unit": unit,
        })
        assert response.status_code == 400
    assert client.patch(f"/api/products/{product['id']}", json={
        "quantity": 500, "quantity_unit": "g",
    }).status_code == 200
    cleared = client.patch(f"/api/products/{product['id']}", json={
        "quantity": None, "quantity_unit": None,
    }).json()
    assert cleared["quantity"] is None and cleared["quantity_unit"] is None


def test_snapshot_round_trip_preserves_pack_quantity(conn):
    from app import ingest
    product_id = store.create_product(conn, {
        "name": "Protein powder", "model": "PROTEIN-500", "quantity": 500,
        "quantity_unit": "g",
    })
    snapshot = ingest.export_all(conn)
    assert ingest.import_snapshot(conn, snapshot)["products"] == 1
    saved = store.get_product(conn, product_id)
    assert saved["quantity"] == 500 and saved["quantity_unit"] == "g"


def test_legacy_product_without_quantity_remains_valid(conn):
    product_id = store.create_product(conn, {"name": "Old entry", "model": "OLD-1"})
    assert store.get_product(conn, product_id)["quantity"] is None
