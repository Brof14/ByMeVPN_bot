"""Customer-facing referral program. Rewards are VPN days only."""
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
)

from database import get_referral_stats_enhanced, get_referred_users_list
from utils import send_with_photo, safe_answer
from bot_assets import REFERRALS, MY_INVITATIONS
from constants import format_timestamp, INTRO_TRIAL_DAYS, INTRO_TRIAL_PRICE_RUB, RECURRING_MONTHLY_PRICE, RECURRING_DAYS
from bot_style import themed_button as InlineKeyboardButton

router = Router()


def partner_main_kb(link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus(
        "Попробуй ByMeVPN — VPN без сложных настроек.\n"
        "3 дня бесплатно, без карты."
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться", url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}", )],
        [InlineKeyboardButton(text="Скопировать ссылку", copy_text={"text": link})],
        [InlineKeyboardButton(text="Мои приглашения", callback_data="partner_referrals_list")],
        [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")],
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
    registrations = int(stats.get("total_registrations", 0))
    paid_referrals = int(stats.get("paid_clients", 0))
    bonus_days = int(stats.get("total_bonus_days", 0))
    pending_note = (
        f"\n\n⚠️ Не удалось начислить бонус за регистрацию {pending_rewards} приглашённого. "
        "Начисление сохранено — откройте этот раздел позже, чтобы повторить попытку."
        if pending_rewards else ""
    )
    text = (
        "<b>Приглашение друзей</b>\n\n"
        "+15 дней за регистрацию друга.\n"
        "+30 дней за его первую подходящую оплату.\n\n"
        f"Приглашено: <b>{registrations}</b> · оплатили: <b>{paid_referrals}</b>\n"
        f"Начислено или ожидает начисления: <b>{bonus_days} дней</b>"
        f"{pending_note}"
    )
    await send_with_photo(bot, callback, text, partner_main_kb(link), photo=REFERRALS)


@router.callback_query(F.data == "partner_referrals_list")
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    referred_users = await get_referred_users_list(callback.from_user.id)
    if not referred_users:
        text = "<b>Мои приглашения</b>\n\nПока нет приглашённых."
    else:
        lines = ["<b>Мои приглашения</b>"]
        for i, user in enumerate(referred_users[:20], 1):
            status = "Активен" if user["is_active"] else "Нет активной подписки"
            payment = "Оплатил" if user["has_paid"] else "Не оплатил"
            lines.append(
                f"\n{i}. {format_timestamp(user['registration_date'])}\n"
                f"   {status} · {payment}\n"
                f"   Начислено или ожидает начисления: <b>+{user['bonus_days_awarded']} дней</b>"
            )
        if len(referred_users) > 20:
            lines.append(f"\nПоказаны последние 20 из {len(referred_users)}.")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К приглашениям", callback_data="partner")],
        [InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb, photo=MY_INVITATIONS)
