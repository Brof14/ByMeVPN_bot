# ByMeVPN Telegram UI — Architecture and Change Log

## Scope and design rules (2026-11-06)

The bot is a Telegram-based subscription manager, not a standalone mobile UI.
The Telegram client owns font, chat background and most button colors.
Customer messages should emphasize **current state → next action → optional details**.

- Main navigation has exactly one definition in `keyboards._customer_menu`, with stable public wrappers.
- Customer-facing sections use neutral buttons; destructive actions may use red.
- One large brand image is allowed on the home view. Purchase, support, device and referral screens are text-first.
- Do not quote backend details (UUID, Xray protocols, 3x-ui IDs) on marketing screens.
- Use **Подписка**, **Тарифы**, **Бесплатные дни**, **Устройства** consistently.
- The 2 / 5 / 10 device tiers, tariff prices, durations and bonus months remain controlled by `constants.py`.
- Renewals keep the existing backend rules. Explain potential stored-payment recurring charges in checkout before the first payment action. Existing subscribers can inspect and disable recurring charges in account management.
- Promised bonuses must match the actual one-time 3-day free trial and the referral-grant rules.

## Module boundaries

| Area | Owner |
| --- | --- |
| Bot lifecycle, middleware, routers | `main.py` |
| Entry, account status, promo deep links and free trial | `handlers/start.py` |
| Tariff chooser and checkout | `handlers/buy.py` |
| Subscription status, devices and billing preferences | `handlers/keys.py` |
| Connection help | `handlers/guide.py` |
| Referral customer screens | `handlers/partner.py` |
| Advertiser and admin review workflows | `handlers/ad_program.py` |
| Admin tools | `handlers/admin.py` |
| Shared keyboards and styles | `keyboards.py`, `bot_style.py` |
| Active image sources | `bot_assets.py` |
| Monetary policy | `constants.py`, `payments.py` |
| Access provisioning and renewal | `subscription.py`, `autorenew.py` |
| Payment reconciliation | `webhook.py`, `payment_monitor.py` |
| Persistence / idempotency | `database.py` |

### Safety rules

Preserve callback data when renaming visible labels: Telegram users can tap buttons in old messages. Keep payment idempotency, authenticated device ownership and protected admin callbacks. Do not delete persisted client data, or change VPN server state, as part of a visual cleanup.

## Implementation log

1. Unified customer home keyboards to remove menu duplication; preserved callback actions.
2. Removed automatic primary/success coloring from ordinary actions; red remains for destructive actions.
3. Simplified subscription, device, referral, advertisement and onboarding copy.
4. Moved advertising bonus tiers behind a separate button to keep the initial screen short.
5. Kept the first-step recurring-payment disclosure in checkout and removed duplicate later explanations.
6. Added an official Android download-page entry, with Happ as a fallback where ByMeVPN is not distributed.
7. Removed repeated artwork from utility screens, retaining first-view branding.
8. Removed an unregistered duplicate referral handler and unused media assets.
9. Added a GitHub Actions workflow to compile Python and run the regression suite on each PR/push.
10. Updated behavior tests where button labels, media presentation or navigation changed.

## Verification

Run:

```sh
python -m pip install -r requirements.txt
python -m compileall -q .
python -m unittest discover -s tests -p 'test_*.py' -q
```

CI is a regression gate, not a guarantee of production readiness. Before deploying,
test navigation in Telegram mobile and desktop and verify payment callbacks, 3x-ui
provisioning, free trials, device removal and subscription continuity against staging
services. Do not deploy simply because a UI-only PR was merged.
