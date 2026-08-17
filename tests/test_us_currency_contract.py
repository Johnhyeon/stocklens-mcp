"""US 통화·결측 계약 — '$'가 아닌 통화를 '$'로 찍지 않는다.

ADR은 주가 통화와 재무제표 통화가 갈린다(TSM: 거래 USD / 재무 TWD).
yfinance는 marketCap을 주가에서, enterpriseValue·매출추정을 재무제표에서 뽑아
두 통화를 섞어 준다. 여기에 '$'를 일괄로 붙이면 TWD 금액이 달러로 읽힌다
(2026-08-17 실측: TSM 분기 매출 추정이 `$1,454.96B`, 약 31배).
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server


class CurrencyPrefixTests(unittest.TestCase):
    def test_usd_uses_dollar_sign(self):
        self.assertEqual(server._ccy_prefix("USD"), "$")
        self.assertEqual(server._ccy_prefix("usd"), "$")

    def test_unknown_currency_defaults_to_dollar(self):
        # 통화를 모를 때까지 새 표기를 만들지는 않는다(기존 동작 유지).
        self.assertEqual(server._ccy_prefix(None), "$")
        self.assertEqual(server._ccy_prefix(""), "$")

    def test_non_usd_uses_code_not_dollar(self):
        self.assertEqual(server._ccy_prefix("TWD"), "TWD ")
        self.assertEqual(server._ccy_prefix("jpy"), "JPY ")

    def test_fmt_num_applies_currency(self):
        self.assertEqual(server._fmt_num(1_454_960_072_270, currency="TWD "), "TWD 1,454.96B")
        self.assertEqual(server._fmt_num(1_454_960_072_270), "$1,454.96B")

    def test_fmt_num_keeps_small_numbers_unitless(self):
        # 비율(P/E 등)에는 통화 기호가 붙으면 안 된다.
        self.assertEqual(server._fmt_num(25.5), "25.50")


class AnalystEstimateCurrencyTests(unittest.IsolatedAsyncioTestCase):
    ESTIMATES = {
        "ticker": "TSM",
        "currency": "USD",
        "financial_currency": "TWD",
        "earnings_estimate": [],
        "revenue_estimate": [
            {"period": "0q", "avg": 1_454_960_072_270, "low": 1_423_000_000_000,
             "high": 1_476_430_000_000, "growth": 0.4698},
        ],
        "eps_revisions": [],
    }

    # get_us_analyst 는 ratings 가 비면 조기 반환한다 — 최소한의 커버리지를 채운다.
    RATINGS = {"ticker": "TSM", "recommendation_key": "buy", "analyst_count": 30}

    async def _render(self, estimates):
        with patch.object(server.us, "get_analyst_estimates", AsyncMock(return_value=estimates)), \
             patch.object(server.us, "get_analyst_ratings", AsyncMock(return_value=self.RATINGS)):
            return await server.get_us_analyst("TSM")

    async def test_twd_estimate_is_not_labelled_dollar(self):
        out = await self._render(self.ESTIMATES)
        self.assertIn("TWD 1,454.96B", out)
        self.assertNotIn("$1,454.96B", out)

    async def test_mixed_currency_is_flagged(self):
        out = await self._render(self.ESTIMATES)
        self.assertIn("통화", out)
        self.assertRegex(out, r"TWD.*USD|USD.*TWD")

    async def test_zero_is_not_shown_as_missing(self):
        """`avg or average` 는 0을 falsy로 보고 넘겨, 같은 0인데 평균만 '-'가 됐다."""
        est = dict(self.ESTIMATES)
        est["revenue_estimate"] = [
            {"period": "0q", "avg": 0, "low": 0, "high": 0, "growth": None}
        ]
        out = await self._render(est)
        row = [l for l in out.splitlines() if l.startswith("0q |")]
        self.assertTrue(row, "0q 행이 있어야 한다")
        cells = [c.strip() for c in row[0].split("|")]
        # 평균·낮음·높음이 같은 0이므로 표기도 같아야 한다.
        self.assertEqual(cells[1], cells[2], f"평균과 낮음이 다르게 표기됨: {row[0]}")
        self.assertEqual(cells[2], cells[3], f"낮음과 높음이 다르게 표기됨: {row[0]}")


class CompanyInfoCurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def _render(self, info):
        with patch.object(server.us, "get_info", AsyncMock(return_value=info)):
            return await server.get_us_info("TSM")

    async def test_market_cap_and_ev_use_their_own_currency(self):
        out = await self._render({
            "ticker": "TSM", "name": "TSMC",
            "market_cap": 2_252_978_257_920, "enterprise_value": 15_282_526_486_528,
            "currency": "USD", "financial_currency": "TWD",
        })
        self.assertIn("시가총액: $2,252.98B", out)
        self.assertIn("기업가치(EV): TWD 15,282.53B", out)
        self.assertIn("통화 불일치", out)

    async def test_same_currency_has_no_warning(self):
        out = await self._render({
            "ticker": "AAPL", "name": "Apple",
            "market_cap": 4_435_609_124_864, "enterprise_value": 4_486_742_409_216,
            "currency": "USD", "financial_currency": "USD",
        })
        self.assertIn("시가총액: $4,435.61B", out)
        self.assertNotIn("통화 불일치", out)


if __name__ == "__main__":
    unittest.main()
