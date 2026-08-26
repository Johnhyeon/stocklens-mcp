"""SL-03: 미국 재무의 기준기간·혼재 상태를 구조화한다.

실측(UAT 2026-08-27): get_us_financials / get_us_financial_statement 는
data_basis=filing, data_completeness=complete 인데 data_as_of=null 이었다.
본문 표에는 기간이 있지만 메타에는 최신 기간·주기·통화·혼재 여부가 없어,
서로 다른 분기의 두 티커를 나란히 놓아도 아무 신호가 없었다.
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


_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _stmt(periods, currency="USD", ticker="CRSP", stype="balance", period="quarterly"):
    return {
        "ticker": ticker,
        "statement_type": stype,
        "period": period,
        "currency": currency,
        "rows": [
            {"item": "Total Assets", "periods": list(periods),
             "values": [100.0] * len(periods)},
            {"item": "Total Debt", "periods": list(periods),
             "values": [10.0] * len(periods)},
        ],
    }


class StatementPeriodTests(unittest.IsolatedAsyncioTestCase):
    """재무제표 - 최신 기간·주기·통화·열 수가 메타에 있어야 한다."""

    Q = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]

    async def _run(self, data, **kw):
        opts = dict(ticker="CRSP", statement_type="balance", period="quarterly")
        opts.update(kw)
        with patch.object(server.us, "get_financial_statement",
                          AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_financial_statement(**opts)

    async def test_latest_period_lands_in_meta(self):
        """수용 1: CRSP 분기 재무상태표의 최신 기간 2026-06-30 이 메타에 있다."""
        text = await self._run(_stmt(self.Q))
        meta = extract_meta(text)
        self.assertEqual(meta["data_as_of"], "2026-06-30")
        pc = meta["period_coverage"]
        self.assertEqual(pc["cycle"], "quarterly")
        self.assertEqual(pc["latest_period"], "2026-06-30")
        self.assertEqual(pc["periods_returned"], 4)
        self.assertEqual(pc["currency"], "USD")
        self.assertEqual(pc["basis"], "period_end")
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_cash_flow_uses_the_same_contract(self):
        text = await self._run(_stmt(self.Q, stype="cash_flow"),
                               statement_type="cash_flow")
        meta = extract_meta(text)
        self.assertEqual(meta["data_as_of"], "2026-06-30")
        self.assertEqual(meta["period_coverage"]["latest_period"], "2026-06-30")

    async def test_annual_cycle_is_recorded(self):
        text = await self._run(
            _stmt(["2025-12-31", "2024-12-31"], period="annual"), period="annual")
        meta = extract_meta(text)
        self.assertEqual(meta["period_coverage"]["cycle"], "annual")
        self.assertEqual(meta["data_as_of"], "2025-12-31")

    async def test_missing_periods_are_not_complete(self):
        """기간 열을 못 읽었으면 '다 봤다'가 아니라 '모른다'다."""
        data = _stmt(self.Q)
        for r in data["rows"]:
            r["periods"] = []
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertIsNone(meta["data_as_of"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "unknown")


class RatioBasisTests(unittest.IsolatedAsyncioTestCase):
    """비율 지표 - 공급자 기준일이 없으면 조용히 complete 가 되지 않는다."""

    BASE = {
        "ticker": "CRSP", "currency": "USD", "financial_currency": "USD",
        "trailing_pe": None, "forward_pe": None, "peg_ratio": None,
        "price_to_book": 5.2, "price_to_sales": None, "eps_trailing": -3.1,
        "eps_forward": None, "revenue_per_share": None, "book_value": 12.0,
        "return_on_equity": -0.3, "return_on_assets": None, "profit_margin": None,
        "operating_margin": None, "debt_to_equity": 10.0, "current_ratio": 4.0,
        "dividend_yield": None, "payout_ratio": None,
        "revenue_growth": None, "earnings_growth": None,
    }

    async def _run(self, data):
        with patch.object(server.us, "get_financial_info",
                          AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_financials(ticker="CRSP")

    async def test_known_basis_quarter_is_reported(self):
        data = dict(self.BASE, most_recent_quarter="2026-06-30",
                    last_fiscal_year_end="2025-12-31")
        text = await self._run(data)
        meta = extract_meta(text)
        self.assertEqual(meta["data_as_of"], "2026-06-30")
        self.assertEqual(meta["period_coverage"]["latest_period"], "2026-06-30")
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertIn("2026-06-30", text.split(rmeta.MARKER_START)[0])

    async def test_unknown_basis_is_partial_with_warning(self):
        """수용 3: 기준일 미확인 비율이 조용히 complete 가 되지 않는다."""
        text = await self._run(dict(self.BASE))   # 기준일 없음
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "unknown")
        self.assertTrue(any("기준" in w for w in meta["warnings"]), meta["warnings"])


class BatchPeriodConsistencyTests(unittest.TestCase):
    """수용 2: 서로 다른 최신 분기를 가진 두 티커의 비교는 mixed 다.

    미국 재무 배치 도구는 아직 없다(요구사항의 '향후 배치'). 판정 자체는
    KR 배치와 같은 헬퍼로 미리 고정해 두어, 배치가 생길 때 같은 계약을 쓴다.
    """

    def test_mixed_quarters(self):
        pc = server._period_coverage_of({"CRSP": "2026-06-30", "RIVN": "2026-03-31"})
        self.assertEqual(pc["consistency"], "mixed")
        self.assertEqual(pc["oldest"], "2026-03-31")
        self.assertEqual(pc["newest"], "2026-06-30")

    def test_uniform_quarters(self):
        pc = server._period_coverage_of({"CRSP": "2026-06-30", "NVDA": "2026-06-30"})
        self.assertEqual(pc["consistency"], "uniform")

    def test_no_periods_is_unknown(self):
        pc = server._period_coverage_of({})
        self.assertEqual(pc["consistency"], "unknown")
        self.assertIsNone(pc["oldest"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
