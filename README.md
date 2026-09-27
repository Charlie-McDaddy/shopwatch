<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/logo-dark.svg">
  <img src="docs/brand/logo.svg" alt="" width="72" height="72">
</picture>

# Shopwatch

[![CI](https://github.com/rodlunt/shopwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/rodlunt/shopwatch/actions/workflows/ci.yml)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm--Noncommercial--1.0.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12-informational)
![Version](https://img.shields.io/badge/version-0.2.0-informational)

A self-hosted shopping comparison and price-watch board for a homelab. One product, many
retailer listings, ranked by **delivered price** rather than headline price, with per-field
provenance so hand-confirmed values are never clobbered by an automated refresh.

Built to replace a single-file HTML comparison board. It is a research tool, not a shop.

**Download it and run your own copy** - see [Deploy with Docker](#deploy-with-docker). The
author's own instance stays live and keeps its own data; cloning this repo starts you with
an empty board.

<a href="docs/screenshots/board-light.png"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/board-dark.png">
  <img src="docs/screenshots/board-light.png" alt="The board: every tracked product, ranked by delivered price">
</picture></a>

<a href="docs/screenshots/product-light.png"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/product-dark.png">
  <img src="docs/screenshots/product-light.png" alt="A product's detail view: the price axis, listings and specs">
</picture></a>

<a href="docs/screenshots/group-light.png"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/group-dark.png">
  <img src="docs/screenshots/group-light.png" alt="A watch group: several candidates on one merged axis, hovering one highlights its price">
</picture></a>

<a href="docs/screenshots/wizard-light.png"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/wizard-dark.png">
  <img src="docs/screenshots/wizard-light.png" alt="The guided product wizard, with an LLM-suggested model candidate">
</picture></a>

<a href="docs/screenshots/llm-setup-light.png"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/llm-setup-dark.png">
  <img src="docs/screenshots/llm-setup-light.png" alt="Set up your LLM: connecting your own Claude Code or Codex CLI, no API key">
</picture></a>


---

## What it actually does

* **A product exists once.** `Samsung Q-Series 9.1.4ch Soundbar`, model `HW-Q930H/XY`, with
  a listing under each of Crowdshop, Harvey Norman, JB Hi-Fi, The Good Guys, Appliance
  Central, and anyone else found later.
* **Delivered price is the ranking metric.**
  `advertised + freight - cashback - rebate - coupon`.
* **Unresolved freight is unresolved, never free.** A NULL freight yields a *provisional*
  delivered price, marked `PROV`, classified `UNRESOLVED`, and ranked with a configurable
  penalty (default $60) so it cannot silently outrank a slightly dearer listing whose
  delivered price is confirmed.
* **Provenance is per field, not per listing.** Every editable field carries a state
  (`LIVE`, `MANUAL`, `IMPORTED`, `UNVERIFIED`, `STALE`), a source, a timestamp, and a
  manual lock. A locked field survives every scrape and every import until you clear it.
* **Verification is tracked per aspect** (model, price, stock, freight, condition,
  warranty, contents), not as one vague boolean.
* **History is append-only.** Prices are never deleted because the market moved. The
  recorded historical low is lowered automatically by a confirmed better price and is
  never raised automatically.

### Price classification

Thresholds are inclusive, evaluated best first, and expressed as delivered prices:

| Label | Condition |
|---|---|
| `HISTORICAL LOW TERRITORY` | delivered ≤ `historical_low_price` |
| `EXCELLENT` | delivered ≤ `excellent_price` |
| `TRIGGER MET` | delivered ≤ `trigger_price` |
| `ABOVE TARGET` | above the trigger |
| `UNRESOLVED` | no price, or freight unconfirmed on a listing that would otherwise rate |

An unresolved listing that is *above* target still reads `ABOVE TARGET`, because freight
can only make it worse.

**`historical_low_price`/`excellent_price`/`trigger_price` auto-fill from
`lowest_known_price` when unset** (`store.maybe_derive_price_targets`, issue #101). A
product can carry a confirmed `lowest_known_price` - via `maybe_lower_known_low` finding
a genuinely cheaper delivered price, or a human confirming a "Research historical low"
finding - for a long time before anyone types in any of the three thresholds, and until
they do it sits un-actionable: no listing can ever rate above `ABOVE TARGET`. Whenever
`lowest_known_price` changes, every one of the three still NULL gets filled from it:
`historical_low_price = lowest_known_price` and `excellent_price = lowest_known_price`
(matching the record IS what both mean here), `trigger_price = lowest_known_price × 1.10`
(loose enough to catch a genuine improving trend before the record itself breaks). This
only ever writes into a NULL field - a value already there, typed by hand or from an
earlier auto-fill the user has not cleared, is never touched - and a filled-in value is
marked `*_price_auto` so the edit dialog can label it "auto" rather than showing it as a
deliberately chosen target. Changing it by hand locks it; clearing it back to blank hands
it back to auto-fill.

`historical_low_price` is not just a display default the way the other two are:
`pricing.classify()` reads it directly and checks it first, so on a product that already
has a `lowest_known_price` and no `historical_low_price`, this can change a listing's
live classification the moment it runs - a listing at or under the known low newly rates
`HISTORICAL LOW TERRITORY` instead of whatever it rated before. Because
`historical_low_price` and `excellent_price` default to the same value, the `EXCELLENT`
tier has no width under pure auto-derived defaults - `HISTORICAL_LOW` is checked first
and wins the tie - until a human types over one of the two, which locks it.

**A one-time backfill catches products that already existed before this shipped**
(`store.run_price_target_backfill`, run from `app/main.py`'s `lifespan` on every boot,
self-marking via a `python_backfills` table so it only actually sweeps once). The
derivation above only ever fires from a write (`create_product`/`update_product`/
`maybe_lower_known_low`) - a product that already had a `lowest_known_price` sitting
there before this feature existed would otherwise stay un-derived forever, since nothing
would touch it again until some unrelated future edit happened to.

### The three states a listing can be in

| State | On the board | On the axis | Can it be the answer |
|---|---|---|---|
| **Active** | yes | yes | yes |
| **Ruled out** | yes, struck through | yes, struck through | **no** |
| **Deactivated** | no | no | no |

`active = 0` means *this is not a real listing*: a duplicate, a wrong model, something
entered by mistake. It disappears.

`ruled_out = 1` means *this is real and I will not buy it*. It stays listed and stays
plotted, in its true position, but `best_listing()` skips it, so it can never drive the
verdict or the best-price marker.

The distinction exists because neither of the other two states fits a real listing you
have decided against. Crowdshop is the case that forced it: **$869 is genuinely the
cheapest price anyone advertises**, but it is a group-buy whose freight is only costed
after the purchase closes, so it can never become a delivered price. Deactivating it
would have hidden the cheapest number on the board with no explanation. Leaving it alone
had the verdict reading *"Unconfirmed. Crowdshop shows $869..."* for hours after that
price had been rejected, because cheapest and best were being treated as the same
question and they are not.

Ruling out records a reason (`ruled_out_reason`) and a timestamp, and is reversible from
the listing detail. The reason is stored rather than remembered, because *why did we
discount this* is the question the next session asks.

**A ruled-out listing is still price-watched.** The price watch keys off `active`, not
`ruled_out`, so a retailer you have dismissed keeps being checked and its history keeps
accruing. That is deliberate: "it would need to be a lot cheaper" is only actionable if
something is still watching for a lot cheaper.

---

## Where things live

| | |
|---|---|
| Live | https://shop.yourdomain.example (LAN only, `basic_auth`) |
| Code | GitHub `rodlunt/shopwatch` (private) |
| Deploy | merge to `main`; a green CI run deploys, see **Deployed on opti** |
| Secrets | `SECURITY.md` names every lane |
| Tests | `.venv/bin/python -m pytest` and `.venv/bin/python -m ruff check .` |

**One remote.** `origin` is GitHub and holds the history, issues, CI and the deploy.
The bare repo on opti that used to accept push-to-deploy is retired: it still exists, but
its `pre-receive` hook rejects pushes and points you back at `origin`.

**CI gates the deploy.** The Deploy workflow runs on opti's self-hosted runner and only
fires on a CI run that passed, against the exact commit CI verified. A commit failing
test, lint or audit has no path to production.

## Architecture

```
app/
  main.py            FastAPI: HTML board + JSON API
  config.py          environment-driven configuration
  db.py              SQLite connect/migrate/backup/restore (WAL)
  migrations/*.sql   forward-only, applied in filename order, never destructive
  pricing.py         delivered price, classification, ranking
  provenance.py      per-field state and manual locks: the single write path
  store.py           queries and view assembly
  ingest.py          findings import, snapshot import/export, CSV export
  price_watch.py     the watcher (CLI / cron / API)
  alerts.py          rule evaluation, ntfy / webhook / console delivery
  seed.py            category profiles, retailers, HW-Q930H/XY board
  research.py        the wizard's research-job lifecycle (deploy/research-runner.py claims)
  llm_jobs.py         the wizard's model-suggestion job lifecycle (tools/llm-helper.py claims)
  retailers/
    base.py          adapter framework, JSON-LD reader, Observation
    crowdshop.py  harvey_norman.py  jb_hifi.py  the_good_guys.py  appliance_central.py
  templates/  static/
tools/
  llm-helper.py      run on YOUR OWN machine - see "Suggesting a model number" below
  llm-helper-bundle/ README.txt + double-click launchers zipped by GET /tools/llm-helper.zip
deploy/
  research-runner.py opti-only host script - see "The product wizard's research step"
tests/
```

Stack: Python 3.12, FastAPI, Jinja2, vanilla JavaScript, stdlib `sqlite3`. No ORM, no
build step, no cloud dependency.

---

## Deploy with Docker

```bash
cd ~/Projects/shopwatch
cp .env.example .env          # edit ntfy URL etc.
docker compose up -d --build
docker compose logs -f shopwatch
```

Then open `http://SERVER-IP:8477/`.

Day-to-day:

```bash
docker compose ps                                  # status and health
docker compose restart shopwatch
docker compose down                                # stop (the volume survives)
docker compose up -d --build                       # after a code change
docker compose exec shopwatch python -m app.price_watch          # manual watch run
docker compose exec shopwatch python -m app.seed                 # idempotent re-seed
docker compose exec shopwatch python -c "from app.db import backup; print(backup())"
```

### Changing the port

`SHOPWATCH_PORT` in `.env` sets both the published port and the port uvicorn binds inside
the container, so one variable moves both. To publish on a different LAN port from the
container port, edit the `ports:` line in `docker-compose.yml` to `"9000:8477"`.

### Where the data lives

The named volume `shopwatch-data` is mounted at `/data`, with the database at
`/data/shopwatch.db`. Find it on disk with:

```bash
docker volume inspect shopwatch_shopwatch-data --format '{{ .Mountpoint }}'
```

Running outside Docker, the default is `data/shopwatch.db` relative to the working
directory; override with `SHOPWATCH_DB`.

### Backup

```bash
# writes /data/backups/shopwatch-YYYYmmdd-HHMMSS.db using SQLite's online backup API,
# which is consistent while the app is running
docker compose exec shopwatch python -c "from app.db import backup; print(backup())"

# or over HTTP
curl -X POST http://SERVER-IP:8477/api/backup

# and pull a copy out of the volume
docker compose cp shopwatch:/data/backups ./backups
```

A JSON export (`GET /api/export`) is the portable alternative and survives schema changes,
where a `.db` copy is exact but version-tied. Take both.

### Restore

Restore is an **offline** operation. SQLite will not notice the file changing underneath an
open connection.

```bash
docker compose stop shopwatch
docker compose cp ./backups/shopwatch-20260911-090000.db shopwatch:/data/restore.db
docker compose start shopwatch
docker compose exec shopwatch python -c \
  "from app.db import restore; print(restore('/data/restore.db'))"
docker compose restart shopwatch
```

`restore()` keeps the database it replaced as `shopwatch.replaced-<timestamp>.db` next to
the live file, and removes the stale `-wal`/`-shm` pair.

---

## Watching retailer email for offers

**Who actually sends.** JB Hi-Fi (341 messages) and The Good Guys (66) are the bulk.
Harvey Norman was subscribed to on 2026-09-11 and sends from the bare
`harveynorman.com.au`. Appliance Central, Bing Lee and Crowdshop send nothing yet and
sit in the allowlist inert; Appliance Central is worth subscribing to, since they hold
the best confirmed price on the board.

**A run says what it saw at every stage**, because "scanned 0" on its own cannot tell an
empty mailbox from a broken one from a mailbox full of brand noise:

```
looked in: INBOX holds 15
2 retailer email(s) carried no offer:
    Harvey Norman: Please confirm your Harvey Norman account
    Harvey Norman: Welcome to Harvey Norman
scanned 0 (seen before 0), extracted 0, not offers 0, recorded 0, errors 0
```

A sender whose display name is a watched retailer but whose domain is not on the
allowlist is reported too - the shape a campaign sent through a third-party ESP takes,
and otherwise a silent miss forever.


The price scrapers can only see product pages, and the discounts that matter are not on
them. Measured on a real corpus of 204 retailer emails: **not one mentioned a tracked
model**, but 67 carried a quantified offer, and those offers are where the money is -
"Spend $2000 or more on TVs & get $500 OFF" never touches a product page.

Keyword matching is not enough. On that corpus it flagged 44 emails as relevant when a
handful were: a catalogue blast mentions dozens of products and several dollar figures,
and "$400 off" next to the word "TV" somewhere in 6,000 characters means nothing.
Working out what an offer *covers* means reading it, so that step goes to a model with a
fixed output schema.

```bash
python -m app.mailwatch --dry-run          # what it would read; makes no API calls
python -m app.mailwatch                    # extract and post to the board
python -m app.mailwatch --since 2026-09-01 --limit 20
```

### How it is wired

It runs **on opti**, reusing credentials that were already there for the job-search
`/check-seek` pipeline, so nothing new is stored anywhere:

| Needs | Where it already lives | Why |
|---|---|---|
| Mail access | `ICLOUD_EMAIL` / `ICLOUD_APP_PASSWORD` in `/srv/prod/career/runner.env` | Proven: that runner has been polling on it every 15 minutes |
| The model | `CLAUDE_BIN` + `CLAUDE_CODE_OAUTH_TOKEN`, same file | career drives headless Claude Code, so extraction costs nothing beyond the subscription and needs **no `ANTHROPIC_API_KEY`** |

```bash
ssh root@YOUR-SERVER-IP
set -a; . /srv/prod/career/runner.env; set +a
cd /srv/prod/shopwatch/repo
export PYTHONPATH=$PWD SHOPWATCH_DB=/tmp/mw.db \
       SHOPWATCH_URL="http://$(docker inspect shopwatch --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'):8477"
/srv/prod/shopwatch/mailwatch-venv/bin/python -m app.mailwatch --imap --cli --since 2026-09-01
```

Caddy holds the basic_auth, so on the docker network the app answers directly and the
job needs no password of its own.

Three things learned the hard way against the real account, all of them now in the code:

* **INBOX only, by choice.** `/check-seek` also reads Trash because Seek alerts get
  swiped away before it runs. Deal mail is different: keep what you want watched in the
  inbox and the job stays small. Trawling 4,800 deleted messages every run is work
  nobody asked for and a wider reach into the mailbox than the task needs. For a one-off
  backfill of things already deleted, `ICLOUD_FOLDERS=INBOX,Trash`.
* **Search on the server.** The first live run fetched every message in both folders just
  to read a From header and never finished. One SEARCH per retailer domain, then fetch
  only the matches.
* **Search by token, never by domain.** iCloud will not match a bare domain sitting
  directly after the `@`:

  ```
  FROM "harveynorman.com.au"              -> 0 hits
  FROM "harveynorman"                     -> 2 hits
  FROM "do_not_reply@harveynorman.com.au" -> 2 hits
  ```

  JB Hi-Fi only ever worked by accident, because they send from
  `email.jbhifi.com.au` where the domain follows a dot. Every retailer using their
  bare domain was searched for and never found, with the run reporting a clean zero.
  `SEARCH_TOKENS` is deliberately broader than the allowlist; `retailer_for()` still
  does the precise domain check on the results.
* **A `None` result set is ambiguous on iCloud** - it means both "no matches" and "that
  search was malformed". Each folder now runs a control search that must return
  something before an empty per-domain result is believed to be a real zero.

The local Thunderbird mbox still works (`--dry-run` with no `--imap`) and is the better
source for a one-off backfill of history, since the server copy only holds what has not
been cleared out.

### Reading the artwork

Retailer marketing puts the numbers in pictures, so `--render` (or `SHOPWATCH_RENDER=1`)
adds a second pass: render the email with headless Chrome and read the offer off the
screenshot.

Measured on a live Good Guys email. Extracted text is 4,301 characters containing `20%`
three times and **zero** occurrences of `Ends`, `13/09/2026` or `11.59` - those are
pixels. Side by side:

| | text pass | render pass |
|---|---|---|
| amount | 20 | 20 |
| expires | *nothing* | **2026-09-13** |
| confidence | medium | high |

Without it the board recorded that offer as never expiring, when it actually died two
days later.

It only runs when the text pass comes back **weak** - a real offer with no amount, no
deadline or no code. A complete offer never renders, and a product announcement never
renders however prettily it is drawn, so most mail still costs a single text call. A
failed render degrades to the text answer rather than discarding it.

"No code" joined the weak list on 27 September 2026, after The Good Guys' "Just For You"
mail of 24 September was recorded with no code at all. Its subject carried "20% off" and
"Ends Sunday", so the text pass produced an amount and an expiry, the offer read as
complete, and the render never ran. The code (`JFY2026`), the eligible categories and
the exclusion list were all in two banner images. A percent-off with no code cannot be
acted on, which is exactly what the render is for.

The render takes **tiles**, not one screenshot. Chrome's `--screenshot` only ever
captures the top 900x3,200 of the page, and that same Good Guys mail put its exclusion
list at about 4,000px. So the page is rendered again with the document pulled up by one
screen (a negative top margin injected ahead of the email's markup), up to
`SHOPWATCH_RENDER_TILES` (default 2) screens, stopping at the first one that comes back
blank. All tiles go to the model in one call, because the code is on one screen and its
exclusions on another and they belong to one offer. Measured 27 September: the second
tile carried the full exclusion block and the fine print, legibly.

Chrome is also told to stop waiting: `--timeout=45000` (`SHOPWATCH_CHROME_TIMEOUT_MS`)
makes it screenshot whatever has loaded after 45 seconds. Marketing email carries
tracking pixels and image hosts that never answer, and without the flag Chrome waited for
every one of them; two of the three renders on opti between 24 and 26 September ran into
the 120-second subprocess timeout that way and fell back to the text pass. Measured
27 September: an `<img>` pointing at a port that drops the SYN never produced a screenshot
in 130 seconds without the flag and produced one at 45 seconds with it.
`--virtual-time-budget` did not help.

Chrome runs in a container (`zenika/alpine-chrome`), so nothing is installed on the host
and it can see only the one directory it is handed. The screenshots are deleted as soon
as they have been read.

It also runs with `--disable-dev-shm-usage`, which is not optional. Docker gives the
container a 64 MB `/dev/shm`, and on image-heavy mail Chrome's GPU process crashes in it
and exits cleanly with no screenshot, which read as a render that "produced nothing".
Measured 27 September against the same Good Guys email, five renders each: three of five
succeeded without the flag, five of five with it. A render that still ends with no file
gets exactly one more go; a timeout does not.

Two limits, both found by trying:

* **It only works while the offer is live.** The hero images are served from a live URL
  and swapped when the offer ends - an August email now renders as "This offer has
  ended" rather than what it once said. Useless for a backfill, fine for a daily job.
* **It cannot avoid the tracking pixel.** The images carry the offer, so they have to
  load, and loading them tells the retailer the mail was opened. On a schedule, that is
  a slightly different signal from opening it yourself.

There is no text shortcut, either: the "View Online" link was checked and its hosted page
is the same image-based email, 1,427 characters of text with none of the offer in it.

### Clearing the inbox as it goes

`--cleanup` (or `SHOPWATCH_MAIL_CLEANUP=1`) moves messages it has processed to Trash,
the same as swiping them away. It is **off unless asked for**, so a manual run never
touches the mailbox.

Three constraints, all copied from `extract-seek-alerts.py` because the failure modes
are the same:

* **Only processed messages move.** Anything the extractor choked on stays in the inbox.
  That is not politeness, it is the alarm: a broken extractor that binned its own
  evidence would look exactly like a quiet week.
* **INBOX only.** Never Trash, never any other folder.
* **It never expunges.** iCloud purges Trash after 30 days by itself, so an interrupted
  run is always recoverable.

Messages are addressed by UID, not sequence number - sequence numbers shift the instant
a message moves, and a cleanup keyed on them deletes the wrong mail.

### Two rules it will not break

**An offer is a lead, never a price.** Nothing in this path writes to a listing's
advertised price, freight or coupon. It produces a suggestion with the arithmetic done
and says plainly that it has not been applied.

**An offer only discounts the retailer that sent it.** A Good Guys code projected from
Crowdshop's cheaper price produces a number you cannot buy at any counter. If the sending
retailer is not on a product's board there is no match, even though they may well sell
the thing - add their listing and the offer becomes visible.

### Cost

A regex gate runs first, so only emails carrying an actual offer reach the model - 137 of
204 never do. At roughly seven offer-emails a week that is a few dollars a year on
`claude-opus-5`; `--model claude-haiku-4-5` is cheaper again. Extraction runs at effort
`low`, which is the right setting for reading marketing copy.

### When it runs, and why

Once a day at **10:00 Brisbane**, chosen from 383 retailer emails spanning 163 days
rather than picked out of the air.

```
BY HOUR (Brisbane)                    BY DAY
  06:00    4  #                         Mon   45  ###########################
  07:00   24  #####                     Tue   44  ###########################
  08:00  202  ####################      Wed   44  ###########################
  09:00   44  ##########                Thu   71  ###########################################
  10:00   14  ###                       Fri   73  ############################################
  12:00   18  ####                      Sat   67  ########################################
  17:00   12  ###                       Sun   39  ########################
  20:00   14  ###
```

**70% arrive between 07:00 and 09:59, and 202 of 383 at 08:00 exactly.** Nothing at all
lands between 21:00 and 06:00. Both retailers behave the same way: JB Hi-Fi peaks at
08:00 (150 of 317), The Good Guys at 08:00 (52 of 66).

A run at 10:00 puts the entire morning batch through within three hours of arrival. The
afternoon stragglers wait until tomorrow, which costs nothing: these offers run for days,
not hours.

Thursday to Saturday is the promo window and Sunday is quietest, but every weekday still
carries 39-73 emails over the period. **Skipping days is not worth it** - restricting to
Thu-Sat would have missed 133 of 383.

### Running it on a timer

```ini
# ~/.config/systemd/user/shopwatch-mail.timer
[Unit]
Description=Read retailer email for offers
[Timer]
OnCalendar=*-*-* 08,18:00:00
Persistent=true          # catches up after the lid has been shut
[Install]
WantedBy=timers.target
```

## The product wizard's research step

The wizard replacing the old "Track something new" dialog (a guided, multi-step flow:
required fields, then optional, then a price target, then retailers, then "Let's go")
can trigger a live research pass across chosen retailers.

**Same shape as the mail watcher above, on purpose.** A research job never runs inside
this container: `POST /api/research-jobs` just queues a row and returns immediately
(202). A host-level script on opti - its own small venv (`research-venv`), exactly like
`mailwatch-venv` - polls `POST /api/research-jobs/claim` every 2 minutes
(`shopwatch-research-runner.timer`), runs the headless `claude` CLI once per retailer,
and reports back via `POST /api/research-jobs/{id}/results` and `/complete`. The
container never holds the Claude Code OAuth token and never gains new outbound egress;
only the host script does, reusing the same `/srv/prod/career/runner.env` credential
mailwatch already reuses. Nothing new to store or rotate.

**At most one active job per product.** A database constraint (not just application
logic) rejects a second `QUEUED` or `RUNNING` job for a product with a 409, so a
double-click or a retried request can't spend the shared research quota twice. A
finished job (`DONE` or `FAILED`) never blocks a new one, so a retailer that came back
`NEEDS_MANUAL_CHECK`, `BLOCKED` or `TIMED_OUT` can always be tried again - either from
the wizard's own "done" screen while it's still open, or from the product page's own
"Research retailers again" action afterwards, which pre-selects whichever retailers
didn't turn up a price last time via `GET /api/products/{id}/research-jobs/latest`.

**A job that never reports back is judged purely by elapsed time**
(`research.JOB_CEILING_SECONDS`, currently 10 minutes), never by anything at container
startup - the host script is a separate process from this container, so an ordinary
`git merge` → redeploy never interrupts a job actually in progress on the host.

**Findings land through the ordinary import path**, `POST /api/import`, under the
ordinary `IMPORTED` provenance state - a research job is a new way to trigger that
endpoint, not a second way for prices to enter the database.

**A same-day price-target suggestion never wears the words a real historical low
earns.** `GET /api/products/{id}/price-suggestion` refuses to return anything from
fewer than 2 real listings, and when it does, the response is deliberately labelled
"estimate, from N listings" rather than reusing `HISTORICAL LOW TERRITORY` or
`EXCELLENT` - those are earned through real tracking over time.

## Paste a listing URL

Sometimes you already have the one thing that matters: a link to the listing. "Add
retailer manually" wants a retailer name typed in, and the wizard's research step wants a
retailer picked from a list - neither is built for "I found this page, just add it."
The product page's **Add from a URL** button is: paste an address, nothing else,
and `POST /api/products/{id}/retailers/from-url` works out the rest.

1. The retailer is derived from the URL's own domain and created via
   `store.ensure_retailer` if it's new - the same call "Add retailer manually" already
   uses, so a retailer created this way behaves identically to one typed by hand.
2. The listing is created (or matched, if you paste the same URL twice) with that URL
   saved, unconditionally, before anything else is attempted - a URL with nowhere to
   land was the actual gap this closes, and that much happens even if every step below
   fails.
3. If the domain matches a retailer shopwatch already has a scraper adapter for, that
   adapter is called synchronously and the price lands immediately, under the ordinary
   `LIVE` provenance state - the same as an ordinary price-watch pass.
4. Otherwise (no adapter, or the adapter's fetch failed - bot protection, a dead link),
   a one-off research job is queued, scoped to **that exact URL** rather than a general
   search: the runner (`deploy/research-runner.py`) reads the page directly with
   `WebFetch` and nothing else - `WebSearch` is withheld from that call outright, not
   just asked not to be used, so "read this page, don't go looking for a different one"
   is enforced by the tool grant, not only the prompt. Its finding still lands through
   the ordinary `POST /api/import` path, exactly like every other research job.

A job already running for this product (the wizard's own concurrency guard, one active
job per product) does not block the listing from being created - it just means the
research half sits out this round, same as it would for any other retry.

## Researching a historical low

Shopwatch only ever knows what it has observed since a product was added - `lowest_known_price`
only ever moves downward, and only from a confirmed delivered price found going forward
(`store.maybe_lower_known_low`). There was no way to ask "has this ever been cheaper?" for
something that already existed before tracking started. The product page's **"Research
historical low"** button asks the same research-job pipeline described above a different
question to close that gap.

**Same job table, a second `kind`.** `research_jobs.kind` is `"price"` (the default, and the
only kind before this) or `"historical_low"`. A price job asks "what's it selling for today",
once per selected retailer. A historical-low job asks "has this ever been cheaper", once for the
whole product - no retailer selection, no `research_job_results` rows. `POST /api/research-jobs`
takes an optional `"kind"` in its body; `retailer_ids` is required for `"price"` and ignored for
`"historical_low"`. Both kinds share the same one-active-job-per-product guard, since both spend
the same shared, credentialed CLI quota.

**The host runner asks a different question, not a different pipeline.** `deploy/research-runner.py`
still claims the job, still runs the headless `claude` CLI with the same `WebSearch`/`WebFetch`
tools, still reports back and completes it - `process_historical_low_job` is one call instead of a
per-retailer loop, and the prompt asks the model to look specifically for AU price-history
trackers, deal-forum threads and cached listings, not today's advertised price.

**The finding never lands through `/api/import`, and never touches the product.** A price job's
findings become real listings through the ordinary import path; a historical-low job's finding is
a single best-effort guess, reported via `POST /api/research-jobs/{id}/historical-low` and stored
only on the job row (`historical_low_price` / `_date` / `_retailer` / `_notes` / `_confidence`).
There is no code path from those columns to `products.lowest_known_price` - the product page shows
the finding labelled "UNCONFIRMED ESTIMATE - not a real listing" (never `HISTORICAL LOW TERRITORY`
or any other earned classification, for the same reason the price-suggestion guard above exists),
and "Use this" only prefills the ordinary product-edit dialog's `lowest_known_*` fields. Saving it
still goes through `PATCH /api/products/{id}`, exactly like typing the numbers in by hand - a
research pass is a source for that decision, never a shortcut around it.

**A byproduct of the same search: other retailers noticed along the way.** `HISTORICAL_LOW_PROMPT`
already asks the model to search broadly across "any legitimate Australian retailer" for the
lowest price ever seen, so it will often incidentally notice retailers currently selling the item
that shopwatch does not yet track. Rather than throw that away, the model is also asked for a
best-effort `other_retailers` list (`{"name": ..., "url": ...}`), stored as a raw JSON string on
`research_jobs.historical_low_other_retailers` - same convention `retailer_search_jobs.result` and
`llm_jobs.result` already use, parsed client-side rather than server-side. Both the runner
(`deploy/research-runner.py`) and the API (`app/research.py`) drop any candidate whose URL is not
`http`/`https` before it is ever stored - a URL surfaced by an LLM's web search is untrusted
content relayed through the model, and the product page renders it straight into a link's `href`.
The product page shows each candidate as a checkbox next to the historical-low result; ticking one
and pressing "Add ticked as listings" calls the exact same `POST /api/products/{id}/retailers` (no
URL) or `POST /api/products/{id}/retailers/from-url` (URL present) endpoints "Add retailer
manually" and "Add from a URL" already use - no separate listing-creation path, and nothing
is added until a person ticks the box and confirms, same discipline as "Use this" above.

An excluded retailer (see "Excluding a retailer" below) never reaches this checkbox list: it is
named directly in `HISTORICAL_LOW_PROMPT` so the model is told not to bother, and dropped again
server-side (`app/research.py`'s `_clean_other_retailers`) as the backstop if it turns up anyway.

**The reason text is dot points, not one dense paragraph.** `HISTORICAL_LOW_PROMPT` asks for
`reason_points` (an array, one distinct fact/source per string) rather than a single `reason`
sentence - `deploy/research-runner.py`'s `_format_reason` joins them into `"- "`-prefixed lines,
one per line. A reply that still uses the old single-`reason` shape (a stale cached CLI session,
or a model that ignored the field) falls back to that string unchanged, so nothing already relying
on it breaks. The rendering side has two independent implementations that must stay in sync: the
product page uses `app/main.py`'s `notes_html` Jinja filter (real `<ul>`/`<li>`, HTML-escaped since
this is LLM-relayed web content); the historical-low dialog, which shows a job's result before it
is ever saved to the product, uses `app.js`'s `notesElement` to build the identical structure in
JS. Both fall back to a single plain paragraph when fewer than two `"- "` lines are present, so old
single-paragraph notes (pre-`reason_points`, or typed by hand in Edit) render exactly as before.

## Excluding a retailer

Issue #112: some retailers just are not relevant - wrong city, a marketplace you don't use,
somewhere you had a bad experience - and there was no way to tell shopwatch "never suggest this
one again." The **Retailers** page (top nav) lists every known retailer with how many active
listings it currently has, and an Exclude/Un-exclude button per row (`PATCH /api/retailer/{id}`,
singular - deliberately not another route nested under `/api/retailers/{id}`, which already means
a *listing* id everywhere else in this API).

Excluding a retailer:
- deactivates every listing already tracked under it, across every product, the same way deleting
  a single listing does (`active = 0`) - it stops being watched, not just stops being suggested;
- blocks `POST /api/products/{id}/retailers` and `.../retailers/from-url` from creating a new
  listing under it (a clear 400, not a 500);
- drops it from the wizard's and "Research retailers again"'s retailer picker;
- drops it from the historical-low search's "other retailers found" candidates, and is named
  directly in the search prompt so the model is asked not to look for it in the first place.

**Un-excluding does not reactivate the listings it switched off.** A listing turned off by
exclusion might also have been turned off for an unrelated reason, so resuming it silently just
because the exclusion lifted would be a surprise, not a convenience. There is no separate
"reactivate" action (shopwatch has never had one, even for a listing deactivated the ordinary way
via `DELETE /api/retailers/{listing_id}`) - "reactivate by hand" means adding the retailer again
through the ordinary "Add retailer manually" or "Add from a URL" flow, which creates a fresh
listing.

## Suggesting a model number ("Set up your LLM")

**TL;DR - 5 minutes, no coding:**

1. Click **"Set up your LLM"** at the top of the page, then **"Download the helper"**.
2. Unzip it.
3. Double-click the one file matching your computer and AI tool (a plain-English
   `README.txt` is in the zip too if you're not sure which):

   |            | Claude Code               | Codex                     |
   |------------|----------------------------|----------------------------|
   | Windows    | `run-claude-windows.bat`   | `run-codex-windows.bat`   |
   | Mac        | `run-claude-mac.command`   | `run-codex-mac.command`   |
   | Linux      | `run-claude-linux.sh`      | `run-codex-linux.sh`      |

4. A window opens and asks for your shopwatch username and password (the same ones
   you already use to open this site). Enter them and leave that window open.

That's it. Back in the wizard, type what you're watching and click "Suggest models" -
candidates appear as clickable chips a few seconds later. You'll need Claude Code or
Codex already installed and signed in on that computer, but nothing else: no API key,
no `pip install`, nothing this server stores or bills for.

<details>
<summary>How it works, the command-line option, and why it's built this way</summary>

The wizard's "Suggest models" button turns a rough description ("Dreame RoboMower")
into a short list of real candidate model numbers, using **your own** Claude Code or
Codex CLI login - no API key, nothing this server holds or bills for.

**The zip** (`GET /tools/llm-helper.zip`) contains `llm-helper.py`, a plain-English
`README.txt`, and a double-click launcher per OS x backend combination, each already
pointed at this shopwatch instance - no command to type or edit.

**Setup, from the command line**, if you'd rather:

1. Download `tools/llm-helper.py` (the running app serves it directly at
   `/tools/llm-helper.py`, or grab it from this repo).
2. Run it:
   ```
   python3 llm-helper.py --url https://your-shopwatch-url --backend claude
   ```
   (`--backend codex` for Codex.) No `pip install` needed - the script is pure standard
   library. Enter your shopwatch username and password when prompted.
3. Leave it running. Back in the wizard, type what you're watching and click "Suggest
   models" - it queues a job, the helper picks it up within a couple of seconds, and
   candidates appear as clickable chips that fill "Exact model" for you to verify.

**Why this shape, not a container-side API key:** every other credentialed feature in
this app (mailwatch, the research runner above) deliberately keeps the credential off
the web container entirely - a compromised container has nothing worth stealing. This
extends that same principle one step further: the credential does not even belong to
*this* server. It belongs to whoever's machine is running the helper, using whatever CLI
subscription they already pay for. Nothing to configure server-side, nothing to rotate.

**Same job-queue shape as the research runner above** (`app/llm_jobs.py` mirrors
`app/research.py` closely): `POST /api/llm-jobs` queues a job and returns immediately,
`POST /api/llm-jobs/claim` is how a helper claims the next one, `POST
/api/llm-jobs/{id}/complete` reports the answer back, and a job stuck `RUNNING` for
longer than two minutes (`llm_jobs.JOB_CEILING_SECONDS`) is judged stale and failed
automatically - a killed terminal must not leave the wizard waiting forever.

**A candidate is a suggestion to verify, never applied automatically.** "Exact model" is
used to match future price imports, so a wrong-but-plausible model number would silently
corrupt matching later. The prompt asks the model to return zero candidates rather than
guess when it isn't genuinely confident, and every response is shown with a caveat note
about confidence or recency - clicking a chip only fills the text field, exactly as if
you had typed it yourself.

**The URL that goes into the zip is validated, not trusted.** `GET
/tools/llm-helper.zip?url=...` writes that value as plain data into the zip
(`shopwatch-url.txt`, which each launcher reads at runtime) rather than ever splicing
it into a script's own text, and rejects anything that isn't exactly `https?://host
[:port]` before it gets that far - a download link is something someone could be
tricked into clicking, so the value behind `url` gets no more trust than any other
request parameter.

</details>

## Deployed on opti

Live at **https://shop.yourdomain.example** (LAN only, trusted `*.yourdomain.example` wildcard cert).

| Thing | Where |
|---|---|
| Self-hosted runner | `actions.runner.rodlunt-shopwatch.opti-shopwatch.service` |
| Deploy script | `/usr/local/bin/shopwatch-deploy` (root, from `deploy/`) |
| Bare repo (retired) | `root@YOUR-SERVER-IP:/root/shopwatch.git`, rejects pushes |
| Stack (Dockge-managed) | `/srv/prod/shopwatch` |
| Working clone | `/srv/prod/shopwatch/repo` |
| Database | docker volume `shopwatch_shopwatch-data` at `/data/shopwatch.db` |
| Secrets | `/srv/prod/shopwatch/.env` (0600, opti-only, never in the repo) |
| Caddy vhost | `opti-stacks/caddy/Caddyfile` in the `smart-home` repo |
| DNS record | `dns.hosts` in `/srv/prod/pihole/etc-pihole/pihole.toml` |

The container publishes **no host port**. Caddy reaches it by container name on the `web`
network, which is what supplies the certificate and keeps it off the LAN without a
hostname.

### How a deploy happens

```bash
B=$(git branch --show-current) && git push origin "$B"   # then open a PR
```

Merging to `main` runs CI. If test, lint and audit all pass, the Deploy workflow starts on
opti's self-hosted runner, checks out the exact commit CI verified, and hands that SHA to
`/usr/local/bin/shopwatch-deploy` over `sudo`.

The script fetches that commit **by id** and checks it out detached, rather than pulling a
branch. A pull takes whatever the branch points at when it runs, which after a second
merge is not the commit CI verified (hardening rule 14). It then copies
`deploy/docker-compose.opti.yml` into place and rebuilds **only** when `app/`,
`Dockerfile`, `requirements.txt` or the compose file changed, so a README-only merge moves
the clone and leaves the container running. After a rebuild it waits for the container to
report `healthy` and fails the job if it does not, because `docker compose up` returning 0
only means the container started.

Migrations are forward-only and run on boot, so a deploy never wipes the database.

There is no manual deploy path by design. To ship, merge to `main`.

### Alerting is on

`SHOPWATCH_ALERTS_ENABLED` is `"true"` in the compose file, and `/srv/prod/shopwatch/.env`
carries a non-empty `SHOPWATCH_NTFY_URL` and `SHOPWATCH_NTFY_TOKEN`. Both are rendered from
the SOPS vault at `smart-home:opti-stacks/shopwatch/secrets.sops.env` by `sops-render.sh`.
Never hand-edit that file: a hand-edited secret is one nothing can rotate.

The server rejects unauthenticated publishes with a 403, so a missing or wrong token would
fail at the server rather than in the app. The app reports that as `[ALERT-FAILURE]` and a
non-zero exit instead of swallowing it.

This cuts across the house "ntfy is for faults only" policy: a price alert is not a fault.
That was a deliberate decision, made in #7, not a default inherited by accident.

### Scheduling the watch

**There is no timer, and the reason is egress monitoring, not scraping.**

The reason recorded here used to be "three of the five seeded retailers cannot be
scraped at all, so a schedule would be a no-op that looks like coverage". That was
measured before two adapters had ever been run, and it was wrong: JB Hi-Fi and The Good
Guys both return a price, stock status and condition from a plain fetch with no
warnings. A schedule would refresh two real listings.

The actual blocker turned up the first time anything scraped from the container. opti
runs `egress-watch`, which alerts on a container opening an outbound connection it has
not made before. A single probe run produced four pushes to `security-events`:

```
egress: shopwatch -> Cloudflare [104.21.4.235]
egress: shopwatch -> shops.myshopify.com (unrecognised)
egress: shopwatch -> UNKNOWN (no reverse-DNS) [45.223.141.121]
egress: shopwatch -> UNKNOWN (no reverse-DNS) [104.26.1.138]
```

That is egress-watch working correctly. The container had genuinely never reached a
retailer before. But a scheduled run would fire those on **every** pass, and they cannot
be suppressed cleanly:

* retailers are Cloudflare-fronted and anycast IPs rotate, so `/32` allowlist entries go
  stale and the alerts come back
* `allowlist.conf` deliberately refuses wide CIDRs, in its own words because a
  Cloudflare `/13` "would blind this tool to a compromised container beaconing to any
  other Cloudflare-fronted host"
* leaving it alone trains you to ignore `security-events`, which is the one topic that
  has to keep meaning "something is wrong"

**Decision, 2026-09-11: run it by hand, no timer.** Two of four watchable listings
refresh, and both sit around $700 above the trigger, so automation buys very little
today. That is not worth weakening egress monitoring for. Revisit if an Appliances
Online adapter gets written, since they are the only reachable retailer in the price
bracket that matters.

A run also produces two failures every time, and they are **counted differently**, which
matters if you are checking the summary to confirm them:

| Retailer | What happens | Counted as |
|---|---|---|
| Harvey Norman | `FetchError` on the bot interstitial | `errors` |
| Appliance Central | fetches fine, parses no price | `unresolved` |

`check_listing` returns `"error"` only for a fetch failure; anything that fetched but
yielded no price is `"ok" if advertised_price is not None else "unresolved"`. So one
error out of four attempted makes the run `partial`, not `failed`, and the CLI exits 0.
Only a run where **everything** errored, or where an alert could not be delivered, exits
non-zero.

Anyone reading "two failures" and checking the `errors` count will see one and conclude
something changed. Run it by hand with:

```bash
ssh root@YOUR-SERVER-IP 'docker compose -f /srv/prod/shopwatch/docker-compose.yml \
  exec -T shopwatch python -m app.price_watch --trigger cron'
```

## Running without Docker

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m app.seed
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8477
```

---

## The price watch

```bash
python -m app.price_watch                 # everything
python -m app.price_watch --product 1     # one product
python -m app.price_watch --no-alerts     # check prices, send nothing
python -m app.price_watch --json          # machine-readable summary
curl -X POST http://SERVER-IP:8477/api/price-watch/run -d '{}' \
     -H 'Content-Type: application/json'
```

Each run: fetches every active listing that has both an adapter and a URL, verifies the
model, writes the fields that are not manually locked, appends a history observation,
downgrades fields that have not refreshed inside `SHOPWATCH_STALE_AFTER_DAYS` to `STALE`,
then evaluates the alert rules.

**Exit codes matter.** The command exits non-zero when every attempted check errored, or
when an alert could not be delivered. A run that exits 0 genuinely did something.

### cron

> ⚠ **Do not install this without reading "Scheduling the watch" above.** There is
> deliberately no timer: a scheduled run pushes four `security-events` alerts on every
> pass and they cannot be suppressed without weakening opti's egress allowlist. These
> recipes are kept for the day that changes, or for a host with no egress monitoring.

```cron
# /etc/cron.d/shopwatch: twice a day, output captured, failures mailed
MAILTO=you@example.com
17 7,19 * * * root cd /opt/shopwatch && docker compose exec -T shopwatch \
  python -m app.price_watch --trigger cron >> /var/log/shopwatch.log 2>&1
```

Set `MAILTO` or pipe to something that reads the exit code. A cron line that redirects
everything to a logfile nobody reads is how a dead watcher stays dead.

### systemd timer

> ⚠ Same warning as above: no timer is installed on opti on purpose.

`/etc/systemd/system/shopwatch-watch.service`:

```ini
[Unit]
Description=Shopwatch price watch
[Service]
Type=oneshot
WorkingDirectory=/opt/shopwatch
ExecStart=/usr/bin/docker compose exec -T shopwatch python -m app.price_watch --trigger cron
# A failed run is visible rather than silent.
OnFailure=status-email@%n.service
```

`/etc/systemd/system/shopwatch-watch.timer`:

```ini
[Unit]
Description=Run the Shopwatch price watch twice a day
[Timer]
OnCalendar=*-*-* 07,19:17:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload && systemctl enable --now shopwatch-watch.timer
systemctl list-timers shopwatch-watch.timer     # proves it is scheduled
journalctl -u shopwatch-watch.service -n 50     # proves it ran
```

---

## Alerts (ntfy)

```dotenv
SHOPWATCH_NTFY_URL=https://ntfy.example.com/shopwatch
SHOPWATCH_NTFY_TOKEN=tk_...          # omit for an unauthenticated topic
SHOPWATCH_NTFY_PRIORITY=high
SHOPWATCH_WEBHOOK_URL=               # optional, receives the alert as JSON
SHOPWATCH_ALERTS_ENABLED=true
```

Console output always happens. A delivery failure is printed as `[ALERT-FAILURE]`, logged
at ERROR, returned in the run summary, and makes the CLI exit non-zero. An ntfy 2xx means
ntfy accepted the publish, not that a device displayed it.

Rules live in the `alert_rules` table, one row per rule:

| Column | Meaning |
|---|---|
| `conditions` | comma-separated allowlist, e.g. `NEW` or `FACTORY_SECOND,CARTON_DAMAGED` |
| `max_delivered` | fire at or below this **delivered** price |
| `require_resolved` | ignore listings whose freight is unconfirmed (default on) |
| `require_complete` | ignore listings with no recorded included components |
| `min_change` | suppress a re-alert when the price moved less than this |

The seeded Q930H rules are *new stock ≤ $900 delivered* and *secondary stock ≤ $800
delivered, complete systems only*. `POST /api/alerts/evaluate` is a dry run: it reports
what would fire without sending anything.

---

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/products` | every product with its assembled matrix |
| `POST` | `/api/products` | `{name, model, category, ...}` |
| `GET` | `/api/products/{id}` | `?sort=delivered\|headline\|retailer\|stock\|condition\|verification\|diff` |
| `PATCH` | `/api/products/{id}` | targets, specs, dimensions, verdict |
| `DELETE` | `/api/products/{id}` | archives, never deletes |
| `GET` | `/api/products/{id}/retailers` | the listings |
| `POST` | `/api/products/{id}/retailers` | add a listing |
| `POST` | `/api/products/{id}/retailers/from-url` | `{"url": "..."}`; derives the retailer, always saves the URL, scrapes or queues research |
| `GET` | `/api/products/check-model?model=...` | deterministic duplicate check for the wizard |
| `GET` | `/api/products/{id}/price-suggestion` | a same-day trigger-price estimate from real listings, or `null` below 2 |
| `POST` | `/api/retailers` | `{"name": "..."}`; ensures a retailer exists with no listing attached |
| `GET` | `/api/retailers` | every known retailer, with `excluded`, `listing_count`, and adapter/mail-alert metadata merged in |
| `POST` | `/api/groups` | `{"name": "..."}`; several candidates tracked together, no shared pricing |
| `GET` / `PATCH` | `/api/groups/{id}` | the merged comparison view / rename, edit notes |
| `DELETE` | `/api/groups/{id}` | detaches every member (never deletes them - a group is just a grouping); a group that empties out via this, "Leave group", or a permanent product delete removes itself automatically |
| `POST` / `DELETE` | `/api/products/{id}/group` | join (`{"group_id": 1}` or `{"name": "..."}` to create one) / leave |
| `PATCH` | `/api/retailer/{id}` | **singular** - a retailer-level flag, currently `{"excluded": true\|false}` (issue #112); excluding deactivates every listing under it, across every product; un-excluding does NOT reactivate them |
| `POST` | `/api/research-jobs` | `{"product_id": 1, "retailer_ids": [...], "kind": "price"\|"historical_low"}`; `retailer_ids` required for `price`, ignored for `historical_low`; 202, 409 if one is already active for this product |
| `GET` | `/api/research-jobs/{id}` | poll a job's status; per-retailer results for `price`, `historical_low_*` fields for `historical_low` |
| `POST` | `/api/research-jobs/claim` | host-runner only: claims the oldest QUEUED job |
| `POST` | `/api/research-jobs/{id}/results` | host-runner only: one retailer's outcome (`price` jobs) |
| `POST` | `/api/research-jobs/{id}/historical-low` | host-runner only: the job's proposed finding (`historical_low` jobs), plus an optional best-effort `other_retailers` list - never written to the product, never turns into a retailer or listing itself |
| `POST` | `/api/research-jobs/{id}/complete` | host-runner only: marks DONE or FAILED |
| `GET` | `/api/retailers/{listing_id}` | one enriched listing |
| `PATCH` | `/api/retailers/{listing_id}` | inline edit; **locks the field as MANUAL** |
| `POST` | `/api/retailers/{listing_id}/clear-override` | `{"field": "freight"}` or `{"field": "all"}` |
| `POST` | `/api/retailers/{listing_id}/verification` | `{"aspect": "freight", "status": "VERIFIED"}` |
| `POST` | `/api/retailers/{listing_id}/ruled-out` | `{"ruled_out": true, "reason": "..."}`; stays visible, never the answer |
| `DELETE` | `/api/retailers/{listing_id}` | deactivates the listing, which hides it |
| `GET` | `/api/price-history/{product_id}` | observations, newest first |
| `POST` | `/api/price-watch/run` | `{"product_id": 1, "send_alerts": true}` |
| `GET` | `/api/price-watch/runs` | recent runs with per-listing results |
| `POST` | `/api/alerts/evaluate` | dry run |
| `POST` | `/api/import` | findings, a list of findings, or a snapshot |
| `GET` | `/api/export` / `/api/export.csv` | snapshot / flat comparison |
| `POST` | `/api/backup` | server-side database snapshot |
| `GET` | `/api/meta` | enums, categories, adapters |
| `GET` | `/healthz` | touches the database, not just the port |

`PATCH /api/retailers/{id}` accepts `{"manual": false}` to record a value without locking
it, and `{"record_history": false}` to skip the history row.

---

## Running several products at once

A product carries two independent fields, deliberately not merged:

| Field | Question it answers | Values |
|---|---|---|
| `verdict` | Is this worth buying? | `BUY` / `MAYBE` / `IGNORE` |
| `status` | Where is it in the process? | `ACTIVE` / `PURCHASED` / `PARKED` |

You can be `verdict=BUY status=ACTIVE` (want it, still hunting) or `verdict=MAYBE
status=PURCHASED` (bought it anyway). Collapsing the two loses that.

The board defaults to `ACTIVE`, with counts per status in the filter row, and always sorts
ACTIVE first so a new research target never buries the thing you are actually hunting. Add
as many products as you like: `POST /api/products` or the **New product** button needs only
a name and an exact model, and everything else can be filled in later.

`PARKED` is for a product you have not given up on but do not want notifications about. It
keeps every listing, price and history row, and stops the watch checking it.

## Marking a purchase

**Mark purchased** on the product page, or:

```bash
curl -X POST http://SERVER-IP:8477/api/products/1/purchase \
  -H 'Content-Type: application/json' -d '{
    "listing_id": 1,
    "price_paid": 894,
    "advertised_paid": 869,
    "freight_paid": 25,
    "order_reference": "CS-12345",
    "warranty_months": 12,
    "price_protection_until": "2026-10-11"
  }'
```

`price_paid` is the **delivered** figure and is the only required field, because it is the
only number that settles the question. `listing_id` is optional: plenty of things get
bought in a shop or from someone who was never on the board, so pass `retailer_name`
instead and it still records cleanly.

Recording a purchase:

* moves the product to `PURCHASED`, which **stops its alerts** and takes it out of the
  watch, so a bought item does not keep telling you its price is good;
* writes a `price_history` row with source `purchase` and `freight_resolved = 1`, because
  a price someone actually paid is the most trustworthy observation there is;
* keeps everything else. `DELETE /api/products/{id}/purchase` undoes the record and returns
  the product to `ACTIVE`, deliberately leaving the history row behind: it records a price
  that really was paid, and history is append-only even when the bookkeeping was wrong.

### Price protection

`price_protection_until` (a `YYYY-MM-DD` date) is optional and off by default. Set it and
the watch keeps running on that product and raises a **Price protection** alert if a
*confirmed* delivered price drops below what you paid, which is what you need to claim
against a retailer's price guarantee. Unconfirmed prices are ignored: sending someone to a
checkout to discover it was never actually cheaper is worse than saying nothing. Leave the
field empty and buying simply stops the watch.

## Importing external research

The import path exists so research done elsewhere can land on the board without hand
retyping. Post one finding, a list of them, or `{"findings": [...], "source": "..."}`:

```json
[{
  "model": "HW-Q930H/XY",
  "retailer": "JB Hi-Fi",
  "price": 1699,
  "freight": null,
  "stock": "Available",
  "condition": "New",
  "warranty": "1 year",
  "source_url": "https://www.jbhifi.com.au/...",
  "checked_at": "2026-09-11T02:00:00Z",
  "verification": {"model": true, "price": true, "stock": true}
}]
```

```bash
curl -X POST http://SERVER-IP:8477/api/import \
     -H 'Content-Type: application/json' --data @findings.json
```

On import the app matches the product by exact model (case and punctuation insensitive,
region suffix significant: `/XY` ≠ `/XU`), matches or creates the retailer, creates the
listing if it is new, writes only the fields that are not manually locked, stores a price
history row, and updates `last_checked_at`. The response lists `updated` and
`preserved_manual` per finding, so you can see exactly what a lock stopped.

An unknown model is rejected rather than guessed at. Pass
`{"findings": [...], "create_missing_product": true}` if you want it created.

`"freight": null` means unresolved. Do not send `0` unless free shipping is confirmed.

---

## Adding a retailer adapter

1. Create `app/retailers/my_retailer.py`:

```python
from .base import Observation, RetailerAdapter, observation_from_json_ld, register

@register
class MyRetailerAdapter(RetailerAdapter):
    slug = "my_retailer"
    name = "My Retailer"
    homepage = "https://example.com"
    # Fields this retailer structurally cannot expose to a scraper.
    never_scrapable = ("freight", "pickup_status")

    def parse(self, html: str, expected_model: str | None = None) -> Observation:
        obs = observation_from_json_ld(html, expected_model)
        # retailer-specific fallbacks here; leave anything you cannot read as None
        return obs
```

2. Import it in `app/retailers/__init__.py` so registration happens.
3. Point the retailer row at it: `UPDATE retailers SET adapter = 'my_retailer' WHERE ...`,
   or set it when the retailer is first created.
4. Add a test with a saved fragment of the real page.

Rules for adapters: return `None` for anything you could not determine, never a plausible
guess and never `0`. Do not defeat CAPTCHAs or anti-bot measures. Keep `never_scrapable`
honest, it is what tells the UI a field is a manual one. If the retailer serves a
challenge page with HTTP 200, add its marker to `BLOCK_MARKERS` in `retailers/base.py`
so the block surfaces as an error rather than as an empty product page.

## Adding a category

Categories are rows, not schema. Either:

```bash
curl -X POST http://SERVER-IP:8477/api/categories/espresso_machine/fields \
  -H 'Content-Type: application/json' \
  -d '{"field_key":"boiler_type","label":"Boiler type","kind":"text","sort":1}'
```

or add an entry to `CATEGORY_PROFILES` in `app/seed.py` and re-run `python -m app.seed`
(idempotent). Product values live in the `specs_json` object, so no migration is needed.
`kind` is one of `text`, `number`, `bool`, `select`.

---

## Tests

```bash
.venv/bin/python -m pytest        # 144 tests
.venv/bin/python -m ruff check .
```

They cover the behaviour that is easy to break quietly: delivered-price arithmetic,
cashback, threshold classification, ranking with unresolved freight, manual-override
preservation through both scrapes and imports, import merge behaviour, model matching,
stale-data handling, adapter normalisation, fetch failures not destroying stored values,
alert suppression on trivial moves, and migration/backup safety.

---

## Retailer scraping limitations

Read this before trusting an empty field.

### What was actually measured

Probed 2026-09-11 from an Australian residential IP: first with the adapters' own
`fetch()`, then the same products read in a signed-in desktop browser. Re-run rather than
trust this table; it is a snapshot.

Re-probed 2026-09-11 from the container, against the live listing URLs, after the
retailer-SKU fix. This replaces an earlier table that recorded two adapters as
"untested URL" and Appliance Central as a 403; both were wrong by the time anyone
acted on them.

| Retailer | Adapter | URL on file | Plain fetch | Yields a price |
|---|---|---|---|---|
| **JB Hi-Fi** | yes | yes | 886 KB | **$1,699, In Stock, no warnings** |
| **The Good Guys** | yes | yes | 1220 KB | **$1,699, In Stock, no warnings** |
| Appliance Central | yes | yes | 100 KB, **not a 403** | no, parses nothing |
| Harvey Norman | yes | yes | **Incapsula interstitial** | no, raised as FetchError |
| Crowdshop | yes | **none** | n/a | unreachable: the watcher needs a URL |
| Appliances Online | **none** | yes | n/a | no adapter written |
| Bing Lee, Betta | none | none | n/a | search-page prices only |

**Two of the four watchable listings return a price.** "Watchable" means an adapter,
a URL and an active listing, which is what `price_watch` requires. Crowdshop has an
adapter and no URL, so it is not reachable however well the adapter works, and
Appliances Online has the opposite problem.

**Harvey Norman is the instructive one.** It returns HTTP 200 with a "Pardon Our
Interruption" interstitial, so a naive adapter parses it, finds no price, and reports
`unresolved`, identical to what it reports for a product page that genuinely has no
price. `retailers.base.detect_block()` catches that family of pages and raises
`FetchError` instead, so a block reaches the run as an **error**, which is what it is.
Add a marker there if a new retailer starts doing the same.

**The Good Guys is the surprise.** It publishes a real freight figure on the product page,
but only against the browser's remembered preferred store. A headless scraper carrying no
store cookie sees nothing, so this stays a browser-assisted field rather than an adapter
one.

### Structural limits, which no amount of scraping fixes

* **Freight is not scrapable at any of the seeded retailers.** Every one quotes delivery
  at checkout against a postcode. Freight is a manual field by design, which is why an
  unresolved freight is ranked with a penalty rather than assumed to be zero.
* **Store pickup is not scrapable** for the same reason: it needs a store selection.
* **Crowdshop quotes a price guide range** on some listings. The low end becomes the
  headline figure and the full range is kept in `price_guide`. Its displayed `$0` delivery
  is not treated as confirmed free shipping.
* **Appliance Central's headline price is pre-coupon.** Record the coupon in
  `coupon_discount`, not by editing the advertised price down, or the history becomes a
  fiction.
* **Search-engine snippets and cached prices are not a source.** The live product page
  wins. The older ~$999 Q930H snippets floating around are stale.
* **A model mismatch flags the listing** (`model: FLAGGED`) rather than updating it
  quietly.
* **Playwright is not used.** If a retailer genuinely needs a headless browser, add it as
  an optional dependency in that adapter alone; the rest of the stack stays on
  `requests` + `BeautifulSoup`.

### Known gap: the historical low latches

`store.maybe_lower_known_low()` lowers a product's recorded low whenever a **confirmed**
delivered price beats it, and never raises it again. That is right for a market that moves
and wrong for a typo: a mistyped freight or price that produces a resolved delivered figure
sets the low permanently, and every later listing then reads as dearer than a price that
never existed. This bit during development: a `$25` freight typed into a persistence test
set the low to `$894`.

There is no heuristic guarding it, deliberately: any rule that rejects "implausible" lows
would also reject the genuine bargain the tool exists to catch. The low is editable in the
product dialog, and `GET /api/price-history/{id}` shows which observation produced it. If a
low looks wrong, check the history row's `source` before believing it.

### Filling a blocked retailer by hand, with a browser

A blocked retailer is filled by a person reading the page in a normal browser and posting
what they saw. Claude-in-Chrome is a convenient way to do that leg, but it is a **manual
research lane, not an adapter**: it needs a live session and a desktop browser, and the
watch runs headless on the server from cron, so it can never be what fills these on a
schedule.

The distinction that matters: a person driving a browser occasionally to read a public
product page is ordinary use. Wiring a *scheduled* job to drive a real browser so it stops
looking like automation is anti-bot evasion, and this project does not do it. That is also
the route that gets an account blocked rather than a price.

The values land through the normal import path, so they are recorded honestly:

```bash
curl -X POST http://SERVER-IP:8477/api/import \
  -H 'Content-Type: application/json' -d '{
    "source": "claude-in-chrome",
    "findings": [{
      "model": "HW-Q930H/XY",
      "retailer": "Harvey Norman",
      "price": 1695,
      "freight": null,
      "stock": "In stock",
      "source_url": "https://www.harveynorman.com.au/...",
      "checked_at": "2026-09-11T12:00:00Z",
      "verification": {"model": true, "price": true}
    }]
  }'
```

That writes state `IMPORTED` with `source = claude-in-chrome`, **not** `LIVE`. The
distinction is the point: `LIVE` means an adapter read the page itself, and a value a
human transcribed should never claim to be that. It also leaves the field unlocked, so if
the retailer ever becomes scrapable the automated run takes it back. Lock it only when the
value is one a scraper could never get right anyway, such as a negotiated price or a
freight figure confirmed at checkout.
