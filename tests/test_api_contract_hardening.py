"""Contract regression tests: never trust prices, days or fake identity tokens."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import api_router
from constants import VALID_DEVICE_LIMITS, get_price_for_months


class CheckoutValidationTests(unittest.TestCase):
    def test_all_canonical_windows_and_android_plans(self):
        for plan, months, android in [
            ("month", 1, "month1"),
            ("quarter", 3, "month4"),
            ("half_year", 6, "month8"),
            ("year", 12, "month15"),
        ]:
            for devices in VALID_DEVICE_LIMITS:
                price, days = get_price_for_months(months, devices)
                with self.subTest(plan=plan, devices=devices):
                    expected = (price, days, devices)
                    self.assertEqual(api_router.resolve_checkout_terms(plan, devices, price, days), expected)
                    android_code = f"plan_{android}_{devices}dev"
                    self.assertEqual(api_router.resolve_checkout_terms(android_code, devices, price, days), expected)

    def test_tampered_price_duration_and_device_count_rejected(self):
        price, days = get_price_for_months(12, 2)
        for code, devices, amount, duration in [
            ("year", 2, 1, days),
            ("year", 2, price, days + 365),
            ("year", 100, price, days),
            ("unknown", 2, price, days),
            ("plan_month15_5dev", 2, price, days),
        ]:
            with self.subTest(code=code, devices=devices, amount=amount, duration=duration):
                with self.assertRaises(HTTPException) as context:
                    api_router.resolve_checkout_terms(code, devices, amount, duration)
                self.assertEqual(context.exception.status_code, 422)

    def test_no_predictable_jwt_secret(self):
        with patch.object(api_router, "JWT_SECRET", ""):
            with self.assertRaises(HTTPException) as context:
                api_router.create_simple_jwt(42, "test@example.com")
            self.assertEqual(context.exception.status_code, 503)
            self.assertIsNone(api_router.verify_simple_jwt("42:test@example.com:9999999999:forged"))


class AuthenticationHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_google_id_token_never_creates_account(self):
        with self.assertRaises(HTTPException) as context:
            await api_router.login_google(api_router.GoogleLoginRequest(id_token="a.b.c"))
        self.assertEqual(context.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
