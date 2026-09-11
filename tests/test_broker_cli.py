"""stocklens-broker CLI 계약 테스트 (Task 5, 1.0 Task 6에서 v2 백엔드 포팅).

- stdin JSON 하나를 받아 JSON 문서 하나만 출력한다 (뒤에 사람용 텍스트 금지)
- 알 수 없는 contract_version·action 은 어떤 변경도 없이 실패한다
- 응답에 비밀값, 길이, prefix, suffix, hash 를 포함하지 않는다
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.connection_state import load_state_v2

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-111"
SENTINEL_SECRET = "PSA-SENTINEL-APP-SECRET-111"


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


def _assert_no_secret(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for banned in (SENTINEL_KEY, SENTINEL_SECRET,
                   SENTINEL_KEY[:8], SENTINEL_SECRET[:8]):
        assert banned not in text, f"응답에 비밀 흔적: {banned}"


def _ok_verifier(provider_expected="kis"):
    def _verify(provider, profile, payload):
        assert provider == provider_expected
        return {"auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"}
    return _verify


class BrokerCliHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _handle(self, request: dict, verifier=None) -> dict:
        return broker_cli.handle_request(
            request, service=self.service, verifier=verifier)

    def _base(self, action: str, **extra) -> dict:
        req = {"contract_version": 1, "action": action, "provider": "kis"}
        req.update(extra)
        return req

    def _save(self, profile="real",
              key=SENTINEL_KEY, secret=SENTINEL_SECRET):
        resp = self._handle(self._base(
            "verify_and_save", profile=profile,
            credentials={"app_key": key, "app_secret": secret}),
            verifier=_ok_verifier())
        assert resp["ok"], resp
        return resp

    def _gen(self) -> int:
        return load_state_v2(self.home)["routing_generation"]

    # --- 계약 검증 ---

    def test_unknown_contract_version_fails_without_mutation(self):
        self._save()
        gen = self._gen()
        resp = self._handle({"contract_version": 999, "action": "status",
                             "provider": "kis"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "unsupported_contract_version")
        self.assertEqual(self._gen(), gen)

    def test_unknown_action_fails_without_mutation(self):
        gen = self._gen()
        resp = self._handle(self._base("format_disk"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "unknown_action")
        self.assertEqual(self._gen(), gen)

    def test_unknown_provider_rejected(self):
        resp = self._handle(self._base("status", provider="unknown_sec"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_request")

    # --- status ---

    def test_status_reports_profiles_without_secrets(self):
        self._save()
        resp = self._handle(self._base("status"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["contract_version"], 1)
        st = resp["status"]
        self.assertEqual(st["active_profile"], "real")
        self.assertTrue(st["profiles"]["real"]["configured"])
        self.assertFalse(st["profiles"]["demo"]["configured"])
        _assert_no_secret(resp)

    # --- 무네트워크 변경 액션 ---

    def test_switch_profile(self):
        self._save(profile="real")
        self._save(profile="demo", key="k2", secret="s2")
        resp = self._handle(self._base("switch_profile", profile="real"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"]["active_profile"], "real")

    def test_switch_to_unconfigured_profile_fails(self):
        resp = self._handle(self._base("switch_profile", profile="demo"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "profile_not_configured")

    def test_disconnect_profile_and_provider(self):
        self._save(profile="real")
        self._save(profile="demo", key="k2", secret="s2")

        resp = self._handle(self._base("disconnect_profile", profile="demo"))
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["status"]["profiles"]["demo"]["configured"])
        self.assertTrue(resp["status"]["profiles"]["real"]["configured"])

        resp = self._handle(self._base("disconnect_provider"))
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["status"]["profiles"]["real"]["configured"])
        self.assertIsNone(resp["status"]["active_profile"])

    def test_set_data_source_mode(self):
        resp = self._handle(self._base("set_data_source_mode", mode="auto"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"]["data_source_mode"], "auto")

        resp = self._handle(self._base("set_data_source_mode", mode="bogus"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_request")

    # --- 검증 액션은 verifier 주입 전까지 통제된 unavailable ---

    def test_verify_without_verifier_is_controlled_unavailable(self):
        for action in ("verify", "verify_and_save"):
            resp = self._handle(self._base(
                action, profile="real",
                credentials={"app_key": SENTINEL_KEY,
                             "app_secret": SENTINEL_SECRET}))
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], "verifier_unavailable")
            _assert_no_secret(resp)
        # 저장되지 않았어야 한다.
        self.assertFalse(self.service.has_profile("kis", "real"))

    def test_missing_credentials_for_verify_is_invalid_request(self):
        resp = self._handle(self._base("verify", profile="real"),
                            verifier=_ok_verifier())
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_request")


class BrokerCliMainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_main(self, request: dict) -> tuple[int, str]:
        stdin = io.StringIO(json.dumps(request))
        stdout = io.StringIO()
        service = broker_cli.BrokerService(
            keyring_module=FakeKeyring(), home=self.home)
        with patch.object(sys, "stdin", stdin), \
             patch.object(sys, "stdout", stdout), \
             patch.object(broker_cli, "_default_service",
                          return_value=service):
            code = broker_cli.main(
                ["--json", "--non-interactive", "--stdin"])
        return code, stdout.getvalue()

    def test_main_outputs_exactly_one_json_document(self):
        code, out = self._run_main(
            {"contract_version": 1, "action": "status", "provider": "kis"})
        self.assertEqual(code, 0)
        parsed = json.loads(out)  # 뒤에 다른 텍스트가 있으면 여기서 죽는다
        self.assertTrue(parsed["ok"])

    def test_main_invalid_stdin_json(self):
        stdin = io.StringIO("{not json")
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), \
             patch.object(sys, "stdout", stdout):
            code = broker_cli.main(["--json", "--non-interactive", "--stdin"])
        self.assertNotEqual(code, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "invalid_request")

    def test_main_error_exit_code_nonzero(self):
        code, out = self._run_main(
            {"contract_version": 42, "action": "status", "provider": "kis"})
        self.assertNotEqual(code, 0)
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])


if __name__ == "__main__":
    unittest.main()
