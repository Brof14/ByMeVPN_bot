"""Opt-in verified email registration and reset with one-time scoped tokens.

Legacy email accounts are not retroactively locked. New accounts are gated
only when EMAIL_VERIFICATION_REQUIRED=1 and SMTP + public HTTPS URLs are
configured. Token plaintext is never stored or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import aiosqlite

from database import DB_FILE


def verification_enabled() -> bool:
    return os.getenv("EMAIL_VERIFICATION_REQUIRED", "0").strip() == "1"


def _mail_settings(purpose: str) -> tuple[str, int, str, str, str, str]:
    if purpose not in ("verify", "reset"):
        raise ValueError("invalid email action")
    base = os.getenv(
        "EMAIL_VERIFY_URL" if purpose == "verify" else "EMAIL_RESET_URL", ""
    ).strip()
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or
            parsed.username or parsed.password or parsed.fragment):
        raise RuntimeError("Email action HTTPS URL is not configured")
    host = os.getenv("EMAIL_SMTP_HOST", "").strip()
    sender = os.getenv("EMAIL_SMTP_FROM", "").strip()
    username = os.getenv("EMAIL_SMTP_USERNAME", "").strip()
    password = os.getenv("EMAIL_SMTP_PASSWORD", "")
    if not all((host, sender, username, password)):
        raise RuntimeError("Email delivery is not configured")
    port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    if port not in (465, 587):
        raise RuntimeError("Email TLS port must be 465 or 587")
    return host, port, sender, username, password, base


def _schema_sql() -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS pending_email_verification (
            user_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS email_action_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL CHECK(purpose IN ('verify','reset')),
            expires_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS email_auth_reset_epoch (
            user_id INTEGER PRIMARY KEY, revoked_before INTEGER NOT NULL
        )""",
    ]


async def _begin(db):
    await db.execute("PRAGMA busy_timeout=30000")
    await db.execute("BEGIN IMMEDIATE")
    for query in _schema_sql():
        await db.execute(query)


async def mark_registration_pending(user_id: int) -> None:
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await _begin(db)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO pending_email_verification(user_id,created_at) VALUES(?,?)",
                (int(user_id), int(time.time())),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def is_email_pending(user_id: int) -> bool:
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        # Existing email accounts are grandfathered unless explicitly marked.
        exists = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_email_verification'"
        )).fetchone()
        if not exists:
            return False
        row = await (await db.execute(
            "SELECT 1 FROM pending_email_verification WHERE user_id=?",
            (int(user_id),),
        )).fetchone()
        return row is not None


async def issue_action(user_id: int, purpose: str) -> str:
    if purpose not in ("verify", "reset"):
        raise ValueError("invalid email action")
    token = secrets.token_urlsafe(40)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    expiry = int(time.time()) + (1800 if purpose == "verify" else 900)
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await _begin(db)
        try:
            await db.execute("DELETE FROM email_action_tokens WHERE expires_at<=?",
                             (int(time.time()),))
            await db.execute("DELETE FROM email_action_tokens WHERE user_id=? AND purpose=?",
                             (int(user_id), purpose))
            await db.execute(
                "INSERT INTO email_action_tokens(token_hash,user_id,purpose,expires_at) "
                "VALUES(?,?,?,?)", (digest, int(user_id), purpose, expiry),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return token


async def action_owner(token: str, purpose: str) -> int | None:
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or purpose not in ("verify", "reset"):
        return None
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        row = await (await db.execute(
            "SELECT user_id FROM email_action_tokens "
            "WHERE token_hash=? AND purpose=? AND expires_at>?",
            (digest, purpose, int(time.time())),
        )).fetchone()
        return int(row[0]) if row else None


async def verify_registration(token: str) -> tuple[int, str] | None:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return None
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await _begin(db)
        try:
            row = await (await db.execute(
                "SELECT t.user_id,u.email FROM email_action_tokens t "
                "JOIN app_users u ON u.user_id=t.user_id "
                "WHERE t.token_hash=? AND t.purpose='verify' AND t.expires_at>?",
                (digest, int(time.time())),
            )).fetchone()
            if not row:
                await db.rollback()
                return None
            user_id, email = int(row[0]), str(row[1])
            await db.execute("DELETE FROM pending_email_verification WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM email_action_tokens WHERE user_id=? AND purpose='verify'",
                             (user_id,))
            await db.commit()
            return user_id, email
        except Exception:
            await db.rollback()
            raise


async def reset_password(token: str, password_hash: str, salt: str) -> bool:
    """Rotate password and revoke existing access/refresh sessions atomically."""
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return False
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await _begin(db)
        try:
            row = await (await db.execute(
                "SELECT user_id FROM email_action_tokens WHERE token_hash=? "
                "AND purpose='reset' AND expires_at>?", (digest, int(time.time())),
            )).fetchone()
            if not row:
                await db.rollback()
                return False
            user_id, now = int(row[0]), int(time.time())
            cursor = await db.execute(
                "UPDATE app_users SET password_hash=?,salt=? WHERE user_id=?",
                (password_hash, salt, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO email_auth_reset_epoch(user_id,revoked_before) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET revoked_before=MAX(revoked_before,excluded.revoked_before)",
                (user_id, now),
            )
            await db.execute(
                "DELETE FROM mobile_refresh_sessions WHERE user_id=?", (user_id,),
            )
            await db.execute("DELETE FROM email_action_tokens WHERE user_id=?", (user_id,))
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def session_invalidated_by_reset(user_id: int, issued_at: int) -> bool:
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        table = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_auth_reset_epoch'"
        )).fetchone()
        if not table:
            return False
        row = await (await db.execute(
            "SELECT revoked_before FROM email_auth_reset_epoch WHERE user_id=?",
            (int(user_id),),
        )).fetchone()
        return bool(row and int(issued_at) <= int(row[0]))


async def minimum_new_session_iat(user_id: int) -> int:
    """Prevent same-second JWT issuance from inheriting a reset-revoked epoch."""
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        table = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_auth_reset_epoch'"
        )).fetchone()
        if not table:
            return 0
        row = await (await db.execute(
            "SELECT revoked_before FROM email_auth_reset_epoch WHERE user_id=?",
            (int(user_id),),
        )).fetchone()
        return int(row[0]) + 1 if row else 0


def _send_email(address: str, purpose: str, token: str) -> None:
    host, port, sender, username, password, base = _mail_settings(purpose)
    parsed = urlsplit(base)
    query = urlencode([*parse_qsl(parsed.query, keep_blank_values=True), ("token", token)])
    link = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = address
    message["Subject"] = (
        "Подтвердите email — ByMeVPN" if purpose == "verify"
        else "Сброс пароля — ByMeVPN"
    )
    message.set_content(
        ("Подтвердите ваш email:" if purpose == "verify" else "Изменить пароль:") +
        f"\n{link}\n\nСрок действия ссылки ограничен. Если запрос не ваш, проигнорируйте письмо."
    )
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


async def send_action(address: str, purpose: str, token: str) -> None:
    await asyncio.to_thread(_send_email, address, purpose, token)
