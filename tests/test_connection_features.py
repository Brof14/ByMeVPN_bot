"""Tests for device visibility and first-connection reminders."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_DB_PATH = "/tmp/test_connection_features.db"
os.environ["DB_FILE"] = TEST_DB_PATH

import database
import notifications
import xui_client


def _clean_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(TEST_DB_PATH + suffix)
        except FileNotFoundError:
            pass


class TestConnectionReminderDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()
        await database.ensure_user(753159)

    async def asyncTearDown(self):
        await database.close_db()
        _clean_db()

    async def test_scheduled_reminder_is_due_once_and_terminal_state_is_sticky(self):
        key_id = await database.add_key(753159, "sub", "test", "uuid", 30, 2)
        self.assertTrue(await database.schedule_connection_reminder(key_id, 12, delay_seconds=0))
        self.assertFalse(await database.schedule_connection_reminder(key_id, 99, delay_seconds=0))

        due = await database.get_due_connection_reminders()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["traffic_baseline"], 12)
        self.assertEqual(due[0]["user_id"], 753159)

        await database.update_connection_reminder(key_id, "connected")
        self.assertEqual(await database.get_due_connection_reminders(), [])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestXuiConnectionInfo(unittest.IsolatedAsyncioTestCase):
    async def test_parses_ip_timestamp_and_profile_online_status(self):
        calls = []

        class FakeClient:
            def _url(self, path):
                return path

            async def _post(self, url, headers, data):
                calls.append(url)
                if "/ips/753159" in url:
                    return _FakeResponse({
                        "success": True,
                        "obj": ["203.0.113.8 (1700000000)", "not-an-ip (1700000010)"],
                    })
                return _FakeResponse({"success": True, "obj": ["753159", "42"]})

        fake_api = SimpleNamespace(client=FakeClient())

        async def passthrough(method, *args, **kwargs):
            return await method(*args, **kwargs)

        with (
            patch("xui_client._get_api", new=AsyncMock(return_value=fake_api)),
            patch("xui_client._api_call_with_retry", new=passthrough),
        ):
            result = await xui_client.get_xui_connection_info(753159)

        self.assertTrue(result["is_online"])
        self.assertEqual(result["ips"], [{"ip": "203.0.113.8", "last_seen": 1700000000}])
        self.assertEqual(len(calls), 2)

    async def test_clear_ips_uses_profile_scoped_panel_route(self):
        fake_client = SimpleNamespace(
            _url=lambda path: path,
            _post=AsyncMock(return_value=_FakeResponse({"success": True})),
        )
        fake_api = SimpleNamespace(client=fake_client)

        async def passthrough(method, *args, **kwargs):
            return await method(*args, **kwargs)

        with (
            patch("xui_client._get_api", new=AsyncMock(return_value=fake_api)),
            patch("xui_client._api_call_with_retry", new=passthrough),
        ):
            self.assertTrue(await xui_client.clear_xui_client_ips(753159))

        self.assertIn("clearIps/753159", fake_client._post.await_args.args[0])


class TestConnectionReminderWorker(unittest.IsolatedAsyncioTestCase):
    def _item(self):
        return {
            "key_id": 17, "user_id": 753159, "created": 1,
            "expiry": 4_000_000_000, "traffic_baseline": 0, "attempts": 0,
        }

    async def test_traffic_seen_suppresses_message(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.get_due_connection_reminders", new=AsyncMock(return_value=[self._item()])),
            patch("notifications.update_connection_reminder", new=AsyncMock()) as update,
            patch("xui_client.get_xui_user", new=AsyncMock(return_value={"used_traffic": 5})),
            patch("database.log_analytics_event_once", new=AsyncMock()) as log_event,
        ):
            await notifications._connection_reminder_pass(bot)

        bot.send_message.assert_not_awaited()
        update.assert_awaited_once_with(17, "connected")
        log_event.assert_awaited_once_with(753159, "first_connection", "3x-ui traffic observed")

    async def test_no_traffic_sends_one_actionable_message(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.get_due_connection_reminders", new=AsyncMock(return_value=[self._item()])),
            patch("notifications.update_connection_reminder", new=AsyncMock()) as update,
            patch("xui_client.get_xui_user", new=AsyncMock(return_value={"used_traffic": 0})),
            patch("xui_client.get_xui_connection_info", new=AsyncMock(return_value={"is_online": False, "ips": []})),
        ):
            await notifications._connection_reminder_pass(bot)

        bot.send_message.assert_awaited_once()
        self.assertIn("key_instructions:17", str(bot.send_message.await_args.kwargs["reply_markup"]))
        update.assert_awaited_once_with(17, "sent")

    async def test_panel_unavailable_defers_without_sending(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.get_due_connection_reminders", new=AsyncMock(return_value=[self._item()])),
            patch("notifications.update_connection_reminder", new=AsyncMock()) as update,
            patch("xui_client.get_xui_user", new=AsyncMock(return_value=None)),
        ):
            await notifications._connection_reminder_pass(bot)

        bot.send_message.assert_not_awaited()
        update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
