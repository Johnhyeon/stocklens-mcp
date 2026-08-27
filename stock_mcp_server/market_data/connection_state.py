"""비밀 없는 연결 상태 파일 (broker_state.json).

Manager CLI 는 실행 중인 MCP 프로세스의 메모리 토큰을 직접 지울 수 없다.
대신 이 파일의 connection_generation 을 올리고, MCP 프로세스는 각 KIS 호출
전에 generation 을 확인해 달라졌으면 토큰·메모리 캐시를 폐기하고 다시 읽는다.

이 파일에는 어떤 비밀값도 쓰지 않는다. 손상되면 legacy 모드로 안전하게
동작한다 (KIS 호출 없음).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_FILENAME = "broker_state.json"

DATA_SOURCE_MODES = ("auto", "broker_first", "legacy")

DEFAULT_STATE: dict = {
    "connection_generation": 0,
    "active_provider": None,
    "active_profile": None,
    "data_source_mode": "legacy",
}


def _home(home: Path | str | None = None) -> Path:
    if home is not None:
        return Path(home)
    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def state_path(home: Path | str | None = None) -> Path:
    return _home(home) / STATE_FILENAME


def _sanitize(raw: object) -> dict:
    """형식이 깨진 상태 파일은 legacy 기본값으로 안전하게 내린다."""
    if not isinstance(raw, dict):
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    gen = raw.get("connection_generation")
    mode = raw.get("data_source_mode")
    provider = raw.get("active_provider")
    profile = raw.get("active_profile")
    if not isinstance(gen, int) or gen < 0:
        return dict(DEFAULT_STATE)
    if mode not in DATA_SOURCE_MODES:
        return dict(DEFAULT_STATE)
    if provider is not None and not isinstance(provider, str):
        return dict(DEFAULT_STATE)
    if profile is not None and not isinstance(profile, str):
        return dict(DEFAULT_STATE)
    state.update({
        "connection_generation": gen,
        "active_provider": provider,
        "active_profile": profile,
        "data_source_mode": mode,
    })
    # 검증 결과 등 비밀 아닌 부가 키는 보존한다.
    for key, value in raw.items():
        if key not in state:
            state[key] = value
    return state


def load_state(home: Path | str | None = None) -> dict:
    path = state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)
    return _sanitize(raw)


def save_state(state: dict, home: Path | str | None = None) -> None:
    """임시 파일에 쓴 뒤 os.replace 로 원자 교체한다."""
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".broker_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def bump_generation(state: dict) -> dict:
    updated = dict(state)
    updated["connection_generation"] = int(state.get(
        "connection_generation", 0)) + 1
    return updated


# ---------------------------------------------------------------------------
# 상태 v2 (1.0 멀티 증권사, 설계 10절)
#
# - 공급자별 독립 상태와 generation
# - 전역 primary_provider 하나 (첫 연결이 primary, 추가 연결은 유지)
# - 엄격한 allowlist sanitizer: 모르는 필드·공급자·프로필은 버린다
# - 손상 상태는 legacy 안전 모드 (증권사 호출 없음)
# - 비밀값·토큰·요청/응답 원문은 어떤 필드에도 쓰지 않는다
# ---------------------------------------------------------------------------

from stock_mcp_server.market_data.provider_registry import registry  # noqa: E402

STATE_VERSION = 2

# v1 고정 keyring 슬롯에서 migration 된 프로필의 credential 참조값.
LEGACY_CREDENTIAL_REF = "legacy"

LIFECYCLES = ("connected", "disabled_pending_cleanup")

# 능력 키 allowlist. 1.0 은 봉 데이터 능력만 다룬다.
_CAPABILITY_KEYS = ("auth", "kr_intraday", "us_intraday",
                    "kr_daily", "us_daily")

# 비밀 없는 pending 작업 레코드에 허용되는 키 (Task 5 credential 트랜잭션).
_PENDING_KEYS = ("op", "provider", "profile", "credential_ref",
                 "retired_ref", "created_at")

DEFAULT_STATE_V2: dict = {
    "state_version": STATE_VERSION,
    "routing_generation": 0,
    "primary_provider": None,
    "data_source_mode": "legacy",
    "providers": {},
    "pending_operations": [],
}


def _default_v2() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE_V2))


def _clean_capabilities(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in _CAPABILITY_KEYS and isinstance(value, str)
        and len(value) <= 64
    }


def _clean_profile_record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ref = raw.get("credential_ref")
    verified = raw.get("verified")
    verified_at = raw.get("verified_at")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 128):
        return None
    if not isinstance(verified, bool):
        verified = False
    if verified_at is not None and not isinstance(verified_at, str):
        verified_at = None
    return {
        "credential_ref": ref,
        "verified": verified,
        "verified_at": verified_at,
        "capabilities": _clean_capabilities(raw.get("capabilities")),
    }


def _clean_provider_record(provider_id: str, raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        descriptor = registry.require(provider_id)
    except Exception:
        return None
    generation = raw.get("generation")
    if not isinstance(generation, int) or generation < 0:
        return None
    lifecycle = raw.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        return None
    profiles_raw = raw.get("profiles")
    profiles: dict = {}
    if isinstance(profiles_raw, dict):
        for name, record in profiles_raw.items():
            if name not in descriptor.supported_profiles:
                continue
            cleaned = _clean_profile_record(record)
            if cleaned is not None:
                profiles[name] = cleaned
    active_profile = raw.get("active_profile")
    if active_profile is not None and active_profile not in profiles:
        active_profile = None
    return {
        "generation": generation,
        "lifecycle": lifecycle,
        "active_profile": active_profile,
        "profiles": profiles,
    }


def _clean_pending(raw: object) -> list:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        record = {
            key: value
            for key, value in entry.items()
            if key in _PENDING_KEYS and isinstance(value, str)
            and len(value) <= 256
        }
        if record.get("op"):
            cleaned.append(record)
    return cleaned


def sanitize_v2(raw: object) -> dict:
    """v2 상태를 allowlist 로 정제한다. 형식이 깨지면 legacy 안전 모드."""
    if not isinstance(raw, dict):
        return _default_v2()
    routing = raw.get("routing_generation")
    mode = raw.get("data_source_mode")
    if not isinstance(routing, int) or routing < 0:
        return _default_v2()
    if mode not in DATA_SOURCE_MODES:
        return _default_v2()

    providers_raw = raw.get("providers")
    providers: dict = {}
    if isinstance(providers_raw, dict):
        for provider_id, record in providers_raw.items():
            cleaned = _clean_provider_record(provider_id, record)
            if cleaned is not None:
                providers[provider_id] = cleaned

    primary = raw.get("primary_provider")
    if primary is not None and primary not in providers:
        primary = None

    return {
        "state_version": STATE_VERSION,
        "routing_generation": routing,
        "primary_provider": primary,
        "data_source_mode": mode,
        "providers": providers,
        "pending_operations": _clean_pending(raw.get("pending_operations")),
    }


def migrate_v1(raw: object) -> dict:
    """v1(KIS 단일) 상태를 v2 로 순수 변환한다. 비밀 필드는 옮기지 않는다."""
    v1 = _sanitize(raw)
    state = _default_v2()
    generation = v1["connection_generation"]
    state["routing_generation"] = generation
    state["data_source_mode"] = v1["data_source_mode"]

    if v1["active_provider"] != "kis":
        # KIS 외 값은 v1 에 존재할 수 없었다. 알 수 없는 공급자는 버린다.
        return state

    capability_results = v1.get("capability_results")
    if not isinstance(capability_results, dict):
        capability_results = {}

    profiles: dict = {}
    profile_names = list(capability_results.keys())
    active_profile = v1["active_profile"]
    if active_profile and active_profile not in profile_names:
        profile_names.append(active_profile)
    supported = registry.require("kis").supported_profiles
    for name in profile_names:
        if name not in supported:
            continue
        caps = _clean_capabilities(capability_results.get(name))
        # 0.9 는 인증 게이트(verify_and_save) 통과 시에만 상태를 썼고,
        # capability_results 에 auth 키가 없었다. 기록의 존재 자체가
        # 인증 완료의 근거다 - auth 부재를 미검증으로 읽으면 업그레이드가
        # 기존 구매자의 KIS 라우팅을 꺼버린다 (2026-08-27 실사고).
        caps.setdefault("auth", "ok")
        profiles[name] = {
            "credential_ref": LEGACY_CREDENTIAL_REF,
            "verified": True,
            "verified_at": None,
            "capabilities": caps,
        }
    if not profiles:
        return state

    if active_profile not in profiles:
        active_profile = None
    state["primary_provider"] = "kis"
    state["providers"]["kis"] = {
        "generation": generation,
        "lifecycle": "connected",
        "active_profile": active_profile,
        "profiles": profiles,
    }
    return state


def load_state_v2(home: Path | str | None = None) -> dict:
    path = state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_v2()
    if isinstance(raw, dict) and raw.get("state_version") == STATE_VERSION:
        return sanitize_v2(raw)
    return migrate_v1(raw)


def save_state_v2(state: dict, home: Path | str | None = None) -> None:
    """정제를 통과한 상태만 원자적으로 저장한다."""
    save_state(sanitize_v2(state), home)


def connect_profile_v2(
    state: dict,
    provider: str,
    profile: str,
    *,
    credential_ref: str,
    verified: bool,
    verified_at: str | None,
    capabilities: dict,
) -> dict:
    """프로필 연결을 반영한 새 상태를 돌려준다 (순수 함수).

    첫 연결 공급자만 primary 가 된다. 추가 연결은 primary 를 바꾸지
    않는다 (변경은 set_primary_v2 라는 별도 action 뿐이다).
    """
    descriptor = registry.require(provider)
    if profile not in descriptor.supported_profiles:
        raise ValueError(f"{provider}가 지원하지 않는 프로필: {profile}")

    updated = sanitize_v2(state)
    record = updated["providers"].get(provider) or {
        "generation": 0,
        "lifecycle": "connected",
        "active_profile": None,
        "profiles": {},
    }
    record = json.loads(json.dumps(record))
    record["generation"] = int(record["generation"]) + 1
    record["lifecycle"] = "connected"
    record["active_profile"] = profile
    record["profiles"][profile] = {
        "credential_ref": credential_ref,
        "verified": bool(verified),
        "verified_at": verified_at,
        "capabilities": _clean_capabilities(capabilities),
    }
    updated["providers"][provider] = record
    updated["routing_generation"] = int(updated["routing_generation"]) + 1
    if updated["primary_provider"] is None:
        updated["primary_provider"] = provider
    return updated


def provider_capabilities_v2(state: dict, provider: str) -> dict:
    """라우터가 쓰는 능력 dict. disable 된 provider 는 connected=False.

    disable 우선 해제(11.4)의 핵심: lifecycle 이 connected 가 아니면
    비밀 삭제가 끝나지 않았어도 이 provider 는 요청에 사용되지 않는다.

    능력 활성화는 두 게이트를 모두 통과해야 한다:
    1. endpoint_available - 연결 시험이 available 로 기록 (상태 파일)
    2. release_verified - 실계좌 UAT·출시 게이트 통과 (코드 고정 표)
    """
    from stock_mcp_server.market_data.provider_registry import (
        is_release_verified,
    )

    cleaned = sanitize_v2(state)
    record = cleaned["providers"].get(provider)
    empty = {"connected": False, "kr_intraday": False, "us_intraday": False,
             "kr_daily": False, "us_daily": False,
             "kr_intraday_state": "unknown", "us_intraday_state": "unknown"}
    if record is None or record["lifecycle"] != "connected":
        return empty
    active = record.get("active_profile")
    profile = record["profiles"].get(active) if active else None
    if profile is None or not profile.get("verified"):
        return empty
    caps = profile.get("capabilities") or {}

    def _on(name: str) -> bool:
        return caps.get(name) == "available" and \
            is_release_verified(provider, name)

    def _state(name: str) -> str:
        """라우터 오류 구분용 세부 상태.

        available   두 게이트 모두 통과 (라우팅 가능)
        verifying   연결 시험은 통과했으나 출시 검증 전
        unsupported 이 provider 가 해당 시장을 지원하지 않음/계약 불일치
        unknown     연결 시험 판정 없음
        """
        value = caps.get(name)
        if value == "available":
            return "available" if is_release_verified(provider, name) \
                else "verifying"
        if value == "unavailable":
            return "unsupported"
        return "unknown"

    return {
        "connected": True,
        "kr_intraday": _on("kr_intraday"),
        "us_intraday": _on("us_intraday"),
        "kr_daily": _on("kr_daily"),
        "us_daily": _on("us_daily"),
        "kr_intraday_state": _state("kr_intraday"),
        "us_intraday_state": _state("us_intraday"),
    }


def set_primary_v2(state: dict, provider: str) -> dict:
    """주 사용 증권사 변경. 검증된 연결이 있는 공급자만 허용한다."""
    updated = sanitize_v2(state)
    record = updated["providers"].get(provider)
    if record is None or record["lifecycle"] != "connected":
        raise ValueError(f"연결되지 않은 공급자는 primary가 될 수 없습니다: "
                         f"{provider}")
    active = record.get("active_profile")
    profile = record["profiles"].get(active) if active else None
    if profile is None or not profile.get("verified"):
        raise ValueError(f"검증된 프로필이 없는 공급자는 primary가 될 수 "
                         f"없습니다: {provider}")
    updated["primary_provider"] = provider
    updated["routing_generation"] = int(updated["routing_generation"]) + 1
    return updated
