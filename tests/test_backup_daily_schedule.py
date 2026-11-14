"""Regressions for daily backup cadence and failure backoff."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from notifications import _database_backup_scheduler


class BackupSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_runs_after_each_calendar_wait_without_extra_daily_sleep(self):
        with (
            patch(
                "notifications._wait_until_03_00",
                new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
            ) as wait,
            patch("notifications._backup_database", new=AsyncMock()) as backup,
            patch("notifications.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _database_backup_scheduler(object())

        self.assertEqual(wait.await_count, 3)
        self.assertEqual(backup.await_count, 2)
        sleep.assert_not_awaited()

    async def test_unexpected_error_backs_off_before_next_calendar_wait(self):
        with (
            patch(
                "notifications._wait_until_03_00",
                new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
            ) as wait,
            patch(
                "notifications._backup_database",
                new=AsyncMock(side_effect=[RuntimeError("offline"), None]),
            ) as backup,
            patch("notifications.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _database_backup_scheduler(object())

        self.assertEqual(wait.await_count, 3)
        self.assertEqual(backup.await_count, 2)
        sleep.assert_awaited_once_with(300)

    async def test_expiry_reminders_wait_until_ten_each_day(self):
        from notifications import _expiry_notification_scheduler

        with (
            patch(
                "notifications._wait_until_10_00_moscow",
                new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
            ) as wait,
            patch("notifications._send_expiry_notifications", new=AsyncMock()) as deliver,
            patch("notifications.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _expiry_notification_scheduler(object())

        self.assertEqual(wait.await_count, 3)
        self.assertEqual(deliver.await_count, 2)
        sleep.assert_not_awaited()

    async def test_expiry_reminder_failure_backoff(self):
        from notifications import _expiry_notification_scheduler

        with (
            patch(
                "notifications._wait_until_10_00_moscow",
                new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
            ) as wait,
            patch(
                "notifications._send_expiry_notifications",
                new=AsyncMock(side_effect=[RuntimeError("temporary failure"), None]),
            ) as deliver,
            patch("notifications.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _expiry_notification_scheduler(object())

        self.assertEqual(wait.await_count, 3)
        self.assertEqual(deliver.await_count, 2)
        sleep.assert_awaited_once_with(300)
