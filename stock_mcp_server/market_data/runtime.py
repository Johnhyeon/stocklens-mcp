"""공급자 runtime (1.0 Task 9).

서버가 증권사 클라이언트·어댑터를 만들 때 쓰는 단일 진입점.

- 상태는 v2 스냅샷으로 읽는다. 요청 하나는 스냅샷 하나에 고정된다.
- 클라이언트는 (provider, profile, provider_generation) 으로 캐시한다.
  KIS 토큰 발급 1분 1회 제한 때문에 재사용이 필수다.
- disable 된 provider 는 구성 자체를 하지 않는다.
- kiwoom·toss 클라이언트는 해당 어댑터 Task 에서 factory 로 연결된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from stock_mcp_server.market_data.connection_state import (
    load_state_v2,
    provider_capabilities_v2,
)
from stock_mcp_server.market_data.credential_store import CredentialStore
from stock_mcp_server.market_data.naver_provider import NaverBarProvider
from stock_mcp_server.market_data.yahoo_provider import YahooBarProvider


@dataclass(frozen=True)
class RuntimeSnapshot:
    """요청 하나가 쓰는 고정 상태. 진행 중 primary 교체에 오염되지 않는다."""

    state: dict = field(repr=False)
    mode: str
    primary_provider: str | None
    routing_generation: int

    def capabilities(self, provider: str) -> dict:
        return provider_capabilities_v2(self.state, provider)

    def active_profile(self, provider: str) -> str | None:
        record = self.state["providers"].get(provider)
        return record.get("active_profile") if record else None

    def provider_generation(self, provider: str) -> int:
        record = self.state["providers"].get(provider)
        return int(record["generation"]) if record else -1


class ProviderRuntime:
    def __init__(self, keyring_module=None,
                 home: Path | str | None = None,
                 token_store=None) -> None:
        if token_store is None:
            from stock_mcp_server.market_data.token_store import TokenStore
            try:
                token_store = TokenStore(keyring_module=keyring_module)
            except Exception:  # noqa: BLE001
                token_store = None
        self._token_store = token_store
        self._credentials = CredentialStore(
            keyring_module=keyring_module, home=home,
            token_store=token_store)
        self._home = home
        # (provider, profile) -> (generation, client)
        self._clients: dict[tuple[str, str], tuple[int, object]] = {}

    @property
    def home(self) -> Path | str | None:
        return self._home

    def snapshot(self) -> RuntimeSnapshot:
        state = load_state_v2(self._home)
        return RuntimeSnapshot(
            state=state,
            mode=state["data_source_mode"],
            primary_provider=state["primary_provider"],
            routing_generation=state["routing_generation"],
        )

    def invalidate_provider(self, provider: str) -> None:
        for key in [k for k in self._clients if k[0] == provider]:
            del self._clients[key]

    def client(self, provider: str, profile: str,
               snapshot: RuntimeSnapshot | None = None):
        """provider generation 에 고정된 클라이언트. 자격 증명이 없으면 None."""
        if snapshot is None:
            snapshot = self.snapshot()
        generation = snapshot.provider_generation(provider)
        cached = self._clients.get((provider, profile))
        if cached is not None and cached[0] == generation:
            return cached[1]

        client = self._build_client(provider, profile)
        if client is None:
            return None
        self._clients[(provider, profile)] = (generation, client)
        return client

    def _build_client(self, provider: str, profile: str):
        payload = self._credentials.load_active(provider, profile)
        if payload is None:
            return None
        home = self._home

        def _generation() -> int:
            return load_state_v2(home)["providers"].get(
                provider, {}).get("generation", -1)

        # 토큰은 프로세스 사이에서도 재사용한다. 실측(2026-08-28) 결과
        # KIS·키움은 재발급해도 같은 토큰을 주고(발급 자체는 KIS 가 1분
        # 1회 제한), 토스는 재발급이 이전 토큰을 즉시 무효화한다. 새
        # 프로세스가 매번 받으면 토스에서는 다른 프로세스를 망가뜨린다.
        store = self._token_store
        if provider == "kis":
            from stock_mcp_server.market_data.kis_client import KisClient
            return KisClient(payload, profile,
                             generation_provider=_generation,
                             token_store=store)
        if provider == "kiwoom":
            from stock_mcp_server.market_data.kiwoom_client import (
                KiwoomClient,
            )
            return KiwoomClient(payload, profile,
                                generation_provider=_generation,
                                token_store=store)
        if provider == "toss":
            from stock_mcp_server.market_data.toss_client import TossClient
            return TossClient(payload, profile,
                              generation_provider=_generation,
                              token_store=store)
        return None

    def providers_for(self, market: str, source: str = "auto",
                      snapshot: RuntimeSnapshot | None = None) -> dict:
        """요청 시장·source 에 필요한 공급자만 구성한다.

        기존 naver·yahoo 는 항상 포함한다 (일봉·legacy 경로).
        증권사는 요청이 향하는 하나(명시 source 또는 primary)만, 그리고
        connected + 자격 증명이 있을 때만 만든다.
        """
        if snapshot is None:
            snapshot = self.snapshot()
        providers: dict = {
            "naver": NaverBarProvider(),
            "yahoo": YahooBarProvider(),
        }
        from stock_mcp_server.market_data.provider_registry import registry

        if source in registry.ids():
            candidate = source
        else:
            candidate = snapshot.primary_provider
        if candidate is None:
            return providers
        caps = snapshot.capabilities(candidate)
        if not caps["connected"]:
            return providers
        profile = snapshot.active_profile(candidate)
        if not profile:
            return providers
        client = self.client(candidate, profile, snapshot=snapshot)
        if client is None:
            return providers
        adapter = self._build_adapter(candidate, client, profile, market)
        if adapter is not None:
            providers[candidate] = adapter
        return providers

    def _build_adapter(self, provider: str, client, profile: str,
                       market: str):
        if provider == "kis":
            if market == "KR":
                from stock_mcp_server.market_data.kis_domestic import (
                    KisDomesticProvider,
                )
                return KisDomesticProvider(client, profile)
            from stock_mcp_server.market_data.kis_overseas import (
                KisOverseasProvider,
            )
            return KisOverseasProvider(client, profile)
        if provider == "kiwoom":
            if market == "KR":
                from stock_mcp_server.market_data.kiwoom_domestic import (
                    KiwoomDomesticProvider,
                )
                return KiwoomDomesticProvider(client, profile)
            from stock_mcp_server.market_data.kiwoom_overseas import (
                KiwoomOverseasProvider,
            )
            return KiwoomOverseasProvider(client, profile)
        if provider == "toss":
            from stock_mcp_server.market_data.toss_provider import (
                TossBarProvider,
            )
            return TossBarProvider(client, profile)
        return None
