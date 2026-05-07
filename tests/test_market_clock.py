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


if __name__ == "__main__":
    unittest.main()
