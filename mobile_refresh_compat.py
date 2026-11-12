"""Additive refresh-token storage for legacy bind-mounted DB adapters.

The canonical database.py methods take precedence. This adapter deliberately
uses the adapter's own get_db and lock; it never opens a different database.
"""
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_refresh_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    access_jti TEXT NOT NULL UNIQUE,
    access_expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mobile_refresh_sessions_user
    ON mobile_refresh_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_mobile_refresh_sessions_expiry
    ON mobile_refresh_sessions(expires_at);
"""


def install_mobile_refresh_storage_compat(adapter) -> None:
    if all(callable(getattr(adapter, name, None)) for name in (
        "create_mobile_refresh_session", "rotate_mobile_refresh_session",
        "revoke_mobile_refresh_for_access",
        "upgrade_legacy_mobile_refresh",
    )):
        return

    async def create_mobile_refresh_session(
        token_hash, user_id, email, access_jti, access_expires_at, expires_at,
    ):
        db = await adapter.get_db()
        async with adapter._db_semaphore:
            await db.executescript(SCHEMA)
            await db.execute(
                """INSERT INTO mobile_refresh_sessions
                   (token_hash,user_id,email,access_jti,access_expires_at,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (token_hash, int(user_id), email, access_jti,
                 int(access_expires_at), int(time.time()), int(expires_at)),
            )
            await db.commit()

    async def rotate_mobile_refresh_session(
        old_hash, new_hash, new_jti, new_access_expiry, new_refresh_expiry,
    ):
        db = await adapter.get_db()
        now = int(time.time())
        async with adapter._db_semaphore:
            await db.executescript(SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    """SELECT user_id,email,access_jti,access_expires_at
                       FROM mobile_refresh_sessions
                       WHERE token_hash=? AND expires_at>?""",
                    (old_hash, now),
                )
                row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return None
                user_id, email, old_jti, old_expiry = row
                await db.execute(
                    """UPDATE mobile_refresh_sessions SET token_hash=?,access_jti=?,
                       access_expires_at=?,created_at=?,expires_at=?
                       WHERE token_hash=?""",
                    (new_hash, new_jti, int(new_access_expiry), now,
                     int(new_refresh_expiry), old_hash),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO revoked_mobile_jwt_sessions(jti,expires_at) VALUES(?,?)",
                    (old_jti, old_expiry),
                )
                await db.commit()
                return {"user_id": int(user_id), "email": str(email)}
            except Exception:
                await db.rollback()
                raise

    async def upgrade_legacy_mobile_refresh(
        old_jti, old_access_expiry, user_id, email, new_token_hash,
        new_access_jti, new_access_expiry, new_refresh_expiry,
    ):
        """Atomic single-use legacy token exchange on the bound DB."""
        db = await adapter.get_db()
        now = int(time.time())
        if int(old_access_expiry) <= now:
            return False
        async with adapter._db_semaphore:
            await db.executescript(SCHEMA)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS revoked_mobile_jwt_sessions "
                "(jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
            )
            await db.commit()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "DELETE FROM revoked_mobile_jwt_sessions WHERE expires_at<=?", (now,)
                )
                cur = await db.execute(
                    "INSERT OR IGNORE INTO revoked_mobile_jwt_sessions(jti,expires_at) "
                    "VALUES(?,?)", (old_jti, int(old_access_expiry)),
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    return False
                await db.execute(
                    "INSERT INTO mobile_refresh_sessions "
                    "(token_hash,user_id,email,access_jti,access_expires_at,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (new_token_hash, int(user_id), email, new_access_jti,
                     int(new_access_expiry), now, int(new_refresh_expiry)),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def revoke_mobile_refresh_for_access(access_jti):
        db = await adapter.get_db()
        async with adapter._db_semaphore:
            await db.executescript(SCHEMA)
            await db.execute(
                "DELETE FROM mobile_refresh_sessions WHERE access_jti=?",
                (str(access_jti),),
            )
            await db.commit()

    if not callable(getattr(adapter, "create_mobile_refresh_session", None)):
        adapter.create_mobile_refresh_session = create_mobile_refresh_session
    if not callable(getattr(adapter, "rotate_mobile_refresh_session", None)):
        adapter.rotate_mobile_refresh_session = rotate_mobile_refresh_session
    if not callable(getattr(adapter, "revoke_mobile_refresh_for_access", None)):
        adapter.revoke_mobile_refresh_for_access = revoke_mobile_refresh_for_access
    if not callable(getattr(adapter, "upgrade_legacy_mobile_refresh", None)):
        adapter.upgrade_legacy_mobile_refresh = upgrade_legacy_mobile_refresh
