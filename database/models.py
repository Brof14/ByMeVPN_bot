"""
Database schema models for ByMeVPN bot
Contains SQL constants for table creation and management
"""

# Database file path
DB_FILE = "byemevpn.db"

# =============================================================================
# Table Creation SQL
# =============================================================================

# Users table
CREATE_TABLE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    currency TEXT DEFAULT 'RUB',
    trial_used INTEGER DEFAULT 0,
    ref_bonus_claimed INTEGER DEFAULT 0,
    referrer_id INTEGER,
    notifications_enabled INTEGER DEFAULT 1,
    auto_renewal INTEGER DEFAULT 0,
    news_notifications INTEGER DEFAULT 1,
    created INTEGER DEFAULT 0
);
"""

# Users table indexes
CREATE_INDEX_USERS_REFERRER = "CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);"
CREATE_INDEX_USERS_NOTIFICATIONS = "CREATE INDEX IF NOT EXISTS idx_users_notifications ON users(notifications_enabled);"
CREATE_INDEX_USERS_CREATED = "CREATE INDEX IF NOT EXISTS idx_users_created ON users(created);"

# Keys table
CREATE_TABLE_KEYS = """
CREATE TABLE IF NOT EXISTS keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    remark TEXT,
    days INTEGER NOT NULL,
    created INTEGER NOT NULL,
    expiry INTEGER NOT NULL,
    server_check INTEGER DEFAULT 0,
    last_check INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

# Keys table indexes
CREATE_INDEX_KEYS_USER = "CREATE INDEX IF NOT EXISTS idx_keys_user ON keys(user_id);"
CREATE_INDEX_KEYS_EXPIRY = "CREATE INDEX IF NOT EXISTS idx_keys_expiry ON keys(expiry);"

# Payments table
CREATE_TABLE_PAYMENTS = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    method TEXT NOT NULL,
    days INTEGER NOT NULL,
    created INTEGER NOT NULL,
    payload TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

# Payments table indexes
CREATE_INDEX_PAYMENTS_USER = "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);"
CREATE_INDEX_PAYMENTS_CREATED = "CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created);"

# Tournaments table
CREATE_TABLE_TOURNAMENTS = """
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    requirement_type TEXT NOT NULL,
    requirement_count INTEGER NOT NULL,
    reward_days INTEGER NOT NULL,
    start_date INTEGER NOT NULL,
    end_date INTEGER NOT NULL,
    is_active INTEGER DEFAULT 1,
    created INTEGER DEFAULT (strftime('%s', 'now'))
);
"""

# Tournaments table indexes
CREATE_INDEX_TOURNAMENTS_ACTIVE = "CREATE INDEX IF NOT EXISTS idx_tournaments_active ON tournaments(is_active);"
CREATE_INDEX_TOURNAMENTS_DATES = "CREATE INDEX IF NOT EXISTS idx_tournaments_dates ON tournaments(start_date, end_date);"

# Tournament participants table
CREATE_TABLE_TOURNAMENT_PARTICIPANTS = """
CREATE TABLE IF NOT EXISTS tournament_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    progress INTEGER DEFAULT 0,
    completed INTEGER DEFAULT 0,
    reward_given INTEGER DEFAULT 0,
    joined_at INTEGER DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE(tournament_id, user_id)
);
"""

# Tournament participants table indexes
CREATE_INDEX_TOURNAMENT_PARTICIPANTS_TOURNAMENT = "CREATE INDEX IF NOT EXISTS idx_tournament_participants_tournament ON tournament_participants(tournament_id);"
CREATE_INDEX_TOURNAMENT_PARTICIPANTS_USER = "CREATE INDEX IF NOT EXISTS idx_tournament_participants_user ON tournament_participants(user_id);"
CREATE_INDEX_TOURNAMENT_PARTICIPANTS_PROGRESS = "CREATE INDEX IF NOT EXISTS idx_tournament_participants_progress ON tournament_participants(progress, completed);"

# =============================================================================
# Table Alteration SQL (for migrations)
# =============================================================================

# Add news_notifications column to users table
ALTER_TABLE_USERS_ADD_NEWS_NOTIFICATIONS = "ALTER TABLE users ADD COLUMN news_notifications INTEGER DEFAULT 1"

# Add created column to users table
ALTER_TABLE_USERS_ADD_CREATED = "ALTER TABLE users ADD COLUMN created INTEGER DEFAULT 0"

# Add server_check column to keys table
ALTER_TABLE_KEYS_ADD_SERVER_CHECK = "ALTER TABLE keys ADD COLUMN server_check INTEGER DEFAULT 0"

# Add last_check column to keys table
ALTER_TABLE_KEYS_ADD_LAST_CHECK = "ALTER TABLE keys ADD COLUMN last_check INTEGER DEFAULT 0"

# =============================================================================
# Complete Database Schema
# =============================================================================

ALL_TABLES_SCHEMA = [
    CREATE_TABLE_USERS,
    CREATE_INDEX_USERS_REFERRER,
    CREATE_INDEX_USERS_NOTIFICATIONS,
    CREATE_INDEX_USERS_CREATED,
    CREATE_TABLE_KEYS,
    CREATE_INDEX_KEYS_USER,
    CREATE_INDEX_KEYS_EXPIRY,
    CREATE_TABLE_PAYMENTS,
    CREATE_INDEX_PAYMENTS_USER,
    CREATE_INDEX_PAYMENTS_CREATED,
    CREATE_TABLE_TOURNAMENTS,
    CREATE_INDEX_TOURNAMENTS_ACTIVE,
    CREATE_INDEX_TOURNAMENTS_DATES,
    CREATE_TABLE_TOURNAMENT_PARTICIPANTS,
    CREATE_INDEX_TOURNAMENT_PARTICIPANTS_TOURNAMENT,
    CREATE_INDEX_TOURNAMENT_PARTICIPANTS_USER,
    CREATE_INDEX_TOURNAMENT_PARTICIPANTS_PROGRESS,
]

# =============================================================================
# Migration SQL
# =============================================================================

MIGRATIONS = [
    # Migration 1: Add news_notifications to users table
    {
        "version": 1,
        "description": "Add news_notifications column to users table",
        "check_sql": "SELECT name FROM pragma_table_info('users') WHERE name = 'news_notifications'",
        "migrate_sql": ALTER_TABLE_USERS_ADD_NEWS_NOTIFICATIONS
    },
    # Migration 2: Add created column to users table
    {
        "version": 2,
        "description": "Add created column to users table",
        "check_sql": "SELECT name FROM pragma_table_info('users') WHERE name = 'created'",
        "migrate_sql": ALTER_TABLE_USERS_ADD_CREATED
    },
    # Migration 3: Add server_check column to keys table
    {
        "version": 3,
        "description": "Add server_check column to keys table",
        "check_sql": "SELECT name FROM pragma_table_info('keys') WHERE name = 'server_check'",
        "migrate_sql": ALTER_TABLE_KEYS_ADD_SERVER_CHECK
    },
    # Migration 4: Add last_check column to keys table
    {
        "version": 4,
        "description": "Add last_check column to keys table",
        "check_sql": "SELECT name FROM pragma_table_info('keys') WHERE name = 'last_check'",
        "migrate_sql": ALTER_TABLE_KEYS_ADD_LAST_CHECK
    },
]

# =============================================================================
# Table Information
# =============================================================================

TABLES = {
    "users": {
        "description": "User accounts and preferences",
        "columns": {
            "user_id": "INTEGER PRIMARY KEY",
            "balance": "INTEGER DEFAULT 0",
            "currency": "TEXT DEFAULT 'RUB'",
            "trial_used": "INTEGER DEFAULT 0",
            "ref_bonus_claimed": "INTEGER DEFAULT 0",
            "referrer_id": "INTEGER",
            "notifications_enabled": "INTEGER DEFAULT 1",
            "auto_renewal": "INTEGER DEFAULT 0",
            "news_notifications": "INTEGER DEFAULT 1",
            "created": "INTEGER DEFAULT 0"
        }
    },
    "keys": {
        "description": "VPN keys for users",
        "columns": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "user_id": "INTEGER NOT NULL",
            "key": "TEXT NOT NULL",
            "remark": "TEXT",
            "days": "INTEGER NOT NULL",
            "created": "INTEGER NOT NULL",
            "expiry": "INTEGER NOT NULL",
            "server_check": "INTEGER DEFAULT 0",
            "last_check": "INTEGER DEFAULT 0"
        }
    },
    "payments": {
        "description": "Payment history",
        "columns": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "user_id": "INTEGER NOT NULL",
            "amount": "INTEGER NOT NULL",
            "currency": "TEXT NOT NULL",
            "method": "TEXT NOT NULL",
            "days": "INTEGER NOT NULL",
            "created": "INTEGER NOT NULL",
            "payload": "TEXT"
        }
    },
    "tournaments": {
        "description": "Tournament information",
        "columns": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "title": "TEXT NOT NULL",
            "description": "TEXT",
            "requirement_type": "TEXT NOT NULL",
            "requirement_count": "INTEGER NOT NULL",
            "reward_days": "INTEGER NOT NULL",
            "start_date": "INTEGER NOT NULL",
            "end_date": "INTEGER NOT NULL",
            "is_active": "INTEGER DEFAULT 1",
            "created": "INTEGER DEFAULT (strftime('%s', 'now'))"
        }
    },
    "tournament_participants": {
        "description": "Tournament participation tracking",
        "columns": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "tournament_id": "INTEGER NOT NULL",
            "user_id": "INTEGER NOT NULL",
            "progress": "INTEGER DEFAULT 0",
            "completed": "INTEGER DEFAULT 0",
            "reward_given": "INTEGER DEFAULT 0",
            "joined_at": "INTEGER DEFAULT (strftime('%s', 'now'))"
        }
    }
}