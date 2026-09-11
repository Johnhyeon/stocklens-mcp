# -*- coding: utf-8 -*-
"""1.0 전 기능 점검 — MCP 도구 68개를 하나씩 실제로 호출한다.

단위 테스트는 응답을 흉내 내므로 원천이 사라진 것을 못 잡는다. 2026-09 네이버
개편 때 테스트가 전부 초록인 채로 국내 도구가 전멸했던 이유가 그것이다.
여기서는 **실제 네트워크로** 도구를 하나씩 부르고, 돌아온 글에 실패 표지가
있는지까지 본다.

    python docs/release/v1_tool_sweep.py [--home <STOCKLENS_HOME>] [--only <이름조각>]

`--home` 을 주지 않으면 임시 홈을 만들어 쓴다. 증권사 연결이 필요한 도구를
보려면 연결된 홈(예: .uat-home-1.0)을 복사해 넘긴다.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 돌아온 글에 이게 있으면 그 호출은 실패다. 도구가 예외를 삼키고 안내문만
# 돌려주는 경로가 많아서, 예외 없음만으로는 동작 확인이 되지 않는다.
#
# 단, **메타 봉투 안은 보지 않는다.** 거기 실린 warnings 는 정상 동작의 일부다
# ("발표 시각을 실적 일정에서 찾지 못했습니다" 같은 안내가 들어 있어서, 본문과
# 같이 훑으면 멀쩡한 도구가 실패로 잡힌다). 대신 메타의 data_completeness 가
# none 이면 그건 도구 스스로 "아무것도 못 채웠다"고 말한 것이라 실패로 센다.
FAIL_MARKS = (
    "가져올 수 없습니다",
    "파싱 실패",
    "읽지 못했습니다",
    "찾지 못했습니다",
    "처리 중 오류",
    "Traceback",
)

# 자료가 원래 없을 수 있어 비어도 실패로 세지 않는 도구. 이유를 함께 적는다.
SOFT = {
    "get_us_options": "만기·행사가가 없는 종목이 있다",
    "get_us_short": "공매도 공시가 없는 종목이 있다",
    "get_us_insider": "내부자 거래가 없는 분기가 있다",
    "get_event_reaction": "그날 공시가 없을 수 있다",
    "get_metrics_summary": "기록이 비어 있으면 빈 요약이 정상이다",
    "watchlist": "관심종목이 비어 있는 게 기본 상태다",
    "get_report_content": "mode=link 는 본문을 안 읽는 게 정상이다",
}


def build_calls(ctx: dict) -> list[tuple[str, dict]]:
    """(도구 이름, 인자). 앞선 호출 결과가 필요한 인자는 ctx 에서 꺼낸다."""
    kr, kr2, us = "005930", "000660", "AAPL"
    return [
        # ── 국내 시세·검색 ─────────────────────────────
        ("stocklens_status", {}),
        ("get_market_clock", {}),
        ("search", {"query": "삼성전자"}),
        ("search_stock", {"query": "삼성전자"}),
        ("get_price", {"code": kr}),
        ("get_multi_stocks", {"codes": [kr, kr2, "035420"]}),
        ("get_index", {}),
        ("get_chart", {"code": kr, "timeframe": "day", "count": 30}),
        ("get_indicators", {"code": kr, "days": 120}),
        ("get_indicators_bulk", {"codes": [kr, kr2], "days": 120}),
        ("get_multi_chart_stats", {"codes": [kr, kr2], "days": 120}),
        # ── 국내 재무·수급·공시 ────────────────────────
        ("get_financial", {"code": kr}),
        ("get_financial_batch", {"codes": [kr, kr2]}),
        ("get_financial_soundness", {"symbol": "105560"}),
        ("get_flow", {"code": kr, "days": 5}),
        ("get_flow_batch", {"codes": [kr, kr2], "days": 5}),
        ("screen_by_flow", {"market": "KOSPI", "top_n": 3, "foreign_days": 1,
                            "inst_days": 0}),
        ("get_disclosure", {"code": kr}),
        ("get_consensus", {"code": kr}),
        ("get_reports", {"code": kr, "count": 2}),
        ("get_report_content", {"nid": ctx.get("nid", "0"), "mode": "link"}),
        ("get_event_reactions", {"code": kr, "max_events": 2, "after": 3}),
        ("get_event_reaction", {"code": kr, "event_date": ctx.get("event_date", "2026-08-21"),
                                "after": 3}),
        # ── 국내 목록·랭킹 ─────────────────────────────
        ("list_themes", {"page": 1}),
        ("get_theme_stocks", {"theme_name": "반도체", "count": 3}),
        ("list_sectors", {}),
        ("get_sector_stocks", {"sector_name": "반도체", "count": 3}),
        ("get_sector_valuation", {"code": kr, "top_n": 3}),
        ("get_change_ranking", {"direction": "up", "market": "ALL", "count": 3}),
        ("get_volume_ranking", {"market": "ALL", "count": 3}),
        ("get_market_cap_ranking", {"market": "KOSPI", "count": 3}),
        ("get_etf_list", {"limit": 3}),
        ("get_etf_info", {"code": "069500"}),
        ("watchlist", {"action": "list"}),
        ("get_metrics_summary", {"days": 7}),
        # ── 1.0 분봉 (증권사 연결 구간) ─────────────────
        ("get_intraday_chart", {"symbol": kr, "market": "KR", "interval": "5m",
                                "row_limit": 20}),
        ("get_intraday_indicators", {"symbol": kr, "market": "KR", "interval": "60m",
                                     "bars": 60}),
        ("get_intraday_chart", {"symbol": us, "market": "US", "interval": "5m",
                                "row_limit": 20, "venue": "NAS"}),
        ("get_intraday_indicators", {"symbol": us, "market": "US", "interval": "60m",
                                     "bars": 60, "venue": "NAS"}),
        # ── 1.1 상세 수급 (증권사 연결 구간) ────────────
        ("get_detailed_investor_flow", {"code": kr, "days": 10}),
        ("get_detailed_investor_flow", {"codes": [kr, kr2], "days": 5,
                                        "measure": "net_amount"}),
        ("get_supply_pressure", {"code": kr, "kind": "program_trading",
                                 "days": 10}),
        ("get_supply_pressure", {"code": kr, "kinds": ["short_selling", "credit"],
                                 "days": 10}),
        # ── 미국 ───────────────────────────────────────
        ("get_us_market", {}),
        ("get_us_search", {"query": "apple"}),
        ("get_us_price", {"ticker": us}),
        ("get_us_multi_price", {"tickers": [us, "MSFT"]}),
        ("get_us_info", {"ticker": us}),
        ("get_us_chart", {"ticker": us, "period": "3mo", "interval": "1d"}),
        ("get_us_financials", {"ticker": us}),
        ("get_us_financial_statement", {"ticker": us, "statement_type": "income"}),
        ("get_us_earnings", {"ticker": us}),
        ("get_us_analyst", {"ticker": us}),
        ("get_us_dividends", {"ticker": us, "limit": 3}),
        ("get_us_holders", {"ticker": us}),
        ("get_us_insider", {"ticker": us}),
        ("get_us_liquidity", {"ticker": us}),
        ("get_us_short", {"ticker": us}),
        ("get_us_options", {"ticker": us}),
        ("get_us_news", {"ticker": us, "limit": 3}),
        ("get_us_sector", {"sector_key": "technology", "top_n": 3}),
        ("get_us_screener", {"preset": "gainers", "count": 3}),
        ("get_us_multi_diagnosis", {"tickers": [us, "MSFT"]}),
        ("get_us_etf_info", {"ticker": "SPY"}),
        ("get_us_filings", {"ticker": us, "limit": 3}),
        ("get_us_filing_detail", {"ticker": us,
                                  "accession_no": ctx.get("accession_no", "0"),
                                  "find": "revenue"}),
        # 주말 날짜를 넣으면 "거래일을 찾지 못했습니다"가 나온다. 그건 도구가
        # 고장난 게 아니라 점검 쪽 실수다. 평일을 고정으로 쓴다.
        ("get_us_event_reaction", {"ticker": us,
                                   "event_date": ctx.get("us_event_date", "2026-07-31"),
                                   "after": 3}),
        # ── 엑셀 ───────────────────────────────────────
        ("export_to_excel", {"data_type": "chart", "code": kr, "days": 30}),
        ("export_us_to_excel", {"ticker": us, "period": "1mo"}),
        ("scan_to_excel", {"codes": [kr, kr2], "fields": ["price", "chart"], "days": 60}),
        ("save_analysis_to_excel", {"title": "점검", "rows": [{"코드": kr, "메모": "확인"}]}),
        ("query_excel", {"file_path": ctx.get("xlsx", ""), "limit": 3}),
    ]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=os.environ.get("SWEEP_HOME"))
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    tmp = None
    if args.home:
        tmp = Path(tempfile.mkdtemp(prefix="sweep-home-"))
        home = tmp / "home"
        shutil.copytree(args.home, home)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="sweep-home-"))
        home = tmp / "home"
        home.mkdir(parents=True)
    os.environ["STOCKLENS_HOME"] = str(home)

    import logging
    logging.disable(logging.INFO)
    from stock_mcp_server import server

    ctx: dict = {}
    # 뒤 호출이 필요로 하는 값(리포트 nid, 공시 accession)을 먼저 채운다.
    try:
        import re as _re
        reports = await server.get_reports(code="005930", count=1)
        m = _re.search(r'nid="?(\d+)"?', str(reports))
        if m:
            ctx["nid"] = m.group(1)
    except Exception:
        pass
    try:
        import re
        filings = await server.get_us_filings(ticker="AAPL", limit=1)
        m = re.search(r"\b(\d{10}-\d{2}-\d{6})\b", str(filings))
        if m:
            ctx["accession_no"] = m.group(1)
    except Exception:
        pass

    results = []
    for name, kwargs in build_calls(ctx):
        if args.only and args.only not in name:
            continue
        fn = getattr(server, name, None)
        if fn is None:
            results.append((name, "MISSING", 0.0, "서버에 그 도구가 없다"))
            continue
        t0 = time.time()
        try:
            out = await asyncio.wait_for(fn(**kwargs), timeout=120)
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            body = text.split("RESULT_META_JSON_START")[0]
            completeness = None
            if "RESULT_META_JSON_START" in text:
                raw = text.split("RESULT_META_JSON_START")[1]
                raw = raw.split("RESULT_META_JSON_END")[0].strip()
                try:
                    completeness = json.loads(raw).get("data_completeness")
                except Exception:
                    completeness = None
            hit = next((m for m in FAIL_MARKS if m in body), None)
            if hit:
                verdict = "SOFT" if name in SOFT else "FAIL"
                detail = f"'{hit}' 포함"
            elif completeness == "none":
                verdict = "SOFT" if name in SOFT else "FAIL"
                detail = "data_completeness=none (도구가 아무것도 못 채웠다)"
            elif len(body.strip()) < 20:
                verdict = "SOFT" if name in SOFT else "FAIL"
                detail = f"본문이 너무 짧다({len(body.strip())}자)"
            else:
                verdict = "OK"
                detail = f"{len(body)}자" + (f" ({completeness})" if completeness else "")
            results.append((name, verdict, time.time() - t0, detail))
            # 엑셀 경로를 뒤 호출(query_excel)에 넘긴다.
            if name.endswith("_to_excel") and "경로:" in text and "xlsx" not in ctx:
                ctx["xlsx"] = text.split("경로:")[1].splitlines()[0].strip()
        except Exception as exc:
            results.append((name, "ERROR", time.time() - t0,
                            f"{type(exc).__name__}: {exc}"))

    print()
    print(f"{'도구':34} {'판정':6} {'초':>6}  비고")
    print("-" * 96)
    for name, verdict, sec, detail in results:
        print(f"{name:34} {verdict:6} {sec:6.1f}  {detail[:52]}")

    bad = [r for r in results if r[1] in ("FAIL", "ERROR", "MISSING")]
    soft = [r for r in results if r[1] == "SOFT"]
    print("-" * 96)
    print(f"통과 {len(results) - len(bad) - len(soft)} / 주의 {len(soft)} / 실패 {len(bad)}"
          f"  (총 {len(results)})")
    if soft:
        print("\n주의(자료 없음이 정상일 수 있는 것):")
        for name, _, _, detail in soft:
            print(f"  {name}: {detail} — {SOFT.get(name, '')}")
    if bad:
        print("\n실패:")
        for name, verdict, _, detail in bad:
            print(f"  [{verdict}] {name}: {detail}")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
