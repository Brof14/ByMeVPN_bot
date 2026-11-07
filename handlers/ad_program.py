"""
«VPN за рекламу» — Telegram-каналы.

Flow (spec #23-29):
  public channel  → send @username / t.me link → auto-detect via getChat →
                    subscriber count if available (bot membership NOT required —
                    otherwise user provides the count) → application.
  private channel → invite link (t.me/+... or t.me/c/...) → manual subscriber
                    count → application pending manual review.
  post            → user forwards the official post from @ByMeVPN to their channel,
                    then submits the public link to the forwarded post
                    (status post_submitted, 30-day retention clock starts).
  bonus           → granted immediately after an admin verifies the published
                    post. The post is separately checked after 30 days.

Legacy status 'active_30_days' (older schema) is treated as post_submitted.
"""
import logging
import os
import re
import time
from html import escape
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.types import (
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS
from database import (
    create_ad_application, get_user_ad_applications, update_ad_application,
    get_ad_application_by_id, get_ad_applications_by_statuses,
    get_ad_applications_due_for_retention,
    channel_already_used, get_active_ad_campaign, log_ad_notification,
    calculate_bonus_months, log_ad_analytics_event, log_ad_audit,
    get_user_keys, delete_user_ad_application,
)
from utils import send_with_photo as _send_with_photo, safe_answer
from bot_assets import AD_PROGRAM
from keyboards import back_to_menu, support_url
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()


async def send_with_photo(bot, target, text, reply_markup=None, **kwargs):
    """Use the advertising artwork consistently across this section."""
    kwargs.setdefault("photo", AD_PROGRAM)
    return await _send_with_photo(bot, target, text, reply_markup, **kwargs)


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
_ACTIVE_APP_STATUSES = ("draft", "channel_pending", "waiting_for_post", "post_submitted", "active_30_days", "grant_pending")

STATUS_EMOJI = {
    "draft": "📝",
    "channel_pending": "⏳",
    "waiting_for_post": "📋",
    "post_submitted": "🔍",
    "active_30_days": "🔍",
    "approved": "✅",
    "grant_pending": "⚙️",
    "rejected": "❌",
    "completed": "🎉",
    "retention_issue": "⚠️",
}
STATUS_TEXT = {
    "draft": "Черновик",
    "channel_pending": "Ожидает проверки",
    "waiting_for_post": "Ожидает пост",
    "post_submitted": "Пост отправлен на проверку",
    "active_30_days": "Пост отправлен на проверку",
    "grant_pending": "Пост подтверждён, бонус начисляется",
    "approved": "Бонус начислен · пост нужно сохранить 30 дней",
    "rejected": "Отклонено",
    "completed": "Проверка поста завершена",
    "retention_issue": "Нужна ручная проверка поста",
}

# Public channel username patterns
_PUBLIC_PATTERNS = (
    re.compile(r"^@([A-Za-z0-9_]{4,64})$"),
    re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,64})/?$"),
)
# Private invite patterns: https://t.me/+Hh0x... or https://t.me/joinchat/... or t.me/c/1234/5
_PRIVATE_INVITE_RE = re.compile(r"^(?:https?://)?t\.me/(?:\+[\w-]{10,}|joinchat/[\w-]{10,}|c/\d+(?:/\d+)?)$")
_POST_URL_RE = re.compile(r"^(https?://)?t\.me/(?:[A-Za-z0-9_]{4,64}/\d+|c/\d+/\d+)(?:\?single)?/?$", re.IGNORECASE)


OFFICIAL_AD_CHANNEL_URL = "https://t.me/ByMeVPN"


def official_ad_post_url() -> str | None:
    """Canonical advertising post, configured by the operator after publication."""
    url = os.getenv("AD_SOURCE_POST_URL", "").strip().rstrip("/")
    return url if re.fullmatch(r"https://t\.me/ByMeVPN/[1-9][0-9]*", url, re.I) else None


def valid_ad_post_for_application(post_url: str, app: dict) -> bool:
    """Reject a public post URL pointing at a different channel.

    Telegram Bot API cannot inspect arbitrary channel messages via post URL,
    so the admin must still verify the Telegram forwarded-from attribution.
    """
    if not _POST_URL_RE.fullmatch(post_url):
        return False
    public = str(app.get("channel_username") or "").lstrip("@").lower()
    path = urlsplit(post_url if post_url.startswith("http") else "https://" + post_url).path.strip("/").split("/")
    if public:
        return len(path) >= 2 and path[0].lower() == public
    channel_id = app.get("channel_id")
    if channel_id and len(path) >= 3 and path[0] == "c":
        return path[1] == str(channel_id).removeprefix("-100")
    return True


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


@router.callback_query(F.data == "ad_program")
async def cb_ad_program(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Show main ad program screen."""
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id

    try:
        campaign = await get_active_ad_campaign()
        await log_ad_notification(user_id, campaign["id"] if campaign else None)
    except Exception as e:
        logger.debug("ad notification log failed: %s", e)
    await log_ad_analytics_event(user_id, "ad_offer_view")

    text = (
        "<b>VPN за рекламу</b>\n\n"
        "Перешлите пост из @ByMeVPN в свой канал. Бонус начисляется после проверки канала и публикации.\n\n"
        "Сохраните репост без изменений на 30 дней. Своей ссылкой для приглашений можно поделиться отдельным сообщением."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить канал", callback_data="ad_check_channel")],
        [InlineKeyboardButton(text="Размер бонусов", callback_data="ad_bonus_tiers")],
        [InlineKeyboardButton(text="Моя ссылка и приглашения", callback_data="partner")],
        [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="← Назад", callback_data="free_vpn")],
    ])

    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data == "ad_bonus_tiers")
async def cb_ad_bonus_tiers(callback: CallbackQuery, bot: Bot):
    """Show campaign rewards only on request, not on the first screen."""
    await safe_answer(callback)
    text = (
        "<b>Бонусы за рекламу</b>\n\n"
        f"{BONUS_TIERS}\n\n"
        "Публикацию нужно сохранить на 30 дней. Каждый канал участвует один раз."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить канал", callback_data="ad_check_channel")],
        [InlineKeyboardButton(text="← Назад", callback_data="ad_program")],
    ])
    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data == "ad_check_channel")
async def cb_ad_check_channel(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Start channel verification flow."""
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id

    await log_ad_analytics_event(user_id, "ad_offer_click", {"action": "check_channel"})

    existing_apps = await get_user_ad_applications(user_id)
    pending_apps = [app for app in existing_apps if app["status"] in _ACTIVE_APP_STATUSES]

    if pending_apps:
        text = (
            "<b>У вас уже есть заявка</b>\n\n"
            "Ожидайте её проверки или завершите текущую заявку, прежде чем подавать новую."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="← Назад", callback_data="ad_program")],
        ])
        await send_with_photo(bot, callback, text, kb)
        return

    text = (
        "<b>Проверка канала</b>\n\n"
        "Отправьте ссылку на канал: <code>@mychannel</code>\n"
        "Для приватного канала — ссылку-приглашение.\n\n"
        "Права администратора не нужны."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="ad_program")],
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

    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or not 1 <= int(raw) <= 2**63 - 1:
        await message.answer(
            "❌ Отправьте положительное число подписчиков, например: <code>1284</code>",
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
            [InlineKeyboardButton(text="← Назад", callback_data="ad_program")]
        ])
        await message.answer(text, reply_markup=kb)
        return

    # 350+ needs individual agreement with the admin; everything else goes
    # straight to "publish the post" step.
    needs_review = subscriber_count >= 350 or manual_count
    status = "channel_pending" if needs_review else "waiting_for_post"

    campaign = await get_active_ad_campaign()
    campaign_id = campaign["id"] if campaign else None

    try:
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
    except Exception as error:
        if "unique" in str(error).lower():
            await message.answer(
                "⚠️ По этому каналу уже есть заявка или он участвовал раньше. "
                "Откройте «Мои заявки» или выберите другой канал.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")
                ]]),
            )
            return
        logger.exception("Failed to create ad application for user %d", user_id)
        await message.answer("Не удалось создать заявку. Попробуйте позже или напишите в поддержку.", reply_markup=back_to_menu())
        return

    await log_ad_analytics_event(user_id, "ad_application_submitted", {
        "app_id": app_id,
        "subscriber_count": subscriber_count,
        "bonus_months": bonus_months,
        "manual_count": manual_count,
    })
    await log_ad_audit(user_id, "ad_application_created", f"app={app_id} subs={subscriber_count}")

    if needs_review:
        text = (
            f"<b>Заявка #{app_id} принята</b>\n\n"
            f"Канал: <b>{escape(str(chat_title))}</b>\n"
            f"👥 Подписчиков: <b>{subscriber_count}</b>{' (указано вами)' if manual_count else ''}\n"
            f"Возможный бонус: <b>{bonus_months} мес.</b>\n\n"
            "После проверки канала перешлите официальный пост из @ByMeVPN."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="← Назад", callback_data="ad_program")]
        ])
    else:
        text = (
            f"<b>Канал найден: {escape(str(chat_title))}</b>\n\n"
            f"👥 Подписчиков: <b>{subscriber_count}</b>\n"
            f"Ваш бонус: <b>{bonus_months} мес.</b>\n\n"
            "1. Перешлите пост из @ByMeVPN в свой канал без изменений.\n"
            "2. Отправьте ссылку на пересланное сообщение.\n"
            "3. После проверки получите бонус. Сохраните репост на 30 дней."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть рекламный пост", callback_data=f"ad_get_post:{app_id}")],
            [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
            [InlineKeyboardButton(text="← Назад", callback_data="ad_program")]
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
            "Неверная ссылка. Отправьте @username, "
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
            ad_channel_ref=private_ref,
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

    if chat.type != "channel":
        await message.answer(
            "Эта ссылка ведёт не на канал. Отправьте ссылку на Telegram-канал.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="ad_program")]
            ]),
        )
        return

    subscriber_count = None
    try:
        subscriber_count = await bot.get_chat_member_count(chat.id)
    except Exception:
        pass  # bot is not a member — that's fine, user will provide the count

    if (not isinstance(subscriber_count, int) or isinstance(subscriber_count, bool)
            or not 1 <= subscriber_count <= 2**63 - 1):
        await state.update_data(
            ad_channel_ref=public_username.lower(),
            ad_channel_username=public_username,
            ad_channel_id=chat.id,
            ad_chat_title=chat.title or f"@{public_username}",
        )
        await _ask_subscriber_count(
            bot, message, state,
            prompt=(
                f"<b>Канал найден: {escape(chat.title or '@' + public_username)}</b>\n\n"
                "Не удалось получить корректное число подписчиков от Telegram.\n\n"
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
    """Show the original-channel post link, never copy-paste advertising text."""
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])
    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заявка не найдена.", alert=True)
        return
    if app["status"] not in ("waiting_for_post", "post_submitted", "active_30_days"):
        await safe_answer(callback, "Сначала дождитесь проверки канала.", alert=True)
        return
    post_url = official_ad_post_url()
    text = (
        "<b>Рекламный пост</b>\n\n"
        "Перешлите официальный рекламный пост из @ByMeVPN в свой канал.\n"
        "Не копируйте текст, не редактируйте пост и не добавляйте свою рекламу.\n\n"
        "После публикации отправьте ссылку на пересланное сообщение. "
        "Не удаляйте его 30 дней."
    )
    if not post_url:
        text += "\n\nПрямая ссылка на пост пока не задана. Откройте канал и найдите рекламную публикацию."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Открыть исходный пост" if post_url else "Открыть @ByMeVPN",
            url=post_url or OFFICIAL_AD_CHANNEL_URL,
        )],
        [InlineKeyboardButton(text="Отправить ссылку на репост", callback_data=f"ad_submit_post:{app_id}")],
        [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="← Назад", callback_data="ad_program")],
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

    if app["status"] != "waiting_for_post":
        await safe_answer(callback, "Дождитесь проверки канала.", alert=True)
        return

    text = (
        "<b>Ссылка на репост</b>\n\n"
        "Отправьте ссылку на пересланное из @ByMeVPN сообщение в вашем канале.\n"
        "Текст и оформление оригинала менять нельзя.\n\n"
        "Пример: <code>https://t.me/mychannel/123</code>"
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

    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != user_id or app["status"] != "waiting_for_post":
        await message.answer("Дождитесь проверки канала или откройте активную заявку.", reply_markup=back_to_menu())
        await state.clear()
        return

    post_match = _POST_URL_RE.fullmatch(post_url)
    if not post_match:
        await message.answer(
            "❌ Неверный формат ссылки. Пожалуйста, отправьте ссылку на пост "
            "из вашего канала, например https://t.me/channel/123 или https://t.me/c/123456/5",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"ad_get_post:{app_id}")]
            ])
        )
        return
    if not post_match.group(1):
        post_url = f"https://{post_url.lstrip('/')}"

    if not valid_ad_post_for_application(post_url, app):
        await message.answer(
            "Ссылка должна вести на публикацию в канале из вашей заявки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← Назад", callback_data=f"ad_get_post:{app_id}")
            ]]),
        )
        return

    updated = await update_ad_application(
        app_id, status="post_submitted", post_url=post_url,
        expected_statuses=["waiting_for_post"],
    )
    if not updated:
        await message.answer("Заявка уже закрыта или удалена. Откройте раздел «Мои заявки».", reply_markup=back_to_menu())
        await state.clear()
        return
    # The post URL is stored on the application for verification. Do not copy
    # private-channel links into general-purpose analytics and audit logs.
    await log_ad_analytics_event(user_id, "ad_post_submitted", {"app_id": app_id})
    await log_ad_audit(user_id, "ad_post_submitted", f"app={app_id}")

    text = (
        "<b>Ссылка получена</b>\n\n"
        "Проверим пересылку из @ByMeVPN и отсутствие изменений. "
        "После подтверждения начислим бонус.\n\n"
        "Сохраните публикацию на 30 дней."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="← Назад", callback_data="ad_program")]
    ])

    await message.answer(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "ad_my_applications")
async def cb_ad_my_applications(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Show user's ad applications."""
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id

    applications = await get_user_ad_applications(user_id)

    if not applications:
        text = (
            "<b>Мои заявки</b>\n\n"
            "Пока нет заявок."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Проверить канал", callback_data="ad_check_channel")],
            [InlineKeyboardButton(text="← Назад", callback_data="ad_program")]
        ])
        await send_with_photo(bot, callback, text, kb)
        return

    text = "<b>Мои заявки</b>\n\n"

    for app in applications:
        status_emoji = STATUS_EMOJI.get(app["status"], "❓")
        status_label = STATUS_TEXT.get(app["status"], "Неизвестно")
        channel_label = f"@{app['channel_username']}" if app["channel_username"] else (app.get("channel_ref") or "приватный канал")
        text += (
            f"{status_emoji} <b>Заявка #{app['id']}</b>\n"
            f"{escape(str(channel_label))} · {app['subscriber_count']} подписчиков\n"
            f"Бонус: <b>{app['bonus_months']} мес.</b> · {status_label}\n"
        )
        if app["status"] == "approved" and app.get("verification_due_at"):
            text += f"Сохраните публикацию до {time.strftime('%d.%m.%Y', time.localtime(app['verification_due_at']))}.\n"
        if app["status"] == "grant_pending":
            text += "Если статус не изменится, напишите в поддержку — заявка сохранена.\n"
        if app["status"] == "rejected" and app.get("rejection_reason"):
            text += f"Причина: {escape(str(app['rejection_reason']))}\n"
        if app["status"] == "channel_pending":
            text += "Канал проверяет администратор. После проверки здесь появится рекламный пост.\n"
        elif app["status"] == "waiting_for_post":
            text += "Перешлите пост из @ByMeVPN без изменений и отправьте ссылку.\n"
        if app["status"] in _ACTIVE_APP_STATUSES or app["status"] == "rejected":
            text += "Можно удалить эту заявку, пока бонус ещё не начислен.\n"
        text += "\n"

    rows = []
    for app in applications:
        if app["status"] == "waiting_for_post":
            rows.append([InlineKeyboardButton(
                text=f"Открыть пост · заявка #{app['id']}",
                callback_data=f"ad_get_post:{app['id']}",
                
            )])
        if app["status"] in ("draft", "channel_pending", "waiting_for_post", "post_submitted", "active_30_days", "rejected"):
            rows.append([InlineKeyboardButton(
                text=f"🗑 Удалить заявку #{app['id']}",
                callback_data=f"ad_cancel_confirm:{app['id']}",
                style="danger",
            )])
    rows.extend([
        [InlineKeyboardButton(text="📣 Проверить новый канал", callback_data="ad_check_channel", )],
        [InlineKeyboardButton(text="← Назад", callback_data="ad_program")],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data.startswith("ad_cancel_confirm:"))
async def cb_ad_cancel_confirm(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await safe_answer(callback, "Неверный номер заявки.", alert=True)
        return
    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != callback.from_user.id or (app["status"] not in _ACTIVE_APP_STATUSES and app["status"] != "rejected"):
        await safe_answer(callback, "Эту заявку нельзя удалить.", alert=True)
        return
    text = (
        f"Удалить заявку <b>#{app_id}</b>?\n\n"
        "Заявка будет закрыта, а проверка и начисление бонуса остановятся. "
        "Если захотите участвовать снова, можно будет отправить новую заявку."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить заявку", callback_data=f"ad_cancel:{app_id}", style="danger")],
        [InlineKeyboardButton(text="◀️ Оставить заявку", callback_data="ad_my_applications")],
    ])
    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data.startswith("ad_cancel:"))
async def cb_ad_cancel(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await safe_answer(callback, "Неверный номер заявки.", alert=True)
        return
    deleted = await delete_user_ad_application(app_id, callback.from_user.id)
    if not deleted:
        await safe_answer(callback, "Заявку уже закрыли или её нельзя удалить.", alert=True)
        return
    await log_ad_audit(callback.from_user.id, "ad_application_deleted_by_user", f"app={app_id}")
    await log_ad_analytics_event(callback.from_user.id, "ad_application_cancelled", {"app_id": app_id})
    data = await state.get_data()
    if data.get("current_app_id") == app_id:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
        [InlineKeyboardButton(text="◀️ В раздел VPN за рекламу", callback_data="ad_program")],
    ])
    await send_with_photo(bot, callback, f"✅ Заявка #{app_id} удалена. Бонус за неё начисляться не будет.", kb)


@router.message(F.text.regexp(r"^/ad_post_(\d+)$"))
async def cmd_ad_submit_post(message: Message, bot: Bot, state: FSMContext):
    """Command shortcut to submit a post link for an existing application."""
    app_id = int(message.text.split("_")[-1])
    user_id = message.from_user.id

    app = await get_ad_application_by_id(app_id)
    if not app or app["user_id"] != user_id:
        await message.answer("Заявка не найдена.", reply_markup=back_to_menu())
        return
    if app["status"] != "waiting_for_post":
        await message.answer("Дождитесь проверки канала или откройте активную заявку.", reply_markup=back_to_menu())
        return

    await state.set_state(AdProgramFlow.waiting_post_url)
    await state.update_data(current_app_id=app_id)
    await message.answer(
        "Отправьте ссылку на пересланный из @ByMeVPN пост "
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
        [InlineKeyboardButton(text="← Назад", callback_data="ad_program")],
    ])


@router.callback_query(F.data == "ad_admin_list")
async def cb_ad_admin_list(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)

    apps = await get_ad_applications_by_statuses(
        ["post_submitted", "active_30_days", "channel_pending", "grant_pending"], limit=15
    )
    retention_apps = await get_ad_applications_due_for_retention(limit=50)
    apps.extend(retention_apps)
    apps = list({app["id"]: app for app in apps}.values())[:50]
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
    kb.row(InlineKeyboardButton(text="← Назад", callback_data="ad_program"))
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
        f"Пост: {escape(str(app.get('post_url') or '—'))}\n"
        f"User: <code>{app['user_id']}</code>\n"
        f"Создана: {time.strftime('%d.%m.%Y', time.localtime(app['created_at']))}"
    )

    kb = InlineKeyboardBuilder()
    if app["status"] in ("post_submitted", "active_30_days"):
        text += (
            "\n\n<b>Проверьте вручную:</b> сообщение переслано из @ByMeVPN "
            "без изменений и заявитель управляет каналом. Одна ссылка на пост "
            "не подтверждает ни происхождение репоста, ни владение каналом."
        )
        kb.row(
            InlineKeyboardButton(text="Подтвердить репост и выдать бонус", callback_data=f"ad_admin_approve:{app_id}"),
        )
        kb.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ad_admin_reject:{app_id}"))
    elif app["status"] == "grant_pending":
        kb.row(InlineKeyboardButton(text="↻ Повторить начисление", callback_data=f"ad_admin_approve:{app_id}"))
    elif app["status"] == "channel_pending":
        text += (
            "\n\n<b>Проверьте вручную:</b> принадлежность канала, число подписчиков "
            "и прошлое участие. Разные пригласительные ссылки могут вести в один канал."
        )
        kb.row(InlineKeyboardButton(text="✓ Канал проверен · ждать пост", callback_data=f"ad_admin_channel_ok:{app_id}"))
        kb.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ad_admin_reject:{app_id}"))
    elif app["status"] in ("approved", "retention_issue") and due and due <= now:
        kb.row(InlineKeyboardButton(text="✓ Пост сохранён 30 дней", callback_data=f"ad_admin_retention_ok:{app_id}"))
        if app["status"] == "approved":
            kb.row(InlineKeyboardButton(text="⚠️ Пост не найден · нужна связь", callback_data=f"ad_admin_retention_issue:{app_id}"))
    else:
        text += f"\n\nСтатус финальный — действие недоступно."
    kb.row(InlineKeyboardButton(text="◀️ К списку", callback_data="ad_admin_list"))
    await send_or_edit_safe(bot, callback, text, kb.as_markup())


@router.callback_query(F.data.startswith("ad_admin_retention_ok:"))
async def cb_ad_admin_retention_ok(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer(callback, "Неверный номер заявки.", alert=True)
        return
    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] not in ("approved", "retention_issue") or (app.get("verification_due_at") or 0) > int(time.time()):
        await safe_answer(callback, "Срок проверки ещё не наступил или заявка уже закрыта.", alert=True)
        return
    updated = await update_ad_application(app_id, status="completed", expected_statuses=["approved", "retention_issue"])
    if not updated:
        await safe_answer(callback, "Заявка уже обработана.", alert=True)
        return
    await log_ad_audit(callback.from_user.id, "ad_post_retained_30_days", f"app={app_id}")
    await send_or_edit_safe(bot, callback, f"Проверка поста по заявке #{app_id} завершена. Выданный бонус не менялся.", _ad_admin_kb())


@router.callback_query(F.data.startswith("ad_admin_retention_issue:"))
async def cb_ad_admin_retention_issue(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer(callback, "Неверный номер заявки.", alert=True)
        return
    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] != "approved" or (app.get("verification_due_at") or 0) > int(time.time()):
        await safe_answer(callback, "Срок проверки ещё не наступил или заявка уже закрыта.", alert=True)
        return
    updated = await update_ad_application(app_id, status="retention_issue", expected_statuses=["approved"])
    if not updated:
        await safe_answer(callback, "Заявка уже обработана.", alert=True)
        return
    await log_ad_audit(callback.from_user.id, "ad_post_retention_manual_review", f"app={app_id}")
    try:
        await bot.send_message(
            app["user_id"],
            f"Нам не удалось найти публикацию по заявке #{app_id}. Выданный бонус остаётся у вас. Если пост опубликован, отправьте ссылку в поддержку — мы проверим вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Написать в поддержку", url=support_url())]]),
        )
    except Exception:
        logger.warning("Could not notify user about ad retention check (app=%d)", app_id)
    await send_or_edit_safe(bot, callback, f"Заявка #{app_id} отмечена для ручной проверки. Бонус пользователю не отзывался.", _ad_admin_kb())


async def send_or_edit_safe(bot: Bot, callback: CallbackQuery, text: str, kb) -> None:
    await send_with_photo(bot, callback, text, kb)


async def _deliver_verified_ad_reward(bot: Bot, app: dict) -> bool:
    """Deliver only a reward already approved for provisioning."""
    app_id, user_id = app["id"], app["user_id"]
    try:
        from subscription import deliver_key
        keys = await get_user_keys(user_id)
        existing = max(keys, key=lambda key: key.get("expiry", 0)) if keys else None
        return await deliver_key(
            bot=bot, user_id=user_id, chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}", days=int(app["bonus_months"]) * 30,
            limit_ip=existing.get("limit_ip", 2) if existing else 2,
            is_paid=True, amount=0, method="ad_bonus", payload=f"ad_bonus_{app_id}",
            extend_existing=True,
        )
    except Exception:
        logger.exception("Failed to grant ad bonus for app %d", app_id)
        return False


async def process_pending_ad_rewards(bot: Bot, limit: int = 10) -> int:
    """Retry rewards for manually verified posts, with fair bounded batches."""
    from database import get_pending_ad_reward_retries, mark_ad_reward_attempt

    completed = 0
    for app in await get_pending_ad_reward_retries(limit):
        try:
            await mark_ad_reward_attempt(app["id"])
            if not await _deliver_verified_ad_reward(bot, app):
                continue
            if await update_ad_application(app["id"], status="approved", expected_statuses=["grant_pending"]):
                completed += 1
                await log_ad_audit(None, "ad_approved_retry", f"app={app['id']} user={app['user_id']}")
                await log_ad_analytics_event(app["user_id"], "ad_approved", {
                    "app_id": app["id"], "days": int(app["bonus_months"]) * 30,
                })
        except Exception:
            logger.exception("Ad bonus retry failed for app %d", app["id"])
    return completed


@router.callback_query(F.data.startswith("ad_admin_approve:"))
async def cb_ad_admin_approve(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    app_id = int(callback.data.split(":")[1])
    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] not in ("post_submitted", "active_30_days", "grant_pending"):
        await safe_answer(callback, "Заявка уже закрыта", alert=True)
        return

    user_id = app["user_id"]
    days = int(app["bonus_months"]) * 30

    # Keep the request retryable until the idempotent provisioning operation
    # succeeds. deliver_key stores a fixed expiry intent for this application,
    # so retrying after a timeout cannot add the same reward twice.
    if app["status"] != "grant_pending":
        marked = await update_ad_application(
            app_id, status="grant_pending",
            expected_statuses=["post_submitted", "active_30_days"],
        )
        if not marked:
            await safe_answer(callback, "Заявка уже обрабатывается.", alert=True)
            return
    success = await _deliver_verified_ad_reward(bot, app)

    if not success:
        await log_ad_audit(callback.from_user.id, "ad_bonus_pending", f"app={app_id} user={user_id}")
        await send_or_edit_safe(
            bot, callback,
            f"⚠️ Пост подтверждён, но бонус по заявке #{app_id} пока не выдан. Заявка сохранена; можно безопасно повторить начисление.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↻ Повторить начисление", callback_data=f"ad_admin_approve:{app_id}")]]),
        )
        return

    try:
        updated = await update_ad_application(app_id, status="approved", expected_statuses=["grant_pending"])
    except Exception:
        logger.exception("Could not close ad application %d after delivery", app_id)
        updated = False
    if not updated:
        try:
            current = await get_ad_application_by_id(app_id)
        except Exception:
            current = None
        if current and current["status"] == "approved":
            text = f"Заявка #{app_id} уже подтверждена и закрыта."
        else:
            text = (
                f"⚠️ Бонус по заявке #{app_id} выдан, но статус заявки не подтверждён. "
                "Нужна сверка; не выдавайте бонус повторно вручную."
            )
            logger.error("Ad application %d needs status reconciliation after delivery", app_id)
        await send_or_edit_safe(bot, callback, text, _ad_admin_kb())
        return

    await log_ad_audit(callback.from_user.id, "ad_approved", f"app={app_id} user={user_id} days={days}")
    await log_ad_analytics_event(user_id, "ad_approved", {"app_id": app_id, "days": days})

    await send_or_edit_safe(
        bot, callback,
        f"✅ Заявка #{app_id} одобрена. Бонус {app['bonus_months']} мес. ({days} дн.) начислен.",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data="ad_admin_list")]]),
    )


@router.callback_query(F.data.startswith("ad_admin_channel_ok:"))
async def cb_ad_admin_channel_ok(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Недоступно", alert=True)
        return
    await safe_answer(callback)
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer(callback, "Неверный номер заявки.", alert=True)
        return
    app = await get_ad_application_by_id(app_id)
    if not app or app["status"] != "channel_pending":
        await safe_answer(callback, "Заявка уже обработана.", alert=True)
        return
    updated = await update_ad_application(app_id, status="waiting_for_post", expected_statuses=["channel_pending"])
    if not updated:
        await safe_answer(callback, "Заявка уже обрабатывается.", alert=True)
        return
    await log_ad_audit(callback.from_user.id, "ad_channel_verified", f"app={app_id}")
    try:
        await bot.send_message(
            app["user_id"],
            f"Канал по заявке #{app_id} проверен. Перешлите пост из @ByMeVPN без изменений и отправьте ссылку на репост.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть рекламный пост", callback_data=f"ad_get_post:{app_id}")],
                [InlineKeyboardButton(text="Мои заявки", callback_data="ad_my_applications")],
            ]),
        )
    except Exception:
        logger.warning("Could not notify user about verified ad channel (app=%d)", app_id)
    await send_or_edit_safe(bot, callback, f"Канал по заявке #{app_id} проверен. Ожидаем ссылку на публикацию.", _ad_admin_kb())


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

    updated = await update_ad_application(
        app_id,
        status="rejected",
        rejection_reason=reason or "Не соответствует условиям",
        expected_statuses=["post_submitted", "active_30_days", "channel_pending"],
    )
    if not updated:
        await message.answer("Заявка была удалена или уже обработана.")
        return
    await log_ad_audit(admin_id, "ad_rejected", f"app={app_id} user={app['user_id']} reason={reason}")
    await log_ad_analytics_event(app["user_id"], "ad_rejected", {"app_id": app_id})

    try:
        await bot.send_message(
            app["user_id"],
            f"❌ <b>Заявка #{app_id} отклонена</b>\n\n"
            f"{('Причина: ' + escape(reason)) if reason else 'Заявка не соответствует условиям программы.'}\n\n"
            f"Если считаете это ошибкой — напишите в поддержку.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to notify user %d about rejection: %s", app["user_id"], e)

    await message.answer(f"Заявка #{app_id} отклонена.")
