"""상세 수급 증거의 공용 모델 (1.1).

공급자마다 주는 것이 다르다. 어느 쪽도 상위집합이 아니라서, 같은 모양의
결과에 담되 **무엇을 못 주는지**를 capability 로 정직하게 신고한다
(2026-08-28 실측):

| | 키움 | KIS |
|---|---|---|
| 투자자 구분 | 13종 (기관 세부 포함) | 3종 (개인·외국인·기관계) |
| 매수·매도 분해 | 없음 (순매매만) | 있음 (매수·매도·순매수) |
| 미정산 표현 | 0 + 합계 검산 깨짐 | 빈 문자열 |

정산 전 값은 어느 쪽이든 **값이 아니라 상태**다. 0 으로 채우거나 합을
맞추려고 만들어내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# 측정 단위. 라벨과 값이 갈라지지 않게 요청 파라미터와 함께 고정한다.
# 실측 교차 검증(2026-08-27 삼성전자): 수량 기준에서 키움 -3,223,427 과
# KIS -3,223,427 이 자릿수까지 같다. 금액 기준은 -862,106 으로 같다.
UNIT_BY_MEASURE = {
    "net_quantity": "shares",
    "net_amount": "KRW_million",
}
DEFAULT_MEASURE = "net_quantity"

# 정규 투자자 구분 이름. 공급자별 원본 필드명은 어댑터가 함께 싣는다.
PRINCIPAL_CATEGORIES = ("individual", "foreign", "institution_total",
                        "other_corporation", "domestic_foreign")
INSTITUTION_PARTS = ("financial_investment", "insurance",
                     "investment_trust", "other_financial", "bank",
                     "pension_fund", "private_equity_fund", "government")


@dataclass(frozen=True)
class InvestorFlowRow:
    """하루치 투자자별 수급.

    values 에 없는 이름은 값이 없다는 뜻이고, unsettled 에 있으면 정산
    전이라 아직 값이 아니라는 뜻이다. 둘을 0 으로 뭉개지 않는다.
    """

    date: date
    close: Decimal | None
    volume: int | None
    values: dict[str, int]
    unsettled: tuple[str, ...]
    data_state: str
    balance_ok: bool
    principal_sum: int | None
    raw_categories: dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> int | None:
        return self.values.get(name)

    def is_unsettled(self, name: str) -> bool:
        return name in self.unsettled

    def raw_category(self, name: str) -> str | None:
        return self.raw_categories.get(name)

    def institution_subtotal(self) -> int | None:
        parts = [self.values.get(p) for p in INSTITUTION_PARTS]
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
    # 값의 이름표. 이게 없으면 수량과 금액이 같은 이름으로 섞인다.
    measure: str = DEFAULT_MEASURE
    unit: str = "shares"
    source_endpoint: str = ""


@dataclass(frozen=True)
class PressureRow:
    date: date
    measures: dict[str, Decimal]
    raw_fields: dict[str, str]

    def value(self, name: str) -> Decimal | None:
        return self.measures.get(name)

    def raw_field(self, name: str) -> str | None:
        return self.raw_fields.get(name)


@dataclass(frozen=True)
class PressureBlock:
    """한 종류의 증거. 다른 종류와 절대 합치지 않는다.

    granularity 를 함께 싣는 이유: 같은 종류라도 공급자마다 모양이 다르다.
    실측(2026-08-28) 프로그램매매는 키움이 **일별**, KIS 가 **장중
    시계열**이다. 말하지 않으면 사용자가 두 숫자를 같은 것으로 읽는다.

    status 는 available 여부만이 아니라 확인 수준까지 담는다:
    - ok           데이터를 받았다
    - unsupported  공급자가 이 종목 단위로 제공하지 않는다 (사유 필수)
    - unverified   호출은 되는데 데이터를 확인하지 못했다. 된다고도
                   안 된다고도 하지 않는다
    - 그 외         공급자 오류 상태 그대로
    """

    kind: str
    status: str
    provider: str
    market: str
    rows: tuple[PressureRow, ...]
    data_as_of: date | None
    data_completeness: str
    warnings: tuple[str, ...]
    unavailable_reason: str | None
    coverage: dict
    granularity: str = "daily"
