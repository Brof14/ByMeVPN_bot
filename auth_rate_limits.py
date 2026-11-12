"""Durable, process-safe authentication attempt quotas without logging PII.

Each key is a SHA-256 digest. Fixed-window increments occur under an
exclusive SQLite writer transaction shared by every primary/secondary bot.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import time

import aiosqlite
from fastapi import HTTPException, Request
from database import DB_FILE


def client_ip(request: Request) -> str:
    peer = (request.client.host if request.client else "") or "unknown"
    try:
        ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"
    trusted = {p.strip() for p in os.getenv(
        "AUTH_TRUSTED_PROXY_IPS", "127.0.0.1,::1"
    ).split(",") if p.strip()}
    if peer not in trusted:
        return peer
    forwarded = request.headers.get("X-Real-IP", "").strip()
    try:
        ipaddress.ip_address(forwarded)
    except ValueError:
        return peer
    return forwarded


async def require_auth_quota(
    request: Request | None,
    category: str,
    *,
    limit: int,
    window_seconds: int,
    subject: str | None = None,
) -> None:
    """Raise 429 once the bucket is exhausted; never silently skip a DB error."""
    if request is None:
        return  # Supports direct invocation of handlers in isolated tests.
    if not category.isascii() or not category.replace("_", "").isalpha():
        raise ValueError("invalid quota category")
    if not 1 <= limit <= 1000 or not 1 <= window_seconds <= 86400:
        raise ValueError("invalid quota bounds")
    identity = subject.strip().casefold()[:320] if subject is not None else client_ip(request)
    bucket_window = int(time.time()) // window_seconds
    key = hashlib.sha256(
        f"auth:{category}:{identity}:{bucket_window}".encode("utf-8")
    ).hexdigest()
    now = int(time.time())
    end = (bucket_window + 1) * window_seconds
    try:
        async with aiosqlite.connect(DB_FILE, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=10000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS auth_rate_limits (
                         bucket_hash TEXT PRIMARY KEY,
                         count INTEGER NOT NULL,
                         expires_at INTEGER NOT NULL
                    )"""
                )
                await db.execute(
                    "DELETE FROM auth_rate_limits WHERE expires_at<=?", (now,)
                )
                await db.execute(
                    "INSERT OR IGNORE INTO auth_rate_limits(bucket_hash,count,expires_at) "
                    "VALUES(?,0,?)", (key, end)
                )
                await db.execute(
                    "UPDATE auth_rate_limits SET count=count+1 WHERE bucket_hash=?",
                    (key,)
                )
                cur = await db.execute(
                    "SELECT count FROM auth_rate_limits WHERE bucket_hash=?", (key,)
                )
                row = await cur.fetchone()
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Проверка входа временно недоступна"
        ) from None
    if not row or int(row[0]) > limit:
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов. Повторите попытку позже.",
            headers={"Retry-After": str(max(1, end - now))},
        )
