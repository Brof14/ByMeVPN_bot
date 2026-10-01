"""
Payment reconciliation for ByMeVPN (stuck yookassa_pending / crypto_pending).

Purpose:
  Classify every pending-payment row against the DB and the live provider,
  find "paid but never delivered" cases (P0) and stale rows.

Modes (default: report — strictly read-only):
  python scripts/reconcile_payments.py report
      Read-only audit. Prints a table: every yookassa_pending / crypto_pending
      row with its DB state and live provider status.

  python scripts/reconcile_payments.py cleanup-stale
      Archive (JSON export to backups/) then DELETE rows that are PROVEN stale:
      - yookassa_pending: payment processed AND recorded in payments (delivered)
      - crypto_pending:   provider status is expired/active (never paid)
      Never touches rows whose money state is ambiguous.

  python scripts/reconcile_payments.py recover
      For rows where the provider says money was received but the key was NOT
      delivered: re-run the standard idempotent processing path
      (webhook._process_payment / _process_crypto_invoice). Safe to re-run.

Run inside the vpnbot container (has the code, env and network):
  docker exec vpnbot python scripts/reconcile_payments.py report
"""
import asyncio
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN, BOT_TOKEN  # noqa: E402
import database  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("reconcile")


# ---------------------------------------------------------------------------
# Provider status helpers (read-only)
# ---------------------------------------------------------------------------

async def yookassa_payment_status(payment_id: str) -> dict:
    """Fetch payment status from YooKassa API (same verify pattern as webhook)."""
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return {"status": "unknown", "reason": "no credentials"}
    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                headers={"Authorization": f"Basic {auth}"},
            )
            r.raise_for_status()
            d = r.json()
            return {"status": d.get("status"), "paid": d.get("paid"), "metadata": d.get("metadata", {})}
    except Exception as e:
        return {"status": "unknown", "reason": str(e)[:120]}


async def cryptobot_invoice_status(invoice_id: str) -> dict:
    """Fetch invoice status from CryptoBot API."""
    if not CRYPTO_BOT_TOKEN:
        return {"status": "unknown", "reason": "no token"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://pay.crypt.bot/api/getInvoices",
                params={"invoice_ids": str(invoice_id)},
                headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            )
            r.raise_for_status()
            d = r.json()
            items = d.get("result", {}).get("items", []) if d.get("ok") else []
            if not items:
                return {"status": "not_found"}
            inv = items[0]
            return {"status": inv.get("status"), "amount": inv.get("amount")}
    except Exception as e:
        return {"status": "unknown", "reason": str(e)[:120]}


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------

async def payment_row(provider: str, provider_payment_id: str):
    db = await database.get_db()
    cur = await db.execute(
        "SELECT id, status FROM payments WHERE provider=? AND provider_payment_id=?",
        (provider, provider_payment_id),
    )
    row = await cur.fetchone()
    return {"id": row[0], "status": row[1]} if row else None


async def processed_flag(table: str, column: str, value: str) -> bool:
    db = await database.get_db()
    cur = await db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,))
    return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

async def mode_report() -> None:
    print("=" * 100)
    print("YOOKASSA PENDING RECONCILIATION")
    print("=" * 100)
    db = await database.get_db()
    cur = await db.execute("SELECT payment_id, user_id, days, devices, amount_rub, created FROM yookassa_pending ORDER BY created")
    yk_rows = await cur.fetchall()
    for pid, user_id, days, devices, amount, created in yk_rows:
        proc = await processed_flag("yookassa_processed", "payment_id", pid)
        prow = await payment_row("yookassa", pid)
        live = await yookassa_payment_status(pid)
        verdict = classify_yk(proc, prow, live)
        print(f"  payment={pid[:18]}.. user={user_id} days={days} amount={amount}₽ "
              f"created={datetime.fromtimestamp(created):%Y-%m-%d} | processed={proc} "
              f"payments_row={prow} | provider={live.get('status')} → {verdict}")

    print()
    print("=" * 100)
    print("CRYPTOBOT PENDING RECONCILIATION")
    print("=" * 100)
    cur = await db.execute("SELECT invoice_id, user_id, days, devices, amount_rub, created FROM crypto_pending ORDER BY created")
    cr_rows = await cur.fetchall()
    for iid, user_id, days, devices, amount, created in cr_rows:
        proc = await processed_flag("crypto_processed", "invoice_id", iid)
        prow = await payment_row("cryptobot", iid)
        live = await cryptobot_invoice_status(iid)
        verdict = classify_crypto(proc, prow, live)
        print(f"  invoice={iid} user={user_id} days={days} amount={amount}₽ "
              f"created={datetime.fromtimestamp(created):%Y-%m-%d} | processed={proc} "
              f"payments_row={prow} | provider={live.get('status')} → {verdict}")

    print()
    print(f"Summary: yookassa_pending={len(yk_rows)}, crypto_pending={len(cr_rows)}")
    print("Verdicts: DELIVERED / STALE_UNPAID → cleanup-stale is safe for these.")
    print("          PAID_NOT_DELIVERED → run `recover` (P0: money received, no service).")


def classify_yk(processed: bool, prow, live: dict) -> str:
    if prow and prow.get("status") == "success":
        return "DELIVERED"
    reason = str(live.get("reason", ""))
    if live.get("status") == "unknown" and "not_found" in reason:
        # Payment ID does not exist in the CURRENT shop (created under an old
        # shop id / config). Never re-chargeable, never verifiable here —
        # legacy artifact. Keep the row for owner review (possible
        # compensation), cleanup-stale will not touch it.
        return "NOT_IN_CURRENT_SHOP (legacy — owner review)"
    if processed:
        return "PROCESSED_NO_PAYROW (investigate)"
    if live.get("status") == "succeeded":
        return "PAID_NOT_DELIVERED (P0)"
    if live.get("status") in ("pending", "waiting_for_capture"):
        return "PENDING_AT_PROVIDER (do not touch)"
    if live.get("status") in ("canceled",):
        return "STALE_UNPAID"
    return f"UNKNOWN ({live})"


def classify_crypto(processed: bool, prow, live: dict) -> str:
    if prow and prow.get("status") == "success":
        return "DELIVERED"
    if processed:
        return "PROCESSED_NO_PAYROW (investigate)"
    if live.get("status") == "paid":
        return "PAID_NOT_DELIVERED (P0)"
    if live.get("status") == "active":
        return "STILL_ACTIVE_AT_PROVIDER (do not touch)"
    if live.get("status") in ("expired",):
        return "STALE_UNPAID"
    return f"UNKNOWN ({live})"


async def mode_cleanup_stale() -> None:
    os.makedirs("backups", exist_ok=True)
    export_path = f"backups/reconcile_export_{datetime.now():%Y%m%d_%H%M%S}.json"
    export = {"timestamp": int(time.time()), "yookassa_deleted": [], "crypto_deleted": []}
    db = await database.get_db()
    deleted_yk = deleted_cr = 0

    cur = await db.execute("SELECT payment_id, user_id, days, devices, amount_rub, created FROM yookassa_pending")
    for pid, user_id, days, devices, amount, created in await cur.fetchall():
        prow = await payment_row("yookassa", pid)
        if prow and prow["status"] == "success":
            export["yookassa_deleted"].append(
                {"payment_id": pid, "user_id": user_id, "days": days, "devices": devices,
                 "amount_rub": amount, "created": created, "reason": "delivered (payments row success)"})
            await db.execute("DELETE FROM yookassa_pending WHERE payment_id=?", (pid,))
            deleted_yk += 1
        else:
            logger.info("Keep yookassa_pending %s (payments row: %s) — not proven delivered", pid, prow)

    cur = await db.execute("SELECT invoice_id, user_id, days, devices, amount_rub, created FROM crypto_pending")
    for iid, user_id, days, devices, amount, created in await cur.fetchall():
        prow = await payment_row("cryptobot", iid)
        live = await cryptobot_invoice_status(iid)
        if not prow and live.get("status") in ("expired",):
            export["crypto_deleted"].append(
                {"invoice_id": iid, "user_id": user_id, "days": days, "devices": devices,
                 "amount_rub": amount, "created": created,
                 "reason": f"provider status={live.get('status')} (never paid)"})
            await db.execute("DELETE FROM crypto_pending WHERE invoice_id=?", (iid,))
            deleted_cr += 1
        else:
            logger.info("Keep crypto_pending %s (payments row: %s, provider: %s)", iid, prow, live.get("status"))

    await db.commit()
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"Deleted: yookassa_pending={deleted_yk}, crypto_pending={deleted_cr}")
    print(f"Archive exported to: {export_path}")


async def mode_recover() -> None:
    """Re-run the standard idempotent processing for paid-but-undelivered rows."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        db = await database.get_db()
        recovered = 0
        cur = await db.execute("SELECT payment_id FROM yookassa_pending")
        for (pid,) in await cur.fetchall():
            proc = await processed_flag("yookassa_processed", "payment_id", pid)
            prow = await payment_row("yookassa", pid)
            live = await yookassa_payment_status(pid)
            if not proc and not prow and live.get("status") == "succeeded":
                logger.warning("P0 RECOVERY: yookassa payment %s is succeeded but never delivered — reprocessing", pid)
                from webhook import _process_payment
                await _process_payment(bot, pid)
                recovered += 1

        cur = await db.execute("SELECT invoice_id FROM crypto_pending")
        for (iid,) in await cur.fetchall():
            proc = await processed_flag("crypto_processed", "invoice_id", iid)
            prow = await payment_row("cryptobot", iid)
            live = await cryptobot_invoice_status(iid)
            if not proc and not prow and live.get("status") == "paid":
                logger.warning("P0 RECOVERY: crypto invoice %s is paid but never delivered — reprocessing", iid)
                pending = await database.get_crypto_pending(iid)
                if pending:
                    from webhook import _process_crypto_invoice
                    await _process_crypto_invoice(bot, dict(pending))
                    recovered += 1

        print(f"Recovered payments: {recovered} (0 means everything is consistent)")
    finally:
        await bot.session.close()


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    await database.init_db()
    try:
        if mode == "report":
            await mode_report()
        elif mode == "cleanup-stale":
            await mode_cleanup_stale()
        elif mode == "recover":
            await mode_recover()
        else:
            print(f"Unknown mode: {mode}. Use: report | cleanup-stale | recover")
            sys.exit(2)
    finally:
        await database.close_db()


if __name__ == "__main__":
    asyncio.run(main())
