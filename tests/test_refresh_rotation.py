"""One-time opaque refresh sessions must be independently revocable."""
import os
import secrets
import sys
import tempfile
import unittest

os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_router
import database
from fastapi import HTTPException


class TestRefreshRotation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "auth.sqlite")
        await database.init_db()
        await database.ensure_user(987650123)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.saved_path
        self.tmp.cleanup()

    async def test_rotation_invalidates_previous_refresh_and_access(self):
        initial = await api_router.issue_session_tokens(987650123, "real@example.invalid")
        self.assertTrue(initial["refresh"].startswith("rf2_"))
        first_jti = api_router._verified_token_claims(initial["access"])["jti"]
        replacement = await api_router.refresh_token(api_router.RefreshRequest(refresh=initial["refresh"]))
        self.assertNotEqual(initial["access"], replacement["access"])
        self.assertNotEqual(initial["refresh"], replacement["refresh"])
        self.assertTrue(await database.is_mobile_jwt_revoked(first_jti))
        with self.assertRaises(HTTPException) as error:
            await api_router.refresh_token(api_router.RefreshRequest(refresh=initial["refresh"]))
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(api_router.verify_simple_jwt(replacement["access"]), 987650123)

    async def test_logout_revokes_rotated_refresh_credential(self):
        issued = await api_router.issue_session_tokens(987650123, "real@example.invalid")
        access = issued["access"]
        self.assertEqual(
            await api_router.logout(f"Bearer {access}", user_id=987650123),
            {"status": "ok"},
        )
        with self.assertRaises(HTTPException) as error:
            await api_router.refresh_token(api_router.RefreshRequest(refresh=issued["refresh"]))
        self.assertEqual(error.exception.status_code, 401)

    async def test_legacy_refresh_is_upgraded_without_requiring_relogin(self):
        old = api_router.create_simple_jwt(987650123, "real@example.invalid")
        upgraded = await api_router.refresh_token(api_router.RefreshRequest(refresh="ref_" + old))
        self.assertTrue(upgraded["refresh"].startswith("rf2_"))
        self.assertEqual(api_router.verify_simple_jwt(upgraded["access"]), 987650123)


if __name__ == "__main__":
    unittest.main()
