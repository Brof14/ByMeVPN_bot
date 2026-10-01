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

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    ensure_user, set_referrer,
    has_trial_used,
    has_active_subscription,
    has_ever_had_key, get_user_keys,
)
from keyboards import main_menu_new_user, main_menu_existing, main_menu_with_keys, back_to_menu, cancel_kb
from utils import send_with_photo, safe_answer, LOGO_URL
from async_utils import monitor_performance
from cache import cache_subscription_data

logger = logging.getLogger(__name__)
router = Router()

# Bonus days referrer gets when referral makes first paid purchase
REF_BONUS_DAYS = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@monitor_performance("user_state_check")
@cache_subscription_data
async def _user_state(user_id: int) -> str:
    """Определить состояние пользователя с использованием кэша."""
    # Выполняем все проверки параллельно для максимальной скорости
    tasks = [
        has_active_subscription(user_id),
        has_ever_had_key(user_id),
        has_trial_used(user_id),
    ]
    
    active, ever_had, trial_used = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Обрабатываем возможные исключения
    if isinstance(active, Exception):
        active = False
    if isinstance(ever_had, Exception):
        ever_had = False
    if isinstance(trial_used, Exception):
        trial_used = False
    
    if active:
        return "active"
    if ever_had or trial_used:
        return "expired"
    return "new"


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


def _referral_welcome_kb() -> InlineKeyboardMarkup:
    """Welcome keyboard for referral users (intro trial: 1 ₽ → 3 days)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Попробовать за 1 ₽", callback_data="trial_ref")],
    ])


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

    if is_new_referral and state == "new":
        # Referral landing - fire bonus text (when someone clicks referral link)
        text = (
            "🔥 Нормальный VPN сейчас найти сложно — либо дорогой, либо не работает.\n\n"
            "🎁 <b>3 дня ByMeVPN — за 1 ₽</b>\n\n"
            "Что получите сразу:\n"
            "• Telegram и YouTube работают без ограничений\n"
            "• Instagram, TikTok, сайты — открываются\n"
            "• Подключение за пару минут, без сложных настроек\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💳 Сегодня — 1 ₽\n"
            "🔁 Через 3 дня — 89 ₽/мес, автопродление каждые 30 дней\n"
            "🔓 Отключить автопродление можно в любой момент\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👇 Нажмите кнопку и подключитесь"
        )
        kb = _referral_welcome_kb()
    elif state == "new":
        # Main menu - standard welcome text
        text = (
            "<b>ByMeVPN — VPN, который просто работает.</b>\n\n"
            "Не грузятся YouTube, Telegram, Instagram или сайты? Подключите ByMeVPN — "
            "это займёт пару минут и не потребует сложных настроек.\n\n"
            "🎁 <b>3 дня за 1 ₽</b> — попробуйте прежде чем платить полную цену.\n"
            "💳 Дальше — 89 ₽/мес (или выгоднее при оплате за 3–12 месяцев).\n\n"
            "Наши приложения доступны для:\n"
            "<a href='https://apps.apple.com/ru/app/incy/id6756943388'>iOS</a>, "
            "<a href='https://play.google.com/store/apps/details?id=com.happproxy&pcampaignid=web_share'>Android</a>, "
            "<a href='https://github.com/Happ-proxy/happ-desktop/releases'>Windows</a>, "
            "<a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>macOS</a> и "
            "<a href='https://github.com/2dust/v2rayN/releases'>Linux</a>.\n\n"
            "После оплаты бот пришлёт ключ — просто вставьте его в приложение."
        )
        kb = main_menu_new_user()
    elif state == "expired":
        text = (
            f"<b>Здравствуйте, {user_name}!</b>\n\n"
            "Ваша подписка закончилась.\n\n"
            "Вы можете продлить VPN и дальше пользоваться сервисом без ограничений.\n\n"
            "Любой из наших тарифов даёт полный доступ к интернету на всех ваших устройствах.\n\n"
            "Чем дольше срок, тем больше вы экономите!"
        )
        # Check if user has keys to show appropriate menu (use direct DB check, not cache)
        user_keys = await get_user_keys(user_id)
        has_keys = len(user_keys) > 0
        trial_used = await has_trial_used(user_id)
        kb = main_menu_with_keys(trial_used=trial_used) if has_keys else main_menu_existing()
    else:  # active
        text = f"<b>Здравствуйте, {user_name}!</b>"
        # Check if user has keys to show appropriate menu (use direct DB check, not cache)
        user_keys = await get_user_keys(user_id)
        has_keys = len(user_keys) > 0
        trial_used = await has_trial_used(user_id)
        kb = main_menu_with_keys(trial_used=trial_used) if has_keys else main_menu_existing()

    if isinstance(target, Message):
        await bot.send_photo(
            chat_id=user_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=kb,
        )
    else:
        await send_with_photo(bot, target, text, kb)


# ---------------------------------------------------------------------------
# /proxy
# ---------------------------------------------------------------------------

@router.message(F.text == "/proxy")
@monitor_performance("proxy_command")
async def cmd_proxy(message: Message, bot: Bot) -> None:
    """Show proxy connection button."""
    text = (
        "🔗 <b>Подключение к прокси</b>\n\n"
        "Нажмите кнопку ниже, чтобы подключиться к прокси в Telegram.\n\n"
        "Это поможет обойти блокировки и использовать Telegram без ограничений."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить прокси", url="tg://proxy?server=hi.notmescat.net&port=7443&secret=ee4b9ba5fcb813d00ef6f7c5a0302f182f68692e6e6f746d65736361742e6e6574")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(F.text.startswith("/start"))
@monitor_performance("start_command")
async def cmd_start(message: Message, bot: Bot) -> None:
    """
    /start — register or return to main menu.
    Supports referral links: /start 123456
    """
    user_id = message.from_user.id
    user_name = message.from_user.full_name or message.from_user.first_name or "Друг"

    # Register user (idempotent)
    await ensure_user(user_id)

    # Process start parameters if provided
    args = message.text.split()
    is_new_referral = False
    referral_processed = False
    
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
                from database import join_giveaway
                joined = await join_giveaway(gw_id, user_id)
                if joined:
                    await message.answer(
                        "🎉 <b>Вы успешно зарегистрировались в розыгрыше!</b>\n\n"
                        "Результаты будут подведены автоматически. Удачи! 🍀",
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.error("Error joining giveaway %s for user %d: %s", param, user_id, e)

        # Referral link: /start 123456 or /start ref_123456
        elif param.isdigit() or (param.startswith("ref_") and param[4:].isdigit()):
            ref_id = int(param[4:]) if param.startswith("ref_") else int(param)
            
            # Process referral (set referrer + give bonus)
            if ref_id != user_id:
                try:
                    from database import set_referrer, get_user_keys, extend_key
                    from referral_system_new import process_referral_click
                    
                    existing_keys = await get_user_keys(user_id)
                    if not existing_keys:
                        # New referral - show welcome screen
                        is_new_referral = True
                        referral_processed = True
                        
                        # Set referrer
                        await set_referrer(user_id, ref_id)
                        
                        # Process referral click using referral_system_new
                        await process_referral_click(ref_id, user_id)
                        
                        # Extend referrer's key by 15 days
                        referrer_keys = await get_user_keys(ref_id)
                        if referrer_keys:
                            # Extend the first active key
                            for key in referrer_keys:
                                if key['expiry'] > int(time.time()):
                                    await extend_key(key['id'], 15)
                                    # Notify referrer with beautiful text
                                    try:
                                        await bot.send_message(
                                            ref_id,
                                            "🎊 <b>Привлекли нового реферала!</b>\n\n"
                                            "✨ По вашей ссылке перешёл новый пользователь\n"
                                            "🔑 Ваш ключ продлён на 15 дней\n"
                                            "💚 Продолжайте приглашать друзей!",
                                            parse_mode="HTML"
                                        )
                                    except Exception as notify_error:
                                        logger.error("Failed to notify referrer: %s", notify_error)
                                    break
                        else:
                            # Referrer has no active key - notify them anyway
                            try:
                                await bot.send_message(
                                    ref_id,
                                    "🎊 <b>Привлекли нового реферала!</b>\n\n"
                                    "✨ По вашей ссылке перешёл новый пользователь\n"
                                    "💡 У вас нет активного ключа для продления\n"
                                    "💚 Оформите подписку и получите +15 дней бонуса!",
                                    parse_mode="HTML"
                                )
                            except Exception as notify_error:
                                logger.error("Failed to notify referrer: %s", notify_error)
                        
                        # Send beautiful message to new user with button
                        try:
                            await bot.send_message(
                                user_id,
                                "🎁 <b>Вы перешли по реферальной ссылке</b>\n\n"
                                "🌟 <b>3 дня ByMeVPN — за 1 ₽</b>\n"
                                "🚀 Нажмите кнопку ниже, чтобы попробовать",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="🎁 Попробовать за 1 ₽", callback_data=f"claim_trial:{ref_id}")
                                ]])
                            )
                        except Exception as msg_error:
                            logger.error("Failed to send referral welcome message: %s", msg_error)
                        
                        logger.info("Referral click from user %s with code %s", user_id, args[1])
                except Exception as e:
                    logger.error("Error processing referral: %s", e)

    # Send appropriate menu
    await _send_main_menu(
        bot, message, user_id, user_name,
        is_new_referral=is_new_referral and referral_processed
    )
    
    # Не удаляем сообщения после /start, чтобы избежать бесконечной кнопки Старт


# ---------------------------------------------------------------------------
# Intro trial — 1 ₽ → 3 days of access → 89 ₽/мес recurring
# ---------------------------------------------------------------------------

async def _start_intro_trial(bot: Bot, callback: CallbackQuery, ref_id: int | None = None) -> None:
    """
    Common entry for the paid intro trial (1 ₽ / 3 days).

    - Eligible: no trial used before and no active key.
    - Trial flag is NOT burned on click — only after successful payment
      (webhook sets trial_used), so a failed payment doesn't lose the offer.
    - A fresh pending checkout link (< 30 min) is reused instead of creating
      a second payment.
    - Explicit recurring disclosure BEFORE payment (89 ₽/мес, cancel anytime).
    """
    from database import (
        has_trial_used, get_user_active_keys,
        get_yookassa_trial_pending, save_yookassa_trial_pending,
    )
    from constants import (
        INTRO_TRIAL_PRICE_RUB, INTRO_TRIAL_DAYS, INTRO_TRIAL_DEVICES,
        RECURRING_MONTHLY_PRICE,
    )
    from keyboards import trial_pay_kb

    user_id = callback.from_user.id

    trial_used = await has_trial_used(user_id)
    active_keys = await get_user_active_keys(user_id)
    if trial_used or active_keys:
        await safe_answer(callback, "Пробный период за 1 ₽ доступен один раз. Выберите тариф в разделе «Тарифы».", alert=True)
        return

    # Reuse a recent pending checkout to avoid duplicate 1 ₽ invoices
    pending = await get_yookassa_trial_pending(user_id)
    if pending and pending.get("confirmation_url") and int(time.time()) - (pending.get("created") or 0) < 1800:
        text = (
            "🎁 <b>3 дня ByMeVPN — 1 ₽</b>\n\n"
            f"Сегодня: <b>1 ₽</b>\n"
            f"Через {INTRO_TRIAL_DAYS} дня: <b>{RECURRING_MONTHLY_PRICE} ₽/мес</b> — "
            "далее каждые 30 дней, пока автопродление не отключено.\n\n"
            "🔓 Отключить автопродление можно в любой момент — "
            "доступ сохранится до конца оплаченного периода."
        )
        await send_with_photo(bot, callback, text, trial_pay_kb(pending["confirmation_url"]))
        return

    from payments import create_yookassa_payment
    confirmation_url = await create_yookassa_payment(
        amount_rub=INTRO_TRIAL_PRICE_RUB,
        description=f"ByMeVPN — {INTRO_TRIAL_DAYS} дня за {INTRO_TRIAL_PRICE_RUB} ₽ (далее {RECURRING_MONTHLY_PRICE} ₽/мес)",
        user_id=user_id,
        days=INTRO_TRIAL_DAYS,
        devices=INTRO_TRIAL_DEVICES,
        months=1,
        extra_metadata={"trial": "1"},
    )

    if not confirmation_url:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await safe_answer(callback, "Оплата временно недоступна, попробуйте чуть позже.", alert=True)
        await send_with_photo(
            bot, callback,
            "😔 Не удалось создать платёж. Попробуйте ещё раз через минуту "
            "или выберите обычный тариф.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💳 Тарифы", callback_data="buy_vpn"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu"),
            ]]),
        )
        return

    # Small click-bonus for the referrer (trial signup) — main reward is
    # granted only after a real recurring payment.
    if ref_id and ref_id != user_id:
        try:
            from referral_system_new import claim_referral_bonus
            await claim_referral_bonus(bot, ref_id, user_id, "trial_bonus")
        except Exception as e:
            logger.error("Failed to claim referral click bonus for referrer %s: %s", ref_id, e)

    text = (
        "🎁 <b>3 дня ByMeVPN — 1 ₽</b>\n\n"
        f"Сегодня: <b>1 ₽</b>\n"
        f"Через {INTRO_TRIAL_DAYS} дня: <b>{RECURRING_MONTHLY_PRICE} ₽/мес</b> — "
        "далее каждые 30 дней, пока автопродление не отключено.\n\n"
        "🔓 Отключить автопродление можно в любой момент — "
        "доступ сохранится до конца оплаченного периода.\n\n"
        "👇 Нажмите «Оплатить 1 ₽» — ключ придёт автоматически."
    )
    await send_with_photo(bot, callback, text, trial_pay_kb(confirmation_url))


@router.callback_query(F.data == "trial_1r")
async def cb_trial_1r(callback: CallbackQuery, bot: Bot):
    """Main menu: «Попробовать за 1 ₽»."""
    await safe_answer(callback)
    await _start_intro_trial(bot, callback)


@router.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery, bot: Bot):
    """Legacy alias: old trial buttons/messages now lead to the 1 ₽ intro trial."""
    await safe_answer(callback)
    await _start_intro_trial(bot, callback)


@router.callback_query(F.data == "trial_ref")
async def cb_trial_ref(callback: CallbackQuery, bot: Bot):
    """Referral welcome button: «Попробовать за 1 ₽»."""
    await safe_answer(callback)
    await _start_intro_trial(bot, callback)


@router.callback_query(F.data.startswith("claim_trial:"))
async def cb_claim_trial(callback: CallbackQuery, bot: Bot):
    """Referral deep-link claim button → same 1 ₽ intro trial flow."""
    await safe_answer(callback)
    try:
        ref_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        ref_id = None
    await _start_intro_trial(bot, callback, ref_id=ref_id)


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
        "ByMeVPN был создан в марте 2026 года.\n\n"
        "⚡️ Наш сервис работает на быстром и безопасном протоколе VLESS, поверх которого используется дополнительная маскировка трафика. Благодаря этому ByMeVPN умело обходит блокировки и работает во всех странах.\n\n"
        "👨‍💻 Мы используем специальные приложения для всех платформ. Начало работы с нашим сервисом максимально простое и не требует никаких специальных умений, не нужны никакие сложные инструкции.\n\n"
        "🔒 В нашем сервисе весь ваш трафик полностью зашифрован. Мы не храним логи и не видим, на какие сайты вы заходите. И никто не увидит.\n\n"
        "🌎 Наши сервера размещены по всему миру, и на любом из наших тарифов (даже на пробном) вы получаете полный доступ ко всем локациям. На одном сервере мы размещаем не более 10 клиентов – таким образом вы получаете максимальную скорость, до 10 Гбит/сек.\n\n"
        "👨‍👩‍👧‍👦 Доступны тарифы на 2, 5 или 10 устройств. Можно делиться вашим ключом от ByMeVPN с близкими.\n\n"
        "🌐 Подробнее о сервисе:\n"
        "https://bymevpn-site.duckdns.org"
    )
    # Создаем клавиатуру с кнопками "Назад" и "Поддержка" в одном ряду
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
         InlineKeyboardButton(text="Поддержка", url="https://t.me/ByMeVPN_support_bot")]
    ])
    await send_with_photo(bot, callback, text, kb)


# ---------------------------------------------------------------------------
# My Keys handlers (moved to keys.py to avoid duplicates)
# ---------------------------------------------------------------------------
