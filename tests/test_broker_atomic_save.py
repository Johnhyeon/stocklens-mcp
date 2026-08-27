"""verify_and_save 원자성 테스트 (Task 14, 1.0 Task 6에서 v2 슬롯 포팅).

- 최소 인증 게이트 통과 시에만 저장
- 시장별 능력 결과를 상태 파일에 보존 (검증 안 된 시장을 available 로 주장 X)
- 실패 시 기존 활성 키·상태 무변경 (v2: pointer 교체 실패 = 이전 키 유지)
- 능력 결과는 pointer 교체와 같은 한 번의 상태 쓰기에 실린다
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.connection_state import load_state_v2

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


def _verifier(result, counter=None):
    def _verify(provider, profile, payload):
        if counter is not None:
            counter.append(provider)
        return dict(result)
    return _verify


class VerifyAndSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _handle(self, action, verifier_result, profile="real",
                key=SENTINEL_KEY, secret=SENTINEL_SECRET):
        return broker_cli.handle_request(
            {"contract_version": 1, "action": action, "provider": "kis",
             "profile": profile,
             "credentials": {"app_key": key, "app_secret": secret}},
            service=self.service, verifier=_verifier(verifier_result))

    def _active_key(self, profile="real"):
        payload = self.service.credentials.load_active("kis", profile)
        return payload.get("app_key") if payload else None

    def test_verify_reports_without_saving(self):
        resp = self._handle("verify", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["verification"]["kr_intraday"], "available")
        self.assertFalse(self.service.has_profile("kis", "real"))
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn(SENTINEL_SECRET, text)

    def test_verify_and_save_persists_profile_and_capabilities(self):
        resp = self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "unavailable"})
        self.assertTrue(resp["ok"])
        self.assertTrue(self.service.has_profile("kis", "real"))

        state = load_state_v2(self.home)
        kis = state["providers"]["kis"]
        self.assertEqual(kis["active_profile"], "real")
        caps = kis["profiles"]["real"]["capabilities"]
        self.assertEqual(caps["kr_intraday"], "available")
        # 검증 안 된 시장을 available 로 주장하지 않는다.
        self.assertEqual(caps["us_intraday"], "unavailable")

        raw = (self.home / "broker_state.json").read_text("utf-8")
        self.assertNotIn(SENTINEL_KEY, raw)
        self.assertNotIn(SENTINEL_SECRET, raw)

    def test_auth_failure_saves_nothing(self):
        self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"}, key="old-key", secret="old-secret")
        gen = load_state_v2(self.home)["routing_generation"]

        resp = self._handle("verify_and_save", {
            "auth": "credential_invalid", "kr_intraday": "unverified",
            "us_intraday": "unverified"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "credential_invalid")

        # 기존 활성 키와 generation 이 그대로다.
        self.assertEqual(self._active_key(), "old-key")
        self.assertEqual(load_state_v2(self.home)["routing_generation"], gen)

    def test_capabilities_land_in_same_write_as_pointer_switch(self):
        """v1 리뷰 지적의 v2 계승: 능력 결과가 pointer 교체와 다른 쓰기로
        찢어지면 그 실패가 트랜잭션 밖이 된다. commit 이후 상태 스냅샷
        하나에 pointer 와 capability 가 함께 있어야 한다."""
        self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"}, key="new-key", secret="new-secret")
        state = load_state_v2(self.home)
        record = state["providers"]["kis"]["profiles"]["real"]
        self.assertIsNotNone(record["credential_ref"])
        self.assertNotEqual(record["credential_ref"], "legacy")
        self.assertEqual(record["capabilities"]["kr_intraday"], "available")
        self.assertTrue(record["verified"])
        # pending 트랜잭션 기록도 같은 쓰기에서 정리됐다.
        self.assertEqual(state["pending_operations"], [])

    def test_pointer_write_failure_keeps_old_key_and_capabilities(self):
        # 이전 저장 성공
        self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"}, key="old-key", secret="old-secret")
        old_state = load_state_v2(self.home)

        # commit 의 상태 쓰기를 죽인다.
        with patch(
                "stock_mcp_server.market_data.credential_store.save_state_v2",
                side_effect=PermissionError("state locked")):
            resp = self._handle("verify_and_save", {
                "auth": "ok", "kr_intraday": "limited",
                "us_intraday": "available"},
                key="new-key", secret="new-secret")

        self.assertFalse(resp["ok"])
        # 기존 활성 키 유지, 능력 결과도 이전 그대로다.
        self.assertEqual(self._active_key(), "old-key")
        state = load_state_v2(self.home)
        self.assertEqual(
            state["providers"]["kis"]["profiles"]["real"]["capabilities"],
            old_state["providers"]["kis"]["profiles"]["real"]["capabilities"])

    def test_limited_demo_is_recorded_as_is(self):
        resp = self._handle("verify_and_save", {
            "auth": "ok", "kr_intraday": "limited",
            "us_intraday": "unavailable"}, profile="demo")
        self.assertTrue(resp["ok"])
        state = load_state_v2(self.home)
        kis = state["providers"]["kis"]
        self.assertEqual(
            kis["profiles"]["demo"]["capabilities"]["kr_intraday"],
            "limited")
        self.assertEqual(kis["active_profile"], "demo")


if __name__ == "__main__":
    unittest.main()
