"""두 공급자가 같은 이름이면 같은 타입을 돌려준다 (1.1).

공급자마다 어댑터를 따로 쓰다 보면 같은 이름의 dataclass 를 파일마다
정의하게 된다. 구조가 같아 보여서 한동안 굴러가지만, 한쪽에만 필드를
추가하는 순간 조용히 갈라진다.

실제로 그렇게 갈라졌던 것: `PressureBlock.granularity`. KIS 프로그램매매는
장중 시계열이고 키움은 일별이라 이 필드를 KIS 쪽에 넣었는데, 키움이
자기 사본을 쓰고 있어서 키움 블록에는 그 필드가 없었다. 서비스 계층이
두 블록을 같은 모양으로 직렬화하면 사용자는 **모양이 다른 두 숫자를
같은 것으로 읽는다.** 라벨-값 계약이 깨지는 자리다.

그래서 공용 모델을 단일 정의로 두고, 여기서 그 사실을 계약으로 박는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data import evidence_models, kis_evidence
from stock_mcp_server.market_data import kiwoom_evidence

SHARED = ("InvestorFlowRow", "InvestorFlowDataset", "PressureRow",
          "PressureBlock")


class SharedModelTests(unittest.TestCase):
    def test_both_adapters_use_the_one_definition(self):
        for name in SHARED:
            shared = getattr(evidence_models, name)
            for module in (kiwoom_evidence, kis_evidence):
                got = getattr(module, name, None)
                self.assertIs(
                    got, shared,
                    f"{module.__name__}.{name} 이 공용 정의가 아니다. "
                    "사본을 두면 한쪽에만 필드가 붙는다.")

    def test_measure_default_is_not_redefined_per_adapter(self):
        self.assertIs(kiwoom_evidence.DEFAULT_MEASURE,
                      evidence_models.DEFAULT_MEASURE)
        self.assertIs(kis_evidence.DEFAULT_MEASURE,
                      evidence_models.DEFAULT_MEASURE)

    def test_category_vocabularies_are_not_forked(self):
        # 5주체·기관 세부 8종은 검산의 기준이다. 어댑터마다 다른 목록을
        # 쓰면 한쪽 검산만 통과하는 행이 생긴다.
        self.assertEqual(tuple(kiwoom_evidence._PRINCIPALS),
                         evidence_models.PRINCIPAL_CATEGORIES)
        self.assertEqual(tuple(kiwoom_evidence._INSTITUTION_PARTS),
                         evidence_models.INSTITUTION_PARTS)


class GranularityContractTests(unittest.TestCase):
    def test_every_provider_block_states_its_granularity(self):
        # 키움 프로그램매매는 일별, KIS 는 장중이다. 두 블록 다 자기
        # granularity 를 말해야 사용자가 둘을 구분할 수 있다.
        block = evidence_models.PressureBlock(
            kind="program_trading", status="ok", provider="kiwoom",
            market="KR", rows=(), data_as_of=None,
            data_completeness="none", warnings=(),
            unavailable_reason=None, coverage={})
        self.assertEqual(block.granularity, "daily")

    def test_kiwoom_blocks_carry_the_field(self):
        fields = {f.name for f in
                  kiwoom_evidence.PressureBlock.__dataclass_fields__.values()}
        self.assertIn("granularity", fields)


class RawFieldPreservationTests(unittest.TestCase):
    def test_kiwoom_rows_keep_their_raw_field_names(self):
        # 공용 모델의 raw_categories 기본값은 비어 있다. 키움 파서가
        # 명시적으로 싣지 않으면 "어느 계정에서 온 숫자인지"가 사라진다.
        row = kiwoom_evidence._parse_row({
            "dt": "20260827", "cur_prc": "70000", "acc_trde_qty": "100",
            "ind_invsr": "-3223427", "frgnr_invsr": "1000000",
            "orgn": "2223427", "etc_corp": "0", "natfor": "0",
            "fnnc_invt": "2223427", "insrnc": "0", "invtrt": "0",
            "etc_fnnc": "0", "bank": "0", "penfnd_etc": "0",
            "samo_fund": "0", "natn": "0",
        }, tolerance=0)
        self.assertIsNotNone(row)
        self.assertEqual(row.raw_category("individual"), "ind_invsr")
        self.assertEqual(row.raw_category("foreign"), "frgnr_invsr")


if __name__ == "__main__":
    unittest.main()
