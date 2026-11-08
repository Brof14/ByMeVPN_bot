"""Panel-confirmed subscription rotation with no production credentials."""
import time
import unittest
from unittest.mock import AsyncMock, patch

import subscription_reissue


OLD_UUID = "c59d8437-9f39-4ca1-90c2-5fbc250470e9"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Panel:
    def __init__(self, *, failed_updates=0):
        self.client = self
        self.failed_updates = failed_updates
        self.payload = {
            "id": 19,
            "email": "123",
            "uuid": OLD_UUID,
            "subId": "oldsubscriptionid",
            "enable": True,
            "expiryTime": int(time.time() * 1000) + 86400_000,
            "limitIp": 0,
            "limitHwid": 2,
            "totalGB": 0,
            "allowedIPs": "10.0.0.2/32, 2001:db8::2/128",
            "up": 42,
            "down": 73,
        }
        self.updates = 0

    def _url(self, path):
        return path

    async def _get(self, path, headers):
        return _Response({"success": True, "obj": {"client": dict(self.payload)}})

    async def _post(self, path, headers, payload):
        self.updates += 1
        self.payload = dict(payload)
        if self.failed_updates:
            self.failed_updates -= 1
            return _Response({"success": False, "obj": None})
        return _Response({"success": True, "obj": None})


class _API:
    def __init__(self, panel):
        self.client = panel


class SubscriptionReissueTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_rotates_id_and_sub_id_while_preserving_access_controls(self):
        panel = _Panel()
        api = _API(panel)
        with (
            patch("subscription_reissue._get_api", new=AsyncMock(return_value=api)),
            patch("subscription_reissue.update_user_key_credentials", new=AsyncMock(return_value=True)) as save,
            patch("subscription_reissue._build_sub_url", side_effect=lambda sub_id: f"https://vpn.example/sub/{sub_id}"),
        ):
            result = await subscription_reissue.reissue_user_subscription(123, expected_uuid=OLD_UUID)

        self.assertEqual(result, {"updated": True})
        self.assertNotEqual(panel.payload["uuid"], OLD_UUID)
        self.assertEqual(panel.payload["subId"], panel.payload["uuid"].replace("-", "")[:16])
        self.assertEqual(panel.payload["limitIp"], 0)
        self.assertEqual(panel.payload["limitHwid"], 2)
        self.assertEqual(panel.payload["allowedIPs"], ["10.0.0.2/32", "2001:db8::2/128"])
        self.assertTrue(panel.payload["enable"])
        self.assertGreater(panel.payload["expiryTime"], int(time.time() * 1000))
        save.assert_awaited_once()
        self.assertEqual(save.await_args.args[0], 123)
        self.assertEqual(save.await_args.args[1], panel.payload["uuid"])
        self.assertTrue(save.await_args.args[2].startswith("https://"))

    async def test_database_failure_restores_previous_panel_credentials(self):
        panel = _Panel()
        api = _API(panel)
        with (
            patch("subscription_reissue._get_api", new=AsyncMock(return_value=api)),
            patch("subscription_reissue.update_user_key_credentials", new=AsyncMock(return_value=False)),
        ):
            result = await subscription_reissue.reissue_user_subscription(123, expected_uuid=OLD_UUID)

        self.assertIsNone(result)
        self.assertEqual(panel.payload["uuid"], OLD_UUID)
        self.assertEqual(panel.payload["subId"], "oldsubscriptionid")
        self.assertEqual(panel.updates, 2)

    async def test_unexpected_panel_rejection_is_compensated_and_not_saved(self):
        panel = _Panel(failed_updates=3)
        api = _API(panel)
        with (
            patch("subscription_reissue._get_api", new=AsyncMock(return_value=api)),
            patch("subscription_reissue.update_user_key_credentials", new=AsyncMock()) as save,
            patch("subscription_reissue._build_sub_url", side_effect=lambda sub_id: f"https://vpn.example/sub/{sub_id}"),
        ):
            result = await subscription_reissue.reissue_user_subscription(123, expected_uuid=OLD_UUID)

        self.assertIsNone(result)
        self.assertEqual(panel.payload["uuid"], OLD_UUID)
        self.assertEqual(panel.updates, 4)
        save.assert_not_awaited()

    async def test_old_confirmation_cannot_rotate_a_newer_subscription(self):
        panel = _Panel()
        panel.payload["uuid"] = "2de6b4ba-6d4f-4a3a-9f9a-ff66ac328d15"
        panel.payload["id"] = 20
        panel.payload["subId"] = "newsubscriptionid"
        api = _API(panel)
        with (
            patch("subscription_reissue._get_api", new=AsyncMock(return_value=api)),
            patch("subscription_reissue.update_user_key_credentials", new=AsyncMock()) as save,
        ):
            result = await subscription_reissue.reissue_user_subscription(123, expected_uuid=OLD_UUID)

        self.assertIsNone(result)
        self.assertEqual(panel.updates, 0)
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
