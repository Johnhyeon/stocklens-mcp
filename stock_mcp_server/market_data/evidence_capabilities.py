"""능력 공개 투영 (1.1 Task 16).

Manager·CLI·doctor 가 같은 것을 보게 하는 단일 계산 지점. 세 곳에서 각자
계산하면 같은 연결을 두고 서로 다른 말을 하게 된다.

**공개 도구를 추가하지 않는다.** capability 안내는 기존
`describe_providers`·`status`·`doctor` 응답에 additive 하게 얹는다
(대표 결정 2026-08-28).

두 가지 모양을 준다:
- `evidence_groups`  Manager 용 묶음 상태. 사용자에게 능력 13개를 나열해
  봤자 판단이 서지 않는다. "상세 수급 되나?" 수준으로 답한다.
- `evidence_capabilities`  진단·AI 용 개별 상태. 왜 안 되는지까지 담는다.

둘 다 공개 허용 목록 투영이다. 내부 탐침 결과·응답 본문·자격 증명은
어느 쪽에도 들어가지 않는다.
"""

from __future__ import annotations

from stock_mcp_server.market_data.evidence_router import (
    EVIDENCE_CAPABILITIES,
    capability_state,
)

EVIDENCE_CONTRACT_VERSION = 1

# 묶음 정의. basic_market_data 는 1.0 분봉 능력이고 나머지가 1.1 이다.
# 1.0 능력을 여기 포함하는 이유는 Manager 가 한 자리에서 "이 증권사로
# 무엇이 되는가"를 답해야 하기 때문이다.
GROUPS: dict[str, tuple[str, ...]] = {
    "basic_market_data": ("kr_intraday", "us_intraday"),
    "detailed_flow": ("kr_investor_flow",),
    "supply_pressure": tuple(c for c in EVIDENCE_CAPABILITIES
                             if c != "kr_investor_flow"),
}

# 묶음 상태. 개별 상태를 그대로 올리지 않고 사용자가 판단할 수 있는
# 말로 줄인다. 다만 '되는 것처럼' 줄이지는 않는다.
AVAILABLE = "available"
PARTIAL = "partial"
CHECKING = "checking"
UNSUPPORTED = "unsupported"
NOT_CONFIGURED = "not_configured"


def _bar_state(connection_caps: dict | None, capability: str) -> str:
    """1.0 분봉 능력의 상태. 상태 파일이 이미 판정해 둔 값을 쓴다."""
    if not connection_caps or not connection_caps.get("connected"):
        return NOT_CONFIGURED
    return str(connection_caps.get(f"{capability}_state") or "unknown")


def _roll_up(states: list[str]) -> str:
    """묶음 하나의 상태.

    '하나라도 되면 available' 로 올리지 않는다. 여섯 종류 중 하나만
    되는데 available 이라고 하면 사용자는 나머지 다섯도 될 거라 읽는다.
    """
    if not states:
        return UNSUPPORTED
    if all(s == NOT_CONFIGURED for s in states):
        return NOT_CONFIGURED
    served = [s for s in states if s == "available"]
    if len(served) == len(states):
        return AVAILABLE
    if served:
        return PARTIAL
    # 아직 아무것도 못 주지만 검증만 끝나면 열릴 것이 있는가.
    if any(s in ("verifying", "unverified", "unknown") for s in states):
        return CHECKING
    return UNSUPPORTED


def capability_states(provider_id: str,
                      connection_caps: dict | None) -> dict[str, str]:
    """능력 이름 -> 상태. 1.0 분봉과 1.1 증거를 한 표에 담는다."""
    states = {name: _bar_state(connection_caps, name)
              for name in GROUPS["basic_market_data"]}
    for name in EVIDENCE_CAPABILITIES:
        states[name] = capability_state(provider_id, name, connection_caps)
    return states


def evidence_projection(provider_id: str,
                        connection_caps: dict | None) -> dict:
    """status·describe_providers·doctor 에 얹는 additive 블록."""
    states = capability_states(provider_id, connection_caps)
    groups = {name: _roll_up([states[c] for c in members])
              for name, members in GROUPS.items()}
    return {
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_groups": groups,
        "evidence_capabilities": [
            {"capability": name, "state": states[name],
             "group": _group_of(name)}
            for name in sorted(states)
        ],
    }


def catalog_projection(provider_id: str) -> dict:
    """연결 전 카탈로그용. "이 증권사가 무엇을 제공하는가"에 답한다.

    `evidence_projection` 과 다른 질문이다. 저쪽은 "내 연결로 지금 되는가"
    이고 이쪽은 "이 증권사를 연결하면 무엇이 열리는가"다. 연결 화면에서
    증권사를 고르는 사람에게 필요한 것은 뒤쪽이다.

    1.0 분봉 능력은 상태 파일이 없으므로 출시 게이트만 본다. 그것이
    이 공급자에 대해 우리가 아는 전부다.
    """
    from stock_mcp_server.market_data.provider_registry import (
        is_release_verified,
    )

    connected = {"connected": True}
    states = {name: ("available" if is_release_verified(provider_id, name)
                     else "unsupported")
              for name in GROUPS["basic_market_data"]}
    for name in EVIDENCE_CAPABILITIES:
        states[name] = capability_state(provider_id, name, connected)
    return {
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_groups": {
            name: _roll_up([states[c] for c in members])
            for name, members in GROUPS.items()},
    }


def _group_of(capability: str) -> str:
    for name, members in GROUPS.items():
        if capability in members:
            return name
    return "other"
