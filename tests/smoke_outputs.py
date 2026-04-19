"""각 MCP tool을 실제 호출하여 출력을 덤프하고 라벨/맥락 누락을 육안 검토."""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "D:/project/stock-mcp-server")

from stock_mcp_server import naver, server


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"=== {title}")
    print("=" * 70)


async def safe(fn, *args, **kw):
    try:
        out = await fn(*args, **kw)
    except Exception as e:
        return f"[EXCEPTION] {type(e).__name__}: {e}"
    return out


async def main() -> None:
    # 1) search — 005930 삼성전자
    banner("search('삼성전자')")
    print(await safe(naver.search_stock, "삼성전자"))

    # 2) get_price — 정상 종목
    banner("get_price via naver.get_current_price('005930')")
    print(await safe(naver.get_current_price, "005930"))

    # 3) get_chart (일봉)
    banner("get_ohlcv('005930', 'day', 5)")
    print(await safe(naver.get_ohlcv, "005930", "day", 5))

    # 4) 투자자 수급
    banner("get_investor_flow('005930', days=5)")
    print(await safe(naver.get_investor_flow, "005930", 5))

    # 5) 시장 지수
    banner("get_market_index()")
    print(await safe(naver.get_market_index))

    # 6) 테마 상세 종목
    banner("get_theme_stocks('AI반도체', 5)")
    print(await safe(naver.get_theme_stocks, "AI반도체", 5))

    # 7) 업종 목록
    banner("list_sectors() [first 3]")
    sectors = await safe(naver.list_sectors)
    print(sectors[:3] if isinstance(sectors, list) else sectors)

    # 8) 업종 소속 종목
    banner("get_sector_stocks('반도체', 5)")
    print(await safe(naver.get_sector_stocks, "반도체", 5))

    # 9) 거래량 랭킹
    banner("get_volume_ranking(market='KOSPI', count=3)")
    print(await safe(naver.get_volume_ranking, "KOSPI", 3))

    # 10) 등락률 랭킹
    banner("get_change_ranking(direction='up', market='KOSPI', count=3)")
    print(await safe(naver.get_change_ranking, "up", "KOSPI", 3))

    # 11) 시가총액 랭킹
    banner("get_market_cap_ranking('KOSPI', 3)")
    print(await safe(naver.get_market_cap_ranking, "KOSPI", 3))

    # 12) 엣지: 알파벳 포함 종목 (메쥬 0088M0)
    banner("get_stock_info('0088M0') — 알파벳 포함")
    print(await safe(naver.get_current_price, "0088M0"))

    banner("get_financials('0088M0')")
    fin = await safe(naver.get_financials, "0088M0")
    if isinstance(fin, dict):
        for k, v in list(fin.items())[:10]:
            print(f"  {k}: {v}")
    else:
        print(fin)

    # 13) 엣지: 존재하지 않는 코드
    banner("get_current_price('999999')")
    print(await safe(naver.get_current_price, "999999"))


asyncio.run(main())
