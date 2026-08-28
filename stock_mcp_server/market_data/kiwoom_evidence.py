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

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

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
_PRINCIPALS = ("individual", "foreign", "institution_total",
               "other_corporation", "domestic_foreign")
# 기관계를 구성하는 세부 8종
_INSTITUTION_PARTS = ("financial_investment", "insurance",
                      "investment_trust", "other_financial", "bank",
                      "pension_fund", "private_equity_fund", "government")
# 실측 반올림 오차 허용치 (정산일에도 ±1~2 가 관찰된다)
_PRINCIPAL_TOLERANCE = 2
_INSTITUTION_TOLERANCE = 2


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


@dataclass(frozen=True)
class InvestorFlowRow:
    """하루치 투자자별 순매매.

    values 에 없는 정규 이름은 '값이 없다'는 뜻이고, unsettled 에 있으면
    '정산 전이라 아직 값이 아니다'라는 뜻이다. 둘을 0 으로 뭉개지 않는다.
    """

    date: date
    close: Decimal | None
    volume: int | None
    values: dict[str, int]
    unsettled: tuple[str, ...]
    data_state: str
    balance_ok: bool
    principal_sum: int | None
    raw_categories: dict[str, str] = field(
        default_factory=lambda: dict(INVESTOR_CATEGORIES))

    def value(self, name: str) -> int | None:
        return self.values.get(name)

    def is_unsettled(self, name: str) -> bool:
        return name in self.unsettled

    def raw_category(self, name: str) -> str | None:
        return self.raw_categories.get(name)

    def institution_subtotal(self) -> int | None:
        parts = [self.values.get(p) for p in _INSTITUTION_PARTS]
        if any(p is None for p in parts):
            return None
        return sum(parts)  # type: ignore[arg-type]


@dataclass(frozen=True)
class InvestorFlowDataset:
    symbol: str
    provider: str
    profile: str
    market: str
    rows: tuple[InvestorFlowRow, ...]
    data_state: str
    coverage: dict
    warnings: tuple[str, ...]
    source_endpoint: str = "kiwoom_kr_investor_daily"


def _parse_row(raw: dict) -> InvestorFlowRow | None:
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
                and abs(principal_sum) <= _PRINCIPAL_TOLERANCE)

    subtotal_parts = [values.get(p) for p in _INSTITUTION_PARTS]
    institution_total = values.get("institution_total")
    institution_ok = (
        institution_total is not None
        and all(p is not None for p in subtotal_parts)
        and abs(sum(subtotal_parts) - institution_total)  # type: ignore
        <= _INSTITUTION_TOLERANCE)

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
    )


class KiwoomEvidenceProvider:
    provider_id = "kiwoom"

    def __init__(self, client: KiwoomClient, profile: str) -> None:
        self._client = client
        self.profile = profile

    async def fetch_investor_flow(
        self,
        symbol: str,
        *,
        base_date: date,
        max_pages: int = _DEFAULT_MAX_PAGES,
        row_limit: int = 60,
    ) -> InvestorFlowDataset:
        """종목별 투자자·기관별 일별 순매매 (ka10059)."""
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
                    # 실측 기준값: 금액이 아니라 수량(순매매 주수)
                    "amt_qty_tp": "1",
                    "trde_tp": "0",
                    "unit_tp": "1000",
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
                parsed = _parse_row(raw)
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
            coverage={
                "pages": pages,
                "rows": len(rows),
                "complete": complete,
                "requested_rows": row_limit,
            },
            warnings=tuple(warnings),
        )
