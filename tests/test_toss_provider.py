"""토스 1분 캔들 공급자 테스트 (1.0 Task 16, 2026-08-27 실측 반영 개정).

공식 스펙: GET /api/v1/candles?symbol&interval=1m&count(<=200)
&before(inclusive ISO)&adjusted=false. 응답 result.candles 최신순,
nextBefore 는 다음 페이지 상한(inclusive 라 경계 봉이 중복된다).

KR 은 실계좌 실측(2026-08-27)에서 정규장 계약 불일치가 확정되어
코드로 차단한다 (TossKrBlockedTests 참조). US 만 검증 대상이다.
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

NY = ZoneInfo("America/New_York")

_FIXTURES = Path(__file__).parent / "fixtures" / "toss"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text("utf-8"))


_TOKEN = _fixture("token_success.json")
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
        symbol="AAPL", market="US", interval="1m",
        start=None, end=None, trading_date=date(2026, 8, 27),
        row_limit=120, venue="NAS", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _kr_request(**overrides) -> BarRequest:
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


class TossKrBlockedTests(unittest.TestCase):
    """2026-08-27 실계좌 실측: 토스 KR 캔들은 정규장 계약과 불일치.

    - timestamp 가 문서와 달리 봉 끝 라벨로 동작 (KIS 대비 1분 시프트,
      210/381분에서 toss[t+1] OHLC == kis[t], 09:00 bar 거래량 0)
    - 거래량이 KRX 단독이 아님 (통합 추정, 일치 구간 중앙값 1.37배)
    - 15:30 마감 동시호가 print 미포함 (마지막 종가 265,000 != 공식
      종가 266,000)
    검증될 때까지 KR 은 코드로 차단한다. 추측 보정 금지.
    """

    def test_kr_fetch_is_rejected_with_reason(self):
        server = PageServer([])
        with self.assertRaises(TossApiError) as ctx:
            _run(_provider(server).fetch_bars(_kr_request()))
        self.assertEqual(ctx.exception.provider_status, "not_configured")
        self.assertEqual(server.candle_requests, [])

    def test_kr_capability_not_advertised(self):
        server = PageServer([])
        caps = _run(_provider(server).capabilities("real"))
        self.assertNotIn("KR", caps.markets)

    def test_verifier_reports_kr_unavailable(self):
        from stock_mcp_server.market_data.toss_verifier import TossVerifier
        seen_symbols = []

        def handler(request):
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=_TOKEN)
            seen_symbols.append(request.url.params.get("symbol"))
            return httpx.Response(200, json={
                "result": {"candles": [], "nextBefore": None}})

        verifier = TossVerifier(transport=httpx.MockTransport(handler))
        result = _run(verifier.verify(_payload(), "real"))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "unavailable")
        # KR 종목 probe 자체를 하지 않는다 (판정이 코드로 고정됨).
        self.assertNotIn("005930", seen_symbols)
        self.assertEqual(result["us_intraday"], "available")


class TossUsTests(unittest.TestCase):
    def test_us_normalization_and_query_contract(self):
        server = PageServer([_US_1])
        ds = _run(_provider(server).fetch_bars(_request(row_limit=3)))

        self.assertEqual(ds.provider, "toss")
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertEqual(ds.adjustment_basis, "unadjusted")
        self.assertEqual(ds.source_endpoint, "toss_candles")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1558", "1559", "1600"])
        self.assertEqual(ds.bars[-1].close, Decimal("225.44"))
        self.assertEqual(
            ds.bars[0].start_at.utcoffset().total_seconds(), -4 * 3600)

        params = server.candle_requests[0].url.params
        self.assertEqual(params.get("symbol"), "AAPL")
        self.assertEqual(params.get("interval"), "1m")
        self.assertEqual(params.get("adjusted"), "false")
        self.assertLessEqual(int(params.get("count")), 200)
        # 과거 날짜 anchoring: before 가 요청 거래일 끝으로 고정된다.
        self.assertEqual(params.get("before"),
                         "2026-08-27T23:59:59-04:00")

    def test_pagination_inclusive_duplicate_deduped_and_prev_day_stop(self):
        server = PageServer([_US_1, _US_2])
        ds = _run(_provider(server).fetch_bars(_request()))
        second = server.candle_requests[1].url.params
        self.assertEqual(second.get("before"), "2026-08-27T15:58:00-04:00")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        # 경계 중복(15:58)은 한 번만, 전일(0826) 행은 채택하지 않는다.
        self.assertEqual(times, ["1557", "1558", "1559", "1600"])
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-27"})
        self.assertTrue(ds.coverage["complete"])
        self.assertEqual(server.pages, [])

    def test_session_filter(self):
        page = json.loads(json.dumps(_US_1))
        page["result"]["candles"].insert(0, {
            "timestamp": "2026-08-27T16:05:00-04:00",
            "openPrice": "225.50", "highPrice": "225.50",
            "lowPrice": "225.50", "closePrice": "225.50",
            "volume": "100", "currency": "USD"})
        page["result"]["candles"].append({
            "timestamp": "2026-08-27T09:15:00-04:00",
            "openPrice": "224.00", "highPrice": "224.00",
            "lowPrice": "224.00", "closePrice": "224.00",
            "volume": "100", "currency": "USD"})
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertNotIn("1605", times)
        self.assertNotIn("0915", times)
        self.assertIn("1600", times)  # 마감 print 는 세션 안이다
        self.assertTrue(any("정규장" in w for w in ds.warnings))

    def test_currency_mismatch_row_dropped(self):
        page = json.loads(json.dumps(_US_1))
        page["result"]["candles"][1]["currency"] = "KRW"
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_malformed_row_dropped(self):
        page = json.loads(json.dumps(_US_1))
        page["result"]["candles"][0]["closePrice"] = None
        page["result"]["nextBefore"] = None
        server = PageServer([page])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_empty_result_is_complete_empty(self):
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
            _US_1,
            httpx.Response(429, json={
                "error": {"code": "rate-limit-exceeded"}}),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 3)
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["failure_status"], "rate_limited")

    def test_old_date_rows_only_gives_empty_with_warning(self):
        # 요청일 데이터가 공급 범위 밖이라 다른 날 행만 오는 경우.
        server = PageServer([_US_1])
        ds = _run(_provider(server).fetch_bars(
            _request(trading_date=date(2026, 8, 20))))
        self.assertEqual(ds.bars, ())
        self.assertTrue(any("기준일" in w for w in ds.warnings))

    def test_winter_anchor_uses_est_offset(self):
        server = PageServer([
            {"result": {"candles": [], "nextBefore": None}}])
        _run(_provider(server).fetch_bars(
            _request(trading_date=date(2026, 1, 15))))
        params = server.candle_requests[0].url.params
        self.assertEqual(params.get("before"),
                         "2026-01-15T23:59:59-05:00")

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
