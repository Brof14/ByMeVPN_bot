"""
Background expiry notification scheduler.

This module handles automated notifications for subscription expiry reminders
and automatic promo code generation for renewals.
Also handles daily database backup at 03:00.
"""
import asyncio
import logging
import random
import string
import time
import zipfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import BufferedInputFile

from config import ADMIN_ID, DB_FILE
from database import get_keys_nearing_expiry, create_promo_code, validate_promo_code

logger = logging.getLogger(__name__)


def get_day_word(days: int) -> str:
    """Return correct form of 'day' word in Russian."""
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20):
        return "дня"
    else:
        return "дней"


def generate_promo_code(length: int = 5) -> str:
    """Generate a short random promo code."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


async def get_or_create_renewal_promo(user_id: int) -> str:
    """
    Get existing renewal promo code for user or create a new one.

    Creates a short 30% discount promo code valid for 7 days with multiple uses.
    Uses a shared code approach: checks for existing active promo codes and reuses them.
    """
    # Try to find existing active 30% promo codes that are valid
    from database import get_all_promo_codes
    promo_codes = await get_all_promo_codes()

    # Look for an existing 30% discount promo code that's active and valid
    for promo in promo_codes:
        if (promo["is_active"] and
            promo["promo_type"] == "percent" and
            promo["discount_value"] == 30 and
            promo["uses_count"] < promo["max_uses"]):
            # Check if still valid
            import time
            if promo["expires_at"] > int(time.time()):
                return promo["code"]

    # No suitable existing promo code, create a new one
    # Short code (5 chars), 30% discount, 7 days validity, 10 uses
    code = generate_promo_code(5)

    success = await create_promo_code(
        code=code,
        promo_type="percent",
        discount_value=30,
        max_uses=10,
        valid_days=7
    )

    if success:
        logger.info("Created shared renewal promo code %s (30%%, 10 uses, 7 days)", code)
        return code
    else:
        # Fallback with different code if creation fails
        code = generate_promo_code(6)
        await create_promo_code(
            code=code,
            promo_type="percent",
            discount_value=30,
            max_uses=10,
            valid_days=7
        )
        return code


async def _send_urgent_notification(bot: Bot, item: dict) -> None:
    """Send urgent notification for keys expiring in 1-3 days."""
    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    promo_code = await get_or_create_renewal_promo(item["user_id"])

    text = (
        f"🚨 <b>СРОЧНО! Ваша подписка истекает!</b>\n\n"
        f"📅 Дата окончания: <b>{date_str}</b>\n"
        f"🔔 Осталось: <b>{days_left} {get_day_word(days_left)}</b>\n\n"
        f"⚠️ <b>ВНИМАНИЕ:</b> После истечения срока вы потеряете доступ к:\n"
        f"• YouTube и все видео\n"
        f"• Telegram и мессенджеры\n"
        f"• Социальные сети\n"
        f"• Все заблокированные сайты\n\n"
        f"🎁 <b>СПЕЦИАЛЬНОЕ ПРЕДЛОЖЕНИЕ:</b>\n"
        f"Используйте промокод <code>{promo_code}</code> для получения <b>30% СКИДКИ</b> на продление!\n"
        f"Промокод действителен 7 дней.\n\n"
        f"💰 <b>Экономия:</b>\n"
        f"• 1 месяц: сэкономите ~30 ₽\n"
        f"• 3 месяца: сэкономите ~70 ₽\n"
        f"• 6 месяцев: сэкономите ~120 ₽\n"
        f"• 12 месяцев: сэкономите ~210 ₽\n\n"
        f"⏰ <b>Не откладывайте!</b> Продлите прямо сейчас, чтобы сохранить доступ."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать промокод", callback_data=f"activate_promo:{promo_code}")],
        [InlineKeyboardButton(text="🔄 Продлить сейчас", callback_data="buy_vpn")],
        [InlineKeyboardButton(text="🎁 Пригласить друга и получить +15 дней", callback_data="partner")]
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)


async def _send_warning_notification(bot: Bot, item: dict) -> None:
    """Send warning notification for keys expiring in 7-14 days."""
    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    promo_code = await get_or_create_renewal_promo(item["user_id"])

    text = (
        f"⏳ <b>Напоминание о продлении подписки</b>\n\n"
        f"📅 Дата окончания: <b>{date_str}</b>\n"
        f"🔔 Осталось: <b>{days_left} {get_day_word(days_left)}</b>\n\n"
        f"🎁 <b>Ваш эксклюзивный промокод:</b>\n"
        f"<code>{promo_code}</code> — <b>30% СКИДКА</b> на продление!\n"
        f"Действителен 7 дней.\n\n"
        f"💡 <b>Почему стоит продлить сейчас?</b>\n"
        f"• Гарантированный доступ без перерывов\n"
        f"• Стабильная скорость работы\n"
        f"• Поддержка всех устройств\n"
        f"• Сэкономьте с промокодом!\n\n"
        f"🤝 <b>Партнёрская программа:</b> Приглашайте друзей и получайте +15 дней за каждого!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать промокод", callback_data=f"activate_promo:{promo_code}")],
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_vpn")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="partner")]
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)


async def _send_early_notification(bot: Bot, item: dict) -> None:
    """Send early notification for keys expiring in 21-30 days."""
    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    text = (
        f"📢 <b>Информация о вашей подписке</b>\n\n"
        f"📅 Дата окончания: <b>{date_str}</b>\n"
        f"🔔 Осталось: <b>{days_left} {get_day_word(days_left)}</b>\n\n"
        f"✅ <b>Ваша подписка активна!</b>\n"
        f"Продлите заранее, чтобы избежать перерывов в работе.\n\n"
        f"💡 <b>Совет:</b> Чем дольше срок подписки, тем меньше цена за месяц!\n"
        f"• 12 месяцев: всего 59 ₽/мес\n"
        f"• 6 месяцев: всего 69 ₽/мес\n"
        f"• 3 месяца: всего 79 ₽/мес\n\n"
        f"🎁 <b>Скоро:</b> Приближается дата продления — мы пришлём вам промокод на скидку!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_vpn")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="partner")]
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)


async def _send_expiry_notifications(bot: Bot) -> None:
    """Send all expiry notifications based on days remaining."""
    # Urgent: 1-3 days remaining
    urgent_keys = await get_keys_nearing_expiry(days_min=1, days_max=3)
    for item in urgent_keys:
        try:
            await _send_urgent_notification(bot, item)
            logger.info("Sent urgent notification to user %d", item["user_id"])
        except Exception as e:
            logger.debug("Urgent notification error for user %d: %s", item["user_id"], e)

    # Warning: 7-14 days remaining
    warning_keys = await get_keys_nearing_expiry(days_min=7, days_max=14)
    for item in warning_keys:
        try:
            await _send_warning_notification(bot, item)
            logger.info("Sent warning notification to user %d", item["user_id"])
        except Exception as e:
            logger.debug("Warning notification error for user %d: %s", item["user_id"], e)

    # Early: 21-30 days remaining
    early_keys = await get_keys_nearing_expiry(days_min=21, days_max=30)
    for item in early_keys:
        try:
            await _send_early_notification(bot, item)
            logger.info("Sent early notification to user %d", item["user_id"])
        except Exception as e:
            logger.debug("Early notification error for user %d: %s", item["user_id"], e)


async def _backup_database(bot: Bot) -> None:
    """Create a backup of the database and send it to admin."""
    try:
        db_path = Path(DB_FILE)
        if not db_path.exists():
            logger.warning("Database file not found: %s", DB_FILE)
            return

        # Create backup filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"vpnbot_backup_{timestamp}.zip"
        backup_path = Path(backup_filename)

        # Create zip archive
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(db_path, db_path.name)

        logger.info("Database backup created: %s", backup_filename)

        # Send backup to admin
        with open(backup_path, 'rb') as backup_file:
            backup_data = backup_file.read()

        backup_file_obj = BufferedInputFile(
            backup_data,
            filename=backup_filename
        )

        await bot.send_document(
            ADMIN_ID,
            backup_file_obj,
            caption=f"📦 <b>Database Backup</b>\n\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="HTML"
        )

        logger.info("Database backup sent to admin")

        # Clean up backup file
        backup_path.unlink()
        logger.info("Backup file deleted: %s", backup_filename)

    except Exception as e:
        logger.error("Database backup failed: %s", e)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Database Backup Failed</b>\n\nError: {str(e)}",
                parse_mode="HTML"
            )
        except Exception as notify_error:
            logger.error("Failed to notify admin about backup failure: %s", notify_error)


async def _wait_until_03_00() -> None:
    """
    Wait until the next 03:00.

    FIX: this used to compute "tomorrow" via target.replace(day=target.day+1),
    which raises ValueError on the last day of a month (e.g. day=32 in
    January). Because backup_scheduler()'s while-loop has no try/except
    around this call, that exception would silently kill the entire backup
    task (asyncio.create_task with nobody awaiting/checking it swallows the
    traceback) — backups would just stop happening with no error logged,
    starting on the first run that landed on a month boundary.
    """
    now = datetime.now()
    target = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now >= target:
        # Already past 03:00 today — wait until 03:00 tomorrow.
        # timedelta correctly rolls over month/year boundaries, unlike
        # manually incrementing the `day` field.
        from datetime import timedelta
        target = target + timedelta(days=1)
    seconds_until_target = (target - now).total_seconds()
    logger.info("Waiting until 03:00 (%d seconds)", int(seconds_until_target))
    await asyncio.sleep(seconds_until_target)


async def start_notification_scheduler(bot: Bot) -> None:
    """Run expiry notifications once per day at ~10:00 and backup at 03:00."""
    logger.info("Notification scheduler started")

    # Start backup scheduler as separate task
    async def backup_scheduler():
        while True:
            try:
                await _wait_until_03_00()
                await _backup_database(bot)
            except Exception as e:
                # FIX: without this try/except, any unhandled exception here
                # (network error, disk full, etc.) would kill this background
                # task permanently with no retry and no visible error.
                logger.error("Backup scheduler error: %s", e)
            # Wait 24 hours before next backup attempt
            await asyncio.sleep(86400)

    asyncio.create_task(backup_scheduler())

    # Main notification scheduler
    while True:
        try:
            await _send_expiry_notifications(bot)
            logger.info("Daily notifications completed")
        except Exception as e:
            logger.error("Scheduler error: %s", e)
        # Wait 24 hours using asyncio.sleep (non-blocking)
        await asyncio.sleep(86400)
