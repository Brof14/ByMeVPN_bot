"""Read-only, cohort-based referral analytics without schema migrations.

A referral counts as paid only once the canonical payment_bonus has been
awarded for a qualifying purchase. We do not equate issued VPN days with
cash or profitability.
"""
import time

from database import get_db


async def referral_growth_snapshot(*, days: int = 30, now: int | None = None) -> dict:
    if not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    cutoff = int(time.time() if now is None else now) - days * 86400
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT referrer_id) FROM referrals WHERE created >= ?",
        (cutoff,),
    )
    invited, advocates = await cur.fetchone()
    cur = await db.execute(
        """SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        WHERE r.created >= ? AND EXISTS (
            SELECT 1 FROM referral_events e
            WHERE e.referrer_id=r.referrer_id
              AND e.referred_id=r.referred_id
              AND e.event_type='payment_bonus'
        )""",
        (cutoff,),
    )
    paying = (await cur.fetchone())[0]
    cur = await db.execute(
        """SELECT COALESCE(SUM(e.days_awarded), 0)
        FROM referral_events e
        JOIN referrals r ON r.referrer_id=e.referrer_id AND r.referred_id=e.referred_id
        WHERE r.created >= ? AND e.event_type IN ('registration_bonus', 'payment_bonus')""",
        (cutoff,),
    )
    bonus_days = int((await cur.fetchone())[0] or 0)
    return {
        "days": days,
        "invited": invited,
        "advocates": advocates,
        "paying": paying,
        "unconverted": invited - paying,
        "conversion_pct": 100 * paying / invited if invited else 0.0,
        "bonus_days": bonus_days,
    }
