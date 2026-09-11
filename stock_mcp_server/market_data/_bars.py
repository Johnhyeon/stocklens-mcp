"""봉 시퀀스 공통 정규화. 역순 응답 오름차순 정렬, 중복 timestamp 제거."""

from __future__ import annotations

from stock_mcp_server.market_data.models import NormalizedBar


def sort_and_dedupe(
    bars: list[NormalizedBar],
) -> tuple[tuple[NormalizedBar, ...], list[str]]:
    """오름차순 정렬 후 같은 start_at 중복을 제거한다(첫 봉 유지).

    중복 제거는 경고로 남긴다. 조용히 버리면 페이지 경계 중복과
    공급자 오류를 구분할 수 없다.
    """
    warnings: list[str] = []
    ordered = sorted(bars, key=lambda b: b.start_at)
    result: list[NormalizedBar] = []
    dropped = 0
    for bar in ordered:
        if result and bar.start_at == result[-1].start_at:
            dropped += 1
            continue
        result.append(bar)
    if dropped:
        warnings.append(f"중복 timestamp 봉 {dropped}개를 제거했습니다.")
    return tuple(result), warnings
