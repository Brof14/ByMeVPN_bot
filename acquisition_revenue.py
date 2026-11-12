"""Read-only source attribution: gross provider settlements, never net profit."""
import time

from database import get_db, get_conversion_funnel


async def source_revenue_snapshot(days: int = 30, now: int | None = None) -> dict:
    days = int(days)
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    end = int(time.time() if now is None else now)
    cutoff = end - days * 86400
    funnel = await get_conversion_funnel(days)
    db = await get_db()
    cursor = await db.execute(
        """WITH cohort AS (
            SELECT u.user_id, u.created AS joined_at,
               COALESCE(
                 (SELECT NULLIF(first_event.source,'')
                  FROM analytics_events first_event
                  WHERE first_event.user_id=u.user_id
                    AND first_event.event_type='bot_started'
                  ORDER BY first_event.timestamp ASC, first_event.id ASC LIMIT 1),
                 NULLIF(u.source,''), 'direct'
               ) AS src
            FROM users u WHERE u.created BETWEEN ? AND ?
        )
        SELECT c.src, p.currency, COUNT(*), COALESCE(SUM(p.amount),0)
        FROM cohort c JOIN payments p ON p.user_id=c.user_id
        WHERE p.created >= c.joined_at AND p.created<=?
          AND p.status='success' AND p.amount>0
        GROUP BY c.src,p.currency""",
        (cutoff, end, end),
    )
    by_source = {
        name: {**row, "gross_by_currency": {}, "payments_by_currency": {}}
        for name, row in funnel["by_source"].items()
    }
    for src, currency, payments, amount in await cursor.fetchall():
        row = by_source.setdefault(str(src), {
            "started": 0, "trial": 0, "paid": 0, "connected": 0, "renewed": 0,
            "gross_by_currency": {}, "payments_by_currency": {},
        })
        row["gross_by_currency"][str(currency)] = int(amount)
        row["payments_by_currency"][str(currency)] = int(payments)
    return {"days": days, "by_source": by_source}
