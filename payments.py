import base64
import time
import uuid
import logging
from async_utils import monitor_performance
from typing import Optional
import httpx

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN

logger = logging.getLogger(__name__)

_yookassa_checkout_users: set[int] = set()


def claim_yookassa_checkout(user_id: int) -> bool:
    """Prevent overlapping payment-creation requests for one customer."""
    user_id = int(user_id)
    if user_id in _yookassa_checkout_users:
        return False
    _yookassa_checkout_users.add(user_id)
    return True


def release_yookassa_checkout(user_id: int) -> None:
    _yookassa_checkout_users.discard(int(user_id))


_yookassa_recurring_supported: Optional[bool] = None


async def _remember_yookassa_checkout(
    payment_id: Optional[str], user_id: int, days: int, devices: int, amount_rub: int,
) -> None:
    """Persist a checkout ID so polling can recover payments after long outages."""
    if not payment_id:
        logger.error("YooKassa created a checkout without an ID (user=%d)", user_id)
        return
    try:
        from database import add_yookassa_pending

        await add_yookassa_pending(payment_id, user_id, days, devices, amount_rub)
    except Exception:
        # The checkout is already created. Keep returning its URL, but make the
        # missing recovery record visible so the user is not blocked from paying.
        logger.exception("Could not persist YooKassa checkout %s for recovery", payment_id)


def is_yookassa_recurring_supported() -> bool:
    """Check if YooKassa store supports recurring payments."""
    global _yookassa_recurring_supported
    if _yookassa_recurring_supported is None:
        import os
        val = os.getenv("YOOKASSA_ENABLE_RECURRING", "").lower()
        if val in ("0", "false", "no"):
            _yookassa_recurring_supported = False
        else:
            _yookassa_recurring_supported = True
    return _yookassa_recurring_supported


@monitor_performance("yookassa_create_payment")
async def create_yookassa_payment(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int = 2,
    promo_code: Optional[str] = None,
    months: int = 1,
    extra_metadata: Optional[dict] = None,
) -> Optional[str]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.warning("YooKassa credentials not configured")
        return None

    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()

    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": f"checkout_{user_id}_{uuid.uuid4().hex}",
        "Content-Type": "application/json",
    }

    metadata = {
        "user_id": str(user_id),
        "days": str(days),
        "devices": str(devices),
        "months": str(months),
        "amount_rub": str(amount_rub),
    }
    if promo_code:
        metadata["promo_code"] = str(promo_code)
    if extra_metadata:
        metadata.update({k: str(v) for k, v in extra_metadata.items()})
    # This must reflect the amount actually sent to YooKassa, even if optional
    # metadata contains an overlapping key.
    metadata["amount_rub"] = str(amount_rub)

    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/ByMeVPN_bot"},
        "capture": True,
        "description": description,
        "metadata": metadata,
    }

    if is_yookassa_recurring_supported():
        payload["save_payment_method"] = True

    logger.info("create_yookassa_payment: user_id=%d days=%d devices=%d months=%d amount=%d promo=%s recurring=%s",
                user_id, days, devices, months, amount_rub, promo_code, payload.get("save_payment_method", False))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            logger.info("Sending YooKassa request for user %d", user_id)
            r = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
            logger.info("YooKassa response status: %d", r.status_code)
            r.raise_for_status()
            data = r.json()
            url = data["confirmation"]["confirmation_url"]
            await _remember_yookassa_checkout(data.get("id"), user_id, days, devices, amount_rub)
            logger.info("YooKassa payment created for user %d: %s", user_id, data.get("id"))
            return url
            
    except httpx.HTTPStatusError as e:
        error_text = e.response.text
        if e.response.status_code == 403 and "recurring payments" in error_text.lower():
            logger.warning(
                "YooKassa shop %s does not have recurring payments enabled in merchant agreement. "
                "Disabling auto-save and immediately retrying as standard payment.",
                YOOKASSA_SHOP_ID,
            )
            global _yookassa_recurring_supported
            _yookassa_recurring_supported = False
            payload.pop("save_payment_method", None)
            headers["Idempotence-Key"] = f"checkout_{user_id}_{uuid.uuid4().hex}_std"
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r2 = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
                    r2.raise_for_status()
                    data = r2.json()
                    url = data["confirmation"]["confirmation_url"]
                    await _remember_yookassa_checkout(data.get("id"), user_id, days, devices, amount_rub)
                    logger.info("YooKassa fallback standard payment created for user %d: %s", user_id, data.get("id"))
                    return url
            except Exception as retry_e:
                logger.error("YooKassa fallback payment error for user %d (%s): %r", user_id, type(retry_e).__name__, retry_e)
                return None

        error_msg = f"HTTP {e.response.status_code}: {error_text[:200]}"
        logger.error("YooKassa HTTP error for user %d: %s", user_id, error_msg)
        return None
    except httpx.RequestError as e:
        logger.error("YooKassa request error for user %d (%s): %r", user_id, type(e).__name__, e)
        return None
    except Exception as e:
        logger.exception("YooKassa payment creation failed for user %d (%s)", user_id, type(e).__name__)
        return None


@monitor_performance("yookassa_recurrent_charge")
async def charge_yookassa_recurrent(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int,
    months: int,
    key_id: int,
    payment_method_id: str,
    idempotence_key: Optional[str] = None,
) -> Optional[dict]:
    """
    Charge a saved payment method via YooKassa API for auto-renewal.
    Returns the created payment dictionary from YooKassa if successful, None on error.

    idempotence_key must be a DETERMINISTIC business key (e.g. derived from
    subscription_id + billing period being renewed + attempt number), NOT a
    bare timestamp: on worker restart / duplicate run YooKassa returns the
    same payment for the same key, which prevents double charges.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.warning("YooKassa credentials not configured")
        return None

    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    if not idempotence_key:
        # Last-resort fallback only — callers must pass a business key.
        idempotence_key = f"autorenew_{user_id}_{key_id}_{int(time.time())}"
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    metadata = {
        "user_id": str(user_id),
        "days": str(days),
        "devices": str(devices),
        "months": str(months),
        "key_id": str(key_id),
        "amount_rub": str(amount_rub),
        "auto_renew": "1",
    }

    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description,
        "metadata": metadata,
    }

    logger.info("charge_yookassa_recurrent: user_id=%d key_id=%d days=%d devices=%d amount=%d pm=%s",
                user_id, key_id, days, devices, amount_rub, payment_method_id)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            logger.info("YooKassa recurrent payment response for user %d: id=%s status=%s",
                        user_id, data.get("id"), data.get("status"))
            return data
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error("YooKassa recurrent HTTP error for user %d: %s", user_id, error_msg)
        return None
    except Exception as e:
        logger.error("YooKassa recurrent payment error for user %d: %s", user_id, e)
        return None


@monitor_performance("cryptobot_create_payment")
async def create_crypto_payment(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int = 2,
    promo_code: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Create payment via Crypto Bot (@send).

    Returns (pay_url, invoice_id) on success, None on failure.
    """
    if not CRYPTO_BOT_TOKEN:
        logger.warning("Crypto Bot token not configured")
        return None

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN,
        "Content-Type": "application/json",
    }

    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount_rub),
        "accepted_assets": "USDT,TON,BTC",
        "description": description,
        "paid_btn_name": "openBot",
        "paid_btn_url": "https://t.me/ByMeVPN_bot",
        "expires_in": 3600,  # 1 hour
        "hidden_message": f"User ID: {user_id}, Days: {days}, Devices: {devices}",
        "payload": f"{user_id}:{days}:{devices}:{promo_code or ''}",
    }

    logger.info("create_crypto_payment: user_id=%d days=%d devices=%d amount=%d", user_id, days, devices, amount_rub)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post("https://pay.crypt.bot/api/createInvoice", json=payload, headers=headers)
            logger.info("Crypto Bot response status: %d", r.status_code)
            r.raise_for_status()
            data = r.json()
            
            if data.get("ok"):
                invoice_data = data.get("result", {})
                pay_url = invoice_data.get("bot_invoice_url") or invoice_data.get("pay_url")
                invoice_id = invoice_data.get("invoice_id")
                logger.info("Crypto Bot payment created for user %d: invoice_id=%s", user_id, invoice_id)
                if not pay_url or invoice_id is None:
                    logger.error("Crypto Bot response missing pay_url/invoice_id: %s", data)
                    return None
                return pay_url, str(invoice_id)
            else:
                logger.error("Crypto Bot error: %s", data.get("error", "Unknown error"))
                return None
            
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error("Crypto Bot HTTP error for user %d: %s", user_id, error_msg)
        return None
    except httpx.RequestError as e:
        logger.error("Crypto Bot request error for user %d: %s", user_id, str(e))
        return None
    except Exception as e:
        logger.error("Crypto Bot error for user %d: %s", user_id, str(e))
        logger.error("Payload: %s", str(payload)[:200])
        return None


async def get_paid_crypto_invoices(limit: int = 50) -> list[dict]:
    """Fetch recently paid Crypto Pay invoices (status=paid) for the monitor to process."""
    if not CRYPTO_BOT_TOKEN:
        return []

    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://pay.crypt.bot/api/getInvoices",
                params={"status": "paid", "count": limit},
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data.get("result", {}).get("items", [])
            logger.error("Crypto Bot getInvoices error: %s", data.get("error"))
            return []
    except httpx.HTTPStatusError as e:
        # Provider-side gateway failures are transient and the monitor retries.
        if e.response.status_code in (500, 502, 503, 504, 520):
            logger.warning("Crypto Bot server error %d (expected, will retry): %s", e.response.status_code, str(e)[:100])
        else:
            logger.error("Crypto Bot HTTP error %d: %s", e.response.status_code, str(e)[:200])
        return []
    except httpx.RequestError as e:
        logger.warning("Crypto Bot request error (network issue): %s", str(e)[:100])
        return []
    except Exception as e:
        logger.error("Crypto Bot getInvoices unexpected error: %s", str(e))
        return []


async def get_crypto_invoice(invoice_id: str | int) -> Optional[dict]:
    """Fetch one persisted invoice by ID, including invoices older than the latest page."""
    invoices = await get_crypto_invoices([invoice_id])
    return invoices[0] if invoices else None


async def get_crypto_invoices(invoice_ids: list[str | int]) -> list[dict]:
    """Fetch specific persisted invoices from Crypto Pay in one provider request."""
    if not CRYPTO_BOT_TOKEN:
        return []
    if not invoice_ids:
        return []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://pay.crypt.bot/api/getInvoices",
                params={"invoice_ids": ",".join(str(invoice_id) for invoice_id in invoice_ids)},
                headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                logger.warning("Crypto Bot getInvoices error: %s", data.get("error"))
                return []
            return data.get("result", {}).get("items", [])
    except httpx.RequestError as exc:
        logger.warning("Crypto Bot invoice lookup failed: %s", str(exc)[:160])
    except Exception:
        logger.exception("Unexpected Crypto Bot invoice lookup failure")
    return []
