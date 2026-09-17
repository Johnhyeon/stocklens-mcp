# -*- coding: utf-8 -*-
"""fetch — 첫 조회에서만 나던 네이티브 접근 위반(OSError)을 한 번 더 시도한다.

2026-09-17 대표 PC(ChatGPT 앱) 실측: search_stock 첫 호출이
"exception: access violation writing 0x0000000000000048" 로 죽고 바로 다시 부르면 됐다.
지키는 것:
- 그 문구의 OSError 는 같은 요청을 한 번 더 보낸다.
- 다른 OSError 는 재시도하지 않고 그대로 올린다(디스크·소켓 계열은 재시도로 안 풀린다).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stock_mcp_server import _http


def _resp(status=200):
    r = MagicMock()
    r.status_code = status
    return r


def test_access_violation_is_retried_once_and_succeeds():
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        OSError("exception: access violation writing 0x0000000000000048"), _resp()])
    with patch.object(_http, "get_client", return_value=client), \
         patch.object(_http.asyncio, "sleep", AsyncMock()):
        resp = asyncio.run(_http.fetch("https://example.invalid/x"))
    assert resp.status_code == 200
    assert client.get.await_count == 2


def test_other_oserror_is_not_retried():
    client = MagicMock()
    client.get = AsyncMock(side_effect=[OSError("[Errno 28] No space left on device"), _resp()])
    with patch.object(_http, "get_client", return_value=client):
        with pytest.raises(OSError, match="No space"):
            asyncio.run(_http.fetch("https://example.invalid/x"))
    assert client.get.await_count == 1


def test_access_violation_every_time_still_raises_after_retries():
    client = MagicMock()
    client.get = AsyncMock(side_effect=OSError("exception: access violation writing 0x48"))
    with patch.object(_http, "get_client", return_value=client), \
         patch.object(_http.asyncio, "sleep", AsyncMock()):
        with pytest.raises(OSError, match="access violation"):
            asyncio.run(_http.fetch("https://example.invalid/x", max_retries=2))
    assert client.get.await_count == 3
