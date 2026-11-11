# ByMeVPN audit remediation status — 2026-11-11

This change set implements the actionable source-level findings from the supplied
audit, excluding the two topics the owner waived: Telegram payment-policy risk
and price transparency/tariff presentation (including section 3). The channel
bonus of +10 days is retained; the owner confirms the bot already administers
the channel.

## Implemented in PR #89

- Put the active subscriber's connection action first; show subscription expiry
  and device allowance; separate renewal from connecting; improve device labels,
  deletion consequences, diagnostics, and help fallbacks.
- Apply a visible Telegram button style everywhere, with red reserved for
  destructive operations, green for reward claims, and blue for other actions.
- Simplify the connection chooser, collapse alternate clients, prioritize the
  guide, use consistent Linux recommendations, clarify handoff and recovery,
  and reduce new connection tickets to one hour.
- Route Android downloads through the manifest-backed page. The manifest is
  validated against the APK's checksum and size and may provide release date,
  minimum Android version, and signing-certificate fingerprint. The no-JavaScript
  connection fallback now opens that same page instead of a hard-coded APK.
- Replace unsupported prefilled support deep links with direct support links.
- Deliver eligible 100% promo orders automatically and idempotently.
- Add a one-time +10 day channel reward with membership verification and a
  SQLite claim ledger. Channel membership is checked through Telegram; an
  already-awarded reward is not revoked when the user later unsubscribes.
- Bind YooKassa renewals to the selected subscription through checkout metadata;
  validate ownership and use that same key for delivery and recurring billing.
  Ambiguous legacy checkouts do not attach a saved payment method to an
  arbitrary key.
- Make recurring billing an explicit YooKassa choice. One-time checkout does
  not request or store a payment method; recurring charges are stored only when
  consent is present in verified payment metadata. Mobile API checkouts also
  default to one-time unless their caller explicitly opts in.
- Remove the remaining claim that an app handoff automatically connects the
  VPN tunnel; the app must confirm the subscription and connection.

## Operational verification still required

Source changes and CI cannot establish that production runs the same commit or
that traffic passes through the VPN. Before treating deployment as complete,
verify the following against the actual release:

1. Reconcile the production image, bind mounts, compose override, and runtime
   overlays with the exact Git commit; preserve required production-only
   features in tracked source before removing their overlays.
2. Publish a real signed Android release manifest and verify its version,
   download link, checksum, file size, signing fingerprint, and upgrade path.
3. Exercise Telegram Android/iOS/Desktop and the app handoff, including expired
   and unavailable links, at 320–1440 px and with keyboard/screen-reader access.
4. Test Android and Windows imports and actual VPN traffic over Wi-Fi and mobile
   networks; cover network switching, DNS, protocol variations, expiry,
   device removal/limits, XUI failure, and recovery after restart.
5. In a sandbox, test duplicate/delayed YooKassa events, one-time versus
   recurring consent, exact-key binding with multiple subscriptions, refunds,
   and interrupted provisioning. Do not use production customers for these
   tests.
6. Confirm encrypted off-host SQLite backups and perform a restore drill,
   including secrets, release configuration, and production overlays.
7. Review data-retention/deletion, administrator access, log redaction, and
   secret-compromise procedures against the live deployment.
8. Add HTML layout, accessibility, real Telegram-rendering, and payment-to-VPN
   checks to the release gate; CI alone does not prove a live tunnel works.

The audit's conditional ideas (Widgets/Rich Messages, device-limit upgrades,
notification preferences, profitability experiments, referral economics, and
automatic server selection) remain gated on feasibility, pricing approval,
or measured production data. The audit itself recommends testing before
shipping these features. Tariff or bonus-month pricing was not changed.

The audit also recommends restructuring the large database, admin, API, and
payment modules. That behavior-neutral refactor is kept separate from this
customer-facing and billing-safety change so it can be reviewed and tested by
responsibility boundaries. Likewise, single-use handoff tickets need a design
that accounts for legitimate browser/link previews; this PR shortens their
lifetime to one hour and keeps subscription URLs out of the ticket query.


The user confirms the bot is already an administrator in the channel; this is
not listed as an outstanding prerequisite.
