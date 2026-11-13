"""
Webhook server — runs alongside the Telegram bot as an asyncio task.

Handles:
  1. POST /webhook/telegram - Telegram bot updates
  2. POST /webhook/yookassa - YooKassa payment notifications

YooKassa Security model:
  1. Accept notifications only from YooKassa's documented source networks.
  2. Bound request size and validate the event envelope before scheduling work.
  3. NEVER trust the incoming body alone — always re-fetch the payment from
     YooKassa API using the payment_id from the body (prevents spoofing).
  4. Check payment status == "succeeded" on the verified response.
  5. Idempotency: mark payment_id as processed in DB before delivering key,
     so a duplicate webhook never gives a second key.
  6. Validate device counts against the current supported tiers.

Requires: fastapi, uvicorn[standard]  (added to requirements.txt)
Config:   WEBHOOK_HOST, WEBHOOK_PORT in .env
"""
import asyncio
import base64
import contextlib
import hmac
import ipaddress
import json
import logging
import re
import traceback
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from yookassa_http import yookassa_client
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup, Update
from bot_style import themed_button as InlineKeyboardButton
from fastapi import FastAPI, Request, Response

from config import (
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    WEBHOOK_HOST, WEBHOOK_PORT, ADMIN_ID, REDIS_URL, YOOKASSA_WEBHOOK_ALLOWED_IPS,
    TELEGRAM_WEBHOOK_SECRET,
    APP_CONNECT_TLS_HOST, APP_CONNECT_TLS_PORT,
    APP_CONNECT_TLS_CERT_FILE, APP_CONNECT_TLS_KEY_FILE,
)
from database import init_db, is_yookassa_processed, mark_yookassa_processed
from subscription import deliver_key

logger = logging.getLogger(__name__)


class _BackgroundUvicornServer(uvicorn.Server):
    """Leave process signals to aiogram's single polling lifecycle."""

    @contextlib.contextmanager
    def capture_signals(self):
        # Multiple embedded Uvicorn servers each install process-wide signal
        # handlers. In one event loop they overwrite and re-raise signals to
        # each other, making a normal container stop look like worker failure.
        yield

MAX_YOOKASSA_WEBHOOK_BYTES = 64 * 1024
MAX_TELEGRAM_WEBHOOK_BYTES = 1024 * 1024
YOOKASSA_PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
YOOKASSA_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in YOOKASSA_WEBHOOK_ALLOWED_IPS.split(",")
    if value.strip()
)


def _is_allowed_yookassa_source(source_ip: str | None) -> bool:
    """Accept only source addresses documented by YooKassa."""
    if not source_ip:
        return False
    try:
        address = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in YOOKASSA_ALLOWED_NETWORKS)

# Disable the schema endpoint as well as the interactive documentation pages.
# Setting only docs_url/redoc_url to None still leaves /openapi.json public.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

try:
    from api_router import api_router
    app.include_router(api_router, prefix="/api/v1")
except Exception as _router_err:
    raise RuntimeError("Required ByMeVPN API failed to initialize; check server configuration") from _router_err

from update_site import router as update_site_router
from download_routes import router as download_router
app.include_router(update_site_router)
app.include_router(download_router)



@app.get("/healthz")
async def health_check():
    """Backward-compatible health endpoint; now also checks Redis readiness."""
    return await readiness_check()


@app.get("/livez")
async def liveness_check():
    """Liveness endpoint: process event loop and HTTP server are responding."""
    return {"status": "ok"}


@app.get("/readyz")
async def readiness_check():
    """Readiness endpoint for local state required to handle customer updates."""
    from database import get_db
    from fastapi import HTTPException
    from redis.asyncio import Redis

    try:
        db = await get_db()
        await db.execute("SELECT 1")
        redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
    except Exception as exc:
        logger.error("Readiness check failed: local dependency unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="local dependency unavailable") from exc
    return {"status": "ready"}


@app.get("/app/login")
async def app_login_redirect(sub: str = "", name: str = "", ticket: str = ""):
    """Open the ByMeVPN app from a Telegram inline button.

    Telegram rejects custom URL schemes in buttons, so the bot links here and
    this page immediately hands the user over to ``bymevpn://login``.
    """
    from fastapi.responses import HTMLResponse

    from app_links import (
        APP_LOGIN_CONTENT_SECURITY_POLICY,
        MAX_SUBSCRIPTION_URL_LENGTH,
        MAX_USER_NAME_LENGTH,
        build_app_login_url,
        read_connect_ticket,
        render_app_open_page,
    )
    if ticket:
        try:
            handoff = read_connect_ticket(ticket)
        except (ValueError, RuntimeError):
            return HTMLResponse(
                "Ссылка подключения истекла или недействительна. <a href='https://t.me/ByMeVPNSupportBot'>Откройте поддержку</a> или вернитесь в бот и запросите новую.",
                status_code=400,
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Referrer-Policy": "no-referrer",
                    "X-Robots-Tag": "noindex, nofollow",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": APP_LOGIN_CONTENT_SECURITY_POLICY,
                },
            )
        sub = handoff["subscription_url"]
        name = handoff["user_name"]
    elif sub:
        return HTMLResponse(
            "Ссылка устарела. <a href='https://t.me/ByMeVPNSupportBot'>Откройте поддержку</a> или вернитесь в бот и снова нажмите «Подключить VPN».",
            status_code=400,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": APP_LOGIN_CONTENT_SECURITY_POLICY,
            },
        )
    sub = sub.strip()
    name = "".join(ch for ch in name if ch.isprintable())[:MAX_USER_NAME_LENGTH].strip()

    if len(sub) > MAX_SUBSCRIPTION_URL_LENGTH:
        return HTMLResponse("Ссылка слишком длинная.", status_code=400)
    if sub and not sub.startswith(("http://", "https://")):
        return HTMLResponse("Некорректная ссылка подписки.", status_code=400)

    page = render_app_open_page(build_app_login_url(sub, name))
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": APP_LOGIN_CONTENT_SECURITY_POLICY,
        },
    )


@app.get("/connect")
async def connection_choice_page(request: Request, ticket: str = ""):
    """Render the connection chooser directly in the single ticket GET."""
    from fastapi.responses import HTMLResponse
    from app_links import (
        APP_LOGIN_CONTENT_SECURITY_POLICY,
        read_connect_ticket,
        render_connection_choice_page,
    )
    from connect_ticket_replay import consume_connect_ticket

    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": APP_LOGIN_CONTENT_SECURITY_POLICY,
    }
    try:
        payload = read_connect_ticket(ticket)
        if not await consume_connect_ticket(ticket):
            return HTMLResponse(
                "<h1>Ссылка уже использована</h1><p>Вернитесь в бот и откройте новую ссылку подключения.</p>",
                status_code=410, headers=headers,
            )
        page = render_connection_choice_page(
            payload["subscription_url"], payload.get("user_name", ""),
        )
        return HTMLResponse(page, headers=headers)
    except (ValueError, TypeError, UnicodeDecodeError):
        return HTMLResponse(
            "<h1>Ссылка недоступна</h1><p>Вернитесь в бот и запросите новую ссылку подключения.</p>",
            status_code=400, headers=headers,
        )
    except Exception:
        logger.exception("Connection ticket redemption unavailable")
        return HTMLResponse(
            "<h1>Страница подключения временно недоступна</h1><p>Запросите новую ссылку в боте.</p>",
            status_code=503, headers=headers,
        )


@app.get("/app/logo")
async def app_login_logo():
    """Serve the official ByMeVPN mark used on the app handoff page."""
    from pathlib import Path

    from fastapi.responses import FileResponse

    logo_path = Path(__file__).resolve().parent / "static" / "bymevpn-mark.png"
    return FileResponse(
        logo_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/app/assets/{asset_name}", include_in_schema=False)
async def app_brand_asset(asset_name: str):
    """Serve the connection page's fixed set of public application icons."""
    from pathlib import Path
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    allowed = {
        "android.png", "apple.png", "bymevpn-logo.png", "happ.png", "incy.png",
        "koala.png", "linux.png", "v2raytun.png", "v2rayng.png", "v2rayn.png", "windows.png",
    }
    root = (Path(__file__).resolve().parent / "static" / "app" / "assets").resolve()
    path = (root / asset_name).resolve()
    if asset_name not in allowed or path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
    })

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
    if not TELEGRAM_WEBHOOK_SECRET:
        logger.error("Telegram webhook disabled: secret token is not configured")
        return Response(status_code=503)

    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied_secret, TELEGRAM_WEBHOOK_SECRET):
        return Response(status_code=401)

    if not dispatcher:
        logger.error("Telegram webhook unavailable: dispatcher not set")
        return Response(status_code=503)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_TELEGRAM_WEBHOOK_BYTES:
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_TELEGRAM_WEBHOOK_BYTES:
            return Response(status_code=413)
    try:
        update = Update.model_validate_json(body)
    except (ValueError, TypeError):
        logger.warning("Telegram webhook rejected: invalid update payload")
        return Response(status_code=400)

    try:
        await dispatcher.feed_update(app.state.bot, update)
        return Response(status_code=200)
    except Exception as exc:
        logger.error("Telegram webhook processing failed (%s)", type(exc).__name__)
        return Response(status_code=500)


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
        async with yookassa_client(timeout=15.0) as client:
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
    source_ip = request.client.host if request.client else None
    if not _is_allowed_yookassa_source(source_ip):
        logger.warning("YooKassa webhook rejected from non-allowlisted source %s", source_ip or "unknown")
        return Response(status_code=403)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_YOOKASSA_WEBHOOK_BYTES:
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)

    body_bytes = bytearray()
    async for chunk in request.stream():
        body_bytes.extend(chunk)
        if len(body_bytes) > MAX_YOOKASSA_WEBHOOK_BYTES:
            return Response(status_code=413)

    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        logger.warning("YooKassa webhook rejected: invalid JSON")
        return Response(status_code=400)

    if not isinstance(body, dict):
        return Response(status_code=400)

    event = body.get("event", "")
    obj = body.get("object", {})
    if not isinstance(event, str) or not isinstance(obj, dict):
        return Response(status_code=400)
    payment_id = obj.get("id", "")
    if payment_id and (not isinstance(payment_id, str) or not YOOKASSA_PAYMENT_ID_RE.fullmatch(payment_id)):
        logger.warning("YooKassa webhook rejected: invalid payment id format")
        return Response(status_code=400)

    logger.info("YooKassa webhook received: event=%s payment_id=%s", event, payment_id or "-")

    # We only care about succeeded payments
    if event != "payment.succeeded" or not payment_id:
        logger.info("Webhook: skipping - not payment.succeeded or no payment_id")
        return Response(status_code=200)

    # Never acknowledge an event which is only in an in-memory task.
    # A terminated worker must be able to replay this event after a restart.
    from database import record_yookassa_webhook
    try:
        await record_yookassa_webhook(payment_id)
    except Exception as exc:
        logger.error("YooKassa inbox persistence failed (%s)", type(exc).__name__)
        return Response(status_code=503)

    bot: Bot = app.state.bot
    asyncio.create_task(_process_recorded_yookassa_webhook(bot, payment_id))
    return Response(status_code=200)


async def _process_recorded_yookassa_webhook(
    bot: Bot, payment_id: str, verified_payment: Optional[dict] = None,
) -> None:
    """Process a persisted notification; only discard after verified resolution."""
    from database import (
        defer_yookassa_webhook, is_yookassa_fulfilled, resolve_yookassa_webhook,
    )
    try:
        if await is_yookassa_fulfilled(payment_id):
            await resolve_yookassa_webhook(payment_id)
            return
        payment = verified_payment or await _fetch_yookassa_payment(payment_id)
        if not payment:
            await defer_yookassa_webhook(payment_id, "provider_unavailable")
            return
        status = payment.get("status")
        if status == "canceled":
            await resolve_yookassa_webhook(payment_id)
            return
        if status != "succeeded":
            await defer_yookassa_webhook(payment_id, str(status or "unknown"))
            return
        await _process_payment(bot, payment_id, verified_payment=payment)
        if await is_yookassa_fulfilled(payment_id):
            await resolve_yookassa_webhook(payment_id)
        else:
            # Keep quarantined/uncertain settlements visible to operators.
            await defer_yookassa_webhook(payment_id, "fulfillment_pending")
    except Exception as exc:
        logger.error("Durable webhook processing failed (%s)", type(exc).__name__)
        try:
            await defer_yookassa_webhook(payment_id, "processing_error")
        except Exception:
            logger.exception("Failed to defer persisted YooKassa event %s", payment_id)


async def _mobile_yookassa_order_matches(
    payment_id: str, user_id: int, days: int, devices: int,
    amount_rub: int, metadata: dict,
) -> bool:
    """Verify a settled app invoice against its durable request and catalog."""
    from database import get_mobile_payment_request_by_invoice
    from constants import TARIFF_DAYS

    order = await get_mobile_payment_request_by_invoice("yookassa", payment_id)
    months = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}.get(order["plan_code"]) if order else None
    if not order or not months:
        return False
    # The provider's verified invoice metadata records the price at creation.
    # Do not reprice an already-created checkout when the catalog later changes.
    try:
        metadata_amount = int(metadata.get("amount_rub", 0))
        metadata_days = int(metadata.get("days", 0))
        metadata_months = int(metadata.get("months", 0))
    except (TypeError, ValueError):
        return False
    plan_code = f"{order['plan_code']}_d{order['devices']}"
    return all((
        order["user_id"] == user_id,
        order["provider"] == "yookassa",
        order["payment_id"] == payment_id,
        order["devices"] == devices,
        plan_code == metadata.get("plan_code"),
        metadata_months == months,
        TARIFF_DAYS[months] == days == metadata_days,
        amount_rub == metadata_amount > 0,
    ))


async def _mobile_v2_yookassa_order_matches(payment: dict) -> bool:
    """Validate the additive legacy-runtime V2 ledger before bot fulfillment."""
    if payment.get("status") != "succeeded":
        return False
    try:
        from mobile_v2_legacy import validate_yookassa_mobile_payment
        return bool(await validate_yookassa_mobile_payment(payment))
    except Exception as exc:
        logger.error("Mobile V2 YooKassa order validation unavailable (%s)", type(exc).__name__)
        return False


async def _mobile_yookassa_order_matches_for_payment(
    payment: dict,
    payment_id: str,
    user_id: int,
    days: int,
    devices: int,
    amount_rub: int,
    metadata: dict,
) -> bool:
    """Validate a mobile invoice against the ledger that created it.

    The current JWT API persists in ``mobile_payment_requests`` and sets both
    mobile markers for compatibility. Prefer that authoritative ledger when
    ``mobile_api_checkout`` is present; the older V2 adapter owns a different
    ``mobile_v2_orders`` table and remains only for invoices created by it.
    """
    if payment.get("status") != "succeeded":
        return False
    if metadata.get("mobile_api_checkout") == "1":
        return await _mobile_yookassa_order_matches(
            payment_id, user_id, days, devices, amount_rub, metadata,
        )
    if metadata.get("mobile_v2") == "1":
        return await _mobile_v2_yookassa_order_matches(payment)
    return True


def _uses_legacy_mobile_v2_order_ledger(metadata: dict) -> bool:
    """Only the legacy adapter needs its separate fulfilled-state marker."""
    return metadata.get("mobile_v2") == "1" and metadata.get("mobile_api_checkout") != "1"


async def _mark_mobile_v2_yookassa_fulfilled(payment_id: str) -> bool:
    """Record V2 fulfillment only after subscription delivery succeeded."""
    try:
        from mobile_v2_legacy import mark_mobile_order_fulfilled
        return bool(await mark_mobile_order_fulfilled("yookassa", payment_id))
    except Exception as exc:
        logger.error("Mobile V2 YooKassa fulfillment record unavailable (%s)", type(exc).__name__)
        return False


async def _mobile_crypto_order_matches(invoice_id: str, pending: dict) -> bool:
    """Verify a settled app Crypto Pay invoice against its durable request."""
    from database import get_mobile_payment_request_by_invoice
    from constants import TARIFF_DAYS

    order = await get_mobile_payment_request_by_invoice("crypto", invoice_id)
    months = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}.get(order["plan_code"]) if order else None
    if not order or not months:
        return False
    return all((
        order["user_id"] == int(pending["user_id"]),
        order["provider"] == "crypto",
        order["payment_id"] == invoice_id,
        order["devices"] == int(pending["devices"]),
        TARIFF_DAYS[months] == int(pending["days"]),
        int(pending["amount_rub"]) > 0,
    ))


async def _quarantine_deleted_account_payment(
    bot: Bot, *, provider: str, payment_id: str, user_id: int,
    amount_rub: int, days: int, devices: int,
) -> bool:
    """Preserve verified late charges for refund review, never restore VPN."""
    from database import (
        is_account_tombstoned, record_payment_idempotent,
        update_payment_status, get_payment_by_id,
    )

    if not await is_account_tombstoned(user_id):
        return False

    is_new, row_id = await record_payment_idempotent(
        user_id=user_id, amount=amount_rub, currency="RUB",
        method=provider, days=days, payload=str(payment_id),
        status="recovery_required", tariff="Deleted account — refund review",
        devices=devices, provider=provider, provider_payment_id=str(payment_id),
    )
    payment = await get_payment_by_id(row_id)
    status = payment.get("status") if payment else None
    if status != "success":
        if status != "recovery_required":
            await update_payment_status(row_id, "recovery_required")
        if is_new or status != "recovery_required":
            from html import escape
            await _notify_admin(
                bot,
                f"⚠️ Оплата {escape(str(provider))} {escape(str(payment_id))} "
                f"поступила после удаления аккаунта {user_id}. "
                "Доступ не выдавался. Нужна ручная сверка и проверка возврата.",
            )
            try:
                await bot.send_message(
                    user_id,
                    "Платёж подтверждён после удаления аккаунта. "
                    "Доступ не будет активирован автоматически. "
                    "Не оплачивайте повторно; обратитесь в @ByMeVPNSupportBot "
                    "для проверки возврата.",
                )
            except Exception:
                logger.warning("Could not notify erased user %d about late payment", user_id)
    return True


def _recurring_terms_for_saved_method(metadata: dict, paid_amount: int, paid_days: int, devices: int) -> tuple[int, int] | None:
    """Validate the disclosed *future* billing quote, not today's promo amount.

    Legacy invoices without a quote keep the terms that were disclosed at
    their creation time. A mismatched new quote never becomes a recurring debit.
    """
    raw_amount = metadata.get("auto_renew_recurring_price_rub")
    raw_days = metadata.get("auto_renew_recurring_days")
    if raw_amount is None and raw_days is None:
        return (paid_amount, paid_days) if paid_amount > 0 and paid_days > 0 else None
    if raw_amount is None or raw_days is None:
        return None
    from constants import get_price_for_months, VALID_DEVICE_LIMITS
    try:
        months = int(metadata.get("months", 0))
        if months not in (1, 3, 6, 12) or int(devices) not in VALID_DEVICE_LIMITS:
            return None
        expected_amount, expected_days = get_price_for_months(months, devices)
        if int(raw_amount) != expected_amount or int(raw_days) != expected_days:
            return None
        return expected_amount, expected_days
    except (ValueError, TypeError, OverflowError):
        return None


async def _process_payment(bot: Bot, payment_id: str, verified_payment: Optional[dict] = None) -> None:
    """
    Verify and process a succeeded YooKassa payment.
    Fully idempotent — safe to call multiple times for the same payment_id.
    """
    try:
        # ── Step 1: Re-fetch from YooKassa to verify (never trust webhook body) ──
        payment = verified_payment or await _fetch_yookassa_payment(payment_id)
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
        # Provider metadata can contain PII and user-controlled text.
        # Only payment IDs and explicit validation outcomes belong in logs.
        logger.info("Webhook: verifying payment_id=%s", payment_id)
        try:
            user_id = int(metadata["user_id"])
            raw_days = int(metadata["days"])
            devices = int(metadata.get("devices", 1))
        except (KeyError, ValueError, TypeError) as e:
            logger.error("Webhook: invalid metadata for payment %s (%s)", payment_id, type(e).__name__)
            await _notify_admin(bot, f"⚠️ YooKassa payment {payment_id}: некорректные метаданные. Требуется сверка.")
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

        # Mobile checkouts have a separate durable request record. Before
        # fulfillment, bind the verified invoice back to that owner and plan.
        # The amount stays the one recorded on the provider invoice at creation.
        # Telegram bot payments do not carry app_payment=1 and retain their
        # existing fulfillment contract.
        mobile_order_valid = await _mobile_yookassa_order_matches_for_payment(
            payment, payment_id, user_id, days, devices, amount_rub, metadata,
        )
        if not mobile_order_valid:
            logger.error("Mobile YooKassa invoice did not match its durable server-owned order")
            await _notify_admin(bot, "⚠️ Данные мобильного платежа не совпали с сохранённым тарифом. Автоматическая выдача остановлена.")
            return

        # Bind a renewal to the key selected when checkout started. Older
        # checkout metadata may omit it; in that case mirror deliver_key's
        # deterministic latest-expiry selection. Never use list position.
        target_key_id = None
        raw_key_id = metadata.get("key_id")
        if raw_key_id not in (None, ""):
            try:
                target_key_id = int(raw_key_id)
            except (TypeError, ValueError):
                target_key_id = 0
            from database import get_key_by_id
            selected_key = await get_key_by_id(target_key_id) if target_key_id > 0 else None
            if not selected_key or selected_key.get("user_id") != user_id:
                logger.error("YooKassa payment %s has invalid subscription binding", payment_id)
                await _notify_admin(bot, "⚠️ Платёж YooKassa содержит неверную привязку подписки. Автоматическая выдача остановлена.")
                return
        else:
            from database import get_user_keys
            now = int(time.time())
            existing_keys = await get_user_keys(user_id)
            extendable = [k for k in existing_keys if int(k.get("expiry") or 0) > now - 7 * 86400]
            if extendable:
                target_key_id = max(extendable, key=lambda k: int(k.get("expiry") or 0))["id"]

        is_autorenew = metadata.get("auto_renew") == "1"
        if is_autorenew and target_key_id is None:
            logger.error("Auto-renew payment %s has no unambiguous subscription binding", payment_id)
            await _notify_admin(bot, "⚠️ Автоплатёж YooKassa не связан с подпиской. Автоматическая выдача остановлена.")
            return

        logger.info(
            "Webhook: processing payment %s — user=%d days=%d devices=%d amount=%d",
            payment_id, user_id, days, devices, amount_rub,
        )

        if await _quarantine_deleted_account_payment(
            bot, provider="yookassa", payment_id=payment_id,
            user_id=user_id, amount_rub=amount_rub, days=days, devices=devices,
        ):
            return

        from database import (
            record_payment_idempotent, update_payment_status,
            delete_yookassa_pending,
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
        try:
            await bot.send_message(
                user_id,
                "<b>Оплата подтверждена</b>\n\nГотовим доступ. Обычно это занимает несколько секунд.\nЕсли активация задержится, повторно платить не нужно — мы проверим заказ.",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("Could not send payment processing status to user %d", user_id)
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
            target_key_id=target_key_id,
        )

        if success:
            if _uses_legacy_mobile_v2_order_ledger(metadata) and not await _mark_mobile_v2_yookassa_fulfilled(payment_id):
                await update_payment_status(pay_db_id, "recovery_required")
                await _notify_admin(bot, "⚠️ Подписка выдана, но мобильный статус платежа не удалось сохранить. Нужна ручная сверка.")
                return
            await update_payment_status(pay_db_id, "success")
            await mark_yookassa_processed(payment_id)
            try:
                await delete_yookassa_pending(payment_id)
            except Exception:
                pass
            promo_code = metadata.get("promo_code")
            if promo_code:
                from payments import record_fulfilled_promo
                await record_fulfilled_promo(bot, promo_code, user_id, "yookassa", payment_id)
            logger.info("YooKassa payment %s successfully fulfilled for user %d", payment_id, user_id)

            # Auto-renew handling: use the exact key targeted by delivery.
            from database import (
                get_user_keys,
                save_auto_renew_subscription,
                update_auto_renew_charge_success_by_user_key,
            )
            user_keys = await get_user_keys(user_id)
            key_id = target_key_id
            if key_id is None and len(user_keys) == 1:
                key_id = user_keys[0]["id"]
            if key_id is None and user_keys:
                logger.warning(
                    "Skipping saved payment method for user %d: checkout has no unique subscription binding",
                    user_id,
                )

            if is_autorenew:
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
                recurring_terms = _recurring_terms_for_saved_method(
                    metadata, amount_rub, days, devices
                ) if metadata.get("auto_renew_consent") == "1" else None
                if metadata.get("auto_renew_consent") == "1" and recurring_terms is None:
                    logger.error("YooKassa payment %s has an invalid renewal quote; refusing recurring enrollment", payment_id)
                if (
                    pm.get("saved") and pm.get("id") and key_id is not None
                    and metadata.get("auto_renew_consent") == "1"
                    and recurring_terms is not None
                ):
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
                        days=recurring_terms[1],
                        devices=devices,
                        amount_rub=recurring_terms[0],
                    )
                    logger.info("Saved auto-renew subscription for user %d (key=%s)", user_id, key_id)
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔄 <b>Автопродление подключено</b>\n\n"
                                f"По окончании оплаченного срока с вашего способа оплаты "
                                f"спишется {recurring_terms[0]} ₽ за следующие {recurring_terms[1]} дней. \n\n"
                                f"Вы можете в любой момент отключить автопродление в меню ключа."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as notify_e:
                        logger.debug("Failed to send autorenew confirmation to user %d: %s", user_id, notify_e)
                elif metadata.get("auto_renew_consent") == "1":
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                "Подписка активирована, но сохранить способ оплаты не удалось. "
                                "Автопродление не включено; после окончания срока продлите подписку вручную."
                            ),
                        )
                    except Exception as notify_e:
                        logger.debug("Failed to send one-time renewal fallback to user %d: %s", user_id, notify_e)

            # A successful first payment earns at most one referral bonus.
            from referral_system_new import award_referral_for_payment
            await award_referral_for_payment(
                user_id, payment_id, amount=amount_rub, bot=bot,
            )
        else:
            await update_payment_status(pay_db_id, "recovery_required")
            logger.error("YooKassa payment %s provisioning failed for user %d", payment_id, user_id)
            await _notify_admin(bot, f"⚠️ Не удалось выдать доступ по подтверждённой оплате YooKassa {payment_id} (user {user_id}). Платёж оставлен на безопасную ручную сверку.")
            try:
                await bot.send_message(
                    user_id,
                    "<b>Оплата получена · доступ проверяется</b>\n\nНе оплачивайте повторно. Мы проверяем выдачу и сообщим, когда ключ будет готов. Если вопрос срочный: @ByMeVPNSupportBot",
                    parse_mode="HTML",
                )
            except Exception:
                logger.debug("Could not send payment recovery status to user %d", user_id)

    except Exception as e:
        logger.exception("Webhook: unexpected error processing payment %s: %s", payment_id, e)
        try:
            await _notify_admin(bot, f"Webhook error for payment {payment_id}: {str(e)[:300]}")
        except Exception:
            pass


def _crypto_invoice_matches_pending(invoice: dict, pending: dict) -> bool:
    """Verify paid invoice amount and metadata against the server's durable order."""
    if invoice.get("currency_type") != "fiat" or invoice.get("fiat") != "RUB":
        return False
    try:
        actual = Decimal(str(invoice.get("amount")))
        expected = Decimal(str(pending.get("amount_rub")))
        if not actual.is_finite() or actual != expected:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    expected_payload = pending.get("payload")
    if expected_payload and invoice.get("payload") != expected_payload:
        return False
    return True


def _crypto_pending_promo(pending: dict) -> str | None:
    """Read a Telegram-order promo only from its verified durable payload."""
    payload = pending.get("payload") or ""
    parts = payload.split(":", 3)
    if len(parts) != 4:
        return None
    expected = (str(pending.get("user_id")), str(pending.get("days")), str(pending.get("devices")))
    if tuple(parts[:3]) != expected:
        return None
    return parts[3] or None


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

        # Look up what we recorded when the invoice was created. Provider status
        # alone is not enough: the invoice must match the server-owned order.
        pending = await get_crypto_pending(invoice_id)
        if not pending:
            logger.error("Crypto monitor: no pending record for invoice %s — cannot deliver", invoice_id)
            await _notify_admin(bot, f"⚠️ Оплаченный Crypto Bot инвойс {invoice_id} без pending-записи")
            return
        if not _crypto_invoice_matches_pending(invoice, pending):
            from database import record_payment_idempotent, update_payment_status

            _, payment_row_id = await record_payment_idempotent(
                user_id=pending["user_id"],
                amount=pending["amount_rub"],
                currency="RUB",
                method="cryptobot",
                days=pending["days"],
                payload=str(invoice_id),
                status="recovery_required",
                tariff=f"Подписка {pending['days']} дней ({pending['devices']} устр.)",
                devices=pending["devices"],
                provider="cryptobot",
                provider_payment_id=str(invoice_id),
            )
            await update_payment_status(payment_row_id, "recovery_required")
            if not ledger_row or ledger_row[1] != "recovery_required":
                logger.error("Crypto invoice %s does not match its stored order; fulfillment blocked", invoice_id)
                await _notify_admin(
                    bot,
                    f"⚠️ Crypto Bot инвойс {invoice_id}: сумма, валюта или данные заказа не совпадают. Выдача остановлена для сверки.",
                )
            return

        # Mobile Crypto Pay invoices carry an opaque per-request payload. Bind
        # those invoices to the owner-scoped mobile request and verify the
        # server catalog terms before the shared provisioning path runs.
        if str(pending.get("payload") or "").startswith("bm1_"):
            if not await _mobile_crypto_order_matches(str(invoice_id), pending):
                logger.error("Mobile Crypto invoice does not match its server-owned checkout terms")
                await _notify_admin(bot, "⚠️ Данные мобильного Crypto Bot платежа не совпали с тарифом. Выдача остановлена.")
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
            delete_crypto_pending,
        )

        if await _quarantine_deleted_account_payment(
            bot, provider="cryptobot", payment_id=str(invoice_id),
            user_id=user_id, amount_rub=amount_rub, days=days, devices=devices,
        ):
            return

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

        try:
            await bot.send_message(
                user_id,
                "<b>Оплата подтверждена</b>\n\nГотовим доступ. Обычно это занимает несколько секунд.\nЕсли активация задержится, повторно платить не нужно — мы проверим заказ.",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("Could not send Crypto Bot payment status to user %d", user_id)
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
            promo_code = _crypto_pending_promo(pending)
            if promo_code:
                from payments import record_fulfilled_promo
                await record_fulfilled_promo(bot, promo_code, user_id, "cryptobot", str(invoice_id))
            await mark_crypto_processed(invoice_id)
            try:
                await delete_crypto_pending(invoice_id)
            except Exception:
                pass
            logger.info("CryptoBot invoice %s successfully fulfilled for user %d", invoice_id, user_id)

            # Referral reward: single canonical path (mirrors YooKassa's rule).
            from referral_system_new import award_referral_for_payment
            await award_referral_for_payment(user_id, str(invoice_id), amount=amount_rub, bot=bot)
        else:
            await update_payment_status(pay_db_id, "recovery_required")
            logger.error("CryptoBot invoice %s provisioning requires recovery for user %d", invoice_id, user_id)
            try:
                await bot.send_message(
                    user_id,
                    "<b>Оплата получена · доступ проверяется</b>\n\nНе оплачивайте повторно. Мы проверяем выдачу и сообщим, когда ключ будет готов. Если вопрос срочный: @ByMeVPNSupportBot",
                    parse_mode="HTML",
                )
            except Exception:
                logger.debug("Could not send Crypto Bot recovery status to user %d", user_id)
            await _notify_admin(bot, f"⚠️ Не удалось выдать доступ по подтверждённой оплате Crypto Bot {invoice_id} (user {user_id}). Платёж оставлен на безопасную ручную сверку.")

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
    server = _BackgroundUvicornServer(config)
    logger.info("Webhook server starting on %s:%d", WEBHOOK_HOST, WEBHOOK_PORT)
    await server.serve()


async def start_connection_tls_server() -> None:
    """Serve only the browser handoff pages over HTTPS on the dedicated port."""
    if not APP_CONNECT_TLS_CERT_FILE or not APP_CONNECT_TLS_KEY_FILE:
        raise RuntimeError("connection page TLS certificate is not configured")

    from pathlib import Path

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse

    secure_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    secure_app.add_api_route("/connect", connection_choice_page, methods=["GET"])
    secure_app.add_api_route("/app/login", app_login_redirect, methods=["GET"])

    logo_path = Path(__file__).resolve().parent / "static" / "bymevpn-mark.png"
    asset_root = Path(__file__).resolve().parent / "static" / "app" / "assets"
    icon_assets = frozenset({
        "android.png", "apple.png", "bymevpn-logo.png", "happ.png", "incy.png", "koala.png", "linux.png",
        "v2raytun.png", "v2rayng.png",
        "v2rayn.png", "windows.png",
    })

    @secure_app.get("/app/logo", include_in_schema=False)
    async def secure_app_logo():
        if not logo_path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден")
        return FileResponse(
            logo_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
        )

    @secure_app.get("/app/assets/{asset_name}", include_in_schema=False)
    async def secure_app_brand_asset(asset_name: str):
        if asset_name not in icon_assets:
            raise HTTPException(status_code=404, detail="Файл не найден")
        root = asset_root.resolve()
        path = (root / asset_name).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден")
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
        )

    @secure_app.get("/healthz")
    async def secure_health():
        return JSONResponse({"status": "ok"})

    config = uvicorn.Config(
        secure_app,
        host=APP_CONNECT_TLS_HOST,
        port=APP_CONNECT_TLS_PORT,
        ssl_certfile=APP_CONNECT_TLS_CERT_FILE,
        ssl_keyfile=APP_CONNECT_TLS_KEY_FILE,
        log_level="warning",
        access_log=False,
    )
    server = _BackgroundUvicornServer(config)
    logger.info("Connection page HTTPS server starting on %s:%d", APP_CONNECT_TLS_HOST, APP_CONNECT_TLS_PORT)
    await server.serve()
