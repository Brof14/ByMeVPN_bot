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
