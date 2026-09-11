"""연결 세대(connection generation) 상태 파일 테스트 (Task 4).

실행 중인 MCP 프로세스는 각 KIS 호출 전에 generation 을 확인해, Manager 가
바꾼 연결 상태를 재시작 없이 반영한다. 상태 파일에는 비밀값이 없다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
)
from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE,
    load_state,
    save_state,
    state_path,
)


class FakeKeyring:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise Exception("not found")
        del self.entries[(service, username)]


class ConnectionStateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_default_legacy(self):
        state = load_state(self.home)
        self.assertEqual(state["connection_generation"], 0)
        self.assertEqual(state["data_source_mode"], "legacy")
        self.assertIsNone(state["active_provider"])
        self.assertIsNone(state["active_profile"])

    def test_malformed_json_fails_safe_to_legacy(self):
        state_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        state_path(self.home).write_text("{broken json", encoding="utf-8")
        state = load_state(self.home)
        self.assertEqual(state["data_source_mode"], "legacy")
        self.assertEqual(state["connection_generation"], 0)

    def test_wrong_types_fail_safe_to_legacy(self):
        state_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        state_path(self.home).write_text(
            json.dumps({"connection_generation": "seven",
                        "data_source_mode": 5}),
            encoding="utf-8")
        state = load_state(self.home)
        self.assertEqual(state["data_source_mode"], "legacy")
        self.assertEqual(state["connection_generation"], 0)

    def test_save_and_reload_roundtrip(self):
        state = dict(DEFAULT_STATE)
        state.update({"connection_generation": 7, "active_provider": "kis",
                      "active_profile": "real", "data_source_mode": "auto"})
        save_state(state, self.home)
        loaded = load_state(self.home)
        self.assertEqual(loaded["connection_generation"], 7)
        self.assertEqual(loaded["active_profile"], "real")
        # 파일은 항상 유효한 JSON 하나다 (원자 교체).
        raw = state_path(self.home).read_text(encoding="utf-8")
        json.loads(raw)

    def test_default_state_is_not_shared_mutable(self):
        a = load_state(self.home)
        a["connection_generation"] = 99
        b = load_state(self.home)
        self.assertEqual(b["connection_generation"], 0)


class GenerationBumpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=FakeKeyring(), home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _gen(self) -> int:
        return load_state(self.home)["connection_generation"]

    def _creds(self) -> BrokerCredentials:
        return BrokerCredentials(app_key="k", app_secret="s")

    def test_generation_increments_on_each_mutation(self):
        g0 = self._gen()

        self.store.save_profile("real", self._creds())
        g1 = self._gen()
        self.assertGreater(g1, g0)

        self.store.save_profile("demo", self._creds())
        g2 = self._gen()
        self.assertGreater(g2, g1)

        self.store.switch_profile("real")
        g3 = self._gen()
        self.assertGreater(g3, g2)

        self.store.disconnect_profile("demo")
        g4 = self._gen()
        self.assertGreater(g4, g3)

        self.store.disconnect_provider()
        g5 = self._gen()
        self.assertGreater(g5, g4)

    def test_mode_change_increments_generation(self):
        g0 = self._gen()
        self.store.set_data_source_mode("auto")
        self.assertGreater(self._gen(), g0)


if __name__ == "__main__":
    unittest.main()
