"""
Auto-renewal service for ByMeVPN.
Periodically checks for expired/expiring subscriptions with active auto-renew
and executes recurrent debits via YooKassa API.
"""
import asyncio
import logging
import time
from typing import Optional

from aiogram import Bot
from database import (
    get_expiring_auto_renew_subscriptions,
    update_auto_renew_charge_success,
    update_auto_renew_charge_failure,
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

        if disabled:
            text = (
                f"❌ <b>Автопродление ByMeVPN отключено</b>\n\n"
                f"Не удалось списать <b>{amount_rub} ₽</b> с карты {pm_title or ''} "
                f"после нескольких попыток"
                f"{f' ({reason})' if reason else ''}.\n\n"
                f"Ваша подписка истекла. Пожалуйста, продлите доступ вручную, "
                f"чтобы продолжить пользоваться VPN."
            )
        else:
            text = (
                f"⚠️ <b>Не удалось списать оплату за автопродление VPN</b>\n\n"
                f"Сумма к списанию: <b>{amount_rub} ₽</b>\n"
                f"Карта: {pm_title or 'Банковская карта'}\n\n"
                f"Пожалуйста, проверьте баланс карты. Мы повторим попытку позже.\n"
                f"Вы также можете продлить подписку вручную."
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
        pm_title = sub.get("payment_method_title") or "Банковская карта"

        logger.info(
            "Executing auto-renewal for user %d (sub_id=%d, key_id=%d, days=%d, devices=%d, amount=%d)",
            user_id, sub_id, key_id, days, devices, amount_rub,
        )

        # DETERMINISTIC business idempotence key: subscription + the billing
        # period being renewed + attempt number. If the worker restarts (or a
        # duplicate run happens) before the charge result is recorded, YooKassa
        # returns the SAME payment for the same key — no double charge.
        idempotence_key = f"autorenew_{sub_id}_{key_id or 0}_{sub['expiry']}_{sub['fail_count']}"

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

        if not payment_res:
            disabled = await update_auto_renew_charge_failure(sub_id, "API request failed")
            await _notify_autorenew_failed(bot, user_id, amount_rub, pm_title, key_id=key_id, disabled=disabled)
            continue

        p_status = payment_res.get("status")
        payment_id = payment_res.get("id")

        if p_status == "succeeded":
            logger.info("Auto-renew payment %s succeeded immediately for user %d", payment_id, user_id)
            await update_auto_renew_charge_success(sub_id, new_days=days, new_amount=amount_rub)
            # Fulfill key extension and analytics through standard webhook logic
            from webhook import _process_payment
            await _process_payment(bot, payment_id)

        elif p_status == "pending":
            logger.info(
                "Auto-renew payment %s is pending for user %d; awaiting webhook/monitor",
                payment_id, user_id,
            )
            # Leave to webhook or payment_monitor to finalize

        else:
            c_details = payment_res.get("cancellation_details", {})
            c_reason = c_details.get("reason", "unknown")
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
