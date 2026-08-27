"""SL-07: 수급 배치의 60거래일 심층 모드.

실측(UAT): 15종목의 60일 수급 판정에 배치 상한(20일)이 걸려 결국 종목마다
단건 get_flow 를 다시 불러야 했다. 심층 모드는 일별 원문 대신 5/20/60일
누적·순매수 일수로 압축해 60일을 배치 한 번에 돌려준다.
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
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _rows(n: int) -> list[dict]:
    """최신 -> 과거 순(네이버 페이지 순서). i=0 이 가장 최근 거래일.

    기관 = +100 고정(전 일수 순매수), 외국인 = i<3 이면 -50, 나머지 +10
    (최근 3일만 순매도) - 창별 누적·양수 일수를 손으로 검산할 수 있는 모양.
    """
    out = []
    for i in range(n):
        m, d = divmod(i, 28)
        out.append({
            "date": f"2026.{8 - m:02d}.{28 - d:02d}",
            "institutional": 100,
            "foreign": -50 if i < 3 else 10,
        })
    return out


class FlowBatchSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, codes, flow_map, **kw):
        async def fake_flow(code, days):
            data = flow_map[code]
            return data[:days]

        with patch.object(server, "get_investor_flow", AsyncMock(side_effect=fake_flow)), \
             patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_flow_batch(codes, **kw)

    async def test_summary_mode_reaches_sixty_days(self):
        """수용 1: 60일 판정을 단건 호출 없이 배치 한 번으로."""
        text = await self._run(["005930"], {"005930": _rows(60)},
                               days=60, summary=True)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "day", "value": 60})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 60})
        self.assertTrue(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_summary_matches_hand_computed_windows(self):
        """수용 2: 5/20/60 누적이 원본 일별 데이터와 일치한다."""
        text = await self._run(["005930"], {"005930": _rows(60)},
                               days=60, summary=True)
        body = text.split(rmeta.MARKER_START)[0]
        # 기관: 전 창 +100*n, 양수 일수 = n
        self.assertIn("+500", body)      # 5일 누적
        self.assertIn("+2,000", body)    # 20일 누적
        self.assertIn("+6,000", body)    # 60일 누적
        # 외국인: 5일 = -150+20 = -130 / 20일 = -150+170 = +20 / 60일 = -150+570 = +420
        self.assertIn("-130", body)
        self.assertIn("+20", body)
        self.assertIn("+420", body)
        # 순매수(양수) 일수: 외국인 5일 중 2일, 20일 중 17일, 60일 중 57일
        self.assertIn("2/5", body)
        self.assertIn("17/20", body)
        self.assertIn("57/60", body)

    async def test_summary_omits_daily_rows(self):
        """요구 2: 심층 모드는 일별 원문을 싣지 않는다(크기 제한 회피가 목적)."""
        text = await self._run(["005930"], {"005930": _rows(60)},
                               days=60, summary=True)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("2026.07.15", body)   # 중간 날짜의 일별 행이 없어야 한다

    async def test_default_daily_mode_unchanged(self):
        """요구 3: summary 를 안 주면 기존 20일 상한·일별 행 그대로."""
        text = await self._run(["005930"], {"005930": _rows(60)}, days=60)
        body = text.split(rmeta.MARKER_START)[0]
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 20})
        self.assertIn("2026.08.28", body)      # 일별 행 유지
        self.assertIn("상한은 20일", body)

    async def test_short_history_is_labeled_not_padded(self):
        """상장 얼마 안 된 종목: 60일 창을 30일치로 계산했으면 그렇게 말한다."""
        text = await self._run(["005930", "999999"],
                               {"005930": _rows(60), "999999": _rows(30)},
                               days=60, summary=True)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("30일치", body)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["short_entities"], ["999999"])
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_fifteen_codes_batch(self):
        """수용 1 그대로: 15종목 60일이 한 번에 나온다."""
        codes = [f"{i:06d}" for i in range(1, 16)]
        text = await self._run(codes, {c: _rows(60) for c in codes},
                               days=60, summary=True)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["returned_entities"], 15)
        self.assertTrue(meta["coverage"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
