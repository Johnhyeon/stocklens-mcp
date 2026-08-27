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


class KiwoomOverseasTests(unittest.TestCase):
    def test_kst_labels_converted_to_eastern(self):
        # cntr_tm 20260827050000(KST) = ET 2026-08-26 16:00 마감 print.
        server = PageServer([(_PAGE_1, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))

        self.assertEqual(ds.provider, "kiwoom")
        self.assertEqual(ds.venue, "NAS")
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertEqual(ds.source_endpoint, "kiwoom_us_minute")
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1558", "1559", "1600"])
        self.assertEqual(
            ds.bars[-1].start_at,
            datetime(2026, 8, 26, 16, 0, tzinfo=NY))
        # 여름(EDT): UTC-4.
        self.assertEqual(
            ds.bars[0].start_at.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(ds.bars[-1].close, Decimal("225.4400"))

        body = json.loads(server.chart_requests[0].content)
        self.assertEqual(body["stex_tp"], "ND")
        self.assertEqual(body["stk_cd"], "AAPL")
        # 실측: strt_dt 는 KST 달력 날짜다. 미국 영업일 26일의 세션
        # 후반은 KST 27일에 있으므로 27일로 anchoring 한다.
        self.assertEqual(body["strt_dt"], "20260827")
        self.assertEqual(body["tic_scope"], "1")
        self.assertEqual(body["upd_stkpc_tp"], "0")
        self.assertEqual(
            server.chart_requests[0].headers["api-id"], "usa06011")

    def test_pagination_stops_at_prior_bus_dt(self):
        server = PageServer([
            (_PAGE_1, "Y", "us-key-1"),
            (_PAGE_2, "Y", "us-key-2"),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))
        second = server.chart_requests[1]
        self.assertEqual(second.headers["next-key"], "us-key-1")
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-26"})
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1557", "1558", "1559", "1600"])
        self.assertEqual(server.pages, [])

    def test_bus_dt_filter_rejects_other_business_days(self):
        # 과거일 요청에 최신 영업일 행이 섞여 와도 채택하지 않는다.
        server = PageServer([(_PAGE_1, None, None)])
        ds = _run(_provider(server).fetch_bars(
            _request(trading_date=date(2026, 8, 20))))
        self.assertEqual(ds.bars, ())
        self.assertTrue(any("기준일" in w for w in ds.warnings))

    def test_session_filter_drops_premarket_and_afterhours(self):
        page = json.loads(json.dumps(_PAGE_1))
        # 애프터마켓: KST 27일 05:10 = ET 26일 16:10
        page["result_list"].insert(0, {
            "cntr_tm": "20260827051000", "bus_dt": "20260826",
            "cur_prc": "225.5000", "open_pric": "225.5000",
            "high_pric": "225.5000", "low_pric": "225.5000",
            "trde_qty": "100", "upd_stkpc_tp": "0"})
        # 프리장: KST 26일 22:15 = ET 26일 09:15
        page["result_list"].append({
            "cntr_tm": "20260826221500", "bus_dt": "20260826",
            "cur_prc": "224.0000", "open_pric": "224.0000",
            "high_pric": "224.0000", "low_pric": "224.0000",
            "trde_qty": "100", "upd_stkpc_tp": "0"})
        server = PageServer([(page, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertNotIn("1610", times)
        self.assertNotIn("0915", times)
        self.assertIn("1600", times)  # 마감 체결 print 는 포함한다

    def test_winter_date_uses_est_offset(self):
        # 겨울(EST, UTC-5): ET 2026-01-15 15:58 = KST 2026-01-16 05:58.
        page = json.loads(json.dumps(_PAGE_1))
        for row, kst in zip(page["result_list"],
                            ("20260116060000", "20260116055900",
                             "20260116055800")):
            row["cntr_tm"] = kst
            row["bus_dt"] = "20260115"
        server = PageServer([(page, None, None)])
        ds = _run(_provider(server).fetch_bars(
            _request(trading_date=date(2026, 1, 15))))
        body = json.loads(server.chart_requests[0].content)
        self.assertEqual(body["strt_dt"], "20260116")
        self.assertEqual(
            ds.bars[0].start_at.utcoffset().total_seconds(), -5 * 3600)
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1558", "1559", "1600"])

    def test_partial_on_page_error(self):
        server = PageServer([
            (_PAGE_1, "Y", "us-key-1"),
            (httpx.Response(500, json={}), None, None),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 3)
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["resume_cursor"], "us-key-1")

    def test_unknown_symbol_empty_field_row_is_entity_not_found(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        page = {"return_code": 0, "result_list": [{
            "cur_prc": "", "trde_qty": "", "cntr_tm": "", "bus_dt": "",
            "open_pric": "", "high_pric": "", "low_pric": ""}]}
        server = PageServer([(page, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")

    def test_unknown_venue_and_class_shares_error(self):
        server = PageServer([])
        provider = _provider(server)
        with self.assertRaises(KiwoomSymbolMappingError):
            _run(provider.fetch_bars(_request(venue="LSE")))
        with self.assertRaises(KiwoomSymbolMappingError):
            _run(provider.fetch_bars(_request(symbol="BRK.B")))
        self.assertEqual(server.chart_requests, [])

    def test_contract_guards(self):
        server = PageServer([])
        provider = _provider(server)
        with self.assertRaisesRegex(ValueError, "1m"):
            _run(provider.fetch_bars(_request(interval="5m")))
        with self.assertRaisesRegex(ValueError, "US"):
            _run(provider.fetch_bars(_request(market="KR", venue="KRX")))
        with self.assertRaisesRegex(ValueError, "session"):
            _run(provider.fetch_bars(_request(session="daytime")))
        with self.assertRaisesRegex(ValueError, "trading_date"):
            _run(provider.fetch_bars(_request(trading_date=None)))


if __name__ == "__main__":
    unittest.main()
