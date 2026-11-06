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
from utils import safe_answer, send_with_photo
from bot_style import themed_button as InlineKeyboardButton
from bot_assets import REFERRALS, MY_INVITATIONS

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
    text, kb = await _partner_program_screen(message.from_user.id, message.from_user.username)
    await send_with_photo(message.bot, message, text, kb, photo=REFERRALS)


async def _partner_program_screen(user_id: int, username: str | None):
    await ensure_user(user_id, username)
    stats = await get_referral_stats_with_free_days(user_id)
    from referral_system_new import get_referral_link
    referral_link = await get_referral_link(user_id, "ByMeVPN_bot")

    text = (
        "<b>Бонусы за друзей</b>\n\n"
        "+15 дней — когда друг впервые запустит бота по вашей ссылке.\n"
        "+30 дней — после его первой подходящей оплаты.\n\n"
        f"<b>Ваша статистика</b>\n"
        f"Переходы: <b>{stats.get('clicks', 0)}</b> · регистрации: <b>{stats['total']}</b>\n"
        f"Оплатили: <b>{stats['paid']}</b> · начислено: <b>{stats.get('free_days', 0)} дней</b>\n\n"
        f"<b>Ваша ссылка</b>\n<code>{referral_link}</code>\n\n"
        "Бонусы продлевают подписку. Денежных начислений нет."
    )
    
    # Create keyboard
    from aiogram.types import InlineKeyboardMarkup
    from urllib.parse import quote_plus
    
    share_text = quote_plus(
        "Попробуй ByMeVPN — бот поможет подключиться за несколько шагов.\n\n"
        "🎁 3 дня бесплатно для новых пользователей\n"
        "📱 Тарифы для нескольких устройств\n\n"
        f"Подключиться: {referral_link}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Поделиться ссылкой",
            url=f"https://t.me/share/url?url={quote_plus(referral_link)}&text={share_text}"
        )],
        [InlineKeyboardButton(
            text="Мои приглашения",
            callback_data="my_referrals_list"
        )],
        [InlineKeyboardButton(
            text="← В главное меню",
            callback_data="back_to_menu"
        )]
    ])
    
    return text, kb


@router.callback_query(F.data == "my_referrals_list")
async def cb_my_referrals_list(callback: CallbackQuery):
    """Show list of referred users."""
    await safe_answer(callback)
    
    user_id = callback.from_user.id
    
    # Get referral events
    from database import get_referral_events
    events = await get_referral_events(user_id, limit=20)
    
    if not events:
        text = "<b>Мои приглашения</b>\n\nПока никто не зарегистрировался по вашей ссылке."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="partner_program")]
        ])
        await send_with_photo(callback.bot, callback, text, kb, photo=MY_INVITATIONS)
        return
    
    text = "<b>Мои приглашения</b>\n\n"
    
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
    
    await send_with_photo(callback.bot, callback, text, kb, photo=MY_INVITATIONS)


@router.callback_query(F.data == "partner_program")
async def cb_partner_program(callback: CallbackQuery):
    """Return to partner program."""
    await safe_answer(callback)
    text, kb = await _partner_program_screen(callback.from_user.id, callback.from_user.username)
    await send_with_photo(callback.bot, callback, text, kb, photo=REFERRALS)
