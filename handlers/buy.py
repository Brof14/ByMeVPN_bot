"""
VPN purchase flow:
  buy_vpn → select type → select period → payment method → invoice/link
"""
import asyncio
import time
import logging
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    Message,
    InlineKeyboardMarkup,
)

from constants import (
    PRICE_CONFIG, PERIOD_LABELS, TRIAL_DAYS,
    VALID_DEVICE_LIMITS, DEFAULT_DEVICE_LIMIT, DEVICE_CONFIG,
    get_price_for_months, get_monthly_display, validate_device_limit,
)
from config import PRICE_1_MONTH, DAYS_1M
from states import BuyFlow
from keyboards import tariff_selection_kb, payment_kb
from payments import create_yookassa_payment, create_crypto_payment, claim_yookassa_checkout, release_yookassa_checkout
from subscription import deliver_key
from database import (
    ensure_user,
    record_payment_idempotent, update_payment_status,
    use_promo_code, validate_promo_code, has_user_used_promo,
)
from utils import send_with_photo, send_or_edit, safe_answer
from bot_assets import PLANS, RENEWAL, PAYMENT, PROMO_CODE
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

# Serialize duplicate Telegram Stars updates for the same charge within this process.
_stars_payment_locks: dict[str, asyncio.Lock] = {}


def _promo_purchase_keyboard() -> InlineKeyboardMarkup:
    """One consistent green action to continue to tariff selection."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn")
    ]])


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
    renew_key_id = data.get("renew_key_id")
    await state.update_data(devices=devices)
    
    dev_info = DEVICE_CONFIG.get(devices, DEVICE_CONFIG[DEFAULT_DEVICE_LIMIT])
    text = (
        f"<b>{dev_info['name']} · {dev_info['badge']}</b>\n"
        f"{dev_info['description']}\n\n"
        "На кнопках указана цена за месяц. При выборе срока покажем полную сумму заранее."
    )
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount),
        photo=RENEWAL if renew_key_id else PLANS,
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
            f"<b>Продление · {dev_info['name']}</b>\n"
            f"{dev_info['description']}\n\n"
            "Цена указана за месяц; итог за выбранный срок покажем до оплаты."
        )
    else:
        text = (
            f"<b>{dev_info['name']} · {dev_info['badge']}</b>\n"
            f"{dev_info['description']}\n\n"
            "На кнопках указана цена за месяц. При выборе срока покажем полную сумму заранее."
        )
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount),
        photo=RENEWAL if renew_key_id else PLANS,
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
    from database import log_analytics_event
    await log_analytics_event(
        callback.from_user.id, "tariff_selected", tariff=f"{months}m", devices=devices,
        amount=price_rub, promo=promo_info.get("code") if promo_info else None,
    )

    dev_name = DEVICE_CONFIG.get(devices, {}).get("name", f"{devices} устройств")

    text = (
        f"<b>Ваш заказ</b>\n\n"
        f"Устройства: <b>{dev_name}</b>\n"
        f"Срок: <b>{days} дней</b>\n"
        f"Итого: <b>{price_rub} ₽</b>{promo_text}\n"
    )

    if promo_info and promo_info.get("promo_type") in ["percent", "fixed_rub"]:
        text += f"<s>Без скидки: {original_price} ₽</s>\n"

    text += (
        "\nПродолжая, вы принимаете условия <a href='https://telegra.ph/Publichnaya-oferta-na-okazanie-uslug-ByMeVPN-10-25'>оферты</a> и "
        "<a href='https://telegra.ph/Politika-v-otnoshenii-obrabotki-personalnyh-dannyh-ByMeVPN-10-25'>политики конфиденциальности</a>.\n\n"
        "Доступ включится после подтверждения оплаты.\n\n"
        "<b>Перед оплатой:</b> если ЮKassa сохранит способ оплаты, подписка может продлеваться "
        f"на {days} дней за {price_rub} ₽ по окончании срока. Отключение — в разделе "
        "«Подписка» → «Оплата и автопродление». Stars и CryptoBot — разовая оплата.\n\n"
        "<b>Выберите способ оплаты</b>"
    )

    await send_with_photo(
        bot, callback, text, payment_kb(price_rub, days, devices=devices), photo=PAYMENT,
    )


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
        "<b>Время оформления истекло</b>\n\nВыберите тариф ещё раз — "
        "это займёт несколько секунд.",
        tariff_selection_kb(),
        photo=PLANS,
    )


# Legacy handler for old period_* callbacks
@router.callback_query(F.data.startswith("period_"))
async def cb_select_period_legacy(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    await send_with_photo(
        bot, callback,
        "<b>Выберите срок подписки</b>\n\nЦена на кнопке указана за месяц. Итоговую сумму покажем перед оплатой.",
        tariff_selection_kb(),
        photo=PLANS,
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
    payload = f"stars_{user_id}_{days}_{devices}_{months}_{promo_code}_{int(time.time())}"

    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=f"ByMeVPN — {days} дней",
            description=f"Подписка на {days} дней, до {devices} устройств",
            payload=payload,
            provider_token="",  # Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN на {days} дней", amount=stars)],
        )
        from database import log_analytics_event
        await log_analytics_event(
            user_id, "checkout_started", tariff=f"{months}m", devices=devices,
            amount=price_rub, payment_provider="stars",
        )
    except Exception as e:
        logger.error("Stars invoice error for user %d: %s", user_id, e)
        try:
            await bot.send_message(
                chat_id=user_id,
                text="Не удалось создать платёж. Попробуйте снова.",
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

    if not claim_yookassa_checkout(user_id):
        await callback.message.answer("Запрос на оплату уже выполняется. Подождите немного.")
        return
    try:
        url = await create_yookassa_payment(
            price_rub, f"ByMeVPN {days} дней ({devices} устр.)", user_id, days, devices, promo_code=promo_code, months=months,
        )
    finally:
        release_yookassa_checkout(user_id)

    if not url:
        await safe_answer(callback)
        support_text = quote_plus(
            f"Здравствуйте! Не открывается оплата ByMeVPN через ЮKassa. "
            f"Хочу оплатить картой: {days} дней, {devices} устройства, {price_rub} ₽."
        )
        await callback.message.answer(
            "Не удалось открыть оплату через ЮKassa.\n\n"
            "Напишите в поддержку — поможем оплатить подписку банковской картой. "
            "Если деньги уже списались, не оплачивайте повторно: отправьте поддержке подтверждение платежа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💬 Оплатить картой через поддержку",
                    url=f"https://t.me/ByMeVPNSupportBot?text={support_text}",
                )],
                [InlineKeyboardButton(text="Оплатить через Telegram Stars", callback_data="pay_stars", )],
            ]),
        )
        return

    from database import log_analytics_event
    await log_analytics_event(
        user_id, "checkout_started", tariff=f"{months}m", devices=devices,
        amount=price_rub, payment_provider="yookassa",
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price_rub} ₽", url=url, )],
        [InlineKeyboardButton(text="Назад", callback_data=f"devtier_{devices}")]
    ])
    
    await send_with_photo(
        bot, callback,
        f"<b>Оплата через ЮKassa</b>\n\n"
        f"Перейдите к оплате по кнопке ниже.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до {devices} одновременно\n\n"
        f"Подписка активируется после подтверждения оплаты.",
        kb,
        photo=PAYMENT,
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

    from database import log_analytics_event
    await log_analytics_event(
        user_id, "checkout_started", tariff=f"{months}m", devices=devices,
        amount=price_rub, payment_provider="cryptobot",
    )

    url, invoice_id = result

    from database import add_crypto_pending
    await add_crypto_pending(invoice_id, user_id, days, devices, price_rub)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"₿ Оплатить {price_rub} ₽", url=url, )],
        [InlineKeyboardButton(text="Назад", callback_data=f"devtier_{devices}")]
    ])
    
    await send_with_photo(
        bot, callback,
        f"<b>Оплата через CryptoBot</b>\n\n"
        f"Перейдите к оплате по кнопке ниже.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до {devices} одновременно\n\n"
        f"Подписка активируется после подтверждения оплаты.",
        kb,
        photo=PAYMENT,
    )


# ---------------------------------------------------------------------------
# Pre-checkout confirmation (required by Telegram for Stars)
# ---------------------------------------------------------------------------

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    await pre.answer(ok=True)


# ---------------------------------------------------------------------------
# Successful Stars payment → idempotent delivery
# ---------------------------------------------------------------------------

@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username)

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
    parts = payload.split("_")
    try:
        if len(parts) < 7 or parts[0] != "stars" or int(parts[1]) != user_id:
            raise ValueError("invalid invoice payload shape or owner")
        days = int(parts[2])
        raw_devices = int(parts[3])
        if raw_devices not in VALID_DEVICE_LIMITS:
            raise ValueError("invalid device limit in invoice payload")
        devices = validate_device_limit(raw_devices)
        months = int(parts[4])
        if days <= 0 or months not in (1, 3, 6, 12) or not parts[-1].isdigit():
            raise ValueError("invalid subscription period or invoice timestamp")
        if currency != "XTR" or stars <= 0:
            raise ValueError("invalid Stars currency or amount")
        promo_code = "_".join(parts[5:-1]) or None
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
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Оплата Stars требует ручной сверки: user <code>{user_id}</code>, "
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
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Платёж Stars <code>{charge_id}</code> пользователя <code>{user_id}</code> "
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
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Не удалось выдать подписку по Stars: user <code>{user_id}</code>, "
                f"charge <code>{charge_id}</code>. Платёж можно безопасно повторить через обработчик failed.",
                parse_mode="HTML",
            )
        except Exception:
            pass


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
        await send_with_photo(callback.bot, callback,
            "<b>Промокод недействителен</b>\n\n"
            "Возможные причины:\n"
            "• Код истёк\n"
            "• Достигнут лимит использований\n"
            "• Код не существует\n"
            "• Код не начался ещё",
            photo=PROMO_CODE,
        )
        return

    # Check if user already used this promo code
    if await has_user_used_promo(code, user_id):
        await send_with_photo(callback.bot, callback,
            "<b>Промокод уже использован</b>\n\n"
            "Каждый промокод можно использовать только один раз.",
            photo=PROMO_CODE,
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

    await send_with_photo(callback.bot, callback,
        f"<b>Промокод активирован</b>\n\n"
        f"🎁 Код: <code>{code}</code>\n"
        f"💰 Бонус: <b>{discount_text}</b>\n\n"
        f"Бонус автоматически применится при следующей покупке.\n\n"
        f"Выберите тариф — скидка применится автоматически.",
        reply_markup=_promo_purchase_keyboard(),
        photo=PROMO_CODE,
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
            await send_with_photo(message.bot, message,
                "<b>Применить промокод</b>\n\n"
                "Введите: <code>/promo КОД</code>\n\n"
                "Например: <code>/promo SALE20</code>\n\n"
                "Промокод даст скидку при следующей покупке VPN.",
                photo=PROMO_CODE,
            )
            return

        code = parts[1].strip().upper()
        user_id = message.from_user.id

        # Validate promo code
        promo = await validate_promo_code(code)
        if not promo:
            await send_with_photo(message.bot, message,
                "<b>Промокод недействителен</b>\n\n"
                "Возможные причины:\n"
                "• Код истёк\n"
                "• Достигнут лимит использований\n"
                "• Код не существует\n"
                "• Код не начался ещё",
                photo=PROMO_CODE,
            )
            return

        # Check if user already used this promo code
        if await has_user_used_promo(code, user_id):
            await send_with_photo(message.bot, message,
                "<b>Промокод уже использован</b>\n\n"
                "Каждый промокод можно использовать только один раз.",
                photo=PROMO_CODE,
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

        await send_with_photo(message.bot, message,
            f"<b>Промокод активирован</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💰 Бонус: <b>{discount_text}</b>\n\n"
            f"Бонус автоматически применится при следующей покупке.\n\n"
            f"Выберите тариф — скидка применится автоматически.",
            _promo_purchase_keyboard(),
            photo=PROMO_CODE,
        )

    except Exception as e:
        logger.error("Promo code error: %s", e)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")
