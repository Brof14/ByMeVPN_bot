Warning: truncated output (original token count: 71135)
Total output lines: 7358

"""
Database operations for ByMeVPN bot.
Uses aiosqlite with WAL mode for better concurrency and connection pooling.
"""
import hashlib
import os
import secrets
import time
import json
import logging
import math
import re
from typing import Optional
import aiosqlite
from cache import cache_user_info, invalidate_user_cache, invalidate_subscription_cache
from async_utils import _db_semaphore
from constants import normalize_days

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
    created     INTEGER DEFAULT (strftime('%s','now')),
    -- Legacy drift: `username` was added to production out-of-band before this
    -- schema was consolidated. Declared so fresh installs match prod 1:1.
    -- No ALTER needed: every existing DB already has the column.
    username    TEXT
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
    connect_reminder_status TEXT NOT NULL DEFAULT 'disabled',
    connect_reminder_due INTEGER NOT NULL DEFAULT 0,
    connect_traffic_baseline INTEGER NOT NULL DEFAULT 0,
    connect_reminder_attempts INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_keys_user   ON keys(user_id);
CREATE INDEX IF NOT EXISTS idx_keys_expiry ON keys(expiry);

-- The last selected plan, not an authorization to charge. Intentionally
-- excludes prices and promo state: provider checkout must be reconfirmed.
CREATE TABLE IF NOT EXISTS checkout_drafts (
    user_id INTEGER PRIMARY KEY,
    months INTEGER NOT NULL,
    devices INTEGER NOT NULL,
    renew_key_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

-- A checkout lease serializes provider invoice creation even across bot processes.
-- The attempt key is reused for short retries of the exact same confirmed order.
CREATE TABLE IF NOT EXISTS churn_feedback (
    user_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_churn_feedback_time ON churn_feedback(recorded_at);

CREATE TABLE IF NOT EXISTS yookassa_checkout_attempts (
    user_id INTEGER PRIMARY KEY,
    order_fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    lease_token TEXT,
    lease_expires_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS revoked_mobile_jwt_sessions (
    jti TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_mobile_jwt_expiry
    ON revoked_mobile_jwt_sessions(expires_at);

CREATE TABLE IF NOT EXISTS mobile_refresh_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    access_jti TEXT NOT NULL UNIQUE,
    access_expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mobile_refresh_sessions_user ON mobile_refresh_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_mobile_refresh_sessions_expiry ON mobile_refresh_sessions(expires_at);

CREATE TABLE IF NOT EXISTS mobile_auth_sessions (
    state_hash TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'authenticated', 'consumed')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mobile_auth_sessions_expiry
    ON mobile_auth_sessions(expires_at);

-- Client-submitted diagnostics (not authoritative VPN connection evidence).
-- Store only allowlisted categories, never logs, URLs, UUID or client IP.
CREATE TABLE IF NOT EXISTS client_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    stage TEXT NOT NULL,
    error_code TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_client_diagnostics_user
    ON client_diagnostics(user_id,created_at);

CREATE TABLE IF NOT EXISTS known_hwid_devices (
    user_id INTEGER NOT NULL,
    device_id INTEGER NOT NULL,
    first_seen INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER NOT NULL DEFAULT 0,
    notified_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_known_hwid_devices_pending
    ON known_hwid_devices(notified_at, user_id);

CREATE TABLE IF NOT EXISTS provisioning_intents (
    provider TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    target_expiry INTEGER NOT NULL,
    created INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY(provider, provider_payment_id)
);

-- Per-user lease shared by every bot worker using this SQLite database.
-- Acquired with a single UPSERT; stale leases are reclaimable after a crash.
CREATE TABLE IF NOT EXISTS delivery_leases (
    user_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Minimal account tombstones prevent deleted users from being silently
-- reactivated by Telegram events or by an old, otherwise valid JWT.
CREATE TABLE IF NOT EXISTS account_tombstones (
    user_id INTEGER PRIMARY KEY,
    deleted_at INTEGER NOT NULL,
    source TEXT NOT NULL
);

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
    signup_bonus_retry INTEGER NOT NULL DEFAULT 0,
    signup_attempt_ms INTEGER,
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

CREATE TABLE IF NOT EXISTS referral_payment_rewards (
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    payment_ref TEXT NOT NULL,
    created INTEGER DEFAULT (strftime('%s','now')),
    last_attempt_ms INTEGER,
    UNIQUE(referrer_id, referred_id)
);

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

-- Provider notifications are acknowledged only after their IDs have been
-- persisted. The monitor retries interrupted processing after restarts.
CREATE TABLE IF NOT EXISTS yookassa_webhook_inbox (
    payment_id TEXT PRIMARY KEY,
    received_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_check INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'received'
);
CREATE INDEX IF NOT EXISTS idx_yookassa_webhook_inbox_due
    ON yookassa_webhook_inbox(next_check,received_at);

-- Unsettled YooKassa checkout IDs. These durable IDs let the monitor recover
-- paid orders after outages that exceed its short provider query window.
CREATE TABLE IF NOT EXISTS yookassa_pending (
    payment_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    devices     INTEGER NOT NULL DEFAULT 1,
    amount_rub  INTEGER NOT NULL DEFAULT 0,
    poll_attempts INTEGER NOT NULL DEFAULT 0,
    next_check INTEGER NOT NULL DEFAULT 0,
    last_status TEXT,
    created     INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_ykp_user ON yookassa_pending(user_id);

-- App checkout idempotency and exact invoice ownership.
CREATE TABLE IF NOT EXISTS mobile_payment_requests (
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    plan_code TEXT NOT NULL,
    provider TEXT NOT NULL,
    payment_id TEXT,
    confirmation_url TEXT,
    devices INTEGER NOT NULL DEFAULT 2,
    generation INTEGER NOT NULL DEFAULT 0,
    creation_state TEXT NOT NULL DEFAULT 'ready',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, request_id),
    UNIQUE (provider, payment_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_payment_requests_payment
    ON mobile_payment_requests(provider, payment_id);

-- Durable admin broadcasts; queued recipients survive bot restarts.
CREATE TABLE IF NOT EXISTS broadcast_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    buttons_json TEXT NOT NULL DEFAULT '[]',
    audience TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    finished INTEGER
);
CREATE TABLE IF NOT EXISTS broadcast_recipients (
    campaign_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (campaign_id, user_id),
    FOREIGN KEY(campaign_id) REFERENCES broadcast_campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_broadcast_queue ON broadcast_recipients(campaign_id, status, user_id);

-- Bounded daily/hourly performance history, independent of process restarts.
CREATE TABLE IF NOT EXISTS performance_samples (
    metric TEXT NOT NULL,
    sampled_at INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    avg_ms INTEGER NOT NULL,
    p95_ms INTEGER NOT NULL,
    PRIMARY KEY (metric, sampled_at)
);
CREATE INDEX IF NOT EXISTS idx_performance_sampled_at ON performance_samples(sampled_at);

-- Crypto Bot (@send): idempotency guard — one row per invoice_id, inserted before key delivery
CREATE TABLE IF NOT EXISTS crypto_processed (
    invoice_id  TEXT PRIMARY KEY,
    processed   INTEGER DEFAULT (strftime('%s','now'))
);

-- Unsettled Crypto Pay invoice IDs, retained until delivery or provider expiry.
CREATE TABLE IF NOT EXISTS crypto_pending (
    invoice_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    devices     INTEGER NOT NULL DEFAULT 1,
    amount_rub  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT,
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

_MOBILE_GOOGLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_google_nonces (
    nonce_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mobile_google_nonces_expiry
    ON mobile_google_nonces(expires_at);

CREATE TABLE IF NOT EXISTS google_accounts (
    google_sub TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    email TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

_MOBILE_TRIAL_BOOTSTRAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_trial_bootstrap (
    user_id INTEGER PRIMARY KEY,
    device_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_trial_bootstrap_expiry
    ON mobile_trial_bootstrap(expires_at);
"""

_MOBILE_DEVICE_BOOTSTRAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_device_bootstraps (
    user_id INTEGER NOT NULL,
    device_hash TEXT NOT NULL,
    subscription_expiry INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, device_hash, subscription_expiry),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_device_bootstraps_capacity
    ON mobile_device_bootstraps(user_id, subscription_expiry, expires_at);
"""

_TRIAL_CLAIM_RESERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_claim_reservations (
    user_id INTEGER PRIMARY KEY,
    claim_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""


async def ensure_mobile_google_schema(db: aiosqlite.Connection) -> None:
    """Apply the additive, repeatable Google nonce/identity schema migration."""
    await db.executescript(_MOBILE_GOOGLE_SCHEMA)


async def ensure_mobile_trial_bootstrap_schema(db: aiosqlite.Connection) -> None:
    """Create the additive storage for a short, device-bound first-connect grant."""
    await db.executescript(_MOBILE_TRIAL_BOOTSTRAP_SCHEMA)


async def ensure_mobile_device_bootstrap_schema(db: aiosqlite.Connection) -> None:
    """Create additive storage for bounded first-connect grants on paid/trial plans."""
    await db.executescript(_MOBILE_DEVICE_BOOTSTRAP_SCHEMA)


async def ensure_trial_claim_reservation_schema(db: aiosqlite.Connection) -> None:
    """Create shared pending-trial reservations without marking trials used."""
    await db.executescript(_TRIAL_CLAIM_RESERVATION_SCHEMA)


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


async def _ensure_schema_column(
    db: aiosqlite.Connection, table: str, column: str, definition: str,
) -> bool:
    """Add one missing schema column and let every unexpected SQLite error surface."""
    identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    if not identifier.fullmatch(table) or not identifier.fullmatch(column):
        raise ValueError("schema identifiers must be static SQL names")
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    if column in {row[1] for row in rows}:
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    logger.info("Migration: added %s.%s", table, column)
    return True


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Run database migrations."""
    referral_columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(referrals)")).fetchall()
    }
    if "signup_bonus_retry" not in referral_columns:
        await db.execute("ALTER TABLE referrals ADD COLUMN signup_bonus_retry INTEGER NOT NULL DEFAULT 0")
    if "signup_attempt_ms" not in referral_columns:
        await db.execute("ALTER TABLE referrals ADD COLUMN signup_attempt_ms INTEGER")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_referrals_signup_retry ON referrals(signup_attempt_ms) WHERE signup_bonus_retry=1"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_referral_events_pair_type ON referral_events(referrer_id, referred_id, event_type)"
    )
    reward_columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(referral_payment_rewards)")).fetchall()
    }
    if "last_attempt_ms" not in reward_columns:
        await db.execute("ALTER TABLE referral_payment_rewards ADD COLUMN last_attempt_ms INTEGER")
    # Additive storage for the opt-in Google flow. This is deliberately a
    # migration rather than part of the base schema so existing SQLite files
    # are upgraded explicitly and can be verified independently before rollout.
    await ensure_mobile_google_schema(db)
    await ensure_mobile_trial_bootstrap_schema(db)
    await ensure_mobile_device_bootstrap_schema(db)
    await ensure_trial_claim_reservation_schema(db)

    # Provisioning intents make paid-key retries idempotent across restarts.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS provisioning_intents (
            provider TEXT NOT NULL,
            provider_payment_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            target_expiry INTEGER NOT NULL,
            created INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY(provider, provider_payment_id)
        )"""
    )
    payment_request_columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(mobile_payment_requests)")).fetchall()
    }
    if "generation" not in payment_request_columns:
        await db.execute(
            "ALTER TABLE mobile_payment_requests ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
        )
    if "devices" not in payment_request_columns:
        await db.execute(
            "ALTER TABLE mobile_payment_requests ADD COLUMN devices INTEGER NOT NULL DEFAULT 2"
        )
    if "creation_state" not in payment_request_columns:
        await db.execute(
            "ALTER TABLE mobile_payment_requests ADD COLUMN creation_state TEXT NOT NULL DEFAULT 'ready'"
        )

    await db.execute(
        """CREATE TABLE IF NOT EXISTS expiry_notification_deliveries (
            user_id INTEGER NOT NULL, expiry INTEGER NOT NULL, bucket TEXT NOT NULL,
            status TEXT NOT NULL, updated_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, expiry, bucket)
        )"""
    )

    await _ensure_schema_column(db, "keys", "limit_ip", "INTEGER NOT NULL DEFAULT 1")

    # Migration: enhance payments table for detailed logging
    await _ensure_schema_column(db, "payments", "status", "TEXT DEFAULT 'success'")
    await _ensure_schema_column(db, "payments", "tariff", "TEXT")
    await _ensure_schema_column(db, "payments", "devices", "INTEGER DEFAULT 1")

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
        raise

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
            "poll_attempts INTEGER NOT NULL DEFAULT 0, "
            "next_check INTEGER NOT NULL DEFAULT 0, last_status TEXT, "
            "created INTEGER DEFAULT (strftime('%s','now')))"
        )
        for column, definition in (
            ("poll_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_check", "INTEGER NOT NULL DEFAULT 0"),
            ("last_status", "TEXT"),
        ):
            await _ensure_schema_column(db, "yookassa_pending", column, definition)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ykp_next_check ON yookassa_pending(next_check)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ykp_user ON yookassa_pending(user_id)"
        )
    except Exception:
        raise

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
        await _ensure_schema_column(db, "crypto_pending", "payload", "TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cp_user ON crypto_pending(user_id)"
        )
    except Exception:
        raise

    await _ensure_schema_column(db, "keys", "short_id", "TEXT")
    await _ensure_schema_column(db, "keys", "last_notification_at", "INTEGER DEFAULT 0")

    # One-time onboarding reminder for newly issued keys that have no VPN traffic.
    for column, definition in (
        ("connect_reminder_status", "TEXT NOT NULL DEFAULT 'disabled'"),
        ("connect_reminder_due", "INTEGER NOT NULL DEFAULT 0"),
        ("connect_traffic_baseline", "INTEGER NOT NULL DEFAULT 0"),
        ("connect_reminder_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ):
        await _ensure_schema_column(db, "keys", column, definition)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_keys_connect_reminder "
        "ON keys(connect_reminder_status, connect_reminder_due)"
    )

    await _ensure_schema_column(db, "users", "email", "TEXT")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
        "ON users(email) WHERE email IS NOT NULL"
    )

    if await _ensure_schema_column(db, "users", "total_paid", "INTEGER DEFAULT 0"):
        # Populate total_paid for existing users
        await db.execute("""
            UPDATE users SET total_paid = (
                SELECT COALESCE(SUM(amount), 0) 
                FROM payments 
                WHERE payments.user_id = users.user_id
            )
        """)
        logger.info("Migration: populated total_paid for existing users")

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
        raise

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
        raise

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
        raise

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
        raise

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
        raise

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
        raise

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
        raise

    await _ensure_schema_column(db, "promo_codes", "promo_type", "TEXT DEFAULT 'percent'")
    await _ensure_schema_column(db, "promo_codes", "discount_value", "INTEGER DEFAULT 10")
    await _ensure_schema_column(db, "promo_codes", "tariff_binding", "INTEGER")

    if await _ensure_schema_column(db, "promo_codes", "start_date", "INTEGER"):
        # Set default value for existing rows
        await db.execute("UPDATE promo_codes SET start_date = created WHERE start_date IS NULL")
        logger.info("Migration: added start_date column to promo_codes")

    await _ensure_schema_column(db, "users", "is_banned", "INTEGER DEFAULT 0")
    await _ensure_schema_column(db, "users", "ban_reason", "TEXT")
    await _ensure_schema_column(db, "users", "source", "TEXT DEFAULT 'direct'")
    await _ensure_schema_column(db, "payments", "provider", "TEXT")
    await _ensure_schema_column(db, "payments", "provider_payment_id", "TEXT")

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
        raise

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
        raise

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
        raise

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
                grant_attempt_ms INTEGER,
                UNIQUE(user_id, channel_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_user ON ad_applications(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_status ON ad_applications(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_channel ON ad_applications(channel_id)")
        ad_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(ad_applications)")).fetchall()}
        if "grant_attempt_ms" not in ad_columns:
            await db.execute("ALTER TABLE ad_applications ADD COLUMN grant_attempt_ms INTEGER")
        logger.info("Migration: created ad_applications table")
    except Exception as e:
        logger.warning("Migration: ad_applications notice: %s", e)
        raise

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
        raise

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
        raise

    # Migration: ad_applications.channel_ref — normalized channel identity
    # (username lowercased / invite reference) so duplicate applications for
    # the same channel are reliably blocked even when channel_id is NULL.
    await _ensure_schema_column(db, "ad_applications", "channel_ref", "TEXT")
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
        raise

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
                created_at INTEGER DEFAULT (strftime('%s','now')),
                created_by INTEGER,
                automation_key TEXT UNIQUE,
                drawn_at INTEGER,
                results_notified_at INTEGER,
                period_start INTEGER,
                announcement_version INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at INTEGER DEFAULT (strftime('%s','now')),
                is_winner INTEGER DEFAULT 0,
                rewarded_at INTEGER,
                reward_attempts INTEGER DEFAULT 0,
                last_reward_attempt_at INTEGER,
                referral_count INTEGER DEFAULT 0,
                winner_rank INTEGER,
                UNIQUE(giveaway_id, user_id),
                FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_active ON giveaways(is_active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_participants_user ON giveaway_participants(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaway_due ON giveaways(is_active, end_date)")
        for table, definition in (
            ("giveaways", "created_by INTEGER"),
            ("giveaways", "automation_key TEXT"),
            ("giveaways", "drawn_at INTEGER"),
            ("giveaways", "results_notified_at INTEGER"),
            ("giveaways", "period_start INTEGER"),
            ("giveaways", "announcement_version INTEGER DEFAULT 0"),
            ("giveaway_participants", "rewarded_at INTEGER"),
            ("giveaway_participants", "reward_attempts INTEGER DEFAULT 0"),
            ("giveaway_participants", "last_reward_attempt_at INTEGER"),
            ("giveaway_participants", "referral_count INTEGER DEFAULT 0"),
            ("giveaway_participants", "winner_rank INTEGER"),
        ):
            column, sql_definition = definition.split(" ", 1)
            await _ensure_schema_column(db, table, column, sql_definition)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_giveaways_automation_key "
            "ON giveaways(automation_key) WHERE automation_key IS NOT NULL"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_notifications (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_at INTEGER,
                PRIMARY KEY(giveaway_id, user_id),
                FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketing_preferences (
                user_id INTEGER PRIMARY KEY,
                promotional_opt_out INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        logger.info("Migration: created giveaways tables")
    except Exception as e:
        logger.warning("Migration: giveaways notice: %s", e)
        raise

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
        raise

    # Migration: referral_earnings.payment_status (code writes this column;
    # older databases created the table without it)
    await _ensure_schema_column(
        db, "referral_earnings", "payment_status", "TEXT DEFAULT 'pending'",
    )

    # Migration: keys.cleaned (admin cleanup soft-delete marker)
    await _ensure_schema_column(db, "keys", "cleaned", "INTEGER DEFAULT 0")


async def init_db() -> None:
    """Initialize database with WAL mode and run migrations."""
    db = await get_db()

    # Create tables
    await db.executescript(_SCHEMA)

    # Run migrations
    await _run_migrations(db)

    # Older key replacement flows could leave an active recurring-payment
    # record pointing at a deleted key. Reattach only when the user has one
    # unambiguous current key; never guess when multiple keys exist.
    cur = await db.execute(
        """SELECT s.id, s.user_id
           FROM auto_renew_subscriptions s
           LEFT JOIN keys old_key ON old_key.id = s.key_id AND old_key.user_id = s.user_id
           WHERE s.status = 'active' AND s.key_id IS NOT NULL AND old_key.id IS NULL"""
    )
    orphaned = await cur.fetchall()
    for sub_id, user_id in orphaned:
        cur = await db.execute(
            "SELECT id FROM keys WHERE user_id = ? ORDER BY expiry DESC, id DESC",
            (user_id,),
        )
        user_keys = await cur.fetchall()
        if len(user_keys) != 1:
            logger.error(
                "Auto-renew subscription %d has an orphaned key link; found %d current keys for user %d, leaving unchanged",
                sub_id, len(user_keys), user_id,
            )
            continue
        new_key_id = user_keys[0][0]
        cur = await db.execute(
            "SELECT 1 FROM auto_renew_subscriptions WHERE user_id = ? AND key_id = ? AND id != ?",
            (user_id, new_key_id, sub_id),
        )
        if await cur.fetchone():
            logger.error(
                "Auto-renew subscription %d cannot be reattached to key %d for user %d: another record exists",
                sub_id, new_key_id, user_id,
            )
            continue
        await db.execute(
            "UPDATE auto_renew_subscriptions SET key_id = ?, updated_at = strftime('%s','now') WHERE id = ?",
            (new_key_id, sub_id),
        )
        logger.warning(
            "Reattached auto-renew subscription %d to the user's only current key %d (user %d)",
            sub_id, new_key_id, user_id,
        )

    await db.commit()
    logger.info("Database initialized: %s", DB_FILE)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def ensure_user(user_id: int, username: Optional[str] = None) -> None:
    """Create/update a Telegram user record and keep its latest username."""
    db = await get_db()
    await db.execute(
        "INSERT INTO users(user_id, username) "
        "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM account_tombstones WHERE user_id=?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username "
        "WHERE NOT EXISTS (SELECT 1 FROM account_tombstones WHERE user_id=excluded.user_id)",
        (user_id, username, user_id),
    )
    await db.commit()


async def claim_yookassa_checkout_attempt(user_id: int, order_fingerprint: str) -> Optional[dict]:
    """Atomically claim invoice creation and reuse the provider key on retries.

    At most one process may create an invoice for this user concurrently.
    A repeated click on the same order for five minutes opens the same invoice.
    After that interval a deliberate new purchase receives a new key.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", str(order_fingerprint)):
        raise ValueError("order fingerprint must be a SHA256 hex digest")
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            deleted = await db.execute(
                "SELECT 1 FROM account_tombstones WHERE user_id=? LIMIT 1",
                (int(user_id),),
            )
            if await deleted.fetchone():
                raise ValueError("Account was deleted; checkout creation forbidden")
            cur = await db.execute(
                "SELECT order_fingerprint,idempotency_key,created_at,lease_expires_at "
                "FROM yookassa_checkout_attempts WHERE user_id=?",
                (int(user_id),),
            )
            existing = await cur.fetchone()
            if existing and int(existing[3]) > now:
                await db.rollback()
                return None
            if existing and existing[0] == order_fingerprint and now - int(existing[2]) < 300:
                idempotency_key = str(existing[1])
                created_at = int(existing[2])
            else:
                idempotency_key = secrets.token_hex(32)
                created_at = now
            await db.execute(
                """INSERT INTO yookassa_checkout_attempts
                   (user_id,order_fingerprint,idempotency_key,created_at,lease_token,lease_expires_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   order_fingerprint=excluded.order_fingerprint,
                   idempotency_key=excluded.idempotency_key,
                   created_at=excluded.created_at,
                   lease_token=excluded.lease_token,
                   lease_expires_at=excluded.lease_expires_at""",
                (int(user_id), order_fingerprint, idempotency_key, created_at, token, now + 90),
            )
            await db.commit()
            return {"token": token, "idempotency_key": idempotency_key}
        except Exception:
            await db.rollback()
            raise


async def release_yookassa_checkout_attempt(user_id: int, token: str) -> None:
    """Never release a lease acquired by a later checkout operation."""
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute(
            "UPDATE yookassa_checkout_attempts SET lease_token=NULL, lease_expires_at=0 "
            "WHERE user_id=? AND lease_token=?",
            (int(user_id), token),
        )
        await db.commit()


async def save_client_diagnostic(
    user_id: int, platform: str, stage: str, error_code: str, app_version: str,
) -> int | None:
    """Save bounded client-reported error codes, no secrets or raw log uploads.

    One report per minute and at most 30 reports per day limit write spam.
    """
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM client_diagnostics WHERE created_at<?",
                             (now - 30 * 86400,))
            cur = await db.execute(
                "SELECT MAX(created_at),COUNT(*) FROM client_diagnostics "
                "WHERE user_id=? AND created_at>=?",
                (int(user_id), now - 86400),
            )
            last, count = await cur.fetchone()
            if count >= 30 or (last is not None and now - int(last) < 60):
                await db.rollback()
                return None
            cursor = await db.execute(
                "INSERT INTO client_diagnostics(user_id,platform,stage,error_code,app_version,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (int(user_id), platform, stage, error_code, app_version, now),
            )
            await db.commit()
            return int(cursor.lastrowid)
        except Exception:
            await db.rollback()
            raise


async def get_recent_client_diagnostics(user_id: int, limit: int = 10) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id,platform,stage,error_code,app_version,created_at "
        "FROM client_diagnostics WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (int(user_id), max(1, min(int(limit), 20))),
    )
    return [
        dict(zip(("id", "platform", "stage", "error_code", "app_version", "created_at"), row))
        for row in await cur.fetchall()
    ]


VALID_CHURN_REASONS = frozenset({
    "connection", "price", "not_needed", "payment", "other",
})


async def save_churn_feedback(user_id: int, reason: str) -> bool:
    """Optional one-response customer survey; no free-text or sensitive logs."""
    if reason not in VALID_CHURN_REASONS:
        raise ValueError("Unsupported churn feedback reason")
    if await is_account_tombstoned(user_id):
        return False
    db = await get_db()
    async with _db_semaphore:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO churn_feedback(user_id,reason,recorded_at) VALUES(?,?,?)",
            (int(user_id), reason, int(time.time())),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_churn_feedback_summary(days: int = 90) -> dict[str, int]:
    if not 1 <= int(days) <= 365:
        raise ValueError("Unsupported feedback window")
    db = await get_db()
    cur = await db.execute(
        "SELECT reason, COUNT(*) FROM churn_feedback WHERE recorded_at>=? GROUP BY reason",
        (int(time.time()) - days * 86400,),
    )
    return {str(reason): int(count) for reason, count in await cur.fetchall()}


async def create_mobile_google_nonce(nonce_hash: str, expires_at: int) -> None:
    """Persist only a hash of a short-lived Google sign-in challenge."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        await db.execute("DELETE FROM mobile_google_nonces WHERE expires_at<=?", (now,))
        await db.execute(
            "INSERT INTO mobile_google_nonces(nonce_hash,created_at,expires_at) VALUES(?,?,?)",
            (nonce_hash, now, int(expires_at)),
        )
        await db.commit()


async def consume_mobile_google_nonce(nonce_hash: str) -> bool:
    """Atomically consume a still-live Google nonce exactly once."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        cursor = await db.execute(
            "DELETE FROM mobile_google_nonces WHERE nonce_hash=? AND expires_at>?",
            (nonce_hash, now),
        )
        await db.commit()
    return cursor.rowcount == 1


async def create_mobile_trial_bootstrap(
    user_id: int, device_hash: str, expires_at: int,
) -> bool:
    """Reserve at most one bounded first-connect grant for an already claimed trial.

    The expiry is fixed at insertion time; retries cannot extend the bootstrap.
    Only a digest of the installation identifier is stored.
    """
    db = await get_db()
    now = int(time.time())
    if int(expires_at) <= now:
        return False
    async with _db_semaphore:
        await db.execute("DELETE FROM mobile_trial_bootstrap WHERE expires_at<=?", (now,))
        cursor = await db.execute(
            """INSERT OR IGNORE INTO mobile_trial_bootstrap
               (user_id,device_hash,expires_at,created_at) VALUES(?,?,?,?)""",
            (int(user_id), str(device_hash), int(expires_at), now),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_mobile_trial_bootstrap(
    user_id: int, device_hash: str,
) -> Optional[int]:
    """Return the fixed expiry for this account/device bootstrap, if still live."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        cursor = await db.execute(
            """SELECT expires_at FROM mobile_trial_bootstrap
               WHERE user_id=? AND device_hash=? AND expires_at>?""",
            (int(user_id), str(device_hash), now),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else None


async def delete_mobile_trial_bootstrap(user_id: int) -> None:
    """Remove a bootstrap once panel-confirmed device access is available."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute("DELETE FROM mobile_trial_bootstrap WHERE user_id=?", (int(user_id),))
        await db.commit()


async def get_mobile_device_bootstrap(
    user_id: int, device_hash: str, subscription_expiry: int,
) -> Optional[int]:
    """Return a live fixed bootstrap expiry for one account/device/subscription."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        cursor = await db.execute(
            """SELECT expires_at FROM mobile_device_bootstraps
               WHERE user_id=? AND device_hash=? AND subscription_expiry=?""",
            (int(user_id), str(device_hash), int(subscription_expiry)),
        )
        row = await cursor.fetchone()
    if not row or int(row[0]) <= now or int(subscription_expiry) <= now:
        return None
    return int(row[0])


async def reserve_mobile_device_bootstrap(
    user_id: int,
    device_hash: str,
    subscription_expiry: int,
    expires_at: int,
    active_devices: int,
    device_limit: int,
) -> Optional[int]:
    """Atomically reserve one free panel slot for a short device bootstrap.

    Repeating a live account/device/subscription request returns its original
    expiry. Once that grant expires, the same device may retry if capacity is
    still available; expired grants never count as occupied slots.
    """
    db = await get_db()
    now = int(time.time())
    if (int(subscription_expiry) <= now or int(expires_at) <= now
            or int(active_devices) < 0 or int(device_limit) not in (2, 5, 10)):
        return None
    async with _db_semaphore:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Old subscription reservations can no longer authorize access.
            await db.execute(
                "DELETE FROM mobile_device_bootstraps WHERE subscription_expiry<=?",
                (now,),
            )
            existing = await db.execute(
                """SELECT expires_at FROM mobile_device_bootstraps
                   WHERE user_id=? AND device_hash=? AND subscription_expiry=?""",
                (int(user_id), str(device_hash), int(subscription_expiry)),
            )
            row = await existing.fetchone()
            if row:
                original_expiry = int(row[0])
                if original_expiry > now:
                    await db.commit()
                    return original_expiry
                # A failed first connect must not strand this installation
                # forever. Remove only its expired reservation, then reserve
                # again below under the same transaction and capacity check.
                await db.execute(
                    """DELETE FROM mobile_device_bootstraps
                       WHERE user_id=? AND device_hash=? AND subscription_expiry=? AND expires_at<=?""",
                    (int(user_id), str(device_hash), int(subscription_expiry), now),
                )

            pending = await db.execute(
                """SELECT COUNT(*) FROM mobile_device_bootstraps
                   WHERE user_id=? AND subscription_expiry=? AND expires_at>?""",
                (int(user_id), int(subscription_expiry), now),
            )
            pending_count = int((await pending.fetchone())[0])
            if int(active_devices) + pending_count >= int(device_limit):
                await db.rollback()
                return None
            await db.execute(
                """INSERT INTO mobile_device_bootstraps
                   (user_id,device_hash,subscription_expiry,expires_at,created_at)
                   VALUES(?,?,?,?,?)""",
                (int(user_id), str(device_hash), int(subscription_expiry), int(expires_at), now),
            )
            await db.commit()
            return int(expires_at)
        except Exception:
            await db.rollback()
            raise


async def delete_mobile_device_bootstrap(user_id: int, device_hash: str) -> None:
    """Remove only this account/device's reservation after panel confirmation."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            "DELETE FROM mobile_device_bootstraps WHERE user_id=? AND device_hash=?",
            (int(user_id), str(device_hash)),
        )
        await db.commit()


async def create_email_account(email: str, password_hash: str, salt: str, name: str) -> int:
    """Create an email identity without collisions with Telegram or deleted users.

    Existing identity IDs remain unchanged; they require a separately reviewed
    migration which reconciles all subscriptions and payment history.
    """
    normalized_name = str(name or email.split("@")[0]).strip()[:160]
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM app_users WHERE email=?", (email,))
            if await cur.fetchone():
                raise ValueError("Пользователь с таким email уже зарегистрирован")
            user_id = 0
            for _ in range(32):
                candidate = secrets.randbelow(2**62 - 1) + 1
                cur = await db.execute(
                    "SELECT 1 FROM users WHERE user_id=? "
                    "UNION ALL SELECT 1 FROM app_users WHERE user_id=? "
                    "UNION ALL SELECT 1 FROM account_tombstones WHERE user_id=? LIMIT 1",
                    (candidate, candidate, candidate),
                )
                if not await cur.fetchone():
                    user_id = candidate
                    break
            if not user_id:
                raise RuntimeError("Could not allocate a unique account ID")
            now = int(time.time())
            await db.execute(
                "INSERT INTO users(user_id,username) VALUES(?,?)",
                (user_id, normalized_name),
            )
            await db.execute(
                "INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, email, password_hash, salt, normalized_name, now),
            )
            await db.commit()
            return user_id
        except Exception:
            await db.rollback()
            raise


async def get_or_create_google_account(google_sub: str, email: str, name: str) -> dict:
    """Resolve Google identities only by `sub`; never link by email."""
    import secrets

    db = await get_db()
    sub_hash = hashlib.sha256(google_sub.encode("utf-8")).hexdigest()
    internal_email = f"google-{sub_hash[:40]}@identity.bymevpn.invalid"
    normalized_name = str(name or "Google user").strip()[:160] or "Google user"
    async with _db_semaphore:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT user_id FROM google_accounts WHERE google_sub=?", (google_sub,),
            )
            row = await cursor.fetchone()
            if row:
                user_id = int(row[0])
                await db.execute(
                    "UPDATE google_accounts SET email=?,name=? WHERE google_sub=?",
                    (email[:320], normalized_name, google_sub),
                )
                await db.execute(
                    "UPDATE users SET username=? WHERE user_id=?", (normalized_name, user_id),
                )
                await db.execute(
                    "UPDATE app_users SET name=? WHERE user_id=?", (normalized_name, user_id),
                )
                await db.commit()
                return {"user_id": user_id, "email": email[:320], "name": normalized_name}

            user_id = 0
            for _ in range(16):
                candidate = secrets.randbelow(2**62 - 1) + 1
                cursor = await db.execute(
                    "SELECT 1 FROM users WHERE user_id=? UNION ALL "
                    "SELECT 1 FROM app_users WHERE user_id=? LIMIT 1",
                    (candidate, candidate),
                )
                if not await cursor.fetchone():
                    user_id = candidate
                    break
            if not user_id:
                raise RuntimeError("Unable to allocate mobile account id")

            now = int(time.time())
            salt = secrets.token_hex(16)
            # Google accounts cannot use password login with this random verifier.
            password_hash = secrets.token_hex(32)
            await db.execute(
                "INSERT INTO users(user_id,username) VALUES(?,?)",
                (user_id, normalized_name),
            )
            await db.execute(
                "INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, internal_email, password_hash, salt, normalized_name, now),
            )
            await db.execute(
                "INSERT INTO google_accounts(google_sub,user_id,email,name,created_at) "
                "VALUES(?,?,?,?,?)",
                (google_sub, user_id, email[:320], normalized_name, now),
            )
            await db.commit()
            return {"user_id": user_id, "email": email[:320], "name": normalized_name}
        except Exception:
            await db.rollback()
            raise


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
        "UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM account_tombstones WHERE user_id=?)",
        (referrer_id, user_id, user_id),
    )
    # Record only the inviter actually attached to the user. A repeated or
    # concurrent request for a different inviter must not create a phantom row.
    await db.execute(
        "INSERT OR IGNORE INTO referrals(referrer_id, referred_id, signup_bonus_retry) "
        "SELECT referrer_id, user_id, 1 FROM users WHERE user_id=? AND referrer_id=?",
        (user_id, referrer_id),
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
    async with _db_semaphore:
        await db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM trial_claim_reservations WHERE user_id=?", (user_id,))
        await db.commit()
    invalidate_user_cache(user_id)


async def reset_trial_for_user(user_id: int) -> None:
    """Reset trial usage for user (admin function)."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute("UPDATE users SET trial_used=0 WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM trial_claim_reservations WHERE user_id=?", (user_id,))
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
    days = normalize_days(days)
    if days is None:
        logger.error("add_manual_days: rejected out-of-range days for user %s (admin %s)", user_id, admin_id)
        return False
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
    expiry: Optional[int] = None,
) -> int:
    db = await get_db()
    expiry = int(expiry) if expiry is not None else int(time.time()) + days * 86400
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


async def schedule_connection_reminder(
    key_id: int, traffic_baseline: int = 0, delay_seconds: int = 7200,
) -> bool:
    """Schedule one connection nudge for a newly issued key."""
    db = await get_db()
    now = int(time.time())
    cur = await db.execute(
        "UPDATE keys SET connect_reminder_status='pending', connect_reminder_due=?, "
        "connect_traffic_baseline=?, connect_reminder_attempts=0 "
        "WHERE id=? AND connect_reminder_status='disabled'",
        (now + max(0, int(delay_seconds)), max(0, int(traffic_baseline)), key_id),
    )
    await db.commit()
    return cur.rowcount == 1


async def get_due_connection_reminders(limit: int = 25) -> list[dict]:
    db = await get_db()
    now = int(time.time())
    cur = await db.execute(
        "SELECT id, user_id, created, expiry, connect_traffic_baseline, "
        "connect_reminder_attempts FROM keys "
        "WHERE connect_reminder_status='pending' AND connect_reminder_due<=? "
        "ORDER BY connect_reminder_due ASC LIMIT ?",
        (now, max(1, min(int(limit), 100))),
    )
    return [
        {
            "key_id": row[0], "user_id": row[1], "created": row[2],
            "expiry": row[3], "traffic_baseline": row[4], "attempts": row[5],
        }
        for row in await cur.fetchall()
    ]


async def update_connection_reminder(key_id: int, status: str) -> None:
    if status not in {"sent", "connected", "expired", "failed"}:
        raise ValueError("invalid connection reminder status")
    db = await get_db()
    await db.execute(
        "UPDATE keys SET connect_reminder_status=? WHERE id=? AND connect_reminder_status='pending'",
        (status, key_id),
    )
    # Recording the first_connection milestone belongs to the verified
    # connection worker, not this state-only update. A duplicate insert here
    # previously defeated the worker's one-time detection.
    await db.commit()


async def get_due_referral_nudges(*, now: int | None = None, limit: int = 10) -> list[int]:
    """Find accounts with a verified connection at least one day ago.

    Offers are optional and bounded to a 14-day cohort: old accounts must not
    receive surprise promotional campaigns after deployment. No offer is sent
    without a current active key or to anyone who disabled marketing.
    """
    clock = int(time.time() if now is None else now)
    db = await get_db()
    cur = await db.execute(
        """SELECT e.user_id
        FROM analytics_events e
        JOIN users u ON u.user_id=e.user_id
        WHERE e.event_type='first_connection'
          AND COALESCE(u.is_banned,0)=0
          AND NOT EXISTS (
            SELECT 1 FROM marketing_preferences m
            WHERE m.user_id=e.user_id AND m.promotional_opt_out=1
          )
          AND NOT EXISTS (
            SELECT 1 FROM analytics_events n
            WHERE n.user_id=e.user_id AND n.event_type='referral_nudge_attempted'
          )
          AND EXISTS (
            SELECT 1 FROM keys k WHERE k.user_id=e.user_id AND k.expiry>?
          )
        GROUP BY e.user_id
        HAVING MIN(e.timestamp) BETWEEN ? AND ?
        ORDER BY MIN(e.timestamp) ASC
        LIMIT ?""",
        (clock, clock - 14 * 86400, clock - 86400, max(1, min(int(limit), 25))),
    )
    return [int(row[0]) for row in await cur.fetchall()]


async def claim_expiry_notification(user_id: int, expiry: int, bucket: str) -> bool:
    """Durably claim a reminder before contacting Telegram (at-most-once on ambiguity)."""
    db = await get_db()
    cur = await db.execute(
        "INSERT OR IGNORE INTO expiry_notification_deliveries(user_id,expiry,bucket,status,updated_at) "
        "VALUES(?,?,?,'sending',?)",
        (int(user_id), int(expiry), str(bucket), int(time.time())),
    )
    if cur.rowcount != 1:
        cur = await db.execute(
            "UPDATE expiry_notification_deliveries SET status='sending',updated_at=? "
            "WHERE user_id=? AND expiry=? AND bucket=? AND status='failed' AND updated_at<?",
            (int(time.time()), int(user_id), int(expiry), str(bucket), int(time.time()) - 900),
        )
    await db.commit()
    return cur.rowcount == 1


async def finish_expiry_notification(user_id: int, expiry: int, bucket: str, status: str) -> None:
    if status not in {"sent", "blocked", "uncertain", "failed", "skipped"}:
        raise ValueError("invalid expiry notification status")
    db = await get_db()
    await db.execute(
        "UPDATE expiry_notification_deliveries SET status=?,updated_at=? "
        "WHERE user_id=? AND expiry=? AND bucket=? AND status='sending'",
        (status, int(time.time()), int(user_id), int(expiry), str(bucket)),
    )
    await db.commit()


async def retry_connection_reminder(key_id: int, attempts: int) -> None:
    """Back off failed Telegram sends and stop after three attempts."""
    db = await get_db()
    attempts = int(attempts) + 1
    status = "failed" if attempts >= 3 else "pending"
    next_try = int(time.time()) + 900
    await db.execute(
        "UPDATE keys SET connect_reminder_status=?, connect_reminder_attempts=?, "
        "connect_reminder_due=? WHERE id=? AND connect_reminder_status='pending'",
        (status, attempts, next_try, key_id),
    )
    await db.commit()


async def get_or_create_provisioning_intent(
    provider: str, provider_payment_id: str, user_id: int, target_expiry: int,
) -> int:
    """Persist one immutable target expiry per paid order for safe retries."""
    if not provider or not provider_payment_id or user_id <= 0 or target_expiry <= 0:
        raise ValueError("invalid provisioning intent")
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            "INSERT OR IGNORE INTO provisioning_intents(provider, provider_payment_id, user_id, target_expiry) "
            "VALUES(?,?,?,?)",
            (provider, str(provider_payment_id), user_id, int(target_expiry)),
        )
        cur = await db.execute(
            "SELECT user_id, target_expiry FROM provisioning_intents WHERE provider=? AND provider_payment_id=?",
            (provider, str(provider_payment_id)),
        )
        row = await cur.fetchone()
        await db.commit()
    if not row or row[0] != user_id:
        raise ValueError("payment provisioning intent belongs to another user")
    return int(row[1])


async def is_account_tombstoned(user_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM account_tombstones WHERE user_id=? LIMIT 1",
        (int(user_id),),
    )
    return await cursor.fetchone() is not None


async def try_acquire_delivery_lease(
    user_id: int, token: str, ttl_seconds: int = 180,
) -> bool:
    """Claim a cross-process SQLite lease for one account, without waiting.

    The UPSERT is atomic even when two bot processes open separate SQLite
    connections. A crashed holder becomes reclaimable after the TTL.
    """
    if user_id <= 0 or not token or ttl_seconds < 30:
        raise ValueError("invalid delivery lease")
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        # Do not start any new delivery for an erased account. Deletion itself
        # acquires its lease *before* writing the tombstone.
        cursor = await db.execute(
            "SELECT 1 FROM account_tombstones WHERE user_id=? LIMIT 1",
            (user_id,),
        )
        if await cursor.fetchone():
            return False
        cursor = await db.execute(
            """INSERT INTO delivery_leases(user_id,token,expires_at)
               VALUES(?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   token=excluded.token, expires_at=excluded.expires_at
               WHERE delivery_leases.expires_at<=?""",
            (user_id, token, now + ttl_seconds, now),
        )
        await db.commit()
        return cursor.rowcount == 1


async def renew_delivery_lease(
    user_id: int, token: str, ttl_seconds: int = 180,
) -> bool:
    """Extend only a live lease still owned by this worker."""
    if ttl_seconds < 30:
        raise ValueError("invalid delivery lease")
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        cursor = await db.execute(
            """UPDATE delivery_leases SET expires_at=?
               WHERE user_id=? AND token=? AND expires_at>?""",
            (now + ttl_seconds, user_id, token, now),
        )
        await db.commit()
        return cursor.rowcount == 1


async def release_delivery_lease(user_id: int, token: str) -> None:
    """Never release a lease that another worker has subsequently acquired."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            "DELETE FROM delivery_leases WHERE user_id=? AND token=?",
            (user_id, token),
        )
        await db.commit()


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


async def update_user_key_credentials(user_id: int, new_uuid: str, new_subscription_url: str) -> bool:
    """Update every saved link for this user's shared panel client atomically."""
    db = await get_db()
    try:
        async with _db_semaphore:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE keys SET uuid=?, key=? WHERE user_id=?",
                (new_uuid, new_subscription_url, user_id),
            )
            await db.commit()
        return cur.rowcount > 0
    except Exception as e:
        async with _db_semaphore:
            await db.rollback()
        logger.error("Failed to rotate saved VPN credentials for user %s: %s", user_id, e)
        return False


async def remember_hwid_devices(user_id: int, devices: list[dict], monitor_started_at: int) -> list[dict]:
    """Record observed panel device IDs and return newly registered devices to notify about."""
    db = await get_db()
    now = int(time.time())
    pending = []
    async with _db_semaphore:
        for device in devices:
            device_id = int(device["id"])
            first_seen = max(0, int(device.get("first_seen") or 0))
            last_seen = max(0, int(device.get("last_seen") or 0))
            # Existing devices discovered after a bot restart are silently seeded;
            # registrations created since this monitor started trigger a notice.
            muted = now if first_seen <= monitor_started_at else 0
            await db.execute(
                "INSERT OR IGNORE INTO known_hwid_devices(user_id,device_id,first_seen,last_seen,notified_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, device_id, first_seen, last_seen, muted),
            )
            await db.execute(
                "UPDATE known_hwid_devices SET last_seen=MAX(last_seen,?) WHERE user_id=? AND device_id=?",
                (last_seen, user_id, device_id),
            )
            cur = await db.execute(
                "SELECT notified_at FROM known_hwid_devices WHERE user_id=? AND device_id=?",
                (user_id, device_id),
            )
            row = await cur.fetchone()
            if row and not row[0] and first_seen > monitor_started_at:
                pending.append(device)
        await db.commit()
    return pending


async def create_mobile_auth_session(state_hash: str, expires_at: int) -> None:
    """Persist only a hash of the short-lived Telegram login state."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        await db.execute(
            "DELETE FROM mobile_auth_sessions WHERE expires_at<=? OR status='consumed'",
            (now,),
        )
        await db.execute(
            "INSERT INTO mobile_auth_sessions(state_hash,status,created_at,expires_at) VALUES(?,?,?,?)",
            (state_hash, "pending", now, int(expires_at)),
        )
        await db.commit()


async def ensure_mobile_payment_request(
    user_id: int, request_id: str, plan_code: str, provider: str, devices: int = 2,
) -> dict:
    """Atomically reserve an idempotent mobile checkout identity and its terms."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        await db.execute(
            "INSERT OR IGNORE INTO mobile_payment_requests"
            "(user_id,request_id,plan_code,provider,devices,created_at) VALUES(?,?,?,?,?,?)",
            (int(user_id), request_id, plan_code, provider, int(devices), now),
        )
        cur = await db.execute(
            "SELECT plan_code,provider,devices,payment_id,confirmation_url,generation,creation_state "
            "FROM mobile_payment_requests WHERE user_id=? AND request_id=?",
            (int(user_id), request_id),
        )
        row = await cur.fetchone()
        await db.commit()
    if not row:
        raise RuntimeError("mobile payment request reservation failed")
    return {
        "plan_code": row[0], "provider": row[1], "devices": int(row[2] or 2),
        "payment_id": row[3], "confirmation_url": row[4],
        "generation": int(row[5] or 0), "creation_state": row[6] or "ready",
    }


async def claim_mobile_payment_creation(user_id: int, request_id: str, plan_code: str, provider: str) -> bool:
    """Claim one external invoice creation attempt; ambiguous attempts are recovered, not repeated."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE mobile_payment_requests SET creation_state='creating' "
            "WHERE user_id=? AND request_id=? AND plan_code=? AND provider=? "
            "AND payment_id IS NULL AND creation_state='ready'",
            (int(user_id), request_id, plan_code, provider),
        )
        await db.commit()
    return cur.rowcount == 1


async def reset_mobile_payment_creation(
    user_id: int, request_id: str, plan_code: str, provider: str,
) -> bool:
    """Release a create claim only when the provider definitely rejected the attempt."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE mobile_payment_requests SET creation_state='ready' "
            "WHERE user_id=? AND request_id=? AND plan_code=? AND provider=? "
            "AND payment_id IS NULL AND creation_state='creating'",
            (int(user_id), request_id, plan_code, provider),
        )
        await db.commit()
    return cur.rowcount == 1


async def advance_mobile_payment_request_generation(
    user_id: int, request_id: str, plan_code: str, provider: str,
    payment_id: str, generation: int,
) -> bool:
    """Atomically rotate an idempotency generation after provider confirms cancellation."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE mobile_payment_requests "
            "SET payment_id=NULL,confirmation_url=NULL,generation=generation+1,creation_state='ready' "
            "WHERE user_id=? AND request_id=? AND plan_code=? AND provider=? "
            "AND payment_id=? AND generation=?",
            (int(user_id), request_id, plan_code, provider, str(payment_id), int(generation)),
        )
        await db.commit()
    return cur.rowcount == 1


async def save_mobile_payment_request(
    user_id: int, request_id: str, plan_code: str, provider: str,
    payment_id: str, confirmation_url: str,
) -> bool:
    """Persist provider invoice ownership and checkout link without logging either."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE mobile_payment_requests SET payment_id=?,confirmation_url=?,creation_state='created' "
            "WHERE user_id=? AND request_id=? AND plan_code=? AND provider=? "
            "AND (payment_id IS NULL OR payment_id=?)",
            (str(payment_id), confirmation_url, int(user_id), request_id, plan_code, provider, str(payment_id)),
        )
        await db.commit()
    return cur.rowcount == 1


async def save_mobile_crypto_checkout(
    user_id: int, request_id: str, plan_code: str, devices: int, invoice_id: str,
    confirmation_url: str, days: int, amount_rub: int, payload: str,
) -> bool:
    """Atomically bind a Crypto Pay invoice to its owner and durable fulfillment intent."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "UPDATE mobile_payment_requests SET payment_id=?,confirmation_url=?,creation_state='created' "
                "WHERE user_id=? AND request_id=? AND plan_code=? AND provider='crypto' AND devices=? "
                "AND creation_state IN ('creating','created') AND (payment_id IS NULL OR payment_id=?)",
                (str(invoice_id), confirmation_url, int(user_id), request_id, plan_code, int(devices), str(invoice_id)),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT OR IGNORE INTO crypto_pending"
                "(invoice_id,user_id,days,devices,amount_rub,payload) VALUES(?,?,?,?,?,?)",
                (str(invoice_id), int(user_id), int(days), int(devices), int(amount_rub), payload),
            )
            pending_cur = await db.execute(
                "SELECT user_id,days,devices,amount_rub,payload FROM crypto_pending WHERE invoice_id=?",
                (str(invoice_id),),
            )
            pending_row = await pending_cur.fetchone()
            expected = (int(user_id), int(days), int(devices), int(amount_rub), payload)
            if not pending_row or tuple(pending_row) != expected:
                await db.rollback()
                return False
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def get_mobile_payment_request(user_id: int, request_id: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT plan_code,provider,payment_id,confirmation_url FROM mobile_payment_requests "
        "WHERE user_id=? AND request_id=?",
        (int(user_id), request_id),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {"plan_code": row[0], "provider": row[1], "payment_id": row[2], "confirmation_url": row[3]}


async def get_mobile_payment_record(provider: str, payment_id: str) -> Optional[dict]:
    """Return only the owner and status of one provider invoice."""
    db = await get_db()
    normalized = "cryptobot" if provider == "crypto" else provider
    cur = await db.execute(
        "SELECT user_id,status,amount,days,devices FROM payments "
        "WHERE provider=? AND CAST(provider_payment_id AS TEXT)=? "
        "UNION ALL SELECT user_id,'pending',0,0,devices FROM mobile_payment_requests "
        "WHERE provider=? AND payment_id=? "
        "UNION ALL SELECT user_id,'pending',amount_rub,days,devices FROM yookassa_pending "
        "WHERE payment_id=? AND ?='yookassa' "
        "UNION ALL SELECT user_id,'pending',amount_rub,days,devices FROM crypto_pending "
        "WHERE CAST(invoice_id AS TEXT)=? AND ?='crypto' LIMIT 1",
        (normalized, str(payment_id), provider, str(payment_id), str(payment_id), provider, str(payment_id), provider),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {"user_id": int(row[0]), "status": str(row[1]), "amount_rub": int(row[2]),
            "days": int(row[3]), "devices": int(row[4])}


async def get_mobile_payment_request_by_invoice(provider: str, payment_id: str) -> Optional[dict]:
    """Resolve an invoice to its durable mobile checkout request, if any.

    This is used by fulfillment handlers to distinguish an app checkout from
    legacy Telegram invoices and to verify that provider metadata still agrees
    with the server-owned request. It intentionally returns no URL or user
    data beyond the owner ID needed for an authorization check.
    """
    if provider not in ("yookassa", "crypto") or not payment_id:
        return None
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "SELECT user_id,request_id,plan_code,provider,devices,payment_id "
            "FROM mobile_payment_requests WHERE provider=? AND payment_id=? LIMIT 1",
            (provider, str(payment_id)),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": int(row[0]), "request_id": str(row[1]),
        "plan_code": str(row[2]), "provider": str(row[3]),
        "devices": int(row[4]), "payment_id": str(row[5]),
    }


async def list_recent_mobile_invoice_refs(user_id: int, max_age_seconds: int = 86400) -> list[dict]:
    """List unique recent provider invoices for safe legacy check-payment calls."""
    db = await get_db()
    since = int(time.time()) - max(60, min(int(max_age_seconds), 7 * 86400))
    cur = await db.execute(
        "SELECT provider,CAST(provider_payment_id AS TEXT),created FROM payments "
        "WHERE user_id=? AND provider IN ('yookassa','cryptobot') AND created>=? "
        "UNION ALL SELECT 'yookassa',payment_id,created FROM yookassa_pending WHERE user_id=? AND created>=? "
        "UNION ALL SELECT 'crypto',CAST(invoice_id AS TEXT),created FROM crypto_pending WHERE user_id=? AND created>=? "
        "UNION ALL SELECT provider,payment_id,created_at FROM mobile_payment_requests "
        "WHERE user_id=? AND payment_id IS NOT NULL AND created_at>=? "
        "ORDER BY 3 DESC",
        (int(user_id), since, int(user_id), since, int(user_id), since, int(user_id), since),
    )
    rows = await cur.fetchall()
    unique = {}
    for row in rows:
        provider = "crypto" if row[0] == "cryptobot" else str(row[0])
        invoice_id = str(row[1] or "")
        if provider in ("yookassa", "crypto") and invoice_id:
            unique.setdefault((provider, invoice_id), {"provider": provider, "payment_id": invoice_id})
    return list(unique.values())


async def confirm_mobile_auth_session(
    state_hash: str,
    user_id: int,
    username: str,
    full_name: str,
) -> bool:
    """Atomically confirm a not-yet-expired login state once."""
    db = await get_db()
    now = int(time.time())
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE mobile_auth_sessions SET status='authenticated', user_id=?, username=?, full_name=? "
            "WHERE state_hash=? AND status='pending' AND expires_at>?",
            (int(user_id), username[:80], full_name[:160], state_hash, now),
        )
        await db.commit()
    return cur.rowcount == 1


async def poll_mobile_auth_session(state_hash: str) -> Optional[dict]:
   …21135 tokens truncated…nhanced referral statistics with detailed tracking"""
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
        "SELECT COUNT(*) FROM referrals r "
        "JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id "
        "WHERE r.referrer_id=?",
        (referrer_id,)
    )
    total_registrations = (await cur.fetchone())[0]
    
    # Active clients (users with active subscriptions)
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        INNER JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        INNER JOIN keys k ON r.referred_id = k.user_id
        WHERE r.referrer_id=? AND k.expiry > ?
        """,
        (referrer_id, current_time)
    )
    active_clients = (await cur.fetchone())[0]
    
    # Count referrals whose first qualifying paid subscription earned the reward.
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT r.referred_id)
        FROM referrals r
        INNER JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        INNER JOIN referral_events e ON e.referrer_id=r.referrer_id
            AND e.referred_id=r.referred_id AND e.event_type='payment_bonus'
        WHERE r.referrer_id=?
        """,
        (referrer_id,)
    )
    paid_clients = (await cur.fetchone())[0]
    
    # Total bonus days earned from referral_events
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(e.days_awarded), 0)
        FROM referral_events e
        JOIN referrals r ON r.referrer_id=e.referrer_id AND r.referred_id=e.referred_id
        JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        WHERE e.referrer_id=?
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
               (SELECT COUNT(*) FROM referral_events WHERE referrer_id=? AND referred_id=r.referred_id AND event_type='payment_bonus') as payment_count,
               (SELECT COALESCE(SUM(days_awarded), 0) FROM referral_events 
                WHERE referrer_id=? AND referred_id=r.referred_id) as bonus_days
        FROM referrals r
        JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
        WHERE r.referrer_id=?
        ORDER BY r.created DESC
        """,
        (current_time, referrer_id, referrer_id, referrer_id)
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
    
    # Keep payment volume separate from recognized revenue: failed/recovery
    # entries are not revenue and Stars must never be labelled as rubles.
    cur = await db.execute("SELECT COUNT(*) FROM payments WHERE status='success' AND method!='trial'")
    total_payments = (await cur.fetchone())[0]
    
    # Ruble revenue only; non-RUB currencies are shown separately in payment stats.
    cur = await db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments "
        "WHERE status='success' AND currency='RUB' AND method!='trial'"
    )
    total_revenue = (await cur.fetchone())[0]
    
    # Today's revenue
    today_start = current_time - (current_time % 86400)
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='success' AND currency='RUB' AND method!='trial' AND created >= ?", (today_start,))
    today_revenue = (await cur.fetchone())[0]
    
    # Week revenue
    week_start = current_time - ((current_time // 86400) % 7) * 86400
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='success' AND currency='RUB' AND method!='trial' AND created >= ?", (week_start,))
    week_revenue = (await cur.fetchone())[0]
    
    # Month revenue
    month_start = current_time - ((current_time // 86400) % 30) * 86400
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='success' AND currency='RUB' AND method!='trial' AND created >= ?", (month_start,))
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
        "SELECT user_id, referrer_id, trial_used, total_paid, created, username, is_banned, ban_reason "
        "FROM users WHERE user_id=?",
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
        "created": row[4],
        "username": row[5],
        "is_banned": bool(row[6]),
        "ban_reason": row[7],
    }


async def delete_user_and_keys(user_id: int, *, source: str = "admin") -> list[str]:
    """Erase account-owned access and identity, keeping financial evidence.

    Call ONLY after every configured 3x-ui inbound confirms remote deletion.
    A tombstone blocks old mobile sessions and Telegram auto-registration.
    Amounts, provider transaction IDs and refunds are retained for accounting
    and dispute reconciliation; this is *not* an unqualified GDPR certification.
    """
    # Use an independent writer connection. Other legacy helpers do not all
    # share _db_semaphore; their statements must never accidentally enter this
    # irreversible multi-table deletion transaction.
    async with _db_semaphore:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT uuid FROM keys WHERE user_id=?", (user_id,),
                )
                uuids = [row[0] for row in await cursor.fetchall() if row[0]]
                await db.execute(
                    "INSERT OR IGNORE INTO account_tombstones(user_id,deleted_at,source) VALUES(?,?,?)",
                    (user_id, int(time.time()), source if source in ("admin", "self_service") else "admin"),
                )
                await db.execute(
                    """UPDATE auto_renew_subscriptions
                       SET status='cancelled', payment_method_id='',
                           payment_method_title='', payment_method_type='',
                           next_retry_at=NULL, updated_at=strftime('%s','now')
                       WHERE user_id=?""",
                    (user_id,),
                )
                # Explicit allowlist of identity/marketing records. Never delete
                # financial transactions or payment-recovery rows in this routine.
                known_tables = {
                    row[0] for row in await (await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )).fetchall()
                }
                personal_tables = (
                    ("keys", "user_id"), ("app_users", "user_id"),
                    ("google_accounts", "user_id"), ("email_auth", "user_id"),
                    ("checkout_drafts", "user_id"), ("known_hwid_devices", "user_id"),
                    ("mobile_trial_bootstrap", "user_id"),
                    ("mobile_device_bootstraps", "user_id"),
                    ("trial_claim_reservations", "user_id"),
                    ("mobile_auth_sessions", "user_id"),
                    ("mobile_refresh_sessions", "user_id"),
                    ("client_diagnostics", "user_id"),
                    ("churn_feedback", "user_id"),
                    ("yookassa_checkout_attempts", "user_id"),
                    ("marketing_preferences", "user_id"),
                    ("analytics_events", "user_id"),
                    ("key_errors", "user_id"),
                    ("ad_applications", "user_id"), ("ad_notifications", "user_id"),
                    ("ad_audit_log", "user_id"),
                    ("giveaway_participants", "user_id"),
                    ("giveaway_notifications", "user_id"),
                    ("broadcast_recipients", "user_id"),
                    ("referral_clicks", "referrer_id"),
                    ("referrals", "referrer_id"), ("referrals", "referred_id"),
                    ("referral_events", "referrer_id"), ("referral_events", "referred_id"),
                    ("ref_bonus_claims", "referrer_id"), ("ref_bonus_claims", "referred_id"),
                    ("referral_payment_rewards", "referrer_id"),
                    ("referral_payment_rewards", "referred_id"),
                )
                for table, column in personal_tables:
                    if table in known_tables:
                        await db.execute(
                            f"DELETE FROM {table} WHERE {column}=?", (user_id,),
                        )
                await db.execute(
                    """UPDATE users
                       SET username=NULL, email=NULL, referrer_id=NULL,
                           trial_used=1, is_banned=1, ban_reason='account_deleted'
                       WHERE user_id=?""",
                    (user_id,),
                )
                await db.commit()
                try:
                    invalidate_user_cache(user_id)
                    invalidate_subscription_cache(user_id)
                except Exception:
                    logger.warning("Could not invalidate caches after account erasure for %d", user_id)
                return uuids
            except Exception:
                await db.rollback()
                raise


async def set_key_days(key_id: int, days: int) -> bool:
    """Extend key to specified days in both DB and 3x-UI panel"""
    days = normalize_days(days)
    if days is None:
        logger.error("set_key_days: rejected out-of-range days for key %s", key_id)
        return False
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
            u.username,
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
            "username": row[4],
            "total_keys": row[5] or 0,
            "active_keys": row[6] or 0,
        }
        for row in rows
    ]


async def get_payment_stats() -> dict:
    """Get payment statistics without adding incompatible currencies together."""
    db = await get_db()
    current_time = int(time.time())

    async def totals(where: str = "", params: tuple = ()) -> tuple[int, dict[str, int]]:
        suffix = f" AND {where}" if where else ""
        cur = await db.execute(
            "SELECT currency, COUNT(*), COALESCE(SUM(amount), 0) FROM payments "
            f"WHERE status='success'{suffix} GROUP BY currency",
            params,
        )
        rows = await cur.fetchall()
        by_currency = {str(row[0]): int(row[2] or 0) for row in rows}
        return sum(int(row[1] or 0) for row in rows), by_currency

    total_count, total_by_currency = await totals()
    today_start = current_time - (current_time % 86400)
    today_count, today_by_currency = await totals("created >= ?", (today_start,))
    month_start = current_time - ((current_time // 86400) % 30) * 86400
    month_count, month_by_currency = await totals("created >= ?", (month_start,))

    cur = await db.execute(
        "SELECT method, currency, COUNT(*), COALESCE(SUM(amount), 0) "
        "FROM payments WHERE status='success' GROUP BY method, currency"
    )
    by_method = {}
    for row in await cur.fetchall():
        method, currency, count, amount = row
        item = by_method.setdefault(method, {"count": 0, "by_currency": {}})
        item["count"] += int(count or 0)
        item["by_currency"][str(currency)] = int(amount or 0)

    # Legacy sum aliases are deliberately RUB-only; the detailed map carries
    # Stars/XTR and any future currencies in their native units.
    total_sum = total_by_currency.get("RUB", 0)
    today_sum = today_by_currency.get("RUB", 0)
    month_sum = month_by_currency.get("RUB", 0)
    
    return {
        "total": {
            "count": total_count or 0,
            "sum": total_sum,
            "by_currency": total_by_currency,
        },
        "today": {
            "count": today_count or 0,
            "sum": today_sum,
            "by_currency": today_by_currency,
        },
        "month": {
            "count": month_count or 0,
            "sum": month_sum,
            "by_currency": month_by_currency,
        },
        "by_method": by_method
    }


async def set_key_expiry(key_id: int, expiry: int, limit_ip: Optional[int] = None) -> bool:
    """Set a key's absolute expiry/device limit; used for safe compensation after provisioning failures."""
    try:
        expiry = int(expiry)
    except (TypeError, ValueError):
        return False
    if expiry < 0:
        return False

    db = await get_db()
    async with _db_semaphore:
        try:
            cur = await db.execute("SELECT user_id FROM keys WHERE id=?", (key_id,))
            row = await cur.fetchone()
            if not row:
                return False
            user_id = row[0]
            cur = await db.execute(
                """
                UPDATE keys
                SET expiry = ?,
                    limit_ip = CASE WHEN ? IS NOT NULL AND ? > 0 THEN ? ELSE limit_ip END
                WHERE id = ?
                """,
                (expiry, limit_ip, limit_ip, limit_ip, key_id),
            )
            await db.commit()
            if cur.rowcount == 0:
                return False
            try:
                from cache import invalidate_user_cache, invalidate_subscription_cache
                invalidate_user_cache(user_id)
                invalidate_subscription_cache(user_id)
            except Exception:
                pass
            return True
        except Exception as e:
            await db.rollback()
            logger.exception("set_key_expiry failed for key %s: %s", key_id, e)
            return False


async def extend_key(key_id: int, additional_days: int, limit_ip: Optional[int] = None) -> bool:
    """Extend key by additional days from max(current_expiry, now) and optionally update device limit atomically."""
    additional_days = normalize_days(additional_days)
    if additional_days is None:
        logger.error("extend_key: rejected out-of-range days for key %s", key_id)
        return False
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
    
    # Refund amounts may be in different currencies (e.g. RUB and Telegram Stars/XTR).
    # Keep legacy aggregate fields for compatibility, but expose currency-separated totals
    # so the admin UI never presents a mixed-currency sum as rubles.
    cur = await db.execute(
        "SELECT currency, COUNT(*), COALESCE(SUM(amount), 0) FROM refunds GROUP BY currency"
    )
    by_currency_total = {
        row[0]: {"count": row[1], "sum": row[2]}
        for row in await cur.fetchall()
    }
    cur = await db.execute(
        "SELECT currency, COUNT(*), COALESCE(SUM(amount), 0) FROM refunds WHERE created >= ? GROUP BY currency",
        (thirty_days_ago,),
    )
    by_currency_30d = {
        row[0]: {"count": row[1], "sum": row[2]}
        for row in await cur.fetchall()
    }

    return {
        "count_total": total_refunds,
        "sum_total": total_refunded,
        "count_30d": count_30d,
        "sum_30d": sum_30d,
        "by_currency_total": by_currency_total,
        "by_currency_30d": by_currency_30d,
        # Backward-compatible aliases; new UI uses the currency-separated fields.
        "total_refunds": total_refunded,
        "refund_count": total_refunds,
    }


# NOTE: get_all_users_csv() used to be defined twice in this file (a simple
# version here, a proper csv.writer-based version further below). Python
# kept only the later one; this dead duplicate has been removed.


# ---------------------------------------------------------------------------
# Expiry Notifications
# ---------------------------------------------------------------------------

async def get_keys_nearing_expiry(days_min: int = 1, days_max: int = 3) -> list[dict]:
    """Fetch due keys, segmenting free-trial users from paying customers."""
    db = await get_db()
    now = int(time.time())
    min_expiry = now + days_min * 86400
    max_expiry = now + days_max * 86400
    cur = await db.execute(
        """
        SELECT k.id, k.user_id, k.expiry, k.last_notification_at,
               CASE WHEN COALESCE(u.trial_used, 0) = 1
                   AND NOT EXISTS (
                       SELECT 1 FROM payments p
                       WHERE p.user_id = k.user_id AND p.status = 'success'
                         AND p.method != 'trial'
                   )
               THEN 1 ELSE 0 END AS is_trial
        FROM keys k
        LEFT JOIN users u ON u.user_id = k.user_id
        WHERE k.expiry BETWEEN ? AND ? AND k.expiry > ?
        ORDER BY k.expiry ASC, k.id ASC
        """,
        (min_expiry, max_expiry, now),
    )
    return [
        {
            "key_id": row[0], "user_id": row[1], "expiry": row[2],
            "last_notification_at": row[3], "is_trial": bool(row[4]),
        }
        for row in await cur.fetchall()
    ]


async def get_recently_expired_keys(*, days_min: int = 2, days_max: int = 4) -> list[dict]:
    """One win-back candidate per lapsed account; never target active access."""
    db = await get_db()
    now = int(time.time())
    lower = now - int(days_max) * 86400
    upper = now - int(days_min) * 86400
    cur = await db.execute(
        """
        SELECT k.id, k.user_id, k.expiry, k.last_notification_at
        FROM keys k
        JOIN users u ON u.user_id = k.user_id
        WHERE k.expiry BETWEEN ? AND ?
          AND COALESCE(u.is_banned, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM keys active
              WHERE active.user_id = k.user_id AND active.expiry > ?
          )
          AND k.id = (
              SELECT k2.id FROM keys k2
              WHERE k2.user_id = k.user_id
              ORDER BY k2.expiry DESC, k2.id DESC LIMIT 1
          )
        ORDER BY k.expiry DESC, k.id DESC
        LIMIT 250
        """,
        (lower, upper, now),
    )
    return [
        {"key_id": row[0], "user_id": row[1],
         "expiry": row[2], "last_notification_at": row[3]}
        for row in await cur.fetchall()
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

def _canonical_promo_type(promo_type: str) -> str:
    """Keep existing admin-created aliases compatible with checkout."""
    return {"fixed": "fixed_rub", "days": "free_days"}.get(promo_type, promo_type)


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
            (code.upper(), _canonical_promo_type(promo_type), discount_value, max_uses, 0, tariff_binding, start_date, expires_at, 1, current_time)
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

    promo_type = _canonical_promo_type(row[1])
    try:
        value = float(row[2])
        if not math.isfinite(value) or value <= 0:
            return None
        if promo_type == "percent":
            if value > 100:
                return None
            discount_value = row[2]
        elif promo_type in ("fixed_rub", "free_days"):
            if not value.is_integer():
                return None
            discount_value = int(value)
        else:
            return None
    except (TypeError, ValueError, OverflowError):
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
        "promo_type": promo_type,
        "discount_value": discount_value,
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
    """Record a fulfilled order's promo usage and counter as one transaction."""
    try:
        # A dedicated connection keeps this transaction separate from unrelated
        # coroutines sharing the regular connection. Existing paid orders must
        # still be recorded even if the code has since expired or hit its cap.
        async with aiosqlite.connect(DB_FILE, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """INSERT OR IGNORE INTO promo_code_uses(code, user_id, used_at)
                   SELECT code, ?, ? FROM promo_codes WHERE code=?""",
                (user_id, int(time.time()), code.upper()),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code=?",
                (code.upper(),),
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
                "promo_type": _canonical_promo_type(row[1]),
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


async def log_analytics_event_once(
    user_id: int, event_type: str, details: Optional[str] = None,
) -> bool:
    """Insert one milestone per user, returning whether this was its first insert.

    The single atomic SQL INSERT plus shared writer guard allows retrying
    workers to skip duplicate notifications without introducing a new table.
    """
    try:
        db = await get_db()
        async with _db_semaphore:
            cur = await db.execute(
                "INSERT INTO analytics_events(user_id,event_type,timestamp,details) "
                "SELECT ?,?,strftime('%s','now'),? WHERE NOT EXISTS "
                "(SELECT 1 FROM analytics_events WHERE user_id=? AND event_type=?)",
                (int(user_id), str(event_type), details, int(user_id), str(event_type)),
            )
            await db.commit()
            return cur.rowcount == 1
    except Exception as exc:
        logger.debug("Failed to record milestone %s: %s", event_type, exc)
        return False


async def save_checkout_draft(
    user_id: int, months: int, devices: int, *, renew_key_id: int | None = None,
) -> None:
    """Persist a selection to recover safely after MemoryStorage is restarted.

    Nothing here authorizes a payment. A resumed user must revisit the full
    order page with a freshly calculated price and choose a provider again.
    """
    if (int(user_id) <= 0 or int(months) not in (1, 3, 6, 12)
            or int(devices) not in (2, 5, 10)
            or (renew_key_id is not None and int(renew_key_id) < 0)):
        raise ValueError("invalid checkout selection")
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            """INSERT INTO checkout_drafts(user_id,months,devices,renew_key_id,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            months=excluded.months, devices=excluded.devices,
            renew_key_id=excluded.renew_key_id, updated_at=excluded.updated_at""",
            (int(user_id), int(months), int(devices), int(renew_key_id or 0), int(time.time())),
        )
        await db.commit()


async def get_checkout_draft(user_id: int, *, max_age: int = 86400) -> dict | None:
    """Return a recent saved plan, never an amount or payment authorization."""
    db = await get_db()
    cur = await db.execute(
        "SELECT months,devices,renew_key_id,updated_at FROM checkout_drafts WHERE user_id=?",
        (int(user_id),),
    )
    row = await cur.fetchone()
    if not row:
        return None
    months, devices, renew_key_id, saved_at = (int(value) for value in row)
    if (months not in (1, 3, 6, 12)
            or devices not in (2, 5, 10) or renew_key_id < 0
            or saved_at < int(time.time()) - max(1, int(max_age))):
        return None
    return {"months": months, "devices": devices, "renew_key_id": renew_key_id}


async def get_checkout_conversion_snapshot(days: int = 30) -> dict:
    """Real registration-cohort funnel, with verified successful payments.

    Amounts are grouped by their recorded currency, never added across XTR,
    RUB and crypto. No gross revenue is passed off as net profit.
    """
    days = max(1, min(int(days), 365))
    cutoff = int(time.time()) - days * 86400
    db = await get_db()
    cur = await db.execute(
        """SELECT COUNT(*) FROM users WHERE created>=?""", (cutoff,),
    )
    registered = int((await cur.fetchone())[0])
    async def event_users(kind: str) -> int:
        cur = await db.execute(
            """SELECT COUNT(DISTINCT e.user_id) FROM analytics_events e
            JOIN users u ON u.user_id=e.user_id
            WHERE u.created>=? AND e.timestamp>=u.created AND e.event_type=?""",
            (cutoff, kind),
        )
        return int((await cur.fetchone())[0])
    views = await event_users("tariffs_viewed")
    selections = await event_users("tariff_selected")
    started = await event_users("checkout_started")
    cur = await db.execute(
        """SELECT COUNT(DISTINCT p.user_id)
        FROM payments p JOIN users u ON u.user_id=p.user_id
        WHERE u.created>=? AND p.created>=u.created AND p.status='success' AND p.amount>0""",
        (cutoff,),
    )
    paid = int((await cur.fetchone())[0])
    cur = await db.execute(
        """SELECT COUNT(DISTINCT p.user_id)
        FROM payments p JOIN users u ON u.user_id=p.user_id
        WHERE u.created>=? AND p.created>=u.created AND p.status='success' AND p.amount>0
          AND EXISTS (
            SELECT 1 FROM analytics_events e
            WHERE e.user_id=p.user_id AND e.event_type='tariffs_viewed'
              AND e.timestamp BETWEEN u.created AND p.created
          )""",
        (cutoff,),
    )
    paid_from_views = int((await cur.fetchone())[0])
    cur = await db.execute(
        """SELECT p.currency, COUNT(*), COALESCE(SUM(p.amount),0)
        FROM payments p JOIN users u ON u.user_id=p.user_id
        WHERE u.created>=? AND p.created>=u.created AND p.status='success' AND p.amount>0
        GROUP BY p.currency ORDER BY p.currency""",
        (cutoff,),
    )
    by_currency = {
        str(currency): {"payments": int(count), "gross_amount": int(amount)}
        for currency, count, amount in await cur.fetchall()
    }
    return {
        "days": days, "registered": registered, "views": views,
        "selections": selections, "started": started, "paid": paid,
        "paid_from_views": paid_from_views,
        "purchase_pct": 100 * paid_from_views / views if views else None,
        "by_currency": by_currency,
    }


async def get_conversion_funnel(days: int = 30) -> dict:
    """Registered-user cohort; repeated /start must not count as new acquisition."""
    days = max(1, min(int(days), 365))
    since = int(time.time()) - days * 86400
    db = await get_db()
    cur = await db.execute(
        """
        WITH cohort AS (
          SELECT u.user_id, u.created AS started_at,
            COALESCE(
              (SELECT NULLIF(first_event.source, '')
               FROM analytics_events first_event
               WHERE first_event.user_id=u.user_id
                 AND first_event.event_type='bot_started'
               ORDER BY first_event.timestamp ASC, first_event.id ASC LIMIT 1),
              NULLIF(u.source, ''), 'direct'
            ) AS start_source
          FROM users u WHERE u.created>=?
        )
        SELECT c.start_source AS source,
          COUNT(DISTINCT c.user_id),
          COUNT(DISTINCT CASE WHEN e.event_type='trial_started' THEN c.user_id END),
          COUNT(DISTINCT CASE WHEN EXISTS (
            SELECT 1 FROM payments p
            WHERE p.user_id=c.user_id AND p.status='success' AND p.amount>0
              AND p.created>=c.started_at
          ) THEN c.user_id END),
          COUNT(DISTINCT CASE WHEN e.event_type='first_connection' THEN c.user_id END),
          COUNT(DISTINCT CASE WHEN e.event_type='subscription_renewed'
            AND COALESCE(e.amount,0)>0 THEN c.user_id END)
        FROM cohort c JOIN users u ON u.user_id=c.user_id
        LEFT JOIN analytics_events e ON e.user_id=c.user_id AND e.timestamp>=c.started_at
        GROUP BY 1 ORDER BY 2 DESC
        """,
        (since,),
    )
    rows = await cur.fetchall()
    columns = ("started", "trial", "paid", "connected", "renewed")
    by_source = {
        row[0]: dict(zip(columns, (int(value or 0) for value in row[1:])))
        for row in rows
    }
    totals = {name: sum(source[name] for source in by_source.values()) for name in columns}
    return {"days": days, "totals": totals, "by_source": by_source}


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
    description: str = "",
    prize_days: int = 30,
    winners_count: int = 1,
    end_date: int = 0,
    *,
    created_by: int | None = None,
    automation_key: str | None = None,
    period_start: int | None = None,
    announcement_version: int = 0,
) -> int:
    """Create a giveaway, optionally with an idempotency key for automation."""
    db = await get_db()
    async with _db_semaphore:
        if automation_key:
            cur = await db.execute(
                """INSERT OR IGNORE INTO giveaways
                (title, description, prize_days, winners_count, end_date, is_active,
                 created_at, created_by, automation_key, period_start, announcement_version)
                VALUES(?,?,?,?,?,1,strftime('%s','now'),?,?,?,?)""",
                (title, description, prize_days, winners_count, end_date, created_by, automation_key, period_start, announcement_version),
            )
            if cur.rowcount == 0:
                cur = await db.execute("SELECT id FROM giveaways WHERE automation_key=?", (automation_key,))
                row = await cur.fetchone()
                await db.commit()
                return int(row[0]) if row else 0
        else:
            cur = await db.execute(
                """INSERT INTO giveaways
                (title, description, prize_days, winners_count, end_date, is_active,
                 created_at, created_by, period_start, announcement_version)
                VALUES(?,?,?,?,?,1,strftime('%s','now'),?,?,?)""",
                (title, description, prize_days, winners_count, end_date, created_by, period_start, announcement_version),
            )
        await db.commit()
        return cur.lastrowid


async def get_giveaway_by_automation_key(automation_key: str) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT id FROM giveaways WHERE automation_key=?", (automation_key,)
    )
    row = await cur.fetchone()
    return await get_giveaway_by_id(row[0]) if row else None


async def get_active_automated_giveaway(now: int | None = None) -> dict | None:
    db = await get_db()
    now = int(now or time.time())
    cur = await db.execute(
        "SELECT id FROM giveaways WHERE automation_key IS NOT NULL AND is_active=1 "
        "AND end_date>? ORDER BY end_date LIMIT 1", (now,)
    )
    row = await cur.fetchone()
    return await get_giveaway_by_id(row[0]) if row else None


async def update_automated_monthly_giveaway(
    giveaway_id: int, title: str, end_date: int, period_start: int,
    winners_count: int = 3, prize_days: int = 30,
    announcement_version: int = 1,
) -> bool:
    """Update an existing monthly campaign; return whether its public terms changed."""
    db = await get_db()
    cur = await db.execute(
        "SELECT title, end_date, period_start, winners_count, prize_days, announcement_version "
        "FROM giveaways WHERE id=? AND automation_key LIKE 'auto-monthly-%'",
        (giveaway_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False
    values = (title, int(end_date), int(period_start), int(winners_count), int(prize_days), int(announcement_version))
    if tuple(row) == values:
        return False
    async with _db_semaphore:
        await db.execute(
            "UPDATE giveaways SET title=?, end_date=?, period_start=?, winners_count=?, prize_days=?, "
            "announcement_version=? WHERE id=?",
            (*values, giveaway_id),
        )
        await db.commit()
    return True


async def get_all_giveaways() -> list[dict]:
    """Get all giveaways."""
    db = await get_db()
    cur = await db.execute("SELECT id, title, description, prize_days, winners_count, end_date, is_active, created_at FROM giveaways ORDER BY id DESC")
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "title": r[1], "description": r[2], "prize_days": r[3],
            "winners_count": r[4], "winner_count": r[4], "end_date": r[5], "end_time": r[5],
            "is_active": r[6], "created_at": r[7],
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
            "winners_count": r[4], "winner_count": r[4], "end_date": r[5], "end_time": r[5],
            "is_active": r[6], "created_at": r[7],
        }
        for r in rows
    ]


async def get_giveaway_by_id(giveaway_id: int) -> dict | None:
    """Get a giveaway by ID."""
    db = await get_db()
    cur = await db.execute(
        "SELECT id, title, description, prize_days, winners_count, end_date, is_active, created_at, period_start FROM giveaways WHERE id = ?",
        (giveaway_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "title": row[1], "description": row[2], "prize_days": row[3],
        "winners_count": row[4], "winner_count": row[4], "end_date": row[5], "end_time": row[5],
        "is_active": row[6], "created_at": row[7], "period_start": row[8],
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

    channel_ref is a lowercased public username or a case-sensitive private
    invite reference. Different private invite links can point to one channel,
    so that case still requires manual verification.
    """
    db = await get_db()
    if not channel_ref and channel_username:
        channel_ref = channel_username.strip().lower()
    cur = await db.execute(
        """INSERT INTO ad_applications
        (user_id, channel_id, channel_username, subscriber_count, bonus_months, campaign_id, channel_ref, status)
        SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (
            SELECT 1 FROM ad_applications
            WHERE (? IS NOT NULL AND channel_id=?)
               OR (? IS NOT NULL AND (channel_ref=? OR channel_ref=?))
        )""",
        (user_id, channel_id, channel_username, subscriber_count, bonus_months, campaign_id, channel_ref, status,
         channel_id, channel_id, channel_ref, channel_ref, channel_ref.lower() if channel_ref else channel_ref),
    )
    await db.commit()
    if cur.rowcount != 1:
        raise ValueError("unique channel application already exists")
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


async def get_pending_ad_reward_retries(limit: int = 10) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        """SELECT id, user_id, bonus_months FROM ad_applications WHERE status='grant_pending'
           ORDER BY grant_attempt_ms IS NOT NULL, grant_attempt_ms, created_at, id LIMIT ?""",
        (int(limit),),
    )
    return [{"id": row[0], "user_id": row[1], "bonus_months": row[2]} for row in await cur.fetchall()]


async def mark_ad_reward_attempt(app_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE ad_applications SET grant_attempt_ms=? WHERE id=? AND status='grant_pending'",
        (time.time_ns() // 1_000_000, app_id),
    )
    await db.commit()


async def get_ad_applications_due_for_retention(now: int | None = None, limit: int = 100) -> list[dict]:
    """Applications whose 30-day post-retention period is due for manual review."""
    db = await get_db()
    now = int(now or time.time())
    cur = await db.execute(
        """SELECT id, user_id, channel_id, channel_username, channel_ref, subscriber_count, bonus_months,
        status, post_url, post_message_id, campaign_id, created_at, published_at,
        verification_due_at, approved_at, rejected_at, rejection_reason
        FROM ad_applications
        WHERE status IN ('approved', 'retention_issue') AND verification_due_at IS NOT NULL
          AND verification_due_at <= ?
        ORDER BY verification_due_at ASC LIMIT ?""",
        (now, int(limit)),
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
    expected_statuses: list[str] | None = None,
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
    where = "id = ?"
    if expected_statuses:
        where += " AND status IN (" + ",".join("?" for _ in expected_statuses) + ")"
        params.extend(expected_statuses)
    cur = await db.execute(f"UPDATE ad_applications SET {', '.join(updates)} WHERE {where}", params)
    await db.commit()
    return cur.rowcount > 0


async def delete_user_ad_application(application_id: int, user_id: int) -> bool:
    """Delete a user's unfinished ad request; completed/rejected history is immutable."""
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM ad_applications WHERE id=? AND user_id=? "
        "AND status IN ('draft','channel_pending','waiting_for_post','post_submitted','active_30_days','rejected')",
        (application_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def channel_already_used(channel_id: int | None, channel_ref: str | None = None) -> bool:
    """Check whether this channel has an existing ad request or completed placement."""
    db = await get_db()
    if channel_ref:
        cur = await db.execute(
            "SELECT 1 FROM ad_applications WHERE channel_ref = ? OR channel_ref = ? LIMIT 1",
            (channel_ref, channel_ref.lower()),
        )
        if await cur.fetchone():
            return True
    if channel_id is not None:
        cur = await db.execute(
            "SELECT 1 FROM ad_applications WHERE channel_id = ? LIMIT 1",
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
    """New application reward; historical applications keep their stored terms."""
    from constants import AD_BONUS_TIERS
    count = int(subscriber_count)
    if count <= 0:
        return 0
    for minimum, maximum, months in AD_BONUS_TIERS:
        if count >= minimum and (maximum is None or count <= maximum):
            return months
    return 0

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
    """Join only a live giveaway; repeated starts do not duplicate entries."""
    db = await get_db()
    async with _db_semaphore:
        try:
            cur = await db.execute(
                "SELECT is_active, end_date FROM giveaways WHERE id=?", (giveaway_id,)
            )
            giveaway = await cur.fetchone()
            if not giveaway or not giveaway[0] or int(giveaway[1]) <= int(time.time()):
                return False
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


async def get_giveaway_entry_status(giveaway_id: int, user_id: int) -> dict | None:
    """Return contest state, entry state and valid referrals for the campaign period."""
    db = await get_db()
    cur = await db.execute(
        """SELECT g.is_active, g.end_date, COALESCE(g.period_start,g.created_at),
        EXISTS(SELECT 1 FROM giveaway_participants p WHERE p.giveaway_id=g.id AND p.user_id=?),
        (SELECT COUNT(*) FROM referrals r
          JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id
          WHERE r.referrer_id=?
          AND r.created>=COALESCE(g.period_start,g.created_at) AND r.created<g.end_date)
        FROM giveaways g WHERE g.id=?""",
        (user_id, user_id, giveaway_id),
    )
    row = await cur.fetchone()
    if not row:
        return None
    is_open = bool(row[0]) and int(row[1]) > int(time.time())
    return {
        "is_open": is_open,
        "end_date": int(row[1]),
        "period_start": int(row[2]),
        "joined": bool(row[3]),
        "referral_count": int(row[4] or 0),
        "qualified": int(row[4] or 0) >= 1,
    }


async def get_giveaway_participants_count(giveaway_id: int) -> int:
    """Get number of participants in a giveaway."""
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,))
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_due_giveaways(now: int | None = None) -> list[dict]:
    db = await get_db()
    now = int(now or time.time())
    cur = await db.execute(
        "SELECT id, title, prize_days, winners_count, end_date FROM giveaways "
        "WHERE is_active=1 AND end_date<=? ORDER BY end_date", (now,)
    )
    rows = await cur.fetchall()
    return [
        {"id": r[0], "title": r[1], "prize_days": r[2], "winners_count": r[3], "end_date": r[4]}
        for r in rows
    ]


async def create_giveaway_notification_snapshot(giveaway_id: int) -> int:
    """Queue one monthly promotion for each user who has not opted out."""
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            """INSERT OR IGNORE INTO giveaway_notifications(giveaway_id, user_id, status)
            SELECT ?, u.user_id, 'pending' FROM users u
            LEFT JOIN marketing_preferences p ON p.user_id=u.user_id
            WHERE COALESCE(p.promotional_opt_out, 0)=0""",
            (giveaway_id,),
        )
        await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*) FROM giveaway_notifications WHERE giveaway_id=?", (giveaway_id,)
    )
    row = await cur.fetchone()
    return int(row[0] or 0)


async def get_pending_giveaway_notifications(giveaway_id: int, limit: int = 50) -> list[int]:
    db = await get_db()
    # A process may stop after reserving a delivery but before recording its
    # outcome. Preserve at-most-once behavior and make the ambiguous attempt
    # visible instead of silently retrying it to the user.
    cutoff = int(time.time()) - 30 * 60
    async with _db_semaphore:
        await db.execute(
            """UPDATE giveaway_notifications SET status='uncertain'
            WHERE giveaway_id=? AND status='sending' AND sent_at<?""",
            (giveaway_id, cutoff),
        )
        await db.commit()
    cur = await db.execute(
        """SELECT n.user_id FROM giveaway_notifications n
        LEFT JOIN marketing_preferences p ON p.user_id=n.user_id
        WHERE n.giveaway_id=? AND n.status='pending'
          AND COALESCE(p.promotional_opt_out, 0)=0
        ORDER BY n.user_id LIMIT ?""",
        (giveaway_id, int(limit)),
    )
    return [int(r[0]) for r in await cur.fetchall()]


async def claim_giveaway_notification(giveaway_id: int, user_id: int) -> bool:
    """Atomically reserve one campaign delivery; claimed users are never requeued."""
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            """UPDATE giveaway_notifications SET status='sending', sent_at=?
            WHERE giveaway_id=? AND user_id=? AND status='pending'
              AND COALESCE((SELECT promotional_opt_out FROM marketing_preferences
                            WHERE user_id=?), 0)=0""",
            (int(time.time()), giveaway_id, user_id, user_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def set_giveaway_notification_status(giveaway_id: int, user_id: int, status: str) -> bool:
    if status not in {"sent", "blocked", "opted_out", "uncertain"}:
        raise ValueError("Unsupported giveaway notification status")
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            """UPDATE giveaway_notifications SET status=?,
            sent_at=CASE WHEN ?='opted_out' THEN NULL ELSE COALESCE(sent_at, ?) END
            WHERE giveaway_id=? AND user_id=? AND status IN ('sending','pending')""",
            (status, status, int(time.time()), giveaway_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_promotional_opt_out(user_id: int, opted_out: bool = True) -> None:
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            """INSERT INTO marketing_preferences(user_id, promotional_opt_out, updated_at)
            VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            promotional_opt_out=excluded.promotional_opt_out, updated_at=excluded.updated_at""",
            (user_id, int(opted_out), int(time.time())),
        )
        if opted_out:
            await db.execute(
                "UPDATE giveaway_notifications SET status='opted_out' "
                "WHERE user_id=? AND status='pending'", (user_id,),
            )
        await db.commit()


async def is_promotional_opted_out(user_id: int) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT promotional_opt_out FROM marketing_preferences WHERE user_id=?", (user_id,)
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def mark_giveaway_reward_attempt(giveaway_id: int, user_id: int) -> None:
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            """UPDATE giveaway_participants SET reward_attempts=COALESCE(reward_attempts,0)+1,
            last_reward_attempt_at=? WHERE giveaway_id=? AND user_id=? AND is_winner=1""",
            (int(time.time()), giveaway_id, user_id),
        )
        await db.commit()


async def get_unrewarded_giveaway_winners(retry_after_seconds: int = 900) -> list[dict]:
    db = await get_db()
    now = int(time.time())
    cur = await db.execute(
        """SELECT g.id, g.title, g.prize_days, p.user_id, p.winner_rank, p.referral_count FROM giveaway_participants p
        JOIN giveaways g ON g.id=p.giveaway_id
        WHERE p.is_winner=1 AND p.rewarded_at IS NULL
          AND g.drawn_at IS NOT NULL
          AND (p.last_reward_attempt_at IS NULL OR p.last_reward_attempt_at<=?)
        ORDER BY g.drawn_at, p.user_id""",
        (now - int(retry_after_seconds),),
    )
    return [
        {"giveaway_id": r[0], "title": r[1], "prize_days": r[2], "user_id": r[3],
         "winner_rank": r[4], "referral_count": r[5]}
        for r in await cur.fetchall()
    ]


async def mark_giveaway_winner_rewarded(giveaway_id: int, user_id: int) -> bool:
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "UPDATE giveaway_participants SET rewarded_at=? WHERE giveaway_id=? AND user_id=? "
            "AND is_winner=1 AND rewarded_at IS NULL",
            (int(time.time()), giveaway_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_giveaways_ready_to_report() -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        """SELECT g.id, g.title, g.prize_days, g.automation_key FROM giveaways g
        WHERE g.drawn_at IS NOT NULL AND g.results_notified_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM giveaway_participants p WHERE p.giveaway_id=g.id
                AND p.is_winner=1 AND p.rewarded_at IS NULL
          ) ORDER BY g.drawn_at"""
    )
    rows = await cur.fetchall()
    out = []
    for row in rows:
        winners_cur = await db.execute(
            "SELECT user_id, COALESCE(winner_rank,0), COALESCE(referral_count,0) "
            "FROM giveaway_participants WHERE giveaway_id=? AND is_winner=1 "
            "ORDER BY CASE WHEN winner_rank IS NULL THEN 999 ELSE winner_rank END, user_id",
            (row[0],),
        )
        winner_rows = await winners_cur.fetchall()
        out.append({
            "id": row[0], "title": row[1], "prize_days": row[2],
            "is_referral_contest": bool(row[3] and str(row[3]).startswith("auto-monthly-")),
            "winners": [
                {"user_id": winner[0], "rank": winner[1], "referral_count": winner[2]}
                for winner in winner_rows
            ],
        })
    return out


async def mark_giveaway_results_notified(giveaway_id: int) -> None:
    db = await get_db()
    async with _db_semaphore:
        await db.execute(
            "UPDATE giveaways SET results_notified_at=? WHERE id=? AND results_notified_at IS NULL",
            (int(time.time()), giveaway_id),
        )
        await db.commit()


async def pick_giveaway_winners(giveaway_id: int) -> list[int]:
    """Pick and persist winners once; retries return the same winners."""
    import random
    import secrets
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute(
            "SELECT winners_count, is_active, end_date, drawn_at, automation_key, "
            "COALESCE(period_start,created_at) FROM giveaways WHERE id = ?",
            (giveaway_id,),
        )
        g_row = await cur.fetchone()
        if not g_row:
            return []
        winners_count = g_row[0]
        if g_row[3] is not None:
            cur = await db.execute(
                "SELECT user_id FROM giveaway_participants WHERE giveaway_id=? AND is_winner=1 ORDER BY user_id",
                (giveaway_id,),
            )
            return [r[0] for r in await cur.fetchall()]
        if not g_row[1] or int(g_row[2]) > int(time.time()):
            return []

        cur = await db.execute(
            "SELECT user_id FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,)
        )
        participant_ids = [int(r[0]) for r in await cur.fetchall()]
        if not participant_ids:
            await db.execute(
                "UPDATE giveaways SET is_active=0, drawn_at=? WHERE id=?",
                (int(time.time()), giveaway_id),
            )
            await db.commit()
            return []

        if g_row[4] and str(g_row[4]).startswith("auto-monthly-"):
            ranked = []
            for uid in participant_ids:
                count_cur = await db.execute(
                    "SELECT COUNT(*) FROM referrals r "
                    "JOIN users u ON u.user_id=r.referred_id AND u.referrer_id=r.referrer_id "
                    "WHERE r.referrer_id=? AND r.created>=? AND r.created<?",
                    (uid, int(g_row[5]), int(g_row[2])),
                )
                count = int((await count_cur.fetchone())[0] or 0)
                await db.execute(
                    "UPDATE giveaway_participants SET referral_count=? WHERE giveaway_id=? AND user_id=?",
                    (count, giveaway_id, uid),
                )
                if count >= 1:
                    ranked.append((uid, count))

            # Highest monthly referral count wins. Equal counts at the cutoff
            # are resolved randomly, so joining earlier gives no hidden edge.
            ranked.sort(key=lambda item: item[1], reverse=True)
            selected = []
            while ranked and len(selected) < int(winners_count):
                score = ranked[0][1]
                tied = [item for item in ranked if item[1] == score]
                ranked = [item for item in ranked if item[1] != score]
                take = min(int(winners_count) - len(selected), len(tied))
                selected.extend(secrets.SystemRandom().sample(tied, take))
            for position, (uid, count) in enumerate(selected, 1):
                await db.execute(
                    "UPDATE giveaway_participants SET is_winner=1, winner_rank=? "
                    "WHERE giveaway_id=? AND user_id=?",
                    (position, giveaway_id, uid),
                )
        else:
            selected = [(uid, 0) for uid in random.sample(participant_ids, min(len(participant_ids), winners_count))]
            for position, (uid, _count) in enumerate(selected, 1):
                await db.execute(
                    "UPDATE giveaway_participants SET is_winner=1, winner_rank=? WHERE giveaway_id=? AND user_id=?",
                    (position, giveaway_id, uid),
                )
        await db.execute(
            "UPDATE giveaways SET is_active=0, drawn_at=? WHERE id=?",
            (int(time.time()), giveaway_id),
        )
        await db.commit()
        return [uid for uid, _count in selected]


async def set_giveaway_status(giveaway_id: int, is_active: int | str) -> bool:
    """Activate or deactivate a giveaway."""
    active_value = int(is_active) if isinstance(is_active, (int, bool)) else int(is_active == "active")
    db = await get_db()
    async with _db_semaphore:
        cur = await db.execute("UPDATE giveaways SET is_active = ? WHERE id = ?", (active_value, giveaway_id))
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


# One reward per Telegram user. A separate SQLite lease prevents concurrent
# delivery attempts, while an immutable provisioning intent makes uncertain
# retries safe after a worker crashes. Never clear an awarded record.
_CHANNEL_BONUS_LEASE_SECONDS = 900


async def get_channel_subscription_bonus_status(user_id: int) -> str | None:
    """Return 'reserved', 'awarded', or None; never infer an award from a click."""
    db = await get_db()
    table = await (await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_subscription_bonus'"
    )).fetchone()
    if not table:
        return None
    row = await (await db.execute(
        "SELECT status FROM channel_subscription_bonus WHERE user_id=?", (int(user_id),)
    )).fetchone()
    return str(row[0]) if row else None


async def reserve_channel_subscription_bonus(user_id: int) -> bool:
    """Lease an unawarded reward atomically across all bot workers.

    A crashed/uncertain attempt can retry after the lease expires. Its target
    expiry remains immutable in provisioning_intents, so it cannot add 10 days
    twice. Other callbacks must not run provisioning while the lease is live.
    """
    user_id = int(user_id)
    if user_id <= 0:
        return False
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_subscription_bonus (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                awarded_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_subscription_bonus_leases (
                user_id INTEGER PRIMARY KEY,
                expires_at INTEGER NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO channel_subscription_bonus(user_id, status) VALUES(?, 'reserved')",
            (user_id,),
        )
        row = await (await db.execute(
            "SELECT status FROM channel_subscription_bonus WHERE user_id=?", (user_id,)
        )).fetchone()
        if not row or row[0] != "reserved":
            await db.rollback()
            return False
        claim = await db.execute("""
            INSERT INTO channel_subscription_bonus_leases(user_id, expires_at)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at
            WHERE channel_subscription_bonus_leases.expires_at <= ?
        """, (user_id, now + _CHANNEL_BONUS_LEASE_SECONDS, now))
        reserved = claim.rowcount == 1
        if reserved:
            await db.commit()
        else:
            await db.rollback()
        return reserved


async def complete_channel_subscription_bonus(user_id: int) -> bool:
    """Finalize once. Keep the claim on any failed/uncertain provisioning."""
    user_id = int(user_id)
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        result = await db.execute(
            "UPDATE channel_subscription_bonus SET status='awarded', awarded_at=strftime('%s','now') "
            "WHERE user_id=? AND status='reserved'",
            (user_id,),
        )
        if result.rowcount == 1:
            await db.execute(
                "DELETE FROM channel_subscription_bonus_leases WHERE user_id=?", (user_id,)
            )
        await db.commit()
        return result.rowcount == 1


# Free orders are financially different from paid webhooks: no payment provider
# enforces a unique invoice. Freeze the first selected entitlement and reserve
# scarce promo capacity before calling 3x-ui. Retried calls reuse that order.
async def reserve_free_promo_order(
    user_id: int, code: str, months: int, devices: int, days: int,
) -> bool:
    user_id, months, devices, days = int(user_id), int(months), int(devices), int(days)
    code = str(code).strip().upper()
    if user_id <= 0 or not code or months <= 0 or devices <= 0 or days <= 0:
        return False
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS free_promo_orders (
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                months INTEGER NOT NULL,
                devices INTEGER NOT NULL,
                days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                awarded_at INTEGER,
                PRIMARY KEY (user_id, code)
            )
        """)
        existing = await (await db.execute(
            "SELECT months, devices, days, status FROM free_promo_orders WHERE user_id=? AND code=?",
            (user_id, code),
        )).fetchone()
        if existing:
            await db.rollback()
            return (existing[3] == "pending" and
                    tuple(int(v) for v in existing[:3]) == (months, devices, days))
        promo = await (await db.execute(
            "SELECT max_uses, uses_count, is_active, start_date, expires_at "
            "FROM promo_codes WHERE code=?", (code,),
        )).fetchone()
        if (not promo or not promo[2] or int(promo[3] or 0) > now
                or int(promo[4] or 0) < now):
            await db.rollback()
            return False
        outstanding = await (await db.execute(
            "SELECT COUNT(*) FROM free_promo_orders WHERE code=? AND status='pending'",
            (code,),
        )).fetchone()
        if int(promo[1]) + int(outstanding[0]) >= int(promo[0]):
            await db.rollback()
            return False
        await db.execute(
            "INSERT INTO free_promo_orders(user_id, code, months, devices, days, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, code, months, devices, days, now),
        )
        await db.commit()
        return True


async def complete_free_promo_order(user_id: int, code: str) -> bool:
    """Mark the entitlement complete only after delivery and promo accounting."""
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        updated = await db.execute(
            "UPDATE free_promo_orders SET status='awarded', awarded_at=? "
            "WHERE user_id=? AND code=? AND status='pending'",
            (int(time.time()), int(user_id), str(code).strip().upper()),
        )
        await db.commit()
        return updated.rowcount == 1
