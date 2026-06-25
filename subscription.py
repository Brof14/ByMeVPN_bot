"""
Core subscription logic: ask config name → create VPN key → deliver.
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import ADMIN_ID
from utils import LOGO_URL, send_with_photo, safe_answer
from database import add_key, get_referrer, add_payment, set_trial_used, log_key_error
from marzban_client import create_marzban_user, get_marzban_user, format_traffic
from keyboards import after_key_kb, cancel_kb
from states import BuyFlow

logger = logging.getLogger(__name__)

# Create router for subscription handlers
router = Router()

# Referral bonus: 15 days for referrer when referred makes first paid purchase
REF_BONUS_DAYS = 15


async def ask_config_name(
    bot: Bot,
    target: "Message | CallbackQuery",
    state: FSMContext,
    context: dict,
) -> None:
    """Deliver key or extend existing one without asking user for config name."""
    user_id = target.from_user.id if hasattr(target, 'from_user') else target.message.from_user.id
    chat_id = target.message.chat.id if hasattr(target, 'message') else target.chat.id
    
    days = context.get("days", 30)
    is_paid = context.get("is_paid", False)
    limit_ip = 5
    amount = context.get("amount", 0)
    currency = context.get("currency", "RUB")
    method = context.get("method", "unknown")
    payload = context.get("payload", "")
    
    await state.clear()
    
    # Check if user has existing keys to extend
    from database import get_user_keys, extend_key, add_payment
    import time
    
    existing_keys = await get_user_keys(user_id)
    current_time = int(time.time())
    
    # Find active or expired keys (extend even expired keys within 7 days grace period)
    extendable_keys = [k for k in existing_keys if k.get("expiry", 0) > current_time - 7*86400]
    
    if extendable_keys:
        # Sort by expiry (most recent first) and extend the first one
        extendable_keys.sort(key=lambda k: k.get("expiry", 0), reverse=True)
        key_to_extend = extendable_keys[0]
        key_id = key_to_extend["id"]
        old_expiry = key_to_extend["expiry"]
        
        # Extend the key in database
        success = await extend_key(key_id, days)
        
        if success:
            # Update expiry in 3x-ui
            from marzban_client import update_marzban_user_expiry
            marzban_updated = await update_marzban_user_expiry(user_id, days)
            
            if not marzban_updated:
                logger.warning("Failed to update 3x-ui user expiry for user %d", user_id)
            
            # Add payment record
            if is_paid and amount > 0:
                tariff_name = f"Продление {days} дней (до 5 устройств)"
                await add_payment(
                    user_id, amount, currency, method, days, payload,
                    status="success", tariff=tariff_name, devices=limit_ip
                )
            
            # Calculate new expiry for display
            from constants import format_timestamp
            new_expiry = old_expiry + days * 86400

            # Get subscription URL from existing key
            existing_key = key_to_extend.get("key", "")

            # Use subscription URL
            key_to_show = existing_key
            
            # Get VLESS key from uuid field
            vless_key = key_to_extend.get("uuid", "")
            
            # If VLESS key is not in DB, try to get it from 3x-ui
            if not vless_key:
                try:
                    from marzban_client import get_user_subscription_links
                    vless_links = await get_user_subscription_links(user_id)
                    if vless_links:
                        vless_key = vless_links[0] if vless_links else ""
                        # Update VLESS key in database
                        from database import update_key_uuid
                        await update_key_uuid(key_id, vless_key)
                        logger.info("Retrieved and saved VLESS key from 3x-ui for user %d", user_id)
                except Exception as e:
                    logger.warning("Failed to get VLESS key from 3x-ui for user %d: %s", user_id, e)

            text = (
                f"✅ <b>Подписка продлена!</b>\n\n"
                f"🔑 <b>Ключ #{key_id}</b> продлен на <b>{days} дней</b>\n"
                f"📅 Новый срок: до <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
                f"🔗 <b>Ссылка на подписку:</b>\n"
                f"<code>{key_to_show}</code>\n\n"
                f"Ссылка на подписку осталась прежней — всё работает как раньше!"
            )
            
            from keyboards import after_key_kb
            from utils import send_with_photo, LOGO_URL
            await bot.send_photo(
                chat_id=chat_id, photo=LOGO_URL,
                caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
            )
            
            # Process referral bonuses for paid extensions
            if is_paid and amount > 0:
                try:
                    from referral_system_new import process_payment_referral_bonus
                    await process_payment_referral_bonus(user_id, amount, bot)
                except ImportError:
                    logger.warning("Referral system module not available, skipping bonus processing")
                except Exception as e:
                    logger.error("Referral bonus error: %s", e)
            
            return
    
    # No existing keys - create new one
    prefix = context.get("prefix", "vpn")
    config_name = f"{prefix}_user_{user_id}"
    
    await deliver_key_with_generated_name(
        bot=bot,
        target=target,
        state=state,
        context=context,
        config_name=config_name,
    )


async def deliver_key_with_generated_name(
    bot: Bot,
    target: "Message | CallbackQuery",
    state: FSMContext,
    context: dict,
    config_name: str,
) -> None:
    """Deliver key using automatically generated name."""
    user_id = target.from_user.id if hasattr(target, 'from_user') else target.message.from_user.id
    chat_id = target.message.chat.id if hasattr(target, 'message') else target.chat.id
    
    days = context.get("days", 30)
    is_paid = context.get("is_paid", False)
    limit_ip = 5  # All plans (trial and paid) support up to 5 devices
    amount = context.get("amount", 0)
    currency = context.get("currency", "RUB")
    method = context.get("method", "unknown")
    payload = context.get("payload", "")
    trial_uid = context.get("_trial_user_id")
    yk_payment_id = context.get("_yk_payment_id")

    await state.clear()

    success = await deliver_key(
        bot=bot, user_id=user_id, chat_id=chat_id,
        config_name=config_name, days=days, limit_ip=limit_ip,
        is_paid=is_paid, amount=amount, currency=currency,
        method=method, payload=payload,
    )

    # Clean up YooKassa pending record after successful delivery
    if success and yk_payment_id:
        from database import delete_yookassa_pending
        try:
            await delete_yookassa_pending(yk_payment_id)
        except Exception as e:
            logger.error("Could not delete pending yk payment %s: %s", yk_payment_id, e)

    # Unmark trial if delivery failed (allow user to retry)
    if not success and trial_uid:
        try:
            # Trial was already marked as used, we can't reset it for security
            logger.warning("Trial delivery failed for user %d, trial remains used", trial_uid)
        except Exception as e:
            logger.error("Could not handle trial reset for %d: %s", trial_uid, e)


async def deliver_key(
    bot: Bot,
    user_id: int,
    chat_id: int,
    config_name: str,
    days: int,
    limit_ip: int = 1,
    is_paid: bool = False,
    amount: int = 0,
    currency: str = "RUB",
    method: str = "trial",
    payload: str = "",
) -> bool:
    """
    Создать клиента в 3x-ui, сохранить в БД, отправить ссылку на подписку пользователю.
    limit_ip — количество одновременных подключений устройств (1, 2 или 5).
    Возвращает True при успехе.
    """
    # Validate device limit (legacy, not used in 3x-ui but kept for compatibility)
    from constants import validate_device_limit as const_validate_device_limit
    limit_ip = const_validate_device_limit(limit_ip)

    try:
        logger.info("deliver_key: user=%d name='%s' days=%d limit_ip=%d method=%s", user_id, config_name, days, limit_ip, method)

        # Создаем клиента в 3x-ui (безлимитный трафик)
        user_result = await create_marzban_user(user_id, days, data_limit_gb=0)
        if not user_result:
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания подписки</b>\n\n"
                "Не удалось создать пользователя.\n"
                "Пожалуйста, попробуйте позже или напишите в поддержку.\n\n"
                "📞 Поддержка: @ByMeVPN_support_bot",
                parse_mode="HTML",
            )
            return False

        # Получаем ссылку на подписку из ответа 3x-ui
        subscription_url = user_result.get("subscription_url", "")

        if not subscription_url:
            logger.error("No subscription_url in 3x-ui response for user %d", user_id)
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания подписки</b>\n\n"
                "Не удалось получить ссылку на подписку.\n"
                "Пожалуйста, напишите в поддержку.\n\n"
                "📞 Поддержка: @ByMeVPN_support_bot",
                parse_mode="HTML",
            )
            return False

        # Получаем VLESS ключи из ответа
        vless_links = user_result.get("vless_links", [])
        vless_key = vless_links[0] if vless_links else ""

        # Логируем сгенерированную ссылку для отладки
        logger.info("Subscription URL for user %d: %s", user_id, subscription_url[:50] + "...")
        if vless_key:
            logger.info("VLESS key for user %d: %s", user_id, vless_key[:50] + "...")

        # Выполняем операции с БД параллельно для лучшей производительности
        db_tasks = []
        # Сохраняем ссылку на подписку в БД (для продления используем subscription_url)
        # Сохраняем VLESS ключ в поле uuid
        db_tasks.append(add_key(user_id, subscription_url, config_name, vless_key, days, limit_ip))

        # Сохраняем запись о платеже только после успешного создания подписки
        if is_paid and amount > 0:
            # Все платные тарифы поддерживают до 5 устройств
            tariff_name = f"{days} дней (до 5 устройств)"
            db_tasks.append(add_payment(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip
            ))

        # Выполняем операции с БД параллельно
        await asyncio.gather(*db_tasks)

        text = (
            f"Подписка активирована! Спасибо, что выбрали нас❤️\n\n"
            f"🔗 <b>Ссылка на подписку:</b>\n"
            f"<code>{subscription_url}</code>\n\n"
            f"📋 <b>Инструкция по подключению:</b>\n"
            f"1. Скопируйте ссылку выше\n"
            f"2. Откройте приложение (v2rayNG / Nekoray / v2rayN)\n"
            f"3. Добавьте подписку через ссылку\n"
            f"4. Подключитесь к серверу"
        )
        
        await bot.send_photo(
            chat_id=chat_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
        )

        # Реферальный бонус: начисляем рефералу 15 дней при первой платной покупке
        if is_paid:
            # Импортируем улучшенную реферальную систему
            try:
                from referral_system_new import process_payment_referral_bonus
                # Автоматически обрабатываем реферальный бонус
                await process_payment_referral_bonus(user_id, amount, bot)
            except ImportError:
                # Пропускаем реферальный бонус, если модуль недоступен
                logger.warning("Referral system module not available, skipping bonus processing")

        logger.info("Subscription delivered: user=%d name='%s' days=%d", user_id, config_name, days)
        return True

    except aiogram.exceptions.TelegramForbiddenError as e:
        logger.warning("User %d blocked the bot, cannot deliver subscription: %s", user_id, e)
        # Пользователь заблокировал бота - это не критическая ошибка
        # Ключ создан в 3x-ui, но не может быть доставлен
        return False
    except Exception as e:
        logger.exception("deliver_key FAILED for user=%d name='%s': %s", user_id, config_name, e)

        # Логируем ошибку в БД для отслеживания в админ-панели
        try:
            await log_key_error(
                user_id=user_id,
                error_type="subscription_creation_failed",
                error_message=str(e),
                context={
                    "config_name": config_name,
                    "days": days,
                    "limit_ip": limit_ip,
                    "is_paid": is_paid,
                    "amount": amount,
                    "method": method,
                    "payload": payload
                }
            )
        except Exception as log_error:
            logger.error("Failed to log key error: %s", log_error)

        # Для платных платежей уведомляем админа об ошибке
        if is_paid:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚨 <b>Ошибка создания подписки после оплаты!</b>\n\n"
                    f"👤 User: <code>{user_id}</code>\n"
                    f"📝 Имя: {config_name}\n"
                    f"⏳ Дней: {days}\n"
                    f"💰 Сумма: {amount} {currency}\n"
                    f"🔧 Метод: {method}\n"
                    f"🎫 Payload: {payload}\n"
                    f"❌ <code>{str(e)[:300]}</code>\n\n"
                    f"⚠️ Необходимо вернуть деньги или выдать подписку вручную!",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        
        try:
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания VPN подписки</b>\n\n"
                "Не удалось создать подписку в панели управления.\n"
                "Если вы оплатили, деньги будут возвращены автоматически.\n"
                "Пожалуйста, напишите в поддержку — мы поможем!\n\n"
                "📞 Поддержка: @ByMeVPN_support_bot",
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


# ---------------------------------------------------------------------------
# Message handlers for config name input
# ---------------------------------------------------------------------------

