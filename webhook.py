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
from database import init_db, is_yookassa_processed, mark_yookassa_processed, add_referral_earning, get_referrer
from subscription import deliver_key

logger = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None)  # disable docs in production

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
            return

        # ── Step 2: Idempotency check ──
        already_processed = await is_yookassa_processed(payment_id)
        if already_processed:
            logger.info("Webhook: payment %s already processed, skipping", payment_id)
            return

        # ── Step 3: Extract metadata ──
        metadata = payment.get("metadata", {})
        logger.info("Webhook: payment_id=%s metadata=%s", payment_id, metadata)
        try:
            user_id = int(metadata["user_id"])
            days = int(metadata["days"])
            devices = int(metadata.get("devices", 1))
        except (KeyError, ValueError, TypeError) as e:
            logger.error("Webhook: bad metadata in payment %s: %s — %s", payment_id, metadata, e)
            await _notify_admin(bot, f"⚠️ YooKassa payment {payment_id}: bad metadata {metadata}")
            return

        # Validate devices with single source of truth
        from constants import validate_device_limit
        devices = validate_device_limit(devices)

        amount_str = payment.get("amount", {}).get("value", "0")
        try:
            amount_rub = int(float(amount_str))
        except ValueError:
            amount_rub = 0

        logger.info(
            "Webhook: processing payment %s — user=%d days=%d devices=%d amount=%d",
            payment_id, user_id, days, devices, amount_rub,
        )

        from database import (
            record_payment_idempotent, update_payment_status,
            use_promo_code, delete_yookassa_pending,
        )

        # Idempotent DB record
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

        if not is_new:
            logger.info("Webhook: payment %s already processed in DB, skipping duplicate", payment_id)
            await mark_yookassa_processed(payment_id)
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
        )

        if success:
            await update_payment_status(pay_db_id, "success")
            await mark_yookassa_processed(payment_id)
            try:
                await delete_yookassa_pending(payment_id)
            except Exception:
                pass
            promo_code = metadata.get("promo_code")
            if promo_code:
                await use_promo_code(promo_code, user_id)
            logger.info("YooKassa payment %s successfully fulfilled for user %d", payment_id, user_id)

            # Начисляем бонус рефералу за первую оплату (50₽)
            try:
                referrer_id = await get_referrer(user_id)
                if referrer_id:
                    bonus_added = await add_referral_earning(referrer_id, user_id, 50, payment_id)
                    if bonus_added:
                        logger.info("Referral bonus 50₽ added for referrer %d from user %d YooKassa payment", referrer_id, user_id)
                        try:
                            await bot.send_message(
                                referrer_id,
                                f"🎉 <b>Поздравляем!</b>\n\n"
                                f"Ваш приглашённый оформил платную подписку.\n"
                                f"Начислено: +50 ₽\n"
                                f"Текущий баланс обновлён в партнёрской программе.",
                                parse_mode="HTML"
                            )
                        except Exception as notify_error:
                            logger.error("Failed to notify referrer %d: %s", referrer_id, notify_error)
            except Exception as e:
                logger.error("Error processing referral bonus for YooKassa user %d: %s", user_id, e)
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

    Mirrors _process_payment() above for YooKassa: verify status, dedupe,
    store pending delivery keyed by invoice_id, prompt the user for a
    config name, and put them into the same waiting_for_config_name FSM
    state that handle_yookassa_config_name() (handlers/buy.py) already
    handles for both payment methods.
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

        if await is_crypto_processed(invoice_id):
            logger.info("Crypto monitor: invoice %s already processed, skipping", invoice_id)
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

        if not is_new:
            logger.info("Crypto monitor: invoice %s already processed in DB, skipping duplicate", invoice_id)
            await mark_crypto_processed(invoice_id)
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

            # Referral bonus (mirrors YooKassa's 50₽ first-payment bonus)
            try:
                referrer_id = await get_referrer(user_id)
                if referrer_id:
                    bonus_added = await add_referral_earning(referrer_id, user_id, 50, str(invoice_id))
                    if bonus_added:
                        logger.info("Referral bonus 50₽ added for referrer %d from user %d Crypto Bot payment", referrer_id, user_id)
                        try:
                            await bot.send_message(
                                referrer_id,
                                "🎉 <b>Поздравляем!</b>\n\n"
                                "Ваш приглашённый оформил платную подписку.\n"
                                "Начислено: +50 ₽\n"
                                "Текущий баланс обновлён в партнёрской программе.",
                                parse_mode="HTML",
                            )
                        except Exception as notify_error:
                            logger.error("Failed to notify referrer %d: %s", referrer_id, notify_error)
            except Exception as e:
                logger.error("Error processing referral bonus for Crypto Bot user %d: %s", user_id, e)
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
