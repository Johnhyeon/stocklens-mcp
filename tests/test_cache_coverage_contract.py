"""캐시 적중 여부로 공개 계약이 달라지지 않는다 (1.1 리뷰 P1·P2).

`coverage.complete` 는 AI 가 "이 답이 요청 범위를 다 채웠는가"를 판단하는
필드다. 캐시에서 온 응답에만 그 필드가 없으면, 같은 질문에 같은 값을
주면서 **완전성 판단만 달라진다.** 캐시는 보이지 않아야 하는데 보인다.

P2: 같은 날짜가 값이 다른 채로 두 번 오면 지금은 조용히 뒤엣것이 이긴다.
저장 결과는 옳지만(중복 행 없음), 공급자가 앞뒤로 다른 값을 준 사실은
남겨야 한다. 조용히 덮으면 어느 쪽이 맞는지 물어볼 기회도 사라진다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_cache import EvidenceCache

FINAL = {"date": "2026-08-27", "data_state": "final",
         "values": {"foreign": 1}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = EvidenceCache(home=Path(self._tmp.name))
        self.key = self.cache.key(
            provider="kiwoom", profile="real", schema_version=1,
            capability="kr.investor_flow.daily", symbol="005930")

    def _round_trip(self, payload, **kwargs):
        self.cache.put(self.key, payload, generation=1,
                       final_through="2026-08-27",
                       fetched_for="2026-08-27", **kwargs)
        return self.cache.get(self.key, connected=True, generation=1,
                              base_date="2026-08-27", row_limit=1)


class CoverageShapeTests(_Base):
    def test_complete_survives_a_round_trip(self):
        got = self._round_trip({"rows": [FINAL],
                                "coverage": {"rows": 1, "complete": True}})
        self.assertIn("complete", got["payload"]["coverage"])
        self.assertTrue(got["payload"]["coverage"]["complete"])

    def test_an_incomplete_fetch_stays_incomplete(self):
        """페이지 예산을 소진한 응답을 캐시가 완전한 것으로 승격하지 않는다."""
        got = self._round_trip({"rows": [FINAL],
                                "coverage": {"rows": 1, "complete": False}})
        self.assertIs(got["payload"]["coverage"]["complete"], False)

    def test_the_cached_shape_matches_the_fresh_shape(self):
        fresh = {"rows": [FINAL], "coverage": {"rows": 1, "complete": True}}
        got = self._round_trip(fresh)
        self.assertEqual(set(fresh["coverage"]) - {"rows"},
                         set(got["payload"]["coverage"]) - {"rows"})

    def test_the_row_count_is_the_served_count(self):
        got = self._round_trip({
            "rows": [FINAL,
                     {"date": "2026-08-28", "data_state": "provisional"}],
            "coverage": {"rows": 2, "complete": True}})
        self.assertEqual(got["payload"]["coverage"]["rows"],
                         len(got["payload"]["rows"]))


class DuplicateDateTests(_Base):
    def test_conflicting_duplicates_are_reported_not_just_collapsed(self):
        got = self._round_trip({"rows": [
            dict(FINAL, values={"foreign": 1}),
            dict(FINAL, values={"foreign": 2}),
        ], "coverage": {"rows": 2, "complete": True}})
        self.assertEqual(len(got["payload"]["rows"]), 1)
        warnings = " ".join(got["payload"].get("warnings") or ())
        self.assertIn("2026-08-27", warnings)
        self.assertIn("중복", warnings)

    def test_identical_duplicates_are_silent(self):
        # 같은 값이 두 번 온 것은 알릴 일이 아니다.
        got = self._round_trip({"rows": [dict(FINAL), dict(FINAL)],
                                "coverage": {"rows": 2, "complete": True}})
        self.assertEqual(len(got["payload"]["rows"]), 1)
        warnings = " ".join(got["payload"].get("warnings") or ())
        self.assertNotIn("중복", warnings)

    def test_a_later_write_overwriting_an_earlier_one_is_not_a_conflict(self):
        # 재조회로 같은 날짜를 다시 받는 것은 정상이다.
        self.cache.put(self.key, {"rows": [dict(FINAL, values={"a": 1})],
                                  "coverage": {"rows": 1, "complete": True}},
                       generation=1, final_through="2026-08-27",
                       fetched_for="2026-08-27")
        got = self._round_trip({"rows": [dict(FINAL, values={"a": 2})],
                                "coverage": {"rows": 1, "complete": True}})
        warnings = " ".join(got["payload"].get("warnings") or ())
        self.assertNotIn("중복", warnings)


class ServiceShapeTests(unittest.TestCase):
    def test_a_cached_dataset_keeps_the_completeness_field(self):
        import asyncio
        from datetime import date

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_evidence_cache_correctness import _RangeAdapter
        from test_evidence_service import _FakeRuntime
        from stock_mcp_server.market_data.evidence_service import (
            EvidenceService,
        )
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )

        with TemporaryDirectory() as tmp:
            adapters = {"kiwoom": _RangeAdapter("kiwoom",
                                                KiwoomEvidenceProvider)}
            service = EvidenceService(
                runtime=_FakeRuntime(primary="kiwoom", adapters=adapters),
                base_date=date(2026, 8, 27), release_override=True,
                cache=EvidenceCache(home=Path(tmp)))
            first = asyncio.run(service.investor_flow(code="005930",
                                                     days=5))
            second = asyncio.run(service.investor_flow(code="005930",
                                                      days=5))
            self.assertTrue(second.records[0].coverage.get("from_cache"))
            self.assertIn("complete", first.records[0].coverage)
            self.assertIn("complete", second.records[0].coverage,
                          "캐시 응답에만 완전성 필드가 없다")
            self.assertEqual(first.records[0].coverage["complete"],
                             second.records[0].coverage["complete"])


if __name__ == "__main__":
    unittest.main()
