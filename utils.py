"""Utility helpers for ByMeVPN bot."""
import asyncio
import json
import os
from pathlib import Path

from aiogram import Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    FSInputFile,
    InputMediaPhoto,
)
from aiogram.exceptions import TelegramBadRequest

from async_utils import monitor_performance, get_preloaded
from constants import LOGO_URL  # Backward compatibility for existing handlers.
from bot_assets import display_artwork

_photo_file_ids: dict[str, str] = {}
_photo_cache_loaded = False
_photo_cache_lock = asyncio.Lock()


def _photo_cache_path() -> Path:
    configured = os.getenv("BOT_MEDIA_CACHE_FILE")
    return Path(configured) if configured else Path("/app/data/telegram-photo-file-ids.json")


async def _load_photo_cache() -> None:
    global _photo_cache_loaded
    if _photo_cache_loaded:
        return
    async with _photo_cache_lock:
        if _photo_cache_loaded:
            return
        try:
            loaded = json.loads(_photo_cache_path().read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _photo_file_ids.update({str(k): str(v) for k, v in loaded.items() if isinstance(v, str)})
        except (OSError, ValueError, TypeError):
            pass
        _photo_cache_loaded = True


async def _photo_source(photo: str):
    """Return a cached Telegram file_id or a local upload source."""
    path = Path(photo)
    if not path.is_file():
        return photo, None
    await _load_photo_cache()
    key = path.name
    return _photo_file_ids.get(key) or FSInputFile(path), key


async def _remember_photo_file_id(key: str | None, message: Message) -> None:
    if not key:
        return
    photos = getattr(message, "photo", None) or []
    if not photos:
        return
    await _load_photo_cache()
    async with _photo_cache_lock:
        _photo_file_ids[key] = photos[-1].file_id
        path = _photo_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(_photo_file_ids, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)
        except OSError:
            # The in-memory cache still speeds up this process; persistent
            # caching is a best-effort optimization, not a delivery dependency.
            pass


async def send_photo_message(
    bot: Bot,
    chat_id: int,
    photo: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Send a local banner, caching Telegram's file_id for later sends."""
    source, cache_key = await _photo_source(photo)
    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=source,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        await _remember_photo_file_id(cache_key, sent)
        return sent
    except Exception:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

# ---------------------------------------------------------------------------
# Message sending helpers
# ---------------------------------------------------------------------------

@monitor_performance("send_or_edit")
async def send_or_edit(
    bot: Bot,
    target: "Message | CallbackQuery",
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    photo: str | None = None,
) -> Message:
    """Edit the current screen and reuse Telegram's file_id for local artwork."""
    photo = display_artwork(photo)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if photo:
        source, cache_key = await _photo_source(photo)
        if getattr(msg, "photo", None):
            try:
                updated = await bot.edit_message_media(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    media=InputMediaPhoto(media=source, caption=text, parse_mode="HTML"),
                    reply_markup=reply_markup,
                )
                await _remember_photo_file_id(cache_key, updated)
                return updated
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return msg
            except Exception:
                pass

        try:
            sent = await bot.send_photo(
                chat_id=msg.chat.id,
                photo=source,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            await _remember_photo_file_id(cache_key, sent)
        except Exception:
            return await send_or_edit(bot, target, text, reply_markup)
        try:
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass
        return sent

    if getattr(msg, "photo", None):
        # Old screens used remote banners. Skip a guaranteed edit-text failure
        # and replace that message in two Telegram calls on the first tap.
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
        # Telegram cannot turn a photo into a text message; replace it below.
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


@monitor_performance("send_with_photo")
async def send_with_photo(
    bot: Bot,
    target: "Message | CallbackQuery",
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    photo: str | None = None,
) -> Message:
    """Send or edit a screen with a locally stored, Telegram-cached banner."""
    photo = display_artwork(photo)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        return await send_or_edit(bot, target, text, reply_markup, photo=photo)

    if photo:
        return await send_photo_message(bot, msg.chat.id, photo, text, reply_markup)

    return await bot.send_message(
        chat_id=msg.chat.id, text=text, parse_mode="HTML", reply_markup=reply_markup,
    )


async def safe_answer(callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass
