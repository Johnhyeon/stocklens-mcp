"""disable 우선 연결 해제 테스트 (1.0 Task 7, 설계 11.4).

- 첫 비밀 삭제 전에 provider 가 이미 비활성이다
- 부분 삭제를 전체 성공으로 보고하지 않는다
- disable 이후 crash 가 나도 그 provider 는 요청에 쓰이지 않는다
- 재시도는 idempotent 하게 정리를 끝낸다
- 캐시 삭제는 해당 provider 디렉터리만, 경로 이탈·symlink 거부
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.connection_state import (
    load_state_v2,
    provider_capabilities_v2,
    state_path,
)
from stock_mcp_server.market_data.provider_cache import ProviderCache
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload


class FakeKeyring:
    def __init__(self):
        self.entries = {}
        self.fail_delete_for: set[str] = set()
        self.delete_log: list[str] = []
        self.on_delete = None

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if self.on_delete is not None:
            self.on_delete(username)
        self.delete_log.append(username)
        if username in self.fail_delete_for:
            raise PermissionError("delete denied")
        if (service, username) not in self.entries:
            raise Exception("not found")
        del self.entries[(service, username)]


def _payload(provider, key="key-value", secret="secret-value"):
    schema = registry.require(provider).credential_schema
    names = [f.name for f in schema]
    return SecretPayload.from_schema(schema, {names[0]: key,
                                              names[1]: secret})


class DisableFirstDisconnectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self, provider, profile="real"):
        self.service.save_verified(provider, profile, _payload(provider), {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def _disconnect(self, provider, cache=None):
        return broker_cli.handle_request(
            {"contract_version": 1, "action": "disconnect_provider",
             "provider": provider},
            service=self.service, cache=cache or _NoopCache())

    def test_provider_is_disabled_before_first_secret_delete(self):
        self._connect("kiwoom")
        observed = []

        def _on_delete(username):
            state = load_state_v2(self.home)
            record = state["providers"].get("kiwoom") or {}
            observed.append(record.get("lifecycle"))

        self.keyring.on_delete = _on_delete
        self._disconnect("kiwoom")
        self.assertTrue(observed)
        self.assertTrue(all(
            lifecycle == "disabled_pending_cleanup"
            for lifecycle in observed), observed)

    def test_partial_keyring_delete_never_reports_complete(self):
        self._connect("kis", "real")
        self._connect("kis", "demo")
        state = load_state_v2(self.home)
        demo_ref = state["providers"]["kis"]["profiles"]["demo"][
            "credential_ref"]
        self.keyring.fail_delete_for.add(f"kis:demo:{demo_ref}")

        resp = self._disconnect("kis")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "keychain_unavailable")
        self.assertTrue(resp["provider_disabled"])
        self.assertTrue(resp["cleanup_required"])

        state = load_state_v2(self.home)
        kis = state["providers"]["kis"]
        self.assertEqual(kis["lifecycle"], "disabled_pending_cleanup")
        # real 은 지워졌고 demo 는 실패로 남았다. 거짓 보고 없음.
        self.assertNotIn("real", kis["profiles"])
        self.assertIn("demo", kis["profiles"])

    def test_crash_after_disable_prevents_provider_use(self):
        self._connect("toss")
        caps_before = provider_capabilities_v2(
            load_state_v2(self.home), "toss")
        self.assertTrue(caps_before["connected"])

        # disable 만 하고 cleanup 전에 crash 났다고 가정한다.
        self.service.credentials.disable_profile("toss", "real")

        caps = provider_capabilities_v2(load_state_v2(self.home), "toss")
        self.assertFalse(caps["connected"])
        self.assertFalse(caps.get("kr_intraday"))
        # primary 도 다른 공급자로 자동 승격되지 않고 비워진다.
        self.assertIsNone(load_state_v2(self.home)["primary_provider"])

    def test_retry_finishes_pending_cleanup_idempotently(self):
        self._connect("kiwoom")
        state = load_state_v2(self.home)
        ref = state["providers"]["kiwoom"]["profiles"]["real"][
            "credential_ref"]
        slot = f"kiwoom:real:{ref}"
        self.keyring.fail_delete_for.add(slot)

        resp = self._disconnect("kiwoom")
        self.assertFalse(resp["ok"])

        # 장애가 풀린 뒤 recover_cleanup 이 정리를 끝낸다.
        self.keyring.fail_delete_for.clear()
        for _ in range(2):  # 두 번 돌려도 안전하다
            resp = broker_cli.handle_request(
                {"contract_version": 1, "action": "recover_cleanup",
                 "provider": "kiwoom"}, service=self.service)
            self.assertTrue(resp["ok"], resp)
        state = load_state_v2(self.home)
        self.assertNotIn("kiwoom", state["providers"])
        self.assertNotIn(("stocklens-broker-kiwoom", slot),
                         self.keyring.entries)

    def test_disconnect_response_has_no_secret(self):
        self._connect("toss", "real")
        resp = self._disconnect("toss")
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("key-value", text)


class _NoopCache:
    def remove_provider(self, provider):
        return None


class ProviderCacheIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cache = ProviderCache(home=self.home)
        self.root = self.home / "cache" / "market_data"

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, provider):
        directory = self.root / provider / "real"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bars.json").write_text("{}", encoding="utf-8")

    def test_deleting_kiwoom_never_touches_kis_or_toss_cache(self):
        for provider in ("kis", "kiwoom", "toss"):
            self._seed(provider)
        self.cache.remove_provider("kiwoom")
        self.assertFalse((self.root / "kiwoom").exists())
        self.assertTrue((self.root / "kis" / "real" / "bars.json").exists())
        self.assertTrue((self.root / "toss" / "real" / "bars.json").exists())

    def test_unregistered_provider_is_rejected(self):
        self._seed("kis")
        for bad in ("naver", "evil", "kis2"):
            with self.assertRaises(ValueError):
                self.cache.remove_provider(bad)
        self.assertTrue((self.root / "kis").exists())

    def test_symlink_or_resolved_path_escape_is_rejected(self):
        victim = self.home / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("do not delete", encoding="utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        link = self.root / "kiwoom"
        try:
            os.symlink(victim, link, target_is_directory=True)
        except OSError:
            self.skipTest("symlink 권한 없음 (Windows 개발자 모드 필요)")
        with self.assertRaises(ValueError):
            self.cache.remove_provider("kiwoom")
        self.assertTrue((victim / "keep.txt").exists())

    def test_traversal_key_is_rejected(self):
        for bad in ("..", "../..", "kis/../kiwoom", "kis\\..\\kiwoom"):
            with self.assertRaises(ValueError):
                self.cache.remove_provider(bad)


if __name__ == "__main__":
    unittest.main()
