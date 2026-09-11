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


class KeychainUnavailableError(Exception):
    """운영체제 자격 증명 저장소에 접근할 수 없다 (백엔드 부재·잠김·거부).

    이 오류가 나면 "저장 안 됨"이나 "삭제 완료"로 단정하지 않는다 -
    실제 상태를 모르는 것이다. 메시지에 비밀값을 싣지 않는다.
    """


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

    def _get_raw(self, profile: str) -> str | None:
        try:
            return self._keyring.get_password(
                self._service(), self._username(profile))
        except Exception as exc:  # noqa: BLE001
            raise KeychainUnavailableError(
                f"자격 증명 저장소를 읽을 수 없습니다: {type(exc).__name__}"
            ) from None

    def load_profile(self, profile: str) -> BrokerCredentials | None:
        self._check_profile(profile)
        raw = self._get_raw(profile)
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
            # 연결 시험이 남긴 시장별 능력 판정. Manager UI 표시용. 비밀 없음.
            "capability_results": state.get("capability_results") or {},
        }

    # --- 변경 ---

    def save_profile(
        self,
        profile: str,
        credentials: BrokerCredentials,
        capability_results: dict | None = None,
    ) -> None:
        """keyring 저장과 상태 파일 갱신을 한 덩어리로 다룬다.

        상태 파일 저장이 실패하면 keyring 을 원복한다 - 안 그러면 새 키가
        남은 채 오류가 보고되어 "실패하면 기존 프로필 유지" 약속이 깨진다
        (리뷰 지적, 2026-08-27).

        capability_results 도 여기서 같은 한 번의 쓰기에 싣는다 - 별도
        두 번째 쓰기로 두면 그 실패가 원복 범위 밖이 된다 (리뷰 재지적).
        """
        self._check_profile(profile)
        previous = self._get_raw(profile)
        payload = json.dumps({
            "app_key": credentials.app_key,
            "app_secret": credentials.app_secret,
        })
        self._keyring.set_password(
            self._service(), self._username(profile), payload)

        try:
            state = load_state(self._home)
            state["active_provider"] = self.provider
            state["active_profile"] = profile
            if capability_results is not None:
                merged = dict(state.get("capability_results") or {})
                merged[profile] = capability_results
                state["capability_results"] = merged
            save_state(bump_generation(state), self._home)
        except BaseException:
            # keyring 원복. 원복마저 실패하면 원래 오류를 우선 보고한다.
            try:
                if previous is None:
                    self._keyring.delete_password(
                        self._service(), self._username(profile))
                else:
                    self._keyring.set_password(
                        self._service(), self._username(profile), previous)
            except Exception:  # noqa: BLE001
                pass
            raise

    def switch_profile(self, profile: str) -> None:
        self._check_profile(profile)
        if not self.has_profile(profile):
            raise ValueError(f"프로필 {profile}이(가) 설정되어 있지 않습니다")
        state = load_state(self._home)
        state["active_provider"] = self.provider
        state["active_profile"] = profile
        save_state(bump_generation(state), self._home)

    def _delete_if_present(self, profile: str) -> None:
        """entry 가 있으면 지운다. 없으면 idempotent 통과.

        삭제가 거부되면 KeychainUnavailableError - 조용히 넘어가면
        자격 증명이 남았는데 "해제 완료"로 보고하게 된다.
        """
        if self._get_raw(profile) is None:
            return
        try:
            self._keyring.delete_password(
                self._service(), self._username(profile))
        except Exception as exc:  # noqa: BLE001
            raise KeychainUnavailableError(
                f"자격 증명을 삭제할 수 없습니다: {type(exc).__name__}"
            ) from None

    def disconnect_profile(self, profile: str) -> None:
        self._check_profile(profile)
        self._delete_if_present(profile)
        state = load_state(self._home)
        if state["active_profile"] == profile and \
                state["active_provider"] == self.provider:
            state["active_profile"] = None
        save_state(bump_generation(state), self._home)

    def disconnect_provider(self) -> None:
        """real·demo 자격 증명과 active pointer 를 모두 제거한다. idempotent.

        하나라도 지우지 못하면 KeychainUnavailableError 로 중단한다 -
        generation 을 올리지 않아 "해제됨"으로 보이지 않는다.
        """
        for profile in PROFILES:
            self._delete_if_present(profile)
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
