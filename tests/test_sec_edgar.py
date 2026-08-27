"""SL-04: SEC 원문·범위·희석·계약 심층 경로.

실측(UAT 2026-08-27): 기존 get_us_filings 는 Yahoo 중계 목록이라
- accession number·report date·SEC 원문이 없고
- GOOGL 은 "공시 정보 없음"인데 같은 발행사의 GOOG 는 15건이 나왔으며
- 목록 잘림·전체 건수·메타가 전혀 없었다.
SEC 는 발행사를 CIK 로 식별한다. 티커를 CIK 로 정규화하고 submissions API 를
1차 출처로 쓰며, 원문을 확보하지 못하면 "없다"가 아니라 "확인 불가"다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import sec_edgar as sec
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    assert rmeta.MARKER_START in text, "메타 봉투가 없다"
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}

_ISSUER_GOOGL = {
    "requested_ticker": "GOOGL", "cik": 1652044, "name": "Alphabet Inc.",
    "primary_ticker": "GOOGL", "issuer_tickers": ["GOOGL", "GOOG"],
}


def _row(form="10-Q", fdate="2026-07-30", rdate="2026-06-30",
         acc="0001874178-26-000054", doc="rivn-20260630.htm"):
    return {"form": form, "filing_date": fdate, "report_date": rdate,
            "accession": acc, "primary_document": doc,
            "primary_doc_description": None, "items": None}


class IssuerResolutionTests(unittest.IsolatedAsyncioTestCase):
    """수용 2: 클래스주 티커가 CIK 중심 동일 발행사로 정규화된다."""

    TICKER_MAP = {
        "GOOGL": {"cik": 1652044, "title": "Alphabet Inc.",
                  "issuer_tickers": ["GOOGL", "GOOG"], "primary_ticker": "GOOGL"},
        "GOOG": {"cik": 1652044, "title": "Alphabet Inc.",
                 "issuer_tickers": ["GOOGL", "GOOG"], "primary_ticker": "GOOGL"},
    }

    async def test_share_classes_resolve_to_the_same_issuer(self):
        with patch.object(sec, "load_ticker_map",
                          AsyncMock(return_value=self.TICKER_MAP)):
            a = await sec.resolve_issuer("GOOGL")
            b = await sec.resolve_issuer("goog")
        self.assertEqual(a["cik"], b["cik"])
        self.assertEqual(a["requested_ticker"], "GOOGL")
        self.assertEqual(b["requested_ticker"], "GOOG")   # 요청 티커는 보존
        self.assertEqual(b["primary_ticker"], "GOOGL")

    async def test_unknown_ticker_is_none_not_error(self):
        with patch.object(sec, "load_ticker_map",
                          AsyncMock(return_value=self.TICKER_MAP)):
            self.assertIsNone(await sec.resolve_issuer("NOPE"))


class FilingsToolTests(unittest.IsolatedAsyncioTestCase):
    """요구 1·2·3·6 - 목록 도구."""

    def _subs(self, rows, has_older=False):
        pages = ([{"name": "CIK0001652044-submissions-001.json", "count": 500,
                   "filing_from": "2015-01-01", "filing_to": "2020-01-01"}]
                 if has_older else [])
        return {"name": "Alphabet Inc.", "tickers": ["GOOGL", "GOOG"],
                "rows": rows, "recent_count": len(rows),
                "has_older": has_older, "pages": pages}

    async def _run(self, issuer=_ISSUER_GOOGL, subs=None, **kw):
        opts = dict(ticker="GOOGL")
        opts.update(kw)
        with patch.object(server.sec, "resolve_issuer", AsyncMock(return_value=issuer)), \
             patch.object(server.sec, "get_submissions",
                          AsyncMock(return_value=subs if subs is not None
                                    else self._subs([_row()]))), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_filings(**opts)

    async def test_rows_carry_sec_identifiers(self):
        """요구 1: form·접수일·보고서 기준일·accession·문서·SEC URL."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("0001874178-26-000054", body)
        self.assertIn("2026-06-30", body)                       # report date
        self.assertIn("rivn-20260630.htm", body)
        self.assertIn("sec.gov/Archives/edgar/data/1652044", body)
        meta = extract_meta(text)
        self.assertEqual(meta["issuer"]["cik"], 1652044)
        self.assertEqual(meta["issuer"]["requested_ticker"], "GOOGL")
        self.assertEqual(meta["issuer"]["issuer_tickers"], ["GOOGL", "GOOG"])

    async def test_truncation_is_reported(self):
        rows = [_row(acc=f"0000000000-26-{i:06d}") for i in range(6)]
        text = await self._run(subs=self._subs(rows), limit=2)
        meta = extract_meta(text)
        cov = meta["coverage"]
        self.assertEqual(cov["returned_count"], 2)
        self.assertEqual(cov["total_count"], 6)
        self.assertTrue(cov["truncated"])
        self.assertFalse(cov["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_forms_filter(self):
        rows = [_row(form="10-Q"), _row(form="8-K", acc="0-8k"),
                _row(form="4", acc="0-f4")]
        text = await self._run(subs=self._subs(rows), forms=["8-K"])
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("8-K", body)
        self.assertNotIn("10-Q", body.split("|", 1)[-1])
        self.assertEqual(extract_meta(text)["coverage"]["requested"]["forms"], ["8-K"])

    async def test_unknown_ticker_has_meta_and_distinct_state(self):
        """수용 3: 잘못된 티커가 메타 없이 '공시 정보 없음'으로 끝나지 않는다."""
        text = await self._run(issuer=None)
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertTrue(any("매핑" in w for w in meta["warnings"]), meta["warnings"])
        self.assertFalse(meta["coverage"]["coverage_complete"])

    async def test_fetch_error_is_not_zero_filings(self):
        """요구 6: 조회 실패는 '0건'과 다른 상태다."""
        with patch.object(server.sec, "resolve_issuer",
                          AsyncMock(return_value=_ISSUER_GOOGL)), \
             patch.object(server.sec, "get_submissions",
                          AsyncMock(side_effect=sec.SecFetchError("timeout"))), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            text = await server.get_us_filings(ticker="GOOGL")
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertTrue(any("실패" in w for w in meta["warnings"]))
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("공시 정보 없음", body)

    async def test_zero_recent_with_older_files_is_not_confirmed_absence(self):
        text = await self._run(subs=self._subs([], has_older=True))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertIsNone(meta["coverage"]["total_count"])
        self.assertFalse(meta["coverage"]["coverage_complete"])


DILUTION_FIXTURE = """
As of June 30, 2026, we had 1,014,260,000 shares of Class A common stock outstanding.
In the offering, we issued 40,000,000 shares of Class A common stock to the purchasers.
The notes are convertible notes with an initial conversion price of $14.50 per share,
subject to adjustment. Upon conversion, we may deliver shares of Class A common stock.
We also issued warrants to purchase 3,500,000 shares at an exercise price of $11.00.
The conversion price adjusts pursuant to a full ratchet anti-dilution provision if we
issue shares below the conversion price. We intend to use the net proceeds from this
offering for working capital and general corporate purposes (use of proceeds).
"""

CONTRACT_FIXTURE = """
Under the supply agreement, the customer agreed to an aggregate purchase price of
$450.0 million over the term of the agreement. The initial term of the agreement is
five years, through December 31, 2031. Either party may terminate the agreement upon
a material breach that remains uncured for 30 days. The agreement contains no minimum
purchase commitment for the first contract year. Portions of this exhibit have been
omitted pursuant to a request for confidential treatment and are marked [***].
"""


class FactExtractionTests(unittest.TestCase):
    """수용 4·5: 희석·계약 사실이 범주로 분리되고, 못 찾은 범주는 미확인이다."""

    def test_dilution_categories_are_separated(self):
        facts = sec.extract_dilution_facts(DILUTION_FIXTURE)
        found = facts["found"]
        self.assertIn("기본주식수", found)
        self.assertIn("1,014,260,000", found["기본주식수"]["shown"][0]["snippet"])
        self.assertIn("확정발행", found)
        self.assertIn("40,000,000", found["확정발행"]["shown"][0]["snippet"])
        self.assertIn("잠재발행_전환", found)
        self.assertIn("$14.50", found["잠재발행_전환"]["shown"][0]["snippet"])
        self.assertIn("잠재발행_워런트", found)
        self.assertIn("리픽싱_조정", found)
        self.assertIn("full ratchet", found["리픽싱_조정"]["shown"][0]["snippet"])
        self.assertIn("자금용도", found)
        self.assertEqual(facts["unconfirmed"], [])

    def test_missing_categories_are_unconfirmed_not_absent(self):
        facts = sec.extract_dilution_facts("Nothing about equity here at all.")
        self.assertEqual(facts["found"], {})
        self.assertEqual(set(facts["unconfirmed"]), set(sec.DILUTION_PATTERNS))

    def test_contract_categories_are_separated(self):
        facts = sec.extract_contract_facts(CONTRACT_FIXTURE)
        found = facts["found"]
        self.assertIn("$450.0 million", found["계약금액"]["shown"][0]["snippet"])
        self.assertIn("기간", found)
        self.assertIn("해지조항", found)
        self.assertIn("최소구매", found)
        self.assertIn("상대방_비공개", found)

    def test_counting_is_separate_from_showing(self):
        text = "Either party may terminate this agreement. " * 10
        facts = sec.extract_contract_facts(text)
        cat = facts["found"]["해지조항"]
        self.assertEqual(cat["total_matches"], 10)
        self.assertLessEqual(len(cat["shown"]), 3)


class FilingDetailToolTests(unittest.IsolatedAsyncioTestCase):
    """요구 4·7 - 원문 검색·분석 도구."""

    async def _run(self, *, text=DILUTION_FIXTURE, index=None,
                   text_error=None, **kw):
        opts = dict(ticker="RIVN", accession_no="0001874178-26-000054")
        opts.update(kw)
        fetch = (AsyncMock(side_effect=text_error) if text_error
                 else AsyncMock(return_value=text))
        subs = {"name": "Rivian", "tickers": ["RIVN"],
                "rows": [_row()], "recent_count": 1, "has_older": False,
                "pages": []}
        issuer = {"requested_ticker": "RIVN", "cik": 1874178, "name": "Rivian",
                  "primary_ticker": "RIVN", "issuer_tickers": ["RIVN"]}
        with patch.object(server.sec, "resolve_issuer", AsyncMock(return_value=issuer)), \
             patch.object(server.sec, "get_submissions", AsyncMock(return_value=subs)), \
             patch.object(server.sec, "fetch_filing_index",
                          AsyncMock(return_value=index or [
                              {"name": "rivn-20260630.htm", "size": 1621956},
                              {"name": "ex10-1.htm", "size": 88000},
                          ])), \
             patch.object(server.sec, "fetch_filing_text", fetch), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_filing_detail(**opts)

    async def test_find_reports_match_coverage(self):
        text = await self._run(text="conversion price " * 9, find="conversion price")
        meta = extract_meta(text)
        mc = meta["match_coverage"]
        self.assertEqual(mc["total_matches"], 9)
        self.assertEqual(mc["displayed_matches"], 5)
        self.assertTrue(mc["truncated"])
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_zero_match_does_not_confirm_absence(self):
        text = await self._run(find="poison pill")
        meta = extract_meta(text)
        self.assertEqual(meta["match_coverage"]["total_matches"], 0)
        self.assertIs(meta["absence_confirmed"], False)
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_analyze_dilution_lists_unconfirmed(self):
        text = await self._run(text="We issued 1,000 shares in this offering.",
                               analyze="dilution")
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("미확인", body)
        meta = extract_meta(text)
        self.assertIn("리픽싱_조정", meta["analysis"]["unconfirmed_categories"])
        self.assertTrue(any("없다는 뜻이 아" in w for w in meta["warnings"]))

    async def test_listing_mode_shows_documents(self):
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("ex10-1.htm", body)
        self.assertIn("sec.gov", body)

    async def test_unfetchable_text_is_unconfirmed_not_absent(self):
        """요구 7: 원문을 못 가져오면 '확인 불가'다. '없다'가 아니다."""
        text = await self._run(find="conversion price",
                               text_error=sec.SecFetchError("blocked"))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertTrue(any("확인" in w for w in meta["warnings"]))
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("매치 없음", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
