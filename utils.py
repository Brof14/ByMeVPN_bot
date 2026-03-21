"""Utility helpers for ByMeVPN bot."""
import logging
from datetime import datetime
from aiogram import Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

# Logo shown in every main screen
LOGO_URL = "https://i.ibb.co/rG9F5PCS/logo.jpg"
# Telegram caption limit for photo messages
_CAPTION_LIMIT = 1024


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_date(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    except Exception:
        return "—"


def fmt_days_left(expiry: int) -> str:
    import time
    left = expiry - int(time.time())
    if left <= 0:
        return "истёк"
    d = left // 86400
    if d > 0:
        return f"{d} дн."
    h = left // 3600
    return f"{h} ч."


# ---------------------------------------------------------------------------
# Message sending helpers
# ---------------------------------------------------------------------------

async def send_or_edit(
    bot: Bot,
    target: "Message | CallbackQuery",
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Edit existing text message or send a new one."""
    msg = target.message if isinstance(target, CallbackQuery) else target

    try:
        return await bot.edit_message_text(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return msg
        # Can't edit (e.g. photo message) — fall through
    except Exception:
        pass

    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    return await bot.send_message(
        chat_id=msg.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def send_with_photo(
    bot: Bot,
    target: "Message | CallbackQuery",
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """
    Send (or edit) a message with the ByMeVPN logo photo.

    - If text > 1024 chars → falls back to text-only (send_or_edit).
    - If current message is already a photo → edits the caption.
    - Otherwise → deletes old message + sends new photo message.
    """
    if len(text) > _CAPTION_LIMIT:
        # Caption too long — use plain text mode
        return await send_or_edit(bot, target, text, reply_markup)

    msg = target.message if isinstance(target, CallbackQuery) else target
    chat_id = msg.chat.id

    # If the current message already has a photo, just edit its caption
    is_photo_msg = bool(getattr(msg, "photo", None))
    if isinstance(target, CallbackQuery) and is_photo_msg:
        try:
            return await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg.message_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return msg
            # Can't edit caption — fall through to delete+send
        except Exception:
            pass

    # Delete old message silently
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass

    # Send new photo message
    try:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=LOGO_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception as e:
        logger.warning("Photo send failed (%s), falling back to text", e)
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def safe_answer(callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass
