"""Admin navigation regression tests; no production database required."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.admin import (
    _main_kb, _clients_kb, _finances_kb, _growth_kb,
    _operations_kb, cb_admin_clients_menu,
)


def actions(keyboard):
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]


class AdminNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_promo_value_rejects_nonfinite_and_fractional_days(self):
        from handlers.admin import receive_promo_value
        for value, promo_type in (("nan", "percent"), ("2.5", "days"), ("3.5", "fixed")):
            message = SimpleNamespace(from_user=SimpleNamespace(id=1), text=value, answer=AsyncMock())
            state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_type": promo_type}), update_data=AsyncMock(), set_state=AsyncMock())
            with patch("handlers.admin._is_admin", return_value=True):
                await receive_promo_value(message, state)
            state.update_data.assert_not_awaited()
            state.set_state.assert_not_awaited()
            message.answer.assert_awaited_once()
    def test_home_is_task_focused(self):
        home = _main_kb()
        self.assertEqual(len(home.inline_keyboard), 5)
        self.assertEqual(
            actions(home),
            ["admin_stats", "admin_clients_menu", "admin_finance_menu", "admin_growth", "admin_ops_menu"],
        )

    def test_all_previous_admin_actions_stay_reachable(self):
        available = set()
        for menu in (_main_kb(), _clients_kb(), _finances_kb(), _growth_kb(), _operations_kb()):
            available.update(actions(menu))
        required = {
            "admin_stats", "admin_users:0", "admin_all_keys:0", "admin_payments",
            "admin_broadcast", "admin_finance", "admin_refunds", "admin_growth",
            "admin_system", "admin_key_errors:0", "admin_export_csv",
            "admin_logs:0", "admin_mass_trial", "admin_mass_days",
            "admin_cleanup", "admin_settings", "admin_search",
        }
        self.assertFalse(required - available, required - available)

    async def test_unauthorized_user_cannot_open_admin_sections(self):
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=999), message=message)
        with (
            patch("handlers.admin._is_admin", return_value=False),
            patch("handlers.admin.safe_answer", new=AsyncMock()) as answer,
        ):
            await cb_admin_clients_menu(callback)
        message.edit_text.assert_not_awaited()
        answer.assert_awaited_once_with(callback, "Нет доступа.", alert=True)


if __name__ == "__main__":
    unittest.main()
