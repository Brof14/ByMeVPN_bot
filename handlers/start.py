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
    ensure_user, set_referrer, find_user_by_id, get_referrer,
    has_trial_used,
    has_active_subscription,
    has_ever_had_key, get_user_keys,
)
from keyboards import main_menu_new_user, main_menu_existing, main_menu_with_keys, back_to_menu, cancel_kb, support_url
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
    
    # A database outage must not turn a paying customer into a "new" user.
    if any(isinstance(value, Exception) for value in (active, ever_had, trial_used)):
        logger.error("Could not determine account state for user %d", user_id)
        return "unknown"
    
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
            "Не удалось загрузить состояние подписки. Это временный сбой, "
            "ваш доступ и платежи не изменились. Попробуйте ещё раз через минуту."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Поддержка", url=support_url())],
            [InlineKeyboardButton(text="← В меню", callback_data="back_to_menu")],
        ])
        if isinstance(target, Message):
            await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        else:
            await send_with_photo(bot, target, text, kb)
        return

    if state == "new":
        if is_new_referral:
            text = (
                "<b>Добро пожаловать в ByMeVPN!</b>\n\n"
                "Приглашение учтено. Подключение займёт несколько шагов — бот подскажет, как установить приложение и добавить ключ.\n"
                "🇷🇺 Российские сайты и приложения работают с VPN — отключать его не нужно.\n\n"
                "Попробуйте 3 дня за 1 ₽ или выберите тариф. Бесплатный доступ за приглашения — в отдельном разделе."
            )
        else:
            text = (
                "<b>ByMeVPN — VPN для сайтов и приложений, которыми вы пользуетесь каждый день.</b>\n\n"
                "Подключение займёт несколько шагов — бот подскажет, как установить приложение и добавить ключ.\n"
                "🇷🇺 Российские сайты и приложения доступны без отключения VPN и дополнительных настроек.\n\n"
                "Попробуйте 3 дня за 1 ₽ или выберите тариф. Получать VPN бесплатно можно за приглашения друзей."
            )
        kb = main_menu_new_user()
    elif state == "expired":
        text = (
            f"<b>Здравствуйте, {user_name}!</b>\n\n"
            "Срок подписки закончился, но ваши ключи и настройки сохранены.\n\n"
            "Продлите доступ или получите бонусные дни за приглашения. Всё для подключения и управления ключами — в «Моих ключах»."
        )
        # Check if user has keys to show appropriate menu (use direct DB check, not cache)
        user_keys = await get_user_keys(user_id)
        has_keys = len(user_keys) > 0
        trial_used = await has_trial_used(user_id)
        kb = main_menu_with_keys(trial_used=trial_used) if has_keys else main_menu_existing(trial_used=trial_used)
    else:  # active
        # Check if user has keys to show appropriate menu (use direct DB check, not cache)
        user_keys = await get_user_keys(user_id)
        has_keys = len(user_keys) > 0
        trial_used = await has_trial_used(user_id)
        active_keys = [k for k in user_keys if k.get("expiry", 0) > int(time.time())]
        if active_keys:
            active_key = max(active_keys, key=lambda k: k.get("expiry", 0))
            from constants import format_timestamp
            expires = format_timestamp(active_key["expiry"])[:10]
            device_count = active_key.get("limit_ip", 2)
            text = (
                f"<b>Здравствуйте, {user_name}!</b>\n\n"
                f"✅ VPN активен до <b>{expires}</b>\n"
                f"📱 Устройств: <b>{device_count}</b>\n"
                "🇷🇺 Российские сайты и приложения доступны без отключения VPN.\n\n"
                "В «Моих ключах» — подключение и настройки подписки. Бонусные дни можно получать за приглашения."
            )
        else:
            text = f"<b>Здравствуйте, {user_name}!</b>"
        kb = main_menu_with_keys(trial_used=trial_used) if has_keys else main_menu_existing(trial_used=trial_used)

    if isinstance(target, Message):
        await bot.send_photo(
            chat_id=user_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=kb,
        )
    else:
        await send_with_photo(bot, target, text, kb)


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

    # Remember whether this is a genuinely new bot user before idempotent insert.
    is_first_start = await find_user_by_id(user_id) is None

    # Register user (idempotent)
    await ensure_user(user_id, message.from_user.username)

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
                from database import join_giveaway, get_giveaway_entry_status, get_giveaway_by_id
                from urllib.parse import urlencode

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

                bot_info = await bot.get_me()
                referral_link = f"https://t.me/{bot_info.username}?start={user_id}"
                share_url = "https://t.me/share/url?" + urlencode({
                    "url": referral_link,
                    "text": "Участвуй в ежемесячном конкурсе ByMeVPN: пригласи друзей и выиграй дни VPN.",
                })
                invite_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🤝 Пригласить друга", url=share_url, style="primary")
                ]])

                referral_count = status["referral_count"]
                results_line = ""
                if contest.get("end_date"):
                    from giveaway_automation import _moscow_date
                    results_line = f"\n\nИтоги и начисление призов: <b>{_moscow_date(contest['end_date'])}</b>."
                if status["qualified"]:
                    entry_text = (
                        "✅ <b>Участие подтверждено</b>" if not already_joined
                        else "✅ <b>Вы участвуете</b>"
                    )
                    text = (
                        f"{entry_text} в ежемесячном конкурсе.\n\n"
                        f"В зачёте: <b>{referral_count}</b> новых приглашённых в этом месяце. "
                        "Учитываются люди, которые впервые запустили бота по вашей ссылке.\n\n"
                        f"Три участника с наибольшим числом приглашённых получат по <b>+{contest['prize_days']} дней VPN</b>. "
                        "Если участников с приглашениями меньше трёх, приз получит каждый; при равном счёте место определит жребий."
                        f"{results_line}"
                    )
                else:
                    pending_intro = (
                        "📝 <b>Заявка уже сохранена</b>\n\n"
                        if already_joined else
                        "📝 <b>Заявка сохранена</b>\n\n"
                    )
                    text = pending_intro + (
                        "Чтобы попасть в рейтинг, нужен минимум один новый приглашённый в этом месяце. "
                        "Он засчитается, когда впервые запустит бота по вашей ссылке; одного перехода недостаточно. "
                        "После регистрации друга условие выполнится автоматически.\n\n"
                        f"Три участника с наибольшим числом приглашённых получат по <b>+{contest['prize_days']} дней VPN</b>. "
                        "Если участников с приглашениями меньше трёх, приз получит каждый."
                        f"{results_line}"
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
                    from referral_system_new import process_referral_click
                    
                    existing_keys = await get_user_keys(user_id)
                    if not existing_keys:
                        # Set referrer
                        await set_referrer(user_id, ref_id)

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
                                await award_referral_signup_bonus(bot, ref_id, user_id)
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
            f"Если способ оплаты сохранится: через {INTRO_TRIAL_DAYS} дня — "
            f"<b>{RECURRING_MONTHLY_PRICE} ₽ за следующие 30 дней</b>, затем каждые 30 дней, пока автопродление не отключено.\n"
            "Если способ оплаты не сохранится, автосписаний не будет — доступ можно продлить вручную.\n\n"
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

    text = (
        "🎁 <b>3 дня ByMeVPN — 1 ₽</b>\n\n"
        f"Сегодня: <b>1 ₽</b>\n"
        f"Если способ оплаты сохранится: через {INTRO_TRIAL_DAYS} дня — "
        f"<b>{RECURRING_MONTHLY_PRICE} ₽ за следующие 30 дней</b>, затем каждые 30 дней, пока автопродление не отключено.\n"
        "Если способ оплаты не сохранится, автосписаний не будет — доступ можно продлить вручную.\n\n"
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
        "<b>ByMeVPN — VPN для привычных сайтов и приложений.</b>\n\n"
        "Подключите телефон или компьютер за несколько шагов: бот поможет установить приложение и добавить ключ.\n"
        "🇷🇺 Российские сайты и приложения доступны с включённым VPN — без дополнительных настроек.\n\n"
        "Можно начать с пробных 3 дней за 1 ₽ или получать бонусные дни за приглашения — без привязки карты. "
        "Для владельцев Telegram-каналов есть программа VPN за рекламу.\n\n"
        "Управление ключами и автопродлением — в разделе «Мои ключи». Условия оплаты бот показывает до покупки."
    )
    # Keep the menu navigation in a fixed, full-width bottom row.
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать тариф", callback_data="buy_vpn")],
        [InlineKeyboardButton(text="← Назад в меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb)


@router.callback_query(F.data == "free_vpn")
async def cb_free_vpn(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "<b>Получить VPN бесплатно</b>\n\n"
        "Выберите подходящий способ:\n\n"
        "<b>Пригласить друзей</b> — получать бонусные дни за приглашения. "
        "Карта и оплата не нужны.\n\n"
        "<b>Реклама в канале</b> — для владельцев Telegram-каналов. "
        "Бонус зависит от размера аудитории."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пригласить друзей", callback_data="partner")],
        [InlineKeyboardButton(text="VPN за рекламу", callback_data="ad_program")],
        [InlineKeyboardButton(text="← Назад в меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb)




# ---------------------------------------------------------------------------
# My Keys handlers (moved to keys.py to avoid duplicates)
# ---------------------------------------------------------------------------
