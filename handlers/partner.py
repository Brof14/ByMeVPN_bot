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
        "Попробуй ByMeVPN — бот подскажет, как подключиться через Happ.\n\n"
        "🎁 Новым пользователям — 3 дня за 1 ₽; если способ оплаты сохранён, затем 89 ₽ за 30 дней. Автопродление можно отключить.\n"
        "📱 Тарифы на 2, 5 или 10 устройств.\n"
        f"Ссылка: {link}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться ссылкой", url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}", style="primary")],
        [InlineKeyboardButton(text="👥 Мои приглашения", callback_data="partner_referrals_list")],
        [InlineKeyboardButton(text="📣 VPN за рекламу", callback_data="ad_program")],
        [InlineKeyboardButton(text="← Назад в меню", callback_data="back_to_menu")],
    ])


@router.callback_query(F.data == "partner")
async def cb_partner(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    stats = await get_referral_stats_enhanced(user_id)
    text = (
        "🎁 <b>Бесплатные дни за приглашения</b>\n\n"
        "Поделитесь ссылкой и получите:\n"
        "• <b>15 дней</b> за регистрацию нового пользователя;\n"
        "• ещё <b>30 дней</b>, когда он впервые оплатит подписку.\n\n"
        "Бонус добавится к вашему ключу; если ключа ещё нет, бот создаст отдельный бонусный. "
        "Дни можно накапливать и пользоваться VPN без оплаты. "
        "Для получения бонусов карта и её привязка не нужны.\n\n"
        f"<b>Ваша статистика:</b> переходы — {stats['total_clicks']} · регистрации — {stats['total_registrations']} · "
        f"оплатили — {stats['paid_clients']} · начислено — {stats['total_bonus_days']} дн.\n\n"
        "<b>Ваша ссылка:</b>\n"
        f"<code>{link}</code>"
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
