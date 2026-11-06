# ByMeVPN Telegram UI — Architecture and Change Log

## Scope and design rules (V3 — 2026-11-07)

The bot is a Telegram-based subscription manager, not a standalone mobile UI.
The Telegram client owns font, chat background and most button colors.
Customer messages should emphasize **current state → next action → optional details**.

- Main navigation has exactly one definition in `keyboards._customer_menu`, with stable public wrappers.
- Customer actions use `primary` blue; trial activation uses `success`; destructive actions use `danger`; navigation/back remains neutral. `bot_style.py` owns these rules and premium icon IDs.
- Only home, about and main subscription screens use a large branded image (`MENU`, `ABOUT`, `KEY`). Purchase, help, devices and bonuses are text-first through `bot_assets.display_artwork`.
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
2. V3: blue primary actions with catalog-sourced custom icons when entitlement is enabled; neutral navigation, green trial and red destructive actions.
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

## Premium button icons

Source: [Zulut30/premium-telegram-emoji — emoji-catalog.md @ 294d22c](https://github.com/Zulut30/premium-telegram-emoji/blob/294d22ccde378dbfba4597317453cbd3715659b2/references/emoji-catalog.md), mainly the minimalist `TgAndroidIcons` pack.

- `bot_style.ICON_IDS` maps semantic meanings to documented catalog IDs.
- The actual `icon_custom_emoji_id` property requires Telegram Bot API >= 9.4 and a qualified bot owner: Telegram Premium on the owner account, or an eligible Fragment username purchased for the bot.
- `BOT_PREMIUM_BUTTON_ICONS=0` is the safe default. On a verified eligible bot set `BOT_PREMIUM_BUTTON_ICONS=1` in the server-side environment, then deploy and inspect all important menus.
- If the bot is not eligible, **leave the variable disabled**: custom emoji can otherwise prevent a keyboard from being accepted by Telegram.
- With icon mode enabled the wrapper strips duplicate *leading* legacy Unicode emoji from decorated labels, but not meaningful status text inside messages.
- The design is still functional on older Telegram clients with no button color or custom emoji support.

## Manual UX acceptance criteria

1. New account: trial button green, other actions blue, bottom pair balanced, custom icons visually align with text.
2. Active account: subscription summary accurate, copy link works, device limit and last seen come from the panel; no fake online status.
3. Expired account: no trial re-offer after use; renewal preserves selected device tier.
4. Tariffs: 2/5/10 and all unchanged term and bonus values; complete payable amount before first payment action; auto-renew details only in checkout and existing account management.
5. Payment: test Stars/YooKassa/CryptoBot callbacks in staging, with real service responses. Never run a production payment for cosmetic QA.
6. Referral: share URL appears once; direct copy action copies the complete generated referral link; bonuses remain idempotent.
7. Ads: channel intake, review and bonus state must survive restart and preserve current terms.
8. Telegram Android/iOS/Desktop: inspect long button rows, Unicode fallback, custom emoji availability and navigation between photo and text.
9. Leave VPS, payment records, secrets, inbounds and user subscriptions unchanged during UX-only development.

## V3 change log

- Added semantic blue button theme with catalog-sourced premium icon IDs, feature-flagged for safe rollout.
- Centralized large-artwork policy so utility screens are text-first without deleting user artwork.
- Shortened onboarding, tariff selection, help, device and referral copy without changing business data.
- Added direct copy for referral links and dedicated help for payments/devices.
- Retained compatibility callbacks and expanded regression testing.

## November 7 UX refinement

- "О ByMeVPN" and "Бесплатные дни" now explain reporting errors and useful suggestions; bonus days remain **manual and conditional**, not promised on every report.
- Referrals and advertising remain distinct channels for bonuses.
- Tariff buttons keep the exact 2 / 5 / 10 tiers, prices, bonus months and real term lengths. The 12+3-month option is labeled **Лучшая цена** because its monthly price is lowest; this is not an unverifiable "most popular" claim.
- Premium gift icons use catalog ID `5301299389245985629` when the bot is entitled and `BOT_PREMIUM_BUTTON_ICONS=1`; payment method buttons have no duplicate Unicode emojis.
- `bot_style.normalize_navigation` moves explicit Back/Cancel controls to separate final keyboard rows on central screen-rendering paths. Their callback payloads are unchanged.

### Advertising: original forwards only

1. A channel owner requests the bonus and, where needed, waits for channel verification.
2. Open the original advertising post in `@ByMeVPN` and **forward** it into the approved target channel, without editing or adding your own advertising copy.
3. Submit the link to the forwarded message from that same channel. The bot rejects a mismatched public-channel link and prevents pending applications from bypassing channel review.
4. A human admin **must verify** the forwarded-from attribution and content before approving a bonus. Telegram Bot API cannot prove the original forwarding source from an arbitrary message URL alone.
5. Keep the repost for 30 days, as required by the existing advertising program.

The optional `AD_SOURCE_POST_URL=https://t.me/ByMeVPN/<message-id>` points directly at the canonical post. If unset, the UI links to the channel without inventing a specific post URL. Configure this exact link after the advertising post is published for a one-tap workflow. Links to other channels are rejected. Do not grant bonuses automatically based on link format alone.

Release checks: all GitHub Actions green; manually verify premium emoji entitlement and the source advertising message in live Telegram before enabling production flags. No live VPS or database changes are part of this UI patch.
