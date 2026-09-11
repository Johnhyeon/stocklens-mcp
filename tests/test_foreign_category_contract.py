"""`foreign` 은 두 공급자에서 같은 뜻이어야 한다 (1.1, 2026-08-28 실측).

실계좌 UAT 에서 잡힌 것: KIS 와 키움의 `foreign` 이 미묘하게 달랐다.
14종목 x 30일 중 361건 불일치. 개인·기관계는 전부 일치했고 외국인만
어긋났다.

원인은 데이터가 아니라 **이름표**였다. 32건 대조로 확정:

    KIS frgn_ntby_qty == 키움 frgnr_invsr + natfor   (32/32)

키움은 외국인을 둘로 나눠 준다.
- `frgnr_invsr`  외국인 (등록 외국인)
- `natfor`       내외국인 (국내 거주 외국인)

KRX·KIS·네이버가 "외국인"이라 부르는 것은 **둘의 합**이다. 어댑터가
키움의 좁은 쪽을 `foreign` 이라는 같은 이름으로 내보내면, 사용자가 두
증권사의 같은 이름 숫자를 비교하면서 차이를 시장 현상으로 읽는다.
값이 틀린 것보다 나쁜 종류의 오류다.

그래서 키움에서는:
- `foreign_registered`  frgnr_invsr 그대로 (좁은 쪽)
- `domestic_foreign`    natfor 그대로
- `foreign`             둘의 합. KIS·네이버와 같은 뜻

합은 만들어낸 값이 아니라 정의상의 합계이고, `raw_categories` 가
어느 필드에서 왔는지 그대로 기록한다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data import kiwoom_evidence
from stock_mcp_server.market_data.evidence_models import (
    PRINCIPAL_CATEGORIES,
)

# 실측값 (005930, 2026-08-19). KIS 외국인 -1,199,222.
ROW = {
    "dt": "20260819", "cur_prc": "247500", "acc_trde_qty": "30000000",
    "ind_invsr": "1000000", "frgnr_invsr": "-1204496", "natfor": "5274",
    "orgn": "199222", "etc_corp": "0",
    "fnnc_invt": "199222", "insrnc": "0", "invtrt": "0", "etc_fnnc": "0",
    "bank": "0", "penfnd_etc": "0", "samo_fund": "0", "natn": "0",
}
KIS_FOREIGN = -1199222


class ForeignDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.row = kiwoom_evidence._parse_row(ROW, tolerance=0)
        self.assertIsNotNone(self.row)

    def test_foreign_means_the_same_thing_as_on_kis(self):
        self.assertEqual(self.row.value("foreign"), KIS_FOREIGN)

    def test_the_narrow_category_keeps_its_own_name(self):
        # 좁은 쪽을 버리지 않는다. 키움만 주는 세부라서 가치가 있다.
        self.assertEqual(self.row.value("foreign_registered"), -1204496)
        self.assertEqual(self.row.value("domestic_foreign"), 5274)

    def test_the_derived_total_records_where_it_came_from(self):
        # 어느 원본 필드에서 왔는지 잃으면 되짚을 수 없다.
        self.assertEqual(self.row.raw_category("foreign"),
                         "frgnr_invsr+natfor")
        self.assertEqual(self.row.raw_category("foreign_registered"),
                         "frgnr_invsr")
        self.assertEqual(self.row.raw_category("domestic_foreign"),
                         "natfor")

    def test_foreign_is_not_double_counted_in_the_balance_check(self):
        """5주체 합 검산은 키움 자신의 분해를 쓴다.

        파생 합계(foreign)를 넣으면 내외국인을 두 번 세서 검산이 깨진다.
        """
        self.assertIn("foreign_registered", PRINCIPAL_CATEGORIES)
        self.assertNotIn("foreign", PRINCIPAL_CATEGORIES)
        self.assertEqual(tuple(kiwoom_evidence._PRINCIPALS),
                         PRINCIPAL_CATEGORIES)
        self.assertEqual(self.row.data_state, "final")
        self.assertTrue(self.row.balance_ok)
        self.assertEqual(self.row.principal_sum, 0)


class MissingComponentTests(unittest.TestCase):
    def test_the_total_is_absent_when_a_component_is(self):
        """한쪽만 있는 합계를 만들지 않는다.

        내외국인이 미정산이면 외국인계도 아직 확정이 아니다. 좁은 쪽
        값을 외국인계인 척 내보내면 그게 바로 이 버그의 재발이다.
        """
        broken = dict(ROW)
        broken["natfor"] = ""
        row = kiwoom_evidence._parse_row(broken, tolerance=0)
        self.assertIsNone(row.value("foreign"))
        self.assertIn("foreign", row.unsettled)

    def test_an_unsettled_component_leaves_the_total_unsettled(self):
        # 정산 전 행: 0 이 값이 아니라 상태다. 합계도 마찬가지다.
        provisional = dict(ROW, ind_invsr="0", natfor="0", orgn="0")
        row = kiwoom_evidence._parse_row(provisional, tolerance=0)
        self.assertNotEqual(row.data_state, "final")
        self.assertIsNone(row.value("foreign"))


class CategoryVocabularyTests(unittest.TestCase):
    def test_the_narrow_name_is_not_reachable_as_plain_foreign(self):
        raw_map = dict(kiwoom_evidence.INVESTOR_CATEGORIES)
        self.assertNotEqual(raw_map.get("foreign"), "frgnr_invsr")
        self.assertEqual(raw_map.get("foreign_registered"), "frgnr_invsr")

    def test_kis_still_reports_foreign_directly(self):
        from stock_mcp_server.market_data import kis_evidence

        names = {entry[0] for entry in kis_evidence._CATEGORIES}
        self.assertIn("foreign", names)
        # KIS 는 세부를 주지 않으므로 좁은 이름을 만들지 않는다.
        self.assertNotIn("foreign_registered", names)


if __name__ == "__main__":
    unittest.main()
