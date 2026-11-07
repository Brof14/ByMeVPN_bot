"""
Auto-renewal service for ByMeVPN.
Periodically checks for expired/expiring subscriptions with active auto-renew
and executes recurrent debits via YooKassa API.
"""
import asyncio
import logging
from html import escape
import time
from typing import Optional

from aiogram import Bot
from database import (
    get_expiring_auto_renew_subscriptions,
    update_auto_renew_charge_failure,
    defer_auto_renew_charge_retry,
)
from payments import charge_yookassa_recurrent

logger = logging.getLogger(__name__)


async def _notify_autorenew_failed(
    bot: Bot,
    user_id: int,
    amount_rub: int,
    pm_title: str,
    key_id: Optional[int] = None,
    disabled: bool = False,
    reason: str = "",
) -> None:
    """Send notification to user when auto-renewal charge fails."""
    try:
        from keyboards import my_keys_list_kb
        from database import get_user_keys

        safe_pm_title = escape(str(pm_title or "Сохранённый способ оплаты"))
        safe_reason = escape(str(reason or ""))

        if disabled:
            text = (
                f"<b>Автопродление отключено</b>\n\n"
                f"Не удалось списать <b>{amount_rub} ₽</b> через {safe_pm_title} "
                f"после нескольких попыток"
                f"{f' ({safe_reason})' if safe_reason else ''}.\n\n"
                f"Ваша подписка истекла. Пожалуйста, продлите доступ вручную, "
                f"чтобы продолжить пользоваться VPN."
            )
        else:
            text = (
                f"<b>Не удалось продлить подписку</b>\n\n"
                f"Сумма к списанию: <b>{amount_rub} ₽</b>\n"
                f"Способ оплаты: {pm_title or 'Сохранённый способ оплаты'}\n\n"
                f"Проверьте способ оплаты. Мы повторим попытку позже.\n"
                f"Подписку также можно продлить вручную."
            )

        keys = await get_user_keys(user_id)
        kb = my_keys_list_kb(keys) if keys else None
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Failed to notify user %d about auto-renewal failure: %s", user_id, e)


async def process_auto_renewals(bot: Bot) -> None:
    """Check and process all subscriptions due for auto-renewal."""
    now = int(time.time())
    try:
        expiring = await get_expiring_auto_renew_subscriptions(now=now)
    except Exception as e:
        logger.error("Error querying expiring auto-renew subscriptions: %s", e)
        return

    if not expiring:
        return

    logger.info("Found %d subscription(s) eligible for auto-renewal", len(expiring))

    for sub in expiring:
        sub_id = sub["sub_id"]
        user_id = sub["user_id"]
        key_id = sub["key_id"] or 0
        days = sub["days"]
        devices = sub["devices"]
        months = sub["months"]
        amount_rub = sub["amount_rub"]
        payment_method_id = sub["payment_method_id"]
        pm_title = sub.get("payment_method_title") or {
            "sbp": "Счёт СБП",
            "bank_card": "Банковская карта",
        }.get((sub.get("payment_method_type") or "").lower(), "Сохранённый способ оплаты")

        logger.info(
            "Executing auto-renewal for user %d (sub_id=%d, key_id=%d, days=%d, devices=%d, amount=%d)",
            user_id, sub_id, key_id, days, devices, amount_rub,
        )

        # One stable key per billing period. In particular, uncertain network
        # failures must retry this same request and never start a second charge.
        # A confirmed cancellation is a completed attempt, so the next retry
        # needs a fresh idempotency key. Uncertain network outcomes do not
        # increment fail_count and therefore keep the same key safely.
        attempt = sub.get("fail_count", 0)
        idempotence_key = f"autorenew_{sub_id}_{key_id or 0}_{sub['expiry']}_{attempt}"

        description = f"Автопродление ByMeVPN ({devices} устр., {days} дн.)"
        payment_res = await charge_yookassa_recurrent(
            amount_rub=amount_rub,
            description=description,
            user_id=user_id,
            days=days,
            devices=devices,
            months=months,
            key_id=key_id,
            payment_method_id=payment_method_id,
            idempotence_key=idempotence_key,
        )

        if not isinstance(payment_res, dict) or not payment_res:
            await defer_auto_renew_charge_retry(sub_id, "provider response was not confirmed")
            logger.warning(
                "Auto-renew payment result is uncertain for subscription %d; retrying with same idempotency key",
                sub_id,
            )
            continue

        p_status = payment_res.get("status")
        payment_id = payment_res.get("id")

        if not isinstance(payment_id, str) or not payment_id.strip() or p_status not in ("succeeded", "pending", "canceled"):
            await defer_auto_renew_charge_retry(sub_id, "provider response was not confirmed")
            logger.warning(
                "Auto-renew response is incomplete or has an unknown status for subscription %d; keeping the same idempotency key",
                sub_id,
            )
            continue

        if payment_id and p_status in ("succeeded", "pending"):
            try:
                from database import add_yookassa_pending
                await add_yookassa_pending(payment_id, user_id, days, devices, amount_rub)
            except Exception:
                # The payment already exists. Keep the worker alive and leave
                # a clear log entry if durable recovery tracking failed.
                logger.exception("Could not persist auto-renew payment %s for recovery", payment_id)

        if p_status == "succeeded":
            logger.info("Auto-renew payment %s succeeded immediately for user %d", payment_id, user_id)
            # Fulfillment in _process_payment is the source of truth for the
            # successful renewal marker. If provisioning fails, keep this
            # subscription eligible and let the same deterministic YooKassa
            # idempotency key return the existing charge for a safe retry.
            from webhook import _process_payment
            await _process_payment(bot, payment_id)

        elif p_status == "pending":
            logger.info(
                "Auto-renew payment %s is pending for user %d; awaiting webhook/monitor",
                payment_id, user_id,
            )
            # Leave to webhook or payment_monitor to finalize

        else:
            c_details = payment_res.get("cancellation_details")
            c_details = c_details if isinstance(c_details, dict) else {}
            c_reason = str(c_details.get("reason") or "unknown")
            logger.warning(
                "Auto-renew payment %s canceled for user %d: %s",
                payment_id, user_id, c_reason,
            )
            disabled = await update_auto_renew_charge_failure(sub_id, c_reason)
            await _notify_autorenew_failed(
                bot, user_id, amount_rub, pm_title, key_id=key_id, disabled=disabled, reason=c_reason,
            )


async def start_autorenew_worker(bot: Bot, interval_seconds: int = 60) -> None:
    """Background worker loop for periodic auto-renewal checks."""
    logger.info("Auto-renew worker started (interval=%ds)", interval_seconds)
    while True:
        try:
            await process_auto_renewals(bot)
        except Exception as e:
            logger.error("Unexpected error in auto-renew worker: %s", e, exc_info=True)
        await asyncio.sleep(interval_seconds)
