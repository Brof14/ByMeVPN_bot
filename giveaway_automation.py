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
    requeue_giveaway_announcements,
    set_giveaway_notification_status,
    get_user_keys,
    update_automated_monthly_giveaway,
)

logger = logging.getLogger(__name__)

MONTHLY_PRIZE_DAYS = 30
MONTHLY_WINNERS = 3
ANNOUNCEMENT_BATCH = 50
REWARD_RETRY_SECONDS = 15 * 60


def _month_period(now: datetime | None = None) -> tuple[str, str, int, int]:
    now = now or (datetime.now(timezone.utc) + timedelta(hours=3))
    month_names = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    local_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if local_start.month == 12:
        local_end = local_start.replace(year=local_start.year + 1, month=1)
    else:
        local_end = local_start.replace(month=local_start.month + 1)
    utc_start = local_start - timedelta(hours=3)
    utc_end = local_end - timedelta(hours=3)
    return (
        now.strftime("%Y-%m"),
        f"{month_names[now.month - 1]} {now.year}",
        int(utc_start.replace(tzinfo=timezone.utc).timestamp()),
        int(utc_end.replace(tzinfo=timezone.utc).timestamp()),
    )


def _moscow_date(timestamp: int) -> str:
    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y в %H:%M МСК")


async def _notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.exception("Could not notify admin %s about giveaway automation", admin_id)


async def ensure_monthly_giveaway(bot: Bot) -> dict:
    """Create/update one calendar-month referral leaderboard."""
    month, month_label, period_start, period_end = _month_period()
    key = f"auto-monthly-{month}"
    existing = await get_giveaway_by_automation_key(key)
    if existing:
        changed = await update_automated_monthly_giveaway(
            existing["id"], f"Конкурс ByMeVPN · {month_label}", period_end,
            period_start, winners_count=MONTHLY_WINNERS, prize_days=MONTHLY_PRIZE_DAYS,
        )
        if changed:
            await requeue_giveaway_announcements(existing["id"])
            await _notify_admins(
                bot,
                f"📣 Обновлены условия конкурса <b>#{existing['id']}</b>: "
                "теперь в зачёт идут приглашённые за месяц, победителей — 3, приз каждому — +30 дней.",
            )
        return await get_giveaway_by_id(existing["id"])

    active = await get_active_automated_giveaway()
    if active:
        return active
    giveaway_id = await create_giveaway(
        title=f"Конкурс ByMeVPN · {month_label}",
        description="Ежемесячный конкурс приглашений",
        prize_days=MONTHLY_PRIZE_DAYS,
        winners_count=MONTHLY_WINNERS,
        end_date=period_end,
        created_by=0,
        automation_key=key,
        period_start=period_start,
    )
    giveaway = await get_giveaway_by_id(giveaway_id)
    if not giveaway:
        raise RuntimeError(f"Could not load automatically created giveaway {giveaway_id}")

    queued = await create_giveaway_notification_snapshot(giveaway_id)
    await _notify_admins(
        bot,
        f"🎁 Создан ежемесячный конкурс приглашений <b>#{giveaway_id}</b>.\n"
        f"Победителей: {MONTHLY_WINNERS} · каждому +{MONTHLY_PRIZE_DAYS} дней\n"
        f"Период: {_moscow_date(period_start)} — {_moscow_date(period_end - 1)}\n"
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
        "🏆 <b>Ежемесячный конкурс ByMeVPN</b>\n\n"
        "Если не грузят:\n"
        "• 🎥 YouTube\n• ✈️ Telegram\n• 📸 Instagram\n• 🌐 сайты\n"
        "— ByMeVPN поможет подключиться за минуту.\n\n"
        "<b>Как участвовать</b>\n"
        "1. Пригласи хотя бы одного нового пользователя по своей ссылке в этом месяце.\n"
        "2. Нажми «Участвовать» — бот проверит приглашение.\n\n"
        "В конце месяца определим <b>3 призовых места</b> по числу приглашённых. "
        "Если подходящих участников будет меньше трёх, приз получит каждый из них. "
        "При равном числе приглашённых место определит жребий. "
        f"Награда за место — <b>+{giveaway['prize_days']} дней VPN</b>. Итоги подведём автоматически; "
        f"победителей и бонусы объявим <b>{_moscow_date(giveaway['end_date'])}</b>.\n\n"
        f"Приглашения принимаются до <b>{_moscow_date(giveaway['end_date'] - 1)}</b>."
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
        winners = result["winners"]
        if winners:
            if result["is_referral_contest"]:
                winner_lines = "\n".join(
                    f"{winner['rank']}. <code>{winner['user_id']}</code> — "
                    f"{winner['referral_count']} приглашённых, +{result['prize_days']} дней"
                    for winner in winners
                )
                detail = "Топ участников по приглашённым за месяц:"
            else:
                winner_lines = "\n".join(f"• <code>{winner['user_id']}</code>" for winner in winners)
                detail = f"Победителям начислено +{result['prize_days']} дней:"
            text = (
                f"🏆 {'Конкурс' if result['is_referral_contest'] else 'Розыгрыш'} "
                f"<b>#{result['id']} {escape(result['title'])}</b> завершён.\n"
                f"{detail}\n{winner_lines}\n"
                "Призы начислены автоматически."
            )
        else:
            text = f"ℹ️ Конкурс <b>#{result['id']} {escape(result['title'])}</b> завершён без участников, выполнивших условие; призы не выдавались."
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
