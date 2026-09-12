# -*- coding: utf-8 -*-
"""2026-09 개편으로 새로 생긴 세 자료의 계약.

여기서 지키는 것은 **라벨과 값의 결합**이다. 숫자만 옮기고 단위를 잃으면
고객이 억원을 원으로 읽는다. 아직 안 정해진 값을 0 으로 채우면 "공모가 0원"이
된다. 그래서 모양이 아니라 그 두 가지를 못박는다.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from stock_mcp_server import naver


def _run(coro):
    return asyncio.run(coro)


class IpoScheduleTests(unittest.TestCase):
    """공모 단계마다 '아직 안 정해진 값'이 있다. 0 으로 채우면 안 된다."""

    PAYLOAD = {
        "examinationList": [{
            "ipoCode": "A001", "compName": "가나기업", "marketType": "KOSDAQ",
            "compUpjong": "소프트웨어", "ipoStatus": "증권신고서제출",
            "hopePubStart": "17000", "hopePubEnd": "18500",
            "fixPubPrice": None, "fnlCmptRatio": None,
            "poStartDate": "2026-10-13", "poEndDate": "2026-10-14",
            "lcalDate": None, "poExptShares": "2228917",
            "orgNm": "대신증권",
        }],
        "listingList": [{
            "ipoCode": "A002", "compName": "다라기업", "marketType": "KOSPI",
            "ipoStatus": "청약완료", "hopePubStart": "13800",
            "hopePubEnd": "15800", "fixPubPrice": "10000",
            "fnlCmptRatio": "1047.78", "lcalDate": "2026-09-21",
            "orgNm": "한국투자증권",
        }],
    }

    def _fetch(self):
        with patch.object(naver, "_api_json", AsyncMock(return_value=self.PAYLOAD)):
            return _run(naver.get_ipo_schedule.__wrapped__())

    def test_stages_use_naver_wording(self):
        data = self._fetch()
        self.assertEqual(set(data["stages"]), {"심사", "상장예정"})
        self.assertEqual(data["total"], 2)

    def test_undecided_values_stay_none_not_zero(self):
        """확정공모가·경쟁률은 그 단계를 지나야 나온다. 0 이 아니다."""
        row = self._fetch()["stages"]["심사"][0]
        self.assertIsNone(row["fixed_price"])
        self.assertIsNone(row["forecast_competition"])
        self.assertIsNone(row["listing_date"])
        # 희망가는 심사 단계에도 있다
        self.assertEqual(row["hope_price_low"], 17000)
        self.assertEqual(row["hope_price_high"], 18500)

    def test_decided_values_are_numbers(self):
        row = self._fetch()["stages"]["상장예정"][0]
        self.assertEqual(row["fixed_price"], 10000)
        self.assertAlmostEqual(row["forecast_competition"], 1047.78)
        self.assertEqual(row["listing_date"], "2026-09-21")

    def test_hope_and_fixed_price_are_separate_fields(self):
        """희망가와 확정가를 한 칸에 섞으면 둘을 구분할 수 없게 된다."""
        row = self._fetch()["stages"]["상장예정"][0]
        self.assertNotEqual(row["hope_price_low"], row["fixed_price"])


class InvestorDepositTests(unittest.TestCase):
    """단위는 억원이다. 라벨은 네이버 화면 표기를 따른다."""

    PAYLOAD = {"content": [{
        "bizdate": "20260909",
        "customerDeposit": "1028462", "customerDepositDiff": "58598",
        "creditLoan": "319898", "creditLoanDiff": "-3287",
        "beneficiaryCertificateStock": "3419611",
        "beneficiaryCertificateStockDiff": "23863",
        "beneficiaryCertificateBond": "2131803",
        "beneficiaryCertificateBondDiff": "1063",
        "beneficiaryCertificateMixing": "477062",
        "beneficiaryCertificateMixingDiff": "-233",
    }]}

    def _fetch(self, payload=None):
        with patch.object(naver, "_api_json",
                          AsyncMock(return_value=payload or self.PAYLOAD)):
            return _run(naver.get_investor_deposit.__wrapped__(days=5))

    def test_unit_travels_with_the_numbers(self):
        self.assertEqual(self._fetch()["unit"], "억원")

    def test_labels_match_naver_wording(self):
        self.assertEqual(
            self._fetch()["labels"],
            ["고객예탁금", "신용잔고", "주식형펀드", "채권형펀드", "혼합형펀드"])

    def test_negative_diff_keeps_its_sign(self):
        """신용잔고가 줄어든 날을 늘어난 날로 읽으면 정반대 결론이 된다."""
        row = self._fetch()["rows"][0]
        self.assertEqual(row["신용잔고"], 319898)
        self.assertEqual(row["신용잔고_전일대비"], -3287)
        self.assertEqual(row["혼합형펀드_전일대비"], -233)

    def test_missing_value_is_absent_not_zero(self):
        payload = {"content": [{"bizdate": "20260909", "creditLoan": "100"}]}
        row = self._fetch(payload)["rows"][0]
        self.assertNotIn("고객예탁금", row)
        self.assertEqual(row["신용잔고"], 100)

    def test_rows_without_a_date_are_dropped(self):
        payload = {"content": [{"customerDeposit": "1"}]}
        self.assertEqual(self._fetch(payload)["rows"], [])


class MarketReportTests(unittest.TestCase):
    PAYLOAD = {
        "market": [{"nid": "1", "title": "시황", "brokerName": "A증권",
                    "writeDate": "2026-09-11", "readCount": "515"}],
        "industry": [{"nid": "2", "title": "산업", "brokerName": "B증권",
                      "writeDate": "2026-09-11", "readCount": "462",
                      "industryKoreanName": "철강금속", "analystName": "홍길동"}],
    }

    def _fetch(self, kind):
        with patch.object(naver, "_api_json", AsyncMock(return_value=self.PAYLOAD)):
            return _run(naver.get_research_by_kind.__wrapped__(kind=kind, count=3))

    def test_unknown_kind_is_rejected_not_guessed(self):
        with self.assertRaises(ValueError):
            self._fetch("없는갈래")

    def test_industry_fields_appear_only_for_industry(self):
        """없는 칸을 만들어 내지 않는다. 시황 리포트에 업종은 없다."""
        self.assertNotIn("industry", self._fetch("market")["reports"][0])
        self.assertEqual(self._fetch("industry")["reports"][0]["industry"], "철강금속")

    def test_kind_label_is_korean(self):
        self.assertEqual(self._fetch("market")["kind_label"], "시황")


class ToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_datasets_are_registered_as_tools(self):
        from stock_mcp_server.server import mcp
        names = {t.name for t in await mcp.list_tools()}
        for name in ("get_ipo_schedule", "get_investor_deposit"):
            self.assertIn(name, names)

    async def test_report_kinds_did_not_become_a_new_tool(self):
        """도구 하나가 승인 클릭 하나다. 같은 자료는 인자로 가른다."""
        from stock_mcp_server.server import mcp
        tools = {t.name: t for t in await mcp.list_tools()}
        self.assertNotIn("get_market_reports", tools)
        params = (tools["get_reports"].inputSchema or {}).get("properties") or {}
        self.assertIn("kind", params)


if __name__ == "__main__":
    unittest.main()
