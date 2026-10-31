# Mobile V2 rollout on the legacy bind-mounted API

This is a deployment runbook for the older production runtime. The current
repository's `api_router.py` is **not** a drop-in replacement for the mounted
production file. Integrate the versioned V2 routes as an additive router and
keep the existing bot routes and payment callbacks intact.

## Runtime compatibility boundary

The V2 implementation in this branch expects the matching database checkout
helpers, provider idempotency/payload support, and webhook fulfillment checks.
Do not copy only the route declarations into the old runtime: that would leave
the old client-priced checkout handler and incomplete invoice persistence in
the call path. The focused runtime change set is:

1. Add the `/mobile/v2/plans`, `/mobile/v2/payment/create`, and
   `/mobile/v2/payment/check` router to the mounted API router.
2. Add the `mobile_payment_requests` schema/bootstrap and owner-scoped request
   helpers from `database.py`.
3. Add the idempotent YooKassa request and Crypto Pay payload reconciliation
   support from `payments.py`, without changing the legacy function signatures
   used by Telegram handlers.
4. Add the mobile-order verification guards from `webhook.py` before either
   provider's fulfillment path can call subscription provisioning. Every new
   invoice created through the JWT-authenticated `/payment/create` API is
   marked and checked, including pre-V2 mobile clients; invoices created
   before this marker and Telegram bot invoices keep their existing path.
5. Retain the existing HWID path: device limit is written to `limitHwid`,
   `limitIp` stays `0`, and current HWID checks remain enabled.

Do not enable the purchase UI unless all five parts are present. Keep Google
Sign-In and real provider checkout out of scope for this change.

## Pre-deploy gate

1. Verify the VPS SSH host key against a fingerprint confirmed by the owner.
   Stop if it differs; do not bypass host-key checking.
2. Read the mounted source paths, container working directory, active DB path,
   and running service state. Do not dump environment values or database rows.
3. Create a fresh SQLite online backup with SQLite's backup API. Restore it to
   a new temporary DB, run `PRAGMA integrity_check`, compare schema objects,
   and run the additive migration only against that restored DB.
4. On the restored DB, run the backend suite and HTTP tests with mocked
   providers. Verify replayed `request_id`, foreign invoice rejection, pending
   status, final webhook confirmation, duplicate delivery, and tariff terms.
5. Before traffic is enabled, require a test YooKassa shop, a Crypto Pay test
   token/account, and an isolated 3x-ui panel/client. Mock tests do not replace
   these checks.
6. Save checksums and protected copies of only the runtime files being edited.
   Prepare the reverse patch and verify that it applies cleanly before rollout.

## Controlled rollout and rollback

1. Stage the additive router and related helper changes beside the legacy API;
   preserve all old bot routes and the current file as the rollback copy.
2. Apply the schema bootstrap to the restored DB first. Compare its table list,
   column definitions, and aggregate row counts with the source backup.
3. Run the isolated HTTP/provider-mock suite and test-panel limit checks. Do not
   create a live provider invoice.
4. Only after the owner has approved the rollout window, install the focused
   runtime patch, restart `vpnbot` once, then check health, the three V2 routes,
   legacy bot checkout, payment confirmation, and `limitHwid`/`limitIp` values.
5. If health or contract checks fail, restore the saved runtime files and
   restart once. Keep the additive payment-request table: it is not safe to
   remove it while any invoice created by V2 may still be pending. Reconcile
   those invoices through the verified payment handler before any later schema
   cleanup.

## Current release status

PR #22 contains code and mock-backed tests on top of merged PR #21. This
runbook does not claim the old runtime has been patched. Read-only inspection
confirmed that the active bind-mounted API has only legacy `/payment/create`
and `/payment/check` routes. It trusts client-supplied price and duration; the
active provider helpers lack stable YooKassa idempotency/response details and
Crypto Pay opaque-payload support; the active webhook has no owner-scoped
mobile-order guard. The active DB initially had no `mobile_payment_requests`
table. Therefore PR #22 is not a drop-in patch for the running image.

A fresh online backup was created with Python SQLite's backup API while WAL
was active, then restored separately and compared. Both integrity checks
returned `ok`; `schema_version` was 1 and `user_version` was 0 before
migration. Migration was applied only to a local restored copy. It added the
three mobile/JWT tables and `crypto_pending.payload`; existing table row
counts and columns were unchanged. `schema_version` became 8 as SQLite
recorded DDL changes; `user_version` remained 0. The exact older runtime's DB
initializer was also run against another copy of the migrated snapshot and
kept the additive objects intact, confirming DB-level rollback compatibility.

HTTP route, migration, request replay, owner, pending-payment, webhook-order,
subscription-delivery, and mocked HWID/IP-limit checks passed locally on the
isolated copy (18 focused tests). The full backend suite passed in GitHub
Actions. Provider and 3x-ui tests are mocks only. No real payment or live
panel check was performed.

The host key was verified strictly against the fingerprint confirmed by the
owner, and the authorized SSH session succeeded. Production API files, active
DB, container, and service were not changed or restarted; only protected
backup/restore files were created. The host checkout has pre-existing
uncommitted changes, so deployment must use a separately staged additive
patch and must not reset or overwrite that checkout. A runtime-specific patch
is still required before rollout; do not copy PR #22 files wholesale.
