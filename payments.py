import base64
import time
import logging
from typing import Optional
import httpx

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN

logger = logging.getLogger(__name__)


async def create_yookassa_payment(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int = 1,
) -> Optional[str]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.warning("YooKassa credentials not configured")
        return None

    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": f"{user_id}_{int(time.time())}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/"},
        "capture": True,
        "description": description,
        "metadata": {"user_id": str(user_id), "days": str(days), "devices": str(devices)},
    }

    logger.info("create_yookassa_payment: user_id=%d days=%d devices=%d amount=%d", user_id, days, devices, amount_rub)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            logger.info("Sending YooKassa request for user %d", user_id)
            r = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
            logger.info("YooKassa response status: %d", r.status_code)
            r.raise_for_status()
            data = r.json()
            url = data["confirmation"]["confirmation_url"]
            logger.info("YooKassa payment created for user %d: %s", user_id, data.get("id"))
            return url
            
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error("YooKassa HTTP error for user %d: %s", user_id, error_msg)
        return None
    except httpx.RequestError as e:
        logger.error("YooKassa request error for user %d: %s", user_id, str(e))
        logger.error("Request details: timeout=%s, headers=%s", str(client.timeout), str(headers)[:100])
        return None
    except Exception as e:
        logger.error("YooKassa error for user %d: %s", user_id, str(e))
        logger.error("Payload: %s", str(payload)[:200])
        return None


async def create_crypto_payment(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int = 1,
) -> Optional[tuple[str, str]]:
    """Create payment via Crypto Bot (@send).

    Returns (pay_url, invoice_id) on success, None on failure.

    FIX: this used to send `"asset": "USDT", "amount": str(amount_rub)`,
    which created an invoice for e.g. 69 *USDT* (~6900 RUB) instead of
    69 RUB worth of crypto — a ~100x overcharge. Crypto Pay supports fiat
    invoices directly (`currency_type: "fiat", "fiat": "RUB"`), where it
    auto-converts to the crypto amount at the current rate, so we now
    charge the correct RUB-equivalent instead of a raw USDT figure.
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
        "payload": f"{user_id}:{days}:{devices}",
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
    except Exception as e:
        logger.error("Crypto Bot getInvoices request failed: %s", str(e))
        return []
