"""
Auto-renewal service for ByMeVPN.
Periodically checks for expired/expiring subscriptions with active auto-renew
and executes recurrent debits via YooKassa API.
"""
import asyncio
import logging
import secrets
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

        safe_pm_title = escape(str(pm_title or "сохранённый способ оплаты"))
        if disabled:
            text = (
                "<b>Автопродление остановлено</b>\n\n"
                f"Не удалось списать <b>{amount_rub} ₽</b> через {safe_pm_title} "
                "после повторных попыток.\n\n"
                "Новых автоматических списаний по этому способу оплаты не будет. "
                "При необходимости продлите VPN вручную."
            )
        else:
            text = (
                "<b>Автопродление пока не прошло</b>\n\n"
                f"Планируемая сумма: <b>{amount_rub} ₽</b>\n"
                f"Способ оплаты: {safe_pm_title}\n\n"
                "Проверьте способ оплаты. Мы повторим попытку позже. "
                "Пока доступ активен, повторно платить вручную необязательно."
            )
        from bot_style import themed_button as InlineKeyboardButton
        from aiogram.types import InlineKeyboardMarkup
        buttons = []
        if key_id:
            buttons.append([InlineKeyboardButton(
                text="Оплата и автопродление",
                callback_data=f"manage_payment_methods:{int(key_id)}",
            )])
            buttons.append([InlineKeyboardButton(
                text="Продлить вручную",
                callback_data=f"key_renew:{int(key_id)}",
            )])
        else:
            buttons.append([InlineKeyboardButton(text="Моя подписка", callback_data="my_keys")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Failed to notify user %d about auto-renewal failure: %s", user_id, e)


_CHARGE_SKIPPED = object()


async def _charge_if_still_due(sub: dict, idempotence_key: str, description: str):
    """Guard invoice creation against concurrent account deletion or renewal.

    The same SQLite lease is used by subscription provisioning and account
    erasure. Re-read the persisted billing terms *after* taking the lease;
    never charge a stale snapshot obtained before a customer cancelled.
    The provider invoice is persisted before releasing that lease, so an
    account deletion observes its unsettled checkout.
    """
    from database import (
        try_acquire_delivery_lease, release_delivery_lease, add_yookassa_pending,
    )

    user_id = int(sub["user_id"])
    sub_id = int(sub["sub_id"])
    token = secrets.token_urlsafe(24)
    try:
        acquired = await try_acquire_delivery_lease(user_id, token)
    except Exception:
        logger.exception("Could not guard recurring charge for user %d", user_id)
        return _CHARGE_SKIPPED
    if not acquired:
        logger.info("Recurring charge deferred while account %d is being modified", user_id)
        return _CHARGE_SKIPPED

    try:
        fresh = await get_expiring_auto_renew_subscriptions(now=int(time.time()))
        match = next((candidate for candidate in fresh if candidate["sub_id"] == sub_id), None)
        # Match all economically meaningful terms, key and billing attempt.
        # Any change means this invoice must be reconsidered next worker pass.
        fields = ("user_id", "key_id", "expiry", "payment_method_id",
                  "days", "devices", "months", "amount_rub", "fail_count")
        if not match or any(match.get(field) != sub.get(field) for field in fields):
            logger.info("Skipping obsolete recurring-charge candidate %d", sub_id)
            return _CHARGE_SKIPPED

        payment_res = await charge_yookassa_recurrent(
            amount_rub=sub["amount_rub"],
            description=description,
            user_id=user_id,
            days=sub["days"],
            devices=sub["devices"],
            months=sub["months"],
            key_id=sub["key_id"] or 0,
            payment_method_id=sub["payment_method_id"],
            idempotence_key=idempotence_key,
        )
        if isinstance(payment_res, dict):
            payment_id = payment_res.get("id")
            if (isinstance(payment_id, str) and payment_id.strip()
                    and payment_res.get("status") in ("succeeded", "pending")):
                try:
                    await add_yookassa_pending(
                        payment_id, user_id, sub["days"],
                        sub["devices"], sub["amount_rub"],
                    )
                except Exception:
                    # A charge may already exist. Do not claim success or
                    # expose a provider ID without durable recovery tracking.
                    logger.exception("Recurring invoice %s has no durable pending record", payment_id)
                    await defer_auto_renew_charge_retry(sub_id, "pending ledger write failed")
                    return _CHARGE_SKIPPED
        return payment_res
    finally:
        try:
            await release_delivery_lease(user_id, token)
        except Exception:
            logger.exception("Recurring charge lease release failed for user %d", user_id)


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
        try:
            payment_res = await _charge_if_still_due(sub, idempotence_key, description)
        except Exception:
            logger.exception("Recurring invoice creation failed for subscription %d", sub_id)
            continue
        if payment_res is _CHARGE_SKIPPED:
            continue

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
