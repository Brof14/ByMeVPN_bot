"""Telegram device deletion must use the same owner-scoped panel adapter."""
import os
import secrets
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DB_FILE", "/tmp/test_telegram_device_delete.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import handlers.keys as keys


class TestTelegramDeviceDelete(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def callback(user_id=123):
        return SimpleNamespace(
            data="key_device_delete:7:42",
            from_user=SimpleNamespace(id=user_id),
        )

    async def test_owner_delete_calls_shared_panel_adapter_and_refreshes_list(self):
        callback = self.callback()
        bot = object()
        key = {"id": 7, "user_id": 123}
        with (
            patch.object(keys, "get_key_by_id", new=AsyncMock(return_value=key)),
            patch.object(keys, "delete_xui_hwid_device", new=AsyncMock(return_value=True)) as delete,
            patch.object(keys, "safe_answer", new=AsyncMock()) as answer,
            patch.object(keys, "_key_devices_view", new=AsyncMock(return_value=("fresh", None))),
            patch.object(keys, "send_or_edit", new=AsyncMock()) as refresh,
        ):
            await keys.cb_key_device_delete(callback, bot)

        delete.assert_awaited_once_with(123, 42)
        answer.assert_awaited_once_with(callback, "Устройство удалено")
        refresh.assert_awaited_once_with(
            bot, callback,
            "<b>Устройство удалено</b>\n\n"
            "Место освобождено. Теперь можно подключить другое устройство.\n\nfresh",
            None, photo=keys.DEVICES,
        )

    async def test_foreign_subscription_never_reaches_panel_delete(self):
        callback = self.callback(user_id=999)
        with (
            patch.object(keys, "get_key_by_id", new=AsyncMock(return_value={"id": 7, "user_id": 123})),
            patch.object(keys, "delete_xui_hwid_device", new=AsyncMock()) as delete,
            patch.object(keys, "safe_answer", new=AsyncMock()) as answer,
        ):
            await keys.cb_key_device_delete(callback, object())

        delete.assert_not_awaited()
        answer.assert_awaited_once_with(callback, "Подписка не найдена.", alert=True)

    async def test_panel_failure_does_not_claim_device_was_deleted(self):
        callback = self.callback()
        with (
            patch.object(keys, "get_key_by_id", new=AsyncMock(return_value={"id": 7, "user_id": 123})),
            patch.object(keys, "delete_xui_hwid_device", new=AsyncMock(side_effect=TimeoutError)),
            patch.object(keys, "safe_answer", new=AsyncMock()) as answer,
        ):
            await keys.cb_key_device_delete(callback, object())

        answer.assert_awaited_once_with(
            callback, "Не удалось подтвердить удаление устройства. Попробуйте ещё раз.", alert=True,
        )
