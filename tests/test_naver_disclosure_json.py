# -*- coding: utf-8 -*-
"""공시 목록 — 2026-09-17 구 HTML 페이지 410 Gone 이후 JSON 으로 옮긴 계약.

지키는 것:
- date 는 datetime 의 날짜 부분(DART 접수일과 같음을 실측). 시각은 네이버 적재 시각이라
  내보내지 않는다.
- 빈 목록은 '공시 없음'이고, JSON 이 아닌 응답(SPA 껍데기·410 페이지)은 파싱 실패다.
  둘을 섞으면 이번처럼 삼성전자가 '공시 없음'이 된다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from stock_mcp_server import naver


ROWS = [
    {"itemCode": "036090", "disclosureId": 90644244,
     "title": "(주)위지트 전환가액의조정", "datetime": "2026-08-03T06:52:44",
     "author": "KOSCOM"},
    {"itemCode": "036090", "disclosureId": 88682015,
     "title": "(주)위지트 전환가액의조정", "datetime": "2026-07-31T06:53:43",
     "author": "KOSCOM"},
    {"itemCode": "036090", "disclosureId": 1, "title": "", "datetime": "2026-07-01T06:50:00",
     "author": "KOSCOM"},  # 제목 없는 행은 버린다
]


def _call(payload):
    with patch.object(naver, "_api_json", AsyncMock(return_value=payload)) as api:
        result = asyncio.run(naver.get_disclosure_list.__wrapped__("036090"))
    return result, api


def test_rows_keep_old_shape_and_date_only():
    items, api = _call(ROWS)
    assert [it["date"] for it in items] == ["2026-08-03", "2026-07-31"]
    assert items[0]["title"] == "(주)위지트 전환가액의조정"
    assert items[0]["source"] == "KOSCOM"
    assert items[0]["link"] == ""
    assert "time" not in items[0]
    url = api.call_args.args[0]
    assert url == "https://m.stock.naver.com/api/stock/036090/disclosure"
    assert api.call_args.kwargs["params"] == {"pageSize": 30, "page": 1}


def test_empty_list_is_no_disclosure_not_failure():
    items, _ = _call([])
    assert items == []


def test_non_list_payload_is_parse_failure():
    with pytest.raises(naver.NaverParseError):
        _call({"message": "Route not found"})


def test_html_response_is_parse_failure_not_empty():
    class _Resp:
        status_code = 410
        text = "<html>Npay 증권</html>"

        def json(self):
            raise ValueError("not json")

    with patch.object(naver, "fetch", AsyncMock(return_value=_Resp())):
        with pytest.raises(naver.NaverParseError):
            asyncio.run(naver.get_disclosure_list.__wrapped__("005930"))
