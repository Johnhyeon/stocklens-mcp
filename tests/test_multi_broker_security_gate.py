"""멀티 증권사 비밀값 전수 게이트 (1.0 Task 23).

세 공급자의 모든 credential 필드·토큰 sentinel 을 실제 흐름(CLI 전 액션,
검증 실패, doctor, 상태 파일, 캐시 경로·내용)에 주입하고 산출물 어디에도
원문·앞조각이 없는지 훑는다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli, diagnostics
from stock_mcp_server.market_data.provider_cache import ProviderCache
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_SENTINELS = {
    "kis": {"app_key": "PSA-G23-KIS-KEY-1111",
            "app_secret": "PSA-G23-KIS-SECRET-1111"},
    "kiwoom": {"app_key": "PSA-G23-KIWOOM-KEY-2222",
               "secret_key": "PSA-G23-KIWOOM-SECRET-2222"},
    "toss": {"client_id": "PSA-G23-TOSS-ID-3333",
             "client_secret": "PSA-G23-TOSS-SECRET-3333"},
}
_TOKEN = "PSA-G23-ACCESS-TOKEN-9999"

_FRAGMENTS = tuple(
    fragment
    for creds in _SENTINELS.values()
    for value in creds.values()
    for fragment in (value, value[:14])
) + (_TOKEN, _TOKEN[:14])


class FakeKeyring:
    def __init__(self):
        self.entries = {}

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise Exception("not found")
        del self.entries[(service, username)]


def _assert_clean(testcase, text: str, where: str):
    for fragment in _FRAGMENTS:
        testcase.assertNotIn(fragment, text, f"{where}에 비밀 흔적")


def _ok_verifier():
    def _verify(provider, profile, payload):
        return {"auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"}
    return _verify


class MultiBrokerSecretSweep(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)
        self._env = patch.dict(
            "os.environ", {"STOCKLENS_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _connect_all(self):
        for provider, creds in _SENTINELS.items():
            resp = broker_cli.handle_request({
                "contract_version": 1, "action": "verify_and_save",
                "provider": provider, "profile": "real",
                "credentials": dict(creds),
            }, service=self.service, verifier=_ok_verifier())
            self.assertTrue(resp["ok"], resp)
            _assert_clean(self, json.dumps(resp, ensure_ascii=False),
                          f"connect {provider}")

    def test_cli_all_actions_all_providers_clean(self):
        self._connect_all()
        actions = [
            {"action": "status"},
            {"action": "describe_providers"},
            {"action": "set_primary_provider"},
            {"action": "switch_profile", "profile": "real"},
            {"action": "set_data_source_mode", "mode": "auto"},
            {"action": "recover_cleanup"},
            {"action": "disconnect_provider"},
        ]
        for provider in registry.ids():
            for req in actions:
                full = {"contract_version": 1, "provider": provider}
                full.update(req)
                resp = broker_cli.handle_request(
                    full, service=self.service, verifier=_ok_verifier())
                _assert_clean(
                    self, json.dumps(resp, ensure_ascii=False),
                    f"{provider}/{req['action']}")
        # 상태 파일 원문
        state_file = self.home / "broker_state.json"
        if state_file.exists():
            _assert_clean(self, state_file.read_text("utf-8"), "state file")

    def test_reflected_secret_error_paths_clean(self):
        # 공급자가 오류 본문에 키·토큰을 그대로 반사하는 최악의 경우.
        from stock_mcp_server.market_data.kiwoom_client import (
            KiwoomApiError,
            KiwoomClient,
        )
        from stock_mcp_server.market_data.toss_client import (
            TossApiError,
            TossClient,
        )

        def kiwoom_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={
                    "token": _TOKEN, "expires_dt": "20270101000000",
                    "token_type": "bearer"})
            return httpx.Response(500, json={
                "echo": _SENTINELS["kiwoom"], "token": _TOKEN})

        kiwoom = KiwoomClient(
            SecretPayload.from_schema(
                registry.require("kiwoom").credential_schema,
                _SENTINELS["kiwoom"]),
            "real", transport=httpx.MockTransport(kiwoom_handler))
        try:
            asyncio.run(kiwoom.request("kr_chart", api_id="ka10080",
                                       body={}))
            self.fail("expected KiwoomApiError")
        except KiwoomApiError as exc:
            _assert_clean(self, str(exc) + repr(exc) + repr(kiwoom),
                          "KiwoomApiError")

        def toss_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={
                    "access_token": _TOKEN, "token_type": "Bearer",
                    "expires_in": 86400})
            return httpx.Response(500, json={
                "echo": _SENTINELS["toss"], "token": _TOKEN})

        toss = TossClient(
            SecretPayload.from_schema(
                registry.require("toss").credential_schema,
                _SENTINELS["toss"]),
            "real", transport=httpx.MockTransport(toss_handler))
        try:
            asyncio.run(toss.request("candles", params={}))
            self.fail("expected TossApiError")
        except TossApiError as exc:
            _assert_clean(self, str(exc) + repr(exc) + repr(toss),
                          "TossApiError")

    def test_doctor_output_clean_with_all_connected(self):
        self._connect_all()
        with patch.object(diagnostics, "_broker_keyring",
                          return_value=self.keyring):
            report = diagnostics.run_diagnostics(online=False)
        _assert_clean(self, report.to_json(), "doctor JSON")

    def test_cache_paths_and_contents_clean_per_provider(self):
        cache = ProviderCache(home=self.home)
        for provider in registry.ids():
            key = {
                "provider": provider, "profile": "real", "market": "KR",
                "symbol": "005930", "venue": "KRX", "session": "regular",
                "source_interval": "1m", "trading_date": "20260827",
                "cursor": "", "adjustment": "unadjusted",
            }
            cache.put(key, {"bars": []}, complete=True)
        for path in (self.home / "cache").rglob("*"):
            _assert_clean(self, str(path), "cache path")
            if path.is_file():
                _assert_clean(self, path.read_text("utf-8"),
                              "cache content")


if __name__ == "__main__":
    unittest.main()
