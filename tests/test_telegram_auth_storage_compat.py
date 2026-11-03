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
        token = response.json()["session_token"]
        self.assertTrue(await api_router.confirm_telegram_session(
            token, user_id=981234, username="test-user", full_name="Test User",
        ))
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
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["status"], "expired")

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


if __name__ == "__main__":
    unittest.main()
