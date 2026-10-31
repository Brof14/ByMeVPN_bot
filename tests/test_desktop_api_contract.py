"""Desktop server contract regressions: auto-node, identity and idempotent device count."""
import os
import secrets
import sys
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_desktop_contract.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router


def request(os_name="Windows"):
    return Request({
        "type": "http", "method": "POST", "http_version": "1.1",
        "path": "/api/v1/vpn/session", "raw_path": b"/api/v1/vpn/session",
        "scheme": "https", "query_string": b"",
        "headers": [(b"x-device-os", os_name.encode())],
        "client": ("127.0.0.1", 1234), "server": ("test.invalid", 443),
    })


HY2 = {"node_code": "node-hy2", "config": {
    "protocol": "hysteria2", "security": "tls", "network": "udp",
    "server": "198.51.100.1", "uuid": "password"
}}
VLESS = {"node_code": "node-vless", "config": {
    "protocol": "vless", "security": "reality", "network": "tcp",
    "server": "vpn.example.invalid", "public_key": "test-key",
    "uuid": "11111111-1111-4111-8111-111111111111"
}}


class DesktopSessionContract(unittest.IsolatedAsyncioTestCase):
    async def test_auto_skips_unsupported_hysteria_on_windows(self):
        with patch.object(api_router, "get_user_real_subscription",
                          new=AsyncMock(return_value=("url", [HY2, VLESS], {}))):
            actual = await api_router.create_vpn_session(
                api_router.SessionRequest(node_code="auto"), request(), user_id=123
            )
        self.assertEqual(actual["server"], "vpn.example.invalid")

    async def test_android_auto_keeps_backward_compatible_choice(self):
        with patch.object(api_router, "get_user_real_subscription",
                          new=AsyncMock(return_value=("url", [HY2, VLESS], {}))):
            actual = await api_router.create_vpn_session(
                api_router.SessionRequest(node_code="auto"), request("Android"), user_id=123
            )
        self.assertEqual(actual["protocol"], "hysteria2")

    async def test_unknown_code_is_not_silently_turned_into_other_server(self):
        with patch.object(api_router, "get_user_real_subscription",
                          new=AsyncMock(return_value=("url", [HY2, VLESS], {}))):
            with self.assertRaises(HTTPException) as caught:
                await api_router.create_vpn_session(
                    api_router.SessionRequest(node_code="absent"), request(), user_id=123
                )
        self.assertEqual(caught.exception.status_code, 404)

    async def test_explicit_unsupported_protocol_is_rejected(self):
        with patch.object(api_router, "get_user_real_subscription",
                          new=AsyncMock(return_value=("url", [HY2, VLESS], {}))):
            with self.assertRaises(HTTPException) as caught:
                await api_router.create_vpn_session(
                    api_router.SessionRequest(node_code="node-hy2"), request(), user_id=123
                )
        self.assertEqual(caught.exception.status_code, 422)

    async def test_same_checkout_id_cannot_change_device_count(self):
        reserved = {"plan_code": "1m", "provider": "yookassa",
                    "payment_id": "existing", "confirmation_url": "https://example.invalid",
                    "generation": 0, "devices": 2}
        with patch.object(api_router.database, "ensure_mobile_payment_request",
                          new=AsyncMock(return_value=reserved)):
            with self.assertRaises(HTTPException) as caught:
                await api_router.api_create_payment(
                    api_router.PaymentCreateRequest(
                        plan_code="1m", request_id="request_12345678",
                        devices=10, provider="yookassa"
                    ), user_id=123
                )
        self.assertEqual(caught.exception.status_code, 409)

    async def test_me_restores_registered_email_not_placeholder(self):
        class Cursor:
            async def fetchone(self):
                return ("example@domain.invalid", "Example")
        class DB:
            async def execute(self, *_args):
                return Cursor()
        with (
            patch.object(api_router.database, "find_user_by_id",
                         new=AsyncMock(return_value={"username": "saved"})),
            patch.object(api_router.database, "get_db",
                         new=AsyncMock(return_value=DB())),
        ):
            profile = await api_router.get_me(user_id=123)
        self.assertEqual(profile["user"]["email"], "example@domain.invalid")
        self.assertEqual(profile["user"]["name"], "Example")


if __name__ == "__main__":
    unittest.main()
