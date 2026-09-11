"""ProviderRuntime 테스트 (1.0 Task 9).

- provider generation 변경은 그 provider 의 클라이언트만 폐기한다
- routing generation 변경(primary 교체)은 진행 중 스냅샷을 오염시키지 않는다
- disable 된 provider 는 절대 구성되지 않는다
- 요청 시장에 맞는 어댑터만 만든다
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.credential_store import CredentialStore
from stock_mcp_server.market_data.kis_domestic import KisDomesticProvider
from stock_mcp_server.market_data.kis_overseas import KisOverseasProvider
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.runtime import ProviderRuntime
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


def _payload(provider):
    schema = registry.require(provider).credential_schema
    names = [f.name for f in schema]
    return SecretPayload.from_schema(
        schema, {names[0]: "key-1", names[1]: "secret-1"})


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = CredentialStore(
            keyring_module=self.keyring, home=self.home)
        self.runtime = ProviderRuntime(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self, provider, profile="real"):
        pending = self.store.stage(provider, profile, _payload(provider))
        self.store.commit(pending, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def test_provider_generation_clears_only_changed_client(self):
        self._connect("kis", "real")
        first = self.runtime.client("kis", "real")
        self.assertIsNotNone(first)
        # 같은 generation 이면 같은 클라이언트를 재사용한다 (토큰 1분 제한).
        self.assertIs(self.runtime.client("kis", "real"), first)

        # 다른 공급자 연결은 kis generation 을 올리지 않는다.
        self._connect("toss", "real")
        self.assertIs(self.runtime.client("kis", "real"), first)

        # kis 재연결(제 generation 증가)은 kis 클라이언트만 폐기한다.
        self._connect("kis", "real")
        second = self.runtime.client("kis", "real")
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)

    def test_routing_generation_changes_primary_without_mixing_requests(self):
        self._connect("kis", "real")
        snap_before = self.runtime.snapshot()
        self.assertEqual(snap_before.primary_provider, "kis")

        # 진행 중 요청이 든 스냅샷은 primary 교체 후에도 그대로다.
        self._connect("toss", "real")
        from stock_mcp_server.market_data.connection_state import (
            load_state_v2,
            save_state_v2,
            set_primary_v2,
        )
        save_state_v2(
            set_primary_v2(load_state_v2(self.home), "toss"), self.home)

        self.assertEqual(snap_before.primary_provider, "kis")
        snap_after = self.runtime.snapshot()
        self.assertEqual(snap_after.primary_provider, "toss")
        self.assertGreater(snap_after.routing_generation,
                           snap_before.routing_generation)

    def test_disabled_provider_is_never_constructed(self):
        self._connect("kis", "real")
        self.store.disable_profile("kis", "real")
        snapshot = self.runtime.snapshot()
        providers = self.runtime.providers_for("KR", snapshot=snapshot)
        self.assertNotIn("kis", providers)
        self.assertFalse(snapshot.capabilities("kis")["connected"])

    def test_runtime_builds_only_requested_market_provider(self):
        self._connect("kis", "real")
        kr = self.runtime.providers_for("KR")
        self.assertIsInstance(kr["kis"], KisDomesticProvider)
        us = self.runtime.providers_for("US")
        self.assertIsInstance(us["kis"], KisOverseasProvider)
        # 기존 공급자는 항상 포함된다 (일봉·legacy 경로).
        for providers in (kr, us):
            self.assertIn("naver", providers)
            self.assertIn("yahoo", providers)

    def test_unconnected_runtime_has_no_broker(self):
        providers = self.runtime.providers_for("KR")
        self.assertNotIn("kis", providers)
        snapshot = self.runtime.snapshot()
        self.assertIsNone(snapshot.primary_provider)
        self.assertFalse(snapshot.capabilities("kis")["connected"])

    def test_invalidate_provider_drops_cached_client(self):
        self._connect("kis", "real")
        first = self.runtime.client("kis", "real")
        self.runtime.invalidate_provider("kis")
        second = self.runtime.client("kis", "real")
        self.assertIsNot(second, first)


if __name__ == "__main__":
    unittest.main()
