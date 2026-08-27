"""SL-13: 미국 다종목 단계 진단 배치.

실측(UAT): 고정 15종목의 차트 통계·기술 상태·추정치 변화·서프라이즈를 보려면
종목당 3~4개 단건 도구를 반복 호출해야 했다. 이 배치는 그 판정 재료를 한 번에
돌려주되, 단일 점수로 압축하지 않는다 - 원값과 확인 불가를 그대로 보존한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server
from stock_mcp_server._indicators import _to_df, compute_ma_cross, compute_rsi


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _bars(n: int = 300, base: float = 100.0) -> list[dict]:
    """오름차순 일봉. 사인 없는 완만한 상승 - MA·RSI 가 결정적으로 계산된다."""
    out = []
    for i in range(n):
        y, rest = 2025 + (i // 250), i % 250
        m, d = (rest // 21) + 1, (rest % 21) + 1
        close = base + i * 0.3 + (1.5 if i % 7 == 0 else 0)
        out.append({
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "open": close - 0.5, "high": close + 1, "low": close - 1,
            "close": round(close, 2), "volume": 1_000_000 + i,
        })
    return out


_DONE_STATE = {
    "timeframe": "day", "last_bar_date": "2026-08-26",
    "last_bar_complete": True, "last_completed_bar_date": "2026-08-26",
    "calculation_includes_incomplete": False,
}


class MultiDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tickers, hist_map, bar_state=_DONE_STATE, include=None,
                   estimates=None, earnings=None, short=None):
        async def fake_hist(ticker, **kw):
            return hist_map.get(ticker, [])

        with patch.object(server.us, "get_history", AsyncMock(side_effect=fake_hist)), \
             patch.object(server.us, "get_analyst_estimates",
                          AsyncMock(return_value=estimates)), \
             patch.object(server.us, "get_earnings_events",
                          AsyncMock(return_value=earnings)), \
             patch.object(server.us, "get_short_interest",
                          AsyncMock(return_value=short)), \
             patch.object(server, "_bar_state", return_value=bar_state), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return json.loads(await server.get_us_multi_diagnosis(tickers, include=include))

    async def test_fifteen_tickers_one_call(self):
        """수용 1: 고정 15종목이 반복 단건 호출 없이 한 배치로 나온다."""
        tickers = [f"T{i:02d}" for i in range(15)]
        out = await self._run(tickers, {t: _bars() for t in tickers})
        self.assertEqual(len(out["tickers"]), 15)
        meta = out["_meta"]
        self.assertEqual(meta["coverage"]["returned_entities"], 15)
        self.assertTrue(meta["coverage"]["coverage_complete"])
        for t in tickers:
            entry = out["tickers"][t]
            self.assertIn("chart", entry)
            self.assertIn("technicals", entry)

    async def test_batch_equals_single_computation(self):
        """수용 2: 배치의 기술 값이 같은 데이터의 단건 계산과 일치한다."""
        bars = _bars()
        out = await self._run(["AAPL"], {"AAPL": bars})
        tech = out["tickers"]["AAPL"]["technicals"]
        df = _to_df(bars)
        self.assertEqual(tech["rsi14"]["value"], compute_rsi(df)["value"])
        self.assertEqual(tech["ma"]["ma20"],
                         round(float(df["close"].rolling(20).mean().iloc[-1]), 2))
        self.assertEqual(tech["cross_20_60"]["type"], compute_ma_cross(df)["type"])
        chart = out["tickers"]["AAPL"]["chart"]
        self.assertEqual(chart["last_close"], bars[-1]["close"])
        r5 = (bars[-1]["close"] / bars[-6]["close"] - 1) * 100
        self.assertAlmostEqual(chart["returns_pct"]["5d"], round(r5, 2))

    async def test_in_progress_bar_is_dropped_from_technicals(self):
        """요구 1: 기술 상태는 완성 봉 기준 - 진행 중 봉은 계산에서 뺀다."""
        bars = _bars()
        state = {
            "timeframe": "day", "last_bar_date": bars[-1]["date"],
            "last_bar_complete": False,
            "last_completed_bar_date": bars[-2]["date"],
            "calculation_includes_incomplete": True,
        }
        out = await self._run(["AAPL"], {"AAPL": bars}, bar_state=state)
        entry = out["tickers"]["AAPL"]
        self.assertEqual(entry["technicals"]["basis"], "completed_bars")
        self.assertEqual(entry["technicals"]["dropped_in_progress_bar"],
                         bars[-1]["date"])
        self.assertEqual(entry["chart"]["last_completed_bar"], bars[-2]["date"])
        self.assertEqual(entry["chart"]["last_close"], bars[-2]["close"])
        # 완성 봉 기준의 단건 계산과 일치
        df = _to_df(bars[:-1])
        self.assertEqual(entry["technicals"]["rsi14"]["value"],
                         compute_rsi(df)["value"])

    async def test_optional_sections_preserve_unavailable(self):
        """요구 3: 확인 불가는 지어내지 않고 unavailable 로 보존한다."""
        short = {"ticker": "AAPL", "shares_short": 100, "short_ratio": 1.5,
                 "short_percent_of_float": 0.012,
                 "date_short_interest": 1787097600,   # 2026-08-19 UTC
                 "shares_short_prior_month": 90, "prior_month_date": None,
                 "float_shares": 10_000}
        earnings = {"events": [
            {"date": "2026-10-29", "eps_estimate": 2.0, "eps_actual": None,
             "eps_surprise_pct": None},
            {"date": "2026-07-30", "eps_estimate": 1.5, "eps_actual": 1.7,
             "eps_surprise_pct": 13.3},
        ]}
        out = await self._run(["AAPL"], {"AAPL": _bars()},
                              include=["estimates", "earnings", "short"],
                              estimates=None, earnings=earnings, short=short)
        entry = out["tickers"]["AAPL"]
        self.assertEqual(entry["estimates"], {"status": "unavailable"})
        self.assertEqual(entry["earnings"]["last_reported"]["date"], "2026-07-30")
        self.assertEqual(entry["earnings"]["last_reported"]["eps_surprise_pct"], 13.3)
        self.assertEqual(entry["short"]["report_date"], "2026-08-19")
        self.assertIn("stale", entry["short"])

    async def test_failed_ticker_is_counted(self):
        out = await self._run(["AAPL", "ZZZZ"],
                              {"AAPL": _bars(), "ZZZZ": []})
        meta = out["_meta"]
        self.assertEqual(meta["coverage"]["failed_entities"], ["ZZZZ"])
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(out["tickers"]["ZZZZ"], {"status": "unavailable",
                                                  "reason": "no_history"})

    async def test_no_composite_score(self):
        """요구 3: 단일 점수로 압축하지 않는다 - score 류 키 자체가 없다."""
        out = await self._run(["AAPL"], {"AAPL": _bars()})
        flat = json.dumps(out["tickers"], ensure_ascii=False).lower()
        for banned in ("\"score\"", "\"rating\"", "\"grade\"", "종합점수"):
            self.assertNotIn(banned, flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
