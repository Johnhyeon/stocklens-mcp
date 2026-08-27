"""세션 안전 집계. 검증된 세션 open 에 버킷을 고정한다.

금지: 자정 기준 floor, 이전 종가 forward fill, 다음 거래일·세션과의 병합.

산식:
- 시가 = 구간 첫 유효 행 open
- 고가 = 구간 최대 high
- 저가 = 구간 최소 low
- 종가 = 구간 마지막 유효 행 close
- 거래량 = 구간 합
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from stock_mcp_server.market_data.models import (
    SUPPORTED_INTERVALS,
    BarDataset,
    NormalizedBar,
)
from stock_mcp_server.market_data.sessions import SessionWindow, session_window


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name}: timezone-aware datetime이 필요합니다")


def _bucketize(
    window: SessionWindow,
    bars: list[NormalizedBar],
    minutes: int,
    source_minutes: int,
    interval: str,
    now: datetime,
) -> list[NormalizedBar]:
    """한 거래일 세션 안에서만 버킷을 만든다."""
    buckets: dict[int, list[NormalizedBar]] = {}
    for bar in bars:
        offset = int((bar.start_at - window.open_at).total_seconds() // 60)
        buckets.setdefault(offset // minutes, []).append(bar)

    out: list[NormalizedBar] = []
    for index in sorted(buckets):
        rows = sorted(buckets[index], key=lambda b: b.start_at)
        bucket_start = window.open_at + timedelta(minutes=index * minutes)
        nominal_end = bucket_start + timedelta(minutes=minutes)
        bucket_end = min(nominal_end, window.close_at)
        actual_minutes = int(
            (bucket_end - bucket_start).total_seconds() // 60)
        session_tail = nominal_end > window.close_at

        expected_rows = actual_minutes // source_minutes
        have_rows = len(rows)
        sources_partial = any(
            b.data_integrity != "complete" for b in rows)
        if have_rows < expected_rows or sources_partial:
            data_integrity = "partial"
            gap_status = "unknown_gap"
        else:
            data_integrity = "complete"
            gap_status = "none"

        out.append(NormalizedBar(
            start_at=bucket_start,
            end_at=bucket_end,
            open=rows[0].open,
            high=max(b.high for b in rows),
            low=min(b.low for b in rows),
            close=rows[-1].close,
            volume=sum(b.volume for b in rows),
            interval=interval,
            session="regular",
            complete=now >= bucket_end,
            session_tail=session_tail,
            expected_minutes=minutes,
            actual_minutes=actual_minutes,
            data_integrity=data_integrity,
            source_gap_status=gap_status,
        ))
    return out


def resample_intraday(
    dataset: BarDataset,
    interval: str,
    *,
    now: datetime,
) -> BarDataset:
    """1분(또는 약수) 원천을 세션 기준으로 상위 간격에 집계한다."""
    _require_aware(now, "now")

    target_minutes = SUPPORTED_INTERVALS.get(interval)
    if target_minutes is None:
        raise ValueError(f"지원하지 않는 interval: {interval}")
    source_minutes = SUPPORTED_INTERVALS.get(dataset.source_interval)
    if source_minutes is None:
        raise ValueError(
            f"집계할 수 없는 source_interval: {dataset.source_interval}")
    if target_minutes % source_minutes != 0:
        raise ValueError(
            f"{interval}은 {dataset.source_interval}의 배수가 아니어서 "
            "집계할 수 없습니다")

    warnings: list[str] = list(dataset.warnings)
    by_day: dict[date, list[NormalizedBar]] = {}
    dropped_out_of_session = 0
    dropped_non_trading = 0

    for bar in dataset.bars:
        day = bar.start_at.date()
        window = session_window(dataset.market, day)
        if window is None:
            dropped_non_trading += 1
            continue
        if bar.start_at < window.open_at or bar.start_at >= window.close_at:
            dropped_out_of_session += 1
            continue
        by_day.setdefault(day, []).append(bar)

    if dropped_out_of_session:
        warnings.append(
            f"세션 밖 행 {dropped_out_of_session}개를 집계에서 제외했습니다.")
    if dropped_non_trading:
        warnings.append(
            f"비거래일 행 {dropped_non_trading}개를 집계에서 제외했습니다.")

    result: list[NormalizedBar] = []
    for day in sorted(by_day):
        window = session_window(dataset.market, day)
        result.extend(_bucketize(
            window, by_day[day], target_minutes, source_minutes,
            interval, now))

    return BarDataset(
        bars=tuple(result),
        market=dataset.market,
        symbol=dataset.symbol,
        provider=dataset.provider,
        profile=dataset.profile,
        venue=dataset.venue,
        timezone=dataset.timezone,
        session=dataset.session,
        requested_interval=interval,
        source_interval=dataset.source_interval,
        aggregation_method=(
            "provider_native" if interval == dataset.source_interval
            else "stocklens_session_resample"),
        adjustment_basis=dataset.adjustment_basis,
        source_endpoint=dataset.source_endpoint,
        coverage=dict(dataset.coverage),
        warnings=tuple(warnings),
    )
