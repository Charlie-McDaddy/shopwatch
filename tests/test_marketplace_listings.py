from app import provenance, retailers


def product_id(client):
    return next(p["id"] for p in client.get("/api/products").json()
                if p["model"] == "HW-Q930H/XY")


def test_marketplace_offers_for_distinct_sellers_remain_separate(client):
    pid = product_id(client)
    url = "https://www.amazon.com.au/dp/B0TEST123"
    first = client.post(f"/api/products/{pid}/retailers/from-url", json={
        "url": url, "seller_name": "Marketplace seller one", "seller_id": "SELLER-1",
    })
    second = client.post(f"/api/products/{pid}/retailers/from-url", json={
        "url": url, "seller_name": "Marketplace seller two", "seller_id": "SELLER-2",
    })
    assert first.status_code == second.status_code == 201
    a, b = first.json()["listing"], second.json()["listing"]
    assert a["id"] != b["id"]
    assert a["seller_id"] == "SELLER-1" and b["seller_id"] == "SELLER-2"
    assert a["check_capability"] == b["check_capability"] == "MANUAL"
    assert first.json()["research_job"] is None and second.json()["research_job"] is None
    from app.db import connect, db_path
    with connect(db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 0
    third = client.post(f"/api/products/{pid}/retailers/from-url", json={
        "url": url, "seller_id": "SELLER-1", "offer_id": "OFFER-2",
    }).json()["listing"]
    assert third["id"] not in {a["id"], b["id"]}
    html = client.get(f"/products/{pid}").text
    assert "Marketplace seller one" in html and "Marketplace seller two" in html
    assert "Manual updates" in html


def test_marketplace_manual_updates_keep_provenance(client):
    pid = product_id(client)
    added = client.post(f"/api/products/{pid}/retailers/from-url", json={
        "url": "https://www.temu.com/goods.html?goods_id=12345",
        "seller_name": "Temu seller", "seller_id": "T-1",
    }).json()
    listing = added["listing"]
    assert listing["check_capability"] == "MANUAL"
    assert added["research_job"] is None
    update = client.patch(f"/api/retailers/{listing['id']}", json={
        "advertised_price": 19.99, "freight": 0,
    })
    assert update.status_code == 200
    assert update.json()["listing"]["advertised_price"] == 19.99
    assert update.json()["listing"]["provenance"]["advertised_price"]["state"] == provenance.MANUAL
    assert update.json()["listing"]["provenance"]["advertised_price"]["manual_locked"] == 1


def test_custom_listing_rejects_internal_and_credential_urls(client):
    pid = product_id(client)
    for url in ("http://127.0.0.1/admin", "http://localhost/secret",
                "http://user:pass@example.com/item", "https://[::1]/item"):
        response = client.post(f"/api/products/{pid}/retailers/from-url", json={"url": url})
        assert response.status_code == 400


def test_adapter_redirect_to_internal_address_is_blocked_before_following(monkeypatch):
    from app import urls
    from app.retailers import base

    class Response:
        status_code = 302
        headers = {"location": "http://127.0.0.1/admin"}
        text = ""

    requests = []
    monkeypatch.setattr(urls.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))])
    monkeypatch.setattr(base, "requests", type("Requests", (), {
        "get": staticmethod(lambda url, **kwargs: (requests.append((url, kwargs)), Response())[1])
    }))
    adapter = retailers.get_adapter("harvey_norman")
    adapter.config.scraping_enabled = True
    adapter.config.request_delay = 0
    import pytest
    with pytest.raises(base.FetchError, match="unsafe URL or redirect target"):
        adapter.fetch("https://www.harveynorman.com.au/product")
    assert len(requests) == 1 and requests[0][1]["allow_redirects"] is False


def test_adapter_model_mismatch_is_held_out_and_does_not_apply_wrong_price(client, monkeypatch):
    from app.retailers.base import Observation, RetailerAdapter

    pid = product_id(client)
    product = client.get(f"/api/products/{pid}").json()
    before = next(row for row in product["listings"] if row["retailer_name"] == "JB Hi-Fi")
    def wrong_model(self, url, expected_model=None):
        return Observation(model_on_page="OTHER-MODEL", advertised_price=1.0,
                           warnings=["model mismatch: OTHER-MODEL does not match HW-Q930H/XY"])
    monkeypatch.setattr(RetailerAdapter, "check", wrong_model)
    response = client.post(f"/api/products/{pid}/retailers/from-url", json={
        "url": "https://www.jbhifi.com.au/products/other-model",
    })
    listing = response.json()["listing"]
    assert response.status_code == 201
    assert listing["ruled_out"] == 0
    assert listing["advertised_price"] == before["advertised_price"]
    assert listing["verification"]["model"]["status"] == provenance.FLAGGED
    assert listing["check_status"] == "BLOCKED"


def test_adapter_rejects_public_hostname_resolving_to_private_ip(monkeypatch):
    from app import urls
    from app.retailers import base

    monkeypatch.setattr(urls.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("127.0.0.1", 80)),
    ])
    adapter = retailers.get_adapter("harvey_norman")
    adapter.config.scraping_enabled = True
    adapter.config.request_delay = 0
    import pytest
    with pytest.raises(base.FetchError, match="unsafe URL or redirect target"):
        adapter.fetch("https://127.0.0.1.nip.io/product")


def test_from_url_rejects_non_text_url(client):
    pid = product_id(client)
    response = client.post(f"/api/products/{pid}/retailers/from-url", json={"url": []})
    assert response.status_code == 400
