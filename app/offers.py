"""Reading retailer marketing email for offers worth acting on.

The problem this solves, measured on a real corpus of 204 retailer emails: only 67
carried a quantified offer, and keyword matching flagged 44 of those as relevant when a
handful actually were. "$400 off" and the word "TV" both appearing somewhere in a 6,000
character catalogue blast says nothing. Working out what an offer covers means reading
it, so that step is done by a model with a fixed output schema.

The standing rule: **an offer is a lead, never a price.** Nothing here writes to a
listing's advertised price, freight or coupon. It produces a suggestion with the
arithmetic already done, and a human decides.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import store
from .db import utcnow

#: Normalised scope vocabulary. Constrained so matching is a set operation rather than
#: another round of string guessing.
CATEGORIES = [
    "storewide", "tv", "audio", "appliance", "tool", "computing",
    "phone", "gaming", "other",
]

#: Which offer categories can touch a product category.
PRODUCT_CATEGORY_SCOPE = {
    "soundbar": {"audio", "tv", "storewide"},
    "tv": {"tv", "storewide"},
    "power_tool": {"tool", "storewide"},
    "appliance": {"appliance", "storewide"},
    "general": {"storewide"},
}

MODEL = "claude-opus-5"

# --------------------------------------------------------------------------- the gate

#: Cheap pre-filter. High recall, low precision, no cost: it only has to decide whether
#: a message is worth spending a model call on. 137 of 204 real messages fail this and
#: never reach the API.
GATE = re.compile(
    r"(\d{1,2}\s*%\s*off"
    r"|\$\s?\d[\d,]*\s*off"
    r"|\bcash\s?back\b"
    r"|\bstorecash\b"
    r"|\bspend\s*\$\s?\d"
    r"|\bhalf\s+price\b"
    r"|\buse\s+code\b"
    r"|\bclearance\b)",
    re.I,
)


def worth_extracting(subject: str, body: str) -> bool:
    """Does this message contain anything that looks like a quantified offer at all?"""
    return bool(GATE.search(subject or "") or GATE.search(body or ""))


# ------------------------------------------------------------------ extraction schema


class ExtractedOffer(BaseModel):
    """What the model is asked to return. Every field is something a person would need
    in order to decide whether to act, and nothing else."""

    is_offer: bool = Field(description="True only if this email contains a specific, quantified offer a customer could actually use. Brand or product announcements with no discount are not offers.")
    kind: Literal["percent_off", "dollar_off", "cashback", "store_credit", "bundle", "finance", "other", "none"]
    amount: float | None = Field(description="The discount size: 20 for 20% off, 500 for $500 off. Null if there is no single figure.")
    spend_threshold: float | None = Field(description="Minimum spend required, e.g. 2000 for 'spend $2000 or more'. Null if none.")
    applies_to: str = Field(description="What the offer covers, in the email's own words, e.g. 'TVs over $2000' or 'a huge range instore and online'.")
    categories: list[str] = Field(description=f"Normalised scope. Choose only from: {', '.join(CATEGORIES)}. Use 'storewide' when the offer is not limited to a product type. Empty list if this is not an offer.")
    excludes: str | None = Field(description="Exclusions that would plausibly matter, summarised. Null if none stated.")
    code: str | None = Field(description="Discount code a customer must enter, if the email gives one.")
    expires: str | None = Field(description="Last day the offer runs, as YYYY-MM-DD. Null if not stated. Do not guess a year that is not implied.")
    requires_signup: bool = Field(description="True if the customer must join something, activate a wallet, or hand over details to get the offer.")
    confidence: Literal["high", "medium", "low"] = Field(description="How certain you are that this offer is real and that the scope above is right. Use 'low' when the email is a catalogue of many products and the discount belongs to one of them rather than to a category.")
    summary: str = Field(description="One plain sentence a person could act on, e.g. '20% off a wide range at The Good Guys, needs StoreCash signup, ends 13 September.'")


EXTRACTION_PROMPT = """You are reading a retailer marketing email for someone who is
watching a small number of specific products and wants to know whether an offer in this
email could apply to one of them.

Be sceptical. Most of these emails are catalogues listing dozens of products, each with
its own price. A dollar figure next to one product is NOT a category-wide offer. Only
report an offer whose scope is genuinely stated, and set confidence to "low" when the
discount plainly belongs to individual products rather than to a category or the store.

Do not infer an expiry year that is not implied by the email. Do not invent a code.
If the email is purely a product announcement, set is_offer false and kind "none".

Subject: {subject}
From: {sender}
Received: {received}

---
{body}
---
"""


CLI_PROMPT = EXTRACTION_PROMPT + """
Return ONLY a JSON object matching this shape, with no prose and no code fence:
{schema}
"""


@dataclass
class ExtractionResult:
    offer: ExtractedOffer | None
    model: str
    error: str | None = None


def extract_via_cli(subject: str, sender: str, received: str, body: str,
                    claude_bin: str = "claude", timeout: int = 180) -> ExtractionResult:
    """Extract using headless Claude Code instead of the API.

    This is the path that costs nothing beyond the subscription already paying for
    /check-seek on the same machine, and needs no ANTHROPIC_API_KEY: the CLI carries
    its own OAuth token. Slower per message than the API, which does not matter for a
    job that runs twice a day over a handful of emails.
    """
    import subprocess

    schema = json.dumps(ExtractedOffer.model_json_schema().get("properties", {}), indent=1)
    prompt = CLI_PROMPT.format(
        subject=subject, sender=sender, received=received, body=body[:12000], schema=schema
    )
    try:
        proc = subprocess.run(
            [claude_bin, "-p"], input=prompt, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return ExtractionResult(None, "claude-cli", f"{claude_bin} not found")
    except subprocess.TimeoutExpired:
        return ExtractionResult(None, "claude-cli", f"timed out after {timeout}s")
    if proc.returncode != 0:
        return ExtractionResult(None, "claude-cli", (proc.stderr or "non-zero exit")[:200])

    return parse_cli_output(proc.stdout)


IMAGE_PROMPT = """Read the attached screenshots of a retailer marketing email. The
discount, the deadline, the code and often the whole offer are drawn into the artwork
rather than written as text, which is why you are being shown pictures.

The screenshots are consecutive screens of the same email, top to bottom. Read EVERY
one before answering: the headline and the code are usually in the first, and the
exclusion list and the fine print are usually in the last. Pay particular attention to
the headline number, any "ends" or "sign up by" date, any "use code" line, and any
exclusions block. The email's extracted text is below for the fine print, but where
the two disagree about the offer itself, trust the images.

Subject: {subject}
From: {sender}
Received: {received}

Text extracted from the same email (fine print, often incomplete):
---
{body}
---

Read these images first, in order:
{image_list}

Then return ONLY a JSON object matching this shape, with no prose and no code fence:
{schema}
"""


def extract_from_image(image_paths: str | Sequence[str], subject: str, sender: str,
                       received: str, body: str, claude_bin: str = "claude",
                       timeout: int = 240) -> ExtractionResult:
    """Read the offer out of rendered screenshot(s), with the text alongside for context.

    One call, not two merged: handing the model both and asking for a single answer
    avoids inventing a reconciliation rule for fields that disagree. Several tiles
    of one email go in the same call for the same reason: the code is on one screen
    and its exclusions on another, and they belong to one offer.
    """
    import subprocess

    paths = [image_paths] if isinstance(image_paths, str) else list(image_paths)
    if not paths:
        return ExtractionResult(None, "claude-cli-vision", "no screenshots to read")
    schema = json.dumps(ExtractedOffer.model_json_schema().get("properties", {}), indent=1)
    prompt = IMAGE_PROMPT.format(
        subject=subject, sender=sender, received=received,
        body=body[:6000], schema=schema,
        image_list="\n".join(f"{i + 1}. {p}" for i, p in enumerate(paths)),
    )
    try:
        proc = subprocess.run(
            [claude_bin, "-p", "--allowedTools", "Read"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return ExtractionResult(None, "claude-cli-vision", f"{claude_bin} not found")
    except subprocess.TimeoutExpired:
        return ExtractionResult(None, "claude-cli-vision", f"timed out after {timeout}s")
    if proc.returncode != 0:
        return ExtractionResult(None, "claude-cli-vision", (proc.stderr or "non-zero exit")[:200])

    result = parse_cli_output(proc.stdout)
    return ExtractionResult(result.offer, "claude-cli-vision", result.error)


#: Fields worth paying for a render to recover. An offer with no amount, no deadline or
#: no code is barely actionable, and all three are typically drawn rather than written.
#:
#: "code" was added 27 Sep 2026 after The Good Guys' "Just For You" mail of 24 Sep was
#: recorded with no code at all: the subject carried "20% off" and "Ends Sunday", so
#: the text pass produced an amount and an expiry, the offer was judged complete, and
#: the render never ran. The code (JFY2026), the eligible categories and the exclusion
#: list were all in two banner images. A percent-off offer with no code cannot be
#: acted on, so it is exactly the case a render is for. An offer that genuinely needs
#: no code costs one extra render, which is cheap next to missing the code that was there.
WEAK_FIELDS = ("amount", "expires", "code")


def is_weak(offer: ExtractedOffer | None) -> bool:
    """Would a look at the artwork plausibly add something?

    Only real offers are worth rendering: a product announcement stays a product
    announcement no matter how prettily it is drawn.
    """
    if offer is None or not offer.is_offer:
        return False
    return any(getattr(offer, field, None) in (None, "") for field in WEAK_FIELDS)


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
    treated the same as a parse failure: skip that candidate and keep looking,
    rather than propagating out of a caller that has no reason to expect it.
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


def parse_cli_output(text: str) -> ExtractionResult:
    """Pull the JSON object out of a CLI reply and validate it against the schema.

    Kept separate from the subprocess call so it can be tested without running anything.
    """
    raw = (text or "").strip()
    start = raw.find("{")
    if start == -1:
        return ExtractionResult(None, "claude-cli", f"no JSON in reply: {raw[:150]!r}")
    try:
        data = _last_json_object(raw, start)
    except ValueError as exc:
        return ExtractionResult(None, "claude-cli", str(exc))
    try:
        return ExtractionResult(ExtractedOffer.model_validate(data), "claude-cli")
    except Exception as exc:
        return ExtractionResult(None, "claude-cli", f"reply did not match the schema: {exc}")


def extract(subject: str, sender: str, received: str, body: str,
            client: Any = None, model: str = MODEL) -> ExtractionResult:
    """Ask the model what the offer is. Returns ExtractionResult; never raises.

    A failure here must not stop the run: one unparseable email is not a reason to stop
    reading the rest of the mailbox.
    """
    try:
        import anthropic
    except ImportError:
        return ExtractionResult(None, model, "anthropic SDK not installed")

    client = client or anthropic.Anthropic()
    # Marketing HTML runs long and the tail is boilerplate; the offer is near the top.
    body = body[:12000]
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=2000,
            output_config={"effort": "low"},
            output_format=ExtractedOffer,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(
                    subject=subject, sender=sender, received=received, body=body
                ),
            }],
        )
    except Exception as exc:  # network, rate limit, refusal, schema failure
        return ExtractionResult(None, model, f"{type(exc).__name__}: {exc}")

    if getattr(response, "stop_reason", None) == "refusal":
        return ExtractionResult(None, model, "model declined to answer")
    return ExtractionResult(response.parsed_output, model)


# ----------------------------------------------------------------------- the matching


def projected_price(offer: dict[str, Any], basis: float, advertised: float | None) -> float | None:
    """What the offer would make the price, or None when it cannot be worked out.

    Deliberately conservative: a spend threshold that the headline price does not meet
    disqualifies the offer outright rather than being ignored.
    """
    kind, amount = offer.get("kind"), offer.get("amount")
    if amount is None or basis is None:
        return None
    threshold = offer.get("spend_threshold")
    if threshold is not None and (advertised or basis) < threshold:
        return None
    if kind == "percent_off":
        if not 0 < amount < 100:
            return None
        return round(basis * (1 - amount / 100), 2)
    if kind in ("dollar_off", "cashback", "store_credit"):
        if amount <= 0 or amount >= basis:
            return None
        return round(basis - amount, 2)
    return None


def match_products(conn: sqlite3.Connection, offer: dict[str, Any]) -> list[dict[str, Any]]:
    """Which tracked products this offer could touch, with the arithmetic done.

    An offer belongs to the retailer that sent it, so it is projected from **that
    retailer's own listing** and nothing else. Discounting the cheapest price on the
    board by a Good Guys code produces a number that cannot be bought at any shop.

    A consequence worth knowing: if the sending retailer is not on a product's board,
    the offer raises no match here even though they may well sell the thing. Adding
    that retailer's listing is what makes the offer visible.
    """
    categories = set(offer.get("categories") or [])
    retailer_name = offer.get("retailer_name")
    if not categories or not retailer_name:
        return []

    matches = []
    for product in store.list_products(conn, status="ACTIVE"):
        scope = PRODUCT_CATEGORY_SCOPE.get(product["category"], {"storewide"})
        if not (categories & scope):
            continue

        listing = next(
            (listing for listing in product["listings"]
             if (listing["retailer_name"] or "").lower() == retailer_name.lower()),
            None,
        )
        if listing is None or listing["delivered_price"] is None:
            continue

        basis = listing["delivered_price"]
        projected = projected_price(offer, basis, listing["advertised_price"])
        if projected is None:
            continue

        trigger = product.get("trigger_price")
        crosses = bool(trigger is not None and projected <= trigger and basis > trigger)
        # A projection from an unconfirmed delivered price is doubly provisional: the
        # freight is unknown and the offer is unverified. Say so rather than imply a price.
        provisional = not listing["delivered_resolved"]
        matches.append({
            "product_id": product["id"],
            "product_name": product["name"],
            "listing_id": listing["id"],
            "basis_delivered": basis,
            "projected_delivered": projected,
            "crosses_trigger": crosses and not provisional,
            # Just the arithmetic. The summary is shown alongside it, and repeating
            # it here produced a run-on sentence saying the same thing twice.
            "rationale": (
                f"Would take {retailer_name} from ${basis:,.0f} to ${projected:,.0f}"
                + (", before freight, which is still unknown" if provisional else "")
                + (f", under your ${trigger:,.0f} trigger" if crosses and not provisional else "")
            ),
        })
    return matches


# ------------------------------------------------------------------------- persistence


def record_offer(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any] | None:
    """Store an offer and its matches. Returns None when it was already seen."""
    existing = conn.execute(
        "SELECT id FROM offers WHERE message_id = ?", (data["message_id"],)
    ).fetchone()
    if existing:
        return None

    retailer = None
    if data.get("retailer_name"):
        retailer = store.ensure_retailer(conn, data["retailer_name"])

    cur = conn.execute(
        "INSERT INTO offers (message_id, source, retailer_id, retailer_name, sender,"
        " subject, received_at, kind, amount, spend_threshold, applies_to, categories,"
        " excludes, code, expires_at, requires_signup, confidence, summary,"
        " extracted_by, raw_excerpt, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["message_id"], data.get("source", "email"),
            retailer["id"] if retailer else None, data.get("retailer_name"),
            data.get("sender"), data.get("subject"), data.get("received_at"),
            data.get("kind"), data.get("amount"), data.get("spend_threshold"),
            data.get("applies_to"), json.dumps(data.get("categories") or []),
            data.get("excludes"), data.get("code"), data.get("expires"),
            int(bool(data.get("requires_signup"))), data.get("confidence"),
            data.get("summary"), data.get("extracted_by"),
            (data.get("raw_excerpt") or "")[:600], utcnow(),
        ),
    )
    offer_id = int(cur.lastrowid)

    for match in match_products(conn, data):
        conn.execute(
            "INSERT OR IGNORE INTO offer_matches (offer_id, product_id, listing_id,"
            " basis_delivered, projected_delivered, crosses_trigger, rationale)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                offer_id, match["product_id"], match["listing_id"],
                match["basis_delivered"], match["projected_delivered"],
                int(match["crosses_trigger"]), match["rationale"],
            ),
        )
    return offer_view(conn, offer_id)


def offer_view(conn: sqlite3.Connection, offer_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if row is None:
        return None
    offer = dict(row)
    offer["categories"] = json.loads(offer["categories"] or "[]")
    offer["matches"] = [
        dict(m) for m in conn.execute(
            "SELECT om.*, p.name AS product_name, p.model FROM offer_matches om"
            " JOIN products p ON p.id = om.product_id WHERE om.offer_id = ?"
            " ORDER BY om.crosses_trigger DESC, om.projected_delivered",
            (offer_id,),
        )
    ]
    offer["live"] = is_live(offer)
    return offer


def is_live(offer: dict[str, Any]) -> bool:
    """Has it expired? An offer with no stated expiry is treated as live."""
    expires = offer.get("expires_at")
    if not expires:
        return offer.get("status") != "DISMISSED"
    return str(expires)[:10] >= utcnow()[:10] and offer.get("status") != "DISMISSED"


def list_offers(conn: sqlite3.Connection, only_live: bool = True,
                only_matched: bool = False) -> list[dict[str, Any]]:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM offers ORDER BY received_at DESC, id DESC LIMIT 500")]
    offers = [o for o in (offer_view(conn, i) for i in ids) if o]
    if only_live:
        offers = [o for o in offers if o["live"]]
    if only_matched:
        offers = [o for o in offers if o["matches"]]
    return offers


def offers_for_product(conn: sqlite3.Connection, product_id: int) -> list[dict[str, Any]]:
    """Live offers that could touch this product, best projection first."""
    ids = [r["offer_id"] for r in conn.execute(
        "SELECT DISTINCT offer_id FROM offer_matches WHERE product_id = ?", (product_id,))]
    offers = [o for o in (offer_view(conn, i) for i in ids) if o and o["live"]]
    offers.sort(key=lambda o: min(
        (m["projected_delivered"] for m in o["matches"] if m["product_id"] == product_id),
        default=float("inf")))
    return offers

