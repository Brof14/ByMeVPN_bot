"""
VPN purchase flow:
  buy_vpn → select type → select period → payment method → invoice/link
"""
import time
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, LabeledPrice, PreCheckoutQuery, Message,
)

from config import (
    BASE_PRICE_1_MONTH, BASE_PRICE_6_MONTHS, BASE_PRICE_12_MONTHS, BASE_PRICE_24_MONTHS,
    PRICE_1_MONTH_2D, PRICE_6_MONTHS_2D, PRICE_12_MONTHS_2D, PRICE_24_MONTHS_2D,
    PRICE_1_MONTH_5D, PRICE_6_MONTHS_5D, PRICE_12_MONTHS_5D, PRICE_24_MONTHS_5D,
    DAYS_1M, DAYS_6M, DAYS_12M, DAYS_24M,
)
from states import BuyFlow
from keyboards import (
    plan_type_kb, period_kb_1d, period_kb_2d, period_kb_5d, payment_kb,
)
from payments import create_yookassa_payment
from subscription import ask_config_name
from database import ensure_user
from utils import send_with_photo, safe_answer

logger = logging.getLogger(__name__)
router = Router()


# Price table: (devices, months) → (price_rub, days)
_PRICES: dict[tuple, tuple] = {
    (1, 1):  (BASE_PRICE_1_MONTH,   DAYS_1M),
    (1, 6):  (BASE_PRICE_6_MONTHS,  DAYS_6M),
    (1, 12): (BASE_PRICE_12_MONTHS, DAYS_12M),
    (1, 24): (BASE_PRICE_24_MONTHS, DAYS_24M),
    (2, 1):  (PRICE_1_MONTH_2D,  DAYS_1M),
    (2, 6):  (PRICE_6_MONTHS_2D, DAYS_6M),
    (2, 12): (PRICE_12_MONTHS_2D, DAYS_12M),
    (2, 24): (PRICE_24_MONTHS_2D, DAYS_24M),
    (5, 1):  (PRICE_1_MONTH_5D,  DAYS_1M),
    (5, 6):  (PRICE_6_MONTHS_5D, DAYS_6M),
    (5, 12): (PRICE_12_MONTHS_5D, DAYS_12M),
    (5, 24): (PRICE_24_MONTHS_5D, DAYS_24M),
}

_DEVICE_LABELS = {1: "Персональный", 2: "Для двоих", 5: "Семья"}
_PERIOD_LABELS = {
    1:  "1 месяц",
    6:  "6 месяцев + 2 мес.🎁",
    12: "1 год + 3 мес.🎁",
    24: "2 года + 6 мес.🎁",
}


# ---------------------------------------------------------------------------
# Step 1: Choose plan type
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_vpn")
async def cb_buy_vpn(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(BuyFlow.choosing_type)
    await send_with_photo(
        bot, callback,
        "Выберите тариф\n\nЧем больше срок, тем ниже стоимость одного месяца.",
        plan_type_kb(),
    )


# ---------------------------------------------------------------------------
# Step 2: Choose period
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("type_"))
async def cb_select_type(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    plan = callback.data.split("_", 1)[1]
    devices = {"personal": 1, "duo": 2, "family": 5}.get(plan)
    if devices is None:
        return

    await state.update_data(devices=devices)
    await state.set_state(BuyFlow.choosing_period)

    text = "Выберите срок подписки\n\nЧем больше срок, тем ниже стоимость одного месяца."
    kb = {1: period_kb_1d, 2: period_kb_2d, 5: period_kb_5d}[devices]()
    await send_with_photo(bot, callback, text, kb)


# ---------------------------------------------------------------------------
# Step 3: Choose payment method
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("period_"))
async def cb_select_period(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    data = await state.get_data()
    devices = data.get("devices", 1)
    months = int(callback.data.split("_", 1)[1])

    price_rub, days = _PRICES.get((devices, months), (BASE_PRICE_1_MONTH, DAYS_1M))
    await state.update_data(months=months, price_rub=price_rub, days=days)

    tariff_name = _DEVICE_LABELS.get(devices, f"{devices} уст.")
    period_name = _PERIOD_LABELS.get(months, f"{months} мес.")

    text = (
        f"<b>{period_name} ({tariff_name})</b>\n\n"
        f"Срок доступа: {days} дней\n"
        f"Стоимость: {price_rub} ₽\n\n"
        "После оплаты введите название конфига и получите ключ.\n\n"
        "Оплачивая подписку, вы соглашаетесь с условиями оферты.\n\n"
        "Выберите способ оплаты:"
    )
    await send_with_photo(bot, callback, text, payment_kb(price_rub, days))


# ---------------------------------------------------------------------------
# Payment: Telegram Stars  (Stars = rubles, 1:1, intentional)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    data = await state.get_data()
    price_rub: int = data.get("price_rub", BASE_PRICE_1_MONTH)
    days: int = data.get("days", DAYS_1M)
    devices: int = data.get("devices", 1)
    user_id = callback.from_user.id

    # Stars amount = rubles (1:1) — intentional, to cover Telegram commission
    stars = price_rub
    # Encode devices in payload so we can recover if FSM is lost
    payload = f"stars_{user_id}_{days}_{devices}_{int(time.time())}"

    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="ByMeVPN — подписка",
            description=f"Доступ к VPN на {days} дней (VLESS + Reality)",
            payload=payload,
            provider_token="",  # empty string for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN на {days} дней", amount=stars)],
        )
    except Exception as e:
        logger.error("Stars invoice error for user %d: %s", user_id, e)
        await safe_answer(
            callback,
            "Ошибка отправки инвойса. Попробуйте ещё раз.",
            alert=True,
        )


# ---------------------------------------------------------------------------
# Payment: YooKassa
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_yookassa")
async def cb_pay_yookassa(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    data = await state.get_data()
    price_rub: int = data.get("price_rub", BASE_PRICE_1_MONTH)
    days: int = data.get("days", DAYS_1M)
    devices: int = data.get("devices", 1)
    user_id = callback.from_user.id

    url = await create_yookassa_payment(
        price_rub, f"ByMeVPN {days} дней", user_id, days, devices,
    )

    if not url:
        await safe_answer(
            callback,
            "ЮKassa временно недоступна. Пожалуйста, выберите оплату через Telegram Stars.",
            alert=True,
        )
        return

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price_rub} ₽", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu")],
    ])
    await send_with_photo(
        bot, callback,
        f"💳 <b>Оплата через ЮKassa</b>\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n\n"
        "После оплаты напишите /start или ожидайте автоматическое уведомление.",
        kb,
    )


# ---------------------------------------------------------------------------
# Pre-checkout confirmation (required by Telegram for Stars)
# ---------------------------------------------------------------------------

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    await pre.answer(ok=True)


# ---------------------------------------------------------------------------
# Successful Stars payment → ask config name → deliver key
# ---------------------------------------------------------------------------

@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user(user_id)

    payment = message.successful_payment
    payload = payment.invoice_payload
    stars = payment.total_amount
    currency = payment.currency  # XTR

    # Parse days and devices from payload: "stars_{user_id}_{days}_{devices}_{ts}"
    parts = payload.split("_")
    try:
        days = int(parts[2])
    except Exception:
        days = DAYS_1M
    try:
        # New format has devices at index 3; old format (4 parts) falls back to FSM
        devices_from_payload = int(parts[3]) if len(parts) >= 5 else None
    except Exception:
        devices_from_payload = None

    # Retrieve devices from FSM; payload value is authoritative fallback if FSM is gone
    data = await state.get_data()
    fsm_devices = data.get("devices")
    devices = fsm_devices or devices_from_payload or 1
    # Validate: only allowed values are 1, 2, 5
    if devices not in (1, 2, 5):
        devices = 1

    try:
        await message.delete()
    except Exception:
        pass

    # Ask for config name before delivering key
    await ask_config_name(
        bot, message, state,
        context={
            "days": days,
            "prefix": "stars",
            "is_paid": True,
            "amount": stars,
            "currency": currency,
            "method": "stars",
            "payload": payload,
            "limit_ip": devices,
        },
    )


# ---------------------------------------------------------------------------
# YooKassa: user taps "Получить ключ" after successful payment
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("yk_deliver:"))
async def cb_yk_deliver(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """
    User taps the button after YooKassa payment notification.
    Loads pending payment context and asks for config name.
    """
    await safe_answer(callback)
    user_id = callback.from_user.id

    try:
        payment_id = callback.data.split(":", 1)[1]
    except IndexError:
        await safe_answer(callback, "Неверный формат.", alert=True)
        return

    from database import get_pending_yookassa_payment, ensure_user
    await ensure_user(user_id)
    pending = await get_pending_yookassa_payment(payment_id)

    if not pending:
        await safe_answer(
            callback,
            "Этот платёж уже обработан или не найден. "
            "Проверьте раздел «Мои ключи» или напишите в поддержку.",
            alert=True,
        )
        return

    # Security: only the owner of the payment can redeem it
    if pending["user_id"] != user_id:
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    devices = pending["devices"]
    # Validate just in case
    if devices not in (1, 2, 5):
        logger.warning("YooKassa pending payment %s has invalid devices=%d, defaulting to 1", payment_id, devices)
        devices = 1

    logger.info("cb_yk_deliver: payment_id=%s pending=%s devices=%d", payment_id, pending, devices)

    await ask_config_name(
        bot, callback, state,
        context={
            "days": pending["days"],
            "prefix": "yookassa",
            "is_paid": True,
            "amount": pending["amount_rub"],
            "currency": "RUB",
            "method": "yookassa",
            "payload": payment_id,
            "limit_ip": devices,
            "_yk_payment_id": payment_id,  # used by receive_config_name to clean up pending
        },
    )
