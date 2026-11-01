"""Compatibility tests for legacy APK download aliases."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from download_routes import router


class LegacyDownloadRedirectTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_legacy_links_redirect_to_the_official_download_page(self):
        for path in ("/download", "/download/app.apk", "/download/debug"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/app/")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("github.com", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
