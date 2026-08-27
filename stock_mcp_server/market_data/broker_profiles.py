"""broker 자격 증명 프로필 저장소.

- App Key·App Secret 은 운영체제 자격 증명 저장소(keyring)에 프로필당
  entry 하나(JSON payload)로 저장한다. 반쪽 저장 상태를 만들지 않는다.
- 상태 파일(broker_state.json)에는 비밀값을 절대 쓰지 않는다.
- 저장·전환·해제 때마다 connection_generation 을 올린다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from stock_mcp_server.market_data.connection_state import (
    DATA_SOURCE_MODES,
    bump_generation,
    load_state,
    save_state,
)

PROFILES = ("real", "demo")

_SERVICE_PREFIX = "stocklens-broker"


@dataclass(frozen=True)
class BrokerCredentials:
    """비밀값 운반체. repr/str 에 원문·길이·일부 문자를 노출하지 않는다."""

    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.app_key or not self.app_secret:
            raise ValueError("app_key와 app_secret이 모두 필요합니다")

    def __repr__(self) -> str:  # noqa: D105
        return "BrokerCredentials(app_key=***, app_secret=***)"

    __str__ = __repr__


class BrokerProfileStore:
    def __init__(
        self,
        provider: str = "kis",
        keyring_module=None,
        home: Path | str | None = None,
    ) -> None:
        if keyring_module is None:
            import keyring as keyring_module  # noqa: PLC0415
        self.provider = provider
        self._keyring = keyring_module
        self._home = home

    @property
    def home(self) -> Path | str | None:
        """이 store 가 보는 StockLens 홈. 캐시 등 부속 자원이 같은 홈을 쓴다."""
        return self._home

    # --- keyring 좌표 ---

    def _service(self) -> str:
        return f"{_SERVICE_PREFIX}-{self.provider}"

    def _username(self, profile: str) -> str:
        return f"{self.provider}:{profile}"

    def _check_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError(f"지원하지 않는 프로필: {profile} (지원: {PROFILES})")

    # --- 조회 ---

    def load_profile(self, profile: str) -> BrokerCredentials | None:
        self._check_profile(profile)
        raw = self._keyring.get_password(self._service(), self._username(profile))
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return BrokerCredentials(
                app_key=parsed["app_key"], app_secret=parsed["app_secret"])
        except (ValueError, KeyError, TypeError):
            # 손상된 entry. 비밀 원문을 오류에 싣지 않는다.
            return None

    def has_profile(self, profile: str) -> bool:
        return self.load_profile(profile) is not None

    def status(self) -> dict:
        """비밀 없는 상태 요약. doctor·CLI 응답에 그대로 실어도 안전하다."""
        state = load_state(self._home)
        return {
            "provider": self.provider,
            "connection_generation": state["connection_generation"],
            "active_provider": state["active_provider"],
            "active_profile": state["active_profile"],
            "data_source_mode": state["data_source_mode"],
            "profiles": {
                p: {"configured": self.has_profile(p)} for p in PROFILES
            },
        }

    # --- 변경 ---

    def save_profile(self, profile: str, credentials: BrokerCredentials) -> None:
        """keyring 저장이 성공했을 때만 상태 파일과 generation 을 갱신한다."""
        self._check_profile(profile)
        payload = json.dumps({
            "app_key": credentials.app_key,
            "app_secret": credentials.app_secret,
        })
        self._keyring.set_password(
            self._service(), self._username(profile), payload)

        state = load_state(self._home)
        state["active_provider"] = self.provider
        state["active_profile"] = profile
        save_state(bump_generation(state), self._home)

    def switch_profile(self, profile: str) -> None:
        self._check_profile(profile)
        if not self.has_profile(profile):
            raise ValueError(f"프로필 {profile}이(가) 설정되어 있지 않습니다")
        state = load_state(self._home)
        state["active_provider"] = self.provider
        state["active_profile"] = profile
        save_state(bump_generation(state), self._home)

    def disconnect_profile(self, profile: str) -> None:
        self._check_profile(profile)
        try:
            self._keyring.delete_password(
                self._service(), self._username(profile))
        except Exception:
            # 이미 없는 entry 는 idempotent 하게 통과한다.
            pass
        state = load_state(self._home)
        if state["active_profile"] == profile and \
                state["active_provider"] == self.provider:
            state["active_profile"] = None
        save_state(bump_generation(state), self._home)

    def disconnect_provider(self) -> None:
        """real·demo 자격 증명과 active pointer 를 모두 제거한다. idempotent."""
        for profile in PROFILES:
            try:
                self._keyring.delete_password(
                    self._service(), self._username(profile))
            except Exception:
                pass
        state = load_state(self._home)
        if state["active_provider"] == self.provider:
            state["active_provider"] = None
            state["active_profile"] = None
        save_state(bump_generation(state), self._home)

    def set_data_source_mode(self, mode: str) -> None:
        if mode not in DATA_SOURCE_MODES:
            raise ValueError(
                f"지원하지 않는 data_source_mode: {mode} (지원: {DATA_SOURCE_MODES})")
        state = load_state(self._home)
        state["data_source_mode"] = mode
        save_state(bump_generation(state), self._home)
