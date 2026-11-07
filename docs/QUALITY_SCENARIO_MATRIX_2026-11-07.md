# ByMeVPN — failure-mode matrix and release gates

**Date:** 2026-11-07. **Scope:** Telegram bot customer UX, multi-inbound VPN deletion, first-connection lifecycle, optional organic recommendations, and regression safety. **Out of scope by explicit owner decision:** ALL tariff prices, total billing amounts, period variants, bonus months and their presentation. No live VPS or payment mutation.

## 1. Scenarios reviewed and implemented

| Customer/system event | Correct outcome | Implementation or test |
|---|---|---|
| Unknown text or command, idle FSM | Preserve original chat; show a one-tap return to main menu | `handlers/fallback.py`, `tests/test_reliability_ux.py` |
| User is entering ad / checkout FSM | Unknown-input fallback must not intercept or erase input | Existing `StateFilter(None)` retained |
| Text-to-text edit works | Reuse same Telegram message | `utils.send_or_edit` unchanged edit-first path |
| Photo-to-text transition | Send new screen *before* removing old | `_replace_text_screen` |
| Telegram send fails | Leave old screen intact; propagate the delivery error | `test_failed_photo_to_text_transition_preserves_original_screen` |
| New Telegram screen sent, old delete fails | New screen remains usable; old one may remain | `_replace_text_screen` best-effort cleanup |
| Expired user, exactly one saved key | Go straight to renewal **for that exact key**, preserve its device limit | `main_menu_expired`, `key_renew:<id>` |
| Expired user, several keys | Choose a subscription before renewing; do not guess | `main_menu_expired`, `my_keys` |
| Expired user, no saved keys | Show purchase chooser, not exhausted trial | `main_menu_expired`, `buy_vpn` |
| Connectivity not confirmed; 3x-ui query unavailable | Defer reminder and do not claim connection or send referral outreach | Existing `_connection_reminder_pass` |
| First connection proven by payload traffic | Record unique milestone, send **at most one** optional referral suggestion | `_record_verified_first_connection`, milestone INSERT |
| First connection proven by panel online state | Same verified milestone without requiring traffic > baseline | `_record_verified_first_connection` |
| Duplicate scheduler run / bot restart | No duplicate referral invitation after unique milestone | `log_analytics_event_once` returns inserted bool |
| User opted out of marketing | Do not send optional referral message | `is_promotional_opted_out` |
| Telegram cannot deliver referral invitation | Do not invalidate successful VPN connection; no retry spam | Growth notification catches provider error |
| User changes marketing preference | Reversible controls and return to main menu | `handlers/marketing.py` |
| Client cannot connect / import config | Direct path to troubleshooting and appropriate guide | `handlers/guide.py` |
| No configured VPN inbound IDs | Fail closed; never declare profile deleted | `xui_client.delete_xui_user` |
| All configured 3x-ui inbounds confirm no user | Report deletion success, including safe retry of already-removed user | `_xui_user_absent_on_all_inbounds` |
| Only some inbounds deleted the user | Report unverified failure, preserve local record, leave renewal stopped | `delete_xui_user` and `cb_key_delete_confirm` |
| Panel readback times out after delete | Fail closed and require reconciliation; no false “deleted” | `_xui_user_absent_on_all_inbounds` |
| Renewal cancellation fails *before* deletion | Do not attempt panel removal | Existing `cancel_auto_renew` guard retained |
| Remote deletion verified, SQLite delete fails/raises | Never resume charges for a deleted VPN; create admin error record, give customer a support button | `_report_unconfirmed_deletion`, regression tests |
| Other saved keys remain, local key deletion fails | Never delete shared panel user; safe to restore that key's billing state | Existing `other_keys` boundary retained |
| User taps destructive action accidentally | Warn that deleting access immediately forfeits remaining time; offer billing-only cancellation | Existing `confirm_delete_kb` retained |

The workflow deliberately distinguishes an **unconfirmed state** from both success and failure. Never silently recreate subscriptions or charged orders when server state is unclear.

## 2. Customer UX rules

- First action: what the customer wants **right now** (connect, renew, get help).
- Keep title + one short explanation; one primary blue CTA; secondary actions underneath.
- Use native Telegram blue for primary, neutral for Back / Help, red only for destructive confirmation.
- Do not log unknown user text, URLs or configuration secrets.
- Successful connectivity must be confirmed by 3x-ui traffic/online status before claiming success.
- Promotional sharing is optional, once per account, and respects opt-out. Referrals retain existing +15/+30 reward policy.
- Refunds, device quotas, renewal charges and all tariff contracts remain unchanged.

## 3. Remaining risks — **not** claimed as solved by this PR

1. **Cross-process deletion coordination:** the database and 3x-ui are separate systems, with no atomic transaction spanning both. A crash between mutations can leave a reconciliation incident. A durable deletion saga / journal and a safe reconciler remain necessary to prove exactly-once behavior across multiple workers.
2. **Panel consistency:** the readback assumes configured inbound client lists represent the authoritative state. A partially failed panel cluster or stale API response still needs real end-to-end validation.
3. **Actual customer UI:** premium custom emojis and keyboard layout depend on Telegram client version and bot entitlement. Inspect on Android, iOS, Desktop.
4. **Connection analytics:** 3x-ui online/traffic is strong evidence, but only live app connection testing can validate perceived success across devices.
5. **Real organic-growth economics:** invitations, first payment and bonus days do not establish net profit or guaranteed self-growth. Require cohort retention, variable traffic costs, payment fees, refunds and abuse monitoring before claiming positive ROI.
6. **Payment provider integration:** YooKassa, CryptoBot, Telegram Stars were not modified; separate test-provider E2E checks are required.
7. **Message delivery:** one-time growth message is best effort. If Telegram was unavailable after milestone storage, it intentionally won't retry to avoid repeated marketing.
8. **Privacy:** ensure actual production telemetry has retention and access controls; this PR does not collect new personal identifiers.

## 4. Test and release evidence

- Added `tests/test_reliability_ux.py`: chat history, safe replacement, all-inbound verification, partial failures, DB failures.
- Added `tests/test_lifecycle_navigation.py`: expired lifecycle, support paths, reversible marketing.
- Added `tests/test_first_connection_growth.py`: confirmed connection, uniqueness, opted-out, failure behavior and real SQLite milestone.
- Existing suite includes payment idempotency, device deletion, referral reward delivery, trial, auth and mobile API contract tests.
- **Gate:** both GitHub Actions checks must be green on the exact PR SHA before merge. The PR should be inspected for unintentional changes to tariff functions and pricing constants.
- **Production:** merge ≠ deploy; don't update VPS, databases or 3x-ui as part of this patch. Deploy with a separate rollback plan after production compatibility checks.

## 5. Work log

1. Read current main and customer, payment-adjacent, referral, connection, VPN deletion and 3x-ui modules before changes.
2. Added failing behavioral tests for chat loss and partial VPN deletion; observed expected red CI prior to fixes.
3. Implemented all-inbound readback instead of accepting the first successful delete.
4. Removed destructive fallback cleanup and raw unknown-message logging; made replacement screens send before delete.
5. Prevented billing resume when remote state is uncertain; logged incidents and added direct customer support.
6. Added customer-specific expired renewal navigation and help shortcuts.
7. Added an opt-out-aware one-time referral suggestion only after verified connectivity; made milestone insert result observable and atomic.
8. Exercised error paths with tests, reviewed final GitHub Actions before merge.
