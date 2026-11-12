"""HTTP regressions for a deployed legacy DB shim without Telegram sessions."""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
from telegram_auth_storage_compat import install_telegram_auth_storage_compat


class TelegramLegacyStorageHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        self._tmp = tempfile.TemporaryDirectory(prefix="telegram-session-compat-")
        self._conn = await aiosqlite.connect(str(Path(self._tmp.name) / "legacy.sqlite3"))
        self.legacy_database = SimpleNamespace(
            get_db=self._get_db,
            _db_semaphore=asyncio.Semaphore(1),
        )
        missing = install_telegram_auth_storage_compat(self.legacy_database)
        self.assertEqual(set(missing), {
            "create_mobile_auth_session", "confirm_mobile_auth_session",
            "poll_mobile_auth_session", "get_mobile_auth_session",
            "consume_mobile_auth_session", "cancel_mobile_auth_session",
            "is_mobile_jwt_revoked", "revoke_mobile_jwt",
            "ensure_mobile_device_bootstrap_schema", "get_mobile_device_bootstrap",
            "reserve_mobile_device_bootstrap", "delete_mobile_device_bootstrap",
        })
        self.original_database = api_router.database
        api_router.database = self.legacy_database
        self.app = FastAPI()
        self.app.include_router(api_router.api_router, prefix="/api/v1")
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="https://testserver",
        )

    async def _get_db(self):
        return self._conn

    async def test_legacy_jwt_revocation_helpers_create_store_and_expire_records(self):
        token_id = "isolated-session-id"
        self.assertFalse(await self.legacy_database.is_mobile_jwt_revoked(token_id))
        await self.legacy_database.revoke_mobile_jwt(
            token_id, int(api_router.time.time()) + 60,
        )
        self.assertTrue(await self.legacy_database.is_mobile_jwt_revoked(token_id))

        await self._conn.execute(
            "UPDATE revoked_mobile_jwt_sessions SET expires_at=? WHERE jti=?",
            (int(api_router.time.time()) - 1, token_id),
        )
        await self._conn.commit()
        self.assertFalse(await self.legacy_database.is_mobile_jwt_revoked(token_id))

    async def test_authenticated_http_status_works_with_legacy_revocation_adapter(self):
        token = api_router.create_simple_jwt(981234, "isolated@example.invalid")
        with patch.object(
            api_router, "get_user_real_subscription",
            new=AsyncMock(return_value=(None, [], {"status": "none", "trial_available": True})),
        ):
            response = await self.client.get(
                "/api/v1/subscription/status",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "none")

    async def test_legacy_http_status_bootstraps_active_subscription_additively(self):
        expiry = int(api_router.time.time()) + 86_400
        fixed_bootstrap_expiry = int(api_router.time.time()) + 600
        with (
            patch.object(
                api_router, "get_user_real_subscription",
                new=AsyncMock(return_value=(None, [], {
                    "status": "active", "seconds_remaining": 86_400,
                    "max_devices": 2, "plan_code": "1m_d2",
                })),
            ),
            patch("xui_client.has_xui_client", new=AsyncMock(return_value=True)),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "expiry_time": expiry * 1000,
            })),
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "limit_hwid": 2, "limit_ip": 0, "devices": [],
            })),
            patch.object(api_router, "device_binding", return_value="isolated-device-hash"),
            patch.object(api_router, "create_entitlement", return_value="isolated-signed-entitlement"),
        ):
            token = api_router.create_simple_jwt(981234, "isolated@example.invalid")
            response = await self.client.get(
                "/api/v1/subscription/status",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-HWID": "isolated-device",
                    "X-Entitlement-Version": "es256-v1",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["max_devices"], 2)
        self.assertTrue(body["entitlement_bootstrap"])
        self.assertLessEqual(body["entitlement_expires_at"], fixed_bootstrap_expiry + 5)
        self.assertEqual(body["entitlement"], "isolated-signed-entitlement")
        schema = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mobile_device_bootstraps'"
        )
        self.assertIsNotNone(await schema.fetchone())

    async def test_legacy_bootstrap_reservation_is_idempotent_and_enforces_capacity(self):
        subscription_expiry = int(api_router.time.time()) + 86_400
        fixed_expiry = int(api_router.time.time()) + 600
        first = await self.legacy_database.reserve_mobile_device_bootstrap(
            981234, "device-a", subscription_expiry, fixed_expiry, 0, 2,
        )
        retry = await self.legacy_database.reserve_mobile_device_bootstrap(
            981234, "device-a", subscription_expiry, fixed_expiry + 300, 0, 2,
        )
        second = await self.legacy_database.reserve_mobile_device_bootstrap(
            981234, "device-b", subscription_expiry, fixed_expiry, 0, 2,
        )
        full = await self.legacy_database.reserve_mobile_device_bootstrap(
            981234, "device-c", subscription_expiry, fixed_expiry, 1, 2,
        )
        self.assertEqual(first, fixed_expiry)
        self.assertEqual(retry, fixed_expiry)
        self.assertEqual(second, fixed_expiry)
        self.assertIsNone(full)
        expired_now = fixed_expiry + 1
        with patch("telegram_auth_storage_compat.time.time", return_value=expired_now):
            renewed = await self.legacy_database.reserve_mobile_device_bootstrap(
                981234, "device-a", subscription_expiry, expired_now + 600, 0, 2,
            )
        self.assertEqual(renewed, expired_now + 600)
        await self.legacy_database.delete_mobile_device_bootstrap(981234, "device-a")
        self.assertIsNone(await self.legacy_database.get_mobile_device_bootstrap(
            981234, "device-a", subscription_expiry,
        ))

    async def asyncTearDown(self):
        api_router.database = self.original_database
        await self.client.aclose()
        await self._conn.close()
        self._tmp.cleanup()

    async def _create_confirmed_state(self) -> str:
        response = await self.client.post(
            "/api/v1/auth/telegram/create-session", json={},
        )
        self.assertEqual(response.status_code, 200)
        correlation_id = response.headers.get("X-Correlation-ID")
        self.assertRegex(correlation_id or "", r"^[a-f0-9]{16}$")
        token = response.json()["session_token"]
        self.assertTrue(await api_router.confirm_telegram_session(
            token, user_id=981234, username="test-user", full_name="Test User",
        ))
        self.created_correlation_id = correlation_id
        return token

    async def test_post_json_exchange_succeeds_once_and_replay_expires(self):
        token = await self._create_confirmed_state()
        with patch.object(
            api_router, "_auth_subscription_snapshot",
            new=AsyncMock(return_value=("", [], {"status": "inactive"})),
        ):
            first = await self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": token},
            )
            replay = await self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": token},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "authenticated")
        self.assertTrue(first.json().get("access_token"))
        self.assertEqual(first.headers.get("X-Correlation-ID"), self.created_correlation_id)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["status"], "expired")
        self.assertEqual(replay.headers.get("X-Correlation-ID"), self.created_correlation_id)

    async def test_legacy_get_remains_compatible(self):
        from urllib.parse import urlencode

        token = await self._create_confirmed_state()
        with patch.object(
            api_router, "_auth_subscription_snapshot",
            new=AsyncMock(return_value=("", [], {"status": "inactive"})),
        ):
            response = await self.client.get(
                "/api/v1/auth/telegram/check-session?" + urlencode({"token": token}),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "authenticated")

    async def test_bot_start_waits_for_confirmation_before_android_can_exchange(self):
        """The bot must persist confirmation before sending its success message."""
        from handlers.start import cmd_start

        token = await self._create_pending_state()
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=981234, username="test-user", full_name="Test User", first_name="Test",
            ),
            text=f"/start {token}",
            answer=AsyncMock(),
        )
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(return_value={"user_id": 981234})),
            patch("handlers.start.ensure_user", new=AsyncMock()),
        ):
            await cmd_start(message, SimpleNamespace(), SimpleNamespace())

        message.answer.assert_awaited_once()
        self.assertIn("Вход в приложение ByMeVPN подтверждён", message.answer.await_args.args[0])
        with patch.object(
            api_router, "_auth_subscription_snapshot",
            new=AsyncMock(return_value=("", [], {"status": "inactive"})),
        ):
            exchange = await self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": token},
            )
        self.assertEqual(exchange.status_code, 200)
        self.assertEqual(exchange.json()["status"], "authenticated")
        self.assertTrue(exchange.json().get("access_token"))

    async def test_bot_does_not_claim_success_when_confirmation_fails(self):
        from handlers.start import cmd_start

        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=981234, username="test-user", full_name="Test User", first_name="Test",
            ),
            text="/start auth_test-state",
            answer=AsyncMock(),
        )
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(return_value={"user_id": 981234})),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch.object(api_router, "confirm_telegram_session", new=AsyncMock(return_value=False)) as confirm,
        ):
            await cmd_start(message, SimpleNamespace(), SimpleNamespace())

        confirm.assert_awaited_once()
        message.answer.assert_awaited_once()
        self.assertIn("уже использована или истекла", message.answer.await_args.args[0])
        self.assertNotIn("подтверждён", message.answer.await_args.args[0])

    async def _create_pending_state(self) -> str:
        response = await self.client.post(
            "/api/v1/auth/telegram/create-session", json={},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["session_token"]

    async def test_profile_snapshot_failure_does_not_block_login_or_consume_twice(self):
        import hashlib

        token = await self._create_confirmed_state()
        state_hash = hashlib.sha256(token.removeprefix("auth_").encode()).hexdigest()
        with patch.object(
            api_router, "_auth_subscription_snapshot",
            new=AsyncMock(side_effect=RuntimeError("isolated snapshot failure")),
        ) as snapshot:
            response = await self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "authenticated")
        self.assertTrue(response.json()["access_token"])
        self.assertEqual(response.json()["subscription"]["status"], "unknown")
        self.assertEqual(response.headers.get("X-Correlation-ID"), state_hash[:16])
        self.assertEqual(
            (await api_router.database.get_mobile_auth_session(state_hash))["status"],
            "expired",
        )
        snapshot.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
