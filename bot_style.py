"""Neutral inline-button defaults for ByMeVPN.

Ordinary actions use the Telegram client theme. Explicit colors remain
available for intentionally styled administrative actions, while dangerous
buttons are marked red without relying on the visible language.
"""

from aiogram.types import InlineKeyboardButton as TelegramInlineKeyboardButton

_DESTRUCTIVE_PREFIXES = (
    "key_delete:", "key_delete_confirm:", "key_device_delete:",
    "key_device_delete_prompt:", "admin_del_", "admin_ban_user:",
    "admin_promo_delete:", "ad_cancel:", "ad_cancel_confirm:",
    "autorenew_unbind_confirm:", "admin_cleanup_do",
)


def themed_button(*args, **kwargs) -> TelegramInlineKeyboardButton:
    """Default to no forced color; reserve red for destructive callbacks."""
    if "style" not in kwargs:
        action = str(kwargs.get("callback_data") or "")
        if action.startswith(_DESTRUCTIVE_PREFIXES):
            kwargs["style"] = "danger"
    return TelegramInlineKeyboardButton(*args, **kwargs)
