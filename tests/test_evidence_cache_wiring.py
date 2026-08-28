"""캐시가 실제 경로에 연결돼 있는가 (1.1 Task 6).

캐시 모듈만 만들고 아무도 부르지 않으면, 테스트는 통과하는데 반복 조회는
여전히 증권사를 다시 부른다. 여기서 그 배선을 확인한다.

연결 해제 정리는 **따로 보고한다**. 분봉 캐시는 지워졌는데 증거 캐시가
남았을 때 둘 다 지웠다고 보고하면, Manager 의 '완전 정리'가 거짓이 된다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_cache import EvidenceCache
from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_evidence_service import _FakeAdapter, _FakeRuntime  # noqa: E402

BASE = date(2026, 8, 27)


def _run(coro):
    return asyncio.run(coro)


class _FinalAdapter(_FakeAdapter):
    """확정 행만 돌려주는 대역. 캐시에 담길 수 있는 모양이다."""


class ServiceCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = EvidenceCache(home=Path(self._tmp.name))
        self.adapters = {
            "kiwoom": _FinalAdapter("kiwoom", KiwoomEvidenceProvider)}
        self.service = EvidenceService(
            runtime=_FakeRuntime(primary="kiwoom", adapters=self.adapters),
            base_date=BASE, release_override=True, cache=self.cache)

    def test_a_second_identical_request_does_not_call_the_provider(self):
        # 이 대역은 row_limit 과 무관하게 1행만 준다. 그래서 1일치를
        # 묻는다 - 20일치를 물으면 캐시가 창을 못 덮어 정당하게
        # 미적중이 된다 (창 판정은 test_evidence_cache_correctness).
        first = _run(self.service.investor_flow(code="005930", days=1))
        self.assertTrue(first.ok)
        calls_after_first = len(self.adapters["kiwoom"].flow_calls)
        second = _run(self.service.investor_flow(code="005930", days=1))
        self.assertTrue(second.ok)
        self.assertEqual(len(self.adapters["kiwoom"].flow_calls),
                         calls_after_first,
                         "같은 요청이 증권사를 다시 불렀다")
        self.assertEqual([r.date for r in second.records[0].rows],
                         [r.date for r in first.records[0].rows])

    def test_a_different_measure_is_a_different_entry(self):
        # 수량과 금액은 다른 숫자다. 같은 칸에 담으면 서로를 덮는다.
        _run(self.service.investor_flow(code="005930", days=20,
                                        measure="net_quantity"))
        _run(self.service.investor_flow(code="005930", days=20,
                                        measure="net_amount"))
        measures = [c[1] for c in self.adapters["kiwoom"].flow_calls]
        self.assertEqual(measures, ["net_quantity", "net_amount"])

    def test_a_generation_change_bypasses_the_cache(self):
        _run(self.service.investor_flow(code="005930", days=20))
        before = len(self.adapters["kiwoom"].flow_calls)
        self.service._runtime.generation += 1
        _run(self.service.investor_flow(code="005930", days=20))
        self.assertEqual(len(self.adapters["kiwoom"].flow_calls), before + 1)

    def test_the_cache_is_off_when_no_cache_is_given(self):
        # 기본 생성자는 캐시 없이도 동작해야 한다 (기존 호출부 보호).
        service = EvidenceService(
            runtime=_FakeRuntime(primary="kiwoom", adapters=self.adapters),
            base_date=BASE, release_override=True)
        self.assertTrue(_run(service.investor_flow(code="005930")).ok)


class DisconnectCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _seed(self, provider):
        cache = EvidenceCache(home=self.home)
        key = cache.key(provider=provider, profile="real",
                        schema_version=1, capability="kr.x", symbol="005930")
        cache.put(key, {"rows": [{"date": "2026-08-27",
                                  "data_state": "final"}]},
                  generation=1, final_through="2026-08-27")
        return cache, key

    def test_removing_a_provider_clears_its_evidence_cache(self):
        cache, key = self._seed("kiwoom")
        cache.remove_provider("kiwoom")
        self.assertIsNone(cache.get(key, connected=True, generation=1))

    def test_the_two_caches_are_reported_separately(self):
        """분봉과 증거를 하나의 불리언으로 합치지 않는다."""
        from stock_mcp_server import broker_cli

        source = Path(broker_cli.__file__).read_text("utf-8")
        self.assertIn("evidence_cache_removed", source)
        self.assertIn("cache_removed", source)

    def test_the_evidence_root_is_not_under_the_bar_cache_root(self):
        cache, key = self._seed("kis")
        path = str(cache.path_for(key))
        self.assertIn("market_evidence", path)
        self.assertNotIn("market_data", path)


if __name__ == "__main__":
    unittest.main()
