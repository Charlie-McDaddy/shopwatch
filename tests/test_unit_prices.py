from app import pricing, store


def test_unit_price_converts_grams_to_price_per_kilogram():
    assert pricing.unit_price(12.50, 500, "g") == (25.0, "kg")


def test_unit_price_does_not_guess_unknown_or_incompatible_quantities():
    assert pricing.unit_price(12.50, None, "g") is None
    assert pricing.unit_price(12.50, 500, "ml") == (25.0, "L")
    assert pricing.unit_price(12.50, 2, "each") == (6.25, "each")


def test_unit_price_does_not_return_an_infinite_result():
    assert pricing.unit_price(1e308, 1e-308, "g") is None


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
    for product in (small, large):
        listing = client.get(f"/api/products/{product['id']}").json()["listings"][0]
        client.patch(f"/api/retailers/{listing['id']}", json={"stock_status": "In stock"})
        for aspect in ("model", "price", "stock"):
            client.post(f"/api/retailers/{listing['id']}/verification",
                        json={"aspect": aspect, "status": "VERIFIED"})
    small = client.get(f"/api/products/{small['id']}").json()
    large = client.get(f"/api/products/{large['id']}").json()
    assert small["best_unit_price"] == [20.0, "kg"]
    assert large["best_unit_price"] == [15.0, "kg"]
    assert small["best_delivered"] == 10 and large["best_delivered"] == 15
    assert small["best_unit_price"] != large["best_unit_price"]
    small_html = client.get(f"/products/{small['id']}").text
    large_html = client.get(f"/products/{large['id']}").text
    assert "Pack: 500.0 g" in small_html and "Best: $20 per kg" in small_html
    assert "Pack: 1.0 kg" in large_html and "Best: $15 per kg" in large_html

    small_history = client.get(f"/api/price-history/{small['id']}").json()
    large_history = client.get(f"/api/price-history/{large['id']}").json()
    assert small_history and large_history
    assert {row["product_id"] for row in small_history} == {small["id"]}
    assert {row["product_id"] for row in large_history} == {large["id"]}


def test_quantity_is_validated_at_http_boundary_and_can_be_cleared(client):
    product = client.post("/api/products", json={"name": "Test", "model": "TEST-Q"}).json()
    for quantity in (0, -1, "nan", "inf", True):
        response = client.patch(f"/api/products/{product['id']}", json={
            "quantity": quantity, "quantity_unit": "g",
        })
        assert response.status_code == 400
    response = client.post("/api/products", json={
        "name": "Huge quantity", "model": "TEST-HUGE-Q", "quantity": 10**1000,
        "quantity_unit": "g",
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


def test_snapshot_without_quantity_fields_remains_importable(conn):
    from app import ingest

    snapshot = {
        "format": "shopwatch-snapshot",
        "version": 1,
        "products": [{"id": 1, "name": "Old protein", "model": "OLD-PROTEIN"}],
    }
    assert ingest.import_snapshot(conn, snapshot)["products"] == 1
    product = store.get_product(conn, 1)
    assert product["quantity"] is None and product["quantity_unit"] is None
