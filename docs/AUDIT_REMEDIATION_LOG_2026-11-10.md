# ByMeVPN audit remediation log

Scope: source changes based on the supplied audit. Audit items 1 and 9 were
explicitly excluded and were not changed.

## Actions

- Secured `/webhook/telegram` with Telegram's secret-token header, rejected
  missing or incorrect secrets, bounded request bodies, rejected malformed
  updates, and returned retryable errors when dispatch fails. The route stays
  disabled until its secret is configured.
- Changed newly generated app and connection handovers to use encrypted tickets.
  Removed acceptance of legacy raw subscription URLs on `/app/login`; old links
  now ask the user to reopen the connection flow from the bot.
- Removed subscription URL fragments from application logs.
- Made the connection chooser require an explicit HTTPS origin instead of
  silently putting the raw subscription URL in a button. Added the variable to
  `.env.example` and corrected the documentation to the implemented seven-day
  ticket lifetime.
- Removed green emphasis from the three-month tariff, preserved explicit
  neutral button styles, and aligned visual regression checks with those rules.
- Made 3x-ui inbound IDs configurable, validated the value, and set the
  documented default to 13, 14, and 40.
- Replaced broad “ignore any error” schema-migration behavior with an
  idempotent column migration helper that reports unexpected SQLite errors.
- Added webhook, handover, migration, and inbound configuration tests. Fixed an
  async test class that previously caused its test body not to run.
- Added `pip-audit` to the quality workflow. A local scan of the pinned runtime
  dependencies found no known advisories on 2026-11-10.
- Reviewed the existing connection page against the supplied Hallmark floor:
  brand tokens, mobile sizing, focus states, reduced-motion behavior, and
  non-emoji app/OS icons are already present. No visual redesign was needed for
  the verified audit fixes, so the existing page composition was preserved.

## Not changed

- Audit items 1 and 9: explicitly excluded by the user.
- Existing back-button red styling: it matches the user's prior instruction.
- Item 8 recurring discounts: the checkout explicitly displays the renewal
  duration and amount, and the recurring record stores those same terms. No
  change was made to existing payment terms.
- Lifecycle marketing and device upsells: the audit provides no measured
  conversion, consent cadence, or approved prorated upgrade pricing. Adding
  messages or charges without those inputs could spam users or change prices.
- Support context: the support bot is a separate project; forwarding user,
  payment, or device context needs an integration review and must not include
  subscription secrets.
- Broad module decomposition and SQLite connection ownership: no failing
  concurrency case was reproduced. A large structural rewrite would exceed the
  verified fixes and increase regression risk.
- Docker non-root conversion and encrypted backups: production bind-mounted
  database permissions and a separately managed recovery key are prerequisites.
  Neither is safely established by this source-only change.
- Load and payment-to-VPN end-to-end tests: they require isolated payment and
  3x-ui environments; the production services were not used for test traffic.

## VPS follow-up

- Verified the SSH host key from the VPS console screenshot and connected only
  to `2.27.16.7` with `StrictHostKeyChecking=yes`. The older address shown in
  the console was not used.
- Confirmed `APP_CONNECT_PAGE_URL` is configured for both bot containers. A
  dummy connection ticket round-tripped through the encrypted ticket reader
  for `https://bymevpn.duckdns.org/connect`; no real subscription URL was used.
- Confirmed both containers use inbound IDs `13,14,40` and both FastAPI apps
  disable `/docs`, `/redoc`, and `/openapi.json`.
- The active production compose stack mounts older webhook implementations
  from separate release overlays. Added the same fail-closed Telegram webhook
  guard, 1 MiB body bound, and legacy `/app/login?sub=...` rejection to the two
  active overlay files. Backups are stored under
  `/root/bymevpn-backups/audit-remediation-20261110/` on the VPS.
- Restarted only `vpnbot` and `bymevpn-newbot`, one at a time. Both returned to
  `healthy`; Redis, support, and ByMeAudio containers stayed running.
- Both bots run polling. `TELEGRAM_WEBHOOK_SECRET` remains unset, so the public
  Telegram webhook endpoint fails closed with HTTP 503. If switching to
  webhook mode later, configure a secret in the runtime and Telegram's
  `setWebhook` together.

The production base checkout is still at `17c7a93`; its active compose stack
layers many release overlays and includes an untracked compose override.
The complete base image was not rebuilt from `495ab988ddeb24e7cbcd3cffd655d2ce68872831`:
doing so without reconciling those overlays could replace live bot features or
alter startup database migrations. The runtime security fixes above were
applied narrowly to the mounted files and verified by container health. A
full source-to-runtime reconciliation remains pending.
