# ByMeVPN — release readiness and audited work (2026-11-07)

## Meaning of “100/100”

100/100 is a **release acceptance goal**, not a quality score that can be
verified from GitHub source or CI alone. No claim of verified 100/100 is made.

This audit covers the repository tree, core payment/VPN/Telegram flows, available
unit tests and workflow logs. It is a static/code-level review, **not** a live
production VPS inspection, penetration test, or a complete manual click-through
of every Telegram screen.

## Modules and risk surfaces

| Surface | Relevant code | Verified in this pass | Release limitation |
| --- | --- | --- | --- |
| Customer lifecycle / start | `handlers/start.py`, `keyboards.py` | Registration-cohort bug fixed; current trial and menu code inspected | Need iOS/Android/Desktop Telegram QA |
| Tariffs and device tiers | `constants.py`, `handlers/buy.py` | Existing tariff/2-5-10 callbacks untouched | Marketing “gift” interpretation vs charged duration needs product decision |
| Checkout persistence | `handlers/buy.py`, `database.py` | Post-restart recovery saves period, devices and renewal key only; real SQLite tests | Open payment-link/session lifecycle requires staging |
| Initial YooKassa payment | `payments.py`, `webhook.py` | Invoice must be durably tracked before URL is exposed; mocked provider regression | Actual provider API and webhook delivery untested live |
| Telegram Stars and Crypto Pay | `handlers/buy.py`, `webhook.py`, `payment_monitor.py` | Existing idempotency and amount checks reviewed | Real invoice/payment/refund E2E remains |
| Auto-renew and cancellation | `autorenew.py`, `handlers/keys.py`, `database.py` | Existing status and renewal paths reviewed | Real SCA, charge retries, failure/reconciliation test needed |
| VPN provisioning and device limits | `xui_client.py`, `subscription.py`, `device_monitor.py` | Existing fail-closed remote delete and tests reviewed | 3x-ui multi-inbound outage/migration drills needed |
| First connection and notifications | `notifications.py`, `database.py` | Fixed double-insert milestone and timed referral opt-in | Real server traffic/online semantics unverified |
| Referrals | `handlers/partner.py`, `referral_system_new.py` | Existing +15/+30 left unchanged, first-paid cohort captured | Measure net contribution after bonus-day cost |
| Channel ads | `handlers/ad_program.py` | Public URL format validated; manual review expected | URL cannot independently prove forwarded-from attribution |
| Admin and growth reporting | `handlers/admin.py`, `growth_metrics.py` | Registration cohorts, verified successful payments and currency split | No margin/cost dataset means no defensible profit report |
| Telegram branding | `bot_style.py`, `bot_assets.py` | Semantic premium IDs and client button tests exist | Live entitlement plus actual rendering unverified |
| Login/API | `api_router.py`, `google_signin.py`, `telegram_auth_storage_compat.py` | Strong JWT configuration, tests/code contracts reviewed | Real OAuth client/server integration and security review needed |
| Data lifecycle and deletion | `api_router.py` | Account-delete route knowingly reports HTTP 501 instead of false success | **Blocker for a complete account-deletion feature** |
| Operations | `main.py`, `config.py`, `background_tasks.py`, `deploy/` | Container/CIs reviewed | No production backup/restore or live load evidence |

## Confirmed defects addressed through GitHub

- PR #60: double recorded `first_connection` defeated one-time logic;
  now one writer and a delayed 24h opt-out-aware invitation with active access.
  Also corrects repeated `/start` acquisitions.
- PR #61: selection lost on restart; now safely persists only plan/device
  selection and renewal identity. Purchases are recalculated on confirmation.
  Marketing report counts actual successful payer cohorts, not failed charges,
  and never sums XTR/RUB into fictional profit.
- This branch: YooKassa returned a customer payment URL even when durable
  `yookassa_pending` storage failed. That prevented the polling monitor from
  reconciling a missing webhook. Now initial **and recurring-method fallback**
  requests fail closed until persistence succeeds; the external provider
  may still have an unpaid invoice, but the customer cannot be directed there.

## Source-level findings not misrepresented as solved

1. **Account deletion:** `api_router.py` currently returns HTTP 501.
   Complete deletion requires explicit billing cancellation, verified removal
   from every configured 3x-ui inbound, protected payment-ledger retention and
   privacy/legal decisions. Do not substitute a success message.
2. **Provider edge cases:** sandbox failures, webhook duplication under
   concurrency, refunds, card authentication and partial provisioning must be
   demonstrated against providers, not only mocked.
3. **UI reality:** Premium Emoji IDs are catalog-grounded but not visually
   verified across Telegram platforms or entitled in production.
4. **Ad provenance:** link shape is not forwarding proof; moderator must
   inspect the forwarded post while Telegram cannot provide access.
5. **Cost economics:** reported gross payment amounts are not net revenue or
   profit; fees, VPS, refunds, marketing spend and the cost of referral bonuses
   need ingestion and cohort accounting.
6. **Production deployment:** GitHub merge is not deployment. Existing
   subscriptions, data and VPS changes require a separate staged rollout,
   backup, smoke tests and an explicit rollback point.
7. **Maintainability:** `database.py`, `api_router.py`, `handlers/admin.py`
   are large. Decomposition should be incremental with tests, not a risky
   rewrite of payments or key lifecycle.
8. **User-facing bonus wording:** price is currently calculated using all
   included months (e.g. 3+1=316 RUB for two devices). Preserve current tariff
   contract, but product owner should confirm whether “gift” accurately
   describes the billing promise.

## Release gates

- [x] PR #60: two GitHub Actions checks passed.
- [x] PR #61: two GitHub Actions checks passed, including real SQLite test.
- [ ] This branch: tests and compile checks must pass before merging.
- [ ] End-to-end tests: Telegram Android/iOS/Desktop, including icon entitlement.
- [ ] Provider sandbox: YooKassa, Stars, Crypto Pay, refunds/retries.
- [ ] Real panel: 3x-ui health, multi-inbound delete, backup/restore.
- [ ] Operational rollback and customer-data reconciliation verified.
- [ ] Attribution and margin dashboard reconciled to external spend.
- [ ] Real conversion/retention measured; no ungrounded “conversion improved” claim.

## Protected commercial contract

No change to duration, total price, monthly labels, promo calculations,
2/5/10-device limits, +15/+30 referral reward rules, webhook identity or
production VPN configuration in this branch.
