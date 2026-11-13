"""A restrained, semantic Telegram button design for ByMeVPN.

Primary source: Zulut30/premium-telegram-emoji @ 294d22c,
"Minimalist B&W Icons" (TgAndroidIcons). Limited exceptions are platform
marks, calendar, Telegram Stars and one consistent gift icon for bonus terms.
Prices, callback contracts and user data are not affected.

Telegram requires bot eligibility for custom emoji icons. Keep
BOT_PREMIUM_BUTTON_ICONS=0 until entitlement is confirmed; fallback buttons
retain readable text and the same actions.
"""
import os

from aiogram.types import InlineKeyboardButton as TelegramInlineKeyboardButton, InlineKeyboardMarkup

# Use Minimalist B&W IDs for common actions; keep limited platform and gift
# exceptions where the pack has no matching glyph.
ICON_IDS: dict[str, str] = {
    "subscription": "6005570495603282482",  # key
    "plans": "5983399041197675256",         # shop
    "free": "5825794181183836432",          # check
    "gift": "6023826881160157558",          # one present icon reused for all bonus terms
    "feedback": "5879841310902324730",      # pencil
    "crypto": "5769403330761593044",        # wallet
    "referrals": "5942877472163892475",     # people
    "ads": "5771695636411847302",           # announcement
    "about": "5879785854284599288",         # information
    "support": "5884510167986343350",       # support chat
    "connect": "5877465816030515018",       # link
    "devices": "5967591100532134862",       # phone
    "device_tier": "5877485980901971030",   # data
    "settings": "5877260593903177342",      # TgAndroidIcons gear
    "copy": "5877301185639091664",          # copy
    "renew": "5877410604225924969",         # updates
    "refresh": "5845943483382110702",       # refresh
    "history": "5967456680940671207",       # archive
    "back": "5875082500023258804",          # back
    "delete": "5879896690210639947",        # trash
    "check": "5805532930662996322",         # verified
    "list": "5877597667231534929",          # list
    "share": "5877468380125990242",         # forward/share
    "card": "5927169041595634481",          # card
    "stars": "5958376256788502078",         # Stars
    "wallet": "5944922791424825958",        # cards in wallet
    "coupon": "5985433648810171091",        # tag
    "download": "5899757765743615694",      # install/download
    "profile": "5879770735999717115",       # profile
    "search": "5874960879434338403",        # magnifier
    "ios": "6021554972309592266",           # iPhone with Dynamic Island
    "ipad": "6019245310696495518",          # iPad and iPhone
    "android": "5226673499254332561",       # Android brand symbol
    "windows": "5366318141771096216",      # Windows 11
    "linux": "5424696849086820459",         # Tux
    "macos": "5323440478232783499",         # MacBook
    "android_tv": "5355012477883004708",    # TV
    "more": "5875019892284985369",          # dots
    "legal": "6021741567163767583",         # document
    "notifications_on": "5909201569898827582",  # notification
    "notifications_off": "5872829476143894491", # ban/mute
    "tariff_1": "6023880246128810031",      # calendar
    "warning": "5879813604068298387",       # exclamation
}

MINIMALIST_BW_ICON_IDS = frozenset({
    "6005570495603282482", "5983399041197675256", "5825794181183836432",
    "5879841310902324730", "5769403330761593044", "5942877472163892475",
    "5771695636411847302", "5879785854284599288", "5884510167986343350",
    "5877465816030515018", "5967591100532134862", "5877485980901971030",
    "5877260593903177342", "5877301185639091664", "5877410604225924969",
    "5845943483382110702", "5967456680940671207", "5875082500023258804",
    "5879896690210639947", "5805532930662996322", "5877597667231534929",
    "5877468380125990242", "5927169041595634481", "5958376256788502078",
    "5985433648810171091", "5899757765743615694", "5879770735999717115",
    "5874960879434338403", "5875019892284985369", "5909201569898827582",
    "5872829476143894491", "5879813604068298387",
})

_DESTRUCTIVE_PREFIXES = (
    "key_delete:", "key_delete_confirm:", "key_device_delete:",
    "key_device_delete_prompt:", "admin_del_", "admin_ban_user:",
    "admin_promo_delete:", "ad_cancel:", "ad_cancel_confirm:",
    "autorenew_unbind_confirm:", "admin_cleanup_do",
)

# Contextual help and informational navigation are not competing blue CTAs.
_SECONDARY_CALLBACKS = frozenset({
    "about", "trouble", "legal", "partner_referrals_list",
    "ad_my_applications", "key_history",
})

_ACTION_ICONS = {
    "my_keys": "subscription",
    "buy_vpn": "plans",
    "free_vpn": "gift",
    "trial_free": "free",
    "about": "about",
    "trouble": "support",
    "partner": "referrals",
    "partner_referrals_list": "history",
    "ad_program": "ads",
    "ad_check_channel": "check",
    "ad_bonus_tiers": "list",
    "ad_my_applications": "list",
    "connection_guide": "connect",
    "pay_yookassa": "card",
    "pay_stars": "stars",
    "pay_crypto": "crypto",
    "key_history": "history",
    "admin_stats": "list",
    "admin_search": "search",
    "legal": "legal",
    "marketing_optin": "notifications_on",
    "marketing_optout": "notifications_off",
    "manage_payment_methods": "card",
}

_PREFIX_ICONS = (
    ("devtier_", "device_tier"),
    ("tariff_", "plans"),
    ("period_", "plans"),
    ("key_devices:", "devices"),
    ("key_device:", "devices"),
    ("key_info:", "subscription"),
    ("key_instructions:", "connect"),
    ("key_renew:", "renew"),
    ("key_settings:", "settings"),
    ("manage_payment_methods", "card"),
    ("autorenew_", "settings"),
    ("support_topic:", "support"),
    ("ad_get_post:", "share"),
    ("ad_submit_post:", "share"),
    ("ad_admin_", "ads"),
    ("admin_user", "profile"),
    ("admin_promo", "coupon"),
)

# A fallback for external URLs and copy buttons without callback data.
_LABEL_ICONS = (
    ("поделиться", "share"),
    ("сообщить об ошибке", "feedback"),
    ("предложить улучшение", "feedback"),
    ("передать идею", "feedback"),
    ("написать в поддержку", "support"),
    ("скопировать", "copy"),
    ("установить", "download"),
    ("скачать", "download"),
    ("подключить", "connect"),
    ("продлить", "renew"),
    ("устройства", "devices"),
    ("поддержка", "support"),
    ("оплатить", "card"),
    ("криптовалют", "wallet"),
    ("telegram stars", "stars"),
    ("профиль", "profile"),
    ("история", "history"),
    ("главное меню", "back"),
)

_LEGACY_PREFIXES = (
    "🔑 ", "📱 ", "🛒 ", "💳 ", "⭐ ", "🎁 ", "👥 ",
    "📋 ", "📣 ", "🔙 ", "🏠 ", "🔄 ", "🔗 ",
    "📤 ", "✏️ ", "🗑 ", "🗑️ ", "💎 ", "🆘 ",
    "◀️ ", "◀ ", "← ", "↻ ",
)


def _is_navigation(text: str, callback: str) -> bool:
    label = text.strip().lower()
    if label.startswith(("←", "◀")):
        return True
    for legacy in ("🔙 ", "🏠 "):
        if label.startswith(legacy):
            label = label[len(legacy):]
            break
    return (
        callback == "back_to_menu"
        or label.startswith(("назад", "отмена", "главное меню", "к меню"))
        or label in {"закрыть", "отменить"}
    )


_PRIMARY_CALLBACKS = frozenset({
    "my_keys", "buy_vpn", "partner", "ad_check_channel",
    "connection_guide", "trial_free",
})
_PRIMARY_PREFIXES = ("key_instructions:", "key_renew:", "ad_get_post:")
_SECONDARY_PREFIXES = (
    "devtier_", "support_topic:", "key_devices:",
    "key_device:", "key_settings:", "manage_payment_methods",
    "autorenew_", "ad_my_", "admin_", "pay_", "tariff_",
)


def _icon_for(text: str, callback: str, *, copy: bool, destructive: bool, back: bool) -> str | None:
    if destructive:
        return ICON_IDS["delete"]
    if back:
        return ICON_IDS["back"]
    if copy:
        return ICON_IDS["copy"]
    if callback.startswith("tariff_1_"):
        return ICON_IDS["tariff_1"]
    if callback.startswith(("tariff_3_", "tariff_6_", "tariff_12_")):
        return ICON_IDS["gift"]
    if callback == "autorenew_checkout_toggle":
        return ICON_IDS["notifications_on"] if "включено" in text.casefold() else ICON_IDS["notifications_off"]
    if callback.startswith("autorenew_enable:"):
        return ICON_IDS["notifications_on"]
    if callback.startswith(("autorenew_unbind", "autorenew_cancel")):
        return ICON_IDS["notifications_off"]
    semantic = _ACTION_ICONS.get(callback)
    if semantic:
        return ICON_IDS[semantic]
    for prefix, semantic in _PREFIX_ICONS:
        if callback.startswith(prefix):
            return ICON_IDS[semantic]
    label = text.lstrip(" 🔑📱⭐💳🔗🎁💎📣🛒↻").lower()
    for prefix, semantic in _LABEL_ICONS:
        if label.startswith(prefix):
            return ICON_IDS[semantic]
    return None


def _button_style(label: str, callback: str, *, destructive: bool, navigation: bool) -> str:
    """Keep every button colored: blue actions, green claims, red destructive actions."""
    if destructive:
        return "danger"
    if callback in {"trial_free", "channel_bonus_check", "free_vpn"}:
        return "success"
    return "primary"


def themed_button(*args, **kwargs) -> TelegramInlineKeyboardButton:
    """Apply a coherent style and optional premium icon to any button.

    Callers may override the exact style or icon. Every button remains colored:
    navigation and standard actions blue, reward actions green, destructive red.
    """
    label = str(kwargs.get("text") or (args[0] if args else ""))
    callback = str(kwargs.get("callback_data") or "")
    destructive = callback.startswith(_DESTRUCTIVE_PREFIXES)
    navigation = _is_navigation(label, callback)

    # Explicit None would render a neutral button; infer a visible color instead.
    if kwargs.get("style") is None or kwargs.get("style") not in {"primary", "success", "danger"}:
        kwargs["style"] = _button_style(label, callback, destructive=destructive, navigation=navigation)

    if (
        "icon_custom_emoji_id" not in kwargs
        and not callback.startswith(("tariff_3_", "tariff_6_", "tariff_12_"))
        and os.getenv("BOT_PREMIUM_BUTTON_ICONS", "0").strip().lower() in {"1", "true", "yes", "on"}
    ):
        icon = _icon_for(
            label, callback, copy=kwargs.get("copy_text") is not None,
            destructive=destructive, back=navigation,
        )
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
            # Avoid displaying the same icon twice in clients supporting
            # button custom emoji. When disabled we preserve old labels.
            clean = label
            for prefix in _LEGACY_PREFIXES:
                if clean.startswith(prefix):
                    clean = clean[len(prefix):]
                    break
            if clean != label:
                if "text" in kwargs:
                    kwargs["text"] = clean
                elif args:
                    args = (clean, *args[1:])
    return TelegramInlineKeyboardButton(*args, **kwargs)


def normalize_navigation(keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Keep back/cancel actions on the bottom, without changing their callbacks.

    Pagination arrows and actions such as 'Отменить автопродление' are not back buttons.
    """
    if keyboard is None:
        return None
    rows: list[list[TelegramInlineKeyboardButton]] = []
    navigation: list[TelegramInlineKeyboardButton] = []
    for row in keyboard.inline_keyboard:
        other = []
        for button in row:
            label = button.text.strip().lower()
            is_back = (
                label.startswith(("← ", "◀ ", "◀️ ", "🔙 ", "🏠 "))
                or (label in {"назад", "отмена", "главное меню", "закрыть"} and not button.url)
                or button.callback_data == "back_to_menu"
            )
            (navigation if is_back else other).append(button)
        if other:
            rows.append(other)
    if not navigation:
        return keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows + [[button] for button in navigation])
