"""Background expiry notification scheduler."""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import get_keys_nearing_expiry

logger = logging.getLogger(__name__)


async def _send_expiry_notifications(bot: Bot) -> None:
    keys = await get_keys_nearing_expiry(days_min=1, days_max=3)
    for item in keys:
        try:
            date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
            import time
            days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))
            text = (
                f"⚠️ <b>Напоминание от ByMeVPN</b>\n\n"
                f"Ваша подписка истекает через <b>{days_left} день(дней)</b> — {date_str}.\n\n"
                "Продлите сейчас, чтобы не потерять доступ!"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Продлить подписку", callback_data="buy_vpn")
            ]])
            await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.debug("Notification error for user %d: %s", item["user_id"], e)


async def start_notification_scheduler(bot: Bot) -> None:
    """Run expiry notifications once per day at ~10:00."""
    logger.info("Notification scheduler started")
    while True:
        try:
            await _send_expiry_notifications(bot)
        except Exception as e:
            logger.error("Scheduler error: %s", e)
        # Wait 24 hours
        await asyncio.sleep(86400)
