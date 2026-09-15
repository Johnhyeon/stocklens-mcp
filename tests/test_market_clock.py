import asyncio
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo


class MarketClockTests(unittest.TestCase):
    def test_krx_regular_session_on_trading_day(self) -> None:
        from stock_mcp_server.market_clock import get_market_clock

        clock = get_market_clock(datetime(2026, 5, 7, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")))

        self.assertEqual(clock["krx"]["status"], "regular")
        self.assertEqual(clock["krx"]["last_trading_day"], "2026-05-07")
        self.assertEqual(clock["krx"]["next_trading_day"], "2026-05-08")
        self.assertFalse(clock["krx"]["is_holiday"])

    def test_krx_weekend_uses_previous_and_next_trading_days(self) -> None:
        from stock_mcp_server.market_clock import get_market_clock

        clock = get_market_clock(datetime(2026, 5, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")))

        self.assertEqual(clock["krx"]["status"], "closed_weekend")
        self.assertEqual(clock["krx"]["last_trading_day"], "2026-05-08")
        self.assertEqual(clock["krx"]["next_trading_day"], "2026-05-11")

    def test_krx_known_holiday_is_closed(self) -> None:
        from stock_mcp_server.market_clock import get_market_clock

        clock = get_market_clock(datetime(2026, 5, 5, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")))

        self.assertEqual(clock["krx"]["status"], "closed_holiday")
        self.assertTrue(clock["krx"]["is_holiday"])
        self.assertIn("Children", clock["krx"]["reason"])

    def test_us_pre_market_status_from_kst_time(self) -> None:
        from stock_mcp_server.market_clock import get_market_clock

        clock = get_market_clock(datetime(2026, 5, 7, 20, 0, tzinfo=ZoneInfo("Asia/Seoul")))

        self.assertEqual(clock["us"]["status"], "pre_market")
        self.assertEqual(clock["us"]["last_trading_day"], "2026-05-06")
        self.assertEqual(clock["us"]["next_trading_day"], "2026-05-07")

    def test_us_known_holiday_is_closed(self) -> None:
        from stock_mcp_server.market_clock import get_market_clock

        clock = get_market_clock(datetime(2026, 7, 3, 12, 0, tzinfo=ZoneInfo("America/New_York")))

        self.assertEqual(clock["us"]["status"], "closed_holiday")
        self.assertTrue(clock["us"]["is_holiday"])
        self.assertIn("Independence", clock["us"]["reason"])

    def test_mcp_tool_returns_text_with_json_payload(self) -> None:
        from stock_mcp_server.server import get_market_clock as tool_get_market_clock

        text = asyncio.run(tool_get_market_clock())

        self.assertIn("MARKET_CLOCK_JSON_START", text)
        payload = text.split("MARKET_CLOCK_JSON_START", 1)[1].split("MARKET_CLOCK_JSON_END", 1)[0].strip()
        data = json.loads(payload)
        self.assertIn("krx", data)
        self.assertIn("us", data)


KST = ZoneInfo("Asia/Seoul")


class KrxExtendedSessionTests(unittest.TestCase):
    """2026-09-14 부터 KRX 애프터마켓(16:00~20:00). 프리마켓은 아직 시행 전."""

    def _krx(self, *args):
        from stock_mcp_server.market_clock import get_market_clock

        return get_market_clock(datetime(*args, tzinfo=KST))["krx"]

    def test_after_market_is_its_own_session_not_regular(self) -> None:
        krx = self._krx(2026, 9, 15, 17, 0)

        self.assertEqual(krx["status"], "after_hours")
        self.assertEqual(krx["current_session"], "after_market")
        self.assertFalse(krx["is_open"])  # is_open 은 정규장만 뜻한다
        self.assertTrue(krx["extended_session_open"])
        self.assertEqual(krx["session_closes_in"], "3시간")
        self.assertEqual(krx["last_trading_day"], "2026-09-15")
        self.assertEqual([s["name"] for s in krx["sessions"]], ["regular", "after_market"])

    def test_gap_between_regular_close_and_after_market(self) -> None:
        krx = self._krx(2026, 9, 15, 15, 45)

        self.assertEqual(krx["status"], "closed_after_hours")
        self.assertIsNone(krx["current_session"])
        self.assertEqual(krx["next_session"]["name"], "after_market")
        self.assertEqual(krx["next_session"]["opens_in"], "15분")

    def test_all_sessions_closed_at_20(self) -> None:
        krx = self._krx(2026, 9, 15, 20, 0)

        self.assertEqual(krx["status"], "closed_after_hours")
        self.assertIsNone(krx["current_session"])
        self.assertIsNone(krx["next_session"])
        self.assertEqual(krx["next_trading_day"], "2026-09-16")

    def test_no_after_market_before_start_date(self) -> None:
        krx = self._krx(2026, 9, 11, 17, 0)

        self.assertEqual(krx["status"], "closed_after_hours")
        self.assertIsNone(krx["current_session"])
        self.assertEqual([s["name"] for s in krx["sessions"]], ["regular"])

    def test_no_krx_pre_market_until_announced(self) -> None:
        for hm in ((7, 30), (8, 40)):
            krx = self._krx(2026, 9, 16, *hm)
            self.assertEqual(krx["status"], "closed_before_open", hm)
            self.assertIsNone(krx["current_session"], hm)

    def test_pre_market_appears_only_from_effective_date(self) -> None:
        from datetime import date
        from unittest.mock import patch

        from stock_mcp_server import market_clock

        with patch.object(market_clock, "KRX_PRE_MARKET_START", date(2026, 9, 16)):
            self.assertEqual(self._krx(2026, 9, 16, 7, 30)["current_session"], "pre_market")
            gap = self._krx(2026, 9, 16, 7, 55)
            self.assertEqual(gap["status"], "closed_before_open")
            self.assertIsNone(gap["current_session"])
            self.assertIsNone(self._krx(2026, 9, 15, 7, 30)["current_session"])

    def test_holiday_has_no_session(self) -> None:
        krx = self._krx(2026, 9, 24, 17, 0)  # 추석

        self.assertEqual(krx["status"], "closed_holiday")
        self.assertIsNone(krx["current_session"])
        self.assertFalse(krx["extended_session_open"])

    def test_session_now_helper(self) -> None:
        from stock_mcp_server.market_clock import krx_session_now

        self.assertEqual(krx_session_now(datetime(2026, 9, 15, 10, 0, tzinfo=KST)), "regular")
        self.assertEqual(krx_session_now(datetime(2026, 9, 15, 16, 30, tzinfo=KST)), "after_market")
        self.assertIsNone(krx_session_now(datetime(2026, 9, 15, 15, 45, tzinfo=KST)))
        self.assertIsNone(krx_session_now(datetime(2026, 9, 19, 17, 0, tzinfo=KST)))  # 토요일
        self.assertIsNone(krx_session_now(datetime(2026, 9, 11, 17, 0, tzinfo=KST)))  # 시행 전

    def test_extended_calendar_closes_daily_bar_at_20(self) -> None:
        from datetime import date

        from stock_mcp_server.market_clock import krx_calendar

        ext, reg = krx_calendar(include_extended=True), krx_calendar()
        self.assertEqual(ext.close_datetime(date(2026, 9, 15)).strftime("%H:%M"), "20:00")
        self.assertEqual(reg.close_datetime(date(2026, 9, 15)).strftime("%H:%M"), "15:30")
        self.assertEqual(ext.close_datetime(date(2026, 9, 11)).strftime("%H:%M"), "15:30")
        at_1700 = datetime(2026, 9, 15, 17, 0, tzinfo=KST)
        self.assertFalse(ext.is_period_closed(date(2026, 9, 15), "day", at_1700))
        self.assertTrue(reg.is_period_closed(date(2026, 9, 15), "day", at_1700))

    def test_regular_window_for_intraday_is_unchanged(self) -> None:
        from datetime import date

        from stock_mcp_server.market_data.sessions import session_window

        w = session_window("KR", date(2026, 9, 15))
        self.assertEqual((w.open_at.strftime("%H:%M"), w.close_at.strftime("%H:%M")), ("09:00", "15:30"))

    def test_text_names_after_market_and_warns_against_mixing(self) -> None:
        from stock_mcp_server.market_clock import format_market_clock, get_market_clock

        text = format_market_clock(get_market_clock(datetime(2026, 9, 15, 17, 0, tzinfo=KST)))
        self.assertIn("한국장: 애프터마켓", text)
        self.assertIn("정규장 09:00~15:30 · 애프터마켓 16:00~20:00 · 프리마켓 미시행", text)
        self.assertIn("정규장 수치와 섞지 마세요", text)
        gap = format_market_clock(get_market_clock(datetime(2026, 9, 15, 15, 45, tzinfo=KST)))
        self.assertIn("애프터마켓 개장까지 15분 남음", gap)


if __name__ == "__main__":
    unittest.main()
