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

    async def test_network_failure_is_not_disguised_as_no_results(self) -> None:
        """예전엔 여기서 []를 돌려줬다. 그러면 네이버에 아예 못 붙는 PC에서도 화면에는
        "종목을 찾을 수 없습니다"만 뜬다 — 2026-08-13 문의에서 고객은 종목명만 바꿔가며
        재시도했고, 기록에도 error=null 로 남아 우리도 성공한 호출로 읽었다.
        '못 찾음'과 '못 물어봄'은 사용자가 할 일이 다르므로 구분해서 올린다."""
        with patch.object(naver, "fetch", AsyncMock(side_effect=ConnectionError("boom"))):
            with self.assertRaises(ConnectionError):
                await naver.search_stock("삼성전자")

    async def test_non_json_body_is_still_treated_as_no_results(self) -> None:
        """200인데 JSON이 아니면 네이버가 형식을 바꾼 것 — 그건 조회 실패가 아니다."""
        class _Bad:
            def json(self):
                raise ValueError("not json")

        with patch.object(naver, "fetch", AsyncMock(return_value=_Bad())):
            self.assertEqual(await naver.search_stock("삼성전자"), [])


if __name__ == "__main__":
    unittest.main()
