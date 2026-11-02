"""Safety checks for the additive patch used by the legacy mounted runtime."""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.patch_legacy_telegram_post import patch_file, patch_source


LEGACY_RUNTIME = '''
from fastapi import APIRouter, Request
from pydantic import BaseModel
api_router = APIRouter()
class TelegramCheckResponse(BaseModel):
    status: str
@api_router.get("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session(token: str, request: Request):
    return TelegramCheckResponse(status="expired")
# ---------------------------------------------------------------------------
# 2. SECURE EMAIL REGISTRATION & LOGIN
'''


class TestLegacyTelegramRuntimePatch(unittest.TestCase):
    def test_patch_is_additive_and_compiles(self):
        updated = patch_source(LEGACY_RUNTIME)
        self.assertIn('@api_router.get("/auth/telegram/check-session"', updated)
        self.assertIn('@api_router.post("/auth/telegram/check-session"', updated)
        ast.parse(updated)

    def test_patch_is_idempotent(self):
        once = patch_source(LEGACY_RUNTIME)
        self.assertEqual(patch_source(once), once)

    def test_patch_refuses_unrecognized_runtime(self):
        with self.assertRaises(ValueError):
            patch_source("api_router = APIRouter()\n")

    def test_file_patch_preserves_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_router.py"
            path.write_text(LEGACY_RUNTIME, encoding="utf-8")
            os.chmod(path, 0o640)
            self.assertTrue(patch_file(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertFalse(patch_file(path))


if __name__ == "__main__":
    unittest.main()
