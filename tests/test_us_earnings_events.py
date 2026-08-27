"""SL-05: 미국 실적 사건 품질 묶음.

실측(UAT): EPS 예상·실제만 있고 매출·현금흐름·발표 시각이 한 사건으로
연결되지 않았고, 장전(BMO)·장후(AMC) 발표에 맞는 반응창을 만들 수 없었다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import market_clock as mc
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}
_EVENING = datetime(2026, 8, 26, 21, tzinfo=mc.ET)


class SessionClassifierTests(unittest.TestCase):
    def test_sessions_from_et_timestamp(self):
        cls = server._earnings_session
        self.assertEqual(cls("2026-08-19T08:00:00-04:00"), "pre_market")
        self.assertEqual(cls("2026-08-19T16:30:00-04:00"), "post_market")
        self.assertEqual(cls("2026-08-19T13:00:00-04:00"), "during_market")

    def test_midnight_timestamp_is_unknown(self):
        """자정 정각은 날짜만 있는 것 - 시각을 지어내지 않는다."""
        self.assertEqual(server._earnings_session("2026-08-19T00:00:00-04:00"),
                         "unknown")


def _events_data(revenue_estimates=None):
    return {
        "ticker": "NVDA", "financial_currency": "USD",
        "events": [
            {"timestamp": "2026-08-19T16:30:00-04:00", "date": "2026-08-19",
             "eps_estimate": 1.77, "eps_actual": 1.87, "eps_surprise_pct": 5.54},
            {"timestamp": "2026-05-20T08:00:00-04:00", "date": "2026-05-20",
             "eps_estimate": 1.54, "eps_actual": 1.62, "eps_surprise_pct": 5.32},
        ],
        "revenue_by_period": {"2026-07-31": 100_000_000_000,
                              "2026-04-30": 92_000_000_000},
        "ocf_by_period": {"2026-07-31": 30_000_000_000},
        "revenue_estimates": revenue_estimates or {},
    }


class EarningsEventRecordTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, data):
        legacy = {"ticker": "NVDA", "upcoming": [], "history": []}
        with patch.object(server.us, "get_earnings", AsyncMock(return_value=legacy)), \
             patch.object(server.us, "get_earnings_events",
                          AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_earnings(ticker="NVDA")

    async def test_event_record_joins_eps_revenue_session_period(self):
        """요구 1·2: EPS·매출·발표 시각·세션·회계기간이 한 레코드다."""
        text = await self._run(_events_data(
            revenue_estimates={"2026-07-31": 105_000_000_000}))
        meta = extract_meta(text)
        ev = meta["earnings_events"]["events"][0]
        self.assertEqual(ev["date"], "2026-08-19")
        self.assertEqual(ev["session"], "post_market")
        self.assertEqual(ev["fiscal_period"], "2026-07-31")
        self.assertEqual(ev["eps_actual"], 1.87)
        self.assertEqual(ev["revenue_actual"], 100_000_000_000)
        self.assertEqual(ev["revenue_estimate"], 105_000_000_000)
        self.assertAlmostEqual(ev["revenue_surprise_pct"], -4.76, places=1)
        self.assertEqual(ev["operating_cash_flow"], 30_000_000_000)

    async def test_eps_beat_revenue_miss_is_one_event(self):
        """수용 2: EPS beat + 매출 miss 가 같은 사건에서 식별된다."""
        text = await self._run(_events_data(
            revenue_estimates={"2026-07-31": 105_000_000_000}))
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("EPS beat", body)
        self.assertIn("매출 miss", body)
        ev = extract_meta(text)["earnings_events"]["events"][0]
        self.assertEqual(ev["verdict"], "EPS beat · 매출 miss")

    async def test_missing_revenue_estimate_is_unconfirmed(self):
        """과거 사건의 매출 예상은 공급자가 주지 않는다 - 지어내지 않는다."""
        text = await self._run(_events_data())
        ev = extract_meta(text)["earnings_events"]["events"][0]
        self.assertIsNone(ev["revenue_estimate"])
        self.assertIsNone(ev["revenue_surprise_pct"])
        self.assertIn("매출", ev["verdict"])
        self.assertIn("확인 불가", ev["verdict"])

    async def test_quality_is_unconfirmed_without_guidance(self):
        """수용 3: 가이던스·일회성 근거가 없으면 실적의 질은 확인 불가다."""
        text = await self._run(_events_data())
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("확인 불가", body)
        self.assertIn("8-K", body)     # 공식 filing 으로 연결(요구 3)
        meta = extract_meta(text)
        self.assertFalse(meta["earnings_events"]["quality_assessable"])
        self.assertTrue(any("가이던스" in w for w in meta["warnings"]))

    async def test_ocf_missing_for_event_is_flagged(self):
        data = _events_data()
        data["ocf_by_period"] = {}
        text = await self._run(data)
        ev = extract_meta(text)["earnings_events"]["events"][0]
        self.assertIsNone(ev["operating_cash_flow"])


def _bar(d, c=100.0):
    return {"date": d, "open": c, "high": c + 1, "low": c - 1, "close": c,
            "volume": 1000}


# 2026-08 셋째 주 거래일: 17(월)~21(금), 24(월)~26(수)
WEEK_BARS = [_bar("2026-08-17", 100), _bar("2026-08-18", 102),
             _bar("2026-08-19", 104), _bar("2026-08-20", 108),
             _bar("2026-08-21", 110), _bar("2026-08-24", 112),
             _bar("2026-08-25", 111), _bar("2026-08-26", 115)]


class UsEventReactionTests(unittest.IsolatedAsyncioTestCase):
    """요구 4: 직전 완성봉과 다음 정규장을 쓰는 미국용 event reaction."""

    async def _run(self, session, event_date="2026-08-19", after=3,
                   bars=WEEK_BARS):
        with patch.object(server.us, "get_history", AsyncMock(return_value=bars)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED), \
             patch.object(server, "_now_kst", return_value=_EVENING):
            return await server.get_us_event_reaction(
                ticker="NVDA", event_date=event_date, session=session, after=after)

    async def test_pre_market_uses_the_same_day(self):
        """수용 1a: 장전 발표는 당일이 기준 거래일이다."""
        text = await self._run("pre")
        meta = extract_meta(text)
        ew = meta["event_window"]
        self.assertEqual(ew["basis_trading_date"], "2026-08-19")
        self.assertEqual(ew["basis_selection"], "same_day_pre_market")

    async def test_post_market_uses_the_next_trading_day(self):
        """수용 1b: 장후 발표는 다음 거래일이 기준이다."""
        text = await self._run("post")
        ew = extract_meta(text)["event_window"]
        self.assertEqual(ew["basis_trading_date"], "2026-08-20")
        self.assertEqual(ew["basis_selection"], "next_trading_day_post_market")

    async def test_friday_post_market_lands_on_monday(self):
        text = await self._run("post", event_date="2026-08-21")
        ew = extract_meta(text)["event_window"]
        self.assertEqual(ew["basis_trading_date"], "2026-08-24")

    async def test_returns_are_vs_prior_close(self):
        text = await self._run("pre")
        body = text.split(rmeta.MARKER_START)[0]
        # 기준 8/19(104) vs 전일 8/18(102) -> D0 +1.96%
        self.assertIn("+1.96%", body)

    async def test_short_post_history_is_partial(self):
        text = await self._run("post", event_date="2026-08-25", after=5)
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(meta["coverage"]["reason"],
                         "insufficient_post_event_history")

    async def test_unknown_session_is_conservative_with_warning(self):
        text = await self._run("unknown")
        meta = extract_meta(text)
        self.assertEqual(meta["event_window"]["basis_trading_date"], "2026-08-20")
        self.assertTrue(any("시각" in w for w in meta["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
