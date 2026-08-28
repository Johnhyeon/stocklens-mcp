"""능력 보고 계약 (1.1 Task 16).

capability 안내용 **도구를 만들지 않는다.** 기존 status·describe_providers
·doctor 계약을 additive 하게 넓힌다. 기존 필드는 하나도 바뀌지 않아야
구버전 Manager 가 계속 읽는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_capabilities import (
    EVIDENCE_CONTRACT_VERSION,
    GROUPS,
    evidence_projection,
)

CONNECTED = {"connected": True, "kr_intraday_state": "available",
             "us_intraday_state": "available"}
CHECKING = {"connected": True, "kr_intraday_state": "verifying",
            "us_intraday_state": "unsupported"}
OFFLINE = {"connected": False}
GROUP_STATES = {"available", "partial", "checking", "unsupported",
                "not_configured"}


class ProjectionTests(unittest.TestCase):
    def test_groups_cover_every_capability_exactly_once(self):
        seen: list[str] = []
        for members in GROUPS.values():
            seen.extend(members)
        self.assertEqual(len(seen), len(set(seen)))

    def test_every_group_reports_a_defined_state(self):
        for caps in (CONNECTED, CHECKING, OFFLINE):
            for provider in ("kis", "kiwoom"):
                groups = evidence_projection(provider, caps)[
                    "evidence_groups"]
                self.assertEqual(set(groups), set(GROUPS))
                for name, state in groups.items():
                    self.assertIn(state, GROUP_STATES, f"{name}={state}")

    def test_an_unconnected_provider_is_not_configured_everywhere(self):
        groups = evidence_projection("kis", OFFLINE)["evidence_groups"]
        for state in groups.values():
            self.assertEqual(state, "not_configured")

    def test_a_partly_served_group_is_never_reported_as_available(self):
        """여섯 중 하나만 되는데 available 이라고 하면 나머지도 될 거라 읽는다."""
        groups = evidence_projection("kis", CONNECTED)["evidence_groups"]
        self.assertNotEqual(groups["supply_pressure"], "available")

    def test_closed_release_gates_read_as_checking_not_unsupported(self):
        # 지금(2026-08-28) 증거 게이트는 전부 닫혀 있다. '지원 안 함'이
        # 아니라 '검증 중'이다. 둘을 섞으면 사용자가 영영 안 되는 줄 안다.
        groups = evidence_projection("kiwoom", CONNECTED)["evidence_groups"]
        self.assertEqual(groups["detailed_flow"], "checking")

    def test_basic_market_data_still_reflects_the_1_0_state_file(self):
        self.assertEqual(
            evidence_projection("kis", CONNECTED)["evidence_groups"][
                "basic_market_data"], "available")
        self.assertEqual(
            evidence_projection("kis", CHECKING)["evidence_groups"][
                "basic_market_data"], "checking")

    def test_the_detail_array_names_a_group_for_every_capability(self):
        detail = evidence_projection("kis", CONNECTED)[
            "evidence_capabilities"]
        self.assertTrue(detail)
        for row in detail:
            self.assertIn(row["group"], GROUPS)
            self.assertIn("state", row)

    def test_no_internal_probe_detail_is_exposed(self):
        blob = json.dumps(evidence_projection("kis", CONNECTED))
        for banned in ("internal_probe_detail", "app_key", "app_secret",
                       "token", "endpoint", "path", "host", "tr_id"):
            self.assertNotIn(banned, blob, banned)

    def test_the_contract_version_is_declared(self):
        projection = evidence_projection("kis", CONNECTED)
        self.assertEqual(projection["evidence_contract_version"],
                         EVIDENCE_CONTRACT_VERSION)


def _seed_connected(*providers: str) -> None:
    """임시 STOCKLENS_HOME 에 연결된 공급자를 심는다.

    conftest 가 모든 테스트에 임시 홈을 강제하므로, 심지 않으면
    `providers` 가 비어 반복문이 한 번도 돌지 않고 테스트가 공허하게
    통과한다. 실제로 그렇게 통과했었다.
    """
    from stock_mcp_server.market_data.connection_state import (
        connect_profile_v2,
        load_state_v2,
        save_state_v2,
    )

    state = load_state_v2()
    for provider in providers:
        state = connect_profile_v2(
            state, provider, "real", credential_ref=f"{provider}:real",
            verified=True, verified_at="2026-08-28T00:00:00+09:00",
            capabilities={"kr_intraday": "available",
                          "us_intraday": "available"})
    save_state_v2(state)


class CliContractTests(unittest.TestCase):
    def _status(self):
        from stock_mcp_server.broker_cli import BrokerService
        _seed_connected("kis", "kiwoom")
        status = BrokerService().minimal_status("kis")
        # 심은 것이 실제로 보이는지 먼저 확인한다. 비어 있으면 아래
        # 반복문들이 아무것도 검사하지 않는다.
        self.assertEqual(set(status["providers"]), {"kis", "kiwoom"})
        return status

    def test_status_keeps_every_1_0_field(self):
        status = self._status()
        for field in ("provider", "connection_generation", "active_provider",
                      "active_profile", "data_source_mode", "profiles",
                      "capability_results", "primary_provider", "providers"):
            self.assertIn(field, status, field)

    def test_status_adds_the_evidence_projection_per_provider(self):
        status = self._status()
        for record in status["providers"].values():
            # 1.0 필드는 그대로다.
            self.assertIn("release_verified", record)
            self.assertIn("kr_intraday", record["release_verified"])
            # 1.1 이 얹은 것.
            self.assertIn("evidence_groups", record)
            self.assertIn("evidence_capabilities", record)
            self.assertEqual(record["evidence_contract_version"],
                             EVIDENCE_CONTRACT_VERSION)

    def test_hidden_providers_stay_out_of_the_capability_report(self):
        # 토스는 고객 모드에서 안 보인다. 능력 보고로 다시 새면 안 된다.
        _seed_connected("toss")
        status = self._status()
        self.assertNotIn("toss", status["providers"])
        self.assertNotIn("toss", json.dumps(status))

    def test_describe_providers_carries_the_same_projection(self):
        from stock_mcp_server.market_data.provider_registry import registry
        for entry in registry.describe_public():
            # 정적 설명이라 연결 상태 없이도 능력 어휘는 노출한다.
            self.assertIn("evidence_groups", entry)
            self.assertIn("credential_fields", entry)  # 1.0 필드 유지


class DoctorContractTests(unittest.TestCase):
    def test_doctor_reports_grouped_capabilities_without_secrets(self):
        from stock_mcp_server.doctor import broker_evidence_report

        _seed_connected("kis", "kiwoom")
        report = broker_evidence_report()
        self.assertEqual(set(report["provider_connections"]),
                         {"kis", "kiwoom"})
        self.assertIn("provider_connections", report)
        blob = json.dumps(report)
        for banned in ("app_key", "app_secret", "secretkey", "Bearer",
                       "internal_probe_detail"):
            self.assertNotIn(banned, blob, banned)
        for record in report["provider_connections"].values():
            for state in record["evidence_groups"].values():
                self.assertIn(state, GROUP_STATES)

    def test_doctor_hides_experimental_providers(self):
        from stock_mcp_server.doctor import broker_evidence_report

        _seed_connected("kis", "toss")
        report = broker_evidence_report()
        self.assertIn("kis", report["provider_connections"])
        self.assertNotIn("toss", report["provider_connections"])


if __name__ == "__main__":
    unittest.main()
