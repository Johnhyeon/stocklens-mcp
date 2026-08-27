"""결함 4: SEC 공시 목록의 구간(페이지) 순회.

실측(UAT): 최근 구간(recent, 최대 1000건)만 조회 가능해 그보다 오래된 공시를
순회할 방법이 없었다 - "완전한 검색"을 끝낼 수 없다. SEC submissions 는
더 오래된 구간을 별도 JSON 파일(filings.files)로 제공한다. 이를 커서로 노출한다.
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


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}
_ISSUER = {"requested_ticker": "RIVN", "cik": 1874178, "name": "Rivian Automotive, Inc.",
           "primary_ticker": "RIVN", "issuer_tickers": ["RIVN"]}


def _recent_payload():
    return {
        "name": "Rivian Automotive, Inc.",
        "tickers": ["RIVN"],
        "sic": "3711", "sicDescription": "Motor Vehicles",
        "filings": {
            "recent": {
                "form": ["10-Q", "8-K"],
                "filingDate": ["2026-08-05", "2026-08-01"],
                "reportDate": ["2026-06-30", ""],
                "accessionNumber": ["0001-26-000001", "0001-26-000002"],
                "primaryDocument": ["q.htm", "e.htm"],
                "primaryDocDescription": ["10-Q", "8-K"],
                "items": ["", "2.02"],
            },
            "files": [
                {"name": "CIK0001874178-submissions-001.json",
                 "filingCount": 700, "filingFrom": "2021-08-01", "filingTo": "2023-05-01"},
            ],
        },
    }


def _older_payload():
    return {
        "filings": None,   # 구간 파일은 최상위가 곧 recent 형태다
        "form": ["S-1", "S-1/A"],
        "filingDate": ["2021-10-01", "2021-11-01"],
        "reportDate": ["", ""],
        "accessionNumber": ["0001-21-000001", "0001-21-000002"],
        "primaryDocument": ["s1.htm", "s1a.htm"],
        "primaryDocDescription": ["S-1", "S-1/A"],
        "items": ["", ""],
    }


class SubmissionsPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_submissions_exposes_pages(self):
        with patch.object(sec, "_get_json", AsyncMock(return_value=_recent_payload())):
            subs = await sec.get_submissions(1874178)
        self.assertTrue(subs["has_older"])
        self.assertEqual(len(subs["pages"]), 1)
        page = subs["pages"][0]
        self.assertEqual(page["name"], "CIK0001874178-submissions-001.json")
        self.assertEqual(page["filing_from"], "2021-08-01")
        self.assertEqual(page["filing_to"], "2023-05-01")
        self.assertEqual(page["count"], 700)

    async def test_get_submissions_page_returns_rows(self):
        with patch.object(sec, "_get_json", AsyncMock(return_value=_older_payload())):
            rows = await sec.get_submissions_page(
                1874178, "CIK0001874178-submissions-001.json")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["form"], "S-1")
        self.assertEqual(rows[0]["accession"], "0001-21-000001")

    async def test_page_name_is_validated(self):
        """커서는 SEC 파일명 형식만 - 임의 URL 조각을 받아 넘기지 않는다."""
        with self.assertRaises(ValueError):
            await sec.get_submissions_page(1874178, "../../etc/passwd")
        with self.assertRaises(ValueError):
            await sec.get_submissions_page(1874178, "CIK0000000001-submissions-001.json")


class FilingsToolPagingTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, **kw):
        async def fake_json(url):
            if url.endswith("-001.json"):
                return _older_payload()
            return _recent_payload()

        with patch.object(server.sec, "resolve_issuer", AsyncMock(return_value=_ISSUER)), \
             patch.object(server.sec, "_get_json", AsyncMock(side_effect=fake_json)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_us_filings("RIVN", **kw)

    def _meta(self, text):
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
        return json.loads(payload.strip())

    async def test_default_lists_older_pages_as_cursor(self):
        text = await self._run()
        meta = self._meta(text)
        pages = meta["coverage"]["older_pages"]
        self.assertEqual(pages[0]["name"], "CIK0001874178-submissions-001.json")
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("2021-08-01", body)     # 구간 범위가 본문에 보인다
        self.assertIn("page=", body)          # 순회 방법 안내

    async def test_page_param_fetches_older_slice(self):
        text = await self._run(page="CIK0001874178-submissions-001.json")
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("S-1", body)
        self.assertIn("2021-10-01", body)
        self.assertNotIn("2026-08-05", body)   # recent 구간이 아니다
        meta = self._meta(text)
        self.assertEqual(meta["coverage"]["page"],
                         "CIK0001874178-submissions-001.json")

    async def test_within_page_offset_cursor(self):
        """출시 차단 2: 구간 파일 1,240건을 limit 100으로도 끝까지 순회할 수 있다.

        실측(AAPL): 구간 조회가 truncated=true 인데 older_pages=[] 라서
        101번째 이후를 요청할 방법이 없었고, 안내문은 순회가 끝났다고 말했다.
        """
        text = await self._run(page="CIK0001874178-submissions-001.json", limit=1)
        meta = self._meta(text)
        cov = meta["coverage"]
        self.assertTrue(cov["truncated"])
        self.assertFalse(cov["coverage_complete"])       # 구간 내부가 남았다
        self.assertEqual(cov["next_offset"], 1)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("offset=1", body)                  # 이어서 조회하는 방법 안내
        self.assertIn("2021-10-01", body)
        self.assertNotIn("2021-11-01", body)
        # 커서로 다음 조각
        text2 = await self._run(page="CIK0001874178-submissions-001.json",
                                limit=1, offset=1)
        body2 = text2.split(rmeta.MARKER_START)[0]
        self.assertIn("2021-11-01", body2)
        self.assertNotIn("2021-10-01", body2)
        meta2 = self._meta(text2)
        self.assertFalse(meta2["coverage"]["truncated"])
        self.assertIsNone(meta2["coverage"]["next_offset"])
        self.assertTrue(meta2["coverage"]["coverage_complete"])
        self.assertEqual(meta2["coverage"]["offset"], 1)

    async def test_recent_truncation_also_gets_offset(self):
        """recent 구간도 같은 커서를 쓴다 - limit 에 걸리면 next_offset."""
        text = await self._run(limit=1)
        meta = self._meta(text)
        self.assertEqual(meta["coverage"]["next_offset"], 1)
        self.assertFalse(meta["coverage"]["coverage_complete"])

    async def test_last_page_without_older_is_complete(self):
        """마지막 구간까지 소진하면 완전한 검색이 끝났다고 말할 수 있어야 한다."""
        text = await self._run(page="CIK0001874178-submissions-001.json")
        meta = self._meta(text)
        # 이 구간 자체는 잘림 없이 전부 표시됨 + 더 오래된 페이지 없음
        self.assertEqual(meta["coverage"]["older_pages"], [])
        self.assertTrue(meta["coverage"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
