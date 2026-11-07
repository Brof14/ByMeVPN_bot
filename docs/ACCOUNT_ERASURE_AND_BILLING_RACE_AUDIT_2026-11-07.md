# ByMeVPN — account erasure and recurring-billing safety audit

**Status:** Implementation is on PR #69 (not deployed); unit/SQLite/mock checks alone do not constitute production acceptance.

## Scope

This work addresses observed gaps: the former admin removal deleted the local user first and only afterwards asked 3x-ui to remove the shared VPN client; the self-service `DELETE /me` returned 501; and renewal/payment-method operations could race with removal or with provisioning by a second bot process.

## Account removal contract

- **Self service:** `DELETE /api/v1/me` requires an authenticated Bearer JWT and the exact header `X-Confirm-Account-Deletion: DELETE`. Missing confirmation returns HTTP 400 without side effects. Administrators cannot self-delete using this endpoint.
- **Admin:** the initial delete button only shows a clear confirmation with paid-term loss warning. A separate danger-style confirmation is required; administrator accounts cannot be deleted this way.
- **Mutual exclusion:** deletion first obtains the per-user SQLite `delivery_leases` lock; another worker holding it blocks deletion. Leases use owner tokens and expiry to permit safe recovery after restarts.
- **Stop charges:** all saved YooKassa recurring payment method identifiers are erased locally and all subscriptions are marked cancelled. Paid key expiry is not modified by this billing-only step.
- **Settlement guard:** removal is deferred if recent pending YooKassa, Crypto, intro-trial or in-flight mobile checkout records indicate a charge may still settle. The recent window is conservative, not a proof of payment finality.
- **Revoke VPN:** `delete_xui_user` must confirm absence across configured 3x-ui inbounds before deleting SQLite keys. A partial/unknown result leaves local keys in place and requires manual reconciliation. Billing remains disabled.
- **Local erasure:** an isolated `BEGIN IMMEDIATE` transaction inserts a minimal account tombstone, deletes credentials, saved VPN links, device registrations and personal marketing/analytics records, and anonymizes the retained `users` row. Financial records and payment-recovery evidence are retained for legitimate accounting or disputes.
- **Session invalidation:** access and refresh JWT verification reject tombstoned accounts. The Telegram profile auto-sync cannot recreate a deleted identity. Legacy authorization adapters create the additive tombstone table if absent.
- **Telegram billing gate:** normal messages and callback flows cannot create new checkouts for tombstoned users. Telegram Stars pre-checkout denies deleted/unverifiable accounts before charging. A late successful Stars update is the only exception to the Telegram gate: it is recorded as requiring manual refund reconciliation and never restores VPN access.
- **Idempotency / retries:** database errors or lease loss never produce a false deletion success; the operator must reconcile panel and database before attempting dependent operations.

## Recurring charge / customer card controls

- The primary renewal worker gets the same per-user SQLite lease as provisioning and deletion.
- It re-reads current due subscriptions after acquiring the lease, verifying subscription ID, key association, expiry, saved method, amount, term, device limit and attempt counter before contacting YooKassa.
- It saves a succeeded/pending provider invoice in `yookassa_pending` **before releasing** the lease, allowing the account deletion guard to observe it.
- An unconfirmed response or failed pending-ledger insert is not treated as fulfilled. The same deterministic YooKassa idempotency key is retained for later reconciliation.
- Customer-initiated payment-method unlink and resume operations also acquire this shared lease. Destructive key removal uses it too; an unavailable lock fails closed with a retry explanation.
- Expired leases and cross-process contention do not prove exactly-once **remote** side effects in arbitrary network partitions: a lease can expire after a successful remote change but before local confirmation. Immutable provisioning intents, ledger reconciliation and a staged 3x-ui test remain necessary.

## Backward compatibility / migration

- Database migrations are additive: `delivery_leases` and `account_tombstones` are created with `IF NOT EXISTS` during `database.init_db()`.
- Back up production SQLite with its WAL using the existing snapshot script before switching code.
- All active application replicas must use the **same** SQLite database path; otherwise SQLite leases do not coordinate processes.
- Install the code and migration on a non-production replica first, then verify the existing Telegram login and the mobile Google auth adapter.
- Client apps must intentionally send the deletion confirmation header; a plain DELETE request receives HTTP 400 and does not remove the account.
- Do not execute deletion against live customers merely to test this implementation.
- No automatic production deployment, provider refunds or 3x-ui inbound changes are part of this PR.

## Code / test evidence

- `database.py`: lease ownership, revalidation guard, cancellation of saved methods, pending checkout guard, tombstones and isolated account erasure.
- `subscription.py`: per-user renewable delivery lease, final ownership check and deterministic provisioning intents.
- `autorenew.py`: just-in-time due revalidation, guarded invoice creation and durable pending tracking.
- `handlers/keys.py`: guarded customer payment-method changes.
- `handlers/admin.py`: confirmed, panel-first administrative erasure.
- `api_router.py`: confirmed self-service erasure and JWT tombstone checks.
- Regression files: `test_delivery_leases.py`, `test_autorenew_uncertain.py`, `test_autorenew_ui_lease.py`, `test_admin_deletion_safety.py`, `test_api_auth_sessions.py`, `test_telegram_auth_storage_compat.py`.

## Unfinished release gates

1. **Live staging:** refund/cancel/pending/timeout/replay cases for YooKassa, Crypto Pay and Stars, with a separate non-customer 3x-ui environment and real packet traffic.
2. **Multi-replica deployment:** verify all processes use one canonical SQLite path; simulate container restart and lease loss during provider calls, DB failures and partial multi-inbound deletion.
3. **Mobile and Telegram client QA:** confirm full account-delete UX, explicit irreversible confirmation, error recovery, light/dark UI and links on Android, iOS and desktop.
4. **Operations:** retained financial records require a documented legal basis, access policy and retention/deletion schedule. Account erasure must be reviewed by the responsible operator/legal advisor for applicable law; **no legal compliance certification is claimed**.
5. **Financial integrity:** record actual payment-provider fees, refunds, hosting costs and partner fraud/abuse limits before claiming positive ROI.
6. **Recovery:** practice WAL-safe backup restore, rollback and reconciliation on a staging database.
7. **Different external writers:** if another service edits 3x-ui or subscriptions directly without this lease, this shared SQLite lock cannot protect those operations.
8. **Legacy safety:** reconcile existing unbound auto-renew records and their saved key associations before billing resumes.

**Release decision:** CI green on the exact head SHA is necessary but not sufficient. Do not describe this change as production-complete or 100/100 without external test evidence.
