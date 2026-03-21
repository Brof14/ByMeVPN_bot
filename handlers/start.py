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

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import TRIAL_DAYS
from database import (
    ensure_user, get_referrer, set_referrer,
    has_used_trial, mark_trial_used,
    has_active_subscription, has_paid_subscription,
    has_ever_had_key,
)
from keyboards import main_menu_new_user, main_menu_existing, back_to_menu, cancel_kb
from utils import send_with_photo, safe_answer, LOGO_URL
from subscription import ask_config_name, deliver_key
from states import BuyFlow

logger = logging.getLogger(__name__)
router = Router()

# Bonus days referrer gets when referral makes first paid purchase
REF_BONUS_DAYS = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _user_state(user_id: int) -> str:
    if await has_active_subscription(user_id):
        return "active"
    if await has_ever_had_key(user_id) or await has_used_trial(user_id):
        return "expired"
    return "new"


async def _clean_chat(bot: Bot, chat_id: int, anchor_msg_id: int, count: int = 50) -> None:
    ids = list(range(anchor_msg_id, anchor_msg_id - count, -1))
    tasks = [bot.delete_message(chat_id, mid) for mid in ids if mid > 0]
    await asyncio.gather(*tasks, return_exceptions=True)


def _referral_welcome_kb() -> InlineKeyboardMarkup:
    """Single-button keyboard for users who came via referral link."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Забрать 3 дня бесплатно", callback_data="trial_ref")],
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
        # Special referral welcome: one big button, no other menu
        text = (
            "Вы перешли по ссылке друга!\n\n"
            "Вам доступен бонус — <b>3 дня бесплатного VPN</b>.\n\n"
            "ByMeVPN — надёжный VPN-сервис:\n"
            "• Высокая скорость соединения\n"
            "• Надёжное шифрование\n"
            "• iOS, Android, Windows, macOS, Linux\n\n"
            "Нажмите кнопку ниже, чтобы получить ключ:"
        )
        kb = _referral_welcome_kb()
    elif state == "new":
        text = (
            f"Привет, {user_name}!\n\n"
            "ByMeVPN — твой надёжный VPN-сервис\n\n"
            "Что ты получаешь:\n"
            "• Высокая скорость соединения\n"
            "• Надёжное шифрование\n"
            "• Все устройства: iOS, Android, Windows, macOS, Linux\n"
            "• Работает с популярными платформами\n"
            "• От 33₽/мес\n\n"
            "Пробный период 3 дня бесплатно — попробуй прямо сейчас!\n\n"
            "Пригласи друга и получи 3 дня бесплатно!\n\n"
            "Выбирай действие ниже:"
        )
        kb = main_menu_new_user()
    elif state == "expired":
        text = (
            "Ваша подписка закончилась\n\n"
            "Вы можете продлить ВПН и дальше пользоваться сервисом без ограничений."
        )
        kb = main_menu_existing()
    else:  # active
        text = f"С возвращением, {user_name}!"
        kb = main_menu_existing()

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

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    name = message.from_user.first_name or "друг"
    await ensure_user(user_id)

    # Handle referral parameter
    args = message.text.split()
    is_new_referral = False
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        existing_ref = await get_referrer(user_id)
        if ref_id != user_id and not existing_ref:
            await set_referrer(user_id, ref_id)
            # Only show special screen if user is new (never had trial/key)
            trial_used = await has_used_trial(user_id)
            ever_key = await has_ever_had_key(user_id)
            if not trial_used and not ever_key:
                is_new_referral = True
            # Notify referrer
            try:
                await bot.send_message(
                    ref_id,
                    "👤 По вашей ссылке перешёл новый пользователь!\n"
                    "Вы получите +15 дней, когда он оплатит подписку.",
                )
            except Exception:
                pass

    # Clean previous messages
    await _clean_chat(bot, message.chat.id, message.message_id, count=50)
    await _send_main_menu(bot, message, user_id, name, is_new_referral=is_new_referral)


# ---------------------------------------------------------------------------
# Back to menu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await safe_answer(callback)
    user_id = callback.from_user.id
    name = callback.from_user.first_name or "друг"
    await _send_main_menu(bot, callback, user_id, name)


# ---------------------------------------------------------------------------
# Trial — regular (from main menu)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    user_id = callback.from_user.id

    if await has_used_trial(user_id) or await has_ever_had_key(user_id):
        await safe_answer(callback, "Пробный период доступен только новым пользователям.", alert=True)
        return

    await mark_trial_used(user_id)
    await ask_config_name(
        bot, callback, state,
        context={
            "days": TRIAL_DAYS, "prefix": "trial", "is_paid": False,
            "amount": 0, "currency": "RUB", "method": "trial",
            "payload": f"trial_{user_id}", "_trial_user_id": user_id,
        },
    )


# ---------------------------------------------------------------------------
# Trial — referral version (from referral welcome screen)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "trial_ref")
async def cb_trial_ref(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """
    Referral welcome button: "Забрать 3 дня бесплатно"
    Same logic as regular trial but activated from referral welcome screen.
    """
    await safe_answer(callback)
    user_id = callback.from_user.id

    if await has_used_trial(user_id) or await has_ever_had_key(user_id):
        # Already used — show normal menu
        name = callback.from_user.first_name or "друг"
        await _send_main_menu(bot, callback, user_id, name)
        return

    await mark_trial_used(user_id)
    await ask_config_name(
        bot, callback, state,
        context={
            "days": TRIAL_DAYS, "prefix": "trial_ref", "is_paid": False,
            "amount": 0, "currency": "RUB", "method": "trial",
            "payload": f"trial_ref_{user_id}", "_trial_user_id": user_id,
        },
    )


# ---------------------------------------------------------------------------
# Config name FSM handler (shared: trial, buy, ref bonus)
# ---------------------------------------------------------------------------

@router.message(StateFilter(BuyFlow.waiting_name), F.text)
async def receive_config_name(message: Message, bot: Bot, state: FSMContext):
    name_input = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not name_input or len(name_input) > 50:
        await bot.send_message(
            message.chat.id,
            "⚠️ Название должно быть от 1 до 50 символов.\nПопробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return

    data = await state.get_data()
    await state.clear()

    if not data:
        logger.error("receive_config_name: empty FSM data for user %d", message.from_user.id)
        await bot.send_message(message.chat.id, "❌ Ошибка сессии. Начните заново — /start")
        return

    user_id = message.from_user.id
    days     = data.get("days", 30)
    is_paid  = data.get("is_paid", False)
    amount   = data.get("amount", 0)
    currency = data.get("currency", "RUB")
    method   = data.get("method", "unknown")
    payload  = data.get("payload", "")
    trial_uid = data.get("_trial_user_id")

    logger.info("Config name: user=%d name='%s' days=%d method=%s", user_id, name_input, days, method)

    success = await deliver_key(
        bot=bot, user_id=user_id, chat_id=message.chat.id,
        config_name=name_input, days=days, is_paid=is_paid,
        amount=amount, currency=currency, method=method, payload=payload,
    )

    # Unmark trial if delivery failed
    if not success and trial_uid:
        from database import DB_FILE
        import aiosqlite
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("UPDATE users SET trial_used=0 WHERE user_id=?", (trial_uid,))
                await db.commit()
        except Exception as e:
            logger.error("Could not unmark trial for %d: %s", trial_uid, e)


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "🌐 <b>О сервисе ByMeVPN</b>\n\n"
        "ByMeVPN работает с 2026 года.\n\n"
        "⚡️ Современные протоколы, стабильное соединение, надёжный обход блокировок.\n\n"
        "🔒 Весь трафик шифруется. Мы не храним логи.\n\n"
        "🏆 <b>Наши преимущества:</b>\n"
        "• Высокая скорость соединения\n"
        "• Все устройства: iOS, Android, Windows, macOS, Linux\n"
        "• Стабильное подключение 24/7\n"
        "• Поддержка VLESS + Reality\n\n"
        "📞 Поддержка: @ByMeVPN_support"
    )
    await send_with_photo(bot, callback, text, back_to_menu())
