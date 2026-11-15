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
    bind_device_upgrade_invoice, finalize_device_upgrade_order, get_auto_renew_subscription,
    get_device_upgrade_order, get_device_upgrade_order_for_invoice,
    get_key_by_id, get_user_keys,
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
        "Доступ не изменён. Если ранее создавали счёт, не оплачивайте его повторно.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подписка", callback_data="my_keys")],
            [InlineKeyboardButton(text="Проверить доплату", callback_data="device_upgrade_status")],
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
        rows.append([InlineKeyboardButton(
            text=f"Картой или CryptoBot · до {target} устройств",
            callback_data=f"device_upgrade_methods:{key_id}:{target}",
        )])
    rows.append([InlineKeyboardButton(text="← К устройствам", callback_data=f"key_devices:{key_id}")])
    disclaimer = (
        "\n\nАвтопродление сохранится с новой ценой, которую покажем перед оплатой."
        if context["recurring"] else ""
    )
    rate_description = (
        "До 5 устройств — +20 ₽/мес.; до 10 — +40 ₽/мес. к тарифу на 2 устройства."
        if old == 2 else
        "До 10 устройств — дополнительно +20 ₽/мес. к текущему тарифу."
    )
    current_word = "устройства" if old == 2 else "устройств"
    await send_with_photo(
        bot, callback,
        f"<b>Увеличить лимит устройств</b>\n\n"
        f"Сейчас: <b>{old} {current_word}</b>\n"
        f"{rate_description}\n"
        "Доплата рассчитывается за оставшийся оплаченный срок. "
        "На этапе оплаты покажем сумму в выбранной валюте. "
        "Срок VPN не изменится."
        f"{disclaimer}\n\nВыберите новый лимит:",
        InlineKeyboardMarkup(inline_keyboard=rows),
        photo=DEVICES,
    )


@router.callback_query(F.data.startswith("device_upgrade_methods:"))
async def cb_device_upgrade_methods(callback: CallbackQuery, bot: Bot):
    """Alternative providers, without changing the existing Stars checkout."""
    await safe_answer(callback)
    try:
        _, key_str, target_str = callback.data.split(":")
        key_id, target = int(key_str), int(target_str)
        context = await _upgrade_context(callback.from_user.id, key_id)
        quote = upgrade_price_stars(
            context["limit"], target, context["expiry"] - int(time.time()),
        )
    except (ValueError, IndexError) as exc:
        await _show_upgrade_error(bot, callback, str(exc))
        return
    except Exception:
        logger.exception("Cannot quote alternative device upgrade providers")
        await _show_upgrade_error(bot, callback, "Не удалось проверить подписку. Попробуйте позже.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"ЮKassa · {quote} ₽",
            callback_data=f"device_upgrade_provider:yookassa:{key_id}:{target}",
        )],
        [InlineKeyboardButton(
            text=f"CryptoBot · {quote} ₽",
            callback_data=f"device_upgrade_provider:cryptobot:{key_id}:{target}",
        )],
        [InlineKeyboardButton(
            text=f"Telegram Stars · {quote} ⭐",
            callback_data=f"device_upgrade_buy:{key_id}:{target}",
        )],
        [InlineKeyboardButton(text="← Назад", callback_data=f"device_upgrade:{key_id}")],
    ])
    await send_with_photo(
        bot, callback,
        f"<b>До {target} устройств</b>\n\n"
        f"Доплата за остаток срока: <b>{quote} ₽</b> "
        f"или <b>{quote} ⭐</b>. Срок доступа не изменится.\n\n"
        "Выберите способ оплаты:",
        kb, photo=DEVICES,
    )


@router.callback_query(F.data.startswith("device_upgrade_provider:"))
async def cb_device_upgrade_provider(callback: CallbackQuery, bot: Bot):
    """Create a one-time, provider-bound invoice for a verified active key."""
    await safe_answer(callback)
    try:
        _, provider, key_str, target_str = callback.data.split(":")
        if provider not in {"yookassa", "cryptobot"}:
            raise ValueError("Недоступный способ оплаты")
        key_id, target = int(key_str), int(target_str)
        user_id = callback.from_user.id
        context = await _upgrade_context(user_id, key_id)
        quote = upgrade_price_stars(
            context["limit"], target, context["expiry"] - int(time.time()),
        )
        recurring = context["recurring"]
        auto_id = int(recurring["id"]) if recurring else 0
        from database import is_account_tombstoned
        if await is_account_tombstoned(user_id):
            raise ValueError("Аккаунт закрыт")
        order_id = await create_device_upgrade_order(
            user_id, key_id, context["limit"], target, context["expiry"],
            quote, provider=provider, auto_renew_id=auto_id,
            prior_auto_renew_amount=int(recurring["amount_rub"]) if recurring else 0,
            next_auto_renew_amount=(
                get_price_for_months(int(recurring["months"]), target)[0]
                if recurring else 0
            ),
        )
        if not order_id:
            raise ValueError(
                "Предыдущий счёт ещё может быть оплачен. "
                "Не оплачивайте повторно; если нужно, обратитесь в поддержку."
            )
    except (ValueError, IndexError) as exc:
        await _show_upgrade_error(bot, callback, str(exc))
        return
    except Exception:
        logger.exception("Cannot safely create alternative provider upgrade checkout")
        await _show_upgrade_error(bot, callback, "Сейчас нельзя создать новый счёт. Попробуйте позже.")
        return

    payment_url = ""
    invoice_id = ""
    try:
        if provider == "yookassa":
            import hashlib
            from payments import create_yookassa_payment
            result = await create_yookassa_payment(
                quote, f"ByMeVPN · увеличение лимита до {target} устройств",
                user_id, days=0, devices=target, months=1,
                extra_metadata={
                    "purpose": "device_upgrade", "device_upgrade_order_id": order_id,
                    "key_id": key_id, "old_devices": context["limit"],
                },
                idempotency_key=hashlib.sha256(
                    f"device_upgrade:yookassa:{order_id}".encode(),
                ).hexdigest(),
                return_payment_details=True, save_payment_method=False,
            )
            if isinstance(result, dict):
                payment_url = str(result.get("payment_url") or "")
                invoice_id = str(result.get("payment_id") or "")
        else:
            from payments import create_crypto_payment
            from database import add_crypto_pending
            payload = f"devup_{order_id}"
            invoice = await create_crypto_payment(
                quote, f"ByMeVPN · до {target} устройств",
                user_id, days=0, devices=target, external_payload=payload,
            )
            if invoice:
                payment_url, invoice_id = invoice
                # The crypto monitor can only process this paid invoice when its
                # server-owned metadata has been durably persisted.
                await add_crypto_pending(
                    invoice_id, user_id, 0, target, quote, payload=payload,
                )
        if not payment_url.startswith("https://") or not invoice_id:
            raise RuntimeError("provider did not confirm a persisted invoice")
        if not await bind_device_upgrade_invoice(order_id, user_id, provider, invoice_id):
            raise RuntimeError("provider invoice was not durably bound to upgrade")
    except Exception:
        # The provider may have created an invoice even after a client timeout.
        # Keep the pending order reserved to prevent duplicate charges.
        logger.exception("Device upgrade provider checkout requires reconciliation")
        await send_with_photo(
            bot, callback,
            "<b>Не удалось подтвердить счёт</b>\n\n"
            "Счёт мог быть создан, но его сохранение не подтверждено. "
            "Не оплачивайте повторно — поддержка проверит заказ.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Поддержка по оплате", url=support_url("payment"))],
                [InlineKeyboardButton(text="← К подписке", callback_data=f"key_info:{key_id}")],
            ]), photo=DEVICES,
        )
        return
    await send_with_photo(
        bot, callback,
        f"<b>Доплата за устройства</b>\n\n"
        f"Новый лимит: <b>{target}</b>\n"
        f"Сумма: <b>{quote} ₽</b>\n"
        "Срок подписки не изменится. После оплаты дождитесь подтверждения "
        "нового лимита. Не создавайте второй счёт, пока этот проверяется.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Перейти к оплате", url=payment_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data="device_upgrade_status")],
            [InlineKeyboardButton(text="Поддержка по оплате", url=support_url("payment"))],
            [InlineKeyboardButton(text="← К устройствам", callback_data=f"key_devices:{key_id}")],
        ]), photo=DEVICES,
    )


@router.callback_query(F.data == "device_upgrade_status")
async def cb_device_upgrade_status(callback: CallbackQuery, bot: Bot):
    """Explain the last device upgrade state without creating another charge."""
    await safe_answer(callback)
    from database import get_latest_device_upgrade_for_user
    try:
        order = await get_latest_device_upgrade_for_user(callback.from_user.id)
    except Exception:
        logger.exception("Cannot load device upgrade status")
        await _show_upgrade_error(bot, callback, "Проверка счёта сейчас недоступна.")
        return
    if not order:
        text = "<b>Доплата не найдена</b>\n\nУ вас пока нет сохранённого счёта на дополнительные устройства."
    elif order["status"] == "fulfilled":
        text = (
            "<b>Доплата подтверждена</b>\n\n"
            f"Лимит: <b>до {order['new_devices']} устройств</b>. "
            "Оставшийся срок подписки не изменился."
        )
    elif order["status"] == "pending":
        external = order.get("provider")
        provider_status = None
        if order.get("provider_payment_id") and external in {"yookassa", "cryptobot"}:
            try:
                from payments import get_yookassa_payment_status, get_crypto_invoice
                if external == "yookassa":
                    provider_status = await get_yookassa_payment_status(order["provider_payment_id"])
                else:
                    invoice = await get_crypto_invoice(order["provider_payment_id"])
                    provider_status = (invoice or {}).get("status")
            except Exception:
                logger.warning("Provider status unavailable for device upgrade")
        if provider_status in {"succeeded", "paid"}:
            description = (
                "Провайдер подтвердил оплату. Лимит ещё проверяется на сервере. "
                "Не создавайте второй счёт."
            )
        elif provider_status in {"canceled", "expired"}:
            # Only the provider's verified terminal state permits releasing
            # an external invoice reservation for another safe checkout.
            await expire_device_upgrade_order(order["id"])
            description = (
                "Провайдер подтвердил отмену или истечение счёта. "
                "Можно выбрать новый лимит и способ оплаты. "
                "Если деньги всё же списались, сначала обратитесь в поддержку."
            )
        else:
            description = (
                "Подтверждение нового лимита ещё ожидается. "
                "Если уже оплатили, не платите повторно; проверка выполняется автоматически."
            )
        text = (
            "<b>Доплата проверяется</b>\n\n"
            f"Новый лимит: <b>до {order['new_devices']} устройств</b>.\n"
            + description
        )
    else:
        text = (
            "<b>Счёт больше не активен</b>\n\n"
            "Если деньги уже списались, не создавайте новый счёт — "
            "поддержка проверит оплату."
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мои устройства", callback_data="my_keys")],
        [InlineKeyboardButton(text="Повторить проверку", callback_data="device_upgrade_status")],
        [InlineKeyboardButton(text="Поддержка по оплате", url=support_url("payment"))],
    ])
    await send_with_photo(bot, callback, text, keyboard, photo=DEVICES)


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
            quote, provider="stars", auto_renew_id=auto_id,
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
        or order.get("provider", "stars") != "stars"
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


async def process_provider_device_upgrade(
    bot: Bot, provider: str, invoice_id: str, paid_invoice: dict,
) -> bool:
    """Confirm a YooKassa/CryptoBot paid invoice without granting extra days.

    The provider invoice must have been persisted and bound to this exact
    order before the customer saw a payment URL. The account-scoped lease
    serializes this with renewal, deletion and reissue across bot workers.
    """
    import secrets
    from decimal import Decimal, InvalidOperation
    from database import (
        get_crypto_pending, get_yookassa_pending, get_payment_by_id,
        try_acquire_delivery_lease, release_delivery_lease,
        is_account_tombstoned, delete_crypto_pending, delete_yookassa_pending,
        mark_crypto_processed, mark_yookassa_processed,
    )
    if provider not in {"yookassa", "cryptobot"} or not invoice_id:
        return False
    order = await get_device_upgrade_order_for_invoice(provider, str(invoice_id))
    if not order:
        logger.error("Paid device upgrade invoice has no durable binding (%s)", provider)
        return False
    user_id = int(order["user_id"])
    amount = int(order["amount_stars"])  # Owner-approved 1:1 Stars/RUB tariff value.
    charge_id = f"{provider}:{invoice_id}"
    # Provider redelivery is normal after successful fulfillment. The pending
    # provider receipt is intentionally deleted on success. Trust only the
    # completed order *and* the successful durable ledger for idempotent reply.
    if order["status"] == "fulfilled":
        from database import get_db
        db = await get_db()
        receipt = await (await db.execute(
            "SELECT user_id,amount,currency,status FROM payments "
            "WHERE provider=? AND provider_payment_id=?",
            (provider, str(invoice_id)),
        )).fetchone()
        try:
            amount_data = paid_invoice.get("amount") if provider == "yookassa" else None
            paid_value = (
                Decimal(str((amount_data or {}).get("value")))
                if provider == "yookassa" else Decimal(str(paid_invoice.get("amount")))
            )
            provider_confirmed = (
                paid_invoice.get("status") == ("succeeded" if provider == "yookassa" else "paid")
                and str(paid_invoice.get("id" if provider == "yookassa" else "invoice_id") or "") == str(invoice_id)
                and (amount_data or {}).get("currency") == "RUB"
                if provider == "yookassa" else (
                    paid_invoice.get("status") == "paid"
                    and str(paid_invoice.get("invoice_id") or "") == str(invoice_id)
                    and paid_invoice.get("currency_type") == "fiat"
                    and paid_invoice.get("fiat") == "RUB"
                )
            )
        except (InvalidOperation, ValueError, TypeError):
            provider_confirmed = False
            paid_value = Decimal(-1)
        if (
            order.get("charge_id") == charge_id and provider_confirmed
            and paid_value.is_finite() and paid_value == Decimal(amount)
            and receipt and int(receipt[0]) == user_id
            and int(receipt[1]) == amount and receipt[2] == "RUB"
            and receipt[3] == "success"
        ):
            return True
        return False

    try:
        if provider == "yookassa":
            metadata = paid_invoice.get("metadata") or {}
            pending = await get_yookassa_pending(str(invoice_id))
            amount_data = paid_invoice.get("amount") or {}
            actual = Decimal(str(amount_data.get("value")))
            valid = (
                paid_invoice.get("status") == "succeeded"
                and str(paid_invoice.get("id") or "") == str(invoice_id)
                and str(amount_data.get("currency")) == "RUB"
                and str(metadata.get("purpose") or "") == "device_upgrade"
                and str(metadata.get("device_upgrade_order_id") or "") == order["id"]
                and str(metadata.get("user_id") or "") == str(user_id)
                and str(metadata.get("days") or "") == "0"
                and str(metadata.get("devices") or "") == str(order["new_devices"])
                and str(metadata.get("key_id") or "") == str(order["key_id"])
                and str(metadata.get("old_devices") or "") == str(order["old_devices"])
                and str(metadata.get("amount_rub") or "") == str(amount)
                and isinstance(pending, dict)
                and int(pending["user_id"]) == user_id
                and int(pending["days"]) == 0
                and int(pending["devices"]) == int(order["new_devices"])
                and int(pending["amount_rub"]) == amount
                and actual.is_finite() and actual == Decimal(amount)
            )
        else:
            pending = await get_crypto_pending(str(invoice_id))
            actual = Decimal(str(paid_invoice.get("amount")))
            valid = (
                paid_invoice.get("status") == "paid"
                and str(paid_invoice.get("invoice_id") or "") == str(invoice_id)
                and paid_invoice.get("currency_type") == "fiat"
                and paid_invoice.get("fiat") == "RUB"
                and str(paid_invoice.get("payload") or "") == f"devup_{order['id']}"
                and isinstance(pending, dict)
                and str(pending.get("payload") or "") == f"devup_{order['id']}"
                and int(pending["user_id"]) == user_id
                and int(pending["days"]) == 0
                and int(pending["devices"]) == int(order["new_devices"])
                and int(pending["amount_rub"]) == amount
                and actual.is_finite() and actual == Decimal(amount)
            )
    except (InvalidOperation, ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        logger.error("Provider device upgrade mismatches persisted invoice (%s)", provider)
        await _notify_upgrade_admin(
            bot, f"Оплаченный {provider} счёт за устройства {invoice_id} "
            f"не совпал с заказом {order['id']} пользователя {user_id}. Нужна ручная сверка.",
        )
        return False

    token = secrets.token_urlsafe(24)
    if not await try_acquire_delivery_lease(user_id, token, ttl_seconds=300):
        logger.warning("Device upgrade invoice blocked by account lease: user=%s", user_id)
        return False
    ledger_id = None
    try:
        if await is_account_tombstoned(user_id):
            raise RuntimeError("account was deleted before upgrade fulfillment")
        is_new, ledger_id = await record_payment_idempotent(
            user_id=user_id, amount=amount, currency="RUB",
            method="device_upgrade", days=0, payload=str(invoice_id),
            status="processing", tariff="Увеличение лимита устройств",
            devices=int(order["new_devices"]), provider=provider,
            provider_payment_id=str(invoice_id),
        )
        if not ledger_id:
            raise RuntimeError("payment ledger id unavailable")
        previous = await get_payment_by_id(ledger_id)
        if not is_new:
            if previous and previous.get("status") == "success":
                return True
            if not previous or previous.get("status") != "failed":
                # An earlier worker may have updated 3x-ui before crashing.
                # Do not retry automatically without reconciling the panel.
                if previous and previous.get("status") == "processing":
                    await update_payment_status(ledger_id, "recovery_required")
                return False
            await update_payment_status(ledger_id, "processing")
        if order["status"] == "fulfilled":
            if order.get("charge_id") != charge_id:
                raise RuntimeError("order was fulfilled against a different invoice")
            await update_payment_status(ledger_id, "success")
            return True
        if order["status"] != "pending":
            raise RuntimeError("order is not fulfillable")

        key = await get_key_by_id(int(order["key_id"]))
        if (
            not key or int(key.get("user_id") or 0) != user_id
            or int(key.get("expiry") or 0) != int(order["key_expiry"])
            or int(key.get("expiry") or 0) <= int(time.time())
            or int(key.get("limit_ip") or 0) not in (
                int(order["old_devices"]), int(order["new_devices"]),
            )
        ):
            raise RuntimeError("local subscription changed since checkout")

        from xui_client import get_xui_client_details, get_xui_devices, update_xui_user
        panel = await get_xui_client_details(user_id, strict=True)
        if (
            not panel or panel.get("enable") is False
            or abs(int(panel.get("expiry_time") or 0) // 1000 - int(order["key_expiry"])) > 2
            or int(panel.get("limit_hwid") or 0) not in (
                int(order["old_devices"]), int(order["new_devices"]),
            )
        ):
            raise RuntimeError("VPN panel does not match paid upgrade")
        if int(panel["limit_hwid"]) != int(order["new_devices"]):
            res = await update_xui_user(
                user_id, new_expiry_ms=int(order["key_expiry"]) * 1000,
                limit_ip=0, limit_hwid=int(order["new_devices"]),
            )
            if not res:
                raise RuntimeError("VPN panel did not confirm device upgrade")
        for attempt in range(3):
            devices = await get_xui_devices(user_id)
            if isinstance(devices, dict) and int(devices.get("limit_hwid") or 0) == int(order["new_devices"]):
                break
            import asyncio
            await asyncio.sleep(0.3)
        else:
            raise RuntimeError("VPN panel HWID limit was not confirmed")

        if not await finalize_device_upgrade_order(order, charge_id):
            raise RuntimeError("device upgrade database commit not confirmed")
        await update_payment_status(ledger_id, "success")
        try:
            if provider == "yookassa":
                await mark_yookassa_processed(str(invoice_id))
                await delete_yookassa_pending(str(invoice_id))
            else:
                await mark_crypto_processed(str(invoice_id))
                await delete_crypto_pending(str(invoice_id))
        except Exception:
            logger.exception("Device upgrade paid successfully, but receipt cleanup failed")
        try:
            from app_links import build_user_connect_page_url
            url = await build_user_connect_page_url(
                str(key.get("key") or ""), user_id=user_id,
                key_id=int(order["key_id"]),
            )
        except Exception:
            url = ""
        buttons = []
        if url:
            buttons.append([InlineKeyboardButton(text="Подключить VPN", url=url)])
        buttons.append([InlineKeyboardButton(
            text="Мои устройства", callback_data=f"key_devices:{order['key_id']}",
        )])
        try:
            await bot.send_message(
                user_id,
                f"<b>Лимит увеличен до {order['new_devices']} устройств</b>\n\n"
                "Оплата подтверждена, срок подписки не изменился.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        except Exception:
            logger.warning("Paid device upgrade confirmation could not be delivered to user %s", user_id)
        return True
    except Exception:
        logger.exception("Paid %s device upgrade requires reconciliation: user=%s", provider, user_id)
        if ledger_id:
            await update_payment_status(ledger_id, "recovery_required")
        await _notify_upgrade_admin(
            bot, f"Оплаченный {provider} счёт {invoice_id}, пользователь {user_id}, "
            f"заказ {order['id']}: сверить HWID, списание и платежный журнал.",
        )
        try:
            await bot.send_message(
                user_id,
                "Оплата за дополнительные устройства получена, но подтверждение "
                "нового лимита пока не завершено. Не платите повторно. "
                "Поддержка проверит ваш заказ.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Поддержка", url=support_url("payment")),
                ]]),
            )
        except Exception:
            logger.warning("Could not send customer recovery message")
        return False
    finally:
        await release_delivery_lease(user_id, token)


async def _notify_upgrade_admin(bot: Bot, text: str) -> None:
    from config import ADMIN_IDS
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Device upgrade incident notification failed")


async def process_device_upgrade_charge(message, bot: Bot, payment, charge_id: str):
    """Serialize Stars upgrade against renewals and external-provider upgrades."""
    import secrets
    from database import try_acquire_delivery_lease, release_delivery_lease
    user_id = int(message.from_user.id)
    token = secrets.token_urlsafe(24)
    if not await try_acquire_delivery_lease(user_id, token, ttl_seconds=300):
        logger.warning("Stars device upgrade is waiting for an account operation: %s", user_id)
        return
    try:
        await _process_device_upgrade_charge_locked(message, bot, payment, charge_id)
    finally:
        await release_delivery_lease(user_id, token)


async def _process_device_upgrade_charge_locked(message, bot: Bot, payment, charge_id: str):
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
            or order.get("provider", "stars") != "stars"
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

        if not await finalize_device_upgrade_order(order, charge_id):
            raise RuntimeError("device upgrade DB finalize did not confirm success")
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

    try:
        from app_links import build_user_connect_page_url
        key = await get_key_by_id(int(order["key_id"]))
        connect_url = await build_user_connect_page_url(
            str((key or {}).get("key") or ""), user_id=user_id,
            key_id=int(order["key_id"]),
        )
    except Exception:
        logger.warning("Could not prepare direct connection link after Stars device upgrade")
        connect_url = ""
    rows = [
        [InlineKeyboardButton(
            text="Подключить VPN",
            url=connect_url,
        )] if connect_url else [InlineKeyboardButton(
            text="Подключить VPN",
            callback_data=f"key_instructions:{order['key_id']}",
        )],
        [InlineKeyboardButton(
            text="Мои устройства",
            callback_data=f"key_devices:{order['key_id']}",
        )],
    ]
    await bot.send_message(
        user_id,
        f"<b>Лимит увеличен до {order['new_devices']} устройств</b>\n\n"
        "Срок подписки не изменился. Теперь можно подключить новое устройство.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
