"""Customer-facing referral program. Rewards are VPN days only."""
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_referral_stats_enhanced, get_referred_users_list
from utils import send_with_photo, safe_answer
from constants import format_timestamp, INTRO_TRIAL_DAYS, INTRO_TRIAL_PRICE_RUB, RECURRING_MONTHLY_PRICE, RECURRING_DAYS

router = Router()


def partner_main_kb(link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus(
        "Попробуй ByMeVPN — VPN с простой настройкой и короткой инструкцией.\n\n"
        f"{INTRO_TRIAL_DAYS} дня за {INTRO_TRIAL_PRICE_RUB} ₽, затем {RECURRING_MONTHLY_PRICE} ₽ за {RECURRING_DAYS} дней, если включено автопродление. Условия видны до оплаты.\n"
        f"Ссылка: {link}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться ссылкой", url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}", style="primary")],
        [InlineKeyboardButton(text="Мои приглашения", callback_data="partner_referrals_list")],
        [InlineKeyboardButton(text="← Назад в меню", callback_data="back_to_menu")],
    ])


@router.callback_query(F.data == "partner")
async def cb_partner(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    from referral_system_new import retry_pending_signup_bonuses
    pending_rewards = await retry_pending_signup_bonuses(bot, user_id)
    stats = await get_referral_stats_enhanced(user_id)
    pending_note = (
        f"\n\n⚠️ Не удалось начислить бонус за регистрацию {pending_rewards} приглашённого. "
        "Начисление сохранено — откройте этот раздел позже, чтобы повторить попытку."
        if pending_rewards else ""
    )
    text = (
        "<b>VPN бесплатно за друзей</b>\n\n"
        "Приглашайте друзей и пользуйтесь VPN без оплаты и привязки карты.\n\n"
        "<b>Ваши бонусы</b>\n"
        "+15 дней — за регистрацию друга по вашей ссылке.\n"
        "+30 дней — после его первой подходящей платной подписки.\n\n"
        "Дни добавляются к вашему ключу. Если ключа нет, бот создаст его автоматически. Для бонуса за регистрацию карта не нужна.\n\n"
        "<b>Ваша ссылка для приглашений</b>\n"
        f"<code>{link}</code>\n\n"
        "<b>Ваши приглашения</b>\n"
        f"Зарегистрировались: <b>{stats['total_registrations']}</b>\n"
        f"Активных подписок: <b>{stats['active_clients']}</b>\n"
        f"Начислено: <b>{stats['total_bonus_days']} дней</b>\n"
        "Поделитесь ссылкой, чтобы получить первые бонусные дни."
        f"{pending_note}"
    )
    await send_with_photo(bot, callback, text, partner_main_kb(link))


@router.callback_query(F.data == "partner_referrals_list")
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    referred_users = await get_referred_users_list(callback.from_user.id)
    if not referred_users:
        text = "<b>Мои приглашения</b>\n\nПока никто не зарегистрировался по вашей ссылке."
    else:
        lines = ["<b>Мои приглашения</b>"]
        for i, user in enumerate(referred_users[:20], 1):
            status = "активна подписка" if user["is_active"] else "подписки нет или она закончилась"
            paid = "оплатил" if user["has_paid"] else "ещё не оплатил"
            lines.append(
                f"\n{i}. {format_timestamp(user['registration_date'])}\n"
                f"   Статус: {status}; {paid}\n"
                f"   Начислено: {user['bonus_days_awarded']} бонусных дней"
            )
        if len(referred_users) > 20:
            lines.append(f"\nПоказаны последние 20 из {len(referred_users)} приглашённых.")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К приглашениям", callback_data="partner")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb)
