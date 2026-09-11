"""G5 장애 주입 테스트: keychain 불가, 캐시 삭제 거부, 네트워크 계열.

원칙: 삭제하지 못한 항목을 삭제했다고 말하지 않는다. 장애는 비밀 없는
코드로 정직하게 보고하고, 기존 정상 프로필을 잃지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
    KeychainUnavailableError,
)
from stock_mcp_server.market_data.connection_state import load_state
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient


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


class DeadKeyring(FakeKeyring):
    """읽기부터 죽는 keychain (백엔드 부재·잠김)."""

    def get_password(self, service, username):
        raise RuntimeError("keychain locked")


class DeleteDeniedKeyring(FakeKeyring):
    """읽기는 되는데 삭제가 거부되는 keychain."""

    def delete_password(self, service, username):
        raise PermissionError("access denied")


class KeychainUnavailableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, keyring):
        return BrokerProfileStore(
            provider="kis", keyring_module=keyring, home=self.home)

    def _service(self, keyring):
        return broker_cli.BrokerService(keyring_module=keyring,
                                        home=self.home)

    def _save_kis(self, service, key="k", secret="s"):
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload

        payload = SecretPayload.from_schema(
            registry.require("kis").credential_schema,
            {"app_key": key, "app_secret": secret})
        service.save_verified("kis", "real", payload, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def test_read_failure_raises_typed_error(self):
        store = self._store(DeadKeyring())
        with self.assertRaises(KeychainUnavailableError):
            store.load_profile("real")

    def test_cli_reports_keychain_unavailable_without_mutation(self):
        from stock_mcp_server.market_data.connection_state import (
            load_state_v2,
        )
        service = self._service(DeadKeyring())
        gen = load_state_v2(self.home)["routing_generation"]
        resp = broker_cli.handle_request(
            {"contract_version": 1, "action": "status", "provider": "kis"},
            service=service)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "keychain_unavailable")
        self.assertEqual(
            load_state_v2(self.home)["routing_generation"], gen)

    def test_disconnect_with_denied_delete_does_not_claim_removal(self):
        from stock_mcp_server.market_data.connection_state import (
            load_state_v2,
        )
        keyring = DeleteDeniedKeyring()
        service = self._service(keyring)
        self._save_kis(service)

        resp = broker_cli.handle_request(
            {"contract_version": 1, "action": "disconnect_provider",
             "provider": "kis"}, service=service)
        # 자격 증명이 남아 있다. ok 로 보고하면 안 된다.
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "keychain_unavailable")
        self.assertEqual(len(keyring.entries), 1)  # 실제로 남아 있음
        # disable 우선: 실패해도 이 공급자는 더 이상 요청에 쓰이지 않는다.
        self.assertTrue(resp.get("provider_disabled"))
        state = load_state_v2(self.home)
        self.assertEqual(state["providers"]["kis"]["lifecycle"],
                         "disabled_pending_cleanup")

    def test_disconnect_missing_entry_stays_idempotent(self):
        service = self._service(FakeKeyring())
        # 저장된 게 없는 상태의 해제는 여전히 조용히 성공한다.
        resp = broker_cli.handle_request(
            {"contract_version": 1, "action": "disconnect_provider",
             "provider": "kis"}, service=service)
        self.assertTrue(resp["ok"])

    def test_doctor_reports_unknown_not_false_negative(self):
        from stock_mcp_server import diagnostics
        from unittest.mock import patch
        import os

        from stock_mcp_server.market_data.connection_state import save_state
        save_state({"connection_generation": 1, "active_provider": "kis",
                    "active_profile": "real", "data_source_mode": "auto"},
                   self.home)
        with patch.dict(os.environ, {"STOCKLENS_HOME": str(self.home)}), \
             patch.object(diagnostics, "_broker_keyring",
                          return_value=DeadKeyring()):
            _, connections = diagnostics._broker_summary()
        # keychain 을 못 읽으면 "미설정"이라고 단정하지 않는다.
        self.assertEqual(connections["kis"]["status"], "unknown")


class StatusAfterCommitFailureTests(unittest.TestCase):
    """리뷰 지적: 저장(커밋)은 성공했는데 응답용 store.status() 의
    keychain 읽기가 실패하면 전체가 실패로 바뀌어, 사용자가 "기존 키
    유지"로 오해한다. 커밋 성공과 상태 조회를 분리한다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    class _DiesAfterSet(FakeKeyring):
        """set_password 성공 후부터 get_password 가 죽는 keychain -
        '커밋은 됐는데 재조회가 안 되는' 순간을 재현한다."""

        def __init__(self):
            super().__init__()
            self.dead = False

        def set_password(self, service, username, password):
            super().set_password(service, username, password)
            self.dead = True

        def get_password(self, service, username):
            if self.dead:
                raise RuntimeError("keychain read failed")
            return super().get_password(service, username)

    def test_verify_and_save_success_survives_status_failure(self):
        keyring = self._DiesAfterSet()
        service = broker_cli.BrokerService(
            keyring_module=keyring, home=self.home)

        class OkVerifier:
            async def verify(self, credentials, profile):
                return {"auth": "ok", "kr_intraday": "available",
                        "us_intraday": "available"}

        resp = broker_cli.handle_request(
            {"contract_version": 1, "action": "verify_and_save",
             "provider": "kis", "profile": "real",
             "credentials": {"app_key": "new-key",
                             "app_secret": "new-secret"}},
            service=service,
            verifier=broker_cli.make_cli_verifier(OkVerifier()))

        # 저장은 실제로 됐다 - 실패로 보고하면 안 된다.
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("status_unavailable"))
        self.assertTrue(any("상태" in w for w in resp.get("warnings", [])))
        # 최소 상태(상태 파일 기반)는 제공한다.
        self.assertEqual(resp["status"]["active_profile"], "real")
        # keychain 에는 새 키가 실제로 저장돼 있다.
        keyring.dead = False
        self.assertEqual(
            service.credentials.load_active("kis", "real").get("app_key"),
            "new-key")

    def test_mode_change_success_survives_status_failure(self):
        keyring = self._DiesAfterSet()
        service = broker_cli.BrokerService(
            keyring_module=keyring, home=self.home)
        # set_data_source_mode 는 keychain 을 안 쓰지만 status() 가 쓴다.
        keyring.dead = True
        resp = broker_cli.handle_request(
            {"contract_version": 1, "action": "set_data_source_mode",
             "provider": "kis", "mode": "auto"}, service=service)
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("status_unavailable"))
        self.assertEqual(resp["status"]["data_source_mode"], "auto")


class StateSaveFailureTests(unittest.TestCase):
    """리뷰 지적(결함 4): keyring 에 새 키를 쓴 뒤 상태 파일 저장이
    실패하면 새 키가 남았다 - "실패하면 기존 프로필 유지" 약속 위반.
    상태 저장 실패 시 keyring 을 원복해야 한다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _fail_state_save(self):
        from unittest.mock import patch
        from stock_mcp_server.market_data import broker_profiles
        return patch.object(
            broker_profiles, "save_state",
            side_effect=PermissionError("state file locked"))

    def test_existing_key_restored_when_state_save_fails(self):
        self.store.save_profile("real", BrokerCredentials(
            app_key="old-key", app_secret="old-secret"))
        with self._fail_state_save():
            with self.assertRaises(PermissionError):
                self.store.save_profile("real", BrokerCredentials(
                    app_key="new-key", app_secret="new-secret"))
        restored = self.store.load_profile("real")
        self.assertEqual(restored.app_key, "old-key")

    def test_first_save_rolls_back_to_empty_when_state_save_fails(self):
        with self._fail_state_save():
            with self.assertRaises(PermissionError):
                self.store.save_profile("real", BrokerCredentials(
                    app_key="new-key", app_secret="new-secret"))
        self.assertIsNone(self.store.load_profile("real"))


class NetworkFailureTests(unittest.TestCase):
    def _client(self, exc):
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc
        return KisClient(
            BrokerCredentials(app_key="k", app_secret="s"), "real",
            transport=httpx.MockTransport(handler))

    def test_dns_failure_is_provider_unavailable(self):
        client = self._client(httpx.ConnectError("getaddrinfo failed"))
        with self.assertRaises(KisApiError) as ctx:
            asyncio.run(client.request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-time-dailychartprice",
                tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")

    def test_tls_failure_is_provider_unavailable(self):
        client = self._client(httpx.ConnectError(
            "CERTIFICATE_VERIFY_FAILED"))
        with self.assertRaises(KisApiError) as ctx:
            asyncio.run(client.request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-time-dailychartprice",
                tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")


class CacheRemovalDeniedTests(unittest.TestCase):
    def test_cache_removal_failure_reported_honestly(self):
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload

        with tempfile.TemporaryDirectory() as home:
            service = broker_cli.BrokerService(
                keyring_module=FakeKeyring(), home=Path(home))
            payload = SecretPayload.from_schema(
                registry.require("kis").credential_schema,
                {"app_key": "k", "app_secret": "s"})
            service.save_verified("kis", "real", payload, {"auth": "ok"})

            class DenyingCache:
                def remove_provider(self, provider):
                    raise PermissionError("in use")

            resp = broker_cli.handle_request(
                {"contract_version": 1, "action": "disconnect_provider",
                 "provider": "kis"},
                service=service, cache=DenyingCache())
            # 리뷰 지적(결함 3): Manager 는 ok 만 보고 전체 정리를
            # 계속한다. 캐시가 남았으면 ok=False 로 멈추게 한다.
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], "cache_cleanup_failed")
            self.assertFalse(resp["cache_removed"])
            # 자격 증명은 이미 지워졌다는 사실도 함께 전달한다.
            self.assertTrue(resp["credentials_removed"])
            self.assertIn("status", resp)


if __name__ == "__main__":
    unittest.main()
