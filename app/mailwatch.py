"""Read retailer marketing email and turn offers into leads on the board.

Runs on the machine where Thunderbird already lives, reading its local mail store
directly. There are no mail credentials anywhere in this: Thunderbird has already
fetched and stored the messages, and this only reads the files.

    python -m app.mailwatch --dry-run        # see what it would do, spend nothing
    python -m app.mailwatch                  # extract and post
    python -m app.mailwatch --since 2026-08-01 --limit 20

The watcher does not need to be more available than you are. If the lid is shut you are
not shopping that week either, which is why this is a laptop job and not a server one.
"""

from __future__ import annotations

import argparse
import email.header
import email.utils
import html
import json
import logging
import mailbox
import os
import re
import sys
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import offers, render
from .db import migrate, session, utcnow

log = logging.getLogger("shopwatch.mailwatch")

#: Senders worth reading. Anything else in the mailbox is ignored outright, which is
#: also the privacy boundary: personal mail is never opened or sent anywhere.
RETAILERS = {
    "jbhifi.com.au": "JB Hi-Fi",
    "email.jbhifi.com.au": "JB Hi-Fi",
    "thegoodguys.com.au": "The Good Guys",
    "email.thegoodguys.com.au": "The Good Guys",
    "harveynorman.com.au": "Harvey Norman",
    "appliancecentral.com.au": "Appliance Central",
    "appliancesonline.com.au": "Appliances Online",
    "binglee.com.au": "Bing Lee",
    "crowdshop.com.au": "Crowdshop",
}

#: Order-confirmation and account senders carry no offers and often carry personal
#: details, so they are skipped even though the domain matches.
SKIP_SUBDOMAINS = ("order.", "orders.", "receipt.", "noreply-account.")

#: What to actually put in an IMAP FROM search.
#:
#: NOT the domain. iCloud will not match a bare domain that sits directly after the "@":
#:   FROM "harveynorman.com.au"              -> 0 hits
#:   FROM "harveynorman"                     -> 2 hits
#:   FROM "do_not_reply@harveynorman.com.au" -> 2 hits
#: JB Hi-Fi only ever worked by accident, because they send from email.jbhifi.com.au
#: where the domain follows a dot. Searching by domain silently returned zero for every
#: retailer using their bare domain, which is most of them.
#:
#: The search is deliberately broader than the allowlist. retailer_for() still does the
#: precise domain check on whatever comes back, so widening this cannot widen what gets
#: read - it only stops things being missed.
SEARCH_TOKENS = (
    "jbhifi", "thegoodguys", "harveynorman",
    "appliancecentral", "appliancesonline", "binglee", "crowdshop",
)


def default_mailbox_paths() -> list[Path]:
    """Every Thunderbird mail file on this machine, newest profile first.

    Deliberately returns candidates rather than one path: Thunderbird keeps more than
    one profile and the live one is not always the one named 'default'.
    """
    root = Path(os.environ.get("SHOPWATCH_THUNDERBIRD", Path.home() / ".thunderbird"))
    if not root.exists():
        return []
    found = []
    for profile in root.iterdir():
        if not profile.is_dir():
            continue
        for store_dir in ("ImapMail", "Mail"):
            base = profile / store_dir
            if not base.exists():
                continue
            for account in base.iterdir():
                if not account.is_dir():
                    continue
                inbox = account / "INBOX"
                if inbox.is_file() and inbox.stat().st_size > 0:
                    found.append(inbox)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def body_html(message) -> str:
    """The richest HTML part, for rendering. Empty when the mail is plain text only."""
    best = ""
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if len(text) > len(best):
            best = text
    return best


def body_text(message) -> str:
    """Readable text from a message, HTML flattened. Never raises on a bad part."""
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_type() == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
        chunks.append(text)
    return re.sub(r"[ \t ]+", " ", "\n".join(chunks)).strip()


#: Retailer names as they appear in a From display name. Used only to notice mail that
#: is obviously from a watched retailer but arrives from a domain the allowlist does not
#: know - the shape a campaign sent via a third-party ESP takes.
DISPLAY_NAMES = {
    "jb hi-fi": "JB Hi-Fi",
    "jbhifi": "JB Hi-Fi",
    "the good guys": "The Good Guys",
    "harvey norman": "Harvey Norman",
    "appliance central": "Appliance Central",
    "bing lee": "Bing Lee",
    "crowdshop": "Crowdshop",
}


def mail_alert_retailers() -> set[str]:
    """Retailer names this watcher can actually turn into a parsed offer.

    The single source of truth for "does mailwatch understand this retailer's alert
    email", so a caller (the product wizard) reads it here rather than keeping its own
    copy that could drift the moment RETAILERS changes. Deliberately not persisted to
    the database: the database would then be a second copy of exactly this fact.
    """
    return set(RETAILERS.values())


def unmatched_retailer(sender: str) -> tuple[str, str] | None:
    """A sender whose NAME is a watched retailer but whose domain is not on the list.

    An allowlist that silently drops mail is a trap: sign up to a new list, the deals
    arrive from some ESP domain, and the watcher reports zero forever while looking
    perfectly healthy. This turns that into a line of output.
    """
    name, addr = email.utils.parseaddr(sender)
    if not addr or "@" not in addr:
        return None
    lowered = (name or "").lower()
    for needle, retailer in DISPLAY_NAMES.items():
        if needle in lowered:
            return (retailer, addr.split("@", 1)[1].lower())
    return None


def retailer_for(sender: str) -> str | None:
    addr = email.utils.parseaddr(sender)[1].lower()
    if not addr or "@" not in addr:
        return None
    domain = addr.split("@", 1)[1]
    if any(domain.startswith(s) for s in SKIP_SUBDOMAINS):
        return None
    for suffix, name in RETAILERS.items():
        if domain == suffix or domain.endswith("." + suffix):
            return name
    return None


def received_at(message) -> str:
    raw = message.get("Date")
    if raw:
        try:
            return email.utils.parsedate_to_datetime(raw).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return utcnow()


class ImapSource:
    """Read the account directly instead of Thunderbird's copy.

    Reuses the credential the /check-seek runner already has on opti, so nothing new
    is stored anywhere.

    Reads INBOX only. /check-seek also reads Trash, because Seek alerts get swiped
    away before it runs; deal mail is different - Rodney keeps what he wants watched
    in the inbox, so trawling 4,800 deleted messages every run is work nobody asked
    for and a wider reach into the mailbox than the job needs. Set ICLOUD_FOLDERS to
    "INBOX,Trash" for a one-off backfill of things already deleted.

    One trap kept from that pipeline: a malformed IMAP search returns an empty result
    set rather than an error, so an empty result is only believed after a control
    search in the same folder has returned something.
    """

    HOST = "imap.mail.me.com"

    def __init__(self, email_addr: str, password: str, folders: list[str] | None = None,
                 cleanup: bool = False):
        self.email_addr = email_addr
        self.password = password
        self.cleanup_enabled = cleanup
        #: message-id -> UID, for the INBOX only. Cleanup never touches other folders.
        self.inbox_uids: dict[str, bytes] = {}
        #: folder -> total messages, from the control search. Reported so that a run
        #: finding nothing can be told apart from a run that never looked: "scanned 0"
        #: alone reads the same whether the inbox is empty or the connection is dead.
        self.folder_totals: dict[str, int] = {}
        self.folders = folders or [
            f.strip() for f in os.environ.get("ICLOUD_FOLDERS", "INBOX").split(",")
            if f.strip()
        ]

    def messages(self, since: str | None = None) -> Iterator[Any]:
        import email as email_mod
        import imaplib

        conn = imaplib.IMAP4_SSL(self.HOST, 993)
        self._conn = conn
        try:
            conn.login(self.email_addr, self.password)
            for folder in self.folders:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    log.warning("cannot open folder %s", folder)
                    continue
                # Filter on the server, one search per retailer domain. Fetching every
                # message in INBOX and Trash just to read the From header pulls hundreds
                # of megabytes and takes minutes; the first live run never finished.
                # IMAP dates are unquoted, and a malformed search yields an empty result
                # set rather than an error, so a None result is a failure not "none found".
                date_clause = ""
                if since:
                    from datetime import datetime
                    when = datetime.strptime(since, "%Y-%m-%d").strftime("%d-%b-%Y")
                    date_clause = f" SINCE {when}"

                # A None result set is ambiguous on iCloud: it means both "no matches"
                # and "that search was malformed". Treating it as a failure cried wolf
                # on every empty folder; treating it as zero would hide a broken query.
                # So fire a control first - a search that MUST return something - and
                # only then read a None as a genuine zero.
                status, control = conn.uid("SEARCH", None, "ALL")
                if status != "OK" or not control or control[0] is None:
                    log.warning("%s: control search returned nothing, folder unusable", folder)
                    continue
                self.folder_totals[folder] = len(control[0].split())
                log.debug("%s holds %d messages", folder, self.folder_totals[folder])

                uids: list[bytes] = []
                for token in SEARCH_TOKENS:
                    criteria = f'(FROM "{token}"{date_clause})'
                    status, data = conn.uid("SEARCH", None, criteria)
                    if status != "OK":
                        log.warning("%s: search for %s returned %s", folder, token, status)
                        continue
                    if not data or data[0] is None:
                        continue  # a real zero, the control proved the folder answers
                    uids.extend(data[0].split())

                for uid in dict.fromkeys(uids):
                    status, fetched = conn.uid("FETCH", uid, "(RFC822)")
                    if status != "OK" or not fetched:
                        continue
                    # A FETCH response interleaves (header, body) tuples with bare
                    # bytes and ints for flags and closing parens. Only the tuples
                    # carry a message; indexing fetched[0][1] blindly crashes on the
                    # rest, which is what happened the first time this ran for real.
                    for item in fetched:
                        if (isinstance(item, tuple) and len(item) >= 2
                                and isinstance(item[1], bytes)):
                            message = email_mod.message_from_bytes(item[1])
                            if folder.upper() == "INBOX":
                                mid = str(message.get("Message-ID") or "").strip()
                                if mid:
                                    self.inbox_uids[mid] = uid
                            yield message
                            break
        finally:
            # Left open deliberately: cleanup() runs after the caller has decided which
            # messages were actually processed, and needs the same session.
            pass

    def cleanup(self, message_ids: set[str]) -> dict[str, Any]:
        """Move processed INBOX messages to Trash, the same as swiping them away.

        Copied wholesale from extract-seek-alerts.py's discipline, because the failure
        modes are identical:

        * only messages that were actually processed move - anything that errored stays
          in the inbox, where it is the alarm that something has broken;
        * INBOX only, never Trash or any other folder;
        * **never expunge**. iCloud purges Trash after 30 days on its own, and an
          interrupted run that has already moved mail must still be recoverable.
        """
        report: dict[str, Any] = {"moved": 0, "failed": [], "skipped": 0}
        if not self.cleanup_enabled:
            return report
        conn = getattr(self, "_conn", None)
        if conn is None:
            report["failed"].append("no open connection")
            return report

        targets = [(mid, uid) for mid, uid in self.inbox_uids.items() if mid in message_ids]
        report["skipped"] = len(self.inbox_uids) - len(targets)
        if not targets:
            return report
        try:
            status, _ = conn.select("INBOX")  # writable this time
            if status != "OK":
                report["failed"].append("could not open INBOX for writing")
                return report
            for mid, uid in targets:
                status, _ = conn.uid("MOVE", uid, "Trash")
                if status != "OK":
                    # Older servers have no MOVE; copy then flag, still no expunge.
                    status, _ = conn.uid("COPY", uid, "Trash")
                    if status == "OK":
                        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                    else:
                        report["failed"].append(mid[:40])
                        continue
                report["moved"] += 1
        except Exception as exc:
            report["failed"].append(f"{type(exc).__name__}: {exc}")
        return report

    def close(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
            self._conn = None


def imap_from_environment(cleanup: bool = False) -> ImapSource | None:
    """An ImapSource when the credential is present, else None. Never prompts."""
    addr = os.environ.get("ICLOUD_EMAIL", "").strip()
    password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
    if addr and password:
        return ImapSource(addr, password, cleanup=cleanup)
    return None


@dataclass
class Candidate:
    message_id: str
    retailer: str
    sender: str
    subject: str
    received: str
    body: str
    html: str = ""


def mbox_messages(paths: list[Path]) -> Iterator[Any]:
    for path in paths:
        try:
            box = mailbox.mbox(str(path))
        except Exception as exc:
            log.warning("could not open %s: %s", path, exc)
            continue
        yield from box


def candidates(messages: Iterator[Any], since: str | None = None,
               limit: int | None = None,
               unmatched: dict[str, set[str]] | None = None,
               gated: list[str] | None = None) -> list[Candidate]:
    """Retailer messages worth a model call, newest first, de-duplicated by Message-ID.

    `unmatched` collects senders that name a watched retailer but arrive from a domain
    the allowlist does not cover, so a missed subscription is visible rather than silent.
    """
    seen: set[str] = set()
    found: list[Candidate] = []
    unmatched = {} if unmatched is None else unmatched
    gated = [] if gated is None else gated
    for message in messages:
        sender = str(message.get("From", ""))
        retailer = retailer_for(sender)
        if not retailer:
            miss = unmatched_retailer(sender)
            if miss:
                unmatched.setdefault(miss[0], set()).add(miss[1])
            continue
        mid = str(message.get("Message-ID") or "").strip()
        if not mid or mid in seen:
            continue
        when = received_at(message)
        if since and when[:10] < since:
            continue
        subject = str(message.get("Subject", ""))
        try:
            subject = str(email.header.make_header(email.header.decode_header(subject)))
        except Exception:
            pass
        body = body_text(message)
        if not offers.worth_extracting(subject, body):
            gated.append(f"{retailer}: {subject[:60]}")
            continue
        seen.add(mid)
        found.append(Candidate(mid, retailer, sender, subject, when, body,
                                body_html(message)))
    found.sort(key=lambda c: c.received, reverse=True)
    return found[:limit] if limit else found


def render_and_read(cand: Candidate, claude_bin: str) -> Any:
    """Render the email and read the offer off the picture. None when there is no HTML."""
    if not cand.html:
        return None
    shots: list[Path] = []
    try:
        shots = render.render_tiles(cand.html)
        return offers.extract_from_image(
            [str(shot) for shot in shots], cand.subject, cand.sender, cand.received,
            cand.body, claude_bin=claude_bin,
        )
    except render.RenderError as exc:
        return offers.ExtractionResult(None, "render", str(exc))
    finally:
        if shots:
            render.cleanup(shots)


@dataclass
class RunSummary:
    scanned: int = 0
    already_seen: int = 0
    extracted: int = 0
    not_offers: int = 0
    errors: list[str] = field(default_factory=list)
    recorded: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    folders: dict[str, int] = field(default_factory=dict)
    rendered: int = 0
    render_helped: int = 0
    render_errors: list[str] = field(default_factory=list)
    unmatched: dict[str, list[str]] = field(default_factory=dict)
    gated: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned, "already_seen": self.already_seen,
            "extracted": self.extracted, "not_offers": self.not_offers,
            "recorded": len(self.recorded), "errors": self.errors,
            "cleanup": self.cleanup,
            "folders": self.folders,
            "rendered": self.rendered, "render_helped": self.render_helped,
            "render_errors": self.render_errors,
            "unmatched_senders": self.unmatched,
            "gated_out": self.gated,
            "leads": [
                {"retailer": o["retailer_name"], "summary": o["summary"],
                 "confidence": o["confidence"],
                 "matches": [m["rationale"] for m in o["matches"]]}
                for o in self.recorded if o["matches"]
            ],
        }


class Sink:
    """Where offers go. Local for tests and single-machine use, remote for the real one.

    This matters: the mail lives on the laptop and the board lives on opti, so the
    default path has to be an HTTP post. Writing to a local SQLite file would file
    everything into a database nobody looks at, and nothing would ever say so.
    """

    def known_message_ids(self) -> set[str]:
        raise NotImplementedError

    def record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError


class LocalSink(Sink):
    def __init__(self, conn) -> None:
        self.conn = conn

    def known_message_ids(self) -> set[str]:
        return {r["message_id"] for r in self.conn.execute("SELECT message_id FROM offers")}

    def record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return offers.record_offer(self.conn, payload)


class ApiSink(Sink):
    """Posts to a running shopwatch. Credentials come from the environment only."""

    def __init__(self, base_url: str, user: str | None, password: str | None) -> None:
        import requests

        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if user and password:
            self.session.auth = (user, password)

    def known_message_ids(self) -> set[str]:
        response = self.session.get(f"{self.base}/api/offers?live=false", timeout=30)
        response.raise_for_status()
        return {o["message_id"] for o in response.json()}

    def record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        response = self.session.post(f"{self.base}/api/offers", json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        return body.get("offer") if body.get("status") == "recorded" else None


def default_sink_config() -> tuple[str | None, str | None, str | None]:
    return (
        os.environ.get("SHOPWATCH_URL"),
        os.environ.get("SHOPWATCH_USER"),
        os.environ.get("SHOPWATCH_PASSWORD"),
    )


def run(paths: list[Path] | None = None, since: str | None = None,
        limit: int | None = None, dry_run: bool = False,
        model: str = offers.MODEL, client: Any = None,
        sink: Sink | None = None, source: Any = None,
        use_cli: bool = False, claude_bin: str = "claude",
        render_weak: bool = False) -> RunSummary:
    """One pass. `source` is anything yielding email.Message; defaults to the local mbox."""
    summary = RunSummary()
    processed: set[str] = set()
    if source is None:
        paths = paths or default_mailbox_paths()
        if not paths:
            summary.errors.append("no Thunderbird mail store found and no IMAP credential")
            return summary
        messages = mbox_messages(paths)
    else:
        messages = source.messages(since=since)

    with session() as conn:
        active = sink or LocalSink(conn)
        try:
            known = active.known_message_ids()
        except Exception as exc:
            summary.errors.append(f"could not reach the board: {type(exc).__name__}: {exc}")
            return summary
        unmatched: dict[str, set[str]] = {}
        gated: list[str] = []
        for cand in candidates(messages, since=since, limit=limit,
                               unmatched=unmatched, gated=gated):
            summary.scanned += 1
            if cand.message_id in known:
                summary.already_seen += 1
                # Recorded on an earlier run; it has served its purpose and can go,
                # otherwise the inbox never drains.
                processed.add(cand.message_id)
                continue
            if dry_run:
                continue

            if use_cli:
                result = offers.extract_via_cli(
                    cand.subject, cand.sender, cand.received, cand.body,
                    claude_bin=claude_bin,
                )
            else:
                result = offers.extract(
                    cand.subject, cand.sender, cand.received, cand.body,
                    client=client, model=model,
                )
            # If the text pass produced an offer with no amount or no deadline, the
            # numbers are probably drawn rather than written. Render and look.
            if render_weak and not result.error and offers.is_weak(result.offer):
                summary.rendered += 1
                looked = render_and_read(cand, claude_bin)
                if looked is not None and looked.offer is not None:
                    before = result.offer
                    result = looked
                    log.info("render recovered: amount %s -> %s, expires %s -> %s",
                             before.amount, looked.offer.amount,
                             before.expires, looked.offer.expires)
                    summary.render_helped += 1
                elif looked is not None and looked.error:
                    # A failed render is not a failed extraction: keep the text answer.
                    summary.render_errors.append(looked.error[:120])

            if result.error:
                # Deliberately NOT marked processed: it stays in the inbox, which is
                # how a broken extractor announces itself instead of quietly binning
                # the evidence.
                summary.errors.append(f"{cand.subject[:50]}: {result.error}")
                continue
            summary.extracted += 1
            processed.add(cand.message_id)
            parsed = result.offer
            if parsed is None or not parsed.is_offer:
                summary.not_offers += 1
                continue

            recorded = active.record({
                "message_id": cand.message_id,
                "retailer_name": cand.retailer,
                "sender": cand.sender,
                "subject": cand.subject,
                "received_at": cand.received,
                "raw_excerpt": cand.body[:600],
                "extracted_by": result.model,
                **parsed.model_dump(),
            })
            if recorded:
                summary.recorded.append(recorded)

        summary.gated = gated
        summary.unmatched = {k: sorted(v) for k, v in unmatched.items()}
        summary.folders = dict(getattr(source, "folder_totals", {}) or {})
        if not summary.folders and source is None:
            summary.folders = {"thunderbird": summary.scanned}
        if source is not None and getattr(source, "cleanup_enabled", False) and not dry_run:
            summary.cleanup = source.cleanup(processed)

        conn.execute(
            "INSERT INTO mail_cursor (source, last_run_at, messages_seen)"
            " VALUES ('thunderbird', ?, ?)"
            " ON CONFLICT(source) DO UPDATE SET last_run_at = excluded.last_run_at,"
            " messages_seen = mail_cursor.messages_seen + excluded.messages_seen",
            (utcnow(), summary.scanned),
        )
    return summary


#: Where a render fault is announced. EMPTY BY DEFAULT, so a dev run, a test, or
#: anyone driving this by hand never publishes anything. Only the unattended
#: runner sets it.
FAULT_NTFY_URL = os.environ.get("SHOPWATCH_FAULT_NTFY_URL", "")
FAULT_NTFY_TOKEN_FILE = os.environ.get(
    "SHOPWATCH_FAULT_NTFY_TOKEN_FILE", "/root/.ntfy_pub_token"
)


def notify_render_failures(d: dict, url: str | None = None,
                           token_file: str | None = None) -> str | None:
    """Tell Rodney the renders are failing. Returns a PROBLEM string, or None on success.

    Renders fail silently by their nature: the text pass still produces an answer,
    the message is still recorded, and the run still exits 0. The only trace was
    the gap between "rendered N" and "the artwork added something in M", written
    to a journal nobody reads. On 2026-09-18 that hid a timeout which left a
    headless Chrome running on opti with network access for ten minutes.

    Returns rather than raises, and never throws, because this runs at the end of
    a successful run and must not destroy the work that run just did. The caller
    decides how loud to be, which is the one thing this must not get to choose:
    an alarm that can quietly decide not to ring is the failure it exists to catch.
    """
    errors = d.get("render_errors") or []
    if not errors:
        return None

    url = url if url is not None else FAULT_NTFY_URL
    if not url:
        # Not configured is not an error: interactive and test runs land here.
        return None

    path = token_file if token_file is not None else FAULT_NTFY_TOKEN_FILE
    try:
        token = Path(path).read_text().strip()
    except OSError as exc:
        # Unauthenticated publish is rejected 403, so a missing token means the
        # alert WILL NOT arrive. Say which file, because the usual cause is the
        # job no longer running as root.
        return f"cannot read the ntfy token at {path}: {exc}"

    body = "\n".join(f"- {e}" for e in errors[:5])
    if len(errors) > 5:
        body += f"\n- ... and {len(errors) - 5} more"
    body += (
        f"\n\n{len(errors)} of {d.get('rendered', 0)} render(s) failed, so those offers "
        "were read from the email text only and anything drawn into the artwork was "
        "missed. Check for a leftover container: docker ps --filter "
        "ancestor=zenika/alpine-chrome:latest"
    )

    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": f"shopwatch: {len(errors)} email render(s) failed",
            "Priority": "default",
            "Tags": "framed_picture",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            code = response.getcode()
    except Exception as exc:  # noqa: BLE001 - any failure here means no alert arrived
        return f"ntfy publish to {url} failed: {type(exc).__name__}: {exc}"

    # A 200 means ntfy ACCEPTED it, not that a phone showed it. That distinction has
    # bitten before, so this claims only what it actually checked.
    if code != 200:
        return f"ntfy publish to {url} returned HTTP {code}, so the alert was not accepted"
    return None


def render_failure_lines(d: dict) -> list[str]:
    """Lines naming the renders that FAILED, for stderr. Empty when none did.

    render_errors was collected into the summary and then never printed anywhere,
    so a render that TIMED OUT read exactly like one where the artwork simply did
    not help: both showed up only as the gap between "rendered N" and "the artwork
    added something in M".

    On 2026-09-18 that hid a 120s timeout which left a headless Chrome running on
    opti with network access and its bind-mount already deleted. egress-watch
    found it. This log said "errors 0".

    Separate from the `errors` list on purpose, because a failed render genuinely
    is not a failed extraction: the text pass still produced an answer and the
    message is still recorded. It is not an error, and it is not nothing, so it
    gets said out loud rather than being inferred from a subtraction.
    """
    errors = d.get("render_errors") or []
    lines = [f"  RENDER FAILED {err}" for err in errors[:3]]
    if len(errors) > 3:
        lines.append(f"  ... and {len(errors) - 3} more render failure(s)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.mailwatch")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be read; makes no API calls")
    parser.add_argument("--since", help="only messages on or after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="stop after N candidates")
    parser.add_argument("--model", default=offers.MODEL)
    parser.add_argument("--mailbox", action="append", type=Path,
                        help="explicit mail file; repeatable")
    parser.add_argument("--imap", action="store_true",
                        help="read the account directly (ICLOUD_EMAIL / ICLOUD_APP_PASSWORD)")
    parser.add_argument("--cli", action="store_true",
                        help="extract with headless Claude Code instead of the API; needs no key")
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--render", action="store_true",
                        default=os.environ.get("SHOPWATCH_RENDER") == "1",
                        help="when the text yields no amount or deadline, render the email "
                             "and read the artwork; needs docker")
    parser.add_argument("--cleanup", action="store_true",
                        default=os.environ.get("SHOPWATCH_MAIL_CLEANUP") == "1",
                        help="move processed INBOX messages to Trash; never expunges")
    parser.add_argument("--local", action="store_true",
                        help="write to the local database instead of posting to the board")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    migrate()

    sink = None
    url, user, password = default_sink_config()
    if not args.local and url:
        sink = ApiSink(url, user, password)
        log.info("posting offers to %s", url)
    elif not args.local and not args.dry_run:
        print("SHOPWATCH_URL is not set, so there is nowhere to send what this finds.\n"
              "Set it (plus SHOPWATCH_USER and SHOPWATCH_PASSWORD), or pass --local to\n"
              "write into the database this machine can see.", file=sys.stderr)
        return 2

    source = None
    if args.imap:
        source = imap_from_environment(cleanup=args.cleanup)
        if source is None:
            print("--imap needs ICLOUD_EMAIL and ICLOUD_APP_PASSWORD in the environment.",
                  file=sys.stderr)
            return 2
        log.info("reading %s over IMAP", source.email_addr)

    summary = run(paths=args.mailbox, since=args.since, limit=args.limit,
                  dry_run=args.dry_run, model=args.model, sink=sink, source=source,
                  use_cli=args.cli, claude_bin=args.claude_bin,
                  render_weak=args.render)

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
    else:
        d = summary.as_dict()
        if d["folders"]:
            where = ", ".join(f"{k} holds {v}" for k, v in d["folders"].items())
            print(f"looked in: {where}")
        elif args.imap:
            # No control total means no folder answered. That is a broken run, not a
            # quiet one, and it must not be reported as "nothing found".
            print("WARNING: no folder answered a control search; nothing was read.",
                  file=sys.stderr)
        if d["gated_out"]:
            # "scanned 0" alone cannot tell "no retailer mail arrived" apart from
            # "mail arrived and none of it carried an offer". Say which.
            print(f"{len(d['gated_out'])} retailer email(s) carried no offer:")
            for line in d["gated_out"][:6]:
                print(f"    {line}")
        print(f"scanned {d['scanned']} (seen before {d['already_seen']}), "
              f"extracted {d['extracted']}, not offers {d['not_offers']}, "
              f"recorded {d['recorded']}, errors {len(d['errors'])}")
        for lead in d["leads"]:
            print(f"\n  {lead['retailer']} [{lead['confidence']}] {lead['summary']}")
            for rationale in lead["matches"]:
                print(f"    -> {rationale}")
        for retailer, domains in d["unmatched_senders"].items():
            print(f"  NOTE: mail from {retailer} arrived via {', '.join(domains)}, which is "
                  f"not on the allowlist and was skipped. Add it to RETAILERS in "
                  f"app/mailwatch.py to start watching it.", file=sys.stderr)
        if d["rendered"]:
            print(f"  rendered {d['rendered']} email(s); the artwork added something in "
                  f"{d['render_helped']}")
            for line in render_failure_lines(d):
                print(line, file=sys.stderr)
        if d["cleanup"]:
            c = d["cleanup"]
            print(f"\n  moved {c['moved']} processed message(s) to Trash"
                  f"{', left ' + str(len(c['failed'])) + ' behind' if c['failed'] else ''}")
        for err in d["errors"][:5]:
            print(f"  ERROR {err}", file=sys.stderr)

    if source is not None:
        source.close()

    # Announce a failed render, and be loud if the announcement itself fails.
    # Rule: the alarm's own failure must not be quieter than the thing it watches.
    # A render fault otherwise leaves no trace a human ever sees: the run exits 0,
    # the offer is still recorded, and the only evidence is a line in a journal
    # nobody reads.
    exit_code = 1 if summary.errors and not summary.recorded else 0
    problem = notify_render_failures(summary.as_dict())
    if problem is not None:
        print(f"  COULD NOT ALERT: {problem}", file=sys.stderr)
        print("  Render failures happened and nobody was told. Fix the notifier.",
              file=sys.stderr)
        # Non-zero so systemd sees it. STATED GAP: shopwatch-mailwatch.service
        # carries no OnFailure=ntfy-fail@%n.service, unlike most units on opti, so
        # today this exit code reaches the journal and stops there. Adding that
        # line to the unit is what makes this reachable.
        exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
