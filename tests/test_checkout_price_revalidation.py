"""Checkout amounts are always server-requoted before provider invoice creation."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.buy import _get_checkout_params


class CheckoutRequoteTests(unittest.IsolatedAsyncioTestCase):
    def state(self, **data):
        return SimpleNamespace(get_data=AsyncMock(return_value=data),
                               update_data=AsyncMock())

    async def test_valid_no_promo_quote_is_preserved(self):
        state = self.state(months=1, devices=2, days=30, price_rub=89)
        params = await _get_checkout_params(state, 12)
        self.assertEqual(params["price_rub"], 89)
        self.assertEqual(params["days"], 30)
        state.update_data.assert_not_awaited()

    async def test_price_change_requires_fresh_confirmation(self):
        state = self.state(months=1, devices=2, days=30, price_rub=89)
        with patch("handlers.buy.get_price_for_months", return_value=(99, 30)):
            self.assertIsNone(await _get_checkout_params(state, 12))
        state.update_data.assert_awaited_once_with(price_rub=None, days=None)

    async def test_changed_days_are_not_silently_charged(self):
        state = self.state(months=1, devices=2, days=35, price_rub=89)
        self.assertIsNone(await _get_checkout_params(state, 12))
        state.update_data.assert_awaited_once_with(price_rub=None, days=None)

    async def test_valid_percent_promo_quote_is_preserved(self):
        promo = {"code": "TEST", "promo_type": "percent", "discount_value": 10,
                 "tariff_binding": None}
        state = self.state(months=1, devices=2, days=30, price_rub=80,
                           promo_code="TEST", promo_info=promo)
        with (patch("handlers.buy.validate_promo_code",
                    new=AsyncMock(return_value=promo)),
              patch("handlers.buy.has_user_used_promo",
                    new=AsyncMock(return_value=False))):
            params = await _get_checkout_params(state, 12)
        self.assertEqual(params["price_rub"], 80)
        state.update_data.assert_not_awaited()

    async def test_changed_discount_requires_fresh_quote(self):
        promo = {"code": "TEST", "promo_type": "percent", "discount_value": 10,
                 "tariff_binding": None}
        state = self.state(months=1, devices=2, days=30, price_rub=79,
                           promo_code="TEST", promo_info=promo)
        with (patch("handlers.buy.validate_promo_code",
                    new=AsyncMock(return_value=promo)),
              patch("handlers.buy.has_user_used_promo",
                    new=AsyncMock(return_value=False))):
            self.assertIsNone(await _get_checkout_params(state, 12))
        state.update_data.assert_awaited_once_with(price_rub=None, days=None)
