"""Recover an already-paid 3x-ui entitlement without altering the panel.

This is deliberately not a provisioning path: it never creates a client,
changes the UUID, extends expiry, charges money, or changes HWID limits.
Only a strict, server-fetched profile belonging to the Telegram user may be
imported. SQLite BEGIN IMMEDIATE deduplicates concurrent bot processes.
"""
from __future__ import annotations

import logging
import time
import uuid
from urllib.parse import urlparse

import aiosqlite

import database

logger = logging.getLogger(__name__)
_ALLOWED_DEVICE_LIMITS = frozenset({2, 5, 10})


async def restore_panel_only_subscription(user_id: int, panel: dict) -> int | None:
    """Return local key ID for an eligible panel profile, else None.

    Call only after fetching `panel` through get_xui_client_details(strict=True).
    """
    user_id = int(user_id)
    if user_id <= 0 or not isinstance(panel, dict):
        return None
    enabled = str(panel.get("enable")).lower() in {"true", "1", "yes", "on"}
    if not enabled or str(panel.get("email") or "") != str(user_id):
        return None
    try:
        expiry = int(panel["expiry_time"]) // 1000
        limit = int(panel["limit_hwid"])
        client_id = str(uuid.UUID(str(panel["uuid"])))
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if expiry <= int(time.time()) or limit not in _ALLOWED_DEVICE_LIMITS:
        return None
    subscription_url = str(panel.get("subscription_url") or "").strip()
    parsed = urlparse(subscription_url)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or len(subscription_url) > 2048):
        return None
    now = int(time.time())
    remaining_days = max(1, (expiry - now + 86399) // 86400)
    async with aiosqlite.connect(database.DB_FILE, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        erased = await (await db.execute(
            "SELECT 1 FROM account_tombstones WHERE user_id=?", (user_id,)
        )).fetchone()
        if erased:
            await db.rollback()
            return None
        user = await (await db.execute(
            "SELECT is_banned FROM users WHERE user_id=?", (user_id,)
        )).fetchone()
        if user and bool(user[0]):
            await db.rollback()
            return None
        current = await (await db.execute(
            "SELECT id FROM keys WHERE user_id=? ORDER BY expiry DESC, id DESC LIMIT 1",
            (user_id,),
        )).fetchone()
        if current:
            await db.rollback()
            return int(current[0])
        if not user:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        cursor = await db.execute(
            "INSERT INTO keys(user_id,key,remark,uuid,days,limit_ip,created,expiry) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, subscription_url, f"ByMeVPN_{user_id}",
             client_id, remaining_days, limit, now, expiry),
        )
        await db.commit()
    from cache import invalidate_user_cache, invalidate_subscription_cache
    invalidate_user_cache(user_id)
    invalidate_subscription_cache(user_id)
    logger.info("Recovered existing panel-only entitlement for user %d", user_id)
    return int(cursor.lastrowid)
