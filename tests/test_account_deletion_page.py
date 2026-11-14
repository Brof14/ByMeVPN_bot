import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import FastAPI

import webhook
import api_router


class TestBrowserAccountDeletion(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.api_router, prefix="/api/v1")
        self.app.add_api_route("/account/delete", webhook.account_deletion_page, methods=["GET"])
        self.app.add_api_route("/app/assets/{asset_name}", webhook.app_brand_asset, methods=["GET"])
        self.client = TestClient(self.app)

    def test_page_and_assets_are_served_without_cache(self):
        page = self.client.get("/account/delete")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Google or email", page.text)
        self.assertIn("X-Confirm-Account-Deletion", self.client.get("/app/assets/account_delete.js").text)
        self.assertEqual(page.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(self.client.get("/app/assets/../../api_router.py").status_code, 404)

    def test_public_google_web_client_exposes_only_configured_client_id(self):
        with patch.dict("os.environ", {"GOOGLE_SIGNIN_ENABLED": "1", "GOOGLE_WEB_CLIENT_ID": "public.apps.googleusercontent.com"}):
            response = self.client.get("/api/v1/auth/google/web-client")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"client_id": "public.apps.googleusercontent.com"})
