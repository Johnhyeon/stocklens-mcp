"""ETF 목록의 정렬 이름표와 실제 순서가 같은가 — get_etf_list.

2026-09-17 실측에서 나온 결함 세 가지:

1. '3개월 수익률' 정렬이 abs() 로 줄을 세워서, 맨 위에 수익률이 가장 나쁜 ETF 들이 왔다
   (TIGER 반도체TOP10레버리지 -67.1%, SOL SK하이닉스단일종목레버리지 -63.6% …).
2. 가이드에 적힌 정렬 이름(market_cap·volume·return_1m·dividend_yield …)은 응답에 없는
   필드라, 전부 0 으로 보고 응답 순서(시가총액 순)가 그대로 나갔다. 오류도 안내도 없었다.
3. 장 시작 전에는 목록 1,171개 전부 등락률·거래량이 0 으로 온다. 표에는 모든 ETF 가
   '+0.00%' 로 찍혔고(보합처럼), 거래량 정렬은 조용히 시가총액 순이었다.

네트워크는 쓰지 않는다. 네이버 ETF 목록 API 는 EUC-KR JSON 한 덩어리다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import naver, server
from stock_mcp_server._cache import clear_cache

_CLOSED = {"krx": {"is_open": False, "last_trading_day": "2026-09-16"}}
_OPEN = {"krx": {"is_open": True, "last_trading_day": "2026-09-17"}}


def _etf(code, name, *, ret3m, market_sum, quant=1000, change=0.5, nav=10000.0, tab=2):
    return {
        "itemcode": code, "itemname": name, "etfTabCode": tab, "nowVal": int(nav),
        "risefall": "2", "changeVal": 10, "changeRate": change, "nav": nav,
        "threeMonthEarnRate": ret3m, "quant": quant, "amonut": 0, "marketSum": market_sum,
    }


# 응답 순서는 네이버처럼 시가총액 내림차순이다.
_ITEMS = [
    _etf("069500", "KODEX 200", ret3m=-23.3, market_sum=246660, quant=5_000_000, change=1.2),
    _etf("396500", "TIGER 반도체TOP10레버리지", ret3m=-60.0, market_sum=9000, quant=9_000_000, change=-3.1),
    _etf("261220", "KODEX WTI원유선물(H)", ret3m=20.0, market_sum=3000, quant=200_000, change=0.8),
    _etf("0000Z0", "ACE 러시아MSCI(합성)", ret3m=0.0, market_sum=500, quant=0, change=0.0),
    _etf("0183J0", "KODEX 200커버드콜액티브", ret3m=None, market_sum=100, quant=50_000, change=0.1),
]


def _resp(items: list[dict]) -> Mock:
    resp = Mock()
    resp.content = json.dumps({"result": {"etfItemList": items}}, ensure_ascii=False).encode("euc-kr")
    return resp


def _preopen(items: list[dict]) -> list[dict]:
    """장 시작 전 모양 — 등락률·거래량만 전 종목 0."""
    return [dict(i, changeRate=0.0, changeVal=0, quant=0, amonut=0) for i in items]


class _EtfList(unittest.IsolatedAsyncioTestCase):
    items = _ITEMS

    def setUp(self):
        clear_cache()
        self.fetch = AsyncMock(return_value=_resp(self.items))
        self._patch = patch.object(naver, "fetch", self.fetch)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        clear_cache()

    async def _names(self, **kwargs) -> list[str]:
        data = await naver.get_etf_list(limit=50, **kwargs)
        return [i["name"] for i in data["items"]]

    async def _tool(self, clock=_CLOSED, **kwargs) -> str:
        with patch.object(server, "build_market_clock", return_value=clock):
            return await server.get_etf_list(**kwargs)


class ReturnSortTests(_EtfList):
    async def test_gainer_ranks_above_heavy_loser(self):
        """+20% ETF 가 -60% ETF 보다 앞이다 — abs() 였을 때는 반대였다."""
        names = await self._names(sort_by="threeMonthEarnRate")
        self.assertLess(names.index("KODEX WTI원유선물(H)"), names.index("TIGER 반도체TOP10레버리지"))

    async def test_three_month_return_is_signed_descending(self):
        data = await naver.get_etf_list(sort_by="threeMonthEarnRate", limit=50)
        self.assertEqual([i["return_3m"] for i in data["items"]], [20.0, 0.0, -23.3, -60.0])

    async def test_missing_return_is_left_out_and_counted(self):
        data = await naver.get_etf_list(sort_by="threeMonthEarnRate", limit=50)
        self.assertNotIn("KODEX 200커버드콜액티브", [i["name"] for i in data["items"]])
        self.assertEqual((data["total"], data["no_sort_value"]), (4, 1))

    async def test_nonnegative_keys_keep_their_order(self):
        """시가총액·거래량·NAV 는 음수가 없어 abs() 를 빼도 순서가 그대로다."""
        self.assertEqual(
            await self._names(sort_by="marketSum"),
            ["KODEX 200", "TIGER 반도체TOP10레버리지", "KODEX WTI원유선물(H)",
             "ACE 러시아MSCI(합성)", "KODEX 200커버드콜액티브"])
        self.assertEqual(
            await self._names(sort_by="quant"),
            ["TIGER 반도체TOP10레버리지", "KODEX 200", "KODEX WTI원유선물(H)",
             "KODEX 200커버드콜액티브", "ACE 러시아MSCI(합성)"])

    async def test_missing_market_cap_goes_last_not_as_zero(self):
        items = [dict(_ITEMS[0], marketSum=None), _ITEMS[3]]
        self.fetch.return_value = _resp(items)
        data = await naver.get_etf_list(sort_by="marketSum", limit=50)
        self.assertEqual([i["name"] for i in data["items"]], ["ACE 러시아MSCI(합성)", "KODEX 200"])
        self.assertEqual(data["no_sort_value"], 1)


class SortKeyNameTests(_EtfList):
    async def test_guide_names_with_a_real_field_are_accepted(self):
        self.assertEqual(await self._names(sort_by="return_3m"),
                         await self._names(sort_by="threeMonthEarnRate"))
        self.assertEqual(await self._names(sort_by="volume"), await self._names(sort_by="quant"))
        self.assertEqual(await self._names(sort_by="market_cap"), await self._names(sort_by="marketSum"))

    async def test_value_not_in_the_list_is_refused_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            await naver.get_etf_list(sort_by="dividend_yield")
        self.fetch.assert_not_called()

    async def test_tool_explains_unsupported_sort(self):
        text = await server.get_etf_list(sort_by="return_1y")
        self.assertIn("'return_1y' 기준으로 정렬할 수 없습니다", text)
        self.assertIn("threeMonthEarnRate(3개월 수익률 높은 순)", text)
        self.fetch.assert_not_called()


class ToolOutputTests(_EtfList):
    async def test_header_names_the_sort_order(self):
        body = (await self._tool(sort_by="threeMonthEarnRate")).split(rmeta.MARKER_START)[0]
        self.assertIn("ETF 목록 (4개 중 상위 4개, 정렬: 3개월 수익률 높은 순)", body)
        self.assertIn("3개월 수익률 값이 없는 ETF 1개는 순위에서 뺐습니다", body)
        first = body.index("KODEX WTI원유선물(H)")
        self.assertLess(first, body.index("TIGER 반도체TOP10레버리지"))

    async def test_zero_three_month_return_is_shown(self):
        """0.0% 는 값이다 — 참거짓으로 거르면 칸이 사라졌다."""
        body = await self._tool(sort_by="threeMonthEarnRate")
        self.assertIn("ACE 러시아MSCI(합성)** (0000Z0) | 10,000원 (+0.00%) | NAV 10,000원 | 시총 500억원 | 3M: +0.0%", body)


class PreOpenListTests(_EtfList):
    items = _preopen(_ITEMS)

    async def test_blank_quotes_are_not_shown_as_flat(self):
        text = await self._tool()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("+0.00%", body)
        self.assertIn("(등락률 없음)", body)
        self.assertIn("보합이라는 뜻이 아니어서", body)
        self.assertEqual(json.loads(text.split(rmeta.MARKER_START)[1].split(rmeta.MARKER_END)[0])
                         ["data_completeness"], "partial")

    async def test_volume_sort_says_it_fell_back_to_market_cap(self):
        data = await naver.get_etf_list(sort_by="quant", limit=50)
        self.assertEqual((data["sort_requested"], data["sort_by"]), ("quant", "marketSum"))
        self.assertIsNone(data["items"][0]["volume"])

        body = (await self._tool(sort_by="quant")).split(rmeta.MARKER_START)[0]
        self.assertIn("정렬: 시가총액 큰 순", body)
        self.assertIn("거래량 많은 순으로 줄 세울 수 없습니다", body)

    async def test_open_market_does_not_blame_the_clock(self):
        body = await self._tool(clock=_OPEN)
        self.assertIn("장중인데 값이 비어", body)


if __name__ == "__main__":
    unittest.main()
