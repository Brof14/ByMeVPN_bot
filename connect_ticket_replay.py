"""Single-use connection handoff tickets, shared between bot processes.

Only opaque SHA-256 digests are stored. Consume is a one-writer SQLite
transaction, so concurrent or replayed requests cannot reveal credentials.
"""
import hashlib
import time

import aiosqlite

from database import DB_FILE


async def consume_connect_ticket(ticket: str) -> bool:
    """Return True exactly once per validated ticket across all processes."""
    if not isinstance(ticket, str) or not 1 <= len(ticket) <= 4096:
        return False
    digest = hashlib.sha256(ticket.encode("ascii")).hexdigest()
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS consumed_connection_tickets (
                   ticket_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                )"""
            )
            await db.execute(
                "DELETE FROM consumed_connection_tickets WHERE expires_at<=?",
                (now,),
            )
            cursor = await db.execute(
                "INSERT OR IGNORE INTO consumed_connection_tickets(ticket_hash,expires_at) "
                "VALUES(?,?)",
                (digest, now + 3600),
            )
            await db.commit()
            return cursor.rowcount == 1
        except Exception:
            await db.rollback()
            raise
