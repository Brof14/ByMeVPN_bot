import unittest
from unittest.mock import patch

import webhook
from fastapi.testclient import TestClient


class _FakeConfig:
    app = None

    def __init__(self, app, **kwargs):
        type(self).app = app


class _FakeServer:
    def __init__(self, config):
        self.config = config

    async def serve(self):
        return None


class ConnectionTlsAssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_tls_app_serves_logo_and_allowlisted_icons(self):
        with (
            patch.object(webhook, "APP_CONNECT_TLS_CERT_FILE", "/unused/cert.pem"),
            patch.object(webhook, "APP_CONNECT_TLS_KEY_FILE", "/unused/key.pem"),
            patch.object(webhook.uvicorn, "Config", _FakeConfig),
            patch.object(webhook.uvicorn, "Server", _FakeServer),
        ):
            await webhook.start_connection_tls_server()

        client = TestClient(_FakeConfig.app)
        logo = client.get("/app/logo")
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/png")
        self.assertEqual(logo.headers["x-content-type-options"], "nosniff")

        for name in (
            "android.png", "apple.png", "happ.png", "incy.png", "koala.png", "linux.png",
            "v2rayn.png", "windows.png",
        ):
            with self.subTest(name=name):
                response = client.get(f"/app/assets/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["cache-control"], "public, max-age=86400")

        self.assertEqual(client.get("/app/assets/unknown.png").status_code, 404)
        self.assertEqual(client.get("/app/assets/../update.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
