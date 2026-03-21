"""
Core subscription logic: ask config name → create VPN key → deliver.
"""
import logging

from aiogram import Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from config import ADMIN_ID
from database import add_user_key, get_referrer, mark_referral_bonus_given, save_payment
from xui import create_client, build_vless_link
from keyboards import after_key_kb, cancel_kb
from states import BuyFlow
from utils import send_with_photo, LOGO_URL

logger = logging.getLogger(__name__)

# Referral bonus: 15 days for referrer when referred makes first paid purchase
REF_BONUS_DAYS = 15


async def ask_config_name(
    bot: Bot,
    target: "Message | CallbackQuery",
    state: FSMContext,
    context: dict,
) -> None:
    """Set FSM state, store context, prompt user for a config name."""
    await state.set_state(BuyFlow.waiting_name)
    await state.update_data(**context)

    days = context.get("days", 0)
    method = context.get("method", "")
    is_free = not context.get("is_paid", False)

    if method in ("trial", "trial_ref"):
        header = f"🎁 Пробный период — {days} дней бесплатно!\n\n"
    elif is_free:
        header = f"🎁 Бонус {days} дней!\n\n"
    else:
        header = ""

    text = (
        f"{header}"
        "✏️ <b>Введите название конфига</b>\n\n"
        "Например: <code>iPhone</code>, <code>Рабочий ноут</code>, <code>Домашний</code>\n\n"
        "Это имя будет видно в вашем приложении и в панели управления.\n\n"
        "⌨️ Просто напишите любое удобное название:"
    )
    await send_with_photo(bot, target, text, cancel_kb())


async def deliver_key(
    bot: Bot,
    user_id: int,
    chat_id: int,
    config_name: str,
    days: int,
    is_paid: bool = False,
    amount: int = 0,
    currency: str = "RUB",
    method: str = "trial",
    payload: str = "",
) -> bool:
    """
    Create 3x-UI client, store in DB, send key to user.
    Returns True on success.
    """
    try:
        logger.info("deliver_key: user=%d name='%s' days=%d method=%s", user_id, config_name, days, method)

        client_uuid = await create_client(config_name, days)
        if not client_uuid:
            raise RuntimeError(
                "3x-UI create_client returned None — "
                "check XUI_HOST, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID in .env"
            )

        vless_key = build_vless_link(client_uuid, config_name)
        await add_user_key(user_id, vless_key, config_name, days, client_uuid)

        if is_paid and amount > 0:
            await save_payment(user_id, amount, currency, method, days, payload)

        text = (
            "🎉 <b>Подписка успешно активирована!</b>\n\n"
            f"📋 Название: <b>{config_name}</b>\n"
            f"⏳ Срок: <b>{days} дней</b>\n\n"
            "🔑 <b>Ваш ключ:</b>\n"
            f"<code>{vless_key}</code>\n\n"
            "📱 Скопируйте ключ и импортируйте в приложение.\n\n"
            "🚀 Наслаждайтесь свободным и безопасным интернетом!"
        )
        await bot.send_photo(
            chat_id=chat_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
        )

        # Referral bonus: give referrer 15 days on first paid purchase
        if is_paid:
            referrer_id = await get_referrer(user_id)
            if referrer_id and referrer_id != user_id:
                first_time = await mark_referral_bonus_given(referrer_id, user_id)
                if first_time:
                    await _notify_referral_bonus(bot, referrer_id, user_id)

        logger.info("Key delivered: user=%d uuid=%s name='%s' days=%d", user_id, client_uuid, config_name, days)
        return True

    except Exception as e:
        logger.exception("deliver_key FAILED for user=%d name='%s': %s", user_id, config_name, e)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚨 <b>Ошибка создания ключа!</b>\n\n"
                f"👤 User: <code>{user_id}</code>\n"
                f"📝 Имя: {config_name}\n"
                f"⏳ Дней: {days}\n"
                f"🔧 Метод: {method}\n"
                f"❌ <code>{str(e)[:300]}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания VPN ключа</b>\n\n"
                "Не удалось создать ключ в панели управления.\n"
                "Пожалуйста, напишите в поддержку — мы поможем!\n\n"
                "📞 Поддержка: @ByMeVPN_support",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False


async def _notify_referral_bonus(bot: Bot, referrer_id: int, new_user_id: int) -> None:
    """
    Notify referrer: their referral just paid → bonus 15 days available.
    Referrer must press a button and enter a config name to activate.
    """
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🎁 Активировать +{REF_BONUS_DAYS} дней",
                callback_data=f"ref_bonus_activate:{new_user_id}",
            )
        ]])
        await bot.send_message(
            referrer_id,
            f"🎁 <b>Ваш реферал оформил подписку!</b>\n\n"
            f"Вам начислено <b>+{REF_BONUS_DAYS} дней</b> бесплатно.\n\n"
            "Для активации нажмите кнопку ниже и введите название конфига:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("Failed to notify referrer %d: %s", referrer_id, e)
