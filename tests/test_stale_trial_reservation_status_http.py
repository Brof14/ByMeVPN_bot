"""HTTP tests for stale trial reservations in an isolated SQLite database."""
from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router


class StaleTrialReservationStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    user_id = 563829

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="bmvpn-trial-status-")
        self.db_path = os.path.join(self.temp_dir.name, "isolated.sqlite3")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE trial_claim_reservations ("
                "user_id INTEGER PRIMARY KEY, claim_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
            )

        async def authenticated_user():
            return self.user_id

        self.app = FastAPI()
        self.app.dependency_overrides[api_router.get_current_user_id] = authenticated_user
        self.app.include_router(api_router.api_router, prefix="/api/v1")
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="https://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp_dir.cleanup()

    def _store_reservation(self, created_at: int) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO trial_claim_reservations(user_id,claim_id,created_at) "
                "VALUES(?,?,?)",
                (self.user_id, "isolated-claim", created_at),
            )

    async def _read_reservation(self, user_id: int):
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT claim_id,created_at FROM trial_claim_reservations WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return {"claim_id": row[0], "created_at": row[1]} if row else None

    async def _get_status(self, panel_lookup):
        with (
            patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)),
            patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])),
            patch.object(api_router.database, "get_trial_claim_reservation", new=self._read_reservation),
            patch("xui_client.get_xui_client_details", new=panel_lookup),
            patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=[])),
            patch.object(api_router, "device_binding", return_value="test-device-binding"),
            patch.object(api_router, "load_private_key", return_value=object()),
        ):
            return await self.client.get(
                "/api/v1/subscription/status",
                headers={
                    "X-HWID": "isolated-install-id",
                    "X-Entitlement-Version": "es256-v1",
                },
            )

    async def test_stale_reservation_does_not_hide_trial_after_confirmed_panel_absence(self):
        created_at = int(time.time()) - api_router.TRIAL_CLAIM_RECONCILE_AFTER_SECONDS - 1
        self._store_reservation(created_at)
        panel_lookup = AsyncMock(return_value=None)

        response = await self._get_status(panel_lookup)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "none")
        self.assertTrue(body["trial_available"])
        self.assertIsNone(body["trial_unavailable_reason"])
        self.assertEqual(panel_lookup.await_count, 2)
        # GET must not spend the claim or mutate its reservation.
        self.assertEqual(await self._read_reservation(self.user_id), {
            "claim_id": "isolated-claim",
            "created_at": created_at,
        })

    async def test_fresh_reservation_remains_pending_without_second_panel_read(self):
        self._store_reservation(int(time.time()) - 10)
        panel_lookup = AsyncMock(return_value=None)

        response = await self._get_status(panel_lookup)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["trial_available"])
        self.assertEqual(body["trial_unavailable_reason"], "trial_activation_pending")
        self.assertEqual(panel_lookup.await_count, 1)

    async def test_uncertain_stale_panel_lookup_fails_closed_without_calling_trial_used(self):
        created_at = int(time.time()) - api_router.TRIAL_CLAIM_RECONCILE_AFTER_SECONDS - 1
        self._store_reservation(created_at)
        panel_lookup = AsyncMock(side_effect=[None, TimeoutError("isolated panel timeout")])

        response = await self._get_status(panel_lookup)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["trial_available"])
        self.assertEqual(body["trial_unavailable_reason"], "device_confirmation_required")
        self.assertEqual(await self._read_reservation(self.user_id), {
            "claim_id": "isolated-claim",
            "created_at": created_at,
        })

