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

## Mandatory production acceptance before rollout

- [ ] Backup production SQLite, run integrity check, rehearse rollback.
- [ ] Query users with multiple active keys; reconcile **paid** periods with YooKassa, Stars and Crypto records. Never automatically truncate time.
- [ ] Verify schema migrations and triggers on a copy of production data.
- [ ] Tap active subscriber's Connect VPN button in Android/iOS/Desktop Telegram: immediate website open, no extra message.
- [ ] Verify expired temporary links fail, personal links survive refresh and rotate on key reissue.
- [ ] Test real VPN tunnels and third-party import for at least Android and desktop; verify the published APK and checksum.
- [ ] Test provider sandbox payments, timeout, duplicate webhook, refunds and recurring cancellation.
- [ ] Check support swipe replies and reaction receipts.
- [ ] Inspect real Android/iPhone/Desktop screenshots including long text, large fonts and narrow screens.
- [ ] Measure first successful VPN connection, checkout completion, payment errors and user retention.

## Not proven or not implemented in this PR

VPS deployment, physical-device live tests, actual payment-provider tests, complete redesign, architecture split of database.py, cross-provider device-upgrade checkout, fully integrated support case management, measured customer conversion and CAC/LTV. GitHub CI cannot establish a production 100/100 score.
