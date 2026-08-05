"""네이버 테마/업종/랭킹 API 수동 스모크 테스트 (v2 확장분).

pytest가 아니라 직접 실행해서 실제 응답을 눈으로 확인하는 개발용 스크립트.
"test_" 접두사를 쓰지 않는 이유: pytest test discovery에 걸려 CI/로컬 테스트
실행 시 실제 네트워크 호출이 섞여 들어가는 걸 막기 위함.

실행:
    python tests/dev_naver_v2_smoke.py
"""

import asyncio
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from stock_mcp_server.naver import (
    list_themes,
    get_theme_stocks,
    list_sectors,
    get_sector_stocks,
    get_volume_ranking,
    get_change_ranking,
    get_market_cap_ranking,
)


async def main():
    print("=" * 60)
    print("TEST 1: list_themes(page=1)")
    print("=" * 60)
    themes = await list_themes(page=1)
    print(f"총 {len(themes)}개 테마")
    for t in themes[:3]:
        print(f"  {t['name']} ({t['theme_id']}) - {t['change_rate']}")

    print()
    print("=" * 60)
    print("TEST 2: get_theme_stocks('AI')")
    print("=" * 60)
    result = await get_theme_stocks("AI")
    print(f"테마: {result['theme_name']} / 종목 {len(result['stocks'])}개")
    for s in result["stocks"][:3]:
        print(f"  {s['code']} {s['name']}: {s['price']:,}원")

    print()
    print("=" * 60)
    print("TEST 3: list_sectors()")
    print("=" * 60)
    sectors = await list_sectors()
    print(f"총 {len(sectors)}개 업종")
    for s in sectors[:3]:
        print(f"  {s['name']} ({s['sector_id']}) - {s['change_rate']}")

    print()
    print("=" * 60)
    print("TEST 4: get_sector_stocks('통신장비')")
    print("=" * 60)
    result = await get_sector_stocks("통신장비")
    print(f"업종: {result['sector_name']} / 종목 {len(result['stocks'])}개")
    for s in result["stocks"][:3]:
        print(f"  {s['code']} {s['name']}: {s['price']:,}원 ({s['change_rate']})")

    print()
    print("=" * 60)
    print("TEST 5: get_volume_ranking(market='KOSPI', count=10)")
    print("=" * 60)
    ranks = await get_volume_ranking(market="KOSPI", count=10)
    print(f"총 {len(ranks)}개")
    for r in ranks:
        print(f"  #{r['rank']} {r['code']} {r['name']}: 거래량 {r['volume']:,}")

    print()
    print("=" * 60)
    print("TEST 6: get_volume_ranking(market='ALL', count=10)")
    print("=" * 60)
    ranks = await get_volume_ranking(market="ALL", count=10)
    print(f"총 {len(ranks)}개 (KOSPI+KOSDAQ 병합)")
    for r in ranks:
        print(f"  #{r['rank']} {r['code']} {r['name']}: 거래량 {r['volume']:,}")

    print()
    print("=" * 60)
    print("TEST 7: get_change_ranking(direction='up', count=10)")
    print("=" * 60)
    ranks = await get_change_ranking(direction="up", market="ALL", count=10)
    print(f"총 {len(ranks)}개 (상승률 순)")
    for r in ranks:
        print(f"  #{r['rank']} {r['code']} {r['name']}: {r['change_rate']}")

    print()
    print("=" * 60)
    print("TEST 8: get_change_ranking(direction='down', count=5)")
    print("=" * 60)
    ranks = await get_change_ranking(direction="down", market="ALL", count=5)
    print(f"총 {len(ranks)}개 (하락률 순)")
    for r in ranks:
        print(f"  #{r['rank']} {r['code']} {r['name']}: {r['change_rate']}")

    print()
    print("=" * 60)
    print("TEST 9: get_market_cap_ranking(market='KOSPI', count=10)")
    print("=" * 60)
    ranks = await get_market_cap_ranking(market="KOSPI", count=10)
    print(f"총 {len(ranks)}개")
    for r in ranks:
        cap_trillion = r["market_cap_billion"] / 10000
        print(f"  #{r['rank']} {r['code']} {r['name']}: {cap_trillion:.1f}조원")


if __name__ == "__main__":
    asyncio.run(main())
