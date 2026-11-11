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

    No reward is withheld automatically. A one-ruble paid trial can cost more
    in free VPN days than it earns in cash, so look for unusually fast growth.
    """
    if not 1 <= days <= 31 or not 1 <= min_invites <= 1000 or not 1 <= limit <= 50:
        raise ValueError("invalid risk window or limits")
    end = int(time.time() if now is None else now)
    start = end - days * 86400
    db = await get_db()
    cursor = await db.execute(
        """SELECT r.referrer_id, COUNT(DISTINCT r.referred_id) AS invitations,
                  SUM(CASE WHEN EXISTS (
                      SELECT 1 FROM payments p
                      WHERE p.user_id=r.referred_id
                        AND p.status='success' AND p.currency='RUB'
                        AND p.amount=1 AND p.days=3
                  ) THEN 1 ELSE 0 END) AS paid_one_rub
           FROM referrals r
           JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
           WHERE r.created BETWEEN ? AND ?
           GROUP BY r.referrer_id
           HAVING invitations >= ?
           ORDER BY invitations DESC, paid_one_rub DESC, r.referrer_id ASC
           LIMIT ?""",
        (start, end, min_invites, limit),
    )
    accounts = [
        {"referrer_id": int(row[0]), "invitations": int(row[1]),
         "one_rub_trials": int(row[2] or 0)}
        for row in await cursor.fetchall()
    ]
    cursor = await db.execute(
        """SELECT COUNT(*)
           FROM referral_payment_rewards rw
           JOIN payments p
             ON p.user_id=rw.referred_id AND p.provider_payment_id=rw.payment_ref
           WHERE rw.created BETWEEN ? AND ?
             AND p.status='success' AND p.currency='RUB'
             AND p.amount=1 AND p.days=3""",
        (start, end),
    )
    count = int((await cursor.fetchone())[0] or 0)
    return {
        "days": days,
        "suspicious_accounts": accounts,
        "one_rub_referral_rewards": count,
        "promised_days_from_one_rub": count * 30,
    }
