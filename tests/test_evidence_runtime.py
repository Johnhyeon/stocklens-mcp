"""증거 어댑터 구성 (1.1 Task 5).

시세 어댑터와 클라이언트를 공유해야 한다. 증거 조회 때문에 토큰을 새로
발급하면 KIS 의 '1분 1회' 제한에 그대로 걸린다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_router import (
    EvidenceRouterError,
    assert_same_generation,
)
from stock_mcp_server.market_data.runtime import ProviderRuntime


class _FakeSnapshot:
    def __init__(self, connected=True, profile="real", generation=3):
        self._connected = connected
        self._profile = profile
        self._generation = generation

    def capabilities(self, provider):
        return {"connected": self._connected}

    def active_profile(self, provider):
        return self._profile

    def provider_generation(self, provider):
        return self._generation


class _Runtime(ProviderRuntime):
    """자격 증명 저장소를 건드리지 않는 얇은 대역."""

    def __init__(self, client=object()):
        self._client = client
        self._clients = {}
        self._home = None
        self._token_store = None
        self.client_calls = []

    def client(self, provider, profile, snapshot=None):
        self.client_calls.append((provider, profile))
        return self._client


class AdapterBuildTests(unittest.TestCase):
    def test_kis_and_kiwoom_get_their_evidence_adapters(self):
        runtime = _Runtime()
        for provider, expected in (("kis", "KisEvidenceProvider"),
                                   ("kiwoom", "KiwoomEvidenceProvider")):
            adapter = runtime.evidence_provider_for(
                provider, snapshot=_FakeSnapshot())
            self.assertEqual(type(adapter).__name__, expected)
            self.assertEqual(adapter.provider_id, provider)
            self.assertEqual(adapter.profile, "real")

    def test_toss_has_no_evidence_adapter_even_when_connected(self):
        # 개발자 모드에서도 구성하지 않는다. 만들지 않은 것을 있는 것처럼
        # 두면 나중에 고객 노출 판단이 흐려진다.
        runtime = _Runtime()
        self.assertIsNone(runtime.evidence_provider_for(
            "toss", snapshot=_FakeSnapshot()))

    def test_unconnected_provider_builds_nothing(self):
        runtime = _Runtime()
        self.assertIsNone(runtime.evidence_provider_for(
            "kis", snapshot=_FakeSnapshot(connected=False)))
        self.assertEqual(runtime.client_calls, [])

    def test_missing_profile_builds_nothing(self):
        runtime = _Runtime()
        self.assertIsNone(runtime.evidence_provider_for(
            "kis", snapshot=_FakeSnapshot(profile=None)))
        self.assertEqual(runtime.client_calls, [])

    def test_missing_credentials_build_nothing(self):
        runtime = _Runtime(client=None)
        self.assertIsNone(runtime.evidence_provider_for(
            "kis", snapshot=_FakeSnapshot()))

    def test_the_bar_client_is_reused_not_reissued(self):
        # 같은 클라이언트를 그대로 쓴다. KIS 토큰 발급 1분 1회 제한.
        sentinel = object()
        runtime = _Runtime(client=sentinel)
        adapter = runtime.evidence_provider_for(
            "kis", snapshot=_FakeSnapshot())
        self.assertIs(adapter._client, sentinel)
        self.assertEqual(runtime.client_calls, [("kis", "real")])


class GenerationGuardTests(unittest.TestCase):
    def test_same_generation_passes(self):
        assert_same_generation("kis", 3, 3)

    def test_changed_generation_is_refused_not_mixed(self):
        with self.assertRaises(EvidenceRouterError) as ctx:
            assert_same_generation("kis", 3, 4)
        self.assertEqual(ctx.exception.error_code,
                         "provider_changed_during_request")
        self.assertEqual(ctx.exception.provider, "kis")


if __name__ == "__main__":
    unittest.main()
