"""종목명/코드 검색(search_stock) 회귀 테스트.

네이버 모바일 증권 autoComplete API 응답을 목(mock)으로 고정해, 실제 네트워크
없이도 코드 필터링(6자리 영숫자만), market 필드 매핑, 상위 5건 캡, 실패 시
빈 리스트 반환 규칙이 깨지지 않는지 검증한다.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from stock_mcp_server import naver
from stock_mcp_server._cache import clear_cache


def _resp(data: dict) -> Mock:
    resp = Mock()
    resp.json = Mock(return_value=data)
    return resp


class SearchStockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_cache()

    def tearDown(self) -> None:
        clear_cache()

    async def test_returns_matching_stocks_with_market_label(self) -> None:
        data = {
            "isSuccess": True,
            "result": {
                "items": [
                    {"code": "005930", "name": "삼성전자", "typeName": "코스피"},
                    {"code": "000660", "name": "SK하이닉스", "typeName": "코스피"},
                ]
            },
        }
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(data))):
            results = await naver.search_stock("삼성")

        self.assertEqual(
            results,
            [
                {"code": "005930", "name": "삼성전자", "market": "코스피"},
                {"code": "000660", "name": "SK하이닉스", "market": "코스피"},
            ],
        )

    async def test_filters_out_non_six_char_codes(self) -> None:
        # 지수/파생 등 종목코드 형식이 아닌 항목은 제외돼야 한다.
        data = {
            "isSuccess": True,
            "result": {
                "items": [
                    {"code": "005930", "name": "삼성전자", "typeName": "코스피"},
                    {"code": "KOSPI", "name": "코스피지수", "typeName": "지수"},
                    {"code": "12345", "name": "다섯자리", "typeName": "코스닥"},
                ]
            },
        }
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(data))):
            results = await naver.search_stock("삼성")

        self.assertEqual([r["code"] for r in results], ["005930"])

    async def test_caps_results_at_five(self) -> None:
        items = [
            {"code": f"{i:06d}", "name": f"종목{i}", "typeName": "코스피"}
            for i in range(10)
        ]
        data = {"isSuccess": True, "result": {"items": items}}
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(data))):
            results = await naver.search_stock("종목")

        self.assertEqual(len(results), 5)

    async def test_unsuccessful_response_returns_empty_list(self) -> None:
        data = {"isSuccess": False, "result": {}}
        with patch.object(naver, "fetch", AsyncMock(return_value=_resp(data))):
            results = await naver.search_stock("없는종목")

        self.assertEqual(results, [])

    async def test_network_failure_returns_empty_list(self) -> None:
        with patch.object(naver, "fetch", AsyncMock(side_effect=ConnectionError("boom"))):
            results = await naver.search_stock("삼성전자")

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
