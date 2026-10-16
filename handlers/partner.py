"""Customer-facing referral program. Rewards are VPN days only."""
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_referral_stats_enhanced, get_referred_users_list
from utils import send_with_photo, safe_answer
from constants import format_timestamp

router = Router()


def partner_main_kb(link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus(
        "Попробуй ByMeVPN — подключение через приложение и короткую инструкцию.\n\n"
        "3 дня за 1 ₽, затем 89 ₽ за 30 дней при включённом автопродлении. Условия видны до оплаты.\n"
        f"Ссылка: {link}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться ссылкой", url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}", style="primary")],
        [InlineKeyboardButton(text="👥 Мои приглашения", callback_data="partner_referrals_list")],
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
        "<b>👥 VPN за приглашения</b>\n\n"
        "Делитесь ссылкой и получайте бонусные дни VPN.\n\n"
        "<b>Ваши бонусы</b>\n"
        "• <b>+15 дней</b> — за регистрацию нового пользователя.\n"
        "• <b>Ещё +30 дней</b> — после его первой платной подписки. Бот пришлёт отдельную кнопку для получения бонуса.\n\n"
        "Бонусы продлевают ваш ключ или активируются на отдельном. Карта и оплата не нужны.\n\n"
        "<b>🔗 Ваша ссылка для приглашений</b>\n"
        f"<code>{link}</code>\n\n"
        "<b>Ваша статистика</b>\n"
        f"👥 Приглашено: <b>{stats['total_registrations']}</b>\n"
        f"🔗 Переходов: <b>{stats['total_clicks']}</b>\n"
        f"🎁 Начислено: <b>{stats['total_bonus_days']} дней</b>"
        f"{pending_note}"
    )
    await send_with_photo(bot, callback, text, partner_main_kb(link))


@router.callback_query(F.data == "partner_referrals_list")
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    referred_users = await get_referred_users_list(callback.from_user.id)
    if not referred_users:
        text = "<b>👥 Мои приглашения</b>\n\nПока никто не зарегистрировался по вашей ссылке."
    else:
        lines = ["<b>👥 Мои приглашения</b>"]
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
        [InlineKeyboardButton(text="◀️ К партнёрской программе", callback_data="partner")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb)
