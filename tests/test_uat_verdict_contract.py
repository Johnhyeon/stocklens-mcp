"""UAT 판정이 스스로를 속이지 않는다 (1.1 리뷰 P1).

세 가지를 막는다.

1. **검증하지 않은 것을 통과로 남기지 않는다.** 잠정→확정 전이를 한 번도
   대조하지 않은 실행이 `failures: 0` 으로 끝나면, 그 증거 파일을 근거로
   게이트를 열게 된다. 실제로 그렇게 끝났다.
2. **정산 판정이 운영 어댑터와 같아야 한다.** 러너는 어댑터 함수를 부르지
   않지만(독립 검증), 판정 **기준**까지 달라지면 안 된다. 러너가 5주체
   합만 보고 어댑터가 기관 세부 합계까지 보면, 러너는 정산됐다고 하고
   어댑터는 미정산이라고 하는 행이 생긴다. 장중 전이 판정이 그 행에서
   틀린다.
3. **전이가 0건이면 전이를 증명한 것이 아니다.** 이미 확정된 데이터를
   두 번 읽으면 당연히 전이가 없다. 그건 회귀가 없다는 뜻이지 전이가
   동작한다는 증거가 아니다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs/uat/evidence/common"))

import evidence_uat as uat  # noqa: E402

from stock_mcp_server.market_data import kiwoom_evidence  # noqa: E402

# 5주체 합은 0 이지만 기관 세부 합(-30)이 기관계(-40)와 다르다.
# 실측상 정산 전 행의 전형이다.
UNSETTLED_RAW = {
    "dt": "20260828", "cur_prc": "70000", "acc_trde_qty": "100",
    "ind_invsr": "100", "frgnr_invsr": "-60", "natfor": "0",
    "orgn": "-40", "etc_corp": "0",
    "fnnc_invt": "-30", "insrnc": "0", "invtrt": "0", "etc_fnnc": "0",
    "bank": "0", "penfnd_etc": "0", "samo_fund": "0", "natn": "0",
}
SETTLED_RAW = dict(UNSETTLED_RAW, fnnc_invt="-40")


class SettlementAgreementTests(unittest.TestCase):
    def _uat_settled(self, raw) -> bool:
        rows = uat._kiwoom_rows([raw])
        return bool(rows[raw["dt"]]["_settled"])

    def _adapter_final(self, raw) -> bool:
        row = kiwoom_evidence._parse_row(raw, tolerance=0)
        return row.data_state == "final"

    def test_the_runner_agrees_with_the_adapter_on_an_unsettled_row(self):
        self.assertFalse(self._adapter_final(UNSETTLED_RAW))
        self.assertFalse(
            self._uat_settled(UNSETTLED_RAW),
            "러너가 정산됐다고 하고 어댑터는 미정산이라고 한다. "
            "장중 전이 판정이 이 행에서 틀린다.")

    def test_the_runner_agrees_with_the_adapter_on_a_settled_row(self):
        self.assertTrue(self._adapter_final(SETTLED_RAW))
        self.assertTrue(self._uat_settled(SETTLED_RAW))

    def test_the_runner_checks_the_institution_subtotal_too(self):
        """5주체 합만으로는 부족하다는 사실을 못 박는다."""
        rows = uat._kiwoom_rows([UNSETTLED_RAW])["20260828"]
        self.assertEqual(rows["_balance"], 0)
        self.assertNotEqual(rows["_institution"],
                            rows["institution_total"])
        self.assertFalse(rows["_settled"])


class TransitionVerdictTests(unittest.TestCase):
    def test_a_missing_snapshot_is_not_a_verified_transition(self):
        self.assertFalse(uat.transition_verified("no_intraday_snapshot", []))

    def test_a_stale_snapshot_is_not_a_verified_transition(self):
        self.assertFalse(
            uat.transition_verified("snapshot_from_another_day", []))

    def test_zero_observed_transitions_is_not_a_verified_transition(self):
        """이미 확정된 값을 두 번 읽은 것은 전이 증명이 아니다."""
        checked = [{"symbol": "005930", "failures": [],
                    "kis_settled_after": 0, "kiwoom_settled_after": 0}]
        self.assertFalse(uat.transition_verified("checked", checked))

    def test_an_observed_transition_counts(self):
        checked = [{"symbol": "005930", "failures": [],
                    "kis_settled_after": 3, "kiwoom_settled_after": 0}]
        self.assertTrue(uat.transition_verified("checked", checked))


class RequireTransitionTests(unittest.TestCase):
    """게이트용 실행은 전이를 검증하지 못하면 실패해야 한다."""

    def test_the_runner_exposes_a_require_flag(self):
        parser = uat.build_parser()
        args = parser.parse_args(["--require-transition"])
        self.assertTrue(args.require_transition)

    def test_the_flag_is_off_by_default(self):
        args = uat.build_parser().parse_args([])
        self.assertFalse(args.require_transition)

    def test_the_verdict_helper_fails_an_unverified_required_run(self):
        self.assertEqual(
            uat.exit_code(case_failures=0, transition_failures=0,
                          require_transition=True,
                          transition_state="no_intraday_snapshot",
                          transitions=[]),
            1)

    def test_case_failures_still_fail_regardless(self):
        self.assertEqual(
            uat.exit_code(case_failures=1, transition_failures=0,
                          require_transition=False,
                          transition_state="checked", transitions=[]),
            1)

    def test_a_clean_unrequired_run_still_passes(self):
        # 전이 검증을 요구하지 않은 평상시 실행은 통과한다.
        self.assertEqual(
            uat.exit_code(case_failures=0, transition_failures=0,
                          require_transition=False,
                          transition_state="no_intraday_snapshot",
                          transitions=[]),
            0)


class EvidenceHonestyTests(unittest.TestCase):
    def test_the_stored_evidence_states_whether_a_transition_was_verified(
            self):
        import json

        path = ROOT / "docs/uat/evidence/common/uat_evidence_flow_20260828.json"
        doc = json.loads(path.read_text("utf-8"))
        transition = doc["provisional_transition"]
        self.assertIn("state", transition)
        # 검증하지 않았으면 검증했다고 적히지 않는다.
        self.assertIn("verified", transition)
        self.assertEqual(
            transition["verified"],
            uat.transition_verified(transition["state"],
                                    transition.get("detail") or []))


if __name__ == "__main__":
    unittest.main()
