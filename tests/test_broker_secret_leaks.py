"""증권사 비밀값 전수 sentinel 스위프 (Task 18).

가짜 App Key·Secret·토큰을 실제 흐름(저장 -> CLI 전 액션 -> doctor ->
캐시 -> 오류 경로)에 주입하고, 산출물 어디에도 원문·앞조각이 없는지
훑는다. 모듈별 테스트가 이미 막고 있는 것을 한 번 더 전체로 조인다.
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
from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
)
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient
from stock_mcp_server.market_data.provider_cache import ProviderCache

KEY = "PSA-SWEEP-APP-KEY-99887766"
SECRET = "PSA-SWEEP-APP-SECRET-99887766"
TOKEN = "PSA-SWEEP-ACCESS-TOKEN-99887766"

FRAGMENTS = (KEY, SECRET, TOKEN, KEY[:12], SECRET[:12], TOKEN[:12])


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
    for fragment in FRAGMENTS:
        testcase.assertNotIn(fragment, text, f"{where}에 비밀 흔적")


class SecretSweepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_all_actions_and_files_clean(self):
        self.store.save_profile(
            "real", BrokerCredentials(app_key=KEY, app_secret=SECRET))

        requests = [
            {"action": "status"},
            {"action": "switch_profile", "profile": "real"},
            {"action": "set_data_source_mode", "mode": "auto"},
            {"action": "verify", "profile": "real",
             "credentials": {"app_key": KEY, "app_secret": SECRET}},
            {"action": "disconnect_profile", "profile": "demo"},
            {"action": "disconnect_provider"},
            {"action": "format_disk"},
            {"contract_version": 999, "action": "status"},
        ]
        for req in requests:
            full = {"contract_version": 1, "provider": "kis"}
            full.update(req)
            resp = broker_cli.handle_request(full, store=self.store)
            _assert_clean(self, json.dumps(resp, ensure_ascii=False),
                          f"CLI {req.get('action')}")

        # 상태 파일 원문 스위프
        state_file = self.home / "broker_state.json"
        if state_file.exists():
            _assert_clean(self, state_file.read_text("utf-8"), "state file")

    def test_doctor_json_clean_when_connected(self):
        self.store.save_profile(
            "real", BrokerCredentials(app_key=KEY, app_secret=SECRET))
        with patch.dict("os.environ",
                        {"STOCKLENS_HOME": str(self.home)}), \
             patch.object(diagnostics, "_broker_keyring",
                          return_value=self.keyring):
            report = diagnostics.run_diagnostics(online=False)
        _assert_clean(self, report.to_json(), "doctor JSON")

    def test_cache_paths_and_contents_clean(self):
        cache = ProviderCache(home=self.home)
        key = {
            "provider": "kis", "profile": "real", "market": "KR",
            "symbol": "005930", "venue": "KRX", "session": "regular",
            "source_interval": "1m", "trading_date": "20260827",
            "cursor": "", "adjustment": "unadjusted",
        }
        cache.put(key, {"bars": []}, complete=True)
        for path in (self.home / "cache").rglob("*"):
            _assert_clean(self, str(path), "cache path")
            if path.is_file():
                _assert_clean(self, path.read_text("utf-8"), "cache content")

    def test_error_paths_clean_even_when_provider_echoes_secret(self):
        # 공급자가 오류 본문에 키·토큰을 그대로 되돌려주는 최악의 경우.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/tokenP"):
                return httpx.Response(200, json={
                    "access_token": TOKEN, "expires_in": 86400})
            return httpx.Response(403, json={
                "echo_key": KEY, "echo_secret": SECRET,
                "echo_token": TOKEN})

        client = KisClient(
            BrokerCredentials(app_key=KEY, app_secret=SECRET), "real",
            transport=httpx.MockTransport(handler))
        try:
            asyncio.run(client.request("GET", "/x", tr_id="T1"))
            self.fail("expected KisApiError")
        except KisApiError as exc:
            _assert_clean(self, str(exc) + repr(exc), "KisApiError")
        _assert_clean(self, repr(client), "KisClient repr")

    def test_metrics_labels_have_no_secret_fields(self):
        # 관측 label 은 provider/market/interval 등 비밀 없는 값만 쓴다.
        # 코드 스위프: market_data 모듈이 metrics 에 자격 증명을 넘기는
        # 호출이 존재하지 않는다.
        root = Path(__file__).resolve().parents[1] / "stock_mcp_server"
        offenders = []
        for path in (root / "market_data").glob("*.py"):
            text = path.read_text("utf-8")
            if "track_metrics" in text or "_metrics" in text:
                if "app_key" in text or "app_secret" in text:
                    offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
