"""
Core subscription logic: ask config name → create VPN key → deliver.
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import ADMIN_ID
from utils import LOGO_URL, send_with_photo, safe_answer
from database import add_key, get_referrer, add_payment, set_trial_used, log_key_error
from xui_client import create_xui_user, get_xui_user, format_traffic
from keyboards import after_key_kb, cancel_kb
from states import BuyFlow

logger = logging.getLogger(__name__)

# Create router for subscription handlers
router = Router()

# Referral bonus: 30 days for referrer when referred makes first paid purchase
REF_BONUS_DAYS = 30


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
    from constants import validate_device_limit, format_timestamp
    limit_ip = validate_device_limit(context.get("devices", context.get("limit_ip", 2)))
    amount = context.get("amount", 0)
    currency = context.get("currency", "RUB")
    method = context.get("method", "unknown")
    payload = context.get("payload", "")
    
    await state.clear()
    
    # Check if user has existing keys to extend
    from database import get_user_keys, extend_key, add_payment, log_analytics_event
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
        old_limit = key_to_extend.get("limit_ip", 2)
        
        base_time = max(old_expiry or 0, current_time)
        new_expiry = base_time + days * 86400
        
        # Extend the key in database with device limit update
        success = await extend_key(key_id, days, limit_ip=limit_ip)
        
        if success:
            # Update expiry and limit_ip in 3x-ui
            from xui_client import update_xui_user
            xui_res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
            
            if not xui_res:
                logger.warning("Failed to update 3x-ui user expiry for user %d", user_id)
            
            # Add payment record
            if is_paid and amount > 0:
                tariff_name = f"Продление {days} дней ({limit_ip} устр.)"
                await add_payment(
                    user_id, amount, currency, method, days, payload,
                    status="success", tariff=tariff_name, devices=limit_ip,
                    provider=method, provider_payment_id=payload,
                )
                await log_analytics_event(
                    user_id=user_id, event_type="subscription_renewed",
                    tariff=tariff_name, devices=limit_ip, amount=amount,
                    payment_provider=method,
                )
                if limit_ip != old_limit:
                    await log_analytics_event(
                        user_id=user_id, event_type="device_limit_changed",
                        devices=limit_ip, details=f"From {old_limit} to {limit_ip}",
                    )

            # Get subscription URL from existing key
            existing_key = key_to_extend.get("key", "")

            # Always get fresh subscription URL from 3x-ui to ensure correct format
            try:
                from xui_client import get_user_subscription_links
                subscription_links = await get_user_subscription_links(user_id)
                if subscription_links:
                    key_to_show = subscription_links[0]
                    from database import update_key_uuid
                    await update_key_uuid(key_id, key_to_show)
                    logger.info("Updated subscription URL for user %d during extension", user_id)
                else:
                    key_to_show = existing_key
            except Exception as e:
                logger.exception("Failed to get fresh subscription URL for user %d: %s", user_id, e)
                key_to_show = existing_key

            text = (
                f"✅ <b>Подписка продлена!</b>\n\n"
                f"🔑 <b>Ключ #{key_id}</b> продлен на <b>{days} дней</b>\n"
                f"📱 Доступно устройств: <b>{limit_ip}</b>\n"
                f"📅 Новый срок: до <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
                f"🔑 <b>Ваша подписка:</b>\n"
                f"<code>{key_to_show}</code>\n\n"
                f"Ключ остался прежним — всё работает автоматически!"
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
                    logger.info(f"Processing referral bonus for extension: user {user_id}, amount {amount}")
                    result = await process_payment_referral_bonus(user_id, amount, bot)
                    logger.info(f"Referral bonus result for extension: {result}")
                except ImportError:
                    logger.warning("Referral system module not available, skipping bonus processing")
                except Exception as e:
                    logger.error("Referral bonus error: %s", e)
            
            return
    
    # Check if user already exists in 3x-ui (e.g. created manually or pre-migration)
    from xui_client import get_xui_client_details, update_xui_user
    xui_client = await get_xui_client_details(user_id)
    if xui_client:
        logger.info("User %d has no DB key, but exists in 3x-ui - importing and extending safely", user_id)
        xui_res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
        sub_url = (xui_res.get("subscription_url") if xui_res else None) or xui_client.get("subscription_url", "")
        uuid = xui_client.get("uuid", "")
        new_expiry = (xui_res.get("expiry_time", 0) // 1000) if xui_res else (int(time.time()) + days * 86400)

        from database import add_key
        key_id = await add_key(user_id, sub_url, f"ByMeVPN_{user_id}", uuid, days, limit_ip)

        if is_paid and amount > 0:
            tariff_name = f"Подписка {days} дней ({limit_ip} устр.)"
            await add_payment(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip,
                provider=method, provider_payment_id=payload,
            )
            await log_analytics_event(
                user_id=user_id, event_type="subscription_renewed",
                tariff=tariff_name, devices=limit_ip, amount=amount,
                payment_provider=method,
            )

        text = (
            f"✅ <b>Подписка успешно активирована!</b>\n\n"
            f"🔑 <b>Ключ #{key_id}</b> активирован на <b>{days} дней</b>\n"
            f"📱 Доступно устройств: <b>{limit_ip}</b>\n"
            f"📅 Срок действия: до <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
            f"🔑 <b>Ваша подписка:</b>\n"
            f"<code>{sub_url}</code>\n\n"
            f"Ваш VPN-ключ сохранён и готов к работе!"
        )
        from keyboards import after_key_kb
        await bot.send_photo(
            chat_id=chat_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
        )
        return

    # No existing keys anywhere - create new one
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
        extend_existing=False,  # Always create new key for generated names
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
    limit_ip: int = 2,
    is_paid: bool = False,
    amount: int = 0,
    currency: str = "RUB",
    method: str = "trial",
    payload: str = "",
    extend_existing: bool = True,
) -> bool:
    """
    Создать или обновить клиента в 3x-ui, сохранить в БД, отправить ссылку на подписку пользователю.
    Гарантирует сохранение существующего UUID и ссылки на подписку.
    """
    from constants import validate_device_limit as const_validate_device_limit, format_timestamp
    limit_ip = const_validate_device_limit(limit_ip)

    # Check if user has existing keys to extend (if extend_existing is True)
    if extend_existing:
        from database import get_user_keys, extend_key, add_payment, log_analytics_event
        import time
        
        existing_keys = await get_user_keys(user_id)
        current_time = int(time.time())
        
        # Find active or expired keys (extend even expired keys within 7 days grace period)
        extendable_keys = [k for k in existing_keys if k.get("expiry", 0) > current_time - 7*86400]
        
        if extendable_keys:
            extendable_keys.sort(key=lambda k: k.get("expiry", 0), reverse=True)
            key_to_extend = extendable_keys[0]
            key_id = key_to_extend["id"]
            old_expiry = key_to_extend["expiry"]
            old_limit = key_to_extend.get("limit_ip", 2)
            
            base_time = max(old_expiry or 0, current_time)
            new_expiry = base_time + days * 86400

            # Extend the key in database with device limit update
            success = await extend_key(key_id, days, limit_ip=limit_ip)
            
            if success:
                # Update expiry and limit_ip in 3x-ui
                from xui_client import update_xui_user
                xui_res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
                
                if not xui_res:
                    logger.warning("Failed to update 3x-ui user expiry for user %d", user_id)
                
                # Add payment record (idempotent)
                if is_paid and amount > 0:
                    tariff_name = f"Продление {days} дней ({limit_ip} устр.)"
                    from database import record_payment_idempotent
                    await record_payment_idempotent(
                        user_id, amount, currency, method, days, payload,
                        status="success", tariff=tariff_name, devices=limit_ip,
                        provider=method, provider_payment_id=payload,
                    )
                    await log_analytics_event(
                        user_id=user_id, event_type="subscription_renewed",
                        tariff=tariff_name, devices=limit_ip, amount=amount,
                        payment_provider=method,
                    )
                    if limit_ip != old_limit:
                        await log_analytics_event(
                            user_id=user_id, event_type="device_limit_changed",
                            devices=limit_ip, details=f"From {old_limit} to {limit_ip}",
                        )

                # Get subscription URL from existing key
                existing_key = key_to_extend.get("key", "")

                # Always get fresh subscription URL from 3x-ui to ensure correct format
                try:
                    from xui_client import get_user_subscription_links
                    subscription_links = await get_user_subscription_links(user_id)
                    if subscription_links:
                        key_to_show = subscription_links[0]
                        from database import update_key_uuid
                        await update_key_uuid(key_id, key_to_show)
                    else:
                        key_to_show = existing_key
                except Exception as e:
                    logger.exception("Failed to get fresh subscription URL for user %d: %s", user_id, e)
                    key_to_show = existing_key

                text = (
                    f"✅ <b>Подписка продлена!</b>\n\n"
                    f"🔑 <b>Ключ #{key_id}</b> продлен на <b>{days} дней</b>\n"
                    f"📱 Доступно устройств: <b>{limit_ip}</b>\n"
                    f"📅 Новый срок: до <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
                    f"🔑 <b>Ваша подписка:</b>\n"
                    f"<code>{key_to_show}</code>\n\n"
                    f"Ключ остался прежним — всё работает автоматически!"
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
                        logger.info(f"Processing referral bonus for extension: user {user_id}, amount {amount}")
                        result = await process_payment_referral_bonus(user_id, amount, bot)
                        logger.info(f"Referral bonus result for extension: {result}")
                    except ImportError:
                        logger.warning("Referral system module not available, skipping bonus processing")
                    except Exception as e:
                        logger.error("Referral bonus error: %s", e)
                
                return True

        # If user has no DB key, check if they exist in 3x-ui before creating new!
        from xui_client import get_xui_client_details, update_xui_user
        xui_client = await get_xui_client_details(user_id)
        if xui_client:
            logger.info("User %d exists in 3x-ui but not in DB keys - updating safely", user_id)
            xui_res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
            sub_url = (xui_res.get("subscription_url") if xui_res else None) or xui_client.get("subscription_url", "")
            uuid = xui_client.get("uuid", "")
            new_expiry = (xui_res.get("expiry_time", 0) // 1000) if xui_res else (int(time.time()) + days * 86400)

            from database import add_key, add_payment, log_analytics_event
            key_id = await add_key(user_id, sub_url, config_name or f"ByMeVPN_{user_id}", uuid, days, limit_ip)

            if is_paid and amount > 0:
                tariff_name = f"Подписка {days} дней ({limit_ip} устр.)"
                from database import record_payment_idempotent
                await record_payment_idempotent(
                    user_id, amount, currency, method, days, payload,
                    status="success", tariff=tariff_name, devices=limit_ip,
                    provider=method, provider_payment_id=payload,
                )
                await log_analytics_event(
                    user_id=user_id, event_type="subscription_created",
                    tariff=tariff_name, devices=limit_ip, amount=amount,
                    payment_provider=method,
                )

            text = (
                f"✅ <b>Подписка успешно активирована!</b>\n\n"
                f"🔑 <b>Ключ #{key_id}</b> активирован на <b>{days} дней</b>\n"
                f"📱 Доступно устройств: <b>{limit_ip}</b>\n"
                f"📅 Срок действия: до <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
                f"🔑 <b>Ваша подписка:</b>\n"
                f"<code>{sub_url}</code>\n\n"
                f"Ваш VPN-ключ сохранён и готов к работе!"
            )
            from keyboards import after_key_kb
            await bot.send_photo(
                chat_id=chat_id, photo=LOGO_URL,
                caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
            )
            return True

    try:
        logger.info("deliver_key: user=%d name='%s' days=%d limit_ip=%d method=%s", user_id, config_name, days, limit_ip, method)

        # Создаем клиента в 3x-ui (безлимитный трафик, лимит по числу устройств = limit_ip)
        user_result = await create_xui_user(user_id, days, data_limit_gb=0, limit_ip=limit_ip)
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
        vless_links = user_result.get("vless_links", [])
        
        # Если subscription_url пуст, пробуем vless_links как fallback
        if not subscription_url and vless_links:
            subscription_url = vless_links[0]

        if not subscription_url:
            logger.error("No subscription URL in 3x-ui response for user %d", user_id)
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания ключа</b>\n\n"
                "Не удалось получить ссылку на подписку.\n"
                "Пожалуйста, напишите в поддержку.\n\n"
                "📞 Поддержка: @ByMeVPN_support_bot",
                parse_mode="HTML",
            )
            return False

        # Логируем ссылку для отладки
        logger.info("Subscription URL for user %d: %s", user_id, subscription_url[:50] + "...")

        # Сохраняем ссылку на подписку в БД
        key_id = await add_key(user_id, subscription_url, config_name, subscription_url, days, limit_ip)

        # Сохраняем запись о платеже (идемпотентно)
        if is_paid and amount > 0:
            tariff_name = f"{days} дней ({limit_ip} устр.)"
            from database import record_payment_idempotent
            await record_payment_idempotent(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip,
                provider=method, provider_payment_id=payload,
            )

        text = (
            f"Ключ активирован! Спасибо, что выбрали нас❤️\n\n"
            f"🔑 <b>Ваша подписка:</b>\n"
            f"<code>{subscription_url}</code>\n\n"
            f"📋 <b>Инструкция по подключению:</b>\n"
            f"1. Скопируйте ссылку выше\n"
            f"2. Откройте приложение (v2rayNG / Nekoray / v2rayN)\n"
            f"3. Добавьте сервер через подписку\n"
            f"4. Подключитесь к серверу"
        )
        
        await bot.send_photo(
            chat_id=chat_id, photo=LOGO_URL,
            caption=text, parse_mode="HTML", reply_markup=after_key_kb(),
        )

        # Реферальный бонус: начисляем рефералу 30 дней при первой платной покупке
        if is_paid:
            # Импортируем улучшенную реферальную систему
            try:
                from referral_system_new import process_payment_referral_bonus
                # Автоматически обрабатываем реферальный бонус
                logger.info(f"Processing payment referral bonus for user {user_id}, amount {amount}")
                result = await process_payment_referral_bonus(user_id, amount, bot)
                logger.info(f"Payment referral bonus result: {result}")
            except ImportError:
                # Пропускаем реферальный бонус, если модуль недоступен
                logger.warning("Referral system module not available, skipping bonus processing")
            except Exception as e:
                logger.error(f"Error processing payment referral bonus: {e}")

        logger.info("Subscription delivered: user=%d name='%s' days=%d", user_id, config_name, days)
        return True

    except TelegramForbiddenError as e:
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

