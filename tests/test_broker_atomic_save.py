"""verify_and_save 원자성 테스트 (Task 14).

- 최소 인증 게이트 통과 시에만 저장
- 시장별 능력 결과를 상태 파일에 보존 (검증 안 된 시장을 available 로 주장 X)
- 실패 시 기존 프로필·active pointer·상태 무변경
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
)
from stock_mcp_server.market_data.connection_state import load_state

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-555"
SENTINEL_SECRET = "PSA-SENTINEL-APP-SECRET-555"


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


class FakeVerifier:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def verify(self, credentials, profile):
        self.calls += 1
        return self.result


class VerifyAndSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _handle(self, action, verifier_result, profile="real",
                key=SENTINEL_KEY, secret=SENTINEL_SECRET):
        verifier = broker_cli.make_cli_verifier(FakeVerifier(verifier_result))
        return broker_cli.handle_request(
            {"contract_version": 1, "action": action, "provider": "kis",
             "profile": profile,
             "credentials": {"app_key": key, "app_secret": secret}},
            store=self.store, verifier=verifier)

    def test_verify_reports_without_saving(self):
        resp = self._handle("verify", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["verification"]["kr_intraday"], "available")
        self.assertFalse(self.store.has_profile("real"))
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn(SENTINEL_SECRET, text)

    def test_verify_and_save_persists_profile_and_capabilities(self):
        resp = self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "unavailable"})
        self.assertTrue(resp["ok"])
        self.assertTrue(self.store.has_profile("real"))

        state = load_state(self.home)
        self.assertEqual(state["active_profile"], "real")
        caps = state["capability_results"]["real"]
        self.assertEqual(caps["kr_intraday"], "available")
        # 검증 안 된 시장을 available 로 주장하지 않는다.
        self.assertEqual(caps["us_intraday"], "unavailable")

        raw = (self.home / "broker_state.json").read_text("utf-8")
        self.assertNotIn(SENTINEL_KEY, raw)
        self.assertNotIn(SENTINEL_SECRET, raw)

    def test_auth_failure_saves_nothing(self):
        self.store.save_profile("real", BrokerCredentials(
            app_key="old-key", app_secret="old-secret"))
        gen = load_state(self.home)["connection_generation"]

        resp = self._handle("verify_and_save", {
            "auth": "credential_invalid", "kr_intraday": "unverified",
            "us_intraday": "unverified"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "credential_invalid")

        # 기존 프로필과 generation 이 그대로다.
        old = self.store.load_profile("real")
        self.assertEqual(old.app_key, "old-key")
        self.assertEqual(load_state(self.home)["connection_generation"], gen)

    def test_no_second_state_write_capability_saved_atomically(self):
        """리뷰 지적: verify_and_save 가 상태 파일을 두 번 써서, 두 번째
        (capability) 저장 실패가 keyring 원복 범위 밖이었다.

        수정 후 계약: capability 는 save_profile 의 원자 쓰기에 포함되고
        CLI 는 connection_state.save_state 를 직접 부르지 않는다. 이
        테스트는 CLI 경로의 save_state 를 죽여놓고도 저장이 성공하며
        capability 까지 기록됨을 요구한다 (두 번째 쓰기 부재 증명).
        """
        from unittest.mock import patch
        from stock_mcp_server.market_data import connection_state

        self.store.save_profile("real", BrokerCredentials(
            app_key="old-key", app_secret="old-secret"))

        # broker_profiles 는 자기 모듈 참조를 쓰므로 영향받지 않고,
        # CLI 가 두 번째 쓰기를 시도하면 여기서 터진다.
        with patch.object(connection_state, "save_state",
                          side_effect=PermissionError("state locked")):
            resp = self._handle("verify_and_save", {
                "auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"},
                key="new-key", secret="new-secret")

        self.assertTrue(resp["ok"], resp)
        stored = self.store.load_profile("real")
        self.assertEqual(stored.app_key, "new-key")
        state = load_state(self.home)
        self.assertEqual(
            state["capability_results"]["real"]["kr_intraday"], "available")

    def test_first_write_failure_still_rolls_back_key(self):
        # 하나로 합친 뒤에도 그 유일한 쓰기가 실패하면 keyring 원복.
        from unittest.mock import patch
        from stock_mcp_server.market_data import broker_profiles

        self.store.save_profile("real", BrokerCredentials(
            app_key="old-key", app_secret="old-secret"))
        gen = load_state(self.home)["connection_generation"]

        with patch.object(broker_profiles, "save_state",
                          side_effect=PermissionError("state locked")):
            resp = self._handle("verify_and_save", {
                "auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"},
                key="new-key", secret="new-secret")

        self.assertFalse(resp["ok"])
        self.assertEqual(self.store.load_profile("real").app_key, "old-key")
        self.assertEqual(load_state(self.home)["connection_generation"], gen)
        self.assertNotIn("capability_results", load_state(self.home))

    def test_limited_demo_is_recorded_as_is(self):
        resp = self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "limited",
            "us_intraday": "unavailable"}, profile="demo")
        self.assertTrue(resp["ok"])
        state = load_state(self.home)
        self.assertEqual(state["capability_results"]["demo"]["kr_intraday"],
                         "limited")
        self.assertEqual(state["active_profile"], "demo")


if __name__ == "__main__":
    unittest.main()
