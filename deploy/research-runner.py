#!/usr/bin/env python3
"""The wizard's "let's go" step, run from opti - never from inside the shopwatch container.

Mirrors app/mailwatch.py's existing wiring exactly: runs as a host-level script in its
own venv, sources the Claude Code OAuth token that already exists in
/srv/prod/career/runner.env (nothing new to store or rotate), and talks to the running
shopwatch container over plain HTTP on the docker network. The container never holds
this credential and never gains new outbound egress - only this script does.

Also claims app/retailer_search.py's queue: same host-level shape, but calling the
self-hosted Firecrawl instance on opti's own loopback (127.0.0.1:3002 by default,
$FIRECRAWL_URL/--firecrawl-url override it) instead of the Claude CLI. Firecrawl sits
on its own docker network, bound to 127.0.0.1 on the host, and is not reachable from the
shopwatch container's own docker network - this script CAN reach it because it runs
directly on the opti host, not inside a container. That is the whole reason this queue
is claimed here rather than called directly from app/main.py.

    ssh root@YOUR-SERVER-IP
    set -a; . /srv/prod/career/runner.env; set +a
    cd /srv/prod/shopwatch/repo
    export SHOPWATCH_URL="http://$(docker inspect shopwatch --format \
        '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'):8477"
    /srv/prod/shopwatch/research-venv/bin/python deploy/research-runner.py --interval 10

Wired into opti as shopwatch-research-runner.timer, firing every 2 minutes via
shopwatch-research-runner.sh (see deploy/).

Default-deny is enforced server-side (research.create_job only ever queues retailer ids
that already exist in shopwatch's own retailers table), so this script never has to
decide which domains are in scope - it only ever sees retailers the job itself named.

Also claims a second kind of research_jobs row (issue #93): kind="historical_low" asks
"has this ever been cheaper" instead of "what's it selling for today", once per job
rather than once per retailer (process_historical_low_job). The finding is reported via
POST .../historical-low, never through /api/import - it is a best-effort estimate for a
human to confirm or discard on the product page, not a confirmed listing. Since that
same search inevitably turns up other retailers currently selling the product, issue
#98 has it also report those (best-effort, name + URL) on the same call, for the
product page to offer as tickable "add as a listing" candidates.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("shopwatch.research_runner")

#: A live search plus reading a handful of result snippets - not a multi-page research
#: pass, so this stays well under retailer_search.JOB_CEILING_SECONDS (120s).
FIRECRAWL_SEARCH_TIMEOUT_SECONDS = 30

#: Firecrawl returns up to this many results; capped independently in case a future
#: query style returns more than the wizard should ever render for one-shot approval.
FIRECRAWL_MAX_CANDIDATES = 8

#: Per-retailer sub-timeout: one slow or hung retailer must not sink the whole job.
#: Matches the house convention already set by app/offers.py's own CLI timeouts.
RETAILER_TIMEOUT_SECONDS = 90

#: CLI stderr can contain account or session details. Keep it out of job results,
#: HTTP responses, and logs; the host operator can inspect local CLI authentication.
CLI_ACCESS_FAILURE = (
    "Claude Code could not complete this search. Reconnect the Claude Code session "
    "on the research host, then retry."
)

#: A historical-low pass reads more sources than a single retailer's product page (a
#: price-history tracker, a deal-forum thread, a comparison site), so it gets more time
#: than RETAILER_TIMEOUT_SECONDS - still one single-shot call, still well under
#: JOB_CEILING_SECONDS.
HISTORICAL_LOW_TIMEOUT_SECONDS = 150

#: A sourced model lookup is a short single-search job, separate from the existing
#: longer retailer price and historical research calls.
LLM_TIMEOUT_SECONDS = 90

CLAUDE_BILLING_OVERRIDES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_VERTEX_REGION",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)


class SubscriptionAccessUnavailable(RuntimeError):
    """The host has no Claude.ai OAuth token configured for subscription-backed jobs."""


def _run_subscription_claude(args: list[str], prompt: str, timeout: int) -> Any:
    """Run Claude with the explicit subscription token and no API/provider overrides."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise SubscriptionAccessUnavailable(
            "Claude subscription OAuth token is not configured on the research host."
        )
    env = os.environ.copy()
    for name in CLAUDE_BILLING_OVERRIDES:
        env.pop(name, None)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    settings = json.dumps({"env": {name: "" for name in CLAUDE_BILLING_OVERRIDES}})
    with tempfile.TemporaryDirectory(prefix="shopwatch-claude-") as isolated_cli_home:
        env["CLAUDE_CONFIG_DIR"] = isolated_cli_home
        return subprocess.run(
            [*args, "--settings", settings], input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=isolated_cli_home, env=env,
        )

#: Hard ceiling for the whole job, independent of how many retailers it names. Backstops
#: app/research.py's own time-based staleness check (JOB_CEILING_SECONDS) from the other
#: side: if this script is still running past the point the server would have already
#: reconciled the job to FAILED, stop reporting into a job that no longer exists as RUNNING.
JOB_CEILING_SECONDS = 600

PROMPT = """You are researching the current price of one specific product at one
specific retailer, for a personal price-tracking tool. Use web search and read the
retailer's own site directly - do not guess or use general knowledge about typical
prices.

Product: {product_name}
Exact model: {model}
Retailer: {retailer_name}{homepage_hint}

Find this exact model's current price at this retailer only. If you cannot find this
exact model at this retailer (wrong model, no listing, the page blocks automated
access, or you are not confident it is the same product), say so - a wrong price is
worse than no price.

Return ONLY a JSON object, no prose, no code fence, matching this shape:
{{
  "found": true or false,
  "price": number or null (the advertised price, before freight),
  "stock": string or null (e.g. "In stock", "Out of stock"),
  "url": string or null (the product page you read),
  "reason": string (why not found, if found is false; brief confirmation if true)
}}
"""

#: issue #93: "has this ever been cheaper", not "what does it cost today". One call for
#: the whole product, not one per retailer - a historical low is a single fact about the
#: item, not something separate at each retailer. Deliberately asks for AU price-history
#: trackers (the same kind of source a person would check by hand) rather than the
#: model's own training-data recall of "typical" prices, and deliberately asks the model
#: to say how confident it is, since this is going to be shown to a human as an estimate
#: to confirm or discard, never as a fact.
HISTORICAL_LOW_PROMPT = """You are researching the lowest price this specific product
has ever been sold for in Australia, including BEFORE today - this is a historical
question, not a current-price check. Use web search: price-history trackers (e.g.
camelcamelcamel-style sites, Australian deal-tracking sites/forums such as OzBargain),
cached or archived retailer pages, and reputable price-comparison sites that show past
prices. Do not rely on general knowledge or a guess about "typical" prices for this
category - only report a price you can point to a source for.

Product: {product_name}
Exact model: {model}
{excluded_section}
Find the lowest confirmed price you can for this exact model, at any legitimate
Australian retailer, at any point in time (including now, if today's price happens to
be the lowest ever seen). If you cannot find a specific historical price you are
reasonably confident is this exact model, say so - a wrong number is worse than no
number, since a person will use this to decide whether a current price is actually
good.

Separately, while you are searching: note any OTHER Australian retailers you notice
currently selling this exact model, even ones you did not end up using as your
historical-low source. This is a bonus, not a second search - only report retailers
you actually saw while looking for the historical low, never invent or guess one.

Return ONLY a JSON object, no prose, no code fence, matching this shape:
{{
  "found": true or false,
  "price": number or null (the lowest price found, before freight),
  "date": string or null (YYYY-MM-DD if known, or a rough period like "mid 2025" if
    that's the best you have - null if you cannot even estimate when),
  "retailer": string or null (who sold it at that price),
  "confidence": "LOW", "MEDIUM" or "HIGH" (how sure you are this is a real historical
    low for this exact model, not a guess or a different model/size),
  "reason_points": array of short strings, one distinct fact or source per string (e.g.
    ["OzBargain thread from 18 Nov 2025: $69.30 via eBay seller Shopping Express, flagged
    as an all-time low", "Checked OzBargain-tracked deals through Aug 2026 (Amazon AU,
    Amazon Business, Officeworks, Costco, digiDirect, KS Computer) - none went lower"]) -
    what source(s) you used, or why nothing usable was found, broken into separate points
    rather than one long sentence - a person reading this wants to scan it, not parse it,
  "other_retailers": [] or a list of objects like {{"name": string, "url": string or
    null}}, one per OTHER retailer you noticed selling this exact model - empty list
    if you did not notice any
}}
"""

#: Used instead of PROMPT when the job names an exact page to read (issue #94's "paste
#: a listing URL" path). Deliberately does not invite a web search at all: the whole
#: point of having a URL in hand is that the runner reads the page the person actually
#: found, not whatever a generic search turns up for the same retailer.
PROMPT_URL = """You are confirming the current price on one specific retailer page,
for a personal price-tracking tool. Read the page at this exact URL directly - do not
search the web, and do not substitute a different page even if you believe you know a
better one.

Product: {product_name}
Exact model: {model}
Retailer: {retailer_name}
URL: {url}

Read that page and find this exact model's current price. If the page cannot be
reached (blocked, 404, moved), or the model on the page does not match, or you are not
confident it is the same product, say so - a wrong price is worse than no price.

Return ONLY a JSON object, no prose, no code fence, matching this shape:
{{
  "found": true or false,
  "price": number or null (the advertised price, before freight),
  "stock": string or null (e.g. "In stock", "Out of stock"),
  "url": string or null (the URL you were given, echoed back),
  "reason": string (why not found, if found is false; brief confirmation if true)
}}
"""


class RunnerError(RuntimeError):
    """A retailer attempt failed in a way worth reporting as its own status."""

    def __init__(self, status: str, note: str) -> None:
        super().__init__(note)
        self.status = status
        self.note = note


def _last_json_object(raw: str, start: int) -> dict[str, Any]:
    """Scan for every complete JSON object at or after `start`, keeping the LAST one
    that parses. A reply with an earlier draft, worked example, or reference case
    followed by the real answer states the real one last in every case seen in
    practice; a stray brace in trailing prose (a parenthetical, a size list) is
    usually not valid JSON on its own and gets skipped. This does not guarantee
    semantic correctness when a reply genuinely contains two independently valid
    JSON objects, but matches every reproduced real-world pattern, where the later
    one was the intended answer. A code fence's backticks are not brace characters,
    so this also handles fenced replies (including two separately fenced blocks)
    without needing to strip fences first. RecursionError (pathological nesting) is
    treated the same as a parse failure: skip that candidate and keep looking.
    """
    decoder = json.JSONDecoder()
    best: dict[str, Any] | None = None
    idx = start
    while True:
        brace = raw.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, brace)
        except (json.JSONDecodeError, RecursionError):
            idx = brace + 1
            continue
        best = obj
        idx = end
    if best is None:
        raise ValueError(f"reply was not valid JSON: no candidate object parsed ({raw[:150]!r})")
    return best


def parse_research_reply(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a CLI reply and validate it has a usable price.

    Kept separate from the subprocess call so it can be tested without running
    anything, same discipline as app/offers.py's parse_cli_output.
    """
    raw = (raw or "").strip()
    start = raw.find("{")
    if start == -1:
        raise RunnerError("NEEDS_MANUAL_CHECK", f"no JSON in reply: {raw[:150]!r}")
    try:
        data = _last_json_object(raw, start)
    except ValueError as exc:
        raise RunnerError("NEEDS_MANUAL_CHECK", str(exc)) from exc

    price = data.get("price")
    # bool is an int subclass in Python, and NaN is a float that survives an
    # `is None` check - both are real replies seen from a model that ignored the
    # requested shape, and both must not be treated as a usable price.
    price_is_usable = (
        isinstance(price, int | float) and not isinstance(price, bool)
        and math.isfinite(price)
    )
    if data.get("found") is not True or not price_is_usable:
        raise RunnerError("NEEDS_MANUAL_CHECK", str(data.get("reason") or "not found"))
    return data


def _safe_http_url(url: str | None) -> str | None:
    """http(s)-only allowlist for a URL that ultimately came out of an LLM's reply to
    a prompt asking it to search the open web - this is untrusted content relayed
    through the model, not content the model itself is trusted to have sanitised.
    Anything else (javascript:, data:, an unparseable string) is dropped rather than
    stored or ever reaching an href - the product page renders `candidate.url`
    straight into a link's href attribute, so a `javascript:` URL surviving this far
    would execute in the page on click. Checked here (server-side, before the value
    is even stored) as well as again in app.js before it is rendered, since the value
    is also exposed as-is through the API and must not rely on client-side
    validation alone.
    """
    if not url:
        return None
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def _clean_other_retailers(raw: Any) -> list[dict[str, Any]]:
    """Best-effort coercion of the model's own "other retailers noticed" list
    (issue #98). Any malformed item is dropped rather than failing the whole reply -
    this rides along with the historical-low answer as incidental information, not
    the thing being validated for correctness the way price/found are. Capped at
    FIRECRAWL_MAX_CANDIDATES, the same limit discover_retailers already applies, so a
    verbose reply cannot blow out the product page's checkbox list.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            continue
        url = item.get("url")
        url = url.strip() if isinstance(url, str) and url.strip() else None
        cleaned.append({"name": name, "url": _safe_http_url(url)})
        if len(cleaned) >= FIRECRAWL_MAX_CANDIDATES:
            break
    return cleaned


#: Was 500 - too tight once "reason" can be several bullet lines rather than one
#: sentence, and a plain character slice cut mid-word/mid-sentence regardless (the
#: "$70.37-$80.. Check before saving." artifact seen live on a real product came from
#: exactly this: a 500-char slice landing mid-number, immediately followed by the
#: dialog's own trailing ". Check before saving." with nothing to signal the cut).
NOTES_LENGTH_LIMIT = 1200


def _truncate_notes(text: str, limit: int = NOTES_LENGTH_LIMIT) -> str:
    """Cut `text` to at most `limit` characters without severing a bullet line (or, for
    old-style single-paragraph text, a sentence) mid-word. Backs off to the last
    newline within the limit when there is one comfortably placed; otherwise falls
    back to a plain slice, same behaviour as before for text with no line breaks at
    all - never worse than the previous [:limit], only ever better."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut > limit * 0.5:
        return text[:cut]
    return text[:limit]


def _format_reason(data: dict[str, Any], found: bool) -> str:
    """Build the human-readable "reason" text from the model's reply.

    Prefers `reason_points` (a list of short strings, one distinct fact/source each) -
    joined into "- " prefixed lines so a renderer (the product page, the historical-low
    dialog) can split on newline and show real bullet points instead of one dense
    run-on paragraph a person has to parse rather than scan. Falls back to a plain
    `reason` string for a reply that still uses the pre-bullet-points schema (an older
    prompt version cached in a stale CLI session, or a model that ignored the new
    field), so nothing already relying on that shape breaks - see the fallback-covering
    tests below.
    """
    points = data.get("reason_points")
    if isinstance(points, list):
        cleaned = [str(p).strip() for p in points if isinstance(p, str) and str(p).strip()]
        if cleaned:
            return "\n".join(f"- {p}" for p in cleaned)
    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "not found" if not found else ""


def parse_historical_low_reply(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a historical-low CLI reply.

    Deliberately does NOT raise RunnerError for a well-formed "not found" or
    unparseable reply, unlike parse_research_reply: a historical-low job has no
    per-retailer result to attach a NEEDS_MANUAL_CHECK status to, so "the model ran
    and came back with nothing usable" is reported as a legitimate empty finding
    (found: False) rather than a failure of the job itself - see
    process_historical_low_job. Only a genuinely empty reply raises, for the caller to
    treat as a hard failure (the CLI produced nothing at all, as distinct from the CLI
    producing a considered "couldn't find one").
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty reply")

    start = raw.find("{")
    if start == -1:
        return {
            "found": False, "price": None, "date": None, "retailer": None,
            "confidence": None, "reason": f"no JSON in reply: {raw[:150]!r}",
            "other_retailers": [],
        }
    try:
        data = _last_json_object(raw, start)
    except ValueError as exc:
        return {
            "found": False, "price": None, "date": None, "retailer": None,
            "confidence": None, "reason": str(exc), "other_retailers": [],
        }

    price = data.get("price")
    price_is_usable = (
        isinstance(price, int | float) and not isinstance(price, bool)
        and math.isfinite(price)
    )
    found = data.get("found") is True and price_is_usable
    confidence = data.get("confidence")
    if confidence not in ("LOW", "MEDIUM", "HIGH"):
        confidence = None
    return {
        "found": found,
        "price": price if found else None,
        "date": data.get("date") if found else None,
        "retailer": data.get("retailer") if found else None,
        "confidence": confidence if found else None,
        "reason": _format_reason(data, found),
        # Independent of whether a historical-low price itself was found - the model
        # can honestly report "no confident historical low, but I did see these
        # retailers selling it" in the same pass.
        "other_retailers": _clean_other_retailers(data.get("other_retailers")),
    }


def research_one_retailer(
    base_url: str,
    claude_bin: str,
    product_name: str,
    model: str,
    retailer_name: str,
    homepage: str | None,
    url: str | None = None,
) -> dict[str, Any]:
    """Ask the headless CLI about one retailer. Returns a parsed finding or raises RunnerError.

    `url` scopes the whole request to one specific page (issue #94) rather than a
    general search - see PROMPT_URL's own docstring-equivalent comment above. The tool
    grant is narrowed to match: WebSearch is withheld outright rather than merely
    asked not to be used, so "read this exact page" is enforced structurally, not just
    by a sentence in the prompt the model could ignore.
    """
    if url:
        prompt = PROMPT_URL.format(
            product_name=product_name, model=model, retailer_name=retailer_name, url=url
        )
        allowed_tools = "WebFetch"
    else:
        hint = f" ({homepage})" if homepage else ""
        prompt = PROMPT.format(
            product_name=product_name, model=model, retailer_name=retailer_name,
            homepage_hint=hint,
        )
        allowed_tools = "WebSearch,WebFetch"
    try:
        proc = _run_subscription_claude(
            [claude_bin, "-p", "--allowedTools", allowed_tools], prompt,
            RETAILER_TIMEOUT_SECONDS,
        )
    except SubscriptionAccessUnavailable as exc:
        raise RunnerError("BLOCKED", str(exc)) from exc
    except FileNotFoundError as exc:
        raise RunnerError("NEEDS_MANUAL_CHECK", f"{claude_bin} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(
            "TIMED_OUT", f"no reply within {RETAILER_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        # Stderr is deliberately not persisted: it can contain session details.
        raise RunnerError("BLOCKED", CLI_ACCESS_FAILURE)

    return parse_research_reply(proc.stdout)


def _excluded_retailers_section(excluded_names: list[str] | None) -> str:
    """The optional block HISTORICAL_LOW_PROMPT gets when the caller passes on the
    current excluded-retailer list (issue #112, item 8). Telling the model directly
    not to bother with these matters more than only filtering them out afterwards
    (research.py's _clean_other_retailers, which still runs too, as the backstop for
    whatever this instruction fails to catch) - the whole point is the model should
    not spend search effort finding them in the first place."""
    names = [n.strip() for n in (excluded_names or []) if n and n.strip()]
    if not names:
        return ""
    joined = ", ".join(names)
    return (
        "\nThese retailers are excluded from this search - do not report a price from "
        "any of them as the historical low, and do not list them in other_retailers, "
        f"even if you notice them selling this product: {joined}\n"
    )


def research_historical_low(
    base_url: str, claude_bin: str, product_name: str, model: str,
    excluded_names: list[str] | None = None,
) -> dict[str, Any]:
    """Ask the headless CLI for the product's historical low. One call, whole product -
    see HISTORICAL_LOW_PROMPT. Returns a parsed finding (which may honestly be "not
    found") or raises RunnerError for a subprocess-level failure (crash, timeout,
    missing binary, non-zero exit)."""
    prompt = HISTORICAL_LOW_PROMPT.format(
        product_name=product_name, model=model,
        excluded_section=_excluded_retailers_section(excluded_names),
    )
    try:
        proc = _run_subscription_claude(
            [claude_bin, "-p", "--allowedTools", "WebSearch,WebFetch"], prompt,
            HISTORICAL_LOW_TIMEOUT_SECONDS,
        )
    except SubscriptionAccessUnavailable as exc:
        raise RunnerError("FAILED", str(exc)) from exc
    except FileNotFoundError as exc:
        raise RunnerError("FAILED", f"{claude_bin} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(
            "FAILED", f"no reply within {HISTORICAL_LOW_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        # Stderr is deliberately not persisted: it can contain session details.
        raise RunnerError("FAILED", CLI_ACCESS_FAILURE)

    try:
        return parse_historical_low_reply(proc.stdout)
    except ValueError as exc:
        raise RunnerError("FAILED", str(exc)) from exc


def process_historical_low_job(base_url: str, claude_bin: str, job: dict[str, Any]) -> None:
    """issue #93: a single whole-product pass, not a loop over retailers. The finding
    (or the honest lack of one) is recorded via /historical-low - never /import, and
    never anything that could move products.lowest_known_price on its own.

    issue #112 (item 8): fetches the current excluded-retailer list via the same
    GET /api/retailers the wizard's own picker reads, and passes the excluded names
    straight into the prompt, so the model is told not to bother rather than only
    having its find discarded after the fact. A failure to fetch the list here must
    not silently mean "nothing is excluded" - it is treated the same as a genuine
    RunnerError (FAILED, not a quiet un-excluded search) rather than let through.
    """
    product = requests.get(f"{base_url}/api/products/{job['product_id']}", timeout=30).json()
    try:
        all_retailers = requests.get(f"{base_url}/api/retailers", timeout=30).json()
    except requests.RequestException as exc:
        log.info("job %s historical-low: could not load excluded retailers: %s", job["id"], exc)
        requests.post(f"{base_url}/api/research-jobs/{job['id']}/complete",
                      json={"status": "FAILED", "error": f"could not load retailers: {exc}"[:300]},
                      timeout=30)
        return
    excluded_names = [r["name"] for r in all_retailers if r.get("excluded")]

    try:
        found = research_historical_low(
            base_url, claude_bin, product["name"], product["model"], excluded_names,
        )
    except RunnerError as exc:
        log.info("job %s historical-low: %s (%s)", job["id"], exc.status, exc.note)
        requests.post(f"{base_url}/api/research-jobs/{job['id']}/complete",
                      json={"status": "FAILED", "error": exc.note[:300]}, timeout=30)
        return

    requests.post(
        f"{base_url}/api/research-jobs/{job['id']}/historical-low",
        json={
            "price": found["price"], "date": found["date"], "retailer": found["retailer"],
            "confidence": found["confidence"], "notes": _truncate_notes(found.get("reason") or ""),
            "other_retailers": found.get("other_retailers") or [],
        },
        timeout=30,
    )
    log.info(
        "job %s historical-low: %s", job["id"],
        f"found ${found['price']}" if found["found"] else "no confident historical low found",
    )
    requests.post(f"{base_url}/api/research-jobs/{job['id']}/complete",
                  json={"status": "DONE"}, timeout=30)


def process_job(base_url: str, claude_bin: str, job: dict[str, Any]) -> None:
    if job.get("kind") == "historical_low":
        process_historical_low_job(base_url, claude_bin, job)
        return

    product = requests.get(f"{base_url}/api/products/{job['product_id']}", timeout=30).json()
    deadline = time.monotonic() + JOB_CEILING_SECONDS

    for result in job["results"]:
        if time.monotonic() > deadline:
            log.warning("job %s hit its own ceiling; leaving the rest PENDING for the "
                        "server's own staleness check to resolve", job["id"])
            break
        retailer_name = result["retailer_name"]
        try:
            found = research_one_retailer(
                base_url, claude_bin, product["name"], product["model"],
                retailer_name, result.get("retailer_homepage"), result.get("url"),
            )
        except RunnerError as exc:
            log.info("job %s / %s: %s (%s)", job["id"], retailer_name, exc.status, exc.note)
            requests.post(
                f"{base_url}/api/research-jobs/{job['id']}/results",
                json={"retailer_id": result["retailer_id"], "status": exc.status,
                      "note": exc.note[:300]},
                timeout=30,
            )
            continue

        imported = requests.post(
            f"{base_url}/api/import",
            json={"findings": [{
                "model": product["model"], "retailer": retailer_name,
                "price": found["price"], "stock": found.get("stock"),
                "url": found.get("url"), "source": "research-runner",
            }]},
            timeout=30,
        ).json()
        listing_id = None
        if imported.get("results"):
            listing_id = imported["results"][0]["listing_id"]
        log.info("job %s / %s: found $%s", job["id"], retailer_name, found["price"])
        requests.post(
            f"{base_url}/api/research-jobs/{job['id']}/results",
            json={"retailer_id": result["retailer_id"], "status": "FOUND",
                  "listing_id": listing_id, "note": found.get("reason", "")[:300]},
            timeout=30,
        )

    requests.post(f"{base_url}/api/research-jobs/{job['id']}/complete",
                   json={"status": "DONE"}, timeout=30)


def poll_once(base_url: str, claude_bin: str) -> bool:
    """Claim and fully process at most one job. Returns True if a job was found."""
    job = requests.post(f"{base_url}/api/research-jobs/claim", timeout=30).json()
    if job is None:
        return False
    log.info("claimed job %s for product %s", job["id"], job["product_id"])
    try:
        process_job(base_url, claude_bin, job)
    except Exception as exc:  # a crash here must not orphan the job silently
        log.exception("job %s crashed", job["id"])
        try:
            requests.post(
                f"{base_url}/api/research-jobs/{job['id']}/complete",
                json={"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"[:300]},
                timeout=30,
            )
        except Exception:
            # The server's own time-based staleness check (research.reconcile_stale) is
            # the backstop if even this report fails to land.
            log.exception("could not even report job %s as FAILED", job["id"])
    return True


def poll_llm_once(base_url: str, claude_bin: str) -> bool:
    """Claim one GUI model-suggestion job and answer it with Claude Code web tools."""
    claim = requests.post(f"{base_url}/api/llm-jobs/claim", timeout=30)
    claim.raise_for_status()
    job = claim.json()
    if job is None:
        return False
    job_url = f"{base_url}/api/llm-jobs/{job['id']}/complete"
    try:
        helper_path = Path(__file__).resolve().parents[1] / "tools" / "llm-helper.py"
        spec = importlib.util.spec_from_file_location("shopwatch_llm_helper", helper_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("the shared LLM helper could not be loaded")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        kind = job.get("kind", "model_suggestion")
        if kind == "model_suggestion":
            prompt, parse = helper.build_web_prompt(job["query"]), helper.parse_web_reply
        elif kind == "retailer_discovery":
            prompt = helper.build_web_retailer_prompt(job["query"])
            parse = helper.parse_web_retailer_reply
        else:
            raise RuntimeError("this job kind is not supported by the server runner")
        proc = _run_subscription_claude(
            [claude_bin, "-p", "--allowedTools", "WebSearch"],
            prompt, LLM_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(CLI_ACCESS_FAILURE)
        result = parse(proc.stdout)
        completed = requests.post(
            job_url, json={"status": "DONE", "result": json.dumps(result)}, timeout=30,
        )
        completed.raise_for_status()
    except subprocess.TimeoutExpired:
        failed = requests.post(
            job_url,
            json={"status": "FAILED", "error": "Claude Code search timed out; retry the job."},
            timeout=30,
        )
        failed.raise_for_status()
    except SubscriptionAccessUnavailable:
        failed = requests.post(
            job_url,
            json={"status": "FAILED", "error": (
                "Claude subscription OAuth token is not configured on the research host."
            )},
            timeout=30,
        )
        failed.raise_for_status()
    except ValueError:
        failed = requests.post(
            job_url,
            json={"status": "FAILED", "error": (
                "Claude Code returned no usable sourced results. Retry the search."
            )},
            timeout=30,
        )
        failed.raise_for_status()
    except Exception as exc:
        # Do not return subprocess diagnostics or exception text, either of which may
        # contain session details, to the GUI or runner journal.
        log.warning("model research job %s failed", job["id"])
        error = CLI_ACCESS_FAILURE if str(exc) == CLI_ACCESS_FAILURE else (
            "Claude Code could not return sourced model suggestions. Reconnect the "
            "Claude Code session on the research host, then retry."
        )
        failed = requests.post(job_url, json={"status": "FAILED", "error": error}, timeout=30)
        failed.raise_for_status()
    return True


# ----------------------------------------------------------------- retailer search jobs
#
# Live web search for "who else sells this", via the self-hosted Firecrawl instance on
# opti's own loopback. See app/retailer_search.py's module docstring for why this lives
# here rather than as a direct HTTP call from the shopwatch container.


def _bare_domain(url: str) -> str:
    """Lowercased so set membership (dedup, excluded-domain checks) is case-insensitive -
    a domain is not case-sensitive, but two Firecrawl results for the same store with
    different URL casing (or a stored homepage typed in a different case) would
    otherwise never match each other."""
    netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _domain_display_name(domain: str) -> str:
    """A readable fallback when the search result's own title doesn't yield a clean
    short name - see _name_from_search_result. Approximate by construction (a bare
    domain rarely spells a brand's actual styling, e.g. "jbhifi.com.au" vs "JB Hi-Fi"),
    which is fine: the wizard shows the homepage alongside it and nothing is created
    without a person ticking a box to approve it."""
    base = re.sub(r"\.(com\.au|net\.au|org\.au|com|net|org|io|co)$", "", domain)
    parts = re.split(r"[.\-_]+", base)
    return " ".join(p.capitalize() for p in parts if p) or domain


def _name_from_search_result(url: str, title: str) -> str:
    domain = _bare_domain(url)
    for sep in (" | ", " - ", " — "):
        if sep in title:
            candidate = title.rsplit(sep, 1)[-1].strip()
            # A trailing "| $899" or "- SKU1234" is a price/SKU, not a store name - a
            # short candidate with no digit is the only case worth trusting over the
            # domain fallback.
            if candidate and len(candidate) <= 40 and not any(c.isdigit() for c in candidate):
                return candidate
    return _domain_display_name(domain)


def discover_retailers(
    firecrawl_url: str, product_name: str, model: str,
    excluded_names: list[str], excluded_homepages: list[str],
) -> dict[str, Any]:
    """Ask Firecrawl who else sells this, excluding retailers already known for this
    product. Raises on any failure to search - a failed search must never look like a
    successful search that simply found nothing (hardening.md rule 2)."""
    query = f"{product_name} {model} buy Australia".strip()
    try:
        resp = requests.post(
            f"{firecrawl_url}/v1/search",
            json={"query": query, "limit": FIRECRAWL_MAX_CANDIDATES},
            timeout=FIRECRAWL_SEARCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"firecrawl search failed: {exc}") from exc
    if not data.get("success"):
        raise RuntimeError(f"firecrawl search did not succeed: {data.get('error') or data}")

    excluded_names_lower = {n.strip().lower() for n in excluded_names if n and n.strip()}
    excluded_domains = {_bare_domain(h) for h in excluded_homepages if h}
    excluded_domains.discard("")

    seen_domains: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for item in data.get("data") or []:
        url = item.get("url") or ""
        domain = _bare_domain(url)
        if not domain or domain in seen_domains or domain in excluded_domains:
            continue
        seen_domains.add(domain)
        name = _name_from_search_result(url, item.get("title") or "")
        if name.lower() in excluded_names_lower:
            continue
        candidates.append({"name": name, "homepage": f"https://{domain}"})
        if len(candidates) >= FIRECRAWL_MAX_CANDIDATES:
            break
    return {"retailers": candidates, "note": None}


def process_retailer_search_job(base_url: str, firecrawl_url: str, job: dict[str, Any]) -> None:
    query = json.loads(job["query"])
    result = discover_retailers(
        firecrawl_url, query.get("product_name", ""), query.get("model", ""),
        query.get("excluded_names") or [], query.get("excluded_homepages") or [],
    )
    requests.post(
        f"{base_url}/api/retailer-search-jobs/{job['id']}/complete",
        json={"status": "DONE", "result": json.dumps(result)}, timeout=30,
    )


def poll_retailer_search_once(base_url: str, firecrawl_url: str) -> bool:
    """Claim and answer at most one retailer-search job. Returns True if one was found."""
    job = requests.post(f"{base_url}/api/retailer-search-jobs/claim", timeout=30).json()
    if job is None:
        return False
    log.info("claimed retailer-search job %s", job["id"])
    try:
        process_retailer_search_job(base_url, firecrawl_url, job)
    except Exception as exc:  # a crash here must not orphan the job silently
        log.exception("retailer-search job %s crashed", job["id"])
        try:
            requests.post(
                f"{base_url}/api/retailer-search-jobs/{job['id']}/complete",
                json={"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"[:300]},
                timeout=30,
            )
        except Exception:
            log.exception("could not even report retailer-search job %s as FAILED", job["id"])
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-runner")
    parser.add_argument("--base-url", default=None,
                         help="defaults to $SHOPWATCH_URL")
    parser.add_argument("--claude-bin", default=None,
                         help="defaults to $CLAUDE_BIN or 'claude'")
    parser.add_argument("--firecrawl-url", default=None,
                         help="defaults to $FIRECRAWL_URL or http://127.0.0.1:3002")
    parser.add_argument("--interval", type=int, default=10,
                         help="seconds between polls when idle")
    parser.add_argument("--once", action="store_true",
                         help="process at most one job of each kind, then exit "
                              "(for cron rather than a loop)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    import os
    base_url = args.base_url or os.environ.get("SHOPWATCH_URL")
    claude_bin = args.claude_bin or os.environ.get("CLAUDE_BIN", "claude")
    firecrawl_url = (
        args.firecrawl_url or os.environ.get("FIRECRAWL_URL", "http://127.0.0.1:3002")
    ).rstrip("/")
    if not base_url:
        parser.error("--base-url or $SHOPWATCH_URL is required")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    def poll_both() -> None:
        # Each queue's own try/except (inside poll_once/poll_retailer_search_once)
        # only covers processing a job already claimed - the claim call itself can
        # still raise (a non-JSON error body, a dropped connection), and that must
        # not cost the OTHER queues their turns. Keep the short GUI discovery work
        # before the potentially long per-retailer price job.
        try:
            poll_llm_once(base_url, claude_bin)
        except Exception:
            log.exception("model research poll failed")
        try:
            poll_retailer_search_once(base_url, firecrawl_url)
        except Exception:
            log.exception("retailer-search poll failed")
        try:
            poll_once(base_url, claude_bin)
        except Exception:
            log.exception("research poll failed")

    if args.once:
        poll_both()
        return 0

    log.info("polling %s every %ss", base_url, args.interval)
    while True:
        poll_both()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
