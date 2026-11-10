"""
Core subscription logic: ask config name → create VPN key → deliver.
"""
import asyncio
import logging
import secrets
import time
import weakref
from datetime import datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from config import ADMIN_ID
from constants import REFERRAL_FIRST_PAYMENT_BONUS_DAYS, format_timestamp
from utils import send_or_edit, send_photo_message, send_with_photo, safe_answer
from bot_assets import KEY
import database
from database import (
    add_key, get_referrer, add_payment, set_trial_used, log_key_error,
    get_user_keys, log_analytics_event, record_payment_idempotent,
    update_key_uuid, set_key_expiry,
)
from xui_client import create_xui_user, get_xui_user, format_traffic
from keyboards import after_key_kb, cancel_kb
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)

# Create router for subscription handlers
router = Router()

# Serialise deliveries for the same account within this process. Other bot
# processes and direct panel writers still require independent coordination.
_delivery_locks = weakref.WeakValueDictionary()


def _activation_message(title: str, bonus_note: str, days: int, limit_ip: int, expiry: int | None = None) -> str:
    """Use the current website-based setup flow in every activation message."""
    expiry_line = f"Действует до: <b>{format_timestamp(expiry)[:10]}</b>\n" if expiry else ""
    return (
        f"{title}\n\n{bonus_note}"
        f"Доступ: <b>{days} дней</b>\n"
        f"Устройства: <b>{limit_ip}</b>\n"
        f"{expiry_line}\n"
        "Ваш ByMeVPN готов к использованию.\n\n"
        "Нажмите «Подключить VPN» ниже — откроется страница, которая подберёт приложение "
        "для вашего устройства и поможет добавить подписку."
    )


async def _send_delivery_message(
    bot: Bot,
    chat_id: int,
    text: str,
    subscription_url: str = "",
    *,
    edit_target=None,
) -> bool:
    """Deliver the key message to the user. NEVER raises.

    Provisioning success must not be downgraded by a notification failure:
    by the time this runs, the key already exists in DB and 3x-ui, so a
    'chat not found' / bot-blocked / deleted-account error means
    notification=FAILED, provisioning=SUCCESS. The user can always fetch
    the key via «Мои ключи».
    """
    try:
        markup = after_key_kb(subscription_url)
        if edit_target is not None:
            # Trial is initiated from the main menu's photo message. Replace
            # that screen with the key card instead of posting a second message.
            await send_or_edit(bot, edit_target, text, markup, photo=KEY)
        else:
            await send_photo_message(bot, chat_id, KEY, text, reply_markup=markup)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error("Delivery message not delivered to %s (provisioning already done): %s", chat_id, e)
        return False
    except Exception as e:
        logger.error("Unexpected delivery-message failure for %s: %s", chat_id, e)
        return False


async def _log_notification_failure(user_id: int, context: dict) -> None:
    """Record that provisioning succeeded but Telegram delivery failed."""
    try:
        await log_key_error(
            user_id=user_id,
            error_type="notification_failed",
            error_message="key provisioned but Telegram message not delivered",
            context=context,
        )
    except Exception as e:
        logger.error("Failed to log notification failure for %s: %s", user_id, e)

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
    skip_referral_bonus: bool = False,
    delivery_target=None,
) -> bool:
    # Keep the lightweight in-process lock to avoid needless contention.
    # The SQLite lease below also serializes workers running in *other* bot
    # processes against the same persistent database file.
    lock = _delivery_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _delivery_locks[user_id] = lock
    async with lock:
        token = secrets.token_urlsafe(24)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        try:
            while True:
                if await database.try_acquire_delivery_lease(user_id, token):
                    break
                if loop.time() >= deadline:
                    logger.warning("Delivery already running for user %d in another worker", user_id)
                    return False
                await asyncio.sleep(0.2)
        except Exception:
            logger.exception("Cannot safely acquire cross-process delivery lease for user %d", user_id)
            return False

        stop_heartbeat = asyncio.Event()
        lost_lease = asyncio.Event()

        async def heartbeat() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    if not await database.renew_delivery_lease(user_id, token):
                        logger.error("Delivery lease ownership lost for user %d", user_id)
                        lost_lease.set()
                        return
                except Exception:
                    logger.exception("Delivery lease renewal failed for user %d", user_id)
                    lost_lease.set()
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            delivered = await _deliver_key_unlocked(
                bot, user_id, chat_id, config_name, days, limit_ip, is_paid,
                amount, currency, method, payload, extend_existing,
                skip_referral_bonus, delivery_target,
            )
            # Confirm ownership at the end as well: a stalled event loop may
            # miss its heartbeat while another worker reclaims an expired lease.
            try:
                still_owner = await database.renew_delivery_lease(user_id, token)
            except Exception:
                logger.exception("Could not confirm delivery lease at completion for user %d", user_id)
                still_owner = False
            if lost_lease.is_set() or not still_owner:
                # The remote update may have succeeded. A deterministic retry /
                # manual reconciliation is safer than falsely claiming delivery.
                logger.error("Unconfirmed provisioning after delivery lease loss for user %d", user_id)
                return False
            return delivered
        finally:
            stop_heartbeat.set()
            await heartbeat_task
            try:
                await database.release_delivery_lease(user_id, token)
            except Exception:
                logger.exception("Could not release delivery lease for user %d", user_id)

async def _deliver_key_unlocked(
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
    skip_referral_bonus: bool = False,
    delivery_target=None,
) -> bool:
    """
    Создать или обновить клиента в 3x-ui, сохранить в БД, отправить ссылку на подписку пользователю.
    Гарантирует сохранение существующего UUID и ссылки на подписку.
    """
    from constants import validate_device_limit as const_validate_device_limit, format_timestamp
    limit_ip = const_validate_device_limit(limit_ip)

    # Check if user has existing keys to extend (if extend_existing is True)
    if extend_existing:
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
            if is_paid and payload:
                new_expiry = await database.get_or_create_provisioning_intent(
                    method, payload, user_id, new_expiry
                )
            if is_paid and payload and old_expiry and new_expiry < old_expiry:
                # Another operation has moved the subscription past this old
                # order intent. Applying it would shorten paid access; leave
                # the order pending for reconciliation instead.
                logger.error("Order %s requires reconciliation for user %d", payload, user_id)
                return False

            # Set the immutable target, rather than applying relative days.
            success = await set_key_expiry(key_id, new_expiry, limit_ip=limit_ip)
            
            if success:
                # Update expiry and limit_ip in 3x-ui
                from xui_client import update_xui_user
                # Apply the exact target expiry. If 3x-ui applied the update
                # but its response timed out, retrying this same order remains
                # idempotent instead of adding the days twice.
                xui_res = await update_xui_user(
                    user_id, new_expiry_ms=new_expiry * 1000, limit_ip=0, limit_hwid=limit_ip
                )

                if not xui_res:
                    # Compensate the DB extension. This keeps a failed provisioning
                    # attempt retryable without blindly charging days twice.
                    rollback_ok = await set_key_expiry(key_id, int(old_expiry or 0), old_limit)
                    error_context = {
                        "branch": "deliver_key.extend",
                        "days": days,
                        "limit_ip": limit_ip,
                        "rollback_ok": rollback_ok,
                        "payment_id": payload,
                    }
                    await log_key_error(
                        user_id=user_id,
                        error_type="subscription_extension_failed",
                        error_message="3x-ui did not confirm subscription extension",
                        context=error_context,
                    )
                    logger.error(
                        "3x-ui extension failed for user %d; DB rollback %s",
                        user_id, "succeeded" if rollback_ok else "FAILED",
                    )
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⚠️ Не удалось подтвердить продление в 3x-ui. Пользователь: <code>{user_id}</code>; "
                            f"платёж: <code>{payload}</code>; откат БД: {'OK' if rollback_ok else 'ОШИБКА'}. "
                            "Требуется сверка; не выдавать дни повторно вслепую.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    if not rollback_ok:
                        raise RuntimeError(
                            f"3x-ui extension failed and DB rollback failed for user {user_id}, key {key_id}"
                        )
                    return False

                # Add payment record (idempotent)
                if is_paid and amount > 0:
                    tariff_name = f"Продление {days} дней ({limit_ip} устр.)"
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
                        await update_key_uuid(key_id, key_to_show)
                    else:
                        key_to_show = existing_key
                except Exception as e:
                    logger.exception("Failed to get fresh subscription URL for user %d: %s", user_id, e)
                    key_to_show = existing_key

                title = (
                    "<b>Друг зарегистрировался по вашей ссылке</b>"
                    if method == "ref_signup_bonus"
                    else "<b>Бонус за друга начислен</b>" if method == "ref_bonus"
                    else "<b>Ваш приз ByMeVPN начислен</b>" if method == "giveaway_prize"
                    else "Бонус за рекламу начислен" if method == "ad_bonus"
                    else "<b>Подписка продлена</b>"
                )
                bonus_note = (
                    f"За регистрацию приглашённого начислено <b>+{days} дней</b>.\n\n"
                    if method == "ref_signup_bonus" else
                    f"За первую оплату приглашённого начислено <b>+{days} дней</b>.\n\n"
                    if method == "ref_bonus" else
                    f"Вам автоматически начислен приз <b>+{days} дней</b>.\n\n"
                    if method == "giveaway_prize" else ""
                )
                text = (
                    f"{title}\n\n"
                    f"{bonus_note}"
                    f"<b>Ключ #{key_id}</b> продлён на <b>{days} дней</b>\n"
                    f"Устройства: <b>{limit_ip}</b>\n"
                    f"Действует до: <b>{format_timestamp(new_expiry)[:10]}</b>\n\n"
                    f"<b>Ссылка для подключения</b>\n"
                    f"<code>{key_to_show}</code>\n\n"
                    f"Ссылка на VPN не изменилась. Повторно подключать устройства не нужно."
                )
                
                delivered = await _send_delivery_message(
                    bot, chat_id, text, key_to_show, edit_target=delivery_target,
                )
                if not delivered:
                    await _log_notification_failure(
                        user_id, {"branch": "deliver_key.extend", "days": days, "limit_ip": limit_ip}
                    )

                # NOTE: no referral reward here — centralized at the payment
                # processing level (see award_referral_for_payment). The
                # skip_referral_bonus flag (intro trial) is honoured there.

                return True

        # If user has no DB key, check if they exist in 3x-ui before creating new!
        from xui_client import get_xui_client_details, update_xui_user
        xui_client = await get_xui_client_details(user_id)
        if xui_client:
            logger.info("User %d exists in 3x-ui but not in DB keys - updating safely", user_id)
            target_expiry = max(
                int(xui_client.get("expiry_time") or 0) // 1000,
                int(time.time()),
            ) + days * 86400
            if is_paid and payload:
                target_expiry = await database.get_or_create_provisioning_intent(
                    method, payload, user_id, target_expiry
                )
            if is_paid and payload and target_expiry < int(xui_client.get("expiry_time") or 0) // 1000:
                logger.error("Order %s requires panel reconciliation for user %d", payload, user_id)
                return False
            xui_res = await update_xui_user(
                user_id, new_expiry_ms=target_expiry * 1000, limit_ip=0, limit_hwid=limit_ip
            )
            if not xui_res:
                # Do not import a stale panel snapshot as a newly provisioned
                # key: the renewal/upgrade may not have reached 3x-ui.
                await log_key_error(
                    user_id=user_id,
                    error_type="subscription_import_update_failed",
                    error_message="Existing 3x-ui client could not be safely updated; key import aborted",
                    context={"branch": "deliver_key.import_xui", "days": days, "limit_ip": limit_ip, "payment_id": payload},
                )
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Не удалось безопасно обновить клиента 3x-ui перед восстановлением ключа. "
                        f"Пользователь: <code>{user_id}</code>; платёж: <code>{payload}</code>. Нужна сверка.",
                        parse_mode="HTML",
                    )
                    await bot.send_message(
                        chat_id,
                        "Оплата получена, но активация VPN пока не завершена. "
                        "Не оплачивайте повторно. Мы проверим операцию и восстановим доступ.",
                    )
                except Exception:
                    pass
                return False

            sub_url = xui_res.get("subscription_url") or xui_client.get("subscription_url", "")
            uuid = xui_client.get("uuid", "")
            new_expiry = xui_res.get("expiry_time", 0) // 1000

            if not sub_url or new_expiry <= 0:
                logger.error(
                    "3x-ui update for user %d returned incomplete subscription data; key import aborted",
                    user_id,
                )
                return False

            key_id = await add_key(
                user_id, sub_url, config_name or f"ByMeVPN_{user_id}", uuid,
                days, limit_ip, expiry=new_expiry,
            )

            if is_paid and amount > 0:
                tariff_name = f"Подписка {days} дней ({limit_ip} устр.)"
                await record_payment_idempotent(
                    user_id, amount, currency, method, days, payload,
                    status="success", tariff=tariff_name, devices=limit_ip,
                    provider=method, provider_payment_id=payload,
                )
                await log_analytics_event(
                    user_id=user_id,
                    event_type="trial_started" if skip_referral_bonus else "subscription_created",
                    tariff=tariff_name, devices=limit_ip, amount=amount,
                    payment_provider=method,
                )

            title = (
                "<b>Друг зарегистрировался по вашей ссылке</b>"
                if method == "ref_signup_bonus"
                else "<b>Ваш приз ByMeVPN начислен</b>" if method == "giveaway_prize"
                else "Бонус за рекламу активирован" if method == "ad_bonus"
                else "<b>Подписка активирована</b>"
            )
            bonus_note = (
                f"За регистрацию приглашённого начислено <b>+{days} дней</b>.\n\n"
                if method == "ref_signup_bonus" else
                f"Вам автоматически начислен приз <b>+{days} дней</b>.\n\n"
                if method == "giveaway_prize" else ""
            )
            text = _activation_message(title, bonus_note, days, limit_ip, new_expiry)
            delivered = await _send_delivery_message(
                bot, chat_id, text, sub_url, edit_target=delivery_target,
            )
            if not delivered:
                await _log_notification_failure(
                    user_id, {"branch": "deliver_key.import_xui", "days": days, "limit_ip": limit_ip}
                )
            return True

    try:
        logger.info("deliver_key: user=%d name='%s' days=%d limit_ip=%d method=%s", user_id, config_name, days, limit_ip, method)

        target_expiry = int(time.time()) + days * 86400
        if is_paid and payload:
            target_expiry = await database.get_or_create_provisioning_intent(
                method, payload, user_id, target_expiry
            )

        # Создаем клиента в 3x-ui (безлимитный трафик, лимит по числу устройств = limit_ip)
        user_result = await create_xui_user(
            user_id, days, data_limit_gb=0, limit_ip=0, limit_hwid=limit_ip,
            new_expiry_ms=target_expiry * 1000,
        )
        if not user_result:
            await bot.send_message(
                chat_id,
                "❌ <b>Ошибка создания подписки</b>\n\n"
                "Не удалось создать пользователя.\n"
                "Пожалуйста, попробуйте позже или напишите в поддержку.\n\n"
                "📞 Поддержка: @ByMeVPNSupportBot",
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
                "📞 Поддержка: @ByMeVPNSupportBot",
                parse_mode="HTML",
            )
            return False

        # Subscription URLs are access credentials; never write any part to logs.

        # Сохраняем ссылку на подписку и настоящий UUID клиента в БД
        client_uuid = user_result.get("uuid") or subscription_url
        key_id = await add_key(
            user_id, subscription_url, config_name, client_uuid, days, limit_ip,
            expiry=target_expiry,
        )
        if user_result.get("created_new_client"):
            await database.schedule_connection_reminder(key_id, traffic_baseline=0)

        # Сохраняем запись о платеже (идемпотентно)
        if is_paid and amount > 0:
            tariff_name = f"{days} дней ({limit_ip} устр.)"
            await record_payment_idempotent(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip,
                provider=method, provider_payment_id=payload,
            )

        device_word = "устройство" if limit_ip == 1 else ("устройства" if limit_ip in (2, 3, 4) else "устройств")
        title = (
            "<b>Друг зарегистрировался по вашей ссылке</b>"
            if method == "ref_signup_bonus"
            else "<b>Ваш приз ByMeVPN начислен</b>" if method == "giveaway_prize"
            else "Бонус за рекламу активирован" if method == "ad_bonus"
            else "<b>Подписка активирована</b>"
        )
        bonus_note = (
            f"За регистрацию приглашённого начислено <b>+{days} дней</b>.\n\n"
            if method == "ref_signup_bonus" else
            f"Вам автоматически начислен приз <b>+{days} дней</b>.\n\n"
            if method == "giveaway_prize" else ""
        )
        text = _activation_message(title, bonus_note, days, limit_ip)
        
        delivered = await _send_delivery_message(
            bot, chat_id, text, subscription_url, edit_target=delivery_target,
        )
        if not delivered:
            await _log_notification_failure(
                user_id, {"branch": "deliver_key.create", "days": days, "limit_ip": limit_ip}
            )

        # NOTE: no referral reward here — one qualifying payment must produce
        # exactly one reward, and it is awarded once at the payment-processing
        # level (webhook.py / Stars handler → award_referral_for_payment).

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
                "Не удалось подтвердить создание подписки на сервере.\n"
                "Если вы оплатили, не оплачивайте повторно — поддержка проверит платёж и восстановит доступ.\n\n"
                "📞 Поддержка: @ByMeVPNSupportBot",
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
        from aiogram.types import InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🎁 Активировать +{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней",
                callback_data=f"ref_bonus_activate:{new_user_id}",
            )
        ]])
        await bot.send_message(
            referrer_id,
            f"🎁 <b>Ваш реферал оформил подписку!</b>\n\n"
            f"Вам начислено <b>+{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней</b> бесплатно.\n\n"
            "Нажмите кнопку ниже — бот автоматически добавит бонус к вашему доступу.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("Failed to notify referrer %d: %s", referrer_id, e)


# ---------------------------------------------------------------------------
# Message handlers for config name input
# ---------------------------------------------------------------------------

