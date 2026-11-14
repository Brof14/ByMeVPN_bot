"""
/start, main menu, trial, back_to_menu, config-name FSM handler.

Menu states:
  new      — never had key, trial not used → show trial button
  referred — arrived via ref link + trial available → single "Забрать" button
  expired  — had key/trial but no active sub → "Подписка закончилась" + existing menu
  active   — has active sub → existing menu
"""
import asyncio
import logging
import time
from html import escape
from urllib.parse import urlencode

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    ensure_user, set_referrer, find_user_by_id, get_referrer,
    has_trial_used,
    get_trial_claim_reservation, reserve_trial_claim, complete_trial_claim,
    release_trial_claim,
    has_active_subscription,
    has_ever_had_key, get_user_keys,
)
from app_links import build_app_login_url, build_app_redirect_url
from config import CHANNEL_BONUS_CHAT_ID, CHANNEL_BONUS_URL
from keyboards import (
    main_menu_new_user, main_menu_existing, main_menu_with_keys, main_menu_active,
    main_menu_panel_active, main_menu_expired,
    back_to_menu, cancel_kb, support_url, app_open_kb,
)
from utils import send_with_photo, safe_answer, get_cached_bot_info
from async_utils import monitor_performance
from bot_style import themed_button as InlineKeyboardButton
from bot_assets import MENU, ABOUT, FREE_VPN

logger = logging.getLogger(__name__)
router = Router()


async def _handle_app_login(message: Message, user_id: int, user_name: str) -> None:
    """Send the ByMeVPN app login link for an active account, or access options."""
    state = await _user_state(user_id)
    if state == "unknown":
        await message.answer(
            "Не удалось проверить статус подписки. Попробуйте ещё раз немного позже."
        )
        return

    if state == "existing":
        await message.answer(
            "Для аккаунта уже создан клиент ByMeVPN. Пробный период доступен только аккаунтам без клиента. "
            "Выберите тариф, чтобы продолжить.",
            reply_markup=main_menu_existing(trial_used=True),
        )
        return

    if state == "active":
        now = int(time.time())
        active_keys = [
            key for key in await get_user_keys(user_id)
            if int(key.get("expiry", 0)) > now and key.get("key")
        ]
        subscription_url = (
            max(active_keys, key=lambda key: int(key.get("expiry", 0)))["key"]
            if active_keys else ""
        )
        if not subscription_url:
            # Recover the subscription URL if 3x-ui has an active client whose
            # key record is missing from the bot database.
            try:
                from xui_client import get_xui_client_details
                client = await get_xui_client_details(user_id)
                if client and int(client.get("expiry_time", 0)) > now * 1000:
                    subscription_url = client.get("subscription_url", "")
            except Exception:
                logger.exception("Could not resolve app subscription URL for user %d", user_id)

        if subscription_url:
            app_name = f"@{message.from_user.username}" if message.from_user.username else user_name
            app_url = build_app_login_url(subscription_url, app_name)
            try:
                redirect_url = build_app_redirect_url(subscription_url, app_name)
            except ValueError:
                # Never put a bearer-style VPN subscription URL into an HTTP
                # query string. Keep manual app login available until HTTPS
                # handover is correctly configured and verified.
                logger.warning("Secure public app URL is not configured for user %d", user_id)
                await message.answer(
                    "<b>Вход в приложение ByMeVPN</b>\n\n"
                    "Автоматический переход временно недоступен. "
                    "Для безопасности скопируйте ссылку входа и вставьте её в приложение.\n\n"
                    f"<code>{escape(app_url)}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Скопировать ссылку входа",
                                              copy_text={"text": app_url[:256]})]
                        if len(app_url) <= 256 else
                        [InlineKeyboardButton(text="Подписка",
                                              callback_data="my_keys")],
                        [InlineKeyboardButton(text="Подключить VPN", callback_data="connection_guide")],
                    ]),
                )
                return
            await message.answer(
                "<b>Вход в приложение ByMeVPN</b>\n\n"
                "Нажмите «🚀 Открыть приложение» — ссылка передаст подписку в ByMeVPN. Подключение VPN подтвердите в приложении.\n\n"
                "Если приложение не открылось, скопируйте ссылку ниже и вставьте её в ByMeVPN вручную.\n\n"
                f"<code>{escape(app_url)}</code>",
                parse_mode="HTML",
                reply_markup=app_open_kb(redirect_url, app_url),
            )
            return

        await message.answer(
            "Подписка отмечена как активная, но ссылку для приложения получить не удалось. "
            "Откройте «Подписка» или обратитесь в поддержку."
        )
        return

    trial_used = await has_trial_used(user_id)
    user_keys = await get_user_keys(user_id)
    if user_keys:
        keyboard = main_menu_with_keys(trial_used=trial_used)
    elif trial_used:
        keyboard = main_menu_existing(trial_used=True)
    else:
        keyboard = main_menu_new_user()
    await message.answer(
        "Чтобы войти в приложение ByMeVPN, сначала активируйте доступ: "
        + ("выберите пробный период или тариф ниже." if not trial_used else "выберите тариф ниже."),
        reply_markup=keyboard,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@monitor_performance("user_state_check")
async def _user_state(user_id: int) -> str:
    """Determine account state without offering trial to a pre-existing panel client."""
    # Выполняем все проверки параллельно для максимальной скорости
    tasks = [
        has_active_subscription(user_id),
        has_ever_had_key(user_id),
        has_trial_used(user_id),
    ]
    
    active, ever_had, trial_used = await asyncio.gather(*tasks, return_exceptions=True)
    
    # A database outage must not turn a paying customer into a "new" user.
    if any(isinstance(value, Exception) for value in (active, ever_had, trial_used)):
        logger.error("Could not determine account state for user %d", user_id)
        return "unknown"
    
    if active:
        return "active"
    try:
        from xui_client import get_xui_client_details
        panel_client = await get_xui_client_details(user_id, strict=True)
    except Exception as exc:
        if ever_had or trial_used:
            return "expired"
        logger.warning("Panel-backed account state check failed (%s)", type(exc).__name__)
        return "unknown"
    if _panel_client_is_active(panel_client):
        return "active"
    if ever_had or trial_used:
        return "expired"
    if panel_client:
        # A pre-created, already-expired bootstrap client is not proof that
        # this account has consumed its one shared trial. Offer the trial only
        # when the panel confirms the client has no unexpired access.
        if _panel_client_is_expired(panel_client):
            return "new"
        return "unknown"
    return "new"


def _panel_client_is_active(panel_client: dict | None) -> bool:
    """Treat an enabled, unexpired panel client as active when the local key row is absent."""
    if not panel_client:
        return False
    enabled = panel_client.get("enable")
    if not (enabled is True or str(enabled).strip().lower() in {"1", "true", "yes", "on"}):
        return False
    try:
        expiry_ms = int(panel_client.get("expiry_time") or 0)
    except (TypeError, ValueError):
        return False
    return expiry_ms > int(time.time() * 1000)


def _panel_client_is_expired(panel_client: dict | None) -> bool:
    """Return true only when the panel confirms there is no unexpired client."""
    if panel_client is None:
        return True
    if not isinstance(panel_client, dict):
        return False
    try:
        expiry_ms = int(panel_client.get("expiry_time") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < expiry_ms <= int(time.time() * 1000)


@monitor_performance("clean_chat")
async def _clean_chat(bot: Bot, chat_id: int, anchor_msg_id: int, count: int = 3) -> None:
    """
    Delete the last `count` messages up to and including anchor_msg_id.
    Runs all deletes concurrently with timeout for speed.
    """
    # Get message IDs to delete (max 3 for speed)
    ids = [mid for mid in range(anchor_msg_id, anchor_msg_id - min(count, 3), -1) if mid > 0]
    if not ids:
        return
    
    # Delete with timeout for each message - don't wait for slow/old messages
    async def delete_with_timeout(msg_id):
        try:
            await asyncio.wait_for(bot.delete_message(chat_id, msg_id), timeout=0.5)
        except asyncio.TimeoutError:
            pass  # Skip slow deletions
        except Exception:
            pass  # Ignore all errors (message too old, already deleted, etc.)
    
    # Execute all deletes concurrently
    await asyncio.gather(*[delete_with_timeout(mid) for mid in ids], return_exceptions=True)


def _remaining_days_label(expiry: int) -> str:
    days = max(1, (int(expiry) - int(time.time()) + 86399) // 86400)
    word = "день" if days % 10 == 1 and days % 100 != 11 else (
        "дня" if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14) else "дней"
    )
    return f"Осталось <b>{days} {word}</b>\n"


async def _send_main_menu(
    bot: Bot,
    target: "Message | CallbackQuery",
    user_id: int,
    user_name: str,
    *,
    is_new_referral: bool = False,
) -> None:
    """
    Send the correct menu screen based on user state.
    is_new_referral=True → show special single-button referral welcome screen.
    """
    state = await _user_state(user_id)

    if state == "unknown":
        text = (
            "Не удалось проверить подписку. Это временная ошибка; "
            "ваш доступ и платежи не изменены. Попробуйте немного позже."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Повторить проверку", callback_data="back_to_menu")],
            [InlineKeyboardButton(text="Поддержка", url=support_url("subscription"))],
        ])
        if isinstance(target, Message):
            await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        else:
            await send_with_photo(bot, target, text, kb, photo=MENU)
        return

    if state == "new":
        if is_new_referral:
            text = (
                "<b>ByMeVPN</b>\n\n"
                "Приглашение учтено.\n\n"
                "<b>3 дня бесплатно</b> — без карты и оплаты.\n"
                "Нажмите кнопку ниже, чтобы получить доступ."
            )
        else:
            text = (
                "<b>ByMeVPN</b>\n\n"
                "VPN для телефона и компьютера.\n\n"
                "<b>3 дня бесплатно</b> — без карты и оплаты.\n\n"
                "Получите доступ и следуйте короткой инструкции подключения."
            )
        kb = main_menu_new_user()
    elif state == "existing":
        text = (
            "<b>С возвращением в ByMeVPN</b>\n\n"
            ""
            "Выберите тариф, чтобы продолжить."
        )
        kb = main_menu_existing(trial_used=True)
    elif state == "expired":
        text = (
            "<b>Срок подписки закончился</b>\n\n"
            "Продлите доступ или получите бонусные дни."
        )
        # The first action must renew the actual existing key, not reset its
        # device choice through a new-purchase checkout.
        user_keys = await get_user_keys(user_id)
        kb = main_menu_expired(key_ids=[int(k["id"]) for k in user_keys])
    else:  # active
        # Check if user has keys to show appropriate menu (use direct DB check, not cache)
        user_keys = await get_user_keys(user_id)
        active_keys = [k for k in user_keys if k.get("expiry", 0) > int(time.time())]
        if active_keys:
            active_key = max(active_keys, key=lambda k: k.get("expiry", 0))
            from constants import format_timestamp
            expires = format_timestamp(active_key["expiry"])[:10]
            remaining_text = _remaining_days_label(active_key["expiry"])
            device_count = int(active_key.get("limit_ip") or 0)
            if device_count not in {2, 5, 10}:
                device_count = 0
            if not device_count:
                try:
                    from xui_client import get_xui_client_details
                    panel_client = await get_xui_client_details(user_id, strict=True)
                    if _panel_client_is_active(panel_client):
                        device_count = int(panel_client.get("limit_hwid") or 0)
                except Exception as exc:
                    logger.warning("Could not read active panel subscription (%s)", type(exc).__name__)
            device_word = "устройство" if device_count == 1 else "устройства" if 2 <= device_count <= 4 else "устройств"
            if device_count in {2, 5, 10}:
                text = (
                    f"<b>Подписка активна</b>\n\n"
                    f"{remaining_text}Действует до <b>{expires}</b>\n"
                    f"Устройств в тарифе: <b>{device_count} {device_word}</b>"
                )
            else:
                text = (
                    f"<b>Подписка активна</b>\n\n"
                    f"{remaining_text}Действует до <b>{expires}</b>\n"
                    "Количество устройств можно проверить в разделе «Подписка»."
                )
        else:
            panel_client = None
            try:
                from xui_client import get_xui_client_details
                panel_client = await get_xui_client_details(user_id, strict=True)
            except Exception as exc:
                logger.warning("Could not read active panel subscription (%s)", type(exc).__name__)
            if _panel_client_is_active(panel_client):
                from constants import format_timestamp
                expires = format_timestamp(int(panel_client["expiry_time"]) // 1000)[:10]
                remaining_text = _remaining_days_label(int(panel_client["expiry_time"]) // 1000)
                device_count = int(panel_client.get("limit_hwid") or 0)
                if device_count in {2, 5, 10}:
                    device_word = "устройство" if device_count == 1 else "устройства" if device_count <= 4 else "устройств"
                    text = (
                        f"<b>Подписка активна</b>\n\n"
                        f"{remaining_text}Действует до <b>{expires}</b>\n"
                        f"Устройств в тарифе: <b>{device_count} {device_word}</b>"
                    )
                else:
                    text = (
                        f"<b>Подписка активна</b>\n\n"
                        f"{remaining_text}Действует до <b>{expires}</b>\n"
                        "Количество устройств можно проверить в разделе «Подписка»."
                    )
            else:
                text = "<b>Подписка активна</b>\n\nОткройте «Подписка», чтобы подключить устройство или посмотреть срок действия."
        # Show a real saved renewal state, not an alarming "unknown" label.
        if len(active_keys) == 1:
            try:
                from database import get_auto_renew_subscription
                renewal = await get_auto_renew_subscription(
                    user_id, int(active_keys[0]["id"]),
                )
                if renewal:
                    renewal_state = (
                        "включено"
                        if renewal.get("status") == "active"
                        and renewal.get("payment_method_id")
                        else "выключено"
                    )
                    text += f"\nАвтопродление: <b>{renewal_state}</b>"
            except Exception:
                logger.debug("Could not load renewal status on the home screen")
        # Direct connection only when there is exactly one active key.
        # With multiple active subscriptions, let the customer choose.
        if len(active_keys) == 1:
            kb = main_menu_active(
                int(active_keys[0]["id"]),
                str(active_keys[0].get("key") or ""),
                user_id=user_id,
                remaining_days=max(
                    1,
                    (int(active_keys[0]["expiry"]) - int(time.time()) + 86399) // 86400,
                ),
            )
        elif not active_keys and _panel_client_is_active(panel_client):
            # Keep the home screen usable if the VPN panel is authoritative but
            # SQLite has not yet imported the entitlement.
            kb = main_menu_panel_active(str(panel_client.get("subscription_url") or ""), user_id=user_id)
        else:
            kb = main_menu_with_keys(trial_used=True)

    # A saved checkout is a recovery opportunity, not a new marketing broadcast.
    # Only show it to customers without an active subscription and never charge
    # from a stored draft: the payment page recalculates the amount.
    if state in {"new", "existing", "expired"}:
        try:
            from database import get_checkout_draft
            draft = await get_checkout_draft(user_id)
        except Exception:
            draft = None
        if draft:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Продолжить оформление", callback_data="checkout_resume")],
                *kb.inline_keyboard,
            ])

    await send_with_photo(bot, target, text, kb, photo=MENU)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(F.text.startswith("/start"))
@monitor_performance("start_command")
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    """
    /start — register or return to main menu.
    Supports referral links: /start 123456
    """
    user_id = message.from_user.id
    user_name = message.from_user.full_name or message.from_user.first_name or "Друг"

    # Remember whether this is a genuinely new bot user before idempotent insert.
    is_first_start = await find_user_by_id(user_id) is None

    # Register user (idempotent)
    await ensure_user(user_id, message.from_user.username)

    # Process start parameters if provided
    args = message.text.split()
    is_new_referral = False
    referral_processed = False

    start_source = "direct"
    if len(args) > 1:
        start_param = args[1].strip()
        if start_param.startswith("src_") and start_param[4:]:
            start_source = start_param[4:][:80]
        elif start_param.lower().startswith("promo_"):
            start_source = "promo"
        elif start_param.startswith(("gw_", "giveaway_")):
            start_source = "giveaway"
        elif start_param.isdigit() or (start_param.startswith("ref_") and start_param[4:].isdigit()):
            start_source = "referral"
    if is_first_start:
        from database import log_analytics_event
        await log_analytics_event(user_id, "bot_started", source=start_source)

    if len(args) > 1 and args[1].strip().lower() == "login":
        await _handle_app_login(message, user_id, user_name)
        return

    if len(args) > 1 and args[1].strip().startswith("auth_"):
        auth_token = args[1].strip()
        try:
            from api_router import confirm_telegram_session
            # This function persists the bot confirmation asynchronously. Do
            # not tell the user it succeeded until that write has completed.
            confirmed = await confirm_telegram_session(
                token=auth_token,
                user_id=user_id,
                username=message.from_user.username or "",
                full_name=message.from_user.full_name or message.from_user.first_name or "",
            )
            if not confirmed:
                await message.answer(
                    "Эта ссылка для входа уже использована или истекла. Вернитесь в приложение и создайте новую."
                )
                return
            await message.answer(
                "✅ <b>Вход в приложение ByMeVPN подтверждён!</b>\n\n"
                "Нажмите «🚀 Открыть приложение» — оно автоматически войдёт в ваш аккаунт и загрузит подписку.\n\n"
                "Если приложение уже открыто, просто вернитесь в него.",
                parse_mode="HTML",
                reply_markup=app_open_kb(build_app_redirect_url()),
            )
            return
        except Exception as e:
            logger.error("Error confirming telegram session (%s)", type(e).__name__)
            await message.answer("Произошла ошибка при авторизации. Попробуйте ещё раз из приложения.")
            return

    # Promo deep link: https://t.me/<bot>?start=promo_CODE
    # Telegram opens the bot with /start promo_CODE; it cannot send /promo CODE
    # directly from an inline URL button.
    if len(args) > 1 and args[1].strip().lower().startswith("promo_"):
        code = args[1].strip()[6:].strip().upper()
        if not code or len(code) > 64:
            await message.answer("Промокод в ссылке указан неверно. Проверьте ссылку и попробуйте ещё раз.")
            return
        try:
            from database import validate_promo_code, has_user_used_promo
            promo = await validate_promo_code(code)
            if not promo:
                await message.answer(
                    "<b>Промокод недействителен</b>\n\n"
                    "Возможно, срок действия закончился или исчерпан лимит использований.",
                    parse_mode="HTML",
                )
                return
            if await has_user_used_promo(code, user_id):
                await message.answer(
                    "<b>Промокод уже использован</b>\n\nКаждый промокод можно применить один раз.",
                    parse_mode="HTML",
                )
                return

            from handlers.buy import _promo_purchase_keyboard
            promo_type = promo.get("promo_type", "percent")
            discount_value = promo.get("discount_value", 0)
            if promo_type == "percent":
                discount_text, promo_discount = f"{discount_value}%", discount_value
            elif promo_type == "fixed_rub":
                discount_text, promo_discount = f"{discount_value} ₽", 0
            elif promo_type == "free_days":
                discount_text, promo_discount = f"+{discount_value} дней", 0
            else:
                discount_text, promo_discount = str(discount_value), 0

            await state.update_data(promo_info=promo, promo_code=code, promo_discount=promo_discount)
            logger.info("Promo code %s activated via deep link for user %d", code, user_id)
            await message.answer(
                f"<b>Промокод активирован</b>\n\n"
                f"🎁 Код: <code>{code}</code>\n"
                f"💰 Бонус: <b>{discount_text}</b>\n\n"
                "Бонус автоматически применится при следующей покупке.",
                parse_mode="HTML",
                reply_markup=_promo_purchase_keyboard(),
            )
        except Exception:
            logger.exception("Could not activate promo deep link for user %d", user_id)
            await message.answer("Не удалось проверить промокод. Попробуйте ещё раз или введите /promo КОД.")
        return
    
    if len(args) > 1:
        param = args[1].strip()

        # Source tracking: /start src_<source> (vk, tiktok, website, telegram, etc.)
        if param.startswith("src_"):
            source = param[4:].strip()
            if source:
                from database import set_user_source, log_analytics_event
                await set_user_source(user_id, source)
                await log_analytics_event(user_id=user_id, event_type="source_visit", source=source)
                logger.info("User %d attributed to source: %s", user_id, source)

        # Giveaway link: /start gw_<id> or /start giveaway_<id>
        elif param.startswith("gw_") or param.startswith("giveaway_"):
            try:
                gw_id = int(param.split("_")[1])
                from database import join_giveaway, get_giveaway_entry_status, get_giveaway_by_id
                from urllib.parse import urlencode
                from database import set_user_source
                await set_user_source(user_id, "giveaway")

                contest = await get_giveaway_by_id(gw_id)
                status = await get_giveaway_entry_status(gw_id, user_id)
                if not contest or not status:
                    await message.answer("Не удалось найти этот конкурс. Откройте актуальную ссылку из сообщения бота.")
                    return
                if not status["is_open"]:
                    await message.answer(
                        "Приём участников завершён. Итоги конкурса бот подведёт автоматически.",
                    )
                    return

                already_joined = status["joined"]
                if not already_joined:
                    await join_giveaway(gw_id, user_id)
                    status = await get_giveaway_entry_status(gw_id, user_id)
                if not status or not status["is_open"]:
                    await message.answer("Приём участников завершён. Итоги конкурса бот подведёт автоматически.")
                    return

                bot_info = await get_cached_bot_info(bot)
                referral_link = f"https://t.me/{bot_info.username}?start={user_id}"
                share_url = "https://t.me/share/url?" + urlencode({
                    "url": referral_link,
                    "text": "Участвуй в ежемесячном конкурсе ByMeVPN: пригласи друзей и выиграй дни VPN.",
                })
                invite_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🤝 Пригласить друга", url=share_url, )
                ]])

                referral_count = status["referral_count"]
                from giveaway_automation import format_giveaway_terms
                terms = format_giveaway_terms(contest)
                if status["qualified"]:
                    entry_text = (
                        "✅ <b>Участие подтверждено</b>" if not already_joined
                        else "✅ <b>Вы участвуете</b>"
                    )
                    text = (
                        f"{entry_text} в ежемесячном конкурсе.\n"
                        f"Приглашённых в зачёте: <b>{referral_count}</b>.\n\n{terms}"
                    )
                else:
                    pending_intro = (
                        "📝 <b>Заявка уже сохранена</b>\n\n"
                        if already_joined else
                        "📝 <b>Заявка сохранена</b>\n\n"
                    )
                    text = pending_intro + (
                        f"Приглашённых в зачёте: <b>{referral_count}</b>.\n"
                        "Следующий шаг: пригласите минимум одного нового пользователя. Он попадёт в зачёт, когда впервые запустит бота по вашей ссылке.\n\n"
                        f"{terms}"
                    )
                await message.answer(text, parse_mode="HTML", reply_markup=invite_kb)
                return
            except Exception as e:
                logger.error("Error joining giveaway %s for user %d: %s", param, user_id, e)
                await message.answer("Не получилось открыть конкурс. Попробуйте ещё раз чуть позже.")
                return

        # Referral link: /start 123456 or /start ref_123456
        elif param.isdigit() or (param.startswith("ref_") and param[4:].isdigit()):
            ref_id = int(param[4:]) if param.startswith("ref_") else int(param)
            
            # Process referral (set referrer + give bonus)
            if ref_id != user_id and is_first_start and await find_user_by_id(ref_id):
                try:
                    from database import set_referrer, get_user_keys
                    from database import set_user_source
                    from referral_system_new import process_referral_click
                    
                    existing_keys = await get_user_keys(user_id)
                    if not existing_keys:
                        # Set referrer
                        await set_referrer(user_id, ref_id)
                        await set_user_source(user_id, "referral")

                        # Only acknowledge a referral if this inviter was newly
                        # attached to a genuinely new user. Repeated /start links
                        # must not look like fresh referrals or earn duplicate days.
                        is_new_referral = await get_referrer(user_id) == ref_id
                        referral_processed = is_new_referral
                        
                        # Process referral click using referral_system_new
                        if is_new_referral:
                            await process_referral_click(ref_id, user_id)
                        
                            try:
                                from referral_system_new import award_referral_signup_bonus
                                bonus_awarded = await award_referral_signup_bonus(bot, ref_id, user_id)
                                if bonus_awarded:
                                    try:
                                        from constants import REFERRAL_SIGNUP_BONUS_DAYS
                                        await bot.send_message(
                                            ref_id,
                                            "🎉 <b>По вашей ссылке зарегистрировался новый пользователь!</b>\n\n"
                                            f"Бонус <b>+{REFERRAL_SIGNUP_BONUS_DAYS} дней VPN</b> уже начислен автоматически.",
                                            parse_mode="HTML",
                                        )
                                    except Exception:
                                        logger.info("Could not send signup referral confirmation to %s", ref_id)
                            except Exception as bonus_error:
                                logger.exception("Could not grant referral signup bonus to %d: %s", ref_id, bonus_error)
                        
                        logger.info("Referral click from user %s with code %s", user_id, args[1])
                except Exception as e:
                    logger.error("Error processing referral: %s", e)

    # Send appropriate menu
    await _send_main_menu(
        bot, message, user_id, user_name,
        is_new_referral=is_new_referral and referral_processed
    )
    
    # Не удаляем сообщения после /start, чтобы избежать бесконечной кнопки Старт


async def _trial_feedback(callback: CallbackQuery, message: str) -> None:
    """Every trial outcome retains a direct recovery route."""
    await callback.message.answer(
        message,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Моя подписка", callback_data="my_keys")],
            [InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn")],
            [InlineKeyboardButton(text="Поддержка", url=support_url())],
        ]),
    )


async def _start_free_trial(bot: Bot, callback: CallbackQuery, ref_id: int | None = None) -> None:
    """Grant one shared trial to a new account with no prior key or unexpired access."""
    user_id = callback.from_user.id

    await safe_answer(callback)
    if await has_trial_used(user_id):
        await _trial_feedback(callback, 
            "Пробный период уже использован. Выберите тариф, чтобы продолжить пользоваться ByMeVPN."
        )
        return
    if await has_ever_had_key(user_id) or await get_user_keys(user_id):
        await _trial_feedback(callback, 
            "Пробный период доступен только новым аккаунтам без ранее оформленной подписки. "
            "Выберите тариф для продления."
        )
        return
    if await has_active_subscription(user_id):
        await _trial_feedback(callback, 
            "У вас уже есть активная подписка. Откройте раздел «Подписка», чтобы посмотреть доступ."
        )
        return

    try:
        from xui_client import get_xui_client_details
        panel_client = await get_xui_client_details(user_id, strict=True)
    except Exception as exc:
        logger.warning("Free trial eligibility check failed (%s)", type(exc).__name__)
        await _trial_feedback(callback, 
            "Не удалось проверить доступность пробного периода. Попробуйте позже или напишите в поддержку."
        )
        return
    if panel_client and not _panel_client_is_expired(panel_client):
        await _trial_feedback(callback, 
            "Не удалось подтвердить отсутствие действующего доступа. Пробный период не списан; попробуйте позже."
        )
        return

    pending = await get_trial_claim_reservation(user_id)
    if pending:
        if int(time.time()) - int(pending.get("created_at") or 0) < 120:
            await _trial_feedback(callback, 
                "Пробный период уже проверяется. Обновите статус через минуту."
            )
            return
        try:
            from xui_client import get_xui_client_details, get_xui_devices
            panel_client = await get_xui_client_details(user_id, strict=True)
            expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
            if panel_client and expiry > int(time.time()) and panel_client.get("enable") is not False:
                panel_devices = await get_xui_devices(user_id)
                if (int(panel_devices.get("limit_hwid") or 0) == 2
                        and int(panel_devices.get("limit_ip") or 0) == 0
                        and isinstance(panel_devices.get("devices"), list)):
                    await complete_trial_claim(user_id, pending["claim_id"])
                await _trial_feedback(callback, 
                    "Для аккаунта уже найдена активная подписка. Откройте приложение и обновите её статус."
                )
                return
            await release_trial_claim(user_id, pending["claim_id"])
        except Exception as exc:
            logger.warning("Free trial reservation reconciliation failed (%s)", type(exc).__name__)
            await _trial_feedback(callback, 
                "Не удалось проверить предыдущую попытку. Попробуйте позже или напишите в поддержку."
            )
            return

    claim_id = await reserve_trial_claim(user_id)
    if not claim_id:
        await _trial_feedback(callback, 
            "Пробный период уже использован или его активация ещё проверяется."
        )
        return

    try:
        from subscription import deliver_key
        trial_started_at = int(time.time())
        delivered = await deliver_key(
            bot=bot,
            user_id=user_id,
            chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}",
            days=3,
            limit_ip=2,
            is_paid=False,
            amount=0,
            currency="RUB",
            method="trial",
            extend_existing=False,
            delivery_target=callback,
        )
    except Exception as exc:
        logger.error("Free trial provisioning failed (%s)", type(exc).__name__)
        await _trial_feedback(callback, 
            "Не удалось подключить пробный период. Напишите в поддержку — поможем."
        )
        return

    if not delivered:
        return

    # ``deliver_key`` only returns true after the panel create/update call and
    # key persistence succeed. Confirm the exact trial limits once more before
    # spending the shared trial flag used by mobile and Telegram.
    try:
        from xui_client import get_xui_client_details, get_xui_devices
        panel_client = await get_xui_client_details(user_id, strict=True)
        panel_state = await get_xui_devices(user_id)
        expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        if (not isinstance(panel_client, dict) or panel_client.get("enable") is False
                or expiry <= int(time.time())
                or abs(expiry - (trial_started_at + 3 * 86400)) > 5
                or not isinstance(panel_state, dict)
                or int(panel_state.get("limit_hwid") or 0) != 2
                or int(panel_state.get("limit_ip") or 0) != 0
                or not isinstance(panel_state.get("devices"), list)):
            raise RuntimeError("Trial panel state is not confirmed")
        if not await complete_trial_claim(user_id, claim_id):
            raise RuntimeError("Trial reservation could not be completed")
    except Exception as exc:
        logger.warning("Free trial panel confirmation failed (%s)", type(exc).__name__)
        await _trial_feedback(callback, 
            "Пробный период создан, но проверка доступа ещё не завершена. Обновите статус позже."
        )
        return

    try:
        from database import log_analytics_event
        await log_analytics_event(
            user_id, "free_trial_activated", tariff="trial_3d", devices=2,
            amount=0, payment_provider="none",
        )
    except Exception as exc:
        logger.debug("Free trial analytics event failed (%s)", type(exc).__name__)


@router.callback_query(F.data == "trial_free")
async def cb_trial_free(callback: CallbackQuery, bot: Bot):
    await _start_free_trial(bot, callback)


@router.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery, bot: Bot):
    await _start_free_trial(bot, callback)


@router.callback_query(F.data == "trial_ref")
async def cb_trial_ref(callback: CallbackQuery, bot: Bot):
    await _start_free_trial(bot, callback)


@router.callback_query(F.data.startswith("claim_trial:"))
async def cb_claim_trial(callback: CallbackQuery, bot: Bot):
    await _start_free_trial(bot, callback)


# ---------------------------------------------------------------------------
# Back to menu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Return to main menu while preserving promo code if active."""
    # Preserve promo_info if it exists
    data = await state.get_data()
    promo_info = data.get("promo_info", {})

    await state.clear()

    # Restore promo_info if it was active
    if promo_info:
        await state.update_data(promo_info=promo_info)

    await safe_answer(callback)
    user_id = callback.from_user.id
    name = callback.from_user.first_name or "друг"
    await _send_main_menu(bot, callback, user_id, name)


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "<b>О сервисе ByMeVPN</b>\n\n"
        "Подключение на телефоне и компьютере через приложение. "
        "Поддерживаются планы на 2, 5 и 10 устройств.\n\n"
        "Новые пользователи могут попробовать сервис 3 дня бесплатно. "
        "\nАктуальные приложения для подключения доступны на странице «Подключить VPN».\n\n"
        "Если подключение не получается, откройте поддержку из меню."
    )
    # Keep this screen informative and minimal; primary actions live in the menu.
    from aiogram.types import InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Новости", url="https://t.me/ByMeVPN")],
        [InlineKeyboardButton(text="Документы", callback_data="legal")],
        [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb, photo=ABOUT)


@router.callback_query(F.data == "winback_feedback")
async def cb_winback_feedback(callback: CallbackQuery):
    await safe_answer(callback)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Не подключается", callback_data="winback_reason:connection")],
        [InlineKeyboardButton(text="Не устраивает цена", callback_data="winback_reason:price")],
        [InlineKeyboardButton(text="VPN больше не нужен", callback_data="winback_reason:not_needed")],
        [InlineKeyboardButton(text="Проблема с оплатой", callback_data="winback_reason:payment")],
        [InlineKeyboardButton(text="Другая причина", callback_data="winback_reason:other")],
        [InlineKeyboardButton(text="Повторить проверку", callback_data="back_to_menu")],
    ])
    await callback.message.answer(
        "Почему вы перестали пользоваться ByMeVPN? Ответ необязателен, "
        "достаточно выбрать один вариант.", reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("winback_reason:"))
async def cb_winback_reason(callback: CallbackQuery):
    from database import VALID_CHURN_REASONS, save_churn_feedback
    reason = callback.data.split(":", 1)[1]
    if reason not in VALID_CHURN_REASONS:
        await safe_answer(callback, "Неизвестный вариант.", alert=True)
        return
    await safe_answer(callback)
    try:
        saved = await save_churn_feedback(callback.from_user.id, reason)
    except Exception as exc:
        logger.error("Churn feedback storage failed (%s)", type(exc).__name__)
        await callback.message.answer("Не удалось сохранить ответ. Попробуйте позже.")
        return
    if not saved:
        await callback.message.answer("Ваш ответ уже учтён.")
        return
    if reason in {"connection", "payment"}:
        await callback.message.answer(
            "Спасибо за ответ. Если хотите восстановить доступ или решить проблему, "
            "откройте поддержку: @ByMeVPNSupportBot."
        )
    else:
        await callback.message.answer("Спасибо за ответ. Больше ничего делать не нужно.")


@router.callback_query(F.data == "free_vpn")
async def cb_free_vpn(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await _render_free_vpn(callback, bot)


async def _render_free_vpn(callback: CallbackQuery, bot: Bot):
    """Render a refreshed reward screen without answering a callback twice."""
    try:
        from database import log_analytics_event
        await log_analytics_event(callback.from_user.id, "channel_bonus_offered", source="free_days")
    except Exception:
        logger.debug("Could not record free-days offer exposure")
    from database import get_channel_subscription_bonus_status
    try:
        channel_status = await get_channel_subscription_bonus_status(callback.from_user.id)
    except Exception:
        logger.exception("Could not read channel reward state for user %d", callback.from_user.id)
        channel_status = None
    if channel_status == "awarded":
        reward_rows = [[
            InlineKeyboardButton(
                text="+10 дней начислены · моя подписка", callback_data="my_keys",
            ),
        ]]
        try:
            current_keys = await get_user_keys(callback.from_user.id)
            latest = max(current_keys, key=lambda k: int(k.get("expiry") or 0)) if current_keys else None
            if latest and int(latest.get("expiry") or 0) > int(time.time()):
                from constants import format_timestamp
                channel_note = (
                    "Бонус за подписку на канал начислен. "
                    f"Доступ действует до <b>{format_timestamp(int(latest['expiry']))[:10]}</b>.\n\n"
                )
            else:
                channel_note = "Бонус за канал начислен.\n\n"
        except Exception:
            channel_note = "Бонус за канал начислен.\n\n"
    elif channel_status == "reserved":
        reward_rows = [[
            InlineKeyboardButton(
                text="Проверить начисление +10 дней", callback_data="channel_bonus_check",
            ),
        ]]
        channel_note = (
            "Бонус за канал проверяется. Повторную покупку оформлять не нужно.\n\n"
        )
    else:
        reward_rows = [
            [InlineKeyboardButton(
                text="Подписаться на канал · +10 дней", url=CHANNEL_BONUS_URL,
            )],
            [InlineKeyboardButton(
                text="Проверить подписку · получить +10 дней",
                callback_data="channel_bonus_check", style="success",
            )],
        ]
        channel_note = ""
    text = (
        "<b>Бесплатные дни</b>\n\n"
        "<b>+10 дней</b> — за подписку на канал (один раз).\n"
        "<b>До +45 дней</b> — за приглашение друга и его первую оплату.\n"
        "<b>VPN за публикацию</b> — за размещение поста по условиям программы.\n\n"
        + channel_note
        + "Выберите способ:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *reward_rows,
        [InlineKeyboardButton(text="Пригласить друзей", callback_data="partner")],
        [InlineKeyboardButton(text="VPN за рекламу", callback_data="ad_program")],
        [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb, photo=FREE_VPN)




# ---------------------------------------------------------------------------
# My Keys handlers (moved to keys.py to avoid duplicates)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "channel_bonus_check")
async def cb_channel_bonus_check(callback: CallbackQuery, bot: Bot):
    """Award a one-time +10 day bonus after a live Telegram membership check."""
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_BONUS_CHAT_ID, user_id=user_id)
    except Exception as exc:
        logger.warning("Channel membership check failed (%s)", type(exc).__name__)
        await safe_answer(callback, "Не удалось проверить подписку. Попробуйте позже.", alert=True)
        return

    is_member = member.status in {"creator", "administrator", "member"}
    if member.status == "restricted":
        is_member = bool(getattr(member, "is_member", False))
    if not is_member:
        await safe_answer(callback, "Сначала подпишитесь на канал, затем нажмите «Проверить подписку».", alert=True)
        return

    from database import (
        reserve_channel_subscription_bonus, complete_channel_subscription_bonus,
        get_channel_subscription_bonus_status, log_analytics_event,
    )
    try:
        reserved = await reserve_channel_subscription_bonus(user_id)
    except Exception:
        logger.exception("Could not reserve channel reward for user %d", user_id)
        await safe_answer(callback, "Не удалось проверить выдачу бонуса. Попробуйте позже.", alert=True)
        return
    if not reserved:
        try:
            status = await get_channel_subscription_bonus_status(user_id)
        except Exception:
            logger.exception("Could not read channel reward state for user %d", user_id)
            status = None
        message = (
            "Бонус +10 дней уже начислен."
            if status == "awarded" else
            "Бонус уже проверяется. Повторите попытку позже; не нужно оформлять новый доступ."
        )
        await safe_answer(callback, message, alert=True)
        await _render_free_vpn(callback, bot)
        return

    try:
        from subscription import deliver_key
        from constants import DEFAULT_DEVICE_LIMIT
        keys = await get_user_keys(user_id)
        latest = max(keys, key=lambda item: int(item.get("expiry") or 0)) if keys else None
        delivered = await deliver_key(
            bot=bot, user_id=user_id, chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}", days=10,
            limit_ip=int(latest.get("limit_ip") or DEFAULT_DEVICE_LIMIT) if latest else DEFAULT_DEVICE_LIMIT,
            is_paid=False, amount=0, method="channel_bonus",
            payload=f"channel_bonus_{user_id}", extend_existing=True,
        )
    except Exception:
        logger.exception("Channel subscription bonus delivery failed for user %d", user_id)
        delivered = False

    if not delivered:
        # Keep the reservation after an uncertain result to prevent duplicate grants.
        await safe_answer(callback, "Запрос принят, выдача проверяется. Оплачивать доступ повторно не нужно.", alert=True)
        await _render_free_vpn(callback, bot)
        return

    try:
        recorded = await complete_channel_subscription_bonus(user_id)
    except Exception:
        logger.exception("Could not finalize channel bonus ledger for user %d", user_id)
        recorded = False
    if not recorded:
        # Actual provisioning may have succeeded. Never mark the user as
        # definitively awarded until the durable ledger confirms it.
        await safe_answer(
            callback, "Доступ мог быть продлён, но подтверждение ещё проверяется. "
            "Не нажимайте повторно; при необходимости обратитесь в поддержку.",
            alert=True,
        )
        await _render_free_vpn(callback, bot)
        return
    try:
        await log_analytics_event(user_id, "channel_bonus_awarded", source="telegram_channel")
    except Exception:
        logger.warning("Could not record channel bonus analytics for user %d", user_id)
    await safe_answer(callback, "Готово: начислено +10 дней.")
    await _render_free_vpn(callback, bot)
