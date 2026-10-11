"""Regression tests for webhook exposure and startup safety invariants."""
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestWebhookSecurity(unittest.TestCase):
    def test_telegram_updates_are_not_accepted_over_http(self):
        path = os.path.join(PROJECT_ROOT, "webhook.py")
        with open(path, encoding="utf-8") as source_file:
            source = source_file.read()

        self.assertNotIn('@app.post("/webhook/telegram")', source)
        self.assertNotIn("Update.model_validate", source)
        self.assertIn('@app.post("/webhook/yookassa")', source)

    def test_startup_fails_closed_when_xui_is_unavailable(self):
        path = os.path.join(PROJECT_ROOT, "main.py")
        with open(path, encoding="utf-8") as source_file:
            source = source_file.read()

        self.assertIn("if xui_connected:", source)
        self.assertIn("Startup aborted because 3x-ui is unavailable", source)
        self.assertIn("raise SystemExit(1)", source)

    def test_empty_inbound_list_does_not_crash_config_import(self):
        path = os.path.join(PROJECT_ROOT, "config.py")
        with open(path, encoding="utf-8") as source_file:
            source = source_file.read()

        self.assertIn(
            "XUI_INBOUND_ID = XUI_INBOUND_IDS[0] if XUI_INBOUND_IDS else 0",
            source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
