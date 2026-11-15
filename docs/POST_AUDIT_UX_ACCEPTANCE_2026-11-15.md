# ByMeVPN — client UX post-audit acceptance (15 November 2026)

Scope: Telegram bot, connection chooser, Android download page and support handoff. The original active-customer menu layout is intentionally preserved.

## Owner exclusions

Do not alter the previously excluded global buttons/banner/caption behavior, secondary home button layout, expired-key history paging, or real-tunnel connection status confirmation. Keep gift-month labels, Stars/RUB pricing, auto-renew default, CryptoBot button symbol and dense tariff labels unchanged.

## Implemented — bot PR #110

- Active user home button is a direct signed HTTPS URL button in the existing layout. The panel-only path also uses a durable revocable signing epoch.
- Encrypted temporary handover tickets expire and tests assert their expiry; permanent per-user links remain separate and revocable.
- SQLite triggers reject creation/reactivation of a second active key; provisioning checks before writing to 3x-ui. Payment and archive-renewal flows block unreconciled duplicate-key accounts.
- Existing paid duplicate keys are NOT silently deleted or shortened; support/operations must reconcile against the payment ledger.
- Unknown platform does not get an unrequested Android app recommendation; contextual support link and copy-button contrast improved.
- Android download page points to the published ByMeVPN Android GitHub release, keeping Happ as a documented fallback.
- App handoff colors/radii aligned with the established web page design tokens.
- Checkout copy shortened without modifying charging defaults. A valid promo can return to the already selected plan. YooKassa has a safe read-only pending-payment status check.
- Device-list error offers retry; after deleting a device, connect the next via direct signed URL.
- Invitation history summarized; advertising reward flow uses three steps and explains the 30-day post retention.
- Subscription/auto-renew date shows Moscow local time; revenue reports use Moscow calendar day/week/month.
- Regression tests added for actual active home, signature, ticket expiry, singleton active keys, provider status, landing page, and calendar.

Support bot PR #4, merged separately, uses a check-mark reaction to confirm delivery, falling back to the original short text receipt when reactions are unsupported.

## Follow-up implementation — PR #111

The first post-audit PR left a cross-provider device upgrade gap. This
follow-up implements YooKassa and CryptoBot payment options **in addition
to the existing Telegram Stars flow**, while keeping the original prices,
1:1 Stars/RUB assumptions, the default auto-renew state, gift-month
language, and the active-customer main menu unchanged.

- Only a single existing active subscription can be upgraded; a fresh,
  server-confirmed device limit/expiry is required before the quote.
- Upgrades use the existing incremental tier cost prorated over the
  remaining paid term. The VPN expiry is never extended by an upgrade.
- A provider invoice is bound durably to the exact customer, key,
  price, limits and order before its URL is displayed.
- Both providers' successful payments use the same safe processing:
  settlement verification, exact currency/amount and metadata,
  account-scoped lease, 3x-ui HWID update and readback,
  atomic billing/account state update, idempotent webhook replay,
  and recovery-required quarantine for ambiguous outcomes.
- For customers, a read-only "Проверить оплату" action explains
  pending/confirmed/terminal states, and never starts another payment.
  External invoices are not silently expired after 15 minutes.
- Existing Stars upgrade processing now observes the same per-account
  delivery lease, and a successful Stars upgrade offers the direct
  signed connection-page URL.
- `scripts/audit_duplicate_subscriptions.py` reports legacy multi-key
  paid-access conflicts on a backup without exposing credentials or
  altering existing paid terms. Existing panel/DB synchronization
  diagnostics redact VPN UUIDs.
- `scripts/predeploy_db_guard.py --db <database> --backup-dir <private-dir>`
  uses SQLite online backup, verifies integrity, rehearses the **current**
  migrations on a disposable clone, and tests the one-active-key
  database triggers. It never writes to the live source.
- A GitHub Chromium job now renders real HTML/JS with synthetic
  subscription data at Android/iOS/desktop sizes, checks horizontal
  overflow, basic platform choice and touch target height, and uploads
  screenshots for manual design review. Screenshots are not proof of
  successful real VPN tunneling.

Important: adding these external billing paths **must not** be interpreted
as successful live provider settlements; the GitHub checks use mocks and
isolated SQLite. No real card charge, CryptoBot asset payment, Telegram
client, VPS deployment or Windows tunnel test is claimed here.

## Mandatory production acceptance before rollout

- [ ] Backup production SQLite, run integrity check, rehearse rollback.
- [ ] Query users with multiple active keys; reconcile **paid** periods with YooKassa, Stars and Crypto records. Never automatically truncate time.
- [ ] Run the predeploy guard on a secure production SQLite backup, check its integrity, migrations and singleton-key triggers.
- [ ] Tap active subscriber's Connect VPN button in Android/iOS/Desktop Telegram: immediate website open, no extra message.
- [ ] Verify expired temporary links fail, personal links survive refresh and rotate on key reissue.
- [ ] Test real VPN tunnels and third-party import for at least Android and desktop; verify the published APK and checksum.
- [ ] Test YooKassa/CryptoBot device upgrades in provider sandbox or controlled accounts, including timeout, duplicate callback, cancellation, refunds, active recurring price, panel readback and restoration of pending billing.
- [ ] Check support swipe replies and reaction receipts.
- [ ] Inspect actual Chromium CI screenshots and real Telegram screenshots on Android/iPhone/Desktop, with large font accessibility and narrow screens. CI checks synthetic layouts, not visual perfection.
- [ ] Measure first successful VPN connection, checkout completion, payment errors and user retention.

## Not proven or not implemented in this PR

VPS deployment, physical-device live tests, actual payment-provider tests, complete redesign, architecture split of database.py, fully integrated support case management, measured customer conversion and CAC/LTV. GitHub CI cannot establish a production 100/100 score.
