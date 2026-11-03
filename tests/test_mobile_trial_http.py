"""HTTP contract coverage for the Android 3-day trial endpoint."""
from __future__ import annotations

import os
import secrets
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router


class MobileTrialHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()

        async def authenticated_user():
            return 987654

        self.app.dependency_overrides[api_router.get_current_user_id] = authenticated_user
        self.app.include_router(api_router.api_router, prefix="/api/v1")
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="https://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_android_trial_http_contract_grants_three_days_and_two_devices(self):
        with (
            patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)),
            patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])),
            patch.object(api_router.database, "try_claim_trial", new=AsyncMock(return_value=True)) as claim,
            patch.object(api_router.database, "add_key", new=AsyncMock()) as add_key,
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)),
            patch("xui_client.create_xui_user", new=AsyncMock(return_value={
                "subscription_url": "https://subscription.invalid/test",
                "uuid": "test-client-id",
            })) as create_client,
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "devices": [], "limit_hwid": 2, "limit_ip": 0,
            })),
            patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[
                {"node_code": "test-node"},
            ])),
        ):
            response = await self.client.post(
                "/api/v1/subscription/trial",
                json={"install_id": "test-install-id", "device_model": "test-device"},
                headers={"X-HWID": "test-install-id"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "trial")
        self.assertEqual(body["max_devices"], 2)
        self.assertEqual(body["seconds_remaining"], 3 * 86400)
        claim.assert_awaited_once_with(987654)
        create_client.assert_awaited_once_with(
            user_id=987654, days=3, limit_ip=0, limit_hwid=2,
        )
        self.assertEqual(add_key.await_args.kwargs["days"], 3)
        self.assertEqual(add_key.await_args.kwargs["limit_ip"], 2)

    async def test_panel_uncertainty_does_not_claim_trial(self):
        with (
            patch.object(api_router.database, "has_trial_used", new=AsyncMock(return_value=False)),
            patch.object(api_router.database, "get_user_keys", new=AsyncMock(return_value=[])),
            patch.object(api_router.database, "try_claim_trial", new=AsyncMock()) as claim,
            patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=TimeoutError())),
        ):
            response = await self.client.post(
                "/api/v1/subscription/trial",
                json={"install_id": "test-install-id", "device_model": "test-device"},
                headers={"X-HWID": "test-install-id"},
            )
        self.assertEqual(response.status_code, 503)
        claim.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
