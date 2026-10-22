# Household watchlist acceptance

Checked on the local WSL host on 22 October 2026. This records bounded checks,
not a claim that unattended production operation is enabled. Parent spec: #1.

## Verified

- Baseline automated checks: 680 tests passed; Ruff passed. The suite includes
  HTTP/rendered flows, migrations, snapshots, location quote invalidation, scheduler
  recovery, alerts, purchases, archive suppression and failed-provider isolation.
- A disposable loopback server and database accepted a real model-suggestion HTTP
  job as `QUEUED`. The host runner completed it as `DONE` using the existing
  CLIProxyAPI `pi-luna` OAuth alias, returning `HW-Q930H/XY` and its
  [Samsung Australia source](https://www.samsung.com/au/audio-devices/soundbar/q930h-black-hw-q930h-xy/).
  The runner requires completed search evidence and rejects incomplete responses.
  No paid route shares this model/alias in the inspected proxy configuration.
  The temporary server/database were removed; the household database was untouched.
- Location HTTP/pricing checks: 169 tests passed. The default radius is 25 km;
  changes preserve history and invalidate quotes for the previous postcode. Unknown
  freight and pickup availability remain unresolved. Pickup distance/stock are
  manual where the retailer supplies no location-specific data.
- Marketplace/intake/watcher checks: 31 tests passed. Distinct marketplace sellers
  remain distinct; unsafe URLs and wrong-model results are rejected. Failed checks
  retain last good values, provenance and manual locks.
- Native WSL app, research-runner, scheduler service and timer units passed
  `systemd-analyze --user verify`. This validates unit configuration, not execution.

## Marketplace capability

Amazon and Temu links remain manual; no supported automatic connection is configured
or demonstrated here. Checking the official documentation on this date found:

- [Amazon Creators API](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction)
  has Associates enrollment, qualifying-sales and credential prerequisites.
  [Amazon's usage policies](https://affiliate-program.amazon.com/help/operating/policies)
  also restrict price tracking and alerts without Amazon's agreement. Documentation
  does not demonstrate authorized seller-specific price extraction for this household.
- The [Temu Partner Platform](https://partner.temu.com/documentation?menu_code=38e79b35d2cb463d85619c1c786dd303)
  describes partner applications and seller authorization, rather than establishing
  a general consumer-offer API for arbitrary links.

Saving a URL does not establish automatic coverage. Use the listing's manual update
controls; do not bypass access restrictions. Supported adapters remain independent
of the AI connection, but their current live retailer coverage was not remeasured.

## Remaining operational gates (#14)

No Shopwatch app, research-runner or scheduler units are installed/enabled on this
WSL host. `~/.config/shopwatch/research.env` is absent. The existing proxy listener
alone does not run Shopwatch's queues. README documents installation, retry, pause,
logs and the native scheduler; research credentials belong only in `research.env`.

Before enabling background work, identify and configure the host's existing outbound
monitoring process and household ntfy destination/token. Verify a representative
retailer refresh, real due run and one catch-up, ntfy plus in-app history, pause/archive
suppression, post-purchase watching and graceful unavailable-history/session handling.
Keep these as bounded checks. Do not disable monitoring or use old opti retailer
measurements as proof of current coverage. WSL/user-manager uptime is also required.
