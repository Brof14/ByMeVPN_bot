"""Additive storage compatibility for legacy deployments missing Telegram sessions.

The current database module is preferred. This adapter installs the small
Telegram one-time-session API only when a legacy runtime does not export it.
The schema and transitions intentionally match database.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_auth_sessions (
    state_hash TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'authenticated', 'consumed')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT ''
)
"""
_SESSION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_mobile_auth_sessions_expiry
ON mobile_auth_sessions(expires_at)
"""
_REVOKED_JWT_SCHEMA = """
CREATE TABLE IF NOT EXISTS revoked_mobile_jwt_sessions (
    jti TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
)
"""
_REVOKED_JWT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_revoked_mobile_jwt_expiry
ON revoked_mobile_jwt_sessions(expires_at)
"""


def install_telegram_auth_storage_compat(database: Any) -> tuple[str, ...]:
    """Install absent session helpers without replacing native implementations."""
    required = (
        "create_mobile_auth_session",
        "confirm_mobile_auth_session",
        "poll_mobile_auth_session",
        "get_mobile_auth_session",
        "consume_mobile_auth_session",
        "cancel_mobile_auth_session",
        "is_mobile_jwt_revoked",
        "revoke_mobile_jwt",
    )
    missing = tuple(name for name in required if not callable(getattr(database, name, None)))
    if not missing:
        return ()

    schema_lock = asyncio.Lock()
    fallback_db_lock = asyncio.Lock()

    async def ensure_schema() -> Any:
        async with schema_lock:
            db = await database.get_db()
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute(_SESSION_SCHEMA)
            await db.execute(_SESSION_INDEX)
            await db.execute(_REVOKED_JWT_SCHEMA)
            await db.execute(_REVOKED_JWT_INDEX)
            await db.commit()
            return db

    def db_lock():
        lock = getattr(database, "_db_semaphore", None) or fallback_db_lock
        return lock

    async def create_mobile_auth_session(state_hash: str, expires_at: int) -> None:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            await db.execute(
                "DELETE FROM mobile_auth_sessions WHERE expires_at<=? OR status='consumed'",
                (now,),
            )
            await db.execute(
                "INSERT INTO mobile_auth_sessions"
                "(state_hash,status,created_at,expires_at) VALUES(?,?,?,?)",
                (state_hash, "pending", now, int(expires_at)),
            )
            await db.commit()

    async def confirm_mobile_auth_session(
        state_hash: str, user_id: int, username: str, full_name: str,
    ) -> bool:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            cur = await db.execute(
                "UPDATE mobile_auth_sessions SET status='authenticated',user_id=?,username=?,full_name=? "
                "WHERE state_hash=? AND status='pending' AND expires_at>?",
                (int(user_id), str(username or "")[:80], str(full_name or "")[:160], state_hash, now),
            )
            await db.commit()
        return cur.rowcount == 1

    async def poll_mobile_auth_session(state_hash: str) -> dict[str, Any]:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            cur = await db.execute(
                "SELECT status,user_id,username,full_name,expires_at "
                "FROM mobile_auth_sessions WHERE state_hash=?",
                (state_hash,),
            )
            row = await cur.fetchone()
            if not row or int(row[4]) <= now or row[0] == "consumed":
                return {"status": "expired"}
            if row[0] == "pending":
                return {"status": "pending"}
            cur = await db.execute(
                "UPDATE mobile_auth_sessions SET status='consumed' "
                "WHERE state_hash=? AND status='authenticated' AND expires_at>?",
                (state_hash, now),
            )
            await db.commit()
            if cur.rowcount != 1:
                return {"status": "expired"}
        return {
            "status": "authenticated", "user_id": int(row[1]),
            "username": row[2], "full_name": row[3],
        }

    async def get_mobile_auth_session(state_hash: str) -> dict[str, Any] | None:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            cur = await db.execute(
                "SELECT status,user_id,username,full_name,expires_at "
                "FROM mobile_auth_sessions WHERE state_hash=?",
                (state_hash,),
            )
            row = await cur.fetchone()
        if not row or int(row[4]) <= now or row[0] == "consumed":
            return {"status": "expired"}
        if row[0] == "pending":
            return {"status": "pending"}
        if row[0] != "authenticated" or not row[1]:
            return {"status": "expired"}
        return {
            "status": "authenticated", "user_id": int(row[1]),
            "username": row[2], "full_name": row[3],
        }

    async def consume_mobile_auth_session(state_hash: str) -> bool:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            cur = await db.execute(
                "UPDATE mobile_auth_sessions SET status='consumed' "
                "WHERE state_hash=? AND status='authenticated' AND expires_at>?",
                (state_hash, now),
            )
            await db.commit()
        return cur.rowcount == 1

    async def cancel_mobile_auth_session(state_hash: str) -> bool:
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            cur = await db.execute(
                "UPDATE mobile_auth_sessions SET status='consumed' "
                "WHERE state_hash=? AND status IN ('pending','authenticated') AND expires_at>?",
                (state_hash, now),
            )
            await db.commit()
        return cur.rowcount == 1

    async def is_mobile_jwt_revoked(jti: str) -> bool:
        """Check the legacy runtime's additive JWT-revocation store."""
        db = await ensure_schema()
        async with db_lock():
            cur = await db.execute(
                "SELECT 1 FROM revoked_mobile_jwt_sessions WHERE jti=? AND expires_at>?",
                (str(jti), int(time.time())),
            )
            return await cur.fetchone() is not None

    async def revoke_mobile_jwt(jti: str, expires_at: int) -> None:
        """Persist logout revocation until the access token's expiry."""
        db = await ensure_schema()
        now = int(time.time())
        async with db_lock():
            await db.execute(
                "DELETE FROM revoked_mobile_jwt_sessions WHERE expires_at<=?", (now,),
            )
            await db.execute(
                "INSERT OR REPLACE INTO revoked_mobile_jwt_sessions(jti,expires_at) VALUES(?,?)",
                (str(jti), int(expires_at)),
            )
            await db.commit()

    implementations = {
        "create_mobile_auth_session": create_mobile_auth_session,
        "confirm_mobile_auth_session": confirm_mobile_auth_session,
        "poll_mobile_auth_session": poll_mobile_auth_session,
        "get_mobile_auth_session": get_mobile_auth_session,
        "consume_mobile_auth_session": consume_mobile_auth_session,
        "cancel_mobile_auth_session": cancel_mobile_auth_session,
        "is_mobile_jwt_revoked": is_mobile_jwt_revoked,
        "revoke_mobile_jwt": revoke_mobile_jwt,
    }
    for name in missing:
        setattr(database, name, implementations[name])
    return missing
