"""Conservative monthly giveaway: one winner, a capped 30-day prize, retry-safe delivery."""
from __future__ import annotations

import asyncio
from html import escape
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_IDS
from database import (
    create_giveaway,
    create_giveaway_notification_snapshot,
    get_active_automated_giveaway,
    get_due_giveaways,
    get_giveaway_by_automation_key,
    get_giveaway_by_id,
    get_giveaways_ready_to_report,
    get_pending_giveaway_notifications,
    get_unrewarded_giveaway_winners,
    mark_giveaway_results_notified,
    mark_giveaway_reward_attempt,
    mark_giveaway_winner_rewarded,
    pick_giveaway_winners,
    set_giveaway_notification_status,
    get_user_keys,
)

logger = logging.getLogger(__name__)

MONTHLY_PRIZE_DAYS = 30
MONTHLY_WINNERS = 1
GIVEAWAY_LENGTH_DAYS = 7
ANNOUNCEMENT_BATCH = 50
REWARD_RETRY_SECONDS = 15 * 60


def _month_key(now: datetime | None = None) -> tuple[str, str]:
    now = now or (datetime.now(timezone.utc) + timedelta(hours=3))
    month_names = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    return now.strftime("%Y-%m"), f"{month_names[now.month - 1]} {now.year}"


def _moscow_date(timestamp: int) -> str:
    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y в %H:%M МСК")


async def _notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.exception("Could not notify admin %s about giveaway automation", admin_id)


async def ensure_monthly_giveaway(bot: Bot) -> dict:
    """Create at most one auto campaign per month, without overlapping campaigns."""
    month, month_label = _month_key()
    key = f"auto-monthly-{month}"
    existing = await get_giveaway_by_automation_key(key)
    if existing:
        return existing

    active = await get_active_automated_giveaway()
    if active:
        return active

    end_date = int(time.time()) + GIVEAWAY_LENGTH_DAYS * 86400
    giveaway_id = await create_giveaway(
        title=f"Розыгрыш ByMeVPN · {month_label}",
        description="Ежемесячный розыгрыш подписки ByMeVPN",
        prize_days=MONTHLY_PRIZE_DAYS,
        winners_count=MONTHLY_WINNERS,
        end_date=end_date,
        created_by=0,
        automation_key=key,
    )
    giveaway = await get_giveaway_by_id(giveaway_id)
    if not giveaway:
        raise RuntimeError(f"Could not load automatically created giveaway {giveaway_id}")

    queued = await create_giveaway_notification_snapshot(giveaway_id)
    await _notify_admins(
        bot,
        f"🎁 Создан ежемесячный розыгрыш <b>#{giveaway_id}</b>.\n"
        f"Приз: +{MONTHLY_PRIZE_DAYS} дней · победителей: {MONTHLY_WINNERS}\n"
        f"Участие до: {_moscow_date(end_date)}\n"
        f"Уведомлений в очереди: {queued}.",
    )
    logger.info("Created monthly giveaway %s (id=%s, queued=%s)", month, giveaway_id, queued)
    return giveaway


async def _send_campaign_announcements(bot: Bot, giveaway: dict) -> None:
    users = await get_pending_giveaway_notifications(giveaway["id"], limit=ANNOUNCEMENT_BATCH)
    if not users:
        return
    info = await bot.get_me()
    link = f"https://t.me/{info.username}?start=gw_{giveaway['id']}"
    text = (
        "🎁 <b>Розыгрыш ByMeVPN</b>\n\n"
        f"Можно выиграть <b>+{giveaway['prize_days']} дней VPN</b>.\n"
        "Участие бесплатное. Бот случайно выберет победителя и автоматически начислит приз.\n\n"
        f"Участвуйте до <b>{_moscow_date(giveaway['end_date'])}</b>."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Участвовать", url=link, style="primary")],
        [InlineKeyboardButton(text="Не присылать акции", callback_data="marketing_optout")],
    ])
    for user_id in users:
        try:
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=keyboard)
            await set_giveaway_notification_status(giveaway["id"], user_id, "sent")
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            await set_giveaway_notification_status(giveaway["id"], user_id, "blocked")
        except Exception:
            # Keep transient failures pending for a later scheduler pass.
            logger.exception("Giveaway announcement failed for user %s", user_id)
            break


async def process_due_giveaways(bot: Bot) -> None:
    """Draw once, retry prize delivery safely, then report completed results."""
    for giveaway in await get_due_giveaways():
        winners = await pick_giveaway_winners(giveaway["id"])
        logger.info("Giveaway %s draw completed with %s winner(s)", giveaway["id"], len(winners))

    for reward in await get_unrewarded_giveaway_winners(REWARD_RETRY_SECONDS):
        giveaway_id = reward["giveaway_id"]
        user_id = reward["user_id"]
        await mark_giveaway_reward_attempt(giveaway_id, user_id)
        try:
            from subscription import deliver_key
            keys = await get_user_keys(user_id)
            latest = max(keys, key=lambda key: key.get("expiry", 0)) if keys else None
            success = await deliver_key(
                bot=bot,
                user_id=user_id,
                chat_id=user_id,
                config_name=f"ByMeVPN_{user_id}",
                days=int(reward["prize_days"]),
                limit_ip=latest.get("limit_ip", 2) if latest else 2,
                is_paid=True,
                amount=0,
                method="giveaway_prize",
                payload=f"giveaway_{giveaway_id}_{user_id}",
                extend_existing=True,
            )
            if success:
                await mark_giveaway_winner_rewarded(giveaway_id, user_id)
            else:
                logger.warning("Giveaway prize pending retry: giveaway=%s user=%s", giveaway_id, user_id)
        except Exception:
            logger.exception("Giveaway prize delivery failed: giveaway=%s user=%s", giveaway_id, user_id)

    for result in await get_giveaways_ready_to_report():
        winner_ids = result["winner_ids"]
        if winner_ids:
            winner_lines = "\n".join(f"• <code>{user_id}</code>" for user_id in winner_ids)
            text = (
                f"🏆 Розыгрыш <b>#{result['id']} {escape(result['title'])}</b> завершён.\n"
                f"Приз: +{result['prize_days']} дней. Победитель(и):\n{winner_lines}\n"
                "Призы начислены автоматически."
            )
        else:
            text = f"ℹ️ Розыгрыш <b>#{result['id']} {escape(result['title'])}</b> завершён без участников; приз не выдавался."
        await _notify_admins(bot, text)
        await mark_giveaway_results_notified(result["id"])


async def run_giveaway_automation(bot: Bot) -> None:
    """One idempotent scheduler pass; safe to retry after process restarts."""
    from referral_system_new import process_pending_referral_payment_rewards
    await process_pending_referral_payment_rewards(bot)
    await process_due_giveaways(bot)
    giveaway = await ensure_monthly_giveaway(bot)
    # Snapshot is INSERT OR IGNORE, so restarting won't duplicate messages.
    await create_giveaway_notification_snapshot(giveaway["id"])
    await _send_campaign_announcements(bot, giveaway)
    await process_due_giveaways(bot)
