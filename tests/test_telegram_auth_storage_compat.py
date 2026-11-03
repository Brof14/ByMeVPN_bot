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

    async def test_profile_snapshot_failure_is_correlated_and_state_remains_retryable(self):
        import hashlib

        token = await self._create_confirmed_state()
        state_hash = hashlib.sha256(token.removeprefix("auth_").encode()).hexdigest()
        with patch.object(
            api_router, "_auth_subscription_snapshot",
            new=AsyncMock(side_effect=RuntimeError("isolated snapshot failure")),
        ):
            response = await self.client.post(
                "/api/v1/auth/telegram/check-session", json={"token": token},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("X-Correlation-ID"), state_hash[:16])
        self.assertEqual(
            (await api_router.database.get_mobile_auth_session(state_hash))["status"],
            "authenticated",
        )


if __name__ == "__main__":
    unittest.main()
