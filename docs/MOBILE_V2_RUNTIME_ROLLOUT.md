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
   provider's fulfillment path can call subscription provisioning.
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
runbook does not claim the old runtime has been patched. As of this review, a
fresh SSH inspection was blocked because the host key presented by the server
did not match the previously owner-supplied fingerprint. No SSH trust setting
was weakened, and no production file, DB, container, or service was changed.
