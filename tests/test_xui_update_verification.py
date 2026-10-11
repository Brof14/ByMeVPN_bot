"""Regression tests for 3x-ui read-back verification."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_xui_update_verification.db")

import xui_client


class _FakeApi:
    def __init__(self, missing_inbound=None):
        self.missing_inbound = missing_inbound
        self.current = SimpleNamespace(
            uuid="existing-uuid",
            expiry_time=100000,
            limit_ip=2,
            sub_id="existing-sub-id",
            total_gb=0,
            enable=True,
        )
        self.inbounds = {
            inbound_id: SimpleNamespace(
                client_stats=[
                    SimpleNamespace(
                        email="12345",
                        uuid="existing-uuid",
                        expiry_time=100000,
                        limit_ip=2,
                        sub_id="existing-sub-id",
                    )
                ]
            )
            for inbound_id in (2, 3)
        }
        self.client = SimpleNamespace(
            get_by_email=self.get_by_email,
            update=self.update,
        )
        self.inbound = SimpleNamespace(get_by_id=self.get_inbound)

    async def get_by_email(self, email):
        return self.current if email == "12345" else None

    async def update(self, client_uuid, client):
        self.current = client
        for inbound_id, inbound in self.inbounds.items():
            if inbound_id == self.missing_inbound:
                inbound.client_stats = []
            else:
                inbound.client_stats = [
                    SimpleNamespace(
                        email=client.email,
                        uuid=client.uuid,
                        expiry_time=client.expiry_time,
                        limit_ip=client.limit_ip,
                        sub_id=client.sub_id,
                    )
                ]
        return None

    async def get_inbound(self, inbound_id):
        return self.inbounds.get(inbound_id)


class TestXuiUpdateVerification(unittest.IsolatedAsyncioTestCase):
    async def _run_update(self, api):
        async def passthrough(func, *args, **kwargs):
            return await func(*args, **kwargs)

        with (
            patch.object(xui_client, "_get_api", new=AsyncMock(return_value=api)),
            patch.object(xui_client, "_find_client_uuid", new=AsyncMock(return_value="existing-uuid")),
            patch.object(xui_client, "_api_call_with_retry", new=passthrough),
            patch("xui_client.asyncio.sleep", new=AsyncMock()),
            patch.object(xui_client, "XUI_INBOUND_IDS", [2, 3]),
        ):
            return await xui_client.update_xui_user(
                12345, new_expiry_ms=200000, limit_ip=5
            )

    async def test_update_returns_success_only_when_all_inbounds_match(self):
        result = await self._run_update(_FakeApi())
        self.assertIsNotNone(result)
        self.assertEqual(result["expiry_time"], 200000)
        self.assertEqual(result["limit_ip"], 5)

    async def test_missing_inbound_fails_closed(self):
        result = await self._run_update(_FakeApi(missing_inbound=3))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
