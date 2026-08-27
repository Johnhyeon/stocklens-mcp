"""SL-17: 금융회사 전용 자금 건전성 경로.

실측(UAT): KB금융·JPM 은 제조업형 CFO 런웨이에서 올바르게 제외됐지만, 그
다음이 없었다. 이 도구는 시장별 규제자본 경로를 제공한다 - 확보되는 값은
출처·기준일과 함께, 확보 안 되는 값은 필요한 보고서와 사유를 구조화해서.
서로 다른 규제 체계를 하나의 점수로 합치지 않는다.
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


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}

_JPM_ISSUER = {"requested_ticker": "JPM", "cik": 19617,
               "name": "JPMORGAN CHASE & CO", "primary_ticker": "JPM",
               "issuer_tickers": ["JPM"]}


def _fact(tag, value, end="2026-06-30", form="10-Q", unit="USD"):
    return {"tag": tag, "value": value, "end": end, "form": form,
            "filed": "2026-08-01", "unit": unit}


_JPM_FACTS = {
    "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest": _fact(
        "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest",
        26_152_000_000),
    "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss": _fact(
        "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss",
        1_400_000_000_000),
    "FinancingReceivableExcludingAccruedInterestAllowanceForCreditLossWriteoffAfterRecovery": _fact(
        "FinancingReceivableExcludingAccruedInterestAllowanceForCreditLossWriteoffAfterRecovery",
        1_500_000_000),
}


def _subs(sic="6021", desc="National Commercial Banks"):
    return {"name": "JPMORGAN CHASE & CO", "tickers": ["JPM"], "rows": [],
            "recent_count": 0, "has_older": True,
            "sic": sic, "sic_description": desc}


class UsSoundnessTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, facts, subs=None, ticker="JPM"):
        async def fake_concept(cik, tag, **kw):
            return facts.get(tag)

        with patch.object(server.sec, "resolve_issuer",
                          AsyncMock(return_value=_JPM_ISSUER)), \
             patch.object(server.sec, "get_concept_latest",
                          AsyncMock(side_effect=fake_concept)), \
             patch.object(server.sec, "get_submissions",
                          AsyncMock(return_value=subs or _subs())), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return json.loads(await server.get_financial_soundness(ticker))

    async def test_ratios_not_in_xbrl_are_structured_not_dropped(self):
        """요구 3: 확보 못한 지표는 필요한 보고서·사유가 구조화된다."""
        out = await self._run(_JPM_FACTS)
        self.assertEqual(out["market"], "US")
        for key in ("cet1_ratio", "lcr", "nsfr", "npl"):
            m = out["metrics"][key]
            self.assertEqual(m["status"], "not_in_xbrl", key)
            self.assertTrue(m["required_reports"], key)
        self.assertIn("CET1", " ".join(out["metrics"]["cet1_ratio"]["required_reports"])
                      + out["metrics"]["cet1_ratio"].get("how", ""))
        self.assertEqual(out["_meta"]["data_completeness"], "partial")
        self.assertFalse(out["_meta"]["coverage"]["coverage_complete"])

    async def test_asset_quality_values_carry_source_and_date(self):
        """요구 1: 확보되는 자산건전성 값은 출처·기준일과 함께 온다."""
        out = await self._run(_JPM_FACTS)
        allow = out["metrics"]["allowance_for_credit_losses"]
        self.assertEqual(allow["value"], 26_152_000_000)
        self.assertEqual(allow["end"], "2026-06-30")
        self.assertEqual(allow["form"], "10-Q")
        cov = out["metrics"]["allowance_coverage_of_loans"]
        self.assertAlmostEqual(cov["value_pct"],
                               round(26_152_000_000 / 1_400_000_000_000 * 100, 2))
        self.assertEqual(cov["basis"], "derived_same_date")

    async def test_stale_xbrl_ratio_is_not_presented_as_current(self):
        """실측(JPM): 2009·2013년에 끝난 규제비율 태그가 최신인 양 나오면 안 된다."""
        facts = dict(_JPM_FACTS)
        facts["TierOneRiskBasedCapitalToRiskWeightedAssets"] = _fact(
            "TierOneRiskBasedCapitalToRiskWeightedAssets", 0.1076,
            end="2009-12-31", form="10-K", unit="pure")
        out = await self._run(facts)
        t1 = out["metrics"]["tier1_ratio"]
        self.assertEqual(t1["status"], "stale_in_xbrl")
        self.assertEqual(t1["last_fact"]["end"], "2009-12-31")
        self.assertGreater(t1["last_fact"]["days_behind_base"], 400)
        self.assertTrue(t1["required_reports"])

    async def test_non_financial_company_is_flagged(self):
        out = await self._run(_JPM_FACTS,
                              subs=_subs(sic="3711", desc="Motor Vehicles"))
        self.assertFalse(out["is_financial_sic"])
        self.assertTrue(any("금융회사" in w for w in out["_meta"]["warnings"]))

    async def test_no_composite_score_and_no_cross_framework_compare(self):
        """요구 2·수용 2: 보편 점수 없음, 규제 체계 간 직접 비교 금지 명시."""
        out = await self._run(_JPM_FACTS)
        flat = json.dumps(out["metrics"], ensure_ascii=False).lower()
        for banned in ("\"score\"", "\"grade\"", "종합점수", "건전성점수"):
            self.assertNotIn(banned, flat)
        self.assertIn("직접 비교", out["framework"]["comparison_note"])


class KrSoundnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_kr_routes_to_official_reports(self):
        """수용 1: KB금융이 한국 규제자본 경로(공식 보고서)로 이어진다."""
        with patch.object(server, "build_market_clock", return_value=_CLOSED):
            out = json.loads(await server.get_financial_soundness("105560"))
        self.assertEqual(out["market"], "KR")
        self.assertIn("금감원", out["framework"]["name"])
        for key in ("cet1_ratio", "total_capital_ratio", "lcr", "npl"):
            m = out["metrics"][key]
            self.assertEqual(m["status"], "not_available_via_stocklens", key)
            self.assertTrue(any("사업보고서" in r or "경영공시" in r
                                for r in m["required_reports"]), key)
        self.assertIn("DartLens", json.dumps(out["route"], ensure_ascii=False))
        self.assertEqual(out["_meta"]["data_completeness"], "partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
