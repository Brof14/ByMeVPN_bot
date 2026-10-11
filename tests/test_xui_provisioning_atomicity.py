"""Provisioning must fail closed if any configured inbound fails."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_xui_provisioning_atomicity.db")

import xui_client


class _FakeClientApi:
    def __init__(self, fail_inbound=None):
        self.fail_inbound = fail_inbound
        self.added = []
        self.deleted = []

    async def add(self, inbound_id, clients):
        if inbound_id == self.fail_inbound:
            raise RuntimeError("simulated inbound failure")
        self.added.append((inbound_id, clients[0].id, clients[0].sub_id))
        return True

    async def delete(self, inbound_id, client_uuid):
        self.deleted.append((inbound_id, client_uuid))
        return True


class _FakeApi:
    def __init__(self, fail_inbound=None):
        self.client = _FakeClientApi(fail_inbound=fail_inbound)


class TestXuiProvisioningAtomicity(unittest.IsolatedAsyncioTestCase):
    async def test_partial_inbound_creation_is_cleaned_and_never_reported_successful(self):
        api = _FakeApi(fail_inbound=3)

        async def api_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        with (
            patch.object(xui_client, "XUI_INBOUND_IDS", [2, 3]),
            patch.object(xui_client, "_get_api", new=AsyncMock(return_value=api)),
            patch.object(xui_client, "_find_client_uuid", new=AsyncMock(return_value=None)),
            patch.object(xui_client, "_api_call_with_retry", new=api_call),
        ):
            result = await xui_client.create_xui_user(user_id=123456, days=30, limit_ip=2)

        self.assertIsNone(result)
        self.assertEqual([item[0] for item in api.client.added], [2])
        self.assertEqual([item[0] for item in api.client.deleted], [2])
        # Cleanup must target the UUID used by the successful creation attempt.
        self.assertEqual(api.client.deleted[0][1], api.client.added[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
