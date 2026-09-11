"""토큰 프로세스 간 재사용 (2026-08-28 실측 기반).

실측한 공급자 계약:
- KIS 24시간, 재발급해도 같은 토큰. 발급 엔드포인트는 1분 1회 제한.
- 키움 약 24시간, 재발급해도 같은 토큰.
- 토스 24시간, **재발급하면 이전 토큰이 즉시 무효화된다(401)**.

그래서 프로세스마다 새로 발급받으면 토스에서는 다른 프로세스를 망가뜨리고,
KIS 에서는 403 을 부른다. 토큰은 OS 보안 저장소에만 두고 재사용한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.token_store import (
    TokenStore,
    credential_fingerprint,
)

_TOKEN_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kiwoom"
     / "token_success.json").read_text("utf-8"))


class FakeKeyring:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_set = False
        self.fail_get = False

    def set_password(self, service, username, password):
        if self.fail_set:
            raise RuntimeError("keyring unavailable")
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        if self.fail_get:
            raise RuntimeError("keyring unavailable")
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise KeyError("missing")
        del self.entries[(service, username)]


class FingerprintTests(unittest.TestCase):
    def test_same_credentials_same_fingerprint(self):
        a = credential_fingerprint({"app_key": "K", "secret_key": "S"})
        b = credential_fingerprint({"secret_key": "S", "app_key": "K"})
        self.assertEqual(a, b)

    def test_rotated_credentials_change_the_slot(self):
        a = credential_fingerprint({"app_key": "K", "secret_key": "S"})
        b = credential_fingerprint({"app_key": "K", "secret_key": "S2"})
        self.assertNotEqual(a, b)

    def test_fingerprint_does_not_contain_the_secret(self):
        fp = credential_fingerprint({"app_key": "APPKEY-1", "secret_key":
                                     "SUPER-SECRET-VALUE"})
        self.assertNotIn("SUPER", fp)
        self.assertNotIn("APPKEY", fp)
        self.assertEqual(len(fp), 16)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        self.kr = FakeKeyring()
        self.store = TokenStore(keyring_module=self.kr,
                                clock=lambda: self.now)

    def test_round_trip(self):
        self.store.save("kis", "real", "fp1", "TOK", self.now + 86400)
        got = self.store.load("kis", "real", "fp1")
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "TOK")

    def test_expired_token_is_not_returned(self):
        self.store.save("kis", "real", "fp1", "TOK", self.now + 60)
        self.assertIsNone(self.store.load("kis", "real", "fp1"),
                          "만료 직전 토큰을 재사용하면 요청 도중 만료된다")

    def test_other_profile_and_fingerprint_are_separate_slots(self):
        self.store.save("kis", "real", "fp1", "TOK", self.now + 86400)
        self.assertIsNone(self.store.load("kis", "demo", "fp1"))
        self.assertIsNone(self.store.load("kis", "real", "fp2"))
        self.assertIsNone(self.store.load("kiwoom", "real", "fp1"))

    def test_delete_removes_the_slot(self):
        self.store.save("kis", "real", "fp1", "TOK", self.now + 86400)
        self.assertTrue(self.store.delete("kis", "real", "fp1"))
        self.assertIsNone(self.store.load("kis", "real", "fp1"))

    def test_keyring_failure_is_not_fatal(self):
        self.kr.fail_set = True
        self.store.save("kis", "real", "fp1", "TOK", self.now + 86400)
        self.kr.fail_set = False
        self.kr.fail_get = True
        self.assertIsNone(self.store.load("kis", "real", "fp1"))

    def test_repr_never_shows_a_token(self):
        self.store.save("kis", "real", "fp1", "SENTINEL-TOKEN",
                        self.now + 86400)
        self.assertNotIn("SENTINEL", repr(self.store))


class Recorder:
    def __init__(self):
        self.token_calls = 0
        self.api_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            self.token_calls += 1
            payload = dict(_TOKEN_FIXTURE)
            payload["token"] = f"TOKEN-{self.token_calls}"
            return httpx.Response(200, json=payload)
        self.api_calls += 1
        return httpx.Response(200, json={
            "return_code": 0, "return_msg": "정상",
            "stk_min_pole_chart_qry": []})


class ClientReuseTests(unittest.TestCase):
    """새 프로세스(= 새 클라이언트 인스턴스)가 토큰을 다시 받지 않는다."""

    def setUp(self):
        self.kr = FakeKeyring()
        self.rec = Recorder()

    def _client(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomClient

        payload = SecretPayload.from_schema(
            registry.require("kiwoom").credential_schema,
            {"app_key": "K", "secret_key": "S"})
        return KiwoomClient(
            payload, "real",
            transport=httpx.MockTransport(self.rec.handler),
            token_store=TokenStore(keyring_module=self.kr))

    def _call(self, client):
        return asyncio.run(client.request(
            "kr_chart", api_id="ka10080", body={"stk_cd": "005930"}))

    def test_second_client_reuses_the_stored_token(self):
        self._call(self._client())
        self.assertEqual(self.rec.token_calls, 1)
        # 새 프로세스처럼 완전히 새 클라이언트를 만든다.
        self._call(self._client())
        self.assertEqual(self.rec.token_calls, 1,
                         "이미 유효한 토큰이 있는데 다시 발급받았다")
        self.assertEqual(self.rec.api_calls, 2)

    def test_rotated_credentials_do_not_reuse_the_old_token(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomClient

        self._call(self._client())
        rotated = SecretPayload.from_schema(
            registry.require("kiwoom").credential_schema,
            {"app_key": "K", "secret_key": "NEW"})
        client = KiwoomClient(
            rotated, "real",
            transport=httpx.MockTransport(self.rec.handler),
            token_store=TokenStore(keyring_module=self.kr))
        self._call(client)
        self.assertEqual(self.rec.token_calls, 2,
                         "키를 바꿨으면 새 토큰을 받아야 한다")

    def test_credentials_never_reach_the_token_slot(self):
        self._call(self._client())
        self.assertTrue(self.kr.entries, "토큰이 저장되지 않았다")
        for (service, username), value in self.kr.entries.items():
            # 토큰은 저장되지만 자격 증명 원문은 어디에도 없어야 한다.
            for secret in ("K", "S"):
                self.assertNotIn(f'"{secret}"', value)
            self.assertNotIn("secret_key", value)
            self.assertNotIn("secret_key", username)
            self.assertIn("stocklens-broker-token", service)

    def test_no_token_store_still_works(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomClient

        payload = SecretPayload.from_schema(
            registry.require("kiwoom").credential_schema,
            {"app_key": "K", "secret_key": "S"})
        client = KiwoomClient(
            payload, "real",
            transport=httpx.MockTransport(self.rec.handler))
        self._call(client)
        self.assertEqual(self.rec.token_calls, 1)


class CleanupTests(unittest.TestCase):
    """연결을 끊으면 그 연결로 받은 토큰도 함께 사라져야 한다."""

    def setUp(self):
        import tempfile

        from stock_mcp_server import broker_cli
        from stock_mcp_server.market_data.credential_store import (
            CredentialStore,
        )

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.kr = FakeKeyring()
        self.store = TokenStore(keyring_module=self.kr)
        self.credentials = CredentialStore(
            keyring_module=self.kr, home=self.home,
            token_store=self.store)
        self.service = broker_cli.BrokerService(
            keyring_module=self.kr, home=self.home)
        self.service.credentials = self.credentials

        self.values = {"app_key": "K1", "secret_key": "S1"}
        payload = SecretPayload.from_schema(
            registry.require("kiwoom").credential_schema, self.values)
        self.service.save_verified("kiwoom", "real", payload, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})
        self.fp = credential_fingerprint(self.values)
        self.store.save("kiwoom", "real", self.fp, "TOKEN",
                        9_999_999_999.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_disconnect_removes_the_stored_token(self):
        self.assertIsNotNone(self.store.load("kiwoom", "real", self.fp))
        report = self.service.disconnect_provider("kiwoom")
        self.assertTrue(report.ok, report.failed)
        self.assertIsNone(
            self.store.load("kiwoom", "real", self.fp),
            "연결을 끊었는데 토큰이 보안 저장소에 남아 있다")

    def test_token_removal_is_reported_separately(self):
        report = self.service.disconnect_provider("kiwoom")
        # 자격 증명 삭제와 토큰 삭제를 한 덩어리로 보고하지 않는다.
        self.assertIn("kiwoom:real", report.tokens_removed)
        self.assertTrue(report.removed)
        self.assertNotEqual(report.removed, report.tokens_removed)


if __name__ == "__main__":
    unittest.main()
