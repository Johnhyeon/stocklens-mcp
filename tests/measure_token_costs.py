"""Tool 응답 토큰 소모 실측.

모든 @mcp.tool() 함수를 대표 시나리오로 호출해 응답 길이를 측정합니다.
결과는 tests/token_costs.csv + 콘솔 표로 출력.

토큰 추정:
- Claude 토크나이저 기준 대략 한글 1자 ≈ 1 token, ASCII 4자 ≈ 1 token
- 정확하지 않지만 tool 간 상대 비교에 충분

실행:
    python tests/measure_token_costs.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import sys
import time
import traceback
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_mcp_server import server as S  # noqa: E402


KR_CODES_20 = [
    "005930", "000660", "373220", "207940", "005380",
    "000270", "068270", "035420", "035720", "051910",
    "005490", "028260", "105560", "055550", "012330",
    "006400", "096770", "066570", "017670", "032830",
]
KR_CODES_50 = KR_CODES_20 + [
    "003550", "015760", "033780", "086790", "034020",
    "010130", "090430", "011200", "018260", "010950",
    "009150", "024110", "267260", "011170", "030200",
    "047050", "251270", "021240", "097950", "036570",
    "078930", "000810", "316140", "138040", "180640",
    "011780", "010140", "005830", "011070", "009540",
]
KR_CODES_100 = KR_CODES_50 + KR_CODES_50  # 중복 허용 (서버가 허용하면)

US_10 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
         "TSLA", "META", "BRK-B", "UNH", "JNJ"]


def estimate_tokens(text: str) -> int:
    """한글≈1token/자, ASCII≈0.25token/자 기반 거친 추정."""
    korean = 0
    for c in text:
        if "\uac00" <= c <= "\ud7a3" or "\u3131" <= c <= "\u318e":
            korean += 1
    other = len(text) - korean
    return int(korean + other * 0.27)


SCENARIOS: list[tuple[str, callable, dict]] = [
    # --- KR 탐색/스냅샷 ---
    ("search('삼성')", S.search, {"query": "삼성"}),
    ("search_stock('카카오')", S.search_stock, {"query": "카카오"}),
    ("get_price(005930)", S.get_price, {"code": "005930"}),
    ("get_index()", S.get_index, {}),
    ("list_themes()", S.list_themes, {}),
    ("list_sectors()", S.list_sectors, {}),
    # --- KR 차트 ---
    ("get_chart default(120)", S.get_chart, {"code": "005930"}),
    ("get_chart count=500", S.get_chart, {"code": "005930", "count": 500}),
    ("get_flow default(20)", S.get_flow, {"code": "005930"}),
    ("get_flow days=60", S.get_flow, {"code": "005930", "days": 60}),
    ("get_financial", S.get_financial, {"code": "005930"}),
    # --- KR 랭킹/멀티 ---
    ("get_volume_ranking()", S.get_volume_ranking, {}),
    ("get_change_ranking()", S.get_change_ranking, {}),
    ("get_market_cap_ranking KOSPI/50", S.get_market_cap_ranking, {"market": "KOSPI", "count": 50}),
    ("get_multi_stocks x10", S.get_multi_stocks, {"codes": KR_CODES_20[:10]}),
    ("get_multi_stocks x20", S.get_multi_stocks, {"codes": KR_CODES_20}),
    ("get_multi_chart_stats x20", S.get_multi_chart_stats, {"codes": KR_CODES_20, "days": 260}),
    # --- KR 지표 ---
    ("get_indicators default", S.get_indicators, {"code": "005930"}),
    ("get_indicators FULL", S.get_indicators, {
        "code": "005930",
        "include": ["ma", "ma_phase", "ma_slope", "ma_cross", "rsi", "macd",
                    "bollinger", "stochastic", "obv", "volume", "position", "candle"],
    }),
    ("get_indicators +SR", S.get_indicators, {
        "code": "005930",
        "days": 500,
        "include": ["ma", "ma_phase", "rsi", "macd", "support_resistance",
                    "volume_profile", "price_channel"],
    }),
    ("get_indicators_bulk x30", S.get_indicators_bulk, {"codes": KR_CODES_50[:30]}),
    ("get_indicators_bulk x50", S.get_indicators_bulk, {"codes": KR_CODES_50}),
    # --- KR ETF/분석 ---
    ("get_etf_list default", S.get_etf_list, {}),
    ("get_etf_info(069500)", S.get_etf_info, {"code": "069500"}),
    ("get_consensus", S.get_consensus, {"code": "005930"}),
    ("get_reports count=5", S.get_reports, {"code": "005930"}),
    ("get_reports count=10", S.get_reports, {"code": "005930", "count": 10}),
    ("get_disclosure", S.get_disclosure, {"code": "005930"}),
    # --- US 탐색/스냅샷 ---
    ("get_us_search('Apple')", S.get_us_search, {"query": "Apple"}),
    ("get_us_price(AAPL)", S.get_us_price, {"ticker": "AAPL"}),
    ("get_us_info(AAPL)", S.get_us_info, {"ticker": "AAPL"}),
    ("get_us_market()", S.get_us_market, {}),
    ("get_us_screener day_gainers", S.get_us_screener, {"preset": "day_gainers", "count": 20}),
    ("get_us_sector technology", S.get_us_sector, {"sector_key": "technology", "top_n": 20}),
    ("get_us_multi_price x10", S.get_us_multi_price, {"tickers": US_10}),
    # --- US 차트 ---
    ("get_us_chart default(3mo)", S.get_us_chart, {"ticker": "AAPL"}),
    ("get_us_chart 2y limit=500", S.get_us_chart, {"ticker": "AAPL", "period": "2y", "limit": 500}),
    ("get_us_chart 5y limit=5000", S.get_us_chart, {"ticker": "AAPL", "period": "5y", "limit": 5000}),
    # --- US 재무/분석 ---
    ("get_us_financials", S.get_us_financials, {"ticker": "AAPL"}),
    ("get_us_financial_stmt income/annual", S.get_us_financial_statement, {"ticker": "AAPL"}),
    ("get_us_financial_stmt income/quarterly", S.get_us_financial_statement, {"ticker": "AAPL", "period": "quarterly"}),
    ("get_us_financial_stmt cashflow/quarterly", S.get_us_financial_statement, {"ticker": "AAPL", "statement_type": "cash_flow", "period": "quarterly"}),
    ("get_us_earnings", S.get_us_earnings, {"ticker": "AAPL"}),
    ("get_us_analyst", S.get_us_analyst, {"ticker": "AAPL"}),
    ("get_us_dividends", S.get_us_dividends, {"ticker": "AAPL"}),
    # --- US 옵션/공시 ---
    ("get_us_options default", S.get_us_options, {"ticker": "AAPL"}),
    ("get_us_insider", S.get_us_insider, {"ticker": "AAPL"}),
    ("get_us_holders", S.get_us_holders, {"ticker": "AAPL"}),
    ("get_us_short", S.get_us_short, {"ticker": "AAPL"}),
    ("get_us_filings(15)", S.get_us_filings, {"ticker": "AAPL"}),
    ("get_us_news(10)", S.get_us_news, {"ticker": "AAPL"}),
    ("get_us_etf_info(SPY)", S.get_us_etf_info, {"ticker": "SPY"}),
]


async def measure_one(name: str, fn, kwargs: dict) -> dict:
    t0 = time.perf_counter()
    try:
        result = await fn(**kwargs)
        if not isinstance(result, str):
            result = str(result)
    except Exception as e:
        return {
            "name": name,
            "error": f"{type(e).__name__}: {e}",
            "chars": 0,
            "bytes": 0,
            "est_tokens": 0,
            "elapsed_s": round(time.perf_counter() - t0, 2),
        }
    elapsed = time.perf_counter() - t0
    chars = len(result)
    utf8 = len(result.encode("utf-8"))
    is_error_msg = result.startswith("⚠️") or result.startswith("티커 '") or "없습니다" in result[:80]
    return {
        "name": name,
        "error": "ERROR_RESPONSE" if is_error_msg else "",
        "chars": chars,
        "bytes": utf8,
        "est_tokens": estimate_tokens(result),
        "elapsed_s": round(elapsed, 2),
    }


async def main():
    results = []
    total = len(SCENARIOS)
    for i, (name, fn, kwargs) in enumerate(SCENARIOS, 1):
        print(f"[{i}/{total}] {name}...", flush=True)
        r = await measure_one(name, fn, kwargs)
        tag = f"ERR ({r['error'][:40]})" if r["error"] else "OK"
        print(f"    {tag}  {r['chars']:>6} chars | {r['est_tokens']:>5} tok | {r['elapsed_s']}s", flush=True)
        results.append(r)
        await asyncio.sleep(0.3)

    out_csv = Path(__file__).parent / "token_costs.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "est_tokens", "chars", "bytes", "elapsed_s", "error"])
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nSaved: {out_csv}")

    print("\n" + "=" * 78)
    print(f"{'Tool':<40} {'est_tok':>8} {'chars':>8} {'bytes':>8} {'sec':>6}")
    print("=" * 78)
    for r in sorted(results, key=lambda x: x["est_tokens"], reverse=True):
        flag = " *" if r["error"] else "  "
        print(f"{r['name']:<40} {r['est_tokens']:>8} {r['chars']:>8} {r['bytes']:>8} {r['elapsed_s']:>6}{flag}")
    print("=" * 78)
    print("(* = error or empty response — 값이 실제보다 작게 측정됨, 실측 재시도 필요)")

    ok = [r for r in results if not r["error"]]
    if ok:
        ok_tokens = sorted([r["est_tokens"] for r in ok])
        total_tok = sum(ok_tokens)
        print(f"\n성공 tool {len(ok)}개 · 합계 ≈ {total_tok:,} tokens")
        print(f"중앙값 {ok_tokens[len(ok_tokens)//2]:,} · 상위 5개 합계 {sum(ok_tokens[-5:]):,}")


if __name__ == "__main__":
    asyncio.run(main())
