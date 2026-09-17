"""키를 꺼내지 못한 두 갈래를 구분해 안내한다 (2026-09-17 실측).

1. 키 저장소를 못 연다 (KeychainUnavailableError). 예전에는 분봉·상세수급
   도구 밖으로 예외가 그대로 새어 고객 화면에 날것 오류가 떴다.
2. 연결 기록이 가리키는 키가 없다. 예전 안내는 이것도 "저장소를 열지
   못했다"고 해서 원인을 거꾸로 말했다.

그리고 상태 조회(Manager·doctor)는 분봉이 실제로 꺼내는 슬롯만 본다.
연결 기록의 슬롯이 없는데 v1 고정 슬롯이 남아 있으면, 상태는 connected
인데 분봉은 not_configured 였다.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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
    save_state_v2,
)
from stock_mcp_server.market_data.credential_store import (
    ISSUE_KEY_MISSING,
    ISSUE_STORE_UNAVAILABLE,
    credential_issue_message,
)
from stock_mcp_server.market_data.evidence_router import EvidenceRouterError
from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.router import (
    RouterError,
    SourceResolution,
    fetch_with_failover,
)
from stock_mcp_server.market_data.runtime import ProviderRuntime


class FakeKeyring:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        self.entries.pop((service, username), None)


class DeadKeyring(FakeKeyring):
    """PYTHON_KEYRING_BACKEND 가 설치 안 된 백엔드를 가리킬 때와 같은 모양."""

    def get_password(self, service, username):
        raise ModuleNotFoundError("No module named 'keyrings'")


class NullTokenStore:
    def load(self, *args, **kwargs):
        return None

    def save(self, *args, **kwargs):
        return None

    def delete(self, *args, **kwargs):
        return False


def _resolution(provider: str) -> SourceResolution:
    return SourceResolution(
        requested_source="auto",
        selected_provider=provider,
        selection_reason="broker_connected_and_intraday_supported",
        fallback_allowed_before_first_bar=False,
        mode="auto",
        capability_version=1,
        primary_provider=provider,
    )


def _connected_home(home: Path, ref: str = "ref-kis") -> None:
    # conftest 가 격리 홈에 둔 라이선스 파일을 옮겨야 도구가 라이선스 게이트를 지난다.
    source = Path(os.environ.get("STOCKLENS_HOME", ""))
    for name in ("license.key", "revoked_cache.json", "clock_seen"):
        if (source / name).exists():
            shutil.copy2(source / name, home / name)
    state = json.loads(json.dumps(DEFAULT_STATE_V2))
    state = connect_profile_v2(
        state, "kis", "real", credential_ref=ref, verified=True,
        verified_at="2026-09-17T09:00:00+09:00",
        capabilities={"auth": "ok", "kr_intraday": "available",
                      "us_intraday": "available"})
    state["primary_provider"] = "kis"
    state["data_source_mode"] = "auto"
    save_state_v2(state, home)


class MessageTests(unittest.TestCase):
    def test_key_missing_says_reconnect_not_store_failure(self):
        message = credential_issue_message("kis", ISSUE_KEY_MISSING)
        self.assertIn("한국투자증권", message)
        self.assertIn("다시 연결", message)
        self.assertNotIn("키 저장소", message)
        self.assertIn("[지원 문의]", message)

    def test_store_unavailable_says_store_and_key_is_fine(self):
        message = credential_issue_message("kiwoom", ISSUE_STORE_UNAVAILABLE)
        self.assertIn("키움증권", message)
        self.assertIn("키 저장소", message)
        self.assertIn("키가 틀린 것은 아닙니다", message)
        self.assertIn("[지원 문의]", message)

    def test_no_technical_names_reach_the_customer(self):
        for issue in (ISSUE_KEY_MISSING, ISSUE_STORE_UNAVAILABLE, None):
            message = credential_issue_message("kis", issue)
            for banned in ("Keychain", "ModuleNotFound", "keyring", "kis "):
                self.assertNotIn(banned, message)

    def test_router_uses_the_issue(self):
        with self.assertRaises(RouterError) as ctx:
            asyncio.run(fetch_with_failover(
                _resolution("kis"), providers={"naver": object()},
                request=None, credential_issue=ISSUE_STORE_UNAVAILABLE))
        self.assertEqual(ctx.exception.provider_status, "not_configured")
        self.assertIn("키 저장소", str(ctx.exception))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        _connected_home(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _runtime(self, keyring) -> ProviderRuntime:
        return ProviderRuntime(keyring_module=keyring, home=self.home,
                               token_store=NullTokenStore())

    def test_dead_store_gives_none_and_reason_not_exception(self):
        runtime = self._runtime(DeadKeyring())
        self.assertIsNone(runtime.client("kis", "real"))
        self.assertEqual(runtime.credential_issue("kis"),
                         ISSUE_STORE_UNAVAILABLE)
        # 분봉 공급자 구성도 예외 없이 기본 공급원만 준다.
        self.assertNotIn("kis", runtime.providers_for("KR"))

    def test_missing_slot_gives_key_missing(self):
        runtime = self._runtime(FakeKeyring())
        self.assertIsNone(runtime.client("kis", "real"))
        self.assertEqual(runtime.credential_issue("kis"), ISSUE_KEY_MISSING)

    def test_reason_clears_after_a_good_read(self):
        keyring = FakeKeyring()
        runtime = self._runtime(keyring)
        runtime.client("kis", "real")
        keyring.set_password(
            "stocklens-broker-kis", "kis:real:ref-kis",
            json.dumps({"app_key": "k", "app_secret": "s"}))
        self.assertIsNotNone(runtime.client("kis", "real"))
        self.assertIsNone(runtime.credential_issue("kis"))

    def test_evidence_adapter_explains_dead_store(self):
        runtime = self._runtime(DeadKeyring())
        service = EvidenceService(runtime=runtime, public_providers=("kis",))
        with self.assertRaises(EvidenceRouterError) as ctx:
            service._adapter(runtime, runtime.snapshot(), "kis")
        self.assertEqual(ctx.exception.provider_status, "not_configured")
        self.assertIn("키 저장소", str(ctx.exception))

    def test_intraday_tool_returns_guidance_on_dead_store(self):
        from stock_mcp_server import server

        runtime = self._runtime(DeadKeyring())
        with patch.dict(os.environ, {"STOCKLENS_HOME": str(self.home)}), \
                patch.object(server, "_PROVIDER_RUNTIME", runtime):
            result = asyncio.run(server.get_intraday_chart(
                "005930", market="KR", interval="5m", source="auto"))
        self.assertIsInstance(result, str)
        self.assertIn("키 저장소", result)
        self.assertIn("[지원 문의]", result)
        self.assertIn('"provider_status": "not_configured"', result)
        self.assertNotIn("KeychainUnavailableError", result)


class StatusMatchesDataPathTests(unittest.TestCase):
    """연결 기록의 슬롯이 없고 v1 고정 슬롯만 남은 홈 (9/17 개발자 PC 상태)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        _connected_home(self.home, ref="gone-ref")
        self.keyring = FakeKeyring({
            ("stocklens-broker-kis", "kis:real"): json.dumps(
                {"app_key": "old", "app_secret": "old"}),
        })
        self._env = patch.dict(os.environ, {"STOCKLENS_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_manager_status_does_not_claim_configured(self):
        service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)
        self.assertFalse(service.has_profile("kis", "real"))

    def test_doctor_does_not_claim_connected(self):
        with patch.object(diagnostics, "_broker_keyring",
                          return_value=self.keyring):
            doc = diagnostics.run_diagnostics(online=False).to_dict()
        kis = doc["provider_connections"]["kis"]
        self.assertNotEqual(kis["status"], "connected")
        self.assertFalse(kis["profiles"]["real"]["configured"])

    def test_data_path_agrees(self):
        runtime = ProviderRuntime(keyring_module=self.keyring, home=self.home,
                                  token_store=NullTokenStore())
        self.assertIsNone(runtime.client("kis", "real"))

    def test_slot_present_is_still_connected(self):
        self.keyring.set_password(
            "stocklens-broker-kis", "kis:real:gone-ref",
            json.dumps({"app_key": "k", "app_secret": "s"}))
        service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)
        self.assertTrue(service.has_profile("kis", "real"))
        with patch.object(diagnostics, "_broker_keyring",
                          return_value=self.keyring):
            doc = diagnostics.run_diagnostics(online=False).to_dict()
        self.assertEqual(doc["provider_connections"]["kis"]["status"],
                         "connected")
