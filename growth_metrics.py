"""Read-only, cohort-based referral analytics without schema migrations.

A referral counts as paid once the canonical payment_bonus has been
recorded for the first successful subscription payment, including the 1 RUB trial. We do not equate issued VPN days with
cash or profitability.
"""
import time

from database import get_db


async def referral_growth_snapshot(*, days: int = 30, now: int | None = None) -> dict:
    if not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    snapshot_time = int(time.time() if now is None else now)
    cutoff = snapshot_time - days * 86400
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT r.referrer_id) FROM referrals r "
        "JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id "
        "WHERE r.created >= ? AND r.created <= ?",
        (cutoff, snapshot_time),
    )
    invited, advocates = await cur.fetchone()
    cur = await db.execute(
        """SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        WHERE r.created >= ? AND r.created <= ? AND EXISTS (
            SELECT 1 FROM referral_events e
            WHERE e.referrer_id=r.referrer_id
              AND e.referred_id=r.referred_id
              AND e.event_type='payment_bonus' AND e.created <= ?
        )""",
        (cutoff, snapshot_time, snapshot_time),
    )
    paying = (await cur.fetchone())[0]
    cur = await db.execute(
        """SELECT COALESCE(SUM(e.days_awarded), 0)
        FROM referral_events e
        JOIN referrals r ON r.referrer_id=e.referrer_id AND r.referred_id=e.referred_id
        JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        WHERE r.created >= ? AND r.created <= ? AND e.created <= ?
          AND e.event_type IN ('registration_bonus', 'payment_bonus')""",
        (cutoff, snapshot_time, snapshot_time),
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


async def referral_risk_snapshot(*, days: int = 1, min_invites: int = 5, limit: int = 10, now: int | None = None) -> dict:
    """Read-only indicators for manual review; never label a customer a fraudster.

    No reward is withheld automatically; this is only an indicator for manual review.
    """
    if not 1 <= days <= 31 or not 1 <= min_invites <= 1000 or not 1 <= limit <= 50:
        raise ValueError("invalid risk window or limits")
    end = int(time.time() if now is None else now)
    start = end - days * 86400
    db = await get_db()
    cursor = await db.execute(
        """SELECT r.referrer_id, COUNT(DISTINCT r.referred_id) AS invitations
           FROM referrals r
           JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
           WHERE r.created BETWEEN ? AND ?
           GROUP BY r.referrer_id
           HAVING invitations >= ?
           ORDER BY invitations DESC, r.referrer_id ASC
           LIMIT ?""",
        (start, end, min_invites, limit),
    )
    accounts = [
        {"referrer_id": int(row[0]), "invitations": int(row[1])}
        for row in await cursor.fetchall()
    ]
    # An unusually small first *RUB* payment is only a manual review signal.
    # Never compare Stars/XTR to RUB and never suppress the approved bonuses.
    cursor = await db.execute(
        """SELECT r.referrer_id, COUNT(DISTINCT r.referred_id)
           FROM referrals r
           JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
           JOIN payments p ON p.id=(
               SELECT first.id FROM payments first
               WHERE first.user_id=r.referred_id
                 AND first.status='success' AND first.amount>0
               ORDER BY first.created, first.id LIMIT 1
           )
           WHERE p.currency='RUB' AND p.amount<=?
             AND p.created BETWEEN ? AND ?
           GROUP BY r.referrer_id
           ORDER BY COUNT(DISTINCT r.referred_id) DESC, r.referrer_id
           LIMIT ?""",
        (30, start, end, limit),
    )
    low_value = [
        {"referrer_id": int(row[0]), "first_payments": int(row[1])}
        for row in await cursor.fetchall()
    ]
    return {
        "days": days,
        "suspicious_accounts": accounts,
        "low_value_first_payments_rub": low_value,
        "low_value_threshold_rub": 30,
    }
