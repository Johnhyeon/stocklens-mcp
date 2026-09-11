"""doctor·지원 파일 allowlist 테스트 (1.0 Task 19).

provider_connections 는 상태 dict 복사가 아니라 allowlist DTO 다.
상태 파일에 낯선 필드·비밀처럼 보이는 값을 주입해도 doctor 출력에
0건이어야 한다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import diagnostics
from stock_mcp_server.market_data.connection_state import state_path

_JUNK_SECRET = "PSA-INJECTED-SECRET-13579"

_ALLOWED_CONNECTION_KEYS = {
    "status", "primary", "lifecycle", "active_profile",
    "data_source_mode", "profiles", "release_verified", "storage",
}
_ALLOWED_PROFILE_KEYS = {
    "configured", "verified", "verified_at", "capabilities",
}


class FakeKeyring:
    def __init__(self):
        self.entries = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        del self.entries[(service, username)]


class SupportAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = patch.dict(
            "os.environ", {"STOCKLENS_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _write_state(self, payload: dict):
        path = state_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _doc(self) -> dict:
        keyring = FakeKeyring()
        keyring.entries[("stocklens-broker-kiwoom",
                         "kiwoom:real:ref-a")] = "{}"
        with patch.object(diagnostics, "_broker_keyring",
                          return_value=keyring):
            return diagnostics.run_diagnostics(online=False).to_dict()

    def test_injected_fields_and_secrets_never_reach_doctor(self):
        self._write_state({
            "state_version": 2,
            "routing_generation": 4,
            "primary_provider": "kiwoom",
            "data_source_mode": "auto",
            "client_secret": _JUNK_SECRET,
            "providers": {
                "kiwoom": {
                    "generation": 2,
                    "lifecycle": "connected",
                    "active_profile": "real",
                    "shell_command": "rm -rf /",
                    "profiles": {
                        "real": {
                            "credential_ref": "ref-a",
                            "verified": True,
                            "verified_at": "2026-08-27T18:00:00+09:00",
                            "app_secret": _JUNK_SECRET,
                            "capabilities": {
                                "kr_intraday": "available",
                                "sneaky": _JUNK_SECRET,
                            },
                        },
                    },
                },
            },
            "pending_operations": [],
        })
        doc = self._doc()
        text = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn(_JUNK_SECRET, text)
        self.assertNotIn("shell_command", text)
        self.assertNotIn("credential_ref", text)  # 내부 참조도 내보내지 않음
        self.assertNotIn("sneaky", text)

    def test_connection_dto_uses_only_allowlisted_keys(self):
        self._write_state({
            "state_version": 2,
            "routing_generation": 1,
            "primary_provider": "kiwoom",
            "data_source_mode": "auto",
            "providers": {
                "kiwoom": {
                    "generation": 1,
                    "lifecycle": "connected",
                    "active_profile": "real",
                    "profiles": {
                        "real": {"credential_ref": "ref-a",
                                 "verified": True,
                                 "verified_at": None,
                                 "capabilities": {}},
                    },
                },
            },
            "pending_operations": [],
        })
        doc = self._doc()
        for pid, connection in doc["provider_connections"].items():
            self.assertLessEqual(
                set(connection), _ALLOWED_CONNECTION_KEYS, pid)
            for name, profile in connection.get("profiles", {}).items():
                self.assertLessEqual(
                    set(profile), _ALLOWED_PROFILE_KEYS, f"{pid}/{name}")


if __name__ == "__main__":
    unittest.main()
