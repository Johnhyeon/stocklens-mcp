"""키움 미국 1분봉 공급자 (1.0 Task 14, 2026-08-27 실계좌 실측 개정).

공식 스펙(usa06011): POST /api/us/chart
- body: stex_tp(NA/ND/NY), stk_cd, strt_dt(YYYYMMDD), tic_scope,
  upd_stkpc_tp(0 미적용), exrt_appl_tp(0 미적용 = USD 원 표기)
- 응답: result_list 최신순. 페이지 크기 실측 100행.
- 연속조회: cont-yn / next-key 헤더 (커서는 과거로 진행, 실측 확인).

실계좌 실측으로 확정한 semantics (AAPL, 2026-08-27):
- cntr_tm 은 **한국 시각(KST) 라벨**이다. ET 09:11 프리장 체결이
  20260826221100 으로 온다. Asia/Seoul 로 파싱한 뒤 America/New_York
  로 변환해 저장한다.
- bus_dt 가 미국 영업일자다. 요청 거래일 필터는 bus_dt 기준이다.
- strt_dt 는 KST 달력 날짜 필터다. 미국 영업일 D 의 세션은 KST 로
  D 22:30 ~ D+1 05:00(EDT)에 걸치므로 **D+1 로 anchoring** 하고
  과거로 페이지네이션한다.
- 응답에 프리장·애프터마켓 행이 포함된다. ET 정규장(09:30~16:00,
  마감 print 포함) 밖 행은 버린다.
- bus_dt 가 요청 거래일과 다른 행은 채택하지 않는다. 과거일 요청에
  최신 데이터를 대신 돌려줘도 메우지 않는다 (빈 결과 + 경고).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_mcp_server.market_data._bars import sort_and_dedupe
from stock_mcp_server.market_data.kiwoom_client import (
    KiwoomApiError,
    KiwoomClient,
)
from stock_mcp_server.market_data.kiwoom_symbols import (
    to_stex_tp,
    validate_ticker,
)
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)

_NY = ZoneInfo("America/New_York")
_KST = ZoneInfo("Asia/Seoul")

_API_ID = "usa06011"
_ENDPOINT = "us_chart"

_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)

_DEFAULT_MAX_PAGES = 40


def _decimal_price(raw: object) -> Decimal:
    text = str(raw).strip()
    if not text:
        raise InvalidOperation("empty price")
    if text[0] in "+-":
        text = text[1:]
    return Decimal(text)


def _parse_row(row: dict) -> NormalizedBar | None:
    try:
        raw_tm = str(row["cntr_tm"])
        # 실측: cntr_tm 은 KST 라벨이다. ET 로 변환해 저장한다.
        start = datetime.strptime(raw_tm, "%Y%m%d%H%M%S")
        start = start.replace(second=0, tzinfo=_KST).astimezone(_NY)
        return NormalizedBar(
            start_at=start,
            end_at=start + timedelta(minutes=1),
            open=_decimal_price(row["open_pric"]),
            high=_decimal_price(row["high_pric"]),
            low=_decimal_price(row["low_pric"]),
            close=_decimal_price(row["cur_prc"]),
            volume=int(str(row["trde_qty"])),
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


def _in_session(start_at: datetime) -> bool:
    return _SESSION_OPEN <= start_at.time() <= _SESSION_CLOSE


class KiwoomOverseasProvider:
    provider_id = "kiwoom"

    def __init__(
        self,
        client: KiwoomClient,
        profile: str,
        *,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> None:
        self._client = client
        self.profile = profile
        self._max_pages = max_pages

    async def capabilities(self, profile: str) -> ProviderCapabilities:
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
            max_rows_per_call=None,
            historical_limit=None,
        )

    async def fetch_bars(self, request: BarRequest) -> BarDataset:
        if request.interval != "1m":
            raise ValueError(
                f"kiwoom_overseas는 1m 원천만 반환합니다. 요청 interval: "
                f"{request.interval}")
        if request.market != "US":
            raise ValueError(
                f"kiwoom_overseas는 US 전용입니다: {request.market}")
        # 2026-08-27 실계좌 실측 (AAPL 완결일 08-26 전수 + 야후·KIS 이중
        # 기준): 공통 82분 전부 종가 불일치, 과거일 시가 불일치(야후
        # 317.46 = KIS 317.46 vs 키움 311.84), 거래량 비율 0.0004~0.002
        # (주수 아님), 커버리지 ET ~11:00 절단. 계약이 규명·검증되기
        # 전까지 US 분봉은 제공하지 않는다. 키 문제가 아니므로
        # unsupported 로 거부한다.
        raise KiwoomApiError("unsupported")
        if request.session != "regular":
            raise ValueError(
                f"검증되지 않은 session: {request.session} "
                "(현재 regular 만 지원)")
        if request.trading_date is None:
            raise ValueError("trading_date가 필요합니다")

        stex_tp = to_stex_tp(request.venue)
        ticker = validate_ticker(request.symbol)

        trading_date_str = request.trading_date.strftime("%Y%m%d")
        # strt_dt 는 KST 달력 날짜다. 미국 영업일 D 의 세션 후반(ET
        # 11:00~마감)은 KST D+1 에 있으므로 D+1 로 anchoring 한다.
        strt_dt = (request.trading_date
                   + timedelta(days=1)).strftime("%Y%m%d")
        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        session_dropped = 0
        other_day_total = 0
        pages = 0
        complete = True
        failure_status: str | None = None
        resume_cursor: str | None = None
        cont_yn: str | None = None
        next_key: str | None = None
        saw_rows = False

        while pages < self._max_pages:
            pages += 1
            try:
                response = await self._client.request(
                    _ENDPOINT, api_id=_API_ID,
                    body={
                        "stex_tp": stex_tp,
                        "stk_cd": ticker,
                        "strt_dt": strt_dt,
                        "tic_scope": "1",
                        "upd_stkpc_tp": "0",
                        "exrt_appl_tp": "0",
                    },
                    cont_yn=cont_yn, next_key=next_key)
            except KiwoomApiError as exc:
                if not bars:
                    raise
                complete = False
                failure_status = exc.provider_status
                resume_cursor = next_key
                warnings.append(
                    f"페이지 {pages} 조회 실패({exc.provider_status}). "
                    "이미 받은 구간만 반환합니다.")
                break

            payload = response.payload
            code = payload.get("return_code")
            if code not in (0, "0", None):
                if not bars:
                    raise KiwoomApiError("provider_unavailable")
                complete = False
                failure_status = "provider_unavailable"
                resume_cursor = next_key
                warnings.append(
                    f"페이지 {pages}에서 공급자 오류 응답. "
                    "이미 받은 구간만 반환합니다.")
                break

            rows = payload.get("result_list") or []
            if not rows:
                break

            # 실측(2026-08-27, 국내와 동일 형태): 없는 종목은 return_code
            # 0 + 전 필드 빈 문자열 행이 온다. 형식 파손과 구분해 종목
            # 없음으로 분류한다.
            if not bars and all(
                    "cntr_tm" in row
                    and not str(row.get("cntr_tm") or "").strip()
                    and not str(row.get("bus_dt") or "").strip()
                    for row in rows):
                raise KiwoomApiError("entity_not_found")
            saw_rows = True

            page_bars: list[NormalizedBar] = []
            earlier_day = 0
            for row in rows:
                row_date = str(row.get("bus_dt")
                               or str(row.get("cntr_tm") or "")[:8])
                if row_date != trading_date_str:
                    other_day_total += 1
                    if row_date and row_date < trading_date_str:
                        earlier_day += 1
                    continue
                bar = _parse_row(row)
                if bar is None:
                    dropped += 1
                    continue
                if not _in_session(bar.start_at):
                    session_dropped += 1
                    continue
                page_bars.append(bar)

            if not page_bars and not other_day_total and \
                    not session_dropped:
                if not bars:
                    raise KiwoomApiError("source_parse_error")
                complete = False
                failure_status = "source_parse_error"
                resume_cursor = next_key
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
            if response.cont_yn != "Y" or not response.next_key:
                break
            cont_yn = "Y"
            next_key = response.next_key
        else:
            complete = False
            resume_cursor = next_key
            warnings.append(
                f"페이지 예산({self._max_pages})을 소진해 중단했습니다.")

        if saw_rows and not bars:
            # 공급자가 다른 날짜 데이터만 돌려줬다. 최신 데이터로 메우지
            # 않는다. 빈 결과가 정직한 결과다.
            warnings.append(
                f"기준일 {request.trading_date.isoformat()} 데이터가 "
                "키움 제공 범위 밖입니다.")

        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        if session_dropped:
            warnings.append(
                f"정규장 밖 행 {session_dropped}개를 제외했습니다.")

        ordered, dedupe_warnings = sort_and_dedupe(bars)
        warnings.extend(dedupe_warnings)
        if len(ordered) > request.row_limit:
            ordered = ordered[-request.row_limit:]

        coverage: dict = {
            "requested_rows": request.row_limit,
            "returned_rows": len(ordered),
            "pages": pages,
            "complete": complete,
            "trading_date": trading_date_str,
        }
        if resume_cursor is not None:
            coverage["resume_cursor"] = resume_cursor
        if failure_status is not None:
            coverage["failure_status"] = failure_status

        return BarDataset(
            bars=ordered,
            market="US",
            symbol=ticker,
            provider=self.provider_id,
            profile=self.profile,
            venue=request.venue,
            timezone="America/New_York",
            session="regular",
            requested_interval=request.interval,
            source_interval="1m",
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="kiwoom_us_minute",
            coverage=coverage,
            warnings=tuple(warnings),
        )
