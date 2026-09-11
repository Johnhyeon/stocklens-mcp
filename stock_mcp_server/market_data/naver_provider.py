"""기존 naver.get_ohlcv 를 감싸는 어댑터. 원본 함수는 변경하지 않는다.

일·주·월봉 전용이다. 네이버에는 이 선로가 다루는 분봉 공급이 없다.
초기에는 라우터 테스트와 fallback 경로에서만 쓰인다.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_mcp_server.naver import get_ohlcv
from stock_mcp_server.market_data._bars import sort_and_dedupe
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)

_KST = ZoneInfo("Asia/Seoul")

# KRX 정규장 09:00~15:30. 세부 세션 판정은 sessions 모듈 몫이고,
# 일봉 어댑터는 거래일 대표 시각만 부여한다.
_SESSION_OPEN = time(9, 0)
_SESSION_CLOSE = time(15, 30)
_SESSION_MINUTES = 390

_INTERVAL_TO_TIMEFRAME = {"1d": "day", "1wk": "week", "1mo": "month"}


def _parse_date(raw: object) -> datetime | None:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        parsed = datetime.strptime(digits[:8], "%Y%m%d")
    except ValueError:
        return None
    return parsed


class NaverBarProvider:
    provider_id = "naver"

    async def capabilities(self, profile: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider_id,
            contract_version=1,
            markets=("KR",),
            venues=("KRX",),
            native_intervals=("1d", "1wk", "1mo"),
            verified_intervals=("1d", "1wk", "1mo"),
            sessions=("regular",),
            # 네이버는 조정 기준을 명시하지 않는다 (event_reaction 참고).
            adjustment_modes=("unknown",),
            max_rows_per_call=None,
            historical_limit=None,
        )

    async def fetch_bars(self, request: BarRequest) -> BarDataset:
        timeframe = _INTERVAL_TO_TIMEFRAME.get(request.interval)
        if timeframe is None:
            raise ValueError(
                f"naver 어댑터가 지원하지 않는 interval: {request.interval}")
        if request.market != "KR":
            raise ValueError(f"naver 어댑터는 KR 전용입니다: {request.market}")

        rows = await get_ohlcv(request.symbol, timeframe, request.row_limit)

        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        for row in rows:
            parsed = _parse_date(row.get("date"))
            if parsed is None:
                dropped += 1
                continue
            try:
                bar = NormalizedBar(
                    start_at=parsed.replace(
                        hour=_SESSION_OPEN.hour, minute=_SESSION_OPEN.minute,
                        tzinfo=_KST),
                    end_at=parsed.replace(
                        hour=_SESSION_CLOSE.hour, minute=_SESSION_CLOSE.minute,
                        tzinfo=_KST),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    interval=request.interval,
                    session="regular",
                    complete=True,
                    session_tail=False,
                    expected_minutes=_SESSION_MINUTES,
                    actual_minutes=_SESSION_MINUTES,
                    data_integrity="complete",
                    source_gap_status="none",
                )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                dropped += 1
                continue
            bars.append(bar)
        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")

        ordered, dedupe_warnings = sort_and_dedupe(bars)
        warnings.extend(dedupe_warnings)

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
            market="KR",
            symbol=request.symbol,
            provider=self.provider_id,
            profile=None,
            venue="KRX",
            timezone="Asia/Seoul",
            session="regular",
            requested_interval=request.interval,
            source_interval=request.interval,
            aggregation_method="provider_native",
            adjustment_basis="unknown",
            source_endpoint="naver_fchart",
            coverage={
                "requested_rows": request.row_limit,
                "returned_rows": len(ordered),
            },
            warnings=tuple(warnings),
        )
