"""Reply parsing for the host-level research runner - the part that can be tested
without a real `claude` subprocess. See deploy/research-runner.py's own docstring for
why this script exists and where it runs (never inside the shopwatch container)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "research-runner.py"
_SPEC = importlib.util.spec_from_file_location("research_runner", _PATH)
research_runner = importlib.util.module_from_spec(_SPEC)
sys.modules["research_runner"] = research_runner
_SPEC.loader.exec_module(research_runner)


@pytest.fixture(autouse=True)
def claude_subscription_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-subscription-token")


def test_a_clean_json_reply_with_a_price_parses(monkeypatch):
    reply = '{"found": true, "price": 1099, "stock": "In stock", "url": "https://x", "reason": "ok"}'
    result = research_runner.parse_research_reply(reply)
    assert result["price"] == 1099
    assert result["stock"] == "In stock"


def test_a_fenced_reply_still_parses():
    reply = 'Here you go:\n```json\n{"found": true, "price": 500, "reason": "ok"}\n```'
    result = research_runner.parse_research_reply(reply)
    assert result["price"] == 500


def test_reply_survives_trailing_prose_with_its_own_brace():
    """Same defect app/offers.py's parse_cli_output and tools/llm-helper.py's
    parse_reply both had: a naive first-`{`/last-`}` slice breaks the moment the
    reply's trailing text has its own brace pair."""
    reply = ('{"found": true, "price": 1099, "stock": "In stock", "url": "https://x",'
              ' "reason": "ok"}\n\n(Prices vary by size, e.g. {55, 65}.)')
    result = research_runner.parse_research_reply(reply)
    assert result["price"] == 1099


def test_a_leading_reference_price_does_not_shadow_the_real_answer():
    """A code-review pass caught the mirror-image defect the raw_decode fix
    introduced: it took the FIRST complete JSON object, so a reply that states an
    older/similar model's price before the exact model requested silently returned
    the wrong price instead of erroring. Every reproduced example had the real
    answer LAST."""
    reply = (
        'For a similar older model I found {"found": true, "price": 999, "reason": "similar model"} '
        'but for the exact model requested: '
        '{"found": true, "price": 1099, "stock": "In stock", "url": "https://x", "reason": "ok"}'
    )
    result = research_runner.parse_research_reply(reply)
    assert result["price"] == 1099


def test_found_as_a_string_is_not_treated_as_true():
    """`"found": "false"` is a non-empty string, which is truthy in Python - a model
    that returns the word instead of the JSON boolean must not bypass the
    not-found guard."""
    reply = '{"found": "false", "price": 199, "reason": "could not confirm"}'
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply(reply)
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"


def test_a_nan_price_is_not_treated_as_usable():
    """Python's json parser accepts the non-standard NaN token by default; NaN is a
    float and is not None, so it would otherwise pass the found-a-price guard and
    get imported as a real price that silently fails every trigger comparison."""
    reply = '{"found": true, "price": NaN, "reason": "estimate"}'
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply(reply)
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"


def test_found_false_is_reported_as_needs_manual_check():
    reply = '{"found": false, "price": null, "reason": "page blocked the request"}'
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply(reply)
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"
    assert "blocked" in exc_info.value.note


def test_found_true_with_no_price_is_still_needs_manual_check():
    """A model claiming success with no number is not a usable finding either."""
    reply = '{"found": true, "price": null, "reason": "saw the page, no price listed"}'
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply(reply)
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"


def test_reply_with_no_json_object_at_all():
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply("I couldn't find pricing for this item.")
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"
    assert "no JSON" in exc_info.value.note


def test_reply_with_malformed_json():
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.parse_research_reply('{"found": true, "price": }')
    assert exc_info.value.status == "NEEDS_MANUAL_CHECK"


def test_empty_reply():
    with pytest.raises(research_runner.RunnerError):
        research_runner.parse_research_reply("")



# --------------------------------------------------- retailer search (Firecrawl-backed)

def test_bare_domain_strips_www_and_scheme():
    assert research_runner._bare_domain("https://www.jbhifi.com.au/products/x") == "jbhifi.com.au"
    assert research_runner._bare_domain("bunnings.com.au") == "bunnings.com.au"


def test_bare_domain_is_lowercased():
    """The control this test exists for: without lowercasing, a Firecrawl result at
    https://WWW.NewStore.example and a stored homepage at https://www.newstore.example
    would never match each other in discover_retailers' exclusion/dedup sets."""
    assert research_runner._bare_domain("https://WWW.NewStore.example/product") == "newstore.example"
    assert research_runner._bare_domain("https://www.newstore.example") == "newstore.example"


def test_domain_display_name_strips_tld_and_title_cases():
    assert research_runner._domain_display_name("jb-hifi.com.au") == "Jb Hifi"
    assert research_runner._domain_display_name("bunnings.com.au") == "Bunnings"


def test_name_from_search_result_prefers_a_short_trailing_title_segment():
    name = research_runner._name_from_search_result(
        "https://www.example.com.au/x", "Samsung Soundbar HW-Q930H | Example Store"
    )
    assert name == "Example Store"


def test_name_from_search_result_falls_back_to_domain_when_trailing_segment_has_a_digit():
    """A trailing "| $899" or "- SKU1234" is a price or SKU, not a store name - trusting
    it would show the user a nonsense "retailer" instead of an honest domain guess."""
    name = research_runner._name_from_search_result(
        "https://www.example.com.au/x", "Samsung Soundbar HW-Q930H | $899"
    )
    assert name == "Example"


def test_name_from_search_result_falls_back_to_domain_with_no_separator():
    name = research_runner._name_from_search_result(
        "https://www.example.com.au/x", "Samsung Soundbar HW-Q930H"
    )
    assert name == "Example"


def _fake_search_response(monkeypatch, data, success=True):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"success": success, "data": data}

    monkeypatch.setattr(research_runner.requests, "post", lambda *a, **k: FakeResponse())


def test_discover_retailers_returns_candidates_from_search_results(monkeypatch):
    _fake_search_response(monkeypatch, [
        {"url": "https://www.newstore.example/product", "title": "HW-Q930H Soundbar | New Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H", [], [],
    )
    assert result["retailers"] == [{
        "name": "New Store", "homepage": "https://newstore.example",
        "source_url": "https://www.newstore.example/product",
        "source_title": "HW-Q930H Soundbar | New Store",
    }]


def test_discover_retailers_excludes_by_homepage_domain(monkeypatch):
    """The control this test exists for: without domain-based exclusion, a retailer
    already selected for this product would be suggested again as if it were new."""
    _fake_search_response(monkeypatch, [
        {"url": "https://www.knownstore.example/product", "title": "HW-Q930H Soundbar | Known Store"},
        {"url": "https://www.newstore.example/product", "title": "HW-Q930H Soundbar | New Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H",
        [], ["https://www.knownstore.example"],
    )
    names = [r["name"] for r in result["retailers"]]
    assert "Known Store" not in names
    assert "New Store" in names


def test_discover_retailers_excludes_by_name(monkeypatch):
    _fake_search_response(monkeypatch, [
        {"url": "https://www.newstore.example/product", "title": "HW-Q930H Soundbar | New Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H", ["New Store"], [],
    )
    assert result["retailers"] == []


def test_discover_retailers_requires_the_complete_variant_model(monkeypatch):
    _fake_search_response(monkeypatch, [
        {"url": "https://www.newstore.example/product", "title": "HW-Q930H/XU Soundbar | New Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H/XY", [], [],
    )
    assert result["retailers"] == []


def test_discover_retailers_deduplicates_the_same_domain(monkeypatch):
    _fake_search_response(monkeypatch, [
        {"url": "https://www.newstore.example/a", "title": "HW-Q930H Soundbar | New Store"},
        {"url": "https://www.newstore.example/b", "title": "HW-Q930H Soundbar A/V | New Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H", [], [],
    )
    assert len(result["retailers"]) == 1


def test_discover_retailers_excludes_by_homepage_domain_regardless_of_case(monkeypatch):
    _fake_search_response(monkeypatch, [
        {"url": "https://WWW.KnownStore.example/product", "title": "HW-Q930H Soundbar | Known Store"},
    ])
    result = research_runner.discover_retailers(
        "http://firecrawl", "Samsung Soundbar", "HW-Q930H",
        [], ["https://www.knownstore.example"],
    )
    assert result["retailers"] == []


def test_discover_retailers_raises_when_firecrawl_reports_failure(monkeypatch):
    """The control this test exists for: a failed search must not silently look like
    a successful search that simply found nothing (hardening.md rule 2)."""
    _fake_search_response(monkeypatch, [], success=False)
    with pytest.raises(RuntimeError, match="did not succeed"):
        research_runner.discover_retailers("http://firecrawl", "Product", "MODEL", [], [])


def test_discover_retailers_raises_when_the_request_itself_fails(monkeypatch):
    def fake_post(*a, **k):
        raise research_runner.requests.RequestException("connection refused")

    monkeypatch.setattr(research_runner.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="firecrawl search failed"):
        research_runner.discover_retailers("http://firecrawl", "Product", "MODEL", [], [])


# --------------------------------------------------- historical-low research (#93)

def test_parse_historical_low_reply_with_a_confident_finding():
    reply = ('{"found": true, "price": 899, "date": "2025-11-20", '
             '"retailer": "Appliances Online", "confidence": "MEDIUM", '
             '"reason": "Black Friday 2025 price-history thread"}')
    result = research_runner.parse_historical_low_reply(reply)
    assert result == {
        "found": True, "price": 899, "date": "2025-11-20",
        "retailer": "Appliances Online", "confidence": "MEDIUM",
        "reason": "Black Friday 2025 price-history thread",
        "other_retailers": [],
    }


def test_parse_historical_low_reply_formats_reason_points_as_bullet_lines():
    """issue: "the notes should be split into dot points". reason_points (an array)
    is the current prompt shape - joined into "- " prefixed lines so a renderer
    (app/main.py's notes_html filter, app.js's notesElement) can build a real <ul>."""
    reply = (
        '{"found": true, "price": 69.30, "date": "2025-11-18", "retailer": "eBay", '
        '"confidence": "MEDIUM", "reason_points": '
        '["OzBargain thread from 18 Nov 2025: $69.30 via eBay seller Shopping Express", '
        '"Checked deals through Aug 2026 - none went lower"]}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["reason"] == (
        "- OzBargain thread from 18 Nov 2025: $69.30 via eBay seller Shopping Express\n"
        "- Checked deals through Aug 2026 - none went lower"
    )


def test_parse_historical_low_reply_falls_back_to_plain_reason_string():
    """A reply that still uses the pre-bullet-points shape (no reason_points at all)
    must keep working exactly as before - the old test above this one pins that."""
    reply = '{"found": true, "price": 500, "reason": "single sentence, no bullets"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["reason"] == "single sentence, no bullets"


def test_parse_historical_low_reply_ignores_reason_points_with_no_usable_strings():
    reply = '{"found": true, "price": 500, "reason_points": ["", "   "], "reason": "fallback text"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["reason"] == "fallback text"


def test_parse_historical_low_reply_ignores_a_non_list_reason_points():
    reply = '{"found": true, "price": 500, "reason_points": "not a list", "reason": "fallback text"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["reason"] == "fallback text"


def test_truncate_notes_backs_off_to_a_line_boundary_rather_than_cutting_mid_line():
    text = "- " + ("a" * 100) + "\n- " + ("b" * 2000)
    truncated = research_runner._truncate_notes(text, limit=150)
    assert truncated == "- " + ("a" * 100)
    assert "b" not in truncated


def test_truncate_notes_falls_back_to_a_plain_slice_with_no_early_newline():
    text = "a" * 2000
    truncated = research_runner._truncate_notes(text, limit=500)
    assert len(truncated) == 500


def test_truncate_notes_leaves_short_text_untouched():
    assert research_runner._truncate_notes("short", limit=500) == "short"


def test_parse_historical_low_reply_not_found_is_a_finding_not_an_exception():
    """Unlike parse_research_reply, there is no per-retailer status to raise into for
    this kind - "found: false" must come back as a usable (empty) finding, not an
    error, so the runner can still report and complete the job normally."""
    reply = '{"found": false, "price": null, "reason": "no trustworthy source found"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["found"] is False
    assert result["price"] is None
    assert result["reason"] == "no trustworthy source found"


def test_parse_historical_low_reply_with_no_json_is_a_finding_not_an_exception():
    result = research_runner.parse_historical_low_reply("I couldn't find historical pricing.")
    assert result["found"] is False
    assert "no JSON" in result["reason"]


def test_parse_historical_low_reply_with_malformed_json_is_a_finding_not_an_exception():
    result = research_runner.parse_historical_low_reply('{"found": true, "price": }')
    assert result["found"] is False


def test_parse_historical_low_reply_empty_raises():
    """The one case that IS treated as a hard failure: the CLI produced nothing at
    all, as distinct from producing a considered "couldn't find one"."""
    with pytest.raises(ValueError):
        research_runner.parse_historical_low_reply("")


def test_parse_historical_low_reply_rejects_a_nan_price():
    reply = '{"found": true, "price": NaN, "reason": "estimate"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["found"] is False
    assert result["price"] is None


def test_parse_historical_low_reply_drops_an_unrecognised_confidence_value():
    reply = '{"found": true, "price": 500, "confidence": "PRETTY_SURE", "reason": "ok"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["found"] is True
    assert result["confidence"] is None


def test_parse_historical_low_reply_takes_the_last_json_object():
    """Same discipline as parse_research_reply: a worked example before the real
    answer must not shadow it."""
    reply = (
        'For a similar model I found {"found": true, "price": 400, "reason": "similar"} '
        'but for the exact model: '
        '{"found": true, "price": 899, "date": "2025-11-20", "reason": "ok"}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["price"] == 899


def test_research_historical_low_grants_web_search_and_fetch(monkeypatch):
    """Same discipline as the price-research call: neither tool is available by
    default in a non-interactive `-p` call, so the prompt asking the model to search
    is useless without explicitly granting them."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.research_historical_low("http://x", "claude", "Some Product", "SKU-1")
    assert exc_info.value.status == "FAILED"
    args = captured["args"]
    assert "--allowedTools" in args
    tools = args[args.index("--allowedTools") + 1]
    assert "WebSearch" in tools.split(",")
    assert "WebFetch" in tools.split(",")


def test_research_historical_low_reports_a_non_zero_exit_as_failed(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "dead token"

    monkeypatch.setattr(research_runner.subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.research_historical_low("http://x", "claude", "Some Product", "SKU-1")
    assert exc_info.value.status == "FAILED"
    assert "dead token" not in exc_info.value.note
    assert "reconnect" in exc_info.value.note.lower()


def test_missing_subscription_token_fails_without_running_the_cli(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    invoked = False

    def fake_run(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("CLI must not run without the subscription token")

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.SubscriptionAccessUnavailable, match="OAuth token"):
        research_runner._run_research(["claude", "-p"], "query", 5)
    assert not invoked


@pytest.mark.parametrize("search_status", ["completed", "failed", "missing"])
def test_cliproxy_research_requires_completed_web_search(monkeypatch, search_status):
    monkeypatch.setenv("SHOPWATCH_RESEARCH_BACKEND", "cliproxy")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_API_KEY", "local-client-key")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_MODEL", "pi-luna")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    reply = '{"candidates":[],"note":"No matching model found."}'

    class Response:
        status_code = 200

        def json(self):
            output = [{"type": "message", "content": [{"type": "output_text", "text": reply}]}]
            if search_status != "missing":
                output.insert(0, {"type": "web_search_call", "status": search_status})
            return {"status": "completed", "output": output}

    def post(url, **kwargs):
        assert url == "http://127.0.0.1:8317/v1/responses"
        assert kwargs["headers"]["Authorization"] == "Bearer local-client-key"
        assert kwargs["json"]["model"] == "pi-luna"
        assert kwargs["json"]["tools"] == [{"type": "web_search"}]
        assert kwargs["allow_redirects"] is False
        return Response()

    monkeypatch.setattr(research_runner.requests, "post", post)
    monkeypatch.setattr(research_runner.subprocess, "run", lambda *a, **kw: pytest.fail(
        "proxy research must not fall back to a CLI or paid API"
    ))
    args = ["claude", "-p", "--allowedTools", "WebSearch"]
    if search_status == "completed":
        assert research_runner._run_research(args, "query", 5).stdout == reply
    else:
        with pytest.raises(research_runner.SubscriptionAccessUnavailable, match="search"):
            research_runner._run_research(args, "query", 5)


def test_cliproxy_discovery_preserves_exact_page_evidence_without_firecrawl(monkeypatch):
    monkeypatch.setenv("SHOPWATCH_RESEARCH_BACKEND", "cliproxy")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_API_KEY", "local-client-key")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_MODEL", "pi-luna")
    retailers = [{"name": "Shop", "homepage": "https://shop.example",
                  "source_url": "https://shop.example/model-1",
                  "source_title": "MODEL-1 exact product"}]
    completed = []

    def post(url, **kwargs):
        from types import SimpleNamespace

        if url == "http://127.0.0.1:8317/v1/responses":
            return SimpleNamespace(status_code=200, json=lambda: {
                "status": "completed", "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {"type": "message", "content": [{"type": "output_text",
                        "text": json.dumps({"retailers": retailers, "note": None})}]},
                ],
            })
        assert url == "http://shopwatch/api/retailer-search-jobs/42/complete"
        completed.append(kwargs["json"])

    monkeypatch.setattr(research_runner.requests, "post", post)
    research_runner.process_retailer_search_job("http://shopwatch", "http://firecrawl", {
        "id": 42, "query": json.dumps({"product_name": "Widget", "model": "MODEL-1"}),
    })
    assert completed[0]["status"] == "DONE"
    assert json.loads(completed[0]["result"])["retailers"] == retailers


def test_cliproxy_does_not_search_instead_of_reading_a_specific_url(monkeypatch):
    monkeypatch.setenv("SHOPWATCH_RESEARCH_BACKEND", "cliproxy")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_API_KEY", "local-client-key")
    monkeypatch.setenv("SHOPWATCH_CLIPROXY_MODEL", "pi-luna")
    monkeypatch.setattr(research_runner.requests, "post", lambda *a, **kw: pytest.fail(
        "URL-only research must not send a substitute search"
    ))
    with pytest.raises(research_runner.RunnerError, match="URL-only"):
        research_runner.research_one_retailer(
            "http://shopwatch", "claude", "Widget", "MODEL-1", "Shop", None,
            "https://shop.example/widget",
        )


def test_retailer_research_does_not_return_cli_stderr_to_the_gui(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "session expired; secret=do-not-expose"

    monkeypatch.setattr(research_runner.subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(research_runner.RunnerError) as exc_info:
        research_runner.research_one_retailer(
            "http://x", "claude", "Some Product", "SKU-1", "Some Retailer", None
        )
    assert "do-not-expose" not in exc_info.value.note
    assert "reconnect" in exc_info.value.note.lower()


def test_poll_llm_claims_the_gui_job_runs_search_and_returns_sourced_candidates(monkeypatch):
    from types import SimpleNamespace

    calls = []
    invoked = {}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                if url.endswith("/claim"):
                    return {"id": 42, "kind": "model_suggestion", "query": "robot mower"}
                return {"id": 42, "status": "DONE"}
        return Response()

    proc = SimpleNamespace(
        returncode=0,
        stdout='{"candidates":[{"model":"RM-1","label":"Mower","source":"https://shop.example/rm-1"}],"note":null}',
        stderr="",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.setattr(research_runner.requests, "post", fake_post)
    def fake_run(args, **kwargs):
        invoked["args"] = args
        invoked["prompt"] = kwargs["input"]
        invoked["env"] = kwargs["env"]
        invoked["cwd"] = kwargs["cwd"]
        return proc
    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)

    assert research_runner.poll_llm_once("http://shopwatch", "claude") is True
    assert calls[0][0] == "http://shopwatch/api/llm-jobs/claim"
    assert calls[1][0] == "http://shopwatch/api/llm-jobs/42/complete"
    assert calls[1][1]["json"]["status"] == "DONE"
    result = json.loads(calls[1][1]["json"]["result"])
    assert result["candidates"][0]["source"] == "https://shop.example/rm-1"
    assert invoked["args"][invoked["args"].index("--allowedTools") + 1] == "WebSearch"
    assert "WebSearch" in invoked["prompt"]
    assert invoked["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-token"
    assert "ANTHROPIC_API_KEY" not in invoked["env"]
    assert invoked["cwd"] == invoked["env"]["CLAUDE_CONFIG_DIR"]
    settings_arg = invoked["args"][invoked["args"].index("--settings") + 1]
    assert '"ANTHROPIC_API_KEY": ""' in settings_arg


def test_poll_llm_also_handles_gui_retailer_discovery_without_the_desktop_helper(monkeypatch):
    from types import SimpleNamespace

    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            if len(posted) == 1:
                return {"id": 43, "kind": "retailer_discovery", "query": "find AU sellers"}
            return {"id": 43, "status": "DONE"}

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return Response()

    monkeypatch.setattr(research_runner.requests, "post", fake_post)
    monkeypatch.setattr(
        research_runner.subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout='{"retailers":[{"name":"Store","homepage":"https://store.example"}],"note":null}',
            stderr="",
        ),
    )

    assert research_runner.poll_llm_once("http://shopwatch", "claude") is True
    assert json.loads(posted[1][1]["json"]["result"])["retailers"] == [
        {"name": "Store", "homepage": "https://store.example"}
    ]


def test_research_historical_low_returns_the_parsed_finding_on_success(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = '{"found": true, "price": 750, "date": "2025-06-01", "reason": "ok"}'
        stderr = ""

    monkeypatch.setattr(research_runner.subprocess, "run", lambda *a, **k: FakeProc())
    result = research_runner.research_historical_low("http://x", "claude", "Some Product", "SKU-1")
    assert result["found"] is True
    assert result["price"] == 750


# ----------------------------------------- other retailers noticed along the way (#98)

def test_parse_historical_low_reply_captures_other_retailers():
    reply = (
        '{"found": true, "price": 899, "reason": "ok", "other_retailers": '
        '[{"name": "Centre Com", "url": "https://www.centrecom.com.au/x"}, '
        '{"name": "Mwave", "url": null}]}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["other_retailers"] == [
        {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
        {"name": "Mwave", "url": None},
    ]


def test_parse_historical_low_reply_reports_other_retailers_even_with_no_price():
    """A model can honestly say "no confident historical low, but I did see these
    retailers selling it" in the same pass - the two findings are independent."""
    reply = (
        '{"found": false, "reason": "no trustworthy source", '
        '"other_retailers": [{"name": "Mwave", "url": null}]}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["found"] is False
    assert result["other_retailers"] == [{"name": "Mwave", "url": None}]


def test_parse_historical_low_reply_drops_a_malformed_other_retailer_entry():
    """A missing/blank name, or a non-object entry, is dropped rather than failing
    the whole reply - this rides along as incidental information, not the thing
    being validated for correctness the way price/found are."""
    reply = (
        '{"found": true, "price": 100, "reason": "ok", "other_retailers": '
        '[{"name": "  "}, {"url": "https://x.com.au"}, "not an object", '
        '{"name": "Good Guys", "url": "  "}]}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["other_retailers"] == [{"name": "Good Guys", "url": None}]


def test_parse_historical_low_reply_ignores_a_non_list_other_retailers():
    reply = '{"found": true, "price": 100, "reason": "ok", "other_retailers": "Centre Com"}'
    result = research_runner.parse_historical_low_reply(reply)
    assert result["other_retailers"] == []


def test_parse_historical_low_reply_caps_other_retailers():
    many = ", ".join(f'{{"name": "Retailer {i}"}}' for i in range(20))
    reply = f'{{"found": true, "price": 100, "reason": "ok", "other_retailers": [{many}]}}'
    result = research_runner.parse_historical_low_reply(reply)
    assert len(result["other_retailers"]) == research_runner.FIRECRAWL_MAX_CANDIDATES


def test_parse_historical_low_reply_defaults_other_retailers_to_empty_list_when_absent():
    result = research_runner.parse_historical_low_reply(
        '{"found": true, "price": 100, "reason": "ok"}'
    )
    assert result["other_retailers"] == []


def test_parse_historical_low_reply_neutralises_a_javascript_url():
    """candidate.url ultimately came out of the model's own reply to a "search the
    web" prompt - untrusted content relayed through the model, not something safe to
    store as-is. A non-http(s) scheme must never survive into the stored finding,
    since the product page renders this value straight into an anchor's href."""
    reply = (
        '{"found": true, "price": 100, "reason": "ok", "other_retailers": '
        '[{"name": "Evil Co", "url": "javascript:alert(1)"}, '
        '{"name": "Good Co", "url": "https://good.com.au/x"}]}'
    )
    result = research_runner.parse_historical_low_reply(reply)
    assert result["other_retailers"] == [
        {"name": "Evil Co", "url": None},
        {"name": "Good Co", "url": "https://good.com.au/x"},
    ]


def test_safe_http_url_rejects_non_http_schemes():
    assert research_runner._safe_http_url("javascript:alert(1)") is None
    assert research_runner._safe_http_url("data:text/html,<script>1</script>") is None
    assert research_runner._safe_http_url(None) is None
    assert research_runner._safe_http_url("https://good.com.au/x") == "https://good.com.au/x"


def test_process_historical_low_job_forwards_other_retailers(monkeypatch):
    """process_historical_low_job must pass the parsed other_retailers list through
    to POST .../historical-low untouched, the same "no third code path" discipline
    the rest of this feature follows."""

    class FakeResponse:
        def json(self):
            return self._data

    def fake_get(url, timeout=None):
        resp = FakeResponse()
        if url.endswith("/api/retailers"):
            resp._data = [{"id": 1, "name": "Bing Lee", "excluded": False}]
        else:
            resp._data = {"name": "Some Product", "model": "SKU-1"}
        return resp

    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        resp = FakeResponse()
        resp._data = {}
        return resp

    def fake_research_historical_low(base_url, claude_bin, product_name, model, excluded_names=None):
        return {
            "found": True, "price": 899, "date": "2025-11-20", "retailer": "Bing Lee",
            "confidence": "MEDIUM", "reason": "ok",
            "other_retailers": [{"name": "Centre Com", "url": "https://x.com.au"}],
        }

    monkeypatch.setattr(research_runner, "research_historical_low", fake_research_historical_low)
    monkeypatch.setattr(research_runner.requests, "get", fake_get)
    monkeypatch.setattr(research_runner.requests, "post", fake_post)

    job = {"id": 42, "product_id": 1}
    research_runner.process_historical_low_job("http://x", "claude", job)

    hist_low_calls = [p for p in posted if p[0] == "http://x/api/research-jobs/42/historical-low"]
    assert len(hist_low_calls) == 1
    assert hist_low_calls[0][1]["other_retailers"] == [
        {"name": "Centre Com", "url": "https://x.com.au"}
    ]


# ------------------------------------------ excluded retailers named to the LLM (#112)

def test_excluded_retailers_section_is_empty_with_nothing_excluded():
    assert research_runner._excluded_retailers_section(None) == ""
    assert research_runner._excluded_retailers_section([]) == ""
    assert research_runner._excluded_retailers_section(["   ", ""]) == ""


def test_excluded_retailers_section_names_every_excluded_retailer():
    section = research_runner._excluded_retailers_section(["Bing Lee", "Officeworks"])
    assert "Bing Lee" in section
    assert "Officeworks" in section
    assert "excluded" in section.lower()


def test_research_historical_low_names_excluded_retailers_in_the_prompt(monkeypatch):
    """issue #112 item 8: the model must be told directly not to bother with an
    excluded retailer, not just have its find discarded after the fact - so the
    excluded names have to actually reach the prompt text sent to the CLI."""
    captured = {}

    def fake_run(args, input=None, **kwargs):
        captured["prompt"] = input
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_historical_low(
            "http://x", "claude", "Some Product", "SKU-1", ["Bing Lee"],
        )
    assert "Bing Lee" in captured["prompt"]


def test_research_historical_low_with_no_exclusions_omits_the_section(monkeypatch):
    captured = {}

    def fake_run(args, input=None, **kwargs):
        captured["prompt"] = input
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_historical_low("http://x", "claude", "Some Product", "SKU-1")
    assert "excluded" not in captured["prompt"].lower()


def test_process_historical_low_job_only_forwards_excluded_retailer_names(monkeypatch):
    class FakeResponse:
        def json(self):
            return self._data

    def fake_get(url, timeout=None):
        resp = FakeResponse()
        if url.endswith("/api/retailers"):
            resp._data = [
                {"id": 1, "name": "Bing Lee", "excluded": True},
                {"id": 2, "name": "JB Hi-Fi", "excluded": False},
            ]
        else:
            resp._data = {"name": "Some Product", "model": "SKU-1"}
        return resp

    monkeypatch.setattr(research_runner.requests, "get", fake_get)
    monkeypatch.setattr(research_runner.requests, "post", lambda *a, **k: FakeResponse())

    captured = {}

    def fake_research_historical_low(base_url, claude_bin, product_name, model, excluded_names=None):
        captured["excluded_names"] = excluded_names
        return {
            "found": False, "price": None, "date": None, "retailer": None,
            "confidence": None, "reason": "not found", "other_retailers": [],
        }

    monkeypatch.setattr(research_runner, "research_historical_low", fake_research_historical_low)

    research_runner.process_historical_low_job("http://x", "claude", {"id": 1, "product_id": 1})
    assert captured["excluded_names"] == ["Bing Lee"]


def test_process_historical_low_job_fails_the_job_when_the_retailer_list_cannot_be_loaded(
    monkeypatch,
):
    """A failure to fetch the excluded list must never silently mean "nothing is
    excluded" - that would be a search that ran unfiltered while looking exactly like
    one that filtered correctly (hardening.md rule 2: no representable pass on a
    skipped check)."""

    class FakeResponse:
        def json(self):
            return self._data

    def fake_get(url, timeout=None):
        if url.endswith("/api/retailers"):
            raise research_runner.requests.RequestException("connection refused")
        resp = FakeResponse()
        resp._data = {"name": "Some Product", "model": "SKU-1"}
        return resp

    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        resp = FakeResponse()
        resp._data = {}
        return resp

    monkeypatch.setattr(research_runner.requests, "get", fake_get)
    monkeypatch.setattr(research_runner.requests, "post", fake_post)

    research_runner.process_historical_low_job("http://x", "claude", {"id": 7, "product_id": 1})

    complete_calls = [p for p in posted if p[0] == "http://x/api/research-jobs/7/complete"]
    assert len(complete_calls) == 1
    assert complete_calls[0][1]["status"] == "FAILED"


def test_the_claude_call_grants_web_search_and_fetch(monkeypatch):
    """The prompt tells the model to search the web and read the retailer's own
    site, but neither tool is available by default in a non-interactive `-p` call
    with no TTY to approve a permission prompt - the CLI just declines in plain
    text instead, which then fails the JSON check and reports every retailer as
    NEEDS_MANUAL_CHECK regardless of whether the price actually exists. Same
    discipline app/offers.py already uses for its own subprocess call
    (--allowedTools Read)."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_one_retailer(
            "http://x", "claude", "Some Product", "SKU-1", "Some Retailer", None
        )
    args = captured["args"]
    assert "--allowedTools" in args
    tools = args[args.index("--allowedTools") + 1]
    assert "WebSearch" in tools.split(",")
    assert "WebFetch" in tools.split(",")


# ------------------------------------------------------ URL-scoped requests (issue #94)


def test_a_url_scoped_request_uses_the_url_prompt_and_names_the_exact_page(monkeypatch):
    captured = {}

    def fake_run(args, input=None, **kwargs):
        captured["args"] = args
        captured["prompt"] = input
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_one_retailer(
            "http://x", "claude", "Some Product", "SKU-1", "Centre Com", None,
            "https://www.centrecom.com.au/some-product",
        )

    assert "https://www.centrecom.com.au/some-product" in captured["prompt"]
    assert "Read the page at this exact URL directly" in captured["prompt"]


def test_a_url_scoped_request_withholds_websearch(monkeypatch):
    """The structural half of "read this exact page, don't search": WebSearch must not
    even be a tool the model has available, not just a tool the prompt asks it to
    avoid."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_one_retailer(
            "http://x", "claude", "Some Product", "SKU-1", "Centre Com", None,
            "https://www.centrecom.com.au/some-product",
        )

    args = captured["args"]
    tools = args[args.index("--allowedTools") + 1].split(",")
    assert "WebFetch" in tools
    assert "WebSearch" not in tools


def test_a_request_with_no_url_still_uses_the_general_prompt_and_both_tools(monkeypatch):
    captured = {}

    def fake_run(args, input=None, **kwargs):
        captured["args"] = args
        captured["prompt"] = input
        raise research_runner.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(research_runner.subprocess, "run", fake_run)
    with pytest.raises(research_runner.RunnerError):
        research_runner.research_one_retailer(
            "http://x", "claude", "Some Product", "SKU-1", "JB Hi-Fi", "https://www.jbhifi.com.au",
        )

    assert "Read the page at this exact URL directly" not in captured["prompt"]
    tools = captured["args"][captured["args"].index("--allowedTools") + 1].split(",")
    assert {"WebSearch", "WebFetch"} <= set(tools)


def test_process_job_passes_the_result_row_url_through(monkeypatch):
    """process_job must forward each result row's own url, not the product's or a
    previous row's - a job mixing a URL-scoped retailer with an ordinary one must not
    cross-contaminate them."""
    captured_urls = []

    def fake_research_one_retailer(base_url, claude_bin, product_name, model,
                                    retailer_name, homepage, url=None):
        captured_urls.append(url)
        return {"price": 100, "stock": None, "url": url, "reason": "ok"}

    class FakeResponse:
        def json(self):
            return self._data

    def fake_get(url, timeout=None):
        resp = FakeResponse()
        resp._data = {"name": "Some Product", "model": "SKU-1"}
        return resp

    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        resp = FakeResponse()
        resp._data = {"results": []}
        return resp

    monkeypatch.setattr(research_runner, "research_one_retailer", fake_research_one_retailer)
    monkeypatch.setattr(research_runner.requests, "get", fake_get)
    monkeypatch.setattr(research_runner.requests, "post", fake_post)

    job = {
        "id": 1,
        "product_id": 1,
        "results": [
            {"retailer_id": 1, "retailer_name": "Centre Com", "retailer_homepage": None,
             "url": "https://www.centrecom.com.au/x"},
            {"retailer_id": 2, "retailer_name": "JB Hi-Fi",
             "retailer_homepage": "https://www.jbhifi.com.au", "url": None},
        ],
    }
    research_runner.process_job("http://x", "claude", job)

    assert captured_urls == ["https://www.centrecom.com.au/x", None]
