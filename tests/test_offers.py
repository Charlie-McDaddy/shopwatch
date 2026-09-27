"""Offer extraction, matching and the arithmetic that turns an offer into a lead.

No test here calls the API. The model's job is to read English; these tests cover
everything either side of that, which is where the mistakes that cost money live.
"""

from __future__ import annotations

import pytest

from app import mailwatch, offers, store
from app.db import connect

# ------------------------------------------------------------------------- the gate


@pytest.mark.parametrize("text", [
    "Spend $2000 or more on TVs & get $500 OFF!",
    "Sign Up, Activate & Get 20% off^ a huge range",
    "Use code SAVENOW at checkout",
    "Half price gift ideas",
    "Get $30 Off* When you Spend $300",
    "Clearance now on",
])
def test_gate_lets_real_offers_through(text):
    assert offers.worth_extracting(text, "")


@pytest.mark.parametrize("text", [
    "Introducing the new Samsung Galaxy range",
    "Your order has shipped",
    "5 ways to get the most from your soundbar",
    "New arrivals this week",
])
def test_gate_keeps_brand_noise_out(text):
    """The control. Without this the gate is just 'always true' and saves nothing."""
    assert not offers.worth_extracting(text, "")


# ------------------------------------------------------------------- the arithmetic


def test_percent_off_projection():
    offer = {"kind": "percent_off", "amount": 20}
    assert offers.projected_price(offer, 1699, 1699) == 1359.2


def test_dollar_off_respects_a_spend_threshold():
    offer = {"kind": "dollar_off", "amount": 500, "spend_threshold": 2000}
    assert offers.projected_price(offer, 2400, 2400) == 1900
    assert offers.projected_price(offer, 1500, 1500) is None, "below the threshold, no offer"


def test_nonsense_amounts_are_refused_rather_than_computed():
    assert offers.projected_price({"kind": "percent_off", "amount": 120}, 1000, 1000) is None
    assert offers.projected_price({"kind": "dollar_off", "amount": 5000}, 1000, 1000) is None
    assert offers.projected_price({"kind": "percent_off", "amount": None}, 1000, 1000) is None
    assert offers.projected_price({"kind": "finance", "amount": 10}, 1000, 1000) is None


# --------------------------------------------------------------------- the matching


def armed_board(db):
    """Q930H with a confirmed $869 delivered price, so projections have a basis."""
    from app import provenance
    with connect(db) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        listing = store.find_listing(
            conn, product["id"], store.ensure_retailer(conn, "Crowdshop")["id"])
        provenance.set_field(conn, listing["id"], "freight", 0.0, state=provenance.MANUAL)
        conn.commit()
    return db


def test_an_offer_only_discounts_the_retailer_that_sent_it(seeded):
    """The bug Rodney caught on sight: a Good Guys code applied to Crowdshop's price.

    An offer belongs to the shop that sent it. Projecting it from whichever listing
    happens to be cheapest invents a price you cannot buy at any counter.
    """
    armed_board(seeded)   # Crowdshop confirmed at $869; The Good Guys at $1,699
    with connect(seeded) as conn:
        offer = {"kind": "percent_off", "amount": 20, "categories": ["audio"],
                 "retailer_name": "The Good Guys", "summary": "20% off audio"}
        matches = offers.match_products(conn, offer)
        assert len(matches) == 1
        m = matches[0]
        assert "The Good Guys" in m["rationale"]
        assert "Crowdshop" not in m["rationale"]
        # 1699 is The Good Guys' own price, not the board's best of 869.
        assert m["basis_delivered"] == 1699.0
        assert m["projected_delivered"] == 1359.2


def test_the_control_the_same_offer_from_crowdshop_uses_crowdshops_price(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        m = offers.match_products(conn, {
            "kind": "percent_off", "amount": 20, "categories": ["audio"],
            "retailer_name": "Crowdshop", "summary": "20% off audio"})[0]
        assert m["basis_delivered"] == 869.0
        assert m["projected_delivered"] == 695.2


def test_an_offer_from_a_shop_not_on_the_board_matches_nothing(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        assert offers.match_products(conn, {
            "kind": "percent_off", "amount": 20, "categories": ["audio"],
            "retailer_name": "Betta", "summary": "20% off audio"}) == []


def test_an_offer_with_no_retailer_matches_nothing(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        assert offers.match_products(conn, {
            "kind": "percent_off", "amount": 20, "categories": ["audio"]}) == []


def test_a_projection_from_an_unconfirmed_price_never_claims_to_cross_the_trigger(seeded):
    """Harvey Norman's freight is unknown, so a discount on it is doubly provisional."""
    with connect(seeded) as conn:
        m = offers.match_products(conn, {
            "kind": "percent_off", "amount": 60, "categories": ["audio"],
            "retailer_name": "Harvey Norman", "summary": "60% off"})[0]
        assert m["projected_delivered"] == 678.0
        assert m["crosses_trigger"] is False
        assert "before freight, which is still unknown" in m["rationale"]


def test_an_offer_that_actually_crosses_the_trigger(conn):
    """The case worth being told about: over target, and the offer brings it under."""
    from app import provenance

    pid = store.create_product(conn, {
        "name": "LG C4 65", "model": "OLED65C4PSA", "category": "tv",
        "trigger_price": 2200, "excellent_price": 2000})
    retailer = store.ensure_retailer(conn, "Bing Lee")
    listing = store.create_listing(conn, {"product_id": pid, "retailer_id": retailer["id"]})
    provenance.set_field(conn, listing, "advertised_price", 2450.0, state=provenance.MANUAL)
    provenance.set_field(conn, listing, "freight", 0.0, state=provenance.MANUAL)
    conn.commit()

    m = offers.match_products(conn, {
        "kind": "dollar_off", "amount": 500, "spend_threshold": 2000,
        "categories": ["tv"], "retailer_name": "Bing Lee",
        "summary": "$500 off TVs over $2000"})[0]
    assert m["basis_delivered"] == 2450.0
    assert m["projected_delivered"] == 1950.0
    assert m["crosses_trigger"] is True
    assert m["rationale"].startswith("Would take Bing Lee from $2,450 to $1,950")
    assert "under your $2,200 trigger" in m["rationale"]


def test_an_offer_on_an_unrelated_category_matches_nothing(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        assert offers.match_products(conn, {
            "kind": "percent_off", "amount": 20, "categories": ["phone"],
            "retailer_name": "Crowdshop", "summary": "20% off phones"}) == []


def test_storewide_reaches_the_sending_retailers_listing(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        matches = offers.match_products(conn, {
            "kind": "dollar_off", "amount": 50, "categories": ["storewide"],
            "retailer_name": "Crowdshop", "summary": "$50 off storewide"})
        assert [m["product_name"] for m in matches] == ["Samsung Q-Series 9.1.4ch Soundbar"]
        assert matches[0]["projected_delivered"] == 819.0


def test_a_bought_product_is_not_matched(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        product = store.product_by_model(conn, "HW-Q930H/XY")
        store.record_purchase(conn, product["id"], {"price_paid": 869})
        conn.commit()
        assert offers.match_products(conn, {
            "kind": "percent_off", "amount": 20, "categories": ["audio"],
            "retailer_name": "Crowdshop"}) == []


# -------------------------------------------------------------------- persistence


SAMPLE = {
    "message_id": "<abc123@email.thegoodguys.com.au>",
    "retailer_name": "The Good Guys",
    "subject": "20% off a huge range",
    "received_at": "2026-09-10T09:00:00Z",
    "kind": "percent_off", "amount": 20, "spend_threshold": None,
    "applies_to": "a huge range instore and online",
    "categories": ["storewide"], "excludes": "Apple, Dyson, gift cards",
    "code": None, "expires": "2026-09-13", "requires_signup": True,
    "confidence": "medium", "summary": "20% off a wide range, needs StoreCash signup",
    "extracted_by": "test",
}


def test_recording_an_offer_stores_it_with_its_matches(seeded):
    armed_board(seeded)
    with connect(seeded) as conn:
        recorded = offers.record_offer(conn, dict(SAMPLE))
        assert recorded["retailer_name"] == "The Good Guys"
        assert recorded["categories"] == ["storewide"]
        assert recorded["requires_signup"] == 1
        assert len(recorded["matches"]) == 1
        # Projected from The Good Guys' own $1,699, because they sent the offer.
        assert recorded["matches"][0]["basis_delivered"] == 1699.0
        assert recorded["matches"][0]["projected_delivered"] == 1359.2


def test_the_same_email_is_never_recorded_twice(seeded):
    with connect(seeded) as conn:
        assert offers.record_offer(conn, dict(SAMPLE)) is not None
        assert offers.record_offer(conn, dict(SAMPLE)) is None, "Message-ID is the dedupe key"


def test_an_expired_offer_drops_out_of_the_live_list(seeded):
    with connect(seeded) as conn:
        offers.record_offer(conn, dict(SAMPLE, expires="2020-01-01",
                                       message_id="<old@x>"))
        offers.record_offer(conn, dict(SAMPLE, expires="2099-01-01",
                                       message_id="<new@x>"))
        conn.commit()
        live = offers.list_offers(conn, only_live=True)
        assert [o["message_id"] for o in live] == ["<new@x>"]
        assert len(offers.list_offers(conn, only_live=False)) == 2


def test_an_offer_with_no_expiry_counts_as_live(seeded):
    with connect(seeded) as conn:
        offers.record_offer(conn, dict(SAMPLE, expires=None))
        conn.commit()
        assert len(offers.list_offers(conn, only_live=True)) == 1


# ------------------------------------------------------------- an offer is not a price


def test_recording_an_offer_never_touches_a_listing(seeded):
    """The standing rule. An offer is a lead; the board's prices stay as they were."""
    armed_board(seeded)
    with connect(seeded) as conn:
        before = [dict(r) for r in conn.execute(
            "SELECT id, advertised_price, freight, coupon_discount FROM listings ORDER BY id")]
        offers.record_offer(conn, dict(SAMPLE))
        conn.commit()
        after = [dict(r) for r in conn.execute(
            "SELECT id, advertised_price, freight, coupon_discount FROM listings ORDER BY id")]
    assert before == after


# ----------------------------------------------------------------- the mail reader


def test_only_known_retailers_are_read():
    assert mailwatch.retailer_for("JB Hi-Fi <deals@email.jbhifi.com.au>") == "JB Hi-Fi"
    assert mailwatch.retailer_for("TGG <x@email.thegoodguys.com.au>") == "The Good Guys"
    assert mailwatch.retailer_for("AO <deals@appliancesonline.com.au>") == "Appliances Online"
    assert mailwatch.retailer_for("Mum <mum@example.com>") is None, "personal mail is never opened"
    assert mailwatch.retailer_for("") is None


def test_order_mail_is_skipped_even_though_the_domain_matches():
    """Order confirmations carry no offers and do carry personal details."""
    assert mailwatch.retailer_for("JB <noreply@order.jbhifi.com.au>") is None


def test_html_bodies_are_flattened_to_readable_text():
    import email.message
    msg = email.message.EmailMessage()
    msg.set_content("<p>Get <b>20% off</b></p><script>evil()</script>", subtype="html")
    text = mailwatch.body_text(msg)
    assert "20% off" in text
    assert "evil()" not in text and "<b>" not in text


# ------------------------------------------------- the headless Claude Code extractor


def test_cli_output_is_parsed_and_validated():
    reply = '''{"is_offer": true, "kind": "percent_off", "amount": 20,
      "spend_threshold": null, "applies_to": "a wide range",
      "categories": ["storewide"], "excludes": "Apple", "code": null,
      "expires": "2026-09-13", "requires_signup": true, "confidence": "medium",
      "summary": "20% off a wide range"}'''
    result = offers.parse_cli_output(reply)
    assert result.error is None
    assert result.offer.amount == 20
    assert result.offer.categories == ["storewide"]


def test_cli_output_survives_a_code_fence_and_surrounding_prose():
    reply = ('Here is the extraction:\n```json\n'
             '{"is_offer": false, "kind": "none", "amount": null, "spend_threshold": null,'
             ' "applies_to": "", "categories": [], "excludes": null, "code": null,'
             ' "expires": null, "requires_signup": false, "confidence": "high",'
             ' "summary": "Product announcement, no offer."}\n```\nHope that helps.')
    result = offers.parse_cli_output(reply)
    assert result.error is None
    assert result.offer.is_offer is False


def test_cli_output_survives_trailing_prose_with_its_own_brace():
    """A real Claude reply hit this: a clean JSON object followed by a parenthetical
    aside containing its own brace, which broke a naive first-`{`/last-`}` slice (it
    grabbed everything up to the LAST `}` in the whole reply, not just the JSON
    object's own closing brace)."""
    reply = ('{"is_offer": true, "kind": "percent_off", "amount": 15,'
             ' "spend_threshold": null, "applies_to": "TVs", "categories": ["tv"],'
             ' "excludes": null, "code": null, "expires": null, "requires_signup": false,'
             ' "confidence": "high", "summary": "15% off TVs"}\n\n'
             '(Note: sizes vary by model, e.g. {55, 65}.)')
    result = offers.parse_cli_output(reply)
    assert result.error is None
    assert result.offer.amount == 15


def test_cli_output_prefers_a_later_reference_offer_over_an_earlier_draft():
    """A code-review pass caught the mirror-image defect the raw_decode fix
    introduced: it took the FIRST complete JSON object, so a reply stating a draft
    or reference case before the real answer silently returned the wrong one
    instead of erroring. Every reproduced example had the real answer LAST -
    reproduced here with a schema-COMPLETE leading draft, the worse variant, since
    an incomplete leading blob would already fail schema validation loudly."""
    reply = (
        '{"is_offer": true, "kind": "percent_off", "amount": 10, "spend_threshold": null,'
        ' "applies_to": "draft", "categories": ["tv"], "excludes": null, "code": null,'
        ' "expires": null, "requires_signup": false, "confidence": "low", "summary": "draft"}\n'
        'Actually, the real offer is:\n'
        '{"is_offer": true, "kind": "percent_off", "amount": 25, "spend_threshold": null,'
        ' "applies_to": "TVs", "categories": ["tv"], "excludes": null, "code": null,'
        ' "expires": null, "requires_signup": false, "confidence": "high", "summary": "25% off TVs"}'
    )
    result = offers.parse_cli_output(reply)
    assert result.error is None
    assert result.offer.amount == 25


def test_pathologically_nested_json_is_reported_not_raised():
    """A code-review pass caught this: raw_decode can raise RecursionError on
    pathologically deep nesting, which the JSONDecodeError-only except clause did
    not catch. mailwatch.py's run() loop has no try/except around parse_cli_output's
    call site, so an uncaught RecursionError there would abort the whole mailbox
    scan - contradicting this module's own stated invariant that one bad email must
    not stop the rest being read."""
    depth = 10_000
    reply = '{"a":' * depth + "1" + "}" * depth
    result = offers.parse_cli_output(reply)
    assert result.offer is None
    assert result.error is not None


def test_a_reply_with_a_brace_but_nothing_that_parses():
    """Coverage gap a code-review pass caught: parse_cli_output is the only one of
    three near-identical CLI-reply parsers actually wired into production (mailwatch
    calls it on every retailer email), and its 'has a brace but nothing valid parses'
    branch had zero test coverage even though tools/llm-helper.py got a dedicated
    test for the equivalent branch."""
    result = offers.parse_cli_output('not json {55, 65} either')
    assert result.offer is None
    assert "not valid JSON" in result.error


def test_a_reply_that_is_not_json_is_an_error_not_a_crash():
    """The control: garbage in must produce a reported error, never a false offer."""
    result = offers.parse_cli_output("I could not read that email, sorry.")
    assert result.offer is None
    assert "no JSON" in result.error


def test_a_reply_missing_required_fields_is_rejected():
    result = offers.parse_cli_output('{"is_offer": true, "kind": "percent_off"}')
    assert result.offer is None
    assert "did not match the schema" in result.error


def test_an_empty_reply_is_an_error():
    assert offers.parse_cli_output("").offer is None


# ------------------------------------------------------ moving read mail to Trash


class FakeImap:
    """Records what was asked of it. Enough to assert on, nothing more."""

    def __init__(self, move_ok: bool = True):
        self.move_ok = move_ok
        self.calls: list[tuple] = []
        self.selected: list[str] = []

    def select(self, folder, readonly=False):
        self.selected.append(folder)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "MOVE":
            return ("OK" if self.move_ok else "NO", [b""])
        return ("OK", [b""])


def imap_with(uids: dict, cleanup: bool = True, move_ok: bool = True):
    src = mailwatch.ImapSource("x@me.com", "pw", cleanup=cleanup)
    src.inbox_uids = dict(uids)
    src._conn = FakeImap(move_ok=move_ok)
    return src


def test_only_processed_messages_are_moved():
    src = imap_with({"<a@x>": b"1", "<b@x>": b"2", "<c@x>": b"3"})
    report = src.cleanup({"<a@x>", "<c@x>"})
    assert report["moved"] == 2
    assert report["skipped"] == 1
    moved = [c[1] for c in src._conn.calls if c[0] == "MOVE"]
    assert moved == [b"1", b"3"], "the unprocessed message stays put"


def test_nothing_moves_when_cleanup_is_off():
    """The control. Off by default, so a casual run never touches the mailbox."""
    src = imap_with({"<a@x>": b"1"}, cleanup=False)
    assert src.cleanup({"<a@x>"}) == {"moved": 0, "failed": [], "skipped": 0}
    assert src._conn.calls == []


def test_it_never_expunges():
    """Trash must stay recoverable: iCloud purges it after 30 days on its own."""
    src = imap_with({"<a@x>": b"1"})
    src.cleanup({"<a@x>"})
    assert not any(c[0] == "EXPUNGE" for c in src._conn.calls)


def test_cleanup_only_ever_opens_the_inbox():
    src = imap_with({"<a@x>": b"1"})
    src.cleanup({"<a@x>"})
    assert src._conn.selected == ["INBOX"]


def test_a_server_without_move_falls_back_to_copy_and_flag():
    src = imap_with({"<a@x>": b"7"}, move_ok=False)
    report = src.cleanup({"<a@x>"})
    assert report["moved"] == 1
    commands = [c[0] for c in src._conn.calls]
    assert commands == ["MOVE", "COPY", "STORE"]
    assert "EXPUNGE" not in commands


def test_nothing_processed_means_nothing_moved():
    src = imap_with({"<a@x>": b"1", "<b@x>": b"2"})
    report = src.cleanup(set())
    assert report["moved"] == 0 and report["skipped"] == 2
    assert src._conn.calls == []


def test_the_default_folder_list_is_inbox_only():
    """Trash is 4,800 messages of already-deleted mail; the job does not need it."""
    assert mailwatch.ImapSource("x@me.com", "pw").folders == ["INBOX"]


def test_a_run_reports_what_it_looked_in_not_just_what_it_found():
    """A watcher that says 'scanned 0' forever reads the same as a dead one.

    The folder totals come from the control search, so an empty inbox and an
    unreachable mailbox produce visibly different output.
    """
    summary = mailwatch.RunSummary()
    summary.folders = {"INBOX": 13}
    assert summary.as_dict()["folders"] == {"INBOX": 13}
    assert mailwatch.RunSummary().as_dict()["folders"] == {}, (
        "no totals means no folder answered, which the CLI reports as a warning"
    )


# --------------------------------------------- deciding when to look at the artwork


def offer(**over):
    base = dict(is_offer=True, kind="percent_off", amount=20, spend_threshold=None,
                applies_to="a range", categories=["storewide"], excludes=None,
                code="SAVE20", expires="2026-09-13", requires_signup=False,
                confidence="high", summary="20% off")
    base.update(over)
    return offers.ExtractedOffer(**base)


def test_a_complete_offer_is_not_worth_rendering():
    """The control. Without this, every email renders and the gate saves nothing."""
    assert offers.is_weak(offer()) is False


def test_a_missing_deadline_is_worth_rendering():
    """The real case: the live Good Guys email had 20% in the text and no date anywhere."""
    assert offers.is_weak(offer(expires=None)) is True


def test_a_missing_amount_is_worth_rendering():
    assert offers.is_weak(offer(amount=None)) is True


def test_a_missing_code_is_worth_rendering():
    """The Good Guys "Just For You" mail of 24 Sep 2026: "20% off" and "Ends Sunday"
    in the subject gave the text pass an amount and an expiry, so the offer read as
    complete, nothing was rendered, and the code that was drawn in the artwork was
    never recorded. A code-less percent-off cannot be acted on."""
    assert offers.is_weak(offer(code=None)) is True
    assert offers.is_weak(offer(code="")) is True


def test_a_non_offer_is_never_rendered():
    """A product announcement stays one however prettily it is drawn."""
    assert offers.is_weak(offer(is_offer=False, amount=None, expires=None)) is False
    assert offers.is_weak(None) is False


def test_the_render_prompt_names_every_tile_and_carries_the_text():
    prompt = offers.IMAGE_PROMPT.format(
        subject="s", sender="f", received="r", body="the fine print",
        image_list="1. /tmp/a/shot.png\n2. /tmp/b/shot.png", schema="{}")
    assert "/tmp/a/shot.png" in prompt and "/tmp/b/shot.png" in prompt
    assert "the fine print" in prompt
    assert "trust the images" in prompt
    assert "exclusion" in prompt, "the prompt has to ask for the fine print, or the model stops at the headline"


def test_extract_from_image_takes_one_path_or_several(monkeypatch):
    """The subprocess is replaced; this checks what the model is asked, not what it says."""
    prompts: list[str] = []

    class Done:
        returncode = 0
        stdout = '{"is_offer": false, "kind": "none", "amount": null, "spend_threshold": null, "applies_to": "", "categories": [], "excludes": null, "code": null, "expires": null, "requires_signup": false, "confidence": "low", "summary": ""}'
        stderr = ""

    def fake_run(cmd, input="", **kw):
        prompts.append(input)
        return Done()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    offers.extract_from_image("/tmp/one.png", "s", "f", "r", "body")
    offers.extract_from_image(["/tmp/a.png", "/tmp/b.png"], "s", "f", "r", "body")
    assert "1. /tmp/one.png" in prompts[0]
    assert "1. /tmp/a.png" in prompts[1] and "2. /tmp/b.png" in prompts[1]
    empty = offers.extract_from_image([], "s", "f", "r", "body")
    assert empty.offer is None and "no screenshots" in empty.error
    assert len(prompts) == 2, "an empty tile list must not call the model at all"


def test_render_errors_do_not_lose_the_text_answer(monkeypatch):
    """A failed render must degrade to the text result, never discard it."""
    cand = mailwatch.Candidate("<a@x>", "JB Hi-Fi", "f", "s", "2026-09-10", "body", "<html>")

    def boom(html, timeout=120, offset=0):
        raise mailwatch.render.RenderError("chrome timed out")

    monkeypatch.setattr(mailwatch.render, "render_html", boom)
    result = mailwatch.render_and_read(cand, "claude")
    assert result.offer is None
    assert "chrome timed out" in result.error


def test_an_email_with_no_html_is_never_rendered():
    cand = mailwatch.Candidate("<a@x>", "JB Hi-Fi", "f", "s", "2026-09-10", "body", "")
    assert mailwatch.render_and_read(cand, "claude") is None


def test_body_html_picks_the_richest_part():
    import email.message
    msg = email.message.EmailMessage()
    msg.set_content("plain text version")
    msg.add_alternative("<html><body>" + "x" * 500 + "</body></html>", subtype="html")
    assert len(mailwatch.body_html(msg)) > 400


# ------------------------------------------- noticing a subscription we cannot see


def test_a_watched_retailer_on_an_unknown_domain_is_noticed():
    """The trap this closes: sign up to a list, the campaigns arrive from an ESP
    domain, and an allowlist silently drops them while looking perfectly healthy."""
    miss = mailwatch.unmatched_retailer('"Harvey Norman" <deals@hn.cmail19.com>')
    assert miss == ("Harvey Norman", "cmail19.com") or miss[0] == "Harvey Norman"


def test_a_retailer_already_on_the_allowlist_is_not_flagged():
    """The control: the real Harvey Norman sender must not produce a false alarm."""
    sender = '"Harvey Norman" <do_not_reply@harveynorman.com.au>'
    assert mailwatch.retailer_for(sender) == "Harvey Norman"


def test_ordinary_mail_is_never_flagged():
    assert mailwatch.unmatched_retailer("Mum <mum@example.com>") is None
    assert mailwatch.unmatched_retailer("") is None
    assert mailwatch.unmatched_retailer("no-display-name@example.com") is None


def test_the_flag_is_collected_during_a_scan():
    import email.message

    def msgs():
        m = email.message.EmailMessage()
        m["From"] = '"The Good Guys" <promo@sendgrid.example>'
        m["Subject"] = "20% off everything"
        m["Message-ID"] = "<x@y>"
        m.set_content("20% off everything this weekend")
        yield m

    seen: dict = {}
    found = mailwatch.candidates(msgs(), unmatched=seen)
    assert found == [], "it is not on the allowlist, so it is not processed"
    assert seen == {"The Good Guys": {"sendgrid.example"}}, "but it is reported"


def test_the_imap_search_uses_tokens_not_domains():
    """iCloud will not match a bare domain that sits directly after the '@'.

    FROM "harveynorman.com.au" returned 0 against the real account while
    FROM "harveynorman" returned 2. Searching by domain silently missed every
    retailer that sends from their bare domain.
    """
    assert "harveynorman" in mailwatch.SEARCH_TOKENS
    assert not any("." in t for t in mailwatch.SEARCH_TOKENS), (
        "a token with a dot is a domain, which is the bug this replaced"
    )
    # Every watched retailer must be reachable by at least one token.
    for domain in mailwatch.RETAILERS:
        assert any(t in domain for t in mailwatch.SEARCH_TOKENS), domain


def test_a_broader_search_does_not_widen_what_gets_read():
    """The search is loose on purpose; retailer_for stays strict."""
    assert mailwatch.retailer_for("Fake <deals@harveynorman.evil.com>") is None
    assert mailwatch.retailer_for("HN <x@harveynorman.com.au>") == "Harvey Norman"


def test_mail_that_arrived_but_carried_no_offer_is_reported():
    """'scanned 0' cannot tell an empty inbox from an inbox full of brand noise."""
    import email.message

    def msgs():
        for subject, body in (("Welcome to Harvey Norman", "Thanks for joining."),
                              ("20% off everything", "20% off everything this weekend")):
            m = email.message.EmailMessage()
            m["From"] = '"Harvey Norman" <do_not_reply@harveynorman.com.au>'
            m["Subject"] = subject
            m["Message-ID"] = f"<{subject}@x>"
            m.set_content(body)
            yield m

    gated: list = []
    found = mailwatch.candidates(msgs(), gated=gated)
    assert len(found) == 1, "only the one with an offer is worth a model call"
    assert len(gated) == 1
    assert "Harvey Norman: Welcome to Harvey Norman" in gated[0]
