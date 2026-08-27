"""SL-14: 공시 반응의 사건 유형 필터.

실측(UAT): 가격제한폭 확대·공매도 과열 같은 행정성 공시가 실적·계약·자금조달과
같은 "기대 사건"으로 섞여 반응 습관 집계를 오염시켰다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
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
_EVENING = datetime(2026, 8, 26, 21, 0, tzinfo=rmeta.KST)


class EventTypeClassifierTests(unittest.TestCase):
    def test_categories(self):
        cls = server._classify_event_type
        self.assertEqual(cls("연결재무제표기준영업(잠정)실적(공정공시)"), "실적")
        self.assertEqual(cls("반기보고서 (2026.06)"), "실적")
        self.assertEqual(cls("단일판매ㆍ공급계약체결"), "계약")
        self.assertEqual(cls("유상증자결정"), "자금조달")
        self.assertEqual(cls("전환사채권발행결정"), "자금조달")
        self.assertEqual(cls("최대주주변경"), "지배구조")
        self.assertEqual(cls("(주)가격제한폭확대요건도달"), "행정")
        self.assertEqual(cls("공매도과열종목지정"), "행정")
        self.assertEqual(cls("기업설명회(IR)개최(안내공시)"), "IR")

    def test_low_confidence_is_unknown_with_title_kept(self):
        """요구 3: 확신이 낮으면 unknown 이지 아무 데나 넣지 않는다."""
        self.assertEqual(server._classify_event_type("아리송한 새 유형의 공시"),
                         "기타")


class EventReactionsFilterTests(unittest.IsolatedAsyncioTestCase):
    ITEMS = [
        {"date": "2026-08-20", "title": "단일판매ㆍ공급계약체결"},
        {"date": "2026-08-18", "title": "(주)가격제한폭확대요건도달"},
        {"date": "2026-08-13", "title": "공매도과열종목지정"},
        {"date": "2026-08-11", "title": "연결재무제표기준영업(잠정)실적(공정공시)"},
        {"date": "2026-08-05", "title": "기업설명회(IR)개최(안내공시)"},
    ]
    BARS = [{"date": f"202608{d:02d}", "open": 1000 + d, "high": 1010 + d,
             "low": 990 + d, "close": 1000 + d, "volume": 1000}
            for d in range(1, 27)]
    FLOWS = [{"date": f"2026.08.{d:02d}", "institutional": 1, "foreign": 2}
             for d in range(1, 27)]

    async def _run(self, **kw):
        opts = dict(code="034020", max_events=8, after=4)
        opts.update(kw)
        with patch.object(server, "naver_get_disclosure_list",
                          AsyncMock(return_value=list(self.ITEMS))), \
             patch.object(server, "get_ohlcv", AsyncMock(return_value=self.BARS)), \
             patch.object(server, "get_investor_flow",
                          AsyncMock(return_value=self.FLOWS)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_EVENING):
            return await server.get_event_reactions(**opts)

    async def test_every_row_shows_its_type(self):
        """수용 2: 원제목과 분류가 함께 보인다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("가격제한폭확대", body)     # 원제목 보존
        self.assertIn("[행정]", body)
        self.assertIn("[계약]", body)
        self.assertIn("[실적]", body)

    async def test_exclude_admin_events(self):
        """수용 1: 행정성 공시를 빼고 반응 습관을 볼 수 있다."""
        text = await self._run(exclude_types=["행정", "IR"])
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("가격제한폭확대", body)
        self.assertNotIn("공매도과열", body)
        self.assertNotIn("기업설명회", body)
        self.assertIn("단일판매", body)
        meta = extract_meta(text)
        self.assertEqual(meta["event_type_filter"]["excluded_types"], ["행정", "IR"])
        self.assertEqual(meta["event_type_filter"]["excluded_count"], 3)

    async def test_include_only_earnings(self):
        text = await self._run(include_types=["실적"])
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("잠정)실적", body)
        self.assertNotIn("단일판매", body)

    async def test_default_keeps_everything(self):
        """요구·하위 호환: 필터를 안 주면 기존과 같이 전부 나온다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        for t in ("단일판매", "가격제한폭확대", "잠정)실적"):
            self.assertIn(t, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
