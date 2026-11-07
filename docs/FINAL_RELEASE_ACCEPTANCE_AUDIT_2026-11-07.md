# ByMeVPN — release acceptance audit (2026-11-07)

**Reference commit:** `98ef1c83c5b37655cb824085cc9195ab4d1e00f4` (GitHub `main` after PRs #63–#67).

**Scope and evidence:** Repository tree and critical source paths were inspected through
the connected GitHub API; existing unit, SQLite integration and mocked-provider
regression tests were reviewed. The PR checks and push checks for #63–#67
completed successfully. **This is a static/code-and-CI audit, not a live VPN,
payment-provider, penetration, legal, device-rendering or financial audit.**
No validated 100/100 certification is possible without the external gates below.

## Fixes merged this audit round

| PR | Work | Evidence |
|---|---|---|
| [#63](https://github.com/Brof14/ByMeVPN_bot/pull/63) | Renewal reminder targets exact saved key, one-key connection guide, graceful initial provider errors, Crypto Pay pending ledger fail-closed, promo failures do not block service notices | 432 passing tests and both CI workflows |
| [#64](https://github.com/Brof14/ByMeVPN_bot/pull/64) | 03:00 backups snapshot SQLite using backup API, include committed WAL, integrity check, off-loop ZIP, private temporary snapshot | 435 tests, restore-from-WAL test, both CI workflows |
| [#65](https://github.com/Brof14/ByMeVPN_bot/pull/65) | HTTPS-only credential-bearing app handover; safe fallback on HTTP; Telegram 256-char copy limit | 438 tests, both CI workflows |
| [#66](https://github.com/Brof14/ByMeVPN_bot/pull/66) | Count paid conversion only from successful, positive amount ledger entries, not subscription-created events | 440 tests, SQLite tests, both CI workflows |
| [#67](https://github.com/Brof14/ByMeVPN_bot/pull/67) | Stars pre-checkout validates invoice owner, currency, amount, canonical period, device count, promo bounds before charge | 449 tests, both CI workflows |

## Area-by-area audit

| Area | Code-level implementation | Unverified requirement / blocker |
|---|---|---|
| Menu and customer status | New/active/expired states, single key connect and expiring-key renewal | Real Telegram click-through after deployment |
| Trial onboarding | 3-day cardless trial, guarded duplicate claims, 3x-ui provision | Verify device install, deep links and panel traffic |
| Platform guides | Native Android handover + Happ fallback, per-OS premium emoji, key-aware guide | Own iOS/Windows/Linux release readiness, true 30–60s connection |
| Tariffs | Existing 1, 3+1, 6+2, 12+3 periods and 2/5/10 tiers untouched | Clarify external marketing meaning of “gift” vs paid total |
| Payment creation | YooKassa durable records, Crypto Pay pending ledger, Stars pre-checkout protection | Real gateway sandbox: timeout/replay/SCA/charge/refund |
| Fulfillment | Idempotency and remote provisioning reconciliation tests | Actual 3x-ui multi-inbound outage, partial commit, device revocation |
| Autopay | Separate cancellation path, pre-charge notification | Test stored payment renewal and bank payment decline |
| Retention | Expiry reminders, direct existing-key renewal, optional promo failure isolation | Delivery rates, quiet hours in real timezone, renewal lift |
| Referrals | +15/+30 days; delayed 24h one-time prompt after verified usage; opt-out | Referral economics after bonus-day cost and abuse review |
| Telegram channel ads | Official-forwarding-only policy and moderated application state | Arbitrary URL does **not** prove forward origin; moderator needs provenance |
| Analytics | Registration cohorts and successful positive payments; gross by currency | Refunds/fees/VPS/ad spend absent: **profit not measurable** |
| Admin | Cohort, referral and checkout summary, support/error paths | Real operator usability and financial reconciliation |
| Premium Emoji / styling | Unique semantic icons; platform symbols; one highlighted period | Bot entitlement and light/dark Android/iOS/Desktop visual review |
| Data backup | WAL-safe consistent daily SQLite archive with integrity test | Off-site encrypted retention and staging restoration drill |
| Login / application link | HTTPS gate and safe copy fallback | Query exposure in reverse-proxy logs, introduce one-time handover token |
| OAuth / API | Existing API with security draft [#14](https://github.com/Brof14/ByMeVPN_bot/pull/14) | Draft needs strong JWT_SECRET, staged migration and real client QA |
| Account deletion | PR #69 implements confirmed `DELETE /me` with billing shutdown, panel-first revocation, tombstones and retained payment evidence | P0 staging checks, legal retention policy and client confirmation UX remain unverified; see `ACCOUNT_ERASURE_AND_BILLING_RACE_AUDIT_2026-11-07.md` |
| Operations | GitHub Actions unit and compile quality, Docker/Redis/SQLite backend | Live VPS deployment, TLS, rollback, backup restore, stress/load monitoring |

## Release checklist: do not mark as passed without evidence

- [x] All PR #63–#67 unit/SQLite/mock regression tests passed (449 on #67).
- [x] Both GitHub Actions workflows passed on PR #67 and its merged `main`.
- [x] No price/device/referral contract changes in PRs #63–#67.
- [ ] Confirm production `APP_PUBLIC_URL` uses working HTTPS and reverse-proxy routes `/app/login` and `/app/logo`.
- [ ] Confirm Telegram Premium button entitlement; verify every actual key action, device icon and style on iOS/Android/Desktop, light and dark.
- [ ] Run end-to-end provider payments/refunds, repeat webhooks, duplicate orders, 3DS/SCA, failed provisioning and recovered order.
- [ ] Test 3x-ui multi-inbound lifecycle, partial errors, add/renew/delete and reconciliation on staging.
- [ ] Test and document encrypted backup upload, restore, VPS rollback and blast radius.
- [x] PR #69 implements guarded account deletion, recurring cancellation, tombstones and retained financial evidence **in code**. [ ] Stage multi-inbound deletion, refund/retention policy and client confirmation UX before production acceptance.
- [ ] Review draft #14 security contract and secret-based JWT rotation in staging before merging.
- [ ] Ingest actual provider fees, hosting, acquisition costs, refund outcomes and bonus-day cost before claiming profitability.
- [ ] Observe real acquisition-to-paid, paid renewal and referral cohorts before claiming conversion uplift.

## Non-negotiable product contract

Do not silently alter tariff price, base month count, included bonus months,
2/5/10-device choices, discount formulas, or +15/+30 referral rewards.
The price shown at payment confirmation must match actual charged currency/amount.
No artificial popularity labels or fabricated conversions.

## Conclusion

The codebase is measurably better protected and the checked test suite is green,
but it is **not verified to be 100/100**, nor is the production VPS updated by a
GitHub merge. Close the release checklist through independently recorded
production/staging evidence rather than assigning a speculative numeric score.
