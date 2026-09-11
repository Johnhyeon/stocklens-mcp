"""연결 상태 v2 테스트 (1.0 Task 4).

- v1(KIS 단일) 상태의 무손실·비밀 없는 migration
- 엄격한 allowlist sanitizer: 모르는 필드·공급자·프로필은 버린다
- 손상 상태는 증권사 호출 없는 legacy 안전 모드로 내린다
- 첫 연결만 primary, 추가 연결은 primary 를 바꾸지 않는다
- routing generation 과 provider generation 은 독립이다
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE_V2,
    connect_profile_v2,
    load_state_v2,
    migrate_v1,
    save_state_v2,
    set_primary_v2,
    state_path,
)

_V1_STATE = {
    "connection_generation": 7,
    "active_provider": "kis",
    "active_profile": "real",
    "data_source_mode": "auto",
    "capability_results": {
        "real": {"auth": "ok", "kr_intraday": "available",
                 "us_intraday": "available"},
    },
}


class MigrationTests(unittest.TestCase):
    def test_v1_kis_state_migrates_to_v2_without_secret_fields(self):
        raw = dict(_V1_STATE)
        raw["client_secret"] = "SHOULD-NEVER-SURVIVE"
        v2 = migrate_v1(raw)
        text = json.dumps(v2)
        self.assertNotIn("SHOULD-NEVER-SURVIVE", text)
        self.assertNotIn("client_secret", text)

        self.assertEqual(v2["state_version"], 2)
        self.assertEqual(v2["routing_generation"], 7)
        self.assertEqual(v2["primary_provider"], "kis")
        self.assertEqual(v2["data_source_mode"], "auto")
        kis = v2["providers"]["kis"]
        self.assertEqual(kis["generation"], 7)
        self.assertEqual(kis["lifecycle"], "connected")
        self.assertEqual(kis["active_profile"], "real")
        real = kis["profiles"]["real"]
        self.assertEqual(real["credential_ref"], "legacy")
        self.assertTrue(real["verified"])
        self.assertEqual(real["capabilities"], {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def test_v1_disconnected_state_migrates_to_empty_providers(self):
        v2 = migrate_v1({
            "connection_generation": 3, "active_provider": None,
            "active_profile": None, "data_source_mode": "legacy",
        })
        self.assertEqual(v2["providers"], {})
        self.assertIsNone(v2["primary_provider"])
        self.assertEqual(v2["routing_generation"], 3)
        self.assertEqual(v2["data_source_mode"], "legacy")


class SanitizerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload: dict):
        path = state_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _v2(self, **overrides) -> dict:
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state.update({
            "routing_generation": 5,
            "primary_provider": "kiwoom",
            "data_source_mode": "auto",
            "providers": {
                "kiwoom": {
                    "generation": 2,
                    "lifecycle": "connected",
                    "active_profile": "real",
                    "profiles": {
                        "real": {
                            "credential_ref": "ref-abc123",
                            "verified": True,
                            "verified_at": "2026-08-27T18:00:00+09:00",
                            "capabilities": {"kr_intraday": "available"},
                        },
                    },
                },
            },
        })
        state.update(overrides)
        return state

    def test_roundtrip_keeps_v2_fields(self):
        save_state_v2(self._v2(), self.home)
        loaded = load_state_v2(self.home)
        self.assertEqual(loaded["primary_provider"], "kiwoom")
        self.assertEqual(
            loaded["providers"]["kiwoom"]["profiles"]["real"]
            ["credential_ref"], "ref-abc123")

    def test_unknown_top_level_fields_are_dropped(self):
        bad = self._v2()
        bad["client_secret"] = "LEAKED-TOP-LEVEL"
        bad["shell_command"] = "rm -rf /"
        self._write(bad)
        loaded = load_state_v2(self.home)
        self.assertNotIn("client_secret", loaded)
        self.assertNotIn("shell_command", loaded)
        save_state_v2(loaded, self.home)
        text = state_path(self.home).read_text(encoding="utf-8")
        self.assertNotIn("LEAKED-TOP-LEVEL", text)

    def test_unknown_provider_is_dropped(self):
        bad = self._v2()
        bad["providers"]["evilbroker"] = {
            "generation": 1, "lifecycle": "connected",
            "active_profile": "real", "profiles": {}}
        self._write(bad)
        loaded = load_state_v2(self.home)
        self.assertNotIn("evilbroker", loaded["providers"])
        self.assertIn("kiwoom", loaded["providers"])

    def test_unknown_profile_is_dropped(self):
        bad = self._v2()
        bad["providers"]["kiwoom"]["profiles"]["superuser"] = {
            "credential_ref": "x", "verified": True,
            "verified_at": None, "capabilities": {}}
        # 토스는 real 만 지원한다. demo 프로필은 버려져야 한다.
        bad["providers"]["toss"] = {
            "generation": 1, "lifecycle": "connected",
            "active_profile": "real",
            "profiles": {
                "real": {"credential_ref": "r", "verified": True,
                         "verified_at": None, "capabilities": {}},
                "demo": {"credential_ref": "d", "verified": True,
                         "verified_at": None, "capabilities": {}},
            },
        }
        self._write(bad)
        loaded = load_state_v2(self.home)
        self.assertNotIn(
            "superuser", loaded["providers"]["kiwoom"]["profiles"])
        self.assertNotIn("demo", loaded["providers"]["toss"]["profiles"])
        self.assertIn("real", loaded["providers"]["toss"]["profiles"])

    def test_unknown_profile_record_fields_are_dropped(self):
        bad = self._v2()
        bad["providers"]["kiwoom"]["profiles"]["real"]["app_secret"] = "LEAK"
        self._write(bad)
        loaded = load_state_v2(self.home)
        self.assertNotIn(
            "app_secret", loaded["providers"]["kiwoom"]["profiles"]["real"])

    def test_corrupt_state_fails_to_legacy_safe_mode(self):
        for payload in ("{broken", '"just a string"', json.dumps({
                "state_version": 2, "routing_generation": "NaN",
                "primary_provider": 5, "providers": []})):
            path = state_path(self.home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            loaded = load_state_v2(self.home)
            self.assertEqual(loaded["data_source_mode"], "legacy")
            self.assertEqual(loaded["providers"], {})
            self.assertIsNone(loaded["primary_provider"])

    def test_v1_file_is_migrated_on_load(self):
        self._write(_V1_STATE)
        loaded = load_state_v2(self.home)
        self.assertEqual(loaded["state_version"], 2)
        self.assertEqual(loaded["primary_provider"], "kis")
        self.assertIn("kis", loaded["providers"])

    def test_primary_not_in_providers_is_cleared(self):
        bad = self._v2(primary_provider="toss")
        self._write(bad)
        loaded = load_state_v2(self.home)
        self.assertIsNone(loaded["primary_provider"])


class PrimarySelectionTests(unittest.TestCase):
    def _connect(self, state, provider, profile="real"):
        return connect_profile_v2(
            state, provider, profile,
            credential_ref=f"ref-{provider}-{profile}",
            verified=True,
            verified_at="2026-08-27T18:00:00+09:00",
            capabilities={"kr_intraday": "available"},
        )

    def test_first_connected_provider_becomes_primary(self):
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state = self._connect(state, "kiwoom")
        self.assertEqual(state["primary_provider"], "kiwoom")

    def test_additional_provider_does_not_change_primary(self):
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state = self._connect(state, "kis")
        state = self._connect(state, "toss")
        state = self._connect(state, "kiwoom")
        self.assertEqual(state["primary_provider"], "kis")

    def test_explicit_set_primary_requires_verified_connection(self):
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state = self._connect(state, "kis")
        with self.assertRaises(ValueError):
            set_primary_v2(state, "toss")
        state = self._connect(state, "toss")
        state = set_primary_v2(state, "toss")
        self.assertEqual(state["primary_provider"], "toss")

    def test_routing_and_provider_generations_are_independent(self):
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state = self._connect(state, "kis")
        kis_gen = state["providers"]["kis"]["generation"]
        routing_gen = state["routing_generation"]

        # 다른 공급자 연결: routing 은 오르고 kis generation 은 그대로다.
        state = self._connect(state, "toss")
        self.assertEqual(state["providers"]["kis"]["generation"], kis_gen)
        self.assertGreater(state["routing_generation"], routing_gen)

        # 같은 공급자 재연결: 해당 provider generation 만 오른다.
        toss_gen = state["providers"]["toss"]["generation"]
        state = self._connect(state, "toss")
        self.assertGreater(state["providers"]["toss"]["generation"], toss_gen)
        self.assertEqual(state["providers"]["kis"]["generation"], kis_gen)


if __name__ == "__main__":
    unittest.main()
