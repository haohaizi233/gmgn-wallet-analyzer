from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.domain.enums import ReportPeriod
from app.utils.time_utils import LOCAL_TZ, now_ts, period_window


class PeriodWindowTests(unittest.TestCase):
    def test_d1_is_rolling_24h_and_keeps_22h_ago_rows(self):
        now = datetime.now(tz=LOCAL_TZ)
        end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if end_of_today < now:
            end_of_today = now
        yesterday_midnight = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts, end_ts = period_window(ReportPeriod.D1, yesterday_midnight, end_of_today)
        current = now_ts()
        twenty_two_h_ago = current - 22 * 3600
        self.assertLess(abs((end_ts - start_ts) - 86400), 3)
        self.assertLess(abs(end_ts - current), 3)
        self.assertGreaterEqual(twenty_two_h_ago, start_ts)
        self.assertLessEqual(twenty_two_h_ago, end_ts)

    def test_d1_excludes_36h_ago(self):
        start_ts, end_ts = period_window(ReportPeriod.D1)
        current = now_ts()
        thirty_six_h_ago = current - 36 * 3600
        self.assertLess(thirty_six_h_ago, start_ts)
        self.assertLess(abs((end_ts - start_ts) - 86400), 3)

    def test_d1_ignores_future_end_of_day(self):
        now = datetime.now(tz=LOCAL_TZ)
        future_eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if future_eod <= now:
            future_eod = now + timedelta(minutes=1)
        start_ts, end_ts = period_window(ReportPeriod.D1, now - timedelta(days=1), future_eod)
        yesterday_eod = int(future_eod.timestamp()) - 86400
        self.assertNotEqual(start_ts, yesterday_eod)
        self.assertLess(abs(end_ts - now_ts()), 3)

    def test_custom_keeps_calendar_bounds(self):
        start = datetime(2026, 9, 18, tzinfo=LOCAL_TZ)
        end = datetime(2026, 9, 19, 23, 59, 59, tzinfo=LOCAL_TZ)
        start_ts, end_ts = period_window(ReportPeriod.CUSTOM, start, end)
        self.assertEqual(start_ts, int(start.timestamp()))
        self.assertEqual(end_ts, int(end.timestamp()))
