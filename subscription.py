"""
Core subscription logic: ask config name → create VPN key → deliver.
"""
import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import ADMIN_ID
from utils import LOGO_URL, safe_answer
from database import add_key, add_payment, log_key_error
from marzban_client import create_marzban_user, get_user_subscription_links
from keyboards import after_key_kb
from states import BuyFlow

logger = logging.getLogger(__name__)

router = Router()

REF_BONUS_DAYS = 15

SUPPORT_ERROR_TEXT = (
    "❌ <b>Ошибка создания ключа</b>\n\n"
    "Не удалось создать VPN-подписку. Пожалуйста, обратитесь в поддержку — "
    "мы поможем вручную.\n\n"
    "📞 Поддержка: @ByMeVPN_support_bot"
)


def _extract_target_ids(target: "Message | CallbackQuery") -> tuple[int, int]:
    """Return (user_id, chat_id) from Message or CallbackQuery."""
    if isinstance(target, CallbackQuery):
        return target.from_user.id, target.message.chat.id
    return target.from_user.id, target.chat.id


def _build_key_caption(
    subscription_url: str,
    *,
    title: str = "Подписка активирована! Спасибо, что выбрали нас❤️",
    vless_key: str = "",
    extra_lines: str = "",
) -> str:
    text = (
        f"{title}\n\n"
        f"🔗 <b>Ссылка на подписку:</b>\n"
        f"<code>{subscription_url}</code>\n\n"
        f"📋 <b>Инструкция по подключению:</b>\n"
        f"1. Скопируйте ссылку выше\n"
        f"2. Откройте приложение (Happ / v2rayNG / Nekoray)\n"
        f"3. Добавьте подписку через ссылку\n"
        f"4. Подключитесь к серверу"
    )
    if vless_key:
        text += (
            f"\n\n🔑 <b>VLESS ключ (для прямого подключения):</b>\n"
            f"<code>{vless_key}</code>"
        )
    if extra_lines:
        text += f"\n\n{extra_lines}"
    return text


async def _send_key_message(
    bot: Bot,
    chat_id: int,
    subscription_url: str,
    *,
    title: str = "Подписка активирована! Спасибо, что выбрали нас❤️",
    vless_key: str = "",
    extra_lines: str = "",
) -> None:
    """Send subscription link to user; fall back to text if photo fails."""
    text = _build_key_caption(
        subscription_url,
        title=title,
        vless_key=vless_key,
        extra_lines=extra_lines,
    )
    kb = after_key_kb(subscription_url)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=LOGO_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("send_photo failed for chat %d: %s — falling back to text", chat_id, e)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )


async def _notify_key_creation_failed(bot: Bot, chat_id: int, user_id: int, reason: str) -> None:
    logger.error("Key delivery failed for user %d: %s", user_id, reason)
    try:
        await log_key_error(
            user_id=user_id,
            error_type="subscription_creation_failed",
            error_message=reason,
            context={"stage": "create_marzban_user"},
        )
    except Exception as log_error:
        logger.error("Failed to log key error: %s", log_error)
    try:
        await bot.send_message(chat_id, SUPPORT_ERROR_TEXT, parse_mode="HTML")
    except Exception as send_error:
        logger.error("Failed to notify user %d about key error: %s", user_id, send_error)


async def ask_config_name(
    bot: Bot,
    target: "Message | CallbackQuery",
    state: FSMContext,
    context: dict,
) -> bool:
    """Deliver key or extend existing one without asking user for config name."""
    user_id, chat_id = _extract_target_ids(target)

    days = context.get("days", 30)
    is_paid = context.get("is_paid", False)
    limit_ip = context.get("limit_ip", 5)
    amount = context.get("amount", 0)
    currency = context.get("currency", "RUB")
    method = context.get("method", "unknown")
    payload = context.get("payload", "")

    await state.clear()

    from database import get_user_keys, extend_key
    import time
    from constants import format_timestamp

    existing_keys = await get_user_keys(user_id)
    current_time = int(time.time())
    extendable_keys = [k for k in existing_keys if k.get("expiry", 0) > current_time - 7 * 86400]

    if extendable_keys:
        extendable_keys.sort(key=lambda k: k.get("expiry", 0), reverse=True)
        key_to_extend = extendable_keys[0]
        key_id = key_to_extend["id"]
        old_expiry = key_to_extend["expiry"]

        success = await extend_key(key_id, days)
        if not success:
            await _notify_key_creation_failed(bot, chat_id, user_id, "extend_key returned False")
            return False

        from marzban_client import update_marzban_user_expiry
        marzban_updated = await update_marzban_user_expiry(user_id, days)
        if not marzban_updated:
            logger.warning("Failed to update 3x-ui user expiry for user %d", user_id)

        if is_paid and amount > 0:
            tariff_name = f"Продление {days} дней (до 5 устройств)"
            await add_payment(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip,
            )

        new_expiry = old_expiry + days * 86400
        key_to_show = key_to_extend.get("key", "")
        vless_key = key_to_extend.get("uuid", "")

        if not vless_key:
            try:
                vless_links = await get_user_subscription_links(user_id)
                if vless_links:
                    vless_key = vless_links[0]
                    from database import update_key_uuid
                    await update_key_uuid(key_id, vless_key)
            except Exception as e:
                logger.warning("Failed to get VLESS key from 3x-ui for user %d: %s", user_id, e)

        await _send_key_message(
            bot,
            chat_id,
            key_to_show,
            title=(
                f"✅ <b>Подписка продлена!</b>\n\n"
                f"🔑 <b>Ключ #{key_id}</b> продлен на <b>{days} дней</b>\n"
                f"📅 Новый срок: до <b>{format_timestamp(new_expiry)[:10]}</b>"
            ),
            vless_key=vless_key,
            extra_lines="Ссылка на подписку осталась прежней — всё работает как раньше!",
        )

        if is_paid and amount > 0:
            try:
                from referral_system_new import process_payment_referral_bonus
                await process_payment_referral_bonus(user_id, amount, bot)
            except ImportError:
                logger.warning("Referral system module not available, skipping bonus processing")
            except Exception as e:
                logger.error("Referral bonus error: %s", e)

        return True

    prefix = context.get("prefix", "vpn")
    config_name = f"{prefix}_user_{user_id}"

    return await deliver_key_with_generated_name(
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
) -> bool:
    """Deliver key using automatically generated name."""
    user_id, chat_id = _extract_target_ids(target)

    days = context.get("days", 30)
    is_paid = context.get("is_paid", False)
    limit_ip = context.get("limit_ip", 5)
    amount = context.get("amount", 0)
    currency = context.get("currency", "RUB")
    method = context.get("method", "unknown")
    payload = context.get("payload", "")
    trial_uid = context.get("_trial_user_id")
    yk_payment_id = context.get("_yk_payment_id")

    await state.clear()

    success = await deliver_key(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        config_name=config_name,
        days=days,
        limit_ip=limit_ip,
        is_paid=is_paid,
        amount=amount,
        currency=currency,
        method=method,
        payload=payload,
    )

    if success and yk_payment_id:
        from database import delete_yookassa_pending
        try:
            await delete_yookassa_pending(yk_payment_id)
        except Exception as e:
            logger.error("Could not delete pending yk payment %s: %s", yk_payment_id, e)

    if not success and trial_uid:
        logger.warning("Trial delivery failed for user %d, trial remains used", trial_uid)

    return success


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
    Возвращает True при успехе.
    """
    from constants import validate_device_limit as const_validate_device_limit
    limit_ip = const_validate_device_limit(limit_ip)

    try:
        logger.info(
            "deliver_key: user=%d name='%s' days=%d limit_ip=%d method=%s",
            user_id, config_name, days, limit_ip, method,
        )

        user_result = await create_marzban_user(user_id, days, data_limit_gb=0)
        if not user_result:
            await _notify_key_creation_failed(
                bot, chat_id, user_id, "create_marzban_user returned None",
            )
            return False

        subscription_url = user_result.get("subscription_url", "")
        if not subscription_url:
            links = await get_user_subscription_links(user_id)
            if links:
                subscription_url = links[0]
                logger.info("Recovered subscription URL from panel for user %d", user_id)

        if not subscription_url:
            await _notify_key_creation_failed(
                bot, chat_id, user_id, "empty subscription_url in API response",
            )
            return False

        vless_links = user_result.get("vless_links", [])
        vless_key = vless_links[0] if vless_links else ""

        logger.info("Subscription URL for user %d: %s", user_id, subscription_url[:80])

        db_tasks = [add_key(user_id, subscription_url, config_name, vless_key, days, limit_ip)]
        if is_paid and amount > 0:
            tariff_name = f"{days} дней (до 5 устройств)"
            db_tasks.append(add_payment(
                user_id, amount, currency, method, days, payload,
                status="success", tariff=tariff_name, devices=limit_ip,
            ))
        await asyncio.gather(*db_tasks)

        await _send_key_message(bot, chat_id, subscription_url, vless_key=vless_key)

        if is_paid:
            try:
                from referral_system_new import process_payment_referral_bonus
                await process_payment_referral_bonus(user_id, amount, bot)
            except ImportError:
                logger.warning("Referral system module not available, skipping bonus processing")
            except Exception as e:
                logger.error("Referral bonus error: %s", e)

        logger.info("Subscription delivered: user=%d name='%s' days=%d", user_id, config_name, days)
        return True

    except TelegramForbiddenError as e:
        logger.warning("User %d blocked the bot, cannot deliver subscription: %s", user_id, e)
        return False
    except Exception as e:
        logger.exception("deliver_key FAILED for user=%d name='%s': %s", user_id, config_name, e)

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
                    "payload": payload,
                },
            )
        except Exception as log_error:
            logger.error("Failed to log key error: %s", log_error)

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
            await bot.send_message(chat_id, SUPPORT_ERROR_TEXT, parse_mode="HTML")
        except Exception:
            pass
        return False
