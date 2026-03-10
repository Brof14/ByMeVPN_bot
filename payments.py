import base64
import time
import logging
from typing import Optional, Tuple

import httpx

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTOBOT_TOKEN, STAR_RATE_RUB

logger = logging.getLogger(__name__)


async def create_yookassa_payment(amount_rub: int, description: str, user_id: int) -> str:
    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": f"{user_id}_{int(time.time())}",
        "Content-Type": "application/json",
    }
    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://bymevpn.duckdns.org:8443/"},
        "capture": True,
        "description": description,
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["confirmation"]["confirmation_url"]


async def create_cryptobot_invoice(
    amount_usd: float,
    description: str,
    user_id: int,
) -> Optional[Tuple[str, int]]:
    """
    Создаёт инвойс в CryptoBot.
    Возвращает (pay_url, invoice_id) или None при ошибке.
    """
    # Проверяем баланс перед созданием инвойса
    try:
        headers = {
            "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post("https://pay.crypt.bot/api/getBalance", headers=headers)
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    for asset in data.get("result", []):
                        if asset["currency_code"] == "USDT" and float(asset["available"]) > 0:
                            # Баланс положительный, продолжаем создание инвойса
                            break
                    else:
                        # Баланс нулевой, возвращаем None
                        logger.warning("CryptoBot баланс нулевой, оплата отключена")
                        return None
    except Exception as e:
        logger.error(f"Ошибка проверки баланса CryptoBot: {e}")
        return None
    
    # Создаем инвойс если баланс есть
    payload = {
        "asset": "USDT",
        "amount": str(amount_usd),
        "description": description,
        "payload": f"vpn_{user_id}_{int(time.time())}",
        "paid_btn_name": "open_bot",
        "paid_btn_url": "https://bymevpn.duckdns.org:8443/",
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post("https://pay.crypt.bot/api/createInvoice", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and data.get("result"):
            result = data["result"]
            return result["pay_url"], result["invoice_id"]
        return None


async def get_cryptobot_invoice_status(invoice_id: int) -> Optional[str]:
    """
    Проверяет статус инвойса в CryptoBot.
    Возвращает статус ('active', 'paid', 'expired', ...) или None при ошибке.
    """
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"invoice_ids": [invoice_id]}
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post("https://pay.crypt.bot/api/getInvoices", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data.get("ok") and data.get("result"):
                inv = data["result"][0]
                return inv.get("status")
    except Exception as e:
        logger.error(f"Ошибка запроса статуса инвойса CryptoBot {invoice_id}: {e}")
    return None


def calculate_stars_amount(rub_price: int) -> int:
    """
    Пересчёт суммы в рублях в количество Telegram Stars.
    """
    return max(1, -(-rub_price // STAR_RATE_RUB))