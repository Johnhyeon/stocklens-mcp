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
            # KIS 전체 연결 해제 때만 공급자 분봉 캐시 전체를 삭제한다.
            # 현재 환경(disconnect_profile) 해제는 캐시를 유지한다.
            if cache is None:
                from stock_mcp_server.market_data.provider_cache import (
                    ProviderCache,
                )
                # store 가 보는 홈과 같은 홈의 캐시를 지운다. 테스트가 tmp 홈
                # store 를 주입하면 캐시도 tmp 홈만 본다.
                cache = ProviderCache(home=store.home)
            cache_removed = True
            cache_error: str | None = None
            try:
                cache.remove_provider(provider)
            except Exception as exc:  # noqa: BLE001
                # 삭제하지 못한 항목을 삭제했다고 말하지 않는다.
                cache_removed = False
                cache_error = type(exc).__name__
            return _ok(action, store, extra={
                "cache_removed": cache_removed,
                **({"cache_error": cache_error} if cache_error else {}),
            })

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


def make_cli_verifier(kis_verifier):
    """KisVerifier 를 handle_request 가 기대하는 callable 로 감싼다.

    - verify: 저장 없이 능력 결과만 보고
    - verify_and_save: 인증 게이트 통과 시에만 keyring 저장 + 상태 파일에
      시장별 능력 결과 기록. 실패 시 어떤 것도 바꾸지 않는다.
    """
    import asyncio

    from stock_mcp_server.market_data.connection_state import (
        load_state,
        save_state,
    )

    def _verifier(*, action, store, profile, credentials):
        result = asyncio.run(kis_verifier.verify(credentials, profile))

        if result.get("auth") != "ok":
            return _error(
                "credential_invalid",
                "App Key 또는 App Secret 인증에 실패했습니다. "
                "키를 다시 확인하세요.")

        if action == "verify":
            return _ok(action, store, extra={"verification": result})

        # verify_and_save: 저장 후 능력 결과를 비밀 없는 상태 파일에 남긴다.
        store.save_profile(profile, credentials)
        state = load_state(store.home)
        capability_results = dict(state.get("capability_results") or {})
        capability_results[profile] = {
            "kr_intraday": result["kr_intraday"],
            "us_intraday": result["us_intraday"],
        }
        state["capability_results"] = capability_results
        save_state(state, store.home)
        return _ok(action, store, extra={"verification": result})

    return _verifier


def _default_store(provider: str = "kis") -> BrokerProfileStore:
    return BrokerProfileStore(provider=provider)


def _interactive_main() -> int:
    """소유자 로컬용 대화형 등록. 비밀값은 화면에 표시되지 않는 입력으로만
    받고, 파일·명령행·출력 어디에도 남기지 않는다."""
    import getpass

    from stock_mcp_server.market_data.kis_verifier import KisVerifier

    print("한국투자증권(KIS) Open API 연결")
    print("비밀값은 입력 중 화면에 표시되지 않으며 OS 자격 증명 저장소에만"
          " 저장됩니다.")
    profile = input("프로필 [real/demo] (기본 real): ").strip() or "real"
    if profile not in PROFILES:
        print("real 또는 demo만 지원합니다.")
        return 2
    app_key = getpass.getpass("App Key: ").strip()
    app_secret = getpass.getpass("App Secret: ").strip()
    if not app_key or not app_secret:
        print("App Key와 App Secret이 모두 필요합니다.")
        return 2

    response = handle_request(
        {
            "contract_version": CONTRACT_VERSION,
            "action": "verify_and_save",
            "provider": "kis",
            "profile": profile,
            "credentials": {"app_key": app_key, "app_secret": app_secret},
        },
        verifier=make_cli_verifier(KisVerifier()),
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if response.get("ok"):
        print("\n연결 성공. 데이터 사용 방식을 자동으로 바꾸려면:")
        print('  echo {"contract_version":1,"action":"set_data_source_mode",'
              '"provider":"kis","mode":"auto"} | '
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

    provider = request.get("provider") if isinstance(request, dict) else None
    store = None
    if provider in SUPPORTED_PROVIDERS:
        store = _default_store(provider)
    from stock_mcp_server.market_data.kis_verifier import KisVerifier
    response = handle_request(
        request, store=store, verifier=make_cli_verifier(KisVerifier()))
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
