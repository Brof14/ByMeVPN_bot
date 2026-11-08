"""Direct-download website and update feed contracts with a temporary file root."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import update_site


class UpdateSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "app"
        (self.root / "downloads").mkdir(parents=True)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for icon in update_site.ICON_ASSETS:
            (self.assets / icon).write_bytes(b"\x89PNG\r\n\x1a\n" + icon.encode())
        app = FastAPI()
        app.include_router(update_site.router)
        self.client = TestClient(app)
        self.root_patch = patch.object(update_site, "ROOT", self.root)
        self.root_patch.start()
        self.assets_patch = patch.object(update_site, "ASSETS", self.assets)
        self.assets_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.assets_patch.stop()
        self.temp.cleanup()

    def test_connection_logos_are_served_from_fixed_asset_allowlist(self):
        for icon in update_site.ICON_ASSETS:
            with self.subTest(icon=icon):
                response = self.client.get(f"/app/assets/{icon}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"\x89PNG\r\n\x1a\n" + icon.encode())
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["cache-control"], "public, max-age=86400")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/app/assets/unknown.png").status_code, 404)
        self.assertEqual(self.client.get("/app/assets/../update.json").status_code, 404)

    def publish_test_artifact(self):
        apk = b"isolated-test-apk-content"
        name = "ByMeVPN-v1.2.3.apk"
        (self.root / "downloads" / name).write_bytes(apk)
        (self.root / "update.json").write_text(json.dumps({
            "version_name": "1.2.3", "version_code": 123,
            "download_url": f"https://bymevpn.duckdns.org/app/downloads/{name}",
            "sha256": hashlib.sha256(apk).hexdigest(), "release_notes": "Test feed",
        }), encoding="utf-8")
        return name, apk

    def test_first_install_page_supports_get_and_head(self):
        get = self.client.get("/app/")
        self.assertEqual(get.status_code, 200)
        self.assertIn("/app/logo", get.text)
        self.assertIn("/app/update.json", get.text)
        self.assertIn("Android 7.0", get.text)
        self.assertIn('href="https://github.com/Brof14/ByMeVPN-app/releases/download/v1.0.12/ByMeVPN-v1.0.12.apk"', get.text)
        self.assertIn('src="/app/logo"', get.text)
        self.assertIn("@media (max-width: 680px)", get.text)
        self.assertIn("prefers-reduced-motion", get.text)
        self.assertIn("fetch('/app/update.json'", get.text)
        self.assertEqual(get.headers["cache-control"], "no-cache")
        head = self.client.head("/app/")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(int(head.headers["content-length"]), len(get.content))

    def test_manifest_and_versioned_apk_get_head_mime_and_cache_headers(self):
        name, apk_bytes = self.publish_test_artifact()
        manifest = self.client.get("/app/update.json")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(set(manifest.json()), {
            "version_name", "version_code", "download_url", "sha256", "release_notes"})
        self.assertEqual(manifest.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(manifest.json()["sha256"], hashlib.sha256(apk_bytes).hexdigest())
        manifest_head = self.client.head("/app/update.json")
        self.assertEqual(manifest_head.status_code, 200)
        self.assertEqual(manifest_head.content, b"")
        self.assertEqual(int(manifest_head.headers["content-length"]), len(manifest.content))

        apk = self.client.get(f"/app/downloads/{name}")
        self.assertEqual(apk.status_code, 200)
        self.assertEqual(apk.content, apk_bytes)
        self.assertEqual(apk.headers["content-type"], "application/vnd.android.package-archive")
        self.assertEqual(apk.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(int(apk.headers["content-length"]), len(apk_bytes))
        head = self.client.head(f"/app/downloads/{name}")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-type"], apk.headers["content-type"])
        self.assertEqual(head.headers["content-length"], apk.headers["content-length"])

    def test_invalid_manifest_and_non_versioned_downloads_fail_closed(self):
        response = self.client.get("/app/update.json")
        self.assertEqual(response.status_code, 404)
        for name in ("../update.json", "latest.apk", "ByMeVPN-v1.2.apk", "ByMeVPN-v1.2.3.apk/.."):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(f"/app/downloads/{name}").status_code, 404)

        name, _ = self.publish_test_artifact()
        manifest = json.loads((self.root / "update.json").read_text(encoding="utf-8"))
        manifest["download_url"] = "https://evil.example/ByMeVPN-v1.2.3.apk"
        (self.root / "update.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get("/app/update.json").status_code, 404)
        self.assertEqual(self.client.get(f"/app/downloads/{name}").status_code, 200)
        manifest["download_url"] = f"https://bymevpn.duckdns.org/app/downloads/{name}"
        manifest["sha256"] = "0" * 64
        (self.root / "update.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get("/app/update.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
