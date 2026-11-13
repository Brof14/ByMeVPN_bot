"""
VPN purchase flow:
  buy_vpn → select type → select period → payment method → invoice/link
"""
import asyncio
import time
import logging
import hashlib
import json
import weakref

from aiogram import Bot, F, Router
from aiogram.filters import Command
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
    TARIFF_BONUS_MONTHS, get_price_for_months, get_monthly_display, validate_device_limit,
)
from config import PRICE_1_MONTH, DAYS_1M
from states import BuyFlow, PromoFlow
from keyboards import tariff_selection_kb, payment_kb, support_url
from payments import create_yookassa_payment, create_crypto_payment, crypto_invoice_payload, record_fulfilled_promo
from subscription import deliver_key
from database import (
    ensure_user,
    record_payment_idempotent, update_payment_status,
    validate_promo_code, has_user_used_promo,
)
from utils import send_with_photo, send_or_edit, safe_answer
from bot_assets import PLANS, RENEWAL, PAYMENT, PROMO_CODE
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

# Serialize duplicate Telegram Stars updates for the same charge within this process.
_stars_payment_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _promo_purchase_keyboard() -> InlineKeyboardMarkup:
    """Return to the tariff chooser after a promotion."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn")
    ]])


def _tariff_intro(devices: int, *, renewal: bool = False, promo_binding: int | None = None) -> str:
    """Consistent purchase/renewal copy, with current selection as context."""
    devices = validate_device_limit(devices)
    device_word = "устройства" if devices == 2 else "устройств"
    audience = "для вас" if devices == 2 else "для вас и ваших близких"
    title = "Продление подписки" if renewal else "Тарифы ByMeVPN"
    text = (
        f"<b>{title}</b>\n\n"
        f"{devices} {device_word} — {audience}.\n\n"
        "Выберите срок подписки. Чем дольше срок, тем ниже стоимость месяца.\n"
        "Полная сумма указана в каждом тарифе."
    )
    if promo_binding in (1, 3, 6, 12):
        extra = TARIFF_BONUS_MONTHS[promo_binding]
        term = f"{promo_binding}+{extra}" if extra else "1"
        text += f"\n\nПромокод действует на тариф {term} мес."
    return text


def _checkout_confirmation_text(data: dict) -> str:
    """All payment choices share one accurate checkout summary."""
    months = int(data["months"])
    days = int(data["days"])
    devices = int(data["devices"])
    price_rub = int(data["price_rub"])
    bonus = TARIFF_BONUS_MONTHS[months]
    term = f"{months}+{bonus} мес." if bonus else "1 месяц"
    recurring = True
    recurring_price, recurring_days = get_price_for_months(months, devices)
    dev_name = DEVICE_CONFIG.get(devices, {}).get("name", f"{devices} устройств")
    lines = [
        "<b>Подтверждение подписки</b>",
        "",
        f"Срок: <b>{term}</b> ({days} дней)",
        f"Устройства: <b>{dev_name}</b>",
        f"К оплате сейчас: <b>{price_rub} ₽</b>",
    ]
    original = int(data.get("original_price") or price_rub)
    if original > price_rub:
        lines.append(f"Цена без промокода: <s>{original} ₽</s>")
    if data.get("promo_mismatch"):
        lines.append("Промокод для другого срока не применён.")
    lines.extend([
        "",
        f"<b>Автопродление ЮKassa: {'включено' if recurring else 'выключено'}</b>",
    ])
    if recurring:
        lines.append(
            f"При оплате через ЮKassa вы соглашаетесь на повторное списание "
            f"{recurring_price} ₽ за следующие {recurring_days} дней по окончании оплаченного срока. "
            "Отключить автопродление можно в разделе «О сервисе» → «Документы» → «Оплата и автопродление»."
        )
    else:
        lines.append("Оплата ЮKassa будет разовой. Автоматических списаний не будет.")
    lines.extend([
        "Автопродление включено по умолчанию. Отключить его можно в разделе «О сервисе» → «Документы» → «Оплата и автопродление»; оплаченные дни сохранятся.",
        "Telegram Stars и CryptoBot — разовые платежи.",
        "",
        "Доступ откроется после подтверждения оплаты.",
        "Оплачивая, вы соглашаетесь с <a href='https://telegra.ph/Publichnaya-oferta-na-okazanie-uslug-ByMeVPN-10-25'>офертой</a> "
        "и <a href='https://telegra.ph/Politika-v-otnoshenii-obrabotki-personalnyh-dannyh-ByMeVPN-10-25'>политикой конфиденциальности</a>.",
        "",
        "<b>Выберите способ оплаты:</b>",
    ])
    return "\n".join(lines)


async def _show_checkout(
    bot: Bot, callback: CallbackQuery, state: FSMContext, data: dict | None = None,
) -> None:
    if data is None:
        data = await state.get_data()
    await send_with_photo(
        bot, callback, _checkout_confirmation_text(data),
        payment_kb(
            int(data["price_rub"]), int(data["days"]), devices=int(data["devices"]),
        ),
        photo=PAYMENT,
    )


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
    promo_binding = (data.get("promo_info") or {}).get("tariff_binding")
    devices = validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT))
    renew_key_id = data.get("renew_key_id")
    await state.update_data(devices=devices)
    from database import log_analytics_event
    await log_analytics_event(callback.from_user.id, "tariffs_viewed", devices=devices)
    
    text = _tariff_intro(devices, renewal=bool(renew_key_id), promo_binding=promo_binding)
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount, promo_tariff_months=promo_binding),
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
    promo_binding = (data.get("promo_info") or {}).get("tariff_binding")
    await state.update_data(devices=devices)

    renew_key_id = data.get("renew_key_id")
    text = _tariff_intro(devices, renewal=bool(renew_key_id), promo_binding=promo_binding)
    await send_with_photo(
        bot, callback,
        text,
        tariff_selection_kb(devices=devices, discount_percent=promo_discount, promo_tariff_months=promo_binding),
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
    promo_info = data.get("promo_info") or {}
    promo_code = data.get("promo_code") or promo_info.get("code", "")
    original_price = price_rub

    binding = promo_info.get("tariff_binding")
    promo_mismatch = binding is not None and binding != months
    if promo_mismatch:
        # The selected button already displayed the full price. Keep the code
        # unused so the customer may activate it again for an eligible tariff.
        promo_info = {}
        promo_code = ""
    if promo_info:
        try:
            current_promo = await validate_promo_code(promo_code, tariff_months=months) if promo_code else None
            already_used = await has_user_used_promo(promo_code, callback.from_user.id) if current_promo else False
        except Exception:
            logger.exception("Could not verify promo eligibility for user %d", callback.from_user.id)
            await send_with_photo(bot, callback, "Не удалось проверить промокод. Попробуйте ещё раз.", tariff_selection_kb(devices=devices), photo=PLANS)
            return
        if not current_promo or already_used:
            await state.update_data(promo_info={}, promo_code="", promo_discount=0, price_rub=None, days=None)
            await send_with_photo(
                bot, callback,
                "<b>Промокод больше недоступен</b>\n\nВыберите тариф заново или введите другой промокод.",
                tariff_selection_kb(devices=devices), photo=PLANS,
            )
            return
        promo_info = current_promo

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

    if price_rub <= 0:
        # An unpaid order has no payment-provider idempotency key. Reserve the
        # code and exact entitlement before provisioning, across all workers.
        from database import reserve_free_promo_order, complete_free_promo_order
        user_id = callback.from_user.id
        if not await reserve_free_promo_order(user_id, promo_code, months, devices, days):
            await send_with_photo(
                bot, callback,
                "<b>Промокод уже применён или недоступен для этого тарифа.</b>\n\n"
                "Если активация ранее прервалась, выберите исходный тариф "
                "или обратитесь в поддержку.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Поддержка", url=support_url())],
                    [InlineKeyboardButton(text="← К тарифам", callback_data=f"devtier_{devices}")],
                ]), photo=PLANS,
            )
            return
        payload = f"freepromo_{user_id}_{promo_code.strip().upper()}"
        delivered = await deliver_key(
            bot=bot, user_id=user_id, chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}", days=days, limit_ip=devices,
            is_paid=False, amount=0, method="promo", payload=payload, extend_existing=True,
        )
        if not delivered:
            await send_with_photo(
                bot, callback,
                "<b>Активация не подтверждена</b>\n\n"
                "Платёж не требуется. Попробуйте применить тот же промокод "
                "к исходному тарифу позже или обратитесь в поддержку.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Поддержка", url=support_url())],
                    [InlineKeyboardButton(text="← К тарифам", callback_data=f"devtier_{devices}")],
                ]), photo=PLANS,
            )
            return
        recorded = await record_fulfilled_promo(bot, promo_code, user_id, "free_promo", payload)
        if not recorded:
            # VPN access might be live. Preserve the reservation for
            # reconciliation and never instruct the user to repeat purchases.
            await send_with_photo(
                bot, callback,
                "<b>Доступ выдан, но учёт промокода проверяется.</b>\n\n"
                "Не применяйте его повторно. При необходимости обратитесь в поддержку.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Подписка", callback_data="my_keys")],
                    [InlineKeyboardButton(text="Поддержка", url=support_url())],
                ]), photo=PLANS,
            )
            return
        finalized = await complete_free_promo_order(user_id, promo_code)
        if not finalized:
            logger.warning("Free promo order completion unconfirmed for user %d", user_id)
        await state.clear()
        await send_with_photo(
            bot, callback,
            f"<b>Доступ активирован</b>\n\nНачислено {days} дней ByMeVPN по промокоду.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Подключить VPN", callback_data="connection_guide")],
                [InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu")],
            ]), photo=PLANS,
        )
        return

    await state.update_data(
        months=months, devices=devices, price_rub=price_rub, days=days,
        original_price=original_price, promo_info=promo_info, promo_code=promo_code,
        promo_discount=promo_info.get("discount_value", 0) if promo_info.get("promo_type") == "percent" else 0,
        promo_mismatch=promo_mismatch,
        checkout_autorenew_disclosed=True,
        autorenew_enabled=True,
    )
    # A restart can erase MemoryStorage. Save only the choice, never its
    # computed price, so recovering cannot silently charge an old amount.
    try:
        from database import save_checkout_draft
        await save_checkout_draft(
            callback.from_user.id, months, devices, renew_key_id=data.get("renew_key_id"),
        )
    except Exception:
        logger.exception("Checkout preference could not be saved for user %d", callback.from_user.id)
    from database import log_analytics_event
    await log_analytics_event(
        callback.from_user.id, "tariff_selected", tariff=f"{months}m", devices=devices,
        amount=price_rub, promo=promo_info.get("code") if promo_info else None,
    )

    # Render from the authoritative newly calculated order. Tests and adapter
    # FSM backends need not reflect update_data() in get_data() immediately.
    checkout_display = {
        **data,
        "months": months, "devices": devices, "price_rub": price_rub,
        "days": days, "original_price": original_price,
        "promo_mismatch": promo_mismatch,
        "checkout_autorenew_disclosed": True,
        "autorenew_enabled": True,
    }
    await _show_checkout(bot, callback, state, checkout_display)

# ---------------------------------------------------------------------------
# Step 3: Choose payment method
# ---------------------------------------------------------------------------

async def _get_checkout_params(state: FSMContext, user_id: int) -> dict | None:
    """
    Read the tariff selected in the buy flow.

    After a bot restart (MemoryStorage) the FSM is empty. Falling back to a
    1-month price would silently charge the WRONG amount for a tariff the
    user selected — so instead we return None and re-ask the user to pick a
    tariff.
    """
    data = await state.get_data()
    if data.get("price_rub") is None or data.get("days") is None:
        return None
    try:
        price_rub, days = int(data["price_rub"]), int(data["days"])
    except (TypeError, ValueError, OverflowError):
        return None
    if price_rub <= 0 or days <= 0:
        return None
    # Reject stale/corrupt FSM quotes rather than charging a different sum
    # from the current server-side tariff catalogue. This is checked on every
    # payment-provider path, including after a price or promotion change.
    try:
        months = int(data.get("months", 1))
        devices = validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT))
        expected_price, expected_days = get_price_for_months(months, devices)
    except (TypeError, ValueError, OverflowError, KeyError):
        return None
    promo_code = data.get("promo_code", "")
    if promo_code:
        snapshot = data.get("promo_info") or {}
        try:
            current = await validate_promo_code(promo_code, tariff_months=int(data.get("months", 1)))
            used = await has_user_used_promo(promo_code, user_id) if current else False
        except Exception:
            logger.exception("Could not verify checkout promo for user %d", user_id)
            return None
        fields = ("promo_type", "discount_value", "tariff_binding")
        if not current or used or any(current.get(field) != snapshot.get(field) for field in fields):
            await state.update_data(promo_info={}, promo_code="", promo_discount=0, price_rub=None, days=None)
            return None
        promo_type = current.get("promo_type")
        value = int(current.get("discount_value") or 0)
        if promo_type == "percent":
            expected_price = round(expected_price * (100 - value) / 100)
        elif promo_type == "fixed_rub":
            expected_price = max(0, expected_price - value)
        elif promo_type == "free_days":
            expected_days += value
        else:
            return None
    if price_rub != expected_price or days != expected_days:
        logger.warning("Checkout quote invalidated for user %d; requesting new confirmation", user_id)
        await state.update_data(price_rub=None, days=None)
        return None
    return {
        "price_rub": price_rub,
        "days": days,
        "months": int(data.get("months", 1)),
        "devices": validate_device_limit(data.get("devices", DEFAULT_DEVICE_LIMIT)),
        "promo_code": data.get("promo_code", ""),
        "renew_key_id": int(data["renew_key_id"]) if data.get("renew_key_id") else None,
        "autorenew_enabled": True,
        "checkout_autorenew_disclosed": True,
    }


async def _ask_tariff_again(bot: Bot, callback: CallbackQuery) -> None:
    """Restore the customer's plan, never resume charging without confirmation."""
    from keyboards import tariff_selection_kb
    from database import get_checkout_draft, log_analytics_event
    try:
        draft = await get_checkout_draft(callback.from_user.id)
    except Exception:
        logger.exception("Could not read checkout draft for user %d", callback.from_user.id)
        draft = None
    if draft:
        months, devices = draft["months"], draft["devices"]
        label = f"{get_price_for_months(months, devices)[1]} дней"
        renewing = int(draft.get("renew_key_id") or 0)
        text = (
            "<b>Оформление было прервано</b>\n\n"
            f"Сохранён выбор: <b>{label}</b>, <b>{devices} устройств</b>.\n"
            + ("Это продление существующей подписки. Для сохранения "
               "её ключа нужно заново выбрать срок.\n\n"
               if renewing else
               "Вернитесь к заказу, чтобы проверить актуальную итоговую сумму "
               "и заново выбрать способ оплаты.\n\n")
            + "Если применяли промокод, активируйте его повторно через /promo КОД. "
            "Оплата пока не создаётся."
        )
        if renewing:
            # key_renew validates ownership again and restores the renewal FSM.
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Вернуться к продлению", callback_data=f"key_renew:{renewing}")],
                [InlineKeyboardButton(text="← В меню", callback_data="back_to_menu")],
            ])
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Проверить мой заказ", callback_data=f"tariff_{months}_{devices}")],
                [InlineKeyboardButton(text="Другой тариф", callback_data=f"devtier_{devices}")],
                [InlineKeyboardButton(text="← В меню", callback_data="back_to_menu")],
            ])
        await log_analytics_event(callback.from_user.id, "checkout_recovery_shown",
                                  tariff=f"{months}m", devices=devices)
    else:
        text = (
            "<b>Оформление было прервано</b>\n\n"
            "Выберите тариф ещё раз. Оплата не создаётся, пока вы "
            "не подтвердите новый заказ."
        )
        keyboard = tariff_selection_kb()
    await send_with_photo(bot, callback, text, keyboard, photo=PLANS)


# Legacy handler for old period_* callbacks
@router.callback_query(F.data == "checkout_resume")
async def cb_checkout_resume(callback: CallbackQuery, bot: Bot):
    """Open the saved selection without silently creating a new payment."""
    await safe_answer(callback)
    await _ask_tariff_again(bot, callback)


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
    params = await _get_checkout_params(state, callback.from_user.id)
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

@router.callback_query(F.data.in_({"pay_yookassa", "pay_yookassa_autorenew"}))
async def cb_pay_yookassa(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    params = await _get_checkout_params(state, callback.from_user.id)
    if not params:
        await _ask_tariff_again(bot, callback)
        return
    price_rub: int = params["price_rub"]
    days: int = params["days"]
    months: int = params["months"]
    devices: int = params["devices"]
    promo_code: str = params["promo_code"]
    user_id = callback.from_user.id
    save_payment_method = True
    next_charge_rub, next_charge_days = get_price_for_months(months, devices)

    # The fingerprint binds retries to the *exact* confirmed order. A
    # per-process set is not enough when multiple bot workers are running.
    from database import claim_yookassa_checkout_attempt, release_yookassa_checkout_attempt
    fingerprint = hashlib.sha256(json.dumps(
        [price_rub, days, months, devices, promo_code,
         params.get("renew_key_id") or 0, bool(save_payment_method)],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    try:
        lease = await claim_yookassa_checkout_attempt(user_id, fingerprint)
    except Exception as exc:
        logger.error("Checkout claim unavailable for user %s (%s)", user_id, type(exc).__name__)
        await callback.message.answer("Не удалось сохранить оформление. Попробуйте немного позже.")
        return
    if not lease:
        await callback.message.answer("Запрос на оплату уже выполняется. Повторите через несколько секунд.")
        return
    recurring_checkout = save_payment_method
    try:
        created = await create_yookassa_payment(
            price_rub, f"ByMeVPN {days} дней ({devices} устр.)", user_id, days, devices,
            promo_code=promo_code, months=months,
            extra_metadata={
                **({"key_id": params["renew_key_id"]} if params.get("renew_key_id") else {}),
                **({
                    "auto_renew_consent": "1",
                    "auto_renew_recurring_price_rub": str(next_charge_rub),
                    "auto_renew_recurring_days": str(next_charge_days),
                } if save_payment_method else {}),
            },
            save_payment_method=save_payment_method,
            return_payment_details=True,
            idempotency_key=lease["idempotency_key"],
        )
        if isinstance(created, dict):
            url = created.get("payment_url")
            recurring_checkout = bool(created.get("recurring_requested", save_payment_method))
        else:
            # Backward compatibility for adapters returning the URL string.
            url = created
    except Exception as exc:
        # A provider timeout is ambiguous. Never leak internal error details to customers.
        logger.error("YooKassa checkout failed for user %d (%s)", user_id, type(exc).__name__)
        url = None
    finally:
        await release_yookassa_checkout_attempt(user_id, lease["token"])

    if not url:
        await safe_answer(callback)
        await callback.message.answer(
            "Не удалось открыть оплату через ЮKassa.\n\n"
            "Напишите в поддержку — поможем оплатить подписку банковской картой. "
            "Если деньги уже списались, не оплачивайте повторно: отправьте поддержке подтверждение платежа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💬 Оплатить картой через поддержку",
                    url="https://t.me/ByMeVPNSupportBot",
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
        f"Автопродление: <b>{'включено' if recurring_checkout else 'выключено'}</b>. "
        + (f"Следующее списание {next_charge_rub} ₽ за {next_charge_days} дней — по окончании срока. "
           "Отключить можно в настройках.\n\n" if recurring_checkout else
           "Если вы выбирали автопродление, сохранить способ оплаты не удалось: "
           "этот платёж будет разовым.\n\n" if save_payment_method else "\n\n")
        + "Подписка активируется после подтверждения оплаты.",
        kb,
        photo=PAYMENT,
    )


# ---------------------------------------------------------------------------
# Payment: Crypto Bot (@send)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    params = await _get_checkout_params(state, callback.from_user.id)
    if not params:
        await _ask_tariff_again(bot, callback)
        return
    price_rub: int = params["price_rub"]
    days: int = params["days"]
    months: int = params["months"]
    devices: int = params["devices"]
    promo_code: str = params["promo_code"]
    user_id = callback.from_user.id

    try:
        result = await create_crypto_payment(
            price_rub, f"ByMeVPN {days} дней ({devices} устр.)", user_id, days, devices, promo_code=promo_code,
        )
    except Exception as exc:
        logger.error("Crypto Pay checkout failed for user %d (%s)", user_id, type(exc).__name__)
        result = None

    if not result:
        await safe_answer(
            callback,
            "Crypto Pay временно недоступен. Пожалуйста, выберите другой способ оплаты.",
            alert=True,
        )
        return

    url, invoice_id = result

    # Never expose an invoice unless its durable ledger record exists.
    try:
        from database import add_crypto_pending
        saved = await add_crypto_pending(
            invoice_id, user_id, days, devices, price_rub,
            payload=crypto_invoice_payload(user_id, days, devices, promo_code),
        )
        if saved is False:
            raise RuntimeError("Crypto pending invoice persistence returned false")
    except Exception as exc:
        logger.error("Crypto pending invoice not recorded for user %d (%s)",
                     user_id, type(exc).__name__)
        await callback.message.answer(
            "Не удалось сохранить заказ Crypto Pay. Ссылка на оплату не выдаётся. "
            "Если деньги уже списались, не платите повторно — напишите в поддержку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Написать в поддержку",
                                      url="https://t.me/ByMeVPNSupportBot")],
                [InlineKeyboardButton(text="← К тарифам", callback_data=f"devtier_{devices}")],
            ]),
        )
        return

    from database import log_analytics_event
    await log_analytics_event(
        user_id, "checkout_started", tariff=f"{months}m", devices=devices,
        amount=price_rub, payment_provider="cryptobot",
    )

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

def _stars_invoice_error(pre: PreCheckoutQuery) -> str | None:
    """Validate an issued invoice before authorizing a Telegram Stars charge.

    No database access or mutable FSM values: the immutable invoice payload
    and the server's canonical pricing are the only accepted references.
    """
    invalid = "Счёт устарел или некорректен. Выберите тариф ещё раз."
    try:
        if pre.currency != "XTR" or int(pre.total_amount) <= 0:
            return invalid
        parts = pre.invoice_payload.split("_")
        if len(parts) < 7 or parts[0] != "stars":
            return invalid
        user_id, days, devices, months = map(int, parts[1:5])
        if user_id != pre.from_user.id or devices not in VALID_DEVICE_LIMITS:
            return invalid
        if months not in (1, 3, 6, 12) or not parts[-1].isdigit():
            return invalid
        issued_at = int(parts[-1])
        now = int(time.time())
        if issued_at > now + 60 or now - issued_at > 48 * 60 * 60:
            return invalid
        from constants import MAX_GRANT_DAYS
        if not 0 < days <= MAX_GRANT_DAYS:
            return invalid
        expected_amount, expected_days = get_price_for_months(months, devices)
        legacy_amount = get_monthly_display(months, devices) * months
        has_promo = bool("_".join(parts[5:-1]))
        amount = int(pre.total_amount)
        if has_promo:
            # Promo terms may reduce amount or add days; they were applied at
            # invoice creation. Never permit a larger or shorter package.
            if amount > expected_amount or days < expected_days:
                return invalid
        elif amount not in {expected_amount, legacy_amount} or days != expected_days:
            return invalid
    except (ValueError, TypeError, AttributeError, IndexError):
        return invalid
    return None


@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    error = _stars_invoice_error(pre)
    if not error:
        from database import is_account_tombstoned
        try:
            if await is_account_tombstoned(pre.from_user.id):
                error = "Аккаунт удалён. Новые оплаты заблокированы. Обратитесь в поддержку."
        except Exception:
            logger.exception("Stars pre-checkout account status check failed")
            error = "Не удалось проверить аккаунт. Повторите оплату позже."
    if error:
        await pre.answer(ok=False, error_message=error)
    else:
        await pre.answer(ok=True)


# ---------------------------------------------------------------------------
# Successful Stars payment → idempotent delivery
# ---------------------------------------------------------------------------

@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot, state: FSMContext):
    user_id = message.from_user.id
    from database import is_account_tombstoned
    if await is_account_tombstoned(user_id):
        # This update can arrive after account erasure even if the pre-checkout
        # was approved earlier. Record the charge without recreating VPN access.
        payment = message.successful_payment
        charge_id = payment.telegram_payment_charge_id or payment.invoice_payload
        is_new, _ = await record_payment_idempotent(
            user_id=user_id,
            amount=payment.total_amount,
            currency=payment.currency,
            method="stars",
            days=0,
            payload=charge_id,
            status="recovery_required",
            tariff="Deleted account — manual refund review",
            devices=DEFAULT_DEVICE_LIMIT,
            provider="stars",
            provider_payment_id=charge_id,
        )
        if is_new:
            from html import escape
            from config import ADMIN_ID
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "⚠️ Stars оплата после удаления аккаунта. "
                    f"Пользователь <code>{user_id}</code>, "
                    f"платёж <code>{escape(str(charge_id))}</code>. "
                    "Не выдавайте доступ автоматически; проверьте возврат вручную.",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Could not alert admin about late Stars charge after deletion")
        try:
            await message.answer(
                "Оплата получена после удаления аккаунта. "
                "Доступ не активируется автоматически. Не оплачивайте повторно; "
                "обратитесь в @ByMeVPNSupportBot для проверки возврата."
            )
        except Exception:
            logger.warning("Could not notify user %d about late Stars charge", user_id)
        return
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
            await record_fulfilled_promo(bot, promo_code, user_id, "stars", charge_id)
        await state.clear()

        # Referral reward: single canonical path (idempotent, one per pair).
        from referral_system_new import award_referral_for_payment
        await award_referral_for_payment(user_id, charge_id, amount=int(payment.total_amount), bot=bot)
    else:
        await update_payment_status(pay_db_id, "recovery_required")
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
        try:
            await bot.send_message(
                user_id,
                "<b>Оплата получена · доступ проверяется</b>\n\nНе оплачивайте повторно. Мы проверяем выдачу и сообщим, когда ключ будет готов. Если вопрос срочный: @ByMeVPNSupportBot",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("Could not send Stars recovery status to user %d", user_id)


# ---------------------------------------------------------------------------
# Promo Code Callback Handler (for button activation)
# ---------------------------------------------------------------------------

async def _activate_promo_code(bot: Bot, target, state: FSMContext, code: str) -> bool:
    """Validate and attach a promo to the next checkout without consuming it."""
    code = code.strip().upper()
    user_id = target.from_user.id
    promo = await validate_promo_code(code)
    if not promo:
        await send_with_photo(
            bot, target,
            "<b>Не удалось применить промокод</b>\n\n"
            "Проверьте код: возможно, он истёк или закончились активации.",
            photo=PROMO_CODE,
        )
        return False
    if await has_user_used_promo(code, user_id):
        await send_with_photo(
            bot, target,
            "<b>Промокод уже использован</b>\n\nКаждый код можно применить один раз.",
            photo=PROMO_CODE,
        )
        return False

    promo_type = promo["promo_type"]
    discount_value = promo["discount_value"]
    if promo_type == "percent":
        discount_text, promo_discount = f"{discount_value}%", discount_value
    elif promo_type == "fixed_rub":
        discount_text, promo_discount = f"{discount_value} ₽", 0
    elif promo_type == "free_days":
        discount_text, promo_discount = f"+{discount_value} дней", 0
    else:
        discount_text, promo_discount = str(discount_value), 0

    await state.update_data(promo_info=promo, promo_code=code, promo_discount=promo_discount)
    await state.set_state(None)
    logger.info("Promo activated for user %s (type=%s)", user_id, promo_type)
    await send_with_photo(
        bot, target,
        f"<b>Промокод применён</b>\n\n"
        f"Бонус: <b>{discount_text}</b>\n"
        "Он учтётся при следующей покупке. Выберите тариф.",
        _promo_purchase_keyboard(),
        photo=PROMO_CODE,
    )
    return True


@router.callback_query(F.data.startswith("activate_promo:"))
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    """Activate promo code via button click."""
    await safe_answer(callback)
    await _activate_promo_code(callback.bot, callback, state, callback.data.split(":", 1)[1])


# ---------------------------------------------------------------------------
# Promo Code Command
# ---------------------------------------------------------------------------

@router.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext):
    """Start promo entry; users can type the code in their next message."""
    try:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await state.set_state(PromoFlow.waiting_code)
            await send_with_photo(
                message.bot, message,
                "<b>Промокод</b>\n\nОтправьте код следующим сообщением.",
                photo=PROMO_CODE,
            )
            return
        await _activate_promo_code(message.bot, message, state, parts[1])
    except Exception:
        logger.exception("Promo activation failed")
        await message.answer("Не удалось применить код. Попробуйте ещё раз позже.")


@router.message(PromoFlow.waiting_code, F.text)
async def receive_promo_code(message: Message, state: FSMContext):
    """Accept a plain promo code after /promo; no command prefix is needed."""
    code = (message.text or "").strip()
    if code.lower() in {"/cancel", "отмена"}:
        await state.set_state(None)
        await message.answer("Ввод промокода отменён.")
        return
    if code.startswith("/"):
        await state.set_state(None)
        await message.answer("Ввод промокода закрыт. Откройте нужный раздел меню и попробуйте снова.")
        return
    if not code or len(code) > 64 or any(ch.isspace() for ch in code):
        await message.answer("Отправьте только код одним сообщением или /cancel для отмены.")
        return
    try:
        await _activate_promo_code(message.bot, message, state, code)
    except Exception:
        logger.exception("Promo activation failed")
        await message.answer("Не удалось проверить код. Попробуйте ещё раз позже.")
