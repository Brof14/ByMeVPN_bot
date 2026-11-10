"""
ByMeVPN Telegram Bot - Main Entry Point

This is the main entry point for the VPN bot. It initializes all components
including the bot, database, routers, webhook server, and starts polling.

Components:
- Telegram Bot (aiogram)
- Database (SQLite via aiosqlite)
- 3x-UI Integration (for VPN key management)
- YooKassa Webhook Server (for payment processing)
- Notification Scheduler (for expiry reminders)
"""

import html
import asyncio
import logging
import sys
import traceback
import time

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent

from config import BOT_TOKEN, BOT_INSTANCE_MODE, ADMIN_ID, REDIS_URL
from database import init_db, close_db
from notifications import start_notification_scheduler
from device_monitor import start_device_monitor
from xui_client import get_api_client  # stub for 3x-ui (no pre-init needed)
from async_utils import preload_static_data
from background_tasks import spawn_supervised, cancel_background_tasks

from handlers import (
    start_router, buy_router, keys_router, partner_router,
    guide_router, legal_router, admin_router, auth_router, fallback_router, ad_program_router
)
from subscription import router as subscription_router
from webhook import start_webhook_server, start_connection_tls_server
from payment_monitor import start_payment_monitor
from handlers.marketing import router as marketing_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Reduce noise from external libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.INFO)

logger = logging.getLogger(__name__)


class UserProfileMiddleware(BaseMiddleware):
    """Keep Telegram usernames fresh for every user who interacts with the bot."""

    async def __call__(self, handler, event, data):
        started = time.perf_counter()
        failed = False
        user = getattr(event, "from_user", None)
        if user is not None:
            try:
                from database import ensure_user, is_account_tombstoned
                # A removed user must not enter purchase/trial/referral flows:
                # the deleted identity and VPN access cannot be re-created by
                # a late Telegram callback or a new invoice.
                if await is_account_tombstoned(user.id):
                    # A Stars successful-payment update can arrive after
                    # account erasure. Allow only this financial event through
                    # so the payment handler can record it for manual refund.
                    if getattr(event, "successful_payment", None) is not None:
                        return await handler(event, data)
                    from aiogram.types import CallbackQuery
                    if isinstance(event, CallbackQuery):
                        await event.answer(
                            "Аккаунт удалён. Новые оплаты недоступны. Обратитесь в поддержку.",
                            show_alert=True,
                        )
                    else:
                        await event.answer(
                            "Аккаунт удалён. Повторная активация через бота недоступна. "
                            "Если нужна помощь, обратитесь в @ByMeVPNSupportBot."
                        )
                    return None
                await ensure_user(user.id, user.username)
            except Exception:
                logger.exception("Could not check Telegram account state for user %s", user.id)
                # If account state cannot be verified, do not risk creating
                # a paid checkout against an already deleted identity.
                try:
                    await event.answer("Сервис временно недоступен. Повторите попытку позже.")
                except Exception:
                    pass
                return None
        try:
            return await handler(event, data)
        except Exception:
            failed = True
            raise
        finally:
            from async_utils import perf_metrics
            perf_metrics.observe("telegram_update", time.perf_counter() - started, failed)


def format_error_messages(exc: BaseException, update=None) -> list[str]:
    """
    Format exception details into HTML-safe messages for Telegram admin.
    Returns a list of messages (split if exceeding Telegram length limits).
    Brief error cause is first, followed by traceback chunks.
    """
    exc_type = type(exc).__name__
    exc_msg = str(exc)

    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb_text = "".join(tb_lines)

    update_info = "Unknown"
    if update is not None:
        user_id = None
        upd_type = getattr(update, "event_type", "update")
        if getattr(update, "message", None) and getattr(update.message, "from_user", None):
            user_id = update.message.from_user.id
        elif getattr(update, "callback_query", None) and getattr(update.callback_query, "from_user", None):
            user_id = update.callback_query.from_user.id
        elif getattr(update, "pre_checkout_query", None) and getattr(update.pre_checkout_query, "from_user", None):
            user_id = update.pre_checkout_query.from_user.id

        upd_id = getattr(update, "update_id", "N/A")
        update_info = f"ID={upd_id}, Type={upd_type}"
        if user_id:
            update_info += f", User={user_id}"

    brief_msg = exc_msg[:1000] + ("..." if len(exc_msg) > 1000 else "")
    safe_type = html.escape(exc_type)
    safe_msg = html.escape(brief_msg)
    safe_update = html.escape(update_info)

    header = (
        f"🚨 <b>Bot Error</b>\n\n"
        f"<b>Exception:</b> <code>{safe_type}</code>\n"
        f"<b>Message:</b> <code>{safe_msg}</code>\n"
        f"<b>Event:</b> <code>{safe_update}</code>"
    )

    safe_tb = html.escape(tb_text)
    full_msg = f"{header}\n\n<b>Traceback:</b>\n<pre><code>{safe_tb}</code></pre>"
    if len(full_msg) <= 3800:
        return [full_msg]

    messages = [header]
    # Chunk raw traceback by lines or slice, then html.escape each chunk
    chunk_size = 3000
    for i in range(0, len(tb_text), chunk_size):
        chunk = tb_text[i:i + chunk_size]
        prefix = "<b>Traceback:</b>\n" if i == 0 else "<b>Traceback (cont.):</b>\n"
        messages.append(f"{prefix}<pre><code>{html.escape(chunk)}</code></pre>")

    return messages


async def error_handler(event: ErrorEvent, bot: Bot) -> None:
    """
    Global error handler for aiogram.
    Sends formatted error cause and full traceback to admin.
    """
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower():
        # Telegram reports an error for idempotent edits; no user or admin
        # action is needed, and these should not be surfaced as bot failures.
        logger.debug("Ignored Telegram no-op message edit")
        return

    logger.error("Bot error [%s]: %s", type(exc).__name__, exc, exc_info=exc)

    try:
        if not ADMIN_ID:
            logger.warning("ADMIN_ID not configured, skipping admin error alert")
            return

        messages = format_error_messages(exc, getattr(event, "update", None))
        for msg in messages:
            await bot.send_message(
                ADMIN_ID,
                msg,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error("Failed to notify admin about error: %s", e)


async def main() -> None:
    """
    Main bot initialization and startup function.

    This function performs the following steps:
    1. Validates BOT_TOKEN configuration
    2. Preloads static data for fast responses
    3. Initializes the database
    4. Tests 3x-UI connection
    5. Creates bot instance and dispatcher
    6. Registers all message handlers (routers)
    7. Starts background tasks (scheduler, webhook)
    8. Begins polling for Telegram updates
    9. Handles graceful shutdown

    Raises:
        SystemExit: If BOT_TOKEN is not configured
    """
    # Validate configuration
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set in .env")
        sys.exit(1)

    # Preload static data for instant responses
    await preload_static_data()
    logger.info("Static data preloaded")

    # Initialize database
    await init_db()
    logger.info("Database ready")

    # Test 3x-ui connection
    from xui_client import test_xui_connection
    xui_connected, xui_message = await test_xui_connection()
    if xui_connected:
        logger.info("3x-ui connection: %s", xui_message)
    else:
        logger.error("3x-ui connection failed: %s", xui_message)

    # Create bot instance with HTML parse mode
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=RedisStorage.from_url(REDIS_URL))
    dp.message.outer_middleware(UserProfileMiddleware())
    dp.callback_query.outer_middleware(UserProfileMiddleware())

    # Register global error handler
    dp.error.register(error_handler)

    # Register all routers (message handlers)
    routers = [
        start_router, buy_router, subscription_router, keys_router,
        partner_router, guide_router, legal_router,
        admin_router, auth_router, ad_program_router, marketing_router, fallback_router
    ]

    for router in routers:
        dp.include_router(router)

    logger.info("All routers registered")

    if BOT_INSTANCE_MODE == "primary":
        # Singleton work belongs to the primary instance only. A second bot
        # may share the database and Redis, but must not duplicate billing,
        # notification, device-monitor, webhook, or broadcast workers.
        spawn_supervised("notification_scheduler", lambda: start_notification_scheduler(bot), bot)
        spawn_supervised("device_monitor", lambda: start_device_monitor(bot), bot)
        spawn_supervised("webhook_server", lambda: start_webhook_server(bot, dp), bot)
        from config import APP_CONNECT_TLS_CERT_FILE, APP_CONNECT_TLS_KEY_FILE
        if APP_CONNECT_TLS_CERT_FILE and APP_CONNECT_TLS_KEY_FILE:
            spawn_supervised("connection_tls_server", start_connection_tls_server, bot)
        await start_payment_monitor(bot)

        from autorenew import start_autorenew_worker
        spawn_supervised("autorenew_worker", lambda: start_autorenew_worker(bot), bot)
        from broadcast_worker import start_broadcast_worker
        spawn_supervised("broadcast_worker", lambda: start_broadcast_worker(bot), bot)

        async def performance_sampler():
            from async_utils import perf_metrics
            from database import save_performance_snapshot
            previous = {}
            while True:
                await asyncio.sleep(60)
                current = perf_metrics.snapshot()
                interval = {}
                for name, values in current.items():
                    old = previous.get(name, {})
                    interval[name] = {
                        **values,
                        "calls": max(0, int(values["calls"]) - int(old.get("calls", 0))),
                        "errors": max(0, int(values["errors"]) - int(old.get("errors", 0))),
                    }
                await save_performance_snapshot(interval)
                previous = current

        spawn_supervised("performance_sampler", performance_sampler, bot)
    else:
        logger.info(
            "Bot instance mode=%s: polling enabled; singleton background services remain on the primary instance",
            BOT_INSTANCE_MODE,
        )

    logger.info("Bot is running in polling mode. Press Ctrl+C to stop.")

    # Keep the bot running (polling mode)
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        # Graceful shutdown
        await cancel_background_tasks()
        await dp.storage.close()
        await bot.session.close()
        await close_db()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
