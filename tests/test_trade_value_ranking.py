"""'거래대금 상위'가 정말 거래대금 상위인가 — get_volume_ranking·screen_by_flow.

2026-09-17 02:24 실측(data_as_of 2026-09-16): `get_volume_ranking(sort_by="trade_value")`
의 제목은 "거래대금 상위"인데 1위가 대한광통신 7,410억(추산)이었다. 그날 거래대금은
SK하이닉스 4.9조·삼성전자 2.9조가 1·2위인데 표에 없었고, 10위에는 3원짜리 정리매매
종목(코스나인 -40%, 1.0억)이 있었다.

원인: 거래량 상위 count 개를 받은 뒤 거래대금으로 다시 줄 세우기만 했다. 모집단이
거래량(주수) 상위라 주가가 높은 대형주가 처음부터 빠졌다. screen_by_flow 도 같은 함수를
불러 "거래대금 상위 100 중"이라는 머리말로 같은 모집단을 썼다.

고친 뒤: 네이버 '거래대금 상위' 목록(orderType=priceTop)을 받아 그 순서를 그대로 쓴다.
표의 거래대금은 그 순서를 만든 값(tradeAmount)이라 열의 크기 순서가 순위와 맞는다.

아래 목록은 2026-09-16 장 마감 후 실제 응답의 앞 10행이다. 네트워크는 쓰지 않는다 —
원천에서 확인하는 실측은 tests/dev_naver_json_smoke.py 에 있다.
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import naver as _naver
from stock_mcp_server import server
from stock_mcp_server._cache import clear_cache

_CLOSED = {"krx": {"is_open": False, "last_trading_day": "2026-09-16"}}


def _row(code, name, price, rate, volume, amount):
    return {
        "itemcode": code, "itemname": name, "type": "ST", "tradeStopYn": "N",
        "nowPrice": str(price), "prevChangeRate": rate, "tradeVolume": str(volume),
        "tradeAmount": str(amount), "tradingSessionType": "AFTER_MARKET",
    }


# orderType=quantTop (거래량 상위)
QUANT_TOP_0916 = [
    _row("069540", "빛과전자", 4425, "7.4", 73431112, 335009000000),
    _row("010170", "대한광통신", 16230, "14.78", 45656543, 714691004000),
    _row("082660", "코스나인", 3, "-40.0", 34729373, 112153000),
    _row("062970", "한국첨단소재", 3365, "0.15", 31964709, 113484708000),
    _row("413630", "씨피시스템", 4680, "4.0", 27646690, 135597792000),
    _row("321370", "센서뷰", 1372, "16.97", 24584922, 33295199000),
    _row("072950", "빛샘전자", 13910, "30.0", 14950961, 195459058000),
    _row("005160", "동국산업", 3390, "10.24", 14816818, 53250639000),
    _row("054920", "한컴위드", 5300, "1.34", 12776780, 69219855000),
    _row("004310", "현대약품", 10150, "0.3", 12531260, 129695000000),
]

# orderType=priceTop (네이버 화면 이름 '거래대금 상위')
PRICE_TOP_0916 = [
    _row("000660", "SK하이닉스", 1757000, "3.96", 2807337, 4858811000000),
    _row("005930", "삼성전자", 253500, "2.01", 11438019, 2877523000000),
    _row("010170", "대한광통신", 16230, "14.78", 45656543, 714691004000),
    _row("005935", "삼성전자우", 194300, "3.85", 3635528, 702889000000),
    _row("009150", "삼성전기", 1372000, "4.18", 430275, 582612000000),
    _row("402340", "SK스퀘어", 1017000, "1.7", 338382, 340212000000),
    _row("069540", "빛과전자", 4425, "7.4", 73431112, 335009000000),
    _row("036930", "주성엔지니어링", 200500, "3.99", 1122839, 220043714000),
    _row("072950", "빛샘전자", 13910, "30.0", 14950961, 195459058000),
    _row("034020", "두산에너빌리티", 84100, "-2.44", 2140785, 180461000000),
]


class _FakeNaverList(unittest.IsolatedAsyncioTestCase):
    """시장 목록 API 흉내. 정렬 키마다 그날의 실제 목록을 돌려주고, 받은 키를 적어 둔다."""

    def setUp(self):
        self.orders: list[str] = []
        lists = {"quantTop": QUANT_TOP_0916, "priceTop": PRICE_TOP_0916}

        async def _fetch(url, params=None, **kwargs):
            order = (params or {}).get("orderType")
            self.orders.append(order)
            # 모르는 정렬 키에 엉뚱한 목록을 주지 않는다 — 형식 오류로 떨어지게 한다.
            payload = [dict(r) for r in lists[order]] if order in lists else {"error": order}
            return types.SimpleNamespace(status_code=200, text="", json=lambda: payload)

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        _naver.fetch = self._real
        clear_cache()


class TradeValueRankingSourceTests(_FakeNaverList):
    async def test_trade_value_asks_naver_for_its_trade_value_list(self):
        await _naver.get_volume_ranking("ALL", 10, "trade_value")
        self.assertEqual(self.orders, ["priceTop"])

    async def test_volume_still_asks_for_the_volume_list(self):
        await _naver.get_volume_ranking("ALL", 10, "volume")
        self.assertEqual(self.orders, ["quantTop"])

    async def test_large_caps_lead_the_trade_value_ranking(self):
        """2026-09-16: 삼성전자·SK하이닉스가 들어오고 3원짜리 정리매매 종목은 빠진다."""
        ranked = await _naver.get_volume_ranking("ALL", 10, "trade_value")
        codes = [r["code"] for r in ranked]

        self.assertEqual(codes[:2], ["000660", "005930"])
        self.assertNotIn("082660", codes)
        self.assertEqual([r["rank"] for r in ranked], list(range(1, 11)))

    async def test_rank_order_matches_the_trade_value_shown(self):
        """순위와 표에 찍는 거래대금의 크기 순서가 어긋나면 안 된다.

        현재가×거래량 추산은 원천 거래대금과 몇 % 다르다. 2026-09-16 13·14위
        엑스게이트(실제 1,674.5억·추산 1,613.3억)와 삼성SDI(실제 1,653.8억·추산
        1,656.3억)는 추산으로 보면 순서가 뒤집힌다 — 그래서 순서도 값도 원천을 쓴다.
        """
        ranked = await _naver.get_volume_ranking("ALL", 10, "trade_value")
        values = [r["trade_value_krw"] for r in ranked]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(values[0], 4858811000000)

    async def test_sort_by_is_not_case_sensitive(self):
        await _naver.get_volume_ranking("ALL", 10, " TRADE_VALUE ")
        self.assertEqual(self.orders, ["priceTop"])

    async def test_missing_trade_amount_is_none_not_zero(self):
        rows = [dict(PRICE_TOP_0916[0])]
        del rows[0]["tradeAmount"]

        async def _fetch(url, params=None, **kwargs):
            return types.SimpleNamespace(status_code=200, text="", json=lambda: rows)

        _naver.fetch = _fetch
        clear_cache()
        ranked = await _naver.get_volume_ranking("ALL", 1, "trade_value")
        self.assertIsNone(ranked[0]["trade_value_krw"])


class VolumeRankingToolTests(_FakeNaverList):
    async def _tool(self, **kwargs) -> str:
        with patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_volume_ranking(**kwargs)

    async def test_trade_value_table_is_the_trade_value_top(self):
        text = await self._tool(market="ALL", count=10, sort_by="trade_value")
        body = text.split(rmeta.MARKER_START, 1)[0]

        self.assertIn("거래대금 상위 (ALL, 10개, 정렬=trade_value)", body)
        self.assertIn("1 | 000660 | SK하이닉스 | 1,757,000 | +3.96% | 2,807,337 | 48,588.1", body)
        self.assertIn("2 | 005930 | 삼성전자 | 253,500 | +2.01% | 11,438,019 | 28,775.2", body)
        self.assertNotIn("코스나인", body)

    async def test_trade_value_column_is_not_labelled_an_estimate(self):
        """열 값이 원천 거래대금이므로 '추산'이라 적지 않는다. 대신 KRX 체결분임을 밝힌다."""
        text = await self._tool(sort_by="trade_value", count=10)
        body = text.split(rmeta.MARKER_START, 1)[0]

        self.assertIn("| 거래대금(억원)\n", body)
        self.assertNotIn("추산", body)
        self.assertIn("KRX 체결분", body)

    async def test_missing_trade_value_says_so(self):
        ranks = [{"rank": 1, "code": "000660", "name": "SK하이닉스", "price": 1757000,
                  "change_rate": "+3.96%", "volume": 2807337, "trade_value_krw": None,
                  "price_session": "after_market"}]
        with patch.object(server, "naver_get_volume_ranking", AsyncMock(return_value=ranks)):
            text = await self._tool(sort_by="trade_value", count=1)
        self.assertIn("| 2,807,337 | 데이터 없음", text)

    async def test_empty_result_names_the_ranking_asked_for(self):
        with patch.object(server, "naver_get_volume_ranking", AsyncMock(return_value=[])):
            text = await self._tool(sort_by="trade_value")
        self.assertIn("거래대금 순위를 가져올 수 없습니다", text)


class ScreenByFlowPopulationTests(_FakeNaverList):
    async def test_default_candidates_are_the_trade_value_top(self):
        seen: list[str] = []

        async def fake_flow(code, days):
            seen.append(code)
            return [{"date": "2026-09-16", "institutional": 1, "foreign": 1,
                     "close": 0, "volume": 0}] * days

        with patch.object(server, "get_investor_flow", side_effect=fake_flow), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.screen_by_flow(top_n=10)

        self.assertEqual(self.orders, ["priceTop"])
        self.assertIn("000660", seen)
        self.assertIn("005930", seen)
        self.assertNotIn("082660", seen)
        self.assertIn("수급 스크리닝 — 거래대금 상위 10 (ALL)", text)

    async def test_empty_ranking_message_names_the_ranking(self):
        with patch.object(server, "naver_get_volume_ranking", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.screen_by_flow()

        meta = json.loads(text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0])
        self.assertIn("거래대금 순위를 가져올 수 없습니다", text)
        self.assertEqual(meta["data_completeness"], "none")


if __name__ == "__main__":
    unittest.main()
