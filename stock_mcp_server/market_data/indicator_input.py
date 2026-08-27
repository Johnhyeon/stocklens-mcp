"""NormalizedBar 를 기존 지표 계산기(compute_indicators) 입력으로 변환한다.

기존 _indicators.py 는 {"date","open","high","low","close","volume"} dict
리스트를 받는다. 신규 코드는 계산기를 재작성하지 않고 입력만 맞춘다.
"""

from __future__ import annotations

from stock_mcp_server.market_data.models import NormalizedBar


def bars_to_ohlcv(bars: tuple[NormalizedBar, ...] | list[NormalizedBar]) -> list[dict]:
    """오름차순 봉을 지표 계산기용 OHLCV dict 로 변환한다.

    date 는 ISO timestamp 문자열이라 정렬이 보존된다. 가격은 float 로
    변환한다 (기존 계산기가 pandas 기반이라 Decimal 을 요구하지 않는다).
    """
    return [
        {
            "date": bar.start_at.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": bar.volume,
        }
        for bar in bars
    ]


def filter_completed(
    bars: tuple[NormalizedBar, ...],
) -> tuple[NormalizedBar, ...]:
    """완성 봉만 남긴다. complete=None(판정 불가)도 보수적으로 제외한다."""
    return tuple(bar for bar in bars if bar.complete is True)
