"""버전 keyring 슬롯 기반 자격 증명 저장소 (1.0 설계 11.3).

새 키는 기존 활성 키를 덮어쓰지 않는다. 순서:

1. 비밀 없는 pending 기록을 상태 파일에 남긴다
2. 무작위 credential_ref 슬롯에 새 키를 저장한다
3. 상태 pointer 를 한 번의 원자 쓰기로 교체한다 (pending 기록 제거 포함)
4. 이전 슬롯을 삭제하고, 실패하면 감추지 않고 보고한다

어느 단계에서 중단돼도 기존 활성 키가 유지되고, orphan 슬롯은
recover_pending() 이 삭제한다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from stock_mcp_server.market_data.broker_profiles import (
    KeychainUnavailableError,
)
from stock_mcp_server.market_data.connection_state import (
    LEGACY_CREDENTIAL_REF,
    connect_profile_v2,
    load_state_v2,
    save_state_v2,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_SERVICE_PREFIX = "stocklens-broker"


@dataclass(frozen=True)
class PendingCredential:
    """stage 가 돌려주는 비밀 없는 트랜잭션 핸들."""

    provider: str
    profile: str
    credential_ref: str
    retired_ref: str | None = field(default=None)


@dataclass(frozen=True)
class CleanupReport:
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    # 자격 증명 삭제와 별개로 보고한다 (요구사항 12: 결과를 섞지 않는다).
    tokens_removed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class CredentialStore:
    def __init__(self, keyring_module=None,
                 home: Path | str | None = None,
                 token_store=None) -> None:
        if keyring_module is None:
            import keyring as keyring_module  # noqa: PLC0415
        self._keyring = keyring_module
        self._home = home
        # 연결을 끊으면 그 연결로 받은 토큰도 함께 지운다. 남겨두면
        # 지운 줄 아는 자격이 보안 저장소에 계속 살아 있게 된다.
        self._token_store = token_store

    @property
    def home(self) -> Path | str | None:
        return self._home

    # --- keyring 좌표 ---

    def _service(self, provider: str) -> str:
        return f"{_SERVICE_PREFIX}-{provider}"

    def _username(self, provider: str, profile: str, ref: str) -> str:
        if ref == LEGACY_CREDENTIAL_REF:
            return f"{provider}:{profile}"
        return f"{provider}:{profile}:{ref}"

    # --- keyring 원자 조작 (비밀 없는 오류) ---

    def _get_raw(self, provider: str, username: str) -> str | None:
        try:
            return self._keyring.get_password(
                self._service(provider), username)
        except Exception as exc:  # noqa: BLE001
            raise KeychainUnavailableError(
                f"자격 증명 저장소를 읽을 수 없습니다: {type(exc).__name__}"
            ) from None

    def _set_raw(self, provider: str, username: str, payload: str) -> None:
        try:
            self._keyring.set_password(
                self._service(provider), username, payload)
        except Exception as exc:  # noqa: BLE001
            raise KeychainUnavailableError(
                f"자격 증명을 저장할 수 없습니다: {type(exc).__name__}"
            ) from None

    def _delete_slot(self, provider: str, username: str) -> str:
        """'removed' | 'missing' | 'failed'. 예외를 삼키지 않고 상태로 준다."""
        try:
            existing = self._keyring.get_password(
                self._service(provider), username)
        except Exception:  # noqa: BLE001
            return "failed"
        if existing is None:
            return "missing"
        try:
            self._keyring.delete_password(self._service(provider), username)
        except Exception:  # noqa: BLE001
            return "failed"
        return "removed"

    # --- 조회 ---

    def load_active(self, provider: str,
                    profile: str) -> SecretPayload | None:
        descriptor = registry.require(provider)
        if profile not in descriptor.supported_profiles:
            raise ValueError(f"{provider}가 지원하지 않는 프로필: {profile}")
        state = load_state_v2(self._home)
        record = (state["providers"].get(provider) or {}).get(
            "profiles", {}).get(profile)
        if record is None or record.get("credential_ref") is None:
            return None
        username = self._username(
            provider, profile, record["credential_ref"])
        raw = self._get_raw(provider, username)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return SecretPayload.from_schema(
                descriptor.credential_schema, parsed)
        except Exception:  # noqa: BLE001
            # 손상 entry. 비밀 원문을 오류에 싣지 않는다.
            return None

    # --- 트랜잭션 ---

    def stage(self, provider: str, profile: str,
              secrets: SecretPayload) -> PendingCredential:
        descriptor = registry.require(provider)
        if profile not in descriptor.supported_profiles:
            raise ValueError(f"{provider}가 지원하지 않는 프로필: {profile}")
        # schema 검증 (필드 불일치 시 여기서 거부)
        payload = json.dumps({
            name: secrets.get(name)
            for name in (f.name for f in descriptor.credential_schema)
        })

        ref = uuid.uuid4().hex[:16]
        state = load_state_v2(self._home)
        current = (state["providers"].get(provider) or {}).get(
            "profiles", {}).get(profile)
        retired_ref = current.get("credential_ref") if current else None

        # 1) 비밀 없는 pending 기록을 먼저 남긴다 (crash 복구용).
        state["pending_operations"].append({
            "op": "stage_credential",
            "provider": provider,
            "profile": profile,
            "credential_ref": ref,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        save_state_v2(state, self._home)

        # 2) 새 슬롯 저장. 실패 시 pending 기록을 되돌린다.
        try:
            self._set_raw(
                provider, self._username(provider, profile, ref), payload)
        except BaseException:
            try:
                rollback = load_state_v2(self._home)
                rollback["pending_operations"] = [
                    op for op in rollback["pending_operations"]
                    if op.get("credential_ref") != ref
                ]
                save_state_v2(rollback, self._home)
            except Exception:  # noqa: BLE001
                pass
            raise

        return PendingCredential(
            provider=provider, profile=profile,
            credential_ref=ref, retired_ref=retired_ref)

    def commit(self, pending: PendingCredential,
               capabilities: dict) -> CommitResult:
        caps = dict(capabilities or {})
        state = load_state_v2(self._home)
        new_state = connect_profile_v2(
            state, pending.provider, pending.profile,
            credential_ref=pending.credential_ref,
            verified=caps.get("auth") == "ok",
            verified_at=datetime.now(timezone.utc).isoformat(),
            capabilities=caps,
        )
        # 3) pending 제거를 같은 원자 쓰기에 싣는다.
        new_state["pending_operations"] = [
            op for op in new_state["pending_operations"]
            if op.get("credential_ref") != pending.credential_ref
        ]
        try:
            save_state_v2(new_state, self._home)
        except BaseException:
            # pointer 교체 실패: 후보 슬롯을 정리하고 기존 활성 키 유지.
            self._delete_slot(
                pending.provider,
                self._username(pending.provider, pending.profile,
                               pending.credential_ref))
            raise

        # 4) 이전 슬롯 삭제. 실패는 보고하고 retire 기록으로 재시도한다.
        removed: list[str] = []
        failed: list[str] = []
        if pending.retired_ref and \
                pending.retired_ref != pending.credential_ref:
            username = self._username(
                pending.provider, pending.profile, pending.retired_ref)
            outcome = self._delete_slot(pending.provider, username)
            if outcome == "removed":
                removed.append(username)
            elif outcome == "failed":
                failed.append(username)
                try:
                    retry_state = load_state_v2(self._home)
                    retry_state["pending_operations"].append({
                        "op": "retire_slot",
                        "provider": pending.provider,
                        "profile": pending.profile,
                        "credential_ref": pending.retired_ref,
                        "created_at":
                            datetime.now(timezone.utc).isoformat(),
                    })
                    save_state_v2(retry_state, self._home)
                except Exception:  # noqa: BLE001
                    pass
        return CommitResult(
            committed=True, removed=tuple(removed), failed=tuple(failed))

    def recover_pending(self) -> CleanupReport:
        """중단된 트랜잭션의 orphan 슬롯을 정리한다."""
        state = load_state_v2(self._home)
        removed: list[str] = []
        failed: list[str] = []
        remaining: list[dict] = []
        for op in state["pending_operations"]:
            provider = op.get("provider")
            profile = op.get("profile")
            ref = op.get("credential_ref")
            if not provider or not profile or not ref:
                continue  # 형식이 깨진 기록은 버린다
            active = (state["providers"].get(provider) or {}).get(
                "profiles", {}).get(profile) or {}
            if op.get("op") == "stage_credential" and \
                    active.get("credential_ref") == ref:
                # 활성 pointer 가 이미 이 슬롯을 가리킨다. 기록만 버린다.
                continue
            username = self._username(provider, profile, ref)
            outcome = self._delete_slot(provider, username)
            if outcome == "failed":
                failed.append(username)
                remaining.append(op)
            else:
                if outcome == "removed":
                    removed.append(username)
        state["pending_operations"] = remaining
        save_state_v2(state, self._home)
        return CleanupReport(removed=tuple(removed), failed=tuple(failed))

    def _forget_token(self, provider: str, profile: str,
                      collected: list[str]) -> None:
        """이 연결로 받은 토큰을 보안 저장소에서 지운다.

        자격 증명을 지우기 **전에** 부른다. 지문이 자격 증명에서 나오므로
        먼저 지우면 어느 슬롯을 지워야 하는지 알 수 없다.
        """
        if self._token_store is None:
            return
        try:
            payload = self.load_active(provider, profile)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is None:
            return
        from stock_mcp_server.market_data.token_store import (
            credential_fingerprint,
        )

        values = {name: payload.get(name) for name in payload.field_names()}
        if self._token_store.delete(provider, profile,
                                    credential_fingerprint(values)):
            collected.append(f"{provider}:{profile}")

    # --- 연결 해제 (disable 우선, 설계 11.4) ---

    def disable_profile(self, provider: str, profile: str) -> None:
        """provider 를 요청에 쓰이지 않는 상태로 먼저 내린다.

        삭제 전에 호출한다. 이후 cleanup_disabled 가 실패해도 이 provider
        는 라우팅에 쓰이지 않는다.
        """
        registry.require(provider)
        state = load_state_v2(self._home)
        record = state["providers"].get(provider)
        if record is None:
            return
        record["lifecycle"] = "disabled_pending_cleanup"
        record["generation"] = int(record["generation"]) + 1
        if record.get("active_profile") == profile:
            record["active_profile"] = None
        if state["primary_provider"] == provider:
            # primary 는 다른 공급자로 자동 승격하지 않는다. 비운다.
            state["primary_provider"] = None
        state["routing_generation"] = int(state["routing_generation"]) + 1
        save_state_v2(state, self._home)

    def cleanup_disabled(self, provider: str,
                         profile: str | None) -> CleanupReport:
        """비활성 provider 의 슬롯과 상태 기록을 지운다.

        부분 실패는 전체 성공으로 보고하지 않는다. 실패한 슬롯은 남기고
        provider 는 disabled 상태를 유지해 다음 실행에서 재개한다.
        """
        descriptor = registry.require(provider)
        state = load_state_v2(self._home)
        record = state["providers"].get(provider)
        if record is None:
            return CleanupReport()

        targets = ([profile] if profile
                   else list(record["profiles"].keys()))
        removed: list[str] = []
        failed: list[str] = []
        tokens_removed: list[str] = []
        for name in targets:
            prof = record["profiles"].get(name)
            if prof is None:
                continue
            slot_failed = False
            self._forget_token(provider, name, tokens_removed)
            ref = prof.get("credential_ref")
            candidates = {self._username(provider, name, ref)} if ref else set()
            # v1 고정 슬롯도 함께 정리한다 (idempotent).
            if name in descriptor.supported_profiles:
                candidates.add(f"{provider}:{name}")
            for username in sorted(candidates):
                outcome = self._delete_slot(provider, username)
                if outcome == "removed":
                    removed.append(username)
                elif outcome == "failed":
                    failed.append(username)
                    slot_failed = True
            if not slot_failed:
                del record["profiles"][name]
                if record.get("active_profile") == name:
                    record["active_profile"] = None

        if failed:
            # 부분 삭제. 다음 실행에서 재개할 수 있게 비활성으로 남긴다.
            record["lifecycle"] = "disabled_pending_cleanup"
        if record["profiles"]:
            # 남은 프로필이 쓰던 토큰·클라이언트를 폐기시킨다.
            record["generation"] = int(record["generation"]) + 1
        else:
            del state["providers"][provider]
            if state["primary_provider"] == provider:
                state["primary_provider"] = None
        state["routing_generation"] = int(state["routing_generation"]) + 1
        save_state_v2(state, self._home)
        return CleanupReport(removed=tuple(removed), failed=tuple(failed),
                             tokens_removed=tuple(tokens_removed))
