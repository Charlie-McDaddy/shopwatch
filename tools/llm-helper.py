#!/usr/bin/env python3
"""Answers shopwatch's wizard requests - "suggest a model number" and "find other
retailers selling this" - using YOUR OWN Claude Code or Codex CLI login - never an API
key, never a credential shopwatch holds or pays for. Run this on whichever machine
already has one of those CLIs installed and signed in; it can be the same machine
you're browsing shopwatch from.

Quick start:
    python3 tools/llm-helper.py --url https://shop.home.lunt.au --backend claude

No pip install needed - this script uses only the Python standard library. You will be
prompted for your shopwatch username/password the first time (the same ones you use to
open the site in a browser); pass --user/--password or $SHOPWATCH_USER/$SHOPWATCH_PASSWORD
to skip the prompt.

How it works: shopwatch's wizard queues a job ("suggest models for: Dreame RoboMower")
and waits. This script polls shopwatch every couple of seconds asking "anything queued
for me?", and when there is, asks your local `claude` or `codex` CLI the question and
posts the answer straight back. Nothing about your CLI login ever leaves this machine -
only the resulting suggestion text does.

Leave this running in a terminal while you use the wizard; Ctrl+C to stop any time.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

#: Codex CLI's non-interactive invocation, correct as of this script's writing but a
#: fast-moving target - if your installed version rejects this, run `codex exec --help`
#: and adjust the one line in call_codex() below rather than anything else here.
#: --skip-git-repo-check is not optional: Codex refuses to run at all otherwise unless
#: launched from inside a trusted/git directory, and this script is meant to be
#: downloaded and run from wherever - a Downloads folder, not a git checkout. Confirmed
#: live: identical prompt failed with "Not inside a trusted directory..." without this
#: flag from a plain temp directory, and answered normally with it.
CODEX_EXEC_ARGS = ["exec", "--skip-git-repo-check"]

#: One text completion, not a multi-page research pass - if a CLI hasn't answered by
#: this point something is wrong (auth prompt waiting on input, hung process), and the
#: person watching the terminal deserves a clear timeout rather than an indefinite hang.
CALL_TIMEOUT_SECONDS = 60

PROMPT_TEMPLATE = """You are helping someone fill in the exact model number for a
product on a personal price-watching tool. They have given a rough description. This
field is later used to match future price imports against, so it must be an exact, real
model number or SKU - never one that merely sounds plausible for the brand/category.

Rough description: {query}

Be short and honest:
- Return 0 to 5 candidates. Fewer, or zero, is the correct answer when you are not
  genuinely confident real models exist matching this description. A plausible-looking
  invented model number is worse than no suggestion at all.
- Only include a model you have specific, genuine knowledge of - not a guess at the
  pattern a model number "would" follow for this brand.
- If the product line is very new, or you are not confident your knowledge of it is
  current, say so in "note" rather than guessing at specific models.

Return ONLY a JSON object, no prose, no code fence, matching this shape:
{{
  "candidates": [
    {{"model": "exact model number or SKU", "label": "sourced product name",
      "variant": "exact size/flavour/variant, or null if unknown",
      "description": "short sourced description, or null if unknown",
      "image_url": "direct public HTTPS product image URL, or null if unavailable"}}
  ],
  "note": "one short sentence, or null - a confidence/recency caveat, not a candidate repeat"
}}
"""


def build_prompt(query: str) -> str:
    return PROMPT_TEMPLATE.format(query=query)


def build_web_prompt(query: str) -> str:
    """Build the server runner's searched variant of the existing model prompt."""
    return build_prompt(query) + """

For this request, use WebSearch to find current, sourced product candidates. Return the
same JSON shape, with an additional "source" field on each candidate containing the
HTTP(S) URL that supports that exact model. Include variant, description, and image_url
only when supported by that source. Do not invent a source or image URL. If no reliable
candidate can be sourced, return an empty candidates list and explain why in note.
"""


def build_web_retailer_prompt(query: str) -> str:
    return build_retailer_prompt(query) + """

Use WebSearch to find current Australian sellers for the exact product. Use the
retailer's verified homepage as its source URL. Do not invent a homepage; return null
when you cannot verify one.
"""


RETAILER_PROMPT_TEMPLATE = """You are helping someone find other real Australian
retailers that sell a specific product, for a personal price-watching tool. They
already have some retailers covered and want to know who else genuinely stocks it.

{query}

Be short and honest:
- Return 0 to 8 retailers. Fewer, or zero, is the correct answer when you are not
  genuinely confident a retailer actually stocks this exact product - a plausible-
  sounding guess is worse than no suggestion.
- Only name a retailer you have specific, genuine reason to believe sells this exact
  product (or this exact model line), not just "a retailer that sells this category".
- Do not repeat any retailer already listed as one already being checked.
- homepage should be the retailer's main site (e.g. "https://www.example.com.au"), or
  null if you are not confident of the exact URL.

Return ONLY a JSON object, no prose, no code fence, matching this shape:
{{
  "retailers": [
    {{"name": "retailer name", "homepage": "https://example.com.au or null"}}
  ],
  "note": "one short sentence, or null - a confidence caveat, not a repeat of the list"
}}
"""


def build_retailer_prompt(query: str) -> str:
    return RETAILER_PROMPT_TEMPLATE.format(query=query)


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


def parse_reply(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a CLI reply. Same discipline as app/offers.py's
    parse_cli_output: tolerate a code fence or chatter around the JSON, but never guess
    at malformed content."""
    raw = (raw or "").strip()
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in reply: {raw[:150]!r}")
    data = _last_json_object(raw, start)
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("reply had no 'candidates' list")
    cleaned = []
    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        candidate = {"model": model, "label": str(item.get("label") or model).strip()}
        for field, limit in (("variant", 200), ("description", 5000), ("image_url", 2048)):
            value = item.get(field)
            if isinstance(value, str) and value.strip() and len(value.strip()) <= limit:
                candidate[field] = value.strip()
        source = item.get("source")
        if isinstance(source, str):
            source = source.strip()
            try:
                parsed_source = urlparse(source)
                if parsed_source.scheme.lower() in ("http", "https") and parsed_source.hostname:
                    candidate["source"] = source
            except ValueError:
                pass
        cleaned.append(candidate)
    return {"candidates": cleaned, "note": data.get("note")}


def parse_web_reply(raw: str) -> dict[str, Any]:
    """Keep only model suggestions with a usable HTTP(S) source."""
    result = parse_reply(raw)
    candidates = result["candidates"]
    result["candidates"] = [item for item in candidates if item.get("source")]
    if candidates and not result["candidates"] and not result["note"]:
        result["note"] = "No candidate had a verifiable source link."
    return result


def parse_retailer_reply(raw: str) -> dict[str, Any]:
    """Same discipline as parse_reply, for the retailer-discovery reply shape."""
    raw = (raw or "").strip()
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in reply: {raw[:150]!r}")
    data = _last_json_object(raw, start)
    retailers = data.get("retailers")
    if not isinstance(retailers, list):
        raise ValueError("reply had no 'retailers' list")
    cleaned = []
    for item in retailers[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        homepage = item.get("homepage")
        homepage = str(homepage).strip() if homepage else None
        try:
            if homepage and (urlparse(homepage).scheme.lower() not in ("http", "https")
                             or not urlparse(homepage).hostname):
                homepage = None
        except ValueError:
            homepage = None
        cleaned.append({"name": name, "homepage": homepage})
    return {"retailers": cleaned, "note": data.get("note")}


def parse_web_retailer_reply(raw: str) -> dict[str, Any]:
    """Keep searched retailer suggestions only when they have a valid homepage."""
    result = parse_retailer_reply(raw)
    retailers = result["retailers"]
    result["retailers"] = [item for item in retailers if item.get("homepage")]
    if retailers and not result["retailers"] and not result["note"]:
        result["note"] = "No retailer had a verifiable homepage link."
    return result


def call_claude(claude_bin: str, prompt: str) -> str:
    proc = subprocess.run(
        [claude_bin, "-p"], input=prompt, capture_output=True, text=True,
        timeout=CALL_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{claude_bin} exited {proc.returncode}: {(proc.stderr or '')[:300]}")
    return proc.stdout


def call_codex(codex_bin: str, prompt: str) -> str:
    proc = subprocess.run(
        [codex_bin, *CODEX_EXEC_ARGS, prompt], capture_output=True, text=True,
        timeout=CALL_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{codex_bin} exited {proc.returncode}: {(proc.stderr or '')[:300]}")
    return proc.stdout


BACKENDS = {"claude": call_claude, "codex": call_codex}


def _auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def api_call(url: str, auth_header: str, method: str = "GET",
             body: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth_header)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "shopwatch rejected those credentials (401) - check --user/--password"
            ) from exc
        raise RuntimeError(f"shopwatch returned HTTP {exc.code}: {exc.read()[:300]}") from exc


#: Each kind's own prompt builder and reply parser, plus the result field whose length
#: is worth printing after a successful answer. Keyed by llm_jobs.KINDS in the server.
JOB_KINDS = {
    "model_suggestion": (build_prompt, parse_reply, "candidates"),
    "retailer_discovery": (build_retailer_prompt, parse_retailer_reply, "retailers"),
}


def resolve_job_kind(kind: str) -> tuple[Any, Any, str]:
    """Look up a kind's prompt builder, reply parser and result field - or raise.

    This script is designed to run as a long-lived standalone copy on a user's own
    machine (see the module docstring), so it can be out of date relative to the
    server. Falling back to model_suggestion's prompt/parser for a kind this copy
    doesn't recognise would answer with the wrong shape and call it a success - a
    skipped check disguised as a pass. Raising here means poll_once's own exception
    handling reports the job FAILED with a clear reason instead.
    """
    try:
        return JOB_KINDS[kind]
    except KeyError:
        raise ValueError(
            f"unrecognised job kind {kind!r} - this copy of llm-helper.py may be out of "
            "date; update it rather than guessing at the reply shape"
        ) from None


def poll_once(base_url: str, auth_header: str, backend: str, bin_name: str) -> bool:
    """Claim and answer at most one job. Returns True if a job was found."""
    job = api_call(f"{base_url}/api/llm-jobs/claim", auth_header, method="POST")
    if job is None:
        return False
    kind = job.get("kind", "model_suggestion")
    print(f"[{time.strftime('%H:%M:%S')}] job {job['id']} ({kind}): {job['query']!r} "
          f"-> asking {backend}...")
    try:
        build, parse, result_key = resolve_job_kind(kind)
        raw = BACKENDS[backend](bin_name, build(job["query"]))
        result = parse(raw)
        api_call(
            f"{base_url}/api/llm-jobs/{job['id']}/complete", auth_header, method="POST",
            body={"status": "DONE", "result": json.dumps(result)},
        )
        print(f"           done: {len(result[result_key])} result(s)")
    except Exception as exc:  # a crash here must not orphan the job silently
        print(f"           failed: {exc}", file=sys.stderr)
        try:
            api_call(
                f"{base_url}/api/llm-jobs/{job['id']}/complete", auth_header, method="POST",
                body={"status": "FAILED", "error": str(exc)[:300]},
            )
        except Exception as report_exc:
            # shopwatch's own time-based staleness check (llm_jobs.reconcile_stale) is
            # the backstop if even this report fails to land.
            print(f"           could not even report the failure: {report_exc}", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-helper")
    parser.add_argument("--url", default=os.environ.get("SHOPWATCH_URL"),
                         help="your shopwatch URL, e.g. https://shop.home.lunt.au "
                              "(default: $SHOPWATCH_URL)")
    parser.add_argument("--user", default=os.environ.get("SHOPWATCH_USER"))
    parser.add_argument("--password", default=os.environ.get("SHOPWATCH_PASSWORD"))
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="claude")
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--interval", type=float, default=2.0,
                         help="seconds between polls when idle (default: 2)")
    parser.add_argument("--once", action="store_true",
                         help="answer at most one queued job, then exit")
    args = parser.parse_args(argv)

    if not args.url:
        parser.error("--url or $SHOPWATCH_URL is required")
    user = args.user or input("shopwatch username: ")
    password = args.password or getpass.getpass("shopwatch password: ")
    auth_header = _auth_header(user, password)
    bin_name = args.claude_bin if args.backend == "claude" else args.codex_bin
    base_url = args.url.rstrip("/")

    # Fail fast and clearly rather than polling forever against a typo'd URL or bad
    # credentials. /healthz is side-effect-free - unlike /api/llm-jobs/claim, hitting it
    # here can never accidentally consume a real queued job before the poll loop starts.
    api_call(f"{base_url}/healthz", auth_header)
    print(f"connected to {base_url}, using {args.backend} ({bin_name})")

    if args.once:
        if not poll_once(base_url, auth_header, args.backend, bin_name):
            print("nothing queued right now")
        return 0

    print(f"watching for wizard requests every {args.interval}s - Ctrl+C to stop")
    try:
        while True:
            if not poll_once(base_url, auth_header, args.backend, bin_name):
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
