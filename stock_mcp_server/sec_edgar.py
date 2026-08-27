"""SEC EDGAR 직접 연결 - 공시 목록·원문·구조화 추출 (SL-04).

yfinance 의 filings 는 Yahoo 중계 목록이라 accession number·report date·원문이
없고, 클래스주 티커(GOOGL)가 발행사에 연결되지 않는 일이 있었다(실측: GOOGL 은
"공시 정보 없음", GOOG 는 15건). SEC 는 발행사를 티커가 아니라 CIK 로 식별하므로
여기서는 티커를 CIK 로 정규화한 뒤 submissions API 를 1차 출처로 쓴다.

- 목록: https://data.sec.gov/submissions/CIK##########.json
- 원문: https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}
- 티커 -> CIK: https://www.sec.gov/files/company_tickers.json

SEC 는 User-Agent 에 연락처를 요구한다. 개인 정보 대신 저장소 URL 을 쓴다.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from html import unescape

import httpx

from stock_mcp_server import __version__

# SEC 공정접근 정책은 User-Agent 에 **이메일 형태의 연락처**를 요구한다.
# 실측(2026-08-27): URL 만 적으면 403, 이메일이 있으면 200. 단 UA 안에
# "github.com" 이 들어가면 그 자체로 403 이라 noreply 주소는 못 쓴다.
# 기본값은 공개 저장소 커밋에 이미 공개된 메인테이너 주소이고, 운영 연락처는
# STOCKLENS_SEC_CONTACT 환경변수로 바꿀 수 있다.
_SEC_CONTACT = os.environ.get("STOCKLENS_SEC_CONTACT", "whdqja216772@gmail.com")
_UA = {
    "User-Agent": f"StockLens/{__version__} ({_SEC_CONTACT})",
    "Accept-Encoding": "gzip, deflate",
}
_TIMEOUT = 20.0

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# 티커 맵은 하루 한 번이면 충분하다(1만 행). 실패는 캐시하지 않는다.
_ticker_cache: dict = {"at": 0.0, "map": None}
_TICKER_TTL = 24 * 3600

FIND_MAX_MATCHES = 5
FIND_CONTEXT_CHARS = 300


class SecFetchError(RuntimeError):
    """SEC 조회 실패. '데이터 없음'과 구분해야 하는 상태다."""


async def _get_json(url: str) -> dict:
    async with httpx.AsyncClient(headers=_UA, timeout=_TIMEOUT,
                                 follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _get_text(url: str) -> str:
    async with httpx.AsyncClient(headers=_UA, timeout=_TIMEOUT,
                                 follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def load_ticker_map() -> dict[str, dict]:
    """티커 -> {cik, title}. 같은 CIK 의 티커들(클래스주)도 함께 담는다."""
    now = time.monotonic()
    if _ticker_cache["map"] is not None and now - _ticker_cache["at"] < _TICKER_TTL:
        return _ticker_cache["map"]
    try:
        raw = await _get_json(TICKER_MAP_URL)
    except Exception as e:
        raise SecFetchError(f"티커 맵 조회 실패: {type(e).__name__}") from e
    by_ticker: dict[str, dict] = {}
    by_cik: dict[int, list[str]] = {}
    for row in raw.values():
        ticker = str(row.get("ticker") or "").upper()
        cik = row.get("cik_str")
        if not ticker or not isinstance(cik, int):
            continue
        by_ticker[ticker] = {"cik": cik, "title": row.get("title") or ""}
        by_cik.setdefault(cik, []).append(ticker)
    for ticker, info in by_ticker.items():
        info["issuer_tickers"] = by_cik[info["cik"]]
        # 파일 순서가 앞선 티커를 대표로 둔다 (SEC 목록 관례상 주 클래스가 먼저).
        info["primary_ticker"] = by_cik[info["cik"]][0]
    _ticker_cache["map"] = by_ticker
    _ticker_cache["at"] = now
    return by_ticker


async def resolve_issuer(ticker: str) -> dict | None:
    """티커 -> 발행사(CIK 중심). 못 찾으면 None - 조회 실패와 다르다.

    GOOGL 과 GOOG 는 같은 발행사(CIK 1652044)다. 요청 티커는 보존해서
    돌려주고, 조회는 CIK 로 한다.
    """
    requested = (ticker or "").strip().upper()
    if not requested:
        return None
    table = await load_ticker_map()
    info = table.get(requested)
    if info is None:
        return None
    return {
        "requested_ticker": requested,
        "cik": info["cik"],
        "name": info["title"],
        "primary_ticker": info["primary_ticker"],
        "issuer_tickers": info["issuer_tickers"],
    }


async def get_submissions(cik: int) -> dict:
    """발행사의 최근 공시 목록(SEC submissions).

    반환: {"name", "rows": [...], "recent_count", "has_older"}
    rows 원소: form / filing_date / report_date / accession / primary_document /
    primary_doc_description / items(8-K 항목).
    """
    try:
        data = await _get_json(SUBMISSIONS_URL.format(cik=cik))
    except Exception as e:
        raise SecFetchError(f"submissions 조회 실패(CIK {cik}): {type(e).__name__}") from e
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    rows = []
    for i in range(len(forms)):
        def g(key):
            values = recent.get(key) or []
            return values[i] if i < len(values) else None
        rows.append({
            "form": g("form"),
            "filing_date": g("filingDate"),
            "report_date": g("reportDate") or None,
            "accession": g("accessionNumber"),
            "primary_document": g("primaryDocument"),
            "primary_doc_description": g("primaryDocDescription"),
            "items": g("items") or None,
        })
    return {
        "name": data.get("name"),
        "tickers": data.get("tickers") or [],
        "rows": rows,
        "recent_count": len(rows),
        # recent 는 최근 1000건까지다. 그보다 오래된 공시는 별도 파일에 있고
        # 여기서는 세지 않는다 - 전체 건수를 안다고 말하면 안 된다.
        "has_older": bool((data.get("filings") or {}).get("files")),
    }


def filing_url(cik: int, accession: str, document: str) -> str:
    return ARCHIVE_URL.format(cik=cik, acc=(accession or "").replace("-", ""),
                              doc=document or "")


async def fetch_filing_index(cik: int, accession: str) -> list[dict]:
    """공시 패키지 안의 문서 목록(본문 + exhibit)."""
    url = ARCHIVE_URL.format(cik=cik, acc=(accession or "").replace("-", ""),
                             doc="index.json")
    try:
        data = await _get_json(url)
    except Exception as e:
        raise SecFetchError(f"filing index 조회 실패: {type(e).__name__}") from e
    items = ((data.get("directory") or {}).get("item")) or []
    return [{"name": it.get("name"), "size": it.get("size")}
            for it in items if it.get("name")]


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(html: str) -> str:
    """SEC 문서 HTML -> 검색 가능한 텍스트. 표 셀 사이 공백은 한 칸으로 모은다."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<(?:br|/p|/tr|/div|/li)[^>]*>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


async def fetch_filing_text(cik: int, accession: str, document: str) -> str:
    try:
        html = await _get_text(filing_url(cik, accession, document))
    except Exception as e:
        raise SecFetchError(f"원문 조회 실패({document}): {type(e).__name__}") from e
    return html_to_text(html)


def find_all_matches(text: str, keyword: str) -> list[int]:
    """키워드의 모든 위치. 세는 것과 보여주는 것을 나눈다(DL-02 와 같은 계약)."""
    if not keyword:
        return []
    lower, kw = text.lower(), keyword.lower()
    out, pos = [], 0
    while True:
        idx = lower.find(kw, pos)
        if idx < 0:
            return out
        out.append(idx)
        pos = idx + len(keyword)


def match_snippets(text: str, keyword: str, positions: list[int]) -> list[dict]:
    snippets = []
    for idx in positions:
        start = max(0, idx - FIND_CONTEXT_CHARS)
        end = min(len(text), idx + len(keyword) + FIND_CONTEXT_CHARS)
        snippet = text[start:idx] + "**" + text[idx:idx + len(keyword)] + "**" + text[idx + len(keyword):end]
        snippets.append({"pos": idx, "snippet": ("…" if start else "") + snippet.strip()
                                                + ("…" if end < len(text) else "")})
    return snippets


# ---------------------------------------------------------------------------
# 구조화 추출 - 희석·계약 (요구 5)
#
# 원문에 있는 문장을 범주별로 **찾아 보여줄 뿐** 숫자를 만들지 않는다.
# 범주가 안 잡히면 "미확인"이다 - 없다는 뜻이 아니다(요구 7).
# ---------------------------------------------------------------------------

DILUTION_PATTERNS: dict[str, list[str]] = {
    "기본주식수": [
        r"shares?\s+of\s+(?:our\s+|the\s+)?(?:Class\s+[A-Z]\s+)?common\s+stock\s+(?:were\s+|was\s+)?"
        r"(?:issued\s+and\s+)?outstanding",
        r"outstanding\s+shares?\s+of\s+(?:Class\s+[A-Z]\s+)?common\s+stock",
    ],
    "확정발행": [
        r"(?:issued|sold|agreed\s+to\s+(?:issue|sell))[^.]{0,120}shares",
        r"shares[^.]{0,80}(?:in\s+this\s+offering|offered\s+hereby)",
    ],
    "잠재발행_전환": [
        r"conversion\s+price", r"convertible\s+(?:notes?|preferred|securities)",
        r"upon\s+conversion",
    ],
    "잠재발행_워런트": [
        r"warrants?\s+to\s+purchase", r"exercise\s+price", r"exercisable",
    ],
    "리픽싱_조정": [
        r"anti-?dilution", r"full\s+ratchet", r"price\s+reset",
        r"adjust(?:ed|ment)s?[^.]{0,80}conversion\s+price",
        r"conversion\s+price[^.]{0,80}adjust",
    ],
    "자금용도": [
        r"use\s+of\s+proceeds", r"net\s+proceeds[^.]{0,120}",
    ],
}

CONTRACT_PATTERNS: dict[str, list[str]] = {
    "계약금액": [
        r"aggregate\s+(?:purchase\s+price|value|consideration)[^.]{0,120}",
        r"total\s+(?:contract\s+)?value[^.]{0,120}",
        r"purchase\s+price\s+of[^.]{0,120}",
        r"\$[\d,.]+(?:\s?(?:million|billion))?[^.]{0,80}(?:agreement|contract|order)",
    ],
    "기간": [
        r"(?:initial\s+)?term\s+of\s+(?:the|this)\s+agreement[^.]{0,120}",
        r"term\s+expir(?:es|ing)[^.]{0,100}",
        r"(?:through|until)\s+(?:December|January|February|March|April|May|June|July|"
        r"August|September|October|November)\s+\d{1,2},?\s+20\d{2}",
    ],
    "해지조항": [
        r"terminat(?:e|ed|ion)[^.]{0,140}",
    ],
    "최소구매": [
        r"minimum\s+(?:purchase|order|commitment|quantity)[^.]{0,120}",
        r"take[-\s+]or[-\s+]pay",
    ],
    "상대방_비공개": [
        r"confidential\s+treatment", r"\[\*+\]",
        r"(?:customer|counterparty)[^.]{0,80}(?:not\s+(?:been\s+)?disclosed|undisclosed)",
    ],
}


def extract_facts(text: str, patterns: dict[str, list[str]],
                  *, max_per_category: int = 3) -> dict:
    """범주별로 근거 문장을 찾는다. 못 찾은 범주는 unconfirmed 로 남긴다."""
    found: dict[str, list[dict]] = {}
    unconfirmed: list[str] = []
    for category, pats in patterns.items():
        hits: list[dict] = []
        total = 0
        for pat in pats:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                total += 1
                if len(hits) >= max_per_category:
                    continue
                start = max(0, m.start() - 160)
                end = min(len(text), m.end() + 160)
                hits.append({"pos": m.start(),
                             "snippet": ("…" if start else "") + text[start:end].strip()
                                        + ("…" if end < len(text) else "")})
        if hits:
            hits.sort(key=lambda h: h["pos"])
            found[category] = {"total_matches": total, "shown": hits[:max_per_category]}
        else:
            unconfirmed.append(category)
    return {"found": found, "unconfirmed": unconfirmed}


def extract_dilution_facts(text: str) -> dict:
    return extract_facts(text, DILUTION_PATTERNS)


def extract_contract_facts(text: str) -> dict:
    return extract_facts(text, CONTRACT_PATTERNS)
