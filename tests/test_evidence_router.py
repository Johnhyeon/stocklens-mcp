"""상세 수급 라우팅 (1.1 Task 5).

1.0 시세 라우팅과 같은 계약을 그대로 따른다:
- 한 요청은 **공급자 하나**에 고정된다. 자동으로 갈아타지 않는다.
- 명시 source 는 strict 다. 실패해도 다른 공급자로 넘어가지 않는다.
- 못 하는 것은 '빈 성공'이 아니라 사유가 있는 미지원이다.
- 능력은 두 축이 **모두** 통과해야 켜진다. 하나로 합치지 않는다.
  1) 공급자가 실제로 그 데이터를 주는가 (어댑터 실측표)
  2) 실계좌 UAT·출시 게이트를 통과했는가 (코드 고정표)

1.1 이 1.0 과 다른 점 하나: 증거 능력은 **상태 파일에 저장하지 않는다.**
연결 시험이 기록하는 것은 1.0 의 분봉 능력뿐이고, 증거 능력은 어댑터가
실측으로 확정한 표에서 읽는다. 상태 파일 스키마를 올리지 않았으므로
1.0.0 으로 되돌려도 상태 파일이 그대로 읽힌다 (아래 롤백 테스트).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_router import (
    EVIDENCE_CAPABILITIES,
    EvidenceRouterError,
    capability_state,
    resolve_evidence_source,
)

CONNECTED = {"connected": True}
DISCONNECTED = {"connected": False}


def _caps(**by_provider) -> dict:
    return {k: (CONNECTED if v else DISCONNECTED)
            for k, v in by_provider.items()}


class VocabularyTests(unittest.TestCase):
    def test_capability_names_cover_flow_and_every_pressure_kind(self):
        self.assertIn("kr_investor_flow", EVIDENCE_CAPABILITIES)
        for kind in ("program_trading", "short_selling", "credit",
                     "securities_lending", "foreign_holding", "cfd"):
            self.assertIn(f"kr_{kind}", EVIDENCE_CAPABILITIES)

    def test_names_do_not_collide_with_the_1_0_bar_capabilities(self):
        # 같은 이름을 쓰면 분봉 게이트를 여는 커밋이 증거까지 함께 연다.
        for existing in ("kr_intraday", "us_intraday", "kr_daily",
                         "us_daily"):
            self.assertNotIn(existing, EVIDENCE_CAPABILITIES)


class CapabilityStateTests(unittest.TestCase):
    def test_unconnected_provider_is_not_configured_not_unsupported(self):
        # 키 문제와 지원 문제를 섞으면 사용자가 멀쩡한 키를 의심한다.
        state = capability_state("kis", "kr_investor_flow", DISCONNECTED)
        self.assertEqual(state, "not_configured")

    def test_provider_that_cannot_do_it_is_unsupported(self):
        # 실측: KIS 는 종목별 대차거래를 주지 않는다 (시장 전체 값뿐).
        self.assertEqual(
            capability_state("kis", "kr_securities_lending", CONNECTED),
            "unsupported")
        # 실측: 키움 REST 에는 CFD 조회 TR 이 없다.
        self.assertEqual(
            capability_state("kiwoom", "kr_cfd", CONNECTED), "unsupported")

    def test_supported_but_ungated_is_verifying_not_available(self):
        # 두 축을 하나로 합치면 이 상태가 사라진다. 공급자가 준다는 것과
        # 출시해도 된다는 것은 다른 사실이다.
        state = capability_state("kiwoom", "kr_short_selling", CONNECTED)
        self.assertIn(state, ("verifying", "available"))
        from stock_mcp_server.market_data.provider_registry import (
            is_release_verified,
        )
        expected = ("available"
                    if is_release_verified("kiwoom", "kr_short_selling")
                    else "verifying")
        self.assertEqual(state, expected)

    def test_unknown_capability_is_unsupported_never_available(self):
        self.assertEqual(
            capability_state("kis", "kr_teleport", CONNECTED), "unsupported")


class AutoRoutingTests(unittest.TestCase):
    def test_auto_uses_the_primary_provider_only(self):
        # 키움이 못 하는 능력을 KIS 가 할 수 있어도 자동으로 갈아타지
        # 않는다. 사용자가 지정하지 않은 공급자로 넘어가지 않는 것이
        # 1.0 계약이다.
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_cfd", requested_source="auto",
                capabilities=_caps(kiwoom=True, kis=True),
                primary_provider="kiwoom")
        self.assertEqual(ctx.exception.error_code,
                         "unsupported_by_selected_provider")
        self.assertEqual(ctx.exception.provider, "kiwoom")

    def test_auto_without_a_primary_is_not_configured(self):
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_investor_flow", requested_source="auto",
                capabilities={}, primary_provider=None)
        self.assertEqual(ctx.exception.provider_status, "not_configured")

    def test_the_message_names_a_working_alternative_without_switching(self):
        """이미 연결된 다른 증권사가 할 수 있으면 사실만 알려준다.

        갈아타지는 않는다. 하지만 사용자가 이미 연결해 둔 증권사로
        가능한 일을 '불가능'이라고만 말하면 그것도 정확하지 않다.

        실측 예: 종목별 대차거래는 키움만 준다. KIS 는 시장 전체 값뿐이라
        종목별 요청의 답으로 쓸 수 없다.
        """
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_securities_lending", requested_source="auto",
                capabilities=_caps(kis=True, kiwoom=True),
                primary_provider="kis", release_override=True)
        self.assertEqual(ctx.exception.alternative, "kiwoom")
        self.assertIn("kiwoom", str(ctx.exception))
        # 안내일 뿐 전환이 아니다. 선택된 공급자는 그대로 kis 다.
        self.assertEqual(ctx.exception.provider, "kis")

    def test_an_alternative_still_in_verification_is_not_named(self):
        """검증 중인 공급자로 안내하면 거기서도 같은 거절을 받는다.

        출시 게이트가 전부 닫힌 지금이 바로 이 상태다. 안 되는 길을
        권하는 것은 아무 말도 안 하는 것보다 나쁘다.
        """
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_securities_lending", requested_source="auto",
                capabilities=_caps(kis=True, kiwoom=True),
                primary_provider="kis")
        self.assertIsNone(ctx.exception.alternative)
        self.assertNotIn("kiwoom", str(ctx.exception))

    def test_no_alternative_is_named_when_none_is_connected(self):
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_securities_lending", requested_source="auto",
                capabilities=_caps(kis=True), primary_provider="kis",
                release_override=True)
        self.assertIsNone(ctx.exception.alternative)


class ExplicitSourceTests(unittest.TestCase):
    def test_explicit_source_is_strict_and_never_falls_back(self):
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_cfd", requested_source="kiwoom",
                capabilities=_caps(kiwoom=True, kis=True),
                primary_provider="kis")
        self.assertEqual(ctx.exception.provider, "kiwoom")
        self.assertIn("strict", str(ctx.exception))

    def test_explicit_source_may_differ_from_primary(self):
        # 연결·검증된 공급자면 primary 가 아니어도 명시해서 쓸 수 있다.
        resolution = resolve_evidence_source(
            capability="kr_investor_flow", requested_source="kiwoom",
            capabilities=_caps(kis=True, kiwoom=True),
            primary_provider="kis", release_override=True)
        self.assertEqual(resolution.selected_provider, "kiwoom")
        self.assertTrue(resolution.selection_reason.endswith("strict"))

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(EvidenceRouterError):
            resolve_evidence_source(
                capability="kr_investor_flow", requested_source="naver",
                capabilities=_caps(kis=True), primary_provider="kis")

    def test_hidden_provider_is_not_selectable_in_customer_mode(self):
        # 토스는 개발자 모드 전용이다. 고객 모드에서는 명시해도 고를 수
        # 없다 (1.0 숨김 계약을 1.1 에서도 유지).
        with self.assertRaises(EvidenceRouterError) as ctx:
            resolve_evidence_source(
                capability="kr_investor_flow", requested_source="toss",
                capabilities=_caps(toss=True), primary_provider="toss",
                public_providers=("kis", "kiwoom"))
        self.assertEqual(ctx.exception.provider_status, "not_configured")


class ResolutionShapeTests(unittest.TestCase):
    def test_resolution_pins_one_provider_and_records_why(self):
        resolution = resolve_evidence_source(
            capability="kr_investor_flow", requested_source="auto",
            capabilities=_caps(kis=True), primary_provider="kis",
            release_override=True)
        self.assertEqual(resolution.selected_provider, "kis")
        self.assertEqual(resolution.capability, "kr_investor_flow")
        self.assertEqual(resolution.requested_source, "auto")
        self.assertTrue(resolution.selection_reason)
        # 대체 공급자 인자를 아예 갖지 않는다.
        self.assertFalse(hasattr(resolution, "fallback_provider"))


class RollbackCompatibilityTests(unittest.TestCase):
    def test_evidence_capabilities_are_not_written_to_the_state_file(self):
        """1.0.0 으로 되돌려도 상태 파일이 그대로 읽혀야 한다.

        증거 능력을 상태 파일에 저장하면 스키마가 올라가고, 구버전이
        그 파일을 읽을 때 정리 대상이 된다. 저장하지 않는 쪽을 택했다.
        """
        from stock_mcp_server.market_data.connection_state import (
            provider_capabilities_v2,
            sanitize_v2,
        )

        empty = sanitize_v2({})
        keys = set(provider_capabilities_v2(empty, "kis"))
        for name in EVIDENCE_CAPABILITIES:
            self.assertNotIn(name, keys)
            self.assertNotIn(f"{name}_state", keys)


if __name__ == "__main__":
    unittest.main()
