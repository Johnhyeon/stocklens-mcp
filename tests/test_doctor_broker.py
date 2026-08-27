"""doctor 증권사 확장 테스트 (Task 13).

- capabilities / provider_connections 는 additive 선택 필드
- 비밀값 없음, KIS 미연결이 전체 doctor 상태를 실패로 만들지 않음
- 실사용 keychain 을 읽지 않도록 상태 파일 우선으로 판단
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import diagnostics
from stock_mcp_server.market_data.connection_state import save_state

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-666"


class FakeKeyring:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.reads = 0

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        self.reads += 1
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        del self.entries[(service, username)]


class DoctorBrokerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = patch.dict(
            "os.environ", {"STOCKLENS_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _report_dict(self, keyring=None) -> dict:
        kr = keyring or FakeKeyring()
        with patch.object(diagnostics, "_broker_keyring", return_value=kr):
            report = diagnostics.run_diagnostics(online=False)
        return report.to_dict()

    def test_capabilities_always_advertised(self):
        doc = self._report_dict()
        self.assertEqual(doc["capabilities"], {
            "broker_connection_contract": 1,
            "market_data_router_contract": 1,
        })

    def test_not_configured_without_state_and_no_keychain_read(self):
        kr = FakeKeyring()
        doc = self._report_dict(keyring=kr)
        kis = doc["provider_connections"]["kis"]
        self.assertEqual(kis["status"], "not_configured")
        # 상태 파일이 없으면 keychain 을 읽지 않는다 (테스트 격리 규칙).
        self.assertEqual(kr.reads, 0)

    def test_connected_state_reported_without_secrets(self):
        save_state({
            "connection_generation": 3,
            "active_provider": "kis",
            "active_profile": "real",
            "data_source_mode": "auto",
            "capability_results": {
                "real": {"kr_intraday": "available",
                         "us_intraday": "available"},
            },
        }, self.home)
        kr = FakeKeyring({
            ("stocklens-broker-kis", "kis:real"): json.dumps({
                "app_key": SENTINEL_KEY, "app_secret": "s"}),
        })
        doc = self._report_dict(keyring=kr)
        kis = doc["provider_connections"]["kis"]
        self.assertEqual(kis["status"], "connected")
        self.assertEqual(kis["active_profile"], "real")
        self.assertEqual(kis["storage"], "os-keychain")
        self.assertTrue(kis["profiles"]["real"]["configured"])
        self.assertTrue(kis["profiles"]["real"]["verified"])
        self.assertFalse(kis["profiles"]["demo"]["configured"])

        text = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn("app_secret", text)

    def test_missing_kis_does_not_fail_overall(self):
        base = self._report_dict()
        # broker 확장이 overall 판정에 영향을 주지 않는다. 기존 check 들의
        # 결과만으로 정해진다 (라이선스 없음 등은 기존 동작 그대로).
        check_ids = {c["id"] for c in base["checks"]}
        self.assertNotIn("BROKER", " ".join(check_ids).upper())

    def test_corrupt_state_file_falls_back_safely(self):
        path = self.home / "broker_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken json", encoding="utf-8")
        doc = self._report_dict()
        self.assertEqual(
            doc["provider_connections"]["kis"]["status"], "not_configured")

    def test_schema_version_unchanged(self):
        doc = self._report_dict()
        self.assertEqual(doc["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
