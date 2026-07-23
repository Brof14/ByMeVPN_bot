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
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from constants import PRICE_CONFIG, PERIOD_LABELS, TRIAL_DAYS
from config import PRICE_1_MONTH, DAYS_1M
from states import BuyFlow
from keyboards import tariff_selection_kb, payment_kb
from payments import create_yookassa_payment, create_crypto_payment
from subscription import ask_config_name
from database import ensure_user, add_referral_earning, get_referrer
from utils import send_with_photo, safe_answer

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Step 1: Choose plan type
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_vpn")
async def cb_buy_vpn(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(BuyFlow.choosing_period)
    
    # Check if user has active promo code
    data = await state.get_data()
    promo_discount = data.get("promo_discount", 0)
    
    await send_with_photo(
        bot, callback,
        "<b>Выберите срок подписки</b>\n\nЧем дольше срок, тем ниже стоимость одного месяца.\n\nВсе тарифы включают до 5 устройств одновременно.",
        tariff_selection_kb(discount_percent=promo_discount),
    )


# ---------------------------------------------------------------------------
# Step 2: Choose period
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("tariff_"))
async def cb_select_tariff(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    months = int(callback.data.split("_", 1)[1])

    price_rub, days = PRICE_CONFIG.get(months, PRICE_CONFIG[1])

    # Check for promo discount
    data = await state.get_data()
    promo_info = data.get("promo_info", {})
    original_price = price_rub

    logger.info(f"Selecting tariff: months={months}, original_price={original_price}, promo_info={promo_info}")

    if promo_info:
        promo_type = promo_info.get("promo_type", "percent")
        discount_value = promo_info.get("discount_value", 0)

        if promo_type == "percent":
            price_rub = int(price_rub * (100 - discount_value) / 100)
            promo_text = f" (скидка {discount_value}% применена)"
        elif promo_type == "fixed_rub":
            price_rub = max(0, price_rub - discount_value)
            promo_text = f" (скидка {discount_value} ₽ применена)"
        elif promo_type == "free_days":
            # Free days promo - add days instead of discount
            days += discount_value
            promo_text = f" (+{discount_value} дней бесплатно)"
        else:
            promo_text = ""
    else:
        promo_text = ""

    await state.update_data(months=months, price_rub=price_rub, days=days, original_price=original_price)

    period_name = PERIOD_LABELS.get(months, f"{months} мес.")

    text = (
        f"<b>Вы покупаете доступ на {days} дней.</b>\n\n"
        f"Стоимость: <b>{price_rub} ₽</b>{promo_text}\n"
    )

    if promo_info and promo_info.get("promo_type") in ["percent", "fixed_rub"]:
        text += f"<s>Без скидки: {original_price} ₽</s>\n"

    text += (
        f"Устройств: до 5 одновременно\n\n"
        "Оплачивая подписку, вы соглашаетесь с <a href='https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-ByMeVPN-03-12'>политикой обработки персональных данных</a>, с <a href='https://telegra.ph/DOGOVOR-PUBLICHNOJ-OFERTY-ByMyVPN-03-12'>договором оферты</a> и с <a href='https://telegra.ph/SOGLASHENIE-O-REGULYARNYH-REKURRENTNYH-PLATEZHAH-ByMeVPN-03-12'>соглашением о присоединении к рекуррентной системе платежей</a>.\n\n"
        "Все подписки продлеваются автоматически. Отмена подписки возможна в любой момент.\n\n"
        "После оплаты бот отправит вам ключ для приложения и подробную инструкцию по установке.\n\n"
        "<b>Выберите способ оплаты:</b>"
    )

    await send_with_photo(bot, callback, text, payment_kb(price_rub, days))


# ---------------------------------------------------------------------------
# Step 3: Choose payment method
# ---------------------------------------------------------------------------

# Legacy handler for old period_* callbacks
@router.callback_query(F.data.startswith("period_"))
async def cb_select_period_legacy(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    # Redirect to new tariff selection
    await send_with_photo(
        bot, callback,
        "<b>Выберите срок подписки</b>\n\nЧем дольше срок, тем ниже стоимость одного месяца.\n\nВсе тарифы включают до 5 устройств одновременно.",
        tariff_selection_kb(),
    )


# ---------------------------------------------------------------------------
# Payment: Telegram Stars  (Stars = rubles, 1:1, intentional)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    data = await state.get_data()
    price_rub: int = data.get("price_rub", PRICE_1_MONTH)
    days: int = data.get("days", DAYS_1M)
    months: int = data.get("months", 1)
    # All plans now support up to 5 devices
    devices: int = 5
    user_id = callback.from_user.id

    # Stars amount = rubles (1:1) — intentional, to cover Telegram commission
    stars = price_rub
    # Encode months in payload so we can recover if FSM is lost
    payload = f"stars_{user_id}_{days}_{months}_{int(time.time())}"

    try:
        # Отправляем инвойс сразу без промежуточных сообщений
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
        # Отправляем сообщение об ошибке, если инвойс не удался
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
    data = await state.get_data()
    price_rub: int = data.get("price_rub", PRICE_1_MONTH)
    days: int = data.get("days", DAYS_1M)
    months: int = data.get("months", 1)
    # All plans now support up to 5 devices
    devices: int = 5
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

    # Показываем сообщение с кнопкой для перехода на страницу оплаты
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price_rub} ₽", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu")]
    ])
    
    await send_with_photo(
        bot, callback,
        f"💳 <b>Оплата через ЮKassa</b>\n\n"
        f"Нажмите кнопку ниже для перехода на страницу оплаты.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до 5 одновременно\n\n"
        f"После оплаты вы получите ключ автоматически.",
        kb,
    )


# ---------------------------------------------------------------------------
# Payment: Crypto Bot (@send)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    data = await state.get_data()
    price_rub: int = data.get("price_rub", PRICE_1_MONTH)
    days: int = data.get("days", DAYS_1M)
    months: int = data.get("months", 1)
    # All plans now support up to 5 devices
    devices: int = 5
    user_id = callback.from_user.id

    result = await create_crypto_payment(
        price_rub, f"ByMeVPN {days} дней", user_id, days, devices,
    )

    if not result:
        await safe_answer(
            callback,
            "Crypto Pay временно недоступен. Пожалуйста, выберите другой способ оплаты.",
            alert=True,
        )
        return

    url, invoice_id = result

    # FIX: previously the invoice was created and shown to the user, but
    # nothing ever recorded it or checked whether it got paid — CryptoBot
    # has no built-in callback here, so paid invoices just vanished and no
    # key was ever delivered. Store it as pending so payment_monitor.py's
    # CryptoPaymentMonitor can pick it up once CryptoBot reports it as paid.
    from database import add_crypto_pending
    await add_crypto_pending(invoice_id, user_id, days, devices, price_rub)

    # Показываем сообщение с кнопкой для перехода на страницу оплаты
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"₿ Оплатить {price_rub} ₽", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu")]
    ])
    
    await send_with_photo(
        bot, callback,
        f"₿ <b>Оплата через Crypto Pay</b>\n\n"
        f"Нажмите кнопку ниже для перехода на страницу оплаты.\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        f"Срок: {days} дней\n"
        f"Устройств: до 5 одновременно\n\n"
        f"После оплаты вы получите ключ автоматически.",
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

    # Parse days and months from payload: "stars_{user_id}_{days}_{months}_{ts}"
    parts = payload.split("_")
    try:
        days = int(parts[2])
    except Exception:
        days = DAYS_1M
    try:
        # New format has months at index 3; old format (4 parts) falls back to FSM
        months_from_payload = int(parts[3]) if len(parts) >= 5 else None
    except Exception:
        months_from_payload = None

    # Retrieve months from FSM; payload value is authoritative fallback if FSM is gone
    data = await state.get_data()
    fsm_months = data.get("months")
    months = fsm_months or months_from_payload or 1
    
    # All paid plans support up to 5 devices
    devices = 5

    try:
        await message.delete()
    except Exception:
        pass

    # NOTE: the payment itself is recorded inside subscription.deliver_key()
    # once the key has actually been delivered. We deliberately do NOT call
    # add_payment() here — this used to record every Stars payment TWICE
    # (once here, once in deliver_key), inflating revenue stats and
    # per-user totals shown in the admin panel.
    payment_id = None

    # Начисляем бонус рефереалу за первую оплату (50₽)
    referrer_id = None
    try:
        referrer_id = await get_referrer(user_id)
        if referrer_id:
            try:
                from database import add_referral_earning
                bonus_added = await add_referral_earning(referrer_id, user_id, 50, payment_id)
                if bonus_added:
                    logger.info("Referral bonus 50₽ added for referrer %d from user %d payment", referrer_id, user_id)
                    # Уведомляем реферера
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 <b>Поздравляем!</b>\n\n"
                            f"Ваш приглашённый оформил платную подписку.\n"
                            f"Начислено: +50 ₽\n"
                            f"Текущий баланс обновлён в партнёрской программе."
                        )
                    except Exception as notify_error:
                        logger.error("Failed to notify referrer %d: %s", referrer_id, notify_error)
            except Exception as e:
                logger.error("Error processing referral bonus for user %d: %s", user_id, e)
    except Exception as e:
        logger.error("Error getting referrer for user %d: %s", user_id, e)

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
        }
    )

# YooKassa payments are now processed automatically in webhook.py
# No manual delivery needed anymore


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
    devices = pending.get("devices", 5)
    amount_rub = pending.get("amount_rub", 0)

    # Clear state before delivery
    await state.clear()

    # Deliver key
    from subscription import deliver_key
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
    from database import validate_promo_code, use_promo_code

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

    # Mark promo code as used
    success = await use_promo_code(code, user_id)
    if not success:
        await callback.message.edit_text(
            "❌ <b>Вы уже использовали этот промокод</b>\n\n"
            "Каждый промокод можно использовать только один раз.",
            parse_mode="HTML"
        )
        return

    # Save promo info to state for next purchase
    promo_type = promo["promo_type"]
    discount_value = promo["discount_value"]

    if promo_type == "percent":
        discount_text = f"{discount_value}%"
    elif promo_type == "fixed_rub":
        discount_text = f"{discount_value} ₽"
    elif promo_type == "free_days":
        discount_text = f"+{discount_value} дней"
    else:
        discount_text = f"{discount_value}"

    await state.update_data(promo_info=promo, promo_code=code)

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
        from database import validate_promo_code, use_promo_code

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

        # Mark promo code as used
        success = await use_promo_code(code, user_id)
        if not success:
            await message.answer(
                "❌ <b>Вы уже использовали этот промокод</b>\n\n"
                "Каждый промокод можно использовать только один раз.",
                parse_mode="HTML"
            )
            return

        # Save promo info to state for next purchase
        promo_type = promo["promo_type"]
        discount_value = promo["discount_value"]

        if promo_type == "percent":
            discount_text = f"{discount_value}%"
        elif promo_type == "fixed_rub":
            discount_text = f"{discount_value} ₽"
        elif promo_type == "free_days":
            discount_text = f"+{discount_value} дней"
        else:
            discount_text = f"{discount_value}"

        await state.update_data(promo_info=promo, promo_code=code)

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
