"""공급자 프로토콜. 모든 시세 공급자(KIS·Naver·Yahoo 어댑터)가 구현한다."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    ProviderCapabilities,
)


@runtime_checkable
class MarketDataProvider(Protocol):
    provider_id: str

    async def capabilities(self, profile: str) -> ProviderCapabilities: ...

    async def fetch_bars(self, request: BarRequest) -> BarDataset: ...
