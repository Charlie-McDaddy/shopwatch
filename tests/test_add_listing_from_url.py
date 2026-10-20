""""Paste a listing URL" (issue #94): POST /api/products/{id}/retailers/from-url.

Covers the endpoint end to end through the HTTP surface - the derived retailer, the
listing the URL always lands on regardless of what else succeeds, the adapter
fast-path when the domain is a configured retailer, and the URL-scoped research-job
fallback when it is not (or the adapter's fetch fails).
"""

from __future__ import annotations

from app import provenance, retailers


def q930h(client):
    products = client.get("/api/products").json()
    return next(p for p in products if p["model"] == "HW-Q930H/XY")


def stub_adapters(monkeypatch, behaviour):
    """Replace every adapter's check() with a function of the adapter slug.

    Same shape as tests/test_price_watch.py's own helper - kept local rather than
    imported, since a shared test helper module is more machinery than one duplicated
    six-line function is worth here.
    """

    def fake_check(self, url, expected_model=None):
        return behaviour(self.slug)

    monkeypatch.setattr(retailers.base.RetailerAdapter, "check", fake_check, raising=True)


def test_product_page_renders_the_paste_url_action(client):
    product = q930h(client)
    response = client.get(f"/products/{product['id']}")
    assert response.status_code == 200
    assert "Add from a URL" in response.text


def test_pasting_a_url_for_an_unconfigured_retailer_creates_a_listing_and_queues_research(
    client,
):
    product = q930h(client)
    url = "https://www.centrecom.com.au/samsung-smarttag2-4-pack-2-x-white-and-2-x-black"

    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["retailer"]["name"] == "Centrecom"
    assert body["listing"]["retailer_name"] == "Centrecom"
    assert body["listing"]["url"] == url
    # No adapter for Centre Com, so nothing was actually fetched.
    assert body["scraped"] is False
    # And a one-off research job was queued, scoped to exactly this URL.
    assert body["research_job"] is not None
    assert body["research_job"]["status"] == "QUEUED"
    result = body["research_job"]["results"][0]
    assert result["retailer_id"] == body["retailer"]["id"]
    assert result["url"] == url


def test_pasting_a_url_for_a_known_adapter_domain_reuses_the_existing_retailer(client):
    """jbhifi.com.au already matches the JB Hi-Fi adapter, and JB Hi-Fi already exists
    from the seed - this must not create a second, differently-named retailer for the
    same shop."""
    product = q930h(client)
    before = {r["name"] for r in client.get("/api/retailers").json()}
    assert "JB Hi-Fi" in before

    url = "https://www.jbhifi.com.au/products/samsung-hw-q930h-soundbar"
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["retailer"]["name"] == "JB Hi-Fi"
    after = {r["name"] for r in client.get("/api/retailers").json()}
    assert after == before, "no new retailer should have been created"


def test_a_successful_adapter_scrape_writes_the_price_and_skips_the_research_job(
    client, monkeypatch
):
    product = q930h(client)
    stub_adapters(
        monkeypatch,
        lambda slug: retailers.Observation(advertised_price=1499.0, stock_status="In Stock"),
    )

    url = "https://www.jbhifi.com.au/products/samsung-hw-q930h-soundbar"
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["scraped"] is True
    assert body["listing"]["advertised_price"] == 1499.0
    assert body["listing"]["provenance"]["advertised_price"]["state"] == provenance.LIVE
    # A successful scrape answers the question outright, so no job is needed.
    assert body["research_job"] is None


def test_an_adapter_fetch_failure_falls_back_to_a_research_job(client, monkeypatch):
    """JB Hi-Fi already has a seeded listing for this product with a real price - a
    failed scrape must leave that value exactly alone (the previous reading is still
    the best information available, same rule price_watch itself follows) and fall
    back to a research job rather than inventing or blanking anything."""
    product = q930h(client)
    before = next(
        listing for listing in product["listings"] if listing["retailer_name"] == "JB Hi-Fi"
    )
    stub_adapters(
        monkeypatch, lambda slug: (_ for _ in ()).throw(retailers.FetchError("blocked"))
    )

    url = "https://www.jbhifi.com.au/products/samsung-hw-q930h-soundbar"
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["scraped"] is False
    assert body["listing"]["advertised_price"] == before["advertised_price"]
    assert body["listing"]["check_status"] == "BLOCKED"
    assert body["research_job"] is not None


def test_pasting_the_same_url_twice_does_not_duplicate_the_listing(client):
    product = q930h(client)
    url = "https://www.centrecom.com.au/samsung-smarttag2-4-pack"

    first = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    ).json()
    second = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    ).json()

    assert first["listing"]["id"] == second["listing"]["id"]
    listings = client.get(f"/api/products/{product['id']}/retailers").json()
    matching = [listing for listing in listings if listing["url"] == url]
    assert len(matching) == 1


def test_from_url_requires_a_url(client):
    product = q930h(client)
    response = client.post(f"/api/products/{product['id']}/retailers/from-url", json={})
    assert response.status_code == 400


def test_from_url_rejects_a_non_http_url(client):
    product = q930h(client)
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url",
        json={"url": "javascript:alert(1)"},
    )
    assert response.status_code == 400


def test_from_url_404s_for_an_unknown_product(client):
    response = client.post(
        "/api/products/999999/retailers/from-url",
        json={"url": "https://www.centrecom.com.au/thing"},
    )
    assert response.status_code == 404


def test_from_url_still_creates_the_listing_when_a_research_job_is_already_running(client):
    """A running job for this product (from the ordinary wizard path) must not stop
    the URL from landing on a listing - the URL is the thing this feature exists to
    never lose, even when the research half of it has to sit out this round."""
    product = q930h(client)
    some_retailer = client.get("/api/retailers").json()[0]
    started = client.post(
        "/api/research-jobs",
        json={"product_id": product["id"], "retailer_ids": [some_retailer["id"]]},
    )
    assert started.status_code == 202

    url = "https://www.centrecom.com.au/samsung-smarttag2-4-pack"
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url", json={"url": url}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["listing"]["url"] == url
    assert body["research_job"] is None
