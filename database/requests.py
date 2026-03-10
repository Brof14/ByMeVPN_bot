"""
Database requests and operations for ByMeVPN bot
Contains all async functions for database interaction
"""

import aiosqlite
import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from .models import DB_FILE, ALL_TABLES_SCHEMA, MIGRATIONS

logger = logging.getLogger(__name__)


async def init_db():
    """Initialize database with all tables and run migrations"""
    async with aiosqlite.connect(DB_FILE) as db:
        # Create all tables
        for sql in ALL_TABLES_SCHEMA:
            await db.execute(sql)
        
        # Run migrations
        await run_migrations(db)
        
        await db.commit()
        logger.info("Database initialized successfully")


async def run_migrations(db):
    """Run database migrations"""
    for migration in MIGRATIONS:
        try:
            # Check if migration is needed
            cursor = await db.execute(migration["check_sql"])
            if not await cursor.fetchone():
                logger.info(f"Running migration: {migration['description']}")
                await db.execute(migration["migrate_sql"])
                
                # Special handling for created column
                if "created" in migration["migrate_sql"]:
                    await db.execute(
                        "UPDATE users SET created = strftime('%s', 'now') WHERE created = 0"
                    )
        except Exception as e:
            logger.warning(f"Error in migration {migration['version']}: {e}")


# =============================================================================
# User Management
# =============================================================================

async def add_user_key(user_id: int, key: str, remark: str, days: int):
    """Add a new VPN key for user"""
    now = int(time.time())
    expiry = now + days * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO keys (user_id, key, remark, days, created, expiry) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, key, remark, days, now, expiry)
        )
        await db.commit()


async def get_user_keys(user_id: int) -> List[Dict]:
    """Get all keys for a user"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT key, remark, days, created, expiry FROM keys WHERE user_id = ? ORDER BY expiry DESC",
            (user_id,)
        )
        rows = await cur.fetchall()
        return [{"key": r[0], "remark": r[1], "days": r[2], "created": r[3], "expiry": r[4]} for r in rows]


async def has_used_trial(user_id: int) -> bool:
    """Check if user has used trial period"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT trial_used FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False


async def mark_trial_used(user_id: int):
    """Mark trial period as used for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO users (user_id, trial_used) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET trial_used = 1",
            (user_id,),
        )
        await db.commit()


async def get_referrer(user_id: int) -> Optional[int]:
    """Get referrer ID for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_referrer(user_id: int, referrer_id: int):
    """Set referrer for user (if not already set)"""
    if user_id == referrer_id:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL",
            (referrer_id, user_id),
        )
        await db.commit()


async def has_active_subscription(user_id: int) -> bool:
    """Check if user has at least one active subscription"""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE user_id = ? AND expiry > ?",
            (user_id, now)
        )
        result = (await cur.fetchone())[0]
        return result > 0


async def has_paid_subscription(user_id: int) -> bool:
    """Check if user has at least one paid subscription"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM payments WHERE user_id = ?", (user_id,)
        )
        result = (await cur.fetchone())[0]
        return result > 0


async def get_monthly_users_count() -> int:
    """Get count of unique users who registered in current month"""
    now = datetime.now()
    month_start = int(datetime(now.year, now.month, 1).timestamp())
    
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            # Try with created column first
            cur = await db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM keys WHERE created >= ?",
                (month_start,)
            )
        except Exception:
            # Fallback to users table if created column doesn't exist in keys
            try:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM users WHERE created >= ?",
                    (month_start,)
                )
            except Exception:
                # Final fallback - count all users (not accurate but works)
                cur = await db.execute("SELECT COUNT(*) FROM users")
        
        result = (await cur.fetchone())[0]
        return result or 0


# =============================================================================
# Payment Management
# =============================================================================

async def save_payment(
    user_id: int,
    amount: int,
    currency: str,
    method: str,
    days: int,
    payload: str | None = None,
) -> None:
    """Save payment record"""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO payments (user_id, amount, currency, method, days, created, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, amount, currency, method, days, now, payload),
        )
        await db.commit()


async def get_user_payments(user_id: int) -> List[Dict]:
    """Get payment history for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT amount, currency, method, days, created FROM payments "
            "WHERE user_id = ? ORDER BY created DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "amount": r[0],
                "currency": r[1],
                "method": r[2],
                "days": r[3],
                "created": r[4],
            }
            for r in rows
        ]


async def get_last_payment(user_id: int) -> Optional[Dict]:
    """Get last payment for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT amount, currency, method, days, created, payload "
            "FROM payments WHERE user_id = ? ORDER BY created DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "amount": row[0],
            "currency": row[1],
            "method": row[2],
            "days": row[3],
            "created": row[4],
            "payload": row[5],
        }


# =============================================================================
# Auto-renewal Management
# =============================================================================

async def set_auto_renewal(user_id: int, enabled: bool) -> None:
    """Set auto-renewal preference for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO users (user_id, auto_renewal) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET auto_renewal = excluded.auto_renewal",
            (user_id, int(enabled)),
        )
        await db.commit()


async def get_auto_renewal(user_id: int) -> bool:
    """Get auto-renewal preference for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT auto_renewal FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False


# =============================================================================
# Admin Statistics
# =============================================================================

async def get_admin_stats() -> Dict:
    """Get admin statistics"""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys WHERE expiry > ?", (now,))
        active_users = (await cur.fetchone())[0]

        cur = await db.execute("SELECT SUM(amount) FROM payments")
        total_revenue = (await cur.fetchone())[0] or 0

        cur = await db.execute(
            "SELECT days, COUNT(*) as cnt FROM payments GROUP BY days ORDER BY cnt DESC LIMIT 3"
        )
        popular = await cur.fetchall()

    return {
        "total_users": total_users,
        "active_users": active_users,
        "total_revenue": total_revenue,
        "popular_plans": popular,
    }


# =============================================================================
# Referral System
# =============================================================================

async def get_referral_stats(user_id: int) -> Dict:
    """Get referral statistics for a user"""
    async with aiosqlite.connect(DB_FILE) as db:
        # Count referred users
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)
        )
        referred_count = (await cur.fetchone())[0] or 0
        
        # Count successful referrals (those who paid)
        cur = await db.execute(
            """SELECT COUNT(*) FROM payments p 
               JOIN users u ON p.user_id = u.user_id 
               WHERE u.referrer_id = ?""",
            (user_id,)
        )
        successful_referrals = (await cur.fetchone())[0] or 0
        
        # Calculate total bonus days earned
        bonus_days = successful_referrals * 7  # 7 days per successful referral
        
        return {
            "referred_count": referred_count,
            "successful_referrals": successful_referrals,
            "bonus_days": bonus_days,
        }


async def get_referral_list(user_id: int) -> List[Dict]:
    """Get list of referred users with their status"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT u.user_id, u.created, 
                      CASE WHEN p.user_id IS NOT NULL THEN 'paid' ELSE 'trial' END as status
               FROM users u 
               LEFT JOIN payments p ON u.user_id = p.user_id 
               WHERE u.referrer_id = ? 
               ORDER BY u.created DESC""",
            (user_id,)
        )
        rows = await cur.fetchall()
        
        return [
            {
                "user_id": row[0],
                "created": row[1],
                "status": row[2],
            }
            for row in rows
        ]


# =============================================================================
# Key Management and Monitoring
# =============================================================================

async def get_keys_nearing_expiry(days_left_min: int = 1, days_left_max: int = 3) -> List[Tuple]:
    """Get keys that are nearing expiry"""
    now = int(time.time())
    min_expiry = now + days_left_min * 86400
    max_expiry = now + days_left_max * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """
            SELECT k.user_id, k.key, (k.expiry - ?) / 86400 AS days_left, k.expiry
            FROM keys k
            WHERE k.expiry BETWEEN ? AND ?
            """,
            (now, min_expiry, max_expiry)
        )
        return await cur.fetchall()


async def update_key_check_status(key_id: int, status: int, check_time: int = None) -> None:
    """Update key server check status"""
    if check_time is None:
        check_time = int(time.time())
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """UPDATE keys 
               SET server_check = ?, last_check = ?
               WHERE id = ?""",
            (status, check_time, key_id)
        )
        await db.commit()


async def get_keys_for_check(check_interval_hours: int = 6) -> List[Dict]:
    """Get keys that need server status check"""
    check_time = int(time.time()) - check_interval_hours * 3600
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT id, user_id, key, remark, days, expiry
               FROM keys 
               WHERE expiry > ? AND (last_check < ? OR last_check IS NULL)
               ORDER BY last_check ASC NULLS FIRST
               LIMIT 100""",
            (int(time.time()), check_time)
        )
        rows = await cur.fetchall()
        
        return [
            {
                "id": row[0],
                "user_id": row[1],
                "key": row[2],
                "remark": row[3],
                "days": row[4],
                "expiry": row[5],
            }
            for row in rows
        ]


async def replace_key(old_key_id: int, new_key: str, new_remark: str, days: int) -> None:
    """Replace failed key with new one"""
    async with aiosqlite.connect(DB_FILE) as db:
        # Get old key info
        cur = await db.execute(
            "SELECT user_id, expiry FROM keys WHERE id = ?",
            (old_key_id,)
        )
        old_key_data = await cur.fetchone()
        
        if old_key_data:
            user_id, old_expiry = old_key_data
            
            # Calculate remaining days
            now = int(time.time())
            remaining_days = max(1, (old_expiry - now) // 86400)
            
            # Create new key
            new_expiry = now + remaining_days * 86400
            await db.execute(
                """INSERT INTO keys 
                   (user_id, key, remark, days, created, expiry, server_check)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (user_id, new_key, new_remark, remaining_days, now, new_expiry)
            )
            
            # Mark old key as replaced
            await db.execute(
                """UPDATE keys 
                   SET remark = remark || '_REPLACED'
                   WHERE id = ?""",
                (old_key_id,)
            )
            
            await db.commit()


# =============================================================================
# Tournaments
# =============================================================================

async def create_tournament(title: str, description: str, requirement_type: str, 
                          requirement_count: int, reward_days: int, 
                          start_date: int, end_date: int) -> int:
    """Create new tournament"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """INSERT INTO tournaments 
               (title, description, requirement_type, requirement_count, reward_days, start_date, end_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, description, requirement_type, requirement_count, reward_days, start_date, end_date)
        )
        await db.commit()
        return cur.lastrowid


async def get_active_tournaments() -> List[Dict]:
    """Get all active tournaments"""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT id, title, description, requirement_type, requirement_count, 
                      reward_days, start_date, end_date
               FROM tournaments 
               WHERE is_active = 1 AND start_date <= ? AND end_date >= ?
               ORDER BY end_date ASC""",
            (now, now)
        )
        rows = await cur.fetchall()
        
        return [
            {
                "id": row[0],
                "title": row[1],
                "description": row[2],
                "requirement_type": row[3],
                "requirement_count": row[4],
                "reward_days": row[5],
                "start_date": row[6],
                "end_date": row[7],
            }
            for row in rows
        ]


async def join_tournament(user_id: int, tournament_id: int) -> bool:
    """Join tournament"""
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                """INSERT INTO tournament_participants (tournament_id, user_id)
                   VALUES (?, ?)""",
                (tournament_id, user_id)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # Already joined


async def update_tournament_progress(user_id: int, tournament_id: int, progress: int) -> bool:
    """Update tournament progress"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """UPDATE tournament_participants 
               SET progress = ? 
               WHERE tournament_id = ? AND user_id = ?""",
            (progress, tournament_id, user_id)
        )
        await db.commit()
        return True


async def get_user_tournaments(user_id: int) -> List[Dict]:
    """Get user's tournament participations"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            """SELECT t.id, t.title, t.requirement_count, t.reward_days, 
                      t.start_date, t.end_date, tp.progress, tp.completed, tp.reward_given
               FROM tournament_participants tp
               JOIN tournaments t ON tp.tournament_id = t.id
               WHERE tp.user_id = ?
               ORDER BY t.end_date DESC""",
            (user_id,)
        )
        rows = await cur.fetchall()
        
        return [
            {
                "id": row[0],
                "title": row[1],
                "requirement_count": row[2],
                "reward_days": row[3],
                "start_date": row[4],
                "end_date": row[5],
                "progress": row[6],
                "completed": row[7],
                "reward_given": row[8],
            }
            for row in rows
        ]


async def complete_tournament_participation(user_id: int, tournament_id: int, reward_days: int) -> bool:
    """Mark tournament participation as completed and give reward"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """UPDATE tournament_participants 
               SET completed = 1, reward_given = 1
               WHERE tournament_id = ? AND user_id = ?""",
            (tournament_id, user_id)
        )
        await db.commit()
        return True


# =============================================================================
# Notifications
# =============================================================================

async def set_news_notifications(user_id: int, enabled: bool) -> None:
    """Enable/disable news notifications for user"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """INSERT INTO users (user_id, news_notifications) 
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET 
               news_notifications = excluded.news_notifications""",
            (user_id, int(enabled))
        )
        await db.commit()


async def get_news_notification_users() -> List[int]:
    """Get users who enabled news notifications"""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT user_id FROM users WHERE news_notifications = 1"
        )
        rows = await cur.fetchall()
        return [row[0] for row in rows]


# =============================================================================
# Server Statistics
# =============================================================================

async def get_server_stats() -> Dict:
    """Get server statistics for admin"""
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        # Total keys
        cur = await db.execute("SELECT COUNT(*) FROM keys")
        total_keys = (await cur.fetchone())[0]
        
        # Active keys
        cur = await db.execute("SELECT COUNT(*) FROM keys WHERE expiry > ?", (now,))
        active_keys = (await cur.fetchone())[0]
        
        # Keys checked recently
        check_time = now - 24 * 3600  # Last 24 hours
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE last_check > ?", 
            (check_time,)
        )
        checked_keys = (await cur.fetchone())[0]
        
        # Failed keys
        cur = await db.execute(
            "SELECT COUNT(*) FROM keys WHERE server_check = 0 AND last_check > ?",
            (check_time,)
        )
        failed_keys = (await cur.fetchone())[0]
        
        return {
            "total_keys": total_keys,
            "active_keys": active_keys,
            "checked_keys": checked_keys,
            "failed_keys": failed_keys,
            "check_rate": round((checked_keys / active_keys * 100) if active_keys > 0 else 0, 1)
        }


# =============================================================================
# Utility Functions
# =============================================================================

async def log_key_replacement(user_id: int, old_key_id: int, new_key: str, reason: str) -> None:
    """Log key replacement for admin review"""
    log_entry = f"{int(time.time())}: User {user_id} - Key {old_key_id} replaced - {reason} - New: {new_key[:20]}..."
    
    # This could be stored in a separate logs table or file
    # For now, we'll use Python logging
    logger.info(f"KEY_REPLACEMENT: {log_entry}")