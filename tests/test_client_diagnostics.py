"""App diagnostics provide bounded, authenticated, non-secret support context."""
import os
import tempfile
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_router
import database


class ClientDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "diagnostics.db")
        await database.init_db()
        await database.ensure_user(552211)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.old
        self.tmp.cleanup()

    async def test_reports_limit_frequency_and_capture_no_secrets(self):
        self.assertIsNotNone(await database.save_client_diagnostic(
            552211, "android", "connect", "TUNNEL_TIMEOUT", "1.0.2",
        ))
        self.assertIsNone(await database.save_client_diagnostic(
            552211, "android", "connect", "TUNNEL_TIMEOUT", "1.0.2",
        ))
        events = await database.get_recent_client_diagnostics(552211)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "TUNNEL_TIMEOUT")


class ClientDiagnosticsInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(api_router.api_router, prefix="/api/v1")
        app.dependency_overrides[api_router.get_current_user_id] = lambda: 552211
        cls.client = TestClient(app)

    def test_untrusted_free_form_text_is_rejected(self):
        response = self.client.post("/api/v1/diagnostics/report", json={
            "platform": "android", "stage": "connect",
            "error_code": "https://private.example/secret/uuid",
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
