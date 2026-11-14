"""Self-service, paid HWID upgrades without renewing or truncating the key.

Quotes use only the incremental monthly device fee, prorated across the
remaining active term. Telegram Stars payment is fulfilled only after a
verified Xray panel update; ambiguous outcomes require reconciliation.
"""
import logging
import math
import time
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery

from bot_style import themed_button as InlineKeyboardButton
from constants import DEVICE_PRICE_EXTRA, get_price_for_months
from database import (
    create_device_upgrade_order, expire_device_upgrade_order,
    finalize_device_upgrade_order, get_auto_renew_subscription,
    get_device_upgrade_order, get_key_by_id, get_user_keys,
    record_payment_idempotent, update_payment_status,
)
from keyboards import support_url
from utils import safe_answer, send_with_photo
from bot_assets import DEVICES

logger = logging.getLogger(__name__)
router = Router()

DAY = 86400
MONTH = 30 * DAY
INVOICE_LIFETIME = 900


def upgrade_price_stars(old_devices: int, new_devices: int, remaining_seconds: int) -> int:
    """Priced with existing device surcharge tiers; round only the final amount."""
    if old_devices not in (2, 5) or new_devices not in (5, 10) or new_devices <= old_devices:
        raise ValueError("unsupported device upgrade")
    if remaining_seconds <= 0:
        raise ValueError("subscription has expired")
    delta = DEVICE_PRICE_EXTRA[new_devices] - DEVICE_PRICE_EXTRA[old_devices]
    return max(1, math.ceil(delta * remaining_seconds / MONTH))


async def _upgrade_context(user_id: int, key_id: int) -> dict:
    key = await get_key_by_id(key_id)
    if not key or int(key.get("user_id") or 0) != user_id:
        raise ValueError("Подписка не найдена")
    now = int(time.time())
    expiry = int(key.get("expiry") or 0)
    if expiry <= now:
        raise ValueError("Срок подписки закончился")
    keys = await get_user_keys(user_id)
    if len([item for item in keys if int(item.get("expiry") or 0) > now]) != 1:
        raise ValueError("Для изменения лимита выберите единственную активную подписку")
    from xui_client import get_xui_client_details
    panel = await get_xui_client_details(user_id, strict=True)
    if (
        not panel or panel.get("enable") is False
        or abs(int(panel.get("expiry_time") or 0) // 1000 - expiry) > 2
    ):
        raise RuntimeError("cannot verify matching local and panel expiration")
    old_limit = int(panel.get("limit_hwid") or 0)
    if old_limit not in (2, 5, 10) or int(key.get("limit_ip") or 0) != old_limit:
        raise RuntimeError("cannot verify matching local and panel device limits")
    recurring = await get_auto_renew_subscription(user_id, key_id, include_legacy=True)
    if recurring and recurring.get("status") == "active" and recurring.get("payment_method_id"):
        if int(recurring.get("key_id") or 0) != key_id:
            raise RuntimeError("cannot unambiguously update future subscription billing")
        if int(recurring.get("months") or 0) not in (1, 3, 6, 12):
            raise RuntimeError("invalid recurring plan term")
    else:
        recurring = None
    return {"key": key, "expiry": expiry, "limit": old_limit, "recurring": recurring}


async def _show_upgrade_error(bot: Bot, callback: CallbackQuery, message: str):
    await send_with_photo(
        bot, callback,
        f"<b>Увеличение лимита недоступно</b>\n\n{message}\n"
        "Доступ и платежи не изменены.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подписка", callback_data="my_keys")],
            [InlineKeyboardButton(text="Поддержка", url=support_url())],
        ]),
        photo=DEVICES,
    )


@router.callback_query(F.data.startswith("device_upgrade:"))
async def cb_upgrade_device_screen(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    try:
        key_id = int(callback.data.split(":")[1])
        context = await _upgrade_context(callback.from_user.id, key_id)
        remaining = context["expiry"] - int(time.time())
        old = context["limit"]
        if old >= 10:
            raise ValueError("У этой подписки уже максимальный лимит — 10 устройств")
    except (IndexError, ValueError) as exc:
        await _show_upgrade_error(bot, callback, str(exc))
        return
    except Exception:
        logger.exception("Cannot quote device upgrade for %s", callback.from_user.id)
        await _show_upgrade_error(bot, callback, "Не удалось подтвердить лимит на сервере. Попробуйте позже.")
        return

    rows = []
    for target in (5, 10):
        if target <= old:
            continue
        quote = upgrade_price_stars(old, target, remaining)
        rows.append([InlineKeyboardButton(
            text=f"До {target} устройств · {quote} ⭐",
            callback_data=f"device_upgrade_buy:{key_id}:{target}",
        )])
    rows.append([InlineKeyboardButton(text="← К устройствам", callback_data=f"key_devices:{key_id}")])
    disclaimer = (
        "\n\nАвтопродление сохранится с новой ценой, которую покажем перед оплатой."
        if context["recurring"] else ""
    )
    await send_with_photo(
        bot, callback,
        f"<b>Увеличить лимит устройств</b>\n\n"
        f"Сейчас: <b>{old} устройств</b>\n"
        f"Доплата по тарифу: +20 ₽/мес. до 5 или +40 ₽/мес. до 10 "
        "(относительно 2 устройств).\n"
        "Доплата рассчитывается за оставшийся оплаченный срок. "
        "На этапе оплаты сумма будет указана в Telegram Stars. "
        "Срок VPN не изменится."
        f"{disclaimer}\n\nВыберите новый лимит:",
        InlineKeyboardMarkup(inline_keyboard=rows),
        photo=DEVICES,
    )


@router.callback_query(F.data.startswith("device_upgrade_buy:"))
async def cb_upgrade_device_buy(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    try:
        _, key_str, target_str = callback.data.split(":")
        key_id, target = int(key_str), int(target_str)
        context = await _upgrade_context(callback.from_user.id, key_id)
        old = context["limit"]
        quote = upgrade_price_stars(old, target, context["expiry"] - int(time.time()))
        recurring = context["recurring"]
        auto_id = int(recurring["id"]) if recurring else 0
        old_auto_amount = int(recurring["amount_rub"]) if recurring else 0
        next_auto_amount = (
            get_price_for_months(int(recurring["months"]), target)[0]
            if recurring else 0
        )
        from database import is_account_tombstoned
        if await is_account_tombstoned(callback.from_user.id):
            raise ValueError("Аккаунт закрыт")
        order_id = await create_device_upgrade_order(
            callback.from_user.id, key_id, old, target, context["expiry"],
            quote, auto_renew_id=auto_id,
            prior_auto_renew_amount=old_auto_amount,
            next_auto_renew_amount=next_auto_amount,
        )
        if not order_id:
            await _show_upgrade_error(
                bot, callback,
                "Предыдущий счёт ещё ожидает оплаты. Не оплачивайте повторно. "
                "Если он не нужен, повторите оформление через 15 минут.",
            )
            return
    except (ValueError, IndexError) as exc:
        await _show_upgrade_error(bot, callback, str(exc))
        return
    except Exception:
        logger.exception("Device upgrade invoice could not be created")
        await _show_upgrade_error(bot, callback, "Не удалось подтвердить цену. Попробуйте позже.")
        return

    next_charge = (
        f" Следующее автопродление — {next_auto_amount} ₽ за "
        f"{int(recurring['days'])} дней; новая сумма будет сохранена после оплаты."
        if recurring else ""
    )
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"ByMeVPN · до {target} устройств",
            description=(
                f"Доплата {quote} ⭐ за оставшийся срок. "
                "Срок подписки не изменится."
                + next_charge
            )[:255],
            payload=f"devup_{order_id}",
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Увеличение лимита устройств", amount=quote)],
        )
    except Exception:
        logger.exception("Device upgrade invoice delivery failed")
        await expire_device_upgrade_order(order_id)
        await _show_upgrade_error(bot, callback, "Не удалось открыть счёт. Попробуйте позже.")


async def device_upgrade_checkout_error(pre: PreCheckoutQuery) -> str | None:
    payload = str(pre.invoice_payload or "")
    if not payload.startswith("devup_"):
        return "Некорректный счёт"
    order = await get_device_upgrade_order(payload[len("devup_"):])
    now = int(time.time())
    if (
        not order or order["status"] != "pending"
        or now - int(order["created_at"]) > INVOICE_LIFETIME
        or int(order["user_id"]) != pre.from_user.id
        or pre.currency != "XTR"
        or int(pre.total_amount) != int(order["amount_stars"])
    ):
        return "Счёт устарел. Откройте подписку и выберите лимит ещё раз."
    key = await get_key_by_id(int(order["key_id"]))
    if (
        not key or int(key.get("user_id") or 0) != pre.from_user.id
        or int(key.get("expiry") or 0) != int(order["key_expiry"])
        or int(key.get("limit_ip") or 0) != int(order["old_devices"])
        or int(key.get("expiry") or 0) <= now
    ):
        return "Условия подписки изменились. Выберите лимит устройств ещё раз."
    if int(order["auto_renew_id"]):
        current = await get_auto_renew_subscription(
            pre.from_user.id, int(order["key_id"]),
        )
        if (
            not current or current["status"] != "active"
            or int(current["id"]) != int(order["auto_renew_id"])
            or int(current["devices"]) != int(order["old_devices"])
            or int(current["amount_rub"]) != int(order["prior_auto_renew_amount"])
        ):
            return "Условия автопродления изменились. Откройте заказ заново."
    return None


async def process_device_upgrade_charge(message, bot: Bot, payment, charge_id: str):
    """Use authoritative server state; never extend duration for an upgrade."""
    user_id = message.from_user.id
    from database import get_payment_by_id
    payload = str(payment.invoice_payload or "")
    order = await get_device_upgrade_order(payload[len("devup_"):])
    is_new, ledger_id = await record_payment_idempotent(
        user_id=user_id,
        amount=int(payment.total_amount),
        currency=str(payment.currency),
        method="device_upgrade",
        days=0,
        payload=charge_id,
        status="processing",
        tariff="Увеличение лимита устройств",
        devices=int(order["new_devices"]) if order else 2,
        provider="stars",
        provider_payment_id=charge_id,
    )
    existing_payment = await get_payment_by_id(ledger_id) if ledger_id else None
    if not is_new and existing_payment and existing_payment.get("status") == "success":
        return
    try:
        if (
            not order or order["user_id"] != user_id
            or payment.currency != "XTR"
            or int(payment.total_amount) != int(order["amount_stars"])
            or order["status"] not in ("pending", "fulfilled")
        ):
            raise RuntimeError("device upgrade paid order does not match")
        if order["status"] == "fulfilled" and order.get("charge_id") == charge_id:
            await update_payment_status(ledger_id, "success")
            return
        if order["status"] == "fulfilled":
            raise RuntimeError("device upgrade already fulfilled for another charge")

        key = await get_key_by_id(int(order["key_id"]))
        if (
            not key or int(key.get("user_id") or 0) != user_id
            or int(key.get("expiry") or 0) != int(order["key_expiry"])
            or int(key.get("expiry") or 0) <= int(time.time())
            or int(key.get("limit_ip") or 0) not in (
                int(order["old_devices"]), int(order["new_devices"]),
            )
        ):
            raise RuntimeError("subscription changed since checkout")

        from xui_client import get_xui_client_details, get_xui_devices, update_xui_user
        panel = await get_xui_client_details(user_id, strict=True)
        if (
            not panel or panel.get("enable") is False
            or abs(int(panel.get("expiry_time") or 0) // 1000 - int(order["key_expiry"])) > 2
            or int(panel.get("limit_hwid") or 0) not in (
                int(order["old_devices"]), int(order["new_devices"]),
            )
        ):
            raise RuntimeError("VPN panel does not match upgrade order")
        if int(panel["limit_hwid"]) != int(order["new_devices"]):
            result = await update_xui_user(
                user_id, new_expiry_ms=int(order["key_expiry"]) * 1000,
                limit_ip=0, limit_hwid=int(order["new_devices"]),
            )
            if not result:
                raise RuntimeError("VPN panel did not confirm device upgrade")
        for _ in range(3):
            devices = await get_xui_devices(user_id)
            if int(devices.get("limit_hwid") or 0) == int(order["new_devices"]):
                break
            import asyncio
            await asyncio.sleep(0.3)
        else:
            raise RuntimeError("VPN panel HWID limit not confirmed")

        await finalize_device_upgrade_order(order, charge_id)
        await update_payment_status(ledger_id, "success")
    except Exception:
        logger.exception("Paid device upgrade needs reconciliation: %s", charge_id)
        if ledger_id:
            await update_payment_status(ledger_id, "recovery_required")
        from config import ADMIN_IDS
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"Платёж Stars за устройства нуждается в сверке. "
                    f"User <code>{user_id}</code>, charge <code>{charge_id}</code>, "
                    f"order <code>{order['id'] if order else 'unknown'}</code>.",
                )
            except Exception:
                logger.exception("Could not notify admin about device upgrade")
        await bot.send_message(
            user_id,
            "Оплата за устройства получена, но проверка нового лимита не завершена. "
            "Не платите повторно — поддержка проверит состояние подписки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Поддержка", url=support_url()),
            ]]),
        )
        return

    await bot.send_message(
        user_id,
        f"<b>Лимит увеличен до {order['new_devices']} устройств</b>\n\n"
        "Срок подписки не изменился. Теперь можно подключить новое устройство.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Подключить VPN",
                callback_data=f"key_instructions:{order['key_id']}",
            )],
            [InlineKeyboardButton(
                text="Мои устройства",
                callback_data=f"key_devices:{order['key_id']}",
            )],
        ]),
    )
