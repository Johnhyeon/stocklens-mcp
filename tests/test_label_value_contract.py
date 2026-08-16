"""라벨-값 계약 회귀 테스트 — "표시된 이름과 실제 숫자가 다르다"류 결함 전용.

배경: 2026-08-16 DartLens 문의(반기 라벨에 2분기 값)를 계기로 세 Lens를 전수
점검하면서 나온 같은 계열의 결함들을 여기 묶는다. 공통 원칙 세 가지:

1. 외부 payload를 **위치**로 읽어 이름표를 붙이지 않는다. 불가피하면 자가검증한다.
2. 단위·통화·기준(연결/별도)은 **선언된 것만** 표시한다. 모르면 모른다고 쓴다.
3. 결측을 0으로 채우지 않는다 — 0은 그 자체로 의미 있는 값이다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from stock_mcp_server.naver import _parse_earnings_surprise, _parse_float


# ---------------------------------------------------------------------------
# 1. 컨센서스 — wisereport `var res` 행 위치 오독
# ---------------------------------------------------------------------------

# 디오(039840) 2026-08-16 실제 응답. 페이지 템플릿(#tmpl-list) 기준 행 배치는
#   0~4: 영업이익   (컨센서스 / 잠정치 / Surprise / 전년동기대비 / 전분기대비)
#   5~9: 당기순이익 (동일)
# 예전 코드는 0~2를 [매출액, 영업이익, 영업이익률]로 라벨링해서
# 영업이익 컨센서스 50억을 "매출액 50", 어닝쇼크 -71.8%를 "영업이익률"로 냈다.
DIO_RES = {
    "yymm": ["202512", "202603", "202606"],
    "yymmdd": ["2026/02/12(연결)", "2026/04/16(연결)", "2026/08/13(연결)"],
    "data": [
        {"1": 50.025, "2": 23.0, "3": 52.0},          # 영업이익 컨센서스
        {"1": 14.11085, "2": 41.47, "3": None},       # 영업이익 잠정치
        {"1": -71.7924, "2": 80.30435, "3": None},    # 영업이익 Surprise(%)
        {"1": 305.65858, "2": 173.17099, "3": None},  # 전년동기대비
        {"1": -66.04073, "2": 193.88726, "3": None},  # 전분기대비
        {"1": 15.0, "2": None, "3": None},            # 당기순이익 컨센서스
        {"1": 15.2406, "2": None, "3": None},         # 당기순이익 잠정치
        {"1": 1.604, "2": None, "3": None},           # 당기순이익 Surprise(%)
        {"1": -83.01245, "2": None, "3": None},
        {"1": -74.24107, "2": None, "3": None},
    ],
}


class EarningsSurpriseMappingTests(unittest.TestCase):
    def test_rows_map_to_operating_profit_and_net_income(self):
        out = _parse_earnings_surprise(DIO_RES)
        self.assertIsNotNone(out)
        self.assertEqual(set(out), {"영업이익", "당기순이익"})
        op = out["영업이익"]["202512"]
        self.assertAlmostEqual(op["consensus"], 50.025)
        self.assertAlmostEqual(op["actual"], 14.11085)
        self.assertAlmostEqual(op["surprise"], -71.7924)
        ni = out["당기순이익"]["202512"]
        self.assertAlmostEqual(ni["consensus"], 15.0)
        self.assertAlmostEqual(ni["actual"], 15.2406)

    def test_revenue_is_not_invented(self):
        """이 표엔 매출액이 아예 없다. 없는 계정을 만들어내면 안 된다."""
        self.assertNotIn("매출액", _parse_earnings_surprise(DIO_RES))

    def test_period_alignment(self):
        """yymm[i] ↔ 키 str(i+1). 디오 2026.03 영업이익 잠정 41.47억은
        DART 1Q26 영업이익 41억과 일치한다."""
        out = _parse_earnings_surprise(DIO_RES)
        self.assertAlmostEqual(out["영업이익"]["202603"]["actual"], 41.47)
        self.assertIsNone(out["영업이익"]["202606"]["actual"])  # 아직 미발표

    def test_shifted_rows_are_rejected_not_relabeled(self):
        """행이 한 칸 밀리면 Surprise 항등식이 깨진다 → 섹션을 통째로 생략."""
        shifted = dict(DIO_RES, data=DIO_RES["data"][1:] + [{"1": 0}])
        self.assertIsNone(_parse_earnings_surprise(shifted))

    def test_unverifiable_payload_rejected(self):
        """컨센서스/잠정치/Surprise가 한 세트도 안 갖춰지면 검증 불가 → None."""
        blank = dict(DIO_RES, data=[{"1": None, "2": None, "3": None}] * 10)
        self.assertIsNone(_parse_earnings_surprise(blank))

    def test_truncated_payload_rejected(self):
        truncated = dict(DIO_RES, data=DIO_RES["data"][:4])
        self.assertIsNone(_parse_earnings_surprise(truncated))

    def test_surprise_identity_holds_for_all_returned_cells(self):
        out = _parse_earnings_surprise(DIO_RES)
        checked = 0
        for rows in out.values():
            for cell in rows.values():
                c, a, s = cell["consensus"], cell["actual"], cell["surprise"]
                if c and a is not None and s is not None:
                    self.assertAlmostEqual((a - c) / abs(c) * 100, s, places=3)
                    checked += 1
        self.assertGreaterEqual(checked, 3)


# ---------------------------------------------------------------------------
# 2. 결측을 0으로 채우지 않는다
# ---------------------------------------------------------------------------


class MissingIsNotZeroTests(unittest.TestCase):
    def test_parse_float_can_return_none(self):
        """베타 0.00·보수 0.000%는 '없음'이 아니라 '0'이라는 뜻이 된다."""
        for blank in ("", "-", "N/A", None):
            self.assertIsNone(_parse_float(blank, None))

    def test_parse_float_default_unchanged_for_other_callers(self):
        self.assertEqual(_parse_float("", ), 0.0)
        self.assertEqual(_parse_float("1,234.5"), 1234.5)

    def test_real_zero_still_parses_as_zero(self):
        self.assertEqual(_parse_float("0", None), 0.0)


# ---------------------------------------------------------------------------
# 3. 재무 표의 기준(연결/별도) 캡처
# ---------------------------------------------------------------------------

# 네이버 종목 메인의 기업실적분석 thead는 3행이다.
#   0행 연간/분기 그룹, 1행 기간, 2행 재무기준(IFRS연결/IFRS별도)
# 예전엔 2행을 통째로 버려 연결·별도가 섞인 종목도 구분 없이 한 줄로 나갔다.
FIN_TABLE_HTML = """
<div class="cop_analysis"><table>
<thead>
<tr><th>주요재무정보</th><th colspan="2">최근 연간 실적</th><th colspan="2">최근 분기 실적</th></tr>
<tr><th>2024.12</th><th>2025.12</th><th>2026.03</th><th>2026.06</th></tr>
<tr><th>IFRS별도</th><th>IFRS연결</th><th>IFRS연결</th><th>IFRS연결</th></tr>
</thead>
<tbody>
<tr><th>매출액</th><td>1,196</td><td>1,641</td><td>413</td><td>449</td></tr>
</tbody>
</table></div>
"""


class FinancialBasisCaptureTests(unittest.TestCase):
    def _parse(self):
        soup = BeautifulSoup(FIN_TABLE_HTML, "lxml")
        table = soup.select_one("div.cop_analysis table")
        thead_rows = table.select("thead tr")
        annual_count, quarterly_count = 0, 0
        for th in thead_rows[0].select("th"):
            span = int(th.get("colspan", "1"))
            if "연간" in th.text:
                annual_count = span
            elif "분기" in th.text:
                quarterly_count = span
        periods = [th.text.strip() for th in thead_rows[1].select("th")]
        bases = [th.text.strip() for th in thead_rows[2].select("th")]
        return annual_count, quarterly_count, periods, bases

    def test_third_header_row_is_the_accounting_basis(self):
        annual_count, quarterly_count, periods, bases = self._parse()
        self.assertEqual((annual_count, quarterly_count), (2, 2))
        self.assertEqual(len(bases), len(periods))
        self.assertEqual(bases[0], "IFRS별도")

    def test_mixed_basis_is_detectable(self):
        """연결과 별도가 섞이면 비교 범위가 달라 경고가 필요하다."""
        _, _, _, bases = self._parse()
        self.assertGreater(len({b for b in bases if b}), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
