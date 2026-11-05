"""ES256 entitlement contract and authenticated subscription HTTP integration tests."""
import base64
import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

os.environ.setdefault("DB_FILE", "/tmp/test_mobile_entitlements_import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
from entitlements import (
    ENTITLEMENT_AUDIENCE,
    ENTITLEMENT_ISSUER,
    PRIVATE_KEY_FILE_ENV,
    EntitlementSigningError,
    _jwt_from_claims,
    create_entitlement,
    device_binding,
    load_private_key,
    verify_entitlement,
)

ORIGINAL_GET_USER_REAL_SUBSCRIPTION = api_router.get_user_real_subscription


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class TestEntitlementJwtContract(unittest.TestCase):
    def setUp(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()
        self.now = 1_800_000_000
        self.hwid = "test-installation-hwid-123456"
        self.subscription = {
            "status": "active",
            "plan_code": "3m_d5",
            "seconds_remaining": 120 * 86400,
            "max_devices": 5,
        }

    def make_token(self):
        return create_entitlement(
            123456, self.hwid, self.subscription, now=self.now,
            private_key=self.private_key,
        )

    def claims_from(self, token):
        return json.loads(_b64url_decode(token.split(".")[1]))

    def test_valid_signature_has_android_compatible_es256_jwt_format(self):
        token = self.make_token()
        header, payload, signature = token.split(".")
        self.assertEqual(json.loads(_b64url_decode(header)), {"alg": "ES256", "typ": "JWT"})
        self.assertEqual(len(_b64url_decode(signature)), 64)
        claims = verify_entitlement(
            token, self.public_key, expected_user_id=123456,
            expected_hwid=self.hwid, now=self.now,
        )
        self.assertIsNotNone(claims)
        self.assertEqual(claims["iss"], ENTITLEMENT_ISSUER)
        self.assertEqual(claims["aud"], ENTITLEMENT_AUDIENCE)
        self.assertEqual(claims["sub"], "123456")
        self.assertEqual(claims["device_hash"], device_binding(123456, self.hwid))
        self.assertEqual(claims["status"], "active")
        self.assertEqual(claims["plan_code"], "3m_d5")
        self.assertEqual(claims["max_devices"], 5)
        self.assertEqual(claims["exp"], self.now + 72 * 60 * 60)
        self.assertNotIn(self.hwid, payload)

    def test_modified_payload_is_rejected(self):
        token = self.make_token()
        head, body, signature = token.split(".")
        claims = json.loads(_b64url_decode(body))
        claims["status"] = "expired"
        changed = f"{head}.{_b64url_encode(json.dumps(claims, separators=(',', ':')).encode())}.{signature}"
        self.assertIsNone(verify_entitlement(
            changed, self.public_key, expected_user_id=123456,
            expected_hwid=self.hwid, now=self.now,
        ))

    def test_invalid_signature_is_rejected(self):
        head, body, signature = self.make_token().split(".")
        raw = bytearray(_b64url_decode(signature))
        raw[0] ^= 1
        altered = f"{head}.{body}.{_b64url_encode(bytes(raw))}"
        self.assertIsNone(verify_entitlement(
            altered, self.public_key, expected_user_id=123456,
            expected_hwid=self.hwid, now=self.now,
        ))

    def test_expired_entitlement_is_rejected(self):
        token = self.make_token()
        expiry = self.claims_from(token)["exp"]
        self.assertIsNone(verify_entitlement(
            token, self.public_key, expected_user_id=123456,
            expected_hwid=self.hwid, now=expiry,
        ))

    def test_wrong_issuer_and_audience_are_rejected(self):
        claims = self.claims_from(self.make_token())
        for field, value in (("iss", "other-service"), ("aud", "other-client")):
            with self.subTest(field=field):
                changed = dict(claims, **{field: value})
                token = _jwt_from_claims(changed, self.private_key)
                self.assertIsNone(verify_entitlement(
                    token, self.public_key, expected_user_id=123456,
                    expected_hwid=self.hwid, now=self.now,
                ))

    def test_entitlement_is_bound_to_expected_user_and_device(self):
        token = self.make_token()
        self.assertIsNone(verify_entitlement(
            token, self.public_key, expected_user_id=999999,
            expected_hwid=self.hwid, now=self.now,
        ))
        self.assertIsNone(verify_entitlement(
            token, self.public_key, expected_user_id=123456,
            expected_hwid="another-installation-hwid-9876", now=self.now,
        ))

    def test_expiry_is_capped_by_actual_subscription_expiry(self):
        subscription = {**self.subscription, "seconds_remaining": 900}
        token = create_entitlement(
            123456, self.hwid, subscription, now=self.now,
            private_key=self.private_key,
        )
        self.assertEqual(self.claims_from(token)["exp"], self.now + 900)

    def test_missing_or_insecure_signing_file_fails_closed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PRIVATE_KEY_FILE_ENV, None)
            with self.assertRaises(EntitlementSigningError):
                load_private_key()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.pem"
            path.write_bytes(self.private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            path.chmod(0o640)
            with patch.dict(os.environ, {PRIVATE_KEY_FILE_ENV: str(path)}):
                with self.assertRaises(EntitlementSigningError):
                    load_private_key()


class TestSubscriptionEntitlementHttpIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(api_router.api_router, prefix="/api/v1")
        app.dependency_overrides[api_router.get_current_user_id] = lambda: 7654321
        app.dependency_overrides[api_router._servers_user_id] = lambda: 7654321
        cls.client = TestClient(app)

    def setUp(self):
        from xui_client import get_xui_devices

        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()
        self.temp = tempfile.TemporaryDirectory()
        self.key_path = Path(self.temp.name) / "entitlement.pem"
        self.key_path.write_bytes(self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        self.key_path.chmod(0o600)
        self.hwid = "integration-test-device-7654321"
        fingerprint = hashlib.sha256(self.hwid.encode()).hexdigest()[:16]
        self.device_state = {
            "limit_hwid": 2,
            "limit_ip": 0,
            "devices": [{"id": 17, "fingerprint": fingerprint, "model": "test"}],
        }
        self.active = {
            "status": "active", "plan_code": "1m_d2", "seconds_remaining": 86_400,
            "max_devices": 2, "active_devices": 0, "subscription_url": "https://example.invalid/sub",
        }
        self.subscription_patch = patch.object(
            api_router, "get_user_real_subscription",
            new=AsyncMock(return_value=("https://example.invalid/sub", [], dict(self.active))),
        )
        self.devices_patch = patch(
            "xui_client.get_xui_devices", new=AsyncMock(return_value=self.device_state),
        )
        self.panel_presence_patch = patch(
            "xui_client.has_xui_client", new=AsyncMock(return_value=True),
        )
        self.panel_details_patch = patch(
            "xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "expiry_time": (int(api_router.time.time()) + 86400) * 1000,
                "enable": True,
            }),
        )
        self.subscription_patch.start()
        self.devices_mock = self.devices_patch.start()
        self.panel_presence_mock = self.panel_presence_patch.start()
        self.panel_details_mock = self.panel_details_patch.start()
        self.env_patch = patch.dict(os.environ, {PRIVATE_KEY_FILE_ENV: str(self.key_path)})
        self.env_patch.start()
        self.key_loader_patch = patch("entitlements.load_private_key", return_value=self.private_key)
        self.key_loader_patch.start()
        self.api_key_loader_patch = patch.object(api_router, "load_private_key", return_value=self.private_key)
        self.api_key_loader_patch.start()
        self.bootstrap_delete_patch = patch.object(
            api_router.database, "delete_mobile_trial_bootstrap", new=AsyncMock(),
        )
        self.bootstrap_delete_patch.start()
        self.device_bootstrap_get_patch = patch.object(
            api_router.database, "get_mobile_device_bootstrap", new=AsyncMock(return_value=None),
        )
        self.device_bootstrap_get_mock = self.device_bootstrap_get_patch.start()
        self.device_bootstrap_reserve_patch = patch.object(
            api_router.database, "reserve_mobile_device_bootstrap", new=AsyncMock(return_value=None),
        )
        self.device_bootstrap_reserve_mock = self.device_bootstrap_reserve_patch.start()
        self.device_bootstrap_delete_patch = patch.object(
            api_router.database, "delete_mobile_device_bootstrap", new=AsyncMock(),
        )
        self.device_bootstrap_delete_mock = self.device_bootstrap_delete_patch.start()
        self.trial_bootstrap_get_patch = patch.object(
            api_router.database, "get_mobile_trial_bootstrap", new=AsyncMock(return_value=None),
        )
        self.trial_bootstrap_get_patch.start()
        self.trial_used_patch = patch.object(
            api_router.database, "has_trial_used", new=AsyncMock(return_value=True),
        )
        self.trial_used_patch.start()
        self.trial_reservation_patch = patch.object(
            api_router.database, "reserve_trial_claim", new=AsyncMock(return_value="test-trial-claim"),
        )
        self.trial_reservation_mock = self.trial_reservation_patch.start()
        self.trial_completion_patch = patch.object(
            api_router.database, "complete_trial_claim", new=AsyncMock(return_value=True),
        )
        self.trial_completion_mock = self.trial_completion_patch.start()
        self.trial_pending_patch = patch.object(
            api_router.database, "get_trial_claim_reservation", new=AsyncMock(return_value=None),
        )
        self.trial_pending_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.api_key_loader_patch.stop()
        self.bootstrap_delete_patch.stop()
        self.key_loader_patch.stop()
        self.devices_patch.stop()
        self.panel_presence_patch.stop()
        self.subscription_patch.stop()
        self.temp.cleanup()
        self.panel_details_patch.stop()
        self.device_bootstrap_get_patch.stop()
        self.device_bootstrap_reserve_patch.stop()
        self.device_bootstrap_delete_patch.stop()
        self.trial_bootstrap_get_patch.stop()
        self.trial_used_patch.stop()
        self.trial_reservation_patch.stop()
        self.trial_completion_patch.stop()
        self.trial_pending_patch.stop()

    def make_request(self, path, *, method="POST", headers=None):
        from starlette.requests import Request

        raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
        return Request({
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "https", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": raw_headers,
            "client": ("127.0.0.1", 1), "server": ("test.invalid", 443),
        })

    def test_http_status_contains_signed_entitlement_for_authenticated_registered_device(self):
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["max_devices"], 2)
        self.assertEqual(body["active_devices"], 1)
        claims = verify_entitlement(
            body["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        )
        self.assertIsNotNone(claims)
        self.assertNotIn(self.hwid, body["entitlement"])

    def test_http_status_preserves_subscription_facts_but_not_credentials_for_unregistered_device(self):
        headers = {"X-Entitlement-Version": "es256-v1"}
        missing = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(missing.status_code, 422)
        self.device_state["devices"] = []
        unregistered = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, **headers},
        )
        self.assertEqual(unregistered.status_code, 200)
        body = unregistered.json()
        self.assertEqual(body["status"], "active")
        self.assertTrue(body["device_confirmation_required"])
        self.assertIsNone(body["entitlement"])
        self.assertIsNone(body["subscription_url"])

    def test_telegram_login_does_not_hide_active_status_when_panel_confirmation_is_pending(self):
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "authenticated", "user_id": 7654321,
            "username": "test_user", "full_name": "Test User",
        })), patch.object(api_router.database, "consume_mobile_auth_session", new=AsyncMock(return_value=True)):
            self.device_state["devices"] = []
            response = self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": "auth_test-state"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "authenticated")
        self.assertTrue(body["access_token"])
        self.assertEqual(body["subscription"]["status"], "active")
        self.assertTrue(body["subscription"]["device_confirmation_required"])
        self.assertIsNone(body["subscription"].get("entitlement"))
        self.assertIsNone(body["subscription"].get("subscription_url"))
        self.assertEqual(body["servers"], [])

    def test_signed_client_cannot_fetch_vpn_config_without_panel_registered_hwid(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        with patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                    "limit_hwid": 2, "devices": [],
                })):
            server_response = self.client.get("/api/v1/servers", headers=headers)
            session_response = self.client.post(
                "/api/v1/vpn/session", json={"node_code": "auto"}, headers=headers,
            )
        self.assertEqual(server_response.status_code, 403)
        self.assertEqual(session_response.status_code, 403)

    def test_panel_verified_trial_limits_allow_only_the_bound_short_bootstrap_device(self):
        request_headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        bootstrap_expiry = int(api_router.time.time()) + 300
        self.device_state["devices"] = []
        with patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value=self.device_state)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                    "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000,
                })), \
                patch.object(api_router.database, "get_mobile_trial_bootstrap", new=AsyncMock(
                    return_value=bootstrap_expiry,
                )), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=True)), \
                patch.object(api_router, "get_user_real_subscription", new=AsyncMock(return_value=(
                    "https://example.invalid/trial", [{"node_code": "test-node"}], dict(self.active),
                ))):
            response = self.client.get("/api/v1/servers", headers=request_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["servers"][0]["node_code"], "test-node")

    def test_bootstrap_is_denied_when_panel_limits_are_not_exactly_two_and_unlimited_ip(self):
        request = self.make_request("/api/v1/servers", method="GET", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        from fastapi import HTTPException
        with patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                    "limit_hwid": 5, "limit_ip": 0, "devices": [],
                })), \
                patch.object(api_router.database, "get_mobile_trial_bootstrap", new=AsyncMock(
                    return_value=int(api_router.time.time()) + 300,
                )), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=True)):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api_router._require_opted_in_device_access(7654321, request))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_http_status_accepts_panel_311_fingerprint_without_bootstrap(self):
        self.device_state["devices"] = [{
            "fingerprint": hashlib.sha256(self.hwid.encode()).hexdigest()[:12],
        }]
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["entitlement"])
        self.assertFalse(response.json().get("entitlement_bootstrap", False))
        self.device_bootstrap_reserve_mock.assert_not_awaited()

    def test_http_status_rejects_wrong_panel_311_fingerprint(self):
        self.device_state["devices"] = [{
            "fingerprint": hashlib.sha256(b"other-installation").hexdigest()[:12],
        }]
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["device_confirmation_required"])
        self.assertIsNone(response.json()["entitlement"])

    def test_http_status_rejects_short_panel_fingerprint(self):
        self.device_state["devices"] = [{"fingerprint": hashlib.sha256(self.hwid.encode()).hexdigest()[:8]}]
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "active")
        self.assertTrue(response.json()["device_confirmation_required"])
        self.assertIsNone(response.json()["entitlement"])
        self.assertIsNone(response.json()["subscription_url"])

    def test_http_status_fails_closed_without_signing_configuration(self):
        with patch("entitlements.load_private_key", side_effect=EntitlementSigningError("not configured")):
            response = self.client.get(
                "/api/v1/subscription/status",
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("entitlement", response.json())

    def test_legacy_status_keeps_response_shape_without_entitlement_or_device_lookup(self):
        response = self.client.get("/api/v1/subscription/status", headers={"X-HWID": self.hwid})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("entitlement", response.json())
        self.devices_mock.assert_not_awaited()

    def test_unsupported_entitlement_version_is_rejected(self):
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v2"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("entitlement", response.json())

    def test_new_email_login_returns_signed_entitlement_only_for_opted_in_device(self):
        class Cursor:
            async def fetchone(self):
                return (7654321, "stored-hash", "stored-salt", "Test User")

        class DB:
            async def execute(self, *_args):
                return Cursor()

        with patch.object(api_router, "_ensure_auth_db", new=AsyncMock()), \
                patch.object(api_router.database, "get_db", new=AsyncMock(return_value=DB())), \
                patch.object(api_router, "hash_password", return_value="stored-hash"), \
                patch.object(api_router, "create_simple_jwt", return_value="test-access-token"):
            response = self.client.post(
                "/api/v1/auth/login", json={"email": "test@example.invalid", "password": "test-password"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()["subscription"]
        self.assertIsNotNone(verify_entitlement(
            body["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        ))

    def test_new_email_login_succeeds_without_access_when_signer_is_unavailable(self):
        class Cursor:
            async def fetchone(self):
                return (7654321, "stored-hash", "stored-salt", "Test User")

        class DB:
            async def execute(self, *_args):
                return Cursor()

        with patch.object(api_router, "_ensure_auth_db", new=AsyncMock()), \
                patch.object(api_router.database, "get_db", new=AsyncMock(return_value=DB())), \
                patch.object(api_router, "hash_password", return_value="stored-hash"), \
                patch.object(api_router, "create_simple_jwt", return_value="test-access-token"), \
                patch("entitlements.load_private_key", side_effect=EntitlementSigningError("not configured")):
            response = self.client.post(
                "/api/v1/auth/login", json={"email": "test@example.invalid", "password": "test-password"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["subscription"]["status"], "unknown")
        self.assertNotIn("entitlement", response.json()["subscription"])

    def test_legacy_email_login_has_no_entitlement_or_panel_lookup(self):
        class Cursor:
            async def fetchone(self):
                return (7654321, "stored-hash", "stored-salt", "Test User")

        class DB:
            async def execute(self, *_args):
                return Cursor()

        with patch.object(api_router, "_ensure_auth_db", new=AsyncMock()), \
                patch.object(api_router.database, "get_db", new=AsyncMock(return_value=DB())), \
                patch.object(api_router, "hash_password", return_value="stored-hash"), \
                patch.object(api_router, "create_simple_jwt", return_value="test-access-token"):
            response = self.client.post(
                "/api/v1/auth/login", json={"email": "test@example.invalid", "password": "test-password"},
                headers={"X-HWID": self.hwid},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("entitlement", response.json()["subscription"])
        self.devices_mock.assert_not_awaited()

    def test_new_telegram_login_preserves_active_status_without_credentials_when_device_is_unregistered(self):
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "authenticated", "user_id": 7654321,
            "username": "test_user", "full_name": "Test User",
        })), patch.object(api_router.database, "consume_mobile_auth_session", new=AsyncMock(return_value=True)):
            self.device_state["devices"] = []
            response = self.client.get(
                "/api/v1/auth/telegram/check-session", params={"token": "auth_test-state"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "authenticated")
        self.assertEqual(body["subscription"]["status"], "active")
        self.assertTrue(body["subscription"]["device_confirmation_required"])
        self.assertIsNone(body["subscription"]["entitlement"])
        self.assertIsNone(body["subscription"]["subscription_url"])
        self.assertEqual(body["servers"], [])

    def test_telegram_check_session_accepts_android_post_json_contract(self):
        # Android PR #25 sends POST /auth/telegram/check-session with {"token": ...}.
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "pending",
        })) as get_session:
            response = self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": "auth_contract-test"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        state_hash = hashlib.sha256(b"contract-test").hexdigest()
        get_session.assert_awaited_once_with(state_hash)

    def test_telegram_check_session_post_completes_first_login(self):
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "authenticated", "user_id": 7654321,
            "username": "test_user", "full_name": "Test User",
        })), patch.object(api_router.database, "consume_mobile_auth_session", new=AsyncMock(return_value=True)):
            response = self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": "auth_first-login"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "authenticated")
        self.assertEqual(response.json()["user"]["id"], "7654321")

    def test_new_telegram_login_completes_without_access_when_signer_unavailable(self):
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "authenticated", "user_id": 7654321,
            "username": "test_user", "full_name": "Test User",
        })), patch.object(api_router.database, "consume_mobile_auth_session", new=AsyncMock(return_value=True)), \
                patch("entitlements.load_private_key", side_effect=EntitlementSigningError("not configured")):
            response = self.client.get(
                "/api/v1/auth/telegram/check-session", params={"token": "auth_test-state"},
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "authenticated")
        self.assertNotIn("entitlement", response.json()["subscription"])

    def test_legacy_telegram_login_keeps_legacy_subscription_shape(self):
        with patch.object(api_router.database, "get_mobile_auth_session", new=AsyncMock(return_value={
            "status": "authenticated", "user_id": 7654321,
            "username": "test_user", "full_name": "Test User",
        })), patch.object(api_router.database, "consume_mobile_auth_session", new=AsyncMock(return_value=True)), patch.object(
            api_router.database, "get_db", new=AsyncMock(side_effect=RuntimeError("isolated test")),
        ):
            response = self.client.get(
                "/api/v1/auth/telegram/check-session", params={"token": "auth_test-state"},
                headers={"X-HWID": self.hwid},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("entitlement", response.json()["subscription"])

    def test_legacy_trial_does_not_require_signer_or_emit_entitlement(self):
        request = self.make_request("/api/v1/subscription/trial", headers={"X-HWID": self.hwid})
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "add_key", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[None, {
                    "enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000,
                }])), \
                patch("xui_client.has_xui_client", new=AsyncMock(return_value=False)), \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                    "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
                })), \
                patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])), \
                patch.object(api_router, "load_private_key", side_effect=AssertionError("legacy path loaded signer")):
            result = asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(result["status"], "trial")
        self.assertNotIn("entitlement", result)

    def test_new_trial_without_registered_device_gets_fixed_short_bootstrap(self):
        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        bootstrap_expiry = int(api_router.time.time()) + api_router.MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS
        with patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
            "limit_hwid": 2, "limit_ip": 0, "devices": [],
        })), patch.object(
            api_router.database, "has_trial_used", new=AsyncMock(side_effect=[False] + [True] * 8),
        ) as trial_used, patch.object(
            api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True),
        ) as claim_trial, patch.object(
            api_router.database, "create_mobile_trial_bootstrap", new=AsyncMock(return_value=True),
        ) as create_bootstrap, patch.object(
            api_router.database, "get_mobile_trial_bootstrap",
            new=AsyncMock(return_value=bootstrap_expiry),
        ), patch.object(
            api_router.database, "delete_mobile_trial_bootstrap", new=AsyncMock(),
        ), patch.object(
            api_router.database, "get_user_keys", new=AsyncMock(return_value=[]),
        ), patch.object(
            api_router.database, "add_key", new=AsyncMock(return_value=True),
        ), patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
            None,
            {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
            {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
        ])), patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), patch(
            "xui_client.create_xui_user", new=AsyncMock(return_value={
                "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
            }),
        ), patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])):
            result = asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertGreaterEqual(trial_used.await_count, 2)
        self.assertTrue(all(call.args == (7654321,) for call in trial_used.await_args_list))
        claim_trial.assert_awaited_once_with(7654321)
        create_bootstrap.assert_awaited_once_with(
            7654321, device_binding(7654321, self.hwid), bootstrap_expiry,
        )
        self.assertEqual(result["status"], "trial")
        self.assertEqual(result["max_devices"], 2)
        self.assertTrue(result["entitlement_bootstrap"])
        claims = verify_entitlement(
            result["entitlement"], self.public_key,
            expected_user_id=7654321, expected_hwid=self.hwid,
        )
        self.assertIsNotNone(claims)
        self.assertEqual(claims["max_devices"], 2)
        self.assertLessEqual(claims["exp"] - claims["iat"], api_router.MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS)

    def test_trial_status_is_offered_before_panel_hwid_exists_when_signer_and_hwid_are_valid(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        with patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "has_trial_claim_reservation", new=AsyncMock(return_value=False)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)), \
                patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=None)), \
                patch.object(api_router, "load_private_key", return_value=object()):
            _url, _servers, status = asyncio.run(
                ORIGINAL_GET_USER_REAL_SUBSCRIPTION(7654321, headers)
            )
        self.assertTrue(status["trial_available"])
        self.assertIsNone(status["trial_unavailable_reason"])

    def test_trial_status_http_is_available_before_panel_registers_hwid(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        with patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "has_trial_claim_reservation", new=AsyncMock(return_value=False)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)), \
                patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=None)), \
                patch.object(api_router, "load_private_key", return_value=self.private_key), \
                patch.object(
                    api_router, "get_user_real_subscription",
                    new=AsyncMock(side_effect=ORIGINAL_GET_USER_REAL_SUBSCRIPTION),
                ):
            response = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "none")
        self.assertTrue(response.json()["trial_available"])
        self.assertIsNone(response.json().get("entitlement"))

    def test_trial_status_remains_available_to_legacy_client_with_no_panel_client(self):
        with patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "has_trial_claim_reservation", new=AsyncMock(return_value=False)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)), \
                patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=None)):
            _url, _servers, status = asyncio.run(
                ORIGINAL_GET_USER_REAL_SUBSCRIPTION(7654321, {})
            )
        self.assertTrue(status["trial_available"])

    def test_trial_status_can_use_expired_panel_device_without_active_access(self):
        now = int(api_router.time.time())
        panel_client = {
            "subscription_url": "https://example.invalid/expired",
            "expiry_time": (now - 100) * 1000,
            "limit_hwid": 2,
        }
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        with patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "has_trial_claim_reservation", new=AsyncMock(return_value=False)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=panel_client)), \
                patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=None)), \
                patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value=self.device_state)):
            _url, _servers, status = asyncio.run(
                ORIGINAL_GET_USER_REAL_SUBSCRIPTION(7654321, headers)
            )
        self.assertEqual(status["status"], "none")
        self.assertTrue(status["trial_available"])

    def test_new_trial_with_registered_device_returns_signed_entitlement(self):
        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        with patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value=self.device_state)), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "add_key", new=AsyncMock(return_value=True)) as add_key, \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                    {"enable": True, "expiry_time": (int(api_router.time.time()) - 100) * 1000},
                    {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
                ])), \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                    "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
                })) as create_panel_client, \
                patch.object(api_router.database, "delete_mobile_trial_bootstrap", new=AsyncMock()), \
                patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])):
            result = asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        create_panel_client.assert_awaited_once_with(
            user_id=7654321, days=3, limit_ip=0, limit_hwid=2,
        )
        self.assertEqual(add_key.await_args.kwargs["days"], 3)
        # Legacy DB column `limit_ip` stores the bot's device allowance;
        # actual panel IP access remains unlimited (limitIp=0).
        self.assertEqual(add_key.await_args.kwargs["limit_ip"], 2)
        self.assertIsNotNone(verify_entitlement(
            result["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        ))
        self.assertEqual(result["seconds_remaining"], 3 * 24 * 60 * 60)
        self.assertEqual(result["max_devices"], 2)
        self.assertEqual(result["active_devices"], 1)

    def test_trial_refuses_to_report_access_when_panel_limit_readback_differs(self):
        request = self.make_request("/api/v1/subscription/trial")
        wrong_panel_limits = {"limit_hwid": 5, "limit_ip": 0, "devices": []}
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "add_key", new=AsyncMock()) as add_key, \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                    {"enable": True, "expiry_time": (int(api_router.time.time()) - 100) * 1000},
                    {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
                ])), \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                    "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
                })), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value=wrong_panel_limits)):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(ctx.exception.status_code, 503)
        add_key.assert_not_awaited()

    def test_trial_recovers_reserved_panel_client_without_extending_expiry(self):
        now = int(api_router.time.time())
        created = now - 300
        expiry = created + 3 * 86400
        client = {"enable": True, "expiry_time": expiry * 1000,
                  "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid"}
        request = self.make_request("/api/v1/subscription/trial")
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "get_trial_claim_reservation", new=AsyncMock(return_value={"claim_id": "claim", "created_at": created})), \
                patch.object(api_router.database, "complete_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "add_key", new=AsyncMock()) as add_key, \
                patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=client)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(side_effect=[
                    {"limit_hwid": 0, "limit_ip": 0, "devices": []},
                    {"limit_hwid": 2, "limit_ip": 0, "devices": []},
                ])), \
                patch("xui_client.set_xui_client_limits", new=AsyncMock(return_value=True)) as repair, \
                patch("xui_client.create_xui_user", new=AsyncMock()) as create, \
                patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])):
            result = asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(result["status"], "trial")
        self.assertLessEqual(result["seconds_remaining"], 3 * 86400 - 299)
        repair.assert_awaited_once_with(7654321, limit_ip=0, limit_hwid=2)
        create.assert_not_awaited()
        self.assertEqual(add_key.await_args.kwargs["expiry"], expiry)

    def test_trial_repeat_returns_conflict_without_reprovisioning_or_extending(self):
        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        create_panel_client = AsyncMock(return_value={
            "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
        })
        with patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_devices", new=AsyncMock(return_value=self.device_state)), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(side_effect=[False, True])), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "add_key", new=AsyncMock(return_value=True)), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                    {"enable": True, "expiry_time": (int(api_router.time.time()) - 100) * 1000},
                    {"enable": True, "expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
                ])), \
                patch("xui_client.create_xui_user", new=create_panel_client), \
                patch.object(api_router.database, "delete_mobile_trial_bootstrap", new=AsyncMock()), \
                patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])):
            first = asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(first["seconds_remaining"], 3 * 24 * 60 * 60)
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        create_panel_client.assert_awaited_once()

    def test_trial_does_not_modify_an_existing_active_panel_client(self):
        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        with patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                    "expiry_time": (int(api_router.time.time()) + 3600) * 1000,
                })), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)) as claim_trial, \
                patch("xui_client.create_xui_user", new=AsyncMock()) as create_panel_client:
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        claim_trial.assert_not_awaited()
        create_panel_client.assert_not_awaited()

    def test_panel_read_failure_after_provisioning_keeps_trial_claim_reserved(self):
        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        with patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                    None, {"expiry_time": (int(api_router.time.time()) + 3 * 86400) * 1000},
                ])), \
                patch("xui_client.get_xui_devices", new=AsyncMock(side_effect=RuntimeError("panel unavailable"))), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)) as claim_trial, \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                    "subscription_url": "https://example.invalid/trial", "uuid": "test-uuid",
                })) as create_panel_client, \
                patch.object(api_router.database, "reset_trial_for_user", new=AsyncMock()) as reset_trial:
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(getattr(raised.exception, "status_code", None), 503)
        claim_trial.assert_awaited_once_with(7654321)
        create_panel_client.assert_awaited_once()
        reset_trial.assert_not_awaited()

    def test_trial_does_not_claim_when_panel_lookup_fails(self):
        request = self.make_request("/api/v1/subscription/trial", headers={"X-HWID": self.hwid})
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=RuntimeError("panel unavailable"))), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)) as claim_trial:
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))
        self.assertEqual(getattr(raised.exception, "status_code", None), 503)
        claim_trial.assert_not_awaited()

    def test_confirmed_no_access_after_provisioning_failure_releases_trial_claim(self):
        request = self.make_request("/api/v1/subscription/trial", headers={"X-HWID": self.hwid})
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)) as claim_trial, \
                patch.object(api_router.database, "release_trial_claim", new=AsyncMock(return_value=True)) as release_trial, \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[None, None, None])), \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value=None)) as create_panel_client:
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))

        self.assertEqual(getattr(raised.exception, "status_code", None), 503)
        claim_trial.assert_awaited_once()
        create_panel_client.assert_awaited_once()
        release_trial.assert_awaited_once_with(7654321, True)

    def test_ambiguous_panel_readback_keeps_trial_reserved_after_provisioning_error(self):
        request = self.make_request("/api/v1/subscription/trial", headers={"X-HWID": self.hwid})
        with patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)), \
                patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])), \
                patch.object(api_router.database, "reserve_trial_claim", new=AsyncMock(return_value=True)), \
                patch.object(api_router.database, "reset_trial_for_user", new=AsyncMock()) as reset_trial, \
                patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[None, RuntimeError("panel unavailable")])), \
                patch("xui_client.create_xui_user", new=AsyncMock(return_value=None)):
            with self.assertRaises(Exception) as raised:
                asyncio.run(api_router.activate_trial(api_router.TrialRequest(), request, user_id=7654321))

        self.assertEqual(getattr(raised.exception, "status_code", None), 503)
        reset_trial.assert_not_awaited()

    def test_payment_check_adds_entitlement_only_for_opted_in_registered_device(self):
        record = {"user_id": 7654321, "status": "success"}
        for path in ("/api/v1/payment/check", "/api/v1/mobile/v2/payment/check"):
            with self.subTest(path=path), patch.object(
                api_router.database, "get_mobile_payment_record", new=AsyncMock(return_value=record),
            ):
                response = self.client.get(
                    path, params={"invoice_id": "test-invoice-123", "provider": "yookassa"},
                    headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            subscription = response.json()["subscription"]
            self.assertIsNotNone(verify_entitlement(
                subscription["entitlement"], self.public_key, expected_user_id=7654321,
                expected_hwid=self.hwid,
            ))

    def test_legacy_payment_check_has_no_entitlement(self):
        for path in ("/api/v1/payment/check", "/api/v1/mobile/v2/payment/check"):
            with self.subTest(path=path), patch.object(
                api_router.database, "get_mobile_payment_record",
                new=AsyncMock(return_value={"user_id": 7654321, "status": "success"}),
            ):
                response = self.client.get(
                    path, params={"invoice_id": "test-invoice-123", "provider": "yookassa"},
                    headers={"X-HWID": self.hwid},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("entitlement", response.json()["subscription"])
        self.devices_mock.assert_not_awaited()

    def test_mobile_payment_check_fails_closed_without_signer(self):
        for path in ("/api/v1/payment/check", "/api/v1/mobile/v2/payment/check"):
            with self.subTest(path=path), patch.object(
                api_router.database, "get_mobile_payment_record",
                new=AsyncMock(return_value={"user_id": 7654321, "status": "success"}),
            ), patch("entitlements.load_private_key", side_effect=EntitlementSigningError("not configured")):
                response = self.client.get(
                    path, params={"invoice_id": "test-invoice-123", "provider": "yookassa"},
                    headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
                )
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("entitlement", response.json().get("subscription") or {})

    def test_trial_without_signer_fails_before_consuming_trial(self):
        from fastapi import HTTPException

        request = self.make_request("/api/v1/subscription/trial", headers={
            "X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1",
        })
        with patch.object(api_router, "load_private_key", side_effect=EntitlementSigningError("not configured")), \
                patch.object(api_router.database, "has_trial_used", new=AsyncMock()) as trial_check:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(api_router.activate_trial(
                    api_router.TrialRequest(), request, user_id=7654321,
                ))
        self.assertEqual(raised.exception.status_code, 503)
        trial_check.assert_not_awaited()

    def test_active_subscription_gets_signed_bootstrap_when_panel_confirms_free_slot(self):
        self.device_state["devices"] = []
        fixed_expiry = int(api_router.time.time()) + 600
        self.device_bootstrap_reserve_mock.return_value = fixed_expiry
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["max_devices"], 2)
        self.assertEqual(body["active_devices"], 0)
        self.assertTrue(body["entitlement_bootstrap"])
        self.assertLessEqual(body["entitlement_expires_at"], fixed_expiry)
        self.assertGreater(body["entitlement_expires_at"], int(api_router.time.time()))
        claims = verify_entitlement(
            body["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        )
        self.assertIsNotNone(claims)
        self.assertEqual(claims["status"], "active")
        self.device_bootstrap_reserve_mock.assert_awaited_once()

    def test_active_subscription_bootstrap_supports_server_limits_five_and_ten(self):
        self.device_state["devices"] = []
        for limit in (5, 10):
            with self.subTest(limit=limit):
                self.device_state["limit_hwid"] = limit
                fixed_expiry = int(api_router.time.time()) + 600
                self.device_bootstrap_get_mock.return_value = None
                self.device_bootstrap_reserve_mock.return_value = fixed_expiry
                plan = {**self.active, "max_devices": limit, "plan_code": f"1m_d{limit}"}
                with patch.object(api_router, "get_user_real_subscription", new=AsyncMock(return_value=(
                    "https://example.invalid/sub", [], dict(plan),
                ))):
                    response = self.client.get(
                        "/api/v1/subscription/status",
                        headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertTrue(body["entitlement_bootstrap"])
                claims = verify_entitlement(
                    body["entitlement"], self.public_key, expected_user_id=7654321,
                    expected_hwid=self.hwid,
                )
                self.assertIsNotNone(claims)
                self.assertEqual(claims["max_devices"], limit)
                self.device_bootstrap_reserve_mock.reset_mock()

    def test_bootstrap_config_response_contains_signed_entitlement(self):
        self.device_state["devices"] = []
        fixed_expiry = int(api_router.time.time()) + 600
        self.device_bootstrap_reserve_mock.return_value = fixed_expiry
        self.device_bootstrap_get_mock.side_effect = [None, fixed_expiry]
        node = {"node_code": "test-node", "config": {"server": "test.invalid", "uuid": "test-uuid"}}
        with patch.object(api_router, "get_user_real_subscription", new=AsyncMock(return_value=(
            "https://example.invalid/sub", [node], dict(self.active),
        ))):
            response = self.client.get(
                "/api/v1/servers",
                headers={"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["entitlement_bootstrap"])
        self.assertEqual(body["servers"][0]["node_code"], "test-node")
        self.assertIsNotNone(verify_entitlement(
            body["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        ))

    def test_status_bootstrap_then_servers_and_panel_confirmation_transition(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        fixed_expiry = int(api_router.time.time()) + 600
        self.device_state["devices"] = []
        self.device_bootstrap_reserve_mock.return_value = fixed_expiry
        self.device_bootstrap_get_mock.side_effect = [None, fixed_expiry, fixed_expiry]
        node = {"node_code": "test-node", "config": {"server": "test.invalid"}}
        with patch.object(api_router, "get_user_real_subscription", new=AsyncMock(return_value=(
            "https://example.invalid/sub", [node], dict(self.active),
        ))):
            status = self.client.get("/api/v1/subscription/status", headers=headers)
            self.assertEqual(status.status_code, 200, status.text)
            status_body = status.json()
            self.assertTrue(status_body["entitlement_bootstrap"])
            self.assertGreater(status_body["entitlement_expires_at"], int(api_router.time.time()))
            self.assertLessEqual(status_body["entitlement_expires_at"], fixed_expiry)
            self.assertIsNotNone(verify_entitlement(
                status_body["entitlement"], self.public_key,
                expected_user_id=7654321, expected_hwid=self.hwid,
            ))

            servers = self.client.get("/api/v1/servers", headers=headers)
            self.assertEqual(servers.status_code, 200, servers.text)
            server_body = servers.json()
            self.assertEqual(server_body["servers"][0]["node_code"], "test-node")
            self.assertTrue(server_body["entitlement_bootstrap"])
            self.assertLessEqual(server_body["entitlement_expires_at"], fixed_expiry)
            self.assertIsNotNone(verify_entitlement(
                server_body["entitlement"], self.public_key,
                expected_user_id=7654321, expected_hwid=self.hwid,
            ))

            self.device_state["devices"] = [{
                "fingerprint": hashlib.sha256(self.hwid.encode()).hexdigest()[:16],
            }]
            confirmed = self.client.get("/api/v1/servers", headers=headers)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        confirmed_body = confirmed.json()
        self.assertNotIn("entitlement_bootstrap", confirmed_body)
        self.assertNotIn("entitlement_expires_at", confirmed_body)
        self.assertIsNotNone(verify_entitlement(
            confirmed_body["entitlement"], self.public_key,
            expected_user_id=7654321, expected_hwid=self.hwid,
        ))
        self.device_bootstrap_delete_mock.assert_awaited()

    def test_servers_denial_returns_safe_stage_code_and_correlation_id(self):
        self.device_state["devices"] = [
            {"fingerprint": "1" * 16}, {"fingerprint": "2" * 16},
        ]
        response = self.client.get(
            "/api/v1/servers",
            headers={
                "X-HWID": self.hwid,
                "X-Entitlement-Version": "es256-v1",
                "X-Correlation-ID": "servers-device-denied-1",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers.get("X-Correlation-ID"), "servers-device-denied-1")
        self.assertEqual(response.headers.get("X-Error-Code"), "device_confirmation_required")
        self.assertNotIn(self.hwid, response.text)

    def test_servers_authentication_denial_also_has_safe_correlation(self):
        self.client.app.dependency_overrides.pop(api_router._servers_user_id)
        try:
            response = self.client.get(
                "/api/v1/servers",
                headers={"X-Correlation-ID": "servers-auth-denied-1"},
            )
        finally:
            self.client.app.dependency_overrides[api_router._servers_user_id] = lambda: 7654321
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("X-Correlation-ID"), "servers-auth-denied-1")
        self.assertEqual(response.headers.get("X-Error-Code"), "authentication_required")

    def test_full_limit_and_expired_panel_client_never_receive_bootstrap(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        self.device_state["devices"] = [{"fingerprint": "1" * 16}, {"fingerprint": "2" * 16}]
        full = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(full.status_code, 200, full.text)
        self.assertIsNone(full.json().get("entitlement"))
        self.assertTrue(full.json()["device_confirmation_required"])
        self.assertEqual(
            self.client.get("/api/v1/servers", headers=headers).status_code,
            403,
        )

        self.device_state["devices"] = []
        self.panel_details_mock.return_value = {
            "expiry_time": (int(api_router.time.time()) - 10) * 1000,
            "enable": True,
        }
        expired = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(expired.status_code, 200, expired.text)
        self.assertIsNone(expired.json().get("entitlement"))
        self.assertTrue(expired.json()["device_confirmation_required"])
        self.device_bootstrap_reserve_mock.assert_not_awaited()

        self.panel_details_mock.return_value = {
            "expiry_time": (int(api_router.time.time()) + 86400) * 1000,
            "enable": True,
        }
        self.device_state["limit_ip"] = 1
        wrong_ip_limit = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertIsNone(wrong_ip_limit.json().get("entitlement"))
        self.assertTrue(wrong_ip_limit.json()["device_confirmation_required"])
        self.device_state["limit_ip"] = 0
        self.device_state["limit_hwid"] = 1
        wrong_device_limit = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertIsNone(wrong_device_limit.json().get("entitlement"))
        self.device_state["limit_hwid"] = 2

        self.panel_details_mock.return_value = {
            "expiry_time": (int(api_router.time.time()) + 86400) * 1000,
            "enable": False,
        }
        disabled = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertIsNone(disabled.json().get("entitlement"))
        self.device_bootstrap_reserve_mock.assert_not_awaited()

    def test_panel_error_fails_closed_and_repeated_status_keeps_original_expiry(self):
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        self.device_state["devices"] = []
        self.panel_details_mock.side_effect = RuntimeError("panel unavailable")
        failed = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("entitlement", failed.json())

        self.panel_details_mock.side_effect = None
        self.panel_details_mock.return_value = {
            "expiry_time": (int(api_router.time.time()) + 86400) * 1000,
            "enable": True,
        }
        fixed_expiry = int(api_router.time.time()) + 500
        self.device_bootstrap_get_mock.side_effect = [None, fixed_expiry]
        self.device_bootstrap_reserve_mock.return_value = fixed_expiry
        first = self.client.get("/api/v1/subscription/status", headers=headers)
        second = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["entitlement_expires_at"], fixed_expiry)
        self.assertEqual(second.json()["entitlement_expires_at"], fixed_expiry)
        self.device_bootstrap_reserve_mock.assert_awaited_once()

    def test_panel_confirmed_hwid_promotes_bootstrap_to_regular_entitlement(self):
        self.device_state["devices"] = []
        fixed_expiry = int(api_router.time.time()) + 600
        self.device_bootstrap_reserve_mock.return_value = fixed_expiry
        headers = {"X-HWID": self.hwid, "X-Entitlement-Version": "es256-v1"}
        first = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertTrue(first.json()["entitlement_bootstrap"])
        self.device_state["devices"] = [{
            "fingerprint": hashlib.sha256(self.hwid.encode()).hexdigest()[:16],
        }]
        confirmed = self.client.get("/api/v1/subscription/status", headers=headers)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        body = confirmed.json()
        self.assertFalse(body.get("entitlement_bootstrap", False))
        self.assertNotIn("entitlement_expires_at", body)
        self.assertIsNotNone(verify_entitlement(
            body["entitlement"], self.public_key, expected_user_id=7654321,
            expected_hwid=self.hwid,
        ))
        self.device_bootstrap_delete_mock.assert_awaited()


if __name__ == "__main__":
    unittest.main()
