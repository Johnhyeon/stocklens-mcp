"""KIS 국내 분봉 공급자 테스트 (Task 7). 전부 fixture 기반, 네트워크 없음.

검증: 역순 정규화, 페이지 경계 중복 제거, malformed 행 처리, 휴장 빈 응답,
cursor 무진행 보호, 일부 페이지 실패 시 partial + 재개 cursor,
실전·모의 능력 차이.
"""

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
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient
from stock_mcp_server.market_data.kis_domestic import KisDomesticProvider
from stock_mcp_server.market_data.models import BarRequest

KST = ZoneInfo("Asia/Seoul")
FIXTURES = Path(__file__).parent / "fixtures" / "kis"

PAGE_1 = json.loads((FIXTURES / "domestic_minute_page_1.json").read_text("utf-8"))
PAGE_2 = json.loads((FIXTURES / "domestic_minute_page_2.json").read_text("utf-8"))


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="005930", market="KR", interval="1m",
        start=None, end=None, trading_date=date(2026, 8, 27),
        row_limit=500, venue="KRX", session="regular",
        adjustment="unadjusted", completed_only=True, source="kis",
    )
    base.update(overrides)
    return BarRequest(**base)


class PagedHandler:
    """cursor(FID_INPUT_HOUR_1)별 응답을 시나리오로 정의하는 fake transport."""

    def __init__(self, pages: dict):
        # pages: cursor hour -> dict(JSON) | httpx.Response | Exception
        self.pages = pages
        self.api_calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json={
                "access_token": "fixture-token", "expires_in": 86400})
        cursor = request.url.params.get("FID_INPUT_HOUR_1")
        self.api_calls.append(cursor)
        item = self.pages.get(cursor)
        if item is None:
            return httpx.Response(200, json={"rt_cd": "0", "output2": []})
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


class EndlessHandler(PagedHandler):
    """어떤 cursor 로 물어도 그 시각부터 1분 간격 3행을 계속 돌려준다."""

    def __init__(self):
        super().__init__({})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json={
                "access_token": "fixture-token", "expires_in": 86400})
        cursor = request.url.params.get("FID_INPUT_HOUR_1")
        self.api_calls.append(cursor)
        base = datetime.strptime(cursor, "%H%M%S")
        rows = []
        for i in range(3):
            minute = base.replace(second=0)
            minute = minute.replace(
                hour=(base.hour * 60 + base.minute - i) // 60,
                minute=(base.hour * 60 + base.minute - i) % 60)
            rows.append({
                "stck_bsop_date": "20260827",
                "stck_cntg_hour": minute.strftime("%H%M%S"),
                "stck_oprc": "70000", "stck_hgpr": "70100",
                "stck_lwpr": "69900", "stck_prpr": "70050",
                "cntg_vol": "100",
            })
        return httpx.Response(200, json={"rt_cd": "0", "output2": rows})


def _provider(handler: PagedHandler, profile: str = "real",
              **kwargs) -> KisDomesticProvider:
    client = KisClient(
        credentials=BrokerCredentials(app_key="k", app_secret="s"),
        profile=profile,
        transport=httpx.MockTransport(handler),
    )
    return KisDomesticProvider(client=client, profile=profile, **kwargs)


def _run(coro):
    return asyncio.run(coro)


class FetchNormalizationTests(unittest.TestCase):
    def test_two_pages_normalized_ascending_with_boundary_dedup(self):
        handler = PagedHandler({
            "153000": PAGE_1,   # 첫 cursor 는 세션 마감
            "093200": PAGE_2,   # 다음 cursor = 최소시각(093300) - 1분
        })
        provider = _provider(handler)
        ds = _run(provider.fetch_bars(_request()))

        self.assertEqual(ds.provider, "kis")
        self.assertEqual(ds.profile, "real")
        self.assertEqual(ds.timezone, "Asia/Seoul")
        self.assertEqual(ds.source_interval, "1m")
        times = [b.start_at for b in ds.bars]
        self.assertEqual(times, sorted(times))
        # 093300 중복은 하나만 남는다.
        self.assertEqual(
            [b.start_at.strftime("%H%M") for b in ds.bars],
            ["0931", "0932", "0933", "0934", "0935"])
        self.assertTrue(any("중복" in w for w in ds.warnings))

        first = ds.bars[0]
        self.assertEqual(first.start_at,
                         datetime(2026, 8, 27, 9, 31, tzinfo=KST))
        self.assertEqual(first.end_at,
                         datetime(2026, 8, 27, 9, 32, tzinfo=KST))
        self.assertEqual(int(first.volume), 700)
        self.assertEqual(str(first.close), "70100")

    def test_malformed_numeric_row_dropped_with_warning(self):
        page = copy.deepcopy(PAGE_1)
        page["output2"][1]["stck_oprc"] = "not-a-number"
        handler = PagedHandler({"153000": page})
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(len(ds.bars), 2)
        self.assertTrue(any("해석" in w for w in ds.warnings))

    def test_all_rows_malformed_raises_parse_error(self):
        page = copy.deepcopy(PAGE_1)
        for row in page["output2"]:
            row["stck_prpr"] = "x"
        handler = PagedHandler({"153000": page})
        with self.assertRaises(KisApiError) as ctx:
            _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "source_parse_error")

    def test_holiday_empty_response_returns_empty_dataset(self):
        handler = PagedHandler({})  # 모든 cursor 에 빈 output2
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(ds.bars, ())
        self.assertEqual(ds.coverage["returned_rows"], 0)

    def test_rt_cd_error_maps_to_provider_error(self):
        handler = PagedHandler({
            "153000": {"rt_cd": "1", "msg_cd": "EGW00121", "output2": []},
        })
        with self.assertRaises(KisApiError) as ctx:
            _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "provider_unavailable")


class PaginationSafetyTests(unittest.TestCase):
    def test_no_progress_cursor_stops_with_warning(self):
        # 같은 페이지를 계속 돌려줘도 무한 pagination 에 빠지지 않아야 한다.
        page = copy.deepcopy(PAGE_1)
        handler = PagedHandler({
            "153000": page,
            "093200": page,  # cursor 가 뒤로 가지 않는 응답
        })
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertLessEqual(len(handler.api_calls), 3)
        self.assertTrue(any("진행" in w for w in ds.warnings))

    def test_max_pages_returns_partial_with_resume_cursor(self):
        handler = EndlessHandler()
        provider = _provider(handler, max_pages=2)
        ds = _run(provider.fetch_bars(_request()))
        self.assertFalse(ds.coverage["complete"])
        self.assertIn("resume_cursor", ds.coverage)
        self.assertLessEqual(len(handler.api_calls), 2)

    def test_partial_page_failure_preserves_accepted_bars_and_cursor(self):
        handler = PagedHandler({
            "153000": PAGE_1,
            "093200": httpx.Response(429, json={}),
        })
        ds = _run(_provider(handler).fetch_bars(_request()))
        # 이미 받은 봉은 유지하고 partial 로 표시한다. 다른 공급원으로
        # 메우는 것은 라우터 차원에서 금지된다.
        self.assertEqual(len(ds.bars), 3)
        self.assertFalse(ds.coverage["complete"])
        self.assertEqual(ds.coverage["failure_status"], "rate_limited")
        self.assertIn("resume_cursor", ds.coverage)

    def test_first_page_failure_raises(self):
        handler = PagedHandler({"153000": httpx.Response(429, json={})})
        with self.assertRaises(KisApiError) as ctx:
            _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")


class TradingDateBoundaryTests(unittest.TestCase):
    """실측(2026-08-27): 페이지네이션이 요청일을 지나 전일 오후로 넘어가
    전일 행 1페이지가 결과에 혼입됐다. 요청한 trading_date 의 행만 채택한다."""

    def test_other_day_rows_are_filtered_out(self):
        prev_day_page = copy.deepcopy(PAGE_2)
        for row in prev_day_page["output2"]:
            row["stck_bsop_date"] = "20260826"  # 전일 행
        handler = PagedHandler({
            "153000": PAGE_1,
            "093200": prev_day_page,
        })
        ds = _run(_provider(handler).fetch_bars(_request()))
        dates = {b.start_at.date().isoformat() for b in ds.bars}
        self.assertEqual(dates, {"2026-08-27"})
        # 전일 페이지에 도달하면 그 지점에서 깔끔히 끝난다 (혼입 0).
        self.assertEqual(len(ds.bars), 3)

    def test_closing_auction_row_at_1530_is_returned(self):
        # 15:30 마감 동시호가 행은 공식 종가다. provider 는 버리지 않는다.
        page = copy.deepcopy(PAGE_1)
        page["output2"].insert(0, {
            "stck_bsop_date": "20260827", "stck_cntg_hour": "153000",
            "stck_oprc": "70500", "stck_hgpr": "70500",
            "stck_lwpr": "70500", "stck_prpr": "70500",
            "cntg_vol": "999999",
        })
        handler = PagedHandler({"153000": page})
        ds = _run(_provider(handler).fetch_bars(_request()))
        self.assertEqual(ds.bars[-1].start_at.strftime("%H%M"), "1530")
        self.assertEqual(ds.bars[-1].volume, 999999)


class CapabilityTests(unittest.TestCase):
    def test_real_and_demo_capabilities_differ(self):
        handler = PagedHandler({})
        real = _run(_provider(handler, "real").capabilities("real"))
        demo = _run(_provider(handler, "demo").capabilities("demo"))
        self.assertEqual(real.provider, "kis")
        self.assertIn("1m", real.native_intervals)
        # 모의는 검증 전이다. 능력을 추측해 활성화하지 않는다.
        self.assertEqual(demo.verified_intervals, ())

    def test_interval_other_than_1m_rejected(self):
        handler = PagedHandler({})
        with self.assertRaisesRegex(ValueError, "interval"):
            _run(_provider(handler).fetch_bars(_request(interval="5m")))


if __name__ == "__main__":
    unittest.main()
