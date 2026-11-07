# Telegram Stars pre-checkout integrity — 2026-11-07

## Finding

The handler previously answered every `pre_checkout_query` with
`ok=True`. Invalid, stale or cross-account payloads would be noticed
only *after* the customer paid; users could need manual refund/reconciliation.

## Fix

- Verify `XTR`, positive amount, payload structure, owner, allowed device
  tiers, subscription period and bounded days.
- For orders without a promotion, require an exact match against the
  server's canonical price and duration.
- For promotion invoices, allow only lower-or-equal amount and
  longer-or-equal duration, with bounded grant days. Offer validity is
  decided at invoice creation, not retroactively after charge.
- Reject malformed invoices **before Telegram charges the customer**.
- Preserve existing billed tariff amounts, 2/5/10-device tiers and
  promotion calculation rules. No new provider, pricing or discount logic.

## Remaining security/release gate

A fully durable invoice-intent ledger (issued amount, immutable user binding,
provider identity, purchase time and expiry) would support stronger validation
of promotional invoices and recovery across bot restarts. Real Telegram Stars
sandbox/live checkout must be exercised to confirm its pre-checkout response
and fallback UX. `successful_payment` idempotency continues to be covered
separately by regression tests. Do not claim 100/100 from unit tests alone.
