"""Новая партнёрская программа: 80₽ за первую оплату приглашённого."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import get_referral_stats, get_referral_balance, create_payout_request, can_claim_payout
from keyboards import back_to_menu
from utils import send_with_photo, safe_answer
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)
router = Router()

class PayoutFlow(StatesGroup):
    choosing_amount = State()

# Константы партнёрской программы
REFERRAL_BONUS = 80  # рублей за первую оплату
MIN_PAYOUT = 200  # минимальная сумма вывода


def partner_main_kb(link: str) -> "InlineKeyboardMarkup":
    """Клавиатура партнёрской программы"""
    from urllib.parse import quote_plus

    share_text = quote_plus(
        "Если у тебя не работает YouTube / Telegram — вот решение.\n\n"
        "Сам пользуюсь — реально норм VPN.\n\n"
        "🎁 3 дня бесплатно (без карты)\n"
        "📱 До 5 устройств\n"
        "⚡ Всё открывается без лагов\n\n"
        "💰 От 39 ₽/мес\n\n"
        f"Попробуй:\n{link}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Поделиться ссылкой",
            url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}"
        )],
        [InlineKeyboardButton(text="📋 Список приглашённых", callback_data="partner_referrals_list")],
        [InlineKeyboardButton(text="Вывести средства", callback_data="payout_request")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
         InlineKeyboardButton(text="Поддержка", url="https://t.me/ByMeVPN_support_bot")]
    ])
    return kb


@router.callback_query(F.data == "partner")
async def cb_partner(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id

    # Получаем реферальную ссылку
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"

    # Получаем расширенную статистику
    from database import get_referral_stats_enhanced
    stats = await get_referral_stats_enhanced(user_id)

    text = (
        "Приглашайте друзей и получайте бонусы:\n\n"
        "🎁 +15 дней за переход по вашей ссылке\n"
        "💰 +30 дней за каждого, кто оформит платную подписку\n"
        "Минимальная сумма вывода — 200 рублей.\n\n"
        f"<b>📊 Ваша статистика:</b>\n"
        f"🔗 Переходов по ссылке: {stats['total_clicks']}\n"
        f"👤 Зарегистрировалось: {stats['total_registrations']}\n"
        f"✅ Активных клиентов: {stats['active_clients']}\n"
        f"💳 Оплативших: {stats['paid_clients']}\n"
        f"🎁 Бонусных дней: {stats['total_bonus_days']}\n"
        f"💰 Заработано (₽): {stats['total_earned_rub']}\n"
        f"💵 Текущий баланс: {stats['balance_rub']} ₽\n\n"
        f"<b>Ваша реферальная ссылка:</b>\n"
        f"<code>{link}</code>\n\n"
        "Бонус +15 дней начисляется сразу при переходе по ссылке. Бонус +30 дней - за первую оплату."
    )

    await send_with_photo(bot, callback, text, partner_main_kb(link))


@router.callback_query(F.data == "partner_referrals_list")
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id

    from database import get_referred_users_list
    from constants import format_timestamp

    referred_users = await get_referred_users_list(user_id)

    if not referred_users:
        text = (
            "<b>📋 Список приглашённых</b>\n\n"
            "У вас пока нет приглашённых пользователей.\n\n"
            "Поделитесь своей реферальной ссылкой, чтобы начать зарабатывать!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="partner")]
        ])
        await send_with_photo(bot, callback, text, kb)
        return

    text = "<b>📋 Список приглашённых</b>\n\n"
    for i, user in enumerate(referred_users[:20], 1):  # Show max 20 users
        status = "✅ Активен" if user["is_active"] else "❌ Не активен"
        paid_status = "💳 Оплатил" if user["has_paid"] else "🆓 Бесплатный"
        reg_date = format_timestamp(user["registration_date"])
        bonus_days = user["bonus_days_awarded"]

        text += (
            f"<b>{i}. User ID: {user['user_id']}</b>\n"
            f"   {status} | {paid_status}\n"
            f"   📅 Регистрация: {reg_date}\n"
            f"   🎁 Бонусных дней: {bonus_days}\n\n"
        )

    if len(referred_users) > 20:
        text += f"... и ещё {len(referred_users) - 20} пользователей\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="partner")]
    ])

    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data == "payout_request")
async def cb_payout_request(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    user_id = callback.from_user.id

    balance_info = await get_referral_balance(user_id)
    
    if balance_info["balance"] < MIN_PAYOUT:
        text = (
            f"<b>Вывод средств недоступен</b>\n\n"
            f"Ваш баланс: {balance_info['balance']} ₽\n"
            f"Минимальная сумма для вывода: {MIN_PAYOUT} ₽\n\n"
            f"Пригласите ещё друзей, чтобы накопить необходимую сумму!"
        )
        await send_with_photo(bot, callback, text, back_to_menu())
        return
    
    # Предлагаем варианты вывода
    possible_amounts = [200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700]
    available_amounts = [amt for amt in possible_amounts if amt <= balance_info["balance"]]

    kb_rows = []
    
    # Кнопки с суммами (по 2 в ряд)
    for i in range(0, len(available_amounts), 2):
        row = []
        if i < len(available_amounts):
            row.append(InlineKeyboardButton(
                text=f"{available_amounts[i]} ₽", 
                callback_data=f"payout_amount_{available_amounts[i]}"
            ))
        if i + 1 < len(available_amounts):
            row.append(InlineKeyboardButton(
                text=f"{available_amounts[i+1]} ₽", 
                callback_data=f"payout_amount_{available_amounts[i+1]}"
            ))
        if row:
            kb_rows.append(row)
    
    kb_rows.append([
        InlineKeyboardButton(text="Назад", callback_data="partner"),
        InlineKeyboardButton(text="Поддержка", url="https://t.me/ByMeVPN_support_bot")
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    text = (
        f"<b>Вывод средств</b>\n\n"
        f"Ваш текущий баланс: {balance_info['balance']} ₽\n\n"
        f"Выберите сумму для вывода (минимум {MIN_PAYOUT} ₽, кратно {REFERRAL_BONUS} ₽):"
    )
    
    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data.startswith("payout_amount_"))
async def cb_payout_amount(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    user_id = callback.from_user.id
    
    try:
        amount = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Неверная сумма", alert=True)
        return
    
    if not await can_claim_payout(user_id, amount):
        await safe_answer(callback, "Недостаточно средств или неверная сумма", alert=True)
        return
    
    # Создаём заявку на вывод
    try:
        payout_id = await create_payout_request(user_id, amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад к партнёрской программе", callback_data="partner"),
             InlineKeyboardButton(text="Поддержка", url="https://t.me/ByMeVPN_support_bot")]
        ])
        
        text = (
            f"<b>Заявка на вывод создана!</b>\n\n"
            f"Сумма: {amount} ₽\n"
            f"Номер заявки: #{payout_id}\n\n"
            f"Ваша заявка будет обработана в течение 24 часов.\n"
            f"Средства поступят на указанный вами способ оплаты.\n\n"
            f"По вопросам вывода обращайтесь в поддержку."
        )
        
        await send_with_photo(bot, callback, text, kb)
        
    except Exception as e:
        logger.error("Error creating payout request for user %d: %s", user_id, e)
        await safe_answer(callback, "Ошибка создания заявки. Попробуйте ещё раз.", alert=True)
