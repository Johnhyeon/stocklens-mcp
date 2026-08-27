"""SL-16: 미국 사용 가능 유동성 원장.

실측(UAT): RIVN 23.4개월·CRSP 9.5개월 런웨이는 현금만 민감도까지만 가능했다.
단기투자·제한 현금·확약 한도·부채 만기·보고기간 후 조달이 한 원장에 없어서
"공식 런웨이"로 승격할 수 없었다. 이 도구는 그 재료를 출처·기준일별로 구조화
한다 - 없는 항목은 0이 아니라 확인 불가로 남긴다.
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

_ISSUER = {"requested_ticker": "RIVN", "cik": 1874178, "name": "Rivian Automotive, Inc.",
           "primary_ticker": "RIVN", "issuer_tickers": ["RIVN"]}


def _fact(tag, value, end="2026-06-30", form="10-Q", filed="2026-08-05"):
    return {"tag": tag, "value": value, "end": end, "form": form,
            "filed": filed, "unit": "USD"}


# 태그 -> fact. 후보 태그 중 사전에 있는 첫 태그가 값이 된다.
_FULL = {
    "CashAndCashEquivalentsAtCarryingValue": _fact(
        "CashAndCashEquivalentsAtCarryingValue", 5_980_000_000),
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": _fact(
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", 6_010_000_000),
    "ShortTermInvestments": _fact("ShortTermInvestments", 1_500_000_000),
    "RestrictedCashCurrent": _fact("RestrictedCashCurrent", 10_000_000),
    "RestrictedCashNoncurrent": _fact("RestrictedCashNoncurrent", 20_000_000),
    "LineOfCreditFacilityRemainingBorrowingCapacity": _fact(
        "LineOfCreditFacilityRemainingBorrowingCapacity", 1_100_000_000),
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": _fact(
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths", 250_000_000),
    "LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths": _fact(
        "LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths", 60_000_000),
    "PurchaseObligationDueInNextTwelveMonths": _fact(
        "PurchaseObligationDueInNextTwelveMonths", 300_000_000),
}

_SUBS = {
    "name": "Rivian Automotive, Inc.", "tickers": ["RIVN"], "has_older": True,
    "recent_count": 4,
    "rows": [
        {"form": "424B5", "filing_date": "2026-08-10", "report_date": None,
         "accession": "0001-26-000001", "primary_document": "prosp.htm",
         "primary_doc_description": "Prospectus Supplement", "items": None},
        {"form": "8-K", "filing_date": "2026-08-07", "report_date": "2026-08-06",
         "accession": "0001-26-000002", "primary_document": "e8k.htm",
         "primary_doc_description": "Current report", "items": "2.03,9.01"},
        {"form": "8-K", "filing_date": "2026-08-03", "report_date": "2026-08-01",
         "accession": "0001-26-000003", "primary_document": "e8k2.htm",
         "primary_doc_description": "Officer change", "items": "5.02"},
        {"form": "424B5", "filing_date": "2026-05-01", "report_date": None,
         "accession": "0001-26-000004", "primary_document": "old.htm",
         "primary_doc_description": "Old prospectus", "items": None},
    ],
}


class UsLiquidityTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, facts, subs=_SUBS, ticker="RIVN"):
        async def fake_concept(cik, tag, **kw):
            return facts.get(tag)

        with patch.object(server.sec, "resolve_issuer", AsyncMock(return_value=_ISSUER)), \
             patch.object(server.sec, "get_concept_latest",
                          AsyncMock(side_effect=fake_concept)), \
             patch.object(server.sec, "get_submissions", AsyncMock(return_value=subs)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return json.loads(await server.get_us_liquidity(ticker))

    async def test_full_ledger_totals_without_double_count(self):
        """요구 1·3: 출처·기준일별 구조화 + 제한 현금은 총액에 안 들어간다."""
        out = await self._run(_FULL)
        liq = out["liquidity"]
        self.assertEqual(liq["cash"]["value"], 5_980_000_000)
        self.assertEqual(liq["cash"]["end"], "2026-06-30")
        self.assertEqual(liq["cash"]["form"], "10-Q")
        total = liq["total_available"]
        # cash + 단기투자 + 가용 리볼버. 제한 현금 30M 은 제외.
        self.assertEqual(total["value"], 5_980_000_000 + 1_500_000_000 + 1_100_000_000)
        self.assertEqual(sorted(total["components"]),
                         ["cash", "marketable_securities", "revolver_available"])
        self.assertNotIn("restricted", " ".join(total["components"]))
        self.assertFalse(liq["restricted_cash"]["included_in_total"])
        # 교차검증: 포함형 총계 - 현금 = 파생 제한액
        self.assertEqual(liq["restricted_cash"]["derived_from_inclusive_total"],
                         30_000_000)
        meta = out["_meta"]
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertEqual(out["runway_inputs"]["official_runway"]["status"],
                         "inputs_available")

    async def test_missing_restricted_and_maturity_blocks_official(self):
        """수용 2: 제한·만기 자료가 없으면 공식 런웨이는 확인 불가로 남는다."""
        facts = {k: v for k, v in _FULL.items()
                 if "Restricted" not in k and "Maturities" not in k
                 and "LineOfCredit" not in k}
        out = await self._run(facts)
        official = out["runway_inputs"]["official_runway"]
        self.assertEqual(official["status"], "unavailable")
        self.assertIn("restricted_cash", official["missing"])
        self.assertIn("debt_due_next_12m", official["missing"])
        # 없는 항목이 0으로 지어내지지 않는다
        self.assertEqual(out["liquidity"]["restricted_cash"]["current"],
                         {"status": "not_reported"})
        self.assertEqual(out["obligations"]["debt_due_next_12m"],
                         {"status": "not_reported"})
        # 현금만 민감도 기반은 여전히 가능
        self.assertEqual(out["runway_inputs"]["cash_only_base"], 5_980_000_000)
        self.assertEqual(out["_meta"]["data_completeness"], "partial")
        self.assertFalse(out["_meta"]["coverage"]["coverage_complete"])

    async def test_as_of_mismatch_is_excluded_from_total(self):
        """기준일이 다른 값은 총액에 섞지 않는다 - 기간 혼합은 원장을 오염시킨다."""
        facts = dict(_FULL)
        facts["LineOfCreditFacilityRemainingBorrowingCapacity"] = _fact(
            "LineOfCreditFacilityRemainingBorrowingCapacity", 1_100_000_000,
            end="2026-03-31", form="10-Q", filed="2026-05-05")
        out = await self._run(facts)
        total = out["liquidity"]["total_available"]
        self.assertEqual(total["value"], 5_980_000_000 + 1_500_000_000)
        self.assertNotIn("revolver_available", total["components"])
        excluded = {e["component"]: e for e in total["excluded"]}
        self.assertEqual(excluded["revolver_available"]["reason"], "as_of_mismatch")
        self.assertEqual(excluded["revolver_available"]["end"], "2026-03-31")
        self.assertTrue(any("2026-03-31" in w for w in out["_meta"]["warnings"]))

    async def test_post_period_financing_filter(self):
        """요구 2: 기준일 이후의 자금조달성 공시만 연결한다."""
        out = await self._run(_FULL)
        rows = out["post_period_financing"]["rows"]
        forms = [(r["form"], r["filing_date"]) for r in rows]
        self.assertIn(("424B5", "2026-08-10"), forms)      # 기준일 후 증권발행
        self.assertIn(("8-K", "2026-08-07"), forms)        # item 2.03 = 채무 발생
        self.assertNotIn(("8-K", "2026-08-03"), forms)     # 5.02 임원변경은 아님
        self.assertNotIn(("424B5", "2026-05-01"), forms)   # 기준일 이전
        self.assertEqual(out["post_period_financing"]["cutoff"], "2026-06-30")

    async def test_stale_obligation_cannot_back_official_runway(self):
        """실측(RIVN): 2년 전 LongTermDebtCurrent=0 이 최신 만기 자료로 둔갑했다.

        기준일보다 400일 넘게 뒤처진 값은 원장에 남기되(days_behind_base 표기)
        공식 런웨이 재료로는 인정하지 않는다. 연 1회 주석(직전 10-K)은 통과한다.
        """
        facts = dict(_FULL)
        facts["LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths"] = _fact(
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths", 0,
            end="2024-06-30", form="10-Q", filed="2024-08-06")
        # 리스는 직전 10-K(반년 전) - 연차 주석 주기라 허용
        facts["LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths"] = _fact(
            "LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths", 60_000_000,
            end="2025-12-31", form="10-K", filed="2026-02-12")
        out = await self._run(facts)
        debt = out["obligations"]["debt_due_next_12m"]
        self.assertEqual(debt["days_behind_base"], 730)
        official = out["runway_inputs"]["official_runway"]
        self.assertEqual(official["status"], "unavailable")
        self.assertIn("debt_due_next_12m", official["missing"])
        self.assertNotIn("lease_due_next_12m", official["missing"])
        self.assertEqual(out["obligations"]["lease_due_next_12m"]["days_behind_base"], 181)
        self.assertTrue(any("debt_due_next_12m" in w for w in out["_meta"]["warnings"]))
        self.assertEqual(out["_meta"]["data_completeness"], "partial")

    async def test_no_cash_fails_loud(self):
        out_text_ready = await self._run({})
        self.assertEqual(out_text_ready["liquidity"]["cash"],
                         {"status": "not_reported"})
        self.assertIsNone(out_text_ready["runway_inputs"]["cash_only_base"])
        self.assertEqual(
            out_text_ready["liquidity"]["total_available"]["status"],
            "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
