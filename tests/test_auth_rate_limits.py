"""Production-grade auth request quotas shared by concurrent workers."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from auth_rate_limits import client_ip, require_auth_quota


def request(ip="192.0.2.5", real_ip=""):
    return SimpleNamespace(client=SimpleNamespace(host=ip),
                           headers={"X-Real-IP": real_ip} if real_ip else {})


class AuthRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_bucket_is_atomic_under_concurrent_requests(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("auth_rate_limits.DB_FILE", str(Path(root) / "auth.sqlite")):
                attempts = await asyncio.gather(
                    *(require_auth_quota(request(), "login_ip", limit=3, window_seconds=300)
                      for _ in range(10)),
                    return_exceptions=True,
                )
                self.assertEqual(sum(x is None for x in attempts), 3)
                denied = [x for x in attempts if isinstance(x, HTTPException)]
                self.assertEqual(len(denied), 7)
                self.assertTrue(all(x.status_code == 429 for x in denied))
                self.assertTrue(all(int(x.headers["Retry-After"]) > 0 for x in denied))

    async def test_account_quota_applies_to_distinct_ips(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("auth_rate_limits.DB_FILE", str(Path(root) / "auth.sqlite")):
                for ip in ("192.0.2.5", "192.0.2.6"):
                    await require_auth_quota(request(ip), "login_account",
                                             limit=2, window_seconds=300,
                                             subject="TEST@EXAMPLE.COM")
                with self.assertRaises(HTTPException) as denied:
                    await require_auth_quota(request("192.0.2.7"), "login_account",
                                             limit=2, window_seconds=300,
                                             subject="test@example.com")
                self.assertEqual(denied.exception.status_code, 429)

    def test_proxy_spoofing_requires_trusted_peer(self):
        self.assertEqual(client_ip(request("192.0.2.5", "10.10.0.1")), "192.0.2.5")
        self.assertEqual(client_ip(request("127.0.0.1", "192.0.2.6")), "192.0.2.6")
        self.assertEqual(client_ip(request("127.0.0.1", "not-an-ip")), "127.0.0.1")
