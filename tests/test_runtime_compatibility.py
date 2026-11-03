"""Guard the compatibility surface required by the deployed mobile runtime."""
import os
import secrets
import unittest


# The router validates JWT configuration at import time. This is an isolated
# test-only value and is never used by a deployed service.
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
import database
import telegram_post_compat
from scripts.check_runtime_bundle import main as check_runtime_bundle


class TestRuntimeCompatibility(unittest.TestCase):
    def test_image_build_guard_accepts_current_bundle(self):
        check_runtime_bundle()

    def test_container_modules_export_required_database_helpers(self):
        required = (
            "create_mobile_google_nonce",
            "consume_mobile_google_nonce",
            "get_or_create_google_account",
            "create_mobile_trial_bootstrap",
            "get_mobile_trial_bootstrap",
            "delete_mobile_trial_bootstrap",
        )
        missing = [name for name in required if not callable(getattr(database, name, None))]
        self.assertEqual(missing, [])

    def test_telegram_compatibility_adapter_is_importable(self):
        self.assertTrue(callable(telegram_post_compat.register_telegram_check_post))

    def test_api_router_keeps_legacy_get_and_registers_post(self):
        route_methods = {}
        for route in api_router.api_router.routes:
            route_methods.setdefault(route.path, set()).update(route.methods or ())
        session_path = "/auth/telegram/check-session"
        self.assertIn("GET", route_methods.get(session_path, set()))
        self.assertIn("POST", route_methods.get(session_path, set()))
        self.assertIn("POST", route_methods.get("/auth/google/nonce", set()))


if __name__ == "__main__":
    unittest.main()
