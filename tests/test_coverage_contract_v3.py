"""메타 규약 v3 회귀 - 요청한 범위와 실제로 돌려준 범위가 갈라져 보이는가.

이 파일이 막는 것은 하나다: **도구가 조용히 잘라놓고 아무 말도 안 하는 것.**

get_flow_batch(days=60)은 서버 상한 20일에 걸려 20일치만 돌려준다. 그런데
지금까지 응답에는 "최근 20일"이라고만 적혀 있었다. 60일을 물어본 사람은 그게
자기가 요청한 범위인 줄 알고 20일치를 60일 흐름으로 읽는다. 요청값과 적용값을
나란히 싣고 data_completeness=partial 로 내려야 그 오독이 구조적으로 막힌다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
        return json.loads(payload)
    return json.loads(text)["_meta"]


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
}


def _flow_rows(n: int) -> list[dict]:
    return [
        {"date": f"2026.08.{day:02d}", "institutional": 1, "foreign": 2}
        for day in range(1, n + 1)
    ]


class FlowBatchCoverageTests(unittest.IsolatedAsyncioTestCase):
    """SL-01. 수급 배치의 days 상한(20)이 사용자에게 보이는가."""

    async def _run(self, codes, days, flow=None):
        flow = flow if flow is not None else AsyncMock(return_value=_flow_rows(20))
        with patch.object(server, "get_investor_flow", flow), \
             patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_flow_batch(codes, days=days)

    async def test_sixty_day_request_exposes_twenty_day_cap(self):
        text = await self._run(["005930"], 60)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "day", "value": 60})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 20})
        self.assertTrue(meta["coverage"]["truncated"])
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "server_cap")
        self.assertEqual(meta["data_completeness"], "partial")
        # 본문만 읽는 사용자에게도 보여야 한다. 메타는 AI만 본다.
        self.assertIn("60일", text)
        self.assertIn("20일", text)

    async def test_twenty_day_request_is_not_truncated(self):
        text = await self._run(["005930"], 20)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "day", "value": 20})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 20})
        self.assertFalse(meta["coverage"]["truncated"])
        self.assertTrue(meta["coverage"]["coverage_complete"])
        self.assertIsNone(meta["coverage"]["reason"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_default_five_days_stays_five(self):
        """기본값은 건드리지 않는다. 5일 요청은 5일 그대로가 정답이다."""
        text = await self._run(["005930"], 5, AsyncMock(return_value=_flow_rows(5)))
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["effective"]["value"], 5)
        self.assertFalse(meta["coverage"]["truncated"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_returned_count_is_per_entity(self):
        """상한 안이어도 종목마다 실제로 몇 행 왔는지는 다르다(신규 상장 등)."""
        async def uneven(code, days):
            return _flow_rows(20 if code == "005930" else 3)

        text = await self._run(["005930", "000660"], 20, AsyncMock(side_effect=uneven))
        meta = extract_meta(text)
        self.assertEqual(
            meta["coverage"]["per_entity_returned_count"],
            {"005930": 20, "000660": 3},
        )
        self.assertEqual(meta["coverage"]["returned_count"], 23)

    async def test_partial_failure_is_partial(self):
        """일부 종목이 실패했으면 전체를 complete 라고 말할 수 없다."""
        async def half_broken(code, days):
            if code == "000660":
                raise RuntimeError("네이버 응답 없음")
            return _flow_rows(20)

        text = await self._run(["005930", "000660"], 20, AsyncMock(side_effect=half_broken))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["per_entity_returned_count"]["000660"], 0)

    async def test_truncated_and_failed_together_still_partial(self):
        async def half_broken(code, days):
            if code == "000660":
                raise RuntimeError("네이버 응답 없음")
            return _flow_rows(20)

        text = await self._run(["005930", "000660"], 60, AsyncMock(side_effect=half_broken))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(meta["coverage"]["truncated"])
        self.assertEqual(meta["coverage"]["reason"], "server_cap")

    async def test_body_table_shape_is_unchanged(self):
        """기존 본문 형식은 유지한다. 경고 한 줄만 늘어난다."""
        text = await self._run(["005930"], 5, AsyncMock(return_value=_flow_rows(5)))
        self.assertIn("각 행: 날짜 | 기관 순매매(주) | 외국인 순매매(주)", text)
        self.assertIn("※ 양수=순매수", text)
        self.assertIn("합계(5일)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
