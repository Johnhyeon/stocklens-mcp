"""SL-06: get_us_multi_price 의 종목별 최신성.

실측(UAT): 배치 스냅샷에 오래된 quote 가 섞여도 표에는 태 없이 나란히 실려,
거래정지·수집 지연 종목의 옛 가격이 "오늘 가격"으로 읽혔다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_ET = ZoneInfo("America/New_York")
_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _epoch(y, m, d, hh=16):
    """미 동부 시각 -> epoch 초. 16시 = 정규장 마감."""
    return int(datetime(y, m, d, hh, 0, tzinfo=_ET).timestamp())


def _row(ticker, day=(2026, 8, 26), state="CLOSED", **kw):
    out = {
        "ticker": ticker, "name": f"{ticker} Inc.", "price": 100.0,
        "change": 1.0, "change_percent": 1.0, "volume": 1_000_000,
        "market_cap": 10**9, "market_state": state,
        "quote_time": _epoch(*day),
    }
    out.update(kw)
    return out


class MultiPriceStalenessTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, rows, tickers=None):
        tickers = tickers or [r.get("ticker", "X") for r in rows]
        with patch.object(server.us, "get_multi_prices", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_us_multi_price(tickers)

    async def test_one_stale_ticker_breaks_silent_complete(self):
        """수용 1: 한 티커만 오래된 응답이 조용히 complete 로 나가지 않는다."""
        rows = [_row("AAPL"), _row("MSFT"), _row("HALT", day=(2026, 8, 12))]
        text = await self._run(rows)
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["stale_entities"], ["HALT"])
        self.assertTrue(any("HALT" in w for w in meta["warnings"]))
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("2026-08-12", body)   # 그 줄에 실제 기준일이 보인다

    async def test_uniform_batch_is_complete_with_matching_dates(self):
        """수용 2: 정상 15티커는 공통 기준일과 종목별 기준일이 일치한다."""
        tickers = [f"T{i:02d}" for i in range(15)]
        rows = [_row(t) for t in tickers]
        text = await self._run(rows)
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertTrue(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_as_of"], "2026-08-26")
        self.assertEqual(meta["per_entity_data_as_of"],
                         {t: "2026-08-26" for t in tickers})
        self.assertEqual(meta["coverage"]["returned_entities"], 15)
        self.assertEqual(meta["coverage"]["requested_entities"], 15)

    async def test_failed_tickers_are_counted_not_dropped(self):
        """요구 3: 실패 티커 수가 coverage 에 남는다."""
        rows = [_row("AAPL"), {"ticker": "ZZZZ", "error": "not found"}]
        text = await self._run(rows)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["returned_entities"], 1)
        self.assertEqual(meta["coverage"]["failed_entities"], ["ZZZZ"])
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_session_recorded_per_entity(self):
        """quote 의 세션(정규/프리/포스트)이 티커별로 남는다."""
        rows = [_row("AAPL", state="POST"), _row("MSFT", state="CLOSED")]
        text = await self._run(rows)
        meta = extract_meta(text)
        self.assertEqual(meta["per_entity_session"],
                         {"AAPL": "POST", "MSFT": "CLOSED"})

    async def test_missing_quote_time_is_unknown_not_fresh(self):
        """quote 시각이 없으면 최신이라 단정하지 않는다 - 미상으로 표시."""
        rows = [_row("AAPL"), _row("NOTS", quote_time=None)]
        text = await self._run(rows)
        meta = extract_meta(text)
        self.assertNotIn("NOTS", meta["per_entity_data_as_of"])
        self.assertIn("NOTS", meta["coverage"]["unknown_as_of_entities"])
        self.assertFalse(meta["coverage"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
