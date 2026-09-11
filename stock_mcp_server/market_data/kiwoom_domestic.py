"""키움 국내(KRX) 1분봉 공급자 (1.0 Task 13).

공식 스펙(ka10080): POST /api/dostk/chart
- body: stk_cd, tic_scope("1"=1분), upd_stkpc_tp("0"=미적용), base_dt
- 응답: stk_min_pole_chart_qry 최신순 리스트. 가격은 등락 부호가 붙은
  문자열("+71000"/"-70900")이라 절대값으로 정규화한다.
- 연속조회: 응답 header cont-yn == "Y" 면 next-key 를 요청 header 로
  되돌려 다음 페이지를 받는다.

안전 규칙 (KIS 국내 공급자와 동일):
- base_dt 거래일의 행만 채택한다. 전일 행에 닿으면 깨끗이 멈춘다.
- 정규장(09:00~15:30, 마감 동시호가 포함) 밖 행은 버린다.
- 페이지 예산 초과·중간 실패는 partial + resume_cursor 로 정직 보고.
- 어댑터는 집계하지 않는다. 1m 원천만 반환한다.

cntr_tm(체결시간)을 봉 시작 시각으로 해석한다. 실계좌 UAT 에서 KIS·
공식 HTS 값과 대조해 정렬을 검증하기 전까지 capability 는 unverified 다.
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
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)

_KST = ZoneInfo("Asia/Seoul")

_API_ID = "ka10080"
_ENDPOINT = "kr_chart"

_SESSION_OPEN = time(9, 0)
_SESSION_CLOSE = time(15, 30)

_DEFAULT_MAX_PAGES = 40


def _decimal_price(raw: object) -> Decimal:
    """등락 부호가 붙은 가격 문자열을 절대값 Decimal 로 바꾼다."""
    text = str(raw).strip()
    if not text:
        raise InvalidOperation("empty price")
    if text[0] in "+-":
        text = text[1:]
    return Decimal(text)


def _parse_row(row: dict) -> NormalizedBar | None:
    try:
        raw_tm = str(row["cntr_tm"])
        start = datetime.strptime(raw_tm, "%Y%m%d%H%M%S")
        start = start.replace(second=0, tzinfo=_KST)
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


class KiwoomDomesticProvider:
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
        # 연결 시험으로 검증되기 전에는 능력을 추측해 활성화하지 않는다.
        verified = ("1m",) if profile == "real" else ()
        return ProviderCapabilities(
            provider=self.provider_id,
            contract_version=1,
            markets=("KR",),
            venues=("KRX",),
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
                f"kiwoom_domestic은 1m 원천만 반환합니다. 요청 interval: "
                f"{request.interval}")
        if request.market != "KR":
            raise ValueError(
                f"kiwoom_domestic은 KR 전용입니다: {request.market}")
        if request.session != "regular":
            raise ValueError(
                f"검증되지 않은 session: {request.session} "
                "(현재 regular 만 지원)")
        if request.trading_date is None:
            raise ValueError("trading_date가 필요합니다")

        trading_date_str = request.trading_date.strftime("%Y%m%d")
        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        session_dropped = 0
        pages = 0
        complete = True
        failure_status: str | None = None
        resume_cursor: str | None = None
        cont_yn: str | None = None
        next_key: str | None = None

        while pages < self._max_pages:
            pages += 1
            try:
                response = await self._client.request(
                    _ENDPOINT, api_id=_API_ID,
                    body={
                        "stk_cd": request.symbol,
                        "tic_scope": "1",
                        "upd_stkpc_tp": "0",
                        "base_dt": trading_date_str,
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

            rows = payload.get("stk_min_pole_chart_qry") or []
            if not rows:
                break

            # 실측(2026-08-27): 없는 종목은 return_code 0 + 전 필드 빈
            # 문자열 행이 온다. 형식 파손(키 자체가 없음)과 구분해
            # 종목 없음으로 분류한다.
            if not bars and all(
                    "cntr_tm" in row
                    and not str(row.get("cntr_tm") or "").strip()
                    for row in rows):
                raise KiwoomApiError("entity_not_found")

            page_bars: list[NormalizedBar] = []
            other_day = 0
            for row in rows:
                raw_tm = str(row.get("cntr_tm") or "")
                if raw_tm[:8] and raw_tm[:8] != trading_date_str:
                    other_day += 1
                    continue
                bar = _parse_row(row)
                if bar is None:
                    dropped += 1
                    continue
                if not _in_session(bar.start_at):
                    session_dropped += 1
                    continue
                page_bars.append(bar)

            if not page_bars and not other_day and not session_dropped:
                # 행은 있는데 하나도 못 읽었다. 형식이 바뀐 것이다.
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

            if other_day:
                # 전일 행에 도달했다 = 요청일 구간을 다 받았다. 정상 종료.
                break
            if len(bars) >= request.row_limit:
                break
            if response.cont_yn != "Y" or not response.next_key:
                break
            cont_yn = "Y"
            next_key = response.next_key
        else:
            # 페이지 예산 소진. 남은 구간이 있을 수 있다.
            complete = False
            resume_cursor = next_key
            warnings.append(
                f"페이지 예산({self._max_pages})을 소진해 중단했습니다.")

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
            market="KR",
            symbol=request.symbol,
            provider=self.provider_id,
            profile=self.profile,
            venue="KRX",
            timezone="Asia/Seoul",
            session="regular",
            requested_interval=request.interval,
            source_interval="1m",
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="kiwoom_kr_minute",
            coverage=coverage,
            warnings=tuple(warnings),
        )
