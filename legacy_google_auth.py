"""Google nonce/auth adapter for the bind-mounted legacy API runtime.

The current API module already contains the main PR implementation.  This
small adapter is used only when that older runtime is deployed with a
read-only overlay; it replaces the closed Google stub without changing the
Telegram/email handlers.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


GOOGLE_SCHEMA = """
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
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
"""


class GoogleNonceLoginRequest(BaseModel):
    id_token: str
    nonce: str
    install_id: str | None = None


def _database_path(database_module: Any) -> str:
    path = str(getattr(database_module, "DB_FILE", "") or "").strip()
    if not path:
        raise RuntimeError("DB_FILE is unavailable")
    return path


async def _ensure_schema(database_module: Any) -> None:
    async with aiosqlite.connect(_database_path(database_module), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.executescript(GOOGLE_SCHEMA)
        await db.commit()


async def _create_nonce(database_module: Any, nonce_hash: str, expires_at: int) -> None:
    await _ensure_schema(database_module)
    async with aiosqlite.connect(_database_path(database_module), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        await db.execute("DELETE FROM mobile_google_nonces WHERE expires_at<=?", (now,))
        await db.execute(
            "INSERT INTO mobile_google_nonces(nonce_hash,created_at,expires_at) VALUES(?,?,?)",
            (nonce_hash, now, int(expires_at)),
        )
        await db.commit()


async def _consume_nonce(database_module: Any, nonce_hash: str) -> bool:
    await _ensure_schema(database_module)
    async with aiosqlite.connect(_database_path(database_module), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "DELETE FROM mobile_google_nonces WHERE nonce_hash=? AND expires_at>?",
            (nonce_hash, int(time.time())),
        )
        await db.commit()
        return cur.rowcount == 1


async def _get_or_create_google_account(database_module: Any, sub: str, email: str, name: str) -> dict[str, Any]:
    await _ensure_schema(database_module)
    normalized_name = str(name or "Google user").strip()[:160] or "Google user"
    normalized_email = str(email).strip()[:320]
    if not sub or not normalized_email or "@" not in normalized_email:
        raise ValueError("Invalid Google identity")
    internal_email = "google-" + hashlib.sha256(sub.encode("utf-8")).hexdigest()[:40] + "@identity.bymevpn.invalid"
    async with aiosqlite.connect(_database_path(database_module), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT user_id FROM google_accounts WHERE google_sub=?", (sub,))
        row = await cur.fetchone()
        if row:
            user_id = int(row[0])
            await db.execute(
                "UPDATE google_accounts SET email=?,name=? WHERE google_sub=?",
                (normalized_email, normalized_name, sub),
            )
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (normalized_name, user_id))
            await db.execute("UPDATE app_users SET name=? WHERE user_id=?", (normalized_name, user_id))
            await db.commit()
            return {"user_id": user_id, "email": normalized_email, "name": normalized_name}

        user_id = 0
        for _ in range(16):
            candidate = secrets.randbelow(2**62 - 1) + 1
            cur = await db.execute(
                "SELECT 1 FROM users WHERE user_id=? UNION ALL SELECT 1 FROM app_users WHERE user_id=? LIMIT 1",
                (candidate, candidate),
            )
            if not await cur.fetchone():
                user_id = candidate
                break
        if not user_id:
            await db.rollback()
            raise RuntimeError("Unable to allocate mobile account id")

        now = int(time.time())
        salt = secrets.token_hex(16)
        password_hash = secrets.token_hex(32)
        await db.execute("INSERT INTO users(user_id,username) VALUES(?,?)", (user_id, normalized_name))
        await db.execute(
            "INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, internal_email, password_hash, salt, normalized_name, now),
        )
        await db.execute(
            "INSERT INTO google_accounts(google_sub,user_id,email,name,created_at) VALUES(?,?,?,?,?)",
            (sub, user_id, normalized_email, normalized_name, now),
        )
        await db.commit()
        return {"user_id": user_id, "email": normalized_email, "name": normalized_name}


def install_legacy_google_auth(router: APIRouter, namespace: dict[str, Any]) -> None:
    """Replace only the legacy closed Google route and add the nonce endpoint."""
    namespace["database"].__dict__["create_mobile_google_nonce"] = lambda digest, expiry: _create_nonce(
        namespace["database"], digest, expiry,
    )
    namespace["database"].__dict__["consume_mobile_google_nonce"] = lambda digest: _consume_nonce(
        namespace["database"], digest,
    )
    namespace["database"].__dict__["get_or_create_google_account"] = lambda sub, email, name: _get_or_create_google_account(
        namespace["database"], sub, email, name,
    )
    router.routes[:] = [
        route for route in router.routes
        if not (getattr(route, "path", None) == "/auth/google" and "POST" in (getattr(route, "methods", set()) or set()))
    ]

    def configured() -> bool:
        return bool(
            os.getenv("GOOGLE_SIGNIN_ENABLED", "").strip().lower() in {"1", "true", "yes"}
            and os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip()
        )

    @router.post("/auth/google/nonce")
    async def create_google_nonce():
        if not configured():
            raise HTTPException(status_code=503, detail="Вход через Google временно недоступен")
        nonce = secrets.token_urlsafe(32)
        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        try:
            await namespace["database"].create_mobile_google_nonce(digest, int(time.time()) + 300)
        except Exception as exc:
            namespace["logger"].error("Google nonce storage unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось начать вход через Google") from None
        return {"nonce": nonce}

    @router.post("/auth/google")
    async def login_google(req: GoogleNonceLoginRequest, request: Request):
        if not configured():
            raise HTTPException(status_code=503, detail="Вход через Google временно недоступен")
        audience = os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip()
        try:
            from google_signin import GoogleIdTokenError, verify_google_id_token
            claims = await asyncio.to_thread(verify_google_id_token, req.id_token, audience)
        except Exception as exc:
            # Keep provider, token and request details out of logs and responses.
            namespace["logger"].warning("Google ID token rejected (%s)", type(exc).__name__)
            raise HTTPException(status_code=401, detail="Не удалось подтвердить аккаунт Google") from None
        if not hmac.compare_digest(str(claims.get("nonce") or ""), req.nonce):
            raise HTTPException(status_code=401, detail="Не удалось подтвердить запрос входа")
        digest = hashlib.sha256(req.nonce.encode("utf-8")).hexdigest()
        try:
            if not await namespace["database"].consume_mobile_google_nonce(digest):
                raise HTTPException(status_code=401, detail="Запрос входа истёк или уже использован")
            await namespace["_ensure_auth_db"]()
            identity = await namespace["database"].get_or_create_google_account(
                claims["sub"], claims["email"], claims["name"],
            )
        except HTTPException:
            raise
        except Exception as exc:
            namespace["logger"].error("Google account storage unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось открыть аккаунт") from None
        user_id = int(identity["user_id"])
        token = namespace["create_simple_jwt"](user_id, identity["email"])
        sub_url, servers, subscription = await namespace["_auth_subscription_snapshot"](
            user_id, namespace["_subscription_headers_from_request"](request),
        )
        return {
            "access": token,
            "refresh": f"ref_{token}",
            "user": {"id": str(user_id), "email": identity["email"],
                     "name": identity["name"], "is_google": True},
            "subscription": subscription,
            "servers": servers,
        }
