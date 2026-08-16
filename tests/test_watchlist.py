"""관심종목 — 세 Lens 공용 목록의 관리·표시.

안 보이면 등록해둔 걸 잊는다. 그래서 (1) 대화에서 바로 관리되고,
(2) 조회할 때 ⭐로 눈에 띄고, (3) 목록 전체를 한 번에 볼 수 있어야 한다.
저장 형식은 TelegramLens가 먼저 쓰던 계약을 그대로 따른다 — 바꾸면 못 읽는다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _watchlist as wl
from stock_mcp_server import server


class _TmpHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("LEETKIT_HOME")
        os.environ["LEETKIT_HOME"] = self.tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("LEETKIT_HOME", None)
        else:
            os.environ["LEETKIT_HOME"] = self._prev


class StorageTests(_TmpHome):
    def test_format_matches_telegramlens_contract(self):
        """{"stocks":[{code,name}]} — 형식을 바꾸면 상대 Lens가 못 읽는다."""
        wl.add("039840", "디오")
        raw = json.loads(wl.watchlist_path().read_text(encoding="utf-8"))
        self.assertEqual(raw, {"stocks": [{"code": "039840", "name": "디오"}]})

    def test_duplicate_is_not_added_twice(self):
        wl.add("039840", "디오")
        ok, why = wl.add("039840", "디오")
        self.assertFalse(ok)
        self.assertIn("이미", why)
        self.assertEqual(len(wl.load()), 1)

    def test_cap_prevents_unbounded_growth(self):
        """'관심'종목이 200개면 관심이 아니다."""
        for i in range(wl.MAX_STOCKS):
            wl.add(f"{i:06d}", f"종목{i}")
        ok, why = wl.add("999999", "초과분")
        self.assertFalse(ok)
        self.assertIn(str(wl.MAX_STOCKS), why)

    def test_remove_by_code_or_name(self):
        wl.add("039840", "디오")
        self.assertIsNotNone(wl.remove("039840"))
        wl.add("039840", "디오")
        self.assertIsNotNone(wl.remove("디오"))
        self.assertEqual(wl.load(), [])

    def test_remove_missing_returns_none(self):
        self.assertIsNone(wl.remove("없는종목"))

    def test_corrupt_file_does_not_block_lookups(self):
        """목록이 깨졌다고 시세 조회까지 막히면 안 된다."""
        wl.watchlist_path().write_text("{ 깨진 json", encoding="utf-8")
        self.assertEqual(wl.load(), [])
        self.assertEqual(wl.codes(), set())

    def test_save_is_atomic(self):
        """데몬과 동시에 쓰다 죽어도 목록이 반쯤 깨지지 않아야 한다."""
        wl.add("039840", "디오")
        leftovers = list(Path(self.tmp).glob("*.tmp"))
        self.assertEqual(leftovers, [], f"임시 파일이 남음: {leftovers}")


class ToolTests(_TmpHome, unittest.IsolatedAsyncioTestCase):
    SEARCH = [
        {"code": "039840", "name": "디오", "market": "코스닥"},
        {"code": "210540", "name": "디와이파워", "market": "코스피"},
    ]

    async def test_exact_name_match_is_not_ambiguous(self):
        """네이버 자동완성은 부분일치도 준다. 정확히 일치하면 되묻지 않는다 —
        매번 한 번 더 물으면 쓸모가 떨어진다."""
        with patch.object(server, "naver_search_stock", AsyncMock(return_value=self.SEARCH)):
            out = await server.watchlist(action="add", query="디오")
        self.assertIn("추가했습니다", out)
        self.assertIn("039840", out)

    async def test_truly_ambiguous_query_asks_back(self):
        with patch.object(server, "naver_search_stock", AsyncMock(return_value=self.SEARCH)):
            out = await server.watchlist(action="add", query="디")
        self.assertIn("여러 개", out)
        self.assertEqual(wl.load(), [])

    async def test_unknown_query_does_not_guess_a_code(self):
        with patch.object(server, "naver_search_stock", AsyncMock(return_value=[])):
            out = await server.watchlist(action="add", query="없는회사")
        self.assertIn("찾을 수 없습니다", out)
        self.assertEqual(wl.load(), [])

    async def test_list_exposes_codes_for_batch_handoff(self):
        """codes를 그대로 배치 도구에 넘기는 게 '내 종목 어때?'의 정답 경로다."""
        wl.add("039840", "디오")
        wl.add("005930", "삼성전자")
        out = await server.watchlist(action="list")
        self.assertIn("codes: 039840, 005930", out)

    async def test_empty_list_guides_instead_of_erroring(self):
        out = await server.watchlist(action="list")
        self.assertIn("비어 있습니다", out)
        self.assertIn("관심종목에 넣어줘", out)

    async def test_bad_action_is_rejected(self):
        self.assertIn("action은", await server.watchlist(action="drop"))

    async def test_price_marks_watchlisted_stock(self):
        """조회할 때마다 보여야 잊지 않는다."""
        wl.add("039840", "디오")
        price = {"name": "디오", "price": 12290, "open": 1, "high": 1, "low": 1, "volume": 1}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), \
             patch.object(server, "build_market_clock",
                          return_value={"krx": {"is_open": False, "status": "closed_weekend",
                                                "last_trading_day": "2026-08-14"}}):
            starred = await server.get_price(code="039840")
            plain = await server.get_price(code="005930")
        self.assertTrue(starred.lstrip().startswith("⭐"))
        self.assertFalse(plain.lstrip().startswith("⭐"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
