"""
Database operations for ByMeVPN bot.
Uses aiosqlite with WAL mode for better concurrency and connection pooling.
"""
import time
import logging
from typing import Optional
import aiosqlite
from cache import cache_user_info, invalidate_user_cache, invalidate_subscription_cache
from async_utils import _db_semaphore

logger = logging.getLogger(__name__)
DB_FILE = "data/vpnbot.db"

# Global database connection pool
_db_pool: Optional[aiosqlite.Connection] = None

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    trial_used  INTEGER DEFAULT 0,
    total_paid  INTEGER DEFAULT 0,
    email       TEXT UNIQUE,
    is_banned   INTEGER DEFAULT 0,
    ban_reason  TEXT,
    created     INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS keys (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    remark   TEXT,
    uuid     TEXT,
    short_id TEXT,
    days     INTEGER NOT NULL,
    limit_ip INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS ref_bonus_claims (
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    claimed     INTEGER DEFAULT 0,
    UNIQUE(referrer_id, referred_id)
);
CREATE INDEX IF NOT EXISTS idx_rbc_referrer ON ref_bonus_claims(referrer_id);

-- Enhanced referral tracking
CREATE TABLE IF NOT EXISTS referral_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,  -- 'trial_bonus', 'payment_bonus', 'registration'
    days_awarded INTEGER NOT NULL,
    description TEXT,
    created INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referral_events_created ON referral_events(created);

-- YooKassa: idempotency guard — one row per payment_id, inserted before key delivery
CREATE TABLE IF NOT EXISTS yookassa_processed (
    payment_id  TEXT PRIMARY KEY,
    processed   INTEGER DEFAULT (strftime('%s','now'))
);

-- YooKassa: pending deliveries waiting for user to enter config name
CREATE TABLE IF NOT EXISTS yookassa_pending (
    payment_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    devices     INTEGER NOT NULL DEFAULT 1,
    amount_rub  INTEGER NOT NULL DEFAULT 0,
    created     INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_ykp_user ON yookassa_pending(user_id);

-- Новая партнёрская программа: 80₽ за первую оплату приглашённого
CREATE TABLE IF NOT EXISTS referral_balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    balance INTEGER DEFAULT 0,  -- накопленный баланс в рублях
    total_earned INTEGER DEFAULT 0,  -- всего заработано
    created INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_referral_balance_user ON referral_balance(user_id);

CREATE TABLE IF NOT EXISTS referral_payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending, completed, cancelled
    created INTEGER DEFAULT (strftime('%s','now')),
    processed INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_referral_payouts_user ON referral_payouts(user_id);

CREATE TABLE IF NOT EXISTS referral_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,  -- 80 рублей за первую оплату
    payment_id INTEGER,  -- ID платежа приглашённого
    created INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE(referrer_id, referred_id)  -- бонус начисляется только один раз
);
CREATE INDEX IF NOT EXISTS idx_referral_earnings_referrer ON referral_earnings(referrer_id);

-- Email authentication for existing clients
CREATE TABLE IF NOT EXISTS email_auth (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    code TEXT NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    expires_at INTEGER NOT NULL,
    used BOOLEAN DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_email_auth_user ON email_auth(user_id);
CREATE INDEX IF NOT EXISTS idx_email_auth_email ON email_auth(email);

-- Promo codes system
CREATE TABLE IF NOT EXISTS promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    promo_type TEXT NOT NULL DEFAULT 'percent',  -- 'percent', 'fixed_rub', 'free_days'
    discount_value INTEGER NOT NULL DEFAULT 10,  -- value based on type
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses_count INTEGER DEFAULT 0,
    tariff_binding INTEGER,  -- optional: bind to specific tariff (months)
    start_date INTEGER DEFAULT (strftime('%s','now')),
    expires_at INTEGER NOT NULL,
    is_active INTEGER DEFAULT 1,
    created INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_codes_active ON promo_codes(is_active);

CREATE TABLE IF NOT EXISTS promo_code_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    used_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (code) REFERENCES promo_codes(code),
    UNIQUE(code, user_id)
);
CREATE INDEX IF NOT EXISTS idx_promo_uses_code ON promo_code_uses(code);
CREATE INDEX IF NOT EXISTS idx_promo_uses_user ON promo_code_uses(user_id);

-- Key issuance error logging for admin panel tracking
CREATE TABLE IF NOT EXISTS key_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT,
    context TEXT,
    created INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_key_errors_user ON key_errors(user_id);
CREATE INDEX IF NOT EXISTS idx_key_errors_created ON key_errors(created);

-- Referral link clicks tracking
CREATE TABLE IF NOT EXISTS referral_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    clicked_at INTEGER DEFAULT (strftime('%s','now')),
    user_agent TEXT,
    ip_address TEXT
);
CREATE INDEX IF NOT EXISTS idx_referral_clicks_referrer ON referral_clicks(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referral_clicks_created ON referral_clicks(clicked_at);

-- Admin action logs
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    action_details TEXT,
    target_user_id INTEGER,
    created INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_logs_admin ON admin_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created);
"""


async def get_db() -> aiosqlite.Connection:
    """Get database connection from pool or create new one."""
    global _db_pool
    if _db_pool is None:
        _db_pool = await aiosqlite.connect(DB_FILE)
        await _db_pool.execute("PRAGMA journal_mode=WAL")
        await _db_pool.execute("PRAGMA synchronous=NORMAL")
        await _db_pool.execute("PRAGMA cache_size=20000")  # Increased cache
        await _db_pool.execute("PRAGMA temp_store=MEMORY")
        await _db_pool.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
    return _db_pool


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Run database migrations."""
    # Migration: add limit_ip column for existing databases
    try:
        await db.execute("ALTER TABLE keys ADD COLUMN limit_ip INTEGER NOT NULL DEFAULT 1")
        logger.info("Migration: added limit_ip column to keys table")
    except Exception:
        pass  # Column already exists

    # Migration: enhance payments table for detailed logging
    try:
        await db.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'success'")
        logger.info("Migration: added status column to payments table")
    except Exception:
        pass  # Column already exists

    try:
        await db.execute("ALTER TABLE payments ADD COLUMN tariff TEXT")
        logger.info("Migration: added tariff column to payments table")
    except Exception:
        pass  # Column already exists

    try:
        await db.execute("ALTER TABLE payments ADD COLUMN devices INTEGER DEFAULT 1")
        logger.info("Migration: added devices column to payments table")
    except Exception:
        pass  # Column already exists

    # Migration: create ref_bonus_claims table for existing databases
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ref_bonus_claims (
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                claimed     INTEGER DEFAULT 0,
                UNIQUE(referrer_id, referred_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rbc_referrer ON ref_bonus_claims(referrer_id)")
        logger.info("Migration: created ref_bonus_claims table")
    except Exception:
        pass  # Table already exists

    # Migration: create yookassa idempotency + pending tables
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS yookassa_processed ("
            "payment_id TEXT PRIMARY KEY, "
            "processed INTEGER DEFAULT (strftime('%s','now')))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS yookassa_pending ("
            "payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
            "days INTEGER NOT NULL, devices INTEGER NOT NULL DEFAULT 1, "
            "amount_rub INTEGER NOT NULL DEFAULT 0, "
            "created INTEGER DEFAULT (strftime('%s','now')))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ykp_user ON yookassa_pending(user_id)"
        )
    except Exception:
        pass

    # Migration: add short_id column to keys table
    try:
        await db.execute("ALTER TABLE keys ADD COLUMN short_id TEXT")
        logger.info("Migration: added short_id column to keys table")
    except Exception:
        pass  # Column already exists

    # Migration: add email column for existing databases
    try:
        await db.execute("ALTER TABLE users ADD COLUMN email TEXT UNIQUE")
        logger.info("Migration: added email column to users table")
    except Exception:
        pass  # Column already exists

    # Migration: add total_paid column for existing databases
    try:
        await db.execute("ALTER TABLE users ADD COLUMN total_paid INTEGER DEFAULT 0")
        logger.info("Migration: added total_paid column to users table")
        # Populate total_paid for existing users
        await db.execute("""
            UPDATE users SET total_paid = (
                SELECT COALESCE(SUM(amount), 0) 
                FROM payments 
                WHERE payments.user_id = users.user_id
            )
        """)
        logger.info("Migration: populated total_paid for existing users")
    except Exception:
        pass  # Column already exists

    # Migration: create refunds table for existing databases
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                method TEXT NOT NULL,
                reason TEXT NOT NULL,
                original_payload TEXT,
                refunded_by INTEGER NOT NULL,
                created INTEGER DEFAULT (strftime('%s','now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (refunded_by) REFERENCES users(user_id)
            )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_refunds_user ON refunds(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_refunds_created ON refunds(created)")
        logger.info("Migration: created refunds table")
    except Exception:
        pass

    # Migration: create new referral program tables
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_balance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                balance INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                created INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_balance_user ON referral_balance(user_id)")
        logger.info("Migration: created referral_balance table")
    except Exception:
        pass

    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created INTEGER DEFAULT (strftime('%s','now')),
                processed INTEGER,
                notes TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_payouts_user ON referral_payouts(user_id)")
        logger.info("Migration: created referral_payouts table")
    except Exception:
        pass

    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payment_id INTEGER,
                created INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(referrer_id, referred_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_earnings_referrer ON referral_earnings(referrer_id)")
        logger.info("Migration: created referral_earnings table")
    except Exception:
        pass

    # Migration: add email column to users table
    try:
        await db.execute("ALTER TABLE users ADD COLUMN email TEXT UNIQUE")
        logger.info("Migration: added email column to users table")
    except Exception:
        pass  # Column already exists

    # Migration: create email_auth table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS email_auth (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                expires_at INTEGER NOT NULL,
                used BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_auth_user ON email_auth(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_auth_email ON email_auth(email)")
        logger.info("Migration: created email_auth table")
    except Exception:
        pass

    # Migration: create referral_clicks table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                clicked_at INTEGER DEFAULT (strftime('%s','now')),
                user_agent TEXT,
                ip_address TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_clicks_referrer ON referral_clicks(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_clicks_created ON referral_clicks(clicked_at)")
        logger.info("Migration: created referral_clicks table")
    except Exception:
        pass

    # Migration: create admin_logs table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                action_details TEXT,
                target_user_id INTEGER,
                created INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_admin ON admin_logs(admin_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created)")
        logger.info("Migration: created admin_logs table")
    except Exception:
        pass

    # Migration: enhance promo_codes table with new columns
    try:
        await db.execute("ALTER TABLE promo_codes ADD COLUMN promo_type TEXT DEFAULT 'percent'")
        logger.info("Migration: added promo_type column to promo_codes")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE promo_codes ADD COLUMN discount_value INTEGER DEFAULT 10")
        logger.info("Migration: added discount_value column to promo_codes")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE promo_codes ADD COLUMN tariff_binding INTEGER")
        logger.info("Migration: added tariff_binding column to promo_codes")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE promo_codes ADD COLUMN start_date INTEGER")
        # Set default value for existing rows
        await db.execute("UPDATE promo_codes SET start_date = created WHERE start_date IS NULL")
        logger.info("Migration: added start_date column to promo_codes")
    except Exception as e:
        logger.info("Migration: start_date column may already exist: %s", e)

    # Migration: add user ban columns
    try:
        await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        logger.info("Migration: added is_banned column to users")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
        logger.info("Migration: added ban_reason column to users")
    except Exception:
        pass


async def init_db() -> None:
    """Initialize database with WAL mode and run migrations."""
    db = await get_db()

    # Create tables
    await db.executescript(_SCHEMA)

    # Run migrations
    await _run_migrations(db)

    await db.commit()
    logger.info("Database initialized: %s", DB_FILE)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def ensure_user(user_id: int) -> None:
    """Create user record if it does not exist."""
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (user_id,)
    )
    await db.commit()


async def get_referrer(user_id: int) -> Optional[int]:
    db = await get_db()
    cur = await db.execute(
        "SELECT referrer_id FROM users WHERE user_id=?", (user_id,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def set_referrer(user_id: int, referrer_id: int) -> None:
    """Set referrer only if not already set and not self-referral."""
    if user_id == referrer_id:
        return
    db = await get_db()
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


@cache_user_info
async def has_trial_used(user_id: int) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT trial_used FROM users WHERE user_id=?", (user_id,)
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def set_trial_used(user_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE users SET trial_used=1 WHERE user_id=?", (user_id,)
    )
    await db.commit()
    invalidate_user_cache(user_id)


async def reset_trial_for_user(user_id: int) -> None:
    """Reset trial usage for user (admin function)."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET trial_used=0 WHERE user_id=?", (user_id,)
    )
    await db.commit()
    # Invalidate ALL user cache entries multiple times to ensure fresh data
    invalidate_user_cache(user_id)


async def update_total_paid(user_id: int, amount: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE users SET total_paid = total_paid + ? WHERE user_id=?",
        (amount, user_id)
    )
    await db.commit()
    invalidate_user_cache(user_id)


async def get_user_stats(user_id: int) -> dict:
    db = await get_db()
    cur = await db.execute(
        "SELECT trial_used, total_paid, referrer_id, is_banned, ban_reason FROM users WHERE user_id=?",
        (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        return {"trial_used": False, "total_paid": 0, "referrer_id": None, "is_banned": False, "ban_reason": None}
    return {
        "trial_used": bool(row[0]),
        "total_paid": row[1] or 0,
        "referrer_id": row[2],
        "is_banned": bool(row[3]),
        "ban_reason": row[4]
    }


async def ban_user(user_id: int, reason: str = None) -> bool:
    """Ban a user."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET is_banned=1, ban_reason=? WHERE user_id=?",
            (reason, user_id)
        )
        await db.commit()
        invalidate_user_cache(user_id)
        return True
    except Exception as e:
        logger.error("Failed to ban user %d: %s", user_id, e)
        return False


async def unban_user(user_id: int) -> bool:
    """Unban a user."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET is_banned=0, ban_reason=NULL WHERE user_id=?",
            (user_id,)
        )
        await db.commit()
        invalidate_user_cache(user_id)
        return True
    except Exception as e:
        logger.error("Failed to unban user %d: %s", user_id, e)
        return False


async def add_manual_days(user_id: int, days: int, admin_id: int) -> bool:
    """Add manual days to user's latest active key or create new one."""
    db = await get_db()
    current_time = int(time.time())

    try:
        # Find user's latest key
        cur = await db.execute(
            "SELECT id, expiry FROM keys WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,)
        )
        row = await cur.fetchone()

        if row:
            # Extend existing key
            key_id, current_expiry = row
            new_expiry = max(current_expiry, current_time) + days * 86400
            await db.execute(
                "UPDATE keys SET expiry=? WHERE id=?",
                (new_expiry, key_id)
            )
        else:
            # Create new key - this will be handled by the calling code
            # For now, just return False to indicate need for key creation
            return False

        await db.commit()
        invalidate_subscription_cache(user_id)
        return True
    except Exception as e:
        logger.error("Failed to add manual days to user %d: %s", user_id, e)
        return False


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

async def add_key(
    user_id: int,
    key: str,
    remark: str,
    uuid: str,
    days: int,
    limit_ip: int = 1,
) -> int:
    db = await get_db()
    expiry = int(time.time()) + days * 86400
    cur = await db.execute(
        "INSERT INTO keys(user_id, key, remark, uuid, days, limit_ip, created, expiry) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id, key, remark, uuid, days, limit_ip, int(time.time()), expiry),
    )
    await db.commit()
    from cache import invalidate_user_cache, invalidate_subscription_cache
    invalidate_user_cache(user_id)
    invalidate_subscription_cache(user_id)
    return cur.lastrowid


async def get_user_keys(user_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, key, remark, uuid, short_id, days, limit_ip, created, expiry "
        "FROM keys WHERE user_id=? ORDER BY created DESC",
        (user_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "key": row[1],
            "remark": row[2],
            "uuid": row[3],
            "short_id": row[4],
            "days": row[5],
            "limit_ip": row[6],
            "created": row[7],
            "expiry": row[8],
        }
        for row in rows
    ]


async def update_key_remark(key_id: int, new_remark: str) -> bool:
    """Update key remark (name) in database."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE keys SET remark=? WHERE id=?",
            (new_remark, key_id)
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to update key remark: %s", e)
        return False


async def update_key_uuid(key_id: int, new_uuid: str) -> bool:
    """Update key uuid (VLESS key) in database."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE keys SET uuid=? WHERE id=?",
            (new_uuid, key_id)
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to update key uuid: %s", e)
        return False


async def get_key_by_uuid(uuid: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, user_id, key, remark, short_id, days, limit_ip, created, expiry "
        "FROM keys WHERE uuid=?",
        (uuid,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "key": row[2],
        "remark": row[3],
        "short_id": row[4],
        "days": row[5],
        "limit_ip": row[6],
        "created": row[7],
        "expiry": row[8],
    }


async def delete_key(key_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM keys WHERE id=?", (key_id,))
    await db.commit()
    return cur.rowcount > 0


async def delete_key_by_uuid(uuid: str) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM keys WHERE uuid=?", (uuid,))
    await db.commit()
    return cur.rowcount > 0


async def get_expired_keys() -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, user_id, uuid FROM keys WHERE expiry < ?",
        (int(time.time()),),
    )
    rows = await cur.fetchall()
    return [
        {"id": row[0], "user_id": row[1], "uuid": row[2]}
        for row in rows
    ]


async def cleanup_expired_keys() -> int:
    """Remove expired keys and return count of removed keys."""
    expired = await get_expired_keys()
    if not expired:
        return 0
    
    db = await get_db()
    for key in expired:
        await db.execute("DELETE FROM keys WHERE id=?", (key["id"],))
    await db.commit()
    return len(expired)


async def mark_keys_cleaned(key_ids: list[int]) -> int:
    """Mark keys as cleaned (soft delete). Returns number of keys marked."""
    if not key_ids:
        return 0
    
    db = await get_db()
    placeholders = ','.join('?' * len(key_ids))
    cur = await db.execute(
        f"UPDATE keys SET cleaned=1 WHERE id IN ({placeholders})",
        key_ids
    )
    await db.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

async def add_payment(
    user_id: int,
    amount: int,
    currency: str,
    method: str,
    days: int,
    payload: str = None,
    status: str = "success",
    tariff: str = None,
    devices: int = 1,
) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO payments(user_id, amount, currency, method, days, created, payload, status, tariff, devices) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, amount, currency, method, days, int(time.time()), payload, status, tariff, devices),
    )
    await db.commit()
    return cur.lastrowid


async def get_user_payments(user_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, amount, currency, method, days, created, payload, status, tariff, devices "
        "FROM payments WHERE user_id=? ORDER BY created DESC",
        (user_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "amount": row[1],
            "currency": row[2],
            "method": row[3],
            "days": row[4],
            "created": row[5],
            "payload": row[6],
            "status": row[7],
            "tariff": row[8],
            "devices": row[9],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

async def get_referrals(referrer_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT referred_id, created FROM referrals WHERE referrer_id=? ORDER BY created DESC",
        (referrer_id,),
    )
    rows = await cur.fetchall()
    return [
        {"referred_id": row[0], "created": row[1]}
        for row in rows
    ]


async def count_referrals(referrer_id: int) -> int:
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
        (referrer_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def add_referral_event(
    referrer_id: int,
    referred_id: int,
    event_type: str,
    days_awarded: int,
    description: str = None,
) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO referral_events(referrer_id, referred_id, event_type, days_awarded, description) "
        "VALUES(?,?,?,?,?)",
        (referrer_id, referred_id, event_type, days_awarded, description),
    )
    await db.commit()
    return cur.lastrowid


async def get_referral_events(referrer_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT referred_id, event_type, days_awarded, description, created "
        "FROM referral_events WHERE referrer_id=? ORDER BY created DESC",
        (referrer_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "referred_id": row[0],
            "event_type": row[1],
            "days_awarded": row[2],
            "description": row[3],
            "created": row[4],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# YooKassa
# ---------------------------------------------------------------------------

async def is_yookassa_processed(payment_id: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM yookassa_processed WHERE payment_id=?",
        (payment_id,),
    )
    row = await cur.fetchone()
    return row is not None


async def mark_yookassa_processed(payment_id: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO yookassa_processed(payment_id) VALUES(?)",
        (payment_id,),
    )
    await db.commit()


async def add_yookassa_pending(
    payment_id: str,
    user_id: int,
    days: int,
    devices: int = 1,
    amount_rub: int = 0,
) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO yookassa_pending(payment_id, user_id, days, devices, amount_rub) "
        "VALUES(?,?,?,?,?)",
        (payment_id, user_id, days, devices, amount_rub),
    )
    await db.commit()


async def get_yookassa_pending(payment_id: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, days, devices, amount_rub, created "
        "FROM yookassa_pending WHERE payment_id=?",
        (payment_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "days": row[1],
        "devices": row[2],
        "amount_rub": row[3],
        "created": row[4],
    }


async def delete_yookassa_pending(payment_id: str) -> None:
    db = await get_db()
    await db.execute(
        "DELETE FROM yookassa_pending WHERE payment_id=?",
        (payment_id,),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Ref Bonus Claims
# ---------------------------------------------------------------------------

async def can_claim_ref_bonus(referrer_id: int, referred_id: int) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT claimed FROM ref_bonus_claims WHERE referrer_id=? AND referred_id=?",
        (referrer_id, referred_id),
    )
    row = await cur.fetchone()
    return row is None or row[0] == 0


async def mark_ref_bonus_claimed(referrer_id: int, referred_id: int) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO ref_bonus_claims(referrer_id, referred_id, claimed) VALUES(?,?,1)",
        (referrer_id, referred_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

async def add_refund(
    user_id: int,
    amount: int,
    currency: str,
    method: str,
    reason: str,
    original_payload: str = None,
    refunded_by: int = None,
) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO refunds(user_id, amount, currency, method, reason, original_payload, refunded_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, amount, currency, method, reason, original_payload, refunded_by),
    )
    await db.commit()
    return cur.lastrowid


async def get_user_refunds(user_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, amount, currency, method, reason, original_payload, refunded_by, created "
        "FROM refunds WHERE user_id=? ORDER BY created DESC",
        (user_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "amount": row[1],
            "currency": row[2],
            "method": row[3],
            "reason": row[4],
            "original_payload": row[5],
            "refunded_by": row[6],
            "created": row[7],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Payment functions - using direct implementations
# Key functions - using direct implementations
# Referral functions - using direct implementations
# Trial functions - using direct implementations
@cache_user_info
async def has_active_subscription(user_id: int) -> bool:
    """Check if user has any non-expired key."""
    db = await get_db()
    current_time = int(time.time())
    cur = await db.execute(
        "SELECT COUNT(*) FROM keys WHERE user_id=? AND expiry > ?",
        (user_id, current_time),
    )
    count = (await cur.fetchone())[0]
    return count > 0


@cache_user_info
async def has_ever_had_key(user_id: int) -> bool:
    """Check if user ever had any key (including expired)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM keys WHERE user_id=?",
        (user_id,),
    )
    count = (await cur.fetchone())[0]
    return count > 0

@cache_user_info
async def get_user_active_keys(user_id: int) -> list[dict]:
    """Get user's active (non-expired) keys"""
    db = await get_db()
    current_time = int(time.time())
    cur = await db.execute(
        "SELECT id, key, remark, uuid, short_id, days, limit_ip, created, expiry "
        "FROM keys WHERE user_id=? AND expiry > ? ORDER BY created DESC",
        (user_id, current_time)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "key": row[1],
            "remark": row[2],
            "uuid": row[3],
            "short_id": row[4],
            "days": row[5],
            "limit_ip": row[6],
            "created": row[7],
            "expiry": row[8],
        }
        for row in rows
    ]


async def get_referral_stats(referrer_id: int) -> dict:
    """Get referral statistics for a user"""
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
        (referrer_id,)
    )
    total = (await cur.fetchone())[0]
    
    # Count paying referrals (those with payment_bonus events)
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT referred_id) FROM referral_events 
        WHERE referrer_id=? AND event_type='payment_bonus'
        """,
        (referrer_id)
    )
    paid = (await cur.fetchone())[0]
    
    return {
        "total": total,
        "paid": paid,
        "total_referrals": total,  # backward compatibility
        "bonuses_given": total  # backward compatibility
    }


@cache_user_info
async def has_active_subscription(user_id: int) -> bool:
    """Check if user has active subscription"""
    async with _db_semaphore:
        active_keys = await get_user_active_keys(user_id)
        return len(active_keys) > 0


@cache_user_info
async def has_paid_subscription(user_id: int) -> bool:
    """Check if user has ever paid for subscription"""
    async with _db_semaphore:
        db = await get_db()
        cur = await db.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND method!='trial' LIMIT 1",
            (user_id,)
        )
        row = await cur.fetchone()
        return row is not None


async def try_claim_trial(user_id: int) -> bool:
    """Try to claim trial - returns True if successful"""
    if await has_trial_used(user_id):
        return False
    await set_trial_used(user_id)
    return True


async def get_all_payments(limit: int = 50, offset: int = 0, method: str = None) -> list[dict]:
    """Get all payments with pagination and optional method filtering."""
    db = await get_db()
    
    if method:
        cur = await db.execute(
            "SELECT id, user_id, amount, currency, method, days, created, payload, status, tariff, devices "
            "FROM payments WHERE method = ? ORDER BY created DESC LIMIT ? OFFSET ?",
            (method, limit, offset)
        )
    else:
        cur = await db.execute(
            "SELECT id, user_id, amount, currency, method, days, created, payload, status, tariff, devices "
            "FROM payments ORDER BY created DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
    
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "user_id": row[1],
            "amount": row[2],
            "currency": row[3],
            "method": row[4],
            "days": row[5],
            "created": row[6],
            "payload": row[7],
            "status": row[8],
            "tariff": row[9],
            "devices": row[10],
        }
        for row in rows
    ]


async def get_key_by_id(key_id: int) -> Optional[dict]:
    """Get key by database ID"""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, user_id, key, remark, uuid, short_id, days, limit_ip, created, expiry "
        "FROM keys WHERE id=?",
        (key_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "key": row[2],
        "remark": row[3],
        "uuid": row[4],
        "short_id": row[5],
        "days": row[6],
        "limit_ip": row[7],
        "created": row[8],
        "expiry": row[9],
    }


async def delete_key_by_id(key_id: int) -> bool:
    """Delete key by database ID"""
    return await delete_key(key_id)


# ---------------------------------------------------------------------------
# Новая партнёрская программа: 80₽ за первую оплату
# ---------------------------------------------------------------------------

async def ensure_referral_balance(user_id: int) -> None:
    """Создать запись о балансе реферала если её нет"""
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO referral_balance(user_id) VALUES(?)",
        (user_id,)
    )
    await db.commit()

async def get_referral_balance(user_id: int) -> dict:
    """Получить баланс реферала"""
    await ensure_referral_balance(user_id)
    db = await get_db()
    cur = await db.execute(
        "SELECT balance, total_earned FROM referral_balance WHERE user_id=?",
        (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        return {"balance": 0, "total_earned": 0}
    return {"balance": row[0], "total_earned": row[1]}

async def add_referral_earning(referrer_id: int, referred_id: int, amount: int = 50, payment_id: int = None) -> bool:
    """Начислить бонус за первую оплату приглашённого (80₽)"""
    db = await get_db()
    try:
        # Проверяем что бонус ещё не начислялся
        cur = await db.execute(
            "SELECT 1 FROM referral_earnings WHERE referrer_id=? AND referred_id=?",
            (referrer_id, referred_id)
        )
        if await cur.fetchone():
            return False  # бонус уже начислялся
        
        # Начисляем бонус с статусом pending
        await db.execute(
            "INSERT INTO referral_earnings(referrer_id, referred_id, amount, payment_id, payment_status) VALUES(?,?,?,?,?)",
            (referrer_id, referred_id, amount, payment_id, 'pending')
        )
        
        # Обновляем баланс реферала
        await ensure_referral_balance(referrer_id)
        await db.execute(
            "UPDATE referral_balance SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id=?",
            (amount, amount, referrer_id)
        )
        
        await db.commit()
        return True
    except Exception as e:
        logger.error("Error adding referral earning: %s", e)
        await db.rollback()
        return False


async def get_referral_stats_detailed(referrer_id: int = None) -> list:
    """Получить детальную статистику по рефералам с источниками и оплатами"""
    db = await get_db()
    
    if referrer_id:
        # Статистика для конкретного реферера
        query = """
            SELECT r.referrer_id, r.referred_id, r.source, r.created,
                   re.amount, re.payment_status, re.created as payment_date,
                   p.amount as payment_amount, p.created as payment_created
            FROM referrals r
            LEFT JOIN referral_earnings re ON r.referrer_id = re.referrer_id AND r.referred_id = re.referred_id
            LEFT JOIN payments p ON re.payment_id = p.id
            WHERE r.referrer_id = ?
            ORDER BY r.created DESC
        """
        cur = await db.execute(query, (referrer_id,))
    else:
        # Статистика для всех рефереров
        query = """
            SELECT r.referrer_id, r.referred_id, r.source, r.created,
                   re.amount, re.payment_status, re.created as payment_date,
                   p.amount as payment_amount, p.created as payment_created
            FROM referrals r
            LEFT JOIN referral_earnings re ON r.referrer_id = re.referrer_id AND r.referred_id = re.referred_id
            LEFT JOIN payments p ON re.payment_id = p.id
            ORDER BY r.created DESC
        """
        cur = await db.execute(query)
    
    rows = await cur.fetchall()
    
    stats = []
    for row in rows:
        stats.append({
            "referrer_id": row[0],
            "referred_id": row[1],
            "source": row[2],
            "referral_date": row[3],
            "bonus_amount": row[4],
            "payment_status": row[5],
            "bonus_date": row[6],
            "payment_amount": row[7],
            "payment_date": row[8]
        })
    
    return stats


async def update_referral_payment_status(earning_id: int, status: str) -> bool:
    """Обновить статус выплаты реферала"""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE referral_earnings SET payment_status=? WHERE id=?",
            (status, earning_id)
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Error updating referral payment status: %s", e)
        await db.rollback()
        return False

async def can_claim_payout(user_id: int, amount: int) -> bool:
    """Проверить можно ли вывести указанную сумму (минимум 400₽, кратно 80₽)"""
    if amount < 400 or amount % 80 != 0:
        return False
    
    balance_info = await get_referral_balance(user_id)
    return balance_info["balance"] >= amount

async def create_payout_request(user_id: int, amount: int) -> int:
    """Создать заявку на вывод средств"""
    if not await can_claim_payout(user_id, amount):
        raise ValueError("Invalid payout amount")
    
    db = await get_db()
    # Списываем средства с баланса
    await db.execute(
        "UPDATE referral_balance SET balance = balance - ? WHERE user_id=?",
        (amount, user_id)
    )
    
    # Создаем заявку на вывод
    cur = await db.execute(
        "INSERT INTO referral_payouts(user_id, amount, status) VALUES(?,?,?)",
        (user_id, amount, "pending")
    )
    await db.commit()
    return cur.lastrowid

async def get_referral_stats(user_id: int) -> dict:
    """Получить статистику реферала"""
    db = await get_db()
    
    # Количество приглашённых
    cur = await db.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
        (user_id,)
    )
    total_referrals = (await cur.fetchone())[0]
    
    # Количество оплативших (начисленные бонусы)
    cur = await db.execute(
        "SELECT COUNT(*) FROM referral_earnings WHERE referrer_id=?",
        (user_id,)
    )
    paid_referrals = (await cur.fetchone())[0]
    
    # Баланс
    balance_info = await get_referral_balance(user_id)
    
    # Общая сумма заработанного
    total_earned = balance_info["total_earned"]
    
    return {
        "total_referrals": total_referrals,
        "paid_referrals": paid_referrals,
        "balance": balance_info["balance"],
        "total_earned": total_earned,
        "can_withdraw": balance_info["balance"] >= 400
    }


async def log_referral_click(referrer_id: int, user_agent: str = None, ip_address: str = None) -> int:
    """Log a referral link click"""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO referral_clicks(referrer_id, user_agent, ip_address) VALUES(?,?,?)",
        (referrer_id, user_agent, ip_address)
    )
    await db.commit()
    return cur.lastrowid


async def get_referral_clicks_count(referrer_id: int) -> int:
    """Get total number of referral link clicks"""
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM referral_clicks WHERE referrer_id=?",
        (referrer_id,)
    )
    return (await cur.fetchone())[0]


async def get_referral_stats_enhanced(referrer_id: int) -> dict:
    """Get enhanced referral statistics with detailed tracking"""
    db = await get_db()
    current_time = int(time.time())
    
    # Total clicks on referral link
    cur = await db.execute(
        "SELECT COUNT(*) FROM referral_clicks WHERE referrer_id=?",
        (referrer_id,)
    )
    total_clicks = (await cur.fetchone())[0]
    
    # Total registrations (users who registered via referral)
    cur = await db.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
        (referrer_id,)
    )
    total_registrations = (await cur.fetchone())[0]
    
    # Active clients (users with active subscriptions)
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        INNER JOIN keys k ON r.referred_id = k.user_id
        WHERE r.referrer_id=? AND k.expiry > ?
        """,
        (referrer_id, current_time)
    )
    active_clients = (await cur.fetchone())[0]
    
    # Paid clients (users who made at least one payment)
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        INNER JOIN payments p ON r.referred_id = p.user_id
        WHERE r.referrer_id=? AND p.method != 'trial'
        """,
        (referrer_id,)
    )
    paid_clients = (await cur.fetchone())[0]
    
    # Total bonus days earned from referral_events
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(days_awarded), 0)
        FROM referral_events
        WHERE referrer_id=?
        """,
        (referrer_id,)
    )
    total_bonus_days = (await cur.fetchone())[0]
    
    # Balance info
    balance_info = await get_referral_balance(user_id=referrer_id)
    
    return {
        "total_clicks": total_clicks,
        "total_registrations": total_registrations,
        "active_clients": active_clients,
        "paid_clients": paid_clients,
        "total_bonus_days": total_bonus_days,
        "balance_rub": balance_info["balance"],
        "total_earned_rub": balance_info["total_earned"],
    }


async def get_referred_users_list(referrer_id: int) -> list[dict]:
    """Get list of referred users with their status"""
    db = await get_db()
    current_time = int(time.time())
    
    cur = await db.execute(
        """
        SELECT r.referred_id, r.created,
               (SELECT COUNT(*) FROM keys WHERE user_id=r.referred_id AND expiry > ?) as active_keys,
               (SELECT COUNT(*) FROM payments WHERE user_id=r.referred_id AND method != 'trial') as payment_count,
               (SELECT COALESCE(SUM(days_awarded), 0) FROM referral_events 
                WHERE referrer_id=? AND referred_id=r.referred_id) as bonus_days
        FROM referrals r
        WHERE r.referrer_id=?
        ORDER BY r.created DESC
        """,
        (current_time, referrer_id, referrer_id)
    )
    
    rows = await cur.fetchall()
    return [
        {
            "user_id": row[0],
            "registration_date": row[1],
            "is_active": row[2] > 0,
            "has_paid": row[3] > 0,
            "bonus_days_awarded": row[4],
        }
        for row in rows
    ]

# ---------------------------------------------------------------------------
# Admin Functions
# ---------------------------------------------------------------------------

async def get_admin_stats() -> dict:
    """Get basic admin statistics"""
    db = await get_db()
    current_time = int(time.time())
    
    # Get total users
    cur = await db.execute("SELECT COUNT(*) FROM users")
    total_users = (await cur.fetchone())[0]
    
    # Get active users (with active keys)
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ?", (current_time,))
    active_users = (await cur.fetchone())[0]
    
    # Get active keys
    cur = await db.execute("SELECT COUNT(*) FROM keys WHERE expiry > ?", (current_time,))
    active_keys = (await cur.fetchone())[0]
    
    # Get total payments
    cur = await db.execute("SELECT COUNT(*) FROM payments WHERE method!='trial'")
    total_payments = (await cur.fetchone())[0]
    
    # Get total revenue
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE method!='trial'")
    total_revenue = (await cur.fetchone())[0]
    
    # Today's revenue
    today_start = current_time - (current_time % 86400)
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE method!='trial' AND created >= ?", (today_start,))
    today_revenue = (await cur.fetchone())[0]
    
    # Week revenue
    week_start = current_time - ((current_time // 86400) % 7) * 86400
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE method!='trial' AND created >= ?", (week_start,))
    week_revenue = (await cur.fetchone())[0]
    
    # Month revenue
    month_start = current_time - ((current_time // 86400) % 30) * 86400
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE method!='trial' AND created >= ?", (month_start,))
    month_revenue = (await cur.fetchone())[0]
    
    # Total referrals
    cur = await db.execute("SELECT COUNT(*) FROM referrals")
    total_referrals = (await cur.fetchone())[0]
    
    return {
        "total_users": total_users,
        "active_users": active_users,
        "active_keys": active_keys,
        "total_payments": total_payments,
        "total_revenue": total_revenue,
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "total_referrals": total_referrals
    }


async def get_extended_stats() -> dict:
    """Get extended admin statistics"""
    basic_stats = await get_admin_stats()
    
    db = await get_db()
    current_time = int(time.time())
    
    # Get trial users
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE trial_used=1")
    trial_users = (await cur.fetchone())[0]
    
    # Get expired keys
    cur = await db.execute("SELECT COUNT(*) FROM keys WHERE expiry <= ?", (current_time,))
    expired_keys = (await cur.fetchone())[0]
    
    # Get referrals count
    cur = await db.execute("SELECT COUNT(*) FROM referrals")
    total_referrals = (await cur.fetchone())[0]
    
    # Get top referrers
    cur = await db.execute("""
        SELECT referrer_id, COUNT(*) as count 
        FROM referrals 
        GROUP BY referrer_id 
        ORDER BY count DESC 
        LIMIT 5
    """)
    top_refs = []
    for row in await cur.fetchall():
        top_refs.append({
            'user_id': row[0],
            'count': row[1]
        })
    
    # New users stats
    day_start = current_time - (current_time % 86400)
    week_start = current_time - ((current_time // 86400) % 7) * 86400
    month_start = current_time - ((current_time // 86400) % 30) * 86400
    
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE created >= ?", (day_start,))
    new_day = (await cur.fetchone())[0]
    
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE created >= ?", (week_start,))
    new_week = (await cur.fetchone())[0]
    
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE created >= ?", (month_start,))
    new_month = (await cur.fetchone())[0]
    
    # Active users by period
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ? AND expiry <= ?", (current_time, current_time + 24*86400))
    active_24h = (await cur.fetchone())[0]
    
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ?", (current_time,))
    active_7d = (await cur.fetchone())[0]
    
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ?", (current_time,))
    active_30d = (await cur.fetchone())[0]
    
    return {
        **basic_stats,
        "trial_users": trial_users,
        "expired_keys": expired_keys,
        "total_referrals": total_referrals,
        "top_refs": top_refs,
        "new_day": new_day,
        "new_week": new_week,
        "new_month": new_month,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "active_30d": active_30d
    }


async def get_all_user_ids() -> list[int]:
    """Get all user IDs"""
    db = await get_db()
    cur = await db.execute("SELECT user_id FROM users")
    rows = await cur.fetchall()
    return [row[0] for row in rows]


async def get_users_count() -> int:
    """Get total users count"""
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM users")
    return (await cur.fetchone())[0]


async def find_user_by_id(user_id: int) -> Optional[dict]:
    """Find user by ID"""
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, referrer_id, trial_used, total_paid, created FROM users WHERE user_id=?",
        (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "referrer_id": row[1],
        "trial_used": bool(row[2]),
        "total_paid": row[3] or 0,
        "created": row[4]
    }


async def delete_user_and_keys(user_id: int) -> list:
    """Delete user and all their keys, return list of deleted UUIDs"""
    db = await get_db()
    # Get UUIDs before deleting
    cur = await db.execute("SELECT uuid FROM keys WHERE user_id=?", (user_id,))
    rows = await cur.fetchall()
    uuids = [row[0] for row in rows if row[0]]
    
    await db.execute("DELETE FROM keys WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    await db.commit()
    return uuids


async def set_key_days(key_id: int, days: int) -> bool:
    """Extend key to specified days in both DB and 3x-UI panel"""
    db = await get_db()
    expiry = int(time.time()) + days * 86400
    
    # Get UUID before updating
    cur = await db.execute("SELECT uuid FROM keys WHERE id=?", (key_id,))
    row = await cur.fetchone()
    client_uuid = row[0] if row else None
    
    # Update database
    cur = await db.execute(
        "UPDATE keys SET days=?, expiry=? WHERE id=?",
        (days, expiry, key_id)
    )
    await db.commit()
    
    if cur.rowcount == 0:
        return False
    
    # Sync to 3x-ui panel if user_id exists
    if user_id:
        try:
            from marzban_client import update_marzban_user_expiry
            success = await update_marzban_user_expiry(user_id, days)
            if not success:
                logger.warning("Failed to sync set_key_days to 3x-ui for key %d", key_id)
        except Exception as e:
            logger.error("Error syncing set_key_days to 3x-ui for key %d: %s", key_id, e)
    
    return True


async def get_all_users() -> list[dict]:
    """Get all users with basic info"""
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, trial_used, total_paid, created FROM users ORDER BY created DESC"
    )
    rows = await cur.fetchall()
    return [
        {
            "user_id": row[0],
            "trial_used": bool(row[1]),
            "total_paid": row[2] or 0,
            "created": row[3]
        }
        for row in rows
    ]


async def get_all_users_paginated(limit: int = 50, offset: int = 0) -> list[dict]:
    """Get users with pagination including key counts"""
    db = await get_db()
    current_time = int(time.time())
    
    # Get users with key counts via subquery
    cur = await db.execute(
        """
        SELECT 
            u.user_id, 
            u.trial_used, 
            u.total_paid, 
            u.created,
            COUNT(k.id) as total_keys,
            SUM(CASE WHEN k.expiry > ? THEN 1 ELSE 0 END) as active_keys
        FROM users u
        LEFT JOIN keys k ON u.user_id = k.user_id
        GROUP BY u.user_id
        ORDER BY u.created DESC
        LIMIT ? OFFSET ?
        """,
        (current_time, limit, offset)
    )
    rows = await cur.fetchall()
    return [
        {
            "user_id": row[0],
            "trial_used": bool(row[1]),
            "total_paid": row[2] or 0,
            "created": row[3],
            "total_keys": row[4] or 0,
            "active_keys": row[5] or 0
        }
        for row in rows
    ]


async def get_payment_stats() -> dict:
    """Get payment statistics"""
    db = await get_db()
    current_time = int(time.time())
    
    # Total payments
    cur = await db.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE status='success'")
    total_count, total_sum = await cur.fetchone()
    
    # Today's payments
    today_start = current_time - (current_time % 86400)
    cur = await db.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE status='success' AND created >= ?", (today_start,))
    today_count, today_sum = await cur.fetchone()
    
    # This month payments
    month_start = current_time - ((current_time // 86400) % 30) * 86400
    cur = await db.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE status='success' AND created >= ?", (month_start,))
    month_count, month_sum = await cur.fetchone()
    
    # By method
    cur = await db.execute("SELECT method, COUNT(*), SUM(amount) FROM payments WHERE status='success' GROUP BY method")
    by_method = {}
    for row in await cur.fetchall():
        by_method[row[0]] = {
            "count": row[1],
            "sum": row[2] or 0
        }
    
    return {
        "total": {
            "count": total_count or 0,
            "sum": total_sum or 0
        },
        "today": {
            "count": today_count or 0,
            "sum": today_sum or 0
        },
        "month": {
            "count": month_count or 0,
            "sum": month_sum or 0
        },
        "by_method": by_method
    }


async def extend_key(key_id: int, additional_days: int) -> bool:
    """Extend key by additional days in both DB and 3x-UI panel"""
    db = await get_db()
    cur = await db.execute("SELECT expiry, uuid FROM keys WHERE id=?", (key_id,))
    row = await cur.fetchone()
    if not row:
        logger.warning("Key %d not found for extension", key_id)
        return False
    
    current_expiry, client_uuid = row
    new_expiry = current_expiry + additional_days * 86400
    
    # Update database
    cur = await db.execute(
        "UPDATE keys SET expiry=? WHERE id=?",
        (new_expiry, key_id)
    )
    await db.commit()
    
    if cur.rowcount == 0:
        logger.warning("Failed to update expiry in DB for key %d", key_id)
        return False
    
    logger.info("Extended key %d by %d days (expiry: %s -> %s)", key_id, additional_days, current_expiry, new_expiry)
    
    return True


async def get_all_refunds() -> list[dict]:
    """Get all refunds"""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, user_id, amount, currency, method, reason, refunded_by, created "
        "FROM refunds ORDER BY created DESC"
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "user_id": row[1],
            "amount": row[2],
            "currency": row[3],
            "method": row[4],
            "reason": row[5],
            "refunded_by": row[6],
            "created": row[7]
        }
        for row in rows
    ]


async def get_refund_stats() -> dict:
    """Get refund statistics"""
    db = await get_db()
    current_time = int(time.time())
    
    # Total refunds
    cur = await db.execute("SELECT COUNT(*) FROM refunds")
    total_refunds = (await cur.fetchone())[0]
    
    # Total refunded amount
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds")
    total_refunded = (await cur.fetchone())[0]
    
    # Last 30 days refunds
    thirty_days_ago = current_time - 30 * 86400
    cur = await db.execute("SELECT COUNT(*) FROM refunds WHERE created >= ?", (thirty_days_ago,))
    count_30d = (await cur.fetchone())[0]
    
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE created >= ?", (thirty_days_ago,))
    sum_30d = (await cur.fetchone())[0]
    
    return {
        "count_total": total_refunds,
        "sum_total": total_refunded,
        "count_30d": count_30d,
        "sum_30d": sum_30d
    }


async def get_all_users_csv() -> str:
    """Get all users in CSV format"""
    users = await get_all_users()
    lines = ["user_id,trial_used,total_paid,created"]
    
    for user in users:
        lines.append(f"{user['user_id']},{user['trial_used']},{user['total_paid']},{user['created']}")
    
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Expiry Notifications
# ---------------------------------------------------------------------------

async def get_keys_nearing_expiry(days_min: int = 1, days_max: int = 3) -> list[dict]:
    """Get keys that will expire in the specified day range."""
    db = await get_db()
    current_time = int(time.time())
    min_expiry = current_time + days_min * 86400
    max_expiry = current_time + days_max * 86400

    cur = await db.execute(
        """
        SELECT DISTINCT user_id, expiry
        FROM keys
        WHERE expiry BETWEEN ? AND ?
        AND expiry > ?
        ORDER BY expiry ASC
        """,
        (min_expiry, max_expiry, current_time)
    )
    rows = await cur.fetchall()
    return [
        {"user_id": row[0], "expiry": row[1]}
        for row in rows
    ]


async def get_all_keys_paginated(limit: int = 20, offset: int = 0) -> list[dict]:
    """Get all keys with user info for admin panel."""
    db = await get_db()
    current_time = int(time.time())

    cur = await db.execute(
        """
        SELECT k.id, k.user_id, k.remark, k.uuid, k.short_id, k.days, k.limit_ip,
               k.created, k.expiry, u.trial_used, u.total_paid
        FROM keys k
        LEFT JOIN users u ON k.user_id = u.user_id
        ORDER BY k.created DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "user_id": row[1],
            "remark": row[2],
            "uuid": row[3],
            "short_id": row[4],
            "days": row[5],
            "limit_ip": row[6],
            "created": row[7],
            "expiry": row[8],
            "trial_used": row[9],
            "total_paid": row[10],
            "is_active": row[8] > current_time if row[8] else False,
        }
        for row in rows
    ]


async def get_keys_count() -> int:
    """Get total keys count."""
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM keys")
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_all_referral_stats() -> dict:
    """Get all referral statistics"""
    db = await get_db()
    cur = await db.execute(
        "SELECT referrer_id, COUNT(*) as total_referrals "
        "FROM referrals GROUP BY referrer_id"
    )
    rows = await cur.fetchall()
    return {row[0]: {"total_referrals": row[1]} for row in rows}


async def cleanup_expired_keys_report() -> dict:
    """Generate cleanup report for expired keys"""
    removed_count = await cleanup_expired_keys()
    return {
        "removed_count": removed_count,
        "timestamp": int(time.time())
    }


async def get_all_users_csv() -> str:
    """Generate CSV export of all users"""
    import csv
    import io

    users = await get_all_users()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        'user_id', 'created', 'trial_used', 'total_paid'
    ])

    # Data rows
    for user in users:
        writer.writerow([
            user['user_id'],
            user['created'],
            int(user['trial_used']),
            user['total_paid']
        ])

    return output.getvalue()


# ---------------------------------------------------------------------------
# Email Authentication
# ---------------------------------------------------------------------------

async def get_user_by_email(email: str) -> Optional[dict]:
    """Find user by email address."""
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, email, trial_used, total_paid FROM users WHERE email=?",
        (email.lower().strip(),)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "email": row[1],
        "trial_used": bool(row[2]),
        "total_paid": row[3] or 0
    }


async def save_email_auth_code(user_id: int, email: str, code: str, expires_at: int) -> int:
    """Save email authentication code."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO email_auth(user_id, email, code, expires_at) VALUES(?,?,?,?)",
        (user_id, email.lower().strip(), code, expires_at)
    )
    await db.commit()
    return cur.lastrowid


async def verify_email_auth_code(user_id: int, code: str) -> bool:
    """Verify email authentication code."""
    db = await get_db()
    current_time = int(time.time())
    
    cur = await db.execute(
        """SELECT id FROM email_auth 
           WHERE user_id=? AND code=? AND expires_at > ? AND used=0
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, code, current_time)
    )
    row = await cur.fetchone()
    
    if row:
        # Mark code as used
        await db.execute(
            "UPDATE email_auth SET used=1 WHERE id=?",
            (row[0],)
        )
        await db.commit()
        return True
    return False


async def link_telegram_to_user(user_id: int, email: str) -> bool:
    """Link Telegram user_id to existing user account by email."""
    db = await get_db()
    try:
        # Update user's email
        await db.execute(
            "UPDATE users SET email=? WHERE user_id=?",
            (email.lower().strip(), user_id)
        )
        await db.commit()
        invalidate_user_cache(user_id)
        return True
    except Exception as e:
        logger.error("Error linking telegram to user: %s", e)
        return False


async def update_user_email(user_id: int, email: str) -> bool:
    """Update user's email address."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET email=? WHERE user_id=?",
            (email.lower().strip(), user_id)
        )
        await db.commit()
        invalidate_user_cache(user_id)
        return True
    except Exception as e:
        logger.error("Error updating user email: %s", e)
        return False


# ---------------------------------------------------------------------------
# Promo Codes
# ---------------------------------------------------------------------------

async def create_promo_code(
    code: str,
    promo_type: str = "percent",
    discount_value: int = 10,
    max_uses: int = 1,
    valid_days: int = 30,
    tariff_binding: int = None,
    start_date: int = None
) -> bool:
    """Create new promo code with enhanced options."""
    db = await get_db()
    current_time = int(time.time())
    if start_date is None:
        start_date = current_time
    expires_at = current_time + valid_days * 86400

    try:
        await db.execute(
            "INSERT INTO promo_codes(code, promo_type, discount_value, max_uses, uses_count, tariff_binding, start_date, expires_at, is_active, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code.upper(), promo_type, discount_value, max_uses, 0, tariff_binding, start_date, expires_at, 1, current_time)
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to create promo code: %s", e)
        return False


async def validate_promo_code(code: str, tariff_months: int = None) -> Optional[dict]:
    """Validate promo code and return info if valid."""
    db = await get_db()
    current_time = int(time.time())

    cur = await db.execute(
        "SELECT code, promo_type, discount_value, max_uses, uses_count, tariff_binding, start_date, expires_at "
        "FROM promo_codes WHERE code=? AND is_active=1",
        (code.upper(),)
    )
    row = await cur.fetchone()

    if not row:
        return None

    # Check if not started yet
    if row[6] > current_time:
        return None

    # Check if expired
    if row[7] < current_time:
        return None

    # Check if max uses reached
    if row[3] <= row[4]:
        return None

    # Check tariff binding if specified
    if row[5] is not None and tariff_months is not None:
        if row[5] != tariff_months:
            return None

    return {
        "code": row[0],
        "promo_type": row[1],
        "discount_value": row[2],
        "max_uses": row[3],
        "uses_count": row[4],
        "tariff_binding": row[5],
        "start_date": row[6],
        "expires_at": row[7],
    }


async def use_promo_code(code: str, user_id: int) -> bool:
    """Mark promo code as used by user."""
    db = await get_db()
    current_time = int(time.time())
    
    try:
        # Check if user already used this code
        cur = await db.execute(
            "SELECT 1 FROM promo_code_uses WHERE code=? AND user_id=?",
            (code.upper(), user_id)
        )
        if await cur.fetchone():
            return False  # Already used
        
        # Record usage
        await db.execute(
            "INSERT INTO promo_code_uses(code, user_id, used_at) VALUES(?,?,?)",
            (code.upper(), user_id, current_time)
        )
        
        # Increment uses count
        await db.execute(
            "UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code=?",
            (code.upper(),)
        )
        
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to use promo code: %s", e)
        return False

async def get_all_promo_codes() -> list[dict]:
    """Get all promo codes with usage stats."""
    db = await get_db()
    current_time = int(time.time())

    try:
        # Try the full query with new columns
        cur = await db.execute(
            "SELECT code, promo_type, discount_value, max_uses, uses_count, tariff_binding, start_date, expires_at, is_active, created "
            "FROM promo_codes ORDER BY created DESC"
        )
        rows = await cur.fetchall()

        return [
            {
                "code": row[0],
                "promo_type": row[1],
                "discount_value": row[2],
                "max_uses": row[3],
                "uses_count": row[4],
                "tariff_binding": row[5],
                "start_date": row[6],
                "expires_at": row[7],
                "is_active": row[8],
                "created": row[9],
            }
            for row in rows
        ]
    except Exception as e:
        # Fallback to simpler query if columns don't exist
        logger.warning("Using fallback query for promo_codes: %s", e)
        cur = await db.execute(
            "SELECT code, discount_percent, max_uses, uses_count, expires_at, is_active, created "
            "FROM promo_codes ORDER BY created DESC"
        )
        rows = await cur.fetchall()

        return [
            {
                "code": row[0],
                "promo_type": "percent",
                "discount_value": row[1],
                "max_uses": row[2],
                "uses_count": row[3],
                "tariff_binding": None,
                "start_date": row[6],
                "expires_at": row[4],
                "is_active": row[5],
                "created": row[6],
            }
            for row in rows
        ]


async def delete_promo_code(code: str) -> bool:
    """Delete promo code."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM promo_codes WHERE code=?", (code.upper(),))
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to delete promo code: %s", e)
        return False


async def extend_promo_code(code: str, additional_days: int) -> bool:
    """Extend promo code validity by additional days."""
    db = await get_db()
    try:
        current_time = int(time.time())
        await db.execute(
            "UPDATE promo_codes SET expires_at = expires_at + ? WHERE code=?",
            (additional_days * 86400, code.upper())
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to extend promo code: %s", e)
        return False


async def update_promo_max_uses(code: str, new_max_uses: int) -> bool:
    """Update promo code max uses limit."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE promo_codes SET max_uses = ? WHERE code=?",
            (new_max_uses, code.upper())
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to update promo code max uses: %s", e)
        return False


async def toggle_promo_active(code: str, is_active: bool) -> bool:
    """Activate or deactivate promo code."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE promo_codes SET is_active = ? WHERE code=?",
            (1 if is_active else 0, code.upper())
        )
        await db.commit()
        return True
    except Exception as e:
        logger.error("Failed to toggle promo code active status: %s", e)
        return False


async def log_admin_action(admin_id: int, action_type: str, action_details: str, target_user_id: int = None) -> int:
    """Log admin action for audit trail."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO admin_logs(admin_id, action_type, action_details, target_user_id) VALUES(?,?,?,?)",
        (admin_id, action_type, action_details, target_user_id)
    )
    await db.commit()
    return cur.lastrowid


async def get_admin_logs(limit: int = 50, offset: int = 0, admin_id: int = None, action_type: str = None) -> list[dict]:
    """Get admin logs with optional filtering."""
    db = await get_db()
    
    query = "SELECT id, admin_id, action_type, action_details, target_user_id, created FROM admin_logs"
    params = []
    
    conditions = []
    if admin_id:
        conditions.append("admin_id = ?")
        params.append(admin_id)
    if action_type:
        conditions.append("action_type = ?")
        params.append(action_type)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY created DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    
    return [
        {
            "id": row[0],
            "admin_id": row[1],
            "action_type": row[2],
            "action_details": row[3],
            "target_user_id": row[4],
            "created": row[5],
        }
        for row in rows
    ]


async def get_all_keys_csv() -> str:
    """Get all keys in CSV format with user info."""
    db = await get_db()
    current_time = int(time.time())
    
    cur = await db.execute(
        """
        SELECT k.id, k.user_id, k.remark, k.uuid, k.short_id, k.days, k.limit_ip,
               k.created, k.expiry, u.total_paid
        FROM keys k
        LEFT JOIN users u ON k.user_id = u.user_id
        ORDER BY k.created DESC
        """
    )
    rows = await cur.fetchall()
    
    lines = ["key_id,user_id,remark,uuid,short_id,days,limit_ip,created,expiry,is_active,total_paid"]
    
    for row in rows:
        key_id = row[0]
        user_id = row[1]
        remark = row[2] or ""
        uuid = row[3] or ""
        short_id = row[4] or ""
        days = row[5]
        limit_ip = row[6]
        created = row[7]
        expiry = row[8]
        total_paid = row[9] or 0
        is_active = 1 if expiry > current_time else 0
        
        lines.append(f"{key_id},{user_id},\"{remark}\",{uuid},{short_id},{days},{limit_ip},{created},{expiry},{is_active},{total_paid}")
    
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Key Error Logging
# ---------------------------------------------------------------------------

async def log_key_error(
    user_id: int,
    error_type: str,
    error_message: str = None,
    context: dict = None
) -> int:
    """Log key issuance error for admin panel tracking."""
    import json
    db = await get_db()
    context_json = json.dumps(context) if context else None
    cur = await db.execute(
        "INSERT INTO key_errors(user_id, error_type, error_message, context) VALUES(?,?,?,?)",
        (user_id, error_type, error_message, context_json)
    )
    await db.commit()
    return cur.lastrowid


async def get_key_errors(limit: int = 50, offset: int = 0) -> list[dict]:
    """Get key issuance errors with pagination."""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, user_id, error_type, error_message, context, created "
        "FROM key_errors ORDER BY created DESC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "user_id": row[1],
            "error_type": row[2],
            "error_message": row[3],
            "context": row[4],
            "created": row[5],
        }
        for row in rows
    ]


async def get_user_key_errors(user_id: int) -> list[dict]:
    """Get key errors for a specific user."""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, error_type, error_message, context, created "
        "FROM key_errors WHERE user_id=? ORDER BY created DESC",
        (user_id,)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "error_type": row[1],
            "error_message": row[2],
            "context": row[3],
            "created": row[4],
        }
        for row in rows
    ]


async def get_key_errors_count() -> int:
    """Get total count of key errors."""
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM key_errors")
    return (await cur.fetchone())[0]


async def delete_key_error(error_id: int) -> bool:
    """Delete a key error log entry."""
    db = await get_db()
    cur = await db.execute("DELETE FROM key_errors WHERE id=?", (error_id,))
    await db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def close_db() -> None:
    """Close database connection."""
    global _db_pool
    if _db_pool:
        await _db_pool.close()
        _db_pool = None
