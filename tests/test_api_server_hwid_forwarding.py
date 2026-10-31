"""Regression tests for preserving HWID metadata during server delivery."""
import base64
import hashlib
import os
import secrets
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_api_server_hwid_forwarding.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

from starlette.requests import Request
import api_router


def _request(headers):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/servers",
        "raw_path": b"/api/v1/servers",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("test.invalid", 443),
    })


class TestSubscriptionHwidForwarding(unittest.IsolatedAsyncioTestCase):
    def test_request_helper_only_forwards_allowed_device_metadata(self):
        request = _request({
            "X-HWID": "test-installation",
            "X-Device-OS": "Android",
            "X-Ver-OS": "test-version",
            "X-Device-Model": "test-model",
            "User-Agent": "ByMeVPN-test",
            "Authorization": "Bearer must-not-forward",
            "Cookie": "must-not-forward",
            "X-Other": "must-not-forward",
        })

        self.assertEqual(
            api_router._subscription_headers_from_request(request),
            {
                "X-HWID": "test-installation",
                "X-Device-OS": "Android",
                "X-Ver-OS": "test-version",
                "X-Device-Model": "test-model",
                "User-Agent": "ByMeVPN-test",
            },
        )

    async def test_subscription_fetch_preserves_https_host_and_hwid(self):
        calls = []
        node = (
            "vless://test-user@example.invalid:443?type=tcp&security=reality"
            "&pbk=test-key&sid=test-short-id#Test%20node"
        )
        encoded = base64.b64encode(node.encode()).decode()

        class Response:
            status_code = 200
            text = encoded

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                calls.append((url, dict(headers or {})))
                return Response()

        headers = {
            "X-HWID": "test-installation",
            "X-Device-OS": "Android",
            "Authorization": "Bearer must-not-forward",
            "Cookie": "must-not-forward",
            "Host": "attacker.invalid",
        }
        with patch.object(api_router.httpx, "AsyncClient", return_value=Client()):
            result = await api_router.fetch_and_parse_real_nodes(
                "https://subscription.example.invalid:2096/yay/test-only",
                request_headers=headers,
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            calls[0][0],
            "https://127.0.0.1:2096/yay/test-only",
        )
        self.assertEqual(calls[0][1]["Host"], "subscription.example.invalid:2096")
        self.assertEqual(calls[0][1]["X-HWID"], "test-installation")
        self.assertEqual(calls[0][1]["X-Device-OS"], "Android")
        self.assertNotIn("Authorization", calls[0][1])
        self.assertNotIn("Cookie", calls[0][1])

    async def test_servers_endpoint_passes_only_whitelisted_headers(self):
        request = _request({
            "X-HWID": "test-installation",
            "X-Device-OS": "Android",
            "Authorization": "Bearer must-not-forward",
        })
        fetch = AsyncMock(return_value=("unused", [{"node_code": "test-node"}], {}))
        with patch.object(api_router, "get_user_real_subscription", new=fetch):
            response = await api_router.get_servers(request, user_id=123)

        self.assertEqual(len(response["servers"]), 1)
        self.assertEqual(fetch.await_args.args[0], 123)
        self.assertEqual(
            fetch.await_args.args[1],
            {"X-HWID": "test-installation", "X-Device-OS": "Android"},
        )

    async def test_device_list_uses_panel_records_and_never_returns_fingerprint(self):
        request = _request({
            "X-HWID": "install-current",
            "X-Device-Model": "Phone X",
            "X-Device-OS": "Android",
            "X-Ver-OS": "15",
            "User-Agent": "ByMeVPN-test",
        })
        panel = {
            "devices": [{
                "id": 42,
                "model": "Phone X",
                "os": "Android",
                "os_version": "15",
                "user_agent": "ByMeVPN-test",
                "last_seen": int(api_router.time.time()),
                "fingerprint": hashlib.sha256(b"install-current").hexdigest()[:12],
            }],
            "limit_hwid": 2,
            "limit_ip": 0,
        }
        with patch("xui_client.get_xui_devices", new=AsyncMock(return_value=panel)):
            result = await api_router.get_devices(request, user_id=123)

        self.assertEqual(result["devices_used"], 1)
        self.assertEqual(result["device_limit"], 2)
        self.assertTrue(result["devices"][0]["is_current"])
        self.assertNotIn("fingerprint", result["devices"][0])
        self.assertNotIn("install_id", result["devices"][0])

    async def test_device_delete_checks_owner_scoped_panel_list(self):
        panel = {"devices": [{"id": 42}], "limit_hwid": 2, "limit_ip": 0}
        with (
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value=panel)) as get_devices,
            patch("xui_client.delete_xui_hwid_device", new=AsyncMock(return_value=True)) as delete_device,
        ):
            result = await api_router.delete_device("42", user_id=123)

        self.assertEqual(result, {"status": "ok"})
        get_devices.assert_awaited_once_with(123)
        delete_device.assert_awaited_once_with(123, 42)

    async def test_device_delete_rejects_unknown_device_and_bad_id(self):
        from fastapi import HTTPException

        with patch("xui_client.get_xui_devices", new=AsyncMock(return_value={"devices": []})):
            with self.assertRaises(HTTPException) as missing:
                await api_router.delete_device("42", user_id=123)
        self.assertEqual(missing.exception.status_code, 404)

        with self.assertRaises(HTTPException) as malformed:
            await api_router.delete_device("../42", user_id=123)
        self.assertEqual(malformed.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
