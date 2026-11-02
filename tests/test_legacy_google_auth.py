import asyncio
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

from legacy_google_auth import _consume_nonce, _create_nonce, _get_or_create_google_account
from ops.patch_legacy_google_auth import MARKER, patched_text


class LegacyGoogleStorageTests(unittest.TestCase):
    def test_nonce_is_single_use_and_schema_creation_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT);"
                "CREATE TABLE app_users(user_id INTEGER PRIMARY KEY,email TEXT UNIQUE," 
                "password_hash TEXT,salt TEXT,name TEXT,created_at INTEGER);"
            )
            db.close()
            module = types.SimpleNamespace(DB_FILE=str(path))
            asyncio.run(_create_nonce(module, "a" * 64, 4_000_000_000))
            self.assertTrue(asyncio.run(_consume_nonce(module, "a" * 64)))
            self.assertFalse(asyncio.run(_consume_nonce(module, "a" * 64)))

    def test_google_subject_is_stable_and_email_does_not_merge_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT);"
                "CREATE TABLE app_users(user_id INTEGER PRIMARY KEY,email TEXT UNIQUE," 
                "password_hash TEXT,salt TEXT,name TEXT,created_at INTEGER);"
            )
            db.close()
            module = types.SimpleNamespace(DB_FILE=str(path))
            first = asyncio.run(_get_or_create_google_account(module, "sub-a", "same@example.test", "A"))
            same = asyncio.run(_get_or_create_google_account(module, "sub-a", "renamed@example.test", "A2"))
            second = asyncio.run(_get_or_create_google_account(module, "sub-b", "same@example.test", "B"))
            self.assertEqual(first["user_id"], same["user_id"])
            self.assertNotEqual(first["user_id"], second["user_id"])


class LegacyGooglePatchTests(unittest.TestCase):
    def test_patch_is_additive_and_idempotent(self):
        source = 'from fastapi import APIRouter\napi_router = APIRouter()\n@api_router.post("/auth/google")\nasync def login_google():\n    pass\n'
        patched = patched_text(source)
        self.assertIn(source, patched)
        self.assertIn(MARKER, patched)
        self.assertEqual(patched_text(patched), patched)


if __name__ == "__main__":
    unittest.main()
