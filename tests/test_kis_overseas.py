"""KIS 미국 분봉 공급자 테스트 (Task 8). 전부 fixture 기반, 네트워크 없음."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials
from stock_mcp_server.market_data.kis_client import KisClient
from stock_mcp_server.market_data.kis_overseas import KisOverseasProvider
from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.us_symbols import (
    SymbolMappingError,
    resolve_us_symbol,
)

NY = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures" / "kis"

PAGE_1 = json.loads((FIXTURES / "us_minute_page_1.json").read_text("utf-8"))
PAGE_2 = json.loads((FIXTURES / "us_minute_page_2.json").read_text("utf-8"))


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="AAPL", market="US", interval="1m",
        start=None, end=None, trading_date=date(2026, 8, 26),
        row_limit=500, venue="NAS", session="regular",
        adjustment="unadjusted", completed_only=True, source="kis",
    )
    base.update(overrides)
    return BarRequest(**base)


class Handler:
    """KEYB 연속키별 응답 시나리오. KEYB 미지정(첫 페이지)은 '' 키."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json={
                "access_token": "fixture-token", "expires_in": 86400})
        params = dict(request.url.params)
        self.calls.append(params)
        item = self.pages.get(params.get("KEYB", ""))
        if item is None:
            return httpx.Response(200, json={"rt_cd": "0", "output2": []})
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def _provider(handler, profile="real", **kwargs) -> KisOverseasProvider:
    client = KisClient(
        credentials=BrokerCredentials(app_key="k", app_secret="s"),
        profile=profile,
        transport=httpx.MockTransport(handler),
    )
    return KisOverseasProvider(client=client, profile=profile, **kwargs)


def _run(coro):
    return asyncio.run(coro)


class SymbolResolverTests(unittest.TestCase):
    def test_exchange_codes(self):
        self.assertEqual(resolve_us_symbol("AAPL", "NAS"), ("NAS", "AAPL"))
        self.assertEqual(resolve_us_symbol("ibm", "NYS"), ("NYS", "IBM"))
        self.assertEqual(resolve_us_symbol("SPY", "AMS"), ("AMS", "SPY"))

    def test_class_share_punctuation_normalized(self):
        # 클래스 구분자는 정규화하되 거래소를 추측하지 않는다.
        self.assertEqual(resolve_us_symbol("BRK-B", "NYS")[1],
                         resolve_us_symbol("BRK.B", "NYS")[1])

    def test_unresolved_exchange_is_structured_error(self):
        with self.assertRaises(SymbolMappingError) as ctx:
            resolve_us_symbol("AAPL", None)
        self.assertEqual(ctx.exception.code, "exchange_unresolved")
        with self.assertRaises(SymbolMappingError):
            resolve_us_symbol("AAPL", "LSE")

    def test_daytime_codes_kept_separate(self):
        # 주간거래 코드는 정규장 코드와 섞이지 않는다.
        self.assertEqual(resolve_us_symbol("AAPL", "BAQ",
                                           session="daytime")[0], "BAQ")
        with self.assertRaises(SymbolMappingError):
            resolve_us_symbol("AAPL", "BAQ", session="regular")
        with self.assertRaises(SymbolMappingError):
            resolve_us_symbol("AAPL", "NAS", session="daytime")

    def test_empty_symbol_rejected(self):
        with self.assertRaises(SymbolMappingError):
            resolve_us_symbol("  ", "NAS")


class FetchTests(unittest.TestCase):
    def test_two_pages_normalized_with_dst_timezone(self):
        handler = Handler({
            "": PAGE_1,
            # 다음 KEYB = 최소시각(093300) - 1분
            "20260826093200": PAGE_2,
        })
        ds = _run(_provider(handler).fetch_bars(_request()))

        self.assertEqual(ds.provider, "kis")
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertEqual(ds.venue, "NAS")
        self.assertEqual(
            [b.start_at.strftime("%H%M") for b in ds.bars],
            ["0931", "0932", "0933", "0934", "0935"])
        first = ds.bars[0]
        self.assertEqual(first.start_at,
                         datetime(2026, 8, 26, 9, 31, tzinfo=NY))
        # 8월은 EDT(UTC-4)다. DST 가 zoneinfo 로 반영되는지 확인한다.
        self.assertEqual(first.start_at.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(first.volume, 6000)
        self.assertEqual(str(first.close), "229.40")
        self.assertTrue(any("중복" in w for w in ds.warnings))

    def test_winter_date_uses_est_offset(self):
        page = copy.deepcopy(PAGE_1)
        for row in page["output2"]:
            row["xymd"] = "20261215"
        handler = Handler({"": page})
        ds = _run(_provider(handler).fetch_bars(
            _request(trading_date=date(2026, 12, 15))))
        self.assertEqual(
            ds.bars[0].start_at.utcoffset().total_seconds(), -5 * 3600)

    def test_request_params_max_120_rows_and_nmin(self):
        handler = Handler({"": PAGE_1})
        _run(_provider(handler).fetch_bars(_request()))
        params = handler.calls[0]
        self.assertEqual(params["NREC"], "120")
        self.assertEqual(params["NMIN"], "1")
        self.assertEqual(params["EXCD"], "NAS")
        self.assertEqual(params["SYMB"], "AAPL")

    def test_native_nmin_interval(self):
        handler = Handler({"": PAGE_1})
        ds = _run(_provider(handler).fetch_bars(_request(interval="5m")))
        self.assertEqual(handler.calls[0]["NMIN"], "5")
        self.assertEqual(ds.source_interval, "5m")

    def test_regular_session_filtering(self):
        page = copy.deepcopy(PAGE_1)
        page["output2"].append({
            "xymd": "20260826", "xhms": "080000",
            "kymd": "20260826", "khms": "210000",
            "open": "1", "high": "1", "low": "1", "last": "1",
            "evol": "10", "eamt": "10",
        })
        handler = Handler({"": page})
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertTrue(all(
            b.start_at.time() >= datetime(2000, 1, 1, 9, 30).time()
            for b in ds.bars))
        self.assertTrue(any("세션 밖" in w for w in ds.warnings))

    def test_no_progress_keyb_stops(self):
        handler = Handler({
            "": PAGE_1,
            "20260826093200": PAGE_1,  # 같은 페이지 반복
        })
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertLessEqual(len(handler.calls), 3)
        self.assertTrue(any("진행" in w for w in ds.warnings))

    def test_unresolved_exchange_raises_mapping_error(self):
        handler = Handler({"": PAGE_1})
        with self.assertRaises(SymbolMappingError):
            _run(_provider(handler).fetch_bars(_request(venue="XXX")))


class CapabilityTests(unittest.TestCase):
    def test_demo_us_minute_unavailable(self):
        handler = Handler({})
        real = _run(_provider(handler, "real").capabilities("real"))
        demo = _run(_provider(handler, "demo").capabilities("demo"))
        self.assertIn("US", real.markets)
        self.assertEqual(real.max_rows_per_call, 120)
        # KIS 모의는 해외 분봉을 지원하지 않는다. 추측 활성화 금지.
        self.assertEqual(demo.markets, ())
        self.assertEqual(demo.verified_intervals, ())


if __name__ == "__main__":
    unittest.main()
