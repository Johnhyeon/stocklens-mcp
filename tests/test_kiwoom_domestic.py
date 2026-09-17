"""키움 국내(KRX) 1분봉 공급자 테스트 (1.0 Task 13).

공식 스펙(ka10080): POST /api/dostk/chart, api-id 헤더, base_dt 기준일,
stk_min_pole_chart_qry 역순 리스트, 부호 붙은 가격 문자열, cont-yn /
next-key 헤더 연속조회.
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

from stock_mcp_server.market_data.kiwoom_client import (
    KiwoomApiError,
    KiwoomClient,
)
from stock_mcp_server.market_data.kiwoom_domestic import (
    KiwoomDomesticProvider,
)
from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

KST = ZoneInfo("Asia/Seoul")

_FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
_PAGE_1 = json.loads(
    (_FIXTURES / "domestic_minute_page_1.json").read_text("utf-8"))
_PAGE_2 = json.loads(
    (_FIXTURES / "domestic_minute_page_2.json").read_text("utf-8"))
_EMPTY = json.loads(
    (_FIXTURES / "domestic_empty.json").read_text("utf-8"))
_TOKEN = json.loads((_FIXTURES / "token_success.json").read_text("utf-8"))


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": "k", "secret_key": "s"})


class PageServer:
    """페이지 시퀀스를 cont-yn/next-key 헤더와 함께 제공한다."""

    def __init__(self, pages):
        # pages: [(payload_or_exc, cont_yn, next_key)]
        self.pages = list(pages)
        self.chart_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOKEN)
        assert request.url.path == "/api/dostk/chart"
        self.chart_requests.append(request)
        payload, cont_yn, next_key = self.pages.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, httpx.Response):
            return payload
        headers = {}
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        return httpx.Response(200, json=payload, headers=headers)


def _provider(server: PageServer, **kwargs) -> KiwoomDomesticProvider:
    client = KiwoomClient(
        _payload(), "real", transport=httpx.MockTransport(server.handler))
    return KiwoomDomesticProvider(client, "real", **kwargs)


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


class KiwoomDomesticTests(unittest.TestCase):
    def test_minute_normalization_signed_prices_ascending(self):
        server = PageServer([(_PAGE_1, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))

        self.assertEqual(ds.provider, "kiwoom")
        self.assertEqual(ds.venue, "KRX")
        self.assertEqual(ds.timezone, "Asia/Seoul")
        self.assertEqual(ds.source_interval, "1m")
        self.assertEqual(ds.adjustment_basis, "unadjusted")
        self.assertEqual(ds.source_endpoint, "kiwoom_kr_minute")

        # 역순 응답이 오름차순으로 정규화된다.
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1528", "1529", "1530"])
        last = ds.bars[-1]
        # 부호는 등락 표시일 뿐이다. 가격은 절대값이다.
        self.assertEqual(last.close, Decimal("70900"))
        self.assertEqual(last.open, Decimal("71000"))
        self.assertEqual(last.volume, 532100)
        self.assertEqual(
            last.start_at, datetime(2026, 8, 27, 15, 30, tzinfo=KST))

        # api-id 헤더와 base_dt 를 확인한다.
        req = server.chart_requests[0]
        self.assertEqual(req.headers["api-id"], "ka10080")
        body = json.loads(req.content)
        self.assertEqual(body["stk_cd"], "005930")
        self.assertEqual(body["tic_scope"], "1")
        self.assertEqual(body["upd_stkpc_tp"], "0")
        self.assertEqual(body["base_dt"], "20260827")

    def test_pagination_uses_cont_headers_and_stops_at_prev_day(self):
        server = PageServer([
            (_PAGE_1, "Y", "key-1"),
            (_PAGE_2, "Y", "key-2"),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))

        # 두 번째 요청에 cont-yn/next-key 가 실린다.
        second = server.chart_requests[1]
        self.assertEqual(second.headers["cont-yn"], "Y")
        self.assertEqual(second.headers["next-key"], "key-1")

        # page 2 의 전일(0826) 행은 채택하지 않고 그 자리에서 멈춘다.
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-27"})
        # 중복 15:27 행은 dedupe 된다.
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1527", "1528", "1529", "1530"])
        self.assertTrue(ds.coverage["complete"])
        self.assertEqual(server.pages, [])  # 3번째 요청 없음

    def test_no_more_pages_when_cont_yn_absent(self):
        server = PageServer([(_PAGE_1, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(server.chart_requests), 1)
        self.assertTrue(ds.coverage["complete"])

    def test_session_filter_drops_out_of_session_rows(self):
        page = json.loads(json.dumps(_PAGE_1))
        page["stk_min_pole_chart_qry"].append(
            {"cntr_tm": "20260827085900", "cur_prc": "+70800",
             "open_pric": "70800", "high_pric": "70800",
             "low_pric": "70800", "trde_qty": "100", "upd_stkpc_tp": "0"})
        page["stk_min_pole_chart_qry"].insert(0,
            {"cntr_tm": "20260827160000", "cur_prc": "+70950",
             "open_pric": "70950", "high_pric": "70950",
             "low_pric": "70950", "trde_qty": "200", "upd_stkpc_tp": "0"})
        server = PageServer([(page, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertNotIn("0859", times)
        self.assertNotIn("1600", times)
        self.assertIn("1530", times)  # 마감 동시호가 행은 세션 안이다

    def test_malformed_row_dropped_with_warning(self):
        page = json.loads(json.dumps(_PAGE_1))
        page["stk_min_pole_chart_qry"][1]["cur_prc"] = None
        server = PageServer([(page, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_empty_holiday_returns_empty_complete(self):
        server = PageServer([(_EMPTY, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ds.bars, ())
        self.assertTrue(ds.coverage["complete"])

    def test_partial_on_second_page_error_keeps_first_page(self):
        server = PageServer([
            (_PAGE_1, "Y", "key-1"),
            (httpx.Response(500, json={}), None, None),
        ])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 3)
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["failure_status"],
                         "provider_unavailable")
        self.assertEqual(ds.coverage["resume_cursor"], "key-1")
        self.assertTrue(any("이미 받은" in w for w in ds.warnings))

    def test_first_page_error_raises(self):
        server = PageServer([(httpx.Response(500, json={}), None, None)])
        with self.assertRaises(KiwoomApiError):
            _run(_provider(server).fetch_bars(_request()))

    def test_body_error_code_first_page_raises(self):
        server = PageServer([
            ({"return_code": 1505, "return_msg": "no api"}, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")

    def test_all_rows_unparseable_raises_parse_error(self):
        page = {"return_code": 0,
                "stk_min_pole_chart_qry": [{"broken": True}]}
        server = PageServer([(page, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "source_parse_error")

    def test_empty_field_row_is_no_data_not_entity_not_found(self):
        # 실측(2026-09-17): 없는 종목(999999)과 보관 기간 밖 날짜(005930
        # 2025-08-29)가 똑같이 return_code 0 + 전 필드 빈 문자열 1행이다.
        # '종목 없음'으로 단정하면 멀쩡한 종목이 없는 종목처럼 읽힌다.
        # KIS(행 0개)와 같이 빈 결과여야 한다.
        page = {"return_code": 0, "return_msg": "정상적으로 처리되었습니다",
                "stk_cd": "005930",
                "stk_min_pole_chart_qry": [{
                    "cur_prc": "", "trde_qty": "", "cntr_tm": "",
                    "open_pric": "", "high_pric": "", "low_pric": "",
                    "acc_trde_qty": "", "pred_pre": "",
                    "pred_pre_sig": ""}]}
        server = PageServer([(page, None, None)])
        ds = _run(_provider(server).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 0)
        self.assertTrue(ds.coverage["complete"])
        self.assertEqual(len(server.chart_requests), 1)

    def test_row_limit_keeps_recent(self):
        server = PageServer([(_PAGE_1, None, None)])
        ds = _run(_provider(server).fetch_bars(_request(row_limit=2)))
        times = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(times, ["1529", "1530"])

    def test_page_budget_marks_partial(self):
        server = PageServer([
            (_PAGE_1, "Y", "key-1"),
            (_PAGE_1, "Y", "key-2"),
        ])
        ds = _run(_provider(server, max_pages=2).fetch_bars(_request()))
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["resume_cursor"], "key-2")

    def test_contract_guards(self):
        server = PageServer([])
        provider = _provider(server)
        with self.assertRaisesRegex(ValueError, "1m"):
            _run(provider.fetch_bars(_request(interval="5m")))
        with self.assertRaisesRegex(ValueError, "KR"):
            _run(provider.fetch_bars(_request(market="US", venue="NAS")))
        with self.assertRaisesRegex(ValueError, "session"):
            _run(provider.fetch_bars(_request(session="extended")))
        with self.assertRaisesRegex(ValueError, "trading_date"):
            _run(provider.fetch_bars(_request(trading_date=None)))


if __name__ == "__main__":
    unittest.main()
