"""KIS 국내(KRX) 1분봉 공급자.

공식 국내주식 일별 분봉 조회 endpoint 를 사용해 원천 1분 행을 뒤로
페이지네이션하며 수집하고 KST 로 정규화한다. 상위 간격 집계는 resample
모듈이 담당하므로 이 공급자는 1m 만 반환한다.

안전 규칙:
- 최대 페이지 예산을 넘기지 않는다. 넘치면 partial + resume_cursor.
- cursor 가 진행되지 않으면 즉시 중단한다 (무한 pagination 금지).
- 일부 페이지 실패 시 이미 채택한 봉을 유지하고 partial 로 표시한다.
  다른 공급원으로 나머지를 채우는 것은 라우터 차원에서 금지된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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

_KST = ZoneInfo("Asia/Seoul")

_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
_TR_ID = "FHKST03010230"

_SESSION_OPEN = "090000"
_SESSION_CLOSE = "153000"

_DEFAULT_MAX_PAGES = 40


def _parse_row(row: dict, trading_date_str: str) -> NormalizedBar | None:
    try:
        raw_date = str(row.get("stck_bsop_date") or trading_date_str)
        raw_hour = str(row["stck_cntg_hour"])
        start = datetime.strptime(raw_date + raw_hour, "%Y%m%d%H%M%S")
        start = start.replace(second=0, tzinfo=_KST)
        return NormalizedBar(
            start_at=start,
            end_at=start + timedelta(minutes=1),
            open=Decimal(str(row["stck_oprc"])),
            high=Decimal(str(row["stck_hgpr"])),
            low=Decimal(str(row["stck_lwpr"])),
            close=Decimal(str(row["stck_prpr"])),
            volume=int(row["cntg_vol"]),
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


class KisDomesticProvider:
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
        # 모의(demo)는 KIS 지원 정책상 과거 분봉이 제한될 수 있다.
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
                f"kis_domestic은 1m 원천만 반환합니다. 요청 interval: "
                f"{request.interval}")
        if request.market != "KR":
            raise ValueError(f"kis_domestic은 KR 전용입니다: {request.market}")
        if request.trading_date is None:
            raise ValueError("trading_date가 필요합니다")

        trading_date_str = request.trading_date.strftime("%Y%m%d")
        cursor = _SESSION_CLOSE
        bars: list[NormalizedBar] = []
        warnings: list[str] = []
        dropped = 0
        pages = 0
        complete = True
        failure_status: str | None = None
        resume_cursor: str | None = None
        prev_earliest: str | None = None

        while pages < self._max_pages:
            pages += 1
            try:
                payload = await self._client.request(
                    "GET", _PATH, tr_id=_TR_ID,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": request.symbol,
                        "FID_INPUT_DATE_1": trading_date_str,
                        "FID_INPUT_HOUR_1": cursor,
                        "FID_PW_DATA_INCU_YN": "Y",
                        "FID_FAKE_TICK_INCU_YN": "N",
                    })
            except KisApiError as exc:
                if not bars:
                    raise
                complete = False
                failure_status = exc.provider_status
                resume_cursor = cursor
                warnings.append(
                    f"페이지 {pages} 조회 실패({exc.provider_status}). "
                    "이미 받은 구간만 반환합니다.")
                break

            if str(payload.get("rt_cd", "")) != "0":
                if not bars:
                    raise KisApiError("provider_unavailable")
                complete = False
                failure_status = "provider_unavailable"
                resume_cursor = cursor
                warnings.append(
                    f"페이지 {pages}에서 공급자 오류 응답. "
                    "이미 받은 구간만 반환합니다.")
                break

            rows = payload.get("output2") or []
            if not rows:
                break

            page_bars: list[NormalizedBar] = []
            for row in rows:
                bar = _parse_row(row, trading_date_str)
                if bar is None:
                    dropped += 1
                    continue
                page_bars.append(bar)

            if not page_bars:
                # 행은 있는데 하나도 못 읽었다. 형식이 바뀐 것이다.
                if not bars:
                    raise KisApiError("source_parse_error")
                complete = False
                failure_status = "source_parse_error"
                resume_cursor = cursor
                warnings.append(
                    f"페이지 {pages}의 행을 해석하지 못했습니다. "
                    "이미 받은 구간만 반환합니다.")
                break

            earliest = min(b.start_at for b in page_bars).strftime("%H%M%S")
            if prev_earliest is not None and earliest >= prev_earliest:
                warnings.append(
                    "cursor가 진행되지 않아 pagination을 중단했습니다.")
                bars.extend(page_bars)
                break
            prev_earliest = earliest
            bars.extend(page_bars)

            if len(bars) >= request.row_limit:
                break
            if earliest <= _SESSION_OPEN:
                break
            next_dt = datetime.strptime(earliest, "%H%M%S") - \
                timedelta(minutes=1)
            next_cursor = next_dt.strftime("%H%M%S")
            if next_cursor < _SESSION_OPEN:
                break
            cursor = next_cursor
        else:
            # 페이지 예산 소진. 남은 구간이 있을 수 있다.
            complete = False
            next_dt = datetime.strptime(
                prev_earliest or cursor, "%H%M%S") - timedelta(minutes=1)
            resume_cursor = next_dt.strftime("%H%M%S")
            warnings.append(
                f"페이지 예산({self._max_pages})을 소진해 중단했습니다.")

        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")

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
            source_endpoint="domestic_minute",
            coverage=coverage,
            warnings=tuple(warnings),
        )
