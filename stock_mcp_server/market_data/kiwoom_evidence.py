"""키움 상세 수급 공급자 (1.1, 2026-08-28 실계좌 실측 기반).

공식 스펙 + 실측으로 확정한 계약 (ka10059, POST /api/dostk/stkinfo):
- 응답 키 `stk_invsr_orgn`, 페이지 100행, 연속조회는 cont-yn / next-key
- 투자자 13종이 원본 필드명으로 온다. 이름을 바꿔 전달하되 **원본 필드명을
  같이 싣는다** (라벨-값 계약: 어느 계정에서 온 숫자인지 잃지 않는다).
- **정산 전 당일 행의 0 은 "매매 없음"이 아니라 "아직 정산 안 됨"이다.**
  실측(2026-08-28): 당일 행은 개인 0, 5주체 순매매 합 216,832 로 검산이
  깨진다. 정산된 날은 개인+외국인+기관계+기타법인+내외국인 합이 0(반올림
  ±1)이고 기관 세부 8종 합이 기관계와 같다.

그래서 이 어댑터는 **검산을 통과한 행만 final** 로 표시하고, 검산이 깨진
행의 0 값은 값이 아니라 미정산 상태로 돌려준다. 합을 맞추려고 값을
만들거나 고치지 않는다 (조용한 보정 금지).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from stock_mcp_server.market_data.evidence_models import (
    DEFAULT_MEASURE,
    INSTITUTION_PARTS,
    PRINCIPAL_CATEGORIES,
    InvestorFlowDataset,
    InvestorFlowRow,
    PressureBlock,
    PressureRow,
)
from stock_mcp_server.market_data.kiwoom_client import (
    KiwoomApiError,
    KiwoomClient,
)

_API_ID = "ka10059"
_ENDPOINT = "kr_investor_daily"
_ROWS_KEY = "stk_invsr_orgn"
_DEFAULT_MAX_PAGES = 10

# 정규 이름 -> 원본 필드명. 원본을 잃지 않는 것이 이 표의 목적이다.
INVESTOR_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("individual", "ind_invsr"),
    ("foreign", "frgnr_invsr"),
    ("institution_total", "orgn"),
    ("financial_investment", "fnnc_invt"),
    ("insurance", "insrnc"),
    ("investment_trust", "invtrt"),
    ("other_financial", "etc_fnnc"),
    ("bank", "bank"),
    ("pension_fund", "penfnd_etc"),
    ("private_equity_fund", "samo_fund"),
    ("government", "natn"),
    ("other_corporation", "etc_corp"),
    ("domestic_foreign", "natfor"),
)
_RAW_BY_NAME = dict(INVESTOR_CATEGORIES)

# 순매매 합이 0 이어야 하는 5주체 (정산 완료일 검산)
_PRINCIPALS = PRINCIPAL_CATEGORIES
# 기관계를 구성하는 세부 8종
_INSTITUTION_PARTS = INSTITUTION_PARTS
# 측정 단위. 라벨과 값이 갈라지지 않게 요청 파라미터와 함께 고정한다.
#
# 실측(2026-08-28) + KIS 교차 검증으로 확정:
# - amt_qty_tp="2", unit_tp="1"  -> 순매매 **수량(단주)**.
#   키움 개인 -3,223,427 = KIS prsn_ntby_qty -3,223,427 (자릿수까지 일치)
# - amt_qty_tp="1"               -> 순매매 **금액(백만원)**.
#   키움 개인 -862,106 = KIS prsn_ntby_tr_pbmn -862,106
# 단위를 섞으면 같은 이름의 숫자가 3.7 배 달라진다. 그래서 measure 를
# 호출자가 고르게 하고, 응답에 measure 와 unit 을 함께 싣는다.
MEASURES: dict[str, dict[str, str]] = {
    "net_quantity": {"amt_qty_tp": "2", "unit_tp": "1", "unit": "shares"},
    "net_amount": {"amt_qty_tp": "1", "unit_tp": "1",
                   "unit": "KRW_million"},
}

# 수량은 정확히 0 으로 맞고, 금액은 반올림 때문에 소폭 어긋난다 (실측).
_TOLERANCE = {"net_quantity": 0, "net_amount": 2}


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


def _parse_row(raw: dict, tolerance: int = 0) -> InvestorFlowRow | None:
    day = str(raw.get("dt") or "").strip()
    if len(day) != 8 or not day.isdigit():
        return None
    try:
        parsed_day = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None

    values: dict[str, int] = {}
    missing: list[str] = []
    for name, raw_key in INVESTOR_CATEGORIES:
        got = _int(raw.get(raw_key))
        if got is None:
            missing.append(name)
        else:
            values[name] = got

    principals = [values.get(p) for p in _PRINCIPALS]
    principal_sum = (None if any(p is None for p in principals)
                     else sum(principals))  # type: ignore[arg-type]
    balanced = (principal_sum is not None
                and abs(principal_sum) <= tolerance)

    subtotal_parts = [values.get(p) for p in _INSTITUTION_PARTS]
    institution_total = values.get("institution_total")
    institution_ok = (
        institution_total is not None
        and all(p is not None for p in subtotal_parts)
        and abs(sum(subtotal_parts) - institution_total)  # type: ignore
        <= tolerance)

    final = balanced and institution_ok
    unsettled: tuple[str, ...] = ()
    if not final:
        # 검산이 깨진 날은 정산 전이다. 그 날의 0 은 확정 수치가 아니라
        # 아직 채워지지 않은 자리다 - 값에서 빼고 상태로 표시한다.
        zero_names = tuple(n for n, v in values.items() if v == 0)
        unsettled = zero_names
        for name in zero_names:
            values.pop(name, None)

    return InvestorFlowRow(
        date=parsed_day,
        close=_decimal(raw.get("cur_prc")),
        volume=_int(raw.get("acc_trde_qty")),
        values=values,
        unsettled=unsettled,
        data_state="final" if final else "provisional",
        balance_ok=final,
        principal_sum=principal_sum,
        raw_categories=dict(INVESTOR_CATEGORIES),
    )


# ---------------------------------------------------------------------------
# 수급 압력 (1.1, 2026-08-28 실측)
#
# 종류마다 TR·경로·응답 키·측정 항목이 다르다. 종류별 블록을 분리해 각각
# 독립적인 상태를 갖게 하고, 서로 다른 종류를 하나의 점수로 합치지 않는다.
# CFD 는 키움 REST 에 조회 TR 이 없어 unsupported 를 그대로 돌려준다
# (빈 성공으로 처리하지 않는다).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PressureSpec:
    kind: str
    endpoint_id: str
    api_id: str
    rows_key: str
    # 정규 이름 -> 원본 필드명
    measures: tuple[tuple[str, str], ...]
    extra_body: tuple[tuple[str, str], ...] = ()
    needs_date_range: bool = False


PRESSURE_SPECS: tuple[PressureSpec, ...] = (
    PressureSpec(
        kind="program_trading", endpoint_id="kr_program_trade",
        api_id="ka90013", rows_key="stk_daly_prm_trde_trnsn",
        measures=(("close", "cur_prc"), ("volume", "trde_qty"),
                  ("sell_amount", "prm_sell_amt"),
                  ("buy_amount", "prm_buy_amt"),
                  ("net_amount", "prm_netprps_amt"),
                  ("sell_qty", "prm_sell_qty"),
                  ("buy_qty", "prm_buy_qty"))),
    PressureSpec(
        kind="short_selling", endpoint_id="kr_short_selling",
        api_id="ka10014", rows_key="shrts_trnsn", needs_date_range=True,
        measures=(("close", "close_pric"), ("volume", "trde_qty"),
                  ("short_volume", "shrts_qty"),
                  ("overseas_short_volume", "ovr_shrts_qty"),
                  ("trade_weight", "trde_wght"),
                  ("short_value", "shrts_trde_prica"),
                  ("short_avg_price", "shrts_avg_pric"))),
    PressureSpec(
        kind="credit", endpoint_id="kr_credit_trade",
        api_id="ka10013", rows_key="crd_trde_trend",
        extra_body=(("qry_tp", "1"),),
        measures=(("close", "cur_prc"), ("volume", "trde_qty"),
                  ("new", "new"), ("repaid", "rpya"),
                  ("balance", "remn"), ("balance_amount", "amt"),
                  ("share_rate", "shr_rt"),
                  ("balance_rate", "remn_rt"))),
    PressureSpec(
        kind="securities_lending", endpoint_id="kr_securities_lending",
        api_id="ka20068", rows_key="dbrt_trde_trnsn",
        measures=(("contracted", "dbrt_trde_cntrcnt"),
                  ("repaid", "dbrt_trde_rpy"),
                  ("change", "dbrt_trde_irds"),
                  ("balance", "rmnd"),
                  ("balance_amount", "remn_amt"))),
    PressureSpec(
        kind="foreign_holding", endpoint_id="kr_foreign_holding",
        api_id="ka10008", rows_key="stk_frgnr",
        measures=(("close", "close_pric"), ("volume", "trde_qty"),
                  ("change_qty", "chg_qty"),
                  ("holding_qty", "poss_stkcnt"),
                  ("holding_weight", "wght"),
                  ("available_qty", "gain_pos_stkcnt"),
                  ("limit_qty", "frgnr_limit"),
                  ("limit_exhaust_rate", "limit_exh_rt"))),
)
_SPEC_BY_KIND = {s.kind: s for s in PRESSURE_SPECS}

# 공급자가 조회 TR 을 제공하지 않는 종류. 빈 성공이 아니라 미지원이다.
UNSUPPORTED_KINDS = {"cfd": "not_provided_by_provider"}
PRESSURE_KINDS = tuple(_SPEC_BY_KIND) + tuple(UNSUPPORTED_KINDS)


def _pressure_value(raw: object) -> Decimal | None:
    """키움 부호 표기를 숫자로.

    실측(2026-08-28, ka90013): 프로그램매매 순매수는 음수를 `--557431`
    처럼 **이중 마이너스**로 보낸다. 산술로 확인된다 - 같은 행의
    매수 544,611 에서 매도 1,102,042 를 빼면 -557,431 이다. 앞의 부호
    문자를 값의 일부로 읽으면 부호가 뒤집히므로 여기서 정규화한다.
    """
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


def _parse_pressure_row(raw: dict, spec: PressureSpec) -> PressureRow | None:
    day = str(raw.get("dt") or "").strip()
    if len(day) != 8 or not day.isdigit():
        return None
    try:
        parsed = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None
    measures: dict[str, Decimal] = {}
    for name, raw_key in spec.measures:
        got = _pressure_value(raw.get(raw_key))
        if got is not None:
            measures[name] = got
    return PressureRow(date=parsed, measures=measures,
                       raw_fields=dict(spec.measures))


# 공급자가 실제로 주는 것만 available 이다. KIS 와 정확히 반대 모양이다:
# 키움은 기관 세부 13종을 주지만 매수·매도 분해가 없고, KIS 는 3종만
# 주지만 매수·매도를 준다. 어느 쪽도 상위집합이 아니다 (2026-08-28 실측).
_CAPABILITIES = {
    "kr.investor_flow.daily.total": "available",
    "kr.investor_flow.daily.breakdown": "available",
    "kr.investor_flow.daily.buy_sell": "unsupported",
}


class KiwoomEvidenceProvider:
    provider_id = "kiwoom"

    def __init__(self, client: KiwoomClient, profile: str) -> None:
        self._client = client
        self.profile = profile

    @staticmethod
    def capabilities() -> dict[str, str]:
        return dict(_CAPABILITIES)

    @staticmethod
    def pressure_capabilities() -> dict[str, str]:
        """종류별 지원 상태. KIS 가 못 주는 것을 키움이 덮는다.

        실측(2026-08-28): 대차·신용·외국인 보유는 키움만 종목 단위로
        준다. 프로그램매매는 키움이 일별, KIS 가 장중이라 모양이 다르다.
        """
        caps = {k: "available" for k in _SPEC_BY_KIND}
        caps.update({k: "unsupported" for k in UNSUPPORTED_KINDS})
        return caps

    @staticmethod
    def pressure_granularity() -> dict[str, str]:
        """종류별 시계열 모양. 키움은 실측상 전부 일별이다."""
        return {kind: "daily" for kind in _SPEC_BY_KIND}

    @staticmethod
    def pressure_unavailable_reasons() -> dict[str, str]:
        """왜 못 주는지. 상태만으로는 복원되지 않는 사실이다."""
        return dict(UNSUPPORTED_KINDS)

    async def fetch_investor_flow(
        self,
        symbol: str,
        *,
        base_date: date,
        measure: str = DEFAULT_MEASURE,
        max_pages: int = _DEFAULT_MAX_PAGES,
        row_limit: int = 60,
    ) -> InvestorFlowDataset:
        """종목별 투자자·기관별 일별 순매매 (ka10059).

        measure 는 수량(net_quantity, 단주)과 금액(net_amount, 백만원)
        중 하나다. 요청 파라미터와 응답 라벨이 같은 표에서 나오므로
        둘이 갈라질 수 없다.
        """
        if measure not in MEASURES:
            raise ValueError(
                f"지원하지 않는 measure: {measure} (지원: {tuple(MEASURES)})")
        spec = MEASURES[measure]
        tolerance = _TOLERANCE[measure]
        rows: list[InvestorFlowRow] = []
        warnings: list[str] = []
        dropped = 0
        pages = 0
        complete = True
        cont_yn: str | None = None
        next_key: str | None = None
        saw_rows = False

        while pages < max_pages:
            pages += 1
            response = await self._client.request(
                _ENDPOINT, api_id=_API_ID,
                body={
                    "dt": base_date.strftime("%Y%m%d"),
                    "stk_cd": symbol,
                    "amt_qty_tp": spec["amt_qty_tp"],
                    "trde_tp": "0",
                    "unit_tp": spec["unit_tp"],
                },
                cont_yn=cont_yn, next_key=next_key)

            payload = response.payload
            code = payload.get("return_code")
            if code not in (0, "0", None):
                if not rows:
                    raise KiwoomApiError("provider_unavailable")
                complete = False
                warnings.append(
                    f"페이지 {pages}에서 공급자 오류 응답. "
                    "이미 받은 구간만 반환합니다.")
                break

            page_rows = payload.get(_ROWS_KEY) or []
            if not page_rows:
                if not rows and not saw_rows:
                    # 실측: 없는 종목은 빈 목록으로 온다.
                    raise KiwoomApiError("entity_not_found")
                break
            saw_rows = True

            for raw in page_rows:
                parsed = _parse_row(raw, tolerance)
                if parsed is None:
                    dropped += 1
                    continue
                rows.append(parsed)

            if len(rows) >= row_limit:
                break
            if response.cont_yn != "Y" or not response.next_key:
                break
            cont_yn = "Y"
            next_key = response.next_key
        else:
            complete = False
            warnings.append(
                f"페이지 예산({max_pages})을 소진해 중단했습니다.")

        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")

        rows.sort(key=lambda r: r.date, reverse=True)
        if len(rows) > row_limit:
            rows = rows[:row_limit]

        provisional = [r for r in rows if r.data_state != "final"]
        if provisional:
            days = ", ".join(r.date.isoformat() for r in provisional[:3])
            warnings.append(
                f"{days} 는 아직 정산 전입니다. 투자자 구분 합이 맞지 않아 "
                "비어 있는 항목을 0으로 채우지 않고 미정산으로 표시했습니다.")

        return InvestorFlowDataset(
            symbol=symbol,
            provider=self.provider_id,
            profile=self.profile,
            market="KR",
            rows=tuple(rows),
            data_state="provisional" if provisional else "final",
            measure=measure,
            unit=spec["unit"],
            coverage={
                "pages": pages,
                "rows": len(rows),
                "complete": complete,
                "requested_rows": row_limit,
            },
            warnings=tuple(warnings),
            source_endpoint="kiwoom_kr_investor_daily",
        )

    async def fetch_supply_pressure(
        self,
        symbol: str,
        *,
        kinds: "tuple[str, ...] | list[str]",
        base_date: date,
        lookback_days: int = 30,
        row_limit: int = 30,
    ) -> dict[str, PressureBlock]:
        """수급 압력 증거를 종류별 블록으로 돌려준다.

        한 종류가 실패해도 확인된 다른 증거를 버리지 않는다. 실패한 종류의
        값을 추정하거나 만들지 않고 상태로만 남긴다.
        """
        unknown = [k for k in kinds if k not in PRESSURE_KINDS]
        if unknown:
            raise ValueError(
                f"지원하지 않는 수급 종류: {unknown} (지원: {PRESSURE_KINDS})")

        blocks: dict[str, PressureBlock] = {}
        for kind in kinds:
            if kind in UNSUPPORTED_KINDS:
                blocks[kind] = PressureBlock(
                    kind=kind, status="unsupported",
                    provider=self.provider_id, market="KR", rows=(),
                    data_as_of=None, data_completeness="none",
                    warnings=(f"{kind} 는 이 증권사가 제공하지 않습니다.",),
                    unavailable_reason=UNSUPPORTED_KINDS[kind],
                    coverage={"rows": 0, "complete": False})
                continue
            blocks[kind] = await self._fetch_pressure_kind(
                symbol, _SPEC_BY_KIND[kind], base_date, lookback_days,
                row_limit)
        return blocks

    async def _fetch_pressure_kind(
        self, symbol: str, spec: PressureSpec, base_date: date,
        lookback_days: int, row_limit: int,
    ) -> PressureBlock:
        body: dict[str, str] = {"stk_cd": symbol}
        body.update(dict(spec.extra_body))
        if spec.needs_date_range:
            start = date.fromordinal(
                max(1, base_date.toordinal() - lookback_days))
            body["strt_dt"] = start.strftime("%Y%m%d")
            body["end_dt"] = base_date.strftime("%Y%m%d")
        elif spec.api_id == "ka10013":
            body["dt"] = base_date.strftime("%Y%m%d")

        try:
            response = await self._client.request(
                spec.endpoint_id, api_id=spec.api_id, body=body)
        except KiwoomApiError as exc:
            return PressureBlock(
                kind=spec.kind, status=exc.provider_status,
                provider=self.provider_id, market="KR", rows=(),
                data_as_of=None, data_completeness="none",
                warnings=(f"{spec.kind} 조회에 실패했습니다.",),
                unavailable_reason=exc.provider_status,
                coverage={"rows": 0, "complete": False})

        payload = response.payload
        code = payload.get("return_code")
        if code not in (0, "0", None):
            return PressureBlock(
                kind=spec.kind, status="provider_unavailable",
                provider=self.provider_id, market="KR", rows=(),
                data_as_of=None, data_completeness="none",
                warnings=(f"{spec.kind} 응답이 공급자 오류였습니다.",),
                unavailable_reason="provider_error_response",
                coverage={"rows": 0, "complete": False})

        raw_rows = payload.get(spec.rows_key) or []
        rows = [r for r in (_parse_pressure_row(x, spec) for x in raw_rows)
                if r is not None]
        rows.sort(key=lambda r: r.date, reverse=True)
        dropped = len(raw_rows) - len(rows)
        if len(rows) > row_limit:
            rows = rows[:row_limit]

        warnings: list[str] = []
        if dropped:
            warnings.append(f"해석할 수 없는 행 {dropped}개를 제외했습니다.")
        if not rows:
            return PressureBlock(
                kind=spec.kind, status="ok",
                provider=self.provider_id, market="KR", rows=(),
                data_as_of=None, data_completeness="none",
                warnings=tuple(warnings + ["해당 구간 데이터가 없습니다."]),
                unavailable_reason=None,
                coverage={"rows": 0, "complete": True})

        return PressureBlock(
            kind=spec.kind, status="ok", provider=self.provider_id,
            market="KR", rows=tuple(rows), data_as_of=rows[0].date,
            data_completeness="complete" if not dropped else "partial",
            warnings=tuple(warnings), unavailable_reason=None,
            coverage={"rows": len(rows), "complete": True,
                      "cont_yn": response.cont_yn})
