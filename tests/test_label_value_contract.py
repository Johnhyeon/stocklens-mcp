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


# ---------------------------------------------------------------------------
# 4. 종목 상태(시장경보·관리종목)는 시세와 함께 보여야 한다
# ---------------------------------------------------------------------------

# 네이버 종목 페이지 div.description 실제 구조 (035290 골드앤에스, 2026-08-16 확인).
# 상태 마커는 em으로 붙고, date/realtime/summary는 마커가 아니다.
_DESC_HTML = """
<div class="wrap_company"><div class="description">
  <span class="code">035290</span>
  <em class="date">2026.08.14 기준(KRX 장마감)</em>
  <em class="realtime">실시간</em>
  <em class="summary">기업개요 동사는 1999년 코스닥시장에 상장하고…</em>
  {markers}
</div></div>
"""


class StockStatusMarkerTests(unittest.TestCase):
    def _flags(self, markers: str) -> list[str]:
        from stock_mcp_server.naver import _NON_STATUS_EM_CLASSES
        soup = BeautifulSoup(_DESC_HTML.format(markers=markers), "lxml")
        desc = soup.select_one("div.description")
        return [
            t
            for em in desc.select("em")
            if not (set(em.get("class") or []) & _NON_STATUS_EM_CLASSES)
            for t in [" ".join(em.get_text(" ", strip=True).split())]
            if t and len(t) <= 12
        ]

    def test_managed_stock_flags_extracted(self):
        flags = self._flags('<em class="caution">투자주의</em><em class="manage">관리종목</em>')
        self.assertEqual(flags, ["투자주의", "관리종목"])

    def test_warning_stock_flag_extracted(self):
        self.assertEqual(self._flags('<em class="warning">투자경고</em>'), ["투자경고"])

    def test_normal_stock_has_no_flags(self):
        self.assertEqual(self._flags(""), [])

    def test_unknown_future_marker_still_surfaces(self):
        """클래스 화이트리스트로 잡으면 새 유형이 생겼을 때 조용히 놓친다.

        마커가 아닌 것만 제외하는 방식이라 처음 보는 클래스도 그대로 드러난다.
        """
        self.assertEqual(self._flags('<em class="brandnew">정리매매</em>'), ["정리매매"])

    def test_long_text_is_not_mistaken_for_a_marker(self):
        """summary처럼 긴 본문이 클래스만 바뀌어도 상태로 오인되면 안 된다."""
        body = "가" * 200
        self.assertEqual(self._flags(f'<em class="odd">{body}</em>'), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# 5. 미국 내부자 거래 — 비율을 정수로 자르지 않고, 거래 유형을 버리지 않는다
# ---------------------------------------------------------------------------

# yfinance 실제 응답 발췌 (NVDA, 2026-08-16 확인).
# 요약 dict에는 **주식수 행과 비율 행이 섞여** 있고, 개별 거래는 transaction이
# 빈 문자열인 대신 text에 진짜 유형이 들어 있다.
_INSIDER = {
    "purchases_last_6m": {
        "Purchases": {"shares": 62275007.0, "trans": 26},
        "% Buy Shares": {"shares": 0.069, "trans": None},
        "% Sell Shares": {"shares": 0.003, "trans": None},
    },
    "recent_transactions": [
        {"start_date": "2026-08-05T00:00:00", "insider": "COXE TENCH C",
         "position": "Director", "transaction": "", "shares": 500000, "value": 0,
         "text": "Stock Gift at price 0.00 per share."},
        {"start_date": "2026-06-25T00:00:00", "insider": "HUDSON BEACH DAWN E",
         "position": "Director", "transaction": "", "shares": 1211, "value": 0,
         "text": "Stock Award(Grant) at price 0.00 per share."},
    ],
}


class InsiderTradeDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def _render(self):
        from unittest.mock import AsyncMock, patch
        from stock_mcp_server import server
        with patch.object(server.us, "get_insider", AsyncMock(return_value=dict(_INSIDER, ticker="NVDA"))), \
             patch.object(server.us, "get_insider_roster", AsyncMock(return_value=[])):
            return await server.get_us_insider("NVDA")

    async def test_percent_rows_keep_decimals(self):
        """0.069를 정수로 자르면 '0'이 되어 비율이 통째로 사라진다."""
        out = await self._render()
        self.assertIn("0.069", out)
        self.assertIn("0.003", out)
        self.assertNotIn("% Buy Shares: 0\n", out)

    async def test_share_rows_stay_integers(self):
        """주식수 행까지 소수로 바뀌면 안 된다 — 같은 dict에 섞여 있다."""
        out = await self._render()
        self.assertIn("62,275,007", out)

    async def test_transaction_type_recovered_from_text(self):
        """transaction이 비어도 text에 유형이 있다. 버리면 '$0'인 이유를 알 수 없다."""
        out = await self._render()
        self.assertIn("Stock Gift", out)
        self.assertIn("Stock Award(Grant)", out)

    async def test_zero_value_is_shown_as_zero_not_dash(self):
        """증여·무상지급은 대금이 실제로 0이다. 결측이 아니므로 '-'가 아니다."""
        out = await self._render()
        self.assertIn("$0", out)


# ---------------------------------------------------------------------------
# 6. 투자자 수급 — 컬럼을 위치로 읽던 결함 (2026-08-17)
# ---------------------------------------------------------------------------
#
# 예전 코드는 `cols[5]=기관`, `cols[6]=외국인` 처럼 자리 번호로 읽었다. 네이버가
# 컬럼을 하나 끼워 넣으면 기관 값이 외국인 자리로 들어가는데, 숫자는 여전히
# 그럴듯해서 아무도 눈치채지 못한다(= 조용히 틀린다). 그래서 헤더 '이름'으로 찾고,
# 이름을 못 찾으면 빈 결과 대신 예외를 던진다.
#
# 전일비도 같은 계열이었다. 네이버는 전일비를 절댓값으로만 주고 방향은
# <em class="bu_pup|bu_pdn"> 로 따로 준다 — 숫자만 뽑으면 하락일도 양수가 된다.

from stock_mcp_server.naver import (  # noqa: E402
    NaverParseError,
    _flatten_header_labels,
    _parse_int_strict,
    _resolve_flow_columns,
    _signed_change,
)

# 네이버 frgn.naver 두 번째 table.type2 구조(2026-08-17 실측). 헤더가 2단이고
# 앞 5개는 rowspan=2, '외국인'은 colspan=3 이다.
FLOW_HTML = """
<table class="type2">
  <tr>
    <th rowspan="2">날짜</th><th rowspan="2">종가</th><th rowspan="2">전일비</th>
    <th rowspan="2">등락률</th><th rowspan="2">거래량</th>
    <th>기관</th><th colspan="3">외국인</th>
  </tr>
  <tr><th>순매매량</th><th>순매매량</th><th>보유주수</th><th>보유율</th></tr>
  <tr>
    <td>2026.08.10</td><td>230,000</td>
    <td><em class="bu_p bu_pdn"><span class="blind">하락</span></em><span>1,000</span></td>
    <td><span>-0.43%</span></td>
    <td>19,000,000</td><td>+625,055</td><td>-4,394,465</td>
    <td>2,730,000,000</td><td>46.70%</td>
  </tr>
</table>
"""


def _flow_table(html: str = FLOW_HTML):
    return BeautifulSoup(html, "lxml").select("table.type2")[0]


class InvestorFlowColumnTests(unittest.TestCase):
    def test_header_flattens_with_rowspan_colspan(self):
        """2단 헤더를 펼쳐 '외국인 순매매량'처럼 구분 가능한 라벨이 나와야 한다."""
        labels = _flatten_header_labels(_flow_table())
        self.assertEqual(labels[5], "기관 순매매량")
        self.assertEqual(labels[6], "외국인 순매매량")
        self.assertEqual(labels[7], "외국인 보유주수")

    def test_resolves_to_known_positions(self):
        """현재 구조에서는 기존 위치 매핑과 같은 결과여야 한다(회귀 방지)."""
        idx = _resolve_flow_columns(_flow_table())
        self.assertEqual(idx["institutional"], 5)
        self.assertEqual(idx["foreign"], 6)

    def test_inserted_column_shifts_indices_instead_of_swapping(self):
        """핵심 회귀 — 컬럼이 끼어들면 인덱스가 따라 움직여야 한다.

        위치로 읽던 예전 코드는 여기서 '개인' 값을 기관으로, 기관을 외국인으로
        읽었다. 에러 없이 값만 한 칸씩 밀리는 게 가장 위험한 실패다.
        """
        table = _flow_table()
        header = [tr for tr in table.select("tr") if tr.select("th")][0]
        new_th = BeautifulSoup('<th rowspan="2">개인</th>', "lxml").th
        header.select("th")[5].insert_before(new_th)

        idx = _resolve_flow_columns(table)
        self.assertEqual(idx["institutional"], 6)
        self.assertEqual(idx["foreign"], 7)

    def test_renamed_column_raises_instead_of_silent_empty(self):
        """이름을 못 찾으면 '데이터 없음'이 아니라 파싱 실패로 알려야 한다."""
        table = _flow_table(FLOW_HTML.replace("<th>기관</th>", "<th>기타법인</th>"))
        with self.assertRaises(NaverParseError):
            _resolve_flow_columns(table)

    def test_missing_header_raises(self):
        table = _flow_table()
        for tr in table.select("tr"):
            if tr.select("th"):
                tr.decompose()
        with self.assertRaises(NaverParseError):
            _resolve_flow_columns(table)


class InvestorFlowChangeSignTests(unittest.TestCase):
    def _change_cell(self, table=None):
        table = table or _flow_table()
        idx = _resolve_flow_columns(table)
        row = [tr for tr in table.select("tr") if tr.select("td")][0]
        return row.select("td")[idx["change"]]

    def test_down_day_change_is_negative(self):
        """전일비 절댓값 1,000 + 등락률 -0.43% → -1,000. 예전엔 +1,000이었다."""
        self.assertEqual(_signed_change(self._change_cell(), -0.43), -1000)

    def test_up_day_change_is_positive(self):
        self.assertEqual(_signed_change(self._change_cell(), 2.43), 1000)

    def test_falls_back_to_marker_when_rate_missing(self):
        """등락률을 못 읽어도 bu_pdn 마커로 방향을 복원한다."""
        self.assertEqual(_signed_change(self._change_cell(), None), -1000)

    def test_no_direction_signal_is_missing_not_positive(self):
        """방향 단서가 없으면 양수로 단정하지 않고 결측으로 둔다."""
        html = FLOW_HTML.replace(
            '<em class="bu_p bu_pdn"><span class="blind">하락</span></em>', ""
        )
        self.assertIsNone(_signed_change(self._change_cell(_flow_table(html)), None))


class StrictIntParsingTests(unittest.TestCase):
    def test_dash_is_missing_not_zero(self):
        """수급에서 0은 '순매매 0주'라는 실제 값이라 결측과 섞이면 안 된다."""
        self.assertIsNone(_parse_int_strict("-"))
        self.assertIsNone(_parse_int_strict(""))
        self.assertIsNone(_parse_int_strict("N/A"))

    def test_real_zero_stays_zero(self):
        self.assertEqual(_parse_int_strict("0"), 0)

    def test_signed_values(self):
        self.assertEqual(_parse_int_strict("+625,055"), 625055)
        self.assertEqual(_parse_int_strict("-4,394,465"), -4394465)
