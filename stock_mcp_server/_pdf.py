"""증권사 리포트 PDF 본문 추출.

네이버 증권 리포트 페이지는 본문 요약(td.view_cnt)과 함께 원문 PDF 링크를 준다.
요약은 500자 남짓이라 목표가 근거·실적 추정 같은 알맹이가 잘린다. 링크는 이미
`get_report_detail` 이 뽑고 있으므로, 그 PDF를 받아 본문을 읽으면 추가 스크래핑
없이 정보량이 크게 늘어난다(실측 평균 13,000자, 요약 대비 약 26배).

**증권사마다 PDF 만드는 방식이 다르다.** 2026-08-17 실측(13개 증권사):

    텍스트 PDF  11곳 — 키움·현대차·SK·하나·교보·대신·유안타·한화·유진·DS·iM
                       (9,745 ~ 19,681자 추출)
    이미지 PDF   1곳 — 미래에셋 (p1 폰트 0개 / 이미지 516개, 136자만 추출)
    PDF 링크 없음 1곳 — 신한투자

이미지 PDF에서 몇 글자 긁어 놓고 "본문"이라고 내보내면, 사용자는 그게 리포트
전체인 줄 안다. 그래서 추출량이 기준에 못 미치면 본문을 주지 않고 **이미지
PDF라 못 읽었다고 말한다** — 내용이 없는 것과 우리가 못 읽은 것은 다르다.

pdftotext 계열은 쓰지 않는다. 한글이 통째로 날아간 전례가 있고, 실측에서
pypdf 만 한글이 살아 나왔다.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import httpx

from stock_mcp_server._http import get_client

# 이 아래면 '본문을 읽어냈다'고 볼 수 없다. 실측상 텍스트 PDF는 9,700자 이상,
# 이미지 PDF는 136자로 갈려서 그 사이 어디를 잡아도 되지만 넉넉히 둔다.
TEXT_MIN_CHARS = 2000

# 리포트 PDF는 보통 1MB 안팎, 이미지 기반은 4MB까지 간다. 그 위는 받지 않는다.
MAX_PDF_BYTES = 20 * 1024 * 1024

# PDF는 본문 페이지보다 오래 걸린다(수 MB). 기본 8초로는 자주 끊긴다.
PDF_TIMEOUT_SEC = 30.0


@dataclass(frozen=True)
class PdfExtract:
    """PDF 추출 결과.

    status:
        "ok"        — 본문을 읽었다
        "image_pdf" — PDF는 받았지만 글자가 거의 없다(이미지로 렌더링된 리포트)
        "too_large" — 크기 상한 초과로 받지 않았다
        "failed"    — 내려받기·파싱 실패
    """

    status: str
    text: str = ""
    pages: int = 0
    chars: int = 0
    size_bytes: int = 0
    detail: str = ""


def extract_text(raw: bytes) -> PdfExtract:
    """PDF 바이트에서 본문 텍스트를 뽑는다. 네트워크를 타지 않는 순수 함수."""
    if not raw:
        return PdfExtract(status="failed", detail="빈 응답")
    if not raw.startswith(b"%PDF-"):
        return PdfExtract(
            status="failed",
            size_bytes=len(raw),
            detail="PDF 형식이 아닙니다 (링크가 바뀌었을 수 있습니다).",
        )

    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                # 한 페이지가 깨져도 나머지는 살린다. 몇 페이지를 잃었는지는
                # chars 로 드러난다.
                continue
        text = "\n".join(parts).strip()
        pages = len(reader.pages)
    except Exception as e:  # 암호화·손상 PDF 등
        return PdfExtract(
            status="failed",
            size_bytes=len(raw),
            detail=f"{type(e).__name__}: {e}",
        )

    if len(text) < TEXT_MIN_CHARS:
        return PdfExtract(
            status="image_pdf",
            pages=pages,
            chars=len(text),
            size_bytes=len(raw),
            detail=(
                f"{pages}쪽에서 {len(text):,}자만 추출됐습니다 — 글자가 아니라 "
                f"이미지로 렌더링된 PDF입니다(일부 증권사가 이렇게 냅니다)."
            ),
        )

    return PdfExtract(
        status="ok", text=text, pages=pages, chars=len(text), size_bytes=len(raw)
    )


async def fetch_and_extract(url: str) -> PdfExtract:
    """PDF를 내려받아 본문을 뽑는다."""
    if not url:
        return PdfExtract(status="failed", detail="PDF 주소가 없습니다.")

    client = get_client()
    try:
        resp = await client.get(url, timeout=PDF_TIMEOUT_SEC)
    except httpx.HTTPError as e:
        return PdfExtract(status="failed", detail=f"{type(e).__name__}: {e}")

    if resp.status_code != 200:
        return PdfExtract(status="failed", detail=f"HTTP {resp.status_code}")

    raw = resp.content
    if len(raw) > MAX_PDF_BYTES:
        return PdfExtract(
            status="too_large",
            size_bytes=len(raw),
            detail=f"{len(raw) / 1e6:.1f}MB — 상한 {MAX_PDF_BYTES / 1e6:.0f}MB 초과",
        )
    return extract_text(raw)
