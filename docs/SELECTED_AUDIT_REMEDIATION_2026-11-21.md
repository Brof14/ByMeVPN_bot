# ByMeVPN — selected audit fixes (21 November 2026)

This change set deliberately **does not modify** promo codes, tariffs, payment methods, provider charge handlers, recurring billing, or referral grant amounts. It does not claim a VPS deployment or real-device test.

## Scope and API contracts

| Audit | Change | Compatibility |
| --- | --- | --- |
| P1.1 | Replaced fabricated node `ping_ms` and `load_percent` with JSON `null`, plus `metrics_status=not_measured` | Keys remain present; Android's parser treats unknown load as -1 and measures ping on-device. Verify Windows/other clients on staging before rollout |
| P1.2 | Parse explicit flag, full country name or standalone ISO code; do not invent cities | Stable `country`, `country_code`, `city`, `flag` fields. Unknown location has empty code/city and 🌐 |
| P1.3 | Added operator-token-protected `GET /provisionz` for 3x-ui provisioning readiness | `/readyz` and `/healthz` still check local dependencies only; billing was not changed |
| P1.4 | Add `expires_at_utc` (ISO 8601 with offset) and `expires_at_unix` (UTC epoch seconds) | Preserve the old local `expires_at` string for existing clients; new clients should prefer new fields |
| P1.5 | On temporary subscription-config failure, preserve structured code and add HTTP `Retry-After: 15` | No caching of VPN credentials or false access grants |
| P1.7 | Manual review signal for small first **RUB** payments to referred users | +15/+30 referral bonuses, billing and automatic grant logic unchanged; Stars are never compared to RUB |
| P1.9/P1.11 | Admin-only `Экономика · 30 дней`: independent cohort milestones, gross revenue by currency, recorded refunds and reported costs | No synthetic net profit, CAC/LTV estimate or currency conversion. Expenses from explicit environment inputs only |
| P1.12 | More useful `О ByMeVPN`: what it does, supported apps and connection setup | No new flow, no AI-style marketing paragraphs |
| P1.13 | Select upgrade target 5/10 first, then existing provider selection screen | All existing Stars, YooKassa and CryptoBot payment methods/callbacks remain |
| P1.17 | Document the required 32+ byte `JWT_SECRET` and optional cost inputs | No credentials or payment settings changed by default |
| P1.19 | Added offline SQLite restore rehearsal script | No access to live VPS or external backups; real drill remains required |
| P2.22 | Updated code/documentation module references and rollout steps | No production-state changes |

**Out of scope:** P1.10 promo UX, payment providers, rates, subscription durations, and migrations to PostgreSQL.

## Staging and deployment checklist

1. Compare the deployed immutable Git SHA/image digest with this reviewed PR. Keep the previous image for rollback. Do not replace persistent SQLite, Redis, or 3x-ui volumes.
2. Run the full Python suite and quality gate. Check new field parsing with **actual Android and Windows clients**; do not deploy to real clients if either application crashes when ping/load is null or city/code is empty.
3. Verify `/readyz` still serves local readiness. Set a random 32+ character `BYMEVPN_MONITOR_TOKEN` on VPS and probe `/provisionz` with header `X-ByMeVPN-Monitor-Token: <token>`; without an authenticated token it returns 404. With a valid token it reports 200 when configured provisioning responds, otherwise 503 with no sensitive detail. Monitor the endpoint at a conservative cadence (e.g. once per minute), not on every user request.
4. Test account with active subscription, expired account, blocked device, invalid HWID and 3x-ui outage. Failed configuration fetch must not return invented servers or credentials; `/api/v1/servers` should send stable `X-Error-Code`, correlation ID and short retry hint.
5. Test dates around UTC midnight; clients should display `expires_at_utc` or `expires_at_unix` using their own timezone. Legacy `expires_at` remains for old clients.
6. Verify device upgrades 2→5, 2→10 and 5→10: target selection appears before the **unchanged** provider chooser; charged amounts and subscription expiry stay unchanged.
7. Check admin analytics for RUB and XTR separately. Populate `BYMEVPN_COSTS_30D_*` only from verified last-30-day receipts (servers, advertising, staff, commissions and other). Blank means unknown; do not equate the partial RUB balance with net profit.
8. Existing referral rewards must remain exactly +15 days for registration and +30 days for a first qualifying payment. Risk signals are advisory only.
9. Verify customer-facing "О сервисе" in actual Telegram Android/iOS/Desktop, including larger text sizes. Ensure app availability claims are still current.
10. Run a real VPS restoration exercise on a **separate staging machine**, not against the live payment database.

## Offline SQLite restoration drill

Create a read-only/point-in-time source snapshot using `scripts/predeploy_db_guard.py`, then transfer the verified snapshot to an access-controlled staging system. Run:

```bash
python scripts/verify_backup_restore.py --snapshot /secure/staging/verified.sqlite
```

The script opens the file read-only, creates a disposable restored clone via the SQLite online backup API, verifies `PRAGMA integrity_check`, verifies the users/keys/payments/referrals tables and compares row counts after reopening the clone. It does **not** modify the supplied snapshot.

A passed drill **does not** prove disaster recovery: the operator must independently reconcile the 3x-ui panel, restored configuration, pending payments/renewals, secure key material, and payments created after the backup. Do not restore a historic snapshot over newer settled payments. Document actual RPO, RTO, backup storage, encryption/permissions, dates and the rollback owner.

## Red CI on starting main

The base commit already exhibited failures in stale tests for the previous tariff/promo UI. These are not caused by the selected fixes. Any changes to old tests must not change payment methods, promos or billing logic; compare against the base failures before merging.

## Operational limits

- The new `/provisionz` probe checks the configured panel API/inbounds, **not** external VPN throughput or end-user connectivity.
- Telegram client UI cannot be exhaustively tested by GitHub Actions.
- The admin economics report is read-only and uses only declared, unconverted currencies; recorded refunds may not reflect provider-side reversals until reconciled.
- No VPS changes, database rollback, live restoration or real-money payment testing are executed by this repository PR.
