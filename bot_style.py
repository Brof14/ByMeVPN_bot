"""A restrained, semantic Telegram button design for ByMeVPN.

Primary source: Zulut30/premium-telegram-emoji @ 294d22c, sfsymbols.
Purpose-specific exceptions: OS brand marks, Telegram Stars and gift variants.
The same action reuses its *own* icon everywhere, while different semantic
actions use different catalog IDs. Prices, callback contracts and user data
are not affected.

Telegram requires bot eligibility for custom emoji icons. Keep
BOT_PREMIUM_BUTTON_ICONS=0 until entitlement is confirmed; fallback buttons
retain readable text and the same actions.
"""
import os

from aiogram.types import InlineKeyboardButton as TelegramInlineKeyboardButton, InlineKeyboardMarkup

# Confirmed catalog IDs; each role has a distinct icon. Reuse roles, not IDs.
# sfsymbols is primary; exceptions add platform identity and restrained gifting.
ICON_IDS: dict[str, str] = {
    "subscription": "6019290828759898301",  # sfsymbols key
    "plans": "6030573613357209704",         # shopping bag
    "free": "5406756500108501710",          # FREE
    "gift": "6023826881160157558",          # sfsymbols outlined present
    "feedback": "6025951481222403788",      # editing circle
    "crypto": "6030443364178992166",        # sfsymbols wallet
    "referrals": "6021791869820738130",     # two people
    "ads": "6021726637857446455",           # megaphone
    "about": "6019094432790354513",         # informational message
    "support": "6019130940012370273",       # chat with question
    "connect": "6019076101869934284",       # key with radio signal
    "devices": "6021859429656304791",       # tablet and phone
    "device_tier": "6023933602507528561",   # user and devices
    "settings": "5877260593903177342",      # TgAndroidIcons gear
    "copy": "5807640904906840469",          # overlapping rectangles
    "renew": "5807492110059838726",         # circular arrows
    "refresh": "6021772508108167860",       # document with refresh
    "history": "6035297458907519073",       # history/time
    "back": "5805509901048356965",          # left arrow
    "delete": "6021413766669801212",        # trash basket
    "check": "6019175208240289774",         # verified mark
    "list": "6026228223145154159",          # checklist
    "share": "5807462045288766810",         # curved share arrow
    "card": "6030410254276106984",          # microchip card
    "stars": "5172484558305625218",         # Telegram Stars
    "wallet": "5944922791424825958",        # cards in wallet
    "coupon": "5985433648810171091",        # label/tag
    "download": "6021856393114426113",      # download into tray
    "profile": "6021659919835469581",       # user in circle
    "search": "5874960879434338403",        # magnifier
    "ios": "6021554972309592266",           # iPhone with Dynamic Island
    "ipad": "6019245310696495518",          # iPad and iPhone
    "android": "5226673499254332561",       # Android brand symbol
    "windows": "5366318141771096216",      # Windows 11
    "linux": "5424696849086820459",         # Tux
    "macos": "5323440478232783499",         # MacBook
    "android_tv": "5355012477883004708",    # TV
    "more": "6030863729808120196",          # message with ellipsis
    "legal": "6021741567163767583",         # document
    "notifications_on": "6021536113108196448",  # bell
    "notifications_off": "6021440013214948027", # muted bell
    "tariff_1": "6023880246128810031",      # calendar
    "tariff_3": "5301299389245985629",      # slim outlined gift
    "tariff_6": "5301163762768707871",      # gift tile
    "tariff_12": "5301192569114366400",     # gift with star
    "warning": "6019102674832595118",       # warning triangle
}

_DESTRUCTIVE_PREFIXES = (
    "key_delete:", "key_delete_confirm:", "key_device_delete:",
    "key_device_delete_prompt:", "admin_del_", "admin_ban_user:",
    "admin_promo_delete:", "ad_cancel:", "ad_cancel_confirm:",
    "autorenew_unbind_confirm:", "admin_cleanup_do",
)

# Contextual help and informational navigation are not competing blue CTAs.
_SECONDARY_CALLBACKS = frozenset({
    "about", "trouble", "legal", "partner_referrals_list",
    "ad_my_applications", "guide_more", "key_history",
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
    "guide_more": "more",
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
    ("guide_", "more"),
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


# Precisely scoped callbacks; avoid generic guide_* mapping hiding the OS.
_PLATFORM_ICONS = {
    "ios": "ios", "ios_mac": "ipad", "android": "android",
    "windows": "windows", "linux": "linux", "macos": "macos",
    "android_tv": "android_tv",
}
_PRIMARY_CALLBACKS = frozenset({
    "my_keys", "buy_vpn", "partner", "ad_check_channel",
    "connection_guide", "trial_free",
})
_PRIMARY_PREFIXES = ("key_instructions:", "key_renew:", "ad_get_post:")
_SECONDARY_PREFIXES = (
    "devtier_", "guide_", "support_topic:", "key_devices:",
    "key_device:", "key_settings:", "manage_payment_methods",
    "autorenew_", "ad_my_", "admin_", "pay_", "tariff_",
)


def _platform_for_callback(callback: str) -> str | None:
    if not callback.startswith("guide_"):
        return None
    action = callback.split(":", 1)[0].removeprefix("guide_")
    if action.startswith("linux_"):
        return "linux"
    return _PLATFORM_ICONS.get(action)


def _icon_for(text: str, callback: str, *, copy: bool, destructive: bool, back: bool) -> str | None:
    if destructive:
        return ICON_IDS["delete"]
    if back:
        return ICON_IDS["back"]
    if copy:
        return ICON_IDS["copy"]
    platform = _platform_for_callback(callback)
    if platform:
        return ICON_IDS[platform]
    for month in (1, 3, 6, 12):
        if callback.startswith(f"tariff_{month}_"):
            return ICON_IDS[f"tariff_{month}"]
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


def _button_style(label: str, callback: str, *, destructive: bool, navigation: bool) -> str | None:
    """Reserve blue for primary customer actions, not every available option."""
    if destructive:
        return "danger"
    if callback == "trial_free":
        return "success"
    if navigation or callback in _SECONDARY_CALLBACKS:
        return None
    if callback.startswith("tariff_"):
        # Only the balanced 6+2 offer is accented; every period is selectable.
        return "primary" if callback.startswith("tariff_6_") else None
    if callback in _PRIMARY_CALLBACKS or callback.startswith(_PRIMARY_PREFIXES):
        return "primary"
    if callback.startswith(_SECONDARY_PREFIXES):
        return None
    if label.strip().lower().startswith(("скачать", "установить", "открыть приложение", "поделиться")):
        return "primary"
    return None


def themed_button(*args, **kwargs) -> TelegramInlineKeyboardButton:
    """Apply a coherent style and optional premium icon to any button.

    Callers may override the exact style or icon. Navigation is neutral,
    common customer actions blue, trial green, destructive actions red.
    """
    label = str(kwargs.get("text") or (args[0] if args else ""))
    callback = str(kwargs.get("callback_data") or "")
    destructive = callback.startswith(_DESTRUCTIVE_PREFIXES)
    navigation = _is_navigation(label, callback)

    if "style" not in kwargs:
        style = _button_style(label, callback, destructive=destructive, navigation=navigation)
        if style:
            kwargs["style"] = style

    if (
        "icon_custom_emoji_id" not in kwargs
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
