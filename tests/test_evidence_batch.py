"""배치 계약 (1.1 리뷰 차단 2·3).

두 도구가 같은 규칙을 따라야 한다. 하나만 고치면 다음 리뷰에서 다른
하나가 걸린다.

1. **한 요청은 한 공급자.** 1.0 핵심 계약이다. 종목마다 상태를 새로
   읽으면 요청 도중 Manager 에서 주 사용 증권사가 바뀌었을 때 앞쪽
   종목과 뒤쪽 종목이 다른 증권사에서 온다. 응답 최상위에는 공급자가
   하나만 적히므로, 사용자는 전부 그 증권사 숫자인 줄 안다.
2. **최대 30종목.** 도구 설명이 약속한 값이다. 실제로 막지 않으면
   31종목이 그대로 나가고, 설명과 동작이 갈라진다.
3. **중복 제거.** 같은 종목을 두 번 적었다고 API 를 두 번 부르지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_service import (
    MAX_BATCH_CODES,
    EvidenceService,
)
from stock_mcp_server.market_data.kis_evidence import KisEvidenceProvider
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_evidence_service import _FakeAdapter, _FakeRuntime  # noqa: E402

BASE = date(2026, 8, 27)
KINDS = ["program_trading", "short_selling"]


def _run(coro):
    return asyncio.run(coro)


class _SwitchingRuntime(_FakeRuntime):
    """요청 도중 주 사용 증권사가 바뀌는 상황.

    Manager 에서 주 사용을 바꾸면 실제로 이렇게 된다. 상태 파일을
    매 요청 다시 읽는 것이 1.0 설계이기 때문이다.
    """

    def __init__(self, order, **kwargs):
        super().__init__(**kwargs)
        self._order = list(order)
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        if self._order:
            self.primary = self._order.pop(0)
        return self


def _switching(order):
    adapters = {
        "kis": _FakeAdapter("kis", KisEvidenceProvider),
        "kiwoom": _FakeAdapter("kiwoom", KiwoomEvidenceProvider),
    }
    runtime = _SwitchingRuntime(order, primary=order[0], adapters=adapters)
    service = EvidenceService(runtime=runtime, base_date=BASE,
                              release_override=True)
    return service, adapters, runtime


class PressureProviderPinningTests(unittest.TestCase):
    def test_a_multi_symbol_request_stays_on_one_provider(self):
        service, adapters, _ = _switching(["kis", "kiwoom", "kiwoom"])
        result = _run(service.supply_pressure_batch(
            codes=["005930", "000660"], kinds=KINDS))
        self.assertEqual(result.provider, "kis")
        for code, blocks in result.entities.items():
            for kind, block in blocks.items():
                self.assertEqual(block.provider, "kis",
                                 f"{code}/{kind} 가 다른 공급자에서 왔다")
        # 두 번째 공급자는 아예 불리지 않는다.
        self.assertEqual(adapters["kiwoom"].pressure_calls, [])

    def test_the_state_is_snapshotted_once_for_the_whole_request(self):
        service, _, runtime = _switching(["kis", "kiwoom", "kiwoom"])
        _run(service.supply_pressure_batch(
            codes=["005930", "000660", "035420"], kinds=KINDS))
        # 종목 수만큼 다시 읽으면 그 사이 변경이 섞여 들어온다.
        # 세대 확인용 재조회 1회까지만 허용한다.
        self.assertLessEqual(runtime.snapshots, 2, runtime.snapshots)

    def test_every_requested_code_is_accounted_for(self):
        service, _, _ = _switching(["kis"])
        codes = ["005930", "000660", "035420"]
        result = _run(service.supply_pressure_batch(codes=codes,
                                                    kinds=KINDS))
        accounted = set(result.entities) | {
            f["code"] for f in result.entity_failures}
        self.assertEqual(accounted, set(codes))


class SharedLimitTests(unittest.TestCase):
    def test_pressure_batch_enforces_the_same_limit_as_flow(self):
        service, adapters, _ = _switching(["kis"])
        codes = [f"{i:06d}" for i in range(MAX_BATCH_CODES + 1)]
        with self.assertRaises(ValueError) as ctx:
            _run(service.supply_pressure_batch(codes=codes, kinds=KINDS))
        self.assertIn(str(MAX_BATCH_CODES), str(ctx.exception))
        # 공급자를 부르기 전에 막는다. 절반만 조회하고 실패하면
        # 사용자는 어디까지 진짜인지 알 수 없다.
        self.assertEqual(adapters["kis"].pressure_calls, [])

    def test_duplicate_codes_are_collapsed(self):
        service, adapters, _ = _switching(["kis"])
        result = _run(service.supply_pressure_batch(
            codes=["005930", "005930", "000660"], kinds=KINDS))
        self.assertEqual(len(adapters["kis"].pressure_calls), 2)
        self.assertEqual(set(result.entities), {"005930", "000660"})


class ToolLevelTests(unittest.TestCase):
    """도구 설명이 약속한 것과 실제 동작이 같아야 한다."""

    def _call(self, name, **kwargs):
        from stock_mcp_server import server
        return json.loads(asyncio.run(getattr(server, name)(**kwargs)))

    def test_both_tools_reject_more_than_the_documented_limit(self):
        codes = [f"{i:06d}" for i in range(MAX_BATCH_CODES + 1)]
        for name in ("get_detailed_investor_flow", "get_supply_pressure"):
            parsed = self._call(name, codes=codes)
            self.assertFalse(parsed["ok"], name)
            joined = " ".join(parsed["_meta"]["warnings"])
            self.assertIn(str(MAX_BATCH_CODES), joined, name)

    def test_both_tools_accept_exactly_the_documented_limit(self):
        codes = [f"{i:06d}" for i in range(MAX_BATCH_CODES)]
        for name in ("get_detailed_investor_flow", "get_supply_pressure"):
            parsed = self._call(name, codes=codes)
            # 연결이 없어 실패하더라도 '개수 초과'로 거절되면 안 된다.
            joined = " ".join(parsed["_meta"]["warnings"])
            self.assertNotIn(f"최대 {MAX_BATCH_CODES}", joined, name)

    def test_duplicates_do_not_consume_the_limit(self):
        # 같은 종목을 30번 적어도 1종목이다.
        parsed = self._call("get_supply_pressure",
                            codes=["005930"] * (MAX_BATCH_CODES + 5))
        joined = " ".join(parsed["_meta"]["warnings"])
        self.assertNotIn(f"최대 {MAX_BATCH_CODES}", joined)

    def test_the_documented_limit_matches_the_enforced_one(self):
        from stock_mcp_server import server

        for name in ("get_detailed_investor_flow", "get_supply_pressure"):
            text = getattr(server, name).__doc__ or ""
            self.assertIn(f"최대 {MAX_BATCH_CODES}개", text, name)


if __name__ == "__main__":
    unittest.main()
