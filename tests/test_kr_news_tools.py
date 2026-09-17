# -*- coding: utf-8 -*-
"""한국 종목 뉴스(get_news)·오늘 움직임 맥락(get_move_context) — 2026-09-17 위지트 문의로 추가.

지키는 것:
- 시각의 정확도를 이름표로 구분한다. 종목 태그 기사는 그대로, 이름검색 기사는 '약'(근사).
- 출처 하나가 실패해도 다른 출처는 낸다. 실패는 '없음'이 아니라 '모름'으로 적는다.
- 공시 0건은 "재료 없음"이 아니라고 결과 본문이 말한다.
- 검색 HTML 에서 기사 표식이 하나도 안 잡히면 0건이 아니라 파싱 실패다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import naver, server
from stock_mcp_server.market_clock import KST


NOW = datetime(2026, 9, 17, 19, 30, tzinfo=KST)


# ---------------------------------------------------------------- naver 층

def test_news_stamp_formats_minute_precision():
    assert naver._news_stamp("202609041323") == "2026-09-04 13:23"
    assert naver._news_stamp("2026-09-04") == ""
    assert naver._news_stamp(None) == ""


@pytest.mark.parametrize("shown, expected, basis", [
    ("4시간 전", "2026-09-17 15:30", "relative_display"),
    ("12분 전", "2026-09-17 19:18", "relative_display"),
    ("2일 전", "2026-09-15", "date_only"),
    ("2026.09.04.", "2026-09-04", "date_only"),
    ("어제", "", "unknown"),
])
def test_resolve_display_time(shown, expected, basis):
    assert naver.resolve_display_time(shown, NOW) == (expected, basis)


def _article(title, press, shown, url, body):
    return (
        '<div class="profile-info-title"><span class="x">%s</span></div>'
        '<div class="profile-info-subtext"><span class="x">%s</span></div>'
        '<a nocr="1" class="c" href="%s" target="_blank" data-heatmap-target=".tit">'
        '<span class="t">%s</span></a>'
        '<a nocr="1" class="c" href="%s" target="_blank" data-heatmap-target=".body">'
        '<span class="b">%s</span></a>'
    ) % (press, shown, url, title, url, body)


SEARCH_HTML = "<html>" + _article(
    "위지트, 대만 T사 다수 팹에 <mark>평가품</mark> 공급", "머니투데이", "10시간 전",
    "https://www.mt.co.kr/a", "반도체 부품 위지트가 …",
).replace(
    # 네이버뉴스에도 실린 기사는 시각 뒤에 "네이버뉴스" 칸이 하나 더 붙는다 — 시각을 잃으면 안 된다
    '<a nocr="1" class="c" href="https://www.mt.co.kr/a"',
    '<div class="profile-info-subtext"><span class="x">네이버뉴스</span></div>'
    '<a nocr="1" class="c" href="https://www.mt.co.kr/a"', 1,
) + _article(
    "코인주 향방 &#39;촉각&#39;", "뉴스토마토", "2026.09.04.",
    "https://www.newstomato.com/b", "위메이드도 내렸고…",
) + "</html>"


def test_parse_news_search_html_extracts_press_time_and_marks_basis():
    items = naver.parse_news_search_html(SEARCH_HTML, NOW)
    assert [it["title"] for it in items] == [
        "위지트, 대만 T사 다수 팹에 평가품 공급", "코인주 향방 '촉각'"]
    assert items[0]["press"] == "머니투데이"
    assert items[0]["datetime"] == "2026-09-17 09:30"
    assert items[0]["time_basis"] == "relative_display"
    assert items[0]["displayed_time"] == "10시간 전"
    assert items[0]["url"] == "https://www.mt.co.kr/a"
    assert items[0]["snippet"].startswith("반도체 부품")
    assert items[0]["source"] == "name_search"
    assert items[1]["time_basis"] == "date_only"
    assert items[1]["datetime"] == "2026-09-04"


def test_parse_news_search_html_no_result_page_is_empty():
    assert naver.parse_news_search_html("<html>검색결과가 없습니다</html>", NOW) == []


def test_parse_news_search_html_unknown_markup_is_parse_failure():
    with pytest.raises(naver.NaverParseError):
        naver.parse_news_search_html("<html><body>Npay 증권</body></html>", NOW)


def test_get_stock_news_flattens_groups_and_builds_article_url():
    payload = [{"total": 1, "items": [{
        "officeId": "081", "articleId": "0003676857", "officeName": "서울신문",
        "datetime": "202609041323", "title": "[서울데이터랩]코스닥 &amp; 혼조",
    }]}, {"total": 0, "items": []}]
    with patch.object(naver, "_api_json", AsyncMock(return_value=payload)):
        items = asyncio.run(naver.get_stock_news.__wrapped__("036090"))
    assert items == [{
        "title": "[서울데이터랩]코스닥 & 혼조", "press": "서울신문",
        "datetime": "2026-09-04 13:23", "time_basis": "article",
        "url": "https://n.news.naver.com/article/081/0003676857",
        "snippet": "", "source": "stock_tag",
    }]


# ---------------------------------------------------------------- 도구 층

TAGGED = [{"title": "비트코인 회복…위지트 17% 급등", "press": "뉴스1",
           "datetime": "2026-09-04 10:20", "time_basis": "article",
           "url": "https://n.news.naver.com/article/421/1", "snippet": "", "source": "stock_tag"}]
SEARCHED = [
    {"title": "위지트, 대만 T사 다수 팹에 평가품 공급", "press": "머니투데이",
     "datetime": "2026-09-17 09:30", "time_basis": "relative_display",
     "displayed_time": "10시간 전", "url": "https://www.mt.co.kr/a",
     "snippet": "반도체 부품 위지트가", "source": "name_search"},
    # 태그 쪽과 같은 기사 — 태그 쪽(정확한 시각)이 남아야 한다
    {"title": "비트코인 회복…위지트 17% 급등", "press": "뉴스1",
     "datetime": "2026-09-04", "time_basis": "date_only",
     "displayed_time": "2026.09.04.", "url": "https://www.news1.kr/x",
     "snippet": "", "source": "name_search"},
]
PRICE = {"code": "036090", "name": "위지트", "price": 1367, "base_price": 1287, "change": 80,
         "open": 1299, "high": 1474, "low": 1296, "volume": 3076963,
         "price_session": "after_market", "quote_date": "2026-09-17"}
OHLCV = [{"date": f"202609{d:02d}", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 150_000}
         for d in range(1, 17)] + [{"date": "20260917", "open": 1299, "high": 1474, "low": 1296,
                                   "close": 1369, "volume": 3076963}]


def _meta(text: str) -> dict:
    import json
    start = text.index("RESULT_META_JSON_START") + len("RESULT_META_JSON_START")
    end = text.index("RESULT_META_JSON_END")
    return json.loads(text[start:end])


def _patched(**over):
    base = {
        "get_current_price": AsyncMock(return_value=PRICE),
        "naver_get_stock_news": AsyncMock(return_value=TAGGED),
        "naver_search_news": AsyncMock(return_value=SEARCHED),
        "get_ohlcv": AsyncMock(return_value=OHLCV),
        "naver_get_disclosure_list": AsyncMock(return_value=[]),
        "naver_get_reports": AsyncMock(return_value=[]),
        "_now_kst": lambda: NOW,
    }
    base.update(over)
    return patch.multiple(server, **base)


def test_get_news_merges_sources_dedups_and_labels_time_precision():
    with _patched():
        text = asyncio.run(server.get_news("036090"))
    assert "**오늘 기사 1건**" in text
    assert "약 2026-09-17 09:30 (네이버 표시 '10시간 전')" in text
    assert "이름검색 · 제목 언급" in text
    # 중복 기사는 한 번, 그리고 태그 쪽(정확한 시각)으로 남는다
    assert text.count("비트코인 회복…위지트 17% 급등") == 1
    assert "2026-09-04 10:20 · 뉴스1" in text
    meta = _meta(text)
    assert meta["data_completeness"] == rmeta.COMPLETE
    assert meta["news_sources"] == {"stock_tag": "ok:1", "name_search": "ok:2"}
    assert meta["entity"]["name"] == "위지트"


def test_get_news_partial_failure_is_marked_not_silent():
    with _patched(naver_search_news=AsyncMock(side_effect=naver.NaverParseError("구조 변경"))):
        text = asyncio.run(server.get_news("036090"))
    assert "출처 '이름검색' 조회 실패(NaverParseError)" in text
    assert "없음이 아니라 모름" in text
    assert "2026-09-04 10:20 · 뉴스1" in text          # 살아남은 출처는 낸다
    meta = _meta(text)
    assert meta["data_completeness"] == rmeta.PARTIAL
    # 세션 경고(애프터마켓 등)는 시각에 따라 붙는다 — 실패 경고가 들어 있는지만 본다
    assert "뉴스 출처 조회 실패: name_search" in meta["warnings"]


def test_get_news_all_sources_failed_is_not_zero_articles():
    with _patched(naver_search_news=AsyncMock(side_effect=OSError("boom")),
                  naver_get_stock_news=AsyncMock(side_effect=OSError("boom"))):
        text = asyncio.run(server.get_news("036090"))
    assert "기사가 없는 것이 아니라 조회 실패" in text
    assert _meta(text)["data_completeness"] == rmeta.NONE


def test_get_news_today_only_filters_by_korean_date():
    with _patched():
        text = asyncio.run(server.get_news("036090", today_only=True))
    assert "평가품 공급" in text
    assert "비트코인 회복" not in text


def test_get_news_rejects_name_instead_of_code():
    assert "6자리" in asyncio.run(server.get_news("위지트"))


def test_get_move_context_orders_today_and_says_no_disclosure_is_not_no_catalyst():
    with _patched():
        text = asyncio.run(server.get_move_context("036090"))
    assert "## 오늘 움직임 맥락 — 위지트 (036090) · 2026-09-17" in text
    assert "현재가 1,367원 [KRX 애프터마켓], 기준가 대비 +80원 (+6.22%) (기준가 1,287원)" in text
    assert "거래량 3,076,963주 — 최근 16거래일 평균(150,000주)의 20.5배" in text
    assert "### 오늘 시각순 — 기사" in text
    assert "약 2026-09-17 09:30" in text
    assert "### 최근 기사 (오늘 제외, 최신 5건)" in text
    assert "없음 (조회 성공, 0건) — 재료 없음이 아닙니다" in text
    assert "원인 판정 아님" in text
    meta = _meta(text)
    assert meta["data_completeness"] == rmeta.COMPLETE
    assert meta["sections"]["disclosure"] == "ok"
    assert meta["today_news_count"] == 1
    assert meta["price_session"] == "after_market"


def test_get_move_context_partial_failures_keep_other_sections():
    with _patched(naver_get_disclosure_list=AsyncMock(side_effect=naver.NaverParseError("410")),
                  get_ohlcv=AsyncMock(side_effect=OSError("net"))):
        text = asyncio.run(server.get_move_context("036090"))
    assert "- 조회 실패(NaverParseError) — 없음이 아니라 모름" in text
    assert "거래량 3,076,963주" in text                  # 일봉이 없으면 배수만 빠진다
    assert "평균(" not in text
    assert "⚠️ 조회 실패: 일봉(OSError), 공시(NaverParseError)" in text
    meta = _meta(text)
    assert meta["data_completeness"] == rmeta.PARTIAL
    assert meta["sections"]["ohlcv"] == "failed"
    assert meta["sections"]["disclosure"] == "failed"


def test_get_move_context_price_failure_still_returns_news():
    with _patched(get_current_price=AsyncMock(side_effect=OSError("net"))):
        text = asyncio.run(server.get_move_context("036090"))
    assert "- 조회 실패(OSError) — 시세 없음이 아니라 모름" in text
    # 이름을 못 얻어 이름검색은 건너뛰지만 태그 기사는 낸다
    assert "비트코인 회복…위지트 17% 급등" in text
    meta = _meta(text)
    assert meta["sections"]["news"]["name_search"] == "skipped:no_name"
    assert meta["data_completeness"] == rmeta.PARTIAL


def test_instructions_open_with_routing_table_within_512_chars():
    head = (server.mcp.instructions or "")[:512]
    assert "get_move_context" in head
    assert "get_news" in head
    assert "search" in head
