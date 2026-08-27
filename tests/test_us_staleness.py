"""SL-09: 미국 공매도·애널리스트 하위 데이터의 최신성.

실측(UAT 2026-08-27):
- CRSP 공매도 본문은 FINRA 보고일 2026-08-14와 2~4주 지연 경고를 표시하지만
  메타는 data_as_of=null, complete 였다.
- META 애널리스트의 분포·추정치는 최신처럼 보이지만 업·다운그레이드 표본은
  2024-09-30 에 멈춰 있었고, 하위 표별 기준일이 없어 서로 다른 시점이 하나의
  complete 로 합쳐졌다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


class ShortInterestStalenessTests(unittest.IsolatedAsyncioTestCase):
    """수용 1: CRSP 결과 메타가 FINRA 보고일 2026-08-14 를 보존한다."""

    BASE = {"ticker": "CRSP", "shares_short": 8_000_000,
            "short_percent_of_float": 0.11, "short_ratio": 5.2,
            "float_shares": 70_000_000,
            "shares_short_prior_month": 7_500_000,
            "prior_month_date": _epoch("2026-07-15")}

    async def _run(self, data):
        with patch.object(server.us, "get_short_interest",
                          AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_short(ticker="CRSP")

    async def test_meta_carries_the_finra_report_date(self):
        data = dict(self.BASE, date_short_interest=_epoch("2026-08-14"))
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertEqual(meta["data_as_of"], "2026-08-14")
        st = meta["staleness"]
        self.assertEqual(st["report_date"], "2026-08-14")
        self.assertEqual(st["cadence"], "FINRA bi-monthly")
        self.assertIsInstance(st["age_days"], int)
        self.assertGreaterEqual(st["age_days"], 0)
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_missing_report_date_is_not_silently_complete(self):
        data = dict(self.BASE)   # date_short_interest 없음
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertIsNone(meta["data_as_of"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertTrue(any("기준일" in w for w in meta["warnings"]), meta["warnings"])

    async def test_very_old_report_is_flagged_stale(self):
        data = dict(self.BASE, date_short_interest=_epoch("2026-05-01"))
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertTrue(meta["staleness"]["stale"])
        self.assertTrue(any("오래" in w or "stale" in w.lower()
                            for w in meta["warnings"]), meta["warnings"])


class AnalystSectionCoverageTests(unittest.IsolatedAsyncioTestCase):
    """요구 2·3: 하위 표마다 기준이 다르고, 오래된 이력이 섞이면 complete 가 아니다."""

    def _data(self, updown_latest="2026-08-20"):
        return {
            "ticker": "META", "current_price": 700.0,
            "price_targets": {"mean": 800.0, "high": 900.0, "low": 600.0},
            "recommendation_key": "buy", "recommendation_mean": 1.8,
            "analyst_count": 45,
            "recommendations_by_month": [
                {"period": "0m", "strongBuy": 20, "buy": 15, "hold": 8,
                 "sell": 1, "strongSell": 0}],
            "recent_upgrades_downgrades": [
                {"GradeDate": f"{updown_latest} 00:00:00", "Firm": "SomeBank",
                 "FromGrade": "Hold", "ToGrade": "Buy", "Action": "up"},
                {"GradeDate": "2024-05-02 00:00:00", "Firm": "OtherBank",
                 "FromGrade": "Buy", "ToGrade": "Hold", "Action": "down"},
            ],
        }

    async def _run(self, data):
        with patch.object(server.us, "get_analyst_ratings",
                          AsyncMock(return_value=data)), \
             patch.object(server.us, "get_analyst_estimates",
                          AsyncMock(return_value=None)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_analyst(ticker="META")

    async def test_sections_have_their_own_basis(self):
        text = await self._run(self._data())
        meta = extract_meta(text)
        sc = meta["section_coverage"]
        self.assertEqual(sc["price_targets"]["basis"], "snapshot")
        self.assertEqual(sc["recommendation_trend"]["basis"], "relative_months")
        self.assertEqual(sc["upgrades_downgrades"]["latest"], "2026-08-20")
        self.assertIn("age_days", sc["upgrades_downgrades"])

    async def test_stale_upgrade_history_is_partial_and_separated(self):
        """수용 2: 2024-09-30 에 멈춘 이력이 최신 추정치와 한 complete 로 합쳐지면 안 된다."""
        text = await self._run(self._data(updown_latest="2024-09-30"))
        meta = extract_meta(text)
        sc = meta["section_coverage"]["upgrades_downgrades"]
        self.assertEqual(sc["latest"], "2024-09-30")
        self.assertTrue(sc["stale"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertTrue(any("업·다운그레이드" in w or "이력" in w
                            for w in meta["warnings"]), meta["warnings"])
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("2024-09-30", body)     # 본문에도 그 표의 기준이 드러난다

    async def test_fresh_history_stays_complete(self):
        text = await self._run(self._data(updown_latest="2026-08-20"))
        meta = extract_meta(text)
        self.assertFalse(meta["section_coverage"]["upgrades_downgrades"]["stale"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_no_history_section_is_not_stale(self):
        data = self._data()
        data["recent_upgrades_downgrades"] = []
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertNotIn("upgrades_downgrades", meta["section_coverage"])
        self.assertEqual(meta["data_completeness"], "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
