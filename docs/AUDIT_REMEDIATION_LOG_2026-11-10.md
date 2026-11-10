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

Deployment configuration still needs `APP_CONNECT_PAGE_URL` on both bot
instances. For webhook mode, configure the same `TELEGRAM_WEBHOOK_SECRET` in
the runtime and Telegram's `setWebhook`; polling mode should leave it unset.
