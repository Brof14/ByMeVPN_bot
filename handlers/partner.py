"""Referral / partner program — with logo photo."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from database import get_referral_stats, ensure_user
from keyboards import partner_kb
from utils import send_with_photo, safe_answer

logger = logging.getLogger(__name__)
router = Router()

REF_BONUS_DAYS = 15  # days referrer gets per paid referral


@router.callback_query(F.data == "partner")
async def cb_partner(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    await ensure_user(user_id)

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"

    stats = await get_referral_stats(user_id)
    total = stats["total"]
    paid = stats["paid"]
    bonus_days = paid * REF_BONUS_DAYS

    text = (
        "👑 <b>Партнёрская программа ByMeVPN</b>\n\n"
        "💎 Зарабатывайте вместе с нами!\n\n"
        "🎁 <b>Ваша выгода:</b>\n"
        f"• +{REF_BONUS_DAYS} дней за каждого друга, оплатившего подписку\n"
        "• Без ограничений по количеству рефералов\n"
        "• Бонус начисляется автоматически\n\n"
        "🚀 <b>Как это работает:</b>\n"
        "1. Поделитесь своей уникальной ссылкой\n"
        "2. Друг переходит по ней и получает 3 дня бесплатно\n"
        "3. Когда он оформляет платную подписку — вы получаете бонус\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        f"👥 Приглашено: {total} человек\n"
        f"💳 Оплатили подписку: {paid} человек\n"
        f"🎉 Заработано бонусов: {bonus_days} дней\n\n"
        f"🔑 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{link}</code>\n\n"
        "💫 Начните зарабатывать прямо сейчас!"
    )
    await send_with_photo(bot, callback, text, partner_kb(link))
