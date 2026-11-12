"""Security regression tests for email ownership and password recovery."""
import asyncio
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
import api_router
import email_identity


class EmailOwnershipStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = str(Path(self.temp.name) / "accounts.sqlite")
        self.db_patch = patch("email_identity.DB_FILE", self.file)
        self.db_patch.start()
        async with aiosqlite.connect(self.file) as db:
            await db.executescript(
                """CREATE TABLE app_users(
                  user_id INTEGER PRIMARY KEY, email TEXT NOT NULL,
                  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
                  name TEXT, created_at INTEGER NOT NULL);
                CREATE TABLE mobile_refresh_sessions (
                  token_hash TEXT PRIMARY KEY, user_id INTEGER,
                  email TEXT, access_jti TEXT UNIQUE,
                  access_expires_at INTEGER, created_at INTEGER, expires_at INTEGER
                );
                INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at)
                VALUES(42,'email@example.test','oldhash','oldsalt','Test',100);
                INSERT INTO mobile_refresh_sessions(
                  token_hash,user_id,email,access_jti,access_expires_at,created_at,expires_at
                ) VALUES('oldrefresh',42,'email@example.test','oldaccess',9999999999,100,9999999999);"""
            )
            await db.commit()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    async def test_verification_is_one_time_and_gates_only_newly_marked_account(self):
        self.assertFalse(await email_identity.is_email_pending(42))
        await email_identity.mark_registration_pending(42)
        self.assertTrue(await email_identity.is_email_pending(42))
        token = await email_identity.issue_action(42, "verify")
        self.assertEqual(await email_identity.action_owner(token, "verify"), 42)
        self.assertIsNone(await email_identity.action_owner(token, "reset"))
        result = await email_identity.verify_registration(token)
        self.assertEqual(result, (42, "email@example.test"))
        self.assertFalse(await email_identity.is_email_pending(42))
        self.assertIsNone(await email_identity.verify_registration(token))
        self.assertIsNone(await email_identity.action_owner(token, "verify"))

    async def test_resend_invalidates_previous_token(self):
        old = await email_identity.issue_action(42, "verify")
        new = await email_identity.issue_action(42, "verify")
        self.assertNotEqual(old, new)
        self.assertIsNone(await email_identity.action_owner(old, "verify"))
        self.assertEqual(await email_identity.action_owner(new, "verify"), 42)

    async def test_reset_changes_password_revokes_refresh_and_old_access(self):
        token = await email_identity.issue_action(42, "reset")
        before = int(time.time())
        self.assertTrue(await email_identity.reset_password(token, "scrypt$v1$new", "1234"))
        self.assertFalse(await email_identity.reset_password(token, "another", "salt"))
        self.assertTrue(await email_identity.session_invalidated_by_reset(42, before))
        self.assertGreater(await email_identity.minimum_new_session_iat(42), before)
        async with aiosqlite.connect(self.file) as db:
            password = await (await db.execute(
                "SELECT password_hash,salt FROM app_users WHERE user_id=42"
            )).fetchone()
            sessions = await (await db.execute(
                "SELECT COUNT(*) FROM mobile_refresh_sessions WHERE user_id=42"
            )).fetchone()
        self.assertEqual(password, ("scrypt$v1$new", "1234"))
        self.assertEqual(sessions[0], 0)

    async def test_wrong_purpose_expired_token_and_empty_are_denied(self):
        token = await email_identity.issue_action(42, "verify")
        self.assertIsNone(await email_identity.action_owner(token, "reset"))
        self.assertIsNone(await email_identity.verify_registration(""))
        self.assertFalse(await email_identity.reset_password(token, "anything", "salt"))
        self.assertFalse(await email_identity.reset_password("", "anything", "salt"))

    async def test_many_parallel_consumers_have_at_most_one_success(self):
        token = await email_identity.issue_action(42, "verify")
        results = await asyncio.gather(
            *(email_identity.verify_registration(token) for _ in range(6))
        )
        self.assertEqual(sum(result is not None for result in results), 1)


class EmailPublicApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_reset_request_never_reports_account_existence(self):
        request = None
        with (patch("api_router.require_auth_quota", new=AsyncMock()),
              patch("email_identity._mail_settings", return_value=None),
              patch("api_router._ensure_auth_db", new=AsyncMock()),
              patch("api_router.database.get_db", new=AsyncMock()) as db_get):
            db = db_get.return_value
            db.execute.return_value.fetchone = AsyncMock(return_value=None)
            result = await api_router.request_password_reset(
                api_router.EmailActionRequest(email="unknown@example.test"), request,
            )
        self.assertEqual(result, {"status": "accepted"})

    async def test_enabled_registration_requires_valid_smtp_before_storage(self):
        fake = api_router.RegisterRequest(
            email="hello@example.test", password="strong-password-123", name="Test"
        )
        with (patch("api_router.email_identity.verification_enabled", return_value=True),
              patch("api_router.email_identity._mail_settings", side_effect=RuntimeError("no SMTP")),
              patch("api_router.require_auth_quota", new=AsyncMock()),
              patch("api_router._ensure_auth_db", new=AsyncMock()),
              patch("api_router.database.create_email_account", new=AsyncMock()) as create):
            with self.assertRaises(api_router.HTTPException) as denied:
                await api_router.register(fake, None)
            self.assertEqual(denied.exception.status_code, 503)
            create.assert_not_awaited()
