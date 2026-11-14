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
from constants import format_timestamp
from bot_style import themed_button as InlineKeyboardButton

router = Router()


def referral_share_url(link: str) -> str:
    """One consistent, editable friend-to-friend invitation everywhere."""
    text = (
        "Попробуй ByMeVPN — VPN для телефона и компьютера.\n\n"
        "3 дня бесплатно, без привязки карты.\n"
        "Если зарегистрируешься по моей ссылке, мне начислят бонусные дни."
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
    await safe_answer(callback)
    user_id = callback.from_user.id
    bot_info = await get_cached_bot_info(bot)
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    from referral_system_new import retry_pending_signup_bonuses
    pending_rewards = await retry_pending_signup_bonuses(bot, user_id)
    stats = await get_referral_stats_enhanced(user_id)
    registrations = int(stats.get("total_registrations", 0))
    paid_referrals = int(stats.get("paid_clients", 0))
    bonus_days = int(stats.get("total_bonus_days", 0))
    pending_note = (
        f"\n\nБонус за {pending_rewards} приглашённого пока не начислен. "
        "Начисление сохранено — откройте этот раздел позже, и мы повторим попытку."
        if pending_rewards else ""
    )
    text = (
        "<b>Пригласите друга — получите до 45 дней</b>\n\n"
        "<b>+15 дней</b> — за его регистрацию по вашей ссылке.\n"
        "<b>+30 дней</b> — за первую успешную оплату любого тарифа.\n\n"
        "<b>Ваши результаты</b>\n"
        f"Приглашено: <b>{registrations}</b>\n"
        f"Оплатили: <b>{paid_referrals}</b>\n"
        f"Получено: <b>{bonus_days} дней</b>\n\n"
        "Отправьте ссылку другу — бонусы начисляются автоматически. "
        "Каждый друг может принести до 45 дней доступа."
        f"{pending_note}"
    )
    await send_with_photo(bot, callback, text, partner_main_kb(link), photo=REFERRALS)


@router.callback_query(F.data.startswith("partner_referrals_list"))
async def cb_partner_referrals_list(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    referred_users = await get_referred_users_list(callback.from_user.id)
    parts = (callback.data or "").split(":")
    try:
        requested_page = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        requested_page = 0
    page_size = 5
    total = len(referred_users)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(requested_page, pages - 1))

    if not referred_users:
        text = "<b>Мои приглашения</b>\n\nПриглашений пока нет. Отправьте другу свою ссылку."
    else:
        lines = [
            "<b>Мои приглашения</b>",
            f"Всего приглашено: <b>{total}</b>",
            f"Страница {page + 1} из {pages}",
        ]
        for index, user in enumerate(referred_users[page * page_size:(page + 1) * page_size], page * page_size + 1):
            days = max(0, int(user.get("bonus_days_awarded") or 0))
            lines.append(
                f"\n{index}. {format_timestamp(user['registration_date'])[:10]}"
                f"\nНачислено: <b>+{days} дней</b>"
            )
        text = "\n".join(lines)
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Ранее", callback_data=f"partner_referrals_list:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"partner_referrals_list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton(text="← К приглашениям", callback_data="partner")],
        [InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, InlineKeyboardMarkup(inline_keyboard=rows), photo=MY_INVITATIONS)
