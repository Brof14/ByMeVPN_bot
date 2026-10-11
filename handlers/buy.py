"""
VPN purchase flow:
  buy_vpn → select type → select period → payment method → invoice/link
"""
import asyncio
import time
import logging
import hashlib
import hmac

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, LabeledPrice, PreCheckoutQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from constants import (
    PRICE_CONFIG, PERIOD_LABELS, TRIAL_DAYS,
    VALID_DEVICE_LIMITS, DEFAULT_DEVICE_LIMIT, DEVICE_CONFIG,
    get_price_for_months, get_monthly_display, validate_device_limit,
)
from config import PRICE_1_MONTH, DAYS_1M, BOT_TOKEN
from states import BuyFlow
from keyboards import tariff_selection_kb, payment_kb
from payments import create_yookassa_payment, create_crypto_payment
from subscription import deliver_key, ask_config_name
from database import (
    ensure_user,
    record_payment_idempotent, update_payment_status,
    use_promo_code, validate_promo_code, has_user_used_promo,
)
from utils import send_with_photo, send_or_edit, safe_answer, notify_admins

logger = logging.getLogger(__name__)
router = Router()

# Serialize duplicate Telegram Stars updates for the same charge within this process.
_stars_payment_locks: dict[str, asyncio.Lock] = {}


def _stars_payload_signature(user_id: int, days: int, devices: int, months: int,
                             amount: int, promo_code: str, timestamp: int) -> str:
    canonical = f"{user_id}|{days}|{devices}|{months}|{amount}|{promo_code}|{timestamp}"
    return hmac.new(BOT_TOKEN.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _build_stars_payload(user_id: int, days: int, devices: int, months: int,
                         amount: int, promo_code: str, timestamp: int) -> str:
    signature = _stars_payload_signature(user_id, days, devices, months, amount, promo_code, timestamp)
    return f"stars_{user_id}_{days}_{devices}_{months}_{amount}_{promo_code}_{timestamp}_{signature}"


def _parse_stars_payload(payload: str) -> dict | None:
    try:
        parts = payload.split("_")
        if len(parts) < 8 or parts[0] != "stars":
            return None
        user_id = int(parts[1])
        days = int(parts[2])
        devices = int(parts[3])
        months = int(parts[4])
        amount = int(parts[5])
        promo_code = "_".join(parts[6:-2])
        timestamp = int(parts[-2])
        signature = parts[-1]
        if (
            user_id <= 0 or days <= 0 or days > 3650
            or devices not in VALID_DEVICE_LIMITS
            or months not in (1, 3, 6, 12)
            or amount <= 0 or timestamp <= 0
        ):
            return None
        expected = _stars_payload_signature(user_id, days, devices, months, amount, promo_code, timestamp)
        if not hmac.compare_digest(signature, expected):
            return None
        return {
            "user_id": user_id, "days": days, "devices": devices,
            "months": months, "amount": amount, "promo_code": promo_code or None,
            "timestamp": timestamp,
        }
    except (ValueError, TypeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Step 1: Choose plan type & devices
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_vpn")
async def cb_buy_vpn(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(BuyFlow.choosing_period)
    
    # Check if user has active promo code
    data = await state.get_data()
    promo_discount = data.get("promo_discount", 0)
    devices = validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT))
    await state.update_data(devices=devices)
    
    dev_info = DEVICE_CONFIG.get(devices, DEVICE_CONFIG[DEFAULT_DEVICE_LIMIT])
    text = (
        f"<b>Тариф: {dev_info['name']} ({dev_info['badge']})</b>\n"
        f"<i>{dev_info['description']}</i>\n\n"
        "Чем дольше срок, тем ниже стоимость одного месяца.\n"
        "Все тарифы поддерживают стабильное и быстрое подключение.\n\n"
        "<b>Выберите срок подписки:</b>"
    )
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount),
    )


@router.callback_query(F.data.startswith("devtier_"))
async def cb_select_devtier(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Switch device tier (2, 5, 10 devices)."""
    await safe_answer(callback)
    try:
        devices = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        devices = DEFAULT_DEVICE_LIMIT
    devices = validate_device_limit(devices)

    data = await state.get_data()
    promo_discount = data.get("promo_discount", 0)
    await state.update_data(devices=devices)

    dev_info = DEVICE_CONFIG.get(devices, DEVICE_CONFIG[DEFAULT_DEVICE_LIMIT])
    renew_key_id = data.get("renew_key_id")
    if renew_key_id:
        text = (
            f"🔄 <b>Продление ключа</b>\n\n"
            f"<b>Тариф: {dev_info['name']} ({dev_info['badge']})</b>\n"
            f"<i>{dev_info['description']}</i>\n\n"
            "<b>Выберите срок подписки:</b>"
        )
    else:
        text = (
            f"<b>Тариф: {dev_info['name']} ({dev_info['badge']})</b>\n"
            f"<i>{dev_info['description']}</i>\n\n"
            "Чем дольше срок, тем ниже стоимость одного месяца.\n"
            "Все тарифы поддерживают стабильное и быстрое подключение.\n\n"
            "<b>Выберите срок подписки:</b>"
        )
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount),
    )


# ---------------------------------------------------------------------------
# Step 2: Choose period
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("tariff_"))
async def cb_select_tariff(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    parts = callback.data.split("_")
    months = int(parts[1])
    
    data = await state.get_data()
    if len(parts) >= 3:
        devices = validate_device_limit(int(parts[2]))
    else:
        devices = validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT))

    price_rub, days = get_price_for_months(months, devices)

    # Check for promo discount
    promo_info = data.get("promo_info", {})
    original_price = price_rub

    logger.info(f"Selecting tariff: months={months}, devices={devices}, original_price={original_price}, promo_info={promo_info}")

    if promo_info:
        promo_type = promo_info.get("promo_type", "percent")
        discount_value = promo_info.get("discount_value", 0)

        if promo_type == "percent":
            price_rub = round(price_rub * (100 - discount_value) / 100)
            promo_text = f" (скидка {discount_value}% применена)"
        elif promo_type == "fixed_rub":
            price_rub = max(0, price_rub - discount_value)
            promo_text = f" (скидка {discount_value} ₽ применена)"
        elif promo_type == "free_days":
            days += discount_value
            promo_text = f" (+{discount_value} дней бесплатно)"
        else:
            promo_text = ""
    else:
        promo_text = ""

    await state.update_data(months=months, devices=devices, price_rub=price_rub, days=days, original_price=original_price)

    period_name = PERIOD_LABELS.get(months, f"{months} мес.")
    dev_name = DEVICE_CONFIG.get(devices, {}).get("name", f"{devices} устройств")

    text = (
        f"<b>Вы покупаете доступ на {days} дней.</b>\n\n"
        f"📱 Устройств: <b>{dev_name}</b>\n"
        f"💰 Стоимость: <b>{price_rub} ₽</b>{promo_text}\n"
    )

    if promo_info and promo_info.get("promo_type") in ["percent", "fixed_rub"]:
        text += f"<s>Без скидки: {original_price} ₽</s>\n"

    text += (
        "\nОплачивая подписку, вы соглашаетесь с <a href='https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-ByMeVPN-03-12'>политикой обработки персональных данных</a>, с <a href='https://telegra.ph/DOGOVOR-PUBLICHNOJ-OFERTY-ByMyVPN-03-12'>договором оферты</a> и с <a href='https://telegra.ph/SOGLASHENIE-O-REGULYARNYH-REKURRENTNYH-PLATEZHAH-ByMeVPN-03-12'>соглашением о присоединении к рекуррентной системе платежей</a>.\n\n"
        "После оплаты подписка будет активирована автоматически.\n\n"
        "<b>Выберите способ оплаты:</b>"
    )

    await send_with_photo(bot, callback, text, payment_kb(price_rub, days, devices=devices))


# ---------------------------------------------------------------------------
# Step 3: Choose payment method
# ---------------------------------------------------------------------------

async def _get_checkout_params(state: FSMContext) -> dict | None:
    """
    Read the tariff selected in the buy flow.

    After a bot restart (MemoryStorage) the FSM is empty. Falling back to a
    1-month price would silently charge the WRONG amount for a tariff the
    user selected — so instead we return None and re-ask the user to pick a
    tariff.
    """
    data = await state.get_data()
    if "price_rub" not in data or "days" not in data:
        return None
    return {
        "price_rub": int(data["price_rub"]),
        "days": int(data["days"]),
        "months": int(data.get("months", 1)),
        "devices": validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT)),
        "promo_code": data.get("promo_code", ""),
    }


async def _ask_tariff_again(bot: Bot, callback: CallbackQuery) -> None:
    from keyboards import tariff_selection_kb
    await send_with_photo(
        bot, callback,
        "⏳ <b>Сессия устарела</b>\n\nПожалуйста, выберите тариф заново — "
        "это займёт несколько секунд.",
        tariff_selection_kb(),
    )


# Legacy handler for old period_* callbacks
@router.callback_query(F.data.startswith("period_"))
async def cb_select_period_legacy(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    await send_with_photo(
        bot, callback,
        "<b>Выберите срок подписки</b>\n\nЧем дольше срок, тем ниже стоимость одного месяца.",
        tariff_selection_kb(),
    )


# ---------------------------------------------------------------------------
# Payment: Telegram Stars  (Stars = rubles, 1:1, intentional)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    params = await _get_checkout_params(state)
    if not params:
        await _ask_tariff_again(bot, callback)
        return
    price_rub: int = params["price_rub"]
    days: int = params["days"]
    months: int = params["months"]
    devices: int = params["devices"]
    promo_code: str = params["promo_code"]
    user_id = callback.from_user.id

    # Stars amount = rubles (1:1)
    stars = price_rub
    payload = _build_stars_payload(user_id, days, devices, months, stars, promo_code or "", int(time.time()))
    if len(payload.encode("utf-8")) > 128:
        await safe_answer(callback, "Промокод слишком длинный для счёта Stars. Уберите его и попробуйте снова.", alert=True)
        return

    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=f"ByMeVPN — {days} дней",
            description=f"VPN подписка на {days} дней ({devices} устр., VLESS + Reality)",
            payload=payload,
            provider_token="",  # Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN на {days} дней", amount=stars)],
        )
    except Exception as e:
        logger.error("Stars invoice error for user %d: %s", user_id, e)
        try:
            await bot.send_message(
                chat_id=user_id,
                text="❌ Ошибка при создании платежа. Попробуйте ещё раз.",
                parse_mode="HTML"
            )
        except Exception as msg_error:
            logger.error("Failed to send error message to user %d: %s", user_id, msg_error)


# ---------------------------------------------------------------------------
# Payment: YooKassa
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_yookassa")
async def cb_pay_yookassa(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    params = await _get_checkout_params(state)
    if not params:
        await _ask_tariff_again(bot, callback)
        return
    price_rub: int = params["price_rub"]
    days: int = params["days"]
    months: int = params["months"]
    devices: int = params["devices"]
    promo_code: str = params["promo_code"]
    user_id = callback.from_user.id

    url = await create_yookassa_payment(
        price_rub, f"ByMeVPN {days} дней ({devices} устр.)", user_id, days, devices, promo_code=promo_code, months=months,
    )

    if not url:
        await safe_answer(
            callback,
            "ЮKassa временно недоступна. Пожалуйста, выберите оплату через Telegram Stars.",
            alert=True,
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price_rub} ₽", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data=f"devtier_{devices}")]
    ])
    
    from payments import is_yookassa_recurring_supported
    ar_note = ""
    if is_yookassa_recurring_supported():
        ar_note = "🔄 <b>Автопродление:</b> включено (по истечении срока подписка продлится автоматически, можно отключить в любой момент в меню ключа).\n\n"

    await send_with_photo(
        bot, callback,
        f"💳 <b>Оплата через ЮKassa</b>\n\n"
        f"Нажмите кнопку ниже для перехода на страницу оплаты.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до {devices} одновременно\n\n"
        f"{ar_note}"
        f"После оплаты подписка будет активирована автоматически.",
        kb,
    )


# ---------------------------------------------------------------------------
# Payment: Crypto Bot (@send)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    params = await _get_checkout_params(state)
    if not params:
        await _ask_tariff_again(bot, callback)
        return
    price_rub: int = params["price_rub"]
    days: int = params["days"]
    months: int = params["months"]
    devices: int = params["devices"]
    promo_code: str = params["promo_code"]
    user_id = callback.from_user.id

    result = await create_crypto_payment(
        price_rub, f"ByMeVPN {days} дней ({devices} устр.)", user_id, days, devices, promo_code=promo_code,
    )

    if not result:
        await safe_answer(
            callback,
            "Crypto Pay временно недоступен. Пожалуйста, выберите другой способ оплаты.",
            alert=True,
        )
        return

    url, invoice_id = result

    from database import add_crypto_pending
    await add_crypto_pending(invoice_id, user_id, days, devices, price_rub)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"₿ Оплатить {price_rub} ₽", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data=f"devtier_{devices}")]
    ])
    
    await send_with_photo(
        bot, callback,
        f"₿ <b>Оплата через Crypto Pay</b>\n\n"
        f"Нажмите кнопку ниже для перехода на страницу оплаты.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до {devices} одновременно\n\n"
        f"После оплаты подписка будет активирована автоматически.",
        kb,
    )


# ---------------------------------------------------------------------------
# Pre-checkout confirmation (required by Telegram for Stars)
# ---------------------------------------------------------------------------

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    order = _parse_stars_payload(pre.invoice_payload or "")
    if (
        not order
        or order["user_id"] != pre.from_user.id
        or pre.currency != "XTR"
        or pre.total_amount != order["amount"]
    ):
        logger.warning("Rejected invalid Telegram Stars pre-checkout query for user %d", pre.from_user.id)
        await pre.answer(
            ok=False,
            error_message="Данные заказа не совпадают. Вернитесь в бот и создайте новый счёт.",
        )
        return
    await pre.answer(ok=True)


# ---------------------------------------------------------------------------
# Successful Stars payment → idempotent delivery
# ---------------------------------------------------------------------------

@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user(user_id)

    payment = message.successful_payment
    charge_id = payment.telegram_payment_charge_id or payment.invoice_payload
    lock = _stars_payment_locks.setdefault(charge_id, asyncio.Lock())
    async with lock:
        await _process_successful_stars_payment(message, bot, state, payment, charge_id)


async def _process_successful_stars_payment(
    message: Message, bot: Bot, state: FSMContext, payment, charge_id: str
) -> None:
    user_id = message.from_user.id
    payload = payment.invoice_payload
    stars = payment.total_amount
    currency = payment.currency  # XTR

    # The invoice payload is the immutable order snapshot. Never use mutable FSM
    # state to decide what was paid for.
    order = _parse_stars_payload(payload)
    try:
        if not order or order["user_id"] != user_id:
            raise ValueError("invalid or unsigned invoice payload")
        days = order["days"]
        devices = order["devices"]
        months = order["months"]
        promo_code = order["promo_code"]
        if currency != "XTR" or stars != order["amount"] or stars <= 0:
            raise ValueError("Stars currency or amount does not match signed order")
    except (ValueError, TypeError, IndexError) as e:
        logger.error("Invalid Stars invoice payload for user %d, charge %s: %s", user_id, charge_id, e)
        await record_payment_idempotent(
            user_id=user_id, amount=stars, currency=currency, method="stars",
            days=0, payload=payload, status="recovery_required",
            tariff="Stars payment — manual review", devices=DEFAULT_DEVICE_LIMIT,
            provider="stars", provider_payment_id=charge_id,
        )
        try:
            from config import ADMIN_ID
            await notify_admins(bot, f"⚠️ Оплата Stars требует ручной сверки: user <code>{user_id}</code>, "
                f"charge <code>{charge_id}</code>, payload некорректен.",
                parse_mode="HTML",
            )
            await bot.send_message(
                user_id,
                "⚠️ Telegram подтвердил оплату, но данные заказа не удалось проверить. "
                "Не оплачивайте повторно — поддержка проверит платёж.",
            )
        except Exception:
            pass
        return

    try:
        await message.delete()
    except Exception:
        pass

    tariff_name = f"Stars {days} дней ({devices} устр.)"
    is_new, pay_db_id = await record_payment_idempotent(
        user_id=user_id,
        amount=stars,
        currency=currency,
        method="stars",
        days=days,
        payload=charge_id,
        status="processing",
        tariff=tariff_name,
        devices=devices,
        provider="stars",
        provider_payment_id=charge_id,
    )

    if not is_new:
        from database import get_payment_by_id
        existing_payment = await get_payment_by_id(pay_db_id) if pay_db_id else None
        existing_status = existing_payment.get("status") if existing_payment else None
        if existing_status == "success":
            logger.info("Stars duplicate payment already fulfilled: charge_id=%s user=%d", charge_id, user_id)
            return
        if existing_status == "failed":
            logger.warning("Retrying failed Stars provisioning: charge_id=%s user=%d", charge_id, user_id)
            await update_payment_status(pay_db_id, "processing")
        else:
            await update_payment_status(pay_db_id, "recovery_required")
            logger.error(
                "Stars payment %s has ambiguous status %r; manual reconciliation required",
                charge_id, existing_status,
            )
            try:
                from config import ADMIN_ID
                await notify_admins(bot, f"⚠️ Платёж Stars <code>{charge_id}</code> пользователя <code>{user_id}</code> "
                    f"имеет неоднозначный статус {existing_status!r}. Нужна ручная сверка.",
                    parse_mode="HTML",
                )
                await bot.send_message(
                    user_id,
                    "⚠️ Оплата найдена, но статус выдачи требует проверки. "
                    "Не оплачивайте повторно — поддержка проверит платёж.",
                )
            except Exception:
                pass
            return

    success = await deliver_key(
        bot=bot,
        user_id=user_id,
        chat_id=message.chat.id,
        config_name=f"ByMeVPN_{user_id}",
        days=days,
        limit_ip=devices,
        is_paid=True,
        amount=stars,
        currency=currency,
        method="stars",
        payload=charge_id,
        extend_existing=True,
    )

    if success:
        await update_payment_status(pay_db_id, "success")
        if promo_code:
            await use_promo_code(promo_code, user_id)
        await state.clear()

        # Referral reward: single canonical path (idempotent, one per pair).
        from referral_system_new import award_referral_for_payment
        await award_referral_for_payment(user_id, charge_id, bot=bot)
    else:
        await update_payment_status(pay_db_id, "failed")
        logger.error("Stars delivery failed for user %d, charge_id %s", user_id, charge_id)
        try:
            from config import ADMIN_ID
            await notify_admins(bot, f"⚠️ Не удалось выдать подписку по Stars: user <code>{user_id}</code>, "
                f"charge <code>{charge_id}</code>. Платёж можно безопасно повторить через обработчик failed.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Handle config name input after YooKassa payment
# ---------------------------------------------------------------------------

@router.message(F.text, BuyFlow.waiting_for_config_name)
async def handle_yookassa_config_name(message: Message, bot: Bot, state: FSMContext):
    """Handle config name input after a YooKassa or Crypto Bot payment.

    Both payment methods put the user into this same state after a
    successful (verified) payment; we look up whichever pending record
    exists for this user and deliver the key. YooKassa is checked first
    since it existed originally; Crypto Bot pending is checked as a
    fallback (a user can only have one pending purchase at a time in
    practice, so this is safe and avoids adding a second FSM state).
    """
    user_id = message.from_user.id
    config_name = message.text.strip()

    if not config_name:
        await message.answer("❌ Пожалуйста, введите имя конфига.")
        return

    # Get pending payment data — check YooKassa first, then Crypto Bot
    from database import get_yookassa_pending_by_user, get_crypto_pending_by_user
    pending = await get_yookassa_pending_by_user(user_id)
    method = "yookassa"
    payment_id = None

    if pending:
        payment_id = pending.get("payment_id", "")
    else:
        pending = await get_crypto_pending_by_user(user_id)
        if pending:
            method = "cryptobot"
            payment_id = pending.get("invoice_id", "")

    if not pending:
        await message.answer("❌ Платеж не найден. Пожалуйста, свяжитесь с поддержкой.")
        await state.clear()
        return

    # Extract data from pending payment
    days = pending.get("days", 30)
    devices = validate_device_limit(pending.get("devices", DEFAULT_DEVICE_LIMIT))
    amount_rub = pending.get("amount_rub", 0)

    # Clear state before delivery
    await state.clear()

    # Deliver key
    from subscription import deliver_key
    # Extend existing key for paid renewals, create new for trials
    extend_existing = (method != "trial")
    success = await deliver_key(
        bot=bot,
        user_id=user_id,
        chat_id=message.chat.id,
        config_name=config_name,
        days=days,
        limit_ip=devices,
        is_paid=True,
        amount=amount_rub,
        currency="RUB",
        method=method,
        payload=str(payment_id),
        extend_existing=extend_existing,
    )

    if success:
        # Delete pending record
        if method == "yookassa":
            from database import delete_yookassa_pending
            try:
                await delete_yookassa_pending(payment_id)
            except Exception as e:
                logger.error("Could not delete pending yk payment %s: %s", payment_id, e)
        else:
            from database import delete_crypto_pending
            try:
                await delete_crypto_pending(payment_id)
            except Exception as e:
                logger.error("Could not delete pending crypto payment %s: %s", payment_id, e)
    else:
        await message.answer("❌ Ошибка при выдаче ключа. Пожалуйста, свяжитесь с поддержкой.")


# ---------------------------------------------------------------------------
# Promo Code Callback Handler (for button activation)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("activate_promo:"))
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    """Activate promo code via button click."""
    await safe_answer(callback)
    
    # Extract promo code from callback data
    code = callback.data.split(":", 1)[1].strip().upper()
    user_id = callback.from_user.id

    # Validate promo code
    promo = await validate_promo_code(code)
    if not promo:
        await callback.message.edit_text(
            "❌ <b>Промокод не действителен</b>\n\n"
            "Возможные причины:\n"
            "• Код истёк\n"
            "• Достигнут лимит использований\n"
            "• Код не существует\n"
            "• Код не начался ещё",
            parse_mode="HTML"
        )
        return

    # Check if user already used this promo code
    if await has_user_used_promo(code, user_id):
        await callback.message.edit_text(
            "❌ <b>Вы уже использовали этот промокод</b>\n\n"
            "Каждый промокод можно использовать только один раз.",
            parse_mode="HTML"
        )
        return

    # Save promo info to state for next purchase (without burning yet!)
    promo_type = promo["promo_type"]
    discount_value = promo["discount_value"]

    if promo_type == "percent":
        discount_text = f"{discount_value}%"
        promo_discount = discount_value
    elif promo_type == "fixed_rub":
        discount_text = f"{discount_value} ₽"
        promo_discount = 0
    elif promo_type == "free_days":
        discount_text = f"+{discount_value} дней"
        promo_discount = 0
    else:
        discount_text = f"{discount_value}"
        promo_discount = 0

    await state.update_data(promo_info=promo, promo_code=code, promo_discount=promo_discount)

    logger.info(f"Promo code {code} activated via button for user {user_id} with type {promo_type} value {discount_value}")

    await callback.message.edit_text(
        f"✅ <b>Промокод активирован!</b>\n\n"
        f"🎁 Код: <code>{code}</code>\n"
        f"💰 Бонус: <b>{discount_text}</b>\n\n"
        f"При следующей покупке VPN бонус будет автоматически применён.\n\n"
        f"👉 Нажмите кнопку ниже для покупки VPN!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить VPN", callback_data="buy_vpn")]
        ])
    )


# ---------------------------------------------------------------------------
# Promo Code Command
# ---------------------------------------------------------------------------

@router.message(F.text.startswith("/promo"))
async def cmd_promo(message: Message, state: FSMContext):
    """Activate promo code."""
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "🎁 <b>Использование промокода</b>\n\n"
                "Введите: <code>/promo КОД</code>\n\n"
                "Например: <code>/promo SALE20</code>\n\n"
                "Промокод даст скидку при следующей покупке VPN.",
                parse_mode="HTML"
            )
            return

        code = parts[1].strip().upper()
        user_id = message.from_user.id

        # Validate promo code
        promo = await validate_promo_code(code)
        if not promo:
            await message.answer(
                "❌ <b>Промокод не действителен</b>\n\n"
                "Возможные причины:\n"
                "• Код истёк\n"
                "• Достигнут лимит использований\n"
                "• Код не существует\n"
                "• Код не начался ещё",
                parse_mode="HTML"
            )
            return

        # Check if user already used this promo code
        if await has_user_used_promo(code, user_id):
            await message.answer(
                "❌ <b>Вы уже использовали этот промокод</b>\n\n"
                "Каждый промокод можно использовать только один раз.",
                parse_mode="HTML"
            )
            return

        # Save promo info to state for next purchase (without burning yet!)
        promo_type = promo["promo_type"]
        discount_value = promo["discount_value"]

        if promo_type == "percent":
            discount_text = f"{discount_value}%"
            promo_discount = discount_value
        elif promo_type == "fixed_rub":
            discount_text = f"{discount_value} ₽"
            promo_discount = 0
        elif promo_type == "free_days":
            discount_text = f"+{discount_value} дней"
            promo_discount = 0
        else:
            discount_text = f"{discount_value}"
            promo_discount = 0

        await state.update_data(promo_info=promo, promo_code=code, promo_discount=promo_discount)

        logger.info(f"Promo code {code} activated for user {user_id} with type {promo_type} value {discount_value}")

        await message.answer(
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💰 Бонус: <b>{discount_text}</b>\n\n"
            f"При следующей покупке VPN бонус будет автоматически применён.\n\n"
            f"👉 Нажмите /buy чтобы купить VPN!",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error("Promo code error: %s", e)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")
