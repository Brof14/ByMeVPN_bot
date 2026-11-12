"""HTTP-level regression tests for the mobile device-tier catalog contract."""
import os
import secrets
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DB_FILE", "/tmp/test_api_plans_http_contract.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
from constants import get_price_for_months


class TestPlansHttpContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(api_router.api_router, prefix="/api/v1")
        cls.client = TestClient(app)

    def test_selected_device_tier_is_preserved_in_http_response(self):
        expected_codes = {"1m", "3m", "6m", "12m"}
        for devices in (2, 5, 10):
            with self.subTest(devices=devices):
                response = self.client.get(f"/api/v1/plans?devices={devices}")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(set(plan["code"] for plan in payload["plans"]), expected_codes)
                self.assertEqual(payload["trial"]["price_rub"], 0)
                for plan, months in zip(payload["plans"], (1, 3, 6, 12)):
                    expected_price, expected_days = get_price_for_months(months, devices)
                    self.assertEqual(plan["devices"], devices)
                    self.assertEqual(plan["price_rub"], expected_price)
                    self.assertEqual(plan["days"], expected_days)
                    self.assertEqual(
                        {option["devices"] for option in plan["device_options"]},
                        {2, 5, 10},
                    )

    def test_discount_is_computed_for_each_device_tier(self):
        from constants import TARIFF_DAYS
        for devices in (2, 5, 10):
            response = self.client.get(f"/api/v1/plans?devices={devices}")
            self.assertEqual(response.status_code, 200)
            baseline = get_price_for_months(1, devices)[0]
            for plan, months in zip(response.json()["plans"], (1, 3, 6, 12)):
                expected = round(
                    100 * (1 - plan["price_rub"] / (baseline * (TARIFF_DAYS[months] // 30)))
                )
                self.assertEqual(plan["discount_percent"], expected)

    def test_unsupported_device_tier_is_rejected_over_http(self):
        response = self.client.get("/api/v1/plans?devices=3")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
