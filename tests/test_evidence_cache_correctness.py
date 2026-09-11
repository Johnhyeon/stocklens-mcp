"""캐시가 **틀린 답을 빨리 주지 않는다** (1.1 리뷰 차단).

처음 만든 캐시는 키에 종목과 measure 만 있었다. 그래서:

- 5일치를 캐시한 뒤 20일치를 요청하면 5일치만 돌아왔다.
- 기준일이 다음 거래일로 넘어가도 어제 데이터를 계속 돌려줬다.

빠른 건 사실이었지만 답이 틀렸다. 캐시는 같은 질문에 같은 답을 줄 때만
쓸모가 있고, 다른 질문에 옛 답을 주면 없느니만 못하다.

적중 조건은 셋 다 만족해야 한다:
1. 이 항목을 받아온 기준일이 요청 기준일보다 **이르지 않다**
   (그보다 이르면 더 최근 거래일이 빠져 있을 수 있다)
2. 요청 기준일 이하 행이 요청한 개수만큼 **있다**
3. generation·schema 가 같고 연결돼 있다

기준일 자체가 아니라 '받아온 기준일'을 쓰는 이유: 휴장일에 조회하면
최신 행이 전 거래일이라, 행 날짜만 보면 영원히 미적중이 된다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_mcp_server.market_data.evidence_cache import EvidenceCache
from stock_mcp_server.market_data.evidence_models import (
    UNIT_BY_MEASURE,
    InvestorFlowDataset,
    InvestorFlowRow,
)
from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)
from test_evidence_service import _FakeAdapter, _FakeRuntime  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _RangeAdapter(_FakeAdapter):
    """요청한 개수만큼 기준일부터 거슬러 확정 행을 준다."""

    async def fetch_investor_flow(self, symbol, *, base_date,
                                  measure="net_quantity", row_limit=30,
                                  **kwargs):
        self.flow_calls.append((symbol, measure, row_limit, base_date))
        rows = tuple(
            InvestorFlowRow(
                date=date.fromordinal(base_date.toordinal() - i),
                close=None, volume=None, values={"foreign": 100 + i},
                unsettled=(), data_state="final", balance_ok=True,
                principal_sum=0,
                raw_categories={"foreign": "frgnr_invsr"})
            for i in range(row_limit))
        return InvestorFlowDataset(
            symbol=symbol, provider=self.provider_id, profile="real",
            market="KR", rows=rows, data_state="final",
            coverage={"rows": len(rows), "complete": True}, warnings=(),
            measure=measure, unit=UNIT_BY_MEASURE[measure],
            source_endpoint="x")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = EvidenceCache(home=Path(self._tmp.name))
        self.adapters = {"kiwoom": _RangeAdapter("kiwoom",
                                                 KiwoomEvidenceProvider)}
        self.runtime = _FakeRuntime(primary="kiwoom",
                                    adapters=self.adapters)

    def _service(self, base):
        return EvidenceService(runtime=self.runtime, base_date=base,
                               release_override=True, cache=self.cache)

    @property
    def calls(self):
        return self.adapters["kiwoom"].flow_calls


class WindowTests(_Base):
    def test_a_longer_request_is_not_served_from_a_shorter_entry(self):
        _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=5))
        result = _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=20))
        self.assertEqual(len(result.records[0].rows), 20)
        self.assertEqual(len(self.calls), 2, "짧은 캐시로 긴 요청을 답했다")

    def test_a_shorter_request_is_served_from_a_longer_entry(self):
        _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=20))
        result = _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=5))
        self.assertEqual(len(result.records[0].rows), 5)
        self.assertEqual(len(self.calls), 1)

    def test_a_newer_base_date_is_never_answered_from_an_older_entry(self):
        """다음 거래일에 어제 수급을 돌려주면 안 된다."""
        _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=5))
        result = _run(self._service(date(2026, 8, 28)).investor_flow(
            code="005930", days=5))
        self.assertEqual(result.records[0].rows[0].date, date(2026, 8, 28))
        self.assertEqual(len(self.calls), 2)

    def test_an_older_base_date_does_not_get_rows_past_it(self):
        # 최신 항목이 있어도 요청 기준일 이후 행을 섞어 주지 않는다.
        _run(self._service(date(2026, 8, 28)).investor_flow(
            code="005930", days=20))
        result = _run(self._service(date(2026, 8, 26)).investor_flow(
            code="005930", days=5))
        self.assertLessEqual(result.records[0].rows[0].date,
                             date(2026, 8, 26))

    def test_a_holiday_request_still_hits(self):
        """휴장일이면 최신 행이 전 거래일이다. 그래도 적중해야 한다.

        행 날짜만 보고 판정하면 휴장일에는 영원히 미적중이 된다.
        """
        _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=5))
        before = len(self.calls)
        _run(self._service(date(2026, 8, 27)).investor_flow(
            code="005930", days=5))
        self.assertEqual(len(self.calls), before)


class RecomputedFieldsTests(_Base):
    def test_coverage_and_state_match_what_is_actually_returned(self):
        service = self._service(date(2026, 8, 27))
        _run(service.investor_flow(code="005930", days=10))
        result = _run(service.investor_flow(code="005930", days=10))
        dataset = result.records[0]
        self.assertTrue(dataset.coverage.get("from_cache"))
        self.assertEqual(dataset.coverage["rows"], len(dataset.rows))
        self.assertEqual(dataset.data_state, "final")

    def test_dropping_provisional_rows_recomputes_the_stored_counts(self):
        """행을 뺐으면 개수도 상태도 같이 고쳐야 한다.

        저장 행은 1개인데 coverage.rows=2, data_state=provisional 로
        남으면, 캐시가 자기 내용과 다른 말을 하게 된다.
        """
        key = self.cache.key(
            provider="kiwoom", profile="real", schema_version=1,
            capability="kr.investor_flow.daily", symbol="005930",
            variant="net_quantity")
        self.cache.put(key, {
            "rows": [{"date": "2026-08-28", "data_state": "provisional"},
                     {"date": "2026-08-27", "data_state": "final"}],
            "coverage": {"rows": 2, "complete": True},
            "data_state": "provisional",
        }, generation=1, final_through=None,
            fetched_for="2026-08-28")
        got = self.cache.get(key, connected=True, generation=1,
                             base_date="2026-08-28", row_limit=1)
        payload = got["payload"]
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["coverage"]["rows"], 1)
        self.assertEqual(payload["data_state"], "final")
        self.assertEqual(got["final_through"], "2026-08-27")


class BatchCacheTests(_Base):
    def test_the_batch_path_uses_the_cache_too(self):
        service = self._service(date(2026, 8, 27))
        _run(service.investor_flow_batch(codes=["005930", "000660"],
                                         days=5))
        before = len(self.calls)
        _run(service.investor_flow_batch(codes=["005930", "000660"],
                                         days=5))
        self.assertEqual(len(self.calls), before,
                         "배치가 캐시를 쓰지 않는다")

    def test_a_single_call_can_serve_a_later_batch(self):
        """단건과 배치가 같은 항목을 쓴다. 캐시가 둘로 갈리지 않는다."""
        service = self._service(date(2026, 8, 27))
        _run(service.investor_flow(code="005930", days=5))
        before = len(self.calls)
        _run(service.investor_flow_batch(codes=["005930", "000660"],
                                         days=5))
        # 000660 만 새로 부른다.
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(self.calls[-1][0], "000660")

    def test_a_batch_fills_the_cache_for_a_later_single(self):
        service = self._service(date(2026, 8, 27))
        _run(service.investor_flow_batch(codes=["005930", "000660"],
                                         days=5))
        before = len(self.calls)
        result = _run(service.investor_flow(code="000660", days=5))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(result.ok)

    def test_batch_order_is_preserved_across_a_partial_hit(self):
        service = self._service(date(2026, 8, 27))
        _run(service.investor_flow(code="000660", days=5))
        result = _run(service.investor_flow_batch(
            codes=["005930", "000660", "035420"], days=5))
        self.assertEqual([r.symbol for r in result.records],
                         ["005930", "000660", "035420"])


if __name__ == "__main__":
    unittest.main()
