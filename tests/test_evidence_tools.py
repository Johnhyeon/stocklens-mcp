"""공개 MCP 도구 계약 (1.1 Task 12).

대표 결정(2026-08-28): 공개 도구를 종류 수만큼 늘리지 않는다. 데스크탑
앱은 도구 단위로 승인을 받기 때문에, 종류마다 도구를 만들면 사용자가
누르는 승인 횟수가 그만큼 늘어난다. 종류는 **인자로 고르고 응답에서
블록으로 나뉜다.** 의미는 분리하되 승인 횟수는 늘리지 않는 구조다.

capability 안내용 도구도 만들지 않는다. 기존 status·describe_providers
계약을 additive 하게 넓힌다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 공개 도구 총수. 데스크탑 앱은 **도구 단위로** 승인을 받기 때문에, 도구가
# 하나 늘 때마다 사용자가 누르는 승인 횟수가 하나 는다. 그래서 이 숫자는
# 저절로 늘지 않게 막고, 늘릴 때는 여기를 같이 고치게 한다.
#
# 66  1.0.0rc1
# 68  1.1 상세 수급 2종 (종류는 도구가 아니라 인자로 가른다)
# 70  2026-09 개편 신규 자료 2종 (공모주 일정·투자자예탁금).
#     리포트 갈래는 도구를 만들지 않고 get_reports 의 kind 인자로 넣었다.
# 72  2026-09-17 위지트 문의: 국내 종목 뉴스(get_news)와 "오늘 왜 오르나" 묶음
#     (get_move_context). 묶음 도구는 조각 도구 4~5개를 잇는 호출을 하나로 줄인다.
EXPECTED_TOOL_COUNT = 72
NEW_TOOLS = {"get_detailed_investor_flow", "get_supply_pressure"}


def _tool_names() -> set[str]:
    from stock_mcp_server.server import mcp
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _tool_map() -> dict:
    from stock_mcp_server.server import mcp
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


class ToolSurfaceTests(unittest.TestCase):
    def test_exactly_two_evidence_tools_are_added(self):
        names = _tool_names()
        self.assertTrue(NEW_TOOLS <= names, NEW_TOOLS - names)
        self.assertEqual(
            len(names), EXPECTED_TOOL_COUNT,
            "공개 도구 수가 바뀌었습니다. 의도한 추가라면 EXPECTED_TOOL_COUNT 를 "
            "이력과 함께 고치세요. 도구 하나가 승인 클릭 하나입니다.")

    def test_no_per_kind_tool_exists(self):
        # 종류마다 도구를 만들면 승인 클릭이 종류 수만큼 늘어난다.
        names = _tool_names()
        for gone in ("get_program_trades", "get_short_selling",
                     "get_credit_trades", "get_securities_lending",
                     "get_cfd_balance", "get_foreigner_holding",
                     "get_foreign_holding", "get_investor_flow"):
            self.assertNotIn(gone, names)

    def test_batch_does_not_add_a_tool(self):
        names = _tool_names()
        self.assertNotIn("get_detailed_investor_flow_batch", names)
        self.assertNotIn("get_supply_pressure_batch", names)

    def test_capability_reporting_adds_no_tool(self):
        self.assertNotIn("get_market_evidence_capabilities", _tool_names())


class NameCollisionTests(unittest.TestCase):
    def test_the_flow_tool_name_does_not_collide_with_the_legacy_one(self):
        """`get_flow` 는 1.0 부터 있던 다른 도구다.

        새 도구를 `get_investor_flow` 로 두면 AI 가 둘을 같은 것으로 읽고
        섞어 쓴다. 그래서 `get_detailed_investor_flow` 로 짓는다.
        """
        names = _tool_names()
        self.assertIn("get_flow", names)
        self.assertIn("get_flow_batch", names)
        self.assertIn("get_detailed_investor_flow", names)
        self.assertNotEqual("get_flow", "get_detailed_investor_flow")


class ToolDescriptionTests(unittest.TestCase):
    """AI 가 숫자를 잘못 읽는 방식은 정해져 있다. 설명에 못 박는다."""

    def setUp(self):
        self.tools = _tool_map()

    def test_flow_tool_states_the_reading_constraints(self):
        text = self.tools["get_detailed_investor_flow"].description
        # 미정산·미지원은 0 이 아니다
        self.assertIn("0", text)
        self.assertIn("미정산", text)
        # 기관계와 세부 항목을 더하면 두 번 센다
        self.assertIn("기관계", text)
        # 수량과 금액은 다른 measure 다 (실측 3.7배 차이)
        self.assertIn("measure", text)
        self.assertIn("금액", text)
        # 미국 주식에는 없다
        self.assertIn("US", text)

    def test_pressure_tool_states_the_reading_constraints(self):
        text = self.tools["get_supply_pressure"].description
        # 대차잔고는 공매도 실행이 아니다
        self.assertIn("대차", text)
        self.assertIn("공매도", text)
        # 종류를 하나의 점수로 합치지 않는다
        self.assertIn("합", text)
        # AI 가 공급자 원본 단위를 추측하지 않고 정규화된 단위를 읽는다.
        self.assertIn("measure_units", text)
        self.assertIn("observed_at", text)

    def test_neither_description_promises_a_recommendation(self):
        # 법적 라인: 종목 추천·매매 신호를 만들지 않는다.
        for name in NEW_TOOLS:
            text = self.tools[name].description
            for banned in ("매수 추천", "매도 추천", "수익 보장", "매매 신호"):
                self.assertNotIn(banned, text)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tool_map()

    def test_flow_tool_accepts_one_or_many_codes(self):
        props = self.tools["get_detailed_investor_flow"].inputSchema[
            "properties"]
        self.assertIn("code", props)
        self.assertIn("codes", props)
        self.assertIn("measure", props)
        self.assertIn("source", props)

    def test_pressure_tool_accepts_one_or_many_kinds(self):
        props = self.tools["get_supply_pressure"].inputSchema["properties"]
        self.assertIn("kind", props)
        self.assertIn("kinds", props)
        self.assertIn("code", props)
        self.assertIn("codes", props)


class ResponseShapeTests(unittest.TestCase):
    """지금은 출시 게이트가 전부 닫혀 있다. 그래도 응답은 계약을 지킨다."""

    def _call(self, name, **kwargs):
        from stock_mcp_server import server
        fn = getattr(server, name)
        raw = asyncio.run(fn(**kwargs))
        return json.loads(raw)

    def test_flow_response_is_one_json_document_with_meta_inside(self):
        parsed = self._call("get_detailed_investor_flow", code="005930")
        self.assertIn("_meta", parsed)
        # 경고는 _meta 안에 있다. JSON 뒤에 텍스트를 붙이지 않는다.
        self.assertIn("warnings", parsed["_meta"])
        self.assertIn("data_availability", parsed)

    def test_pressure_response_separates_blocks_per_kind(self):
        parsed = self._call(
            "get_supply_pressure", code="005930",
            kinds=["program_trading", "short_selling", "cfd"])
        blocks = parsed["blocks"]
        self.assertEqual(set(blocks),
                         {"program_trading", "short_selling", "cfd"})
        for block in blocks.values():
            for field in ("status", "provider", "market", "data_as_of",
                          "data_completeness", "warnings", "granularity"):
                self.assertIn(field, block)
        # 종류를 하나의 점수로 합치지 않는다.
        self.assertNotIn("score", parsed)
        self.assertNotIn("pressure_score", parsed)

    def test_an_unavailable_kind_carries_a_reason_not_an_empty_success(self):
        parsed = self._call("get_supply_pressure", code="005930",
                            kinds=["cfd"])
        block = parsed["blocks"]["cfd"]
        self.assertNotEqual(block["status"], "ok")
        self.assertTrue(block["unavailable_reason"])

    def test_no_requested_code_vanishes_from_a_multi_code_response(self):
        """받은 것과 못 받은 것을 합치면 요청한 것과 정확히 같아야 한다.

        조용히 빠진 종목은 호출자에게 '조회했는데 데이터가 없음'으로
        읽힌다. 시도조차 못 한 것과 전혀 다른 사실이다.
        """
        parsed = self._call("get_detailed_investor_flow",
                            codes=["005930", "000660"])
        self.assertIn("entities", parsed)
        accounted = set(parsed["entities"]) | {
            f["code"] for f in parsed["entity_failures"]}
        self.assertEqual(accounted, {"005930", "000660"})
        for failure in parsed["entity_failures"]:
            self.assertTrue(failure["reason"])

    def test_missing_code_is_rejected_without_calling_a_provider(self):
        parsed = self._call("get_detailed_investor_flow")
        self.assertFalse(parsed["ok"])
        self.assertIn("code", " ".join(parsed["_meta"]["warnings"]))

    def test_unknown_measure_is_rejected(self):
        parsed = self._call("get_detailed_investor_flow", code="005930",
                            measure="feelings")
        self.assertFalse(parsed["ok"])

    def test_unknown_kind_is_rejected(self):
        parsed = self._call("get_supply_pressure", code="005930",
                            kind="teleport")
        self.assertFalse(parsed["ok"])

    def test_pressure_serializer_exposes_time_and_measure_units(self):
        from datetime import date, datetime, timezone, timedelta
        from decimal import Decimal

        from stock_mcp_server.market_data.evidence_models import (
            PressureBlock,
            PressureRow,
        )
        from stock_mcp_server.server import _pressure_block_json

        observed = datetime(2026, 8, 28, 15, 30, 14,
                            tzinfo=timezone(timedelta(hours=9)))
        block = PressureBlock(
            kind="program_trading", status="ok", provider="kis",
            market="KR", rows=(PressureRow(
                date=date(2026, 8, 28), measures={"close": Decimal("1")},
                raw_fields={"close": "stck_prpr"}, observed_at=observed),),
            data_as_of=date(2026, 8, 28), data_completeness="complete",
            warnings=(), unavailable_reason=None,
            coverage={"rows": 1, "complete": True},
            granularity="intraday", measure_units={"close": "KRW"},
        )

        payload = _pressure_block_json(block)
        self.assertEqual(payload["measure_units"], {"close": "KRW"})
        self.assertEqual(payload["rows"][0]["observed_at"],
                         "2026-08-28T15:30:14+09:00")


if __name__ == "__main__":
    unittest.main()
