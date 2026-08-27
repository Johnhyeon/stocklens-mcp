"""멀티 증권사 CLI 계약 테스트 (1.0 Task 6).

- 레지스트리 기반 dispatch: 공급자 목록을 CLI 가 복제하지 않는다
- 공급자별 credential schema 로 입력을 검증한다
- 첫 저장 공급자만 primary, 추가 저장은 primary 유지
- set_primary_provider 는 검증된 연결을 요구한다
- 기존 contract v1 KIS 요청은 그대로 동작한다
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli

SENTINEL_KEY = "PSA-MULTI-APP-KEY-777"
SENTINEL_SECRET = "PSA-MULTI-APP-SECRET-777"


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


def _ok_verifier(seen=None):
    def _verify(provider, profile, payload):
        if seen is not None:
            seen.append((provider, profile, payload))
        return {"auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"}
    return _verify


_CREDS = {
    "kis": {"app_key": SENTINEL_KEY, "app_secret": SENTINEL_SECRET},
    "kiwoom": {"app_key": SENTINEL_KEY, "secret_key": SENTINEL_SECRET},
    "toss": {"client_id": SENTINEL_KEY, "client_secret": SENTINEL_SECRET},
}


class MultiProviderCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _handle(self, request, verifier=None):
        return broker_cli.handle_request(
            request, service=self.service, verifier=verifier)

    def _save(self, provider, profile="real", verifier=None):
        return self._handle({
            "contract_version": 1, "action": "verify_and_save",
            "provider": provider, "profile": profile,
            "credentials": dict(_CREDS[provider]),
        }, verifier=verifier or _ok_verifier())

    def test_describe_providers_returns_public_schema_only(self):
        resp = self._handle({
            "contract_version": 1, "action": "describe_providers",
            "provider": "kis",
        })
        self.assertTrue(resp["ok"])
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn("factory", text)
        self.assertNotIn("allowed_hosts", text)
        ids = [p["provider_id"] for p in resp["providers"]]
        self.assertEqual(ids, ["kis", "kiwoom", "toss"])
        kiwoom = resp["providers"][1]
        self.assertEqual(
            [f["name"] for f in kiwoom["credential_fields"]],
            ["app_key", "secret_key"])
        self.assertTrue(kiwoom["signup_url"].startswith("https://"))

    def test_verify_uses_provider_specific_credentials(self):
        seen = []
        resp = self._handle({
            "contract_version": 1, "action": "verify",
            "provider": "kiwoom", "profile": "real",
            "credentials": {"app_key": SENTINEL_KEY,
                            "secret_key": SENTINEL_SECRET},
        }, verifier=_ok_verifier(seen))
        self.assertTrue(resp["ok"], resp)
        provider, profile, payload = seen[0]
        self.assertEqual(provider, "kiwoom")
        self.assertEqual(payload.field_names(), ("app_key", "secret_key"))

        # KIS schema 필드를 kiwoom 에 넣으면 어떤 변경도 없이 거부된다.
        resp = self._handle({
            "contract_version": 1, "action": "verify",
            "provider": "kiwoom", "profile": "real",
            "credentials": {"app_key": SENTINEL_KEY,
                            "app_secret": SENTINEL_SECRET},
        }, verifier=_ok_verifier())
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_request")
        self.assertNotIn(SENTINEL_SECRET, json.dumps(resp))

    def test_first_saved_provider_becomes_primary(self):
        resp = self._save("kiwoom")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"]["primary_provider"], "kiwoom")
        self.assertEqual(resp["status"]["active_provider"], "kiwoom")

    def test_second_saved_provider_does_not_change_primary(self):
        self._save("kis")
        resp = self._save("toss")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"]["primary_provider"], "kis")
        self.assertIn("toss", resp["status"]["providers"])

    def test_set_primary_provider_requires_verified_connection(self):
        self._save("kis")
        resp = self._handle({
            "contract_version": 1, "action": "set_primary_provider",
            "provider": "toss",
        })
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "provider_not_verified")

        self._save("toss")
        resp = self._handle({
            "contract_version": 1, "action": "set_primary_provider",
            "provider": "toss",
        })
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"]["primary_provider"], "toss")

    def test_status_returns_all_providers_without_secrets(self):
        self._save("kis")
        self._save("kiwoom")
        resp = self._handle({
            "contract_version": 1, "action": "status", "provider": "kis"})
        self.assertTrue(resp["ok"])
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn(SENTINEL_SECRET, text)
        providers = resp["status"]["providers"]
        self.assertEqual(set(providers), {"kis", "kiwoom"})
        self.assertEqual(providers["kiwoom"]["lifecycle"], "connected")
        self.assertIn("real", providers["kiwoom"]["verified_profiles"])

    def test_unsupported_profile_for_provider_is_rejected(self):
        resp = self._handle({
            "contract_version": 1, "action": "verify_and_save",
            "provider": "toss", "profile": "demo",
            "credentials": dict(_CREDS["toss"]),
        }, verifier=_ok_verifier())
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_request")

    def test_recover_cleanup_removes_orphan_slots(self):
        self._save("kiwoom")
        # commit 없이 stage 만 된 orphan 을 만든다.
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload
        payload = SecretPayload.from_schema(
            registry.require("kiwoom").credential_schema,
            {"app_key": "another-key", "secret_key": "another-secret"})
        self.service.credentials.stage("kiwoom", "real", payload)

        resp = self._handle({
            "contract_version": 1, "action": "recover_cleanup",
            "provider": "kiwoom",
        })
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["recovered"]["removed"])
        self.assertFalse(resp["recovered"]["failed"])

    def test_contract_v1_kis_request_still_works(self):
        # Manager v1 이 보내는 요청 그대로. 응답의 기존 필드가 유지된다.
        resp = self._save("kis")
        self.assertTrue(resp["ok"])
        st = resp["status"]
        self.assertEqual(st["active_provider"], "kis")
        self.assertEqual(st["active_profile"], "real")
        self.assertTrue(st["profiles"]["real"]["configured"])
        self.assertFalse(st["profiles"]["demo"]["configured"])
        self.assertEqual(
            st["capability_results"]["real"]["kr_intraday"], "available")
        self.assertIn("connection_generation", st)
        self.assertEqual(resp["verification"]["auth"], "ok")

        resp = self._handle({
            "contract_version": 1, "action": "set_data_source_mode",
            "provider": "kis", "mode": "auto"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"]["data_source_mode"], "auto")

        resp = self._handle({
            "contract_version": 1, "action": "status", "provider": "kis"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["contract_version"], 1)


if __name__ == "__main__":
    unittest.main()
