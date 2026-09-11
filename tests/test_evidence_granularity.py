"""데이터가 없는 블록도 라벨은 틀리면 안 된다 (1.1).

`PressureBlock.granularity` 의 기본값은 "daily" 다. 상태만 담은 블록을
기본값으로 만들면 **KIS 프로그램매매(장중)를 일별이라고 말하게 된다.**
값이 비어 있어도 라벨이 틀리면 사용자는 나중에 그 블록이 열렸을 때
키움의 일별 숫자와 같은 기준으로 비교한다.

그래서 못 준 블록은 공급자가 실제로 주는 모양을 싣거나, 모르면
"unknown" 을 싣는다. 기본값에 기대지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.kis_evidence import KisEvidenceProvider
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_evidence_service import _FakeAdapter, _FakeRuntime  # noqa: E402

BASE = date(2026, 8, 27)


def _run(coro):
    return asyncio.run(coro)


class AdapterGranularityTests(unittest.TestCase):
    def test_kis_program_trading_is_intraday(self):
        shapes = KisEvidenceProvider.pressure_granularity()
        self.assertEqual(shapes["program_trading"], "intraday")
        self.assertEqual(shapes["short_selling"], "daily")

    def test_kiwoom_program_trading_is_daily(self):
        # 같은 이름, 다른 모양. 이 차이를 말하지 않으면 사용자가 두 숫자를
        # 같은 것으로 읽는다.
        shapes = KiwoomEvidenceProvider.pressure_granularity()
        self.assertEqual(shapes["program_trading"], "daily")

    def test_only_served_kinds_claim_a_shape(self):
        # 안 주는 종류에 모양을 붙이면 있지도 않은 사실을 주장하게 된다.
        for provider in (KisEvidenceProvider, KiwoomEvidenceProvider):
            shapes = provider.pressure_granularity()
            served = {k for k, v in provider.pressure_capabilities().items()
                      if v == "available"}
            self.assertEqual(set(shapes), served, provider.__name__)


class StateBlockGranularityTests(unittest.TestCase):
    def _service(self, primary):
        adapters = {
            "kis": _FakeAdapter("kis", KisEvidenceProvider),
            "kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider),
        }
        return EvidenceService(
            runtime=_FakeRuntime(primary=primary, adapters=adapters),
            base_date=BASE)

    def test_a_gated_kis_block_still_says_intraday(self):
        # 출시 게이트가 닫혀 데이터는 없지만, 열리면 장중 시계열이다.
        result = _run(self._service("kis").supply_pressure(
            code="005930", kinds=["program_trading"]))
        block = result.blocks["program_trading"]
        self.assertEqual(block.status, "unverified")
        self.assertEqual(block.granularity, "intraday")

    def test_a_gated_kiwoom_block_says_daily(self):
        result = _run(self._service("kiwoom").supply_pressure(
            code="005930", kinds=["program_trading"]))
        self.assertEqual(result.blocks["program_trading"].granularity,
                         "daily")

    def test_an_unsupported_kind_claims_no_shape(self):
        # 아예 안 주는 것에 "daily" 를 붙이면 없는 사실을 만든 것이다.
        result = _run(self._service("kis").supply_pressure(
            code="005930", kinds=["securities_lending", "cfd"]))
        for kind in ("securities_lending", "cfd"):
            self.assertEqual(result.blocks[kind].granularity, "unknown",
                             kind)

    def test_a_disconnected_request_claims_no_shape(self):
        service = EvidenceService(
            runtime=_FakeRuntime(primary=None, adapters={}), base_date=BASE)
        result = _run(service.supply_pressure(
            code="005930", kinds=["program_trading", "credit"]))
        self.assertEqual(set(result.blocks), {"program_trading", "credit"})
        for block in result.blocks.values():
            self.assertEqual(block.status, "not_configured")
            self.assertEqual(block.granularity, "unknown")


if __name__ == "__main__":
    unittest.main()
