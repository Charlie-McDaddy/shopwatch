"""HTTP and rendered controls for per-product alert settings and history."""

def q930h(client):
    products = client.get("/api/products").json()
    return next(p for p in products if p["model"] == "HW-Q930H/XY")


def test_product_alert_preferences_round_trip(client):
    product = q930h(client)
    path = f"/api/products/{product['id']}/alerts"
    initial = client.get(path)
    assert initial.status_code == 200
    assert initial.json()["preferences"]["target_price"] is None

    saved = client.put(path, json={
        "target_price": 899.5, "drop_enabled": True, "drop_min_change": 35,
    })
    assert saved.status_code == 200
    assert saved.json()["target_price"] == 899.5
    assert saved.json()["drop_enabled"] is True
    assert client.get(path).json()["preferences"]["drop_min_change"] == 35


def test_product_alert_preferences_validate_values_and_missing_products(client):
    product = q930h(client)
    path = f"/api/products/{product['id']}/alerts"
    assert client.put(path, json={"target_price": -1}).status_code == 400
    assert client.put(path, json={"drop_enabled": "yes"}).status_code == 400
    assert client.get("/api/products/999999/alerts").status_code == 404


def test_product_page_shows_alert_controls_and_history(client):
    product = q930h(client)
    body = client.get(f"/products/{product['id']}").text
    assert "Target delivered price" in body
    assert "worthwhile further price drop" in body
