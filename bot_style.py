"""Shared visual defaults for ByMeVPN Telegram inline keyboards."""

from aiogram.types import InlineKeyboardButton as TelegramInlineKeyboardButton


def themed_button(*args, **kwargs) -> TelegramInlineKeyboardButton:
    """Apply ByMeVPN's blue / green / red action palette to a button.

    Explicit styles win. When a caller omits a style, destructive actions are
    red, positive actions are green, and navigation or neutral actions are
    blue. This gives old keyboards a consistent appearance without changing
    their callback payloads or behavior. Labels stay explicit, so a client
    that does not render button colors still gets the same clear actions.
    """
    if not kwargs.get("style"):
        text = str(kwargs.get("text", args[0] if args else "")).strip().casefold()
        action = str(kwargs.get("callback_data", "")).casefold()
        destructive_markers = (
            "delete", "del_", "remove", "unlink", "unbind_confirm", "cleanup_do",
            "promo_delete", "cancel_gw", "ad_cancel", "disable",
        )
        destructive_labels = (
            "удалить", "отключить", "очистить", "деактивировать", "сбросить",
            "отклонить", "заблокировать", "отозвать",
        )
        success_markers = (
            "trial_free", "free_vpn", "pay_", "autorenew_enable", "key_instructions",
            "activate_promo", "claim_ref_bonus",
        )
        success_labels = (
            "оплатить", "подключить", "получить 3 дня", "получить vpn",
            "включить", "активировать", "забрать", "продлить", "продление подписки",
            "подтвердить", "сохранить", "применить", "одобрить",
        )

        if any(marker in action for marker in destructive_markers) or any(label in text for label in destructive_labels):
            kwargs["style"] = "danger"
        elif any(marker in action for marker in success_markers) or text.startswith(success_labels):
            kwargs["style"] = "success"
        else:
            kwargs["style"] = "primary"
    return TelegramInlineKeyboardButton(*args, **kwargs)
