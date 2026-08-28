"""중복 날짜와 경계값 (1.1 리뷰 P2 후속).

네 가지를 막는다.

1. **어댑터가 중복 날짜를 그대로 내보낸다.** 키움은 페이지로 끊어 주므로
   페이지가 겹치면 같은 날짜가 두 번 올 수 있다. 지금은 그대로 두 행이
   되어 사용자에게 나간다. 캐시에만 중복 검사를 넣어 봐야 **최초
   호출자는 못 받는다** - 공급자가 중복을 준 당사자가 모른다.
2. **UAT 러너도 조용히 덮는다.** 러너는 날짜를 dict 에 넣으면서 뒤엣것을
   이기게 한다. 독립 검증기가 원본 이상을 못 보면 검증이 아니다.
3. **날짜 키 유실을 못 잡는다.** 전이 대조가 '겹치는 날짜'만 보므로,
   기존 날짜 2개 중 하나가 사라져도 하나만 겹치면 통과한다.
4. **경계값.** `--symbols 0` 은 0종목 0실패로 끝나고,
   `--phase intraday --require-transition` 은 전이를 확인하지 않은 채
   0 을 돌려준다. 둘 다 '검증했다'로 읽힌다.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs/uat/evidence/common"))

import evidence_uat as uat  # noqa: E402

from stock_mcp_server.market_data import kiwoom_evidence  # noqa: E402


def _raw(day: str, individual: int) -> dict:
    """검산을 통과하는 확정 행. individual 만 다르게 만든다."""
    return {"dt": day, "cur_prc": "70000", "acc_trde_qty": "100",
            "ind_invsr": str(individual), "frgnr_invsr": "-60",
            "natfor": "0", "orgn": str(-individual + 60), "etc_corp": "0",
            "fnnc_invt": str(-individual + 60), "insrnc": "0",
            "invtrt": "0", "etc_fnnc": "0", "bank": "0",
            "penfnd_etc": "0", "samo_fund": "0", "natn": "0"}


class AdapterDuplicateTests(unittest.TestCase):
    def test_conflicting_duplicate_dates_are_collapsed_with_a_warning(self):
        rows = [kiwoom_evidence._parse_row(_raw("20260827", 100), 0),
                kiwoom_evidence._parse_row(_raw("20260827", 200), 0),
                kiwoom_evidence._parse_row(_raw("20260826", 300), 0)]
        deduped, conflicts = kiwoom_evidence.dedupe_rows(rows)
        self.assertEqual([r.date for r in deduped],
                         [date(2026, 8, 27), date(2026, 8, 26)])
        self.assertEqual(conflicts, [date(2026, 8, 27)])

    def test_identical_duplicates_raise_no_conflict(self):
        rows = [kiwoom_evidence._parse_row(_raw("20260827", 100), 0),
                kiwoom_evidence._parse_row(_raw("20260827", 100), 0)]
        deduped, conflicts = kiwoom_evidence.dedupe_rows(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(conflicts, [])

    def test_a_difference_outside_the_flow_values_is_still_a_conflict(self):
        """수급값이 같아도 종가나 거래량이 다르면 같은 행이 아니다.

        `values` 만 비교하면 종가·거래량이 조용히 뒤 행 것으로 바뀐다.
        어느 쪽이 맞는지 물어볼 기회가 사라지는 건 마찬가지다.
        """
        base = _raw("20260827", 100)
        other = dict(base, cur_prc="80000", acc_trde_qty="999")
        rows = [kiwoom_evidence._parse_row(base, 0),
                kiwoom_evidence._parse_row(other, 0)]
        self.assertEqual(rows[0].values, rows[1].values)
        self.assertNotEqual(rows[0], rows[1])
        deduped, conflicts = kiwoom_evidence.dedupe_rows(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(conflicts, [date(2026, 8, 27)])

    def test_a_clean_page_set_is_untouched(self):
        rows = [kiwoom_evidence._parse_row(_raw("20260827", 100), 0),
                kiwoom_evidence._parse_row(_raw("20260826", 200), 0)]
        deduped, conflicts = kiwoom_evidence.dedupe_rows(rows)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(conflicts, [])


class FirstCallerSeesTheWarningTests(unittest.TestCase):
    def test_the_dataset_itself_carries_the_duplicate_warning(self):
        """최초 호출자가 경고를 받는다. 캐시 적중을 기다리지 않는다."""
        import asyncio
        import json

        import httpx

        from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload

        page = {"return_code": 0, "stk_invsr_orgn": [
            _raw("20260827", 100), _raw("20260827", 200),
            _raw("20260826", 300)]}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={
                    "token": "T", "token_type": "Bearer",
                    "expires_dt": "20991231235959"})
            return httpx.Response(200, json=page)

        schema = registry.require("kiwoom").credential_schema
        client = KiwoomClient(
            SecretPayload.from_schema(schema, {f.name: "x" for f in schema}),
            "real", transport=httpx.MockTransport(handler))
        provider = kiwoom_evidence.KiwoomEvidenceProvider(client, "real")
        dataset = asyncio.run(provider.fetch_investor_flow(
            "005930", base_date=date(2026, 8, 27)))

        dates = [r.date for r in dataset.rows]
        self.assertEqual(len(dates), len(set(dates)), "중복 날짜가 나갔다")
        joined = " ".join(dataset.warnings)
        self.assertIn("중복", joined)
        self.assertIn("2026-08-27", joined)
        del json


class UatDuplicateTests(unittest.TestCase):
    def test_the_runner_reports_duplicate_dates_instead_of_overwriting(self):
        rows, conflicts = uat.rows_with_conflicts(
            uat._kiwoom_rows, [_raw("20260827", 100), _raw("20260827", 200)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(conflicts, ["20260827"])

    def test_identical_rows_are_not_reported(self):
        rows, conflicts = uat.rows_with_conflicts(
            uat._kiwoom_rows, [_raw("20260827", 100), _raw("20260827", 100)])
        self.assertEqual(conflicts, [])

    def test_a_duplicate_in_the_raw_response_fails_the_case(self):
        # 원본이 이상하면 러너가 통과시키면 안 된다.
        self.assertTrue(uat.duplicate_is_failure())


class DateRetentionTests(unittest.TestCase):
    def _snapshot(self, dates):
        return {"symbol": "005930",
                "kis": {d: {"individual": 1, "foreign": 2,
                            "institution_total": 3, "settled": True}
                        for d in dates},
                "kiwoom": {}}

    def test_a_dropped_date_key_is_a_failure(self):
        """겹치는 날짜가 하나라도 있으면 통과하던 자리다."""
        before = self._snapshot(["20260827", "20260826"])
        after = self._snapshot(["20260827"])
        verdict = uat._compare_transition(before, after)
        checks = [f["check"] for f in verdict["failures"]]
        self.assertIn("kis_missing_date_keys", checks)

    def test_keeping_every_date_passes(self):
        before = self._snapshot(["20260827", "20260826"])
        after = self._snapshot(["20260828", "20260827", "20260826"])
        verdict = uat._compare_transition(before, after)
        checks = [f["check"] for f in verdict["failures"]]
        self.assertNotIn("kis_missing_date_keys", checks)

    def test_the_missing_dates_are_named(self):
        before = self._snapshot(["20260827", "20260826", "20260825"])
        after = self._snapshot(["20260827"])
        verdict = uat._compare_transition(before, after)
        detail = " ".join(f["detail"] for f in verdict["failures"])
        self.assertIn("20260826", detail)
        self.assertIn("20260825", detail)


class BoundaryTests(unittest.TestCase):
    def test_zero_symbols_is_rejected(self):
        parser = uat.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--symbols", "0"])

    def test_negative_symbols_is_rejected(self):
        with self.assertRaises(SystemExit):
            uat.build_parser().parse_args(["--symbols", "-1"])

    def test_intraday_with_require_transition_is_rejected(self):
        """스냅샷만 남기는 실행이 전이를 확인했다고 끝나면 안 된다."""
        with self.assertRaises(SystemExit):
            uat.build_parser().parse_args(
                ["--phase", "intraday", "--require-transition"])

    def test_intraday_alone_is_fine(self):
        args = uat.build_parser().parse_args(["--phase", "intraday"])
        self.assertEqual(args.phase, "intraday")
        self.assertFalse(args.require_transition)


if __name__ == "__main__":
    unittest.main()
