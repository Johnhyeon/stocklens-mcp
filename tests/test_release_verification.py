"""출시 검증 게이트 분리 테스트 (1.0 리뷰 차단 항목 2).

연결 시험의 available(=키·API 호출 정상)과 출시 검증 완료는 다른 것이다.
자동 라우터 활성화는 코드에 고정된 출시 검증 표(_RELEASE_VERIFIED)를
함께 통과한 능력만 쓴다. 표 항목을 켜는 커밋은 해당 UAT 증거와 짝이다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data import provider_registry
from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE_V2,
    connect_profile_v2,
    provider_capabilities_v2,
)
from stock_mcp_server.market_data.provider_registry import (
    is_release_verified,
)


def _v2_state(*providers, primary=None):
    state = json.loads(json.dumps(DEFAULT_STATE_V2))
    for provider in providers:
        state = connect_profile_v2(
            state, provider, "real",
            credential_ref=f"ref-{provider}", verified=True,
            verified_at="2026-08-27T18:00:00+09:00",
            capabilities={"auth": "ok", "kr_intraday": "available",
                          "us_intraday": "available"})
    if primary:
        state["primary_provider"] = primary
    state["data_source_mode"] = "auto"
    return state


class ReleaseGateTableTests(unittest.TestCase):
    def test_current_honest_table(self):
        # 지금 시점의 정직한 상태: KIS KR·US 만 출시 검증 완료다.
        # (KIS US: 2026-08-28 strict 러너 15사례 failures=0,
        #  검산 9,690 버킷 불일치 0 - uat_kis_us_20260828.json)
        self.assertTrue(is_release_verified("kis", "kr_intraday"))
        self.assertTrue(is_release_verified("kis", "us_intraday"))
        # 키움 KR: 장마감+장중 strict 러너 2회 통과 (2026-08-27/28).
        # 키움 US: 2026-08-28 strict 러너 failures=0 + 완결일 KIS 교차
        # 완전 일치. 토스는 대표 결정으로 1.0 시세 계약 밖 (미지원).
        self.assertTrue(is_release_verified("kiwoom", "kr_intraday"))
        self.assertTrue(is_release_verified("kiwoom", "us_intraday"))
        self.assertFalse(is_release_verified("toss", "kr_intraday"))
        self.assertFalse(is_release_verified("toss", "us_intraday"))
        # 일·주·월봉은 수정주가 게이트 전이라 전부 미검증이다.
        for provider in ("kis", "kiwoom", "toss"):
            self.assertFalse(is_release_verified(provider, "kr_daily"))
            self.assertFalse(is_release_verified(provider, "us_daily"))

    def test_unknown_keys_default_false(self):
        self.assertFalse(is_release_verified("evil", "kr_intraday"))
        self.assertFalse(is_release_verified("kis", "teleport"))


class RouterDistinctionTests(unittest.TestCase):
    """리뷰 잔여 3: 사용자 경로에서 미연결·검증 중·미지원을 구분한다.

    토스 KR 처럼 provider 단계의 unsupported 가 라우터의 not_configured
    에 가려지면 사용자는 키 문제로 오해한다.
    """

    def _resolve(self, caps, source):
        from stock_mcp_server.market_data.router import resolve_source
        return resolve_source(
            mode="auto", market="KR", interval="5m",
            requested_source=source, capabilities={source: caps},
            primary_provider=None)

    def test_not_connected_stays_not_configured(self):
        from stock_mcp_server.market_data.router import RouterError
        with self.assertRaises(RouterError) as ctx:
            self._resolve({"connected": False, "kr_intraday": False,
                           "kr_intraday_state": "unknown"}, "toss")
        self.assertEqual(ctx.exception.provider_status, "not_configured")
        self.assertIn("연결", str(ctx.exception))

    def test_connected_but_market_unsupported_is_unsupported(self):
        from stock_mcp_server.market_data.router import RouterError
        state = _v2_state("toss")
        state["providers"]["toss"]["profiles"]["real"]["capabilities"][
            "kr_intraday"] = "unavailable"
        caps = provider_capabilities_v2(state, "toss")
        self.assertEqual(caps["kr_intraday_state"], "unsupported")
        with self.assertRaises(RouterError) as ctx:
            self._resolve(caps, "toss")
        self.assertEqual(ctx.exception.provider_status, "unsupported")
        self.assertIn("1.0 시세 계약에서 지원하지 않", str(ctx.exception))
        self.assertIn("키나 연결 문제가 아닙니다", str(ctx.exception))

    def test_connected_endpoint_ok_but_unverified_is_verifying(self):
        # 게이트가 닫혀 있으면서 endpoint 기록이 available 인 가상 상태
        # (실제 토스 검증기는 unavailable 로 고정하지만, 상태 계산의
        # verifying 분기는 계약으로 유지한다).
        from stock_mcp_server.market_data.router import RouterError
        state = _v2_state("toss")
        caps = provider_capabilities_v2(state, "toss")
        self.assertEqual(caps["kr_intraday_state"], "verifying")
        with self.assertRaises(RouterError) as ctx:
            self._resolve(caps, "toss")
        self.assertEqual(ctx.exception.provider_status, "unsupported")
        self.assertIn("검증", str(ctx.exception))
        self.assertIn("키 문제가 아닙니다", str(ctx.exception))

    def test_fully_verified_state_is_available(self):
        state = _v2_state("kis")
        caps = provider_capabilities_v2(state, "kis")
        self.assertEqual(caps["kr_intraday_state"], "available")


class RouterGatingTests(unittest.TestCase):
    def test_endpoint_available_alone_does_not_activate(self):
        # 연결 시험은 통과했지만(available) 출시 검증 전인 능력은
        # 라우터에 False 로 보인다. (토스는 게이트가 닫혀 있는 유일한
        # 공급자다 - 1.0 시세 계약 밖.)
        state = _v2_state("toss")
        caps = provider_capabilities_v2(state, "toss")
        self.assertTrue(caps["connected"])
        self.assertFalse(caps["kr_intraday"])
        self.assertFalse(caps["us_intraday"])

    def test_release_verified_capability_activates(self):
        state = _v2_state("kis")
        caps = provider_capabilities_v2(state, "kis")
        self.assertTrue(caps["connected"])
        self.assertTrue(caps["kr_intraday"])  # 검증 완료
        self.assertTrue(caps["us_intraday"])  # 2026-08-27 본장 UAT 완료
        # 게이트가 닫힌 능력은 endpoint available 이어도 꺼져 있다.
        ts = _v2_state("toss")
        self.assertFalse(provider_capabilities_v2(ts, "toss")[
            "kr_intraday"])

    def test_gate_flip_activates_without_state_change(self):
        state = _v2_state("toss")
        with patch.dict(provider_registry._RELEASE_VERIFIED, {
                ("toss", "kr_intraday"): True}):
            caps = provider_capabilities_v2(state, "toss")
        self.assertTrue(caps["kr_intraday"])

    def test_endpoint_unavailable_stays_off_even_if_gate_open(self):
        # 게이트가 열려도 연결 시험이 unavailable 이면 켜지지 않는다.
        state = _v2_state("kis")
        state["providers"]["kis"]["profiles"]["real"]["capabilities"][
            "kr_intraday"] = "unavailable"
        caps = provider_capabilities_v2(state, "kis")
        self.assertFalse(caps["kr_intraday"])


class FakeKeyring:
    def __init__(self):
        self.entries = {}

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        del self.entries[(service, username)]


class StatusExposureTests(unittest.TestCase):
    def setUp(self):
        self._experimental = patch.dict(
            "os.environ", {"LEETKIT_ENABLE_EXPERIMENTAL_BROKERS": "1"})
        self._experimental.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.service = broker_cli.BrokerService(
            keyring_module=self.keyring, home=self.home)

    def tearDown(self):
        self._tmp.cleanup()
        self._experimental.stop()

    def _connect(self, provider, profile="real"):
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload
        schema = registry.require(provider).credential_schema
        payload = SecretPayload.from_schema(
            schema, {f.name: "v" for f in schema})
        self.service.save_verified(provider, profile, payload, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def test_status_separates_endpoint_and_release(self):
        self._connect("kiwoom")
        resp = broker_cli.handle_request({
            "contract_version": 1, "action": "status",
            "provider": "kiwoom"}, service=self.service)
        entry = resp["status"]["providers"]["kiwoom"]
        self.assertEqual(entry["release_verified"], {
            "kr_intraday": True, "us_intraday": True})
        # 연결 시험 값(endpoint)은 그대로 available 로 남는다.
        self.assertEqual(
            resp["status"]["capability_results"]["real"]["kr_intraday"],
            "available")

    def test_mixed_profiles_per_provider_active(self):
        # 리뷰 재현 케이스의 데이터 계약: KIS demo 활성 + 토스 real 활성.
        self._connect("kis", "demo")
        self._connect("toss", "real")
        resp = broker_cli.handle_request({
            "contract_version": 1, "action": "status",
            "provider": "toss"}, service=self.service)
        st = resp["status"]
        # top-level 은 primary(kis) 호환 필드다.
        self.assertEqual(st["active_provider"], "kis")
        self.assertEqual(st["active_profile"], "demo")
        # provider 별 진실은 providers 맵에 있다.
        self.assertEqual(st["providers"]["toss"]["active_profile"], "real")
        self.assertEqual(st["providers"]["kis"]["active_profile"], "demo")


if __name__ == "__main__":
    unittest.main()
