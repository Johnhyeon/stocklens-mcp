"""토스 KR·US 1분 캔들 공급자 (1.0 Task 16).

공식 스펙: GET /api/v1/candles
- interval=1m, count<=200, adjusted=false 명시 (수정주가 미적용 원천)
- before 는 inclusive 상한(ISO 8601 offset). 과거 거래일 요청은 그날
  23:59:59(시장 시간대)로 anchoring 한다.
- 응답 result.candles 최신순, timestamp 는 봉 시작 시각.
- nextBefore 를 다음 페이지 before 로 그대로 넘긴다. inclusive 라 경계
  봉이 중복되므로 dedupe 로 걷어낸다.

해석 규칙:
- 요청 거래일의 행만 채택한다. 그 이전 날짜 행에 닿으면 깨끗이 멈춘다.
- 정규장 밖 행은 버리고 개수를 경고로 남긴다.
- currency 가 시장과 어긋나는 행은 채택하지 않는다 (라벨-값 계약).

KR 차단 (2026-08-27 실계좌 실측, 005930 KIS 381분 교차 대조):
- timestamp 가 문서("봉 시작")와 달리 봉 끝 라벨로 동작
  (toss[t+1] OHLC == kis[t] 210/381분, 09:00 bar 거래량 0)
- 거래량이 KRX 단독 기준이 아님 (일치 구간 중앙값 1.37배, 통합 추정)
- 15:30 마감 동시호가 print 미포함 (마지막 종가 != 공식 종가)
계약 불일치가 해소·검증되기 전까지 KR 요청은 거부한다. 추측 보정 금지.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_mcp_server.market_data._bars import sort_and_dedupe
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)
from stock_mcp_server.market_data.toss_client import (
    TossApiError,
    TossClient,
)

_MARKETS = {
    "KR": {
        "tz": ZoneInfo("Asia/Seoul"),
        "tz_name": "Asia/Seoul",
        "open": time(9, 0),
        "close": time(15, 30),
        "currency": "KRW",
    },
    "US": {
        "tz": ZoneInfo("America/New_York"),
        "tz_name": "America/New_York",
        "open": time(9, 30),
        "close": time(16, 0),
        "currency": "USD",
    },
}

_PAGE_SIZE = 200
_DEFAULT_MAX_PAGES = 40


def _parse_candle(row: dict, market_cfg: dict) -> NormalizedBar | None:
    try:
        if str(row.get("currency")) != market_cfg["currency"]:
            return None
        start = datetime.fromisoformat(str(row["timestamp"]))
        if start.tzinfo is None:
            return None
        start = start.astimezone(market_cfg["tz"]).replace(
            second=0, microsecond=0)
        return NormalizedBar(
            start_at=start,
            end_at=start + timedelta(minutes=1),
            open=Decimal(str(row["openPrice"])),
            high=Decimal(str(row["highPrice"])),
            low=Decimal(str(row["lowPrice"])),
            close=Decimal(str(row["closePrice"])),
            volume=int(str(row["volume"])),
            interval="1m",
            session="regular",
            complete=True,
            session_tail=False,
            expected_minutes=1,
            actual_minutes=1,
            data_integrity="complete",
            source_gap_status="none",
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


class TossBarProvider:
    provider_id = "toss"

    def __init__(
        self,
        client: TossClient,
        profile: str,
        *,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> None:
        self._client = client
        self.profile = profile
        self._max_pages = max_pages

    async def capabilities(self, profile: str) -> ProviderCapabilities:
        # KR 은 실측 계약 불일치로 차단 상태다 (모듈 docstring 참조).
        verified = ("1m",) if profile == "real" else ()
        return ProviderCapabilities(
            provider=self.provider_id,
            contract_version=1,
            markets=("US",),
            venues=("NYS", "NAS", "AMS"),
            native_intervals=("1m",),
            verified_intervals=verified,
            sessions=("regular",),
            adjustment_modes=("unadjusted",),
            max_rows_per_call=_PAGE_SIZE,
            historical_limit=None,
        )

    async def fetch_bars(self, request: BarRequest) -> BarDataset:
        if request.interval != "1m":
            raise ValueError(
                f"toss_provider는 1m 원천만 반환합니다. 요청 interval: "
                f"{request.interval}")
        if request.market == "KR":
            # 실측 계약 불일치 (2026-08-27). 조용한 보정 대신 명시 거부.
            raise TossApiError("not_configured")
        market_cfg = _MARKETS.get(request.market)
        if market_cfg is None:
            raise ValueError(
                f"toss_provider가 지원하지 않는 시장: {request.market}")
        if request.session != "regular":
            raise ValueError(
                f"검증되지 않은 session: {request.session} "
                "(현재 regular 만 지원)")
        if request.trading_date is None:
            raise ValueError("trading_date가 필요합니다")

        tz = market_cfg["tz"]
        # 과거 거래일 요청 anchoring: 그날의 끝(inclusive 상한)에서 시작.
        anchor = datetime.combine(
            request.trading_date, time(23, 59, 59), tzinfo=tz)
        before = anchor.isoformat()

        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        session_dropped = 0
        pages = 0
        complete = True
        failure_status: str | None = None
        resume_cursor: str | None = None
        saw_rows = False

        while pages < self._max_pages:
            pages += 1
            try:
                payload = await self._client.request("candles", params={
                    "symbol": request.symbol,
                    "interval": "1m",
                    "count": str(_PAGE_SIZE),
                    "before": before,
                    "adjusted": "false",
                })
            except TossApiError as exc:
                if not bars:
                    raise
                complete = False
                failure_status = exc.provider_status
                resume_cursor = before
                warnings.append(
                    f"페이지 {pages} 조회 실패({exc.provider_status}). "
                    "이미 받은 구간만 반환합니다.")
                break

            result = payload.get("result")
            if not isinstance(result, dict):
                if not bars:
                    raise TossApiError("source_parse_error")
                complete = False
                failure_status = "source_parse_error"
                resume_cursor = before
                warnings.append(
                    f"페이지 {pages} 응답을 해석하지 못했습니다. "
                    "이미 받은 구간만 반환합니다.")
                break

            rows = result.get("candles") or []
            if not rows:
                break
            saw_rows = True

            page_bars: list[NormalizedBar] = []
            earlier_day = 0
            for row in rows:
                bar = _parse_candle(row, market_cfg)
                if bar is None:
                    dropped += 1
                    continue
                row_date = bar.start_at.date()
                if row_date != request.trading_date:
                    if row_date < request.trading_date:
                        earlier_day += 1
                    continue
                if not (market_cfg["open"] <= bar.start_at.time()
                        <= market_cfg["close"]):
                    session_dropped += 1
                    continue
                page_bars.append(bar)

            if not page_bars and not earlier_day and not session_dropped:
                if not bars:
                    if dropped:
                        raise TossApiError("source_parse_error")
                    break
                complete = False
                failure_status = "source_parse_error"
                resume_cursor = before
                warnings.append(
                    f"페이지 {pages}의 행을 해석하지 못했습니다. "
                    "이미 받은 구간만 반환합니다.")
                break

            bars.extend(page_bars)

            if earlier_day:
                # 요청일 이전 행에 도달했다. 요청일 구간을 다 받았다.
                break
            if len(bars) >= request.row_limit:
                break
            next_before = result.get("nextBefore")
            if not next_before:
                break
            before = str(next_before)
        else:
            complete = False
            resume_cursor = before
            warnings.append(
                f"페이지 예산({self._max_pages})을 소진해 중단했습니다.")

        if saw_rows and not bars and complete:
            warnings.append(
                f"기준일 {request.trading_date.isoformat()} 데이터가 "
                "토스 제공 범위 밖입니다.")

        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        if session_dropped:
            warnings.append(
                f"정규장 밖 행 {session_dropped}개를 제외했습니다.")

        ordered, dedupe_warnings = sort_and_dedupe(bars)
        # inclusive 페이지네이션의 경계 중복은 정상이라 경고로 남기지
        # 않는다 (그 외 dedupe 경고는 유지).
        warnings.extend(dedupe_warnings)
        if len(ordered) > request.row_limit:
            ordered = ordered[-request.row_limit:]

        coverage: dict = {
            "requested_rows": request.row_limit,
            "returned_rows": len(ordered),
            "pages": pages,
            "complete": complete,
            "trading_date": request.trading_date.strftime("%Y%m%d"),
        }
        if resume_cursor is not None:
            coverage["resume_cursor"] = resume_cursor
        if failure_status is not None:
            coverage["failure_status"] = failure_status

        return BarDataset(
            bars=ordered,
            market=request.market,
            symbol=request.symbol,
            provider=self.provider_id,
            profile=self.profile,
            venue=request.venue,
            timezone=market_cfg["tz_name"],
            session="regular",
            requested_interval=request.interval,
            source_interval="1m",
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="toss_candles",
            coverage=coverage,
            warnings=tuple(warnings),
        )
