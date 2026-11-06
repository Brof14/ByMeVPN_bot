"""ByMeVPN Telegram button design system.

All buttons are created with this wrapper, so legacy callback IDs and backend
behavior remain unchanged. Icon IDs are taken from Zulut30/premium-telegram-emoji,
commit 294d22c (mostly the monochrome TgAndroidIcons pack).

Set BOT_PREMIUM_BUTTON_ICONS=1 only when the bot owner has Telegram Premium or
the bot has an eligible Fragment username. Telegram rejects custom emoji icons
without that entitlement. The default leaves icons off, never buttons broken.
"""
import os

from aiogram.types import InlineKeyboardButton as TelegramInlineKeyboardButton

# Stable catalog IDs. One semantic action always has the same icon.
ICON_IDS: dict[str, str] = {
    "subscription": "6005570495603282482",  # key_bw
    "plans": "5927169041595634481",         # card
    "free": "5406756500108501710",          # free
    "referrals": "5942877472163892475",     # people
    "ads": "5771695636411847302",           # announce_bw
    "about": "5879785854284599288",         # info_bw
    "support": "5884510167986343350",       # support
    "connect": "5879585266426973039",       # internet_bw
    "devices": "5987917196469213507",       # qr
    "settings": "5877260593903177342",      # settings_bw
    "copy": "5877301185639091664",          # copy
    "renew": "5877410604225924969",         # updates
    "refresh": "5845943483382110702",       # obnovit
    "history": "5967456680940671207",       # archive
    "back": "5875082500023258804",         # back
    "delete": "5879896690210639947",       # trash
    "check": "5805532930662996322",        # verified
    "list": "5877597667231534929",         # list
    "share": "5877468380125990242",        # forward
    "card": "5927169041595634481",         # card
    "stars": "5958376256788502078",        # stars_bw
    "wallet": "5769403330761593044",       # wallet
    "coupon": "5985433648810171091",       # tag
    "download": "5899757765743615694",     # install
    "profile": "5879770735999717115",      # profile
    "search": "5874960879434338403",       # search
}

_DESTRUCTIVE_PREFIXES = (
    "key_delete:", "key_delete_confirm:", "key_device_delete:",
    "key_device_delete_prompt:", "admin_del_", "admin_ban_user:",
    "admin_promo_delete:", "ad_cancel:", "ad_cancel_confirm:",
    "autorenew_unbind_confirm:", "admin_cleanup_do",
)

_ACTION_ICONS = {
    "my_keys": "subscription",
    "buy_vpn": "plans",
    "free_vpn": "free",
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
    "pay_crypto": "wallet",
    "key_history": "history",
    "admin_stats": "list",
    "admin_search": "search",
}

_PREFIX_ICONS = (
    ("devtier_", "devices"),
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
    ("guide_", "connect"),
    ("support_topic:", "support"),
    ("ad_get_post:", "ads"),
    ("ad_submit_post:", "share"),
    ("ad_admin_", "ads"),
    ("admin_user", "profile"),
    ("admin_promo", "coupon"),
)

# A fallback for external URLs and copy buttons without callback data.
_LABEL_ICONS = (
    ("поделиться", "share"),
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


def _icon_for(text: str, callback: str, *, copy: bool, destructive: bool, back: bool) -> str | None:
    if destructive:
        return ICON_IDS["delete"]
    if back:
        return ICON_IDS["back"]
    if copy:
        return ICON_IDS["copy"]
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
        if destructive:
            kwargs["style"] = "danger"
        elif callback == "trial_free":
            kwargs["style"] = "success"
        elif navigation:
            pass
        else:
            kwargs["style"] = "primary"

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
