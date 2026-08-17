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
