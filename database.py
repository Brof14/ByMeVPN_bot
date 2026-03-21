"""
Database operations for ByMeVPN bot.
Uses aiosqlite with WAL mode for better concurrency.
"""
import time
import logging
from typing import Optional
import aiosqlite

logger = logging.getLogger(__name__)
DB_FILE = "vpnbot.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    trial_used  INTEGER DEFAULT 0,
    created     INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS keys (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    remark   TEXT,
    uuid     TEXT,
    days     INTEGER NOT NULL,
    created  INTEGER NOT NULL,
    expiry   INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_keys_user   ON keys(user_id);
CREATE INDEX IF NOT EXISTS idx_keys_expiry ON keys(expiry);

CREATE TABLE IF NOT EXISTS payments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    amount   INTEGER NOT NULL,
    currency TEXT    NOT NULL,
    method   TEXT    NOT NULL,
    days     INTEGER NOT NULL,
    created  INTEGER NOT NULL,
    payload  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_user    ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created);

CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    bonus_given INTEGER DEFAULT 0,
    created     INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE(referrer_id, referred_id)
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
"""


async def init_db() -> None:
    """Initialize database tables."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript(_SCHEMA)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.commit()
    logger.info("Database initialized: %s", DB_FILE)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def ensure_user(user_id: int) -> None:
    """Create user record if it does not exist."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
            (user_id,)
        )
        await db.commit()


async def get_referrer(user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT referrer_id FROM users WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_referrer(user_id: int, referrer_id: int) -> None:
    """Set referrer only if not already set and not self-referral."""
    if user_id == referrer_id:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,)
        )
        await db.execute(
            "UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL",
            (referrer_id, user_id),
        )
        # Track referral
        await db.execute(
            "INSERT OR IGNORE INTO referrals(referrer_id, referred_id) VALUES(?,?)",
            (referrer_id, user_id),
        )
        await db.commit()


async def has_used_trial(user_id: int) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT trial_used FROM users WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return bool(row[0]) if row else False


async def mark_trial_used(user_id: int) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO users(user_id, trial_used) VALUES(?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET trial_used=1",
            (user_id,),
        )
        await db.commit()


async def has_active_subscription(user_id: int) -> bool:
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE user_id=? AND expiry>?",
            (user_id, now),
        )
        return (await cur.fetchone())[0] > 0


async def has_paid_subscription(user_id: int) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM payments WHERE user_id=?", (user_id,)
        )
        return (await cur.fetchone())[0] > 0


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

async def add_user_key(
    user_id: int, key: str, remark: str, days: int, uuid: str = ""
) -> int:
    """Add key and return its ID."""
    now = int(time.time())
    expiry = now + days * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "INSERT INTO keys(user_id,key,remark,uuid,days,created,expiry) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, key, remark, uuid, days, now, expiry),
        )
        await db.commit()
        return cur.lastrowid


async def get_user_keys(user_id: int) -> list[dict]:
    """Return all keys for a user ordered by expiry desc."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id,key,remark,uuid,days,created,expiry "
            "FROM keys WHERE user_id=? ORDER BY expiry DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0], "key": r[1], "remark": r[2], "uuid": r[3],
            "days": r[4], "created": r[5], "expiry": r[6],
        }
        for r in rows
    ]


async def get_key_by_id(key_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id,user_id,key,remark,uuid,days,created,expiry "
            "FROM keys WHERE id=?",
            (key_id,),
        )
        r = await cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "user_id": r[1], "key": r[2], "remark": r[3],
        "uuid": r[4], "days": r[5], "created": r[6], "expiry": r[7],
    }


async def delete_key_by_id(key_id: int) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM keys WHERE id=?", (key_id,))
        await db.commit()


async def extend_key(key_id: int, days: int) -> None:
    """Add days to existing key (or extend from now if already expired)."""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT expiry FROM keys WHERE id=?", (key_id,))
        row = await cur.fetchone()
        if not row:
            return
        base = max(row[0], now)
        new_expiry = base + days * 86400
        await db.execute(
            "UPDATE keys SET expiry=?, days=days+? WHERE id=?",
            (new_expiry, days, key_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

async def save_payment(
    user_id: int, amount: int, currency: str, method: str,
    days: int, payload: str = ""
) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO payments(user_id,amount,currency,method,days,created,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, amount, currency, method, days, now, payload),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

async def mark_referral_bonus_given(referrer_id: int, referred_id: int) -> bool:
    """Mark bonus as given; returns True if first time."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT bonus_given FROM referrals WHERE referrer_id=? AND referred_id=?",
            (referrer_id, referred_id),
        )
        row = await cur.fetchone()
        if not row or row[0]:
            return False
        await db.execute(
            "UPDATE referrals SET bonus_given=1 WHERE referrer_id=? AND referred_id=?",
            (referrer_id, referred_id),
        )
        await db.commit()
        return True


async def get_referral_stats(user_id: int) -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)
        )
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND bonus_given=1",
            (user_id,),
        )
        paid = (await cur.fetchone())[0]
    return {"total": total, "paid": paid}


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

async def get_admin_stats() -> dict:
    now = int(time.time())
    day_start = now - 86400
    week_start = now - 7 * 86400
    month_start = now - 30 * 86400

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry>?", (now,)
        )
        active_users = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE created>=?",
            (day_start,),
        )
        today_revenue = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE created>=?",
            (week_start,),
        )
        week_revenue = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE created>=?",
            (month_start,),
        )
        month_revenue = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE bonus_given=1"
        )
        total_referrals = (await cur.fetchone())[0]

    return {
        "total_users": total_users,
        "active_users": active_users,
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "total_referrals": total_referrals,
    }


async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_keys_nearing_expiry(days_min: int = 1, days_max: int = 3) -> list[dict]:
    now = int(time.time())
    low = now + days_min * 86400
    high = now + days_max * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT k.user_id, k.key, k.expiry "
            "FROM keys k WHERE k.expiry BETWEEN ? AND ?",
            (low, high),
        )
        rows = await cur.fetchall()
    return [{"user_id": r[0], "key": r[1], "expiry": r[2]} for r in rows]


async def has_ever_had_key(user_id: int) -> bool:
    """True if user ever received any VPN key (active or expired)."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE user_id=?", (user_id,)
        )
        return (await cur.fetchone())[0] > 0


# ---------------------------------------------------------------------------
# Extended admin functions
# ---------------------------------------------------------------------------

async def get_all_users(limit: int = 100, offset: int = 0) -> list[dict]:
    """Return paginated user list with key counts."""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT u.user_id, u.trial_used, u.referrer_id, u.created,
                      COUNT(k.id) as total_keys,
                      SUM(CASE WHEN k.expiry > ? THEN 1 ELSE 0 END) as active_keys
               FROM users u
               LEFT JOIN keys k ON k.user_id = u.user_id
               GROUP BY u.user_id
               ORDER BY u.created DESC
               LIMIT ? OFFSET ?""",
            (now, limit, offset)
        )
        rows = await cur.fetchall()
    return [
        {
            "user_id": r[0], "trial_used": r[1], "referrer_id": r[2],
            "created": r[3], "total_keys": r[4], "active_keys": r[5] or 0,
        }
        for r in rows
    ]


async def get_users_count() -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]


async def find_user_by_id(user_id: int) -> Optional[dict]:
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT u.user_id, u.trial_used, u.referrer_id, u.created,
                      COUNT(k.id) as total_keys,
                      SUM(CASE WHEN k.expiry > ? THEN 1 ELSE 0 END) as active_keys,
                      COALESCE(SUM(p.amount), 0) as total_paid
               FROM users u
               LEFT JOIN keys k ON k.user_id = u.user_id
               LEFT JOIN payments p ON p.user_id = u.user_id
               WHERE u.user_id = ?
               GROUP BY u.user_id""",
            (now, user_id)
        )
        r = await cur.fetchone()
    if not r:
        return None
    return {
        "user_id": r[0], "trial_used": r[1], "referrer_id": r[2],
        "created": r[3], "total_keys": r[4], "active_keys": r[5] or 0,
        "total_paid": r[6] or 0,
    }


async def delete_user_and_keys(user_id: int) -> list[str]:
    """Delete user + all their keys. Returns list of UUIDs to remove from panel."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT uuid FROM keys WHERE user_id=? AND uuid IS NOT NULL AND uuid != ''",
            (user_id,)
        )
        rows = await cur.fetchall()
        uuids = [r[0] for r in rows if r[0]]
        await db.execute("DELETE FROM keys WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM payments WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM referrals WHERE referrer_id=? OR referred_id=?", (user_id, user_id))
        await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await db.commit()
    return uuids


async def set_key_days(key_id: int, new_days_from_now: int) -> None:
    """Set key expiry to now + new_days_from_now days."""
    now = int(time.time())
    new_expiry = now + new_days_from_now * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE keys SET expiry=?, days=? WHERE id=?",
            (new_expiry, new_days_from_now, key_id)
        )
        await db.commit()


async def reset_trial(user_id: int) -> None:
    """Reset trial_used flag so user can activate trial again."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE users SET trial_used=0 WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def get_user_payments(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id, amount, currency, method, days, created, payload "
            "FROM payments WHERE user_id=? ORDER BY created DESC LIMIT 20",
            (user_id,)
        )
        rows = await cur.fetchall()
    return [
        {"id": r[0], "amount": r[1], "currency": r[2], "method": r[3],
         "days": r[4], "created": r[5], "payload": r[6]}
        for r in rows
    ]


async def get_extended_stats() -> dict:
    """Extended stats: new users by period, active subs by plan."""
    now = int(time.time())
    day_start = now - 86400
    week_start = now - 7 * 86400
    month_start = now - 30 * 86400

    async with aiosqlite.connect(DB_FILE) as db:
        # New users
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE created>=?", (day_start,))
        new_day = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE created>=?", (week_start,))
        new_week = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE created>=?", (month_start,))
        new_month = (await cur.fetchone())[0]

        # Active subs by approximate plan (by days range)
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE expiry>? AND days<=35", (now,)
        )
        active_1m = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE expiry>? AND days>35 AND days<=120", (now,)
        )
        active_6m = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE expiry>? AND days>120 AND days<=400", (now,)
        )
        active_12m = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE expiry>? AND days>400", (now,)
        )
        active_24m = (await cur.fetchone())[0]

        # Top referrers
        cur = await db.execute(
            """SELECT referrer_id, COUNT(*) as cnt
               FROM referrals WHERE bonus_given=1
               GROUP BY referrer_id ORDER BY cnt DESC LIMIT 5"""
        )
        top_refs = await cur.fetchall()

    return {
        "new_day": new_day, "new_week": new_week, "new_month": new_month,
        "active_1m": active_1m, "active_6m": active_6m,
        "active_12m": active_12m, "active_24m": active_24m,
        "top_refs": [{"user_id": r[0], "count": r[1]} for r in top_refs],
    }


async def get_all_users_csv() -> str:
    """Return all users as CSV string."""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT u.user_id, u.trial_used, u.referrer_id, u.created,
                      COUNT(k.id), SUM(CASE WHEN k.expiry>? THEN 1 ELSE 0 END),
                      COALESCE((SELECT SUM(p2.amount) FROM payments p2 WHERE p2.user_id=u.user_id),0)
               FROM users u
               LEFT JOIN keys k ON k.user_id=u.user_id
               GROUP BY u.user_id ORDER BY u.created DESC""",
            (now,)
        )
        rows = await cur.fetchall()

    lines = ["user_id,trial_used,referrer_id,registered,total_keys,active_keys,total_paid_rub"]
    for r in rows:
        import datetime
        reg = datetime.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d") if r[3] else ""
        lines.append(f"{r[0]},{r[1]},{r[2] or ''},{reg},{r[4]},{r[5] or 0},{r[6]}")
    return "\n".join(lines)
