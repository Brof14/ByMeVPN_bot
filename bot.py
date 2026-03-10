"""
Main bot file for ByMeVPN
Contains bot initialization, dispatcher setup, and router registration
"""

import asyncio
import logging
import sys
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database.requests import init_db
from handlers import start_router, payment_router, admin_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


async def main():
    """Main bot function"""
    # Initialize bot with default properties
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            protect_content=False
        )
    )

    # Initialize dispatcher with memory storage
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Initialize database
    try:
        await init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return

    # Register routers
    dp.include_router(start_router)
    dp.include_router(payment_router)
    dp.include_router(admin_router)

    # Delete webhook if exists
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted successfully")
    except TelegramAPIError as e:
        logger.warning(f"Failed to delete webhook: {e}")

    # Start polling
    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(
            bot,
            handle_signals=False,
            allowed_updates=["message", "callback_query", "pre_checkout_query", "successful_payment"]
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot error: {e}")
    finally:
        # Close bot session
        with suppress(Exception):
            await bot.session.close()
        logger.info("Bot session closed")


def run_bot():
    """Entry point for running the bot"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Critical error: {e}")
        print(f"\n❌ Критическая ошибка: {e}")


if __name__ == "__main__":
    run_bot()
