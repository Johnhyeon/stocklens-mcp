"""SL-02: 미국 차트에도 완성 봉 메타 v3 를 적용한다.

실측(UAT 2026-08-27): 미국 정규장 중 get_us_chart 일봉의 마지막 행이 진행
중인데 data_completeness=complete 로 반환됐다. 자연어 경고는 있었지만
bar_state / last_completed_bar_date / coverage.reason=incomplete_tail 이 없어
프로그램이 미완성 봉을 구조적으로 걸러낼 수 없었다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import market_clock as mc
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


def _bar(d, c=100.0, v=1000):
    return {"date": d, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": v}


def _et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=mc.ET)


_US_OPEN = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": True, "status": "regular", "last_trading_day": "2026-08-26"},
}
_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


class UsCalendarTests(unittest.TestCase):
    """미국 거래소 달력 - 정규 마감·조기 마감·연장(프리·포스트) 마감."""

    def test_daily_bar_closes_at_regular_close(self):
        cal = mc.us_calendar()
        self.assertFalse(cal.is_period_closed(date(2026, 8, 26), "day",
                                              _et(2026, 8, 26, 14)))
        self.assertTrue(cal.is_period_closed(date(2026, 8, 26), "day",
                                             _et(2026, 8, 26, 16, 30)))

    def test_early_close_day_closes_at_one_pm(self):
        """추수감사절 다음날(2026-11-27)은 13:00 조기 마감이다."""
        cal = mc.us_calendar()
        self.assertTrue(cal.is_period_closed(date(2026, 11, 27), "day",
                                             _et(2026, 11, 27, 13, 30)))

    def test_extended_close_waits_for_after_hours(self):
        """프리·포스트를 포함하면 20:00 이 지나야 그 날이 끝난다."""
        cal = mc.us_calendar(include_extended=True)
        self.assertFalse(cal.is_period_closed(date(2026, 8, 26), "day",
                                              _et(2026, 8, 26, 17)))
        self.assertTrue(cal.is_period_closed(date(2026, 8, 26), "day",
                                             _et(2026, 8, 26, 20, 30)))

    def test_us_week_closes_on_friday(self):
        cal = mc.us_calendar()
        self.assertFalse(cal.is_period_closed(date(2026, 8, 24), "week",
                                              _et(2026, 8, 26, 17)))
        self.assertTrue(cal.is_period_closed(date(2026, 8, 24), "week",
                                             _et(2026, 8, 28, 17)))


class UsChartBarStateTests(unittest.IsolatedAsyncioTestCase):
    """get_us_chart 가 국내 차트와 같은 bar_state 계약을 따르는가."""

    DAILY = [_bar("2026-08-24"), _bar("2026-08-25"), _bar("2026-08-26")]

    def _patches(self, rows, clock, now):
        return (
            patch.object(server.us, "get_history", AsyncMock(return_value=rows)),
            patch.object(server, "build_market_clock", return_value=clock),
            patch.object(server, "_now_kst", return_value=now),
        )

    async def _chart(self, rows, clock, now, **kw):
        a, b, c = self._patches(rows, clock, now)
        with a, b, c:
            return await server.get_us_chart(ticker="NVDA", **kw)

    async def test_daily_bar_during_session_is_incomplete(self):
        """수용 1: 장중 NVDA 일봉 - 마지막 봉 미완성, 직전 거래일이 마지막 완성 봉."""
        text = await self._chart(self.DAILY, _US_OPEN, _et(2026, 8, 26, 14))
        meta = extract_meta(text)
        bs = meta["bar_state"]
        self.assertEqual(bs["timeframe"], "day")
        self.assertEqual(bs["last_bar_date"], "2026-08-26")
        self.assertFalse(bs["last_bar_complete"])
        self.assertEqual(bs["last_completed_bar_date"], "2026-08-25")
        self.assertTrue(bs["calculation_includes_incomplete"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "incomplete_tail")

    async def test_daily_bar_after_close_is_complete(self):
        """수용 2: 장 마감 후 같은 일봉은 완성으로 전환된다."""
        text = await self._chart(self.DAILY, _US_CLOSED, _et(2026, 8, 26, 17))
        meta = extract_meta(text)
        self.assertTrue(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["bar_state"]["last_completed_bar_date"], "2026-08-26")
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertNotIn("coverage", meta)

    async def test_weekly_bar_midweek_is_incomplete(self):
        """수용 3a: 주중 주봉. yfinance 주봉은 주 시작일로 찍힌다."""
        rows = [_bar("2026-08-10"), _bar("2026-08-17"), _bar("2026-08-24")]
        text = await self._chart(rows, _US_CLOSED, _et(2026, 8, 26, 17),
                                 interval="1wk")
        meta = extract_meta(text)
        bs = meta["bar_state"]
        self.assertEqual(bs["timeframe"], "week")
        self.assertFalse(bs["last_bar_complete"])
        self.assertEqual(bs["last_completed_bar_date"], "2026-08-17")
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_monthly_bar_midmonth_is_incomplete(self):
        """수용 3b: 월중 월봉."""
        rows = [_bar("2026-06-01"), _bar("2026-07-01"), _bar("2026-08-03")]
        text = await self._chart(rows, _US_CLOSED, _et(2026, 8, 26, 17),
                                 interval="1mo")
        meta = extract_meta(text)
        self.assertFalse(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["bar_state"]["last_completed_bar_date"], "2026-07-01")

    async def test_prepost_flag_does_not_change_daily_completion(self):
        """요구 4: 일봉은 prepost 와 무관하게 정규 마감으로 끝난다.

        yfinance 일봉은 프리·포스트 체결을 포함하지 않는다(prepost 는
        intraday 전용). 16시 이후면 prepost=True 여도 완성이다.
        """
        text = await self._chart(self.DAILY, _US_CLOSED, _et(2026, 8, 26, 17),
                                 prepost=True)
        meta = extract_meta(text)
        self.assertTrue(meta["bar_state"]["last_bar_complete"])

    async def test_intraday_has_no_bar_state(self):
        """intraday 는 이 계약의 대상이 아니다(항상 진행 중 봉이 섞인다)."""
        rows = [{"datetime": "2026-08-26T14:30:00-04:00", "open": 100,
                 "high": 101, "low": 99, "close": 100, "volume": 10}]
        text = await self._chart(rows, _US_OPEN, _et(2026, 8, 26, 14, 40),
                                 interval="5m")
        self.assertNotIn("bar_state", extract_meta(text))

    async def test_data_as_of_comes_from_the_last_bar(self):
        """기준일은 시장 달력 역산이 아니라 실제 마지막 봉이다."""
        rows = [_bar("2026-08-21"), _bar("2026-08-24")]   # 이틀 뒤처진 데이터
        text = await self._chart(rows, _US_CLOSED, _et(2026, 8, 26, 17))
        self.assertEqual(extract_meta(text)["data_as_of"], "2026-08-24")


if __name__ == "__main__":
    unittest.main(verbosity=2)
