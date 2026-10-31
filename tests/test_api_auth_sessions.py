"""Tests for JWT configuration and durable single-use Telegram login states."""
import asyncio
import hashlib
import os
import secrets
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_api_auth_sessions_import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
import database


TEST_DB_PATH = "/tmp/test_api_auth_sessions.db"


def _clean_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(TEST_DB_PATH + suffix)
        except FileNotFoundError:
            pass


class TestJwtConfiguration(unittest.TestCase):
    def test_valid_signature_is_accepted(self):
        token = api_router.create_simple_jwt(123456, "test@example.invalid")
        self.assertEqual(api_router.verify_simple_jwt(token), 123456)

    def test_wrong_issuer_is_rejected_even_with_valid_signature(self):
        token = api_router.create_simple_jwt(
            123456, "test@example.invalid", issuer="unexpected-issuer"
        )
        self.assertIsNone(api_router.verify_simple_jwt(token))

    def test_wrong_audience_is_rejected_even_with_valid_signature(self):
        token = api_router.create_simple_jwt(
            123456, "test@example.invalid", audience="another-client"
        )
        self.assertIsNone(api_router.verify_simple_jwt(token))

    def test_issuer_and_exact_audience_are_present_in_verified_claims(self):
        token = api_router.create_simple_jwt(123456, "test@example.invalid")
        claims = api_router._verified_token_claims(token)
        self.assertEqual(claims["issuer"], api_router.JWT_ISSUER)
        self.assertEqual(claims["audience"], api_router.JWT_AUDIENCE)

    def test_tampered_signature_is_rejected(self):
        token = api_router.create_simple_jwt(123456, "test@example.invalid")
        parts = token.split(".")
        signature = parts[2]
        changed_first = "A" if signature[0] != "A" else "B"
        altered = ".".join((*parts[:2], changed_first + signature[1:]))
        self.assertIsNone(api_router.verify_simple_jwt(altered))

    def test_expired_token_is_rejected(self):
        original_time = api_router.time.time
        try:
            api_router.time.time = lambda: 1_000
            token = api_router.create_simple_jwt(123456, "test@example.invalid")
            api_router.time.time = lambda: 40 * 86400
            self.assertIsNone(api_router.verify_simple_jwt(token))
        finally:
            api_router.time.time = original_time

    def test_refresh_preserves_original_absolute_expiry(self):
        issued = int(api_router.time.time()) - 100
        expiry = issued + 30 * 86400
        token = api_router.create_simple_jwt(
            123456, "test@example.invalid", issued_at=issued, expires_at=expiry
        )
        claims = api_router._verified_token_claims(token)
        self.assertEqual(claims["expires_at"], expiry)
        replacement = api_router.create_simple_jwt(
            claims["user_id"], claims["email"],
            issued_at=claims["issued_at"], expires_at=claims["expires_at"],
            jwt_id=claims["jti"],
        )
        refreshed = api_router._verified_token_claims(replacement)
        self.assertEqual(refreshed["expires_at"], expiry)
        self.assertEqual(refreshed["jti"], claims["jti"])

    def test_missing_or_short_secret_fails_configuration(self):
        for configured_value in (None, "short"):
            with self.subTest(configured=configured_value is not None):
                env = dict(os.environ)
                if configured_value is None:
                    env.pop("JWT_SECRET", None)
                else:
                    env["JWT_SECRET"] = configured_value
                inherited_pythonpath = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = os.pathsep.join(
                    value for value in (PROJECT_ROOT, inherited_pythonpath) if value
                )
                env["DB_FILE"] = "/tmp/test_missing_jwt_secret.db"
                result = subprocess.run(
                    [sys.executable, "-c", "import api_router"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("JWT_SECRET must be configured", result.stderr)


class TestGoogleLoginClosed(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_google_payload_is_never_accepted(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": "/api/v1/auth/google",
            "raw_path": b"/api/v1/auth/google", "query_string": b"",
            "headers": [], "client": ("127.0.0.1", 1), "server": ("test.invalid", 443),
        })
        forged = "x.eyJlbWFpbCI6ImF0dGFja2VyQGV4YW1wbGUuaW52YWxpZCJ9.x"
        with self.assertRaises(HTTPException) as result:
            await api_router.login_google(api_router.GoogleLoginRequest(id_token=forged), request)
        self.assertEqual(result.exception.status_code, 503)


class TestAccountDeletionUnavailable(unittest.IsolatedAsyncioTestCase):
    async def test_delete_me_does_not_claim_success_without_deleting_account(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as result:
            await api_router.delete_me(user_id=98765)
        self.assertEqual(result.exception.status_code, 501)


class TestTelegramAuthSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        _clean_db()

    async def test_body_polling_consumes_telegram_state_without_query_parameter(self):
        from starlette.requests import Request

        state = "auth_" + secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state.removeprefix("auth_").encode()).hexdigest()
        await database.create_mobile_auth_session(state_hash, int(api_router.time.time()) + 60)
        await database.confirm_mobile_auth_session(state_hash, 98765, "test-user", "Test User")
        request = Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": "/api/v1/auth/telegram/check-session",
            "raw_path": b"/api/v1/auth/telegram/check-session", "query_string": b"",
            "headers": [], "client": ("127.0.0.1", 1), "server": ("test.invalid", 443),
        })
        with patch.object(
            api_router, "get_user_real_subscription",
            new=AsyncMock(return_value=("", [], {"status": "inactive"})),
        ):
            result = await api_router.check_telegram_session_body(
                api_router.TelegramSessionCheckRequest(token=state), request
            )
        self.assertEqual(result.status, "authenticated")
        self.assertEqual(api_router.verify_simple_jwt(result.access_token), 98765)
        self.assertEqual((await database.poll_mobile_auth_session(state_hash))["status"], "expired")

    async def test_state_is_persisted_and_consumed_once(self):
        state_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
        await database.create_mobile_auth_session(state_hash, int(api_router.time.time()) + 60)
        self.assertEqual((await database.poll_mobile_auth_session(state_hash))["status"], "pending")

        self.assertTrue(await database.confirm_mobile_auth_session(state_hash, 98765, "test-user", "Test User"))
        self.assertFalse(await database.confirm_mobile_auth_session(state_hash, 98765, "again", "Again"))
        session = await database.poll_mobile_auth_session(state_hash)
        self.assertEqual(session, {
            "status": "authenticated",
            "user_id": 98765,
            "username": "test-user",
            "full_name": "Test User",
        })
        self.assertEqual((await database.poll_mobile_auth_session(state_hash))["status"], "expired")

    async def test_expired_state_cannot_be_confirmed(self):
        state_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
        await database.create_mobile_auth_session(state_hash, int(api_router.time.time()) - 1)
        self.assertFalse(await database.confirm_mobile_auth_session(state_hash, 98765, "test", "Test"))
        self.assertEqual((await database.poll_mobile_auth_session(state_hash))["status"], "expired")

    async def test_parallel_polling_consumes_state_at_most_once(self):
        state_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
        await database.create_mobile_auth_session(state_hash, int(api_router.time.time()) + 60)
        await database.confirm_mobile_auth_session(state_hash, 98765, "test", "Test")
        results = await asyncio.gather(
            database.poll_mobile_auth_session(state_hash),
            database.poll_mobile_auth_session(state_hash),
        )
        self.assertEqual(sum(result.get("status") == "authenticated" for result in results), 1)
        self.assertEqual(sum(result.get("status") == "expired" for result in results), 1)

    async def test_logout_revokes_access_and_refresh_for_one_session(self):
        from fastapi import HTTPException

        token = api_router.create_simple_jwt(98765, "test@example.invalid")
        claims = api_router._verified_token_claims(token)
        self.assertTrue(claims["jti"])

        result = await api_router.logout(f"Bearer {token}", user_id=98765)
        self.assertEqual(result, {"status": "ok"})
        self.assertTrue(await database.is_mobile_jwt_revoked(claims["jti"]))

        with self.assertRaises(HTTPException) as access_result:
            await api_router.get_current_user_id(f"Bearer {token}")
        self.assertEqual(access_result.exception.status_code, 401)

        with self.assertRaises(HTTPException) as refresh_result:
            await api_router.refresh_token(api_router.RefreshRequest(refresh=f"ref_{token}"))
        self.assertEqual(refresh_result.exception.status_code, 401)

    async def test_revocation_store_failure_denies_authorization(self):
        from fastapi import HTTPException
        from unittest.mock import patch

        token = api_router.create_simple_jwt(98765, "test@example.invalid")
        with patch.object(database, "is_mobile_jwt_revoked", side_effect=RuntimeError("db unavailable")):
            with self.assertRaises(HTTPException) as result:
                await api_router.get_current_user_id(f"Bearer {token}")
        self.assertEqual(result.exception.status_code, 503)

    async def test_revocation_is_limited_to_token_expiry(self):
        token = api_router.create_simple_jwt(98765, "test@example.invalid")
        claims = api_router._verified_token_claims(token)
        await database.revoke_mobile_jwt(claims["jti"], claims["expires_at"])
        self.assertTrue(await database.is_mobile_jwt_revoked(claims["jti"]))

    async def test_existing_mobile_payment_table_migrates_generation_column(self):
        db = await database.get_db()
        await db.execute("DROP TABLE mobile_payment_requests")
        await db.execute(
            """CREATE TABLE mobile_payment_requests (
                user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                plan_code TEXT NOT NULL,
                provider TEXT NOT NULL,
                payment_id TEXT,
                confirmation_url TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, request_id),
                UNIQUE (provider, payment_id)
            )"""
        )
        await db.execute("DROP TABLE crypto_pending")
        await db.execute(
            """CREATE TABLE crypto_pending (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                devices INTEGER NOT NULL DEFAULT 1,
                amount_rub INTEGER NOT NULL DEFAULT 0,
                created INTEGER DEFAULT (strftime('%s','now'))
            )"""
        )
        await db.commit()

        await database.init_db()
        columns = {
            row[1] for row in await (await db.execute(
                "PRAGMA table_info(mobile_payment_requests)"
            )).fetchall()
        }
        self.assertTrue({"generation", "devices", "creation_state"}.issubset(columns))
        crypto_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(crypto_pending)")).fetchall()
        }
        self.assertIn("payload", crypto_columns)
        current = await database.ensure_mobile_payment_request(
            98765, "request-migrated1", "1m", "yookassa", 5
        )
        self.assertEqual(current["generation"], 0)
        self.assertEqual(current["devices"], 5)
        self.assertEqual(current["creation_state"], "ready")

    async def test_crypto_mobile_invoice_and_fulfillment_intent_are_saved_together(self):
        request_id = "request-crypto-1234"
        reserved = await database.ensure_mobile_payment_request(
            98765, request_id, "3m", "crypto", 5
        )
        self.assertTrue(await database.claim_mobile_payment_creation(
            98765, request_id, "3m", "crypto"
        ))
        self.assertTrue(await database.save_mobile_crypto_checkout(
            98765, request_id, "3m", 5, "invoice-123",
            "https://pay.example.invalid/crypto", 120, 499, "bm1_order_digest",
        ))
        pending = await database.get_crypto_pending("invoice-123")
        self.assertEqual(
            (pending["user_id"], pending["days"], pending["devices"], pending["amount_rub"], pending["payload"]),
            (98765, 120, 5, 499, "bm1_order_digest"),
        )
        owner = await database.get_mobile_payment_record("crypto", "invoice-123")
        self.assertEqual(owner["user_id"], 98765)
        self.assertEqual(owner["devices"], 5)

    async def test_canceled_mobile_payment_generation_rotates_once_atomically(self):
        request_id = "request-12345678"
        first = await database.ensure_mobile_payment_request(98765, request_id, "1m", "yookassa")
        self.assertEqual(first["generation"], 0)
        self.assertTrue(await database.save_mobile_payment_request(
            98765, request_id, "1m", "yookassa", "old-invoice", "https://pay.invalid/old"
        ))
        rotations = await asyncio.gather(
            database.advance_mobile_payment_request_generation(
                98765, request_id, "1m", "yookassa", "old-invoice", 0
            ),
            database.advance_mobile_payment_request_generation(
                98765, request_id, "1m", "yookassa", "old-invoice", 0
            ),
        )
        self.assertEqual(sum(rotations), 1)
        current = await database.ensure_mobile_payment_request(98765, request_id, "1m", "yookassa")
        self.assertEqual(current["generation"], 1)
        self.assertIsNone(current["payment_id"])
        self.assertIsNone(current["confirmation_url"])

    async def test_mobile_payment_request_persists_owner_and_is_idempotent(self):
        first = await database.ensure_mobile_payment_request(98765, "request-12345678", "1m", "yookassa")
        replay = await database.ensure_mobile_payment_request(98765, "request-12345678", "1m", "yookassa")
        self.assertEqual(first, replay)
        self.assertTrue(await database.save_mobile_payment_request(
            98765, "request-12345678", "1m", "yookassa", "invoice-123", "https://pay.invalid/checkout"
        ))
        stored = await database.get_mobile_payment_request(98765, "request-12345678")
        self.assertEqual(stored["payment_id"], "invoice-123")
        record = await database.get_mobile_payment_record("yookassa", "invoice-123")
        self.assertEqual(record["user_id"], 98765)
        self.assertEqual(record["status"], "pending")


if __name__ == "__main__":
    unittest.main()
