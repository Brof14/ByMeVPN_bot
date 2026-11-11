"""Customer-facing referral program. Rewards are VPN days only."""
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
)

from database import get_referral_stats_enhanced, get_referred_users_list
from utils import send_with_photo, safe_answer, get_cached_bot_info
from bot_assets import REFERRALS, MY_INVITATIONS
from constants import format_timestamp, INTRO_TRIAL_DAYS, INTRO_TRIAL_PRICE_RUB, RECURRING_MONTHLY_PRICE, RECURRING_DAYS
from bot_style import themed_button as InlineKeyboardButton

router = Router()


def referral_share_url(link: str) -> str:
    """One consistent, editable friend-to-friend invitation everywhere."""
    text = (
        "Попробуй ByMeVPN — VPN для телефона и компьютера.\n\n"
        "3 дня бесплатно. По моей ссылке я получу +15 дней за твою регистрацию "
        "и ещё +30 дней за твою первую оплату."
    )
    return f"https://t.me/share/url?url={quote_plus(link)}&text={quote_plus(text)}"


def partner_main_kb(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пригласить друга", url=referral_share_url(link))],
        [InlineKeyboardButton(text="Скопировать ссылку", copy_text={"text": link})],
        [InlineKeyboardButton(text="Мои приглашения", callback_data="partner_referrals_list")],
        [InlineKeyboardButton(text="← Назад", callback_data="free_vpn")],
    ])


@router.callback_query(F.data == "partner")
async def cb_partner(callback: CallbackQuery, bot: Bot):
    """Send the personal referral link and reward terms without another menu."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    bot_info = await get_cached_bot_info(bot)
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    text = (
        "<b>Приглашайте друзей — получайте до 45 дней за каждого</b>\n\n"
        "+15 дней — за регистрацию друга по вашей ссылке.\n"
        "+30 дней — за его первую оплату.\n\n"
        "Ваша ссылка:\n"
        f"<code>{link}</code>\n\n"
        "Скопируйте её и отправьте другу."
    )
    await send_with_photo(bot, callback, text, photo=REFERRALS)


@router.callback_query(F.data == "partner_referrals_list")
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    referred_users = await get_referred_users_list(callback.from_user.id)
    if not referred_users:
        text = "<b>Мои приглашения</b>\n\nПока пусто. Отправьте свою ссылку другу, чтобы получить +15 дней."
    else:
        lines = ["<b>Мои приглашения</b>"]
        for i, user in enumerate(referred_users[:20], 1):
            status = "Активен" if user["is_active"] else "Нет активной подписки"
            payment = "Первая оплата получена" if user["has_paid"] else "Ожидается первая оплата"
            lines.append(
                f"\n{i}. {format_timestamp(user['registration_date'])}\n"
                f"   {status} · {payment}\n"
                f"   Бонусы за друга: <b>+{user['bonus_days_awarded']} дней</b>"
            )
        if len(referred_users) > 20:
            lines.append(f"\nПоказаны последние 20 из {len(referred_users)}.")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К приглашениям", callback_data="partner")],
        [InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb, photo=MY_INVITATIONS)
