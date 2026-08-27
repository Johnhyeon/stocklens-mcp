"""증권사 Provider Registry (1.0 설계 5절).

증권사 정의는 이 파일의 불변 상수뿐이다. 외부 설정 파일·환경 변수·
사용자 입력으로 공급자를 추가하거나 host·path 를 바꿀 수 없다.

허용 목록 원칙: 실측으로 검증한 시세·인증 endpoint 만 올린다.
주문·계좌·잔고 계열 경로는 어떤 descriptor 에도 정의하지 않는다.
(키움 미국 분 차트 경로는 공식 가이드 확정 후 해당 Task 에서 추가한다.)

factory 필드는 이후 Task 에서 어댑터가 구현되는 대로 연결된다.
serialize 되는 공개 출력(describe_public)에는 절대 포함하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class UnknownProviderError(Exception):
    """레지스트리에 없는 provider 요청."""


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = True
    max_length: int = 2048


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    credential_schema: tuple[CredentialField, ...]
    supported_profiles: tuple[str, ...]
    signup_url: str
    docs_url: str
    # 어댑터 factory. 공개 직렬화 대상이 아니며 repr 에서도 감춘다.
    auth_factory: Callable[..., Any] | None = field(default=None, repr=False)
    provider_factory: Callable[..., Any] | None = field(
        default=None, repr=False)
    capability_probe_factory: Callable[..., Any] | None = field(
        default=None, repr=False)


_KIS = ProviderDescriptor(
    provider_id="kis",
    display_name="한국투자증권",
    allowed_hosts=(
        "openapi.koreainvestment.com:9443",
        "openapivts.koreainvestment.com:29443",
    ),
    allowed_paths=(
        "/oauth2/tokenP",
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
        "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
    ),
    credential_schema=(
        CredentialField(name="app_key", label="앱 키"),
        CredentialField(name="app_secret", label="앱 시크릿"),
    ),
    supported_profiles=("real", "demo"),
    signup_url="https://apiportal.koreainvestment.com",
    docs_url="https://apiportal.koreainvestment.com/docs",
)

_KIWOOM = ProviderDescriptor(
    provider_id="kiwoom",
    display_name="키움증권",
    allowed_hosts=(
        "api.kiwoom.com",
        "mockapi.kiwoom.com",
    ),
    allowed_paths=(
        "/oauth2/token",
        "/api/dostk/chart",
    ),
    credential_schema=(
        CredentialField(name="app_key", label="앱 키"),
        CredentialField(name="secret_key", label="시크릿 키"),
    ),
    supported_profiles=("real", "demo"),
    signup_url="https://openapi.kiwoom.com",
    docs_url="https://openapi.kiwoom.com/m/guide/apiguide",
)

_TOSS = ProviderDescriptor(
    provider_id="toss",
    display_name="토스증권",
    allowed_hosts=(
        "openapi.tossinvest.com",
    ),
    allowed_paths=(
        "/oauth2/token",
        "/api/v1/candles",
    ),
    credential_schema=(
        CredentialField(name="client_id", label="클라이언트 ID"),
        CredentialField(name="client_secret", label="클라이언트 시크릿"),
    ),
    # 캔들 세션·시장 범위 실측 인증 전까지 실전 프로필만 노출한다.
    supported_profiles=("real",),
    signup_url="https://corp.tossinvest.com/ko/open-api",
    docs_url="https://developers.tossinvest.com/docs",
)


class ProviderRegistry:
    def __init__(self, descriptors: tuple[ProviderDescriptor, ...]):
        self._by_id = {d.provider_id: d for d in descriptors}
        self._order = tuple(d.provider_id for d in descriptors)

    def ids(self) -> tuple[str, ...]:
        return self._order

    def require(self, provider_id: str) -> ProviderDescriptor:
        descriptor = self._by_id.get(provider_id)
        if descriptor is None:
            raise UnknownProviderError(
                f"등록되지 않은 provider: {provider_id!r}")
        return descriptor

    def describe_public(self) -> list[dict]:
        """Manager·CLI·doctor 용 안전한 공개 계약.

        factory·host·path 는 포함하지 않는다. host·path 는 transport
        계층이 레지스트리에서 직접 읽는다.
        """
        entries = []
        for provider_id in self._order:
            d = self._by_id[provider_id]
            entries.append({
                "provider_id": d.provider_id,
                "display_name": d.display_name,
                "supported_profiles": list(d.supported_profiles),
                "signup_url": d.signup_url,
                "docs_url": d.docs_url,
                "credential_fields": [
                    {
                        "name": f.name,
                        "label": f.label,
                        "secret": f.secret,
                        "max_length": f.max_length,
                    }
                    for f in d.credential_schema
                ],
            })
        return entries


registry = ProviderRegistry((_KIS, _KIWOOM, _TOSS))
