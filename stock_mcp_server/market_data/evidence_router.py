"""상세 수급 라우팅 (1.1 Task 5).

1.0 시세 라우팅과 같은 계약이다. 요청 하나는 공급자 하나에 고정되고,
자동으로 갈아타지 않으며, 못 하는 것은 빈 성공이 아니라 사유가 있는
미지원이다.

능력 판정의 두 축을 1.0 과 똑같이 유지한다. 하나의 저장된 enum 으로
합치지 않는다:

1. **공급자가 실제로 그 데이터를 주는가** - 어댑터의 실측 capability 표
2. **출시해도 되는가** - `provider_registry._RELEASE_VERIFIED` 코드 고정표

1.0 분봉과 다른 점이 하나 있다. 분봉의 1번 축은 연결 시험 결과라서 상태
파일에 저장되지만, 증거 능력은 **상태 파일에 저장하지 않는다.** 어댑터가
실측으로 확정한 정적 사실이라 저장할 이유가 없고, 저장하면 상태 파일
스키마가 올라가 1.0.0 으로의 롤백 호환이 깨진다. 사용자별 권한 문제는
미리 아는 척하지 않고 호출 시점 상태로 드러낸다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 능력 이름. 1.0 분봉 능력(kr_intraday 등)과 겹치지 않게 짓는다. 이름이
# 겹치면 분봉 게이트를 여는 커밋이 증거 게이트까지 함께 열어버린다.
PRESSURE_KINDS: tuple[str, ...] = (
    "program_trading", "short_selling", "credit",
    "securities_lending", "foreign_holding", "cfd",
)
EVIDENCE_CAPABILITIES: tuple[str, ...] = (
    ("kr_investor_flow",) + tuple(f"kr_{k}" for k in PRESSURE_KINDS))

# 종류마다 게이트를 따로 두는 이유는 모양이 달라서다. 실측(2026-08-28)
# 프로그램매매는 KIS 가 장중 시계열, 키움이 일별이다. 게이트를 공유하면
# 한쪽 UAT 가 다른 쪽을 열어준다.
_CAPABILITY_TO_KIND = {f"kr_{k}": k for k in PRESSURE_KINDS}


class EvidenceRouterError(Exception):
    """비밀 없는 라우팅 오류.

    provider_status 는 1.0 설계 27절 값 그대로 쓴다 (meta 계약이 이미
    검증한다). error_code 는 서비스 계층이 data_availability 에 적는
    사유이고, alternative 는 **안내용**이다. 전환이 아니다.
    """

    def __init__(self, provider_status: str, message: str, *,
                 error_code: str, provider: str | None = None,
                 capability: str | None = None,
                 alternative: str | None = None):
        self.provider_status = provider_status
        self.error_code = error_code
        self.provider = provider
        self.capability = capability
        self.alternative = alternative
        super().__init__(message)


@dataclass(frozen=True)
class EvidenceResolution:
    """고정된 선택 하나. 대체 공급자 인자를 갖지 않는다."""

    capability: str
    requested_source: str
    selected_provider: str
    selection_reason: str
    primary_provider: str | None = None


def _adapter_capabilities(provider_id: str) -> dict[str, str]:
    """어댑터가 실측으로 확정한 능력표를 능력 이름으로 정규화한다."""
    if provider_id == "kis":
        from stock_mcp_server.market_data.kis_evidence import (
            KisEvidenceProvider as _P,
        )
    elif provider_id == "kiwoom":
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider as _P,
        )
    else:
        # 토스 증거 어댑터는 없다 (개발자 모드에서도 미구현).
        return {}

    flow = _P.capabilities()
    result: dict[str, str] = {
        # 투자자 수급은 '합계'를 줄 수 있으면 가능한 것으로 본다. 세부
        # 구분·매수매도 분해 여부는 응답의 data_availability 가 말한다.
        "kr_investor_flow": flow.get("kr.investor_flow.daily.total",
                                     "unsupported"),
    }
    pressure = _P.pressure_capabilities()
    for capability, kind in _CAPABILITY_TO_KIND.items():
        result[capability] = pressure.get(kind, "unsupported")
    return result


def capability_state(provider_id: str, capability: str,
                     provider_caps: dict | None) -> str:
    """이 공급자로 이 증거를 지금 받을 수 있는가.

    not_configured  연결되어 있지 않다 (키 문제와 지원 문제는 다르다)
    unsupported     공급자가 이 데이터를 주지 않는다
    unverified      호출은 되는데 데이터를 확인하지 못했다
    verifying       공급자는 주는데 출시 검증 전이다
    available       두 축 모두 통과
    """
    if not provider_caps or not provider_caps.get("connected"):
        return "not_configured"
    measured = _adapter_capabilities(provider_id).get(capability)
    if measured is None or measured == "unsupported":
        return "unsupported"
    if measured == "unverified":
        return "unverified"

    from stock_mcp_server.market_data.provider_registry import (
        is_release_verified,
    )
    return ("available" if is_release_verified(provider_id, capability)
            else "verifying")


def _public_ids(public_providers: "tuple[str, ...] | None") -> tuple[str, ...]:
    if public_providers is not None:
        return tuple(public_providers)
    from stock_mcp_server.market_data.provider_registry import registry
    return registry.ids()


def _alternative_for(capability: str, exclude: str, state_of,
                     public: tuple[str, ...]) -> str | None:
    """이미 연결된 다른 증권사가 이걸 할 수 있으면 그 이름.

    전환하지 않는다. 다만 사용자가 이미 연결해 둔 증권사로 가능한 일을
    '불가능'이라고만 말하면 그것도 사실이 아니다. 이름만 알려주고
    선택은 사용자가 한다.

    **available 인 공급자만 이름을 댄다.** 검증 중(verifying)인 곳으로
    안내하면 사용자는 그쪽에서도 같은 거절을 받는다. 안 되는 길을
    권하는 것은 아무 말도 안 하는 것보다 나쁘다.
    """
    for provider_id in public:
        if provider_id == exclude:
            continue
        if state_of(provider_id) == "available":
            return provider_id
    return None


def _denied(provider: str, capability: str, state: str, state_of,
            public: tuple[str, ...], *, strict: bool) -> EvidenceRouterError:
    tail = (f" source={provider} 는 strict 모드라 다른 공급자로 전환하지 "
            "않습니다." if strict else "")
    if state == "not_configured":
        return EvidenceRouterError(
            "not_configured",
            f"{provider} 가 연결되어 있지 않습니다. 증권사 연결 후 다시 "
            f"시도하세요.{tail}",
            error_code="provider_not_configured", provider=provider,
            capability=capability)

    alternative = _alternative_for(capability, provider, state_of, public)
    hint = (f" 이미 연결된 {alternative} 는 이 항목을 제공합니다. "
            f"source=\"{alternative}\" 로 요청하면 받을 수 있습니다."
            if alternative else "")
    if state == "verifying":
        return EvidenceRouterError(
            "unsupported",
            f"{provider} 의 {capability} 는 데이터 계약 검증 중이라 아직 "
            f"제공하지 않습니다. API 키 문제가 아닙니다.{hint}{tail}",
            error_code="unverified_by_selected_provider", provider=provider,
            capability=capability, alternative=alternative)
    if state == "unverified":
        return EvidenceRouterError(
            "unsupported",
            f"{provider} 의 {capability} 는 응답은 오지만 데이터를 확인하지 "
            f"못했습니다. 된다고도 안 된다고도 말하지 않습니다.{hint}{tail}",
            error_code="unverified_by_selected_provider", provider=provider,
            capability=capability, alternative=alternative)
    return EvidenceRouterError(
        "unsupported",
        f"{provider} 는 {capability} 를 제공하지 않습니다. 키나 연결 "
        f"문제가 아닙니다.{hint}{tail}",
        error_code="unsupported_by_selected_provider", provider=provider,
        capability=capability, alternative=alternative)


def assert_same_generation(provider: str, before: int, after: int) -> None:
    """요청 도중 연결이 바뀌었으면 결과를 버린다.

    사용자가 Manager 에서 키를 바꾸거나 주 사용 증권사를 옮기는 순간
    진행 중이던 요청의 절반은 옛 연결에서 온 것이다. 두 시점을 섞어
    돌려주면 사용자는 어느 연결의 숫자인지 알 수 없다. 섞느니 버린다.
    """
    if before != after:
        raise EvidenceRouterError(
            "provider_unavailable",
            f"조회 중 {provider} 연결이 변경되어 결과를 반환하지 "
            "않습니다. 다시 요청해 주세요.",
            error_code="provider_changed_during_request", provider=provider)


def resolve_evidence_source(
    *,
    capability: str,
    requested_source: str,
    capabilities: dict,
    primary_provider: str | None = None,
    public_providers: "tuple[str, ...] | None" = None,
    release_override: bool = False,
) -> EvidenceResolution:
    """공급자 하나를 고정해 돌려준다. 고를 수 없으면 EvidenceRouterError.

    capabilities 는 {provider_id: 연결 능력 dict} 다 (1.0 의
    `provider_capabilities_v2` 결과). auto 는 **주 사용 증권사 하나만**
    본다. 추가로 연결된 다른 증권사는 명시 source 로만 쓸 수 있다.

    release_override 는 테스트에서 출시 게이트를 대신 통과시키기 위한
    것이다. 운영 경로에서 쓰지 않는다.
    """
    if capability not in EVIDENCE_CAPABILITIES:
        raise EvidenceRouterError(
            "entity_not_found", f"지원하지 않는 증거 종류: {capability}",
            error_code="unknown_capability", capability=capability)

    public = _public_ids(public_providers)

    def _state(provider: str) -> str:
        state = capability_state(provider, capability,
                                 capabilities.get(provider))
        if release_override and state == "verifying":
            return "available"
        return state

    if requested_source in public:
        state = _state(requested_source)
        if state != "available":
            raise _denied(requested_source, capability, state, _state,
                          public, strict=True)
        return EvidenceResolution(
            capability=capability, requested_source=requested_source,
            selected_provider=requested_source,
            selection_reason=f"explicit_source_{requested_source}_strict",
            primary_provider=primary_provider)

    if requested_source != "auto":
        # 숨김 공급자(고객 모드의 토스)와 시세 전용 공급원(naver·yahoo)이
        # 여기로 온다. 둘 다 이 요청에 쓸 수 없다는 사실은 같다.
        raise EvidenceRouterError(
            "not_configured",
            f"{requested_source} 로는 상세 수급을 받을 수 없습니다. "
            "증권사(한국투자증권·키움증권)를 연결해 주세요.",
            error_code="provider_not_configured",
            provider=requested_source, capability=capability)

    if primary_provider is None:
        raise EvidenceRouterError(
            "not_configured",
            "연결된 증권사가 없습니다. 상세 수급은 증권사 Open API "
            "연결이 필요합니다.",
            error_code="provider_not_configured", capability=capability)

    state = _state(primary_provider)
    if state != "available":
        raise _denied(primary_provider, capability, state, _state,
                      public, strict=False)
    return EvidenceResolution(
        capability=capability, requested_source="auto",
        selected_provider=primary_provider,
        selection_reason="auto_primary_provider",
        primary_provider=primary_provider)
