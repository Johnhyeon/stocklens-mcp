"""broker 프로필 저장소 테스트 (Task 4).

실제 OS keychain을 절대 건드리지 않는다. 모든 테스트는 가짜 keyring과
tmp 홈 디렉터리를 주입한다.
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
from stock_mcp_server.market_data.connection_state import load_state

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-000"
SENTINEL_SECRET = "PSA-SENTINEL-APP-SECRET-000"


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


def _creds(key=SENTINEL_KEY, secret=SENTINEL_SECRET) -> BrokerCredentials:
    return BrokerCredentials(app_key=key, app_secret=secret)


class BrokerProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_real_and_demo_are_independent(self):
        self.store.save_profile("real", _creds())
        self.store.save_profile("demo", _creds("demo-key", "demo-secret"))

        real = self.store.load_profile("real")
        demo = self.store.load_profile("demo")
        self.assertEqual(real.app_key, SENTINEL_KEY)
        self.assertEqual(demo.app_key, "demo-key")

        self.store.disconnect_profile("demo")
        self.assertIsNone(self.store.load_profile("demo"))
        self.assertIsNotNone(self.store.load_profile("real"))

    def test_credentials_saved_as_one_atomic_payload(self):
        self.store.save_profile("real", _creds())
        # keyring entry 는 프로필당 정확히 1개다. key/secret 이 흩어지면
        # 반쪽 저장 상태가 생길 수 있다.
        self.assertEqual(len(self.keyring.entries), 1)
        (service, username), payload = next(iter(self.keyring.entries.items()))
        self.assertIn("stocklens", service.lower())
        self.assertIn("kis", (service + username).lower())
        parsed = json.loads(payload)
        self.assertEqual(parsed["app_key"], SENTINEL_KEY)
        self.assertEqual(parsed["app_secret"], SENTINEL_SECRET)

    def test_no_secret_in_repr_or_str(self):
        creds = _creds()
        for text in (repr(creds), str(creds)):
            self.assertNotIn(SENTINEL_KEY, text)
            self.assertNotIn(SENTINEL_SECRET, text)

    def test_no_secret_in_status_or_state_file(self):
        self.store.save_profile("real", _creds())
        status = json.dumps(self.store.status(), ensure_ascii=False)
        self.assertNotIn(SENTINEL_KEY, status)
        self.assertNotIn(SENTINEL_SECRET, status)

        state_file = self.home / "broker_state.json"
        self.assertTrue(state_file.exists())
        raw = state_file.read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL_KEY, raw)
        self.assertNotIn(SENTINEL_SECRET, raw)

    def test_failed_save_preserves_old_profile_and_generation(self):
        self.store.save_profile("real", _creds("old-key", "old-secret"))
        gen_before = self.store.status()["connection_generation"]
        saved = self.keyring.entries.copy()

        class FailOnSet(FakeKeyring):
            def __init__(self, inner):
                super().__init__()
                self.entries = inner.entries

            def set_password(self, service, username, password):
                raise RuntimeError("keychain unavailable")

        failing_store = BrokerProfileStore(
            provider="kis", keyring_module=FailOnSet(self.keyring),
            home=self.home)
        with self.assertRaises(Exception):
            failing_store.save_profile("real", _creds("new-key", "new-secret"))

        self.assertEqual(self.keyring.entries, saved)
        old = self.store.load_profile("real")
        self.assertEqual(old.app_key, "old-key")
        self.assertEqual(
            self.store.status()["connection_generation"], gen_before)

    def test_save_sets_active_profile(self):
        self.store.save_profile("real", _creds())
        status = self.store.status()
        self.assertEqual(status["active_profile"], "real")
        self.assertEqual(status["active_provider"], "kis")

    def test_switch_requires_configured_profile(self):
        self.store.save_profile("real", _creds())
        with self.assertRaises(ValueError):
            self.store.switch_profile("demo")
        # 실패한 switch 는 active pointer 를 바꾸지 않는다.
        self.assertEqual(self.store.status()["active_profile"], "real")

    def test_disconnect_profile_keeps_other_profile(self):
        self.store.save_profile("real", _creds())
        self.store.save_profile("demo", _creds("d-key", "d-secret"))
        self.store.disconnect_profile("demo")
        st = self.store.status()
        self.assertTrue(st["profiles"]["real"]["configured"])
        self.assertFalse(st["profiles"]["demo"]["configured"])

    def test_disconnect_active_profile_clears_active_pointer(self):
        self.store.save_profile("real", _creds())
        self.store.disconnect_profile("real")
        st = self.store.status()
        self.assertIsNone(st["active_profile"])

    def test_disconnect_provider_removes_all_profiles(self):
        self.store.save_profile("real", _creds())
        self.store.save_profile("demo", _creds("d-key", "d-secret"))
        self.store.disconnect_provider()
        self.assertIsNone(self.store.load_profile("real"))
        self.assertIsNone(self.store.load_profile("demo"))
        st = self.store.status()
        self.assertIsNone(st["active_profile"])
        self.assertEqual(self.keyring.entries, {})

    def test_disconnect_provider_is_idempotent(self):
        self.store.save_profile("real", _creds())
        self.store.disconnect_provider()
        self.store.disconnect_provider()
        self.assertIsNone(self.store.load_profile("real"))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_profile("paper", _creds())

    def test_data_source_mode(self):
        self.assertEqual(self.store.status()["data_source_mode"], "legacy")
        self.store.set_data_source_mode("auto")
        self.assertEqual(self.store.status()["data_source_mode"], "auto")
        with self.assertRaises(ValueError):
            self.store.set_data_source_mode("random")

    def test_status_exposes_capability_results(self):
        # Manager UI 가 국내·미국 분봉 능력을 표시하는 데 쓴다. 비밀 없음.
        self.store.save_profile("real", _creds())
        from stock_mcp_server.market_data.connection_state import (
            load_state,
            save_state,
        )
        state = load_state(self.home)
        state["capability_results"] = {
            "real": {"kr_intraday": "available", "us_intraday": "limited"}}
        save_state(state, self.home)
        st = self.store.status()
        self.assertEqual(
            st["capability_results"]["real"]["kr_intraday"], "available")

    def test_state_survives_reload(self):
        self.store.save_profile("real", _creds())
        self.store.set_data_source_mode("auto")
        reloaded = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)
        st = reloaded.status()
        self.assertEqual(st["active_profile"], "real")
        self.assertEqual(st["data_source_mode"], "auto")
        state = load_state(self.home)
        self.assertEqual(state["active_profile"], "real")


if __name__ == "__main__":
    unittest.main()
