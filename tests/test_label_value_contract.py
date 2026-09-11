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
# 3. 재무 기간 라벨 — 연간/분기/추정치가 한 줄에 섞이던 것
# ---------------------------------------------------------------------------
#
# 재무 값은 [연간 …, 분기 …] 로 이어 붙어 온다. 어느 구간이 어디까지인지를
# `_periods` 가 말해 주지 않으면, 뒤에서부터 집는 소비자가 분기값을 연간 확정치로
# 읽는다. 추정치는 라벨에 (E) 가 붙어야 확정치와 갈린다 — 네이버는 이걸
# isConsensus 플래그로만 주므로 우리가 라벨에 실어야 한다.

from stock_mcp_server.naver import _fin_period_labels, _fin_rows  # noqa: E402

FINANCE_PAYLOAD = {
    "itemCode": "005930",
    "financePeriodType": "annual",
    "financeInfo": {
        "trTitleList": [
            {"isConsensus": "N", "title": "2024.12.", "key": "202412"},
            {"isConsensus": "N", "title": "2025.12.", "key": "202512"},
            {"isConsensus": "Y", "title": "2026.12.", "key": "202612"},
        ],
        "rowList": [
            {"title": "매출액", "columns": {
                "202412": {"value": "3,008,709"},
                "202512": {"value": "3,336,059"},
                "202612": {"value": "7,396,375"},
            }},
            {"title": "부채비율", "columns": {
                "202412": {"value": "27.93"},
                "202512": {"value": "29.94"},
            }},
        ],
    },
}


class FinancePeriodLabelTests(unittest.TestCase):
    def test_consensus_periods_are_marked(self):
        labels, keys = _fin_period_labels(
            FINANCE_PAYLOAD["financeInfo"]["trTitleList"]
        )
        self.assertEqual(labels, ["2024.12", "2025.12", "2026.12(E)"])
        self.assertEqual(keys, ["202412", "202512", "202612"])

    def test_values_are_matched_by_column_key_not_by_order(self):
        """응답의 columns 는 순서가 보장되지 않는다 — 키로 맞춰야 한다."""
        labels, keys, rows = _fin_rows(FINANCE_PAYLOAD)
        self.assertEqual(
            [rows["매출액"][k] for k in keys],
            ["3,008,709", "3,336,059", "7,396,375"],
        )

    def test_absent_column_is_not_shifted_into_a_neighbour(self):
        """부채비율은 추정 기간 값이 없다 — 앞 값을 당겨 채우면 안 된다."""
        labels, keys, rows = _fin_rows(FINANCE_PAYLOAD)
        self.assertIsNone(rows["부채비율"].get("202612"))


# ---------------------------------------------------------------------------
# 4. 종목 상태(시장경보·관리종목)는 시세와 함께 보여야 한다
# ---------------------------------------------------------------------------
#
# 라벨을 우리가 새로 짓지 않는다 — 관리종목·투자경고는 시장이 쓰는 말이고,
# 말을 바꾸면 사용자가 HTS 에서 본 것과 대조할 수 없다.

from stock_mcp_server.naver import _status_flags  # noqa: E402


class StockStatusMarkerTests(unittest.TestCase):
    def test_managed_and_caution_flags_extracted(self):
        flags = _status_flags({"manageStatusGb": "1", "marketAlertType": "01"})
        self.assertEqual(flags, ["관리종목", "투자주의"])

    def test_warning_and_risk_labels(self):
        self.assertEqual(_status_flags({"marketAlertType": "02"}), ["투자경고"])
        self.assertEqual(_status_flags({"marketAlertType": "03"}), ["투자위험"])

    def test_normal_stock_has_no_flags(self):
        self.assertEqual(
            _status_flags({"manageStatusGb": "0", "marketAlertType": "00",
                           "tradeStopYn": "N"}),
            [],
        )

    def test_trading_halt_is_surfaced(self):
        self.assertEqual(_status_flags({"tradeStopYn": "Y"}), ["거래정지"])

    def test_unknown_alert_code_is_not_invented(self):
        """처음 보는 코드에 임의의 라벨을 붙이지 않는다."""
        self.assertEqual(_status_flags({"marketAlertType": "99"}), [])




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
# 6. 투자자 수급 — 이름표와 값이 어긋나던 계열 (2026-08-17 / 2026-09-11 갱신)
# ---------------------------------------------------------------------------
#
# 예전 코드는 HTML 표를 `cols[5]=기관`, `cols[6]=외국인` 처럼 자리 번호로 읽었다.
# 컬럼이 하나 끼면 기관 값이 외국인 자리로 들어가는데, 숫자는 여전히 그럴듯해서
# 아무도 눈치채지 못한다(= 조용히 틀린다).
#
# 2026-09 네이버가 그 화면을 걷어내면서 소스가 JSON API 로 바뀌었다. 자리 번호가
# 사라졌으니 그 결함 자체는 구조적으로 불가능해졌지만, **계약은 그대로다**:
# 필드 이름으로 읽고, 결측을 0으로 채우지 않고, 응답 모양이 다르면 빈 결과가
# 아니라 파싱 실패로 알린다.

import types  # noqa: E402

from stock_mcp_server import naver as _naver  # noqa: E402
from stock_mcp_server._cache import clear_cache  # noqa: E402
from stock_mcp_server.naver import NaverParseError, _parse_int_strict  # noqa: E402

# m.stock.naver.com/api/stock/{code}/trend 실측 응답(2026-09-11).
TREND_ROW = {
    "itemCode": "005930",
    "bizdate": "20260911",
    "foreignerPureBuyQuant": "-3,531,147",
    "foreignerHoldRatio": "46.65%",
    "organPureBuyQuant": "+2,208,594",
    "individualPureBuyQuant": "+3,643,746",
    "closePrice": "259,500",
    "compareToPreviousClosePrice": "-9,500",
    "compareToPreviousPrice": {"code": "5", "text": "하락", "name": "FALLING"},
    "accumulatedTradingVolume": "13,938,673",
}


class InvestorFlowFieldTests(unittest.IsolatedAsyncioTestCase):
    def _serve(self, payload):
        async def _fetch(url, params=None, **kwargs):
            return types.SimpleNamespace(
                status_code=200, text="", json=lambda: payload
            )

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        if hasattr(self, "_real"):
            _naver.fetch = self._real
        clear_cache()

    async def test_each_investor_goes_to_its_own_field(self):
        """기관·외국인·개인이 서로의 자리로 들어가면 안 된다."""
        self._serve([TREND_ROW])
        rows = await _naver.get_investor_flow("005930", days=5)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["institutional"], 2208594)
        self.assertEqual(row["foreign"], -3531147)
        self.assertEqual(row["individual"], 3643746)
        self.assertEqual(row["date"], "2026.09.11")

    async def test_change_keeps_its_sign(self):
        """전일비는 부호가 붙은 채로 온다 — 절댓값으로 읽으면 하락일이 양수가 된다."""
        self._serve([TREND_ROW])
        row = (await _naver.get_investor_flow("005930", days=5))[0]

        self.assertEqual(row["change"], -9500)
        self.assertEqual(row["change_rate"], -3.53)

    async def test_row_with_missing_core_value_is_dropped_not_zeroed(self):
        """순매매 0은 실제 값이다 — 결측을 0으로 채우면 둘을 구분할 수 없다."""
        broken = dict(TREND_ROW, organPureBuyQuant="-")
        self._serve([broken])

        self.assertEqual(await _naver.get_investor_flow("005930", days=5), [])

    async def test_real_zero_is_kept(self):
        self._serve([dict(TREND_ROW, organPureBuyQuant="0")])
        row = (await _naver.get_investor_flow("005930", days=5))[0]
        self.assertEqual(row["institutional"], 0)

    async def test_shape_change_raises_instead_of_silent_empty(self):
        """응답이 목록이 아니면 '데이터 없음'이 아니라 파싱 실패로 알려야 한다."""
        self._serve({"datas": []})
        with self.assertRaises(NaverParseError):
            await _naver.get_investor_flow("005930", days=5)

    async def test_html_instead_of_json_raises(self):
        """2026-09 사고의 모양 — 200 OK 로 SPA 껍데기가 오던 경우."""

        async def _fetch(url, params=None, **kwargs):
            def _boom():
                raise ValueError("not json")

            return types.SimpleNamespace(
                status_code=200, text="<!doctype html>", json=_boom
            )

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()
        with self.assertRaises(NaverParseError):
            await _naver.get_investor_flow("005930", days=5)


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


# ---------------------------------------------------------------------------
# 7. 현재가 — "값이 없음"과 "못 읽었음"이 같은 모양이던 것 (2026-08-17)
# ---------------------------------------------------------------------------
#
# 결측을 0으로 채우면 거래정지 종목의 진짜 0과 구분되지 않는다. KRX/NXT 분리와
# 상태 플래그를 포함한 현재가 계약은 tests/test_naver_price.py 가 전담한다.


# ---------------------------------------------------------------------------
# 8. 차트 응답 — 형식이 바뀌면 '상장폐지된 종목'처럼 보이던 것 (2026-08-17)
# ---------------------------------------------------------------------------
#
# fchart 응답의 각 행은 int() 실패 시 조용히 skip 된다. 네이버가 숫자 표기를
# 바꾸면 전 행이 버려져 빈 리스트가 되고, 화면엔 '차트 데이터 없음'이 뜬다.
# 데이터가 없는 종목과 형식이 바뀐 경우가 같은 모양이 되면 안 된다.

import types  # noqa: E402

from stock_mcp_server import naver as _naver  # noqa: E402
from stock_mcp_server._cache import clear_cache  # noqa: E402

CHART_HEADER = "[['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],"
CHART_ROW = '["20260810", 236000, 238500, 228500, 230000, 16327805, 46.53],'


class ChartParseFailureTests(unittest.IsolatedAsyncioTestCase):
    def _serve(self, text: str):
        async def _fetch(url, params=None, **kwargs):
            return types.SimpleNamespace(text=text)

        self._real_fetch = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        if hasattr(self, "_real_fetch"):
            _naver.fetch = self._real_fetch
        clear_cache()

    async def test_normal_response_parses(self):
        self._serve(f"{CHART_HEADER}\n{CHART_ROW}\n]")
        rows = await _naver.get_ohlcv("005930", "day", 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 230000)

    async def test_format_change_raises_instead_of_empty(self):
        """숫자 표기가 바뀌어 전 행이 버려지면 '없음'이 아니라 파싱 실패다."""
        broken = CHART_ROW.replace("236000", "약236000").replace("238500", "약238500")
        broken = broken.replace("228500", "약228500").replace("230000", "약230000")
        self._serve(f"{CHART_HEADER}\n{broken}\n]")
        with self.assertRaises(_naver.NaverParseError):
            await _naver.get_ohlcv("005930", "day", 5)

    async def test_genuinely_empty_response_is_not_an_error(self):
        """데이터가 진짜 없는 종목은 예외가 아니라 빈 리스트다(오탐 방지)."""
        self._serve("[\n]")
        self.assertEqual(await _naver.get_ohlcv("999999", "day", 5), [])

    async def test_header_only_response_is_not_an_error(self):
        self._serve(f"{CHART_HEADER}\n]")
        self.assertEqual(await _naver.get_ohlcv("999999", "day", 5), [])


# ---------------------------------------------------------------------------
# 9. 테마·업종·리포트 목록 — 이름으로 읽는 계약 (2026-08-17 / 2026-09-11 갱신)
# ---------------------------------------------------------------------------
#
# 예전에는 테마/업종 상세의 현재가·거래량을 cells[2]/cells[7] 처럼 자리로 읽었다.
# 컬럼이 하나 끼면 주가 자리에 호가가 들어가도 알 수 없었다. 새 소스는 JSON 이라
# 자리 개념이 없지만, 계약은 같다 — 필드 이름으로 읽고, 등락률의 부호를 잃지 않고,
# 응답 모양이 다르면 파싱 실패로 알린다.

THEME_GROUP = {
    "no": 405,
    "name": "MLCC(적층세라믹콘덴서)",
    "totalCount": 11,
    "changeRate": "7.69",
    "riseCount": 8,
    "fallCount": 2,
    "steadyCount": 1,
}

GROUP_STOCK = {
    "itemCode": "052710",
    "stockName": "아모텍",
    "closePrice": "15,130",
    "closePriceRaw": "15130",
    "compareToPreviousClosePrice": "3,490",
    "compareToPreviousPrice": {"code": "1", "text": "상한", "name": "UPPER_LIMIT"},
    "fluctuationsRatio": "29.98",
    "accumulatedTradingVolume": "3,396,316",
    "accumulatedTradingVolumeRaw": "3396316",
}


class _JsonServing(unittest.IsolatedAsyncioTestCase):
    """URL 별로 미리 정해둔 JSON 을 돌려주는 fetch 대역."""

    def _serve(self, router):
        async def _fetch(url, params=None, **kwargs):
            payload = router(url, params or {})
            return types.SimpleNamespace(
                status_code=200, text="", json=lambda: payload
            )

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        if hasattr(self, "_real"):
            _naver.fetch = self._real
        clear_cache()


class GroupListTests(_JsonServing):
    async def test_theme_list_keeps_counts_in_their_own_fields(self):
        self._serve(lambda url, params: {"groups": [THEME_GROUP], "totalCount": 266})
        themes = await _naver.list_themes(1)

        self.assertEqual(len(themes), 1)
        t = themes[0]
        self.assertEqual(t["theme_id"], "405")
        self.assertEqual(t["change_rate"], "+7.69%")
        self.assertEqual(t["total_count"], 11)
        self.assertEqual((t["up_count"], t["flat_count"], t["down_count"]), (8, 1, 2))

    async def test_sector_list_shares_the_shape(self):
        self._serve(lambda url, params: {"groups": [dict(THEME_GROUP, changeRate="-3.14")]})
        sectors = await _naver.list_sectors()

        self.assertEqual(sectors[0]["sector_id"], "405")
        self.assertEqual(sectors[0]["change_rate"], "-3.14%")

    async def test_missing_groups_key_raises(self):
        """'테마가 없다'가 아니라 '응답이 바뀌었다'로 알려야 한다."""
        self._serve(lambda url, params: {"stocks": []})
        with self.assertRaises(NaverParseError):
            await _naver.list_themes(1)

    async def test_empty_group_list_is_not_an_error(self):
        self._serve(lambda url, params: {"groups": []})
        self.assertEqual(await _naver.list_themes(7), [])


class GroupMemberTests(_JsonServing):
    def _router(self, reasons=None):
        def route(url, params):
            if url.endswith("/stocks/theme"):
                return {"groups": [THEME_GROUP]}
            return {
                "stocks": [GROUP_STOCK],
                "groupInfo": THEME_GROUP,
                "themeItemInfoMap": reasons or {},
            }

        return route

    async def test_price_and_volume_come_from_named_fields(self):
        self._serve(self._router())
        result = await _naver.get_theme_stocks("MLCC", count=5)

        stock = result["stocks"][0]
        self.assertEqual(stock["code"], "052710")
        self.assertEqual(stock["price"], 15130)        # 호가가 아니라 종가
        self.assertEqual(stock["volume"], 3396316)
        self.assertEqual(stock["change_rate"], "+29.98%")

    async def test_reason_is_matched_by_code_not_by_position(self):
        self._serve(self._router({"052710": "MLCC를 새 사업 아이템으로 양산중."}))
        stock = (await _naver.get_theme_stocks("MLCC", count=5))["stocks"][0]
        self.assertTrue(stock["reason"].startswith("MLCC를 새 사업"))

    async def test_reason_of_another_code_never_leaks_in(self):
        self._serve(self._router({"005930": "전혀 다른 종목의 편입사유"}))
        stock = (await _naver.get_theme_stocks("MLCC", count=5))["stocks"][0]
        self.assertEqual(stock["reason"], "")

    async def test_unknown_theme_returns_empty_not_error(self):
        self._serve(lambda url, params: {"groups": []})
        result = await _naver.get_theme_stocks("없는테마", count=5)
        self.assertIsNone(result["theme_id"])
        self.assertEqual(result["stocks"], [])


class ReportListTests(_JsonServing):
    REPORT = {
        "nid": "96027",
        "title": "HBM으로 매크로 우려 극복",
        "brokerName": "미래에셋증권",
        "writeDate": "2026-09-07",
        "readCount": "32873",
        "itemCode": "005930",
        "itemName": "삼성전자",
        "goalPrice": "400000",
        "opinionText": "매수",
        "content": "<p><strong>환율 하락효과</strong> 반영</p>",
        "attachUrl": "https://stock.pstatic.net/a.pdf",
    }

    async def test_fields_keep_their_labels(self):
        self._serve(lambda url, params: {"005930": [self.REPORT]})
        rows = await _naver.get_reports("005930", 3)

        self.assertEqual(rows[0]["broker"], "미래에셋증권")
        self.assertEqual(rows[0]["date"], "2026-09-07")
        self.assertEqual(rows[0]["views"], 32873)
        self.assertEqual(rows[0]["stock"], "삼성전자")

    async def test_stock_without_reports_is_empty_not_error(self):
        self._serve(lambda url, params: {})
        self.assertEqual(await _naver.get_reports("999999", 3), [])

    async def test_detail_strips_markup_and_keeps_numbers(self):
        self._serve(lambda url, params: self.REPORT)
        detail = await _naver.get_report_detail("96027")

        self.assertEqual(detail["target_price"], 400000)
        self.assertEqual(detail["opinion"], "매수")
        self.assertEqual(detail["pdf_url"], "https://stock.pstatic.net/a.pdf")
        self.assertNotIn("<", detail["summary"])

    async def test_detail_without_body_is_flagged_not_silent(self):
        self._serve(lambda url, params: {k: v for k, v in self.REPORT.items() if k != "content"})
        detail = await _naver.get_report_detail("96027")
        self.assertEqual(detail[_naver.PARSE_MISS_KEY], ["summary"])


# ---------------------------------------------------------------------------
# 10. 랭킹·시장지수 (2026-08-17 / 2026-09-11 갱신)
# ---------------------------------------------------------------------------
#
# 랭킹은 페이지마다 표 구성이 달라 공용 파서가 자리를 가정하고 있었다. 지금은
# 정렬 키만 다른 하나의 API 라 그 문제가 사라졌다. 남은 계약은 단위다 —
# `market_cap_billion` 은 이름이 '억원'이므로 원 단위를 그대로 실으면 안 된다.

RANK_ROW = {
    "itemcode": "005930",
    "itemname": "삼성전자",
    "nowPrice": "259500",
    "prevChangeRate": "-3.53",
    "tradeVolume": "13938673",
    "marketSum": "1517109298000000",
}


class RankingFieldTests(_JsonServing):
    async def test_rank_follows_response_order(self):
        rows = [RANK_ROW, dict(RANK_ROW, itemcode="000660", itemname="SK하이닉스")]
        self._serve(lambda url, params: rows)
        ranked = await _naver.get_volume_ranking(count=5)

        self.assertEqual([r["rank"] for r in ranked], [1, 2])
        self.assertEqual(ranked[0]["code"], "005930")

    async def test_change_rate_keeps_its_sign(self):
        self._serve(lambda url, params: [RANK_ROW])
        ranked = await _naver.get_change_ranking("down", "ALL", 5)
        self.assertEqual(ranked[0]["change_rate"], "-3.53%")

    async def test_market_cap_is_converted_to_its_label_unit(self):
        """`market_cap_billion` 의 단위는 억원이다. 원 단위를 그대로 넣으면 안 된다."""
        self._serve(lambda url, params: [RANK_ROW])
        ranked = await _naver.get_market_cap_ranking("KOSPI", 5)
        self.assertEqual(ranked[0]["market_cap_billion"], 15171092)

    async def test_trade_value_is_an_estimate_from_price_times_volume(self):
        """키 이름이 '_est_' 다 — 실제 체결 거래대금이 아니라 추산임을 유지한다."""
        self._serve(lambda url, params: [RANK_ROW])
        ranked = await _naver.get_volume_ranking(count=5)
        self.assertEqual(ranked[0]["trade_value_est_krw"], 259500 * 13938673)

    async def test_shape_change_raises_instead_of_silent_empty(self):
        self._serve(lambda url, params: {"datas": []})
        with self.assertRaises(NaverParseError):
            await _naver.get_volume_ranking(count=5)


class MarketIndexValueTests(_JsonServing):
    def _index_payload(self, **over):
        base = {
            "itemCode": "KOSPI",
            "closePrice": "6,909.91",
            "compareToPreviousClosePrice": "-124.01",
            "fluctuationsRatio": "-1.76",
        }
        base.update(over)
        return {"datas": [base, dict(base, itemCode="KOSDAQ")]}

    async def test_signs_come_from_the_values_not_a_direction_label(self):
        """방향 라벨은 값과 어긋난 채 내려온 적이 있다 — 부호는 숫자에서 읽는다."""
        self._serve(lambda url, params: self._index_payload())
        items = await _naver.get_market_index()

        self.assertEqual(items[0]["value"], 6909.91)
        self.assertEqual(items[0]["change_value"], -124.01)
        self.assertEqual(items[0]["change_rate"], -1.76)

    async def test_unparseable_index_value_is_missing_not_string(self):
        """예전엔 float 실패 시 원문 문자열을 value 에 넣었다."""
        self._serve(lambda url, params: self._index_payload(closePrice="N/A"))
        items = await _naver.get_market_index()

        for item in items:
            self.assertNotIn("value", item)
            self.assertIn("value", item[_naver.PARSE_MISS_KEY])

    async def test_absent_index_is_reported_not_invented(self):
        self._serve(lambda url, params: {"datas": []})
        items = await _naver.get_market_index()

        self.assertEqual([i["index"] for i in items], ["KOSPI", "KOSDAQ"])
        for item in items:
            self.assertEqual(
                item[_naver.PARSE_MISS_KEY], ["value", "change_value", "change_rate"]
            )



# ---------------------------------------------------------------------------
# 11. ETF·컨센서스·재무 — '자료 없음'과 '형식 변경'의 분리 (2026-08-17)
# ---------------------------------------------------------------------------
#
# 이 셋은 JSON/JS 변수를 읽는다. 형식이 바뀌면 빈 dict 가 되어 화면엔 각각
# 'ETF 없음' / '컨센서스 없음' / '재무지표 없음'으로 뜬다. 커버리지가 없는
# 종목과 구분되지 않으므로, 원천이 통째로 안 읽히면 예외로 알린다.


class SourceFormatChangeTests(unittest.IsolatedAsyncioTestCase):
    def _serve_text(self, text: str):
        async def _fetch(url, params=None, **kwargs):
            return types.SimpleNamespace(text=text)

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def _serve_json(self, router):
        async def _fetch(url, params=None, **kwargs):
            return types.SimpleNamespace(
                status_code=200, text="", json=lambda: router(url, params or {})
            )

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        if hasattr(self, "_real"):
            _naver.fetch = self._real
        clear_cache()

    async def test_etf_list_empty_payload_raises(self):
        class _Resp:
            content = '{"result":{}}'.encode("euc-kr")

        async def _fetch(url, params=None, **kwargs):
            return _Resp()

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()
        with self.assertRaises(_naver.NaverParseError):
            await _naver.get_etf_list()

    async def test_consensus_without_js_vars_raises(self):
        """커버리지 없는 종목도 chartData2/res '선언'은 있다 — 아예 없으면 형식 변경."""
        self._serve_text("<html><body>no vars here</body></html>")
        with self.assertRaises(_naver.NaverParseError):
            await _naver.get_consensus("005930")

    async def test_etf_detail_without_js_vars_raises(self):
        self._serve_text("<html><body>no vars here</body></html>")
        with self.assertRaises(_naver.NaverParseError):
            await _naver.get_etf_detail("069500")

    async def test_financials_missing_table_is_flagged_not_silent(self):
        """종목명은 읽히는데 재무 표가 없으면 '자료 없음'이 아니라 파싱 실패다."""

        def route(url, params):
            if url.endswith("/integration"):
                return {"stockName": "삼성전자"}
            return {"itemCode": "005930", "financeInfo": None}

        self._serve_json(route)
        data = await _naver.get_financials("005930")
        self.assertEqual(data.get(_naver.PARSE_MISS_KEY), ["financial_table"])

    async def test_financials_unknown_page_is_not_flagged(self):
        """종목명조차 없으면 잘못된 코드일 수 있어 파싱 실패로 단정하지 않는다."""

        def route(url, params):
            if url.endswith("/integration"):
                return {"code": "StockConflict"}
            return {"itemCode": "999999", "financeInfo": None}

        self._serve_json(route)
        data = await _naver.get_financials("999999")
        self.assertNotIn(_naver.PARSE_MISS_KEY, data)

    async def test_financials_keep_annual_and_quarterly_apart(self):
        """[연간 …, 분기 …] 로 이어 붙이고 경계를 _periods 로 알려야 한다."""

        def route(url, params):
            if url.endswith("/integration"):
                return {"stockName": "삼성전자", "totalInfos": [
                    {"code": "marketValue", "key": "시총", "value": "1,517조 1,093억"},
                    {"code": "dividendYieldRatio", "key": "배당수익률",
                     "value": "0.64%", "valueDesc": "2025.12."},
                ]}
            period = "quarter" if url.endswith("/quarter") else "annual"
            titles = (
                [{"isConsensus": "N", "title": "2026.06.", "key": "202606"}]
                if period == "quarter"
                else [{"isConsensus": "N", "title": "2025.12.", "key": "202512"}]
            )
            key = titles[0]["key"]
            return {"financeInfo": {
                "trTitleList": titles,
                "rowList": [{"title": "PER", "columns": {key: {"value": "18.27"}}}],
            }}

        self._serve_json(route)
        data = await _naver.get_financials("005930")

        self.assertEqual(data["_periods"],
                         {"annual": ["2025.12"], "quarterly": ["2026.06"]})
        self.assertEqual(data["PER(배)"], ["18.27", "18.27"])   # 라벨 수와 길이가 같다
        self.assertEqual(data["시가총액"], "1,517조 1,093억")

    async def test_dividend_rate_lands_only_on_its_own_period(self):
        """배당수익률은 valueDesc 가 말하는 기간의 값이다 — 최신 칸에 밀어 넣지 않는다."""

        def route(url, params):
            if url.endswith("/integration"):
                return {"stockName": "삼성전자", "totalInfos": [
                    {"code": "dividendYieldRatio", "value": "0.64%",
                     "valueDesc": "2024.12."},
                ]}
            if url.endswith("/quarter"):
                return {"financeInfo": {"trTitleList": [], "rowList": []}}
            return {"financeInfo": {
                "trTitleList": [
                    {"isConsensus": "N", "title": "2024.12.", "key": "202412"},
                    {"isConsensus": "N", "title": "2025.12.", "key": "202512"},
                ],
                "rowList": [{"title": "PER", "columns": {
                    "202412": {"value": "10.75"}, "202512": {"value": "18.27"}}}],
            }}

        self._serve_json(route)
        data = await _naver.get_financials("005930")

        self.assertEqual(data["시가배당률(%)"], ["0.64", "-"])

    async def test_dividend_rate_of_unknown_period_is_dropped(self):
        """기간이 우리 라벨에 없으면 아무 칸에도 넣지 않는다."""

        def route(url, params):
            if url.endswith("/integration"):
                return {"stockName": "삼성전자", "totalInfos": [
                    {"code": "dividendYieldRatio", "value": "0.64%",
                     "valueDesc": "2019.12."},
                ]}
            if url.endswith("/quarter"):
                return {"financeInfo": {"trTitleList": [], "rowList": []}}
            return {"financeInfo": {
                "trTitleList": [{"isConsensus": "N", "title": "2025.12.", "key": "202512"}],
                "rowList": [{"title": "PER", "columns": {"202512": {"value": "18.27"}}}],
            }}

        self._serve_json(route)
        data = await _naver.get_financials("005930")

        self.assertNotIn("시가배당률(%)", data)
