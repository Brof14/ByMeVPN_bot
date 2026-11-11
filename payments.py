import base64
import hashlib
import time
import uuid
import logging
from html import escape
from async_utils import monitor_performance
from typing import Optional
import httpx

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN
from yookassa_http import yookassa_client

logger = logging.getLogger(__name__)


async def record_fulfilled_promo(bot, code: str, user_id: int, provider: str, payment_id: str) -> bool:
    """Record promo use after delivery; alert if accounting needs reconciliation."""
    from config import ADMIN_IDS
    from database import has_user_used_promo, use_promo_code

    try:
        if await use_promo_code(code, user_id) or await has_user_used_promo(code, user_id):
            return True
    except Exception:
        logger.exception("Could not record promo usage for %s payment %s", provider, payment_id)

    logger.error("Promo usage needs reconciliation for %s payment %s", provider, payment_id)
    notice = (
        "⚠️ Подписка выдана, но использование промокода не удалось подтвердить. "
        "Нужна сверка учёта; повторно выдавать подписку не нужно.\n"
        f"Платёж: {escape(str(provider))} / <code>{escape(str(payment_id))}</code>\n"
        f"Пользователь: <code>{user_id}</code>; промокод: <code>{escape(str(code))}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notice, parse_mode="HTML")
        except Exception:
            logger.warning("Could not notify admin about promo reconciliation")
    return False


def crypto_invoice_payload(user_id: int, days: int, devices: int, promo_code: Optional[str] = None) -> str:
    """Canonical Telegram checkout payload, shared with its durable ledger."""
    return f"{user_id}:{days}:{devices}:{promo_code or ''}"

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
) -> bool:
    """Require durable recovery tracking before exposing a checkout link.

    The provider may have created an unpaid invoice. Without its ID persisted,
    a lost webhook cannot be reconciled by the payment monitor. Fail closed:
    do not direct the customer to a checkout that cannot be recovered.
    """
    if not payment_id:
        logger.error("YooKassa created a checkout without an ID (user=%d)", user_id)
        return False
    try:
        from database import add_yookassa_pending

        await add_yookassa_pending(payment_id, user_id, days, devices, amount_rub)
        return True
    except Exception:
        logger.exception("Could not persist YooKassa checkout %s for recovery; not exposing payment URL", payment_id)
        return False


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
    idempotency_key: Optional[str] = None,
    return_payment_details: bool = False,
    save_payment_method: Optional[bool] = None,
) -> Optional[str | dict]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.warning("YooKassa credentials not configured")
        return None

    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()

    stable_key = idempotency_key or f"checkout_{user_id}_{uuid.uuid4().hex}"
    if len(stable_key) > 64:
        stable_key = hashlib.sha256(stable_key.encode()).hexdigest()
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": stable_key,
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

    # Saving a payment method requires an explicit caller opt-in; environment defaults are not customer consent.
    should_save_method = bool(save_payment_method)
    if should_save_method:
        payload["save_payment_method"] = True

    logger.info("create_yookassa_payment: user_id=%d days=%d devices=%d months=%d amount=%d promo=%s recurring=%s",
                user_id, days, devices, months, amount_rub, promo_code, payload.get("save_payment_method", False))

    try:
        async with yookassa_client(timeout=15.0) as client:
            logger.info("Sending YooKassa request for user %d", user_id)
            r = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
            logger.info("YooKassa response status: %d", r.status_code)
            r.raise_for_status()
            data = r.json()
            url = data["confirmation"]["confirmation_url"]
            if not await _remember_yookassa_checkout(data.get("id"), user_id, days, devices, amount_rub):
                return None
            logger.info("YooKassa payment created for user %d: %s", user_id, data.get("id"))
            if return_payment_details:
                return {"payment_url": url, "payment_id": str(data.get("id") or ""), "status": data.get("status")}
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
            headers["Idempotence-Key"] = hashlib.sha256((stable_key + ":standard").encode()).hexdigest()
            try:
                async with yookassa_client(timeout=15.0) as client:
                    r2 = await client.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers)
                    r2.raise_for_status()
                    data = r2.json()
                    url = data["confirmation"]["confirmation_url"]
                    if not await _remember_yookassa_checkout(data.get("id"), user_id, days, devices, amount_rub):
                        return None
                    logger.info("YooKassa fallback standard payment created for user %d: %s", user_id, data.get("id"))
                    if return_payment_details:
                        return {"payment_url": url, "payment_id": str(data.get("id") or ""), "status": data.get("status")}
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


async def get_yookassa_payment_status(payment_id: str) -> Optional[str]:
    """Fetch the authoritative provider status for a specific invoice ID."""
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return None
    if not payment_id or len(payment_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in payment_id):
        return None
    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    try:
        async with yookassa_client(timeout=10.0) as client:
            response = await client.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                headers={"Authorization": f"Basic {auth}"},
            )
            response.raise_for_status()
            data = response.json()
            status = data.get("status") if isinstance(data, dict) else None
            return status if status in {"pending", "waiting_for_capture", "succeeded", "canceled"} else None
    except Exception as exc:
        logger.warning("YooKassa status lookup failed (%s)", type(exc).__name__)
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

    logger.info("charge_yookassa_recurrent: user_id=%d key_id=%d days=%d devices=%d amount=%d",
                user_id, key_id, days, devices, amount_rub)

    try:
        async with yookassa_client(timeout=20.0) as client:
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


class CryptoPayCreateError(RuntimeError):
    """A Crypto Pay invoice creation error with an explicit ambiguity marker."""

    def __init__(self, reason: str, outcome_unknown: bool):
        super().__init__(reason)
        self.outcome_unknown = bool(outcome_unknown)


@monitor_performance("cryptobot_create_payment")
async def create_crypto_payment(
    amount_rub: int,
    description: str,
    user_id: int,
    days: int,
    devices: int = 2,
    promo_code: Optional[str] = None,
    external_payload: Optional[str] = None,
    raise_errors: bool = False,
) -> Optional[tuple[str, str]]:
    """Create a Crypto Pay invoice; optionally expose safe retry semantics to API callers."""
    def fail(reason: str, outcome_unknown: bool):
        if raise_errors:
            raise CryptoPayCreateError(reason, outcome_unknown)
        return None

    if not CRYPTO_BOT_TOKEN:
        logger.warning("Crypto Bot token not configured")
        return fail("not_configured", False)

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
        "expires_in": 3600,
        "hidden_message": (
            "Оплата подписки ByMeVPN"
            if external_payload else f"User ID: {user_id}, Days: {days}, Devices: {devices}"
        ),
        "payload": external_payload or crypto_invoice_payload(user_id, days, devices, promo_code),
    }

    logger.info("create_crypto_payment: user_id=%d days=%d devices=%d amount=%d", user_id, days, devices, amount_rub)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://pay.crypt.bot/api/createInvoice",
                json=payload,
                headers=headers,
            )
            logger.info("Crypto Bot response status: %d", response.status_code)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                # An explicit provider rejection confirms no invoice was created.
                return fail("provider_rejected", False)
            invoice_data = data.get("result") or {}
            pay_url = invoice_data.get("bot_invoice_url") or invoice_data.get("pay_url")
            invoice_id = invoice_data.get("invoice_id")
            if not pay_url or invoice_id is None:
                # The provider may have created an invoice but omitted response fields.
                return fail("incomplete_provider_response", True)
            return str(pay_url), str(invoice_id)
    except CryptoPayCreateError:
        raise
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning("Crypto Bot invoice request returned HTTP %d", status_code)
        return fail(f"http_{status_code}", status_code == 408 or status_code >= 500)
    except httpx.RequestError as exc:
        logger.warning("Crypto Bot invoice request failed (%s)", type(exc).__name__)
        return fail("network_error", True)
    except Exception as exc:
        logger.warning("Crypto Bot invoice creation failed (%s)", type(exc).__name__)
        return fail("unexpected_error", True)


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


async def find_crypto_invoice_by_payload(payload: str, max_pages: int = 5) -> tuple[bool, Optional[dict]]:
    """Find an invoice after an ambiguous create timeout without issuing a duplicate.

    Crypto Pay has no documented create-invoice idempotency key. Scan recent
    invoices for our opaque payload; if the bounded scan cannot prove absence,
    callers must fail closed instead of creating another invoice.
    """
    if not CRYPTO_BOT_TOKEN:
        return False, None
    if not payload or len(payload) > 256:
        return False, None
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    page_size = 1000
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for page in range(max(1, min(int(max_pages), 5))):
                response = await client.get(
                    "https://pay.crypt.bot/api/getInvoices",
                    params={"count": page_size, "offset": page * page_size},
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    return False, None
                result = body.get("result") or {}
                items = result.get("items", []) if isinstance(result, dict) else result
                if not isinstance(items, list):
                    return False, None
                for invoice in items:
                    if isinstance(invoice, dict) and invoice.get("payload") == payload:
                        return True, invoice
                if len(items) < page_size:
                    return True, None
        # A full final page means older invoices may exist beyond our scan.
        return False, None
    except Exception as exc:
        logger.warning("Crypto Pay invoice reconciliation failed (%s)", type(exc).__name__)
        return False, None


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
