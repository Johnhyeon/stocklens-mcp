"""SL-10: 미국 뉴스의 티커 직접 관련도.

실측(UAT + 2026-08-27 재확인): AAPL 뉴스에 InterDigital·NVDA·Sandisk·GOOGL 이
중심인 기사가 섞여 온다. 스킬 08(호재인데 왜 안 오르지)·19가 남의 회사 기사를
이 회사의 원인으로 채택할 위험이 있다.
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


def _n(title, summary=""):
    return {"title": title, "summary": summary, "published": "2026-08-26T12:00:00Z",
            "provider": "TestWire", "url": "https://example.com/a"}


NEWS = [
    _n("Apple unveils new iPhone lineup at fall event",
       "Apple Inc. announced its latest devices."),                     # direct
    _n("InterDigital wins patent ruling",
       "InterDigital shares rose after the decision."),                 # 무관
    _n("Nvidia earnings crush estimates",
       "Nvidia reported record data center revenue."),                  # 무관
    _n("Better Buy: Apple vs. Microsoft",
       "Comparing two tech giants, Apple and Microsoft."),              # 비교
    _n("Apple supplier Foxconn beats quarterly estimates",
       "The contract manufacturer, a key Apple supplier, posted strong results."),  # 공급망
    _n("10 Best Tech Stocks to Buy Now",
       "Our list includes Apple, Microsoft, Nvidia and more."),         # 리스트
    _n("Chip demand stays hot",
       "Analysts see continued strength. Apple and its peers benefit from the cycle."),  # 요약만 언급
]


class RelevanceClassifierTests(unittest.TestCase):
    NAMES = ("Apple", "AAPL")

    def _cls(self, item):
        return server._classify_news_relevance(item, self.NAMES)

    def test_direct_article(self):
        r = self._cls(NEWS[0])
        self.assertEqual(r["category"], "direct")
        self.assertGreaterEqual(r["relevance_score"], 80)
        self.assertIn("제목", r["basis"])

    def test_unrelated_articles_score_low(self):
        for item in (NEWS[1], NEWS[2]):
            r = self._cls(item)
            self.assertEqual(r["category"], "unrelated")
            self.assertLess(r["relevance_score"], 30)

    def test_comparison_is_a_separate_category(self):
        r = self._cls(NEWS[3])
        self.assertEqual(r["category"], "comparison")

    def test_supply_chain_is_a_separate_category(self):
        r = self._cls(NEWS[4])
        self.assertEqual(r["category"], "supply_chain")

    def test_list_article_is_a_separate_category(self):
        r = self._cls(NEWS[5])
        self.assertEqual(r["category"], "list_or_etf")

    def test_summary_only_mention_is_indirect(self):
        r = self._cls(NEWS[6])
        self.assertEqual(r["category"], "mention")
        self.assertLess(r["relevance_score"], 80)


class NewsToolTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, **kw):
        data = {"ticker": "AAPL", "news": list(NEWS)}
        opts = dict(ticker="AAPL")
        opts.update(kw)
        with patch.object(server.us, "get_news", AsyncMock(return_value=data)), \
             patch.object(server.us, "get_info_raw",
                          AsyncMock(return_value={"symbol": "AAPL",
                                                  "longName": "Apple Inc.",
                                                  "shortName": "Apple"})), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_news(**opts)

    async def test_every_article_is_labeled(self):
        """요구 1: 기사마다 관련도와 근거가 붙는다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("direct", body)
        self.assertIn("unrelated", body)
        self.assertIn("supply_chain", body)
        meta = extract_meta(text)
        counts = meta["news_relevance"]["categories"]
        self.assertEqual(counts["direct"], 1)
        self.assertEqual(counts["unrelated"], 2)
        self.assertEqual(counts["comparison"], 1)
        self.assertEqual(counts["supply_chain"], 1)

    async def test_direct_only_excludes_other_companies(self):
        """수용 1: InterDigital·NVDA 단독 기사가 제외된다."""
        text = await self._run(direct_only=True)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("Apple unveils", body)
        self.assertNotIn("InterDigital", body)
        self.assertNotIn("Nvidia earnings", body)
        self.assertNotIn("Better Buy", body)        # 비교도 direct 가 아니다
        meta = extract_meta(text)
        self.assertEqual(meta["news_relevance"]["excluded"]["unrelated"], 2)
        self.assertIn("제외", body)

    async def test_indirect_context_can_remain_in_default_mode(self):
        """수용 2: 기본 모드에서 공급망·비교 기사는 간접 표시로 남는다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("Foxconn", body)
        self.assertIn("supply_chain", body)
        self.assertIn("Better Buy", body)

    async def test_meta_envelope_exists_now(self):
        """이 도구는 메타 봉투 자체가 없었다."""
        text = await self._run()
        self.assertIn(rmeta.MARKER_START, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
