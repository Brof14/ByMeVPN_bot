"""
New Referral System Handlers with "Забрать подарок" functionality.
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from database import ensure_user
from referral_system_new import (
    process_referral_click, claim_referral_bonus, 
    get_referral_stats_with_free_days, validate_referral_link
)
from keyboards import main_menu_new_user, main_menu_existing
from utils import safe_answer, send_with_photo, LOGO_URL

router = Router()


@router.callback_query(F.data.startswith("claim_ref_bonus:"))
async def cb_claim_referral_bonus(callback: CallbackQuery):
    """Handle 'Забрать подарок' button click."""
    await safe_answer(callback)
    
    try:
        # Parse callback data: claim_ref_bonus:{referred_id}:{bonus_type}
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("Неверный формат данных", show_alert=True)
            return
        
        referred_id = int(parts[1])
        bonus_type = parts[2]
        referrer_id = callback.from_user.id
        
        # Claim the bonus
        success = await claim_referral_bonus(callback.bot, referrer_id, referred_id, bonus_type)
        
        if success:
            await callback.answer("Подарок активирован!", show_alert=True)
            # Update the message to remove the button
            await callback.message.edit_text(
                "Подарок активирован. Бонус добавлен к вашему ключу.",
                parse_mode="HTML"
            )
        else:
            await callback.answer("Не удалось активировать подарок", show_alert=True)
            
    except ValueError:
        await callback.answer("Ошибка в данных", show_alert=True)
    except Exception as e:
        await callback.answer("Произошла ошибка", show_alert=True)


@router.message(F.text == "Партнёрская программа")
async def cmd_partner_program(message: Message):
    """Show partner program with referral link and statistics."""
    user_id = message.from_user.id
    
    # Ensure user exists
    await ensure_user(user_id)
    
    # Get referral statistics
    stats = await get_referral_stats_with_free_days(user_id)
    
    # Generate referral link
    from referral_system_new import get_referral_link
    referral_link = await get_referral_link(user_id, "ByMeVPN_bot")  # Update with actual bot username
    
    text = (
        "Приглашайте друзей и получайте бонусы:\n\n"
        "🎁 +15 дней за переход по вашей ссылке\n"
        "💰 +30 дней за каждого, кто оформит платную подписку\n"
        "Минимальная сумма вывода — 200 рублей.\n\n"
        f"<b>📊 Ваша статистика:</b>\n"
        f"🔗 Переходов по ссылке: {stats.get('clicks', 0)}\n"
        f"👤 Зарегистрировалось: {stats['total']} человек\n"
        f"✅ Активных клиентов: {stats['total']}\n"
        f"💳 Оплативших: {stats['paid']} человек\n"
        f"🎁 Бонусных дней: {stats.get('free_days', 0)}\n"
        f"💰 Заработано (₽): {stats.get('total_earned', 0)}\n"
        f"💵 Текущий баланс: {stats.get('balance', 0)} ₽\n\n"
        f"<b>Ваша реферальная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "Бонус +15 дней начисляется сразу при переходе по ссылке. Бонус +30 дней - за первую оплату."
    )
    
    # Create keyboard
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from urllib.parse import quote_plus
    
    share_text = quote_plus(
        "Если у тебя не работает YouTube / Telegram — вот решение.\n\n"
        "Сам пользуюсь — реально норм VPN.\n\n"
        "🎁 3 дня бесплатно (без карты)\n"
        "📱 До 5 устройств\n"
        "⚡ Всё открывается без лагов\n\n"
        "💰 От 59 ₽/мес\n\n"
        f"Попробуй:\n{referral_link}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Поделиться ссылкой",
            url=f"https://t.me/share/url?url={quote_plus(referral_link)}&text={share_text}"
        )],
        [InlineKeyboardButton(
            text="Мои рефералы",
            callback_data="my_referrals_list"
        )],
        [InlineKeyboardButton(
            text="В главное меню",
            callback_data="back_to_menu"
        )]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "my_referrals_list")
async def cb_my_referrals_list(callback: CallbackQuery):
    """Show list of referred users."""
    await safe_answer(callback)
    
    user_id = callback.from_user.id
    
    # Get referral events
    from database import get_referral_events
    events = await get_referral_events(user_id, limit=20)
    
    if not events:
        text = "<b>Мои рефералы</b>\n\nУ вас пока нет рефералов."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="partner_program")]
        ])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return
    
    text = "<b>Мои рефералы</b>\n\n"
    
    from constants import format_timestamp
    for event in events:
        dt = format_timestamp(event["created"])
        
        event_type_text = {
            "trial_bonus": "Бонус за переход",
            "payment_bonus": "Бонус за оплату", 
            "trial_pending": "Ожидает активации",
            "registration": "Регистрация"
        }.get(event["event_type"], "Событие")
        
        status = "✅ Активирован" if event["days_awarded"] > 0 else "⏳ Ожидает"
        
        text += (
            f"ID: <code>{event['referred_id']}</code>\n"
            f"Тип: {event_type_text}\n"
            f"Дата: {dt}\n"
            f"Статус: {status}\n\n"
        )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить", callback_data="my_referrals_list")],
        [InlineKeyboardButton(text="Назад", callback_data="partner_program")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "partner_program")
async def cb_partner_program(callback: CallbackQuery):
    """Return to partner program."""
    await safe_answer(callback)
    
    # Reuse the partner program message handler
    await cmd_partner_program(callback.message)
