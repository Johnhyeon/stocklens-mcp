"""세션 창(session window) 테스트 (Task 9). 기존 market_clock 캘린더 재사용."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.sessions import session_window

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


class KrSessionTests(unittest.TestCase):
    def test_regular_trading_day(self):
        win = session_window("KR", date(2026, 8, 27))
        self.assertEqual(win.open_at, datetime(2026, 8, 27, 9, 0, tzinfo=KST))
        self.assertEqual(win.close_at, datetime(2026, 8, 27, 15, 30, tzinfo=KST))
        self.assertEqual(win.minutes, 390)

    def test_holiday_returns_none(self):
        # 2026-09-24 추석 (market_clock 캘린더 기준)
        self.assertIsNone(session_window("KR", date(2026, 9, 24)))

    def test_weekend_returns_none(self):
        self.assertIsNone(session_window("KR", date(2026, 8, 29)))  # 토요일


class UsSessionTests(unittest.TestCase):
    def test_regular_trading_day(self):
        win = session_window("US", date(2026, 8, 26))
        self.assertEqual(win.open_at, datetime(2026, 8, 26, 9, 30, tzinfo=NY))
        self.assertEqual(win.close_at, datetime(2026, 8, 26, 16, 0, tzinfo=NY))
        self.assertEqual(win.minutes, 390)

    def test_early_close_day(self):
        # 추수감사절 다음날(2026-11-27)은 13:00 조기 폐장.
        win = session_window("US", date(2026, 11, 27))
        self.assertEqual(win.close_at.hour, 13)
        self.assertEqual(win.minutes, 210)

    def test_dst_transition_offsets(self):
        # 2026-03-08 에 서머타임 시작. 전후 거래일의 UTC offset 이 달라진다.
        before = session_window("US", date(2026, 3, 6))
        after = session_window("US", date(2026, 3, 9))
        self.assertEqual(before.open_at.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(after.open_at.utcoffset().total_seconds(), -4 * 3600)
        # 현지 시각은 둘 다 09:30 이다.
        self.assertEqual(before.open_at.hour, 9)
        self.assertEqual(after.open_at.hour, 9)

    def test_us_holiday_returns_none(self):
        self.assertIsNone(session_window("US", date(2026, 12, 25)))


class InvalidInputTests(unittest.TestCase):
    def test_unknown_market(self):
        with self.assertRaises(ValueError):
            session_window("JP", date(2026, 8, 27))


if __name__ == "__main__":
    unittest.main()
