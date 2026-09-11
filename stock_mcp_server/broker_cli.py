"""stocklens-broker: 증권사 연결 관리 CLI.

Manager 가 이 명령을 `--json --non-interactive --stdin` 으로 호출한다.
비밀값은 stdin JSON 으로만 받는다. 명령행 인자, 로그, 오류, 응답에
비밀값을 싣지 않는다.

1.0: 공급자 목록·credential schema 는 Provider Registry 가 단일 출처다.
상태는 connection state v2, 자격 증명은 버전 keyring 슬롯을 쓴다.
응답은 JSON 문서 하나다. JSON 뒤에 사람용 텍스트를 덧붙이지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from stock_mcp_server.market_data.broker_profiles import (
    KeychainUnavailableError,
)
from stock_mcp_server.market_data.connection_state import (
    DATA_SOURCE_MODES,
    load_state_v2,
    save_state_v2,
    set_primary_v2,
)
from stock_mcp_server.market_data.credential_store import (
    CleanupReport,
    CredentialStore,
)
from stock_mcp_server.market_data.provider_registry import (
    UnknownProviderError,
    is_release_verified as _is_release_verified,
    registry,
)
from stock_mcp_server.market_data.secrets import (
    SecretPayload,
    SecretValidationError,
)

CONTRACT_VERSION = 1
EXPERIMENTAL_BROKER_ENV = "LEETKIT_ENABLE_EXPERIMENTAL_BROKERS"
EXPERIMENTAL_BROKERS = frozenset({"toss"})
HIDDEN_PROVIDER_CLEANUP_ACTIONS = frozenset({
    "disconnect_profile", "disconnect_provider", "recover_cleanup",
})


def _provider_is_public(provider: str) -> bool:
    return (
        provider not in EXPERIMENTAL_BROKERS
        or os.environ.get(EXPERIMENTAL_BROKER_ENV) == "1"
    )

ACTIONS = (
    "status",
    "describe_providers",
    "verify",
    "verify_and_save",
    "switch_profile",
    "disconnect_profile",
    "disconnect_provider",
    "set_data_source_mode",
    "set_primary_provider",
    "recover_cleanup",
)


class BrokerService:
    """CLI 액션이 쓰는 v2 상태·자격 증명 서비스."""

    def __init__(self, keyring_module=None,
                 home: Path | str | None = None) -> None:
        self.credentials = CredentialStore(
            keyring_module=keyring_module, home=home)
        self._home = home

    @property
    def home(self) -> Path | str | None:
        return self._home

    def state(self) -> dict:
        return load_state_v2(self._home)

    # --- 조회 ---

    def has_profile(self, provider: str, profile: str) -> bool:
        """keyring 기준 설정 여부. 상태 파일이 지워져도 슬롯을 찾는다."""
        state = self.state()
        record = (state["providers"].get(provider) or {}).get(
            "profiles", {}).get(profile) or {}
        usernames = []
        ref = record.get("credential_ref")
        if ref:
            usernames.append(
                self.credentials._username(provider, profile, ref))
        legacy = f"{provider}:{profile}"
        if legacy not in usernames:
            usernames.append(legacy)
        for username in usernames:
            if self.credentials._get_raw(provider, username):
                return True
        return False

    def status(self, provider: str) -> dict:
        """비밀 없는 상태 요약. keyring 불가 시 KeychainUnavailableError."""
        descriptor = registry.require(provider)
        state = self.state()
        profiles = {
            p: {"configured": self.has_profile(provider, p)}
            for p in descriptor.supported_profiles
        }
        return self._status_from_state(state, provider, profiles)

    def minimal_status(self, provider: str) -> dict:
        """상태 파일만으로 만드는 최소 상태. configured 를 단정하지 않는다."""
        return self._status_from_state(self.state(), provider, {})

    def _status_from_state(self, state: dict, provider: str,
                           profiles: dict) -> dict:
        record = state["providers"].get(provider) or {}
        capability_results = {
            name: dict(rec.get("capabilities") or {})
            for name, rec in record.get("profiles", {}).items()
        }
        primary = state["primary_provider"]
        # 고객 모드에서는 숨김 공급자 레코드를 상태 계약으로도 노출하지
        # 않는다 (2026-08-28 리뷰: 목록에서 숨겨도 status 로 다시 샜다).
        # 주 사용이 숨김 공급자로 남아 있으면 주 사용 없음으로 보고한다
        # - 그 공급자는 게이트가 닫혀 있어 auto 라우팅도 쓰지 않는다.
        # 정리(해제·복구)는 HIDDEN_PROVIDER_CLEANUP_ACTIONS 로 계속
        # 가능하므로 표시만 줄어들 뿐 기능이 막히지 않는다.
        if primary is not None and not _provider_is_public(primary):
            primary = None
        primary_record = state["providers"].get(primary) if primary else None
        return {
            "provider": provider,
            # v1 호환 필드. active_provider 는 주 사용 증권사를 뜻한다.
            "connection_generation": state["routing_generation"],
            "active_provider": primary,
            "active_profile": (primary_record or {}).get("active_profile"),
            "data_source_mode": state["data_source_mode"],
            "profiles": profiles,
            "capability_results": capability_results,
            # 1.0 추가 필드 (additive)
            "primary_provider": primary,
            "providers": {
                pid: {
                    "lifecycle": rec["lifecycle"],
                    "active_profile": rec["active_profile"],
                    "generation": rec["generation"],
                    "verified_profiles": sorted(
                        name for name, prec in rec["profiles"].items()
                        if prec.get("verified")),
                    # 연결 시험(available)과 별개인 출시 검증 상태.
                    # 자동 라우터는 둘 다 통과한 능력만 쓴다.
                    "release_verified": {
                        "kr_intraday": _is_release_verified(
                            pid, "kr_intraday"),
                        "us_intraday": _is_release_verified(
                            pid, "us_intraday"),
                    },
                }
                for pid, rec in state["providers"].items()
                if _provider_is_public(pid)
            },
        }

    # --- 변경 ---

    def save_verified(self, provider: str, profile: str,
                      payload: SecretPayload, verification: dict) -> None:
        """검증 통과한 자격 증명을 슬롯 트랜잭션으로 저장한다."""
        pending = self.credentials.stage(provider, profile, payload)
        self.credentials.commit(pending, verification)

    def switch_profile(self, provider: str, profile: str) -> None:
        state = self.state()
        record = state["providers"].get(provider)
        if record is None or profile not in record["profiles"]:
            raise ValueError(f"프로필 {profile}이(가) 설정되어 있지 않습니다")
        record["active_profile"] = profile
        record["generation"] = int(record["generation"]) + 1
        state["routing_generation"] = int(state["routing_generation"]) + 1
        save_state_v2(state, self._home)

    def set_data_source_mode(self, mode: str) -> None:
        if mode not in DATA_SOURCE_MODES:
            raise ValueError(f"지원하지 않는 data_source_mode: {mode}")
        state = self.state()
        state["data_source_mode"] = mode
        state["routing_generation"] = int(state["routing_generation"]) + 1
        save_state_v2(state, self._home)

    def set_primary(self, provider: str) -> None:
        save_state_v2(set_primary_v2(self.state(), provider), self._home)

    def disconnect_profile(self, provider: str,
                           profile: str) -> CleanupReport:
        return self.credentials.cleanup_disabled(provider, profile)

    def disconnect_provider(self, provider: str) -> CleanupReport:
        """disable 우선 해제. 실패해도 provider 는 요청에 쓰이지 않는다."""
        state = self.state()
        record = state["providers"].get(provider)
        active = (record or {}).get("active_profile")
        if record is not None:
            self.credentials.disable_profile(provider, active or "")
        return self.credentials.cleanup_disabled(provider, None)

    def recover(self) -> CleanupReport:
        """중단된 자격 증명 트랜잭션·미완 삭제를 재개한다."""
        pending_report = self.credentials.recover_pending()
        removed = list(pending_report.removed)
        failed = list(pending_report.failed)
        state = self.state()
        for provider, record in list(state["providers"].items()):
            if record["lifecycle"] == "disabled_pending_cleanup":
                report = self.credentials.cleanup_disabled(provider, None)
                removed.extend(report.removed)
                failed.extend(report.failed)
        return CleanupReport(removed=tuple(removed), failed=tuple(failed))


def _error(code: str, message: str) -> dict:
    return {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "error": {"code": code, "message": message},
    }


def _safe_status(service: BrokerService, provider: str) -> tuple[dict, bool]:
    """(status, unavailable). 커밋이 끝난 뒤의 상태 재조회 실패는 작업
    실패가 아니다 - keyring 을 못 읽으면 상태 파일 기반 최소 상태로
    대신한다."""
    try:
        return service.status(provider), False
    except Exception:  # noqa: BLE001
        return service.minimal_status(provider), True


def _ok(action: str, service: BrokerService, provider: str,
        extra: dict | None = None) -> dict:
    status, unavailable = _safe_status(service, provider)
    resp = {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "action": action,
        "status": status,
    }
    if unavailable:
        resp["status_unavailable"] = True
        resp["warnings"] = [
            "변경은 저장됐지만 상태 재조회에 실패했습니다. "
            "자격 증명 저장소를 읽을 수 없어 최소 상태만 반환합니다."]
    if extra:
        resp.update(extra)
    return resp


def handle_request(
    request: dict,
    *,
    service: BrokerService | None = None,
    verifier=None,
    cache=None,
) -> dict:
    """요청 하나를 처리한다. 계약 위반은 어떤 변경도 없이 실패한다."""
    if not isinstance(request, dict):
        return _error("invalid_request", "요청은 JSON 객체여야 합니다")

    if request.get("contract_version") != CONTRACT_VERSION:
        return _error(
            "unsupported_contract_version",
            f"지원하는 contract_version은 {CONTRACT_VERSION}입니다")

    action = request.get("action")
    if action not in ACTIONS:
        return _error("unknown_action", f"지원하지 않는 action입니다: {action}")

    provider = request.get("provider")
    try:
        descriptor = registry.require(provider)
    except UnknownProviderError:
        return _error(
            "invalid_request",
            f"지원하지 않는 provider입니다 (지원: {registry.ids()})")

    if not _provider_is_public(provider) and \
            action not in HIDDEN_PROVIDER_CLEANUP_ACTIONS:
        return _error(
            "provider_not_public",
            "고객용 버전에서 제공하지 않는 연결입니다")

    if service is None:
        service = _default_service()

    try:
        if action == "describe_providers":
            # 레지스트리 공개 계약. keyring 을 건드리지 않는다.
            return {
                "ok": True,
                "contract_version": CONTRACT_VERSION,
                "action": action,
                "providers": [
                    item for item in registry.describe_public()
                    if _provider_is_public(str(item.get("provider_id", "")))
                ],
            }

        if action == "status":
            # 순수 조회는 keyring 을 못 읽으면 답 자체가 없다 - 최소
            # 상태로 눙치지 않고 keychain_unavailable 로 보고한다.
            # (커밋이 있는 액션들은 _ok 가 커밋 성공을 보존한다.)
            return {
                "ok": True,
                "contract_version": CONTRACT_VERSION,
                "action": action,
                "status": service.status(provider),
            }

        if action == "switch_profile":
            profile = request.get("profile")
            if profile not in descriptor.supported_profiles:
                return _error("invalid_request", "profile이 올바르지 않습니다")
            try:
                service.switch_profile(provider, profile)
            except ValueError:
                return _error(
                    "profile_not_configured",
                    f"프로필 {profile}이(가) 설정되어 있지 않습니다")
            return _ok(action, service, provider)

        if action == "disconnect_profile":
            profile = request.get("profile")
            if profile not in descriptor.supported_profiles:
                return _error("invalid_request", "profile이 올바르지 않습니다")
            report = service.disconnect_profile(provider, profile)
            if report.failed:
                return _error(
                    "keychain_unavailable",
                    "자격 증명을 삭제할 수 없습니다. 프로필은 비활성 "
                    "상태로 남았고 다음 시도에서 정리를 재개합니다.")
            return _ok(action, service, provider)

        if action == "disconnect_provider":
            report = service.disconnect_provider(provider)
            if report.failed:
                # 부분 삭제를 성공으로 보고하지 않는다. provider 는 이미
                # 비활성이라 요청에 쓰이지 않는다 (disable 우선, 11.4).
                resp = _error(
                    "keychain_unavailable",
                    "자격 증명 일부를 삭제할 수 없습니다. 연결은 비활성 "
                    "상태이며 다음 시도에서 정리를 재개합니다.")
                resp["provider_disabled"] = True
                resp["cleanup_required"] = True
                resp["status"], _ = _safe_status(service, provider)
                return resp
            # 자격 증명 정리에 성공했을 때만 공급자 분봉 캐시를 지운다.
            if cache is None:
                from stock_mcp_server.market_data.provider_cache import (
                    ProviderCache,
                )
                cache = ProviderCache(home=service.home)
            try:
                cache.remove_provider(provider)
            except Exception as exc:  # noqa: BLE001
                # 캐시가 남았는데 ok 로 보고하면 Manager 의 완전 정리가
                # 성공으로 이어진다(리뷰 지적). 자격 증명은 지워졌다는
                # 사실과 함께 실패로 보고한다 - 재시도는 idempotent 하다.
                resp = _error(
                    "cache_cleanup_failed",
                    "자격 증명은 삭제됐지만 분봉 캐시 삭제에 실패했습니다. "
                    "다시 시도해주세요.")
                resp["credentials_removed"] = True
                resp["cache_removed"] = False
                resp["cache_error"] = type(exc).__name__
                resp["status"], _ = _safe_status(service, provider)
                return resp
            return _ok(action, service, provider,
                       extra={"cache_removed": True})

        if action == "set_data_source_mode":
            mode = request.get("mode")
            try:
                service.set_data_source_mode(mode)
            except ValueError:
                return _error(
                    "invalid_request", "data_source_mode가 올바르지 않습니다")
            return _ok(action, service, provider)

        if action == "set_primary_provider":
            try:
                service.set_primary(provider)
            except ValueError:
                return _error(
                    "provider_not_verified",
                    "검증된 연결이 있는 공급자만 주 사용 증권사로 지정할 "
                    "수 있습니다.")
            return _ok(action, service, provider)

        if action == "recover_cleanup":
            report = service.recover()
            return _ok(action, service, provider, extra={
                "recovered": {
                    "removed": list(report.removed),
                    "failed": list(report.failed),
                },
            })

        # verify / verify_and_save
        profile = request.get("profile")
        if profile not in descriptor.supported_profiles:
            return _error("invalid_request", "profile이 올바르지 않습니다")
        try:
            payload = SecretPayload.from_request(
                descriptor.credential_schema, request.get("credentials"))
        except SecretValidationError:
            fields = ", ".join(
                f"credentials.{f.name}" for f in descriptor.credential_schema)
            return _error("invalid_request", f"{fields}이(가) 필요합니다")

        if verifier is None:
            return _error(
                "verifier_unavailable",
                "연결 시험 기능이 아직 활성화되지 않았습니다")
        result = verifier(provider, profile, payload)
        if result is None:
            return _error(
                "verifier_unavailable",
                f"{descriptor.display_name} 연결 시험 기능이 아직 "
                "활성화되지 않았습니다")
        if result.get("auth") == "ip_not_allowed":
            # 공식 오류 코드로 확인된 허용 IP 차단. 키 문제가 아니다.
            return _error(
                "ip_not_allowed",
                "허용 IP 목록에 없는 IP에서 호출했습니다. 증권사 설정 "
                "화면에서 현재 PC의 IP를 허용 IP로 등록한 뒤 다시 "
                "시도하세요.")
        if result.get("auth") != "ok":
            return _error(
                "credential_invalid",
                "인증에 실패했습니다. 키를 다시 확인하세요.")
        if action == "verify":
            return _ok(action, service, provider,
                       extra={"verification": result})

        service.save_verified(provider, profile, payload, result)
        return _ok(action, service, provider,
                   extra={"verification": result})
    except KeychainUnavailableError as exc:
        # 실제 상태를 모른다. 저장 안 됨·삭제 완료로 단정하지 않는다.
        return _error("keychain_unavailable", str(exc))
    except Exception as exc:  # noqa: BLE001
        # 비밀값이 예외 문자열에 섞이지 않도록 형식명만 보고한다.
        return _error("internal_error", f"{type(exc).__name__}")


def make_cli_verifier(kis_verifier):
    """KisVerifier 하나만 감싼 verifier (KIS 전용 테스트 호환)."""
    return make_default_verifier(kis=kis_verifier)


def make_default_verifier(*, kis=None, kiwoom=None, toss=None):
    """세 공급자 검증기를 dispatch 하는 기본 verifier.

    verifier(provider, profile, payload) -> 검증 결과 dict | None.
    None 은 "이 공급자의 검증기가 없다"는 뜻이다.
    """
    import asyncio

    verifiers = {"kis": kis, "kiwoom": kiwoom, "toss": toss}

    def _verifier(provider, profile, payload):
        target = verifiers.get(provider)
        if target is None:
            return None
        return asyncio.run(target.verify(payload, profile))

    return _verifier


def _default_verifier():
    from stock_mcp_server.market_data.kis_verifier import KisVerifier
    from stock_mcp_server.market_data.kiwoom_verifier import KiwoomVerifier
    from stock_mcp_server.market_data.toss_verifier import TossVerifier

    return make_default_verifier(
        kis=KisVerifier(), kiwoom=KiwoomVerifier(), toss=TossVerifier())


def _default_service() -> BrokerService:
    return BrokerService()


def _interactive_main() -> int:
    """소유자 로컬용 대화형 등록. 비밀값은 화면에 표시되지 않는 입력으로만
    받고, 파일·명령행·출력 어디에도 남기지 않는다."""
    import getpass
    import os

    ids = registry.ids()
    print("증권사 Open API 연결")
    home = os.environ.get("STOCKLENS_HOME")
    if home:
        print(f"STOCKLENS_HOME = {home}")
    print(f"공급자 선택 {ids}")
    provider = input("증권사 [kis/kiwoom/toss] (기본 kis): ").strip() or "kis"
    try:
        descriptor = registry.require(provider)
    except UnknownProviderError:
        print(f"지원하지 않는 공급자입니다: {provider}")
        return 2

    print(f"{descriptor.display_name} 연결을 시작합니다.")
    print("비밀값은 입력 중 화면에 표시되지 않으며 OS 자격 증명 저장소에만"
          " 저장됩니다.")
    profiles = descriptor.supported_profiles
    if len(profiles) == 1:
        profile = profiles[0]
        print(f"프로필: {profile} (이 증권사는 {profile}만 지원)")
    else:
        profile = input(
            f"프로필 [{'/'.join(profiles)}] (기본 real): ").strip() or "real"
        if profile not in profiles:
            print(f"{'/'.join(profiles)}만 지원합니다.")
            return 2

    credentials = {}
    for field in descriptor.credential_schema:
        value = getpass.getpass(f"{field.label} ({field.name}): ").strip()
        if not value:
            print("모든 값이 필요합니다.")
            return 2
        credentials[field.name] = value

    response = handle_request(
        {
            "contract_version": CONTRACT_VERSION,
            "action": "verify_and_save",
            "provider": provider,
            "profile": profile,
            "credentials": credentials,
        },
        verifier=_default_verifier(),
    )
    credentials.clear()
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if response.get("ok"):
        print("\n연결 성공. 데이터 사용 방식을 자동으로 바꾸려면:")
        print('  echo {"contract_version":1,"action":"set_data_source_mode",'
              f'"provider":"{provider}","mode":"auto"}} | '
              "stocklens-broker --json --non-interactive --stdin")
    return 0 if response.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--interactive" in args:
        return _interactive_main()
    # Manager 계약은 --json --non-interactive --stdin 조합만 지원한다.
    required = {"--json", "--non-interactive", "--stdin"}
    if not required.issubset(set(args)):
        print(json.dumps(_error(
            "invalid_request",
            "--json --non-interactive --stdin 조합이 필요합니다"),
            ensure_ascii=False))
        return 2

    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        print(json.dumps(
            _error("invalid_request", "stdin이 유효한 JSON이 아닙니다"),
            ensure_ascii=False))
        return 2

    response = handle_request(
        request, service=_default_service(),
        verifier=_default_verifier())
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
