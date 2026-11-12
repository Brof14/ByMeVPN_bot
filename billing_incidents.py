"""Read-only, bounded reconciliation view for financial incidents.

Never treat a provider invoice ID as evidence of payment or issue subscription
days from this report. Existing idempotent fulfillment owns the write path.
"""
import time
from database import get_db


async def read_billing_incidents(limit: int = 15, now: int | None = None) -> list[dict]:
    if not 1 <= limit <= 50:
        raise ValueError("limit out of bounds")
    now = int(time.time()) if now is None else int(now)
    db = await get_db()
    cursor = await db.execute(
        """SELECT provider, provider_payment_id, user_id, status, created
           FROM payments
           WHERE status IN ('recovery_required','processing','failed')
           ORDER BY created DESC LIMIT ?""",
        (limit,),
    )
    result = [
        {"provider": str(provider), "invoice_id": str(payment_id),
         "user_id": int(uid), "status": str(status), "created": int(created)}
        for provider, payment_id, uid, status, created in await cursor.fetchall()
    ]
    cursor = await db.execute(
        """SELECT i.payment_id, p.user_id, i.last_status, i.received_at
           FROM yookassa_webhook_inbox i
           LEFT JOIN payments p ON p.provider='yookassa'
             AND p.provider_payment_id=i.payment_id
           WHERE NOT EXISTS (
             SELECT 1 FROM payments settled WHERE settled.provider='yookassa'
             AND settled.provider_payment_id=i.payment_id
             AND settled.status='success'
           )
           ORDER BY i.received_at DESC LIMIT ?""",
        (limit,),
    )
    known = {(row["provider"], row["invoice_id"]) for row in result}
    for invoice_id, uid, status, created in await cursor.fetchall():
        if ("yookassa", str(invoice_id)) in known:
            continue
        result.append({
            "provider": "yookassa", "invoice_id": str(invoice_id),
            "user_id": int(uid) if uid is not None else None,
            "status": "webhook: " + str(status), "created": int(created),
        })
    result.sort(key=lambda row: row["created"], reverse=True)
    return result[:limit]
