"""stocklens-broker: 증권사 연결 관리 CLI.

Manager 가 이 명령을 `--json --non-interactive --stdin` 으로 호출한다.
비밀값(App Key·App Secret)은 stdin JSON 으로만 받는다. 명령행 인자,
로그, 오류, 응답에 비밀값을 싣지 않는다.

응답은 JSON 문서 하나다. JSON 뒤에 사람용 텍스트를 덧붙이지 않는다.
"""

from __future__ import annotations

import json
import sys

from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
    PROFILES,
)

CONTRACT_VERSION = 1

SUPPORTED_PROVIDERS = ("kis",)

ACTIONS = (
    "status",
    "verify",
    "verify_and_save",
    "switch_profile",
    "disconnect_profile",
    "disconnect_provider",
    "set_data_source_mode",
)


def _error(code: str, message: str) -> dict:
    return {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "error": {"code": code, "message": message},
    }


def _ok(action: str, store: BrokerProfileStore, extra: dict | None = None) -> dict:
    resp = {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "action": action,
        "status": store.status(),
    }
    if extra:
        resp.update(extra)
    return resp


def _parse_credentials(request: dict) -> BrokerCredentials | None:
    raw = request.get("credentials")
    if not isinstance(raw, dict):
        return None
    app_key = raw.get("app_key")
    app_secret = raw.get("app_secret")
    if not isinstance(app_key, str) or not isinstance(app_secret, str):
        return None
    if not app_key.strip() or not app_secret.strip():
        return None
    return BrokerCredentials(
        app_key=app_key.strip(), app_secret=app_secret.strip())


def handle_request(
    request: dict,
    *,
    store: BrokerProfileStore | None = None,
    verifier=None,
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
    if provider not in SUPPORTED_PROVIDERS:
        return _error(
            "invalid_request",
            f"지원하지 않는 provider입니다 (지원: {SUPPORTED_PROVIDERS})")

    if store is None:
        store = _default_store(provider)

    try:
        if action == "status":
            return _ok(action, store)

        if action == "switch_profile":
            profile = request.get("profile")
            if profile not in PROFILES:
                return _error("invalid_request", "profile이 올바르지 않습니다")
            if not store.has_profile(profile):
                return _error(
                    "profile_not_configured",
                    f"프로필 {profile}이(가) 설정되어 있지 않습니다")
            store.switch_profile(profile)
            return _ok(action, store)

        if action == "disconnect_profile":
            profile = request.get("profile")
            if profile not in PROFILES:
                return _error("invalid_request", "profile이 올바르지 않습니다")
            store.disconnect_profile(profile)
            return _ok(action, store)

        if action == "disconnect_provider":
            store.disconnect_provider()
            return _ok(action, store)

        if action == "set_data_source_mode":
            mode = request.get("mode")
            try:
                store.set_data_source_mode(mode)
            except ValueError:
                return _error(
                    "invalid_request", "data_source_mode가 올바르지 않습니다")
            return _ok(action, store)

        # verify / verify_and_save
        profile = request.get("profile")
        if profile not in PROFILES:
            return _error("invalid_request", "profile이 올바르지 않습니다")
        credentials = _parse_credentials(request)
        if credentials is None:
            return _error(
                "invalid_request",
                "credentials.app_key와 credentials.app_secret이 필요합니다")
        if verifier is None:
            # 네트워크 검증기는 이후 단계에서 주입된다. 저장 없이 통제된 실패.
            return _error(
                "verifier_unavailable",
                "연결 시험 기능이 아직 활성화되지 않았습니다")
        return verifier(
            action=action, store=store, profile=profile,
            credentials=credentials)
    except Exception as exc:  # noqa: BLE001
        # 비밀값이 예외 문자열에 섞이지 않도록 형식명만 보고한다.
        return _error("internal_error", f"{type(exc).__name__}")


def _default_store(provider: str = "kis") -> BrokerProfileStore:
    return BrokerProfileStore(provider=provider)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # 현재 계약은 --json --non-interactive --stdin 조합만 지원한다.
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

    provider = request.get("provider") if isinstance(request, dict) else None
    store = None
    if provider in SUPPORTED_PROVIDERS:
        store = _default_store(provider)
    response = handle_request(request, store=store)
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
