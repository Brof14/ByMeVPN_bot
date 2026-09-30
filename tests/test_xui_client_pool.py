"""Regression tests for 3x-ui API pool authentication recovery."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_xui_client_pool.db")

import xui_client


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeApi:
    def __init__(self, login_error=None):
        self.login_error = login_error
        self.login_calls = 0
        self.session = _FakeSession()

    async def login(self):
        self.login_calls += 1
        if self.login_error:
            raise self.login_error


class TestXuiClientPool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        xui_client._api_pool = None

    def tearDown(self):
        xui_client._api_pool = None

    async def test_failed_login_is_not_published_and_next_login_recovers(self):
        failed = _FakeApi(RuntimeError("temporary login failure"))
        recovered = _FakeApi()

        with (
            patch("xui_client.AsyncApi", side_effect=[failed, recovered]),
            patch("xui_client.asyncio.sleep", new=AsyncMock()),
        ):
            api = await xui_client._get_api()

        self.assertIs(api, recovered)
        self.assertIs(xui_client._api_pool, recovered)
        self.assertEqual(failed.login_calls, 1)
        self.assertEqual(recovered.login_calls, 1)
        self.assertTrue(failed.session.closed)

    async def test_authentication_error_invalidates_cached_pool(self):
        cached = _FakeApi()
        xui_client._api_pool = cached

        async def unauthorized():
            raise RuntimeError("HTTP 401 Unauthorized")

        with self.assertRaisesRegex(RuntimeError, "401 Unauthorized"):
            await xui_client._api_call_with_retry(unauthorized)

        self.assertIsNone(xui_client._api_pool)


if __name__ == "__main__":
    unittest.main()
