"""Safety regressions for customer chat screens and destructive VPN operations.

Tests are intentionally based on externally observable behavior rather than
implementation details: a failed Telegram send must not erase the old screen,
and a partially failed VPN delete must never claim success.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers.fallback as fallback
import handlers.keys as keys
import xui_client
from utils import send_or_edit


class CustomerChatSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_text_never_deletes_previous_messages(self):
        message = SimpleNamespace(
            text="не понял", from_user=SimpleNamespace(id=123, username=None, first_name="Иван"),
            chat=SimpleNamespace(id=123), message_id=8, answer=AsyncMock(),
        )
        bot = SimpleNamespace(delete_message=AsyncMock())
        with patch("database.ensure_user", new=AsyncMock()), patch(
            "handlers.start._send_main_menu", new=AsyncMock()
        ):
            await fallback.fallback_text(message, bot, object())
        bot.delete_message.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertIn("меню", message.answer.await_args.args[0].lower())

    async def test_failed_photo_to_text_transition_preserves_original_screen(self):
        old = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=55, photo=[object()])
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram unavailable")),
                              delete_message=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
            await send_or_edit(bot, old, "Новый экран")
        bot.delete_message.assert_not_awaited()

    async def test_successful_photo_to_text_sends_before_deleting(self):
        events = []
        async def send(**kwargs):
            events.append("sent")
            return "new"
        async def delete(*args):
            events.append("deleted")
        old = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=55, photo=[object()])
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                              delete_message=AsyncMock(side_effect=delete))
        self.assertEqual(await send_or_edit(bot, old, "Новый экран"), "new")
        self.assertEqual(events, ["sent", "deleted"])


class VPNDeletionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def _delete_with_panel(self, initial: dict[int, bool], failures: tuple[int, ...] = (), lookup_uuid: str | None = "uuid"):
        active = dict(initial)
        async def remove(iid, uuid):
            if iid in failures:
                raise RuntimeError("partial panel failure")
            active[iid] = False
        async def get_by_id(iid):
            return SimpleNamespace(client_stats=[SimpleNamespace(email="123", uuid="uuid")] if active[iid] else [])
        api = SimpleNamespace(client=SimpleNamespace(delete=remove),
                              inbound=SimpleNamespace(get_by_id=get_by_id))
        async def direct(method, *args, **kwargs):
            return await method(*args, **kwargs)
        with (
            patch.object(xui_client, "XUI_INBOUND_IDS", [12, 15]),
            patch.object(xui_client, "_get_api", new=AsyncMock(return_value=api)),
            patch.object(xui_client, "_find_client_uuid", new=AsyncMock(return_value=lookup_uuid)),
            patch.object(xui_client, "_api_call_with_retry", new=direct),
        ):
            return await xui_client.delete_xui_user(123)

    async def test_partial_panel_delete_with_remaining_inbound_is_not_success(self):
        self.assertFalse(await self._delete_with_panel({12: True, 15: True}, failures=(15,)))

    async def test_only_reports_success_when_every_inbound_is_clear(self):
        self.assertTrue(await self._delete_with_panel({12: True, 15: True}))

    async def test_no_uuid_does_not_hide_a_remaining_panel_client(self):
        self.assertFalse(await self._delete_with_panel({12: True, 15: False}, lookup_uuid=None))

    async def test_all_inbounds_empty_is_retry_safe(self):
        self.assertTrue(await self._delete_with_panel({12: False, 15: False}, lookup_uuid=None))

    async def _handler_delete(self, *, remote: bool, db_deleted: bool):
        callback = SimpleNamespace(
            data="key_delete_confirm:7", from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        with (
            patch.object(keys, "safe_answer", new=AsyncMock()),
            patch.object(keys, "get_key_by_id", new=AsyncMock(return_value={
                "id": 7, "user_id": 123, "remark": "ByMeVPN", "expiry": 1900000000
            })),
            patch.object(keys, "get_user_keys", new=AsyncMock(return_value=[{"id": 7}])),
            patch("database.get_auto_renew_subscription", new=AsyncMock(return_value={"status": "active"})),
            patch("database.cancel_auto_renew", new=AsyncMock(return_value={"cancelled": True})),
            patch("database.resume_auto_renew", new=AsyncMock()) as resume,
            patch.object(keys, "delete_xui_user", new=AsyncMock(return_value=remote)),
            patch.object(keys, "delete_key_by_id", new=AsyncMock(return_value=db_deleted)) as delete_db,
            patch("database.log_key_error", new=AsyncMock()),
        ):
            await keys.cb_key_delete_confirm(callback, object())
        return callback.message.answer, resume, delete_db

    async def test_partial_panel_failure_never_reenables_autopay(self):
        answer, resume, delete_db = await self._handler_delete(remote=False, db_deleted=True)
        resume.assert_not_awaited()
        delete_db.assert_not_awaited()
        self.assertTrue(answer.await_args)

    async def test_db_failure_after_panel_delete_never_reenables_autopay(self):
        answer, resume, delete_db = await self._handler_delete(remote=True, db_deleted=False)
        resume.assert_not_awaited()
        delete_db.assert_awaited_once_with(7)
        self.assertIn("автопродление", answer.await_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()
