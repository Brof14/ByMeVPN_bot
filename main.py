"""
ByMeVPN Bot — async Telegram bot for VPN sales.
Stack: aiogram 3, aiosqlite, httpx
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database import init_db
from notifications import start_notification_scheduler
from handlers import (
    start_router,
    buy_router,
    keys_router,
    partner_router,
    guide_router,
    legal_router,
    admin_router,
    fallback_router,  # MUST be last
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set in .env")
        sys.exit(1)

    await init_db()
    logger.info("Database ready")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Register routers in order — fallback MUST be last
    dp.include_router(start_router)
    dp.include_router(buy_router)
    dp.include_router(keys_router)
    dp.include_router(partner_router)
    dp.include_router(guide_router)
    dp.include_router(legal_router)
    dp.include_router(admin_router)
    dp.include_router(fallback_router)

    logger.info("All routers registered")

    await bot.delete_webhook(drop_pending_updates=True)

    scheduler_task = asyncio.create_task(start_notification_scheduler(bot))
    logger.info("Bot is running. Press Ctrl+C to stop.")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler_task.cancel()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
