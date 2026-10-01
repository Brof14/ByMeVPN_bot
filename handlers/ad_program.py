"""
«VPN за рекламу» — Telegram-каналы.

Flow (spec #23-29):
  public channel  → send @username / t.me link → auto-detect via getChat →
                    subscriber count if available (bot membership NOT required —
                    otherwise user provides the count) → application.
  private channel → invite link (t.me/+... or t.me/c/...) → manual subscriber
                    count → application pending manual review.
  post            → user gets the ad post, publishes it, submits the post URL
                    (status post_submitted, 30-day retention clock starts).
  bonus           → granted ONLY by admin approval (never on application
                    creation). Admin reviews, approves → +N days on the user's
                    key. All admin actions are written to ad_audit_log.

Legacy status 'active_30_days' (older schema) is treated as post_submitted.
"""
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_IDS
from database import (
    create_ad_application, get_user_ad_applications, update_ad_application,
    get_ad_application_by_id, get_ad_applications_by_statuses,
    channel_already_used, get_active_ad_campaign, log_ad_notification,
    calculate_bonus_months, log_ad_analytics_event, log_ad_audit,
    get_user_keys, extend_key,
)
from utils import send_with_photo, safe_answer
from keyboards import back_to_menu

logger = logging.getLogger(__name__)
router = Router()


class AdProgramFlow(StatesGroup):
    waiting_channel = State()
    waiting_subscribers = State()
    waiting_post_url = State()
    waiting_rejection_reason = State()


# Bonus tiers (must match database.calculate_bonus_months)
BONUS_TIERS = (
    "1–49 подписчиков → 2 месяца\n"
    "50–99 → 4 месяца\n"
    "100–149 → 6 месяцев\n"
    "150–249 → 8 месяцев\n"
    "250–349 → 1 год\n"
    "350+ → 2 года + индивидуальные условия"
)

# Statuses that mean "user is working on the application" (only one allowed)
_ACTIVE_APP_STATUSES = ("draft", "channel_pending", "waiting_for_post", "post_submitted", "active_30_days")

STATUS_EMOJI = {
    "draft": "📝",
    "channel_pending": "⏳",
    "waiting_for_post": "📋",
    "post_submitted": "🔍",
    "active_30_days": "🔍",
    "approved": "✅",
    "rejected": "❌",
    "completed": "🎉",
}
STATUS_TEXT = {
    "draft": "Черновик",
    "channel_pending": "Ожидает проверки",
    "waiting_for_post": "Ожидает пост",
    "post_submitted": "Пост размещён, идёт проверка 30 дней",
    "active_30_days": "Пост размещён, идёт проверка 30 дней",
    "approved": "Одобрено",
    "rejected": "Отклонено",
    "completed": "Завершено",
}

# Public channel username patterns
_PUBLIC_PATTERNS = (
    re.compile(r"^@([A-Za-z0-9_]{4,64})$"),
    re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,64})/?$"),
)
# Private invite patterns: https://t.me/+Hh0x... or https://t.me/joinchat/... or t.me/c/1234/5
_PRIVATE_INVITE_RE = re.compile(r"^(?:https?://)?t\.me/(?:\+[\w-]{10,}|joinchat/[\w-]{10,}|c/\d+(?:/\d+)?)$")


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


@router.callback_query(F.data == "ad_program")
async def cb_ad_program(callback: CallbackQuery, bot: Bot):
    """Show main ad program screen."""
    await safe_answer(callback)
    user_id = callback.from_user.id

    try:
        campaign = await get_active_ad_campaign()
        await log_ad_notification(user_id, campaign["id"] if campaign else None)
    except Exception as e:
        logger.debug("ad notification log failed: %s", e)
    await log_ad_analytics_event(user_id, "ad_offer_view")

    text = (
        "📣 <b>VPN за рекламу</b>\n\n"
        "Разместите рекламный пост ByMeVPN\n"
        "в своём Telegram-канале на 30 дней —\n"
        "и получите VPN в подарок:\n\n"
        f"{BONUS_TIERS}\n\n"
        "<b>Как это работает:</b>\n"
        "1. Отправьте ссылку на ваш канал\n"
        "2. Получите готовый рекламный пост\n"
        "3. Опубликуйте его и отправьте ссылку на пост\n"
        "4. Через 30 дней мы проверим размещение и начислим бонус\n\n"
        "• Публичные каналы проверяются автоматически\n"
        "• Приватные каналы — по ссылке-приглашению, вручную\n"
        "• Один канал может участвовать только один раз"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Проверить мой канал", callback_data="ad_check_channel")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data == "ad_check_channel")
async def cb_ad_check_channel(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Start channel verification flow."""
    await safe_answer(callback)
    user_id = callback.from_user.id

    await log_ad_analytics_event(user_id, "ad_offer_click", {"action": "check_channel"})

    existing_apps = await get_user_ad_applications(user_id)
    pending_apps = [app for app in existing_apps if app["status"] in _ACTIVE_APP_STATUSES]

    if pending_apps:
        text = (
            "⚠️ <b>У вас уже есть активная заявка</b>\n\n"
            "Вы можете разместить рекламу только в одном канале одновременно.\n\n"
            "Пожалуйста, завершите текущую заявку или дождитесь её одобрения."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
        ])
        await send_with_photo(bot, callback, text, kb)
        return

    text = (
        "📣 <b>Проверка Telegram-канала</b>\n\n"
        "Отправьте ссылку на ваш канал:\n\n"
        "• Публичный: <code>@mychannel</code> или <code>https://t.me/mychannel</code>\n"
        "• Приватный: ссылка-приглашение <code>https://t.me/+abc123...</code>\n\n"
        "Канал найдём автоматически, добавлять бота в администраторы не нужно."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
    ])

    await send_with_photo(bot, callback, text, kb)
    await state.set_state(AdProgramFlow.waiting_channel)


def _parse_channel_input(channel_input: str) -> tuple[str | None, str | None]:
    """Parse user input → (public_username, private_invite_ref). Exactly one is set."""
    text = channel_input.strip()
    for pat in _PUBLIC_PATTERNS:
        m = pat.match(text)
        if m:
            return m.group(1), None
    if _PRIVATE_INVITE_RE.match(text):
        ref = text.split("t.me/", 1)[1]
        return None, ref
    return None, None


async def _ask_subscriber_count(bot: Bot, message: Message, state: FSMContext, prompt: str) -> None:
    await state.set_state(AdProgramFlow.waiting_subscribers)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
    ])
    await message.answer(prompt, reply_markup=kb)


@router.message(AdProgramFlow.waiting_subscribers)
async def msg_ad_subscriber_count(message: Message, bot: Bot, state: FSMContext):
    """Manual subscriber count (private channels / no bot membership)."""
    user_id = message.from_user.id
    data = await state.get_data()
    raw = (message.text or "").strip().replace(" ", "").replace("\u00a0", "")

    if not raw.isdigit():
        await message.answer(
            "❌ Пожалуйста, отправьте число подписчиков, например: <code>1284</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
            ])
        )
        return

    subscriber_count = int(raw)
    channel_ref = data.get("ad_channel_ref")
    channel_username = data.get("ad_channel_username") or ""
    channel_id = data.get("ad_channel_id")
    chat_title = data.get("ad_chat_title") or channel_username or "ваш канал"

    if not channel_ref:
        await message.answer("Ошибка заявки. Начните заново.", reply_markup=back_to_menu())
        await state.clear()
        return

    bonus_months = await calculate_bonus_months(subscriber_count)
    await state.clear()
    await _create_application_and_reply(
        bot, message, user_id,
        channel_id=channel_id,
        channel_username=channel_username,
        channel_ref=channel_ref,
        chat_title=chat_title,
        subscriber_count=subscriber_count,
        bonus_months=bonus_months,
        manual_count=True,
    )


async def _create_application_and_reply(
    bot: Bot, message: Message, user_id: int, *,
    channel_id, channel_username, channel_ref, chat_title,
    subscriber_count, bonus_months, manual_count: bool,
) -> None:
    # Duplicate channel guard (both running and historical approvals)
    if await channel_already_used(channel_id, channel_ref):
        text = (
            "⚠️ <b>Этот канал уже участвует в программе</b>\n\n"
            "Один канал может участвовать только один раз.\n\n"
            "Пожалуйста, используйте другой канал."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
        ])
        await message.answer(text, reply_markup=kb)
        return

    # 350+ needs individual agreement with the admin; everything else goes
    # straight to "publish the post" step.
    needs_review = subscriber_count >= 350 or manual_count
    status = "channel_pending" if needs_review else "waiting_for_post"

    campaign = await get_active_ad_campaign()
    campaign_id = campaign["id"] if campaign else None

    app_id = await create_ad_application(
        user_id=user_id,
        channel_id=channel_id,
        channel_username=channel_username,
        subscriber_count=subscriber_count,
        bonus_months=bonus_months,
        campaign_id=campaign_id,
        channel_ref=channel_ref,
        status=status,
    )

    await log_ad_analytics_event(user_id, "ad_application_submitted", {
        "app_id": app_id,
        "channel_ref": channel_ref,
        "subscriber_count": subscriber_count,
        "bonus_months": bonus_months,
        "manual_count": manual_count,
    })
    await log_ad_audit(user_id, "ad_application_created", f"app={app_id} ref={channel_ref} subs={subscriber_count}")

    if needs_review:
        text = (
            f"📣 <b>Заявка #{app_id} принята!</b>\n\n"
            f"Канал: <b>{chat_title}</b>\n"
            f"👥 Подписчиков: <b>{subscriber_count}</b>{' (указано вами)' if manual_count else ''}\n"
            f"🎁 Потенциальный бонус: <b>{bonus_months} мес.</b>\n\n"
            "Заявку проверит администратор. После подтверждения вы получите рекламный пост."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
        ])
    else:
        text = (
            f"📣 <b>Канал найден: {chat_title}</b>\n\n"
            f"👥 Подписчиков: <b>{subscriber_count}</b>\n"
            f"🎁 Ваш бонус: <b>{bonus_months} мес.</b>\n\n"
            "<b>Что дальше:</b>\n"
            "1. Получите рекламный пост\n"
            "2. Опубликуйте его в канале\n"
            "3. Отправьте ссылку на пост\n"
            "4. Пост должен находиться в канале 30 дней — после этого бонус начислят"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Получить рекламный пост", callback_data=f"ad_get_post:{app_id}")],
            [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
        ])

    await message.answer(text, reply_markup=kb)


@router.message(AdProgramFlow.waiting_channel)
async def msg_ad_channel_input(message: Message, bot: Bot, state: FSMContext):
    """Handle channel input from user."""
    user_id = message.from_user.id
    channel_input = (message.text or "").strip()

    public_username, private_ref = _parse_channel_input(channel_input)

    if not public_username and not private_ref:
        await message.answer(
            "❌ Неверный формат ссылки. Пожалуйста, отправьте @username, "
            "ссылку вида https://t.me/mychannel или ссылку-приглашение https://t.me/+...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
            ])
        )
        return

    if private_ref:
        # Private channel: no admin rights required, manual review path.
        await log_ad_analytics_event(user_id, "ad_channel_submitted", {"type": "private"})
        await _ask_subscriber_count(
            bot, message, state,
            prompt=(
                "🔒 <b>Приватный канал</b>\n\n"
                "Telegram не позволяет проверить его данные автоматически — "
                "заявку рассмотрит администратор вручную.\n\n"
                "Отправьте <b>примерное текущее число подписчиков</b> канала "
                "одним числом, например: <code>1284</code>"
            ),
        )
        await state.update_data(
            ad_channel_ref=private_ref.lower(),
            ad_channel_username="",
            ad_channel_id=None,
            ad_chat_title="приватный канал",
        )
        return

    # Public channel: verify via getChat (no admin rights needed)
    try:
        chat = await bot.get_chat(f"@{public_username}")
    except Exception as e:
        logger.info("get_chat failed for @%s: %s", public_username, e)
        await message.answer(
            "❌ Не удалось найти канал. Проверьте @username или ссылку и попробуйте снова.\n\n"
            "Если канал приватный — отправьте ссылку-приглашение (https://t.me/+...).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
            ])
        )
        return

    subscriber_count = None
    try:
        subscriber_count = await bot.get_chat_member_count(chat.id)
    except Exception:
        pass  # bot is not a member — that's fine, user will provide the count

    if subscriber_count is None:
        await state.update_data(
            ad_channel_ref=public_username.lower(),
            ad_channel_username=public_username,
            ad_channel_id=chat.id,
            ad_chat_title=chat.title or f"@{public_username}",
        )
        await _ask_subscriber_count(
            bot, message, state,
            prompt=(
                f"📣 <b>Канал найден: {chat.title or '@' + public_username}</b>\n\n"
                "Telegram не даёт боту увидеть число подписчиков (бот не участник канала).\n\n"
                "Отправьте <b>текущее число подписчиков</b> одним числом, "
                "например: <code>1284</code>\n"
                "Данные проверит администратор при одобрении."
            ),
        )
        return

    bonus_months = await calculate_bonus_months(subscriber_count)
    await state.clear()
    await _create_application_and_reply(
        bot, message, user_id,
        channel_id=chat.id,
        channel_username=public_username,
        channel_ref=public_username.lower(),
        chat_title=chat.title or f"@{public_username}",
        subscriber_count=subscriber_count,
        bonus_months=bonus_months,
        manual_count=False,
    )


@router.callback_query(F.data.startswith("ad_get_post:"))
async def cb_ad_get_post(callback: CallbackQuery, bot: Bot):
    """Show ad post template for user to copy."""
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])

    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заявка не найдена", alert=True)
        return

    text = (
        "📋 <b>Рекламный пост</b>\n\n"
        "Скопируйте текст ниже и разместите в своём канале:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔥 Не грузится YouTube, Telegram, Instagram или сайты?\n\n"
        "ByMeVPN — VPN, который просто работает.\n\n"
        "✅ Подключение за пару минут, без сложных настроек\n"
        "✅ Telegram, YouTube, сайты — всё открывается\n"
        "✅ Стабильная связь для видео и игр\n"
        "✅ Работает на iPhone, Android, Windows, macOS, Linux\n\n"
        "🎁 3 дня — за 1 ₽\n"
        "💳 Дальше — 89 ₽/мес, отключить автопродление можно в любой момент\n\n"
        "👇 Попробовать: https://t.me/ByMeVPN_bot\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "После размещения отправьте ссылку на пост."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить ссылку на пост", callback_data=f"ad_submit_post:{app_id}")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
    ])

    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data.startswith("ad_submit_post:"))
async def cb_ad_submit_post(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Start post URL submission flow."""
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])

    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заявка не найдена", alert=True)
        return

    text = (
        "📤 <b>Отправьте ссылку на рекламный пост</b>\n\n"
        "Ссылку на пост в вашем канале.\n\n"
        "Примеры:\n"
        "• <code>https://t.me/mychannel/123</code>\n"
        "• <code>https://t.me/c/1234567890/5</code> (приватный канал)"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"ad_get_post:{app_id}")]
    ])

    await send_with_photo(bot, callback, text, kb)
    await state.set_state(AdProgramFlow.waiting_post_url)
    await state.update_data(current_app_id=app_id)


@router.message(AdProgramFlow.waiting_post_url)
async def msg_ad_post_url(message: Message, bot: Bot, state: FSMContext):
    """Handle post URL submission."""
    user_id = message.from_user.id
    post_url = (message.text or "").strip()

    data = await state.get_data()
    app_id = data.get("current_app_id")

    if not app_id:
        await message.answer("Ошибка заявки. Попробуйте снова.", reply_markup=back_to_menu())
        await state.clear()
        return

    if "t.me/" not in post_url:
        await message.answer(
            "❌ Неверный формат ссылки. Пожалуйста, отправьте ссылку на пост "
            "в формате https://t.me/channel/123",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"ad_get_post:{app_id}")]
            ])
        )
        return

    await update_ad_application(app_id, status="post_submitted", post_url=post_url)
    await log_ad_analytics_event(user_id, "ad_post_submitted", {"app_id": app_id, "post_url": post_url})
    await log_ad_audit(user_id, "ad_post_submitted", f"app={app_id} url={post_url}")

    text = (
        "✅ <b>Ссылка на пост получена!</b>\n\n"
        "Пост должен оставаться в канале <b>30 дней</b>.\n\n"
        "Мы проверим размещение, и администратор начислит бонус.\n"
        "Вы получите уведомление о результате."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
    ])

    await message.answer(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "ad_my_applications")
async def cb_ad_my_applications(callback: CallbackQuery, bot: Bot):
    """Show user's ad applications."""
    await safe_answer(callback)
    user_id = callback.from_user.id

    applications = await get_user_ad_applications(user_id)

    if not applications:
        text = (
            "📋 <b>Мои заявки</b>\n\n"
            "У вас пока нет заявок на участие в программе.\n\n"
            "Нажмите «Проверить мой канал», чтобы начать."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📣 Проверить мой канал", callback_data="ad_check_channel")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
        ])
        await send_with_photo(bot, callback, text, kb)
        return

    text = "📋 <b>Мои заявки</b>\n\n"

    for app in applications:
        status_emoji = STATUS_EMOJI.get(app["status"], "❓")
        status_label = STATUS_TEXT.get(app["status"], "Неизвестно")
        channel_label = f"@{app['channel_username']}" if app["channel_username"] else (app.get("channel_ref") or "приватный канал")
        text += (
            f"{status_emoji} <b>Заявка #{app['id']}</b>\n"
            f"Канал: {channel_label}\n"
            f"Подписчиков: {app['subscriber_count']}\n"
            f"Бонус: {app['bonus_months']} мес.\n"
            f"Статус: {status_label}\n"
        )
        if app["status"] == "rejected" and app.get("rejection_reason"):
            text += f"Причина: {app['rejection_reason']}\n"
        if app["status"] in ("waiting_for_post", "channel_pending"):
            text += f"👉 Отправить пост: /ad_post_{app['id']}\n"
        text += "\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Проверить новый канал", callback_data="ad_check_channel")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")]
    ])

    await send_with_photo(bot, callback, text, kb)


@router.message(F.text.regexp(r"^/ad_post_(\d+)$"))
async def cmd_ad_submit_post(message: Message, bot: Bot, state: FSMContext):
    """Command shortcut to submit a post link for an existing application."""
    app_id = int(message.text.split("_")[-1])
    user_id = message.from_user.id

    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != user_id:
        await message.answer("Заявка не найдена.", reply_markup=back_to_menu())
        return
    if app["status"] not in ("waiting_for_post", "channel_pending"):
        await message.answer("Для этой заявки пост уже отправлен или она закрыта.", reply_markup=back_to_menu())
        return

    await state.set_state(AdProgramFlow.waiting_post_url)
    await state.update_data(current_app_id=app_id)
    await message.answer(
        "📤 Отправьте ссылку на опубликованный пост "
        "(например, <code>https://t.me/mychannel/123</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_my_applications")]
        ]),
    )


# ---------------------------------------------------------------------------
# Admin review (approve → grant bonus; reject → with reason)
# ---------------------------------------------------------------------------

def _ad_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Заявки на проверку", callback_data="ad_admin_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program")],
    ])


@router.callback_query(F.data == "ad_admin_list")
async def cb_ad_admin_list(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)

    apps = await get_ad_applications_by_statuses(
        ["post_submitted", "active_30_days", "channel_pending"], limit=15
    )
    if not apps:
        await send_with_photo(bot, callback, "📭 Новых заявок на проверку нет.", _ad_admin_kb())
        return

    now = int(time.time())
    text = "📥 <b>Заявки на проверку</b>\n\n"
    for app in apps:
        due = app.get("verification_due_at")
        retention = ""
        if due:
            left_days = max(0, (due - now) // 86400)
            retention = f" · 30 дней: {'✅ прошли' if due <= now else f'ещё {left_days} дн.'}"
        channel_label = f"@{app['channel_username']}" if app["channel_username"] else (app.get("channel_ref") or "приватный")
        text += (
            f"#{app['id']} · {channel_label} · {app['subscriber_count']} подписчиков\n"
            f"бонус {app['bonus_months']} мес. · статус {STATUS_TEXT.get(app['status'], app['status'])}{retention}\n"
            f"user: <code>{app['user_id']}</code>\n\n"
        )

    kb = InlineKeyboardBuilder()
    for app in apps:
        channel_label = f"@{app['channel_username']}" if app['channel_username'] else f"#{app['id']}"
        kb.row(InlineKeyboardButton(
            text=f"#{app['id']} · {channel_label} · {app['bonus_months']} мес.",
            callback_data=f"ad_admin_view:{app['id']}",
        ))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="ad_program"))
    await send_with_photo(bot, callback, text, kb.as_markup())


@router.callback_query(F.data.startswith("ad_admin_view:"))
async def cb_ad_admin_view(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])
    app = await get_ad_application_by_id(app_id)
    if not app:
        await safe_answer(callback, "Заявка не найдена", alert=True)
        return

    now = int(time.time())
    due = app.get("verification_due_at")
    retention = "—"
    if due:
        retention = f"прошли ({due <= now and 'да' or f'осталось {(due - now) // 86400} дн.'})"
    channel_label = f"@{app['channel_username']}" if app['channel_username'] else (app.get("channel_ref") or "приватный")

    text = (
        f"📥 <b>Заявка #{app['id']}</b>\n\n"
        f"Канал: {channel_label}\n"
        f"Подписчиков: {app['subscriber_count']}\n"
        f"Бонус: <b>{app['bonus_months']} мес. ({app['bonus_months'] * 30} дн.)</b>\n"
        f"Статус: {STATUS_TEXT.get(app['status'], app['status'])}\n"
        f"Проверка 30 дней: {retention}\n"
        f"Пост: {app.get('post_url') or '—'}\n"
        f"User: <code>{app['user_id']}</code>\n"
        f"Создана: {time.strftime('%d.%m.%Y', time.localtime(app['created_at']))}"
    )

    kb = InlineKeyboardBuilder()
    if app["status"] in ("post_submitted", "active_30_days", "channel_pending"):
        kb.row(
            InlineKeyboardButton(text="✅ Одобрить и выдать бонус", callback_data=f"ad_admin_approve:{app_id}"),
        )
        kb.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ad_admin_reject:{app_id}"))
    else:
        text += f"\n\nСтатус финальный — действие недоступно."
    kb.row(InlineKeyboardButton(text="◀️ К списку", callback_data="ad_admin_list"))
    await send_or_edit_safe(bot, callback, text, kb.as_markup())


async def send_or_edit_safe(bot: Bot, callback: CallbackQuery, text: str, kb) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("ad_admin_approve:"))
async def cb_ad_admin_approve(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])
    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] not in ("post_submitted", "active_30_days", "channel_pending"):
        await safe_answer(callback, "Заявка уже закрыта", alert=True)
        return

    user_id = app["user_id"]
    days = int(app["bonus_months"]) * 30

    # Approve FIRST (idempotent guard against double-click), then grant.
    ok = await update_ad_application(app_id, status="approved")
    if not ok:
        await safe_answer(callback, "Не удалось обновить заявку", alert=True)
        return

    granted_via = ""
    try:
        keys = await get_user_keys(user_id)
        active_keys = [k for k in keys if (k.get("expiry") or 0) > int(time.time())]
        if active_keys:
            target = max(active_keys, key=lambda k: k["expiry"])
            await extend_key(target["id"], days)
            granted_via = f"extended key #{target['id']} +{days}d"
            await bot.send_message(
                user_id,
                f"🎉 <b>Бонус начислен!</b>\n\n"
                f"Реклама в вашем канале подтверждена.\n"
                f"Ваш ключ ByMeVPN продлён на <b>{days} дней</b>.\n"
                f"Спасибо за сотрудничество!",
                parse_mode="HTML",
            )
        else:
            from subscription import deliver_key
            success = await deliver_key(
                bot=bot, user_id=user_id, chat_id=user_id,
                config_name=f"ad_bonus_{user_id}",
                days=days, limit_ip=2, is_paid=False, amount=0,
                method="ad_bonus", payload=f"ad_bonus_{app_id}",
                extend_existing=False,
            )
            granted_via = "delivered new key" if success else "delivery FAILED"
    except Exception as e:
        logger.error("Failed to grant ad bonus for app %d: %s", app_id, e)
        granted_via = f"ERROR: {e}"

    await log_ad_audit(callback.from_user.id, "ad_approved", f"app={app_id} user={user_id} via={granted_via}")
    await log_ad_analytics_event(user_id, "ad_approved", {"app_id": app_id, "days": days})

    await send_or_edit_safe(
        bot, callback,
        f"✅ Заявка #{app_id} одобрена. Бонус {app['bonus_months']} мес. ({days} дн.) начислен ({granted_via}).",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data="ad_admin_list")]]),
    )


@router.callback_query(F.data.startswith("ad_admin_reject:"))
async def cb_ad_admin_reject(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])
    await state.set_state(AdProgramFlow.waiting_rejection_reason)
    await state.update_data(reject_app_id=app_id)
    await callback.message.answer(
        f"❌ Укажите причину отклонения заявки #{app_id} одним сообщением\n"
        "(или отправьте «-» чтобы отклонить без причины):"
    )


@router.message(AdProgramFlow.waiting_rejection_reason)
async def msg_ad_rejection_reason(message: Message, bot: Bot, state: FSMContext):
    admin_id = message.from_user.id
    if not _is_admin(admin_id):
        await state.clear()
        return

    data = await state.get_data()
    app_id = data.get("reject_app_id")
    reason = (message.text or "").strip()
    if reason == "-":
        reason = ""

    await state.clear()
    if not app_id:
        await message.answer("Ошибка: заявка не найдена.")
        return

    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] not in ("post_submitted", "active_30_days", "channel_pending"):
        await message.answer("Заявка уже закрыта.")
        return

    await update_ad_application(app_id, status="rejected", rejection_reason=reason or "Не соответствует условиям")
    await log_ad_audit(admin_id, "ad_rejected", f"app={app_id} user={app['user_id']} reason={reason}")
    await log_ad_analytics_event(app["user_id"], "ad_rejected", {"app_id": app_id})

    try:
        await bot.send_message(
            app["user_id"],
            f"❌ <b>Заявка #{app_id} отклонена</b>\n\n"
            f"{('Причина: ' + reason) if reason else 'Заявка не соответствует условиям программы.'}\n\n"
            f"Если считаете это ошибкой — напишите в поддержку.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to notify user %d about rejection: %s", app["user_id"], e)

    await message.answer(f"Заявка #{app_id} отклонена.")
