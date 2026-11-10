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
- Saved the shared SQLite database using SQLite's online backup API and
  verified the snapshot. Preserved both bot `.env` files, the untracked compose
  override, and the modified runtime overlays under
  `/root/bymevpn-backups/full-rollout-20261110/` on the VPS.
- Fast-forwarded the VPS checkout to GitHub `main` at
  `d5da71bae5532b660c68b179926d881e8720acd7`. Rebuilt the shared bot image
  from that checkout. The APK download on the app page now points to the
  requested v1.0.13 asset.
- Found stale active overlays that bypassed the audited app-link and API code:
  they accepted raw subscription URLs and logged a subscription URL fragment.
  Replaced the primary and secondary `app_links.py`, `api_router.py`, and
  `subscription.py` overlays with the current GitHub versions; updated the
  primary connection-page template, update-site handler, and public app logos.
  Preserved the separate device-admin, worker-shutdown, and XUI log-redaction
  overlays. Applied the audited migration changes to both device-admin database
  overlays without removing their additional admin features.
- Stopped the secondary bot during the shared-database startup migration, then
  recreated the primary and secondary bots sequentially. Both are running
  `healthy` on the same newly built image. Redis, support, ByMeAudio, and VPN
  services were not recreated.
- After startup, SQLite `integrity_check` returned `ok`; both containers had
  zero restart count and no `connection_tls_server` or `webhook_server`
  background-task failures in the observed five-minute window.
- Ran the full standard-library unit suite inside an isolated container from
  the new image: 643 tests passed. The Docker build's runtime-bundle check and
  Python compilation also passed.
- Smoke-checked a generated dummy connection ticket against the active primary
  app: the connection page returned HTTP 200, included ByMeVPN, Happ, INCY,
  v2rayN, Koala Clash, and the v1.0.13 APK link, and set no-store/no-referrer
  headers. The dummy URL was not a customer subscription. `/docs`, `/redoc`,
  and `/openapi.json` each returned 404. Public logo/app asset routes returned
  HTTP 200; `/connect` without a ticket returned an expected HTTP 400.
- Found and fixed the bare `/app` reverse-proxy redirect to the internal,
  unreachable port `9443`. Added exact `/app` and `/app/` routes in the tracked
  `deploy/nginx/bymevpn-app-page.conf`, included that file in the live Nginx
  server, backed up the prior config, passed `nginx -t`, and reloaded Nginx.
  Both paths and the public app page now return HTTP 200 with the v1.0.13 APK
  link; logo and app asset routes also return HTTP 200.

The production stack still intentionally retains its device-admin,
worker-shutdown, and XUI log-redaction release overlays. They are preserved
and are not represented by the just-built base image alone; each active mount
must remain part of future deployments unless its code is merged into the
repository. The untracked compose override and secrets are preserved on the
VPS and were not committed.
