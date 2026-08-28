"""KIS 상세 수급 공급자 (1.1, 2026-08-28 실계좌 실측 기반).

실측으로 확정한 계약 (GET /uapi/domestic-stock/v1/quotations/
inquire-investor, tr_id FHKST01010900):
- 응답 키 `output`, 30행. 페이지네이션 없음.
- 투자자 구분은 **3종뿐**이다: prsn(개인), frgn(외국인), orgn(기관계).
  키움이 주는 기관 세부 8종이 없다.
- 대신 구분마다 **순매수·매수·매도 × 수량·금액** 6개를 준다.
  그래서 `순매수 = 매수 - 매도` 를 응답 안에서 검산할 수 있다.
- **정산 전 당일 행은 빈 문자열**이다 (18개 필드 전부). 키움이 0 을
  주는 것과 표현만 다르고 뜻은 같다.

교차 검증 (2026-08-27, 삼성전자): KIS prsn_ntby_qty -3,223,427 이
키움 ind_invsr -3,223,427 과 자릿수까지 같다. 금액도 -862,106 으로 같다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from stock_mcp_server.market_data.evidence_models import (
    DEFAULT_MEASURE,
    UNIT_BY_MEASURE,
    InvestorFlowDataset,
    InvestorFlowRow,
    PressureBlock,
    PressureRow,
)
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient

_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
_TR_ID = "FHKST01010900"

# (정규 이름, 수량 필드, 금액 필드)
_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("individual", "prsn_ntby_qty", "prsn_ntby_tr_pbmn"),
    ("foreign", "frgn_ntby_qty", "frgn_ntby_tr_pbmn"),
    ("institution_total", "orgn_ntby_qty", "orgn_ntby_tr_pbmn"),
    ("individual_buy", "prsn_shnu_vol", "prsn_shnu_tr_pbmn"),
    ("foreign_buy", "frgn_shnu_vol", "frgn_shnu_tr_pbmn"),
    ("institution_total_buy", "orgn_shnu_vol", "orgn_shnu_tr_pbmn"),
    ("individual_sell", "prsn_seln_vol", "prsn_seln_tr_pbmn"),
    ("foreign_sell", "frgn_seln_vol", "frgn_seln_tr_pbmn"),
    ("institution_total_sell", "orgn_seln_vol", "orgn_seln_tr_pbmn"),
)

# 공급자가 실제로 주는 것만 available 이다. 못 주는 것을 빈 값으로
# 채우지 않고 unsupported 로 신고한다.
_CAPABILITIES = {
    "kr.investor_flow.daily.total": "available",
    "kr.investor_flow.daily.buy_sell": "available",
    # 기관 세부 8종(금융투자·보험·투신·은행·연기금·사모·국가 등)이 없다.
    "kr.investor_flow.daily.breakdown": "unsupported",
}


def _int(raw: object) -> int | None:
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    if text[0] == "+":
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return None


def _decimal(raw: object) -> Decimal | None:
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    if text[0] in "+-":
        text = text[1:]
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_row(raw: dict, measure: str) -> InvestorFlowRow | None:
    day = str(raw.get("stck_bsop_date") or "").strip()
    if len(day) != 8 or not day.isdigit():
        return None
    try:
        parsed = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None

    index = 1 if measure == "net_quantity" else 2
    values: dict[str, int] = {}
    unsettled: list[str] = []
    raw_map: dict[str, str] = {}
    for entry in _CATEGORIES:
        name, field_name = entry[0], entry[index]
        raw_map[name] = field_name
        got = _int(raw.get(field_name))
        if got is None:
            # 실측: 정산 전 당일 행은 빈 문자열이다. 값이 아니라 상태다.
            unsettled.append(name)
        else:
            values[name] = got

    final = not unsettled
    return InvestorFlowRow(
        date=parsed,
        close=_decimal(raw.get("stck_clpr")),
        volume=None,
        values=values,
        unsettled=tuple(unsettled),
        data_state="final" if final else "provisional",
        balance_ok=final,
        # 5주체 합계 검산은 KIS 로 불가능하다 - 기타법인·내외국인이
        # 응답에 없어서 합이 0 이 되지 않는다. 대신 매수-매도 검산을 쓴다.
        principal_sum=None,
        raw_categories=raw_map,
    )


# ---------------------------------------------------------------------------
# 수급 압력 (1.1, 2026-08-28 실측)
#
# 실측으로 데이터를 받은 것만 구현한다. 확인하지 못한 것은 사유와 함께
# 상태로 신고하고, 시장 전체 값을 종목별 요청의 답으로 돌려주지 않는다.
# ---------------------------------------------------------------------------

_PRESSURE_SPECS: dict[str, dict] = {
    "short_selling": {
        "endpoint_id": "kr_short_selling",
        "path": "/uapi/domestic-stock/v1/quotations/daily-short-sale",
        "tr_id": "FHPST04830000",
        "rows_key": "output2",
        "granularity": "daily",
        "date_field": "stck_bsop_date",
        "measures": (("close", "stck_clpr"), ("volume", "acml_vol"),
                     ("short_volume", "ssts_cntg_qty"),
                     ("short_ratio", "ssts_vol_rlim"),
                     ("short_value", "ssts_tr_pbmn"),
                     ("cum_short_volume", "acml_ssts_cntg_qty"),
                     ("cum_short_ratio", "acml_ssts_cntg_qty_rlim")),
        "date_range": True,
    },
    "program_trading": {
        "endpoint_id": "kr_program_trade",
        "path": "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
        "tr_id": "FHPPG04650100",
        "rows_key": "output",
        # 실측: 키움 ka90013 은 일별인데 KIS 는 장중 시계열이다.
        "granularity": "intraday",
        "time_field": "bsop_hour",
        "measures": (("close", "stck_prpr"), ("volume", "acml_vol"),
                     ("sell_qty", "whol_smtn_seln_vol"),
                     ("buy_qty", "whol_smtn_shnu_vol"),
                     ("net_qty", "whol_smtn_ntby_qty"),
                     ("sell_amount", "whol_smtn_seln_tr_pbmn"),
                     ("buy_amount", "whol_smtn_shnu_tr_pbmn"),
                     ("net_amount", "whol_smtn_ntby_tr_pbmn")),
    },
}

# 공급자가 이 종목 단위로 주지 않는 것. 사유를 함께 신고한다.
_PRESSURE_UNSUPPORTED = {
    # daily-loan-trans 는 지수 단위 값을 돌려준다 (실측: stck_prpr
    # 6912.37, acml_vol 2.68억). 종목별 요청의 답으로 쓰면 안 된다.
    "securities_lending": "market_level_only",
    # 종목별 외국인 보유주수·한도소진율 조회를 찾지 못했다. inquire-member
    # 는 회원사 순위, frgnmem-pchs-trend 는 외국계 회원사 매매 추이다.
    "foreign_holding": "not_provided_by_provider",
    "cfd": "not_provided_by_provider",
}
# 호출은 되는데 데이터를 확인하지 못했다. 된다고도 안 된다고도 하지 않는다.
_PRESSURE_UNVERIFIED = {
    # daily-credit-balance: 필수 파라미터를 모두 채워 rt_cd 0 을 받았지만
    # output 이 모든 날짜에서 비어 있었다 (2026-08-28). 권한 문제인지
    # 파라미터 문제인지 실측으로 가르지 못했다.
    "credit": "empty_response_not_verified",
}
PRESSURE_KINDS = (tuple(_PRESSURE_SPECS) + tuple(_PRESSURE_UNSUPPORTED)
                  + tuple(_PRESSURE_UNVERIFIED))



def _parse_pressure_row(raw: dict, spec: dict,
                        base_date: date) -> PressureRow | None:
    """압력 행 하나. 일별은 날짜 필드를, 장중은 기준일 + 시각을 쓴다."""
    if spec.get("date_field"):
        day = str(raw.get(spec["date_field"]) or "").strip()
        if len(day) != 8 or not day.isdigit():
            return None
        try:
            parsed = datetime.strptime(day, "%Y%m%d").date()
        except ValueError:
            return None
    else:
        # 장중 시계열은 날짜 필드가 없다. 조회 기준일로 붙인다.
        if not str(raw.get(spec["time_field"]) or "").strip():
            return None
        parsed = base_date

    measures = {}
    for name, field_name in spec["measures"]:
        got = _decimal_signed(raw.get(field_name))
        if got is not None:
            measures[name] = got
    return PressureRow(date=parsed, measures=measures,
                       raw_fields=dict(spec["measures"]))


def _decimal_signed(raw: object) -> Decimal | None:
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("--"):
        text = text[1:]
    elif text[0] == "+":
        text = text[1:]
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


class KisEvidenceProvider:
    provider_id = "kis"

    def __init__(self, client: KisClient, profile: str) -> None:
        self._client = client
        self.profile = profile

    @staticmethod
    def capabilities() -> dict[str, str]:
        return dict(_CAPABILITIES)

    @staticmethod
    def pressure_capabilities() -> dict[str, str]:
        caps = {k: "available" for k in _PRESSURE_SPECS}
        caps.update({k: "unsupported" for k in _PRESSURE_UNSUPPORTED})
        caps.update({k: "unverified" for k in _PRESSURE_UNVERIFIED})
        return caps

    @staticmethod
    def pressure_unavailable_reasons() -> dict[str, str]:
        """왜 못 주는지. 상태만으로는 복원되지 않는 사실이다.

        `market_level_only` 와 `not_provided_by_provider` 는 사용자에게
        전혀 다른 말이다. 앞의 것은 숫자가 있긴 한데 종목 단위가 아니라
        이 질문의 답으로 쓰면 라벨이 틀리는 경우고, 뒤의 것은 공급자에게
        아예 없는 경우다. 상태 하나로 뭉개면 이 구분이 사라진다.
        """
        reasons = dict(_PRESSURE_UNSUPPORTED)
        reasons.update(_PRESSURE_UNVERIFIED)
        return reasons

    async def fetch_supply_pressure(
        self, symbol: str, *, kinds, base_date: date,
        lookback_days: int = 30, row_limit: int = 30,
    ) -> dict[str, PressureBlock]:
        unknown = [k for k in kinds if k not in PRESSURE_KINDS]
        if unknown:
            raise ValueError(
                f"지원하지 않는 수급 종류: {unknown} (지원: {PRESSURE_KINDS})")

        blocks: dict[str, PressureBlock] = {}
        for kind in kinds:
            if kind in _PRESSURE_UNSUPPORTED:
                blocks[kind] = self._state_block(
                    kind, "unsupported", _PRESSURE_UNSUPPORTED[kind],
                    f"{kind} 는 이 증권사가 종목 단위로 제공하지 않습니다.")
                continue
            if kind in _PRESSURE_UNVERIFIED:
                blocks[kind] = self._state_block(
                    kind, "unverified", _PRESSURE_UNVERIFIED[kind],
                    f"{kind} 는 응답은 오지만 데이터를 확인하지 못했습니다.")
                continue
            blocks[kind] = await self._fetch_kind(
                kind, symbol, base_date, lookback_days, row_limit)
        return blocks

    def _state_block(self, kind: str, status: str, reason: str,
                     message: str) -> PressureBlock:
        return PressureBlock(
            kind=kind, status=status, provider=self.provider_id,
            market="KR", rows=(), data_as_of=None,
            data_completeness="none", warnings=(message,),
            unavailable_reason=reason,
            coverage={"rows": 0, "complete": False},
            granularity="unknown")

    async def _fetch_kind(self, kind: str, symbol: str, base_date: date,
                          lookback_days: int,
                          row_limit: int) -> PressureBlock:
        spec = _PRESSURE_SPECS[kind]
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        if spec.get("date_range"):
            start = date.fromordinal(
                max(1, base_date.toordinal() - lookback_days))
            params["FID_INPUT_DATE_1"] = start.strftime("%Y%m%d")
            params["FID_INPUT_DATE_2"] = base_date.strftime("%Y%m%d")

        try:
            payload = await self._client.request(
                "GET", spec["path"], tr_id=spec["tr_id"], params=params)
        except KisApiError as exc:
            return PressureBlock(
                kind=kind, status=exc.provider_status,
                provider=self.provider_id, market="KR", rows=(),
                data_as_of=None, data_completeness="none",
                warnings=(f"{kind} 조회에 실패했습니다.",),
                unavailable_reason=exc.provider_status,
                coverage={"rows": 0, "complete": False},
                granularity=spec["granularity"])

        if str(payload.get("rt_cd", "0")) != "0":
            return PressureBlock(
                kind=kind, status="provider_unavailable",
                provider=self.provider_id, market="KR", rows=(),
                data_as_of=None, data_completeness="none",
                warnings=(f"{kind} 응답이 공급자 오류였습니다.",),
                unavailable_reason="provider_error_response",
                coverage={"rows": 0, "complete": False},
                granularity=spec["granularity"])

        raw_rows = payload.get(spec["rows_key"]) or []
        rows: list[PressureRow] = []
        for raw in raw_rows:
            parsed = _parse_pressure_row(raw, spec, base_date)
            if parsed is not None:
                rows.append(parsed)
        dropped = len(raw_rows) - len(rows)
        rows.sort(key=lambda r: r.date, reverse=True)
        if len(rows) > row_limit:
            rows = rows[:row_limit]

        warnings: list[str] = []
        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        if spec["granularity"] == "intraday":
            warnings.append(
                "이 증권사의 프로그램매매는 장중 시계열입니다. "
                "일별 값과 직접 비교하지 마세요.")
        return PressureBlock(
            kind=kind, status="ok", provider=self.provider_id, market="KR",
            rows=tuple(rows),
            data_as_of=rows[0].date if rows else None,
            data_completeness="complete" if rows and not dropped
            else ("partial" if rows else "none"),
            warnings=tuple(warnings), unavailable_reason=None,
            coverage={"rows": len(rows), "complete": True},
            granularity=spec["granularity"])

    async def fetch_investor_flow(
        self,
        symbol: str,
        *,
        base_date: date,
        measure: str = DEFAULT_MEASURE,
        row_limit: int = 30,
    ) -> InvestorFlowDataset:
        """종목별 투자자 일별 수급 (FHKST01010900).

        base_date 는 계약상 사용하지 않는다. KIS 는 최근 30 거래일을
        고정으로 돌려주며 조회 기준일 인자를 받지 않는다 (실측). 인자를
        받아 두는 이유는 호출부가 공급자별로 갈라지지 않게 하기 위해서다.
        """
        if measure not in UNIT_BY_MEASURE:
            raise ValueError(
                f"지원하지 않는 measure: {measure} "
                f"(지원: {tuple(UNIT_BY_MEASURE)})")

        payload = await self._client.request(
            "GET", _PATH, tr_id=_TR_ID,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            })

        if str(payload.get("rt_cd", "0")) != "0":
            raise KisApiError("provider_unavailable")

        raw_rows = payload.get("output") or []
        if not raw_rows:
            # 없는 종목은 빈 목록으로 온다.
            raise KisApiError("entity_not_found")

        rows = [r for r in (_parse_row(x, measure) for x in raw_rows)
                if r is not None]
        dropped = len(raw_rows) - len(rows)
        rows.sort(key=lambda r: r.date, reverse=True)
        if len(rows) > row_limit:
            rows = rows[:row_limit]

        warnings: list[str] = []
        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        provisional = [r for r in rows if r.data_state != "final"]
        if provisional:
            days = ", ".join(r.date.isoformat() for r in provisional[:3])
            warnings.append(
                f"{days} 는 아직 정산 전입니다. 공급자가 값을 비워 보내므로 "
                "0으로 채우지 않고 미정산으로 표시했습니다.")

        return InvestorFlowDataset(
            symbol=symbol,
            provider=self.provider_id,
            profile=self.profile,
            market="KR",
            rows=tuple(rows),
            data_state="provisional" if provisional else "final",
            measure=measure,
            unit=UNIT_BY_MEASURE[measure],
            coverage={"rows": len(rows), "complete": True,
                      "requested_rows": row_limit},
            warnings=tuple(warnings),
            source_endpoint="kis_kr_investor_daily",
        )
