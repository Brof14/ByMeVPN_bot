"""
Background expiry notification scheduler.

This module handles automated notifications for subscription expiry reminders
and automatic promo code generation for renewals.
Also handles daily database backup at 03:00.
"""
import asyncio
import io
import logging
from contextlib import closing
import secrets
import sqlite3
import tempfile
import string
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup,
)
from aiogram.types import BufferedInputFile

from config import ADMIN_ID, DB_FILE
from database import (
    get_keys_nearing_expiry, create_promo_code, validate_promo_code,
    update_key_last_notification, is_promotional_opted_out,
    get_due_connection_reminders, update_connection_reminder,
    retry_connection_reminder,
    claim_expiry_notification, finish_expiry_notification,
)
from bot_style import themed_button as InlineKeyboardButton

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
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_or_create_renewal_promo(user_id: int) -> str:
    """
    Get existing renewal promo code for user or create a new one.

    Creates a short 30% discount promo code valid for 7 days with multiple uses.
    Uses a shared code approach: checks for existing active promo codes and reuses them.
    """
    # Try to find existing active 30% promo codes that are valid
    from database import get_all_promo_codes, has_user_used_promo
    promo_codes = await get_all_promo_codes()
    now = int(time.time())

    # Look for an existing 30% discount promo code that's active and valid
    for promo in promo_codes:
        if (promo["is_active"] and
            promo["promo_type"] == "percent" and
            promo["discount_value"] == 30 and
            promo["uses_count"] < promo["max_uses"] and
            promo["tariff_binding"] is None and
            promo["start_date"] is not None and
            promo["start_date"] <= now < promo["expires_at"]):
            if not await has_user_used_promo(promo["code"], user_id):
                return promo["code"]

    # Keep existing discount and usage limits, but retry code collisions rather
    # than returning a code the database failed to create.
    for attempt in range(5):
        code = generate_promo_code(5 + min(attempt, 2))
        if await create_promo_code(
            code=code,
            promo_type="percent",
            discount_value=30,
            max_uses=10,
            valid_days=7,
        ):
            logger.info("Created shared renewal promo code (30%%, 10 uses, 7 days)")
            return code
    raise RuntimeError("Could not create a unique renewal promo code after 5 attempts")


async def _get_active_autorenew(user_id: int, key_id: int | None) -> dict | None:
    """Find auto-renew for this key, including applicable legacy rows."""
    if not key_id:
        return None
    try:
        from database import get_auto_renew_subscription
        sub = await get_auto_renew_subscription(user_id, key_id, include_legacy=True)
        if sub and sub.get("status") == "active" and (sub.get("payment_method_id") or "").strip():
            return sub
    except Exception as e:
        logger.debug("autorenew lookup failed for %s: %s", user_id, e)
    return None


async def _send_autorenew_upcoming(bot: Bot, item: dict, sub: dict) -> None:
    """Pre-charge notice: auto-renew is ON — nothing to do, no promo spam.

    This also serves as the trial-ending warning: trial keys have an
    auto-renew row with the real recurring price (89 ₽ / 30 days).
    """
    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time()) + 86399) / 86400))
    amount = sub.get("amount_rub") or 89
    devices = sub.get("devices") or 2
    pm_title = sub.get("payment_method_title") or {
        "sbp": "счёт СБП",
        "bank_card": "банковская карта",
    }.get((sub.get("payment_method_type") or "").lower(), "сохранённый способ оплаты")

    text = (
        f"<b>Скоро автоматическое продление</b>\n\n"
        f"Дата списания: <b>{date_str}</b> (через {days_left} {get_day_word(days_left)})\n"
        f"Сумма: <b>{amount} ₽</b> за следующие {sub.get('days') or 30} дней ({devices} устр.)\n"
        f"Способ оплаты: {pm_title}\n\n"
        f"Ничего делать не нужно — доступ продлится автоматически.\n"
        f"Если продление не нужно, отключите его в настройках ключа — "
        f"доступ сохранится до конца оплаченного периода."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписка", callback_data="my_keys")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/ByMeVPNSupportBot")],
    ])
    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)
    return True


def _renewal_callback(item: dict) -> str:
    """Preserve the expiring key and its device tier when renewing from a reminder."""
    try:
        key_id = int(item.get("key_id") or 0)
    except (TypeError, ValueError):
        key_id = 0
    return f"key_renew:{key_id}" if key_id > 0 else "buy_vpn"


async def _optional_renewal_promo(user_id: int) -> tuple[str | None, dict | None]:
    """An optional marketing offer must never block the service reminder."""
    if await is_promotional_opted_out(user_id):
        return None, None
    try:
        code = await get_or_create_renewal_promo(user_id)
        promo = await validate_promo_code(code) if code else None
        return code, promo
    except Exception:
        logger.exception("Optional renewal promotion unavailable for user %d", user_id)
        return None, None


async def _send_urgent_notification(bot: Bot, item: dict) -> None:
    """Send urgent notification for keys expiring in 1-3 days."""
    sub = await _get_active_autorenew(item["user_id"], item.get("key_id"))
    if sub:
        return await _send_autorenew_upcoming(bot, item, sub)

    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    promo_code, promo = await _optional_renewal_promo(item["user_id"])
    promo_expiry = datetime.fromtimestamp(promo["expires_at"]).strftime("%d.%m.%Y") if promo else None
    text = (
        f"<b>До окончания подписки — {days_left} {get_day_word(days_left)}</b>\n\n"
        f"Доступ активен до <b>{date_str}</b>. Продлите подписку, чтобы пользоваться VPN и дальше.\n\n"
        + (f"🎁 Промокод <code>{promo_code}</code> даёт <b>30% скидки</b> до {promo_expiry}.\n\n" if promo_expiry else "")
        + "Откройте тарифы, чтобы выбрать срок и лимит устройств."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продлить подписку", callback_data=_renewal_callback(item), )],
        [InlineKeyboardButton(text="Получить VPN бесплатно", callback_data="free_vpn")],
        *([[InlineKeyboardButton(text="Применить промокод", callback_data=f"activate_promo:{promo_code}")]] if promo_expiry else []),
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)
    return True


async def _send_warning_notification(bot: Bot, item: dict) -> None:
    """Send warning notification for keys expiring in 7-14 days."""
    sub = await _get_active_autorenew(item["user_id"], item.get("key_id"))
    if sub:
        # Keep renewal messages to one clear reminder in the final days.
        return False

    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    promo_code, promo = await _optional_renewal_promo(item["user_id"])
    promo_expiry = datetime.fromtimestamp(promo["expires_at"]).strftime("%d.%m.%Y") if promo else None
    text = (
        f"<b>Подписка активна до {date_str}</b>\n"
        f"Осталось {days_left} {get_day_word(days_left)}. Продлите заранее, чтобы сохранить непрерывный доступ. Ключи и настройки останутся прежними.\n\n"
        + (f"🎁 Промокод <code>{promo_code}</code> — <b>30% скидки</b> до {promo_expiry}.\n\n" if promo_expiry else "")
        + "Откройте тарифы, чтобы выбрать срок и лимит устройств."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продлить подписку", callback_data=_renewal_callback(item), )],
        [InlineKeyboardButton(text="Получить VPN бесплатно", callback_data="free_vpn")],
        *([[InlineKeyboardButton(text="Применить промокод", callback_data=f"activate_promo:{promo_code}")]] if promo_expiry else []),
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)
    return True


async def _send_early_notification(bot: Bot, item: dict) -> None:
    """Send early notification for keys expiring in 21-30 days."""
    sub = await _get_active_autorenew(item["user_id"], item.get("key_id"))
    if sub:
        # An auto-renewing customer only gets the pre-charge reminder close to billing.
        return False

    date_str = datetime.fromtimestamp(item["expiry"]).strftime("%d.%m.%Y")
    days_left = max(1, int((item["expiry"] - int(time.time())) / 86400))

    text = (
        f"<b>Подписка активна ещё {days_left} {get_day_word(days_left)}</b>\n\n"
        f"Доступ завершится {date_str}. Откройте тарифы, чтобы выбрать подходящий срок."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть тарифы", callback_data=_renewal_callback(item), )],
        [InlineKeyboardButton(text="Получить VPN бесплатно", callback_data="free_vpn")]
    ])

    await bot.send_message(item["user_id"], text, parse_mode="HTML", reply_markup=kb)
    return True


async def _send_expiry_notifications(bot: Bot) -> None:
    """Send expiry reminders with bounded concurrency and durable deduplication."""
    import time as _time
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from async_utils import perf_metrics
    
    current_time = int(_time.time())
    
    # Minimum interval between notifications (in seconds)
    # Urgent (1-3 days): once per 2 days
    # Warning (7-14 days): once per 10 days
    # Early (21-30 days): once per 25 days
    URGENT_INTERVAL = 2 * 86400      # 2 days
    WARNING_INTERVAL = 10 * 86400    # 10 days
    EARLY_INTERVAL = 25 * 86400      # 25 days
    
    batches = [
        ("urgent", URGENT_INTERVAL, await get_keys_nearing_expiry(days_min=1, days_max=3), _send_urgent_notification),
        ("warning", WARNING_INTERVAL, await get_keys_nearing_expiry(days_min=7, days_max=14), _send_warning_notification),
        ("early", EARLY_INTERVAL, await get_keys_nearing_expiry(days_min=21, days_max=30), _send_early_notification),
    ]
    # One reminder per user per daily pass; the nearest expiry / highest urgency wins.
    candidates = {}
    priority = {"urgent": 0, "warning": 1, "early": 2}
    for bucket, interval, rows, sender in batches:
        for item in rows:
            prior = candidates.get(item["user_id"])
            if prior is None or priority[bucket] < priority[prior[0]]:
                candidates[item["user_id"]] = (bucket, interval, item, sender)

    semaphore = asyncio.Semaphore(8)

    async def deliver(user_id, candidate):
        bucket, interval, item, sender = candidate
        last_notif = item.get("last_notification_at", 0)
        if last_notif and current_time - last_notif < interval:
            return
        if not await claim_expiry_notification(user_id, item["expiry"], bucket):
            return
        started = _time.perf_counter()
        status = "uncertain"
        try:
            async with semaphore:
                perf_metrics.observe("notification_queue_wait", _time.perf_counter() - started)
                try:
                    sent = await sender(bot, item)
                except TelegramRetryAfter as error:
                    await asyncio.sleep(min(float(error.retry_after), 90))
                    sent = await sender(bot, item)
            status = "sent" if sent else "skipped"
            if sent:
                await update_key_last_notification(user_id, item["expiry"])
        except TelegramForbiddenError:
            status = "blocked"
            logger.info("Expiry reminder blocked by user %d", user_id)
        except TelegramRetryAfter:
            status = "failed"
            logger.warning("Expiry reminder rate limited twice; will retry later for user %d", user_id)
        except Exception:
            # A timeout can happen after Telegram accepted the message. Never blindly resend.
            logger.exception("Expiry reminder delivery uncertain: user=%d bucket=%s", user_id, bucket)
        finally:
            await finish_expiry_notification(user_id, item["expiry"], bucket, status)
            perf_metrics.observe("expiry_notification_dispatch", _time.perf_counter() - started, status == "uncertain")

    await asyncio.gather(*(deliver(uid, candidate) for uid, candidate in candidates.items()))


def _verified_sqlite_backup_zip(source: Path) -> bytes:
    """Snapshot a live WAL database before compressing it.

    Directly zipping the .db file can omit transactions still in its -wal file.
    SQLite's online backup API includes committed WAL data and yields a
    consistent snapshot without suspending the bot.
    """
    if not source.is_file():
        raise FileNotFoundError(source)
    with tempfile.TemporaryDirectory(prefix="bymevpn-sqlite-backup-") as tmp:
        snapshot = Path(tmp) / source.name
        # sqlite3.Connection's context manager does not close connections.
        # Explicit closing avoids locked temporary files on Windows.
        with closing(sqlite3.connect(
            f"file:{source.resolve()}?mode=ro", uri=True, timeout=30
        )) as original, closing(sqlite3.connect(snapshot, timeout=30)) as target:
            original.backup(target, pages=256, sleep=0.1)
            check = target.execute("PRAGMA integrity_check").fetchone()
            if check != ("ok",):
                raise RuntimeError("SQLite backup integrity check failed")

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, arcname=source.name)
        return out.getvalue()


async def _backup_database(bot: Bot) -> None:
    """Send a verified online SQLite snapshot, never a live database file."""
    try:
        db_path = Path(DB_FILE)
        if not db_path.is_file():
            logger.warning("Database file not found: %s", DB_FILE)
            return
        # SQLite backup is synchronous. Keep database snapshot and ZIP work
        # outside the bot's event loop so Telegram callbacks remain responsive.
        archive_bytes = await asyncio.to_thread(_verified_sqlite_backup_zip, db_path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        await bot.send_document(
            ADMIN_ID,
            BufferedInputFile(archive_bytes, filename=f"vpnbot_backup_{stamp}.zip"),
            caption=f"📦 <b>Database Backup</b>\n\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="HTML",
        )
        logger.info("Verified SQLite backup sent to admin")
    except Exception as exc:
        logger.error("Database backup failed (%s)", type(exc).__name__)
        try:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ <b>Database Backup Failed</b>\n\n"
                "Не удалось создать или отправить проверенную резервную копию. "
                "Проверьте журналы сервера.",
                parse_mode="HTML",
            )
        except Exception as notify_error:
            logger.error("Could not report database backup failure (%s)", type(notify_error).__name__)


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


async def _wait_until_10_00_moscow() -> None:
    """Run expiry reminders at 10:00 Moscow time, not immediately on restart."""
    now = datetime.now(timezone.utc) + timedelta(hours=3)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    seconds_until_target = (target - now).total_seconds()
    logger.info("Waiting until 10:00 Moscow time (%d seconds)", int(seconds_until_target))
    await asyncio.sleep(seconds_until_target)


async def _record_verified_first_connection(bot: Bot, item: dict, signal: str) -> None:
    """Record verified 3x-ui activity once, without an immediate marketing push.

    Connection is proof that a tunnel worked, not proof that the customer
    recommends the product. Referral outreach is handled separately after
    at least 24 hours of actual opportunity to use the service.
    """
    from database import log_analytics_event_once
    await update_connection_reminder(item["key_id"], "connected")
    await log_analytics_event_once(
        item["user_id"], "first_connection", f"3x-ui {signal} observed"
    )


async def _referral_nudge_pass(bot: Bot) -> None:
    """At-most-once, post-connection offer; respects opt-outs and active access."""
    from database import get_due_referral_nudges, log_analytics_event_once, log_analytics_event

    for user_id in await get_due_referral_nudges(limit=10):
        # The preference may have changed after selecting the cohort.
        if await is_promotional_opted_out(user_id):
            continue
        # Mark before contacting Telegram: if the network times out after the
        # message was accepted, retrying would send unwanted duplicate ads.
        if not await log_analytics_event_once(user_id, "referral_nudge_attempted"):
            continue
        try:
            await bot.send_message(
                user_id,
                "<b>Если ByMeVPN оказался полезен</b>\n\n"
                "Можете пригласить друга и получить бонусные дни: "
                "+15 дней за регистрацию и +30 дней за его первую подходящую оплату.\n\n"
                "Это необязательно.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Пригласить друга", callback_data="partner")],
                    [InlineKeyboardButton(text="Отключить предложения", callback_data="marketing_optout")],
                ]),
            )
            await log_analytics_event(user_id, "referral_nudge_sent")
        except Exception as exc:
            logger.info(
                "Referral suggestion unavailable for user %d (%s)",
                user_id, type(exc).__name__,
            )


async def _connection_reminder_pass(bot: Bot) -> None:
    """Process due one-time first-connection nudges without false positives."""
    from xui_client import get_xui_user, get_xui_connection_info

    for item in await get_due_connection_reminders(limit=25):
        key_id = item["key_id"]
        if item["expiry"] <= int(time.time()):
            await update_connection_reminder(key_id, "expired")
            continue

        # A missing response can mean an API outage or a missing panel client.
        # Keep the reminder pending and retry on a later scheduler pass.
        panel_user = await get_xui_user(item["user_id"])
        if panel_user is None or panel_user.get("used_traffic") is None:
            logger.warning("Deferring first-connection reminder: 3x-ui stats unavailable for key %s", key_id)
            continue

        if int(panel_user["used_traffic"]) > int(item["traffic_baseline"]):
            await _record_verified_first_connection(bot, item, "traffic")
            continue

        # A client can establish a tunnel before any payload traffic is counted.
        # Check the profile-level online signal as well before nudging.
        try:
            connection_info = await get_xui_connection_info(item["user_id"])
        except Exception as exc:
            logger.warning("Deferring first-connection reminder: online status unavailable for key %s: %s", key_id, exc)
            continue
        if connection_info.get("is_online"):
            await _record_verified_first_connection(bot, item, "online status")
            continue

        try:
            await bot.send_message(
                item["user_id"],
                "<b>Помочь подключить VPN?</b>\n\n"
                "По статистике ключ пока не использовался. Нажмите кнопку — подскажем, как добавить его в приложение.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Подключить VPN", callback_data=f"key_instructions:{key_id}")],
                ]),
            )
            await update_connection_reminder(key_id, "sent")
        except Exception as exc:
            logger.warning("Could not send first-connection reminder for key %s: %s", key_id, exc)
            await retry_connection_reminder(key_id, item["attempts"])


async def _connection_reminder_scheduler(bot: Bot) -> None:
    """Check due keys every five minutes; pending items survive restarts."""
    while True:
        try:
            await _connection_reminder_pass(bot)
            # Optional marketing is sent only during daytime in Moscow.
            moscow_hour = (datetime.now(timezone.utc) + timedelta(hours=3)).hour
            if 10 <= moscow_hour < 20:
                await _referral_nudge_pass(bot)
        except Exception:
            logger.exception("First-connection reminder pass failed")
        await asyncio.sleep(300)


async def start_notification_scheduler(bot: Bot) -> None:
    """Run expiry notifications once per day at ~10:00 and backup at 03:00."""
    logger.info("Notification scheduler started")

    async def giveaway_scheduler():
        from giveaway_automation import run_giveaway_automation
        while True:
            try:
                await run_giveaway_automation(bot)
            except Exception as e:
                logger.exception("Giveaway automation pass failed: %s", e)
            await asyncio.sleep(60)

    from background_tasks import spawn_supervised
    spawn_supervised("giveaway_scheduler", giveaway_scheduler, bot)
    spawn_supervised(
        "connection_reminder_scheduler",
        lambda: _connection_reminder_scheduler(bot),
        bot,
    )

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

    spawn_supervised("database_backup_scheduler", backup_scheduler, bot)

    # Do not send renewal reminders as a side effect of every container restart.
    # The daily cadence is anchored to 10:00 Moscow time.
    await _wait_until_10_00_moscow()

    # Main notification scheduler
    while True:
        try:
            await _send_expiry_notifications(bot)
            logger.info("Daily notifications completed")
        except Exception as e:
            logger.error("Scheduler error: %s", e)
        # Wait 24 hours using asyncio.sleep (non-blocking)
        await asyncio.sleep(86400)
