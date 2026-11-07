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
from bot_style import normalize_navigation

_photo_file_ids: dict[str, str] = {}
_photo_cache_loaded: set[str] = set()
_photo_cache_lock = asyncio.Lock()
_PHOTO_CAPTION_LIMIT = 1024


def _bot_media_namespace(bot: Bot | None) -> str:
    """Stable, non-secret cache namespace for a Telegram bot account."""
    token = str(getattr(bot, "token", ""))
    bot_id = token.partition(":")[0]
    return bot_id if bot_id.isdecimal() else "default"


def _photo_cache_path(bot: Bot | None = None) -> Path:
    configured = os.getenv("BOT_MEDIA_CACHE_FILE")
    path = Path(configured) if configured else Path("/app/data/telegram-photo-file-ids.json")
    namespace = _bot_media_namespace(bot)
    return path.with_name(f"{path.stem}-{namespace}{path.suffix or '.json'}")


async def _load_photo_cache(bot: Bot | None = None) -> None:
    namespace = _bot_media_namespace(bot)
    if namespace in _photo_cache_loaded:
        return
    async with _photo_cache_lock:
        if namespace in _photo_cache_loaded:
            return
        try:
            loaded = json.loads(_photo_cache_path(bot).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _photo_file_ids.update({f"{namespace}:{k}": str(v) for k, v in loaded.items() if isinstance(v, str)})
        except (OSError, ValueError, TypeError):
            pass
        _photo_cache_loaded.add(namespace)


async def _photo_source(photo: str, bot: Bot | None = None):
    """Return a cached Telegram file_id or a local upload source."""
    path = Path(photo)
    if not path.is_file():
        return photo, None
    namespace = _bot_media_namespace(bot)
    await _load_photo_cache(bot)
    key = f"{namespace}:{path.name}"
    return _photo_file_ids.get(key) or FSInputFile(path), key


async def _remember_photo_file_id(key: str | None, message: Message, bot: Bot | None = None) -> None:
    if not key:
        return
    photos = getattr(message, "photo", None) or []
    if not photos:
        return
    namespace = _bot_media_namespace(bot)
    await _load_photo_cache(bot)
    async with _photo_cache_lock:
        _photo_file_ids[key] = photos[-1].file_id
        path = _photo_cache_path(bot)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            cached = {
                cache_key.split(":", 1)[1]: file_id
                for cache_key, file_id in _photo_file_ids.items()
                if cache_key.startswith(f"{namespace}:")
            }
            temp.write_text(json.dumps(cached, separators=(",", ":")), encoding="utf-8")
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
    reply_markup = normalize_navigation(reply_markup)
    source, cache_key = await _photo_source(photo, bot)
    try:
        long_text = len(text) > _PHOTO_CAPTION_LIMIT
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=source,
            caption="ByMeVPN" if long_text else text,
            parse_mode="HTML",
            reply_markup=None if long_text else reply_markup,
        )
        await _remember_photo_file_id(cache_key, sent, bot)
        if long_text:
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
            )
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

async def _replace_text_screen(
    bot: Bot, current: Message, text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> Message:
    """Publish replacement first so an outbound failure never erases a screen."""
    sent = await bot.send_message(
        chat_id=current.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
    try:
        await bot.delete_message(current.chat.id, current.message_id)
    except Exception:
        # Telegram may prohibit deleting older messages; the new one exists.
        pass
    return sent


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
    reply_markup = normalize_navigation(reply_markup)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if photo:
        source, cache_key = await _photo_source(photo, bot)
        long_text = len(text) > _PHOTO_CAPTION_LIMIT
        if long_text:
            if getattr(msg, "photo", None):
                try:
                    image = await bot.edit_message_media(
                        chat_id=msg.chat.id,
                        message_id=msg.message_id,
                        media=InputMediaPhoto(media=source, caption="ByMeVPN", parse_mode="HTML"),
                        reply_markup=None,
                    )
                    await _remember_photo_file_id(cache_key, image, bot)
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e):
                        image = msg
                        try:
                            await bot.edit_message_reply_markup(
                                chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None,
                            )
                        except Exception:
                            pass
                    else:
                        source, cache_key = await _photo_source(photo, bot)
                        image = await bot.send_photo(
                            chat_id=msg.chat.id, photo=source, caption="ByMeVPN", parse_mode="HTML",
                        )
                        await _remember_photo_file_id(cache_key, image, bot)
                except Exception:
                    source, cache_key = await _photo_source(photo, bot)
                    image = await bot.send_photo(
                        chat_id=msg.chat.id, photo=source, caption="ByMeVPN", parse_mode="HTML",
                    )
                    await _remember_photo_file_id(cache_key, image, bot)
            else:
                image = await bot.send_photo(
                    chat_id=msg.chat.id, photo=source, caption="ByMeVPN", parse_mode="HTML",
                )
                await _remember_photo_file_id(cache_key, image, bot)
            updated = await bot.send_message(
                chat_id=msg.chat.id, text=text, parse_mode="HTML", reply_markup=reply_markup,
            )
            if not getattr(msg, "photo", None):
                try:
                    await bot.delete_message(msg.chat.id, msg.message_id)
                except Exception:
                    pass
            return updated
        if getattr(msg, "photo", None):
            try:
                updated = await bot.edit_message_media(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    media=InputMediaPhoto(media=source, caption=text, parse_mode="HTML"),
                    reply_markup=reply_markup,
                )
                await _remember_photo_file_id(cache_key, updated, bot)
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
            await _remember_photo_file_id(cache_key, sent, bot)
        except Exception:
            return await send_or_edit(bot, target, text, reply_markup)
        try:
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass
        return sent

    if getattr(msg, "photo", None):
        # Reuse the existing media message when switching to a text-first screen.
        # Telegram accepts a new caption without deleting the old message;
        # if the caption limit is exceeded, fall back to a replacement.
        try:
            return await bot.edit_message_caption(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return msg
        except Exception:
            pass
        return await _replace_text_screen(bot, msg, text, reply_markup)

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

    return await _replace_text_screen(bot, msg, text, reply_markup)


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
    reply_markup = normalize_navigation(reply_markup)
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
