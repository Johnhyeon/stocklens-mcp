"""get_etf_list(keyword=...) 회귀 테스트.

네이버 ETF API가 전체 목록을 한 번에 EUC-KR JSON으로 내려주는 걸 기존
category 필터처럼 in-memory로 한 번 더 거른다 — 추가 네트워크 호출 없이
이름 substring으로 테마 ETF를 찾을 수 있어야 한다(예: "우주" → "TIGER
미국우주테크"). 실제로 이 기능이 필요했던 계기: search()/get_etf_list의
category만으로는 이름에 낯선 표현이 섞인 신규 상장 ETF를 못 찾았던 사례.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from stock_mcp_server import naver
from stock_mcp_server._cache import clear_cache

_ITEMS = [
    {"itemcode": "0183J0", "itemname": "TIGER 미국우주테크", "etfTabCode": 4,
     "nowVal": 7730, "changeRate": 4.67, "nav": 7489, "threeMonthEarnRate": -23.2,
     "quant": 1000, "marketSum": 11072},
    {"itemcode": "463250", "itemname": "TIGER K방산&우주", "etfTabCode": 2,
     "nowVal": 36215, "changeRate": 1.02, "nav": 36272, "threeMonthEarnRate": -31.4,
     "quant": 2000, "marketSum": 5414},
    {"itemcode": "360750", "itemname": "TIGER 미국S&P500", "etfTabCode": 4,
     "nowVal": 27195, "changeRate": -0.77, "nav": 27159, "threeMonthEarnRate": 4.3,
     "quant": 3000, "marketSum": 203473},
]


def _resp(items: list[dict]) -> Mock:
    payload = {"result": {"etfItemList": items}}
    resp = Mock()
    resp.content = json.dumps(payload, ensure_ascii=False).encode("euc-kr")
    return resp


class GetEtfListKeywordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_cache()

    def tearDown(self) -> None:
        clear_cache()

    async def test_keyword_matches_name_substring_case_insensitive(self) -> None:
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(_ITEMS))):
            data = await naver.get_etf_list(keyword="우주")

        names = {i["name"] for i in data["items"]}
        self.assertEqual(names, {"TIGER 미국우주테크", "TIGER K방산&우주"})
        self.assertEqual(data["total"], 2)

    async def test_keyword_and_category_combine_as_and(self) -> None:
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(_ITEMS))):
            data = await naver.get_etf_list(category="해외 주식", keyword="우주")

        names = [i["name"] for i in data["items"]]
        self.assertEqual(names, ["TIGER 미국우주테크"])  # K방산&우주는 국내 업종/테마라 제외

    async def test_no_match_returns_empty_not_error(self) -> None:
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(_ITEMS))):
            data = await naver.get_etf_list(keyword="존재하지않는테마")

        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)

    async def test_blank_keyword_behaves_like_no_filter(self) -> None:
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(_ITEMS))):
            data = await naver.get_etf_list(keyword="   ")

        self.assertEqual(data["total"], len(_ITEMS))


if __name__ == "__main__":
    unittest.main()
