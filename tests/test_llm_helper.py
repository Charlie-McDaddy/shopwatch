"""Reply parsing for tools/llm-helper.py - the part that can be tested without a real
`claude`/`codex` subprocess. See that script's own docstring for why it exists and where
it runs (on the user's own machine, never inside the shopwatch container)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from app import llm_jobs

_PATH = Path(__file__).resolve().parents[1] / "tools" / "llm-helper.py"
_SPEC = importlib.util.spec_from_file_location("llm_helper", _PATH)
llm_helper = importlib.util.module_from_spec(_SPEC)
sys.modules["llm_helper"] = llm_helper
_SPEC.loader.exec_module(llm_helper)


def test_a_clean_json_reply_parses():
    reply = '{"candidates": [{"model": "DREAME-A2", "label": "Dreame A2"}], "note": null}'
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == [{"model": "DREAME-A2", "label": "Dreame A2"}]
    assert result["note"] is None


def test_a_fenced_reply_still_parses():
    reply = 'Sure thing:\n```json\n{"candidates": [], "note": "too new to be sure"}\n```'
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == []
    assert result["note"] == "too new to be sure"


def test_zero_candidates_is_valid_not_an_error():
    """The honest answer when unsure - must not be treated as a parse failure."""
    reply = '{"candidates": [], "note": "no real Dreame RoboMower model exists yet"}'
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == []


def test_web_reply_requires_candidates_to_have_a_full_http_source():
    reply = ('{"candidates": ['
             '{"model":"GOOD-1","source":"https://shop.example/item"},'
             '{"model":"BAD-1","source":"javascript:alert(1)"},'
             '{"model":"NO-HOST","source":"https:"}],"note":null}')
    result = llm_helper.parse_web_reply(reply)
    assert result["candidates"] == [
        {"model": "GOOD-1", "label": "GOOD-1", "source": "https://shop.example/item"}
    ]


def test_web_reply_reports_when_every_candidate_lacks_a_source():
    result = llm_helper.parse_web_reply(
        '{"candidates":[{"model":"UNSOURCED-1"}],"note":null}'
    )
    assert result["candidates"] == []
    assert "source" in result["note"].lower()


def test_a_candidate_missing_a_model_is_dropped_not_kept_blank():
    reply = '{"candidates": [{"model": "", "label": "no idea"}, {"model": "REAL-1"}], "note": null}'
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == [{"model": "REAL-1", "label": "REAL-1"}]


def test_more_than_five_candidates_is_truncated():
    many = [{"model": f"M{i}", "label": f"M{i}"} for i in range(9)]
    reply = f'{{"candidates": {many!r}, "note": null}}'.replace("'", '"')
    result = llm_helper.parse_reply(reply)
    assert len(result["candidates"]) == 5


def test_reply_with_no_json_object_at_all():
    with pytest.raises(ValueError, match="no JSON"):
        llm_helper.parse_reply("I'm not sure what that product is.")


def test_a_truncated_json_reply_reports_what_the_cli_actually_said():
    """Regression: switching parse_reply to raw_decode (to fix the trailing-prose bug)
    initially dropped the diagnostic snippet for the separate case of a reply that has
    a `{` but is not valid JSON at all (e.g. cut off mid-object by a hung CLI or the
    call timeout) - whoever is debugging a failed wizard request needs to see what the
    CLI actually said, not a bare JSONDecodeError with no content.

    Matches against `repr(reply[:150])`, built the same way the error message is,
    rather than `re.escape(reply)` directly: a code-review pass caught that the two
    diverge whenever the reply contains an apostrophe, because Python's repr() picks
    its quote style (and what it escapes) based on the string's own content, not on
    whatever quoting `re.escape` was given.
    """
    reply = "{\"candidates\": [{\"model\": \"REAL-1\", \"label\": \"Rodney's cut off mid-str"
    with pytest.raises(ValueError, match=re.escape(repr(reply[:150]))):
        llm_helper.parse_reply(reply)


def test_reply_missing_the_candidates_key():
    with pytest.raises(ValueError, match="candidates"):
        llm_helper.parse_reply('{"note": "hmm"}')


def test_a_reply_with_trailing_prose_after_the_json_still_parses():
    """A real Claude reply hit this: a clean JSON object followed by trailing text
    containing its own brace, which broke the old first-`{`/last-`}` slice (it grabbed
    everything up to the LAST `}` in the whole reply, not just the JSON object's own
    closing brace)."""
    reply = (
        '{"candidates": [{"model": "QA55S90DAWXXY", "label": "Samsung 55\\" S90D OLED (AU)"}], '
        '"note": null}\n\n(Let me know if you meant a different size, e.g. {55, 65}.)'
    )
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == [{"model": "QA55S90DAWXXY", "label": 'Samsung 55" S90D OLED (AU)'}]


def test_a_leading_draft_does_not_shadow_the_real_answer():
    """A code-review pass caught the mirror-image defect the raw_decode fix
    introduced: it took the FIRST complete JSON object, so a reply that states a
    draft or a reference case before the real answer silently returned the wrong
    one instead of erroring. Every reproduced example had the real answer LAST."""
    reply = (
        'For an unrecognised product I would say {"candidates": [], "note": "unrecognised"}. '
        'For this one: {"candidates": [{"model": "DREAME-A2", "label": "Dreame A2"}], "note": null}'
    )
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == [{"model": "DREAME-A2", "label": "Dreame A2"}]


def test_a_non_dict_candidate_item_is_dropped_not_a_crash():
    """A code-review pass caught this: candidates[:5] items were never type-checked
    before .get() was called, so a model returning a bare string in the list raised
    an unhandled AttributeError instead of being treated like any other malformed
    candidate."""
    reply = '{"candidates": ["oops-a-string", {"model": "REAL-1"}], "note": null}'
    result = llm_helper.parse_reply(reply)
    assert result["candidates"] == [{"model": "REAL-1", "label": "REAL-1"}]


def test_build_prompt_includes_the_query():
    prompt = llm_helper.build_prompt("Dreame RoboMower")
    assert "Dreame RoboMower" in prompt


# --------------------------------------------------------- retailer discovery (kind 2)

def test_a_clean_retailer_reply_parses():
    reply = '{"retailers": [{"name": "Bunnings", "homepage": "https://www.bunnings.com.au"}], "note": null}'
    result = llm_helper.parse_retailer_reply(reply)
    assert result["retailers"] == [{"name": "Bunnings", "homepage": "https://www.bunnings.com.au"}]


def test_zero_retailers_is_valid_not_an_error():
    reply = '{"retailers": [], "note": "nothing beyond what is already listed"}'
    result = llm_helper.parse_retailer_reply(reply)
    assert result["retailers"] == []


def test_a_retailer_missing_a_name_is_dropped_not_kept_blank():
    reply = '{"retailers": [{"name": "", "homepage": "https://example.com"}, {"name": "Bunnings"}]}'
    result = llm_helper.parse_retailer_reply(reply)
    assert result["retailers"] == [{"name": "Bunnings", "homepage": None}]


def test_a_retailer_with_no_homepage_gets_null_not_a_crash():
    reply = '{"retailers": [{"name": "Bunnings"}], "note": null}'
    result = llm_helper.parse_retailer_reply(reply)
    assert result["retailers"] == [{"name": "Bunnings", "homepage": None}]


def test_web_retailer_reply_requires_a_verified_homepage():
    reply = ('{"retailers": ['
             '{"name":"Good Store","homepage":"https://store.example"},'
             '{"name":"Unsourced Store","homepage":null}],"note":null}')
    result = llm_helper.parse_web_retailer_reply(reply)
    assert result["retailers"] == [
        {"name": "Good Store", "homepage": "https://store.example"}
    ]


def test_more_than_eight_retailers_is_truncated():
    many = [{"name": f"Retailer {i}"} for i in range(12)]
    reply = f'{{"retailers": {many!r}, "note": null}}'.replace("'", '"')
    result = llm_helper.parse_retailer_reply(reply)
    assert len(result["retailers"]) == 8


def test_retailer_reply_missing_the_retailers_key():
    with pytest.raises(ValueError, match="retailers"):
        llm_helper.parse_retailer_reply('{"note": "hmm"}')


def test_build_retailer_prompt_includes_the_query():
    prompt = llm_helper.build_retailer_prompt("Product: Dreame RoboMower. Already checking: Bunnings.")
    assert "Dreame RoboMower" in prompt
    assert "Bunnings" in prompt


def test_job_kinds_map_covers_every_kind_the_server_knows_about():
    """The control this test exists for: a kind the server will accept (llm_jobs.KINDS)
    but this script's JOB_KINDS doesn't recognise is exactly the scenario
    resolve_job_kind must raise on - checked against the real source of truth, not a
    hardcoded literal that could drift from it unnoticed."""
    assert set(llm_helper.JOB_KINDS) == set(llm_jobs.KINDS)
    assert llm_helper.JOB_KINDS["retailer_discovery"] == (
        llm_helper.build_retailer_prompt, llm_helper.parse_retailer_reply, "retailers",
    )


def test_resolve_job_kind_returns_the_matching_builder_and_parser():
    build, parse, result_key = llm_helper.resolve_job_kind("retailer_discovery")
    assert build is llm_helper.build_retailer_prompt
    assert parse is llm_helper.parse_retailer_reply
    assert result_key == "retailers"


def test_resolve_job_kind_raises_loudly_for_an_unrecognised_kind():
    """The control this test exists for: an out-of-date llm-helper.py copy claiming a
    job of a kind it predates must fail that job, not silently answer it with the
    wrong prompt and parser - see resolve_job_kind's own docstring."""
    with pytest.raises(ValueError, match="unrecognised job kind"):
        llm_helper.resolve_job_kind("a_future_kind_this_copy_predates")
