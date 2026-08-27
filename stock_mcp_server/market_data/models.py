"""공통 요청·봉·데이터 묶음 모델.

규칙 (설계 9~11절):
- timezone-aware timestamp만 허용
- OHLC 순서 검증, 음수 거래량 금지
- row_limit 는 최대 반환 행 수. 미달 자체는 partial 이 아니다
- 비밀값(App Key, App Secret, 토큰)은 어떤 모델에도 싣지 않는다
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

SUPPORTED_INTERVALS: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
    "30m": 30, "60m": 60, "120m": 120, "240m": 240,
}

# 일·주·월봉은 라우터가 기존 공급원(Naver·Yahoo)으로 처리한다. KIS 일봉은
# 수정주가 검증 전까지 unverified 다 (설계 6.3절).
DAILY_INTERVALS: tuple[str, ...] = ("1d", "1wk", "1mo")

SUPPORTED_MARKETS: tuple[str, ...] = ("KR", "US")

SUPPORTED_SESSIONS: tuple[str, ...] = ("regular", "pre", "after", "daytime")

DATA_INTEGRITY_VALUES: tuple[str, ...] = ("complete", "partial", "unknown")

SOURCE_GAP_STATUSES: tuple[str, ...] = (
    "none", "confirmed_no_trade", "provider_gap", "unknown_gap",
)

# 설계 27절 장애 계약. 이 값은 meta 의 provider_status 로만 나간다.
# coverage reason 에 임의 문자열을 추가하지 않는다.
PROVIDER_STATUSES: tuple[str, ...] = (
    "ok", "not_configured", "credential_invalid",
    "authentication_failed", "permission_denied", "rate_limited",
    "provider_unavailable", "source_parse_error", "entity_not_found",
    "no_session", "partial",
)


def _require_aware(value: datetime | None, name: str) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name}: timezone-aware datetime이 필요합니다")


@dataclass(frozen=True)
class BarRequest:
    """정규화된 봉 조회 요청. 공급자에 독립적이다."""

    symbol: str
    market: str
    interval: str
    start: datetime | None
    end: datetime | None
    trading_date: date | None
    row_limit: int
    venue: str
    session: str
    adjustment: str
    completed_only: bool
    source: str

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol이 비어 있습니다")
        if self.market not in SUPPORTED_MARKETS:
            raise ValueError(
                f"지원하지 않는 market: {self.market} (지원: {SUPPORTED_MARKETS})")
        if self.interval not in SUPPORTED_INTERVALS and \
                self.interval not in DAILY_INTERVALS:
            raise ValueError(
                f"지원하지 않는 interval: {self.interval}")
        if self.row_limit < 1:
            raise ValueError(f"row_limit는 1 이상이어야 합니다: {self.row_limit}")
        if self.session not in SUPPORTED_SESSIONS:
            raise ValueError(f"지원하지 않는 session: {self.session}")
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end는 start보다 뒤여야 합니다")


@dataclass(frozen=True)
class NormalizedBar:
    """공급자 응답을 정규화한 봉 하나.

    complete 는 봉 시간 상태(마감 여부), data_integrity 는 데이터 완전성이다.
    둘을 섞지 않는다 (설계 15절). 모르면 complete=None.
    """

    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    interval: str
    session: str
    complete: bool | None
    session_tail: bool
    expected_minutes: int
    actual_minutes: int
    data_integrity: str
    source_gap_status: str

    def __post_init__(self) -> None:
        _require_aware(self.start_at, "start_at")
        _require_aware(self.end_at, "end_at")
        if self.end_at <= self.start_at:
            raise ValueError("end_at은 start_at보다 뒤여야 합니다")
        if self.high < self.low:
            raise ValueError("OHLC 순서 위반: high < low")
        if self.high < self.open or self.high < self.close:
            raise ValueError("OHLC 순서 위반: high가 open/close보다 낮습니다")
        if self.low > self.open or self.low > self.close:
            raise ValueError("OHLC 순서 위반: low가 open/close보다 높습니다")
        if self.volume < 0:
            raise ValueError(f"volume은 음수일 수 없습니다: {self.volume}")
        if self.data_integrity not in DATA_INTEGRITY_VALUES:
            raise ValueError(
                f"지원하지 않는 data_integrity: {self.data_integrity}")
        if self.source_gap_status not in SOURCE_GAP_STATUSES:
            raise ValueError(
                f"지원하지 않는 source_gap_status: {self.source_gap_status}")
        if self.expected_minutes < 0 or self.actual_minutes < 0:
            raise ValueError("expected/actual_minutes는 음수일 수 없습니다")


@dataclass(frozen=True)
class BarDataset:
    """한 요청의 결과. 반드시 단일 공급자·단일 프로필의 봉만 담는다."""

    bars: tuple[NormalizedBar, ...]
    market: str
    symbol: str
    provider: str
    profile: str | None
    venue: str
    timezone: str
    session: str
    requested_interval: str
    source_interval: str
    aggregation_method: str
    adjustment_basis: str
    source_endpoint: str
    coverage: dict
    warnings: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        prev: datetime | None = None
        for bar in self.bars:
            if prev is not None:
                if bar.start_at == prev:
                    raise ValueError(
                        f"duplicate timestamp: {bar.start_at.isoformat()}")
                if bar.start_at < prev:
                    raise ValueError(
                        "bars는 ascending 순서여야 합니다: "
                        f"{bar.start_at.isoformat()} < {prev.isoformat()}")
            prev = bar.start_at


def bar_to_dict(bar: NormalizedBar) -> dict:
    """디스크 캐시용 직렬화. timestamp 는 offset 포함 ISO, 가격은 문자열."""
    return {
        "start_at": bar.start_at.isoformat(),
        "end_at": bar.end_at.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "interval": bar.interval,
        "session": bar.session,
        "complete": bar.complete,
        "session_tail": bar.session_tail,
        "expected_minutes": bar.expected_minutes,
        "actual_minutes": bar.actual_minutes,
        "data_integrity": bar.data_integrity,
        "source_gap_status": bar.source_gap_status,
    }


def bar_from_dict(raw: dict) -> NormalizedBar:
    """bar_to_dict 역변환. 형식이 어긋나면 예외를 던진다 (조용한 복구 금지)."""
    return NormalizedBar(
        start_at=datetime.fromisoformat(raw["start_at"]),
        end_at=datetime.fromisoformat(raw["end_at"]),
        open=Decimal(raw["open"]),
        high=Decimal(raw["high"]),
        low=Decimal(raw["low"]),
        close=Decimal(raw["close"]),
        volume=int(raw["volume"]),
        interval=raw["interval"],
        session=raw["session"],
        complete=raw["complete"],
        session_tail=bool(raw["session_tail"]),
        expected_minutes=int(raw["expected_minutes"]),
        actual_minutes=int(raw["actual_minutes"]),
        data_integrity=raw["data_integrity"],
        source_gap_status=raw["source_gap_status"],
    )


@dataclass(frozen=True)
class ProviderCapabilities:
    """공급자·프로필별 능력. 실전과 모의를 같은 능력으로 가정하지 않는다."""

    provider: str
    contract_version: int
    markets: tuple[str, ...]
    venues: tuple[str, ...]
    native_intervals: tuple[str, ...]
    verified_intervals: tuple[str, ...]
    sessions: tuple[str, ...]
    adjustment_modes: tuple[str, ...]
    max_rows_per_call: int | None
    historical_limit: str | None
