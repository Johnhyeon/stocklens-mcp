"""키움 미국 1분봉 공급자 테스트 (1.0 Task 14, 2026-08-27 실측 개정판).

공식 스펙(usa06011) + 실계좌 실측으로 확정한 semantics:
- cntr_tm 은 **한국 시각(KST) 라벨**이다 (실측: ET 09:11 프리장 행이
  20260826221100 으로 옴). ET 로 변환해 저장한다.
- bus_dt 가 미국 영업일자다. 요청 거래일 필터의 기준이다.
- strt_dt 는 KST 달력 날짜 필터다. 미국 영업일 D 의 세션은 KST 로
  D 22:30~D+1 05:00(EDT 기준)에 걸치므로 D+1 을 넣고 과거로
  페이지네이션한다.
- 페이지 크기 실측 100행. 하루 세션(391행)은 항상 다페이지다.
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

from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
from stock_mcp_server.market_data.kiwoom_overseas import (
    KiwoomOverseasProvider,
)
from stock_mcp_server.market_data.kiwoom_symbols import (
    KiwoomSymbolMappingError,
    to_stex_tp,
    validate_ticker,
)
from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

NY = ZoneInfo("America/New_York")

_FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
_PAGE_1 = json.loads(
    (_FIXTURES / "us_minute_page_1.json").read_text("utf-8"))
_PAGE_2 = json.loads(
    (_FIXTURES / "us_minute_page_2.json").read_text("utf-8"))
_TOKEN = json.loads((_FIXTURES / "token_success.json").read_text("utf-8"))


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": "k", "secret_key": "s"})


class PageServer:
    def __init__(self, pages):
        self.pages = list(pages)
        self.chart_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOKEN)
        assert request.url.path == "/api/us/chart"
        self.chart_requests.append(request)
        payload, cont_yn, next_key = self.pages.pop(0)
        if isinstance(payload, httpx.Response):
            return payload
        headers = {}
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        return httpx.Response(200, json=payload, headers=headers)


def _provider(server: PageServer, **kwargs) -> KiwoomOverseasProvider:
    client = KiwoomClient(
        _payload(), "real", transport=httpx.MockTransport(server.handler))
    return KiwoomOverseasProvider(client, "real", **kwargs)


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="AAPL", market="US", interval="1m",
        start=None, end=None, trading_date=date(2026, 8, 26),
        row_limit=120, venue="NAS", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _run(coro):
    return asyncio.run(coro)


class SymbolMappingTests(unittest.TestCase):
    def test_venue_to_stex_tp(self):
        self.assertEqual(to_stex_tp("NAS"), "ND")
        self.assertEqual(to_stex_tp("NYS"), "NY")
        self.assertEqual(to_stex_tp("AMS"), "NA")

    def test_unknown_venue_is_structured_error(self):
        for bad in ("KRX", "LSE", "", None):
            with self.assertRaises(KiwoomSymbolMappingError):
                to_stex_tp(bad)

    def test_ticker_validation(self):
        self.assertEqual(validate_ticker("aapl"), "AAPL")
        self.assertEqual(validate_ticker("TSLA"), "TSLA")

    def test_class_share_forms_are_not_guessed(self):
        for bad in ("BRK.B", "BRK/B", "BF-B", "AAPL US", ""):
            with self.assertRaises(KiwoomSymbolMappingError):
                validate_ticker(bad)


class KiwoomUsBlockedTests(unittest.TestCase):
    """2026-08-27 실계좌 실측: 키움 US 분봉은 데이터 계약 불일치.

    AAPL 완결일(08-26) 전수 대조에서:
    - 공통 82분 전부 종가 불일치 (예 09:31 KIS 309.39 vs 키움 310.99)
    - 과거일(08-20) 시가: 야후 317.46 = KIS 317.46, 키움만 311.84
      (독립 기준 2개가 일치, 키움만 반증됨)
    - 거래량 비율 0.0004~0.002 (주수 단위가 아님, 26일 09:30 KIS
      491,063주 vs 키움 171)
    - 커버리지가 ET ~11:00 에서 절단 (완결일 82/391분)
    계약이 규명·검증되기 전까지 US 요청은 거부한다. 추측 보정 금지.
    """

    def test_us_fetch_is_rejected_as_unsupported(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        server = PageServer([])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "unsupported")
        self.assertEqual(server.chart_requests, [])

    def test_verifier_reports_us_unavailable_without_probe(self):
        from stock_mcp_server.market_data.kiwoom_verifier import (
            KiwoomVerifier,
        )
        seen_paths = []

        def handler(request):
            seen_paths.append(request.url.path)
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=_TOKEN)
            return httpx.Response(200, json={
                "return_code": 0,
                "stk_min_pole_chart_qry": [{"cur_prc": "+70000",
                                            "cntr_tm": "20260827100000"}]})

        verifier = KiwoomVerifier(transport=httpx.MockTransport(handler))
        result = _run(verifier.verify(_payload(), "real"))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["us_intraday"], "unavailable")
        self.assertNotIn("/api/us/chart", seen_paths)


if __name__ == "__main__":
    unittest.main()
