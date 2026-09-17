"""도구 입력 스키마가 중계 클라이언트를 통과하는 모양인가.

2026-09-16 Claude 데스크탑 2.110.0 / Claude Code 2.1.271 의 로컬 MCP 중계 경로에서
기본값이 있는 선택 인자를 생략하면 `expected nonoptional` 로 거부됐고,
`X | None` 인자는 타입을 잃어 모델이 날짜를 숫자로 보냈다(자세한 경위는
stock_mcp_server/_tool_schema.py). 이 테스트는 우리가 내보내는 스키마와 인자 처리가
그 경로에서 살아남는 모양을 지키는지 본다.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server._tool_schema import client_safe_input_schema, coerce_integer_strings
from stock_mcp_server.server import mcp


_SAMPLE = {
    "type": "object",
    "properties": {
        "symbol": {"title": "Symbol", "type": "string"},
        "date": {"anyOf": [{"type": "string"}, {"type": "null"}],
                 "default": None, "title": "Date"},
        "include": {"anyOf": [{"items": {"type": "string"}, "type": "array"},
                              {"type": "null"}],
                    "default": None, "title": "Include"},
        "session": {"default": "regular", "title": "Session", "type": "string"},
        "completed_only": {"default": True, "title": "Completed Only", "type": "boolean"},
        "row_limit": {"default": 120, "title": "Row Limit", "type": "integer"},
        "year": {"anyOf": [{"type": "integer"}, {"type": "string"}], "title": "Year"},
        "mixed": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}],
                  "default": None, "title": "Mixed"},
    },
    "required": ["symbol", "year"],
}


class ClientSafeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(_SAMPLE)
        self.out = client_safe_input_schema(_SAMPLE)
        self.props = self.out["properties"]

    def test_optional_args_carry_no_default(self):
        for name in ("date", "include", "session", "completed_only", "row_limit", "mixed"):
            self.assertNotIn("default", self.props[name], name)

    def test_nullable_collapses_to_the_real_type(self):
        self.assertEqual(self.props["date"], {"type": "string", "title": "Date"})
        self.assertEqual(self.props["include"],
                         {"items": {"type": "string"}, "type": "array", "title": "Include"})

    def test_non_null_unions_are_kept(self):
        self.assertEqual(self.props["year"], _SAMPLE["properties"]["year"])
        self.assertEqual(self.props["mixed"]["anyOf"],
                         [{"type": "integer"}, {"type": "string"}])

    def test_required_and_names_do_not_change(self):
        self.assertEqual(self.out["required"], ["symbol", "year"])
        self.assertEqual(list(self.props), list(_SAMPLE["properties"]))

    def test_source_schema_is_not_mutated(self):
        self.assertEqual(_SAMPLE, self.original)


class IntegerStringTests(unittest.TestCase):
    def test_integer_for_string_arg_becomes_text(self):
        out = coerce_integer_strings(_SAMPLE, {"symbol": 5930, "date": 20260916})
        self.assertEqual(out, {"symbol": "5930", "date": "20260916"})

    def test_leading_zeros_are_not_guessed(self):
        out = coerce_integer_strings(_SAMPLE, {"symbol": 126380})
        self.assertEqual(out["symbol"], "126380")

    def test_other_types_are_left_alone(self):
        args = {"completed_only": True, "row_limit": 5, "year": 2024,
                "mixed": 7, "session": "regular", "unknown": 3}
        self.assertIs(coerce_integer_strings(_SAMPLE, args), args)


class PublishedToolListTests(unittest.TestCase):
    """실제로 내보내는 도구 목록 전체에 대한 불변식."""

    @classmethod
    def setUpClass(cls):
        cls.tools = asyncio.run(mcp.list_tools())

    def test_there_are_tools(self):
        self.assertGreater(len(self.tools), 0)

    def test_no_optional_arg_advertises_a_default(self):
        offenders = []
        for tool in self.tools:
            required = set(tool.inputSchema.get("required") or [])
            for name, prop in tool.inputSchema.get("properties", {}).items():
                if name not in required and "default" in prop:
                    offenders.append(f"{tool.name}.{name}")
        self.assertEqual(offenders, [])

    def test_no_arg_loses_its_type_to_a_null_branch(self):
        offenders = []
        for tool in self.tools:
            for name, prop in tool.inputSchema.get("properties", {}).items():
                branches = prop.get("anyOf") or []
                if any(b.get("type") == "null" for b in branches):
                    offenders.append(f"{tool.name}.{name}")
                if "type" not in prop and not branches:
                    offenders.append(f"{tool.name}.{name} (타입 없음)")
        self.assertEqual(offenders, [])

    def test_registered_signature_still_has_defaults(self):
        """목록만 바꾼다. 생략 시 기본값을 채우는 등록 정보는 그대로다."""
        has_default = [
            t for t in mcp._tool_manager.list_tools()
            if any("default" in p for p in t.parameters.get("properties", {}).values())
        ]
        self.assertGreater(len(has_default), 0)


class CallThroughServerTests(unittest.IsolatedAsyncioTestCase):
    """중계 경로에서 모델이 실제로 보낸 인자 모양 그대로 서버에 넣어본다."""

    async def _call(self, arguments):
        from datetime import datetime
        from unittest.mock import AsyncMock, patch

        from stock_mcp_server import server
        from tests.test_intraday_tools import (
            KST, _OPEN_CLOCK, _dataset, _minute_bars, _route_meta,
        )

        bars = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 5,
                            minutes=5, interval="5m")
        fetch = AsyncMock(return_value=(_dataset(bars), _route_meta()))
        with patch.object(server, "_fetch_intraday_dataset", fetch),              patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK),              patch.object(server, "is_licensed", return_value=True),              patch.object(server, "get_update_notice", AsyncMock(return_value="")):
            await mcp.call_tool("get_intraday_chart", arguments)
        return fetch.await_args.kwargs

    async def test_intraday_chart_without_optional_args(self):
        """2026-09-17 새벽 실패한 호출 그대로 — date·venue·session·completed_only·source 생략."""
        kw = await self._call({"symbol": "010170", "market": "KR",
                               "interval": "5m", "row_limit": 12})
        self.assertEqual(
            (kw["venue"], kw["session"], kw["completed_only"], kw["source"]),
            (None, "regular", True, "auto"))

    async def test_numeric_date_is_read_as_yyyymmdd(self):
        from datetime import date

        kw = await self._call({"symbol": "005930", "interval": "5m",
                               "date": 20260827})
        self.assertEqual(kw["trading_date"], date(2026, 8, 27))

if __name__ == "__main__":
    unittest.main(verbosity=2)
