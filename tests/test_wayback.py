"""Controlled tests for the bounded Internet Archive lookup and evidence parser."""

from __future__ import annotations

import json
import socket

import pytest

from app import urls, wayback


class Response:
    def __init__(self, payload: bytes, status_code: int = 200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {"Content-Length": str(len(payload))}
        self.closed = False

    def iter_content(self, size):
        yield self.payload

    def close(self):
        self.closed = True


def allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        urls.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))
        ],
    )


def cdx(original="https://shop.example/item", timestamp="20230101000000"):
    return json.dumps(
        [
            ["timestamp", "original", "statuscode", "mimetype"],
            [timestamp, original, "200", "text/html"],
        ]
    ).encode()


def html(model="MODEL-1", currency="AUD", price="99.95"):
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Widget",
        "model": model,
        "offers": {"@type": "Offer", "price": price, "priceCurrency": currency},
    }
    return (
        "<html><script type='application/ld+json'>" + json.dumps(product) + "</script></html>"
    ).encode()


def test_exact_model_aud_snapshot_returns_archive_source_and_unknown_freight(monkeypatch):
    original = "https://shop.example/item"
    responses = iter([Response(cdx()), Response(html())])
    calls = []
    allow_public_dns(monkeypatch)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(wayback.requests, "get", get)
    result = wayback.lookup_listing(original, "MODEL-1")
    assert result["status"] == "evidence"
    assert result["snapshots"] == [
        {
            "snapshot_url": "https://web.archive.org/web/20230101000000id_/https://shop.example/item",
            "captured_at": "2023-01-01T00:00:00Z",
            "model": "MODEL-1",
            "advertised_price": 99.95,
            "currency": "AUD",
            "price_context": "Archived advertised price; freight and discount eligibility unknown.",
        }
    ]
    assert calls[0][0] == wayback.CDX_URL
    assert calls[1][0].startswith("https://web.archive.org/web/")
    assert all(call[1]["allow_redirects"] is False for call in calls)
    assert all(call[1]["timeout"] <= wayback.MAX_SINGLE_TIMEOUT_SECONDS for call in calls)


@pytest.mark.parametrize(
    ("payload", "expected_model", "status"),
    [
        (html(model="MODEL-2"), "MODEL-1", "no_usable_price"),
        (html(currency="USD"), "MODEL-1", "no_usable_price"),
        (b"<html><script>{broken json</script>MODEL-1 $99.95</html>", "MODEL-1", "no_usable_price"),
    ],
)
def test_wrong_variant_unknown_currency_and_malformed_snapshot_are_gaps(
    monkeypatch, payload, expected_model, status
):
    responses = iter([Response(cdx()), Response(payload)])
    allow_public_dns(monkeypatch)
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: next(responses))
    result = wayback.lookup_listing("https://shop.example/item", expected_model)
    assert result["status"] == status
    assert result["snapshots"] == []
    assert "exact model" in result["note"] or "AUD price" in result["note"]


def test_jsonld_boolean_price_is_not_a_numeric_archive_amount():
    page = html(price=True)
    assert wayback._model_price_from_html(page, "MODEL-1") is None


def test_unrepresentably_large_archive_price_is_an_honest_gap(monkeypatch):
    allow_public_dns(monkeypatch)
    responses = iter([Response(cdx()), Response(html(price=10**400))])
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: next(responses))
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert result["status"] == "no_usable_price"
    assert result["snapshots"] == []


def test_no_cdx_capture_is_an_honest_gap(monkeypatch):
    allow_public_dns(monkeypatch)
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: Response(b'[["timestamp","original"]]'))
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert result["status"] == "no_snapshot"
    assert not result["snapshots"]


def test_invalid_listing_reference_never_contacts_the_archive(monkeypatch):
    called = False

    def get(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("no archive call expected")

    monkeypatch.setattr(wayback.requests, "get", get)
    result = wayback.lookup_listing("http://127.0.0.1/admin", "MODEL-1")
    assert result["status"] == "blocked"
    assert not called


def test_public_url_resolver_rejects_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        urls.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, None, ("10.0.0.8", 443)),
        ],
    )
    assert not wayback.public_http_url("https://retailer.example/item")


def test_redirects_and_oversized_archive_bodies_are_rejected(monkeypatch):
    allow_public_dns(monkeypatch)
    redirect = Response(b"", status_code=302, headers={"Location": "http://127.0.0.1/"})
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: redirect)
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert result["status"] == "blocked"
    assert redirect.closed

    too_large = Response(b"x", headers={"Content-Length": str(wayback.MAX_RESPONSE_BYTES + 1)})
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: too_large)
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert result["status"] == "blocked"
    assert too_large.closed


def test_archive_data_must_refer_to_exact_url(monkeypatch):
    allow_public_dns(monkeypatch)
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: Response(cdx("https://attacker.example/")))
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert result["status"] == "no_snapshot"


def test_timeout_and_malformed_cdx_return_gaps(monkeypatch):
    allow_public_dns(monkeypatch)
    monkeypatch.setattr(
        wayback.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(wayback.requests.Timeout("slow")),
    )
    timed_out = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert timed_out["status"] == "blocked"
    assert timed_out["snapshots"] == []

    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: Response(b"not json"))
    malformed = wayback.lookup_listing("https://shop.example/item", "MODEL-1")
    assert malformed["status"] == "blocked"
    assert malformed["snapshots"] == []


def test_lookup_stops_at_shared_request_budget(monkeypatch):
    budget = wayback.RequestBudget(remaining_requests=1)
    allow_public_dns(monkeypatch)
    bodies = iter([Response(cdx())])
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: next(bodies))
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1", budget)
    assert result["status"] == "blocked"
    assert "budget" in result["note"].lower()
    assert budget.remaining_requests == 0


def test_persistence_rejects_boolean_prices_and_nonarchive_sources(conn, seeded):
    from app import store

    product = store.list_products(conn)[0]
    listing = product["listings"][0]
    wayback.persist_lookup(
        conn,
        product["id"],
        listing["id"],
        "https://shop.example/item",
        product["model"],
        {
            "status": "evidence",
            "snapshots": [
                {
                    "snapshot_url": "https://evil.example/web/20230101000000id_/https://shop.example/item",
                    "captured_at": "2023-01-01T00:00:00Z",
                    "model": product["model"],
                    "advertised_price": True,
                    "currency": "AUD",
                },
                {
                    "snapshot_url": "https://web.archive.org/web/20230101000000id_/https://shop.example/item",
                    "captured_at": "2023-01-01T00:00:00Z",
                    "model": product["model"],
                    "advertised_price": True,
                    "currency": "AUD",
                },
            ],
        },
        "2026-10-21T00:00:00Z",
    )
    evidence, coverage = wayback.history(conn, product["id"])
    assert evidence == []
    assert coverage[0]["status"] == "no_usable_price"


def test_persistence_rejects_unrepresentably_large_archive_prices(conn, seeded):
    from app import store

    product = store.list_products(conn)[0]
    listing = product["listings"][0]
    original = "https://shop.example/item"
    wayback.persist_lookup(
        conn,
        product["id"],
        listing["id"],
        original,
        product["model"],
        {
            "status": "evidence",
            "snapshots": [
                {
                    "snapshot_url": (
                        "https://web.archive.org/web/20230101000000id_/" + original
                    ),
                    "captured_at": "2023-01-01T00:00:00Z",
                    "model": product["model"],
                    "advertised_price": 10**400,
                    "currency": "AUD",
                }
            ],
        },
        "2026-10-21T00:00:00Z",
    )
    evidence, coverage = wayback.history(conn, product["id"])
    assert evidence == []
    assert coverage[0]["status"] == "no_usable_price"


def test_invalid_calendar_timestamp_and_non_200_cdx_rows_are_ignored(monkeypatch):
    allow_public_dns(monkeypatch)
    invalid_date = cdx(timestamp="20261301120000")
    responses = iter([Response(invalid_date), Response(cdx(timestamp="20230101000000"))])
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: next(responses))
    assert wayback.lookup_listing("https://shop.example/item", "MODEL-1")["status"] == "no_snapshot"

    wrong_status = json.dumps(
        [
            ["timestamp", "original", "statuscode", "mimetype"],
            ["20230101000000", "https://shop.example/item", "404", "text/html"],
        ]
    ).encode()
    monkeypatch.setattr(wayback.requests, "get", lambda *a, **k: Response(wrong_status))
    assert wayback.lookup_listing("https://shop.example/item", "MODEL-1")["status"] == "no_snapshot"


def test_expired_wall_budget_makes_no_archive_request(monkeypatch):
    import time

    allow_public_dns(monkeypatch)
    monkeypatch.setattr(
        wayback.requests, "get", lambda *a, **k: pytest.fail("expired budget must stop first")
    )
    budget = wayback.RequestBudget(deadline=time.monotonic() - 1)
    result = wayback.lookup_listing("https://shop.example/item", "MODEL-1", budget)
    assert result["status"] == "blocked"
    assert "budget" in result["note"].lower()
