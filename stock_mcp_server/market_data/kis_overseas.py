"""KIS 미국(NYS·NAS·AMS) 분봉 공급자.

공식 해외주식 분봉 endpoint(HHDFS76950200) 사용. 호출당 최대 120행,
KEYB 연속키로 뒤로 페이지네이션한다. 시각은 거래소 현지(xymd/xhms)를
America/New_York 로 부여한다 (DST 는 zoneinfo 가 처리).

정규장 세션 밖 행은 제외하고 경고로 남긴다. 거래소는 추측하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_mcp_server.market_data._bars import sort_and_dedupe
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)
from stock_mcp_server.market_data.us_symbols import resolve_us_symbol

_NY = ZoneInfo("America/New_York")

_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
_TR_ID = "HHDFS76950200"

_MAX_ROWS_PER_CALL = 120
_DEFAULT_MAX_PAGES = 30

_NATIVE_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10,
                   "15m": 15, "30m": 30, "60m": 60}

_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)


def _parse_row(row: dict, minutes: int, interval: str) -> NormalizedBar | None:
    try:
        start = datetime.strptime(
            str(row["xymd"]) + str(row["xhms"]), "%Y%m%d%H%M%S")
        start = start.replace(second=0, tzinfo=_NY)
        return NormalizedBar(
            start_at=start,
            end_at=start + timedelta(minutes=minutes),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["last"])),
            volume=int(row["evol"]),
            interval=interval,
            session="regular",
            complete=True,
            session_tail=False,
            expected_minutes=minutes,
            actual_minutes=minutes,
            data_integrity="complete",
            source_gap_status="none",
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


class KisOverseasProvider:
    provider_id = "kis"

    def __init__(
        self,
        client: KisClient,
        profile: str,
        *,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> None:
        self._client = client
        self.profile = profile
        self._max_pages = max_pages

    async def capabilities(self, profile: str) -> ProviderCapabilities:
        if profile != "real":
            # KIS 모의는 해외 시세를 지원하지 않는다. 추측 활성화 금지.
            return ProviderCapabilities(
                provider=self.provider_id,
                contract_version=1,
                markets=(),
                venues=(),
                native_intervals=(),
                verified_intervals=(),
                sessions=(),
                adjustment_modes=(),
                max_rows_per_call=_MAX_ROWS_PER_CALL,
                historical_limit=None,
            )
        return ProviderCapabilities(
            provider=self.provider_id,
            contract_version=1,
            markets=("US",),
            venues=("NYS", "NAS", "AMS"),
            native_intervals=tuple(_NATIVE_MINUTES),
            verified_intervals=("1m",),
            sessions=("regular",),
            adjustment_modes=("unadjusted",),
            max_rows_per_call=_MAX_ROWS_PER_CALL,
            historical_limit=None,
        )

    async def fetch_bars(self, request: BarRequest) -> BarDataset:
        minutes = _NATIVE_MINUTES.get(request.interval)
        if minutes is None:
            raise ValueError(
                f"kis_overseas가 지원하지 않는 interval: {request.interval}")
        if request.market != "US":
            raise ValueError(f"kis_overseas는 US 전용입니다: {request.market}")
        if request.session != "regular":
            # pre·after·daytime 은 세션 경계·집계가 검증되기 전까지 거부한다.
            # 라벨만 daytime 이고 내용은 regular 인 혼선을 만들지 않는다.
            raise ValueError(
                f"검증되지 않은 session: {request.session} "
                "(현재 regular 만 지원)")

        exchange, kis_symbol = resolve_us_symbol(
            request.symbol, request.venue, session=request.session)

        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        out_of_session = 0
        after_date = 0
        pages = 0
        complete = True
        failure_status: str | None = None
        resume_keyb: str | None = None
        # 요청 거래일이 있으면 첫 KEYB 를 그 다음날 00:00 으로 고정한다.
        # 빈 KEYB(최신부터)로 시작하면 과거 날짜 요청이 무시된다(리뷰 지적).
        if request.trading_date is not None:
            anchor = datetime.combine(
                request.trading_date + timedelta(days=1),
                time(0, 0))
            keyb = anchor.strftime("%Y%m%d%H%M%S")
        else:
            keyb = ""
        prev_earliest: datetime | None = None

        while pages < self._max_pages:
            pages += 1
            params = {
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": kis_symbol,
                "NMIN": str(minutes),
                "PINC": "1",
                "NEXT": "" if keyb == "" else "1",
                "NREC": str(_MAX_ROWS_PER_CALL),
                "FILL": "",
                "KEYB": keyb,
            }
            try:
                payload = await self._client.request(
                    "GET", _PATH, tr_id=_TR_ID, params=params)
            except KisApiError as exc:
                if not bars:
                    raise
                complete = False
                failure_status = exc.provider_status
                resume_keyb = keyb
                warnings.append(
                    f"페이지 {pages} 조회 실패({exc.provider_status}). "
                    "이미 받은 구간만 반환합니다.")
                break

            if str(payload.get("rt_cd", "")) != "0":
                if not bars:
                    raise KisApiError("provider_unavailable")
                complete = False
                failure_status = "provider_unavailable"
                resume_keyb = keyb
                warnings.append(
                    f"페이지 {pages}에서 공급자 오류 응답. "
                    "이미 받은 구간만 반환합니다.")
                break

            rows = payload.get("output2") or []
            if not rows:
                break

            # cursor 진행은 세션 필터와 무관하게 "파싱된 전체 행" 기준이다.
            # 실측(2026-08-27): NAS 피드는 최신부터 애프터마켓 행이 이어져
            # 한 페이지 전체가 세션 밖일 수 있다 - 채택 행 기준으로 멈추면
            # 그 뒤의 정규장 행에 영영 도달하지 못한다 (AAPL 0행 재현).
            page_bars: list[NormalizedBar] = []
            parsed_all: list[NormalizedBar] = []
            for row in rows:
                bar = _parse_row(row, minutes, request.interval)
                if bar is None:
                    dropped += 1
                    continue
                parsed_all.append(bar)
                # 방어: 앵커가 무시돼 요청일보다 뒤의 행이 와도 채택하지
                # 않는다. 기준일 이전 이력은 허용(지표용 다일 조회).
                if request.trading_date is not None and \
                        bar.start_at.date() > request.trading_date:
                    after_date += 1
                    continue
                if request.session == "regular":
                    local = bar.start_at.time()
                    # 마감 정각(16:00) 체결은 공식 종가라 포함한다.
                    if local < _REGULAR_OPEN or local > _REGULAR_CLOSE:
                        out_of_session += 1
                        continue
                page_bars.append(bar)

            if not parsed_all:
                # 행은 있는데 하나도 못 읽었다. 형식이 바뀐 것이다.
                if not bars:
                    raise KisApiError("source_parse_error")
                complete = False
                failure_status = "source_parse_error"
                resume_keyb = keyb
                warnings.append(
                    f"페이지 {pages}의 행을 해석하지 못했습니다. "
                    "이미 받은 구간만 반환합니다.")
                break

            earliest = min(b.start_at for b in parsed_all)
            if prev_earliest is not None and earliest >= prev_earliest:
                warnings.append(
                    "KEYB가 진행되지 않아 pagination을 중단했습니다.")
                bars.extend(page_bars)
                break
            prev_earliest = earliest
            bars.extend(page_bars)

            if len(bars) >= request.row_limit:
                break
            next_start = earliest - timedelta(minutes=minutes)
            keyb = next_start.strftime("%Y%m%d%H%M%S")
        else:
            complete = False
            if prev_earliest is not None:
                resume_keyb = (prev_earliest - timedelta(minutes=minutes)
                               ).strftime("%Y%m%d%H%M%S")
            warnings.append(
                f"페이지 예산({self._max_pages})을 소진해 중단했습니다.")

        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        if out_of_session:
            warnings.append(
                f"정규장 세션 밖 행 {out_of_session}개를 제외했습니다.")
        if after_date:
            warnings.append(
                f"기준일 이후 행 {after_date}개를 제외했습니다.")

        ordered, dedupe_warnings = sort_and_dedupe(bars)
        warnings.extend(dedupe_warnings)
        if len(ordered) > request.row_limit:
            ordered = ordered[-request.row_limit:]

        coverage: dict = {
            "requested_rows": request.row_limit,
            "returned_rows": len(ordered),
            "pages": pages,
            "complete": complete,
        }
        if resume_keyb is not None:
            coverage["resume_cursor"] = resume_keyb
        if failure_status is not None:
            coverage["failure_status"] = failure_status

        return BarDataset(
            bars=ordered,
            market="US",
            symbol=request.symbol,
            provider=self.provider_id,
            profile=self.profile,
            venue=exchange,
            timezone="America/New_York",
            session=request.session,
            requested_interval=request.interval,
            source_interval=request.interval,
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="overseas_minute",
            coverage=coverage,
            warnings=tuple(warnings),
        )
