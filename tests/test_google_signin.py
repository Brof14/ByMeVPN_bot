"""HTTP and cryptographic coverage for the opt-in Google sign-in contract."""
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_google_signin_import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
import database
from google_signin import GoogleIdTokenError, verify_google_id_token


TEST_DB_PATH = "/tmp/test_google_signin.db"
WEB_CLIENT_ID = "test-web-client.apps.googleusercontent.com"


def _clean_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(TEST_DB_PATH + suffix)
        except FileNotFoundError:
            pass


class TestGoogleSchemaMigration(unittest.IsolatedAsyncioTestCase):
    async def test_migration_is_additive_and_repeatable_for_legacy_sqlite(self):
        import aiosqlite

        with tempfile.TemporaryDirectory(prefix="bymevpn-google-migration-") as temp_dir:
            path = os.path.join(temp_dir, "legacy.sqlite3")
            async with aiosqlite.connect(path) as db:
                await db.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)")
                await db.commit()
                await database.ensure_mobile_google_schema(db)
                await database.ensure_mobile_google_schema(db)

                tables = {
                    row[0]
                    for row in await (await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )).fetchall()
                }
                indexes = {
                    row[0]
                    for row in await (await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )).fetchall()
                }
                fk_rows = await (await db.execute(
                    "PRAGMA foreign_key_list(google_accounts)"
                )).fetchall()

        self.assertTrue({"users", "mobile_google_nonces", "google_accounts"} <= tables)
        self.assertIn("idx_mobile_google_nonces_expiry", indexes)
        self.assertEqual(fk_rows[0][2:4], ("users", "user_id"))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_int(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _signed_google_token(key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": WEB_CLIENT_ID,
        "sub": "google-subject-1",
        "email": "same@example.test",
        "email_verified": True,
        "nonce": "nonce-value",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-key"}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


class TestGoogleIdTokenVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = cls.private_key.public_key().public_numbers()
        cls.jwks = {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "test-key",
            "n": _b64_int(public_numbers.n), "e": _b64_int(public_numbers.e),
        }]}

    def _verify(self, **claims):
        token = _signed_google_token(self.private_key, **claims)
        with patch("google.oauth2.id_token._fetch_certs", return_value=self.jwks):
            return verify_google_id_token(token, WEB_CLIENT_ID)

    def test_valid_signed_token_is_verified_with_google_auth(self):
        result = self._verify()
        self.assertEqual(result["sub"], "google-subject-1")
        self.assertEqual(result["aud"], WEB_CLIENT_ID)
        self.assertEqual(result["nonce"], "nonce-value")

    def test_safe_error_codes_distinguish_verifier_stages(self):
        with self.assertRaises(GoogleIdTokenError) as wrong_audience:
            self._verify(aud="another-client.apps.googleusercontent.com")
        self.assertEqual(wrong_audience.exception.code, "wrong_audience")
        with self.assertRaises(GoogleIdTokenError) as expired:
            self._verify(exp=int(time.time()) - 60)
        self.assertEqual(expired.exception.code, "expired_token")
        with self.assertRaises(GoogleIdTokenError) as unverified:
            self._verify(email_verified=False)
        self.assertEqual(unverified.exception.code, "email_unverified")

    def test_configured_desktop_audience_is_verified_and_other_clients_rejected(self):
        desktop_id = "test-windows-desktop.apps.googleusercontent.com"
        token = _signed_google_token(self.private_key, aud=desktop_id)
        with patch("google.oauth2.id_token._fetch_certs", return_value=self.jwks):
            claims = verify_google_id_token(token, (WEB_CLIENT_ID, desktop_id))
            self.assertEqual(claims["aud"], desktop_id)
            with self.assertRaises(GoogleIdTokenError) as failure:
                verify_google_id_token(token, (WEB_CLIENT_ID,))
            self.assertEqual(failure.exception.code, "wrong_audience")

    def test_modified_payload_or_signature_is_rejected(self):
        token = _signed_google_token(self.private_key)
        header, payload, signature = token.split(".")
        changed_payload = _b64(json.dumps({"iss": "https://accounts.google.com", "aud": WEB_CLIENT_ID}).encode())
        with patch("google.oauth2.id_token._fetch_certs", return_value=self.jwks):
            with self.assertRaises(GoogleIdTokenError):
                verify_google_id_token(f"{header}.{changed_payload}.{signature}", WEB_CLIENT_ID)
            bad_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
            with self.assertRaises(GoogleIdTokenError) as rejected:
                verify_google_id_token(f"{header}.{payload}.{bad_signature}", WEB_CLIENT_ID)
            self.assertEqual(rejected.exception.code, "invalid_signature")

    def test_expired_token_is_rejected(self):
        with self.assertRaises(GoogleIdTokenError):
            self._verify(exp=int(time.time()) - 60)

    def test_wrong_issuer_or_audience_is_rejected(self):
        with self.assertRaises(GoogleIdTokenError):
            self._verify(iss="https://attacker.invalid")
        with self.assertRaises(GoogleIdTokenError):
            self._verify(aud="another-client.apps.googleusercontent.com")

    def test_unverified_email_and_missing_nonce_are_rejected(self):
        with self.assertRaises(GoogleIdTokenError):
            self._verify(email_verified=False)
        with self.assertRaises(GoogleIdTokenError):
            self._verify(nonce="")


class TestGoogleSignInHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()
        self.app = FastAPI()
        self.app.include_router(api_router.api_router, prefix="/api/v1")
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="https://testserver",
        )
        self.env = patch.dict(os.environ, {
            "GOOGLE_SIGNIN_ENABLED": "1",
            "GOOGLE_WEB_CLIENT_ID": WEB_CLIENT_ID,
        })
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.client.aclose()
        await database.close_db()
        _clean_db()

    @staticmethod
    def _claims(sub="stable-subject", email="same@example.test", nonce=""):
        return {
            "sub": sub, "email": email, "name": "Test Google User", "nonce": nonce,
            "iss": "https://accounts.google.com", "aud": WEB_CLIENT_ID,
            "exp": int(time.time()) + 300,
        }

    async def test_desktop_client_endpoint_fails_closed_until_configured(self):
        response = await self.client.get("/api/v1/auth/google/desktop-client")
        self.assertEqual(response.status_code, 503)
        desktop_id = "test-windows-desktop.apps.googleusercontent.com"
        with patch.dict(os.environ, {"GOOGLE_DESKTOP_CLIENT_ID": desktop_id}):
            response = await self.client.get("/api/v1/auth/google/desktop-client")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"client_id": desktop_id, "server_code_exchange": False})
        with patch.dict(os.environ, {"GOOGLE_DESKTOP_CLIENT_ID": "attacker.invalid"}):
            self.assertEqual((await self.client.get("/api/v1/auth/google/desktop-client")).status_code, 503)

    async def test_nonce_endpoint_returns_server_nonce_and_persists_only_its_hash(self):
        response = await self.client.post("/api/v1/auth/google/nonce", json={})
        self.assertEqual(response.status_code, 200)
        nonce = response.json()["nonce"]
        self.assertGreaterEqual(len(nonce), 32)
        # Query the isolated database directly to ensure the raw challenge is not stored.
        db = await database.get_db()
        stored = await (await db.execute("SELECT nonce_hash FROM mobile_google_nonces")).fetchone()
        self.assertEqual(stored[0], hashlib.sha256(nonce.encode()).hexdigest())

    async def test_disabled_google_signin_fails_closed(self):
        with patch.dict(os.environ, {"GOOGLE_SIGNIN_ENABLED": "0"}):
            response = await self.client.post("/api/v1/auth/google/nonce", json={})
        self.assertEqual(response.status_code, 503)

    async def test_login_rejects_nonce_mismatch_without_consuming_challenge(self):
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with patch.object(api_router, "verify_google_id_token", return_value=self._claims(nonce="different")):
            response = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "opaque", "nonce": nonce},
            )
        self.assertEqual(response.status_code, 401)
        self.assertTrue(await database.consume_mobile_google_nonce(hashlib.sha256(nonce.encode()).hexdigest()))

    async def test_expired_challenge_is_rejected(self):
        nonce = secrets.token_urlsafe(32)
        await database.create_mobile_google_nonce(
            hashlib.sha256(nonce.encode()).hexdigest(), int(time.time()) - 1,
        )
        with patch.object(
            api_router, "verify_google_id_token", return_value=self._claims(nonce=nonce),
        ):
            response = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "opaque", "nonce": nonce},
            )
        self.assertEqual(response.status_code, 401)

    async def test_invalid_google_signature_fails_closed_without_creating_account(self):
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with patch.object(
            api_router, "verify_google_id_token", side_effect=GoogleIdTokenError("invalid signature"),
        ):
            response = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "forged", "nonce": nonce},
            )
        self.assertEqual(response.status_code, 401)
        db = await database.get_db()
        count = await (await db.execute("SELECT COUNT(*) FROM google_accounts")).fetchone()
        self.assertEqual(count[0], 0)

    async def test_rejection_returns_correlation_id_and_logs_only_safe_code(self):
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with patch.object(
            api_router, "verify_google_id_token",
            side_effect=GoogleIdTokenError("invalid_signature"),
        ), self.assertLogs(api_router.logger, level="WARNING") as captured:
            response = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "sensitive-token-value", "nonce": nonce},
            )
        self.assertEqual(response.status_code, 401)
        correlation_id = response.headers.get("x-correlation-id")
        self.assertRegex(correlation_id or "", r"^[a-f0-9]{16}$")
        log_text = "\n".join(captured.output)
        self.assertIn("stage=id_token code=invalid_signature", log_text)
        self.assertIn(correlation_id, log_text)
        self.assertNotIn("sensitive-token-value", log_text)

    async def test_success_consumes_nonce_and_replay_fails(self):
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with patch.object(
            api_router, "verify_google_id_token", return_value=self._claims(nonce=nonce),
        ), patch.object(
            api_router, "_auth_subscription_snapshot", new=AsyncMock(
                return_value=(None, [], {"status": "inactive"}),
            ),
        ):
            body = {"id_token": "opaque", "nonce": nonce, "install_id": "ignored-test-id"}
            response = await self.client.post("/api/v1/auth/google", json=body)
            replay = await self.client.post("/api/v1/auth/google", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["user"]["is_google"])
        self.assertEqual(replay.status_code, 401)

    async def test_google_session_can_claim_the_shared_three_day_two_device_trial(self):
        """A Google-issued JWT uses the same users.trial_used claim as other logins."""
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with (
            patch.object(
                api_router, "verify_google_id_token",
                return_value=self._claims(sub="google-trial-test", nonce=nonce),
            ),
            patch.object(
                api_router, "_auth_subscription_snapshot", new=AsyncMock(
                    return_value=(None, [], {"status": "inactive", "trial_available": True}),
                ),
            ),
        ):
            login = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "isolated-test-token", "nonce": nonce},
            )
        self.assertEqual(login.status_code, 200, login.text)
        access_token = login.json()["access"]

        with (
            patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                None,
                {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
            ])),
            patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                "subscription_url": "https://subscription.invalid/google-trial-test",
                "uuid": "isolated-test-client",
            })) as create_client,
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "devices": [], "limit_hwid": 2, "limit_ip": 0,
            })),
            patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])),
        ):
            trial = await self.client.post(
                "/api/v1/subscription/trial", json={},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            retry = await self.client.post(
                "/api/v1/subscription/trial", json={},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        self.assertEqual(trial.status_code, 200, trial.text)
        self.assertEqual(trial.json()["status"], "trial")
        self.assertGreaterEqual(trial.json()["seconds_remaining"], 3 * 86400 - 5)
        self.assertLessEqual(trial.json()["seconds_remaining"], 3 * 86400)
        self.assertEqual(trial.json()["max_devices"], 2)
        self.assertEqual(retry.status_code, 409)
        create_client.assert_awaited_once_with(
            user_id=int(login.json()["user"]["id"]), days=3, limit_ip=0, limit_hwid=2,
        )
        self.assertTrue(await database.has_trial_used(int(login.json()["user"]["id"])))

    async def test_two_subjects_with_same_email_are_not_merged(self):
        ids = []
        for sub in ("subject-a", "subject-b"):
            nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
            with patch.object(
                api_router, "verify_google_id_token", return_value=self._claims(sub=sub, nonce=nonce),
            ), patch.object(
                api_router, "_auth_subscription_snapshot", new=AsyncMock(
                    return_value=(None, [], {"status": "inactive"}),
                ),
            ):
                response = await self.client.post(
                    "/api/v1/auth/google",
                    json={"id_token": "opaque", "nonce": nonce},
                )
            self.assertEqual(response.status_code, 200)
            ids.append(response.json()["user"]["id"])
        self.assertNotEqual(ids[0], ids[1])

    async def test_google_sub_is_not_linked_to_existing_email_account(self):
        await api_router._ensure_auth_db()
        db = await database.get_db()
        credential_id = 734829102
        await db.execute(
            "INSERT INTO users(user_id,username) VALUES(?,?)", (credential_id, "credential user"),
        )
        await db.execute(
            "INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (credential_id, "same@example.test", "stored-hash", "stored-salt", "Existing", int(time.time())),
        )
        await db.commit()
        nonce = (await self.client.post("/api/v1/auth/google/nonce", json={})).json()["nonce"]
        with patch.object(
            api_router, "verify_google_id_token", return_value=self._claims(nonce=nonce),
        ), patch.object(
            api_router, "_auth_subscription_snapshot", new=AsyncMock(
                return_value=(None, [], {"status": "inactive"}),
            ),
        ):
            response = await self.client.post(
                "/api/v1/auth/google", json={"id_token": "opaque", "nonce": nonce},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["user"]["id"], str(credential_id))

