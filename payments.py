import base64
import time
import logging
from typing import Optional
import httpx

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

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
