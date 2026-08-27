"""공급자 공통 계약 테스트 (1.0 Task 17).

같은 계약이 KIS·키움·토스, KR·US 어댑터 전부에 적용된다. 특정 공급자를
통과시키려고 공통 불변식을 약화하지 않는다.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_conformance import ALL_PROVIDER_CASES
from stock_mcp_server._result_meta import PROVIDER_STATUSES


class ConformanceTests(unittest.TestCase):
    def test_bars_are_aware_sorted_unique_and_valid(self):
        for case in ALL_PROVIDER_CASES:
            with self.subTest(provider=case.name):
                ds = case.fetch_single()
                self.assertTrue(ds.bars, case.name)
                starts = [b.start_at for b in ds.bars]
                self.assertEqual(starts, sorted(starts))
                self.assertEqual(len(set(starts)), len(starts))
                for bar in ds.bars:
                    self.assertIsNotNone(bar.start_at.tzinfo)
                    self.assertEqual(
                        bar.end_at - bar.start_at, timedelta(minutes=1))
                    self.assertGreaterEqual(bar.high, max(bar.open,
                                                          bar.close))
                    self.assertLessEqual(bar.low, min(bar.open, bar.close))
                    self.assertGreaterEqual(bar.volume, 0)
                    self.assertEqual(bar.interval, "1m")
                    self.assertEqual(bar.session, "regular")

    def test_provenance_fields_are_exact(self):
        for case in ALL_PROVIDER_CASES:
            with self.subTest(provider=case.name):
                ds = case.fetch_single()
                self.assertEqual(ds.provider, case.provider_id)
                self.assertEqual(ds.market, case.market)
                self.assertEqual(ds.timezone, case.tz_name)
                self.assertEqual(ds.source_interval, "1m")
                self.assertEqual(ds.adjustment_basis, "unadjusted")
                self.assertEqual(ds.session, "regular")
                self.assertTrue(ds.source_endpoint)
                self.assertIn("complete", ds.coverage)
                self.assertIn("returned_rows", ds.coverage)

    def test_no_requested_date_escape(self):
        # 공통 불변식: 요청일 이후 행 금지 + 최신 행은 요청일이다.
        # (KIS 미국 endpoint 는 설계상 KEYB 로 과거일 이력을 이어 받으므로
        # 과거 행 자체는 허용된다. 요청일 뒤로 새는 것이 금지 대상이다.)
        for case in ALL_PROVIDER_CASES:
            with self.subTest(provider=case.name):
                ds = case.fetch_single()
                dates = sorted({b.start_at.date() for b in ds.bars})
                self.assertLessEqual(dates[-1], case.trading_date, case.name)
                self.assertEqual(dates[-1], case.trading_date, case.name)

    def test_partial_never_becomes_complete(self):
        for case in ALL_PROVIDER_CASES:
            with self.subTest(provider=case.name):
                ds = case.fetch_partial()
                self.assertTrue(ds.bars, case.name)
                self.assertFalse(ds.coverage["complete"], case.name)
                self.assertIn(ds.coverage.get("failure_status"),
                              PROVIDER_STATUSES)
                self.assertTrue(ds.warnings)

    def test_error_status_vocabulary(self):
        for case in ALL_PROVIDER_CASES:
            with self.subTest(provider=case.name):
                try:
                    case.fetch_error()
                except Exception as exc:  # noqa: BLE001
                    status = getattr(exc, "provider_status", None)
                    self.assertIn(status, PROVIDER_STATUSES, case.name)
                else:
                    self.fail(f"{case.name}: 첫 페이지 장애가 조용히 "
                              "지나갔습니다")


if __name__ == "__main__":
    unittest.main()
