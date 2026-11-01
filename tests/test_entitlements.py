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
        self.subscription_patch.start()
        self.devices_patch.start()
        self.env_patch = patch.dict(os.environ, {PRIVATE_KEY_FILE_ENV: str(self.key_path)})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.devices_patch.stop()
        self.subscription_patch.stop()
        self.temp.cleanup()

    def test_http_status_contains_signed_entitlement_for_authenticated_registered_device(self):
        response = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid},
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

    def test_http_status_refuses_an_unregistered_or_missing_device(self):
        missing = self.client.get("/api/v1/subscription/status")
        self.assertEqual(missing.status_code, 422)
        self.device_state["devices"] = []
        unregistered = self.client.get(
            "/api/v1/subscription/status",
            headers={"X-HWID": self.hwid},
        )
        self.assertEqual(unregistered.status_code, 403)

    def test_http_status_fails_closed_without_signing_configuration(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PRIVATE_KEY_FILE_ENV, None)
            response = self.client.get(
                "/api/v1/subscription/status",
                headers={"X-HWID": self.hwid},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("entitlement", response.json())

    def test_trial_without_signer_fails_before_consuming_trial(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": "/api/v1/subscription/trial",
            "raw_path": b"/api/v1/subscription/trial", "query_string": b"",
            "headers": [(b"x-hwid", self.hwid.encode())],
            "client": ("127.0.0.1", 1), "server": ("test.invalid", 443),
        })
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PRIVATE_KEY_FILE_ENV, None)
            with patch.object(api_router.database, "has_trial_used", new=AsyncMock()) as trial_check:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(api_router.activate_trial(
                        api_router.TrialRequest(), request, user_id=7654321,
                    ))
        self.assertEqual(raised.exception.status_code, 503)
        trial_check.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
