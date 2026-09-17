"""분봉 이름표 결함(2026-09-17)과 같은 갈래를 StockLens 전체에서 찾아 고친 것.

공통 원인은 둘이다.
- 이름표(data_basis·장 상태 안내·data_as_of)를 받은 데이터가 아니라 '지금 장이
  열렸나'에서 정했다. 지난 사건 창·결제일 통계·완성 봉만 쓴 계산에 "장중 조회 —
  마지막 봉이 아직 마감되지 않았습니다"가 붙었다.
- 공급자가 알려준 실패 사유·받은 범위를 버리고 뭉뚱그렸다. 만료된 키·호출 한도가
  "이 증권사는 지원하지 않음"으로, 30일치가 60일치 완전으로 나갔다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import market_clock as mc
from stock_mcp_server import server
from stock_mcp_server.market_data.evidence_models import (
    UNIT_BY_MEASURE,
    InvestorFlowDataset,
    InvestorFlowRow,
    PressureBlock,
)
from stock_mcp_server.market_data.evidence_service import (
    BatchEvidenceResult,
    BatchPressureResult,
    EvidenceResult,
)

_KRX_OPEN = {
    "krx": {"is_open": True, "status": "regular", "current_session": "regular",
            "last_trading_day": "2026-09-17", "regular_close": "15:30"},
    "us": {"is_open": False, "status": "closed_overnight",
           "last_trading_day": "2026-09-16"},
}
_KRX_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "current_session": None, "next_session": None,
            "last_trading_day": "2026-09-17", "regular_close": "15:30"},
    "us": {"is_open": False, "status": "closed_pre_market",
           "last_trading_day": "2026-09-16"},
}
_US_OPEN = {
    "krx": _KRX_CLOSED["krx"],
    "us": {"is_open": True, "status": "regular",
           "last_trading_day": "2026-08-26"},
}


def _text_meta(text: str) -> dict:
    body = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(body)


def _no_forming_warning(test: unittest.TestCase, meta: dict) -> None:
    test.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])
    test.assertFalse(any("마감되지 않" in w for w in meta["warnings"]),
                     meta["warnings"])


def _bar(d: str, close: float, volume: int = 1000) -> dict:
    return {"date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": volume}


# 2026-08 거래일(월~금) + 9월 초
_DAYS = ["20260803", "20260804", "20260805", "20260806", "20260807",
         "20260810", "20260811", "20260812", "20260813", "20260814",
         "20260817", "20260818", "20260819", "20260820", "20260821",
         "20260824", "20260825", "20260826", "20260827", "20260828",
         "20260831", "20260901", "20260902", "20260903", "20260904"]


# ---------------------------------------------------------------------------
# 사건 반응 (KR)
# ---------------------------------------------------------------------------


class KrEventReactionLabelTests(unittest.IsolatedAsyncioTestCase):
    async def _reaction(self, clock, flows=None, **kwargs):
        bars = [_bar(d, 1000 + i) for i, d in enumerate(_DAYS)]
        flow_mock = AsyncMock(return_value=flows or [])
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "get_investor_flow", flow_mock), \
             patch.object(server, "build_market_clock", return_value=clock):
            text = await server.get_event_reaction(code="005930", **kwargs)
        return text, _text_meta(text), flow_mock

    async def test_past_window_during_market_hours_is_confirmed(self):
        # 보고된 모양: 2025년 사건 창이 오늘 날짜·장중 미완성 봉으로 나갔다.
        _, meta, _ = await self._reaction(
            _KRX_OPEN, event_date="2026-08-05", before=2, after=5)
        self.assertEqual(meta["data_as_of"], "2026-08-12")  # 창의 마지막 날
        self.assertEqual(meta["data_basis"], "last_close")
        _no_forming_warning(self, meta)

    async def test_past_window_after_close_does_not_claim_latest_day(self):
        _, meta, _ = await self._reaction(
            _KRX_CLOSED, event_date="2026-08-05", before=2, after=5)
        self.assertFalse(any("최근 거래일" in w for w in meta["warnings"]),
                         meta["warnings"])

    async def test_flow_fetch_reaches_back_to_the_event_window(self):
        # 창 길이(2+5+10)가 아니라 오늘에서 사건 창 시작까지 닿아야 한다.
        _, _, flow_mock = await self._reaction(
            _KRX_CLOSED, event_date="2026-08-05", before=2, after=5)
        days = flow_mock.await_args.args[1]
        since_event = sum(1 for d in _DAYS if d >= "20260805")
        self.assertGreaterEqual(days, since_event + 2)

    def test_flow_days_are_capped_at_the_naver_limit(self):
        bars = [{"date": f"2026{m:02d}{d:02d}"} for m in range(1, 9)
                for d in range(1, 29)]
        self.assertEqual(
            server._event_flow_days(bars, ["2026-01-05"], 5, 5), 100)
        self.assertEqual(
            server._event_flow_days(bars, ["2026-08-20"], 5, 5), 20)


class KrEventReactionsBatchLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_is_dated_by_the_latest_window_end(self):
        items = [{"date": "2026-08-05", "title": "실적 공시"},
                 {"date": "2026-08-12", "title": "수주 공시"}]
        bars = [_bar(d, 1000 + i) for i, d in enumerate(_DAYS)]
        with patch.object(server, "naver_get_disclosure_list",
                          AsyncMock(return_value=items)), \
             patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "get_investor_flow",
                          AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            text = await server.get_event_reactions(
                code="005930", max_events=2, before=2, after=3)
        meta = _text_meta(text)
        self.assertEqual(meta["data_as_of"], "2026-08-17")
        self.assertEqual(meta["data_basis"], "last_close")
        _no_forming_warning(self, meta)


# ---------------------------------------------------------------------------
# 미국: 진행 중 봉을 이미 뺀 도구
# ---------------------------------------------------------------------------


_US_BARS = [_bar(d, 100 + i) for i, d in enumerate(
    ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
     "2026-08-24", "2026-08-25", "2026-08-26"])]


class UsCompletedBarLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_reaction_uses_completed_bars_label(self):
        evening = datetime(2026, 8, 26, 21, tzinfo=mc.ET)
        with patch.object(server.us, "get_history",
                          AsyncMock(return_value=_US_BARS)), \
             patch.object(server, "build_market_clock", return_value=_US_OPEN), \
             patch.object(server, "_now_kst", return_value=evening):
            text = await server.get_us_event_reaction(
                ticker="NVDA", event_date="2026-08-19", session="pre",
                after=3)
        meta = _text_meta(text)
        self.assertEqual(meta["data_basis"], "last_close")
        self.assertEqual(meta["data_as_of"], "2026-08-24")  # D+3
        _no_forming_warning(self, meta)

    async def test_multi_diagnosis_on_completed_bars_is_not_in_progress(self):
        done = {"timeframe": "day", "last_bar_date": "2026-08-26",
                "last_bar_complete": True,
                "last_completed_bar_date": "2026-08-26",
                "calculation_includes_incomplete": False}
        with patch.object(server.us, "get_history",
                          AsyncMock(return_value=_US_BARS)), \
             patch.object(server.us, "get_analyst_estimates",
                          AsyncMock(return_value=None)), \
             patch.object(server.us, "get_earnings_events",
                          AsyncMock(return_value=None)), \
             patch.object(server.us, "get_short_interest",
                          AsyncMock(return_value=None)), \
             patch.object(server, "_bar_state", return_value=done), \
             patch.object(server, "build_market_clock", return_value=_US_OPEN):
            text = await server.get_us_multi_diagnosis(tickers=["NVDA"])
        meta = json.loads(text)["_meta"]
        self.assertEqual(meta["data_basis"], "last_close")
        _no_forming_warning(self, meta)

    async def test_multi_diagnosis_with_unknown_last_bar_keeps_the_clock(self):
        unknown = {"timeframe": "day", "last_bar_date": "2026-08-26",
                   "last_bar_complete": None}
        with patch.object(server.us, "get_history",
                          AsyncMock(return_value=_US_BARS)), \
             patch.object(server.us, "get_analyst_estimates",
                          AsyncMock(return_value=None)), \
             patch.object(server.us, "get_earnings_events",
                          AsyncMock(return_value=None)), \
             patch.object(server.us, "get_short_interest",
                          AsyncMock(return_value=None)), \
             patch.object(server, "_bar_state", return_value=unknown), \
             patch.object(server, "build_market_clock", return_value=_US_OPEN):
            text = await server.get_us_multi_diagnosis(tickers=["NVDA"])
        self.assertEqual(json.loads(text)["_meta"]["data_basis"],
                         "in_progress_bar")


# ---------------------------------------------------------------------------
# 투자자예탁금: 결제일 기준으로 며칠 늦은 확정 통계
# ---------------------------------------------------------------------------


class InvestorDepositLabelTests(unittest.IsolatedAsyncioTestCase):
    _DATA = {"unit": "억원", "labels": ["고객예탁금"],
             "rows": [{"date": "2026-09-14", "고객예탁금": 100},
                      {"date": "2026-09-11", "고객예탁금": 99}]}

    async def _call(self, clock):
        with patch.object(server, "naver_get_investor_deposit",
                          AsyncMock(return_value=self._DATA)), \
             patch.object(server, "build_market_clock", return_value=clock):
            return _text_meta(await server.get_investor_deposit(days=2))

    async def test_lagged_statistics_are_not_a_live_snapshot(self):
        meta = await self._call(_KRX_OPEN)
        self.assertEqual(meta["data_basis"], "last_close")
        self.assertEqual(meta["data_as_of"], "2026-09-14")

    async def test_closed_market_note_does_not_claim_latest_day(self):
        meta = await self._call(_KRX_CLOSED)
        self.assertFalse(any("최근 거래일" in w for w in meta["warnings"]),
                         meta["warnings"])


# ---------------------------------------------------------------------------
# 상세 수급·수급 압력
# ---------------------------------------------------------------------------


def _flow_row(day: date) -> InvestorFlowRow:
    return InvestorFlowRow(
        date=day, close=None, volume=None, values={"individual": 1},
        unsettled=(), data_state="final", balance_ok=True, principal_sum=0)


def _availability(returned=("total",), unavailable=()):
    return {"requested": list(returned) + [u["capability"] for u in unavailable],
            "returned": list(returned), "unavailable": list(unavailable)}


class _FakeService:
    def __init__(self, flow=None, pressure=None):
        self._flow = flow
        self._pressure = pressure

    async def investor_flow(self, **kwargs):
        return self._flow

    async def investor_flow_batch(self, **kwargs):
        return self._flow

    async def supply_pressure_batch(self, **kwargs):
        return self._pressure


class EvidenceStatusTests(unittest.TestCase):
    def _single(self, code):
        return EvidenceResult(
            ok=False, provider="kis", profile="real", market="KR",
            data_availability=_availability(returned=()), records=(),
            coverage={}, warnings=(), error_code=code)

    def test_adapter_reasons_are_not_hidden_as_unsupported(self):
        for code in ("rate_limited", "credential_invalid",
                     "authentication_failed", "entity_not_found",
                     "provider_unavailable"):
            self.assertEqual(server._evidence_status_of(self._single(code)),
                             code)

    def test_router_reasons_keep_their_meaning(self):
        self.assertEqual(server._evidence_status_of(
            self._single("provider_not_configured")), "not_configured")
        self.assertEqual(server._evidence_status_of(
            self._single("unsupported_by_selected_provider")), "unsupported")

    def test_batch_failure_reports_the_dominant_entity_reason(self):
        result = BatchEvidenceResult(
            ok=False, provider="kis", profile="real", market="KR",
            data_availability=_availability(returned=()), records=(),
            coverage={}, warnings=(), error_code="all_entities_failed",
            entity_failures=[{"code": "A", "reason": "credential_invalid"},
                             {"code": "B", "reason": "credential_invalid"},
                             {"code": "C", "reason": "entity_not_found"}])
        self.assertEqual(server._evidence_status_of(result),
                         "credential_invalid")


class DetailedFlowToolTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, result, clock=_KRX_OPEN, **kwargs):
        with patch.object(server, "_evidence_service",
                          return_value=_FakeService(flow=result)), \
             patch.object(server, "build_market_clock", return_value=clock):
            return json.loads(await server.get_detailed_investor_flow(
                code="005930", **kwargs))

    async def test_single_code_failure_says_why(self):
        result = EvidenceResult(
            ok=False, provider="kis", profile="real", market="KR",
            data_availability=_availability(returned=()), records=(),
            coverage={"rows": 0, "complete": False}, warnings=(),
            error_code="rate_limited")
        payload = await self._call(result)
        meta = payload["_meta"]
        self.assertEqual(meta["provider_status"], "rate_limited")
        self.assertTrue(any("005930 조회 실패(rate_limited)" in w
                            for w in meta["warnings"]), meta["warnings"])
        _no_forming_warning(self, meta)

    async def test_fewer_days_than_asked_is_partial(self):
        # 한국투자증권은 30거래일까지만 준다. 60일을 물었으면 전부가 아니다.
        dataset = InvestorFlowDataset(
            symbol="005930", provider="kis", profile="real", market="KR",
            rows=(_flow_row(date(2026, 9, 16)),), data_state="final",
            coverage={"rows": 30, "complete": False, "requested_rows": 60},
            warnings=("한국투자증권은 최근 30거래일까지만 줍니다",),
            measure="net_quantity", unit=UNIT_BY_MEASURE["net_quantity"])
        result = EvidenceResult(
            ok=True, provider="kis", profile="real", market="KR",
            data_availability=_availability(), records=(dataset,),
            coverage=dict(dataset.coverage), warnings=dataset.warnings,
            error_code=None)
        meta = (await self._call(result, days=60))["_meta"]
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "source_limit")
        # 어제까지 확정된 행이다. 장중이어도 미완성 봉이 아니다.
        self.assertEqual(meta["data_basis"], "last_close")

    async def test_batch_counts_each_entity_coverage(self):
        dataset = InvestorFlowDataset(
            symbol="005930", provider="kis", profile="real", market="KR",
            rows=(_flow_row(date(2026, 9, 16)),), data_state="final",
            coverage={"rows": 30, "complete": False, "requested_rows": 60},
            warnings=(), measure="net_quantity",
            unit=UNIT_BY_MEASURE["net_quantity"])
        result = BatchEvidenceResult(
            ok=True, provider="kis", profile="real", market="KR",
            data_availability=_availability(), records=(dataset,),
            coverage={"requested_entities": 2, "returned_entities": 2,
                      "complete": True},
            warnings=(), error_code=None, entity_failures=[])
        with patch.object(server, "_evidence_service",
                          return_value=_FakeService(flow=result)), \
             patch.object(server, "build_market_clock",
                          return_value=_KRX_CLOSED):
            payload = json.loads(await server.get_detailed_investor_flow(
                codes=["005930", "000660"], days=60))
        self.assertEqual(payload["_meta"]["data_completeness"], "partial")


class SupplyPressureToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_ok_block_without_rows_is_not_served(self):
        empty = PressureBlock(
            kind="short_selling", status="ok", provider="kiwoom", market="KR",
            rows=(), data_as_of=None, data_completeness="none",
            warnings=("해당 구간 데이터가 없습니다.",), unavailable_reason=None,
            coverage={"rows": 0, "complete": True})
        result = BatchPressureResult(
            ok=True, provider="kiwoom", profile="real", market="KR",
            entities={"005930": {"short_selling": empty}}, entity_failures=[],
            coverage={}, warnings=(), error_code=None)
        with patch.object(server, "_evidence_service",
                          return_value=_FakeService(pressure=result)), \
             patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            payload = json.loads(await server.get_supply_pressure(
                code="005930", kind="short_selling"))
        meta = payload["_meta"]
        self.assertEqual(meta["data_completeness"], "none")
        self.assertEqual(meta["coverage"]["served_kinds"], 0)
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertIn("해당 구간 데이터가 없습니다.", meta["warnings"])
        _no_forming_warning(self, meta)

    async def test_argument_error_reply_has_no_forming_bar_warning(self):
        with patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            payload = json.loads(await server.get_supply_pressure())
        _no_forming_warning(self, payload["_meta"])


class EvidenceServiceReturnedTests(unittest.TestCase):
    def test_nothing_returned_when_every_entity_failed(self):
        from tests.test_evidence_service import _service

        service, _ = _service("kis", fail_codes=("999999",))
        result = asyncio.run(service.investor_flow(code="999999", days=20))
        self.assertFalse(result.ok)
        self.assertEqual(result.data_availability["returned"], [])


class KisFlowCoverageTests(unittest.TestCase):
    """FHKST01010900 은 최근 30거래일 고정이다."""

    def _fetch(self, row_count: int, row_limit: int):
        from tests.test_kis_evidence import _DAILY, Server, _provider

        settled = next(r for r in _DAILY["output"]
                       if str(r.get("prsn_ntby_qty") or "").strip())
        rows = []
        for i in range(row_count):
            row = dict(settled)
            row["stck_bsop_date"] = f"2026{(7 if i >= 28 else 8):02d}" \
                f"{(28 - i if i < 28 else 58 - i):02d}"
            rows.append(row)
        payload = dict(_DAILY, output=rows)
        return asyncio.run(_provider(Server(payload)).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), row_limit=row_limit))

    def test_asking_more_than_thirty_days_is_not_complete(self):
        ds = self._fetch(30, 60)
        self.assertFalse(ds.coverage["complete"])
        self.assertTrue(any("30거래일" in w for w in ds.warnings))

    def test_thirty_days_or_less_is_complete(self):
        self.assertTrue(self._fetch(30, 20).coverage["complete"])

    def test_short_history_is_complete(self):
        # 30행보다 적게 왔으면 그 종목 이력이 거기까지다.
        self.assertTrue(self._fetch(12, 60).coverage["complete"])


if __name__ == "__main__":
    unittest.main()
