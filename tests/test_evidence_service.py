"""상세 수급 서비스 계층 (1.1 Task 11).

이 계층이 지켜야 하는 한 가지: **못 준 것을 빈 성공으로 돌려주지 않는다.**

공급자마다 줄 수 있는 것이 정확히 반대로 갈린다 (2026-08-28 실측).
키움은 기관 세부 13종을 주지만 매수·매도 분해가 없고, KIS 는 3종만 주지만
매수·매도를 준다. 어느 쪽도 상위집합이 아니다. 그래서 응답은 '무엇을
받았는지'와 '무엇을 왜 못 받았는지'를 함께 싣는다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_models import (
    UNIT_BY_MEASURE,
    InvestorFlowDataset,
    InvestorFlowRow,
    PressureBlock,
)
from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.kis_evidence import KisEvidenceProvider
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)

BASE = date(2026, 8, 27)


def _run(coro):
    return asyncio.run(coro)


def _row(day: date) -> InvestorFlowRow:
    return InvestorFlowRow(
        date=day, close=None, volume=None,
        values={"individual": -3223427, "foreign": 1000000},
        unsettled=(), data_state="final", balance_ok=True,
        principal_sum=0, raw_categories={"individual": "ind_invsr"})


class _FakeAdapter:
    """실제 어댑터의 capability 표를 그대로 빌려 쓰는 대역.

    능력 표를 손으로 흉내 내면 실제와 갈라진다. 네트워크만 대신한다.
    """

    def __init__(self, provider_id, source_cls, *, fail_codes=(),
                 blocks=None):
        self.provider_id = provider_id
        self.profile = "real"
        self.capabilities = source_cls.capabilities
        self.pressure_capabilities = source_cls.pressure_capabilities
        self.pressure_unavailable_reasons = (
            source_cls.pressure_unavailable_reasons)
        self._fail = set(fail_codes)
        self._blocks = blocks or {}
        self.flow_calls = []
        self.pressure_calls = []

    async def fetch_investor_flow(self, symbol, *, base_date,
                                  measure="net_quantity", row_limit=30,
                                  **kwargs):
        self.flow_calls.append((symbol, measure, row_limit))
        if symbol in self._fail:
            from stock_mcp_server.market_data.kis_client import KisApiError
            raise KisApiError("entity_not_found")
        return InvestorFlowDataset(
            symbol=symbol, provider=self.provider_id, profile="real",
            market="KR", rows=(_row(base_date),), data_state="final",
            coverage={"rows": 1, "complete": True}, warnings=(),
            measure=measure, unit=UNIT_BY_MEASURE[measure],
            source_endpoint=f"{self.provider_id}_kr_investor_daily")

    async def fetch_supply_pressure(self, symbol, *, kinds, base_date,
                                    lookback_days=30, row_limit=30):
        self.pressure_calls.append((symbol, tuple(kinds)))
        return {k: self._blocks.get(k) or PressureBlock(
            kind=k, status="ok", provider=self.provider_id, market="KR",
            rows=(), data_as_of=base_date, data_completeness="complete",
            warnings=(), unavailable_reason=None,
            coverage={"rows": 0, "complete": True}) for k in kinds}


class _FakeRuntime:
    def __init__(self, primary="kiwoom", adapters=None, connected=None,
                 generation=1):
        self.primary = primary
        self.adapters = adapters or {}
        self.connected = connected or set(self.adapters)
        self.generation = generation

    def snapshot(self):
        return self

    def capabilities(self, provider):
        return {"connected": provider in self.connected}

    @property
    def primary_provider(self):
        return self.primary

    def active_profile(self, provider):
        return "real"

    def provider_generation(self, provider):
        return self.generation

    def evidence_provider_for(self, provider, snapshot=None):
        return self.adapters.get(provider)


def _service(primary="kiwoom", **kwargs):
    adapters = {
        "kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider, **kwargs),
        "kis": _FakeAdapter("kis", KisEvidenceProvider, **kwargs),
    }
    runtime = _FakeRuntime(primary=primary, adapters=adapters)
    service = EvidenceService(runtime=runtime, base_date=BASE,
                              release_override=True)
    return service, adapters


class AvailabilityEnvelopeTests(unittest.TestCase):
    def test_kiwoom_returns_breakdown_and_states_the_missing_split(self):
        service, _ = _service("kiwoom")
        result = _run(service.investor_flow(code="005930", days=20))
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "kiwoom")
        returned = result.data_availability["returned"]
        self.assertIn("kr.investor_flow.daily.total", returned)
        self.assertIn("kr.investor_flow.daily.breakdown", returned)
        self.assertEqual(result.data_availability["unavailable"], [{
            "capability": "kr.investor_flow.daily.buy_sell",
            "reason": "unsupported_by_selected_provider",
        }])

    def test_kis_returns_the_split_and_states_the_missing_breakdown(self):
        # 정확히 반대다. 한쪽을 상위집합처럼 다루면 거짓말이 된다.
        service, _ = _service("kis")
        result = _run(service.investor_flow(code="005930", days=20))
        self.assertTrue(result.ok)
        returned = result.data_availability["returned"]
        self.assertIn("kr.investor_flow.daily.buy_sell", returned)
        self.assertEqual(result.data_availability["unavailable"], [{
            "capability": "kr.investor_flow.daily.breakdown",
            "reason": "unsupported_by_selected_provider",
        }])

    def test_requested_lists_everything_that_was_asked_for(self):
        service, _ = _service("kis")
        result = _run(service.investor_flow(code="005930", days=20))
        requested = set(result.data_availability["requested"])
        self.assertEqual(
            requested,
            set(result.data_availability["returned"])
            | {u["capability"]
               for u in result.data_availability["unavailable"]})

    def test_the_envelope_always_has_every_contract_field(self):
        service, _ = _service("kiwoom")
        result = _run(service.investor_flow(code="005930", days=20))
        for field in ("ok", "provider", "profile", "market",
                      "data_availability", "records", "coverage",
                      "warnings", "error_code"):
            self.assertTrue(hasattr(result, field), field)

    def test_measure_and_unit_travel_with_the_numbers(self):
        # 같은 항목이 measure 에 따라 3.7 배 달라진다 (실측). 라벨 없이
        # 숫자만 내보내면 안 된다.
        service, _ = _service("kiwoom")
        result = _run(service.investor_flow(
            code="005930", days=20, measure="net_amount"))
        self.assertEqual(result.measure, "net_amount")
        self.assertEqual(result.unit, "KRW_million")


class NotAnEmptySuccessTests(unittest.TestCase):
    def test_unsupported_is_a_stated_failure_not_an_empty_dataset(self):
        service, _ = _service("kis")
        result = _run(service.supply_pressure(
            code="005930", kinds=["securities_lending"]))
        block = result.blocks["securities_lending"]
        self.assertEqual(block.status, "unsupported")
        self.assertEqual(block.unavailable_reason, "market_level_only")
        self.assertEqual(block.rows, ())

    def test_no_connected_provider_is_an_error_not_an_empty_result(self):
        runtime = _FakeRuntime(primary=None, adapters={})
        service = EvidenceService(runtime=runtime, base_date=BASE)
        result = _run(service.investor_flow(code="005930", days=20))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "provider_not_configured")
        self.assertEqual(result.records, ())

    def test_a_closed_release_gate_is_not_reported_as_available(self):
        # release_override 없이는 게이트가 전부 닫혀 있다 (2026-08-28).
        adapters = {"kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider)}
        service = EvidenceService(
            runtime=_FakeRuntime(primary="kiwoom", adapters=adapters),
            base_date=BASE)
        result = _run(service.investor_flow(code="005930", days=20))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code,
                         "unverified_by_selected_provider")


class PressureBlockTests(unittest.TestCase):
    def test_each_kind_keeps_its_own_status_and_nothing_is_merged(self):
        service, _ = _service("kis")
        result = _run(service.supply_pressure(
            code="005930",
            kinds=["short_selling", "securities_lending", "credit"]))
        self.assertEqual(set(result.blocks),
                         {"short_selling", "securities_lending", "credit"})
        self.assertEqual(result.blocks["short_selling"].status, "ok")
        self.assertEqual(result.blocks["securities_lending"].status,
                         "unsupported")
        self.assertEqual(result.blocks["credit"].status, "unverified")
        for name in ("score", "pressure_score", "combined", "total"):
            self.assertFalse(hasattr(result, name), name)

    def test_unavailable_kinds_never_reach_the_network(self):
        service, adapters = _service("kis")
        _run(service.supply_pressure(
            code="005930", kinds=["short_selling", "cfd", "foreign_holding"]))
        # 미지원 종류를 굳이 호출해서 오류를 받아올 이유가 없다.
        self.assertEqual(adapters["kis"].pressure_calls,
                         [("005930", ("short_selling",))])

    def test_granularity_is_carried_so_shapes_are_not_confused(self):
        service, _ = _service("kis")
        result = _run(service.supply_pressure(
            code="005930", kinds=["program_trading"]))
        self.assertTrue(hasattr(result.blocks["program_trading"],
                                "granularity"))

    def test_unknown_kind_is_rejected_before_any_call(self):
        service, adapters = _service("kis")
        with self.assertRaises(ValueError):
            _run(service.supply_pressure(code="005930", kinds=["teleport"]))
        self.assertEqual(adapters["kis"].pressure_calls, [])


class BatchTests(unittest.TestCase):
    def test_batch_pins_one_provider_and_reports_entity_failures(self):
        adapters = {
            "kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider,
                                   fail_codes=("999999",)),
            "kis": _FakeAdapter("kis", KisEvidenceProvider),
        }
        service = EvidenceService(
            runtime=_FakeRuntime(primary="kiwoom", adapters=adapters),
            base_date=BASE, release_override=True)
        result = _run(service.investor_flow_batch(
            codes=["005930", "000660", "999999"], days=20))
        self.assertEqual(result.provider, "kiwoom")
        self.assertEqual({r.provider for r in result.records}, {"kiwoom"})
        self.assertEqual(result.entity_failures,
                         [{"code": "999999", "reason": "entity_not_found"}])
        # 실패한 종목 때문에 다른 공급자로 넘어가지 않는다.
        self.assertEqual(adapters["kis"].flow_calls, [])

    def test_input_order_is_preserved(self):
        service, _ = _service("kiwoom")
        result = _run(service.investor_flow_batch(
            codes=["000660", "005930"], days=20))
        self.assertEqual([r.symbol for r in result.records],
                         ["000660", "005930"])

    def test_batch_limit_is_enforced_before_any_provider_call(self):
        service, adapters = _service("kiwoom")
        with self.assertRaises(ValueError) as ctx:
            _run(service.investor_flow_batch(
                codes=[f"{i:06d}" for i in range(31)], days=20))
        self.assertIn("30", str(ctx.exception))
        self.assertEqual(adapters["kiwoom"].flow_calls, [])

    def test_duplicate_codes_are_collapsed_once(self):
        service, adapters = _service("kiwoom")
        result = _run(service.investor_flow_batch(
            codes=["005930", "005930"], days=20))
        self.assertEqual(len(adapters["kiwoom"].flow_calls), 1)
        self.assertEqual(len(result.records), 1)


class GenerationTests(unittest.TestCase):
    def test_a_connection_change_mid_request_discards_the_result(self):
        class _Changing(_FakeRuntime):
            def provider_generation(self, provider):
                self.generation += 1
                return self.generation

        adapters = {"kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider)}
        service = EvidenceService(
            runtime=_Changing(primary="kiwoom", adapters=adapters),
            base_date=BASE, release_override=True)
        result = _run(service.investor_flow(code="005930", days=20))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "provider_changed_during_request")
        self.assertEqual(result.records, ())


if __name__ == "__main__":
    unittest.main()
