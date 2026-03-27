"""
YooKassa webhook server — runs alongside the Telegram bot as an asyncio task.

Security model:
  1. Receive POST /yookassa/webhook
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
from typing import Optional

import httpx
import uvicorn
from aiogram import Bot
from fastapi import FastAPI, Request, Response

from config import (
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    WEBHOOK_HOST, WEBHOOK_PORT, ADMIN_ID,
)
from database import init_db, save_payment, has_yookassa_payment_processed, mark_yookassa_payment_processed
from subscription import deliver_key

logger = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None)  # disable docs in production

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

@app.post("/yookassa/webhook")
async def yookassa_webhook(request: Request) -> Response:
    """
    Receive YooKassa payment notification.
    Always responds 200 quickly — heavy work runs as background task.
    """
    try:
        body = await request.json()
    except Exception:
        logger.warning("Webhook: invalid JSON body")
        return Response(status_code=400)

    event = body.get("event", "")
    obj = body.get("object", {})
    payment_id = obj.get("id", "")

    # We only care about succeeded payments
    if event != "payment.succeeded" or not payment_id:
        return Response(status_code=200)

    # Schedule async processing — respond immediately to YooKassa
    bot: Bot = app.state.bot
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
        already_processed = await has_yookassa_payment_processed(payment_id)
        if already_processed:
            logger.info("Webhook: payment %s already processed, skipping", payment_id)
            return

        # Mark as processed BEFORE delivering to prevent race on duplicate webhooks
        marked = await mark_yookassa_payment_processed(payment_id)
        if not marked:
            # Another concurrent task beat us to it
            logger.info("Webhook: payment %s lost idempotency race, skipping", payment_id)
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

        # Validate devices — only 1, 2, 5 allowed; anything else → 1
        if devices not in (1, 2, 5):
            logger.warning("Webhook: invalid devices=%d in payment %s, defaulting to 1", devices, payment_id)
            devices = 1

        amount_str = payment.get("amount", {}).get("value", "0")
        try:
            amount_rub = int(float(amount_str))
        except ValueError:
            amount_rub = 0

        logger.info(
            "Webhook: processing payment %s — user=%d days=%d devices=%d amount=%d",
            payment_id, user_id, days, devices, amount_rub,
        )

        # ── Step 4: Deliver key ──
        # User needs to enter a config name — send them a prompt via bot
        await _prompt_config_name(bot, user_id, days, devices, amount_rub, payment_id)

    except Exception as e:
        logger.exception("Webhook: unexpected error processing payment %s: %s", payment_id, e)
        try:
            await _notify_admin(bot, f"🚨 Webhook error for payment {payment_id}: {str(e)[:300]}")
        except Exception:
            pass


async def _prompt_config_name(
    bot: Bot, user_id: int, days: int, devices: int, amount_rub: int, payment_id: str
) -> None:
    """
    Send the user a message asking for their config name,
    storing the payment context so the FSM handler can complete delivery.
    We store the context in a pending_yookassa_payments table and send
    a deep-link button that re-enters the FSM with the right context.
    """
    from database import save_pending_yookassa_payment
    from config import BOT_TOKEN
    import aiogram

    # Persist pending context so the FSM can pick it up after user taps the button
    await save_pending_yookassa_payment(
        payment_id=payment_id,
        user_id=user_id,
        days=days,
        devices=devices,
        amount_rub=amount_rub,
    )

    device_label = {1: "1 устройство", 2: "2 устройства", 5: "5 устройств"}.get(
        devices, f"{devices} устр."
    )

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Получить ключ",
            callback_data=f"yk_deliver:{payment_id}",
        )
    ]])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"📦 Тариф: <b>{device_label}</b>\n"
                f"⏳ Срок: <b>{days} дней</b>\n\n"
                "Нажмите кнопку ниже, затем введите название конфига\n"
                "(например: <code>iPhone</code>, <code>Ноутбук</code>):"
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        logger.info("Webhook: prompted user %d for config name (payment=%s)", user_id, payment_id)
    except Exception as e:
        logger.error("Webhook: could not message user %d: %s", user_id, e)
        await _notify_admin(
            bot,
            f"⚠️ Оплата {payment_id} прошла, но не удалось написать пользователю {user_id}.\n"
            f"Дней: {days}, устройств: {devices}, сумма: {amount_rub} ₽\n"
            f"Выдай ключ вручную через /admin."
        )


async def _notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.error("Could not notify admin: %s", e)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

async def start_webhook_server(bot: Bot) -> None:
    """Start the uvicorn server as an asyncio task."""
    app.state.bot = bot
    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("YooKassa webhook server starting on %s:%d", WEBHOOK_HOST, WEBHOOK_PORT)
    await server.serve()
