"""가격·차트·스크리닝 핵심 도구의 결과 메타데이터(RESULT_META_JSON) 표준화 테스트.

get_price/get_chart/screen_by_flow 응답 끝에 붙는
RESULT_META_JSON_START...END 블록이 항상 파싱 가능하고, market 휴장 여부·
부분/완전/무데이터 상태를 정직하게 반영하는지 확인한다. 네트워크 없이
naver/market_clock 호출만 mock으로 대체한다.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from stock_mcp_server import server


def _extract_meta(text: str) -> dict:
    payload = text.split("RESULT_META_JSON_START", 1)[1].split("RESULT_META_JSON_END", 1)[0].strip()
    return json.loads(payload)


_OPEN_CLOCK = {"krx": {"is_open": True, "last_trading_day": "2026-08-06"}}
_CLOSED_CLOCK = {"krx": {"is_open": False, "last_trading_day": "2026-08-05"}}


class GetPriceMetaTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_data_during_open_market(self) -> None:
        price = {
            "name": "삼성전자",
            "price": 246000,
            "change": 1000,
            "open": 245000,
            "high": 247000,
            "low": 244000,
            "volume": 12345,
        }
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_price(code="005930")

        meta = _extract_meta(text)
        self.assertEqual(meta["market"], "KR")
        self.assertFalse(meta["is_delayed"])
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertEqual(meta["warnings"], [])
        self.assertIn("as_of", meta)

    async def test_partial_when_optional_fields_missing(self) -> None:
        price = {"name": "삼성전자", "price": 246000}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_price(code="005930")

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_closed_market_adds_warning_without_marking_delayed(self) -> None:
        price = {
            "name": "삼성전자", "price": 246000, "change": 0,
            "open": 246000, "high": 246000, "low": 246000, "volume": 0,
        }
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), patch.object(
            server, "build_market_clock", return_value=_CLOSED_CLOCK
        ):
            text = await server.get_price(code="005930")

        meta = _extract_meta(text)
        self.assertFalse(meta["is_delayed"])  # 지연이 아니라 '휴장 중 최근 종가'라는 별개 사실
        self.assertTrue(any("2026-08-05" in w for w in meta["warnings"]))

    async def test_no_data_reports_none_completeness(self) -> None:
        with patch.object(server, "get_current_price", AsyncMock(return_value=None)), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_price(code="999999")

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertTrue(meta["warnings"])


class GetChartMetaTests(unittest.IsolatedAsyncioTestCase):
    def _bar(self, date: str) -> dict:
        return {"date": date, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}

    async def test_complete_when_full_count_returned(self) -> None:
        bars = [self._bar(f"2026080{i}") for i in range(1, 6)]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_chart(code="005930", count=5)

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_partial_when_fewer_bars_than_requested(self) -> None:
        bars = [self._bar("20260805")]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_chart(code="005930", count=120)

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(any("120" in w for w in meta["warnings"]))

    async def test_no_data_reports_none_completeness(self) -> None:
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=[])), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.get_chart(code="999999")

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")


class ScreenByFlowMetaTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_ranks_reports_none_completeness(self) -> None:
        with patch.object(server, "naver_get_volume_ranking", AsyncMock(return_value=[])), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            text = await server.screen_by_flow()

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")

    async def test_flow_fetch_failure_marks_partial(self) -> None:
        ranks = [
            {"code": "000001", "name": "종목A", "price": 1000, "change_rate": "+1%"},
            {"code": "000002", "name": "종목B", "price": 2000, "change_rate": "+2%"},
        ]

        async def fake_flow(code, days):
            if code == "000001":
                raise RuntimeError("boom")
            return [{"date": "2026-08-06", "institutional": 1, "foreign": 1, "close": 0, "volume": 0}] * days

        with patch.object(server, "naver_get_volume_ranking", AsyncMock(return_value=ranks)), patch.object(
            server, "get_investor_flow", side_effect=fake_flow
        ), patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK):
            text = await server.screen_by_flow(foreign_days=0, inst_days=0)

        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(any("1개" in w for w in meta["warnings"]))


if __name__ == "__main__":
    unittest.main()
