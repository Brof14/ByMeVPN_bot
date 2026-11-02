"""Isolated harness for the additive POST adapter used by GET-only runtimes."""
import unittest

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from telegram_post_compat import register_telegram_check_post


class CheckResponse(BaseModel):
    status: str


class TestTelegramPostCompatibility(unittest.TestCase):
    def setUp(self):
        router = APIRouter()
        self.seen_tokens = []

        @router.get("/auth/telegram/check-session", response_model=CheckResponse)
        async def legacy_get_check_session(token: str, request: Request):
            self.seen_tokens.append(token)
            return CheckResponse(status="pending")

        register_telegram_check_post(router, legacy_get_check_session, CheckResponse)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        self.client = TestClient(app)

    def test_post_json_delegates_to_existing_get_handler(self):
        response = self.client.post(
            "/api/v1/auth/telegram/check-session", json={"token": "auth_safe-body"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "pending"})
        self.assertEqual(self.seen_tokens, ["auth_safe-body"])

    def test_legacy_get_remains_available(self):
        response = self.client.get(
            "/api/v1/auth/telegram/check-session", params={"token": "auth_legacy-query"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.seen_tokens, ["auth_legacy-query"])

    def test_empty_or_oversized_post_token_never_reaches_legacy_handler(self):
        empty = self.client.post(
            "/api/v1/auth/telegram/check-session", json={"token": ""},
        )
        oversized = self.client.post(
            "/api/v1/auth/telegram/check-session", json={"token": "x" * 257},
        )
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"status": "expired"})
        self.assertEqual(oversized.status_code, 200)
        self.assertEqual(oversized.json(), {"status": "expired"})
        self.assertEqual(self.seen_tokens, [])
