"""Regression tests for 3x-ui API pool authentication recovery."""
import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_xui_client_pool.db")

import xui_client
import device_monitor


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeApi:
    def __init__(self, login_error=None):
        self.login_error = login_error
        self.login_calls = 0
        self.session = _FakeSession()

    async def login(self):
        self.login_calls += 1
        if self.login_error:
            raise self.login_error


class TestXuiClientPool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        xui_client._api_pool = None

    def tearDown(self):
        xui_client._api_pool = None

    def test_inbound_labels_use_live_remarks_and_verified_fallbacks(self):
        self.assertEqual(xui_client._inbound_name(12), "🇩🇪Germany-1")
        self.assertEqual(
            xui_client._inbound_name(12, SimpleNamespace(remark="🇩🇪Germany-1 live")),
            "🇩🇪Germany-1 live",
        )
        self.assertEqual(xui_client._inbound_name(999), "ByMeVPN · 999")

    def test_manual_fallback_links_are_generated_only_for_vless(self):
        self.assertTrue(xui_client._is_vless_inbound(SimpleNamespace(protocol="vless")))
        self.assertFalse(xui_client._is_vless_inbound(SimpleNamespace(protocol="hysteria")))

    async def test_failed_login_is_not_published_and_next_login_recovers(self):
        failed = _FakeApi(RuntimeError("temporary login failure"))
        recovered = _FakeApi()

        with (
            patch("xui_client.AsyncApi", side_effect=[failed, recovered]),
            patch("xui_client.asyncio.sleep", new=AsyncMock()),
        ):
            api = await xui_client._get_api()

        self.assertIs(api, recovered)
        self.assertIs(xui_client._api_pool, recovered)
        self.assertEqual(failed.login_calls, 1)
        self.assertEqual(recovered.login_calls, 1)
        self.assertTrue(failed.session.closed)

    async def test_authentication_error_invalidates_cached_pool(self):
        cached = _FakeApi()
        xui_client._api_pool = cached

        async def unauthorized():
            raise RuntimeError("HTTP 401 Unauthorized")

        with self.assertRaisesRegex(RuntimeError, "401 Unauthorized"):
            await xui_client._api_call_with_retry(unauthorized)

        self.assertIsNone(xui_client._api_pool)

    async def test_existing_client_update_failure_never_falls_through_to_add(self):
        """A found UUID must not be re-added when safe update fails."""
        api = object()
        with (
            patch("xui_client._get_api", new=AsyncMock(return_value=api)),
            patch("xui_client._find_client_uuid", new=AsyncMock(return_value="stable-uuid")),
            patch("xui_client._find_client_sub_id", new=AsyncMock(return_value="stable-sub-id")),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value=None)),
            patch("xui_client._api_call_with_retry", new=AsyncMock()) as api_call,
        ):
            result = await xui_client.create_xui_user(12345, days=30, limit_ip=5)

        self.assertIsNone(result)
        api_call.assert_not_awaited()

    async def test_device_limit_update_changes_hwid_and_keeps_ip_unlimited(self):
        state = {"client": {"email": "123", "limitIp": 2, "limitHwid": 0, "totalGB": 0, "custom": "preserve"}}

        class Response:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self.data

        class ClientApi:
            def _url(self, path):
                return path

            async def _get(self, url, headers):
                return Response({"success": True, "obj": {"client": dict(state["client"])}})

            async def _post(self, url, headers, payload):
                state["client"] = dict(payload)
                return Response({"success": True})

        api = SimpleNamespace(client=ClientApi())
        with patch("xui_client._get_api", new=AsyncMock(return_value=api)):
            result = await xui_client.set_xui_client_limits(123, limit_ip=0, limit_hwid=5)

        self.assertTrue(result)
        self.assertEqual(state["client"]["limitIp"], 0)
        self.assertEqual(state["client"]["limitHwid"], 5)
        self.assertEqual(state["client"]["custom"], "preserve")

    async def test_client_lookup_fallback_scans_configured_inbounds(self):
        client = SimpleNamespace(get_by_email=AsyncMock(return_value=None))
        inbound = SimpleNamespace(
            get_by_id=AsyncMock(side_effect=[
                SimpleNamespace(client_stats=[]),
                SimpleNamespace(client_stats=[SimpleNamespace(email="123", uuid="stable-uuid")]),
            ])
        )
        api = SimpleNamespace(client=client, inbound=inbound)

        async def passthrough(method, *args, **kwargs):
            return await method(*args, **kwargs)

        with (
            patch("xui_client.XUI_INBOUND_IDS", [12, 15]),
            patch("xui_client._api_call_with_retry", new=passthrough),
        ):
            found = await xui_client._find_client_uuid(api, "123")

        self.assertEqual(found, "stable-uuid")
        self.assertEqual(
            [call.args[0] for call in inbound.get_by_id.await_args_list],
            [12, 15],
        )

    async def test_hwid_delete_is_verified_and_stale_button_is_idempotent(self):
        device = {"id": 42}
        listed = [
            {"devices": [device], "limit_hwid": 2, "limit_ip": 0},
            {"devices": [], "limit_hwid": 2, "limit_ip": 0},
        ]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True}

        client = SimpleNamespace(
            _url=lambda path: path,
            cookies={"3x-ui": "session-cookie"},
            use_tls_verify=True,
            custom_certificate_path="/tmp/test-ca.pem",
            _generate_headers=lambda headers: {**headers, "X-CSRF-Token": "test-csrf"},
        )
        api = SimpleNamespace(client=client)

        class HttpClient:
            def __init__(self, **kwargs):
                self.options = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def delete(self, url, *, headers):
                self.url = url
                self.headers = headers
                return Response()

        async def passthrough(func, *args, **kwargs):
            return await func(*args, **kwargs)

        with (
            patch("xui_client.get_xui_devices", new=AsyncMock(side_effect=listed)) as get_devices,
            patch("xui_client._get_api", new=AsyncMock(return_value=api)),
            patch("xui_client._api_call_with_retry", new=passthrough),
            patch("xui_client.httpx.AsyncClient", HttpClient),
        ):
            self.assertTrue(await xui_client.delete_xui_hwid_device(123, 42))

        self.assertEqual(get_devices.await_count, 2)
        self.assertEqual(client.cookies, {"3x-ui": "session-cookie"})

        with (
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "devices": [], "limit_hwid": 2, "limit_ip": 0,
            })),
            patch("xui_client._get_api", new=AsyncMock()) as get_api,
        ):
            self.assertTrue(await xui_client.delete_xui_hwid_device(123, 42))
        get_api.assert_not_awaited()

    async def test_hwid_delete_returns_false_when_panel_keeps_device(self):
        client = SimpleNamespace(
            _url=lambda path: path,
            cookies={}, use_tls_verify=True, custom_certificate_path=None,
            _generate_headers=lambda headers: headers,
        )
        api = SimpleNamespace(client=client)

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True}

        class HttpClient:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
            async def delete(self, url, *, headers): return Response()

        async def passthrough(func, *args, **kwargs):
            return await func(*args, **kwargs)

        with (
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "devices": [{"id": 42}], "limit_hwid": 2, "limit_ip": 0,
            })),
            patch("xui_client._get_api", new=AsyncMock(return_value=api)),
            patch("xui_client._api_call_with_retry", new=passthrough),
            patch("xui_client.asyncio.sleep", new=AsyncMock()),
            patch("xui_client.httpx.AsyncClient", HttpClient),
        ):
            self.assertFalse(await xui_client.delete_xui_hwid_device(123, 42))

    async def test_hwid_delete_sends_authenticated_delete_with_panel_tls_settings(self):
        device = {"id": 42}
        class Response:
            def raise_for_status(self): return None
            def json(self): return {"success": True}
        class HttpClient:
            instances = []
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.__class__.instances.append(self)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
            async def delete(self, url, *, headers):
                self.url, self.headers = url, headers
                return Response()
        client_api = SimpleNamespace(
            _url=lambda path: f"https://panel.invalid/{path}",
            cookies={"session": "cookie"}, use_tls_verify=True,
            custom_certificate_path="/etc/ssl/test-ca.pem",
            _generate_headers=lambda headers: {**headers, "Authorization": "Bearer mock"},
        )
        api = SimpleNamespace(client=client_api)
        async def passthrough(func, *args, **kwargs): return await func(*args, **kwargs)
        with (
            patch("xui_client.get_xui_devices", new=AsyncMock(side_effect=[
                {"devices": [device], "limit_hwid": 2, "limit_ip": 0},
                {"devices": [], "limit_hwid": 2, "limit_ip": 0},
            ])),
            patch("xui_client._get_api", new=AsyncMock(return_value=api)),
            patch("xui_client._api_call_with_retry", new=passthrough),
            patch("xui_client.httpx.AsyncClient", HttpClient),
        ):
            self.assertTrue(await xui_client.delete_xui_hwid_device(123, 42))
        options = HttpClient.instances[-1].kwargs
        self.assertEqual(options["cookies"], {"session": "cookie"})
        self.assertEqual(options["verify"], "/etc/ssl/test-ca.pem")
        self.assertEqual(options["timeout"], 15.0)

    async def test_new_device_notice_explains_full_limit_but_not_unlimited(self):
        user_id = 123
        key = {"id": 7, "expiry": int(time.time()) + 3600}
        device = {"id": 42, "model": "Test phone", "os": "Android", "os_version": "15"}
        bot = SimpleNamespace(send_message=AsyncMock())

        async def run(limit):
            bot.send_message.reset_mock()
            with (
                patch("device_monitor.get_user_keys", new=AsyncMock(return_value=[key])),
                patch("device_monitor.get_xui_devices", new=AsyncMock(return_value={
                    "devices": [device] if limit else [device],
                    "limit_hwid": limit,
                    "limit_ip": 0,
                })),
                patch("device_monitor.remember_hwid_devices", new=AsyncMock(return_value=[device])),
                patch("device_monitor.mark_hwid_device_notified", new=AsyncMock()),
            ):
                await device_monitor._check_user(bot, user_id, asyncio.Semaphore(1))
            return bot.send_message.await_args.args[1]

        self.assertIn("Лимит устройств достигнут: 1 из 1", await run(1))
        self.assertNotIn("Лимит устройств достигнут", await run(0))


if __name__ == "__main__":
    unittest.main()
