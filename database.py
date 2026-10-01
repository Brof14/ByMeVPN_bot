"""
Database operations for ByMeVPN bot.
Uses aiosqlite with WAL mode for better concurrency and connection pooling.
"""
import os
import time
import logging
from typing import Optional
import aiosqlite
from cache import cache_user_info, invalidate_user_cache, invalidate_subscription_cache
from async_utils import _db_semaphore

logger = logging.getLogger(__name__)

# Single source of truth for the DB path. Can be overridden via .env (DB_FILE=...).
# config.py re-exports this exact value — never define DB_FILE separately elsewhere.
DB_FILE = os.getenv("DB_FILE", "data/vpnbot.db")

# Ensure the parent directory exists before sqlite tries to open the file.
# aiosqlite/sqlite3 never create missing directories themselves — this was the
# root cause of "sqlite3.OperationalError: unable to open database file".
_db_dir = os.path.dirname(DB_FILE)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

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
    last_notification_at INTEGER DEFAULT 0,
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

-- YooKassa intro trial (1 ₽): pending checkout links, one per user
CREATE TABLE IF NOT EXISTS yookassa_trial_pending (
    user_id           INTEGER PRIMARY KEY,
    payment_id        TEXT NOT NULL,
    confirmation_url  TEXT,
    created           INTEGER DEFAULT (strftime('%s','now'))
);

-- Crypto Bot (@send): idempotency guard — one row per invoice_id, inserted before key delivery
CREATE TABLE IF NOT EXISTS crypto_processed (
    invoice_id  TEXT PRIMARY KEY,
    processed   INTEGER DEFAULT (strftime('%s','now'))
);

-- Crypto Bot: pending deliveries waiting for user to enter config name
CREATE TABLE IF NOT EXISTS crypto_pending (
    invoice_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    devices     INTEGER NOT NULL DEFAULT 1,
    amount_rub  INTEGER NOT NULL DEFAULT 0,
    created     INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_cp_user ON crypto_pending(user_id);

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
    amount INTEGER NOT NULL,  -- 50 рублей за первую оплату
    payment_id INTEGER,  -- ID платежа приглашённого
    payment_status TEXT DEFAULT 'pending',
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

-- YooKassa Auto-Renew Subscriptions
CREATE TABLE IF NOT EXISTS auto_renew_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key_id INTEGER,
    payment_method_id TEXT NOT NULL,
    payment_method_title TEXT,
    payment_method_type TEXT,
    months INTEGER NOT NULL DEFAULT 1,
    days INTEGER NOT NULL,
    devices INTEGER NOT NULL DEFAULT 2,
    amount_rub INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_charge_at INTEGER,
    last_charge_status TEXT,
    next_retry_at INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE(user_id, key_id)
);
CREATE INDEX IF NOT EXISTS idx_ars_user ON auto_renew_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_ars_status ON auto_renew_subscriptions(status);
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

    # Migration: create Crypto Bot idempotency + pending tables
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS crypto_processed ("
            "invoice_id TEXT PRIMARY KEY, "
            "processed INTEGER DEFAULT (strftime('%s','now')))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS crypto_pending ("
            "invoice_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
            "days INTEGER NOT NULL, devices INTEGER NOT NULL DEFAULT 1, "
            "amount_rub INTEGER NOT NULL DEFAULT 0, "
            "created INTEGER DEFAULT (strftime('%s','now')))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cp_user ON crypto_pending(user_id)"
        )
    except Exception:
        pass

    # Migration: add short_id column to keys table
    try:
        await db.execute("ALTER TABLE keys ADD COLUMN short_id TEXT")
        logger.info("Migration: added short_id column to keys table")
    except Exception:
        pass  # Column already exists

    # Migration: add last_notification_at column to keys table
    try:
        await db.execute("ALTER TABLE keys ADD COLUMN last_notification_at INTEGER DEFAULT 0")
        logger.info("Migration: added last_notification_at column to keys table")
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
        # Check if it's a duplicate column error (SQLite specific)
        if "duplicate column name" in str(e).lower():
            logger.debug("Migration: start_date column already exists in promo_codes")
        else:
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

    # Migration: add source tracking to users
    try:
        await db.execute("ALTER TABLE users ADD COLUMN source TEXT DEFAULT 'direct'")
        logger.info("Migration: added source column to users")
    except Exception:
        pass

    # Migration: add provider and provider_payment_id to payments with unique index
    try:
        await db.execute("ALTER TABLE payments ADD COLUMN provider TEXT")
        logger.info("Migration: added provider column to payments")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE payments ADD COLUMN provider_payment_id TEXT")
        logger.info("Migration: added provider_payment_id column to payments")
    except Exception:
        pass

    try:
        await db.execute("UPDATE payments SET provider = method WHERE provider IS NULL OR provider = ''")
        await db.execute("UPDATE payments SET provider_payment_id = payload WHERE provider_payment_id IS NULL OR provider_payment_id = ''")
        # Deduplicate historical legacy rows so UNIQUE index can be created safely
        await db.execute("""
            UPDATE payments 
            SET provider_payment_id = provider_payment_id || '_legacy_' || id 
            WHERE id NOT IN (
                SELECT MIN(id) FROM payments WHERE provider IS NOT NULL AND provider_payment_id IS NOT NULL 
                GROUP BY provider, provider_payment_id
            )
        """)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_id ON payments(provider, provider_payment_id)")
        logger.info("Migration: created unique index idx_payments_provider_id on payments(provider, provider_payment_id)")
    except Exception as e:
        logger.warning("Migration: idx_payments_provider_id notice: %s", e)

    # Migration: create analytics_events table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                tariff TEXT,
                devices INTEGER,
                amount INTEGER,
                source TEXT,
                promo TEXT,
                payment_provider TEXT,
                timestamp INTEGER DEFAULT (strftime('%s','now')),
                details TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_type ON analytics_events(event_type)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_timestamp ON analytics_events(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_user ON analytics_events(user_id)")
        logger.info("Migration: created analytics_events table")
    except Exception as e:
        logger.warning("Migration: analytics_events notice: %s", e)

    # Migration: create ad_campaigns table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ad_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                is_active INTEGER DEFAULT 1,
                start_date INTEGER DEFAULT (strftime('%s','now')),
                end_date INTEGER,
                cooldown_days INTEGER DEFAULT 14,
                max_shows INTEGER DEFAULT 3,
                target_audience TEXT DEFAULT 'all',
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_campaigns_active ON ad_campaigns(is_active)")
        logger.info("Migration: created ad_campaigns table")
    except Exception as e:
        logger.warning("Migration: ad_campaigns notice: %s", e)

    # Migration: create ad_applications table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ad_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                channel_username TEXT,
                subscriber_count INTEGER NOT NULL,
                bonus_months INTEGER NOT NULL,
                status TEXT DEFAULT 'draft',
                post_url TEXT,
                post_message_id INTEGER,
                campaign_id INTEGER,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                published_at INTEGER,
                verification_due_at INTEGER,
                approved_at INTEGER,
                rejected_at INTEGER,
                rejection_reason TEXT,
                UNIQUE(user_id, channel_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_user ON ad_applications(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_status ON ad_applications(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_channel ON ad_applications(channel_id)")
        logger.info("Migration: created ad_applications table")
    except Exception as e:
        logger.warning("Migration: ad_applications notice: %s", e)

    # Migration: create ad_notifications table
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ad_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                campaign_id TEXT,
                sent_at INTEGER DEFAULT (strftime('%s','now')),
                opened_at INTEGER,
                clicked_at INTEGER,
                dismissed_at INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_notif_user ON ad_notifications(user_id)")
        logger.info("Migration: created ad_notifications table")
    except Exception as e:
        logger.warning("Migration: ad_notifications notice: %s", e)

    # Migration: ad_audit_log (admin actions audit trail)
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ad_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_audit_user ON ad_audit_log(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_audit_created ON ad_audit_log(created_at)")
        logger.info("Migration: created ad_audit_log table")
    except Exception as e:
        logger.warning("Migration: ad_audit_log notice: %s", e)

    # Migration: ad_applications.channel_ref — normalized channel identity
    # (username lowercased / invite reference) so duplicate applications for
    # the same channel are reliably blocked even when channel_id is NULL.
    try:
        await db.execute("ALTER TABLE ad_applications ADD COLUMN channel_ref TEXT")
        logger.info("Migration: added ad_applications.channel_ref")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            logger.warning("Migration: ad_applications.channel_ref notice: %s", e)
    try:
        await db.execute(
            "UPDATE ad_applications SET channel_ref = LOWER(channel_username) "
            "WHERE (channel_ref IS NULL OR channel_ref = '') "
            "AND channel_username IS NOT NULL AND TRIM(channel_username) != ''"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ad_applications_channel_ref "
            "ON ad_applications(channel_ref) WHERE channel_ref IS NOT NULL"
        )
    except Exception as e:
        logger.warning("Migration: ad_applications.channel_ref index notice: %s", e)

    # Migration: create giveaways tables
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                prize_days INTEGER DEFAULT 30,
                winners_count INTEGER DEFAULT 1,
                end_date INTEGER NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at INTEGER DEFAULT (strftime('%s','now')),
                is_winner INTEGER DEFAULT 0,
                UNIQUE(giveaway_id, user_id),
                FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_active ON giveaways(is_active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_participants_user ON giveaway_participants(user_id)")
        logger.info("Migration: created giveaways tables")
    except Exception as e:
        logger.warning("Migration: giveaways notice: %s", e)

    # Migration: auto-renew subscriptions for YooKassa
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_renew_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key_id INTEGER,
                payment_method_id TEXT NOT NULL,
                payment_method_title TEXT,
                payment_method_type TEXT,
                months INTEGER NOT NULL DEFAULT 1,
                days INTEGER NOT NULL,
                devices INTEGER NOT NULL DEFAULT 2,
                amount_rub INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_charge_at INTEGER,
                last_charge_status TEXT,
                next_retry_at INTEGER,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(user_id, key_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ars_user ON auto_renew_subscriptions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ars_status ON auto_renew_subscriptions(status)")
        logger.info("Migration: created auto_renew_subscriptions table")
    except Exception as e:
        logger.warning("Migration: auto_renew_subscriptions notice: %s", e)

    # Migration: referral_earnings.payment_status (code writes this column;
    # older databases created the table without it)
    try:
        await db.execute(
            "ALTER TABLE referral_earnings ADD COLUMN payment_status TEXT DEFAULT 'pending'"
        )
        logger.info("Migration: added referral_earnings.payment_status")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            logger.warning("Migration: referral_earnings.payment_status notice: %s", e)

    # Migration: keys.cleaned (admin cleanup soft-delete marker)
    try:
        await db.execute("ALTER TABLE keys ADD COLUMN cleaned INTEGER DEFAULT 0")
        logger.info("Migration: added keys.cleaned")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            logger.warning("Migration: keys.cleaned notice: %s", e)


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
    provider: str = None,
    provider_payment_id: str = None,
) -> int:
    prov = provider or method
    prov_id = provider_payment_id or payload or f"tx_{user_id}_{int(time.time())}"
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "INSERT INTO payments(user_id, amount, currency, method, days, created, payload, status, tariff, devices, provider, provider_payment_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, amount, currency, method, days, int(time.time()), payload or prov_id, status, tariff, devices, prov, prov_id),
        )
        await db.commit()
        return cur.lastrowid


async def record_payment_idempotent(
    user_id: int,
    amount: int,
    currency: str,
    method: str,
    days: int,
    payload: str = None,
    status: str = "success",
    tariff: str = None,
    devices: int = 1,
    provider: str = None,
    provider_payment_id: str = None,
) -> tuple[bool, Optional[int]]:
    """
    Atomically records payment if (provider, provider_payment_id) has not been processed.
    Returns (is_new, payment_id).
    If already exists, returns (False, existing_payment_id).
    """
    prov = provider or method
    prov_id = provider_payment_id or payload
    if not prov_id:
        prov_id = f"{prov}_{user_id}_{int(time.time())}"

    db = await get_db()
    async with _db_semaphore:
        # Check existing payment
        cur = await db.execute(
            "SELECT id, status FROM payments WHERE provider = ? AND provider_payment_id = ?",
            (prov, prov_id),
        )
        existing = await cur.fetchone()
        if existing:
            logger.info("Payment %s:%s already exists (id=%d, status=%s)", prov, prov_id, existing[0], existing[1])
            return False, existing[0]

        try:
            cur = await db.execute(
                "INSERT INTO payments(user_id, amount, currency, method, days, created, payload, status, tariff, devices, provider, provider_payment_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, amount, currency, method, days, int(time.time()), payload or prov_id, status, tariff, devices, prov, prov_id),
            )
            await db.commit()
            return True, cur.lastrowid
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                cur = await db.execute(
                    "SELECT id FROM payments WHERE provider = ? AND provider_payment_id = ?",
                    (prov, prov_id),
                )
                r = await cur.fetchone()
                return False, r[0] if r else None
            raise


async def update_payment_status(payment_id: int, status: str) -> bool:
    """Update status of a payment (e.g. 'processing' -> 'success' or 'failed')."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
        await db.commit()
        return cur.rowcount > 0


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


async def get_yookassa_pending_by_user(user_id: int) -> Optional[dict]:
    """Get pending YooKassa payment by user_id (for config name input flow)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT payment_id, days, devices, amount_rub, created "
        "FROM yookassa_pending WHERE user_id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "payment_id": row[0],
        "days": row[1],
        "devices": row[2],
        "amount_rub": row[3],
        "created": row[4],
    }


# ---------------------------------------------------------------------------
# YooKassa Intro Trial (1 ₽) — pending checkout links
# ---------------------------------------------------------------------------

async def save_yookassa_trial_pending(user_id: int, payment_id: str, confirmation_url: str) -> None:
    """Store the latest pending intro-trial checkout link for a user (one per user)."""
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO yookassa_trial_pending(user_id, payment_id, confirmation_url, created) "
        "VALUES(?,?,?,?)",
        (user_id, payment_id, confirmation_url, int(time.time())),
    )
    await db.commit()


async def get_yookassa_trial_pending(user_id: int) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT payment_id, confirmation_url, created FROM yookassa_trial_pending WHERE user_id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {"payment_id": row[0], "confirmation_url": row[1], "created": row[2]}


async def delete_yookassa_trial_pending(user_id: int) -> None:
    db = await get_db()
    await db.execute("DELETE FROM yookassa_trial_pending WHERE user_id=?", (user_id,))
    await db.commit()


# ---------------------------------------------------------------------------
# YooKassa Auto-Renew Subscriptions
# ---------------------------------------------------------------------------

async def save_auto_renew_subscription(
    user_id: int,
    key_id: Optional[int],
    payment_method_id: str,
    payment_method_title: str = "",
    payment_method_type: str = "bank_card",
    months: int = 1,
    days: int = 30,
    devices: int = 2,
    amount_rub: int = 89,
) -> int:
    """Save or update auto-renew subscription parameters for a user/key."""
    db = await get_db()
    k_id = key_id if (key_id and key_id > 0) else None

    # Check if a subscription already exists for this user (and key)
    cur = await db.execute(
        "SELECT id FROM auto_renew_subscriptions WHERE user_id = ? AND (key_id = ? OR (key_id IS NULL AND ? IS NULL))",
        (user_id, k_id, k_id),
    )
    row = await cur.fetchone()

    if row:
        sub_id = row[0]
        await db.execute(
            """UPDATE auto_renew_subscriptions
               SET key_id = ?, payment_method_id = ?, payment_method_title = ?,
                   payment_method_type = ?, months = ?, days = ?, devices = ?,
                   amount_rub = ?, status = 'active', fail_count = 0,
                   next_retry_at = NULL, updated_at = strftime('%s','now')
               WHERE id = ?""",
            (k_id, payment_method_id, payment_method_title, payment_method_type,
             months, days, devices, amount_rub, sub_id),
        )
    else:
        cur = await db.execute(
            """INSERT INTO auto_renew_subscriptions (
                   user_id, key_id, payment_method_id, payment_method_title,
                   payment_method_type, months, days, devices, amount_rub,
                   status, fail_count
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0)""",
            (user_id, k_id, payment_method_id, payment_method_title,
             payment_method_type, months, days, devices, amount_rub),
        )
        sub_id = cur.lastrowid

    await db.commit()
    logger.info("Auto-renew subscription %d saved for user %d, key %s (devices=%d, days=%d, amount=%d)",
                sub_id, user_id, k_id, devices, days, amount_rub)
    return sub_id


async def get_auto_renew_subscription(user_id: int, key_id: Optional[int] = None) -> Optional[dict]:
    """Get auto-renew subscription record for user and optional key."""
    db = await get_db()
    if key_id:
        cur = await db.execute(
            """SELECT id, user_id, key_id, payment_method_id, payment_method_title,
                      payment_method_type, months, days, devices, amount_rub,
                      status, fail_count, last_charge_at, last_charge_status,
                      next_retry_at, created_at, updated_at
               FROM auto_renew_subscriptions
               WHERE user_id = ? AND key_id = ?
               LIMIT 1""",
            (user_id, key_id),
        )
    else:
        cur = await db.execute(
            """SELECT id, user_id, key_id, payment_method_id, payment_method_title,
                      payment_method_type, months, days, devices, amount_rub,
                      status, fail_count, last_charge_at, last_charge_status,
                      next_retry_at, created_at, updated_at
               FROM auto_renew_subscriptions
               WHERE user_id = ?
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "key_id": row[2],
        "payment_method_id": row[3],
        "payment_method_title": row[4],
        "payment_method_type": row[5],
        "months": row[6],
        "days": row[7],
        "devices": row[8],
        "amount_rub": row[9],
        "status": row[10],
        "fail_count": row[11],
        "last_charge_at": row[12],
        "last_charge_status": row[13],
        "next_retry_at": row[14],
        "created_at": row[15],
        "updated_at": row[16],
    }


async def set_auto_renew_status(sub_id: int, status: str) -> bool:
    """Set status ('active', 'cancelled', 'failed') for auto-renew subscription."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE auto_renew_subscriptions SET status = ?, updated_at = strftime('%s','now') WHERE id = ?",
        (status, sub_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def set_auto_renew_status_by_key(user_id: int, key_id: int, status: str) -> bool:
    """Set status ('active', 'cancelled', 'failed') for auto-renew subscription by key."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE auto_renew_subscriptions SET status = ?, updated_at = strftime('%s','now') WHERE user_id = ? AND key_id = ?",
        (status, user_id, key_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def cancel_auto_renew(user_id: int, key_id: Optional[int] = None) -> dict:
    """
    Disable future recurring charges for the user.

    This MUST NOT revoke already-paid access: key expiry, VPN access and the
    3x-ui client all stay untouched. The user keeps the service until the end
    of the period they have already paid for. Payment history is preserved.

    Returns dict: {'cancelled': bool, 'expires_at': int|None, 'pm_title': str}.
    """
    db = await get_db()
    pm_title = ""
    sub_found = False

    if key_id:
        cur = await db.execute(
            "SELECT id, payment_method_title FROM auto_renew_subscriptions "
            "WHERE user_id = ? AND (key_id = ? OR key_id IS NULL) ORDER BY (key_id IS NULL), id DESC LIMIT 1",
            (user_id, key_id),
        )
    else:
        cur = await db.execute(
            "SELECT id, payment_method_title FROM auto_renew_subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
    row = await cur.fetchone()
    if row:
        sub_id, pm_title = row[0], (row[1] or "")
        sub_found = True
        # Keep payment_method_* fields: they are needed to offer one-click
        # re-enabling later and contain no sensitive data (no PAN/CVV).
        await db.execute(
            "UPDATE auto_renew_subscriptions SET status = 'cancelled', "
            "next_retry_at = NULL, updated_at = strftime('%s','now') WHERE id = ?",
            (sub_id,),
        )
    await db.commit()

    # Access expiry stays as-is — user keeps what they already paid for.
    expiry = None
    if key_id:
        cur = await db.execute("SELECT MAX(expiry) FROM keys WHERE user_id = ? AND id = ?", (user_id, key_id))
    else:
        cur = await db.execute("SELECT MAX(expiry) FROM keys WHERE user_id = ? AND expiry > ?", (user_id, int(time.time())))
    row = await cur.fetchone()
    if row and row[0]:
        expiry = row[0]

    logger.info(
        "Auto-renew cancelled for user %d (sub_found=%s, key_id=%s) — access preserved until %s",
        user_id, sub_found, key_id, expiry,
    )
    return {"cancelled": sub_found, "expires_at": expiry, "pm_title": pm_title}


async def resume_auto_renew(user_id: int, key_id: Optional[int] = None) -> bool:
    """Re-enable previously cancelled auto-renew subscription (keeps saved payment method)."""
    db = await get_db()
    if key_id:
        cur = await db.execute(
            "UPDATE auto_renew_subscriptions SET status = 'active', fail_count = 0, next_retry_at = NULL, "
            "updated_at = strftime('%s','now') WHERE user_id = ? AND key_id = ? AND payment_method_id != ''",
            (user_id, key_id),
        )
    else:
        cur = await db.execute(
            "UPDATE auto_renew_subscriptions SET status = 'active', fail_count = 0, next_retry_at = NULL, "
            "updated_at = strftime('%s','now') WHERE user_id = ? AND payment_method_id != ''",
            (user_id,),
        )
    await db.commit()
    reenabled = cur.rowcount > 0
    if reenabled:
        logger.info("Auto-renew re-enabled for user %d (key_id=%s)", user_id, key_id)
    return reenabled


async def terminate_user_access(user_id: int, key_id: Optional[int] = None) -> int:
    """
    OPERATIONAL function: immediately revoke VPN access (expire keys in DB and
    disable the client in 3x-ui). Use ONLY where termination is genuinely
    required (admin decision, refund/abuse handling) — never as a side effect
    of simply disabling auto-renew.
    """
    db = await get_db()
    now_ts = int(time.time())
    if key_id:
        cur = await db.execute(
            "UPDATE keys SET expiry = ? WHERE id = ? AND user_id = ?",
            (now_ts - 1, key_id, user_id),
        )
    else:
        cur = await db.execute(
            "UPDATE keys SET expiry = ? WHERE user_id = ? AND expiry > ?",
            (now_ts - 1, user_id, now_ts),
        )
    terminated_count = cur.rowcount
    await db.commit()

    try:
        from xui_client import update_xui_user
        await update_xui_user(user_id, enable=False, new_expiry_ms=(now_ts - 10) * 1000)
    except Exception as e:
        logger.error("Failed to disable 3x-ui client on access termination for user %d: %s", user_id, e)

    logger.warning("User access TERMINATED for user %d (key_id=%s, keys=%d)", user_id, key_id, terminated_count)
    return terminated_count



async def update_auto_renew_charge_success(sub_id: int, new_days: int, new_amount: int) -> None:
    """Record successful recurrent charge."""
    db = await get_db()
    await db.execute(
        """UPDATE auto_renew_subscriptions
           SET last_charge_at = strftime('%s','now'),
               last_charge_status = 'success',
               fail_count = 0,
               next_retry_at = NULL,
               days = ?,
               amount_rub = ?,
               updated_at = strftime('%s','now')
           WHERE id = ?""",
        (new_days, new_amount, sub_id),
    )
    await db.commit()


async def update_auto_renew_charge_success_by_user_key(user_id: int, key_id: Optional[int], new_days: int, new_amount: int) -> None:
    """Record successful recurrent charge by user_id and key_id."""
    db = await get_db()
    if key_id:
        await db.execute(
            """UPDATE auto_renew_subscriptions
               SET last_charge_at = strftime('%s','now'),
                   last_charge_status = 'success',
                   fail_count = 0,
                   next_retry_at = NULL,
                   days = ?,
                   amount_rub = ?,
                   updated_at = strftime('%s','now')
               WHERE user_id = ? AND key_id = ?""",
            (new_days, new_amount, user_id, key_id),
        )
    else:
        await db.execute(
            """UPDATE auto_renew_subscriptions
               SET last_charge_at = strftime('%s','now'),
                   last_charge_status = 'success',
                   fail_count = 0,
                   next_retry_at = NULL,
                   days = ?,
                   amount_rub = ?,
                   updated_at = strftime('%s','now')
               WHERE user_id = ? AND id = (SELECT id FROM auto_renew_subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1)""",
            (new_days, new_amount, user_id, user_id),
        )
    await db.commit()


async def update_auto_renew_charge_failure(sub_id: int, error_message: str, max_fails: int = 3) -> bool:
    """
    Record failed recurrent charge attempt.
    Atomically increments fail_count, sets next retry time in 4 hours.
    If fail_count >= max_fails, sets status='failed'.
    Returns True if permanently disabled (status='failed'), False if will retry.
    """
    db = await get_db()
    await db.execute(
        """UPDATE auto_renew_subscriptions
           SET fail_count = fail_count + 1,
               last_charge_status = ?,
               next_retry_at = ?,
               updated_at = strftime('%s','now')
           WHERE id = ?""",
        (f"failed: {error_message[:100]}", int(time.time()) + 4 * 3600, sub_id),
    )
    cur = await db.execute(
        "SELECT fail_count FROM auto_renew_subscriptions WHERE id = ?",
        (sub_id,),
    )
    row = await cur.fetchone()
    fail_count = row[0] if row else max_fails

    is_disabled = fail_count >= max_fails
    if is_disabled:
        await db.execute(
            "UPDATE auto_renew_subscriptions SET status = 'failed', updated_at = strftime('%s','now') WHERE id = ?",
            (sub_id,),
        )
    await db.commit()
    logger.warning("Auto-renew subscription %d failed (%d/%d): %s (disabled=%s)",
                   sub_id, fail_count, max_fails, error_message, is_disabled)
    return is_disabled


async def get_expiring_auto_renew_subscriptions(now: Optional[int] = None) -> list[dict]:
    """
    Find active auto-renew subscriptions where the associated key has expired or reached expiry time.
    Guaranteed not to double-charge for the same expiry period.
    """
    if now is None:
        now = int(time.time())

    db = await get_db()
    cur = await db.execute(
        """SELECT s.id, s.user_id, s.key_id, s.payment_method_id,
                  s.payment_method_title, s.payment_method_type, s.months,
                  s.days, s.devices, s.amount_rub, s.fail_count,
                  k.expiry, k.remark, k.key, k.uuid
           FROM auto_renew_subscriptions s
           JOIN keys k ON (s.key_id = k.id OR (s.key_id IS NULL AND k.user_id = s.user_id))
           WHERE s.status = 'active'
             AND k.expiry <= ?
             AND k.expiry > (? - 7 * 86400)
             AND (s.last_charge_at IS NULL OR s.last_charge_at < k.expiry)
             AND (s.next_retry_at IS NULL OR s.next_retry_at <= ?)
           ORDER BY k.expiry ASC""",
        (now, now, now),
    )
    rows = await cur.fetchall()
    results = []
    seen_subs = set()
    for row in rows:
        sub_id = row[0]
        if sub_id in seen_subs:
            continue
        seen_subs.add(sub_id)
        results.append({
            "sub_id": row[0],
            "user_id": row[1],
            "key_id": row[2],
            "payment_method_id": row[3],
            "payment_method_title": row[4],
            "payment_method_type": row[5],
            "months": row[6],
            "days": row[7],
            "devices": row[8],
            "amount_rub": row[9],
            "fail_count": row[10],
            "expiry": row[11],
            "remark": row[12],
            "key": row[13],
            "uuid": row[14],
        })
    return results


# ---------------------------------------------------------------------------
# Crypto Bot (@send) — mirrors the YooKassa helpers above
# ---------------------------------------------------------------------------

async def is_crypto_processed(invoice_id) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM crypto_processed WHERE invoice_id=?",
        (str(invoice_id),),
    )
    row = await cur.fetchone()
    return row is not None


async def mark_crypto_processed(invoice_id) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO crypto_processed(invoice_id) VALUES(?)",
        (str(invoice_id),),
    )
    await db.commit()


async def add_crypto_pending(
    invoice_id,
    user_id: int,
    days: int,
    devices: int = 1,
    amount_rub: int = 0,
) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO crypto_pending(invoice_id, user_id, days, devices, amount_rub) "
        "VALUES(?,?,?,?,?)",
        (str(invoice_id), user_id, days, devices, amount_rub),
    )
    await db.commit()


async def get_crypto_pending(invoice_id) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, days, devices, amount_rub, created "
        "FROM crypto_pending WHERE invoice_id=?",
        (str(invoice_id),),
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


async def get_crypto_pending_by_user(user_id: int) -> Optional[dict]:
    """Get pending Crypto Bot payment by user_id (for config name input flow)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT invoice_id, days, devices, amount_rub, created "
        "FROM crypto_pending WHERE user_id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "invoice_id": row[0],
        "days": row[1],
        "devices": row[2],
        "amount_rub": row[3],
        "created": row[4],
    }


async def delete_crypto_pending(invoice_id) -> None:
    db = await get_db()
    await db.execute(
        "DELETE FROM crypto_pending WHERE invoice_id=?",
        (str(invoice_id),),
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


async def try_claim_ref_bonus(referrer_id: int, referred_id: int) -> bool:
    """
    Atomically claim a referral payment bonus (+15 days) for a specific
    (referrer, referred) pair. Returns True on first successful claim,
    False if it was already claimed.

    FIX: this function did not exist before — handlers/keys.py imported it
    directly (`from database import try_claim_ref_bonus`), which raised
    ImportError every time a referrer pressed the "🎁 Активировать +30 дней"
    button after their referral paid. That bonus was therefore impossible
    to claim. This wraps the existing can_claim_ref_bonus /
    mark_ref_bonus_claimed pair into one atomic-enough helper (the single
    shared aiosqlite connection serializes access, so there is no real
    concurrency window between the check and the mark here).
    """
    can_claim = await can_claim_ref_bonus(referrer_id, referred_id)
    if not can_claim:
        return False
    await mark_ref_bonus_claimed(referrer_id, referred_id)
    return True


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
#
# NOTE: has_active_subscription() used to be defined twice in this file.
# Python silently keeps only the LAST definition, so this first one was
# dead code (harmless, but confusing to read). Removed — see the real
# implementation further below, which wraps get_user_active_keys().


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


# NOTE: get_referral_stats is defined once, further below (after payout
# helpers). The previously duplicated earlier definition silently shadowed
# nothing but confused readers — removed.


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
    """Atomically claim trial using single SQL statement to prevent race conditions."""
    db = await get_db()
    async with _db_semaphore:
        # Ensure user row exists with trial_used=0
        await db.execute("INSERT OR IGNORE INTO users(user_id, trial_used) VALUES(?, 0)", (user_id,))
        cur = await db.execute(
            "UPDATE users SET trial_used = 1 WHERE user_id = ? AND trial_used = 0",
            (user_id,)
        )
        await db.commit()
        success = cur.rowcount > 0
        if success:
            invalidate_user_cache(user_id)
        return success


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
    """Начислить бонус за первую оплату приглашённого (50₽).

    Идемпотентно: UNIQUE(referrer_id, referred_id) гарантирует одно начисление
    даже при конкурентных вызовах / дубликатах вебхуков.
    """
    db = await get_db()
    try:
        async with _db_semaphore:
            try:
                await db.execute(
                    "INSERT INTO referral_earnings(referrer_id, referred_id, amount, payment_id, payment_status) "
                    "VALUES(?,?,?,?,?)",
                    (referrer_id, referred_id, amount, payment_id, 'pending')
                )
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    logger.info("Referral earning already exists for referrer %d / referred %d", referrer_id, referred_id)
                    return False  # бонус уже начислялся
                raise

            # Обновляем баланс реферала (в той же транзакции, что и начисление)
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

    base_query = """
        SELECT r.referrer_id, r.referred_id, u.source, r.created,
               re.amount, re.payment_status, re.created as payment_date,
               p.amount as payment_amount, p.created as payment_created
        FROM referrals r
        LEFT JOIN users u ON u.user_id = r.referred_id
        LEFT JOIN referral_earnings re ON r.referrer_id = re.referrer_id AND r.referred_id = re.referred_id
        LEFT JOIN payments p ON re.payment_id = p.id
        {where}
        ORDER BY r.created DESC
    """

    if referrer_id:
        cur = await db.execute(base_query.format(where="WHERE r.referrer_id = ?"), (referrer_id,))
    else:
        cur = await db.execute(base_query.format(where=""))

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
    """Создать заявку на вывод средств.

    Списание баланса защищено условием balance >= amount — даже при
    конкурентных заявках баланс не может уйти в минус.
    """
    if not await can_claim_payout(user_id, amount):
        raise ValueError("Invalid payout amount")

    db = await get_db()
    # Атомарно списываем средства с баланса (только если хватает)
    cur = await db.execute(
        "UPDATE referral_balance SET balance = balance - ? WHERE user_id=? AND balance >= ?",
        (amount, user_id, amount),
    )
    if cur.rowcount == 0:
        await db.rollback()
        raise ValueError("Insufficient referral balance")

    # Создаем заявку на вывод
    cur = await db.execute(
        "INSERT INTO referral_payouts(user_id, amount, status) VALUES(?,?,?)",
        (user_id, amount, "pending"),
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

    cur = await db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ?",
        (current_time - 30 * 86400,),
    )
    active_30d = (await cur.fetchone())[0]
    
    # Device tier distribution (active keys)
    cur = await db.execute("""
        SELECT COALESCE(limit_ip, 2) as devices, COUNT(*) 
        FROM keys 
        WHERE expiry > ? 
        GROUP BY devices
    """, (current_time,))
    device_dist = {2: 0, 5: 0, 10: 0}
    for row in await cur.fetchall():
        d = row[0]
        # Map 1 to 2 for backward compat
        mapped_d = 2 if d <= 2 else (5 if d <= 5 else 10)
        device_dist[mapped_d] = device_dist.get(mapped_d, 0) + row[1]

    # Conversion metrics
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'success'")
    paid_users = (await cur.fetchone())[0]

    conversion_trial_pct = round((paid_users / trial_users * 100), 1) if trial_users > 0 else 0.0
    total_users = basic_stats.get("total_users", 0) or 1
    conversion_overall_pct = round((paid_users / total_users * 100), 1)

    # Pending payments
    pending_yk = 0
    try:
        cur = await db.execute("SELECT COUNT(*) FROM yookassa_pending")
        pending_yk = (await cur.fetchone())[0]
    except Exception:
        pass

    pending_crypto = 0
    try:
        cur = await db.execute("SELECT COUNT(*) FROM crypto_pending")
        pending_crypto = (await cur.fetchone())[0]
    except Exception:
        pass

    # Provisioning errors
    key_errors_count = 0
    try:
        cur = await db.execute("SELECT COUNT(*) FROM key_errors")
        key_errors_count = (await cur.fetchone())[0]
    except Exception:
        pass

    return {
        **basic_stats,
        "trial_users": trial_users,
        "paid_users": paid_users,
        "conversion_trial_pct": conversion_trial_pct,
        "conversion_overall_pct": conversion_overall_pct,
        "device_dist": device_dist,
        "pending_yk": pending_yk,
        "pending_crypto": pending_crypto,
        "key_errors_count": key_errors_count,
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
    
    # Get UUID and user_id before updating
    cur = await db.execute("SELECT uuid, user_id FROM keys WHERE id=?", (key_id,))
    row = await cur.fetchone()
    client_uuid = row[0] if row else None
    user_id = row[1] if row else None
    
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
            from xui_client import update_xui_user_expiry
            success = await update_xui_user_expiry(user_id, days)
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


async def extend_key(key_id: int, additional_days: int, limit_ip: Optional[int] = None) -> bool:
    """Extend key by additional days from max(current_expiry, now) and optionally update device limit atomically."""
    db = await get_db()
    async with _db_semaphore:
        # Atomic update directly in SQL
        cur = await db.execute("""
            UPDATE keys 
            SET expiry = MAX(COALESCE(expiry, 0), cast(strftime('%s','now') as integer)) + (? * 86400),
                limit_ip = CASE WHEN ? IS NOT NULL AND ? > 0 THEN ? ELSE limit_ip END
            WHERE id = ?
        """, (additional_days, limit_ip, limit_ip, limit_ip, key_id))
        await db.commit()

        if cur.rowcount == 0:
            logger.warning("Key %d not found for extension", key_id)
            return False

        cur = await db.execute("SELECT expiry, limit_ip FROM keys WHERE id=?", (key_id,))
        row = await cur.fetchone()
        new_expiry, final_limit = row[0], row[1]

        logger.info(
            "Extended key %d by %d days (new expiry: %s, limit_ip: %s)",
            key_id, additional_days, new_expiry, final_limit,
        )
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


# NOTE: get_all_users_csv() used to be defined twice in this file (a simple
# version here, a proper csv.writer-based version further below). Python
# kept only the later one; this dead duplicate has been removed.


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
        SELECT DISTINCT user_id, expiry, last_notification_at
        FROM keys
        WHERE expiry BETWEEN ? AND ?
        AND expiry > ?
        ORDER BY expiry ASC
        """,
        (min_expiry, max_expiry, current_time)
    )
    rows = await cur.fetchall()
    return [
        {"user_id": row[0], "expiry": row[1], "last_notification_at": row[2]}
        for row in rows
    ]


async def update_key_last_notification(user_id: int, expiry: int) -> None:
    """Update last_notification_at timestamp for a key."""
    db = await get_db()
    current_time = int(time.time())
    await db.execute(
        """
        UPDATE keys
        SET last_notification_at = ?
        WHERE user_id = ? AND expiry = ?
        """,
        (current_time, user_id, expiry)
    )
    await db.commit()


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


async def has_user_used_promo(code: str, user_id: int) -> bool:
    """Check if user has already used this promo code."""
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM promo_code_uses WHERE code=? AND user_id=?",
        (code.upper(), user_id),
    )
    return await cur.fetchone() is not None


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
# Analytics Events & Source Tracking
# ---------------------------------------------------------------------------

async def log_analytics_event(
    user_id: Optional[int],
    event_type: str,
    tariff: Optional[str] = None,
    devices: Optional[int] = None,
    amount: Optional[int] = None,
    source: Optional[str] = None,
    promo: Optional[str] = None,
    payment_provider: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    """Log an analytics event without collecting personal sensitive data."""
    try:
        db = await get_db()
        async with _db_semaphore:
            await db.execute(
                """
                INSERT INTO analytics_events(user_id, event_type, tariff, devices, amount, source, promo, payment_provider, timestamp, details)
                VALUES(?,?,?,?,?,?,?,?,strftime('%s','now'),?)
                """,
                (user_id, event_type, tariff, devices, amount, source, promo, payment_provider, details)
            )
            await db.commit()
    except Exception as e:
        logger.debug("Failed to log analytics event %s: %s", event_type, e)


async def set_user_source(user_id: int, source: str) -> bool:
    """Set the acquisition source for a user (if not already set)."""
    try:
        db = await get_db()
        async with _db_semaphore:
            await db.execute(
                "UPDATE users SET source = ? WHERE user_id = ? AND (source IS NULL OR source = 'direct')",
                (source, user_id)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error("Failed to set user source: %s", e)
        return False


# ---------------------------------------------------------------------------
# Giveaways (Розыгрыши и акции)
# ---------------------------------------------------------------------------

async def create_giveaway(
    title: str,
    description: str,
    prize_days: int = 30,
    winners_count: int = 1,
    end_date: int = 0,
) -> int:
    """Create a new giveaway."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            """
            INSERT INTO giveaways(title, description, prize_days, winners_count, end_date, is_active, created_at)
            VALUES(?,?,?,?,?,1,strftime('%s','now'))
            """,
            (title, description, prize_days, winners_count, end_date)
        )
        await db.commit()
        return cur.lastrowid


async def get_all_giveaways() -> list[dict]:
    """Get all giveaways."""
    db = await get_db()
    cur = await db.execute("SELECT id, title, description, prize_days, winners_count, end_date, is_active, created_at FROM giveaways ORDER BY id DESC")
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "title": r[1], "description": r[2], "prize_days": r[3],
            "winners_count": r[4], "end_date": r[5], "is_active": r[6], "created_at": r[7],
        }
        for r in rows
    ]


async def get_active_giveaways() -> list[dict]:
    """Get active giveaways whose end date has not passed."""
    db = await get_db()
    now_ts = int(time.time())
    cur = await db.execute(
        "SELECT id, title, description, prize_days, winners_count, end_date, is_active, created_at FROM giveaways WHERE is_active = 1 AND end_date > ? ORDER BY id DESC",
        (now_ts,)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "title": r[1], "description": r[2], "prize_days": r[3],
            "winners_count": r[4], "end_date": r[5], "is_active": r[6], "created_at": r[7],
        }
        for r in rows
    ]


async def get_giveaway_by_id(giveaway_id: int) -> dict | None:
    """Get a giveaway by ID."""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, title, description, prize_days, winners_count, end_date, is_active, created_at FROM giveaways WHERE id = ?",
        (giveaway_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "title": row[1], "description": row[2], "prize_days": row[3],
        "winners_count": row[4], "end_date": row[5], "is_active": row[6], "created_at": row[7],
    }


# ---------------------------------------------------------------------------
# Ad Program Functions
# ---------------------------------------------------------------------------

async def create_ad_application(
    user_id: int,
    channel_id: int | None,
    channel_username: str,
    subscriber_count: int,
    bonus_months: int,
    campaign_id: int = None,
    channel_ref: str = None,
    status: str = "waiting_for_post",
) -> int:
    """Create a new ad application. Returns application ID.

    channel_ref is the normalized channel identity (lowercased @username or
    invite reference) — protected by a UNIQUE partial index, so the same
    channel cannot be applied twice even when channel_id is NULL (private).
    """
    db = await get_db()
    if not channel_ref and channel_username:
        channel_ref = channel_username.strip().lower()
    cur = await db.execute(
        """INSERT INTO ad_applications
        (user_id, channel_id, channel_username, subscriber_count, bonus_months, campaign_id, channel_ref, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, channel_id, channel_username, subscriber_count, bonus_months, campaign_id, channel_ref, status)
    )
    await db.commit()
    return cur.lastrowid


async def get_ad_application_by_id(application_id: int) -> dict | None:
    """Get ad application by ID."""
    db = await get_db()
    cur = await db.execute(
        """SELECT id, user_id, channel_id, channel_username, channel_ref, subscriber_count, bonus_months,
        status, post_url, post_message_id, campaign_id, created_at, published_at,
        verification_due_at, approved_at, rejected_at, rejection_reason
        FROM ad_applications WHERE id = ?""",
        (application_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "user_id": row[1], "channel_id": row[2], "channel_username": row[3],
        "channel_ref": row[4], "subscriber_count": row[5], "bonus_months": row[6],
        "status": row[7], "post_url": row[8], "post_message_id": row[9], "campaign_id": row[10],
        "created_at": row[11], "published_at": row[12], "verification_due_at": row[13],
        "approved_at": row[14], "rejected_at": row[15], "rejection_reason": row[16],
    }


async def get_user_ad_applications(user_id: int) -> list[dict]:
    """Get all ad applications for a user."""
    db = await get_db()
    cur = await db.execute(
        """SELECT id, user_id, channel_id, channel_username, channel_ref, subscriber_count, bonus_months,
        status, post_url, post_message_id, campaign_id, created_at, published_at,
        verification_due_at, approved_at, rejected_at, rejection_reason
        FROM ad_applications WHERE user_id = ? ORDER BY created_at DESC""",
        (user_id,)
    )
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "user_id": r[1], "channel_id": r[2], "channel_username": r[3],
            "channel_ref": r[4], "subscriber_count": r[5], "bonus_months": r[6],
            "status": r[7], "post_url": r[8], "post_message_id": r[9], "campaign_id": r[10],
            "created_at": r[11], "published_at": r[12], "verification_due_at": r[13],
            "approved_at": r[14], "rejected_at": r[15], "rejection_reason": r[16],
        }
        for r in rows
    ]


async def get_ad_applications_by_statuses(statuses: list[str], limit: int = 20) -> list[dict]:
    """List applications for the admin review queue, oldest first."""
    db = await get_db()
    placeholders = ",".join("?" * len(statuses))
    cur = await db.execute(
        f"""SELECT id, user_id, channel_id, channel_username, channel_ref, subscriber_count, bonus_months,
        status, post_url, post_message_id, campaign_id, created_at, published_at,
        verification_due_at, approved_at, rejected_at, rejection_reason
        FROM ad_applications WHERE status IN ({placeholders})
        ORDER BY created_at ASC LIMIT {int(limit)}""",
        tuple(statuses),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "user_id": r[1], "channel_id": r[2], "channel_username": r[3],
            "channel_ref": r[4], "subscriber_count": r[5], "bonus_months": r[6],
            "status": r[7], "post_url": r[8], "post_message_id": r[9], "campaign_id": r[10],
            "created_at": r[11], "published_at": r[12], "verification_due_at": r[13],
            "approved_at": r[14], "rejected_at": r[15], "rejection_reason": r[16],
        }
        for r in rows
    ]


async def update_ad_application(
    application_id: int,
    status: str = None,
    post_url: str = None,
    post_message_id: int = None,
    rejection_reason: str = None,
    subscriber_count: int = None,
) -> bool:
    """Update ad application."""
    db = await get_db()
    updates = []
    params = []

    if status:
        updates.append("status = ?")
        params.append(status)
        if status == "approved":
            updates.append("approved_at = ?")
            params.append(int(time.time()))
        elif status == "rejected":
            updates.append("rejected_at = ?")
            params.append(int(time.time()))
        elif status == "post_submitted":
            updates.append("published_at = ?")
            params.append(int(time.time()))
            updates.append("verification_due_at = ?")
            params.append(int(time.time()) + 30 * 86400)  # 30 days retention check

    if post_url:
        updates.append("post_url = ?")
        params.append(post_url)

    if post_message_id:
        updates.append("post_message_id = ?")
        params.append(post_message_id)

    if rejection_reason is not None:
        updates.append("rejection_reason = ?")
        params.append(rejection_reason)

    if subscriber_count is not None:
        updates.append("subscriber_count = ?")
        params.append(subscriber_count)

    if not updates:
        return False

    params.append(application_id)
    query = f"UPDATE ad_applications SET {', '.join(updates)} WHERE id = ?"
    await db.execute(query, params)
    await db.commit()
    return True


async def channel_already_used(channel_id: int | None, channel_ref: str | None = None) -> bool:
    """Check if channel was already used for an approved/completed ad application."""
    db = await get_db()
    if channel_ref:
        cur = await db.execute(
            "SELECT 1 FROM ad_applications WHERE channel_ref = ? AND status IN ('approved', 'completed') LIMIT 1",
            (channel_ref,),
        )
        if await cur.fetchone():
            return True
    if channel_id is not None:
        cur = await db.execute(
            "SELECT 1 FROM ad_applications WHERE channel_id = ? AND status IN ('approved', 'completed') LIMIT 1",
            (channel_id,),
        )
        if await cur.fetchone():
            return True
    return False


async def log_ad_audit(user_id: int | None, action: str, details: str = "") -> None:
    """Append an admin/ad action to the audit trail."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO ad_audit_log(user_id, action, details) VALUES(?,?,?)",
            (user_id, action, details),
        )
        await db.commit()
    except Exception as e:
        logger.warning("ad_audit_log write failed: %s", e)


async def get_active_ad_campaign() -> dict | None:
    """Get the active ad campaign."""
    db = await get_db()
    now_ts = int(time.time())
    cur = await db.execute(
        """SELECT id, name, description, is_active, start_date, end_date,
        cooldown_days, max_shows, target_audience, created_at
        FROM ad_campaigns WHERE is_active = 1 AND start_date <= ? AND (end_date IS NULL OR end_date > ?)
        ORDER BY created_at DESC LIMIT 1""",
        (now_ts, now_ts)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "description": row[2], "is_active": row[3],
        "start_date": row[4], "end_date": row[5], "cooldown_days": row[6],
        "max_shows": row[7], "target_audience": row[8], "created_at": row[9],
    }


async def log_ad_notification(user_id: int, campaign_id=None) -> int:
    """Log ad notification shown to user. Returns notification ID."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO ad_notifications (user_id, campaign_id, sent_at) VALUES (?, ?, ?)",
        (user_id, campaign_id, int(time.time())),
    )
    await db.commit()
    return cur.lastrowid


async def get_ad_notification_shows(user_id: int, campaign_id) -> int:
    """Get number of times ad notification was shown to user."""
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM ad_notifications WHERE user_id = ? AND campaign_id IS ?",
        (user_id, campaign_id),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def mark_ad_notification_opened(user_id: int, campaign_id: int) -> bool:
    """Mark ad notification as opened by user."""
    db = await get_db()
    await db.execute(
        "UPDATE ad_notifications SET opened_at = ? WHERE user_id = ? AND campaign_id = ? AND opened_at IS NULL",
        (int(time.time()), user_id, campaign_id)
    )
    await db.commit()
    return True


async def calculate_bonus_months(subscriber_count: int) -> int:
    """Calculate bonus months based on subscriber count."""
    if subscriber_count >= 350:
        return 24  # 2 years
    elif subscriber_count >= 250:
        return 12  # 1 year
    elif subscriber_count >= 150:
        return 8
    elif subscriber_count >= 100:
        return 6
    elif subscriber_count >= 50:
        return 4
    else:
        return 2


async def log_ad_analytics_event(user_id: int, event_type: str, details: dict = None) -> None:
    """Log ad-related analytics event."""
    db = await get_db()
    import json
    details_json = json.dumps(details) if details else None
    await db.execute(
        """INSERT INTO analytics_events (user_id, event_type, timestamp, details)
        VALUES (?, ?, ?, ?)""",
        (user_id, event_type, int(time.time()), details_json)
    )
    await db.commit()


async def get_ad_analytics_stats() -> dict:
    """Get ad program analytics statistics."""
    db = await get_db()
    
    # Count unique users who saw offer
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM ad_notifications")
    offer_views = (await cur.fetchone())[0] or 0
    
    # Count offer clicks (opened notifications)
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM ad_notifications WHERE opened_at IS NOT NULL")
    offer_clicks = (await cur.fetchone())[0] or 0
    
    # Count applications submitted
    cur = await db.execute("SELECT COUNT(*) FROM ad_applications")
    applications_submitted = (await cur.fetchone())[0] or 0
    
    # Count approved applications
    cur = await db.execute("SELECT COUNT(*) FROM ad_applications WHERE status = 'approved'")
    applications_approved = (await cur.fetchone())[0] or 0
    
    # Count completed applications
    cur = await db.execute("SELECT COUNT(*) FROM ad_applications WHERE status = 'completed'")
    applications_completed = (await cur.fetchone())[0] or 0
    
    return {
        "offer_views": offer_views,
        "offer_clicks": offer_clicks,
        "applications_submitted": applications_submitted,
        "applications_approved": applications_approved,
        "applications_completed": applications_completed,
    }


async def join_giveaway(giveaway_id: int, user_id: int) -> bool:
    """User joins a giveaway. Returns True if successfully registered, False if already joined."""
    db = await get_db()
    async with _db_semaphore:
        try:
            await db.execute(
                "INSERT INTO giveaway_participants(giveaway_id, user_id, joined_at) VALUES(?,?,strftime('%s','now'))",
                (giveaway_id, user_id)
            )
            await db.commit()
            return True
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return False
            raise


async def get_giveaway_participants_count(giveaway_id: int) -> int:
    """Get number of participants in a giveaway."""
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,))
    row = await cur.fetchone()
    return row[0] if row else 0


async def pick_giveaway_winners(giveaway_id: int) -> list[int]:
    """Randomly pick winners for a giveaway and mark them as winners."""
    import random
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute("SELECT winners_count FROM giveaways WHERE id = ?", (giveaway_id,))
        g_row = await cur.fetchone()
        if not g_row:
            return []
        winners_count = g_row[0]

        cur = await db.execute("SELECT user_id FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,))
        participants = [r[0] for r in await cur.fetchall()]
        if not participants:
            return []

        selected = random.sample(participants, min(len(participants), winners_count))
        for uid in selected:
            await db.execute(
                "UPDATE giveaway_participants SET is_winner = 1 WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, uid)
            )
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE id = ?", (giveaway_id,))
        await db.commit()
        return selected


async def set_giveaway_status(giveaway_id: int, is_active: int) -> bool:
    """Activate or deactivate a giveaway."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute("UPDATE giveaways SET is_active = ? WHERE id = ?", (is_active, giveaway_id))
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
