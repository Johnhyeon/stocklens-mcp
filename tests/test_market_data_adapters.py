"""Naver·Yahoo 어댑터 테스트 (Task 3).

어댑터는 기존 naver.get_ohlcv / yfinance_source.get_history 를 그대로 호출해
공통 BarDataset 으로 변환만 한다. 원본 함수와 원본 행을 절대 변경하지 않는다.
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.naver_provider import NaverBarProvider
from stock_mcp_server.market_data.yahoo_provider import YahooBarProvider

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def _kr_request(**overrides) -> BarRequest:
    base = dict(
        symbol="005930", market="KR", interval="1d",
        start=None, end=None, trading_date=None,
        row_limit=120, venue="KRX", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _us_request(**overrides) -> BarRequest:
    base = dict(
        symbol="AAPL", market="US", interval="5m",
        start=None, end=None, trading_date=None,
        row_limit=120, venue="NAS", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


_NAVER_ROWS = [
    {"date": "20260825", "open": 100, "high": 110, "low": 95,
     "close": 105, "volume": 1000},
    {"date": "20260826", "open": 105, "high": 112, "low": 104,
     "close": 111, "volume": 2000},
]

_YAHOO_INTRADAY_ROWS = [
    {"datetime": datetime(2026, 8, 26, 9, 30, tzinfo=NY),
     "open": 200.0, "high": 201.5, "low": 199.0, "close": 201.0,
     "volume": 5000.0},
    {"datetime": datetime(2026, 8, 26, 9, 35, tzinfo=NY),
     "open": 201.0, "high": 202.0, "low": 200.5, "close": 201.5,
     "volume": 6000.0},
]

_YAHOO_DAILY_ROWS = [
    {"date": datetime(2026, 8, 25), "open": 200.0, "high": 205.0,
     "low": 199.0, "close": 204.0, "volume": 1_000_000.0},
    {"date": datetime(2026, 8, 26), "open": 204.0, "high": 206.0,
     "low": 202.0, "close": 205.0, "volume": 1_100_000.0},
]


class NaverAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_normalization(self) -> None:
        mock = AsyncMock(return_value=copy.deepcopy(_NAVER_ROWS))
        with patch("stock_mcp_server.market_data.naver_provider.get_ohlcv", mock):
            provider = NaverBarProvider()
            ds = await provider.fetch_bars(_kr_request(row_limit=2))

        mock.assert_awaited_once_with("005930", "day", 2)
        self.assertEqual(provider.provider_id, "naver")
        self.assertEqual(ds.provider, "naver")
        self.assertIsNone(ds.profile)
        self.assertEqual(ds.venue, "KRX")
        self.assertEqual(ds.timezone, "Asia/Seoul")
        self.assertEqual(ds.requested_interval, "1d")
        self.assertEqual(ds.source_interval, "1d")
        self.assertEqual(ds.aggregation_method, "provider_native")
        # 네이버는 조정 기준을 명시하지 않는다. 아는 척하지 않는다.
        self.assertEqual(ds.adjustment_basis, "unknown")

        self.assertEqual(len(ds.bars), 2)
        first = ds.bars[0]
        self.assertEqual(first.start_at, datetime(2026, 8, 25, 9, 0, tzinfo=KST))
        self.assertEqual(first.end_at, datetime(2026, 8, 25, 15, 30, tzinfo=KST))
        self.assertEqual(first.open, Decimal("100"))
        self.assertEqual(first.close, Decimal("105"))
        self.assertEqual(first.volume, 1000)
        self.assertTrue(first.complete)
        # 어댑터는 시장 시계를 모른다. 마지막 봉의 마감 여부는 단정하지 않는다.
        self.assertIsNone(ds.bars[-1].complete)

    async def test_week_month_mapping(self) -> None:
        for interval, timeframe in (("1wk", "week"), ("1mo", "month")):
            mock = AsyncMock(return_value=copy.deepcopy(_NAVER_ROWS))
            with patch("stock_mcp_server.market_data.naver_provider.get_ohlcv", mock):
                ds = await NaverBarProvider().fetch_bars(
                    _kr_request(interval=interval, row_limit=2))
            mock.assert_awaited_once_with("005930", timeframe, 2)
            self.assertEqual(ds.source_interval, interval)

    async def test_source_rows_not_mutated(self) -> None:
        rows = copy.deepcopy(_NAVER_ROWS)
        snapshot = copy.deepcopy(rows)
        with patch("stock_mcp_server.market_data.naver_provider.get_ohlcv",
                   AsyncMock(return_value=rows)):
            await NaverBarProvider().fetch_bars(_kr_request(row_limit=2))
        self.assertEqual(rows, snapshot)

    async def test_intraday_not_supported(self) -> None:
        with self.assertRaisesRegex(ValueError, "interval"):
            await NaverBarProvider().fetch_bars(_kr_request(interval="5m"))

    async def test_descending_rows_normalized_ascending_with_dedup(self) -> None:
        rows = [copy.deepcopy(_NAVER_ROWS[1]), copy.deepcopy(_NAVER_ROWS[0]),
                copy.deepcopy(_NAVER_ROWS[0])]
        with patch("stock_mcp_server.market_data.naver_provider.get_ohlcv",
                   AsyncMock(return_value=rows)):
            ds = await NaverBarProvider().fetch_bars(_kr_request(row_limit=3))
        self.assertEqual([b.start_at.day for b in ds.bars], [25, 26])
        self.assertTrue(any("중복" in w for w in ds.warnings))

    async def test_capabilities(self) -> None:
        caps = await NaverBarProvider().capabilities("real")
        self.assertEqual(caps.provider, "naver")
        self.assertEqual(caps.markets, ("KR",))
        self.assertIn("1d", caps.native_intervals)
        self.assertNotIn("5m", caps.native_intervals)


class YahooAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_intraday_normalization(self) -> None:
        mock = AsyncMock(return_value=copy.deepcopy(_YAHOO_INTRADAY_ROWS))
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history", mock):
            provider = YahooBarProvider()
            ds = await provider.fetch_bars(_us_request(interval="5m", row_limit=2))

        self.assertEqual(mock.await_args.args, ("AAPL",))
        self.assertEqual(mock.await_args.kwargs["interval"], "5m")
        self.assertIs(mock.await_args.kwargs["prepost"], False)
        self.assertEqual(provider.provider_id, "yahoo")
        self.assertEqual(ds.provider, "yahoo")
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertEqual(ds.requested_interval, "5m")
        self.assertEqual(ds.source_interval, "5m")
        # yfinance get_history 는 auto_adjust=False 로 호출된다.
        self.assertEqual(ds.adjustment_basis, "unadjusted")

        first = ds.bars[0]
        self.assertEqual(first.start_at, datetime(2026, 8, 26, 9, 30, tzinfo=NY))
        self.assertEqual(first.end_at, datetime(2026, 8, 26, 9, 35, tzinfo=NY))
        self.assertEqual(first.volume, 5000)
        self.assertIsNone(ds.bars[-1].complete)

    async def test_60m_maps_to_yahoo_1h(self) -> None:
        mock = AsyncMock(return_value=[])
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history", mock):
            await YahooBarProvider().fetch_bars(_us_request(interval="60m"))
        self.assertEqual(mock.await_args.kwargs["interval"], "1h")

    async def test_daily_normalization(self) -> None:
        mock = AsyncMock(return_value=copy.deepcopy(_YAHOO_DAILY_ROWS))
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history", mock):
            ds = await YahooBarProvider().fetch_bars(
                _us_request(interval="1d", row_limit=2))
        self.assertEqual(mock.await_args.kwargs["interval"], "1d")
        first = ds.bars[0]
        self.assertEqual(first.start_at, datetime(2026, 8, 25, 9, 30, tzinfo=NY))
        self.assertEqual(first.end_at, datetime(2026, 8, 25, 16, 0, tzinfo=NY))

    async def test_row_limit_keeps_recent_rows(self) -> None:
        mock = AsyncMock(return_value=copy.deepcopy(_YAHOO_INTRADAY_ROWS))
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history", mock):
            ds = await YahooBarProvider().fetch_bars(
                _us_request(interval="5m", row_limit=1))
        self.assertEqual(len(ds.bars), 1)
        self.assertEqual(ds.bars[0].start_at.minute, 35)

    async def test_unsupported_interval_rejected(self) -> None:
        for interval in ("3m", "10m", "120m", "240m"):
            with self.assertRaisesRegex(ValueError, "interval"):
                await YahooBarProvider().fetch_bars(_us_request(interval=interval))

    async def test_source_rows_not_mutated(self) -> None:
        rows = copy.deepcopy(_YAHOO_INTRADAY_ROWS)
        snapshot = copy.deepcopy(rows)
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history",
                   AsyncMock(return_value=rows)):
            await YahooBarProvider().fetch_bars(_us_request(interval="5m"))
        self.assertEqual(rows, snapshot)

    async def test_missing_volume_row_dropped_with_warning(self) -> None:
        rows = copy.deepcopy(_YAHOO_INTRADAY_ROWS)
        rows[0]["volume"] = None
        with patch("stock_mcp_server.market_data.yahoo_provider.get_history",
                   AsyncMock(return_value=rows)):
            ds = await YahooBarProvider().fetch_bars(_us_request(interval="5m"))
        # 결측을 0으로 바꾸지 않는다. 해당 행을 버리고 경고로 남긴다.
        self.assertEqual(len(ds.bars), 1)
        self.assertTrue(any("결측" in w for w in ds.warnings))

    async def test_capabilities(self) -> None:
        caps = await YahooBarProvider().capabilities("real")
        self.assertEqual(caps.provider, "yahoo")
        self.assertEqual(caps.markets, ("US",))
        for iv in ("1m", "5m", "15m", "30m", "60m", "1d"):
            self.assertIn(iv, caps.native_intervals)
        for iv in ("3m", "120m", "240m"):
            self.assertNotIn(iv, caps.native_intervals)


if __name__ == "__main__":
    unittest.main()
