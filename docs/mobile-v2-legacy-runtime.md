# Mobile API V2 adapter for the legacy runtime

This is an additive compatibility package for the old bind-mounted API. It does
not replace `api_router.py`, change the existing Telegram payment contract, or
alter existing tables other than adding `mobile_v2_orders`.

## Routes

- `GET /api/v1/mobile/v2/plans` returns the server-owned 2/5/10-device catalog
  and the current Android V2 fields.
- `POST /api/v1/mobile/v2/payment/create` accepts only `plan_code`,
  `request_id`, and `provider`; prices, days, and device limits come from the
  server catalog.
- `GET /api/v1/mobile/v2/payment/check` is owner-scoped and reports fulfillment
  only after the legacy webhook marks delivery complete, using the Android
  response fields `status`, `is_active`, and `subscription`. Provider-side
  `paid` alone never activates access.

## Required integration before enabling payment

1. Place `mobile_v2_legacy.py` next to the legacy API modules and run
   `migrations/20261101_mobile_v2_legacy.sql` against the restored/staging DB.
   It is safe to run repeatedly. The route also ensures the additive table on
   first use for deployments without a startup migration hook.
2. At the bottom of the old `api_router.py`, include
   `api_router.include_router(create_router(), prefix="/mobile/v2")` after its
   auth dependency is defined. Do not replace the file.
3. In YooKassa webhook processing, after fetching the payment from YooKassa and
   confirming `status == "succeeded"`, call
   `validate_yookassa_mobile_payment(payment)` before interpreting metadata or
   calling `deliver_key`. Abort processing if it returns false. After
   `deliver_key` succeeds and the payment ledger is marked successful, call
   `mark_mobile_order_fulfilled("yookassa", payment_id)`.
4. In Crypto Pay processing, after confirming the fetched invoice has
   `status == "paid"` and before reading `crypto_pending` or calling
   `deliver_key`, call `validate_crypto_mobile_invoice(invoice)`. Abort if false.
   After successful delivery and ledger update call
   `mark_mobile_order_fulfilled("crypto", invoice_id)`.
5. These guards pass Telegram invoices through unchanged when there is no
   matching mobile order. If a mobile marker/order is missing or mismatched,
   fulfillment is blocked. The existing provider final-status checks and
   payment-ledger idempotency remain in force.
6. Keep `MOBILE_V2_WEBHOOK_GUARD_ENABLED` unset while integrating/testing. Set
   it to `1` only after the actual old webhook contains and passes both guards
   and the post-delivery finalizer. Without the flag, catalog works but creating
   a payment returns 503.

The old API maps its `devices` argument through the existing key provisioning
path; its Xray client maps that value to `limitHwid` while keeping `limitIp=0`.
No change to IP semantics or HWID checks is part of this adapter.

## Retry and failure behavior

The per-user `request_id` is unique. An identical completed creation returns
the same invoice; reuse with another plan/provider is rejected. A request left
in `creating` without a linked invoice fails closed instead of making another
invoice. YooKassa uses a deterministic provider idempotency key. Crypto Pay uses
an opaque deterministic payload, but because the legacy Crypto API has no
create-idempotency contract, uncertain creation is never retried automatically.
Such an order requires provider reconciliation before retry.

This package cannot prove a live YooKassa/Crypto Pay account or 3x-ui panel.
Keep payment creation disabled until the isolated provider shop and panel
checks in the rollout plan succeed.
