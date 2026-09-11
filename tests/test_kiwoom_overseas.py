"""키움 미국 1분봉 공급자 테스트 (1.0 Task 14, 2026-08-28 실측 재개정).

공식 스펙(usa06011) + 실계좌 실측(2026-08-28 lag 스캔)으로 확정:
- cntr_tm 은 **미국 동부시각(ET) 라벨, 봉 시작**이다. ET 그대로
  해석하면 KIS 기준과 lag 0 에서 391/391분 완전 일치한다
  (median|dClose|=0.0000, 거래량 상관 1.0). 이전의 "KST 라벨" 해석은
  오독이었고, 그 해석이 읽던 22:30~05:00 행은 실제로는 미국
  오버나이트 세션(ET) 데이터였다.
- 응답은 24시간 스트림이다: 정규장(09:30~16:00, 마감 print 포함
  391행) + 프리장 + 애프터 + 오버나이트. 정규장 밖 행은 버린다.
- bus_dt 가 미국 영업일자다. 요청 거래일 필터의 기준이다.
- strt_dt 는 ET 달력 날짜 필터다. 거래일 D 를 그대로 넣으면 D 23:59
  ET 에서 시작해 과거로 페이지네이션한다 (D+1 앵커 불필요).
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


class EtLabelContractTests(unittest.TestCase):
    """2026-08-28 확정 계약: cntr_tm=ET 라벨, strt_dt=ET 달력 D 앵커."""

    def test_et_labels_and_session_filter(self):
        server = PageServer([(_PAGE_1, "Y", "K1"), (_PAGE_2, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        # 18:05 애프터 행은 버려지고 정규장 4행(15:57~16:00)만 남는다.
        self.assertEqual(
            [b.start_at for b in ds.bars],
            [datetime(2026, 8, 26, 15, 57, tzinfo=NY),
             datetime(2026, 8, 26, 15, 58, tzinfo=NY),
             datetime(2026, 8, 26, 15, 59, tzinfo=NY),
             datetime(2026, 8, 26, 16, 0, tzinfo=NY)])
        close_print = ds.bars[-1]
        self.assertEqual(close_print.close, Decimal("225.4400"))
        self.assertEqual(close_print.volume, 1250000)
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertIn("정규장 밖 행 1개", " ".join(ds.warnings))

    def test_strt_dt_is_trading_date_itself(self):
        server = PageServer([(_PAGE_1, "Y", "K1"), (_PAGE_2, None, None)])
        _run(_provider(server).fetch_bars(_request()))
        body = json.loads(server.chart_requests[0].content)
        # ET 달력 필터: D+1 앵커가 아니라 거래일 그대로.
        self.assertEqual(body["strt_dt"], "20260826")
        self.assertEqual(body["stex_tp"], "ND")
        self.assertEqual(body["exrt_appl_tp"], "0")

    def test_stops_when_earlier_bus_dt_reached(self):
        server = PageServer([(_PAGE_1, "Y", "K1"), (_PAGE_2, "Y", "K2")])
        ds = _run(_provider(server).fetch_bars(_request()))
        # page2 의 bus_dt=20260825 행에서 요청일 구간이 끝났음을 알고
        # 더 페이지를 당기지 않는다.
        self.assertEqual(len(server.chart_requests), 2)
        self.assertTrue(ds.coverage["complete"])

    def test_unknown_symbol_code7_1903_is_entity_not_found(self):
        # 실측(2026-08-28): 없는 종목은 return_code 7 + return_msg 에
        # 내부 코드 1903(종목 정보가 없습니다)이 온다.
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        resp = {"return_code": 7,
                "return_msg": ("서비스를 처리하는 중에 오류가 발생했습니다"
                               "[1903:종목 정보가 없습니다. 입력한 "
                               "종목코드, 거래소구분 값을 확인바랍니다.]"),
                "result_list": []}
        server = PageServer([(resp, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")

    def test_generic_code7_stays_provider_unavailable(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        resp = {"return_code": 7, "return_msg": "일시적인 오류",
                "result_list": []}
        server = PageServer([(resp, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")

    def test_missing_symbol_blank_row_is_entity_not_found(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        blank = {"return_code": 0, "return_msg": "정상",
                 "result_list": [{"cntr_tm": "", "bus_dt": "",
                                  "cur_prc": "", "open_pric": "",
                                  "high_pric": "", "low_pric": "",
                                  "trde_qty": ""}]}
        server = PageServer([(blank, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")

    def test_verifier_probes_us_chart(self):
        from stock_mcp_server.market_data.kiwoom_verifier import (
            KiwoomVerifier,
        )
        seen_paths = []

        def handler(request):
            seen_paths.append(request.url.path)
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=_TOKEN)
            if request.url.path == "/api/us/chart":
                return httpx.Response(200, json=_PAGE_1)
            return httpx.Response(200, json={
                "return_code": 0,
                "stk_min_pole_chart_qry": [{"cur_prc": "+70000",
                                            "cntr_tm": "20260827100000"}]})

        verifier = KiwoomVerifier(transport=httpx.MockTransport(handler))
        result = _run(verifier.verify(_payload(), "real"))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "available")
        self.assertEqual(result["us_intraday"], "available")
        self.assertIn("/api/us/chart", seen_paths)


if __name__ == "__main__":
    unittest.main()
