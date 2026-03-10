import time
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import REF_BONUS_DAYS
from database import (
    get_referrer,
    get_auto_renewal,
    set_auto_renewal,
    save_payment,
    has_paid_subscription,
)
from payments import (
    create_yookassa_payment,
    create_cryptobot_invoice,
    get_cryptobot_invoice_status,
    calculate_stars_amount,
)
from states import UserStates
from utils import update_menu
from keyboards import (
    plan_type_keyboard,
    period_keyboard,
    payment_methods_keyboard,
)
from notifications import send_payment_success_notification

logger = logging.getLogger(__name__)

router = Router()


async def give_subscription(
    bot: Bot,
    user_id: int,
    days: int,
    prefix: str,
    target: CallbackQuery,
    is_paid: bool = False
) -> None:
    """Выдача подписки пользователю"""
    try:
        logger.info(f"Giving subscription: user_id={user_id}, days={days}, prefix={prefix}, is_paid={is_paid}")
        
        from utils import add_client, generate_vless
        remark = f"{prefix}_{user_id}_{int(time.time())}"
        uuid_c = await add_client(days, remark)
        key = generate_vless(uuid_c)
        from database import add_user_key
        await add_user_key(user_id, key, remark, days)

        chat_id = target.message.chat.id

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Инструкция подключения", callback_data="instructions")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")]
        ])

        success_text = (
            "🎉 <b>Подписка успешно активирована!</b>\n\n"
            f"🎁 <b>Ключ на {days} дней</b> <i>готов к использованию</i>\n"
            "📱 <i>Скопируйте ключ или отсканируйте QR-код ниже</i>\n\n"
            "🚀 <b>Наслаждайтесь свободным интернетом!</b>"
        )

        from utils import generate_and_send_beautiful_qr
        await generate_and_send_beautiful_qr(bot, chat_id, key, success_text, reply_markup=kb)

        # Handle referral bonuses
        if is_paid:
            referrer_id = await get_referrer(user_id)
            if referrer_id and referrer_id != user_id:
                try:
                    await give_subscription(bot, referrer_id, REF_BONUS_DAYS, "ref_bonus", target, is_paid=False)
                    from notifications import send_referral_bonus_notification
                    await send_referral_bonus_notification(bot, referrer_id, REF_BONUS_DAYS)
                    logger.info(f"Referral bonus given to {referrer_id} for user {user_id}")
                except Exception as e:
                    logger.error(f"Error giving referral bonus: {e}")

    except Exception as e:
        logger.exception("Ошибка выдачи подписки")
        chat_id = target.message.chat.id
        error_text = (
            "<b>Ошибка при выдаче ключа</b>\n\n"
            "Что-то пошло не так. Пожалуйста, напишите в поддержку — мы поможем!\n\n"
            "Техподдержка: @ByMeVPN_support"
        )
        await bot.send_message(chat_id, error_text, parse_mode="HTML")


@router.callback_query(F.data == "buy_vpn")
async def buy_vpn(callback: CallbackQuery, state: FSMContext):
    """Начало покупки подписки"""
    await callback.answer()
    await state.set_state(UserStates.choosing_type)
    await update_menu(callback.bot, callback, "<b>Выберите тариф</b>", plan_type_keyboard())


@router.callback_query(F.data.startswith("type_"))
async def select_type(callback: CallbackQuery, state: FSMContext):
    """Выбор типа тарифа"""
    await callback.answer()
    plan = callback.data.split("_")[1]
    prices = {"personal": 79, "duo": 129, "family": 199}
    if plan not in prices:
        await callback.answer("Неверный тариф. Попробуйте ещё раз.", show_alert=True)
        return
    await state.update_data(plan_type=plan, base_price=prices[plan])
    await state.set_state(UserStates.choosing_period)
    await update_menu(callback.bot, callback, "<b>Выберите срок</b>", period_keyboard())


@router.callback_query(F.data.startswith("period_"))
async def select_period(callback: CallbackQuery, state: FSMContext):
    """Выбор периода подписки"""
    await callback.answer()
    data = await state.get_data()
    base = data.get("base_price")
    if base is None:
        await callback.answer("Сначала выберите тип тарифа.", show_alert=True)
        return

    months = int(callback.data.split("_")[1])
    if months == 6:
        disc = 0.82
    elif months == 12:
        disc = 0.73
    else:
        disc = 1.0

    total_rub = round(base * months * disc)
    days = months * 30
    price_str = f"{total_rub} ₽"

    await state.update_data(days=days, total_rub=total_rub, auto_renewal=False)
    await update_menu(
        callback.bot,
        callback,
        f"<b>Итого: {price_str}</b>\nСрок: {months} мес.",
        payment_methods_keyboard(price_str),
    )


@router.callback_query(F.data == "pay_stars")
async def pay_stars(callback: CallbackQuery, state: FSMContext):
    """Оплата через Telegram Stars"""
    await callback.answer()
    data = await state.get_data()
    total_rub = int(data.get("total_rub", 79))
    days = int(data.get("days", 30))

    stars_amount = calculate_stars_amount(total_rub)
    prices = [
        LabeledPrice(
            label=f"Подписка ByMeVPN на {days} дней",
            amount=stars_amount,
        )
    ]

    payload = f"stars_vpn_{callback.from_user.id}_{days}_{int(time.time())}"

    try:
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title="ByMeVPN — подписка",
            description=f"Доступ к VPN на {days} дней (VLESS + Reality)",
            payload=payload,
            provider_token="",  # для Telegram Stars токен не требуется
            currency="XTR",
            prices=prices,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса Stars: {e}")
        await callback.answer(
            "Оплата через Telegram Stars временно недоступна. "
            "Пожалуйста, выберите другой способ оплаты.",
            show_alert=True,
        )


@router.callback_query(F.data == "pay_yookassa")
async def pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Оплата через ЮKassa"""
    await callback.answer()
    data = await state.get_data()
    total_rub = data.get("total_rub")
    days = data.get("days", 30)
    if not total_rub:
        await callback.answer("Сначала выберите тариф и срок.", show_alert=True)
        return

    try:
        url = await create_yookassa_payment(
            int(total_rub),
            f"ByMeVPN {days} дней",
            callback.from_user.id,
        )
    except Exception as e:
        logger.error(f"Ошибка создания платежа ЮKassa: {e}")
        await callback.answer(
            "Оплата через ЮKassa сейчас недоступна. Попробуйте другой способ.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить ЮKassa", url=url)],
            [InlineKeyboardButton(text="⚫️ Назад", callback_data="buy_vpn")],
        ]
    )
    await callback.bot.send_message(
        callback.from_user.id,
        f"Перейдите по ссылке для оплаты:\n{url}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "pay_cryptobot")
async def pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Оплата через CryptoBot"""
    await callback.answer()
    data = await state.get_data()
    total_rub = data.get("total_rub")
    days = data.get("days", 30)
    if not total_rub:
        await callback.answer("Сначала выберите тариф и срок.", show_alert=True)
        return

    # Примерная конвертация RUB → USDT
    amount_usd = round(int(total_rub) * 0.0103, 2)

    try:
        created = await create_cryptobot_invoice(
            amount_usd,
            f"ByMeVPN {days} дней",
            callback.from_user.id,
        )
        if not created:
            await callback.answer(
                "💳 Оплата через CryptoBot временно недоступна. Попользуйтесь Telegram Stars или другими способами оплаты.",
                show_alert=True,
            )
            return
        url, invoice_id = created
    except Exception as e:
        logger.error(f"Ошибка создания инвойса CryptoBot: {e}")
        await callback.answer(
            "Оплата через CryptoBot сейчас недоступна. Попробуйте другой способ.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="₿ Оплатить через CryptoBot", url=url)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил, выдать ключ",
                    callback_data=f"cryptobot_check_{invoice_id}_{days}",
                )
            ],
            [InlineKeyboardButton(text="⚫️ Назад", callback_data="buy_vpn")],
        ]
    )
    await callback.bot.send_message(
        callback.from_user.id,
        f"Оплата через CryptoBot:\n{url}",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("cryptobot_check_"))
async def cryptobot_check(callback: CallbackQuery):
    """Проверка статуса оплаты CryptoBot"""
    await callback.answer()
    try:
        _, _, invoice_id_str, days_str = callback.data.split("_", 3)
        invoice_id = int(invoice_id_str)
        days = int(days_str)
    except Exception:
        await callback.answer("Некорректные данные платежа.", show_alert=True)
        return

    status = await get_cryptobot_invoice_status(invoice_id)
    if not status:
        await callback.answer(
            "Не удалось получить статус платежа. Попробуйте чуть позже или напишите в поддержку.",
            show_alert=True,
        )
        return

    if status == "paid":
        await give_subscription(
            callback.bot,
            callback.from_user.id,
            days,
            "paid_crypto",
            callback,
            is_paid=True,
        )
        await callback.answer("Оплата найдена, подписка выдана!", show_alert=True)
    elif status in {"active", "pending"}:
        await callback.answer(
            "Платёж ещё не завершён. Завершите оплату в CryptoBot и попробуйте ещё раз.",
            show_alert=True,
        )
    else:
        await callback.answer(
            f"Статус платежа: {status}. Если вы уверены, что оплата прошла, напишите в поддержку.",
            show_alert=True,
        )


@router.callback_query(F.data == "toggle_auto_renewal")
async def toggle_auto_renewal(callback: CallbackQuery, state: FSMContext):
    """Переключение автопродления"""
    await callback.answer()
    data = await state.get_data()
    current = bool(data.get("auto_renewal", False))
    new_value = not current
    await state.update_data(auto_renewal=new_value)

    # Меняем подпись кнопки в текущей клавиатуре
    markup = callback.message.reply_markup
    if markup:
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "toggle_auto_renewal":
                    label_state = "вкл" if new_value else "выкл"
                    btn.text = f"🔁 Автопродление: {label_state}"
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            pass

    msg = "Автопродление включено ✅" if new_value else "Автопродление выключено ❌"
    await callback.answer(msg, show_alert=True)


@router.pre_checkout_query()
async def pre_checkout_query(pre: PreCheckoutQuery):
    """Подтверждение предварительного запроса оплаты"""
    await pre.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    """Обработка успешной оплаты"""
    payment: SuccessfulPayment = message.successful_payment
    user_id = message.from_user.id
    currency = payment.currency
    total_amount = payment.total_amount
    payload = payment.invoice_payload or ""

    logger.info(
        "Успешная оплата: user=%s currency=%s amount=%s payload=%s",
        user_id,
        currency,
        total_amount,
        payload,
    )

    days_to_give = 30
    prefix = "paid"

    if payload.startswith("stars_vpn_") and currency == "XTR":
        parts = payload.split("_")
        if len(parts) >= 4:
            try:
                days_to_give = int(parts[3])
            except ValueError:
                days_to_give = 30
        prefix = "paid_stars"
        success_text = (
            f"✅ <b>Оплата через Telegram Stars прошла!</b>\n\n"
            f"⭐ Сумма: {total_amount} звёзд\n"
            f"🔑 Подписка будет выдана на {days_to_give} дней\n\n"
            f"🎉 Спасибо за доверие!"
        )
        await message.answer(success_text, parse_mode="HTML")
    else:
        prefix = f"paid_{currency.lower()}"
        success_text = (
            f"✅ <b>Оплата успешно проведена!</b>\n\n"
            f"💰 Валюта: {currency}\n"
            f"💳 Сумма: {total_amount}\n"
            f"🔑 Подписка сейчас будет активирована\n\n"
            f"🎉 Спасибо за покупку!"
        )
        await message.answer(success_text, parse_mode="HTML")

    try:
        # Создаем объект CallbackQuery для передачи в give_subscription
        class MockCallback:
            def __init__(self, bot, message):
                self.bot = bot
                self.message = message
        
        mock_callback = MockCallback(message.bot, message)
        await give_subscription(message.bot, user_id, days_to_give, prefix, mock_callback, is_paid=True)
        
        # Save payment to history
        method = "stars" if currency == "XTR" else currency.lower()
        await save_payment(
            user_id=user_id,
            amount=total_amount,
            currency=currency,
            method=method,
            days=days_to_give,
            payload=payload,
        )
        
        # Save auto-renewal setting if enabled
        from aiogram import Dispatcher
        dp = Dispatcher()  # Это нужно будет исправить при интеграции
        state = dp.fsm.get_context(message.bot, message.chat, message.from_user)
        try:
            data = await state.get_data()
            auto_renew = bool(data.get("auto_renewal", False))
            if auto_renew:
                await set_auto_renewal(user_id, True)
                logger.info(f"Auto-renewal enabled for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving auto-renewal setting: {e}")
        
        # Send payment success notification
        await send_payment_success_notification(message.bot, user_id, days_to_give, method)
    except Exception as e:
        logger.error(f"Ошибка выдачи после оплаты: {e}")
        await message.answer("❌ Ключ не выдан автоматически. Напишите в поддержку.")


@router.callback_query(F.data == "payment_history")
async def payment_history(callback: CallbackQuery):
    """История платежей"""
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        # Check if user has paid subscriptions
        has_paid = await has_paid_subscription(user_id)
        if not has_paid:
            await callback.message.answer(
                "💰 <b>История платежей</b>\n\n"
                "Этот раздел доступен только для пользователей с оплаченными подписками\n\n"
                "🎁 <b>Оформите подписку и получите:</b>\n"
                "• Доступ к истории всех платежей\n"
                "• Подробную статистику\n"
                "• Приоритетную поддержку"
            )
            return
        
        from database import get_user_payments
        payments = await get_user_payments(user_id)
        if not payments:
            text = (
                "💰 <b>История платежей пуста</b>\n\n"
                "Как только вы оплатите подписку, здесь появятся все операции с датами и суммами."
            )
        else:
            lines = ["💰 <b>История ваших платежей</b>\n"]
            total_spent = 0
            
            for p in payments[:15]:  # Show last 15 payments
                dt = datetime.fromtimestamp(p["created"]).strftime("%d.%m.%Y %H:%M")
                method_emoji = {
                    "stars": "⭐",
                    "yookassa": "💳", 
                    "cryptobot": "₿"
                }.get(p["method"], "💰")
                
                lines.append(
                    f"{method_emoji} {dt} — {p['amount']} {p['currency']} "
                    f"за {p['days']} дней"
                )
                total_spent += p["amount"] if p["currency"] == "RUB" else 0
            
            lines.append(f"\n💎 Всего потрачено: {total_spent:,} ₽".replace(",", " "))
            text = "\n".join(lines)
        
        await callback.message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error showing payment history for user {user_id}: {e}")
        await callback.answer("Ошибка загрузки истории платежей", show_alert=True)