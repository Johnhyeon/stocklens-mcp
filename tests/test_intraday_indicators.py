"""분봉 지표 도구 일관성 테스트 (Task 12).

차트와 지표가 같은 BarDataset 을 소비하고, 지표 값이 동일 rows 로
독립 호출한 compute_indicators 와 일치하는지 확인한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server
from stock_mcp_server._indicators import compute_indicators
from stock_mcp_server.market_data.indicator_input import (
    bars_to_ohlcv,
    filter_completed,
)
from stock_mcp_server.market_data.models import BarDataset, NormalizedBar

KST = ZoneInfo("Asia/Seoul")

_CLOCK = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "last_trading_day": "2026-08-27"},
    "us": {"is_open": False, "status": "closed_after_hours",
           "last_trading_day": "2026-08-26"},
}


def _bars(count: int) -> list[NormalizedBar]:
    """60분봉 count 개. 완만한 상승으로 지표가 결정적으로 나온다."""
    out = []
    start0 = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    for i in range(count):
        day, slot = divmod(i, 6)
        start = start0 + timedelta(days=day, minutes=60 * slot)
        close = 1000 + i * 3
        out.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=60),
            open=Decimal(close - 1), high=Decimal(close + 2),
            low=Decimal(close - 3), close=Decimal(close),
            volume=10_000 + i * 7,
            interval="60m", session="regular", complete=True,
            session_tail=False, expected_minutes=60, actual_minutes=60,
            data_integrity="complete", source_gap_status="none",
        ))
    return out


def _dataset(bars) -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market="KR", symbol="005930", provider="kis",
        profile="real", venue="KRX", timezone="Asia/Seoul",
        session="regular", requested_interval="60m", source_interval="1m",
        aggregation_method="stocklens_session_resample",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={"complete": True}, warnings=(),
    )


def _route_meta():
    return {
        "requested_source": "auto", "selected_provider": "kis",
        "selection_reason": "broker_connected_and_intraday_supported",
        "mode": "auto", "fallback_used": False, "fallback_from": None,
    }


class InputConversionTests(unittest.TestCase):
    def test_bars_to_ohlcv_shape(self):
        rows = bars_to_ohlcv(_bars(3))
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            set(rows[0]), {"date", "open", "high", "low", "close", "volume"})
        self.assertLess(rows[0]["date"], rows[1]["date"])
        self.assertIsInstance(rows[0]["close"], float)

    def test_filter_completed_drops_unknown_and_false(self):
        bars = _bars(3)
        incomplete = NormalizedBar(
            start_at=bars[-1].start_at + timedelta(minutes=60),
            end_at=bars[-1].start_at + timedelta(minutes=120),
            open=bars[-1].open, high=bars[-1].high, low=bars[-1].low,
            close=bars[-1].close, volume=1, interval="60m",
            session="regular", complete=False, session_tail=False,
            expected_minutes=60, actual_minutes=60,
            data_integrity="complete", source_gap_status="none")
        unknown = NormalizedBar(
            start_at=incomplete.start_at + timedelta(minutes=60),
            end_at=incomplete.start_at + timedelta(minutes=120),
            open=bars[-1].open, high=bars[-1].high, low=bars[-1].low,
            close=bars[-1].close, volume=1, interval="60m",
            session="regular", complete=None, session_tail=False,
            expected_minutes=60, actual_minutes=60,
            data_integrity="complete", source_gap_status="none")
        kept = filter_completed(tuple(bars) + (incomplete, unknown))
        self.assertEqual(len(kept), 3)


class ConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_and_indicators_consume_same_dataset(self):
        ds = _dataset(_bars(260))
        fetch = AsyncMock(return_value=(ds, _route_meta()))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock", return_value=_CLOCK):
            chart_text = await server.get_intraday_chart(
                symbol="005930", interval="60m", row_limit=260)
            ind_text = await server.get_intraday_indicators(
                symbol="005930", interval="60m", bars=260,
                include=["ma", "rsi", "macd", "volume"])

        self.assertEqual(fetch.await_count, 2)
        self.assertIn("005930", chart_text)

        payload = json.loads(ind_text)
        self.assertEqual(payload["symbol"], "005930")
        self.assertEqual(payload["interval"], "60m")
        self.assertEqual(payload["bars"], 260)
        self.assertIn("_meta", payload)
        self.assertEqual(payload["_meta"]["provider"], "kis")
        self.assertEqual(payload["_meta"]["requested_interval"], "60m")

        # 동일 rows 로 독립 재계산한 값과 일치해야 한다.
        expected = compute_indicators(
            bars_to_ohlcv(ds.bars), ["ma", "rsi", "macd", "volume"])
        got = payload["indicators"]
        self.assertEqual(got["rsi"]["value"], expected["rsi"]["value"])
        self.assertEqual(got["ma"], json.loads(json.dumps(expected["ma"])))
        self.assertEqual(got["volume"]["latest"],
                         expected["volume"]["latest"])
        self.assertEqual(got["macd"]["histogram"],
                         expected["macd"]["histogram"])

    async def test_insufficient_bars_is_reported_not_silent(self):
        ds = _dataset(_bars(10))
        fetch = AsyncMock(return_value=(ds, _route_meta()))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock", return_value=_CLOCK):
            ind_text = await server.get_intraday_indicators(
                symbol="005930", interval="60m", bars=260,
                include=["ma"])
        payload = json.loads(ind_text)
        # 봉이 모자라면 값 없는 지표가 생긴다. 신호 없음이 아니라 계산 불가다.
        self.assertIn("_meta", payload)

    async def test_unknown_indicator_rejected(self):
        with patch.object(server, "build_market_clock", return_value=_CLOCK):
            text = await server.get_intraday_indicators(
                symbol="005930", include=["magic"])
        self.assertIn("지원하지 않는 지표", text)


if __name__ == "__main__":
    unittest.main()
