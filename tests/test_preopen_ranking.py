"""장 시작 전 순위 — "가져올 수 없습니다"가 아니라 "장 시작 전이라 아직 없습니다".

2026-09-17 08:33~08:43 실측: 네이버 시장 목록 API 는 개장 전(marketStatus=PREOPEN)에
거래 기반 정렬(up·down·quantTop·priceTop …)을 빈 배열 `[]` 로 준다. 시가총액 목록은 오고,
행에 marketStatus=PREOPEN·tradeVolume 0 이 실린다. get_volume_ranking·get_change_ranking·
screen_by_flow 는 이때 "순위를 가져올 수 없습니다"만 돌려줘서 장애처럼 읽혔고,
장전 루틴은 원인을 알 수 없었다.

장 상태는 우리 시계로 짐작하지 않고 네이버 행의 marketStatus 로 판단한다. 그 값을 못 읽거나
PREOPEN 이 아니면 예전 안내를 그대로 쓴다. 네트워크는 쓰지 않는다.
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

_BEFORE_OPEN = {"krx": {"is_open": False, "last_trading_day": "2026-09-16"}}

# 2026-09-17 08:33 시가총액 목록 첫 행(필요한 필드만)
_PREOPEN_CAP_ROW = {
    "itemcode": "005930", "itemname": "삼성전자", "type": "ST", "tradeStopYn": "N",
    "marketStatus": "PREOPEN", "tradingSessionType": None, "tradeVolume": "0",
    "nowPrice": "253500", "prevChangeRate": "0.0", "tradeAmount": "0",
    "marketSum": "1482031600000000",
}


def _meta(text: str) -> dict:
    return json.loads(text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0])


class _FakeMarketList(unittest.IsolatedAsyncioTestCase):
    """정렬 키별 응답을 정해 두는 시장 목록 API 흉내."""

    cap_rows: object = [_PREOPEN_CAP_ROW]

    def setUp(self):
        self.orders: list[str] = []

        async def _fetch(url, params=None, **kwargs):
            order = (params or {}).get("orderType")
            self.orders.append(order)
            payload = self.cap_rows if order == "marketSum" else []
            return types.SimpleNamespace(status_code=200, text="", json=lambda: payload)

        self._real = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()
        self._clock = patch.object(server, "build_market_clock", return_value=_BEFORE_OPEN)
        self._clock.start()

    def tearDown(self):
        self._clock.stop()
        _naver.fetch = self._real
        clear_cache()


class PreOpenTests(_FakeMarketList):
    async def test_status_comes_from_the_market_list_row(self):
        self.assertEqual(await _naver.get_market_list_status("ALL"), "PREOPEN")

    async def test_volume_ranking_says_it_is_before_the_open(self):
        text = await server.get_volume_ranking(sort_by="trade_value", count=10)

        self.assertIn("장 시작 전이라 거래대금 순위가 아직 없습니다 (ALL)", text)
        self.assertNotIn("가져올 수 없습니다", text)
        meta = _meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertTrue(any("PREOPEN" in w for w in meta["warnings"]))

    async def test_volume_sort_names_volume(self):
        text = await server.get_volume_ranking(market="KOSDAQ")
        self.assertIn("장 시작 전이라 거래량 순위가 아직 없습니다 (KOSDAQ)", text)

    async def test_change_ranking_says_it_is_before_the_open(self):
        up = await server.get_change_ranking(direction="up")
        down = await server.get_change_ranking(direction="down", market="KOSPI")

        self.assertIn("장 시작 전이라 상승률 순위가 아직 없습니다 (ALL)", up)
        self.assertIn("장 시작 전이라 하락률 순위가 아직 없습니다 (KOSPI)", down)
        self.assertNotIn("가져올 수 없습니다", up + down)

    async def test_screen_by_flow_stops_before_fetching_flows(self):
        flow = AsyncMock()
        with patch.object(server, "get_investor_flow", flow):
            text = await server.screen_by_flow(top_n=10)

        self.assertIn("장 시작 전이라 거래대금 순위가 아직 없습니다", text)
        self.assertEqual(_meta(text)["data_completeness"], "none")
        flow.assert_not_called()


class NotPreOpenTests(_FakeMarketList):
    """PREOPEN 이 아닌데 순위가 비었으면 원인을 지어내지 않고 예전 안내를 쓴다."""

    cap_rows = [dict(_PREOPEN_CAP_ROW, marketStatus="OPEN")]

    async def test_empty_ranking_keeps_the_failure_message(self):
        text = await server.get_volume_ranking(sort_by="trade_value")
        self.assertIn("거래대금 순위를 가져올 수 없습니다", text)
        self.assertNotIn("장 시작 전", text)

    async def test_change_ranking_keeps_the_failure_message(self):
        text = await server.get_change_ranking(direction="up")
        self.assertIn("등락률 순위를 가져올 수 없습니다", text)


class StatusUnreadableTests(_FakeMarketList):
    cap_rows = {"unexpected": "shape"}  # 형식이 바뀌어 장 상태를 못 읽는 경우

    async def test_unreadable_status_falls_back_to_the_failure_message(self):
        text = await server.get_volume_ranking()
        self.assertIn("거래량 순위를 가져올 수 없습니다", text)
        self.assertNotIn("장 시작 전", text)


if __name__ == "__main__":
    unittest.main()
