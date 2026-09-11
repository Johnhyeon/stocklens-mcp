# -*- coding: utf-8 -*-
"""네이버 JSON API 전환 실측 스모크 (수동 실행용, 네트워크 필요).

2026-09 네이버가 finance.naver.com 화면을 폐지한 뒤 국내 도구가 통째로 죽었다.
같은 사고를 다시 놓치지 않으려고, 도구가 **실제 값을 돌려주는지**를 사람이 눈으로
확인하는 자리를 남긴다. 단위 테스트는 응답을 흉내 내므로 원천이 사라진 것을
잡지 못한다 — 그게 이번 사고에서 테스트가 전부 초록이던 이유다.

    python tests/dev_naver_json_smoke.py
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from stock_mcp_server import naver  # noqa: E402

CODE = "005930"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


async def main() -> int:
    price = await naver.get_current_price(CODE)
    check("get_current_price", bool(price.get("price")) and not price[naver.PARSE_MISS_KEY],
          f"{price.get('name')} {price.get('price'):,} ({price.get('quote_date')})")

    flow = await naver.get_investor_flow(CODE, days=5)
    check("get_investor_flow", len(flow) == 5 and "individual" in flow[0],
          f"{flow[0]['date']} 기관 {flow[0]['institutional']:+,} 외국인 {flow[0]['foreign']:+,}"
          if flow else "빈 결과")

    fin = await naver.get_financials(CODE)
    periods = fin.get("_periods") or {}
    check("get_financials", bool(periods.get("annual")) and bool(fin.get("PER(배)")),
          f"연간 {periods.get('annual')} / 분기 {periods.get('quarterly')}")

    themes = await naver.list_themes(1)
    check("list_themes", len(themes) > 10, f"{len(themes)}개, 1위 {themes[0]['name']}" if themes else "")

    theme = await naver.get_theme_stocks("반도체", count=3)
    check("get_theme_stocks", bool(theme["stocks"]), f"{theme['theme_name']} {len(theme['stocks'])}종목")

    sectors = await naver.list_sectors()
    check("list_sectors", len(sectors) > 50, f"{len(sectors)}개")

    sector = await naver.get_stock_sector(CODE)
    check("get_stock_sector", bool(sector.get("sector_name")),
          f"{sector.get('sector_name')} (PER {sector.get('per_ttm')})")

    sec_stocks = await naver.get_sector_stocks("반도체", count=3)
    check("get_sector_stocks", bool(sec_stocks["stocks"]), sec_stocks["sector_name"] or "")

    multi = await naver.get_multi_stocks([CODE, "000660", "035420"])
    check("get_multi_stocks", len(multi) == 3, ", ".join(m["name"] for m in multi))

    vol = await naver.get_volume_ranking(count=5)
    check("get_volume_ranking", len(vol) == 5, f"1위 {vol[0]['name']} {vol[0]['volume']:,}주" if vol else "")

    up = await naver.get_change_ranking("up", "ALL", 5)
    check("get_change_ranking", len(up) == 5, f"1위 {up[0]['name']} {up[0]['change_rate']}" if up else "")

    cap = await naver.get_market_cap_ranking("KOSPI", 5)
    check("get_market_cap_ranking", len(cap) == 5 and cap[0]["market_cap_billion"] > 0,
          f"1위 {cap[0]['name']} {cap[0]['market_cap_billion']:,}억원" if cap else "")

    idx = await naver.get_market_index()
    check("get_market_index", all(i.get("value") for i in idx),
          " / ".join(f"{i['index']} {i.get('value')}" for i in idx))

    reports = await naver.get_reports(CODE, 3)
    check("get_reports", bool(reports), f"{reports[0]['broker']} {reports[0]['title']}" if reports else "")

    if reports:
        detail = await naver.get_report_detail(reports[0]["nid"])
        check("get_report_detail", bool(detail.get("summary")),
              f"목표가 {detail.get('target_price')} {detail.get('opinion')}")

    alerts = await naver.get_alert_codes()
    check("get_alert_codes", bool(alerts), f"{len(alerts)}종목")

    # 손대지 않은 경로 — 회귀 확인
    check("get_ohlcv", len(await naver.get_ohlcv(CODE, count=3)) == 3)
    check("search_stock", bool(await naver.search_stock("삼성전자")))
    check("get_disclosure_list", bool(await naver.get_disclosure_list(CODE)))
    check("get_consensus", bool((await naver.get_consensus(CODE)).get("target_price")))

    print()
    if FAILURES:
        print(f"실패 {len(FAILURES)}건: {', '.join(FAILURES)}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
