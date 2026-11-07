# ByMeVPN: renewal, checkout and connection audit (2026-11-07)

## Verified source-level findings and corrections

- Expiry reminders previously pointed to `buy_vpn` even for an existing, expiring key. This started a generic purchase rather than that key's renewal. Expiry rows now include the actual key ID; reminders use `key_renew:<id>`. If a legacy caller supplies no key ID, the old generic path remains supported.
- Users with exactly one active key who open the general connection guide now receive a key-specific guide, reducing navigation to manual subscription import. Multiple active keys are not guessed.
- An optional 30% renewal promotion previously blocked the entire service notice on promo storage/validation error. Promo failures now fail open for the **service reminder only**, without inventing a discount.
- Provider errors from initial Telegram YooKassa/Crypto Pay checkout now show a recoverable customer message rather than propagating a raw exception.
- Crypto Pay payment URLs are not handed out unless the pending invoice record is persisted, so the reconciliation monitor can track the invoice.

## Commercial parameters preserved

No changes to prices, 1/3+1/6+2/12+3-month tariff labels, total amounts, 2/5/10-device tiers, referral +15/+30 days, or existing promo values/formulas.

## Full-repository risk inventory (not a claim of all bugs fixed)

| Surface | Principal files | Outstanding external or design gate |
| --- | --- | --- |
| Telegram lifecycle & UI | handlers/start.py, handlers/guide.py, keyboards.py, bot_style.py | Android/iOS/Desktop visual QA, actual premium-icon entitlement |
| Payments | handlers/buy.py, payments.py, webhook.py, payment_monitor.py | YooKassa/Crypto Pay/Stars sandbox, refunds, replays, ambiguous timeouts |
| Subscription lifecycle | subscription.py, autorenew.py, handlers/keys.py | Staging multi-inbound deletion/recovery; cancellation and migrations |
| VPN panels & devices | xui_client.py, device_monitor.py | Live 3x-ui faults, actual first-connection telemetry |
| Referral, ads, retention | referral_system_new.py, handlers/partner.py, handlers/ad_program.py, notifications.py | Referral cohort economics and moderator-verified forwarded provenance |
| Reporting/admin | growth_metrics.py, handlers/admin.py, database.py | No attributable margin without actual VPS/provider/advertising costs |
| Mobile/API/OAuth | api_router.py, google_signin.py, webhook.py | Open draft PR #14 requires explicit security/deployment review |
| Privacy/data lifecycle | api_router.py, database.py | Account deletion currently reports 501; pending transaction-safe design |
| Operations | main.py, Dockerfile, scripts/backup_db.py | Verified online backups, restore drill, rollback and monitoring |

**No source-only review can certify a live 100/100.** See `docs/RELEASE_READINESS_AUDIT_2026-11-07.md` for the full acceptance gates.
