"""Contract tests for server-side Desktop OAuth code exchange.
No real Google secrets or network requests are used.
"""
import os
import secrets
import sys
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
import api_router


class FakeTokenClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None):
        self.captured.append((url, data))
        return self.response


class GoogleDesktopExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.include_router(api_router.api_router, prefix="/api/v1")
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
        self.env = patch.dict(os.environ, {
            "GOOGLE_SIGNIN_ENABLED": "1",
            "GOOGLE_WEB_CLIENT_ID": "web.apps.googleusercontent.com",
            "GOOGLE_DESKTOP_CLIENT_ID": "desktop.apps.googleusercontent.com",
            "GOOGLE_DESKTOP_CLIENT_SECRET": "test-not-a-real-secret",
        })
        self.env.start()
        self.payload = {
            "code": "4/" + "q" * 60,
            "code_verifier": "a" * 64,
            "redirect_uri": "http://127.0.0.1:54321/oauth2/callback",
            "nonce": "n" * 40,
            "install_id": "test-install",
        }

    async def asyncTearDown(self):
        self.env.stop()
        await self.client.aclose()

    async def test_config_advertises_server_exchange_without_secret(self):
        response = await self.client.get("/api/v1/auth/google/desktop-client")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_id"], "desktop.apps.googleusercontent.com")
        self.assertIs(response.json()["server_code_exchange"], True)
        self.assertNotIn("test-not-a-real-secret", response.text)

    async def test_rejects_non_loopback_redirect_without_google_network(self):
        payload = dict(self.payload, redirect_uri="http://evil.test/oauth2/callback")
        result = await self.client.post("/api/v1/auth/google/desktop-exchange", json=payload)
        self.assertEqual(result.status_code, 400)

    async def test_rejects_short_verifier_without_google_network(self):
        payload = dict(self.payload, code_verifier="weak")
        result = await self.client.post("/api/v1/auth/google/desktop-exchange", json=payload)
        self.assertEqual(result.status_code, 400)

    async def test_exchanges_code_using_server_secret_then_delegates_nonce_validation(self):
        captured = []
        token = "aaa.bbb.ccc"
        with patch.object(api_router.httpx, "AsyncClient",
                side_effect=lambda **kwargs: FakeTokenClient(Response(200, json={"id_token": token}), captured)):
            with patch.object(api_router, "login_google", new_callable=AsyncMock) as verify:
                verify.return_value = {"user": {"id": "42"}, "subscription": None}
                result = await self.client.post("/api/v1/auth/google/desktop-exchange", json=self.payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["user"]["id"], "42")
        self.assertEqual(captured[0][0], "https://oauth2.googleapis.com/token")
        self.assertEqual(captured[0][1]["client_secret"], "test-not-a-real-secret")
        self.assertEqual(captured[0][1]["code_verifier"], self.payload["code_verifier"])
        self.assertEqual(verify.await_args.args[0].nonce, self.payload["nonce"])
        self.assertEqual(verify.await_args.args[0].id_token, token)

    async def test_provider_error_is_sanitized(self):
        captured = []
        with patch.object(api_router.httpx, "AsyncClient",
                side_effect=lambda **kwargs: FakeTokenClient(
                    Response(400, json={"error": "invalid_request",
                                        "error_description": "PRIVATE_DETAILS"}), captured)):
            result = await self.client.post("/api/v1/auth/google/desktop-exchange", json=self.payload)
        self.assertEqual(result.status_code, 401)
        self.assertIn("invalid_request", result.text)
        self.assertNotIn("PRIVATE_DETAILS", result.text)
        self.assertNotIn("test-not-a-real-secret", result.text)

    async def test_missing_secret_disables_server_exchange_without_breaking_desktop_config(self):
        with patch.dict(os.environ, {"GOOGLE_DESKTOP_CLIENT_SECRET": ""}):
            cfg = await self.client.get("/api/v1/auth/google/desktop-client")
            self.assertEqual(cfg.status_code, 200)
            self.assertFalse(cfg.json()["server_code_exchange"])
            denied = await self.client.post("/api/v1/auth/google/desktop-exchange", json=self.payload)
            self.assertEqual(denied.status_code, 503)


if __name__ == "__main__":
    unittest.main()
