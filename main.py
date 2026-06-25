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

import asyncio
import logging
import sys
import traceback

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from config import BOT_TOKEN, ADMIN_ID
from database import init_db, close_db
from notifications import start_notification_scheduler
from marzban_client import get_api_client  # stub for 3x-ui (no pre-init needed)
from async_utils import preload_static_data

from handlers import (
    start_router, buy_router, keys_router, partner_router,
    guide_router, legal_router, admin_router, auth_router, fallback_router
)
from subscription import router as subscription_router
from webhook import start_webhook_server
from payment_monitor import start_payment_monitor

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


async def error_handler(event: ErrorEvent, bot: Bot) -> None:
    """
    Global error handler for aiogram.
    Sends formatted traceback to admin on any exception.
    """
    error_text = traceback.format_exc()
    logger.error("Bot error: %s", error_text)

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>Bot Error</b>\n\n<code>{error_text[:3000]}</code>",
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
    from marzban_client import test_marzban_connection
    marzban_connected, marzban_message = await test_marzban_connection()
    if marzban_connected:
        logger.info("3x-ui connection: %s", marzban_message)
    else:
        logger.error("3x-ui connection failed: %s", marzban_message)

    # Create bot instance with HTML parse mode
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # Register global error handler
    dp.error.register(error_handler)

    # Register all routers (message handlers)
    routers = [
        start_router, buy_router, subscription_router, keys_router,
        partner_router, guide_router, legal_router,
        admin_router, auth_router, fallback_router
    ]

    for router in routers:
        dp.include_router(router)

    # Register global error handler
    dp.error.register(error_handler)

    logger.info("All routers registered")

    # Start background tasks
    scheduler_task = asyncio.create_task(start_notification_scheduler(bot))

    # Start automatic payment monitoring
    await start_payment_monitor(bot)

    logger.info("Bot is running in polling mode. Press Ctrl+C to stop.")

    # Keep the bot running (polling mode)
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        # Graceful shutdown
        scheduler_task.cancel()
        await bot.session.close()
        await close_db()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
