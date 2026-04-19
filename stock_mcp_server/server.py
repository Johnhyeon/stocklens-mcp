"""한국 주식 데이터 MCP 서버.

네이버 증권에서 차트, 수급, 재무 데이터를 가져와
Claude에서 자연어로 분석할 수 있게 해줍니다.
"""

import functools
import re
from datetime import datetime
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP
from stock_mcp_server.naver import (
    search_stock as naver_search_stock,
    get_ohlcv,
    get_current_price,
    get_investor_flow,
    get_financials,
    get_market_index,
    list_themes as naver_list_themes,
    get_theme_stocks as naver_get_theme_stocks,
    list_sectors as naver_list_sectors,
    get_sector_stocks as naver_get_sector_stocks,
    get_volume_ranking as naver_get_volume_ranking,
    get_change_ranking as naver_get_change_ranking,
    get_market_cap_ranking as naver_get_market_cap_ranking,
    get_multi_stocks as naver_get_multi_stocks,
    get_multi_chart_stats as naver_get_multi_chart_stats,
    scan_stocks_to_snapshot as naver_scan_snapshot,
    get_etf_list as naver_get_etf_list,
    get_etf_detail as naver_get_etf_detail,
    get_consensus as naver_get_consensus,
    get_reports as naver_get_reports,
    get_report_detail as naver_get_report_detail,
    get_disclosure_list as naver_get_disclosure_list,
)
from stock_mcp_server._excel import (
    get_snapshot_dir,
    generate_filename,
    save_dataframe_to_excel,
    load_excel,
    apply_filters,
)
from stock_mcp_server._metrics import (
    track_metrics,
    load_metrics,
    summarize_metrics,
    get_metrics_file,
)
from stock_mcp_server._indicators import (
    compute_indicators,
    AVAILABLE_INDICATORS,
)
from stock_mcp_server._chart_html import render_chart_html, render_multi_chart_html
from stock_mcp_server import yfinance_source as us
import asyncio
import json
import pandas as pd


def safe_tool(func):
    """MCP 도구 함수의 예외를 사용자 친화적 메시지로 변환합니다."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            result = await func(*args, **kwargs)
        except httpx.TimeoutException:
            return "⚠️ 네이버 증권 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요."
        except httpx.ConnectError:
            return "⚠️ 네이버 증권에 연결할 수 없습니다. 인터넷 연결을 확인해주세요."
        except httpx.HTTPError as e:
            return f"⚠️ 네트워크 오류가 발생했습니다: {type(e).__name__}"
        except Exception as e:
            return (
                f"⚠️ 데이터 처리 중 오류가 발생했습니다: {type(e).__name__}\n"
                f"종목코드가 올바른지, 상장된 종목인지 확인해주세요."
            )
        return result

    return wrapper

mcp = FastMCP(
    "StockLens",
    instructions="""StockLens — 한국 주식 데이터를 네이버 증권에서 실시간 조회합니다.
종목코드(예: 005930)나 종목명(예: 삼성전자)으로 검색할 수 있습니다.
차트 데이터, 투자자 수급, 재무지표, 시장 지수를 제공합니다.

## 🚨 종목코드 규칙 (절대 원칙)

사용자가 6자리 코드가 아닌 **종목명**을 주면, **반드시 아래 순서를 지켜라**:

1. 먼저 `search` 또는 `search_stock` 도구로 종목명 조회
2. 검색 결과가 여러 개면 사용자에게 확인 요청
3. 검색 결과 없으면 "종목명을 찾을 수 없습니다. 6자리 코드를 알려주세요" 요청
4. **종목 코드 추측 절대 금지** — 학습 지식으로 "아마 XXXXXX일 것" 식 추론 금지
5. 종목 코드는 상장폐지·합병·재할당으로 바뀔 수 있음. 반드시 실시간 조회로 검증.

이 규칙을 어기면 잘못된 종목 분석으로 사용자를 오도할 수 있다.

## 도구 역할 구분
- `get_chart`: **OHLCV 시계열 데이터** (수치 분석·요약���, count 최소 120)
- `get_indicators`: **기술지표 판정값** (RSI/MACD/Phase 등 숫자+라벨)
- `get_price`: **현재가 스냅샷** (단일 시점, 시계열 아님)

## ETF 도구
- `get_etf_list`: ETF 목록 조회 + 카테고리 필터 + 정렬
- `get_etf_info`: ETF 상세 정보 (기초지수, 보수율, 구성종목, 수익률)
- ETF도 일반 종목 도구(`search`, `get_price`, `get_chart`)로 조회 가능

## 분석 도구
- `get_consensus`: 컨센서스 — 증권사 투자의견, 목표주가, 실적 추정치
- `get_reports`: 증권사 리포트 — 목록 + 본문 요약 + PDF 링크
- `get_disclosure`: 공시 목록 — DART 전자공시 제목/날짜

## 🇺🇸 US 주식 도구 (NYSE / NASDAQ · yfinance)

**티커 구분 규칙:**
- 6자리 영숫자 (예: `005930`) → 한국 주식 → `get_price`, `get_chart` 등
- 1~5자 알파벳 (예: `AAPL`, `TSLA`, `BRK.B`) → 미국 주식 → **`get_us_*` 도구 사용**

사용자가 "애플", "테슬라" 같은 한국어·회사명만 말하면 **추측 금지**. `get_us_search`로
먼저 검색해 티커를 확정한 뒤 다른 tool을 호출하세요.

### 탐색
- `get_us_search`: 종목명·티커 검색 (예: "Apple" → AAPL)
- `get_us_market`: 주요 지수 (S&P 500, Dow, Nasdaq, Russell 2000, VIX)
- `get_us_screener`: 프리셋 스크리너 (day_gainers/losers/most_actives/undervalued 등)
- `get_us_sector`: 섹터별 overview + top 20 기업

### 기본 데이터
- `get_us_price`: 현재가 + 전일대비 + 52주 고저 + 베타 + 시총
- `get_us_info`: 기업정보 (섹터·산업·사업요약)
- `get_us_chart`: OHLCV 시계열 (period/interval/prepost)
- `get_us_financials`: Valuation (P/E, PEG, P/B) + Profitability + Dividend 비율
- `get_us_financial_statement`: 재무제표 (income/balance/cash_flow, annual/quarterly)
- `get_us_multi_price`: 여러 티커 일괄 가격 스냅샷

### US 고유 정보
- `get_us_earnings`: 다음 실적발표일 + 최근 서프라이즈 이력
- `get_us_analyst`: 목표주가 + buy/hold/sell + 업·다운그레이드 + EPS/매출 추정치
- `get_us_dividends`: 배당 이력 + ex-date + yield + payout ratio
- `get_us_options`: 옵션 체인 (calls/puts, IV, OI) — Greeks 미포함
- `get_us_insider`: Form 4 내부자 거래 + 현재 내부자 명단
- `get_us_holders`: 기관 (13F) + 뮤추얼펀드 + 주주 비중 요약
- `get_us_short`: 공매도 % of float + days to cover (2~4주 stale 경고)
- `get_us_filings`: SEC 공시 목록 (10-K, 10-Q, 8-K) + EDGAR URL
- `get_us_news`: 최근 뉴스 헤드라인
- `get_us_etf_info`: ETF 전용 상세 (top holdings, 섹터 비중, 자산 배분)

⚠️ **데이터 제약:** Yahoo Finance는 최대 15분 지연. 실시간 호가창·다크풀 미지원.
프리/포스트 마켓은 `get_us_chart(prepost=True)` 사용.
""",
)


async def _search_impl(query: str) -> str:
    """공통 구현 — search와 search_stock 도구에서 공유."""
    results = await naver_search_stock(query)
    if not results:
        return f"'{query}'에 대한 검색 결과가 없습니다. 6자리 코드를 직접 알려주세요."

    lines = [f"검색 결과 ({len(results)}건):"]
    for r in results:
        market = r.get("market", "")
        suffix = f" [{market}]" if market else ""
        lines.append(f"  - {r['name']} ({r['code']}){suffix}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("search")
async def search(query: str) -> str:
    """종목검색 — 종목명 또는 종목코드로 한국 주식 종목을 조회합니다.
    "삼성전자 종목코드", "반도체 관련주", "005930 뭐야" 같은 질문에 사용합니다.

    ⚠️ 사용자가 종목명만 주고 코드를 모를 때 **반드시 이 도구를 먼저 호출**.
    종목 코드를 추측하지 말 것. 상장폐지·재할당으로 코드가 바뀔 수 있음.

    search_stock으로도 동일하게 호출 가능 (별명).

    Args:
        query: 검색할 종목명 또는 코드 (예: "삼성전자", "005930", "알멕")
    """
    return await _search_impl(query)


@mcp.tool()
@safe_tool
@track_metrics("search_stock")
async def search_stock(query: str) -> str:
    """종목코드조회 (stock lookup) — 한국 주식 종목명/코드 조회 전용 도구.

    `search`와 동일 기능. 도구 디스커버리에서 "stock"/"ticker"/"종목" 키워드로
    빠르게 매칭되도록 명확한 이름을 갖는 별명입니다.

    ⚠️ 종목명만 있고 6자리 코드를 모를 때 **이 도구를 먼저 호출**해야 합니다.
    코드 추측(guessing) 금지. 다른 도구(get_price, get_chart 등)에 잘못된 코드를
    넣으면 엉뚱한 종목이 조회됩니다.

    Args:
        query: 종목명(한/영) 또는 6자리 코드. 예: "알멕", "Samsung", "005930"

    Returns:
        매칭된 종목 리스트. 여러 개면 사용자에게 확인 요청 필요.
    """
    return await _search_impl(query)


@mcp.tool()
@safe_tool
@track_metrics("get_chart")
async def get_chart(code: str, timeframe: str = "day", count: int = 120) -> str:
    """캔들차트 OHLCV — 종목의 시계열 캔들 데이터(시가/고가/저가/종가/거래량, candlestick OHLCV).
    "삼성전자 일봉", "차트 보여줘", "3개월 주봉", "월봉 데이터", "캔들 시각화",
    "candlestick chart", "price history" 같은 질문에 사용합니다.

    ⭐ **차트 시각화의 유일한 진입점.** 아티팩트/플롯으로 차트를 그릴 땐 이 도구만 사용하고,
    **캔들 + 거래량만 렌더링**하세요. 지지·저항은 캔들 위에서 눈으로 판단합니다.
    이평선·RSI·MACD·볼린저 등 보조지표는 **사용자가 명시적으로 요청할 때만** 추가하고,
    그때도 get_indicators로 숫자만 받아 텍스트로 요약 — 한 차트에 여러 지표를 선으로
    얹어 "덕지덕지" 만들지 말 것.

    **기본값 120일 (약 6개월 거래일)**.

    Args:
        code: 종목코드 6자리 (예: "005930")
        timeframe: "day"(일봉), "week"(주봉), "month"(월봉)
        count: 가져올 봉 개수 (기본 120, 최대 500)
    """
    count = min(count, 500)
    data = await get_ohlcv(code, timeframe, count)
    if not data:
        return f"종목코드 {code}의 차트 데이터를 가져올 수 없습니다."

    tf_name = {"day": "일봉", "week": "주봉", "month": "월봉"}.get(timeframe, timeframe)
    lines = [f"종목 {code} {tf_name} 데이터 ({len(data)}개):", ""]
    lines.append("날짜 | 시가 | 고가 | 저가 | 종가 | 거래량")
    lines.append("---|---|---|---|---|---")
    for row in data:
        lines.append(
            f"{row['date']} | {row['open']:,} | {row['high']:,} | "
            f"{row['low']:,} | {row['close']:,} | {row['volume']:,}"
        )
    return "\n".join(lines)


# --- get_chart_html 비활성화 (v0.2.5~) ---
# 차트 HTML 렌더링 도구. 필요 시 주석 해제.
#
# @mcp.tool()
# @safe_tool
# @track_metrics("get_chart_html")
# async def get_chart_html(
#     code: str,
#     timeframe: str = "day",
#     count: int = 120,
#     timeframes: list[str] | None = None,
#     show_sr: bool = True,
#     custom_sr: list[dict] | None = None,
# ) -> str:
#     """차트HTML — 완성된 캔들+거래량 차트 HTML 반환."""
#     info = await get_current_price(code)
#     name = info.get("name", code) if info else code
#     if timeframes:
#         frame_defaults = {"day": 120, "week": 52, "month": 24}
#         frames = []
#         for tf in timeframes:
#             c = frame_defaults.get(tf, 120)
#             ohlcv = await get_ohlcv(code, tf, c)
#             if ohlcv:
#                 frames.append({"timeframe": tf, "ohlcv": ohlcv})
#         if not frames:
#             return f"종목코드 {code}의 차트 데이터를 가져올 수 없습니다."
#         html = render_multi_chart_html(code, name, frames, show_sr=show_sr, custom_sr=custom_sr)
#     else:
#         count = min(count, 500)
#         ohlcv = await get_ohlcv(code, timeframe, count)
#         if not ohlcv:
#             return f"종목코드 {code}의 차트 데이터를 가져올 수 없습니다."
#         html = render_chart_html(
#             code, name, ohlcv,
#             timeframe=timeframe, show_sr=show_sr, custom_sr=custom_sr,
#         )
#     MAX_INLINE = 60_000
#     if len(html) <= MAX_INLINE:
#         return html
#     chart_dir = get_snapshot_dir() / "charts"
#     chart_dir.mkdir(parents=True, exist_ok=True)
#     tf_label = "-".join(timeframes) if timeframes else timeframe
#     fname = chart_dir / f"{code}_{tf_label}.html"
#     fname.write_text(html, encoding="utf-8")
#     size_kb = len(html) / 1024
#     return (
#         f"✓ 차트 HTML 생성 완료 ({size_kb:.0f}KB — 멀티프레임이라 파일로 저장)\n"
#         f"경로: {fname}\n\n"
#         f"사용자에게 이 경로를 안내해주세요. 브라우저에서 직접 열면 "
#         f"캔들차트 + 거래량 + S/R 오버레이가 표시됩니다."
#     )


@mcp.tool()
@safe_tool
@track_metrics("get_price")
async def get_price(code: str) -> str:
    """현재가 — 종목의 현재 시세 스냅샷 (오늘 하루치 OHLC + 거래량).
    "삼성전자 지금 얼마", "현재가", "오늘 시세", "주가 알려줘" 같은 질문에 사용합니다.

    ⚠️ **단일 시점 스냅샷**. 과거 시계열 아님. 차트/히스토리 필요 시 get_chart 사용.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    data = await get_current_price(code)
    if not data or "price" not in data:
        return f"종목코드 {code}의 현재가를 가져올 수 없습니다."

    lines = [
        f"종목: {data.get('name', code)} ({code})",
        f"현재가: {data['price']:,}원",
    ]
    if "change" in data:
        sign = "+" if data["change"] > 0 else ""
        lines.append(f"전일대비: {sign}{data['change']:,}원")
    if "open" in data:
        lines.append(f"시가: {data['open']:,}원")
    if "high" in data:
        lines.append(f"고가: {data['high']:,}원")
    if "low" in data:
        lines.append(f"저가: {data['low']:,}원")
    if "volume" in data:
        lines.append(f"거래량: {data['volume']:,}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_flow")
async def get_flow(code: str, days: int = 20) -> str:
    """투자자수급 — 투자자별 매매동향 (기관/외국인 순매매 주식 수)을 가져옵니다.
    네이버 증권 소스 특성상 **개인 순매매는 제공되지 않습니다** (기관·외국인만).
    "외국인 수급", "기관 순매수", "수급 분석", "누가 사고 있어" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
        days: 조회할 일수 (기본 20일)
    """
    days = min(days, 60)
    data = await get_investor_flow(code, days)
    if not data:
        return f"종목코드 {code}의 수급 데이터를 가져올 수 없습니다."

    lines = [f"종목 {code} 투자자별 매매동향 ({len(data)}일):", ""]
    lines.append("날짜 | [주] 기관 순매매 | [주] 외국인 순매매 | [참고] 종가 | [참고] 거래량")
    lines.append("---|---|---|---|---")
    for row in data:
        lines.append(
            f"{row['date']} | {row['institutional']:,} | {row['foreign']:,} | "
            f"{row['close']:,} | {row['volume']:,}"
        )

    # 합계
    total_inst = sum(r["institutional"] for r in data)
    total_frgn = sum(r["foreign"] for r in data)
    lines.append("")
    lines.append(f"합계 | {total_inst:,} | {total_frgn:,} | - | -")
    lines.append("")
    lines.append(
        "※ [주] 필드는 이 도구의 주 목적 (수급 분석). "
        "[참고] 종가·거래량은 편의 제공이며, **가격 차트·시계열 분석 소스로 사용 금지**. "
        "차트는 get_chart, 현재가는 get_price 사용."
    )

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_financial")
async def get_financial(code: str) -> str:
    """재무지표 — 종목의 주요 재무지표(PER, PBR, 시가총액 등)를 가져옵니다.
    "PER", "PBR", "재무제표", "시가총액", "저평가" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    data = await get_financials(code)
    if not data:
        return f"종목코드 {code}의 재무지표를 가져올 수 없습니다."

    periods = data.get("_periods") or {}
    annual = periods.get("annual", [])
    quarterly = periods.get("quarterly", [])

    def _fmt(p: list[str], v: list[str]) -> str:
        parts = []
        for pp, vv in zip(p, v):
            if not pp and not vv:
                continue
            if not vv:
                vv = "추정치 없음" if "(E)" in pp else "데이터 없음"
            parts.append(f"{pp or '?'}={vv}")
        return " | ".join(parts)

    lines = [f"종목: {data.get('name', code)} ({code})", ""]
    for key, value in data.items():
        if key in ("code", "name", "_periods"):
            continue
        if isinstance(value, list):
            if annual or quarterly:
                a_vals = value[: len(annual)]
                q_vals = value[len(annual) : len(annual) + len(quarterly)]
                lines.append(f"{key}:")
                if annual:
                    lines.append(f"  [연간] {_fmt(annual, a_vals)}")
                if quarterly:
                    lines.append(f"  [분기] {_fmt(quarterly, q_vals)}")
            else:
                lines.append(f"{key}: {' | '.join(value)}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_index")
async def get_index() -> str:
    """시장지수 — KOSPI, KOSDAQ 지수 현재값을 가져옵니다.
    "코스피", "코스닥", "시장 지수", "오늘 시장 어때" 같은 질문에 사용합니다.
    """
    data = await get_market_index()
    if not data:
        return "시장 지수를 가져올 수 없습니다."

    lines = ["시장 지수:"]
    for item in data:
        value = item.get("value", "-")
        if isinstance(value, float):
            value = f"{value:,.2f}"
        change = item.get("change_raw") or ""
        rate = item.get("change_rate")
        rate_str = f" ({rate:+.2f}%)" if rate is not None else ""
        lines.append(f"  {item['index']}: {value}{rate_str} {change}".rstrip())
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("list_themes")
async def list_themes(page: int = 1) -> str:
    """테마목록 — 네이버 증권의 테마 목록을 가져옵니다.
    "어떤 테마가 있어?", "테마 리스트", "오늘 강세 테마" 같은 질문에 사용합니다.
    총 7페이지가 있으며 한 페이지당 40개 테마가 있습니다. 전일대비 등락률 순으로 정렬되어 있어요.

    Args:
        page: 페이지 번호 (1~7, 기본 1)
    """
    page = max(1, min(page, 7))
    themes = await naver_list_themes(page=page)
    if not themes:
        return f"페이지 {page}의 테마 목록을 가져올 수 없습니다."

    lines = [f"테마 목록 (page {page}, {len(themes)}개):", ""]
    lines.append("테마명 | 전일대비 | 최근3일 | 상승/보합/하락 | 주도주")
    lines.append("---|---|---|---|---")
    for t in themes:
        leaders = ", ".join(
            f"{ld['name']}({ld['code']})" if ld.get("code") else ld["name"]
            for ld in t["leaders"]
        )
        counts = f"{t['up_count']}/{t['flat_count']}/{t['down_count']}"
        lines.append(
            f"{t['name']} | {t['change_rate']} | {t['recent_3d_rate']} | {counts} | {leaders}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_theme_stocks")
async def get_theme_stocks(
    theme_name: str,
    count: int = 30,
    include_reason: bool = True,
) -> str:
    """테마종목 — 특정 테마에 속한 종목 리스트를 가져옵니다.
    "반도체 테마 종목", "2차전지 관련주", "AI 테마주" 같은 질문에 사용합니다.
    테마명 부분 매칭을 지원합니다.

    Args:
        theme_name: 테마명 (예: "2차전지", "AI", "반도체")
        count: 반환할 최대 종목 수 (기본 30)
        include_reason: 편입사유 포함 여부. False로 하면 토큰 대폭 절감.
                        "왜 이 테마에 들어갔는지" 필요 없으면 False 권장.
    """
    count = min(count, 50)
    result = await naver_get_theme_stocks(
        theme_name,
        count=count,
        include_reason=include_reason,
    )
    if not result.get("theme_id"):
        return (
            f"'{theme_name}' 테마를 찾을 수 없습니다. "
            f"list_themes로 전체 테마 목록을 먼저 확인해보세요."
        )

    stocks = result["stocks"]
    lines = [f"테마: {result['theme_name']} ({len(stocks)}개 종목)", ""]

    if include_reason:
        lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량 | 편입사유")
        lines.append("---|---|---|---|---|---")
        for s in stocks:
            reason = s.get("reason", "")
            lines.append(
                f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | "
                f"{s['volume']:,} | {reason}"
            )
    else:
        lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량")
        lines.append("---|---|---|---|---")
        for s in stocks:
            lines.append(
                f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | {s['volume']:,}"
            )

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("list_sectors")
async def list_sectors() -> str:
    """업종목록 — 네이버 증권의 업종(섹터) 목록을 가져옵니다.
    "업종별 현황", "섹터 리스트", "업종 등락률" 같은 질문에 사용합니다.
    약 79개 업종이 전일대비 등락률 순으로 정렬됩니다.
    """
    sectors = await naver_list_sectors()
    if not sectors:
        return "업종 목록을 가져올 수 없습니다."

    lines = [f"업종 목록 ({len(sectors)}개):", ""]
    lines.append("업종명 | 전일대비 | 종목수 | 상승/보합/하락")
    lines.append("---|---|---|---")
    for s in sectors:
        counts = f"{s['up_count']}/{s['flat_count']}/{s['down_count']}"
        lines.append(
            f"{s['name']} | {s['change_rate']} | {s['total_count']} | {counts}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_sector_stocks")
async def get_sector_stocks(sector_name: str, count: int = 30) -> str:
    """업종종목 — 특정 업종에 속한 종목 리스트를 가져옵니다.
    "통신장비 업종 종목", "반도체 업종", "제약 섹터 종목" 같은 질문에 사용합니다.
    업종명 부분 매칭을 지원합니다.

    Args:
        sector_name: 업종명 (예: "통신장비", "반도체", "제약")
        count: 반환할 최대 종목 수 (기본 30)
    """
    count = min(count, 50)
    result = await naver_get_sector_stocks(sector_name, count=count)
    if not result.get("sector_id"):
        return (
            f"'{sector_name}' 업종을 찾을 수 없습니다. "
            f"list_sectors로 전체 업종 목록을 먼저 확인해보세요."
        )

    stocks = result["stocks"]
    lines = [f"업종: {result['sector_name']} ({len(stocks)}개 종목)", ""]
    lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량")
    lines.append("---|---|---|---|---")
    for s in stocks:
        lines.append(
            f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | {s['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_volume_ranking")
async def get_volume_ranking(
    market: str = "ALL",
    count: int = 50,
    sort_by: str = "volume",
) -> str:
    """거래량/거래대금 순위 — 상위 종목을 가져옵니다.

    "거래량 많은 종목" → sort_by="volume" (기본, 주수 기준)
    "거래대금 많은 종목"/"거래 규모 큰 종목" → sort_by="trade_value" (원 기준)

    대형 고단가 종목(삼전·하이닉스 등)은 거래량(주수)이 작아도 거래대금은 클 수 있어
    스크리닝 시에는 sort_by="trade_value"가 더 적합한 경우가 많습니다.

    Args:
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL)
        count: 가져올 종목 수 (기본 50, 최대 500)
        sort_by: "volume"(거래량 주수) / "trade_value"(거래대금 원)
    """
    count = min(count, 500)
    ranks = await naver_get_volume_ranking(market=market, count=count, sort_by=sort_by)
    if not ranks:
        return f"{market} 거래량 순위를 가져올 수 없습니다."

    sort_label = "거래대금" if sort_by == "trade_value" else "거래량"
    lines = [f"{sort_label} 상위 ({market}, {len(ranks)}개, 정렬={sort_by}):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 거래량(주) | 거래대금(원)")
    lines.append("---|---|---|---|---|---|---")
    for r in ranks:
        tv = r.get("trade_value_krw", 0)
        # 거래대금 억원 단위 표시 (가독성)
        tv_billion = tv / 100_000_000
        lines.append(
            f"{r['rank']} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['volume']:,} | {tv_billion:,.1f}억"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_change_ranking")
async def get_change_ranking(
    direction: str = "up",
    market: str = "ALL",
    count: int = 50,
) -> str:
    """등락률순위 — 등락률 상위/하위 종목을 가져옵니다.
    "상한가 종목", "급등주", "급락주", "상승률 상위" 같은 질문에 사용합니다.

    Args:
        direction: "up"(상승률 상위) / "down"(하락률 상위)
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL)
        count: 가져올 종목 수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    ranks = await naver_get_change_ranking(direction=direction, market=market, count=count)
    if not ranks:
        return f"{direction} 등락률 순위를 가져올 수 없습니다."

    dir_label = "상승률 상위" if direction.lower() == "up" else "하락률 상위"
    lines = [f"{dir_label} ({market}, {len(ranks)}개):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 거래량")
    lines.append("---|---|---|---|---|---")
    for i, r in enumerate(ranks, 1):
        lines.append(
            f"{i} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_market_cap_ranking")
async def get_market_cap_ranking(market: str = "KOSPI", count: int = 50) -> str:
    """시가총액순위 — 시가총액 상위 종목을 가져옵니다.
    "대형주", "시가총액 TOP", "코스피 대장주" 같은 질문에 사용합니다.

    Args:
        market: "KOSPI" / "KOSDAQ" (기본 KOSPI, ALL 미지원)
        count: 가져올 종목 수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    ranks = await naver_get_market_cap_ranking(market=market, count=count)
    if not ranks:
        return f"{market} 시가총액 순위를 가져올 수 없습니다."

    lines = [f"시가총액 상위 ({market}, {len(ranks)}개):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 시가총액(억원)")
    lines.append("---|---|---|---|---|---")
    for r in ranks:
        lines.append(
            f"{r['rank']} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['market_cap_billion']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_multi_stocks")
async def get_multi_stocks(codes: list[str]) -> str:
    """벌크조회 — 여러 종목의 기본 정보(가격/등락률/거래량)를 한 번에 가져옵니다.
    "이 종목들 현재가 보여줘", "리스트 종목 시세 한번에" 같은 질문에 사용합니다.
    개별 get_price를 여러 번 호출하는 것보다 훨씬 토큰 효율적입니다.
    스크리닝 결과 N개 종목을 비교 분석할 때 필수 도구.

    Args:
        codes: 종목코드 리스트 (최대 30개, 예: ["005930", "000660", "005380"])
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."

    stocks = await naver_get_multi_stocks(codes)
    if not stocks:
        return "종목 정보를 가져올 수 없습니다."

    lines = [f"종목 정보 ({len(stocks)}개):", ""]
    lines.append("코드 | 종목명 | 현재가 | 전일대비 | 등락률 | 거래량")
    lines.append("---|---|---|---|---|---")
    for s in stocks:
        change = s["change"]
        change_str = f"{change:+,}" if change != 0 else "0"
        lines.append(
            f"{s['code']} | {s['name']} | {s['price']:,} | "
            f"{change_str} | {s['change_rate']} | {s['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_multi_chart_stats")
async def get_multi_chart_stats(codes: list[str], days: int = 260) -> str:
    """차트통계벌크 — 여러 종목의 **집계 통계**(최고가/최저가/현재가/낙폭)를 한 번에 병렬 조회.

    ⚠️ **시계열 도구 아님.** 기간 내 요약값(집계)만 반환. 캔들 차트·시계열 시각화는
    get_chart 별도 호출 필수. 이름에 "차트"가 들어 있지만 시계열 OHLCV를 주지 않음.

    ⭐ 스크리닝 필수 도구. 개별 get_chart 를 N번 호출하지 말고 이것 한 번으로 해결.

    각 종목의 지정 기간 내 (모두 집계값):
      - high/high_date: 최고가 + 그날 날짜
      - low/low_date: 최저가 + 그날 날짜
      - current_price: 현재가 (오늘 종가)
      - drawdown_pct: 현재가가 최고가 대비 얼마나 내렸는지 (음수)
      - recovery_pct: 현재가가 최저가에서 얼마나 올랐는지 (양수)
      - period_return_pct: 기간 시작 대비 수익률
      - avg_volume: 평균 거래량

    활용 예시:
      - "52주 고점 대비 30% 이상 하락한 종목 찾기" → days=260, drawdown_pct < -30 필터
      - "52주 신고가 근접 종목" → days=260, drawdown_pct > -5 필터
      - "가격 박스권 횡보 종목" → drawdown_pct와 recovery_pct 모두 작은 종목

    Args:
        codes: 종목코드 리스트 (최대 100개)
        days: 과거 조회 일수 (기본 260 = 52주)
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."
    if days < 10:
        days = 10
    if days > 500:
        days = 500

    stats = await naver_get_multi_chart_stats(codes, days=days)
    if not stats:
        return "차트 통계를 가져올 수 없습니다."

    lines = [f"차트 통계 ({len(stats)}개 종목, 최근 {days}일 집계):", ""]
    lines.append("코드 | 현재가 | 최고가(날짜) | 최저가(날짜) | 고점대비낙폭 | 기간수익률")
    lines.append("---|---|---|---|---|---")
    for s in stats:
        lines.append(
            f"{s['code']} | {s['current_price']:,} | "
            f"{s['high']:,}({s['high_date']}) | "
            f"{s['low']:,}({s['low_date']}) | "
            f"{s['drawdown_pct']:+.1f}% | "
            f"{s['period_return_pct']:+.1f}%"
        )
    lines.append("")
    lines.append(
        "※ 기간 **집계값**만 반환. 시계열 OHLCV 아님. "
        "**캔들 차트·시계열 시각화는 get_chart 별도 호출** 필수."
    )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_indicators")
async def get_indicators(
    code: str,
    days: int = 260,
    include: list[str] | None = None,
    timeframe: str = "day",
) -> str:
    """기술지표 — 단일 종목의 이평선·RSI·MACD·볼린저·스토캐스틱 등 종합 판정.

    ⚠️ **스크리닝·판정 전용 도구. 차트 시각화용 아님.**
    시각화(아티팩트/플롯)는 `get_chart`로 캔들+거래량만 그리고, 지지·저항은
    캔들 위에서 눈으로 판단하면 됩니다. 이 도구는 플레이북 조건 필터·상태 판정처럼
    **숫자 비교가 필요할 때만** 호출하세요.

    OHLCV 원본 대신 계산·판정 결과만 JSON으로 반환해 토큰을 절약합니다.

    Args:
        code: 종목코드 (예: "005930")
        days: 조회 일수 (기본 260, 최소 30, 최대 500)
        include: 계산할 지표 키 리스트. 기본 ["ma", "ma_phase", "volume", "candle"].
            스냅샷 지표: ma / ma_phase / ma_slope / ma_cross / rsi / macd / bollinger /
                       stochastic / obv / volume / position / candle
            구조 분석: support_resistance / volume_profile / price_channel
            (구조 분석은 days=500~750 등 긴 lookback 권장)
        timeframe: "day"(일봉) / "week"(주봉) / "month"(월봉). 분봉은 현재 미지원.

    ma_phase 값:
        0 완전역배열 / 1 단기상승꼬임 / 2 꼬임 / 3 단기하락꼬임 / 4 완전정배열
        ⚠️ 반환값의 `phase_label` 필드를 **그대로** 사용. 직접 "꼬임"을 타이핑하지 말 것 (토크나이저 오류 위험).
        ma_cross/macd.cross도 `type_label` 필드(골든크로스/데드크로스) 그대로 사용.
        bollinger는 `position` 필드 (상단 돌파/상단 근접/밴드 내/하단 근접/하단 이탈)

    support_resistance 반환:
        피벗 자동 추출 + 클러스터링 + 터치 횟수·일자·강도(weak/medium/strong).
        2~3년치 일봉 권장. 근거 없는 S/R 추정 제거 목적.

    volume_profile 반환:
        가격대별 누적 거래량, POC(최대 매물 집중), Value Area(70% 구간).

    price_channel 반환:
        Donchian 채널. Upper=N봉 고가, Lower=N봉 저가, 현재 위치 %.

    반환: JSON 문자열.
    """
    if not code:
        return "종목코드가 필요합니다."
    if include is None:
        include = ["ma", "ma_phase", "volume", "candle"]
    unknown = [k for k in include if k not in AVAILABLE_INDICATORS]
    if unknown:
        return (
            f"지원하지 않는 지표: {unknown}\n"
            f"사용 가능: {AVAILABLE_INDICATORS}"
        )
    days = max(30, min(days, 500))

    ohlcv = await get_ohlcv(code, timeframe=timeframe, count=days)
    if not ohlcv:
        return f"차트 데이터를 가져올 수 없습니다: {code}"

    result = compute_indicators(ohlcv, include)
    payload = {
        "code": code,
        "timeframe": timeframe,
        "days": days,
        "indicators": result,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
@safe_tool
@track_metrics("get_indicators_bulk")
async def get_indicators_bulk(
    codes: list[str],
    days: int = 260,
    include: list[str] | None = None,
    timeframe: str = "day",
) -> str:
    """기술지표벌크 — 여러 종목의 지표를 병렬 계산. 스크리닝 핵심 도구.

    ⚠️ **시계열/캔들 차트 아님.** 집계 판정값만 반환. 캔들 시각화는 get_chart 사용.

    최대 100개 종목의 이평선 Phase·RSI·MACD 등을 한 번에 판정합니다.
    get_indicators를 N번 부르지 말고 이걸로 일괄 처리.

    Args:
        codes: 종목코드 리스트 (최대 100개)
        days: 조회 일수 (기본 260)
        include: 지표 키 리스트 (기본 ["ma_phase", "volume"]). get_indicators 참조.
        timeframe: "day" / "week" / "month"

    반환: 코드별 지표 결과 JSON.
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."
    if include is None:
        include = ["ma_phase", "volume"]
    unknown = [k for k in include if k not in AVAILABLE_INDICATORS]
    if unknown:
        return (
            f"지원하지 않는 지표: {unknown}\n"
            f"사용 가능: {AVAILABLE_INDICATORS}"
        )
    codes = codes[:100]
    days = max(30, min(days, 500))

    async def one(code: str) -> tuple[str, dict]:
        try:
            ohlcv = await get_ohlcv(code, timeframe=timeframe, count=days)
            if not ohlcv:
                return code, {"error": "OHLCV 없음"}
            return code, compute_indicators(ohlcv, include)
        except Exception as e:
            return code, {"error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*[one(c) for c in codes])
    payload = {
        "timeframe": timeframe,
        "days": days,
        "include": include,
        "count": len(results),
        "results": {code: data for code, data in results},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
@safe_tool
@track_metrics("export_to_excel")
async def export_to_excel(
    data_type: str,
    code: str = "",
    days: int = 180,
    filename: str = "",
) -> str:
    """엑셀내보내기 — 단일 종목의 데이터를 Excel 파일로 저장합니다.

    Gemini/GPT 같은 다른 AI에 파일 업로드로 넘기거나,
    엑셀에서 직접 분석/차트 작성할 때 사용합니다.

    Args:
        data_type: "chart"(일봉 OHLCV) / "flow"(투자자별 수급) / "financial"(재무지표)
        code: 종목코드 6자리 (예: "005930")
        days: chart/flow의 경우 과거 일수 (기본 180)
        filename: 파일명 (비우면 자동 생성)

    Returns:
        저장된 파일 경로
    """
    if not code:
        return "종목코드가 필요합니다."

    if data_type == "chart":
        data = await get_ohlcv(code, "day", days)
        if not data:
            return f"차트 데이터를 가져올 수 없습니다: {code}"
        df = pd.DataFrame(data)
        prefix = f"chart_{code}"
        sheet = "OHLCV"

    elif data_type == "flow":
        data = await get_investor_flow(code, days)
        if not data:
            return f"수급 데이터를 가져올 수 없습니다: {code}"
        df = pd.DataFrame(data)
        prefix = f"flow_{code}"
        sheet = "Investor Flow"

    elif data_type == "financial":
        data = await get_financials(code)
        if not data:
            return f"재무지표를 가져올 수 없습니다: {code}"
        # dict → DataFrame 변환
        rows = [{"항목": k, "값": str(v)} for k, v in data.items() if k not in ("code",)]
        df = pd.DataFrame(rows)
        prefix = f"financial_{code}"
        sheet = "Financial"

    else:
        return f"지원하지 않는 data_type: {data_type}. 'chart', 'flow', 'financial' 중 선택."

    fname = filename or generate_filename(prefix)
    if not fname.endswith(".xlsx"):
        fname += ".xlsx"
    file_path = get_snapshot_dir() / fname
    saved = save_dataframe_to_excel(df, file_path, sheet_name=sheet)

    return (
        f"✓ Excel 파일 저장 완료\n"
        f"경로: {saved}\n"
        f"행 수: {len(df)}\n"
        f"컬럼: {', '.join(df.columns)}\n\n"
        f"💡 이 파일을 Gemini/ChatGPT에 업로드하면 다른 AI에서도 분석할 수 있어요."
    )


@mcp.tool()
@safe_tool
@track_metrics("scan_to_excel")
async def scan_to_excel(
    codes: list[str],
    days: int = 260,
    include_financial: bool = True,
    filename: str = "",
) -> str:
    """시장스캔 — 여러 종목의 기본정보+차트통계+재무지표를 한 번에 수집해 Excel로 저장.

    ⭐ 로컬 캐시 패턴: 한 번 스캔해두면 이후 query_excel로 즉시 반복 조회 가능.
    DB 없이도 HTS 같은 빠른 분석 경험을 제공합니다.

    사용 흐름:
      1. scan_to_excel(KOSPI 시총 100개 코드) → 파일 저장 (한 번)
      2. query_excel(파일경로, 조건) → 즉시 필터링 (반복)
      3. query_excel(파일경로, 다른 조건) → 즉시

    Args:
        codes: 종목코드 리스트 (최대 500개)
        days: 차트 통계 과거 일수 (기본 260 = 52주)
        include_financial: 재무지표(PER/PBR) 포함 여부
        filename: 파일명 (비우면 자동 생성)

    Returns:
        저장된 파일 경로 + 컬럼 목록
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."

    rows = await naver_scan_snapshot(codes, days=days, include_financial=include_financial)
    if not rows:
        return "데이터를 수집하지 못했습니다."

    df = pd.DataFrame(rows)
    fname = filename or generate_filename(f"snapshot_{len(df)}stocks")
    if not fname.endswith(".xlsx"):
        fname += ".xlsx"
    file_path = get_snapshot_dir() / fname

    saved = save_dataframe_to_excel(
        df,
        file_path,
        sheet_name="Snapshot",
        metadata={
            "days": days,
            "include_financial": include_financial,
            "requested_codes": len(codes),
        },
    )

    return (
        f"✓ 스냅샷 저장 완료 ({len(df)}개 종목)\n"
        f"경로: {saved}\n"
        f"컬럼: {', '.join(df.columns)}\n\n"
        f"💡 이 파일을 query_excel 도구로 조건 필터링하면 즉시 분석 가능합니다.\n"
        f"예시: query_excel(file_path='{saved}', filters={{'per_max': 10, 'drawdown_pct_max': -30}})"
    )


@mcp.tool()
@safe_tool
@track_metrics("query_excel")
async def query_excel(
    file_path: str,
    filters: dict | None = None,
    sort_by: str = "",
    descending: bool = True,
    limit: int = 30,
) -> str:
    """엑셀쿼리 — 저장된 Excel 스냅샷에서 조건에 맞는 종목을 즉시 필터링.

    scan_to_excel로 만든 파일을 조회할 때 사용. HTTP 호출 없이 로컬 파일에서
    필터링하므로 매우 빠릅니다. 같은 파일에 여러 조건을 번갈아 쿼리 가능.

    필터 형식 (두 가지 다 지원):
        간단: {"per_max": 10, "pbr_max": 1.5, "drawdown_pct_max": -30}
        상세: {"per": {"max": 10, "min": 0}, "drawdown_pct": {"max": -30}}

    Args:
        file_path: scan_to_excel로 만든 파일 경로
        filters: 필터 조건 (컬럼명_max / 컬럼명_min 형식)
        sort_by: 정렬 기준 컬럼 (예: "market_cap", "drawdown_pct")
        descending: 내림차순 여부 (기본 True)
        limit: 반환 최대 개수 (기본 30)

    Returns:
        필터링된 종목 리스트 (마크다운 테이블)
    """
    try:
        df = load_excel(file_path, sheet_name="Snapshot")
    except FileNotFoundError:
        return f"파일을 찾을 수 없습니다: {file_path}"
    except Exception as e:
        return f"파일 로드 실패: {type(e).__name__}: {e}"

    if filters:
        df = apply_filters(df, filters)

    if sort_by and sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=not descending)

    df = df.head(limit)

    if df.empty:
        return f"조건에 맞는 종목이 없습니다. (원본: {file_path})"

    # 주요 컬럼만 표시
    display_cols = [
        c for c in ["code", "name", "current_price", "drawdown_pct", "per", "pbr", "volume"]
        if c in df.columns
    ]
    if display_cols:
        df_display = df[display_cols]
    else:
        df_display = df

    lines = [f"쿼리 결과 ({len(df)}개 종목, 파일: {Path(file_path).name}):", ""]
    lines.append("| " + " | ".join(df_display.columns) + " |")
    lines.append("|" + "|".join(["---"] * len(df_display.columns)) + "|")
    for _, row in df_display.iterrows():
        values = []
        for col in df_display.columns:
            v = row[col]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if col in ("current_price", "volume", "avg_volume", "high", "low"):
                    values.append(f"{int(v):,}")
                elif col.endswith("_pct"):
                    values.append(f"{v:+.2f}%")
                else:
                    values.append(f"{v:.2f}" if isinstance(v, float) else str(v))
            else:
                values.append(str(v))
        lines.append("| " + " | ".join(values) + " |")

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_metrics_summary")
async def get_metrics_summary(days: int = 1) -> str:
    """사용량통계 — 최근 N일간 MCP 도구 사용량을 집계해서 보여줍니다.

    디버깅/최적화용. 도구별로:
    - 호출 횟수
    - 평균/p50/p95 실행 시간
    - 평균 토큰 소모량
    - 캐시 히트율
    - 에러 발생 횟수
    를 보여줍니다.

    로그 파일 위치: ~/Downloads/kstock/logs/metrics_YYYYMMDD.jsonl

    Args:
        days: 조회할 일수 (기본 1, 오늘만. 최대 30)
    """
    days = max(1, min(days, 30))

    records = load_metrics(days=days)
    if not records:
        return (
            f"최근 {days}일간 기록이 없습니다.\n"
            f"로그 파일: {get_metrics_file()}"
        )

    summary = summarize_metrics(records)

    sorted_tools = sorted(
        summary.items(),
        key=lambda x: x[1]["call_count"],
        reverse=True,
    )

    lines = [
        f"📊 MCP 사용량 통계 (최근 {days}일, 총 {len(records)}회 호출)",
        "",
        "도구 | 호출 | 평균시간 | p95시간 | 평균토큰 | 캐시율 | 에러",
        "---|---|---|---|---|---|---",
    ]

    total_tokens = 0
    total_calls = 0

    for tool, s in sorted_tools:
        total_tokens += s["total_output_tokens"]
        total_calls += s["call_count"]
        lines.append(
            f"{tool} | "
            f"{s['call_count']} | "
            f"{s['avg_duration_ms']}ms | "
            f"{s['p95_duration_ms']}ms | "
            f"{s['avg_tokens']:,} | "
            f"{s['cache_hit_rate']}% | "
            f"{s['errors']}"
        )

    lines.append("")
    lines.append(f"**총 소모 토큰: {total_tokens:,}**")
    lines.append(f"**총 호출: {total_calls}회**")
    lines.append(f"**평균 호출당 토큰: {total_tokens // max(total_calls, 1):,}**")
    lines.append("")
    lines.append(f"로그 파일: {get_metrics_file()}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ETF 도구
# ---------------------------------------------------------------------------


@mcp.tool()
@safe_tool
@track_metrics("get_etf_list")
async def get_etf_list(
    category: str = "",
    sort_by: str = "marketSum",
    limit: int = 20,
) -> str:
    """ETF목록 — ETF 전체 목록 조회 및 카테고리별 필터링.

    category: 카테고리 필터 (빈 문자열=전체). 가능한 값:
      "국내 ���장지수", "국내 업종/테마", "국내 파생",
      "해외 주식", "원자��", "채권/금리", "단기자금"
    sort_by: 정렬 기준 — "marketSum"(시가총액), "quant"(거래량),
      "threeMonthEarnRate"(3개월수익률)
    limit: 반환 개수 (기본 20, 최대 50)
    """
    data = await naver_get_etf_list(
        category=category or None,
        sort_by=sort_by,
        limit=limit,
    )

    items = data["items"]
    if not items:
        return "조건에 맞는 ETF가 없습니다."

    lines = [
        f"ETF 목록 ({data['total']}개 중 상위 {len(items)}개"
        + (f", 카테고리: {category}" if category else "")
        + ")",
        "",
    ]

    for it in items:
        chg = it.get("change_rate", 0) or 0
        chg_sign = "+" if chg > 0 else ""
        ret3m = it.get("return_3m")
        ret3m_str = f" | 3M: {'+' if ret3m > 0 else ''}{ret3m:.1f}%" if ret3m else ""
        mcap = it.get("market_cap", 0)
        mcap_str = f"{mcap:,.0f}억" if mcap else ""

        lines.append(
            f"- **{it['name']}** ({it['code']}) "
            f"| {it.get('price', 0):,.0f}원 ({chg_sign}{chg:.2f}%) "
            f"| NAV {it.get('nav', 0):,.0f} "
            f"| 시총 {mcap_str}"
            f"{ret3m_str}"
        )

    lines.append("")
    lines.append("카테고리: " + ", ".join(data["categories"].values()))

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_etf_info")
async def get_etf_info(code: str) -> str:
    """ETF정보 — ETF 상세 정보 (기초지수, 보수율, 수익률, 구성종목 TOP10).

    code: ETF 종목코드 (예: "069500" KODEX 200, "360750" TIGER 미국S&P500)
    """
    import re
    if not re.match(r"^[A-Za-z0-9]{6}$", code):
        return f"⚠️ 종목코드 형식이 올바르지 않습니다: {code}"

    data = await naver_get_etf_detail(code)

    if not data.get("name"):
        return f"ETF코드 {code}의 정보를 가져올 수 없습니다. 코드를 확인해��세요."

    lines = [
        f"# {data['name']} ({code})",
        "",
        "## 기본 정보",
        f"- 기초지수: {data.get('base_index', '-')}",
        f"- 유형: {data.get('etf_type', '-')}",
        f"- 자산운용사: {data.get('issuer', '-')}",
        f"- 상장일: {data.get('listing_date', '-')}",
        f"- 펀드보수: 연 {data.get('total_fee', 0):.3f}%",
        f"- 펀드유형: {data.get('fund_type', '-')}",
    ]

    if data.get("dividend_base"):
        lines.append(f"- 분배금 기준일: {data['dividend_base']}")

    lines.append("")
    lines.append("## 시세 정보")
    lines.append(f"- 현재가: {data.get('price', 0):,.0f}원")

    chg = data.get("price_change", 0)
    chg_rate = data.get("price_change_rate", 0)
    chg_sign = "+" if chg > 0 else ""
    lines.append(f"- 전일대비: {chg_sign}{chg:,.0f}원 ({chg_sign}{chg_rate:.2f}%)")
    lines.append(f"- 시가���액: {data.get('market_cap', 0):,.0f}억원")
    lines.append(f"- 52주 최고/최저: {data.get('year_high', 0):,.0f} / {data.get('year_low', 0):,.0f}")
    lines.append(f"- 베타: {data.get('beta', 0):.2f}")
    lines.append(f"- 외국인 비율: {data.get('foreign_rate', 0):.2f}%")

    lines.append("")
    lines.append("## 수익률")
    for period, key in [("1개월", "return_1m"), ("3개월", "return_3m"),
                        ("6개월", "return_6m"), ("1년", "return_1y")]:
        val = data.get(key, 0)
        sign = "+" if val > 0 else ""
        lines.append(f"- {period}: {sign}{val:.2f}%")

    holdings = data.get("holdings", [])
    if holdings:
        has_weight = data.get("holdings_has_weight", True)
        lines.append("")
        lines.append(f"## 구성종목 (총 {data.get('holdings_count', len(holdings))}개)")
        lines.append("")

        if has_weight:
            lines.append("종목명 | 비중(%)")
            lines.append("---|---")
            for h in holdings[:10]:
                lines.append(f"{h['name']} | {h['weight']:.2f}%")
            top10_sum = sum(h["weight"] for h in holdings[:10])
            lines.append(f"**TOP10 합계** | **{top10_sum:.2f}%**")
        else:
            lines.append("종목명 | 주식수")
            lines.append("---|---")
            for h in holdings[:10]:
                lines.append(f"{h['name']} | {h['shares']:,.0f}")

    return "\n".join(lines)


# ========================================
# 🇺🇸 US Stock Tools (NYSE / NASDAQ via yfinance)
# ========================================

def safe_us_tool(func):
    """US tool 전용 에러 래퍼 — yfinance·Yahoo Finance 컨텍스트 메시지."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.TimeoutException:
            return "⚠️ Yahoo Finance 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요."
        except httpx.ConnectError:
            return "⚠️ Yahoo Finance에 연결할 수 없습니다. 인터넷 연결을 확인해주세요."
        except Exception as e:
            return (
                f"⚠️ 미국 주식 데이터 처리 중 오류: {type(e).__name__}\n"
                f"티커가 올바른지 확인해주세요 (예: AAPL, MSFT, BRK.B)."
            )

    return wrapper


def _fmt_num(v, unit: str = "", digits: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        if abs(v) >= 1_000_000_000:
            return f"${v/1_000_000_000:,.{digits}f}B"
        if abs(v) >= 1_000_000:
            return f"${v/1_000_000:,.{digits}f}M"
        return f"{v:,.{digits}f}{unit}"
    return str(v)


def _fmt_ratio(v, digits: int = 2) -> str:
    """소수 비율(ROE=1.52, margin=0.27)을 백분율로. *100 후 %."""
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v * 100:.{digits}f}%"
    return str(v)


def _fmt_yield(v, digits: int = 2) -> str:
    """yfinance의 dividendYield/fiveYearAvgDividendYield는 이미 %. 그대로 출력."""
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}%"
    return str(v)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_price")
async def get_us_price(ticker: str) -> str:
    """US stock price — NYSE/NASDAQ 미국 주식 현재가 스냅샷 (US market via yfinance).
    "AAPL price", "Tesla 현재가", "MSFT quote", "Nvidia 얼마" 같은 질문에 사용합니다.

    현재가 + 전일대비 + 시/고/저 + 거래량 + 52주 고저 + 베타 + 시가총액 + 마켓 상태
    (정규장/프리/포스트) 반환. Yahoo Finance 데이터는 최대 15분 지연 가능.

    Args:
        ticker: US 티커 (예: "AAPL", "TSLA", "BRK.B", "SPY")
    """
    data = await us.get_price(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다. 정확한 미국 주식 심볼인지 확인해주세요."

    currency = data.get("currency") or "USD"
    sym = "$" if currency == "USD" else ""
    lines = [
        f"**{data.get('name', data['ticker'])}** ({data['ticker']}) — {data.get('exchange', '')}",
        f"현재가: {sym}{data['price']:,.2f}" if data.get("price") is not None else "현재가: -",
    ]
    ch = data.get("change")
    chp = data.get("change_percent")
    if ch is not None and chp is not None:
        lines.append(f"전일대비: {ch:+,.2f} ({chp:+.2f}%)")
    lines.append(f"시가/고가/저가: {_fmt_num(data.get('open'))} / {_fmt_num(data.get('day_high'))} / {_fmt_num(data.get('day_low'))}")
    lines.append(f"거래량: {data['volume']:,}" if data.get("volume") else "거래량: -")
    lines.append(f"52주 고저: {_fmt_num(data.get('52w_high'))} / {_fmt_num(data.get('52w_low'))}")
    if data.get("beta") is not None:
        lines.append(f"베타: {data['beta']:.2f}")
    if data.get("market_cap"):
        lines.append(f"시가총액: {_fmt_num(data['market_cap'])}")
    if data.get("market_state"):
        lines.append(f"마켓 상태: {data['market_state']}")
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_info")
async def get_us_info(ticker: str) -> str:
    """US stock info — 미국 주식 기업 정보 (섹터, 산업, 시총, 사업 요약 · US company profile).
    "Apple 어떤 회사", "NVDA 사업 설명", "TSLA sector", "기업 정보" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커 (예: "AAPL", "NVDA")
    """
    data = await us.get_info(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    lines = [
        f"**{data.get('name') or data['ticker']}** ({data['ticker']})",
        f"섹터/산업: {data.get('sector') or '-'} / {data.get('industry') or '-'}",
        f"거래소: {data.get('exchange') or '-'} ({data.get('quote_type') or '-'})",
        f"국가: {data.get('country') or '-'}",
    ]
    if data.get("market_cap"):
        lines.append(f"시가총액: {_fmt_num(data['market_cap'])}")
    if data.get("enterprise_value"):
        lines.append(f"기업가치(EV): {_fmt_num(data['enterprise_value'])}")
    if data.get("shares_outstanding"):
        lines.append(f"발행주식수: {data['shares_outstanding']:,.0f}")
    if data.get("employees"):
        lines.append(f"임직원: {data['employees']:,}명")
    if data.get("website"):
        lines.append(f"웹사이트: {data['website']}")
    summary = data.get("business_summary")
    if summary:
        lines.append("")
        lines.append("## 사업 요약")
        lines.append(summary[:800] + ("..." if len(summary) > 800 else ""))
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_chart")
async def get_us_chart(
    ticker: str,
    period: str = "3mo",
    interval: str = "1d",
    prepost: bool = False,
    limit: int = 500,
) -> str:
    """US stock chart OHLCV — 미국 주식 시계열 캔들 데이터 (US historical price data).
    "AAPL 차트", "Tesla 1년 주가", "NVDA history", "월봉" 같은 질문에 사용합니다.

    ⭐ US 주식의 시계열/차트 진입점. 한국 주식은 get_chart 사용.

    Args:
        ticker: US 티커 (예: "AAPL")
        period: "1d","5d","1mo","3mo","6mo","1y","2y","5y","10y","ytd","max" (기본 3mo)
        interval: "1m","5m","15m","30m","1h","1d","1wk","1mo" (기본 1d)
        prepost: 프리/포스트 마켓 포함 (intraday interval에서만 유효)
        limit: 반환 최대 행수 (기본 500, 최대 5000). 토큰 소비 보호용 상한.
               10년 일봉(2,515행) · 백테스트 용도엔 2000~5000으로 올려 사용.
               더 큰 데이터는 export_us_to_excel로 파일 저장 권장 (토큰 0).
    """
    limit = max(10, min(limit, 5000))  # 10~5000 범위로 클램프
    rows = await us.get_history(ticker, period=period, interval=interval, prepost=prepost)
    if not rows:
        return f"티커 '{ticker}'의 차트 데이터를 가져올 수 없습니다."

    total = len(rows)
    truncated = total > limit
    if truncated:
        rows = rows[-limit:]  # 최근 구간 유지

    header = f"**{ticker.upper()}** {period} {interval} OHLCV ({len(rows)} bars"
    if truncated:
        header += f" · 원본 {total}행 중 최근 {limit}행"
    header += ")"

    lines = [
        header,
        "",
        "날짜/시간 | 시가 | 고가 | 저가 | 종가 | 거래량",
        "---|---|---|---|---|---",
    ]
    for r in rows:
        date = r.get("date") or r.get("datetime") or ""
        date_s = str(date)[:19]  # 초단위 절삭
        lines.append(
            f"{date_s} | {r.get('open', 0):,.2f} | {r.get('high', 0):,.2f} | "
            f"{r.get('low', 0):,.2f} | {r.get('close', 0):,.2f} | {int(r.get('volume') or 0):,}"
        )
    if truncated:
        lines.append("")
        lines.append(
            f"💡 더 필요하면 `limit={min(total, 5000)}`로 재호출하세요. "
            f"백테스트·CSV 저장 용도면 **export_us_to_excel** 로 파일 저장 (토큰 0)."
        )
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_financials")
async def get_us_financials(ticker: str) -> str:
    """US stock financials — 미국 주식 재무지표 (PER, PBR, PEG, ROE, 배당률 · US valuation ratios).
    "AAPL PER", "Apple 재무", "NVDA valuation", "forward P/E" 같은 질문에 사용합니다.

    Trailing / Forward P/E, PEG, P/B, P/S, EPS, ROE, ROA, 부채비율, 마진, 성장률,
    배당수익률, 배당성향을 반환합니다.

    Args:
        ticker: US 티커 (예: "AAPL")
    """
    data = await us.get_financial_info(ticker)
    if data is None:
        return f"티커 '{ticker}'의 재무 데이터를 가져올 수 없습니다."

    lines = [f"**{data['ticker']}** 재무지표", "", "## Valuation"]
    lines.append(f"- Trailing P/E: {_fmt_num(data.get('trailing_pe'))}")
    lines.append(f"- Forward P/E: {_fmt_num(data.get('forward_pe'))}")
    lines.append(f"- PEG Ratio: {_fmt_num(data.get('peg_ratio'))}")
    lines.append(f"- P/B: {_fmt_num(data.get('price_to_book'))}")
    lines.append(f"- P/S (TTM): {_fmt_num(data.get('price_to_sales'))}")

    lines.append("")
    lines.append("## Per Share")
    lines.append(f"- EPS (Trailing): {_fmt_num(data.get('eps_trailing'))}")
    lines.append(f"- EPS (Forward): {_fmt_num(data.get('eps_forward'))}")
    lines.append(f"- Revenue/Share: {_fmt_num(data.get('revenue_per_share'))}")
    lines.append(f"- Book Value: {_fmt_num(data.get('book_value'))}")

    lines.append("")
    lines.append("## Profitability")
    lines.append(f"- ROE: {_fmt_ratio(data.get('return_on_equity'))}")
    lines.append(f"- ROA: {_fmt_ratio(data.get('return_on_assets'))}")
    lines.append(f"- Profit Margin: {_fmt_ratio(data.get('profit_margin'))}")
    lines.append(f"- Operating Margin: {_fmt_ratio(data.get('operating_margin'))}")

    lines.append("")
    lines.append("## Balance & Growth")
    lines.append(f"- Debt/Equity: {_fmt_num(data.get('debt_to_equity'))}")
    lines.append(f"- Current Ratio: {_fmt_num(data.get('current_ratio'))}")
    lines.append(f"- Revenue Growth (YoY): {_fmt_ratio(data.get('revenue_growth'))}")
    lines.append(f"- Earnings Growth (YoY): {_fmt_ratio(data.get('earnings_growth'))}")

    lines.append("")
    lines.append("## Dividend")
    lines.append(f"- Dividend Yield: {_fmt_yield(data.get('dividend_yield'))}")
    lines.append(f"- Payout Ratio: {_fmt_ratio(data.get('payout_ratio'))}")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_earnings")
async def get_us_earnings(ticker: str) -> str:
    """US earnings calendar — 다음 실적 발표일 + 최근 EPS 서프라이즈 이력 (US earnings date / EPS surprise).
    "AAPL 실적 언제", "NVDA earnings date", "Tesla 다음 실적" 같은 질문에 사용합니다.

    미국 시장은 분기 실적(10-Q)이 주가 변동의 핵심 이벤트입니다.

    Args:
        ticker: US 티커 (예: "NVDA")
    """
    data = await us.get_earnings(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    upcoming = data.get("upcoming", []) or []
    history = data.get("history", []) or []
    if not upcoming and not history:
        return f"**{data['ticker']}** 실적 일정 & 이력\n\n실적 데이터 없음 (ETF나 개별 주식이 아닌 자산은 해당 없음)."

    lines = [f"**{data['ticker']}** 실적 일정 & 이력"]
    if upcoming:
        lines.append("")
        lines.append("## 🗓️ 예정")
        for e in upcoming[:4]:
            date = str(e.get("earnings_date", ""))[:10]
            est = e.get("eps_estimate")
            lines.append(f"- {date} (EPS 추정: {_fmt_num(est)})")
    else:
        lines.append("")
        lines.append("예정된 실적 발표일 정보 없음.")

    if history:
        lines.append("")
        lines.append("## 📊 최근 실적 서프라이즈")
        lines.append("발표일 | EPS 추정 | 실제 EPS | 서프라이즈 %")
        lines.append("---|---|---|---")
        for e in history[:8]:
            date = str(e.get("earnings_date", ""))[:10]
            est = e.get("eps_estimate")
            rep = e.get("reported_eps")
            sur = e.get("surprise(%)") or e.get("surprise_%") or e.get("surprise")
            lines.append(f"{date} | {_fmt_num(est)} | {_fmt_num(rep)} | {_fmt_num(sur)}%")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_analyst")
async def get_us_analyst(ticker: str) -> str:
    """US analyst ratings — 미국 주식 애널리스트 목표주가 + 투자의견 (US analyst price target / rating).
    "AAPL 목표가", "NVDA analyst rating", "Tesla buy/hold", "Wall Street 의견" 같은 질문에 사용합니다.

    목표주가(mean/high/low) + buy/hold/sell 분포 + 최근 업·다운그레이드를 반환합니다.

    Args:
        ticker: US 티커 (예: "NVDA")
    """
    data = await us.get_analyst_ratings(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    # ETF·펀드는 애널리스트 커버 없음
    has_any = bool(
        data.get("price_targets") or data.get("recommendation_key")
        or data.get("recommendation_mean") or data.get("analyst_count")
        or data.get("recommendations_by_month") or data.get("recent_upgrades_downgrades")
    )
    if not has_any:
        return f"**{data['ticker']}** 애널리스트 의견\n\n애널리스트 커버리지 없음 (ETF나 신규·소형주의 경우 해당 없음)."

    lines = [f"**{data['ticker']}** 애널리스트 의견"]

    cur = data.get("current_price")
    targets = data.get("price_targets") or {}
    if targets:
        lines.append("")
        lines.append("## 🎯 목표주가")
        if cur is not None:
            lines.append(f"현재가: ${cur:,.2f}")
        for label, key in [("평균", "mean"), ("중앙값", "median"), ("최고", "high"), ("최저", "low")]:
            v = targets.get(key)
            if v is not None:
                upside = ((v - cur) / cur * 100) if cur else None
                up_str = f" ({upside:+.1f}%)" if upside is not None else ""
                lines.append(f"- {label}: ${v:,.2f}{up_str}")

    rec_key = data.get("recommendation_key")
    rec_mean = data.get("recommendation_mean")
    n_analysts = data.get("analyst_count")
    if rec_key or rec_mean:
        lines.append("")
        lines.append("## 📈 투자의견")
        if rec_key:
            lines.append(f"- 종합: **{rec_key.upper()}**")
        if rec_mean is not None:
            lines.append(f"- 평균 점수: {rec_mean:.2f} (1=Strong Buy, 5=Strong Sell)")
        if n_analysts:
            lines.append(f"- 커버 애널리스트: {n_analysts}명")

    rec_monthly = data.get("recommendations_by_month") or []
    if rec_monthly:
        lines.append("")
        lines.append("## 월별 의견 분포 (최근)")
        lines.append("기간 | Strong Buy | Buy | Hold | Sell | Strong Sell")
        lines.append("---|---|---|---|---|---")
        for r in rec_monthly[:4]:
            period = r.get("period", "-")
            lines.append(
                f"{period} | {r.get('strongBuy', 0)} | {r.get('buy', 0)} | "
                f"{r.get('hold', 0)} | {r.get('sell', 0)} | {r.get('strongSell', 0)}"
            )

    updown = data.get("recent_upgrades_downgrades") or []
    if updown:
        lines.append("")
        lines.append("## 🔄 최근 업·다운그레이드")
        for u in updown[:6]:
            date = str(u.get("GradeDate", ""))[:10]
            firm = u.get("Firm", "-")
            from_g = u.get("FromGrade", "")
            to_g = u.get("ToGrade", "")
            action = u.get("Action", "")
            lines.append(f"- {date} · {firm} · {from_g or '-'} → {to_g or '-'} ({action})")

    # 애널리스트 추정치 (EPS / Revenue / 성장률)
    estimates = await us.get_analyst_estimates(ticker)
    if estimates:
        eps_est = estimates.get("earnings_estimate") or []
        rev_est = estimates.get("revenue_estimate") or []
        eps_rev = estimates.get("eps_revisions") or []
        if eps_est or rev_est:
            lines.append("")
            lines.append("## 🔮 애널리스트 추정치")
            if eps_est:
                lines.append("### EPS Estimate")
                lines.append("기간 | 평균 | 낮음 | 높음 | # 애널리스트 | YoY 성장")
                lines.append("---|---|---|---|---|---")
                for e in eps_est:
                    per = e.get("period") or e.get("index") or "-"
                    avg = e.get("avg") or e.get("average")
                    lo = e.get("low")
                    hi = e.get("high")
                    n = e.get("numberofanalysts") or e.get("numberofanalyst")
                    gr = e.get("growth")
                    gr_s = f"{gr*100:+.2f}%" if isinstance(gr, (int, float)) else "-"
                    lines.append(f"{per} | {_fmt_num(avg)} | {_fmt_num(lo)} | {_fmt_num(hi)} | {int(n) if isinstance(n, (int, float)) else '-'} | {gr_s}")
            if rev_est:
                lines.append("")
                lines.append("### Revenue Estimate")
                lines.append("기간 | 평균 | 낮음 | 높음 | YoY 성장")
                lines.append("---|---|---|---|---")
                for e in rev_est:
                    per = e.get("period") or e.get("index") or "-"
                    avg = e.get("avg") or e.get("average")
                    lo = e.get("low")
                    hi = e.get("high")
                    gr = e.get("growth")
                    gr_s = f"{gr*100:+.2f}%" if isinstance(gr, (int, float)) else "-"
                    lines.append(f"{per} | {_fmt_num(avg)} | {_fmt_num(lo)} | {_fmt_num(hi)} | {gr_s}")
        if eps_rev:
            lines.append("")
            lines.append("### 최근 EPS 추정 변경 (7일/30일 up/down)")
            lines.append("기간 | up7d | down7d | up30d | down30d")
            lines.append("---|---|---|---|---")
            for e in eps_rev:
                per = e.get("period") or e.get("index") or "-"
                lines.append(
                    f"{per} | {e.get('upLast7days') or e.get('uplast7days') or 0} | "
                    f"{e.get('downLast7days') or e.get('downlast7days') or 0} | "
                    f"{e.get('upLast30days') or e.get('uplast30days') or 0} | "
                    f"{e.get('downLast30days') or e.get('downlast30days') or 0}"
                )

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_dividends")
async def get_us_dividends(ticker: str, limit: int = 12) -> str:
    """US dividends — 미국 주식 배당 이력 + ex-date + yield (US dividend history / yield).
    "AAPL 배당", "KO dividend yield", "SCHD 배당 이력", "ex-date" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커 (예: "KO", "JNJ", "SCHD")
        limit: 표시할 최근 배당 건수 (기본 12)
    """
    data = await us.get_dividends(ticker, limit=limit)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    lines = [f"**{data['ticker']}** 배당 정보"]

    lines.append("")
    lines.append("## 📊 요약")
    lines.append(f"- Dividend Yield: {_fmt_yield(data.get('dividend_yield'))}")
    lines.append(f"- Annual Dividend Rate: {_fmt_num(data.get('dividend_rate'))}")
    lines.append(f"- 5-Year Avg Yield: {_fmt_yield(data.get('five_year_avg_yield'))}")
    lines.append(f"- Payout Ratio: {_fmt_ratio(data.get('payout_ratio'))}")

    ex_date = data.get("ex_dividend_date")
    if ex_date:
        # yfinance는 Unix timestamp로 주는 경우가 많음
        if isinstance(ex_date, (int, float)):
            ex_date = datetime.utcfromtimestamp(ex_date).strftime("%Y-%m-%d")
        lines.append(f"- 다음 Ex-Dividend Date: {ex_date}")

    last_v = data.get("last_dividend_value")
    last_d = data.get("last_dividend_date")
    if last_v and last_d:
        if isinstance(last_d, (int, float)):
            last_d = datetime.utcfromtimestamp(last_d).strftime("%Y-%m-%d")
        lines.append(f"- 최근 배당: ${last_v:.4f} ({last_d})")

    history = data.get("history") or []
    if history:
        lines.append("")
        lines.append(f"## 배당 이력 (최근 {len(history)}건)")
        lines.append("날짜 | 배당금")
        lines.append("---|---")
        for h in reversed(history):
            date = str(h.get("date", ""))[:10]
            amt = h.get("dividends", 0)
            lines.append(f"{date} | ${amt:.4f}")
    else:
        lines.append("")
        lines.append("배당 이력 없음 (무배당 주식일 수 있습니다).")

    return "\n".join(lines)


# --- US Phase 2 tools ---

@mcp.tool()
@safe_us_tool
@track_metrics("get_us_options")
async def get_us_options(
    ticker: str,
    expiration: str | None = None,
    strikes_around_spot: int = 10,
) -> str:
    """US options chain — 미국 주식 옵션 체인 (calls/puts, IV, OI · US equity options).
    "AAPL options", "Tesla 콜옵션", "NVDA implied volatility", "옵션 체인" 같은 질문에 사용합니다.

    기본: 최근접 만기의 현재가 근처 strike. Greeks (delta/gamma/theta)는 미포함.

    Args:
        ticker: US 티커 (예: "AAPL")
        expiration: 만기일 "YYYY-MM-DD". 미지정 시 가장 가까운 만기.
        strikes_around_spot: 현재가 기준 좌우 strike 개수 (기본 10)
    """
    data = await us.get_options(ticker, expiration=expiration, strikes_around_spot=strikes_around_spot)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."
    if not data.get("expirations"):
        return f"**{data['ticker']}** — 옵션 거래 없음."

    lines = [
        f"**{data['ticker']}** 옵션 체인",
        f"현재가(spot): ${data['spot']:,.2f}" if data.get("spot") else "",
        f"선택 만기: **{data['selected_expiration']}**",
        f"사용 가능 만기: {', '.join(data['expirations'][:8])}{' ...' if len(data['expirations'])>8 else ''}",
    ]

    def _table(title: str, rows: list[dict]) -> list[str]:
        if not rows:
            return [f"\n### {title}", "데이터 없음"]
        out = [f"\n### {title}", "Strike | Last | Bid | Ask | Vol | OI | IV | ITM"]
        out.append("---|---|---|---|---|---|---|---")
        for r in rows:
            iv = r.get("impliedVolatility")
            iv_s = f"{iv*100:.1f}%" if isinstance(iv, (int, float)) else "-"
            out.append(
                f"{r.get('strike', 0):.2f} | {r.get('lastPrice', 0):.2f} | "
                f"{r.get('bid') or 0:.2f} | {r.get('ask') or 0:.2f} | "
                f"{int(r.get('volume') or 0)} | {int(r.get('openInterest') or 0)} | "
                f"{iv_s} | {'Y' if r.get('inTheMoney') else 'N'}"
            )
        return out

    lines.extend(_table("📞 Calls", data.get("calls", [])))
    lines.extend(_table("📉 Puts", data.get("puts", [])))
    return "\n".join(l for l in lines if l)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_insider")
async def get_us_insider(ticker: str) -> str:
    """US insider trading — 내부자 거래 Form 4 + 최근 6개월 순매수 요약 (US insider Form 4).
    "AAPL insider trading", "NVDA 내부자 매수", "CEO stock sale" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커
    """
    data = await us.get_insider(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    lines = [f"**{data['ticker']}** 내부자 거래"]

    summary = data.get("purchases_last_6m") or {}
    if summary:
        lines.append("")
        lines.append("## 📊 최근 6개월 요약")
        for label, vals in summary.items():
            shares = vals.get("shares")
            trans = vals.get("trans")
            if shares is None and trans is None:
                continue
            sh_str = f"{shares:,.0f}" if isinstance(shares, (int, float)) else "-"
            tr_str = f" ({int(trans)}건)" if isinstance(trans, (int, float)) else ""
            lines.append(f"- {label}: {sh_str}{tr_str}")

    tx = data.get("recent_transactions") or []
    if tx:
        lines.append("")
        lines.append(f"## 🗓️ 최근 거래 ({len(tx)}건)")
        lines.append("일자 | 인사 | 직위 | 유형 | 주식수 | 금액")
        lines.append("---|---|---|---|---|---")
        for t in tx[:15]:
            date = str(t.get("start_date", ""))[:10]
            insider = (t.get("insider") or "-")[:25]
            pos = (t.get("position") or "-")[:20]
            action = t.get("transaction") or "-"
            shares = t.get("shares") or 0
            value = t.get("value")
            val_s = f"${value:,.0f}" if isinstance(value, (int, float)) else "-"
            lines.append(f"{date} | {insider} | {pos} | {action} | {int(shares):,} | {val_s}")

    if not summary and not tx:
        lines.append("")
        lines.append("내부자 거래 정보 없음 (ETF나 펀드는 해당 없음).")

    # 현재 내부자 명단 (roster)
    roster = await us.get_insider_roster(ticker)
    if roster:
        lines.append("")
        lines.append(f"## 👥 현재 내부자 명단 ({len(roster)})")
        lines.append("이름 | 직위 | 보유주식 | 최근 거래")
        lines.append("---|---|---|---")
        for r in roster[:15]:
            name = (r.get("name") or "-")[:25]
            pos = (r.get("position") or "-")[:25]
            sh = r.get("most_recent_transaction") or r.get("position_direct_date") or ""
            held = r.get("position_direct") or r.get("shares_owned_directly") or r.get("shares")
            held_s = f"{int(held):,}" if isinstance(held, (int, float)) else "-"
            recent = str(r.get("latest_transaction_date", ""))[:10]
            lines.append(f"{name} | {pos} | {held_s} | {recent}")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_holders")
async def get_us_holders(ticker: str) -> str:
    """US institutional holders — 기관·뮤추얼펀드 보유 현황 (13F holdings).
    "AAPL institutional holders", "Vanguard 보유", "13F holdings", "who owns" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커
    """
    data = await us.get_holders(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    lines = [f"**{data['ticker']}** 보유 현황"]

    # ETF는 institutional_holders/mutualfund_holders 데이터 없음
    has_any = bool(
        data.get("institutional_holders") or data.get("mutualfund_holders")
        or data.get("held_pct_institutions") or data.get("held_pct_insiders")
    )
    if not has_any:
        lines.append("")
        lines.append("보유자 정보 없음 (ETF·신규 상장·소형주의 경우 데이터가 제공되지 않을 수 있습니다).")
        return "\n".join(lines)

    inst_pct = data.get("held_pct_institutions")
    insd_pct = data.get("held_pct_insiders")
    if inst_pct is not None or insd_pct is not None:
        lines.append("")
        lines.append("## 📊 전체 비중")
        if inst_pct is not None:
            lines.append(f"- 기관 보유: {inst_pct*100:.2f}%")
        if insd_pct is not None:
            lines.append(f"- 내부자 보유: {insd_pct*100:.2f}%")

    inst = data.get("institutional_holders") or []
    if inst:
        lines.append("")
        lines.append("## 🏦 기관 투자자 TOP 10")
        lines.append("기관 | 보유% | 주식수 | 평가액 | 변동% | 보고일")
        lines.append("---|---|---|---|---|---")
        for h in inst:
            holder = ((h.get("holder") or "-").strip())[:30].rstrip()
            pct = h.get("pctheld")
            pct_s = f"{pct*100:.2f}%" if isinstance(pct, (int, float)) else "-"
            shares = h.get("shares") or 0
            value = h.get("value") or 0
            chg = h.get("pctchange")
            chg_s = f"{chg*100:+.2f}%" if isinstance(chg, (int, float)) else "-"
            date = str(h.get("date_reported", ""))[:10]
            lines.append(f"{holder} | {pct_s} | {int(shares):,} | ${value:,.0f} | {chg_s} | {date}")

    mf = data.get("mutualfund_holders") or []
    if mf:
        lines.append("")
        lines.append("## 💼 뮤추얼 펀드 TOP 10")
        lines.append("펀드 | 보유% | 주식수 | 평가액")
        lines.append("---|---|---|---")
        for h in mf:
            holder = ((h.get("holder") or "-").strip())[:35].rstrip()
            pct = h.get("pctheld")
            pct_s = f"{pct*100:.2f}%" if isinstance(pct, (int, float)) else "-"
            shares = h.get("shares") or 0
            value = h.get("value") or 0
            lines.append(f"{holder} | {pct_s} | {int(shares):,} | ${value:,.0f}")

    # Major holders 요약 (breakdown: insiders/institutions/float 등)
    major = await us.get_major_holders(ticker)
    if major and major.get("summary"):
        lines.append("")
        lines.append("## 📋 Breakdown")
        for label, val in major["summary"].items():
            if isinstance(val, (int, float)):
                # yfinance는 0~1 소수 or 라벨+값 혼용
                val_s = f"{val*100:.2f}%" if 0 <= abs(val) <= 1 else f"{val:,.0f}"
            else:
                val_s = str(val)
            lines.append(f"- {label}: {val_s}")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_short")
async def get_us_short(ticker: str) -> str:
    """US short interest — 공매도 잔고 + % of float + days to cover (US short interest).
    "AAPL short interest", "GME 공매도", "short squeeze" 같은 질문에 사용합니다.

    ⚠️ FINRA bi-monthly 공시라 데이터가 2~4주 stale합니다. 반드시 'date_short_interest'를
    확인하세요.

    Args:
        ticker: US 티커
    """
    data = await us.get_short_interest(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    def _ts(v):
        if isinstance(v, (int, float)):
            return datetime.utcfromtimestamp(v).strftime("%Y-%m-%d")
        return "-"

    lines = [f"**{data['ticker']}** 공매도 지표"]
    lines.append("")
    lines.append(f"⚠️ 보고 기준일: **{_ts(data.get('date_short_interest'))}** (FINRA bi-monthly — 최대 2~4주 stale)")
    lines.append("")
    lines.append("## 📊 현재 지표")
    ss = data.get("shares_short")
    lines.append(f"- Shares Short: {ss:,.0f}" if isinstance(ss, (int, float)) else "- Shares Short: -")
    spf = data.get("short_percent_of_float")
    if isinstance(spf, (int, float)):
        lines.append(f"- % of Float (shorted): **{spf*100:.2f}%**")
    sr = data.get("short_ratio")
    if isinstance(sr, (int, float)):
        lines.append(f"- Days to Cover (short ratio): {sr:.2f}일")
    fl = data.get("float_shares")
    if isinstance(fl, (int, float)):
        lines.append(f"- Float: {fl:,.0f}주")

    prior = data.get("shares_short_prior_month")
    prior_date = data.get("prior_month_date")
    if isinstance(prior, (int, float)) and isinstance(ss, (int, float)):
        lines.append("")
        lines.append("## 📈 전월 대비")
        change = ss - prior
        pct = (change / prior * 100) if prior else 0
        lines.append(f"- 전월 ({_ts(prior_date)}): {prior:,.0f}")
        lines.append(f"- 변동: {change:+,.0f} ({pct:+.2f}%)")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_filings")
async def get_us_filings(ticker: str, limit: int = 15) -> str:
    """US SEC filings — 10-K, 10-Q, 8-K 공시 목록 + EDGAR URL (US SEC EDGAR filings).
    "AAPL 10-K", "NVDA latest filings", "8-K", "SEC filing" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커
        limit: 표시할 공시 건수 (기본 15)
    """
    data = await us.get_sec_filings(ticker, limit=limit)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."

    filings = data.get("filings") or []
    if not filings:
        return f"**{data['ticker']}** — 공시 정보 없음."

    lines = [f"**{data['ticker']}** SEC 공시 (최근 {len(filings)}건)", ""]
    lines.append("일자 | 유형 | 제목 | URL")
    lines.append("---|---|---|---")
    for f in filings:
        date = str(f.get("date", ""))[:10]
        typ = f.get("type") or "-"
        title = (f.get("title") or "-")[:50]
        url = f.get("edgar_url") or "-"
        lines.append(f"{date} | **{typ}** | {title} | [link]({url})")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_news")
async def get_us_news(ticker: str, limit: int = 10) -> str:
    """US stock news — 미국 주식 관련 뉴스 헤드라인 (US stock news feed).
    "AAPL 뉴스", "Tesla news", "NVDA headlines" 같은 질문에 사용합니다.

    Args:
        ticker: US 티커
        limit: 헤드라인 개수 (기본 10)
    """
    data = await us.get_news(ticker, limit=limit)
    if data is None or not data.get("news"):
        return f"티커 '{ticker}'의 뉴스를 가져올 수 없습니다."

    lines = [f"**{data['ticker'].upper()}** 최근 뉴스", ""]
    for n in data["news"]:
        title = n.get("title") or "-"
        pub = (n.get("published") or "")[:16].replace("T", " ")
        provider = n.get("provider") or "-"
        url = n.get("url") or ""
        summary = (n.get("summary") or "").replace("\n", " ").strip()
        # HTML 태그 간단 제거
        summary = re.sub(r"<[^>]+>", "", summary)[:200]
        lines.append(f"### {title}")
        lines.append(f"_{pub} · {provider}_")
        if summary:
            lines.append(summary)
        if url:
            lines.append(f"[링크]({url})")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 컨센서스 / 목표가 / 리포트 / 공시
# ---------------------------------------------------------------------------


@mcp.tool()
@safe_tool
@track_metrics("get_consensus")
async def get_consensus(code: str) -> str:
    """컨센서스 — 증권사 투자의견, 목표주가, 실적 추정치 (매출액/영업이익 컨센서스).

    "목표가 얼마야", "컨센서스", "증권사 의견", "��정가", "실적 전망" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    import re
    if not re.match(r"^[A-Za-z0-9]{6}$", code):
        return f"⚠️ 종목코드 형식이 올바르지 않습니다: {code}"

    data = await naver_get_consensus(code)

    if not data.get("target_price") and not data.get("opinion"):
        return f"종목코드 {code}의 컨센서스 데이터가 없습니다. 분석 대상 종목이 아니거나 코드를 확인해주세요."

    lines = [f"## 컨센서스 ({code})", ""]

    # 목표주가
    tp = data.get("target_price")
    if tp:
        lines.append(f"**목표주가: {tp:,.0f}원**")

    # 투자의견
    opinion = data.get("opinion", {})
    if opinion:
        total = sum(opinion.values())
        parts = [f"{k} {v}건" for k, v in opinion.items() if v > 0]
        lines.append(f"투자의견: {', '.join(parts)} (총 {total}건)")

        ago = data.get("opinion_1m_ago", {})
        if ago:
            ago_parts = [f"{k} {v}건" for k, v in ago.items() if v > 0]
            lines.append(f"1개월 전: {', '.join(ago_parts)}")

    # 실적 추정치
    estimates = data.get("estimates", {})
    periods = data.get("estimate_periods", [])
    if estimates and periods:
        lines.append("")
        lines.append("### 실적 추정치 (컨센서스)")
        lines.append("")

        # 기간 헤더
        period_labels = [f"{p[:4]}.{p[4:]}" for p in periods]
        lines.append("지표 | " + " | ".join(period_labels))
        lines.append("---|" + "|".join(["---"] * len(periods)))

        for label in ["매출액", "영업이익"]:
            vals = estimates.get(label, {})
            cells = []
            for p in periods:
                v = vals.get(p)
                if v is not None:
                    cells.append(f"{v:,.0f}")
                else:
                    cells.append("-")
            lines.append(f"{label} | " + " | ".join(cells))

        # 영업이익��
        opr = estimates.get("영업이익률", {})
        if opr:
            cells = []
            for p in periods:
                v = opr.get(p)
                cells.append(f"{v:.1f}%" if v is not None else "-")
            lines.append(f"영업이익률 | " + " | ".join(cells))

    lines.append("")
    lines.append("※ 단위: 억원, 에프앤가이드 컨센서스 기준")

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_reports")
async def get_reports(code: str, count: int = 5) -> str:
    """증권사리포트 — 종목의 최근 증권사 분석 리포트 (목표가, 투자의견, 본문 요약, PDF).

    "리포트", "증권사 분석", "애널리스트 의견", "리서치" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
        count: 가져올 리포트 수 (기본 5, 최대 10)
    """
    import re
    if not re.match(r"^[A-Za-z0-9]{6}$", code):
        return f"⚠️ 종목코드 형식이 올바르지 않습니다: {code}"

    count = min(count, 10)
    report_list = await naver_get_reports(code, count)

    if not report_list:
        return f"종목코드 {code}의 증권사 리포트가 없습니다."

    # 각 리포트 상세를 병렬 조회
    import asyncio as _asyncio
    details = await _asyncio.gather(
        *[naver_get_report_detail(r["nid"]) for r in report_list]
    )

    lines = [f"## 증권사 리포트 ({code}, 최근 {len(report_list)}건)", ""]

    for report, detail in zip(report_list, details):
        tp = detail.get("target_price")
        op = detail.get("opinion", "")
        tp_str = f" | 목표가 {tp:,}원" if tp else ""
        op_str = f" | {op}" if op else ""

        lines.append(f"### {report['title']}")
        lines.append(f"{report['broker']} | {report['date']}{tp_str}{op_str}")

        summary = detail.get("summary", "")
        if summary:
            lines.append(f"> {summary[:300]}")

        pdf = detail.get("pdf_url", "")
        if pdf:
            lines.append(f"[PDF 원문]({pdf})")

        lines.append("")

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_disclosure")
async def get_disclosure(code: str) -> str:
    """공시목록 — 종목의 최근 공시 (DART 전자공시) 목록.

    "공시", "IR", "실적 발표", "공정공시" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    import re
    if not re.match(r"^[A-Za-z0-9]{6}$", code):
        return f"⚠️ 종목코드 형식이 올바르지 않습니다: {code}"

    items = await naver_get_disclosure_list(code)

    if not items:
        return f"종목코드 {code}의 공시 내역이 없습니다."

    lines = [f"## 공시 목록 ({code}, 최근 {len(items)}건)", ""]
    lines.append("날짜 | 제목 | 출처")
    lines.append("---|---|---")

    for item in items:
        lines.append(f"{item['date']} | {item['title']} | {item['source']}")

    lines.append("")
    lines.append("※ 공시 본문은 DART(dart.fss.or.kr)에서 확인 가능합니다.")

    return "\n".join(lines)


# --- US Phase 3: 탐색/시장/재무제표/ETF/멀티 ---

@mcp.tool()
@safe_us_tool
@track_metrics("get_us_search")
async def get_us_search(query: str) -> str:
    """US stock search — 미국 주식 종목명/티커 검색 (US stock search, ticker lookup).
    "Apple 티커", "Tesla symbol", "반도체 ETF", "Nvidia 찾아줘" 같은 질문에 사용합니다.

    ⚠️ 사용자가 "애플"·"테슬라" 같은 회사명만 주면 **이 도구를 먼저 호출**하세요.
    결과가 여러 개면 사용자에게 확인 요청. 추측 금지.

    Args:
        query: 검색어 (회사명·티커, 한/영 무관)
    """
    results = await us.search(query)
    if not results:
        return f"'{query}'에 대한 미국 주식 검색 결과가 없습니다."

    lines = [f"**'{query}'** 검색 결과 ({len(results)}건):", ""]
    for r in results:
        sector = r.get("sector") or ""
        sector_str = f" · {sector}" if sector else ""
        lines.append(f"- **{r['name']}** ({r['symbol']}) [{r.get('exchange', '-')}, {r.get('type', '-')}]{sector_str}")
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_market")
async def get_us_market() -> str:
    """US market indices — 미국 주요 지수 스냅샷 (S&P 500, Dow, Nasdaq, VIX).
    "시장 지수", "S&P 지금", "Nasdaq 얼마", "VIX" 같은 질문에 사용합니다.
    """
    data = await us.get_market_summary()
    indices = data.get("indices", [])
    if not indices:
        return "지수 데이터를 가져올 수 없습니다."

    lines = ["**미국 주요 지수**", ""]
    lines.append("지수 | 심볼 | 현재 | 변동 | 변동률 | 상태")
    lines.append("---|---|---|---|---|---")
    for i in indices:
        price = i.get("price")
        change = i.get("change")
        chp = i.get("change_percent")
        price_s = f"{price:,.2f}" if isinstance(price, (int, float)) else "-"
        ch_s = f"{change:+,.2f}" if isinstance(change, (int, float)) else "-"
        chp_s = f"{chp:+.2f}%" if isinstance(chp, (int, float)) else "-"
        lines.append(f"{i['label']} | {i.get('symbol', '-')} | {price_s} | {ch_s} | {chp_s} | {i.get('market_state', '-')}")
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_screener")
async def get_us_screener(preset: str = "day_gainers", count: int = 20) -> str:
    """US stock screener — 미국 주식 프리셋 스크리너 (US predefined screener).
    "오늘 급등주", "top gainers", "가장 많이 거래된 종목", "저평가 성장주" 같은 질문에 사용합니다.

    사용 가능 preset: day_gainers, day_losers, most_actives, most_shorted_stocks,
    aggressive_small_caps, growth_technology_stocks, undervalued_growth_stocks,
    undervalued_large_caps, small_cap_gainers, conservative_foreign_funds

    Args:
        preset: 스크리너 ID (기본 day_gainers)
        count: 반환 종목 수 (기본 20)
    """
    data = await us.screen(preset, count=count)
    if data.get("error"):
        return f"⚠️ {data['error']}"

    quotes = data.get("quotes", [])
    if not quotes:
        return f"'{preset}' 결과 없음."

    lines = [
        f"**{data.get('title', preset)}** ({len(quotes)} / 전체 {data.get('total', '-')})",
    ]
    if data.get("description"):
        lines.append(f"_{data['description']}_")
    lines.append("")
    lines.append("티커 | 이름 | 현재가 | 변동% | 거래량 | 시총 | P/E")
    lines.append("---|---|---|---|---|---|---")
    for q in quotes:
        chp = q.get("change_percent")
        chp_s = f"{chp:+.2f}%" if isinstance(chp, (int, float)) else "-"
        vol = q.get("volume")
        vol_s = f"{vol/1_000_000:.2f}M" if isinstance(vol, (int, float)) else "-"
        mcap = q.get("market_cap")
        mcap_s = _fmt_num(mcap) if mcap else "-"
        pe = q.get("pe")
        pe_s = f"{pe:.2f}" if isinstance(pe, (int, float)) else "-"
        price = q.get("price")
        price_s = f"${price:,.2f}" if isinstance(price, (int, float)) else "-"
        name = (q.get("name") or "-")[:30]
        lines.append(f"{q['symbol']} | {name} | {price_s} | {chp_s} | {vol_s} | {mcap_s} | {pe_s}")
    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_financial_statement")
async def get_us_financial_statement(
    ticker: str,
    statement_type: str = "income",
    period: str = "annual",
) -> str:
    """US financial statements — 미국 주식 재무제표 3종 (income/balance/cash_flow).
    "AAPL 손익계산서", "NVDA 현금흐름표", "Apple balance sheet quarterly" 같은 질문에 사용합니다.

    핵심 row만 추출 (Total Revenue, Net Income, Total Assets, Free Cash Flow 등).

    Args:
        ticker: US 티커
        statement_type: "income" / "balance" / "cash_flow" (기본 income)
        period: "annual" / "quarterly" (기본 annual)
    """
    data = await us.get_financial_statement(ticker, statement_type=statement_type, period=period)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."
    if data.get("error"):
        return f"⚠️ {data['error']}"

    rows = data.get("rows", [])
    if not rows:
        return f"**{data['ticker']}** {statement_type} ({period}) — 데이터 없음."

    name_map = {"income": "손익계산서", "balance": "재무상태표", "cash_flow": "현금흐름표"}
    lines = [
        f"**{data['ticker']}** {name_map.get(statement_type, statement_type)} ({period})",
        f"통화: {data.get('currency', '-')}",
        "",
    ]

    # 기간 헤더 — 첫 row의 periods 사용
    periods = rows[0]["periods"]
    header = "항목 | " + " | ".join(periods)
    sep = "---|" + "---|" * len(periods)
    lines.append(header)
    lines.append(sep[:-1])

    for r in rows:
        vals = []
        for v in r["values"]:
            if v is None:
                vals.append("-")
            elif isinstance(v, (int, float)):
                vals.append(_fmt_num(v, digits=1))
            else:
                vals.append(str(v))
        lines.append(f"{r['item']} | " + " | ".join(vals))

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_sector")
async def get_us_sector(sector_key: str, top_n: int = 20) -> str:
    """US sector overview — 미국 섹터별 top 기업 + 시장 비중 (US sector top companies).
    "기술주 섹터", "healthcare top companies", "technology 대장주", "섹터 비중" 같은 질문에 사용합니다.

    Args:
        sector_key: technology, healthcare, financial-services, consumer-cyclical,
                    consumer-defensive, communication-services, industrials,
                    energy, basic-materials, utilities, real-estate
        top_n: top 기업 수 (기본 20)
    """
    data = await us.get_sector(sector_key, top_n=top_n)
    if data is None or data.get("error"):
        return f"⚠️ {data.get('error') if data else '섹터 조회 실패'}"

    lines = [
        f"**섹터: {sector_key}**",
        "",
        "## 📊 Overview",
    ]
    if data.get("companies_count"):
        lines.append(f"- 종목 수: {int(data['companies_count']):,}")
    if data.get("market_cap"):
        lines.append(f"- 시가총액: {_fmt_num(data['market_cap'])}")
    if data.get("market_weight"):
        lines.append(f"- 시장 비중: {data['market_weight']*100:.2f}%")
    if data.get("industries_count"):
        lines.append(f"- 산업 수: {int(data['industries_count'])}")
    if data.get("employee_count"):
        lines.append(f"- 총 고용: {int(data['employee_count']):,}명")

    desc = data.get("description")
    if desc:
        lines.append("")
        lines.append(f"_{desc[:400]}_")

    companies = data.get("top_companies", [])
    if companies:
        lines.append("")
        lines.append(f"## 🏢 Top {len(companies)} 기업")
        lines.append("순위 | 종목 | 투자의견 | 시장 비중")
        lines.append("---|---|---|---")
        for idx, c in enumerate(companies, 1):
            sym = c.get("symbol") or c.get("index") or "-"
            name = (c.get("name") or "-")[:35]
            rating = c.get("rating") or "-"
            mw = c.get("market_weight") or c.get("market weight")
            mw_s = f"{mw*100:.2f}%" if isinstance(mw, (int, float)) else "-"
            lines.append(f"{idx} | **{sym}** {name} | {rating} | {mw_s}")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_etf_info")
async def get_us_etf_info(ticker: str) -> str:
    """US ETF info — ETF 전용 상세 (top holdings, 섹터 비중, 자산 배분 · US ETF details).
    "SPY 구성종목", "QQQ holdings", "VOO 섹터", "ETF 보수" 같은 질문에 사용합니다.

    Args:
        ticker: ETF 티커 (예: "SPY", "QQQ", "VOO", "SCHD")
    """
    data = await us.get_etf_info(ticker)
    if data is None:
        return f"티커 '{ticker}'를 찾을 수 없습니다."
    if data.get("error"):
        return f"⚠️ {data['error']}"

    lines = [
        f"**{data.get('name', data['ticker'])}** ({data['ticker']})",
        f"카테고리: {data.get('category', '-')} · {data.get('family', '-')}",
    ]
    if data.get("expense_ratio") is not None:
        # yfinance netExpenseRatio는 이미 % 단위 (0.0945 = 0.0945%)
        lines.append(f"보수율: {data['expense_ratio']:.3f}%")
    if data.get("total_assets"):
        lines.append(f"운용자산(AUM): {_fmt_num(data['total_assets'])}")
    if data.get("ytd_return") is not None:
        # ytdReturn도 이미 % 단위
        lines.append(f"YTD 수익률: {data['ytd_return']:+.2f}%")

    ac = data.get("asset_classes") or {}
    if ac:
        lines.append("")
        lines.append("## 📊 자산 배분")
        for k, v in ac.items():
            if isinstance(v, (int, float)) and v > 0:
                lines.append(f"- {k}: {v*100:.2f}%")

    sw = data.get("sector_weightings") or {}
    if sw:
        lines.append("")
        lines.append("## 🏭 섹터 비중")
        for k, v in sorted(sw.items(), key=lambda x: -(x[1] if isinstance(x[1], (int, float)) else 0)):
            if isinstance(v, (int, float)) and v > 0:
                lines.append(f"- {k.replace('_', ' ').title()}: {v*100:.2f}%")

    th = data.get("top_holdings") or []
    if th:
        lines.append("")
        lines.append(f"## 🏆 Top Holdings ({len(th)}개)")
        lines.append("")
        lines.append(
            f"> ℹ️ **Yahoo Finance는 상위 {len(th)}개 보유 종목만 공개합니다.** "
            "ETF 전체 구성종목(예: SPY 500종목)은 발행사 공식 페이지에서 확인 가능합니다 "
            "(예: SPY → ssga.com, QQQ → invesco.com, SCHD → schwabassetmanagement.com)."
        )
        lines.append("")
        lines.append("종목 | 비중")
        lines.append("---|---")
        for h in th:
            sym = h.get("symbol") or h.get("index") or "-"
            name = h.get("name") or h.get("holding_name") or h.get("holding") or ""
            weight = h.get("holding_percent") or h.get("weight") or h.get("pct_held")
            w_s = f"{weight*100:.2f}%" if isinstance(weight, (int, float)) else "-"
            lines.append(f"**{sym}** {name} | {w_s}")

    desc = data.get("description")
    if desc:
        lines.append("")
        lines.append(f"_{desc[:400]}_")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("get_us_multi_price")
async def get_us_multi_price(tickers: list[str]) -> str:
    """US multi-ticker prices — 여러 미국 주식 일괄 가격 조회 (US multi-ticker snapshot).
    "AAPL MSFT NVDA 동시", "big tech 가격", "내 포트폴리오 현재가" 같은 질문에 사용합니다.

    Args:
        tickers: 티커 리스트 (예: ["AAPL", "MSFT", "NVDA"]). 최대 20개 권장.
    """
    if not tickers:
        return "티커 리스트가 비어있습니다."
    if len(tickers) > 30:
        tickers = tickers[:30]

    rows = await us.get_multi_prices(tickers)
    if not rows:
        return "데이터를 가져올 수 없습니다."

    lines = [f"**멀티 티커 스냅샷** ({len(rows)}개)", ""]
    lines.append("티커 | 이름 | 현재가 | 변동 | 변동% | 거래량 | 시총")
    lines.append("---|---|---|---|---|---|---")
    for r in rows:
        if r.get("error"):
            lines.append(f"{r['ticker']} | ⚠️ {r['error']} | - | - | - | - | -")
            continue
        price = r.get("price")
        price_s = f"${price:,.2f}" if isinstance(price, (int, float)) else "-"
        ch = r.get("change")
        ch_s = f"{ch:+,.2f}" if isinstance(ch, (int, float)) else "-"
        chp = r.get("change_percent")
        chp_s = f"{chp:+.2f}%" if isinstance(chp, (int, float)) else "-"
        vol = r.get("volume")
        vol_s = f"{vol/1_000_000:.1f}M" if isinstance(vol, (int, float)) else "-"
        mcap = r.get("market_cap")
        mcap_s = _fmt_num(mcap) if mcap else "-"
        name = (r.get("name") or "-")[:25]
        lines.append(f"**{r['ticker']}** | {name} | {price_s} | {ch_s} | {chp_s} | {vol_s} | {mcap_s}")

    return "\n".join(lines)


@mcp.tool()
@safe_us_tool
@track_metrics("export_us_to_excel")
async def export_us_to_excel(
    ticker: str,
    period: str = "10y",
    interval: str = "1d",
    filename: str = "",
) -> str:
    """US Excel export — 미국 주식 장기 데이터를 Excel 파일로 저장 (토큰 소비 없음).
    "AAPL 10년치 CSV 저장", "TSLA 5년 일봉 엑셀", "S&P 장기 데이터 파일로" 같은 질문에 사용합니다.

    ⭐ `get_us_chart`는 기본 500행 상한이라 10년치 장기 데이터 조회 시 잘립니다.
    **백테스트·CSV 분석·다른 AI 업로드용**이면 이 도구로 파일에 저장하세요. 행 수 무제한.

    저장 위치: `~/Downloads/kstock/` (Windows: `%USERPROFILE%\\Downloads\\kstock\\`)

    Args:
        ticker: US 티커 (예: "AAPL", "SPY", "BRK.B")
        period: "1d","5d","1mo","3mo","6mo","1y","2y","5y","10y","ytd","max" (기본 10y)
        interval: "1d","1wk","1mo" (기본 1d). 분봉은 기간 짧아서 파일 저장 의미 약함
        filename: 파일명 (비우면 자동)
    """
    rows = await us.get_history(ticker, period=period, interval=interval, prepost=False)
    if not rows:
        return f"티커 '{ticker}'의 데이터를 가져올 수 없습니다."

    df = pd.DataFrame(rows)
    prefix = f"us_chart_{ticker.upper().replace('.', '-')}_{period}_{interval}"
    fname = filename or generate_filename(prefix)
    if not fname.endswith(".xlsx"):
        fname += ".xlsx"
    file_path = get_snapshot_dir() / fname
    saved = save_dataframe_to_excel(
        df,
        file_path,
        sheet_name="OHLCV",
        source="Yahoo Finance (yfinance)",
        metadata={
            "ticker": ticker.upper(),
            "period": period,
            "interval": interval,
        },
    )

    return (
        f"✓ US Excel 저장 완료\n"
        f"티커: {ticker.upper()} · 기간: {period} · 간격: {interval}\n"
        f"경로: {saved}\n"
        f"행 수: {len(df)}\n"
        f"컬럼: {', '.join(df.columns)}\n\n"
        f"💡 이 파일을 엑셀·Gemini·ChatGPT 등에서 바로 분석 가능. Claude 토큰 소비 없음."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
