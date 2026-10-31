"""Tests for JWT configuration and durable single-use Telegram login states."""
import asyncio
import hashlib
import os
import secrets
import subprocess
import sys
import unittest

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
        altered = token[:-1] + ("0" if token[-1] != "0" else "1")
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
        )
        self.assertEqual(api_router._verified_token_claims(replacement)["expires_at"], expiry)

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


class TestTelegramAuthSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        _clean_db()

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
