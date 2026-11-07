# ByMeVPN audit: onboarding, referral and acquisition metrics

Scope: source review of current `main` plus integration tests. This document
does not certify real Telegram delivery, YooKassa billing or 3x-ui uptime.

## Confirmed defect

The first connection worker called `update_connection_reminder(..., "connected")`,
which inserted an analytics event, then called `log_analytics_event_once()`
with the same `first_connection` event. The second insertion always returned
false; consequently the intended sharing prompt was never sent. Mock-based tests
hid the interaction. The update now changes reminder status only; the verified
worker owns one milestone event. New tests use actual SQLite storage.

## Better customer timing

- First connection is recorded from server-verified traffic or online status,
  never guessed from a button click.
- Referral invitations wait at least 24 hours after that milestone, are offered
  only within 14 days to accounts with active VPN access, and respect explicit
  marketing opt-out.
- The optional invitation is at-most-once and sent in the 10:00-20:00 Moscow
  window. A network-ambiguous Telegram failure is **not retried** to avoid spam.
- Reward rules and partner referral payments are unchanged.

## Cohort reporting correction

The marketing funnel previously treated every recent `/start` as a newly
acquired user. Existing customers revisiting the bot inflated the cohort.
The reporting baseline is now `users.created` (registration date), while
later paid/trial/connection milestones must occur after that date. This is a
registration cohort, not an absolute count of installations or successful payments.

## Remaining blockers before a defensible 100/100

1. Real Telegram visual QA (Android/iOS/Desktop), Premium Emoji entitlement
   and appearance in dark/light modes.
2. Provider sandbox/live reconciliation: payment replays, partial provisioning,
   charge disputes, SCA, renewal, cancellation, refunds and device changes.
3. 3x-ui migration/recovery drills, including panel outages and partial deletion.
4. Robust checkout restoration after process restarts; current MemoryStorage
   loses pending selection and safely makes customers reselect instead of
   charging an assumed amount.
5. Promo economics and margin verification: existing renewal reminders can
   distribute shared 30% discount codes; their effect on LTV is unknown.
6. Better analytics: data on verified payment conversion, provider fees,
   refunds, server costs, incremental lift and retention by source.
7. Telegram forwarding proof is not available from an arbitrary post URL;
   advertising claims require moderation.
8. Unimplemented account deletion and transaction-safe API lifecycle,
   documented in existing open issues/draft security PR.

No changes to tariffs, 2/5/10 tiers, included months, discount formulas,
provider contracts, referral rewards or production VPS were made here.
