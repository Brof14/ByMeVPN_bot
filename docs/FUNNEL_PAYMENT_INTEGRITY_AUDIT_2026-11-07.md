# Source-of-truth correction: paid customers vs granted VPN access

## Audited defect
The older `get_conversion_funnel()` reported a user as **paid** when
an analytics event named `subscription_created` existed. A created VPN key
is not proof of a settled positive-value provider payment. The separate
checkout funnel used successful payment ledger entries, but could also count
a zero-amount record as a buyer.

## Correction
- Count distinct paid users through `payments.status='success'` **and**
  `payments.amount>0` for the corresponding registration cohort.
- Apply the same positive-payment guard to the currency-separated checkout
  conversion report and aggregate.
- Count renewal milestones only for positive-amount events.
- Verify in a real SQLite test that free activations, failed invoices and
  zero-valued success records never inflate paid conversion.
- Do not alter prices, payment creation, granting, referral rules or app APIs.

## Remaining caveats
- A settlement may later be refunded or charged back; the ledger must reflect
  that before this is a final net revenue statement.
- Currency aggregates are gross only; provider commissions, marketing,
  referral days and hosting costs are not accounted for.
- Source attribution requires valid campaign parameters and verified user
  cohort dates. No statistical uplift is claimed.
