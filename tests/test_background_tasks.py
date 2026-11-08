"""Safe failure alerts and cooldowns for supervised workers."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from background_tasks import (
    _ALERT_COOLDOWN_SECONDS,
    _last_alert,
    _safe_error_type,
    _should_alert,
    _supervise,
)


class BackgroundTaskAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _last_alert.clear()

    def tearDown(self):
        _last_alert.clear()

    def test_first_failure_alerts_and_repeated_failure_is_rate_limited(self):
        self.assertTrue(_should_alert("webhook_server", 10.0))
        self.assertFalse(_should_alert("webhook_server", 10.0 + _ALERT_COOLDOWN_SECONDS - 1))
        self.assertTrue(_should_alert("webhook_server", 10.0 + _ALERT_COOLDOWN_SECONDS))

    def test_error_notice_uses_exception_type_without_message(self):
        error = RuntimeError("sensitive detail must not appear in a bot notice")
        self.assertEqual(_safe_error_type(error), "RuntimeError")

    def test_unusual_dynamic_exception_name_is_replaced(self):
        DynamicError = type("bad<class>", (Exception,), {})
        self.assertEqual(_safe_error_type(DynamicError("not exposed")), "Exception")

    async def test_worker_alert_includes_safe_failure_type_but_not_exception_message(self):
        class BotStub:
            send_message = AsyncMock()

        async def fail():
            raise RuntimeError("private error detail")

        with (
            patch("background_tasks.ADMIN_ID", 123),
            patch("background_tasks.time.monotonic", return_value=10_000.0),
            patch("background_tasks.asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _supervise("webhook_server", fail, BotStub())

        message = BotStub.send_message.await_args.args[1]
        self.assertIn("RuntimeError", message)
        self.assertIn("сбой №1", message)
        self.assertNotIn("private error detail", message)


if __name__ == "__main__":
    unittest.main()
