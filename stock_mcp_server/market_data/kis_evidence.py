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


class KisEvidenceProvider:
    provider_id = "kis"

    def __init__(self, client: KisClient, profile: str) -> None:
        self._client = client
        self.profile = profile

    @staticmethod
    def capabilities() -> dict[str, str]:
        return dict(_CAPABILITIES)

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
