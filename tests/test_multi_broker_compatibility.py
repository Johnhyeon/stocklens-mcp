"""멀티 증권사 호환성 매트릭스 (1.0 Task 23).

연결 조합(없음 / KIS만 / 키움만 / 토스만 / 셋 다 x primary 별)마다:
- auto 라우팅은 primary 하나만 선택하고 다른 증권사·Yahoo 로 새지 않는다
- CLI status 는 구 Manager(v1 KIS 소비자)가 읽는 필드를 유지한다
- doctor provider_connections 는 정확한 상태를 보고한다
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli, diagnostics
from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE_V2,
    connect_profile_v2,
    provider_capabilities_v2,
    save_state_v2,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.router import (
    RouterError,
    resolve_source,
)


def _v2_state(*providers, primary=None, mode="auto"):
    state = json.loads(json.dumps(DEFAULT_STATE_V2))
    for provider in providers:
        state = connect_profile_v2(
            state, provider, "real",
            credential_ref=f"ref-{provider}", verified=True,
            verified_at="2026-08-27T18:00:00+09:00",
            capabilities={"auth": "ok", "kr_intraday": "available",
                          "us_intraday": "available"})
    if primary is not None:
        state["primary_provider"] = primary
    state["data_source_mode"] = mode
    return state


def _caps_map(state):
    return {pid: provider_capabilities_v2(state, pid)
            for pid in registry.ids()}


_MATRIX = [
    ("none", (), None),
    ("kis_only", ("kis",), "kis"),
    ("kiwoom_only", ("kiwoom",), "kiwoom"),
    ("toss_only", ("toss",), "toss"),
    ("all_primary_kis", ("kis", "kiwoom", "toss"), "kis"),
    ("all_primary_kiwoom", ("kis", "kiwoom", "toss"), "kiwoom"),
    ("all_primary_toss", ("kis", "kiwoom", "toss"), "toss"),
]


class RoutingMatrixTests(unittest.TestCase):
    def test_auto_intraday_selects_only_primary(self):
        for name, providers, primary in _MATRIX:
            with self.subTest(case=name):
                state = _v2_state(*providers, primary=primary)
                caps = _caps_map(state)
                if primary is None:
                    with self.assertRaises(RouterError):
                        resolve_source(
                            mode="auto", market="KR", interval="5m",
                            requested_source="auto", capabilities=caps,
                            primary_provider=None)
                    res = resolve_source(
                        mode="auto", market="US", interval="5m",
                        requested_source="auto", capabilities=caps,
                        primary_provider=None)
                    self.assertEqual(res.selected_provider, "yahoo")
                    continue
                for market in ("KR", "US"):
                    res = resolve_source(
                        mode="auto", market=market, interval="5m",
                        requested_source="auto", capabilities=caps,
                        primary_provider=primary)
                    self.assertEqual(res.selected_provider, primary, name)
                    self.assertEqual(res.primary_provider, primary)

    def test_daily_stays_on_naver_yahoo_in_every_case(self):
        for name, providers, primary in _MATRIX:
            with self.subTest(case=name):
                state = _v2_state(*providers, primary=primary)
                caps = _caps_map(state)
                res = resolve_source(
                    mode="auto", market="KR", interval="1d",
                    requested_source="auto", capabilities=caps,
                    primary_provider=primary)
                self.assertEqual(res.selected_provider, "naver")
                res = resolve_source(
                    mode="auto", market="US", interval="1d",
                    requested_source="auto", capabilities=caps,
                    primary_provider=primary)
                self.assertEqual(res.selected_provider, "yahoo")

    def test_explicit_connected_non_primary_allowed_everywhere(self):
        state = _v2_state("kis", "kiwoom", "toss", primary="kis")
        caps = _caps_map(state)
        for source in ("kiwoom", "toss"):
            res = resolve_source(
                mode="auto", market="KR", interval="5m",
                requested_source=source, capabilities=caps,
                primary_provider="kis")
            self.assertEqual(res.selected_provider, source)


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


class CliStatusMatrixTests(unittest.TestCase):
    """구 Manager 는 status 의 v1 필드만 읽는다. 어떤 조합에서도
    active_provider/active_profile/profiles/capability_results 가 있다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self, provider):
        schema = registry.require(provider).credential_schema
        creds = {f.name: f"{provider}-value" for f in schema}
        resp = broker_cli.handle_request({
            "contract_version": 1, "action": "verify_and_save",
            "provider": provider, "profile": "real",
            "credentials": creds,
        }, service=self.service,
            verifier=lambda p, pr, payload: {
                "auth": "ok", "kr_intraday": "available",
                "us_intraday": "available"})
        assert resp["ok"], resp

    def test_v1_fields_present_in_every_combination(self):
        for name, providers, primary in _MATRIX:
            with self.subTest(case=name):
                for path in (self.home / "broker_state.json",):
                    if path.exists():
                        path.unlink()
                self.keyring.entries.clear()
                for provider in providers:
                    self._connect(provider)
                if primary and providers:
                    broker_cli.handle_request({
                        "contract_version": 1,
                        "action": "set_primary_provider",
                        "provider": primary}, service=self.service)
                resp = broker_cli.handle_request({
                    "contract_version": 1, "action": "status",
                    "provider": "kis"}, service=self.service)
                self.assertTrue(resp["ok"], resp)
                st = resp["status"]
                for field in ("connection_generation", "active_provider",
                              "active_profile", "data_source_mode",
                              "profiles", "capability_results"):
                    self.assertIn(field, st, name)
                self.assertEqual(st["active_provider"],
                                 primary if providers else None, name)


class DoctorMatrixTests(unittest.TestCase):
    def test_doctor_reports_each_combination(self):
        for name, providers, primary in _MATRIX:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as home:
                    state = _v2_state(*providers, primary=primary)
                    save_state_v2(state, home)
                    keyring = FakeKeyring()
                    for provider in providers:
                        keyring.entries[(
                            f"stocklens-broker-{provider}",
                            f"{provider}:real:ref-{provider}")] = "{}"
                    with patch.dict("os.environ",
                                    {"STOCKLENS_HOME": home}), \
                         patch.object(diagnostics, "_broker_keyring",
                                      return_value=keyring):
                        _, connections = diagnostics._broker_summary()
                    for pid in registry.ids():
                        expected = ("connected" if pid in providers
                                    else "not_configured")
                        self.assertEqual(
                            connections[pid]["status"], expected,
                            f"{name}/{pid}")
                        self.assertEqual(
                            connections[pid]["primary"], pid == primary,
                            f"{name}/{pid}")


if __name__ == "__main__":
    unittest.main()
