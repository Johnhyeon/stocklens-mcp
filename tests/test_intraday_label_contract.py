"""분봉·짧은 이력에서 지표 이름표가 값과 맞는가.

실측(2026-09-17): get_intraday_indicators(interval=5m, bars=60) 가
position.high_52w=16500 / low_52w=15160 을 냈다. 60봉 조회 구간의 고저였고
high_date 는 당일 11:35 였다. 경고는 position_52w 가 봉 부족이라고 했는데 필드는
52주 이름으로 채워져 있었다. 분봉 경로가 timeframe 없이 일봉 규칙으로 계산했다.

같은 계열로 함께 고친 것:
  - 일봉이라도 1년치(252봉)가 안 되면 *_52w 는 null, 값은 lookback_* 로
  - 크로스 경과 days_ago 는 봉 개수다. 일봉 밖(주·월·분봉)에서는 bars_ago
  - 미국 분봉 가격이 KRX 호가단위로 뭉개졌다(187.35 → 187). 거래대금 추산도 _krw
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
from stock_mcp_server._indicators import _to_df, compute_indicators, compute_position
from stock_mcp_server.market_data.models import BarDataset, NormalizedBar

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

_CLOCK = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "last_trading_day": "2026-09-16"},
    "us": {"is_open": False, "status": "closed_after_hours",
           "last_trading_day": "2026-09-16"},
}


def _rows(n: int, *, base: float = 15_500.0, step: float = 5.0) -> list[dict]:
    return [{"date": f"2026-01-01 {i:05d}", "open": base + i * step,
             "high": base + i * step + 20, "low": base + i * step - 20,
             "close": base + i * step, "volume": 1_000 + i} for i in range(n)]


def _five_minute_bars(market: str = "KR", count: int = 60) -> list[NormalizedBar]:
    """09:00(뉴욕은 09:30)부터 5분봉. KR 은 11:35 봉에 16,500 고점."""
    tz, open_at = (KST, datetime(2026, 9, 16, 9, 0)) if market == "KR" \
        else (NY, datetime(2026, 9, 16, 9, 30))
    bars = []
    for i in range(count):
        start = open_at.replace(tzinfo=tz) + timedelta(minutes=5 * i)
        if market == "KR":
            close = Decimal(15_800 + (i % 7) * 10)
            high = Decimal(16_500) if (start.hour, start.minute) == (11, 35) \
                else close + 20
            low = Decimal(15_160) if i == 3 else close - 30
        else:
            close = Decimal("187.35") + Decimal(i % 5) / 100
            high, low = close + Decimal("0.12"), close - Decimal("0.07")
        bars.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=5),
            open=close, high=high, low=low, close=close, volume=10_000 + i,
            interval="5m", session="regular", complete=True, session_tail=False,
            expected_minutes=5, actual_minutes=5, data_integrity="complete",
            source_gap_status="none"))
    return bars


def _dataset(bars, market="KR") -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market=market,
        symbol="010170" if market == "KR" else "AAPL",
        provider="kis" if market == "KR" else "yahoo",
        profile="real" if market == "KR" else None,
        venue="KRX" if market == "KR" else "NAS",
        timezone="Asia/Seoul" if market == "KR" else "America/New_York",
        session="regular", requested_interval="5m", source_interval="1m",
        aggregation_method="stocklens_session_resample",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={"complete": True}, warnings=())


def _route_meta(provider="kis"):
    return {"requested_source": "auto", "selected_provider": provider,
            "selection_reason": "broker_connected_and_intraday_supported",
            "mode": "auto", "fallback_used": False, "fallback_from": None}


class PositionNameTests(unittest.TestCase):
    def test_intraday_window_never_claims_52_weeks(self):
        pos = compute_position(_to_df(_rows(60)), bars_per_year=None)
        self.assertFalse([k for k in pos if "52w" in k])
        self.assertEqual(pos["lookback_high"], int(15_500 + 59 * 5 + 20))
        self.assertEqual(pos["lookback_low"], int(15_500 - 20))
        self.assertEqual(pos["lookback_bars"], 60)
        self.assertIn("pct_from_lookback_high", pos)

    def test_short_daily_history_empties_52w_fields(self):
        pos = compute_position(_to_df(_rows(120)), bars_per_year=252)
        for key in ("high_52w", "low_52w", "pct_from_high_52w", "pct_from_low_52w"):
            self.assertIsNone(pos[key], key)
        self.assertEqual(pos["lookback_bars"], 120)
        self.assertIsNotNone(pos["lookback_high"])

    def test_full_year_keeps_the_existing_shape(self):
        pos = compute_position(_to_df(_rows(260)), bars_per_year=252)
        self.assertIsNotNone(pos["high_52w"])
        self.assertFalse([k for k in pos if k.startswith("lookback_") and k != "lookback_bars"])
        self.assertEqual(pos["lookback_bars"], 252)

    def test_params_cannot_shrink_the_52_week_window(self):
        ind = compute_indicators(_rows(120), ["position"],
                                 params={"position": {"bars_per_year": 60}})
        self.assertIsNone(ind["position"]["high_52w"])

    def test_intraday_timeframe_reaches_the_calculator(self):
        ind = compute_indicators(_rows(300), ["position"], timeframe="5m")
        self.assertNotIn("high_52w", ind["position"])
        self.assertEqual(ind["position"]["lookback_bars"], 300)


class CrossAgeNameTests(unittest.TestCase):
    def test_daily_keeps_days_ago(self):
        ind = compute_indicators(_rows(120), ["ma_cross", "macd"])
        self.assertIn("days_ago", ind["macd"]["cross"])
        self.assertIn("days_ago", ind["ma_cross"]["ma20_60"])

    def test_other_timeframes_count_bars(self):
        for tf in ("week", "month", "5m"):
            ind = compute_indicators(_rows(120), ["ma_cross", "macd"], timeframe=tf)
            for cross in (ind["macd"]["cross"], ind["ma_cross"]["ma20_60"],
                          ind["ma_cross"]["ma60_120"]):
                self.assertNotIn("days_ago", cross, tf)
                self.assertIn("bars_ago", cross, tf)


class CurrencyTests(unittest.TestCase):
    def _usd_rows(self, n=60):
        return [{"date": f"t{i:03d}", "open": 187.31, "high": 187.47,
                 "low": 187.28, "close": 187.35 + i * 0.001, "volume": 5_000}
                for i in range(n)]

    def test_usd_prices_keep_cents(self):
        ind = compute_indicators(self._usd_rows(),
                                 ["ma", "candle", "position", "price_channel",
                                  "volume_profile", "support_resistance"],
                                 timeframe="5m", currency="USD")
        self.assertEqual(ind["candle"]["close"], round(187.35 + 59 * 0.001, 2))
        self.assertEqual(ind["candle"]["high"], 187.47)
        self.assertEqual(ind["ma"]["ma20"], round(sum(187.35 + i * 0.001
                                                      for i in range(40, 60)) / 20, 2))
        self.assertEqual(ind["position"]["lookback_high"], 187.47)
        self.assertEqual(ind["price_channel"]["upper"], 187.47)
        self.assertIsInstance(ind["volume_profile"]["current_price"], float)

    def test_usd_trade_value_is_named_in_dollars(self):
        vol = compute_indicators(self._usd_rows(), ["volume"], currency="USD")["volume"]
        self.assertNotIn("trade_value_est_krw", vol)
        self.assertEqual(vol["trade_value_est_usd"], round((187.35 + 59 * 0.001) * 5_000))

    def test_krw_output_is_unchanged(self):
        ind = compute_indicators(_rows(60), ["ma", "candle", "volume"])
        self.assertIsInstance(ind["ma"]["ma20"], int)
        self.assertIsInstance(ind["candle"]["close"], int)
        self.assertIn("trade_value_est_krw", ind["volume"])


class CoverageTests(unittest.TestCase):
    def test_intraday_coverage_does_not_ask_for_a_year(self):
        cov = server._indicator_coverage(include=["position"], available_bars=60,
                                         timeframe="5m")
        self.assertNotIn("position_52w", cov["required_bars"])
        self.assertEqual(cov["insufficient"], [])


class IntradayToolTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, market="KR", include=None):
        ds = _dataset(_five_minute_bars(market), market=market)
        fetch = AsyncMock(return_value=(ds, _route_meta(ds.provider)))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock", return_value=_CLOCK):
            text = await server.get_intraday_indicators(
                symbol=ds.symbol, market=market, interval="5m", bars=60,
                include=include or ["position", "volume", "macd", "candle"])
        return json.loads(text)

    async def test_reported_case_is_labeled_as_lookback(self):
        payload = await self._run()
        pos = payload["indicators"]["position"]
        self.assertFalse([k for k in pos if "52w" in k])
        self.assertEqual((pos["lookback_high"], pos["lookback_low"]), (16_500, 15_160))
        self.assertIn("11:35", pos["high_date"])
        meta = payload["_meta"]
        self.assertNotIn("position_52w", meta["indicator_coverage"]["required_bars"])
        self.assertFalse(any("position_52w" in w for w in meta["warnings"]))
        self.assertIn("bars_ago", payload["indicators"]["macd"]["cross"])

    async def test_us_intraday_prices_and_currency(self):
        payload = await self._run(market="US")
        ind = payload["indicators"]
        self.assertIsInstance(ind["candle"]["close"], float)
        self.assertAlmostEqual(ind["position"]["lookback_high"], 187.51, places=2)
        self.assertIn("trade_value_est_usd", ind["volume"])
        self.assertNotIn("trade_value_est_krw", ind["volume"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
