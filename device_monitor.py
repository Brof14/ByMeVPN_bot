"""Low-load polling for new Happ HWID registrations on currently online profiles."""
import asyncio
import html
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_user_keys, remember_hwid_devices, mark_hwid_device_notified
from xui_client import get_xui_devices, get_xui_online_user_ids

logger = logging.getLogger(__name__)
_MONITOR_STARTED_AT = int(time.time())
_POLL_SECONDS = 180
_MAX_CONCURRENT_PROFILES = 8


async def _check_user(bot: Bot, user_id: int, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            keys = await get_user_keys(user_id)
            active_keys = [key for key in keys if int(key.get("expiry") or 0) > int(time.time())]
            if not active_keys:
                return
            state = await get_xui_devices(user_id)
            pending = await remember_hwid_devices(user_id, state["devices"], _MONITOR_STARTED_AT)
            if not pending:
                return
            key_id = active_keys[0]["id"]
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Мои устройства", callback_data=f"key_devices:{key_id}")
            ]])
            for device in pending:
                name = html.escape(device.get("model") or device.get("user_agent") or "Устройство")
                system = " ".join(part for part in (device.get("os"), device.get("os_version")) if part)
                system_line = f"\n⚙️ {html.escape(system)}" if system else ""
                try:
                    await bot.send_message(
                        user_id,
                        "📱 <b>Новое устройство</b>\n"
                        f"🔗 {name}{system_line}\n\n"
                        "Не вы? Перевыпустите ссылку в разделе ключа — старый доступ отключится.",
                        reply_markup=markup,
                    )
                    await mark_hwid_device_notified(user_id, device["id"])
                except Exception:
                    logger.info("Could not send new-device notice to user %s", user_id, exc_info=True)
        except Exception:
            logger.debug("Could not inspect devices for online user %s", user_id, exc_info=True)


async def start_device_monitor(bot: Bot) -> None:
    """Poll only online subscriptions, limiting panel calls and parallelism."""
    while True:
        started = time.monotonic()
        try:
            online_ids = await get_xui_online_user_ids()
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROFILES)
            ordered_ids = sorted(online_ids)
            for offset in range(0, len(ordered_ids), 50):
                await asyncio.gather(
                    *(_check_user(bot, user_id, semaphore) for user_id in ordered_ids[offset:offset + 50]),
                    return_exceptions=True,
                )
            if online_ids:
                logger.debug("Device monitor checked %d currently online profiles", len(online_ids))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Device monitor poll failed")
        await asyncio.sleep(max(5, _POLL_SECONDS - (time.monotonic() - started)))
