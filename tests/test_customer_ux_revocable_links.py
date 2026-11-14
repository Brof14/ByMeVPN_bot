"""Regression coverage for customer-facing signed links and billing choices."""
import os
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app_links import build_connect_page_url, verify_permanent_connect_token
from handlers.buy import _checkout_confirmation_text
from keyboards import payment_kb


class PermanentLinkTests(unittest.TestCase):
    def setUp(self):
        from webhook import app
        self.client = TestClient(app)
        self.config = patch.dict(os.environ, {"APP_HANDOFF_SECRET": "customer-link-test-secret"})
        self.origin = patch("app_links.APP_CONNECT_PAGE_URL", "https://connect.example")
        self.config.start()
        self.origin.start()
        # Legacy migration tests do not initialize SQLite. They simulate a
        # pre-migration account whose v2 URL remains readable until rotation.
        self.link_state = patch("database.get_connect_link_state", new=AsyncMock(return_value=None))
        self.link_state.start()

    def tearDown(self):
        self.link_state.stop()
        self.origin.stop()
        self.config.stop()

    def test_signature_is_bound_to_key_and_revoked_on_reissue(self):
        initial = "https://vpn.example/sub/old-secret"
        rotated = "https://vpn.example/sub/new-secret"
        link = build_connect_page_url(initial, user_id=101, key_id=77)
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(query["k"], ["77"])
        self.assertEqual(query["u"], ["101"])
        self.assertNotIn("old-secret", link)
        signature = query["s"][0]
        self.assertTrue(verify_permanent_connect_token(101, signature, initial, key_id=77))
        self.assertFalse(verify_permanent_connect_token(101, signature, rotated, key_id=77))
        self.assertFalse(verify_permanent_connect_token(101, signature, initial, key_id=78))
        self.assertFalse(verify_permanent_connect_token(102, signature, initial, key_id=77))

    def test_rotated_epoch_rejects_old_link_even_when_subscription_url_is_unchanged(self):
        current = "https://vpn.example/sub/still-the-same"
        first_seed, second_seed = "a" * 64, "b" * 64
        first_url = build_connect_page_url(current, user_id=101, key_id=77, link_seed=first_seed)
        params = {key: values[0] for key, values in parse_qs(urlsplit(first_url).query).items()}
        active = [{"id": 77, "user_id": 101, "key": current, "expiry": int(time.time()) + 86400}]
        with (
            patch("database.get_connect_link_state", new=AsyncMock(return_value={
                "seed": first_seed, "legacy_revoked": False,
            })),
            patch("database.get_user_keys", new=AsyncMock(return_value=active)),
        ):
            opened = self.client.get("/connect", params=params)
        self.assertEqual(opened.status_code, 200)

        with (
            patch("database.get_connect_link_state", new=AsyncMock(return_value={
                "seed": second_seed, "legacy_revoked": True,
            })),
            patch("database.get_user_keys", new=AsyncMock(return_value=active)),
        ):
            revoked = self.client.get("/connect", params=params)
        self.assertEqual(revoked.status_code, 400)
        self.assertNotIn(current, revoked.text)

    def test_legacy_link_stops_working_after_first_successful_reissue(self):
        current = "https://vpn.example/sub/key"
        legacy = build_connect_page_url(current, user_id=101, key_id=77)
        params = {key: values[0] for key, values in parse_qs(urlsplit(legacy).query).items()}
        with (
            patch("database.get_connect_link_state", new=AsyncMock(return_value={
                "seed": "c" * 64, "legacy_revoked": True,
            })),
            patch("database.get_user_keys", new=AsyncMock(return_value=[{
                "id": 77, "user_id": 101, "key": current, "expiry": int(time.time()) + 3600,
            }])),
        ):
            response = self.client.get("/connect", params=params)
        self.assertEqual(response.status_code, 400)

    def test_page_resolves_selected_key_and_rejects_rotated_credentials(self):
        from database import get_user_keys
        initial = "https://vpn.example/sub/old-secret"
        first = {"id": 77, "user_id": 101, "key": initial, "expiry": int(time.time()) + 86400}
        second = {"id": 78, "user_id": 101, "key": "https://vpn.example/sub/other-secret",
                  "expiry": int(time.time()) + 86400}
        url = build_connect_page_url(initial, user_id=101, key_id=77)
        query = parse_qs(urlsplit(url).query)
        params = {key: value[0] for key, value in query.items()}
        with patch("database.get_user_keys", new=AsyncMock(return_value=[first, second])):
            opened = self.client.get("/connect", params=params)
        self.assertEqual(opened.status_code, 200)
        self.assertIn(initial, opened.text)
        self.assertNotIn(second["key"], opened.text)

        rotated = {**first, "key": "https://vpn.example/sub/new-secret"}
        with patch("database.get_user_keys", new=AsyncMock(return_value=[rotated, second])):
            revoked = self.client.get("/connect", params=params)
        self.assertEqual(revoked.status_code, 400)
        self.assertNotIn(rotated["key"], revoked.text)
        self.assertIn("Ссылка недоступна", revoked.text)

    def test_server_renewal_overrides_stale_local_expiry_only_for_same_key(self):
        current = "https://vpn.example/sub/same-credential"
        expiry_local = int(time.time()) - 60
        expiry_panel = int(time.time()) + 86400
        link = build_connect_page_url(current, user_id=101, key_id=77)
        params = {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}
        local = [{"id": 77, "user_id": 101, "key": current, "expiry": expiry_local}]
        panel = {
            "subscription_url": current, "expiry_time": expiry_panel * 1000, "enable": True,
        }
        with (
            patch("database.get_user_keys", new=AsyncMock(return_value=local)),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=panel)),
        ):
            restored = self.client.get("/connect", params=params)
        self.assertEqual(restored.status_code, 200)
        self.assertIn(current, restored.text)

        with (
            patch("database.get_user_keys", new=AsyncMock(return_value=local)),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                **panel, "subscription_url": "https://vpn.example/sub/new-credential",
            })),
        ):
            revoked = self.client.get("/connect", params=params)
        self.assertEqual(revoked.status_code, 400)
        self.assertIn("Подписка закончилась", revoked.text)
        self.assertNotIn("same-credential", revoked.text)

    def test_panel_only_handoff_survives_stale_expired_database_row(self):
        panel_url = "https://vpn.example/sub/server-only"
        expiry = int(time.time()) + 86400
        url = build_connect_page_url(panel_url, user_id=101)
        params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        with (
            patch("database.get_user_keys", new=AsyncMock(return_value=[{
                "id": 23, "user_id": 101,
                "key": "https://vpn.example/sub/stale",
                "expiry": int(time.time()) - 86400,
            }])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "subscription_url": panel_url,
                "expiry_time": expiry * 1000, "enable": True,
            })),
        ):
            page = self.client.get("/connect", params=params)
        self.assertEqual(page.status_code, 200)
        self.assertIn(panel_url, page.text)

    def test_malformed_signature_rejects_before_database_and_panel_reads(self):
        with (
            patch("database.get_user_keys", new=AsyncMock()) as db,
            patch("xui_client.get_xui_client_details", new=AsyncMock()) as panel,
        ):
            page = self.client.get("/connect", params={"u": 101, "k": 1, "s": "invalid"})
        self.assertEqual(page.status_code, 400)
        db.assert_not_awaited()
        panel.assert_not_awaited()

    def test_server_only_access_is_still_verified(self):
        import webhook
        from xui_client import get_xui_client_details
        current = "https://vpn.example/sub/panel-only"
        url = build_connect_page_url(current, user_id=101)
        query = parse_qs(urlsplit(url).query)
        params = {key: value[0] for key, value in query.items()}
        with (
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "subscription_url": current,
                "expiry_time": (int(time.time()) + 3600) * 1000,
                "enable": True,
            })),
        ):
            response = self.client.get("/connect", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertIn(current, response.text)


class CheckoutPreferenceTests(unittest.IsolatedAsyncioTestCase):
    def _checkout(self, enabled):
        return {
            "months": 1, "days": 30, "devices": 2, "price_rub": 89,
            "autorenew_enabled": enabled,
        }

    def test_yookassa_preference_changes_visible_copy(self):
        self.assertIn("Автопродление ЮKassa: включено", _checkout_confirmation_text(self._checkout(True)))
        self.assertIn("Автопродление ЮKassa: выключено", _checkout_confirmation_text(self._checkout(False)))
        self.assertIn(
            "checkout_autorenew_toggle",
            [button.callback_data for row in payment_kb(89, 30).inline_keyboard for button in row],
        )

    async def test_switcher_preserves_quote_and_updates_state(self):
        from handlers.buy import cb_checkout_autorenew_toggle
        data = self._checkout(True)
        state = SimpleNamespace(get_data=AsyncMock(return_value=data), update_data=AsyncMock())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_checkout_autorenew_toggle(callback, object(), state)
        state.update_data.assert_awaited_once_with(autorenew_enabled=False)
        self.assertIn("Автопродление ЮKassa: выключено", send.await_args.args[2])
        self.assertIn("К оплате сейчас: <b>89 ₽</b>", send.await_args.args[2])
