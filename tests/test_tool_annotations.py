# -*- coding: utf-8 -*-
"""도구 주석(readOnlyHint) — Codex·ChatGPT 앱이 승인 없이 읽기 도구를 부르게 하는 표시.

2026-09-17: 주석이 없던 우리 도구는 "쓰기일지도 모르는 도구"로 취급돼 첫 호출이 승인
대기에 걸렸다(codex exec 에서 "user cancelled MCP tool call" 네 번 연속). 여기서 지키는 것:
- 모든 도구에 주석이 붙는다.
- 파일을 만들거나 사용자 상태를 바꾸는 도구만 readOnlyHint=False 다.
- write_tools 에 없는 이름을 적으면 기동 검사가 잡는다.
"""
from __future__ import annotations

import asyncio

import pytest

from stock_mcp_server import server
from stock_mcp_server._tool_schema import LensFastMCP


WRITE = {"watchlist", "export_to_excel", "scan_to_excel", "save_analysis_to_excel",
         "export_us_to_excel"}


def test_every_tool_carries_read_or_write_annotation():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 70
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.openWorldHint is True, tool.name
        if tool.name in WRITE:
            assert tool.annotations.readOnlyHint is False, tool.name
        else:
            assert tool.annotations.readOnlyHint is True, tool.name
            assert tool.annotations.destructiveHint is False, tool.name


def test_new_kr_news_tools_are_read_only_and_registered():
    names = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert names["get_news"].annotations.readOnlyHint is True
    assert names["get_move_context"].annotations.readOnlyHint is True
    assert names["get_disclosure"].annotations.readOnlyHint is True


def test_write_tools_names_are_checked_at_startup():
    server.mcp.check_write_tools()  # 실제 등록 이름과 맞아야 한다

    broken = LensFastMCP("x", write_tools=("no_such_tool",))
    with pytest.raises(RuntimeError, match="no_such_tool"):
        broken.check_write_tools()
