"""토스 KR·US 1분 캔들 공급자 테스트 (1.0 Task 16).

공식 스펙: GET /api/v1/candles?symbol&interval=1m&count(<=200)
&before(inclusive ISO)&adjusted=false. 응답 result.candles 최신순,
timestamp 는 봉 시작 시각, nextBefore 는 다음 페이지 상한(inclusive 라
경계 봉이 중복된다).
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.toss_client import (
    TossApiError,
    TossClient,
)
from stock_mcp_server.market_data.toss_provider import TossBarProvider

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

_FIXTURES = Path(__file__).parent / "fixtures" / "toss"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text("utf-8"))


_TOKEN = _fixture("token_success.json")
_KR_1 = _fixture("kr_candles_page_1.json")
_KR_2 = _fixture("kr_candles_page_2.json")
_US_1 = _fixture("us_candles_page_1.json")
_US_2 = _fixture("us_candles_page_2.json")


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("toss").credential_schema,
        {"client_id": "cid", "client_secret": "csec"})


class PageServer:
    def __init__(self, pages):
        self.pages = list(pages)
        self.candle_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOKEN)
        assert request.url.path == "/api/v1/candles"
        self.candle_requests.append(request)
        item = self.pages.pop(0)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def _provider(server: PageServer, **kwargs) -> TossBarProvider:
    client = TossClient(
        _payload(), "real", transport=httpx.MockTransport(server.handler))
    return TossBarProvider(client, "real", **kwargs)


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="005930", market="KR", interval="1m",
        start=None, end=None, trading_date=date(2026, 8, 27),
        row_limit=120, venue="KRX", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _run(coro):
    return asyncio.run(coro)


class TossKrTests(unittest.TestCase):
    def test_kr_normalization_and_query_contract(self):
        server = PageServer([_KR_1])
        ds = _run(_provider(server).fetch_bars(_request(row_limit=3)))

        self.assertEqual(ds.provider, "toss")
        self.assertEqual(ds.timezone, "Asia/Seoul")
        self.assertEqual(ds.adjustment_basis, "unadjusted")
        self.assertEqual(ds.source_endpoint, "toss_candles")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1528", "1529", "1530"])
        self.assertEqual(ds.bars[-1].close, Decimal("70900"))
        self.assertEqual(ds.bars[-1].volume, 532100)
        self.assertEqual(
            ds.bars[-1].start_at,
            datetime(2026, 8, 27, 15, 30, tzinfo=KST))

        params = server.candle_requests[0].url.params
        self.assertEqual(params.get("symbol"), "005930")
        self.assertEqual(params.get("interval"), "1m")
        self.assertEqual(params.get("adjusted"), "false")
        self.assertLessEqual(int(params.get("count")), 200)
        # 과거 날짜 anchoring: before 가 요청 거래일 끝으로 고정된다.
        self.assertEqual(params.get("before"),
                         "2026-08-27T23:59:59+09:00")

    def test_pagination_inclusive_duplicate_deduped_and_prev_day_stop(self):
        server = PageServer([_KR_1, _KR_2])
        ds = _run(_provider(server).fetch_bars(_request()))
        second = server.candle_requests[1].url.params
        self.assertEqual(second.get("before"), "2026-08-27T15:28:00+09:00")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        # 경계 중복(15:28)은 한 번만, 전일(0826) 행은 채택하지 않는다.
        self.assertEqual(times, ["1527", "1528", "1529", "1530"])
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-27"})
        self.assertTrue(ds.coverage["complete"])
        self.assertEqual(server.pages, [])

    def test_session_filter(self):
        page = json.loads(json.dumps(_KR_1))
        page["result"]["candles"].insert(0, {
            "timestamp": "2026-08-27T16:10:00+09:00", "openPrice": "70950",
            "highPrice": "70950", "lowPrice": "70950",
            "closePrice": "70950", "volume": "100", "currency": "KRW"})
        page["result"]["candles"].append({
            "timestamp": "2026-08-27T08:55:00+09:00", "openPrice": "70800",
            "highPrice": "70800", "lowPrice": "70800",
            "closePrice": "70800", "volume": "100", "currency": "KRW"})
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertNotIn("1610", times)
        self.assertNotIn("0855", times)
        self.assertTrue(any("정규장" in w for w in ds.warnings))

    def test_currency_mismatch_row_dropped(self):
        page = json.loads(json.dumps(_KR_1))
        page["result"]["candles"][1]["currency"] = "USD"
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_malformed_row_dropped(self):
        page = json.loads(json.dumps(_KR_1))
        page["result"]["candles"][0]["closePrice"] = None
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_empty_result_is_out_of_coverage_warning(self):
        server = PageServer([
            {"result": {"candles": [], "nextBefore": None}}])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ds.bars, ())
        self.assertTrue(ds.coverage["complete"])

    def test_rate_limit_first_page_propagates(self):
        server = PageServer([httpx.Response(
            429, json={"error": {"code": "rate-limit-exceeded"}},
            headers={"Retry-After": "1"})])
        with self.assertRaises(TossApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")

    def test_rate_limit_mid_pagination_is_partial(self):
        server = PageServer([
            _KR_1,
            httpx.Response(429, json={
                "error": {"code": "rate-limit-exceeded"}}),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 3)
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["failure_status"], "rate_limited")

    def test_old_date_rows_only_gives_empty_with_warning(self):
        # 요청일 데이터가 공급 범위 밖이라 다른 날 행만 오는 경우.
        server = PageServer([_KR_1])
        ds = _run(_provider(server).fetch_bars(
            _request(trading_date=date(2026, 8, 20))))
        self.assertEqual(ds.bars, ())
        self.assertTrue(any("기준일" in w for w in ds.warnings))


class TossUsTests(unittest.TestCase):
    def test_us_normalization_eastern(self):
        server = PageServer([_US_1])
        ds = _run(_provider(server).fetch_bars(_request(
            symbol="AAPL", market="US", venue="NAS", row_limit=3)))
        self.assertEqual(ds.timezone, "America/New_York")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1558", "1559", "1600"])
        self.assertEqual(ds.bars[-1].close, Decimal("225.44"))
        self.assertEqual(
            ds.bars[0].start_at.utcoffset().total_seconds(), -4 * 3600)
        params = server.candle_requests[0].url.params
        self.assertEqual(params.get("symbol"), "AAPL")
        # US anchoring 은 뉴욕 시간대 오프셋으로 만든다 (여름 -04:00).
        self.assertEqual(params.get("before"),
                         "2026-08-27T23:59:59-04:00")

    def test_us_pagination_and_close_print(self):
        server = PageServer([_US_1, _US_2])
        ds = _run(_provider(server).fetch_bars(_request(
            symbol="AAPL", market="US", venue="NAS")))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1557", "1558", "1559", "1600"])
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-27"})

    def test_contract_guards(self):
        server = PageServer([])
        provider = _provider(server)
        with self.assertRaisesRegex(ValueError, "1m"):
            _run(provider.fetch_bars(_request(interval="5m")))
        with self.assertRaisesRegex(ValueError, "session"):
            _run(provider.fetch_bars(_request(session="extended")))
        with self.assertRaisesRegex(ValueError, "trading_date"):
            _run(provider.fetch_bars(_request(trading_date=None)))


if __name__ == "__main__":
    unittest.main()
