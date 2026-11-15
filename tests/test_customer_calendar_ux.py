"""Customer-facing expiry and commercial calendar tests."""
import unittest
from datetime import datetime, timedelta, timezone

from constants import format_expiry_msk
from database import _moscow_calendar_starts

MOSCOW = timezone(timedelta(hours=3))


class CalendarUxTests(unittest.TestCase):
    def test_expiry_displays_explicit_moscow_time(self):
        timestamp = int(datetime(2026, 1, 1, 22, 59, tzinfo=timezone.utc).timestamp())
        self.assertEqual(format_expiry_msk(timestamp), "02.01.2026 01:59 МСК")

    def test_day_week_month_starts_follow_calendar_not_rolling_month(self):
        current = int(datetime(2026, 11, 15, 20, 0, tzinfo=MOSCOW).timestamp())
        day, week, month = _moscow_calendar_starts(current)
        self.assertEqual(datetime.fromtimestamp(day, tz=MOSCOW), datetime(2026, 11, 15, tzinfo=MOSCOW))
        self.assertEqual(datetime.fromtimestamp(week, tz=MOSCOW), datetime(2026, 11, 9, tzinfo=MOSCOW))
        self.assertEqual(datetime.fromtimestamp(month, tz=MOSCOW), datetime(2026, 11, 1, tzinfo=MOSCOW))


if __name__ == "__main__":
    unittest.main()
