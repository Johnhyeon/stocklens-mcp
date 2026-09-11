"""CredentialStore 테스트 (1.0 Task 5, 설계 11.3 버전 keyring 슬롯).

새 키는 활성 키를 덮어쓰지 않는다. 비밀 없는 pending 기록 -> 새 슬롯 저장
-> 상태 pointer 원자 교체 -> 이전 슬롯 삭제 순서로 처리하고, 어느 단계에서
중단돼도 기존 활성 키가 유지된다. 실제 OS keyring 은 절대 건드리지 않는다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_profiles import (
    KeychainUnavailableError,
)
from stock_mcp_server.market_data.connection_state import (
    load_state_v2,
    save_state,
    state_path,
)
from stock_mcp_server.market_data.credential_store import (
    CredentialStore,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_OLD_KEY = "OLD-APP-KEY-VALUE"
_OLD_SECRET = "OLD-APP-SECRET-VALUE"
_NEW_KEY = "NEW-APP-KEY-VALUE"
_NEW_SECRET = "NEW-APP-SECRET-VALUE"


class FakeKeyring:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_delete_for: set[str] = set()
        self.fail_set = False

    def set_password(self, service, username, password):
        if self.fail_set:
            raise RuntimeError("set denied")
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if username in self.fail_delete_for:
            raise RuntimeError("delete denied")
        if (service, username) not in self.entries:
            raise Exception("not found")
        del self.entries[(service, username)]

    def usernames(self) -> set[str]:
        return {username for _, username in self.entries}


def _payload(provider: str, key: str, secret: str) -> SecretPayload:
    schema = registry.require(provider).credential_schema
    names = [f.name for f in schema]
    return SecretPayload.from_schema(schema, {names[0]: key, names[1]: secret})


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = CredentialStore(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self, provider="kiwoom", profile="real",
                 key=_OLD_KEY, secret=_OLD_SECRET):
        pending = self.store.stage(
            provider, profile, _payload(provider, key, secret))
        report = self.store.commit(
            pending, {"auth": "ok", "kr_intraday": "available"})
        return pending, report

    def test_stage_and_commit_roundtrip(self):
        pending, report = self._connect()
        self.assertTrue(report.committed)
        loaded = self.store.load_active("kiwoom", "real")
        self.assertEqual(loaded.get("app_key"), _OLD_KEY)
        state = load_state_v2(self.home)
        self.assertEqual(state["primary_provider"], "kiwoom")
        self.assertEqual(state["pending_operations"], [])
        record = state["providers"]["kiwoom"]["profiles"]["real"]
        self.assertEqual(record["credential_ref"], pending.credential_ref)
        self.assertTrue(record["verified"])

    def test_candidate_slot_is_not_active_before_state_commit(self):
        self._connect()
        old_ref = load_state_v2(self.home)[
            "providers"]["kiwoom"]["profiles"]["real"]["credential_ref"]

        self.store.stage(
            "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))

        # commit 전: 활성 키는 여전히 이전 값이다.
        loaded = self.store.load_active("kiwoom", "real")
        self.assertEqual(loaded.get("app_key"), _OLD_KEY)
        state = load_state_v2(self.home)
        self.assertEqual(
            state["providers"]["kiwoom"]["profiles"]["real"]
            ["credential_ref"], old_ref)
        # pending 기록은 비밀 없이 남는다.
        self.assertEqual(len(state["pending_operations"]), 1)
        self.assertNotIn(_NEW_KEY, json.dumps(state))

    def test_state_failure_keeps_old_active_slot(self):
        self._connect()
        pending = self.store.stage(
            "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))

        with patch(
                "stock_mcp_server.market_data.credential_store.save_state_v2",
                side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.commit(pending, {"auth": "ok"})

        # 기존 활성 키 유지, 후보 슬롯은 정리됐다.
        loaded = self.store.load_active("kiwoom", "real")
        self.assertEqual(loaded.get("app_key"), _OLD_KEY)
        self.assertNotIn(
            f"kiwoom:real:{pending.credential_ref}",
            self.keyring.usernames())

    def test_crash_recovery_removes_uncommitted_candidate(self):
        self._connect()
        pending = self.store.stage(
            "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))
        # 여기서 프로세스가 죽었다고 가정한다 (commit 없음).

        fresh = CredentialStore(keyring_module=self.keyring, home=self.home)
        report = fresh.recover_pending()

        self.assertIn(
            f"kiwoom:real:{pending.credential_ref}", report.removed)
        self.assertNotIn(
            f"kiwoom:real:{pending.credential_ref}",
            self.keyring.usernames())
        self.assertEqual(load_state_v2(self.home)["pending_operations"], [])
        # 활성 키는 그대로다.
        self.assertEqual(
            fresh.load_active("kiwoom", "real").get("app_key"), _OLD_KEY)

    def test_old_slot_cleanup_failure_is_reported_not_hidden(self):
        pending1, _ = self._connect()
        old_slot = f"kiwoom:real:{pending1.credential_ref}"
        self.keyring.fail_delete_for.add(old_slot)

        pending2 = self.store.stage(
            "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))
        report = self.store.commit(pending2, {"auth": "ok"})

        # 새 키는 활성이고, 이전 슬롯 정리 실패는 감춰지지 않는다.
        self.assertTrue(report.committed)
        self.assertIn(old_slot, report.failed)
        self.assertEqual(
            self.store.load_active("kiwoom", "real").get("app_key"),
            _NEW_KEY)
        # 재시도용 pending 기록이 남고, recover 가 지운다.
        state = load_state_v2(self.home)
        self.assertTrue(any(
            op["op"] == "retire_slot" for op in state["pending_operations"]))
        self.keyring.fail_delete_for.clear()
        recover = self.store.recover_pending()
        self.assertIn(old_slot, recover.removed)
        self.assertEqual(load_state_v2(self.home)["pending_operations"], [])

    def test_legacy_kis_entry_remains_readable(self):
        # v1 시절: 고정 슬롯 + v1 상태 파일.
        self.keyring.entries[("stocklens-broker-kis", "kis:real")] = \
            json.dumps({"app_key": _OLD_KEY, "app_secret": _OLD_SECRET})
        state_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        save_state({
            "connection_generation": 4,
            "active_provider": "kis",
            "active_profile": "real",
            "data_source_mode": "auto",
            "capability_results": {"real": {"auth": "ok"}},
        }, self.home)

        loaded = self.store.load_active("kis", "real")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("app_key"), _OLD_KEY)
        self.assertEqual(loaded.get("app_secret"), _OLD_SECRET)

    def test_secret_repr_and_errors_are_clean(self):
        pending = self.store.stage(
            "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))
        self.assertNotIn(_NEW_KEY, repr(pending))
        self.assertNotIn(_NEW_SECRET, repr(pending))

        # keyring 이 죽어도 오류에 비밀이 없다.
        self.keyring.fail_set = True
        try:
            self.store.stage(
                "kiwoom", "real", _payload("kiwoom", _NEW_KEY, _NEW_SECRET))
        except KeychainUnavailableError as exc:
            self.assertNotIn(_NEW_KEY, str(exc))
            self.assertNotIn(_NEW_SECRET, str(exc))
        else:
            self.fail("keyring 실패가 전파되지 않았습니다")

    def test_unknown_provider_and_profile_rejected(self):
        with self.assertRaises(Exception):
            self.store.stage(
                "evil", "real", _payload("kis", _NEW_KEY, _NEW_SECRET))
        with self.assertRaises(ValueError):
            self.store.stage(
                "toss", "demo", _payload("toss", _NEW_KEY, _NEW_SECRET))

    def test_disable_then_cleanup_removes_only_that_provider(self):
        self._connect(provider="kis", profile="real")
        self._connect(provider="kiwoom", profile="real")

        self.store.disable_profile("kiwoom", "real")
        state = load_state_v2(self.home)
        self.assertEqual(state["providers"]["kiwoom"]["lifecycle"],
                         "disabled_pending_cleanup")
        # disable 만으로는 keyring 이 남아 있다 (cleanup 이 지운다).
        report = self.store.cleanup_disabled("kiwoom", None)
        self.assertTrue(report.removed)
        self.assertFalse(report.failed)

        state = load_state_v2(self.home)
        self.assertNotIn("kiwoom", state["providers"])
        # kis 는 영향이 없다.
        self.assertEqual(
            self.store.load_active("kis", "real").get("app_key"), _OLD_KEY)

    def test_cleanup_failure_keeps_provider_disabled(self):
        pending, _ = self._connect(provider="kiwoom", profile="real")
        self.store.disable_profile("kiwoom", "real")
        slot = f"kiwoom:real:{pending.credential_ref}"
        self.keyring.fail_delete_for.add(slot)

        report = self.store.cleanup_disabled("kiwoom", None)
        self.assertIn(slot, report.failed)
        state = load_state_v2(self.home)
        # 부분 삭제를 전체 성공으로 보고하지 않는다. 공급자는 비활성 유지.
        self.assertEqual(state["providers"]["kiwoom"]["lifecycle"],
                         "disabled_pending_cleanup")


if __name__ == "__main__":
    unittest.main()
