# ByMeVPN — premium emoji & visual UX design system

**Source audited:** [Zulut30/premium-telegram-emoji](https://github.com/Zulut30/premium-telegram-emoji), commit `294d22ccde378dbfba4597317453cbd3715659b2` (68 catalog sections). Sources include `SKILL.md`, `references/emoji-selection.md`, `references/pack-analysis.md`, `data/selection-policy.json`, and the attached full `references/emoji-catalog.md`. The source repository has a selection engine, style metadata, preview workflow and QA/test utilities; it is not a guarantee that every individual icon has been visually inspected on every Telegram client.

## Visual direction
- **Premium = predictable, minimal and readable**, not an abundance of illustrations.
- Primary source pack: [sfsymbols](https://t.me/addemoji/sfsymbols) for consistent system-like silhouette, with deliberate exceptions for true operating-system logos, Telegram Stars and distinctive gift tiers.
- No cheap neon, pixel, game, cartoon, random stickers or unrelated icons in customer screens.
- One semantic role = one unique catalog ID. The same role reuses its icon everywhere. Each platform and duration tier receives its own ID. The runtime single source of truth is `bot_style.ICON_IDS`.
- Keep customer copy short. Prefer one primary blue action, green only for the free trial, red only for destructive confirmation. Navigation is uncolored and always the last keyboard row.
- Strong brand illustrations only on menu, about, tariff, subscription-key, connection, referrals and advertising program screens. Intermediate forms, platform steps, troubleshooting, device settings and checkout are text-first.
- All button callbacks, payment logic, entitlement rules and 2/5/10 device tiers stay unchanged.

## Platform roles (catalog verified)

| Platform | Catalog object | Custom emoji ID |
|---|---|---|
| iPhone | iPhone with Dynamic Island / sfsymbols | `6021554972309592266` |
| iPad + iPhone legacy selector | iPad and iPhone / sfsymbols | `6019245310696495518` |
| Android | Android mark / MorkovkaAdaptive | `5226673499254332561` |
| Windows | Windows 11 / Microsoft icons | `5366318141771096216` |
| Linux | Tux / DigitalEmojiPack | `5424696849086820459` |
| Mac | MacBook / Apple icons | `5323440478232783499` |
| Android TV | Television / TopicIcons | `5355012477883004708` |

Tariff buttons use the catalog calendar `6023880246128810031` for all durations. Gift icons were removed from these buttons because the current calculation charges for the full included period. The actual prices and durations remain the existing purchase contract.

## UX release checklist
1. Check button icons on Android Telegram, iOS Telegram and Telegram Desktop, both dark and light theme. Compare stroke weight, cropping, animation, tap targets and legibility; replace visibly mismatched exceptions after actual visual review.
2. Confirm bot's eligibility for button icons. `BOT_PREMIUM_BUTTON_ICONS=1` only for an eligible bot. Leave it disabled otherwise: Telegram can reject unauthorised premium button icons. Never enable blindly in production.
3. Confirm fallback without icons retains the same labels and callbacks; back navigation stays at the bottom.
4. Check first visit, first trial, expired/active with one or more keys, tariff selection for all 2/5/10 tiers, all three payment methods, platform guide, referral, advertising forward and support.
5. Test returned subscriber with exactly one active key: connect action must use that key ID, avoiding the wrong subscription; multiple keys use the subscription picker.
6. Check real first connection, payment success/failure, stopped notification preferences and restoration on VPS separately. This PR does not deploy or test real payment providers.

## Commercial rationale and limits
- Highlight **6+2** as the price/commitment compromise while keeping **12+3** visibly marked as the lowest effective monthly price; do not insert fake social proof or countdown timers.
- Show total checkout price from the canonical calculation, not from display labels. This PR never changes the canonical calculation.
- Prioritize app install and connection over exposing subscription URLs; manual link copy remains available.
- Fewer pictures improve screen density and avoid needless Telegram photo upload overhead; the seven existing brand visuals remain in use.
- Conversion and premium appearance **cannot be rated 100/100 without live client visual inspection and cohort metrics**. No uplift estimates are claimed.

## Source-repository assessment
- The upstream source has 68 topical sections and systematic matching rules, but styles vary widely. Blindly choosing one icon from each section destroys consistency.
- sfsymbols covers nearly all functional UI intents; OS brand symbols are a justified exception.
- Telegram eligibility and style rendering are platform API limitations; an offline verified ID does not prove a bot can use it.
- The upstream repository provides previews for candidates via its catalog tooling, but they were not all visually approved in Telegram on behalf of ByMeVPN.
