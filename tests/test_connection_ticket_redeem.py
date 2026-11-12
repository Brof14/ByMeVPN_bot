"""No VPN credentials before explicit, single-use connection-page redemption."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app_links
import webhook
from connect_ticket_replay import consume_connect_ticket


class ConnectionTicketStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_redemptions_allow_exactly_one(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("connect_ticket_replay.DB_FILE", str(Path(root) / "tickets.sqlite")):
                results = await asyncio.gather(*(
                    consume_connect_ticket("same-encrypted-ticket") for _ in range(6)
                ))
                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), 5)
                self.assertTrue(await consume_connect_ticket("different-ticket"))

    async def test_invalid_ticket_is_rejected_without_db_access(self):
        with patch("connect_ticket_replay.DB_FILE", "/missing/parent/db.sqlite"):
            self.assertFalse(await consume_connect_ticket(""))
            self.assertFalse(await consume_connect_ticket("x" * 4097))


class ConnectionTicketHttpTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webhook.app)

    def _ticket(self, url):
        with patch("app_links.BOT_TOKEN", "local-test-secret"):
            return app_links.build_connect_ticket(url)

    def test_get_is_preview_safe_and_post_is_one_time(self):
        secret = "https://panel.example/sub/private-user-token"
        with patch("app_links.BOT_TOKEN", "local-test-secret"):
            ticket = app_links.build_connect_ticket(secret)
            preview = self.client.get("/connect", params={"ticket": ticket})
            self.assertEqual(preview.status_code, 200)
            self.assertNotIn(secret, preview.text)
            self.assertIn("Продолжить подключение", preview.text)
            self.assertIn("no-store", preview.headers["cache-control"])
            with patch("connect_ticket_replay.consume_connect_ticket", new=AsyncMock(side_effect=[True, False])):
                unlocked = self.client.post("/connect/redeem", json={"ticket": ticket})
                reused = self.client.post("/connect/redeem", json={"ticket": ticket})
            self.assertEqual(unlocked.status_code, 200)
            self.assertIn(secret, unlocked.text)
            self.assertEqual(reused.status_code, 410)
            self.assertNotIn(secret, reused.text)

    def test_tampered_redeem_does_not_consume_anything(self):
        with patch("connect_ticket_replay.consume_connect_ticket", new=AsyncMock()) as consume:
            response = self.client.post("/connect/redeem", json={"ticket": "tampered"})
            self.assertEqual(response.status_code, 400)
            consume.assert_not_awaited()
