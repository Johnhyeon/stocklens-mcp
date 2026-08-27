"""기존 yfinance_source.get_history 를 감싸는 어댑터. 원본 함수는 변경하지 않는다.

get_history 는 auto_adjust=False 로 호출되므로 adjustment_basis 는 unadjusted 다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_mcp_server.yfinance_source import get_history
from stock_mcp_server.market_data._bars import sort_and_dedupe
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)

_NY = ZoneInfo("America/New_York")

# StockLens interval -> yfinance interval
_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h",
    "1d": "1d", "1wk": "1wk", "1mo": "1mo",
}

_INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

# 요청 범위 지정이 없을 때의 조회 구간. coverage 에 그대로 기록한다.
_DEFAULT_PERIOD = {
    "1m": "5d", "5m": "5d", "15m": "5d", "30m": "5d", "60m": "5d",
    "1d": "6mo", "1wk": "2y", "1mo": "10y",
}

_US_OPEN = time(9, 30)
_US_CLOSE = time(16, 0)
_US_SESSION_MINUTES = 390


def _to_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class YahooBarProvider:
    provider_id = "yahoo"

    async def capabilities(self, profile: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider_id,
            contract_version=1,
            markets=("US",),
            venues=("NYS", "NAS", "AMS"),
            native_intervals=("1m", "5m", "15m", "30m", "60m",
                              "1d", "1wk", "1mo"),
            verified_intervals=("1m", "5m", "15m", "30m", "60m",
                                "1d", "1wk", "1mo"),
            sessions=("regular",),
            adjustment_modes=("unadjusted",),
            max_rows_per_call=None,
            historical_limit=None,
        )

    async def fetch_bars(self, request: BarRequest) -> BarDataset:
        yf_interval = _INTERVAL_MAP.get(request.interval)
        if yf_interval is None:
            raise ValueError(
                f"yahoo 어댑터가 지원하지 않는 interval: {request.interval}")
        if request.market != "US":
            raise ValueError(f"yahoo 어댑터는 US 전용입니다: {request.market}")
        if request.session != "regular":
            raise ValueError(
                f"yahoo 어댑터가 검증하지 않은 session: {request.session}")

        period = _DEFAULT_PERIOD[request.interval]
        rows = await get_history(
            request.symbol, period=period, interval=yf_interval, prepost=False)

        intraday_minutes = _INTRADAY_MINUTES.get(request.interval)
        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped_unparsable = 0
        dropped_missing_volume = 0
        for row in rows:
            raw_ts = row.get("datetime") if intraday_minutes else row.get("date")
            if raw_ts is None:
                raw_ts = row.get("date") or row.get("datetime")
            ts = _to_datetime(raw_ts)
            if ts is None:
                dropped_unparsable += 1
                continue
            volume_raw = row.get("volume")
            if volume_raw is None:
                # 결측을 0으로 바꾸지 않는다. 행을 버리고 경고로 남긴다.
                dropped_missing_volume += 1
                continue
            try:
                if intraday_minutes:
                    if ts.tzinfo is None:
                        dropped_unparsable += 1
                        continue
                    start_at = ts.astimezone(_NY)
                    end_at = start_at + timedelta(minutes=intraday_minutes)
                    expected = intraday_minutes
                else:
                    start_at = ts.replace(
                        hour=_US_OPEN.hour, minute=_US_OPEN.minute,
                        second=0, microsecond=0, tzinfo=_NY)
                    end_at = ts.replace(
                        hour=_US_CLOSE.hour, minute=_US_CLOSE.minute,
                        second=0, microsecond=0, tzinfo=_NY)
                    expected = _US_SESSION_MINUTES
                bar = NormalizedBar(
                    start_at=start_at,
                    end_at=end_at,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(round(float(volume_raw))),
                    interval=request.interval,
                    session="regular",
                    complete=True,
                    session_tail=False,
                    expected_minutes=expected,
                    actual_minutes=expected,
                    data_integrity="complete",
                    source_gap_status="none",
                )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                dropped_unparsable += 1
                continue
            bars.append(bar)

        if dropped_unparsable:
            warnings.append(
                f"해석할 수 없는 행 {dropped_unparsable}개를 제외했습니다.")
        if dropped_missing_volume:
            warnings.append(
                f"거래량 결측 행 {dropped_missing_volume}개를 제외했습니다"
                "(결측을 0으로 간주하지 않습니다).")

        ordered, dedupe_warnings = sort_and_dedupe(bars)
        warnings.extend(dedupe_warnings)

        total = len(ordered)
        if total > request.row_limit:
            ordered = ordered[-request.row_limit:]

        # 어댑터는 시장 시계를 모르므로 마지막 봉의 마감 여부를 단정하지 않는다.
        if ordered:
            last = ordered[-1]
            ordered = ordered[:-1] + (
                NormalizedBar(
                    start_at=last.start_at, end_at=last.end_at,
                    open=last.open, high=last.high, low=last.low,
                    close=last.close, volume=last.volume,
                    interval=last.interval, session=last.session,
                    complete=None, session_tail=last.session_tail,
                    expected_minutes=last.expected_minutes,
                    actual_minutes=last.actual_minutes,
                    data_integrity=last.data_integrity,
                    source_gap_status=last.source_gap_status,
                ),
            )

        return BarDataset(
            bars=ordered,
            market="US",
            symbol=request.symbol,
            provider=self.provider_id,
            profile=None,
            venue=request.venue,
            timezone="America/New_York",
            session="regular",
            requested_interval=request.interval,
            source_interval=request.interval,
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="yfinance_history",
            coverage={
                "requested_rows": request.row_limit,
                "returned_rows": len(ordered),
                "source_rows": total,
                "period": period,
            },
            warnings=tuple(warnings),
        )
