"""Additive mobile V2 adapter for the legacy ByMeVPN API runtime.

This module is intentionally separate from the Telegram payment flow. Payment
creation stays disabled until the legacy webhook calls both validation/finalize
hooks and MOBILE_V2_WEBHOOK_GUARD_ENABLED is explicitly enabled.
"""
from __future__ import annotations

import hashlib
import base64
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from constants import get_price_for_months


CATALOG = {
    f"{period}_d{devices}": {
        "plan_code": f"{period}_d{devices}",
        "months": months,
        "devices": devices,
        "days": days,
        "price_rub": get_price_for_months(months, devices)[0],
    }
    for period, months, days in (("1m", 1, 30), ("3m", 3, 120), ("6m", 6, 240), ("12m", 12, 450))
    for devices in (2, 5, 10)
}


class CreateOrder(BaseModel):
    plan_code: str
    request_id: str
    provider: str = "yookassa"

    class Config:
        extra = "forbid"


def _runtime():
    import database
    import config
    return database, config


async def current_user_id() -> int:
    # Deferred import avoids a cycle when the legacy api_router includes this
    # router at the bottom of the module.
    from api_router import get_current_user_id
    return await get_current_user_id()


async def _db():
    import aiosqlite
    database, _ = _runtime()
    # Dedicated connections keep the multi-statement reservation transaction
    # isolated from Telegram's long-lived shared aiosqlite connection.
    db = await aiosqlite.connect(database.DB_FILE, timeout=15)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=15000")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mobile_v2_orders (
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            provider TEXT NOT NULL,
            amount_rub INTEGER NOT NULL,
            days INTEGER NOT NULL,
            devices INTEGER NOT NULL,
            payment_id TEXT,
            payment_url TEXT,
            provider_payload TEXT,
            state TEXT NOT NULL DEFAULT 'creating',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, request_id),
            UNIQUE(provider, payment_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_mobile_v2_orders_invoice ON mobile_v2_orders(provider, payment_id)")
    await db.commit()
    return db


def _catalog_response(devices: int = 2) -> dict[str, Any]:
    labels = {"1m": ("month", "1 месяц", 0, False),
              "3m": ("quarter", "4 месяца (3+1)", 11, True),
              "6m": ("half_year", "8 месяцев (6+2)", 22, False),
              "12m": ("year", "15 месяцев (12+3)", 34, False)}
    plans = []
    for period, (desktop_code, name, discount, popular) in labels.items():
        options = [{"plan_code": f"{period}_d{d}", "devices": d,
                    "price_rub": CATALOG[f"{period}_d{d}"]["price_rub"],
                    "days": CATALOG[f"{period}_d{d}"]["days"]} for d in (2, 5, 10)]
        selected = next(option for option in options if option["devices"] == devices)
        plans.append({"code": selected["plan_code"], "desktop_code": desktop_code,
                      "name": name, "price": f"{selected['price_rub']} ₽",
                      "price_rub": selected["price_rub"], "days": selected["days"],
                      "devices": devices, "device_options": options,
                      "discount_percent": discount, "is_popular": popular})
    return {"trial": {"code": "trial", "name": "3 дня бесплатно", "price": "0 ₽",
                       "price_rub": 0, "days": 3, "devices": 2}, "plans": plans}


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/plans")
    async def plans(devices: int = 2):
        if devices not in (2, 5, 10):
            raise HTTPException(422, "Недопустимое количество устройств")
        return _catalog_response(devices)

    @router.post("/payment/create")
    async def create_payment(req: CreateOrder, user_id: int = Depends(current_user_id)):
        if os.getenv("MOBILE_V2_WEBHOOK_GUARD_ENABLED") != "1":
            raise HTTPException(503, "Мобильная оплата временно недоступна")
        plan = CATALOG.get(req.plan_code)
        if not plan or req.provider not in ("yookassa", "crypto"):
            raise HTTPException(400, "Неизвестный тариф или способ оплаты")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", req.request_id or ""):
            raise HTTPException(422, "Некорректный request_id")

        db = await _db()
        now = int(time.time())
        # BEGIN IMMEDIATE serializes competing request_id reservations across
        # connections. Unique(user_id, request_id) is the final idempotency guard.
        await db.execute("BEGIN IMMEDIATE")
        try:
            inserted = await db.execute(
                "INSERT OR IGNORE INTO mobile_v2_orders"
                "(user_id,request_id,plan_code,provider,amount_rub,days,devices,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,'creating',?,?)",
                (user_id, req.request_id, req.plan_code, req.provider, plan["price_rub"],
                 plan["days"], plan["devices"], now, now),
            )
            cur = await db.execute(
                "SELECT plan_code,provider,payment_id,payment_url,state FROM mobile_v2_orders WHERE user_id=? AND request_id=?",
                (user_id, req.request_id),
            )
            row = await cur.fetchone()
            if not row or row[0] != req.plan_code or row[1] != req.provider:
                await db.rollback()
                await db.close()
                raise HTTPException(409, "request_id уже использован для другого заказа")
            if row[2] and row[3]:
                await db.commit()
                await db.close()
                return {"payment_url": row[3], "invoice_id": row[2], "provider": req.provider,
                        "request_id": req.request_id}
            if not inserted.rowcount:
                # A prior request may have timed out after provider acceptance.
                # Reconcile manually rather than create a potentially duplicate invoice.
                await db.rollback()
                await db.close()
                raise HTTPException(503, "Проверяем создание счёта; повторное создание заблокировано")
            await db.commit()
        except HTTPException:
            raise
        except Exception:
            await db.rollback()
            await db.close()
            raise
        await db.close()

        database, config = _runtime()
        order_hash = hashlib.sha256(f"{user_id}:{req.request_id}".encode()).hexdigest()[:24]
        description = f"ByMeVPN — {plan['days']} дней, {plan['devices']} устройства"
        if req.provider == "yookassa":
            result = await _create_yookassa(config, plan, user_id, description, order_hash)
            if not result or not result.get("id") or not result.get("url"):
                raise HTTPException(502, "Провайдер не подтвердил создание счёта")
            invoice_id, payment_url = str(result["id"]), str(result["url"])
            payload = None
        else:
            payload = "bmv2_" + order_hash
            result = await _create_crypto(config, plan, description, payload)
            if not result or len(result) != 2:
                raise HTTPException(502, "Провайдер не подтвердил создание счёта")
            payment_url, invoice_id = str(result[0]), str(result[1])

        db = await _db()
        await db.execute(
            "UPDATE mobile_v2_orders SET payment_id=?,payment_url=?,provider_payload=?,state='pending',updated_at=? "
            "WHERE user_id=? AND request_id=? AND plan_code=? AND provider=?",
            (invoice_id, payment_url, payload, int(time.time()), user_id, req.request_id, req.plan_code, req.provider),
        )
        await db.commit()
        await db.close()
        if req.provider == "yookassa":
            await database.add_yookassa_pending(invoice_id, user_id, plan["days"], plan["devices"], plan["price_rub"])
        else:
            await database.add_crypto_pending(invoice_id, user_id, plan["days"], plan["devices"], plan["price_rub"])
        return {"payment_url": payment_url, "invoice_id": invoice_id,
                "provider": req.provider, "request_id": req.request_id}

    @router.get("/payment/check")
    async def check_payment(request: Request, invoice_id: str, provider: str,
                            user_id: int = Depends(current_user_id)):
        if provider not in ("yookassa", "crypto") or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice_id):
            raise HTTPException(400, "Некорректный счёт")
        db = await _db()
        cur = await db.execute("SELECT state FROM mobile_v2_orders WHERE provider=? AND payment_id=? AND user_id=?",
                               (provider, invoice_id, user_id))
        row = await cur.fetchone()
        await db.close()
        if not row:
            raise HTTPException(404, "Счёт не найден")
        # A provider-side paid status does not activate access. The webhook hook
        # marks this order fulfilled only after the existing delivery succeeds.
        active = row[0] == "fulfilled"
        return {"status": "succeeded" if active else "pending", "is_active": active,
                "subscription": None, "invoice_id": invoice_id, "provider": provider}

    return router


async def _create_yookassa(config, plan: dict, user_id: int, description: str, order_hash: str):
    """Use YooKassa's stable idempotency key without changing Telegram callers."""
    import httpx
    shop_id = getattr(config, "YOOKASSA_SHOP_ID", "")
    secret = getattr(config, "YOOKASSA_SECRET_KEY", "")
    if not shop_id or not secret:
        return None
    auth = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
    body = {"amount": {"value": f"{plan['price_rub']}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/ByMeVPN_bot"},
            "capture": True, "description": description,
            "metadata": {"user_id": str(user_id), "days": str(plan["days"]),
                         "devices": str(plan["devices"]), "months": str(plan["months"]),
                         "amount_rub": str(plan["price_rub"]), "mobile_v2": "1",
                         "plan_code": plan["plan_code"], "mobile_order": order_hash}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.yookassa.ru/v3/payments", json=body,
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json",
                         "Idempotence-Key": f"mobile-v2-{order_hash}"})
            response.raise_for_status()
            result = response.json()
            url = (result.get("confirmation") or {}).get("confirmation_url")
            if not result.get("id") or not url:
                return None
            return {"id": str(result["id"]), "url": str(url)}
    except Exception:
        return None


async def _create_crypto(config, plan: dict, description: str, opaque_payload: str):
    """Create a Crypto Pay invoice tagged with an opaque, non-user payload."""
    import httpx
    token = getattr(config, "CRYPTO_BOT_TOKEN", "")
    if not token:
        return None
    body = {"currency_type": "fiat", "fiat": "RUB", "amount": str(plan["price_rub"]),
            "accepted_assets": "USDT,TON,BTC", "description": description,
            "paid_btn_name": "openBot", "paid_btn_url": "https://t.me/ByMeVPN_bot",
            "expires_in": 3600, "hidden_message": "Оплата подписки ByMeVPN",
            "payload": opaque_payload}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://pay.crypt.bot/api/createInvoice", json=body,
                                         headers={"Crypto-Pay-API-Token": token})
            response.raise_for_status()
            result = (response.json().get("result") or {})
            url = result.get("bot_invoice_url") or result.get("web_app_invoice_url") or result.get("pay_url")
            invoice_id = result.get("invoice_id")
            return (str(url), str(invoice_id)) if url and invoice_id is not None else None
    except Exception:
        return None


async def validate_yookassa_mobile_payment(payment: dict) -> bool:
    """Call before legacy YooKassa fulfillment; non-mobile payments pass through."""
    metadata = payment.get("metadata") or {}
    invoice = str(payment.get("id") or "")
    db = await _db()
    cur = await db.execute("SELECT user_id,plan_code,amount_rub,days,devices,state,payment_id FROM mobile_v2_orders WHERE provider='yookassa' AND payment_id=?", (invoice,))
    row = await cur.fetchone()
    if not row:
        await db.close()
        return metadata.get("mobile_v2") != "1"
    if metadata.get("mobile_v2") != "1" or payment.get("status") != "succeeded":
        await db.close()
        return False
    if not row or row[6] != invoice or row[5] not in ("pending", "fulfilled"):
        return False
    plan = CATALOG.get(row[1])
    try:
        amount = Decimal(str((payment.get("amount") or {}).get("value", "0")))
        valid = bool(plan and metadata.get("mobile_order") == hashlib.sha256(
            f"{row[0]}:{(await _order_request_id(db, 'yookassa', invoice))}".encode()
        ).hexdigest()[:24] and int(metadata.get("user_id", -1)) == row[0]
            and int(metadata.get("days", -1)) == row[3]
            and int(metadata.get("devices", -1)) == row[4]
            and metadata.get("plan_code") == row[1]
            and int(metadata.get("months", -1)) == plan["months"]
            and plan["price_rub"] == row[2] and plan["days"] == row[3]
            and plan["devices"] == row[4]
            and amount.is_finite() and amount == Decimal(row[2])
            and (payment.get("amount") or {}).get("currency") == "RUB")
        await db.close()
        return valid
    except (TypeError, ValueError, InvalidOperation):
        await db.close()
        return False


async def _order_request_id(db, provider: str, invoice_id: str) -> str:
    cur = await db.execute("SELECT request_id FROM mobile_v2_orders WHERE provider=? AND payment_id=?", (provider, invoice_id))
    row = await cur.fetchone()
    return str(row[0]) if row else ""


async def validate_crypto_mobile_invoice(invoice: dict) -> bool:
    """Call before legacy Crypto Pay fulfillment; non-mobile invoices pass through."""
    payload = str(invoice.get("payload") or "")
    invoice_id = str(invoice.get("invoice_id") or "")
    db = await _db()
    cur = await db.execute("SELECT request_id,user_id,plan_code,amount_rub,days,devices,state,payment_id,provider_payload FROM mobile_v2_orders WHERE provider='crypto' AND payment_id=?", (invoice_id,))
    row = await cur.fetchone()
    if not row:
        await db.close()
        return not payload.startswith("bmv2_")
    if not payload.startswith("bmv2_"):
        await db.close()
        return False
    if not row or row[7] != invoice_id or row[8] != payload or row[6] not in ("pending", "fulfilled"):
        await db.close()
        return False
    plan = CATALOG.get(row[2])
    try:
        amount = Decimal(str(invoice.get("amount", "0")))
        valid = bool(plan and payload == "bmv2_" + hashlib.sha256(f"{row[1]}:{row[0]}".encode()).hexdigest()[:24]
                    and plan["price_rub"] == row[3] and plan["days"] == row[4]
                    and plan["devices"] == row[5] and amount.is_finite() and amount == Decimal(row[3])
                    and invoice.get("status") == "paid" and invoice.get("currency_type") == "fiat"
                    and invoice.get("fiat") == "RUB")
        await db.close()
        return valid
    except (TypeError, ValueError, InvalidOperation):
        await db.close()
        return False


async def mark_mobile_order_fulfilled(provider: str, invoice_id: str) -> None:
    db = await _db()
    await db.execute("UPDATE mobile_v2_orders SET state='fulfilled',updated_at=? WHERE provider=? AND payment_id=? AND state='pending'",
                     (int(time.time()), provider, str(invoice_id)))
    await db.commit()
    await db.close()
