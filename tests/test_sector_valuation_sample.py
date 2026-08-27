"""SL-11: 업종 가치 중앙값의 표본 편향 제거.

실측(UAT): 반도체 172개·기계 104개 같은 큰 업종에서 등락률 상위 30~40개만으로
중앙값을 만들면 그날 오른 종목 쪽으로 치우친 값이 "업종 중앙값"으로 나갔다.
집계는 업종 전체로, top_n 은 표시 전용으로 분리한다.
"""

from __future__ import annotations

import json
import re
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


def _universe(n):
    """등락률 순 목록 흉내 - 앞쪽일수록 그날 많이 오른 종목."""
    return {"sector_name": "반도체", "sector_id": "202",
            "stocks": [{"code": f"{i:06d}", "name": f"종목{i}"} for i in range(1, n + 1)]}


def _snap_row(code, per):
    return {"code": code, "name": f"종목{int(code)}", "per": per, "pbr": 1.5,
            "roe": 10.0, "시가총액": f"{int(code) * 10}억원", "fin_period": "2026.06"}


class FullUniverseAggregationTests(unittest.IsolatedAsyncioTestCase):
    """수용 1: 표시 개수를 바꿔도 업종 중앙값은 변하지 않는다."""

    def _patches(self, universe_n=120, loss_codes=(), missing_codes=()):
        uni = _universe(universe_n)
        captured = {}

        async def scan(codes, days=60, include_financial=True):
            captured["codes"] = list(codes)
            out = []
            for c in codes:
                if c in missing_codes:
                    out.append({"code": c, "name": f"종목{int(c)}", "per": None,
                                "pbr": None, "roe": None})
                elif c in loss_codes:
                    out.append(_snap_row(c, -5.0))
                else:
                    # 등락률 상위(앞 번호)일수록 PER 이 높게 - 편향이 보이도록
                    out.append(_snap_row(c, 100.0 - int(c) * 0.5))
            return out

        return uni, scan, captured

    async def _run(self, uni, scan, **kw):
        opts = dict(sector_name="반도체")
        opts.update(kw)
        with patch.object(server, "naver_get_sector_stocks",
                          AsyncMock(return_value=uni)), \
             patch.object(server, "naver_scan_snapshot", AsyncMock(side_effect=scan)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_sector_valuation(**opts)

    @staticmethod
    def _median_from(text):
        m = re.search(r"PER\(배\) \| ([\d,.]+)", text)
        return m.group(1) if m else None

    async def test_aggregation_covers_the_whole_sector(self):
        uni, scan, captured = self._patches(universe_n=120)
        text = await self._run(uni, scan, top_n=30)
        self.assertEqual(len(captured["codes"]), 120)   # 30이 아니라 전체
        meta = extract_meta(text)
        vs = meta["valuation_sample"]
        self.assertEqual(vs["universe"], 120)
        self.assertEqual(vs["aggregated"], 120)
        self.assertEqual(vs["scope"], "full")

    async def test_display_count_does_not_move_the_median(self):
        uni, scan, _ = self._patches(universe_n=120)
        t30 = await self._run(uni, scan, top_n=30)
        t10 = await self._run(uni, scan, top_n=10)
        self.assertIsNotNone(self._median_from(t30))
        self.assertEqual(self._median_from(t30), self._median_from(t10))
        # 표시 행 수만 달라진다
        rows30 = len(re.findall(r"^종목\d+\(", t30, re.M))
        rows10 = len(re.findall(r"^종목\d+\(", t10, re.M))
        self.assertEqual(rows30, 30)
        self.assertEqual(rows10, 10)

    async def test_exclusions_and_denominator_are_in_meta(self):
        """수용 2: 적자·결측 제외 수와 실제 분모가 메타에 남는다."""
        loss = {"000001", "000002", "000003"}
        missing = {"000004", "000005"}
        uni, scan, _ = self._patches(universe_n=50, loss_codes=loss,
                                     missing_codes=missing)
        text = await self._run(uni, scan)
        vs = extract_meta(text)["valuation_sample"]
        self.assertEqual(vs["valid"]["per"], 45)
        self.assertEqual(vs["excluded"]["loss_per"], 3)
        self.assertEqual(vs["excluded"]["missing_per"], 2)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("45", body)     # 표본 열에 실제 분모

    async def test_oversized_universe_is_called_sample_median(self):
        """요구 3: 전량 집계가 불가능하면 이름을 낮춘다 - 전체 중앙값이라 부르지 않는다."""
        uni, scan, captured = self._patches(universe_n=120)
        with patch.object(server, "_SECTOR_AGG_CAP", 40):
            text = await self._run(uni, scan, top_n=10)
        self.assertEqual(len(captured["codes"]), 40)
        meta = extract_meta(text)
        vs = meta["valuation_sample"]
        self.assertEqual(vs["scope"], "sample")
        self.assertEqual(vs["aggregated"], 40)
        self.assertEqual(vs["universe"], 120)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("표본 중앙값", body)
        self.assertIn("sample_median", body)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(meta["coverage"]["reason"], "server_cap")
        self.assertFalse(meta["coverage"]["coverage_complete"])

    async def test_full_scope_is_called_median(self):
        uni, scan, _ = self._patches(universe_n=30)
        text = await self._run(uni, scan)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("sample_median", body)
        self.assertIn("업종 전체", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
