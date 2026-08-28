"""상세 수급 공급자 프로토콜 (1.1 Task 5).

시세(`MarketDataProvider`)와 분리해 둔다. 공급자는 봉만, 증거만, 또는
둘 다 구현할 수 있다. 실제로 갈라진다: 네이버·야후는 봉만 주고, 증권사
어댑터는 둘 다 준다.

이 프로토콜이 종류별 메서드(`fetch_short_selling`, `fetch_credit_trades`
...)를 두지 않는 이유는 대표 결정(2026-08-28) 때문이다. 공개 도구를
종류 수만큼 늘리지 않기로 했고, 종류는 인자로 고르고 응답에서 **블록으로**
나뉜다. 내부 프로토콜도 같은 모양을 따라야 계층 사이에서 종류 목록이
두 번 관리되지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from stock_mcp_server.market_data.evidence_models import (
    InvestorFlowDataset,
    PressureBlock,
)


@runtime_checkable
class MarketEvidenceProvider(Protocol):
    """증권사 상세 수급 어댑터가 만족해야 하는 계약.

    capability 표는 **정적**이다. 실측으로 확정한 사실이라 연결 상태나
    요청과 무관하게 같은 답을 준다. 이 키에 대한 사용자별 권한 문제는
    호출 시점의 상태(`permission_denied` 등)로 드러나며, 여기서 미리
    안다고 주장하지 않는다.
    """

    provider_id: str
    profile: str

    @staticmethod
    def capabilities() -> dict[str, str]:
        """투자자 수급 세부 능력. 값은 available | unsupported."""
        ...

    @staticmethod
    def pressure_capabilities() -> dict[str, str]:
        """수급 압력 종류별 능력. 값은 available | unsupported | unverified."""
        ...

    async def fetch_investor_flow(
        self, symbol: str, *, base_date: date, measure: str = ...,
        row_limit: int = ...,
    ) -> InvestorFlowDataset:
        ...

    async def fetch_supply_pressure(
        self, symbol: str, *, kinds: "tuple[str, ...] | list[str]",
        base_date: date, lookback_days: int = ..., row_limit: int = ...,
    ) -> dict[str, PressureBlock]:
        ...
