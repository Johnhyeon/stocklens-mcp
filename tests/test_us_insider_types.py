"""SL-15: 미국 내부자 거래 유형과 부호 일관성.

실측(UAT, XOM 최근 6개월): 제공자 요약은 Purchases 10,818,651주 / Net
+6,914,499주인데 % Net 은 -1.948 이었다. 실제 현금 매수는 0건, 매도 6건 -
무상 부여·증여가 Purchases 에 섞여 상단 요약만 읽으면 경제적 방향이 반대로
읽혔다.
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


def _tx(action, shares, value, date="2026-08-10", insider="DOE JOHN", pos="Officer"):
    return {"start_date": date, "insider": insider, "position": pos,
            "transaction": action, "text": action, "shares": shares, "value": value}


XOM_TX = (
    [_tx("Sale", 100_000, 11_000_000, date=f"2026-0{m}-1{i}")
     for i, m in enumerate((3, 4, 5, 6, 7, 8), start=1)][:6]
    + [_tx("Stock Award(Grant) at price 0.00 per share", 3_000_000, 0,
           date="2026-06-02"),
       _tx("Stock Gift at price 0.00 per share", 500_000, 0, date="2026-05-20"),
       _tx("Conversion of Exercise of derivative security", 200_000, 0,
           date="2026-04-15"),
       _tx("Payment of exercise price or tax liability", 50_000, 2_000_000,
           date="2026-04-15")]
)

# 제공자 요약(라벨 -> shares/trans). % 행은 shares 자리에 비율이 들어온다.
XOM_SUMMARY_CONTRADICTORY = {
    "Total Insider Purchases Last 6m": {"shares": 10_818_651, "trans": 9},
    "Total Insider Sales Last 6m": {"shares": 3_904_152, "trans": 6},
    "Net Shares Purchased (Sold)": {"shares": 6_914_499, "trans": 15},
    "% Net Shares Purchased (Sold)": {"shares": -1.948, "trans": None},
}
CONSISTENT_SUMMARY = {
    "Net Shares Purchased (Sold)": {"shares": -6_914_499, "trans": 15},
    "% Net Shares Purchased (Sold)": {"shares": -1.948, "trans": None},
}


class InsiderClassifierTests(unittest.TestCase):
    def test_categories_are_separated(self):
        cls = server._classify_insider_tx
        self.assertEqual(cls(_tx("Purchase at price 110.00", 100, 11_000)),
                         "open_market_buy")
        self.assertEqual(cls(_tx("Sale", 100, 11_000)), "sale")
        self.assertEqual(cls(_tx("Stock Award(Grant) at price 0.00", 100, 0)), "grant")
        self.assertEqual(cls(_tx("Stock Gift at price 0.00", 100, 0)), "gift")
        self.assertEqual(cls(_tx("Conversion of Exercise of derivative security",
                                 100, 0)), "exercise")
        self.assertEqual(cls(_tx("Payment of exercise price or tax liability",
                                 100, 5_000)), "tax_withholding")

    def test_cashless_purchase_is_not_open_market(self):
        """대금 없는 'Purchase' 를 공개시장 매수로 세지 않는다."""
        self.assertNotEqual(
            server._classify_insider_tx(_tx("Purchase", 100, 0)), "open_market_buy")

    def test_unrecognized_action_is_unknown(self):
        self.assertEqual(server._classify_insider_tx(_tx("???", 100, 0)), "unknown")


class InsiderToolTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tx=XOM_TX, summary=XOM_SUMMARY_CONTRADICTORY):
        data = {"ticker": "XOM", "purchases_last_6m": summary,
                "recent_transactions": list(tx)}
        with patch.object(server.us, "get_insider", AsyncMock(return_value=data)), \
             patch.object(server.us, "get_insider_roster",
                          AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_insider(ticker="XOM")

    async def test_cash_headline_comes_first(self):
        """수용 1·요구 2: 기본 헤드라인은 현금 거래다 - 매수 0건, 매도 6건."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        cash_pos = body.find("실제 현금 거래")
        provider_pos = body.find("제공자 요약")
        self.assertGreater(cash_pos, -1)
        self.assertGreater(provider_pos, -1)
        self.assertLess(cash_pos, provider_pos)   # 현금 거래가 먼저
        self.assertIn("매수(Purchase): 0건", body)
        self.assertIn("매도(Sale): 6건", body)

    async def test_categories_are_not_merged_into_purchases(self):
        """수용 2: grant 포함 총이동과 현금 거래를 같은 이름으로 합치지 않는다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("grant", body)
        self.assertIn("gift", body)
        self.assertIn("exercise", body)
        self.assertIn("tax_withholding", body)
        meta = extract_meta(text)
        cats = meta["insider_coverage"]["categories"]
        self.assertEqual(cats["open_market_buy"]["count"], 0)
        self.assertEqual(cats["sale"]["count"], 6)
        self.assertEqual(cats["grant"]["count"], 1)
        self.assertEqual(cats["grant"]["shares"], 3_000_000)
        self.assertEqual(cats["gift"]["count"], 1)

    async def test_sign_contradiction_is_marked_invalid(self):
        """수용 3: 순주식수 양수 vs 순비율 음수 모순은 invalid 로 표시된다."""
        text = await self._run()
        meta = extract_meta(text)
        self.assertFalse(meta["insider_coverage"]["provider_summary_valid"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(any("모순" in w or "invalid" in w.lower()
                            for w in meta["warnings"]), meta["warnings"])
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("invalid", body)

    async def test_consistent_summary_is_valid_and_complete(self):
        text = await self._run(summary=CONSISTENT_SUMMARY)
        meta = extract_meta(text)
        self.assertTrue(meta["insider_coverage"]["provider_summary_valid"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_meta_keeps_dates_and_counts(self):
        """요구 4: 하위 집계의 기준일과 포함 거래 수가 메타에 있다."""
        text = await self._run()
        meta = extract_meta(text)
        ic = meta["insider_coverage"]
        self.assertEqual(ic["transactions_listed"], len(XOM_TX))
        self.assertEqual(meta["data_as_of"], "2026-08-16")   # 가장 최근 거래일
        self.assertEqual(ic["latest_transaction_date"], "2026-08-16")
        self.assertEqual(ic["provider_window"], "6m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
