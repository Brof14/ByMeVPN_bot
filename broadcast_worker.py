"""Durable, single-worker Telegram broadcast queue."""
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_ID
from database import (
    claim_broadcast_recipient,
    finish_broadcast_campaign,
    get_active_broadcast_campaigns,
    get_broadcast_campaign_stats,
    recover_interrupted_broadcasts,
    retry_broadcast_recipient,
    update_broadcast_recipient,
)

logger = logging.getLogger(__name__)


async def _run_campaign(bot: Bot, campaign: dict) -> None:
    campaign_id = int(campaign["id"])
    try:
        buttons = json.loads(campaign.get("buttons_json") or "[]")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton.model_validate(button) for button in row] for row in buttons]
        ) if buttons else None
    except Exception:
        logger.exception("Broadcast campaign %s has invalid button data", campaign_id)
        await finish_broadcast_campaign(campaign_id)
        return

    while True:
        user_id = await claim_broadcast_recipient(campaign_id)
        if user_id is None:
            break
        while True:
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=int(campaign["source_chat_id"]),
                    message_id=int(campaign["source_message_id"]),
                    reply_markup=markup,
                )
                await update_broadcast_recipient(campaign_id, user_id, "sent")
                break
            except TelegramRetryAfter as error:
                # Telegram explicitly asks the bot to slow down; honor the full wait.
                await asyncio.sleep(float(error.retry_after) + 0.25)
            except TelegramForbiddenError as error:
                await update_broadcast_recipient(campaign_id, user_id, "blocked", str(error))
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retry = await retry_broadcast_recipient(campaign_id, user_id, f"{type(error).__name__}: {error}")
                if retry:
                    await asyncio.sleep(5)
                else:
                    logger.warning("Broadcast %s failed for one recipient after retries (%s)", campaign_id, type(error).__name__)
                break
        # Keep below Telegram's global send rate while leaving capacity for normal bot traffic.
        await asyncio.sleep(0.08)

    stats = await get_broadcast_campaign_stats(campaign_id)
    await finish_broadcast_campaign(campaign_id)
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Рассылка #{campaign_id} завершена</b>\n\n"
                f"📤 Доставлено: {stats.get('sent', 0)}\n"
                f"🚫 Недоступны: {stats.get('blocked', 0)}\n"
                f"⚠️ Ошибки после повторов: {stats.get('failed', 0)}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not send broadcast completion summary to admin")


async def start_broadcast_worker(bot: Bot) -> None:
    """Resume active broadcasts after startup and keep the queue supervised."""
    logger.info("Durable broadcast worker started")
    await recover_interrupted_broadcasts()
    while True:
        try:
            campaigns = await get_active_broadcast_campaigns()
            if not campaigns:
                await asyncio.sleep(2)
                continue
            for campaign in campaigns:
                await _run_campaign(bot, campaign)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broadcast queue pass failed; it will resume")
            try:
                await recover_interrupted_broadcasts()
            except Exception:
                logger.exception("Could not recover interrupted broadcast recipients")
            await asyncio.sleep(5)
