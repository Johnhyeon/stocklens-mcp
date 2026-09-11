"""0.9 실사용 상태 마이그레이션 회귀 (리뷰 P1, 2026-08-27 실사고 재현).

실사고: 0.9 로 KIS 를 연결해 쓰던 실사용 홈에서 1.0 CLI 로 키움을
연결하자 v2 상태에 kis 레코드가 사라져, 기존 구매자의 KIS 연결이
끊긴 것처럼 됐다.

원칙: 0.9 상태 파일은 추측으로 만들지 않는다. 0.9 와 동일한 기록기
(BrokerProfileStore.save_profile)가 실제로 쓴 파일을 그대로 넣는다.
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
from stock_mcp_server.market_data.connection_state import (
    load_state_v2,
    provider_capabilities_v2,
)
from stock_mcp_server.market_data.secrets import SecretPayload


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


class V1MigrationRegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        # 0.9 실사용 흐름 그대로: verify_and_save 가 하던 것과 동일하게
        # 0.9 기록기(BrokerProfileStore)로 상태 파일과 keyring 을 만든다.
        # 0.9 의 capability_results 는 auth 키 없이 시장별 판정만 담았다.
        legacy = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)
        legacy.save_profile(
            "real",
            BrokerCredentials(app_key="real-09-key",
                              app_secret="real-09-secret"),
            capability_results={"kr_intraday": "available",
                                "us_intraday": "available"})
        legacy.set_data_source_mode("auto")
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_09_state_migrates_to_connected_verified_kis(self):
        state = load_state_v2(self.home)
        self.assertIn("kis", state["providers"], "0.9 KIS 연결이 사라짐")
        self.assertEqual(state["primary_provider"], "kis")
        record = state["providers"]["kis"]
        self.assertEqual(record["lifecycle"], "connected")
        self.assertEqual(record["active_profile"], "real")
        profile = record["profiles"]["real"]
        # 0.9 는 verify_and_save 통과 시에만 저장했다 = 인증 완료 상태다.
        # capability_results 에 auth 키가 없다고 미검증으로 만들면
        # 업그레이드가 기존 구매자의 연결을 끊는다.
        self.assertTrue(profile["verified"],
                        "0.9 연결이 미검증으로 강등되면 라우팅이 꺼진다")
        self.assertEqual(profile["credential_ref"], "legacy")

    def test_migrated_kis_still_routes(self):
        state = load_state_v2(self.home)
        caps = provider_capabilities_v2(state, "kis")
        self.assertTrue(caps["connected"])
        self.assertTrue(caps["kr_intraday"],
                        "업그레이드 직후 KIS KR 라우팅이 꺼지면 안 된다")
        self.assertTrue(caps["us_intraday"])

    def test_migrated_credentials_still_load(self):
        payload = self.service.credentials.load_active("kis", "real")
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("app_key"), "real-09-key")

    def test_adding_kiwoom_keeps_kis_intact(self):
        # 실사고 재현 경로: 1.0 CLI 로 키움을 연결한다.
        schema_payload = SecretPayload.from_schema(
            __import__("stock_mcp_server.market_data.provider_registry",
                       fromlist=["registry"]).registry.require(
                           "kiwoom").credential_schema,
            {"app_key": "kw-key", "secret_key": "kw-secret"})
        self.service.save_verified("kiwoom", "real", schema_payload, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

        state = load_state_v2(self.home)
        self.assertIn("kis", state["providers"],
                      "키움 연결이 기존 KIS 를 지워버림 (실사고)")
        self.assertIn("kiwoom", state["providers"])
        # 첫 연결(0.9 시절 KIS)이 primary 로 유지된다.
        self.assertEqual(state["primary_provider"], "kis")
        self.assertTrue(
            state["providers"]["kis"]["profiles"]["real"]["verified"])
        # 자격 증명도 그대로다.
        payload = self.service.credentials.load_active("kis", "real")
        self.assertEqual(payload.get("app_key"), "real-09-key")

    def test_cli_status_after_upgrade_shows_kis_connected(self):
        resp = broker_cli.handle_request({
            "contract_version": 1, "action": "status", "provider": "kis",
        }, service=self.service)
        self.assertTrue(resp["ok"], resp)
        st = resp["status"]
        self.assertEqual(st["active_provider"], "kis")
        self.assertEqual(st["active_profile"], "real")
        self.assertTrue(st["profiles"]["real"]["configured"])
        self.assertIn(
            "real", st["providers"]["kis"]["verified_profiles"],
            "구 Manager 가 미연결로 표시하게 된다")


if __name__ == "__main__":
    unittest.main()
