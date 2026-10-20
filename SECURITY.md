# Security

Shopwatch is built as a private, single-user application: one deployment, one person,
no accounts. The author's own deployment runs on a home network behind `basic_auth`
and is not reachable from the internet - the same is recommended for anyone else
running their own copy.

## Reporting

Raise a GitHub issue on this repository. There is no bug bounty and no coordinated
disclosure process.

## What this application holds

Worth knowing before deciding how sensitive a given bug is:

- Purchase records, including what was paid, when, from whom, and order references.
- Retailer page URLs and research notes.
- Offers read out of personal email, including the sending address and subject.

It holds no payment details, no card numbers and no retailer account credentials.

## Where secrets live

- **Local development:** a gitignored `.env`. `.gitignore` has covered `.env*` since the
  first commit.
- **CI:** GitHub Actions secrets. The workflows in this repo currently need none.
- **Deployed on opti:** `/srv/prod/shopwatch/.env`, mode 0600, root-owned, never in git.
  **Rendered, never hand-written**, from the SOPS vault at
  `smart-home:opti-stacks/shopwatch/secrets.sops.env` via `sops-render.sh shopwatch`.
  The age private key lives only on opti. It carries the ntfy topic URL and publish
  token and nothing else; non-secret tuning sits in the compose file, in git, where it
  can be reviewed.

- **The mail watcher** reuses credentials that already exist in
  `/srv/prod/career/runner.env` on opti: the iCloud app password and the Claude Code
  OAuth token. It stores no credential of its own, and the local Thunderbird path needs
  none at all.

- **The product wizard's research runner** (`deploy/research-runner.py`) reuses the same
  `/srv/prod/career/runner.env` Claude Code OAuth token as the mail watcher, for the
  same reason: nothing new to store or rotate. It runs as its own host-level script,
  polled by `shopwatch-research-runner.timer` every 2 minutes, never inside the
  `shopwatch` container. The web container never holds this CLI credential.

- **The wizard's model suggestions and retailer discovery** use that same host research
  runner and subscription OAuth token. Server-side Claude invocations disable API-key
  and cloud-provider billing overrides and use an isolated CLI configuration. The
  optional standalone helper can still use its operator's own CLI login, but normal
  GUI use does not require it. See the README's "Product and retailer research" section.

- **Wayback history** needs no credential. The web app makes bounded HTTPS requests to
  fixed `web.archive.org` endpoints, rejects redirects and private destinations, and
  treats captured prices as sourced advertised-price evidence rather than confirmed
  delivered prices. Retailer fetching likewise validates each destination and redirect.

## If this is an active incident

Rotate in this order: the `basic_auth` hash (`CADDY_HASH_SHOPWATCH` in the caddy SOPS
vault), then the iCloud app password, then the Claude Code OAuth token. Take a database
backup first (`POST /api/backup`); price history is append-only and worth keeping.
