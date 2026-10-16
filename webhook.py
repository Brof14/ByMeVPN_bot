"""
Webhook server — runs alongside the Telegram bot as an asyncio task.

Handles:
  1. POST /webhook/telegram - Telegram bot updates
  2. POST /webhook/yookassa - YooKassa payment notifications

YooKassa Security model:
  1. Receive POST /webhook/yookassa
  2. NEVER trust the incoming body alone — always re-fetch the payment from
     YooKassa API using the payment_id from the body (prevents spoofing).
  3. Check payment status == "succeeded" on the verified response.
  4. Idempotency: mark payment_id as processed in DB before delivering key,
     so a duplicate webhook never gives a second key.
  5. Validate devices value from metadata — only 1, 2, 5 are legal.

Requires: fastapi, uvicorn[standard]  (added to requirements.txt)
Config:   WEBHOOK_HOST, WEBHOOK_PORT in .env
"""
import asyncio
import base64
import logging
import traceback
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Request, Response

from config import (
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    WEBHOOK_HOST, WEBHOOK_PORT, ADMIN_ID,
)
from database import init_db, is_yookassa_processed, mark_yookassa_processed
from subscription import deliver_key

logger = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None)  # disable docs in production


@app.get("/healthz")
async def health_check():
    """Container liveness/readiness endpoint; returns 503 if SQLite is unavailable."""
    from database import get_db
    from fastapi import HTTPException

    try:
        db = await get_db()
        await db.execute("SELECT 1")
    except Exception as exc:
        logger.error("Healthcheck failed: database unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}

# Global dispatcher for Telegram webhook handling
dispatcher: Optional[Dispatcher] = None


# ---------------------------------------------------------------------------
# Telegram Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    """
    Receive Telegram bot updates via webhook.
    Forwards updates to aiogram dispatcher for processing.
    """
    if not dispatcher:
        logger.error("Telegram webhook: dispatcher not set")
        return Response(status_code=500)

    try:
        body = await request.json()
        update = Update.model_validate(body)
        bot = app.state.bot  # Get bot from app state
        await dispatcher.feed_update(bot, update)
        return Response(status_code=200)
    except Exception as e:
        logger.error("Telegram webhook error: %s", e)
        return Response(status_code=200)  # Always return 200 to avoid retries


# ---------------------------------------------------------------------------
# YooKassa API helper — verify payment by fetching it directly
# ---------------------------------------------------------------------------

async def _fetch_yookassa_payment(payment_id: str) -> Optional[dict]:
    """
    Fetch payment details from YooKassa API.
    Returns the payment dict on success, None on error.
    CRITICAL: always call this to verify — never trust the webhook body alone.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("YooKassa credentials not configured")
        return None

    auth = base64.b64encode(
        f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Failed to fetch YooKassa payment %s: %s", payment_id, e)
        return None


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request) -> Response:
    """
    Receive YooKassa payment notification.
    Always responds 200 quickly — heavy work runs as background task.
    """
    try:
        body = await request.json()
        logger.info("Webhook received: %s", str(body)[:500])  # Log incoming webhook
    except Exception as e:
        logger.warning("Webhook: invalid JSON body - %s", str(e))
        return Response(status_code=400)

    event = body.get("event", "")
    obj = body.get("object", {})
    payment_id = obj.get("id", "")

    logger.info("Webhook parsed: event='%s', payment_id='%s'", event, payment_id)

    # We only care about succeeded payments
    if event != "payment.succeeded" or not payment_id:
        logger.info("Webhook: skipping - not payment.succeeded or no payment_id")
        return Response(status_code=200)

    # Schedule async processing — respond immediately to YooKassa
    bot: Bot = app.state.bot
    logger.info("Webhook: scheduling payment processing for %s", payment_id)
    asyncio.create_task(_process_payment(bot, payment_id))

    return Response(status_code=200)


async def _process_payment(bot: Bot, payment_id: str) -> None:
    """
    Verify and process a succeeded YooKassa payment.
    Fully idempotent — safe to call multiple times for the same payment_id.
    """
    try:
        # ── Step 1: Re-fetch from YooKassa to verify (never trust webhook body) ──
        payment = await _fetch_yookassa_payment(payment_id)
        if not payment:
            logger.error("Webhook: could not verify payment %s", payment_id)
            return

        if payment.get("status") != "succeeded":
            logger.info("Webhook: payment %s status=%s, skipping", payment_id, payment.get("status"))
            if payment.get("status") == "canceled":
                # Stop polling a checkout that can no longer become paid.
                from database import delete_yookassa_pending
                await delete_yookassa_pending(payment_id)
            return

        # ── Step 2: Read the legacy processed marker, but do not trust it alone ──
        # Older code paths could write this marker before successful delivery.
        # The payment ledger below is the source of truth for fulfillment status.
        already_marked_processed = await is_yookassa_processed(payment_id)
        if already_marked_processed:
            logger.warning(
                "Webhook: payment %s has a processed marker; verifying ledger state",
                payment_id,
            )

        # ── Step 3: Extract metadata ──
        metadata = payment.get("metadata", {})
        logger.info("Webhook: payment_id=%s metadata=%s", payment_id, metadata)
        try:
            user_id = int(metadata["user_id"])
            raw_days = int(metadata["days"])
            devices = int(metadata.get("devices", 1))
        except (KeyError, ValueError, TypeError) as e:
            logger.error("Webhook: bad metadata in payment %s: %s — %s", payment_id, metadata, e)
            await _notify_admin(bot, f"⚠️ YooKassa payment {payment_id}: bad metadata {metadata}")
            return

        from constants import normalize_days
        days = normalize_days(raw_days)
        if user_id <= 0 or days is None:
            logger.error("Webhook: invalid user_id/days for payment %s: user=%s days=%s", payment_id, user_id, raw_days)
            await _notify_admin(bot, f"⚠️ YooKassa payment {payment_id}: invalid user_id/days in metadata")
            return

        # Validate devices with single source of truth
        from constants import validate_device_limit
        devices = validate_device_limit(devices)

        amount_data = payment.get("amount") or {}
        amount_str = amount_data.get("value", "0")
        currency = amount_data.get("currency")
        try:
            amount_decimal = Decimal(str(amount_str))
        except (InvalidOperation, ValueError, TypeError):
            amount_decimal = Decimal("0")

        # A succeeded payment must still match our currency and integer-ruble
        # accounting model. Never truncate a fractional amount or silently
        # reinterpret a foreign-currency payment as RUB.
        if currency != "RUB" or not amount_decimal.is_finite() or amount_decimal <= 0 or amount_decimal != amount_decimal.to_integral_value():
            logger.error(
                "Webhook: invalid amount/currency for payment %s: amount=%r currency=%r",
                payment_id, amount_str, currency,
            )
            await _notify_admin(
                bot,
                f"⚠️ Платёж YooKassa {payment_id}: некорректная сумма/валюта ({amount_str} {currency}); автоматическая выдача остановлена.",
            )
            return
        amount_rub = int(amount_decimal)
        expected_amount = metadata.get("amount_rub")
        if expected_amount is not None:
            try:
                expected_amount_decimal = Decimal(str(expected_amount))
            except (InvalidOperation, ValueError, TypeError):
                expected_amount_decimal = Decimal("0")
            if (
                not expected_amount_decimal.is_finite()
                or expected_amount_decimal <= 0
                or expected_amount_decimal != expected_amount_decimal.to_integral_value()
                or amount_decimal != expected_amount_decimal
            ):
                logger.error(
                    "Webhook: amount mismatch for payment %s: received=%s expected=%s",
                    payment_id, amount_str, expected_amount,
                )
                await _notify_admin(
                    bot,
                    f"⚠️ Платёж YooKassa {payment_id}: сумма {amount_str} ₽ не совпадает с ожидаемой {expected_amount} ₽. Автоматическая выдача остановлена.",
                )
                return

        logger.info(
            "Webhook: processing payment %s — user=%d days=%d devices=%d amount=%d",
            payment_id, user_id, days, devices, amount_rub,
        )

        # Intro trial marker: metadata.trial == "1" → 1 ₽ for 3 days,
        # then 89 ₽/мес recurring. Trial payments must NOT trigger referral
        # rewards and must save renewal terms (89 ₽ / 30 days), not 1 ₽ / 3 days.
        is_trial = metadata.get("trial") == "1"
        if is_trial:
            from constants import INTRO_TRIAL_PRICE_RUB
            if amount_rub != INTRO_TRIAL_PRICE_RUB:
                logger.error(
                    "Webhook: trial payment %s amount=%s does not match intro price",
                    payment_id, amount_str,
                )
                await _notify_admin(
                    bot,
                    f"⚠️ Пробный платёж {payment_id}: сумма {amount_str} ₽ (ожидалось {INTRO_TRIAL_PRICE_RUB} ₽), user {user_id}. Автоматическая выдача остановлена.",
                )
                return

        from database import (
            record_payment_idempotent, update_payment_status,
            use_promo_code, delete_yookassa_pending,
        )

        # Idempotent DB record
        if is_trial:
            tariff_name = "Пробный период 3 дня (2 устр.)"
        else:
            tariff_name = f"Подписка {days} дней ({devices} устр.)"
        is_new, pay_db_id = await record_payment_idempotent(
            user_id=user_id,
            amount=amount_rub,
            currency="RUB",
            method="yookassa",
            days=days,
            payload=payment_id,
            status="processing",
            tariff=tariff_name,
            devices=devices,
            provider="yookassa",
            provider_payment_id=payment_id,
        )

        if is_new and already_marked_processed:
            # A legacy marker exists but there is no ledger row to prove whether
            # access was delivered. Never re-deliver blindly: this is ambiguous.
            await update_payment_status(pay_db_id, "recovery_required")
            logger.error(
                "Webhook: payment %s has a legacy processed marker but no ledger row; manual reconciliation required",
                payment_id,
            )
            await _notify_admin(
                bot,
                f"⚠️ Платёж YooKassa {payment_id}: есть старый processed-маркер, но нет записи в журнале платежей. Автоматическую выдачу остановил — нужна ручная сверка.",
            )
            return

        if not is_new:
            # A unique payment row may exist from an earlier attempt that
            # failed during provisioning. Only a confirmed success is safe to
            # acknowledge as processed. Retry explicit failures; quarantine an
            # interrupted "processing" row rather than silently losing a paid
            # order or blindly extending the subscription twice.
            from database import get_db
            db = await get_db()
            cur = await db.execute("SELECT status FROM payments WHERE id=?", (pay_db_id,))
            status_row = await cur.fetchone()
            existing_status = status_row[0] if status_row else None
            if existing_status == "success":
                logger.info("Webhook: payment %s already fulfilled in DB", payment_id)
                await mark_yookassa_processed(payment_id)
                return
            if existing_status == "failed":
                logger.warning("Webhook: retrying failed provisioning for payment %s", payment_id)
                await update_payment_status(pay_db_id, "processing")
            elif existing_status == "recovery_required":
                logger.warning("Webhook: payment %s awaits manual reconciliation", payment_id)
                return
            else:
                logger.error(
                    "Webhook: payment %s has interrupted status %r; requires reconciliation",
                    payment_id, existing_status,
                )
                await update_payment_status(pay_db_id, "recovery_required")
                await _notify_admin(
                    bot,
                    f"⚠️ Платёж {payment_id} уже записан со статусом {existing_status!r}, но не подтверждена выдача. Автоматически не повторяю, чтобы не продлить подписку дважды; нужна сверка.",
                )
                return

        # Deliver or extend key immediately without blocking on user input
        success = await deliver_key(
            bot=bot,
            user_id=user_id,
            chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}",
            days=days,
            limit_ip=devices,
            is_paid=True,
            amount=amount_rub,
            currency="RUB",
            method="yookassa",
            payload=payment_id,
            extend_existing=True,
            skip_referral_bonus=is_trial,
        )

        if success:
            await update_payment_status(pay_db_id, "success")
            await mark_yookassa_processed(payment_id)
            try:
                await delete_yookassa_pending(payment_id)
            except Exception:
                pass
            promo_code = metadata.get("promo_code")
            if promo_code and not is_trial:
                await use_promo_code(promo_code, user_id)
            logger.info("YooKassa payment %s successfully fulfilled for user %d", payment_id, user_id)

            # Auto-renew handling
            is_autorenew = metadata.get("auto_renew") == "1"
            from database import (
                get_user_keys,
                save_auto_renew_subscription,
                update_auto_renew_charge_success_by_user_key,
            )
            user_keys = await get_user_keys(user_id)
            key_id = user_keys[0]["id"] if user_keys else None

            if is_trial:
                # Intro trial succeeded: mark trial as used, clean pending link
                # and store RENEWAL terms (89 ₽ / 30 days), so the autorenew
                # worker charges the real price after 3 days — not 1 ₽.
                from database import set_trial_used, delete_yookassa_trial_pending
                from constants import (
                    RECURRING_MONTHLY_PRICE, RECURRING_MONTHS, RECURRING_DAYS,
                    INTRO_TRIAL_DEVICES, INTRO_TRIAL_DAYS,
                )
                try:
                    await set_trial_used(user_id)
                    await delete_yookassa_trial_pending(user_id)
                except Exception as e:
                    logger.warning("Trial bookkeeping failed for user %d: %s", user_id, e)

                pm = payment.get("payment_method") or {}
                if pm.get("saved") and pm.get("id"):
                    await save_auto_renew_subscription(
                        user_id=user_id,
                        key_id=key_id,
                        payment_method_id=pm["id"],
                        payment_method_title=pm.get("title", ""),
                        payment_method_type=pm.get("type", "bank_card"),
                        months=RECURRING_MONTHS,
                        days=RECURRING_DAYS,
                        devices=INTRO_TRIAL_DEVICES,
                        amount_rub=RECURRING_MONTHLY_PRICE,
                    )
                    logger.info(
                        "Intro trial completed for user %d: ARS saved (89₽/%dd, pm_id=%s)",
                        user_id, RECURRING_DAYS, pm["id"],
                    )
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🎁 <b>Пробный период активирован!</b>\n\n"
                                f"✅ Сегодня списано <b>1 ₽</b>, доступ — <b>{INTRO_TRIAL_DAYS} дня</b>.\n\n"
                                f"🔁 <b>Дальше автоматически:</b>\n"
                                f"Через {INTRO_TRIAL_DAYS} дня спишется <b>{RECURRING_MONTHLY_PRICE} ₽</b> — "
                                f"и подписка продолжится на 30 дней.\n"
                                f"Затем каждые 30 дней по {RECURRING_MONTHLY_PRICE} ₽, "
                                f"пока автопродление не отключено.\n\n"
                                f"🔓 Отключить автопродление можно в любой момент "
                                f"в меню ключа — доступ сохранится до конца оплаченного периода."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as notify_e:
                        logger.debug("Failed to send trial recurring notice to user %d: %s", user_id, notify_e)
                else:
                    logger.info(
                        "Intro trial for user %d completed without saved payment method "
                        "(recurring unavailable) — manual renewal only", user_id,
                    )
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🎁 <b>Пробный период активирован!</b>\n\n"
                                f"✅ Сегодня списано <b>1 ₽</b>, доступ — <b>{INTRO_TRIAL_DAYS} дня</b>.\n\n"
                                f"Автопродление не подключено: банк не вернул сохранённый способ оплаты.\n"
                                f"Продлить после окончания можно вручную — пришлём напоминание."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as notify_e:
                        logger.debug("Failed to send trial notice to user %d: %s", user_id, notify_e)

            elif is_autorenew:
                await update_auto_renew_charge_success_by_user_key(user_id, key_id, days, amount_rub)
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🔄 <b>Автопродление выполнено успешно!</b>\n\n"
                            f"Сумма: <b>{amount_rub} ₽</b>\n"
                            f"Подписка продлена на: <b>{days} дней</b> ({devices} устр.)\n\n"
                            f"VPN продолжает работать без перебоев!"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as notify_e:
                    logger.debug("Failed to send autorenew notification to user %d: %s", user_id, notify_e)
            else:
                pm = payment.get("payment_method") or {}
                if pm.get("saved") and pm.get("id"):
                    pm_id = pm["id"]
                    pm_title = pm.get("title", "")
                    pm_type = pm.get("type", "bank_card")
                    months = int(metadata.get("months", 1))
                    await save_auto_renew_subscription(
                        user_id=user_id,
                        key_id=key_id,
                        payment_method_id=pm_id,
                        payment_method_title=pm_title,
                        payment_method_type=pm_type,
                        months=months,
                        days=days,
                        devices=devices,
                        amount_rub=amount_rub,
                    )
                    logger.info("Saved auto-renew subscription for user %d (key=%s, pm_id=%s)", user_id, key_id, pm_id)
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔄 <b>Автопродление подключено</b>\n\n"
                                f"По окончании срока подписки ({days} дн.) с вашей карты "
                                f"автоматически спишется {amount_rub} ₽ для продления.\n\n"
                                f"Вы можете в любой момент отключить автопродление в меню ключа."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as notify_e:
                        logger.debug("Failed to send autorenew confirmation to user %d: %s", user_id, notify_e)

            # Referral reward: single canonical path — idempotent (UNIQUE in DB)
            # and trial-safe. One qualifying payment = exactly one reward.
            # The outer guard is deliberate defense-in-depth: the helper also
            # refuses trial payments internally.
            if not is_trial:
                from referral_system_new import award_referral_for_payment
                await award_referral_for_payment(user_id, payment_id, is_trial=False, bot=bot)
        else:
            await update_payment_status(pay_db_id, "failed")
            logger.error("YooKassa payment %s provisioning failed for user %d", payment_id, user_id)
            await _notify_admin(bot, f"⚠️ Не удалось выдать VPN по платежу YooKassa {payment_id} (user {user_id}). Статус помечен как failed.")

    except Exception as e:
        logger.exception("Webhook: unexpected error processing payment %s: %s", payment_id, e)
        try:
            await _notify_admin(bot, f"Webhook error for payment {payment_id}: {str(e)[:300]}")
        except Exception:
            pass


async def _process_crypto_invoice(bot: Bot, invoice: dict) -> None:
    """
    Process a Crypto Bot (@send) invoice reported as paid by CryptoPaymentMonitor.
    Fully idempotent — safe to call multiple times for the same invoice_id.

    Mirrors _process_payment() for YooKassa: verify status, deduplicate,
    and provision the paid subscription immediately without a post-payment
    input step.
    """
    from database import (
        is_crypto_processed, mark_crypto_processed,
        get_crypto_pending, add_crypto_pending,
    )

    invoice_id = invoice.get("invoice_id")
    if invoice_id is None:
        return

    try:
        if invoice.get("status") != "paid":
            return

        already_marked_processed = await is_crypto_processed(invoice_id)

        # Look up the ledger before trusting a legacy marker. Successful ledger
        # records are authoritative; ambiguous/missing records must not be
        # blindly fulfilled because that could extend access twice.
        from database import get_db
        db = await get_db()
        cur = await db.execute(
            "SELECT id, status FROM payments WHERE provider = ? AND provider_payment_id = ?",
            ("cryptobot", str(invoice_id)),
        )
        ledger_row = await cur.fetchone()
        if already_marked_processed and ledger_row and ledger_row[1] == "success":
            logger.info("Crypto monitor: invoice %s already fulfilled in ledger", invoice_id)
            return
        if already_marked_processed and not ledger_row:
            logger.error("Crypto monitor: invoice %s has processed marker but no payment ledger row", invoice_id)
            await _notify_admin(
                bot,
                f"⚠️ Crypto Bot инвойс {invoice_id}: есть старый processed-маркер, но нет записи в журнале платежей. Нужна ручная сверка.",
            )
            return
        if already_marked_processed and ledger_row and ledger_row[1] == "recovery_required":
            logger.warning("Crypto monitor: invoice %s awaits manual reconciliation", invoice_id)
            return

        # Look up what we recorded when the invoice was created
        pending = await get_crypto_pending(invoice_id)
        if not pending:
            logger.error("Crypto monitor: no pending record for invoice %s — cannot deliver", invoice_id)
            await _notify_admin(bot, f"⚠️ Оплаченный Crypto Bot инвойс {invoice_id} без pending-записи")
            return

        user_id = pending["user_id"]
        days = pending["days"]
        devices = pending["devices"]
        amount_rub = pending["amount_rub"]

        logger.info(
            "Crypto monitor: processing invoice %s — user=%d days=%d devices=%d amount=%d",
            invoice_id, user_id, days, devices, amount_rub,
        )

        # Validate devices with single source of truth
        from constants import validate_device_limit
        devices = validate_device_limit(devices)

        from database import (
            record_payment_idempotent, update_payment_status,
            use_promo_code, delete_crypto_pending,
        )

        tariff_name = f"Подписка {days} дней ({devices} устр.)"
        is_new, pay_db_id = await record_payment_idempotent(
            user_id=user_id,
            amount=amount_rub,
            currency="RUB",
            method="cryptobot",
            days=days,
            payload=str(invoice_id),
            status="processing",
            tariff=tariff_name,
            devices=devices,
            provider="cryptobot",
            provider_payment_id=str(invoice_id),
        )

        if is_new and already_marked_processed:
            await update_payment_status(pay_db_id, "recovery_required")
            logger.error(
                "Crypto monitor: invoice %s has a legacy processed marker but no ledger row; manual reconciliation required",
                invoice_id,
            )
            await _notify_admin(
                bot,
                f"⚠️ Crypto Bot инвойс {invoice_id}: старый processed-маркер без записи в журнале платежей. Автоматическую выдачу остановил — нужна ручная сверка.",
            )
            return

        if not is_new:
            from database import get_db
            db = await get_db()
            cur = await db.execute("SELECT status FROM payments WHERE id=?", (pay_db_id,))
            status_row = await cur.fetchone()
            existing_status = status_row[0] if status_row else None
            if existing_status == "success":
                logger.info("Crypto monitor: invoice %s already fulfilled in DB", invoice_id)
                await mark_crypto_processed(invoice_id)
                return
            if existing_status == "failed":
                logger.warning("Crypto monitor: retrying failed provisioning for invoice %s", invoice_id)
                await update_payment_status(pay_db_id, "processing")
            elif existing_status == "recovery_required":
                logger.warning("Crypto monitor: invoice %s awaits manual reconciliation", invoice_id)
                return
            else:
                logger.error("Crypto monitor: invoice %s has interrupted status %r; requires reconciliation", invoice_id, existing_status)
                await update_payment_status(pay_db_id, "recovery_required")
                await _notify_admin(
                    bot,
                    f"⚠️ Crypto Bot инвойс {invoice_id} уже записан со статусом {existing_status!r}, но выдача не подтверждена. Нужна сверка.",
                )
                return

        success = await deliver_key(
            bot=bot,
            user_id=user_id,
            chat_id=user_id,
            config_name=f"ByMeVPN_{user_id}",
            days=days,
            limit_ip=devices,
            is_paid=True,
            amount=amount_rub,
            currency="RUB",
            method="cryptobot",
            payload=str(invoice_id),
            extend_existing=True,
        )

        if success:
            await update_payment_status(pay_db_id, "success")
            await mark_crypto_processed(invoice_id)
            try:
                await delete_crypto_pending(invoice_id)
            except Exception:
                pass
            promo_code = pending.get("promo_code")
            if promo_code:
                await use_promo_code(promo_code, user_id)
            logger.info("CryptoBot invoice %s successfully fulfilled for user %d", invoice_id, user_id)

            # Referral reward: single canonical path (mirrors YooKassa's rule).
            from referral_system_new import award_referral_for_payment
            await award_referral_for_payment(user_id, str(invoice_id), bot=bot)
        else:
            await update_payment_status(pay_db_id, "failed")
            logger.error("CryptoBot invoice %s provisioning failed for user %d", invoice_id, user_id)
            await _notify_admin(bot, f"⚠️ Не удалось выдать VPN по CryptoBot инвойсу {invoice_id} (user {user_id}). Статус помечен как failed.")

    except Exception as e:
        logger.exception("Crypto monitor: unexpected error processing invoice %s: %s", invoice_id, e)
        try:
            await _notify_admin(bot, f"Crypto Bot processing error for invoice {invoice_id}: {str(e)[:300]}")
        except Exception:
            pass


async def _notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.error("Could not notify admin: %s", e)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

async def start_webhook_server(bot: Bot, dp: Dispatcher) -> None:
    """
    Start the uvicorn server as an asyncio task.

    NOTE: this only serves /webhook/yookassa (payment notifications).
    Telegram updates are received via long polling (see main.py,
    dp.start_polling) — we must NOT also call bot.set_webhook() for
    Telegram, since a bot cannot use both polling and a webhook at the
    same time (Telegram returns 409 Conflict on getUpdates once a webhook
    is set). The previous version of this function called bot.set_webhook()
    unconditionally, which silently broke polling for anyone running the
    bot the way main.py actually starts it.
    """
    global dispatcher
    dispatcher = dp
    app.state.bot = bot
    logger.info("Webhook module ready: dispatcher and bot registered for YooKassa processing")

    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Webhook server starting on %s:%d", WEBHOOK_HOST, WEBHOOK_PORT)
    await server.serve()
