"""증권사 리포트 PDF 본문 추출 — '못 읽음'과 '내용 없음'의 구분.

증권사마다 PDF 만드는 방식이 다르다. 글자가 아니라 이미지로 렌더링해 내는 곳은
본문을 읽을 수 없는데, 거기서 긁힌 몇 글자를 '본문'이라고 내보내면 사용자는
그게 리포트 전체인 줄 안다(2026-08-17 실측: 미래에셋 9쪽에서 144자).
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _pdf as pdfx


def _blank_pdf(pages: int = 2) -> bytes:
    """글자가 없는 유효한 PDF — 이미지 렌더링 리포트의 대역."""
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class ExtractTextTests(unittest.TestCase):
    def test_non_pdf_bytes_is_failed_not_empty_body(self):
        out = pdfx.extract_text(b"<html>not a pdf</html>")
        self.assertEqual(out.status, "failed")
        self.assertIn("PDF 형식이 아닙니다", out.detail)

    def test_empty_payload_is_failed(self):
        self.assertEqual(pdfx.extract_text(b"").status, "failed")

    def test_textless_pdf_is_reported_as_image_pdf(self):
        """0자가 나와도 '본문 없음'이 아니라 '못 읽음'이어야 한다."""
        out = pdfx.extract_text(_blank_pdf(3))
        self.assertEqual(out.status, "image_pdf")
        self.assertEqual(out.pages, 3)
        self.assertLess(out.chars, pdfx.TEXT_MIN_CHARS)
        self.assertEqual(out.text, "")  # 몇 글자라도 본문인 척 내보내지 않는다
        self.assertIn("이미지로 렌더링된 PDF", out.detail)

    def test_threshold_decides_ok_vs_image(self):
        """임계값이 판정의 유일한 기준이라는 걸 고정한다."""
        raw = _blank_pdf(1)
        original = pdfx.TEXT_MIN_CHARS
        try:
            pdfx.TEXT_MIN_CHARS = 0
            self.assertEqual(pdfx.extract_text(raw).status, "ok")
        finally:
            pdfx.TEXT_MIN_CHARS = original
        self.assertEqual(pdfx.extract_text(raw).status, "image_pdf")

    def test_corrupt_pdf_header_but_broken_body_is_failed(self):
        out = pdfx.extract_text(b"%PDF-1.4\n<<broken>>")
        self.assertEqual(out.status, "failed")

    def test_size_guard_constant_is_sane(self):
        # 리포트는 보통 1MB 안팎, 이미지 기반이 4MB 정도. 상한이 그 아래로
        # 내려가면 정상 리포트가 막힌다.
        self.assertGreaterEqual(pdfx.MAX_PDF_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()


class ReportContentModeTests(unittest.IsolatedAsyncioTestCase):
    """mode 선택지 — 사용자가 발췌/전문/링크를 고를 수 있어야 한다."""

    FAKE_PDF = "https://stock.pstatic.net/stock-research/company/38/fake.pdf"

    def _patch(self, extract: pdfx.PdfExtract | None = None):
        from unittest.mock import AsyncMock, patch

        from stock_mcp_server import server

        self._ctxs = [
            patch.object(
                server, "naver_get_report_detail",
                AsyncMock(return_value={"nid": "1", "pdf_url": self.FAKE_PDF}),
            ),
        ]
        if extract is not None:
            self._ctxs.append(
                patch.object(pdfx, "fetch_and_extract", AsyncMock(return_value=extract))
            )
        for c in self._ctxs:
            c.start()

    def tearDown(self):
        for c in getattr(self, "_ctxs", []):
            c.stop()

    @staticmethod
    def _body(out: str) -> str:
        return out.split("RESULT_META_JSON_START")[0]

    async def test_unknown_mode_is_rejected_with_choices(self):
        from stock_mcp_server import server

        out = await server.get_report_content("1", mode="아무거나")
        self.assertIn("알 수 없는 mode", out)
        for name in ("summary", "full", "link"):
            self.assertIn(name, out)

    async def test_link_mode_does_not_download_pdf(self):
        """링크만 원하면 PDF를 받을 이유가 없다."""
        from unittest.mock import AsyncMock, patch

        from stock_mcp_server import server

        self._patch()
        with patch.object(pdfx, "fetch_and_extract", AsyncMock()) as fetcher:
            out = await server.get_report_content("1", mode="link")
        fetcher.assert_not_awaited()
        body = self._body(out)
        self.assertIn(self.FAKE_PDF, body)
        self.assertIn("리포트 페이지", body)

    async def test_korean_mode_aliases(self):
        from stock_mcp_server import server

        long_text = "가" * 12000
        self._patch(pdfx.PdfExtract(status="ok", text=long_text, pages=5, chars=12000))

        excerpt = self._body(await server.get_report_content("1", mode="요약"))
        whole = self._body(await server.get_report_content("1", mode="전문"))

        self.assertIn("리포트 발췌", excerpt)
        self.assertIn("리포트 전문", whole)
        self.assertLess(len(excerpt), len(whole))

    async def test_summary_caps_and_says_so(self):
        from stock_mcp_server import server

        self._patch(pdfx.PdfExtract(status="ok", text="나" * 12000, pages=5, chars=12000))
        body = self._body(await server.get_report_content("1"))  # 기본 = summary
        self.assertIn("앞 4,000자", body)
        self.assertIn('mode="full"', body)  # 더 보는 방법을 알려준다

    async def test_full_mode_returns_whole_body(self):
        from stock_mcp_server import server

        self._patch(pdfx.PdfExtract(status="ok", text="다" * 12000, pages=5, chars=12000))
        body = self._body(await server.get_report_content("1", mode="full"))
        self.assertIn("전체", body)
        # 고지문에도 '다'가 들어가므로 총 개수가 아니라 '끊기지 않은 12,000자 덩어리'를 본다.
        self.assertIn("다" * 12000, body)
        self.assertNotIn("생략", body)

    async def test_image_pdf_tunnels_to_links_in_any_mode(self):
        from stock_mcp_server import server

        self._patch(
            pdfx.PdfExtract(status="image_pdf", pages=9, chars=144, detail="이미지 PDF")
        )
        for mode in ("summary", "full"):
            body = self._body(await server.get_report_content("1", mode=mode))
            self.assertIn("우리가 못 읽은 것", body)
            self.assertIn(self.FAKE_PDF, body)
