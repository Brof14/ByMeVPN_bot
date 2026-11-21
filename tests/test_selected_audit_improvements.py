"""Regression contracts for the selected no-payment-logic audit fixes."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "audit-test-only-secret-value-that-is-long-enough")

import database
from api_router import parse_single_node_line, get_user_real_subscription, _correlated_http_error
from business_metrics import economic_overview
from growth_metrics import referral_risk_snapshot
from scripts.verify_backup_restore import rehearse_restore
from server_metadata import detect_server_geo
from webhook import provisioning_health_check
from fastapi import HTTPException

UUID = "11111111-1111-4111-8111-111111111111"


class ExplicitServerMetadataTests(unittest.TestCase):
    def test_no_country_code_substring_collisions(self):
        self.assertEqual(detect_server_geo("Finland Helsinki")[1:3], ("fi", "Хельсинки"))
        self.assertEqual(detect_server_geo("Sweden Stockholm")[1:3], ("se", "Стокгольм"))
        self.assertEqual(detect_server_geo("Netherlands Amsterdam")[1:3], ("nl", "Амстердам"))
        self.assertEqual(detect_server_geo("Germany")[1:3], ("de", ""))
        self.assertEqual(detect_server_geo("ByMeVPN US #1")[1], "us")
        self.assertEqual(detect_server_geo("New Unknown Fast Relay")[1], "")
        self.assertEqual(detect_server_geo("🇫🇮 Helsinki #1")[1:4], ("fi", "Хельсинки", "🇫🇮"))

    def test_real_subscription_transport_has_no_fabricated_metrics(self):
        examples = (
            f"vless://{UUID}@vpn.example.com:443?security=tls#Finland",
            "hysteria2://dummy@vpn.example.com:443?sni=cdn.example.com#Sweden",
        )
        for index, url in enumerate(examples):
            node = parse_single_node_line(url, index)
            self.assertIsNotNone(node)
            self.assertIsNone(node["ping_ms"])
            self.assertIsNone(node["load_percent"])
            self.assertEqual(node["metrics_status"], "not_measured")
            self.assertEqual(node["city"], "")
            self.assertIn(node["country_code"], ("fi", "se"))


class ExpiryAndReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_utc_expiry_additive_legacy_field_is_preserved(self):
        expiry = int(time.time()) + 3000
        user_keys = [{
            "expiry": expiry, "days": 30, "limit_ip": 2,
            "key": "https://vpn.example/sub/test", "uuid": UUID, "remark": "paid",
        }]
        with (
            patch("api_router.database.get_user_keys", new=AsyncMock(return_value=user_keys)),
            patch("api_router.fetch_and_parse_real_nodes", new=AsyncMock(return_value=[{"node_code": "test"}])),
        ):
            _, nodes, sub = await get_user_real_subscription(123)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(sub["expires_at_unix"], expiry)
        self.assertEqual(
            sub["expires_at_utc"],
            datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(timespec="seconds"),
        )
        self.assertIn("expires_at", sub)

    async def test_provisioning_health_is_independent_and_secrets_free(self):
        request = SimpleNamespace(headers={"X-ByMeVPN-Monitor-Token": "x" * 40})
        with patch.dict(os.environ, {"BYMEVPN_MONITOR_TOKEN": "x" * 40}):
            with patch("xui_client.test_xui_connection", new=AsyncMock(return_value=(True, "secret panel detail"))):
                self.assertEqual(
                    await provisioning_health_check(request),
                    {"status": "ready", "provisioning": "ready"},
                )
            with patch("xui_client.test_xui_connection", new=AsyncMock(return_value=(False, "password is wrong"))):
                with self.assertRaises(HTTPException) as error:
                    await provisioning_health_check(request)
            self.assertEqual(error.exception.status_code, 503)
            self.assertNotIn("password", str(error.exception.detail))
        with patch.dict(os.environ, {"BYMEVPN_MONITOR_TOKEN": "x" * 40}):
            with patch("xui_client.test_xui_connection", new=AsyncMock()) as probe:
                with self.assertRaises(HTTPException) as denied:
                    await provisioning_health_check(SimpleNamespace(headers={}))
                self.assertEqual(denied.exception.status_code, 404)
                probe.assert_not_awaited()

    def test_retry_after_keeps_correlation_and_failure_code(self):
        error = _correlated_http_error(503, "Try again", "id1234", "subscription_configuration_unavailable", retry_after_seconds=15)
        self.assertEqual(error.headers["Retry-After"], "15")
        self.assertEqual(error.headers["X-Correlation-ID"], "id1234")
        self.assertEqual(error.headers["X-Error-Code"], "subscription_configuration_unavailable")


class DeviceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_selects_device_limit_before_existing_payment_methods(self):
        from handlers.device_upgrades import cb_upgrade_device_screen
        callback = SimpleNamespace(data="device_upgrade:19", from_user=SimpleNamespace(id=100))
        with (
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("handlers.device_upgrades._upgrade_context", new=AsyncMock(
                return_value={"expiry": int(time.time()) + 30 * 86400, "limit": 2, "recurring": None},
            )),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as display,
        ):
            await cb_upgrade_device_screen(callback, object())
        buttons = [
            button.callback_data
            for row in display.await_args.args[3].inline_keyboard for button in row
        ]
        self.assertEqual(buttons, [
            "device_upgrade_methods:19:5", "device_upgrade_methods:19:10", "key_devices:19",
        ])


class ActualEconomicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db_path, self.original_pool = database.DB_FILE, database._db_pool
        database.DB_FILE, database._db_pool = str(Path(self.temp.name) / "data.sqlite"), None
        await database.init_db()
        await database.ensure_user(100)
        await database.ensure_user(200)
        await database.set_referrer(200, 100)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.original_db_path, self.original_pool
        self.temp.cleanup()

    async def test_missing_costs_do_not_produce_fake_profit(self):
        with patch.dict(os.environ, {
            "BYMEVPN_COSTS_30D_VPS_RUB": "",
            "BYMEVPN_COSTS_30D_MARKETING_RUB": "",
            "BYMEVPN_COSTS_30D_STAFF_RUB": "",
            "BYMEVPN_COSTS_30D_PROCESSING_RUB": "",
            "BYMEVPN_COSTS_30D_OTHER_RUB": "",
        }):
            report = await economic_overview()
        self.assertIsNone(report["known_rub_balance"])
        self.assertIsNone(report["cac_rub"])
        self.assertIsNone(report["marketing_spend_per_new_payer_rub"])
        self.assertFalse(report["costs_complete"])

    async def test_rub_costs_only_and_real_provider_receipts(self):
        await database.record_payment_idempotent(
            user_id=200, amount=20, currency="RUB", method="test",
            days=30, payload="small-payment-200", status="success", tariff="basic",
            devices=2, provider="test", provider_payment_id="small-payment-200",
        )
        await database.record_payment_idempotent(
            user_id=100, amount=100, currency="XTR", method="test",
            days=30, payload="stars-payment-100", status="success", tariff="basic",
            devices=2, provider="test", provider_payment_id="stars-payment-100",
        )
        with patch.dict(os.environ, {
            "BYMEVPN_COSTS_30D_VPS_RUB": "2",
            "BYMEVPN_COSTS_30D_MARKETING_RUB": "1",
            "BYMEVPN_COSTS_30D_STAFF_RUB": "0",
            "BYMEVPN_COSTS_30D_PROCESSING_RUB": "1",
            "BYMEVPN_COSTS_30D_OTHER_RUB": "0",
        }):
            report = await economic_overview()
        self.assertEqual(report["payments"]["RUB"]["gross"], 20)
        self.assertEqual(report["payments"]["XTR"]["gross"], 100)
        self.assertEqual(report["known_rub_balance"], 16)
        self.assertTrue(report["costs_complete"])
        self.assertIsNone(report["ltv_rub"])
        risks = await referral_risk_snapshot(days=1, min_invites=5)
        self.assertEqual(risks["low_value_first_payments_rub"], [
            {"referrer_id": 100, "first_payments": 1},
        ])


class RestoreDrillTests(unittest.TestCase):
    def test_offline_snapshot_is_read_only_and_reopenable(self):
        with tempfile.TemporaryDirectory() as temp:
            original = Path(temp) / "snapshot.sqlite"
            with sqlite3.connect(str(original)) as db:
                for table in ("users", "keys", "payments", "referrals"):
                    db.execute(f"CREATE TABLE {table} (id INTEGER)")
                    db.execute(f"INSERT INTO {table} VALUES (1)")
            self.assertEqual(rehearse_restore(str(original)), {
                "users": 1, "keys": 1, "payments": 1, "referrals": 1,
            })
            self.assertTrue(original.exists())
