"""목록 도구가 말없이 잘리지 않는다 — 시가총액 순위·업종·테마 구성종목.

2026-09-17 블로그 데이터 조사 중 실측한 결함 두 가지:

1. get_market_cap_ranking 은 count 를 500 에서 자르고 쪽 인자가 없어서 501위 아래를
   받을 방법이 없었다. 네이버 목록 API 의 startIdx 는 오프셋이 아니라 **0부터 세는
   쪽 번호**다(pageSize=500, startIdx=1 → 501~1000위, startIdx=500 → 빈 목록).
2. get_sector_stocks 는 count 를 50 에서 잘랐다. 목록이 등락률 내림차순이라 그날 많이
   내린 종목이 빠지는데, 머리말은 "(50개 종목)"이라 잘린 줄 몰랐다(제약 175종목).
   같은 코드를 쓰는 get_theme_stocks 도 같았고(2차전지 143종목), 구성종목을 10쪽
   (1,000행)에서 멈춰 '기타'(1,537종목)는 count 를 키워도 뒤가 빠졌다.

지키는 계약: 표에 담긴 게 목록의 일부이면 머리말이 전체 수와 그 범위를 말하고,
다음 쪽 호출법이 나오고, 메타 coverage 가 그 사실을 싣는다. 네트워크는 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import naver as _naver
from stock_mcp_server import server
from stock_mcp_server._cache import clear_cache

_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-09-16"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-09-16"},
}


def _meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


def _body(text: str) -> str:
    return text.split(rmeta.MARKER_START, 1)[0]


def _row_count(body: str) -> int:
    """표의 종목 행 수 (머리행 '코드 | 종목명' 은 세지 않는다)."""
    return len(re.findall(r"^\d{6} \| 종목\d+ \|", body, re.M))


# ---------------------------------------------------------------------------
# 가짜 네이버
# ---------------------------------------------------------------------------

def _market_rows(n: int, *, types_by_index: dict | None = None,
                 halted: set | None = None) -> list[dict]:
    """시가총액 내림차순 시장 목록 흉내. i 번째 행의 시총은 (n - i)억원."""
    types_by_index = types_by_index or {}
    halted = halted or set()
    return [
        {
            "itemcode": f"{i + 1:06d}",
            "itemname": f"종목{i + 1}",
            "type": types_by_index.get(i, "ST"),
            "tradeStopYn": "Y" if i in halted else "N",
            "nowPrice": "1000",
            "prevChangeRate": "-1.00",
            "tradeVolume": "10",
            "marketSum": str((n - i) * 100_000_000),
            "tradingSessionType": "REGULAR_MARKET",
        }
        for i in range(n)
    ]


def _member_rows(n: int, start: int = 1) -> list[dict]:
    """업종·테마 구성종목 흉내. 앞쪽일수록 많이 오른 종목."""
    return [
        {
            "itemCode": f"{i:06d}",
            "stockName": f"종목{i}",
            "closePriceRaw": "1000",
            "fluctuationsRatio": f"{(n / 2 - i) / 10:.2f}",
            "accumulatedTradingVolumeRaw": "10",
        }
        for i in range(start, start + n)
    ]


class _FakeNaver(unittest.IsolatedAsyncioTestCase):
    """URL 과 쪽 인자를 보고 응답하는 fetch 대역. 받은 요청을 모두 적어 둔다."""

    def setUp(self):
        self.requests: list[tuple[str, dict]] = []
        self.market: list[dict] = []
        self.groups: dict[str, dict] = {}   # kind -> {"no", "name", "rows", "total"}
        self.member_pages: dict | None = None  # (kind, page) -> 강제로 돌려줄 행

        async def _fetch(url, params=None, **kwargs):
            params = dict(params or {})
            self.requests.append((url, params))
            payload = self._route(url, params)
            return types.SimpleNamespace(status_code=200, text="", json=lambda: payload)

        self._real_fetch = _naver.fetch
        _naver.fetch = _fetch
        clear_cache()

    def tearDown(self):
        _naver.fetch = self._real_fetch
        clear_cache()

    def _route(self, url: str, params: dict):
        if url.endswith("/domestic/market/stock/default"):
            size, idx = int(params["pageSize"]), int(params["startIdx"])
            return self.market[idx * size:(idx + 1) * size]   # startIdx = 쪽 번호
        for kind, g in self.groups.items():
            if url.endswith(f"/stocks/{kind}"):
                return {"groups": [{"no": g["no"], "name": g["name"]}]}
            if url.endswith(f"/stocks/{kind}/{g['no']}"):
                page, size = int(params["page"]), int(params["pageSize"])
                if self.member_pages and (kind, page) in self.member_pages:
                    rows = self.member_pages[(kind, page)]
                else:
                    rows = g["rows"][(page - 1) * size:page * size]
                return {"stocks": rows, "totalCount": g["total"], "page": page}
        raise AssertionError(f"예상하지 못한 요청: {url} {params}")

    def _group(self, kind: str, name: str, n: int, *, total: int | None = None):
        self.groups[kind] = {"no": "261", "name": name, "rows": _member_rows(n),
                             "total": n if total is None else total}

    def _market_starts(self) -> list[int]:
        return [p["startIdx"] for u, p in self.requests
                if u.endswith("/domestic/market/stock/default")]


# ---------------------------------------------------------------------------
# 1. 시가총액 순위 — naver 계층
# ---------------------------------------------------------------------------

class MarketCapPageTests(_FakeNaver):
    async def test_start_idx_is_a_page_number_not_an_offset(self):
        """startIdx=500 을 보내면 빈 목록이 온다. 쪽 번호 0,1,2,3 으로 끝까지 받는다."""
        self.market = _market_rows(1820)
        result = await _naver.get_market_cap_page("KOSDAQ", 500, 1)

        self.assertEqual(self._market_starts(), [0, 1, 2, 3])
        self.assertTrue(all(p["pageSize"] == 500 for u, p in self.requests))
        self.assertEqual(result["total_count"], 1820)
        self.assertTrue(result["complete"])

    async def test_rank_501_and_below_are_reachable(self):
        self.market = _market_rows(1820)
        last = await _naver.get_market_cap_page("KOSDAQ", 500, 4)

        self.assertEqual(len(last["rows"]), 320)
        self.assertEqual((last["rows"][0]["rank"], last["rows"][-1]["rank"]), (1501, 1820))
        self.assertEqual(last["rows"][0]["code"], "001501")
        self.assertFalse(last["has_next"])
        self.assertEqual(last["pages"], 4)

    async def test_pages_join_into_the_whole_market_once(self):
        self.market = _market_rows(1234)
        codes, page = [], 1
        while True:
            chunk = await _naver.get_market_cap_page("KOSPI", 300, page)
            codes += [r["code"] for r in chunk["rows"]]
            if not chunk["has_next"]:
                break
            page += 1
        self.assertEqual(page, 5)
        self.assertEqual(codes, [r["itemcode"] for r in self.market])

    async def test_later_pages_are_cut_from_the_same_snapshot(self):
        """2쪽을 받으려고 네이버에 다시 가지 않는다 — 그 사이 순위가 바뀌면 겹치거나 빠진다."""
        self.market = _market_rows(700)
        await _naver.get_market_cap_page("KOSPI", 100, 1)
        before = len(self.requests)
        second = await _naver.get_market_cap_page("KOSPI", 100, 2)

        self.assertEqual(len(self.requests), before)
        self.assertEqual(second["rows"][0]["rank"], 101)

    async def test_market_cap_stays_on_its_own_row_when_a_row_is_skipped(self):
        """코드가 이상한 행을 건너뛰어도 시가총액이 옆 종목으로 밀리지 않는다.

        예전 코드는 건너뛴 순위 목록과 원본 행을 zip 해서, 한 행이 빠지면 그 뒤
        전부가 한 칸씩 어긋난 시가총액을 달았다.
        """
        rows = _market_rows(4)
        rows[1]["itemcode"] = "BAD"
        self.market = rows
        result = await _naver.get_market_cap_page("KOSPI", 10, 1)

        by_code = {r["code"]: r["market_cap_billion"] for r in result["rows"]}
        self.assertEqual(by_code, {"000001": 4, "000003": 2, "000004": 1})
        self.assertEqual([r["rank"] for r in result["rows"]], [1, 2, 3])
        self.assertEqual(result["total_count"], 3)

    async def test_reits_funds_and_halted_rows_are_marked_not_dropped(self):
        self.market = _market_rows(6, types_by_index={1: "RT", 2: "IF", 3: "DR", 4: "ZZ"},
                                   halted={5})
        result = await _naver.get_market_cap_page("KOSPI", 10, 1)
        kinds = [(r["security_type"], r["trade_stop"]) for r in result["rows"]]

        self.assertEqual(kinds, [(None, False), ("리츠", False), ("인프라펀드", False),
                                 ("외국기업 DR", False), ("기타(ZZ)", False), (None, True)])
        self.assertEqual(result["type_counts"], {"ST": 2, "RT": 1, "IF": 1, "DR": 1, "ZZ": 1})
        self.assertEqual(result["halted_count"], 1)

    async def test_all_market_is_answered_as_kospi_and_says_so(self):
        self.market = _market_rows(3)
        result = await _naver.get_market_cap_page("ALL", 10, 1)
        self.assertEqual(result["market"], "KOSPI")
        self.assertTrue(all(p["marketType"] == "KOSPI" for u, p in self.requests))

    async def test_repeated_page_stops_and_is_not_called_complete(self):
        """startIdx 해석이 바뀌어 같은 쪽만 되풀이되면 끝을 모르는 것이다 — 전체라 부르지 않는다."""
        self.market = _market_rows(500)
        self._route = lambda url, params: self.market   # 어느 쪽을 달래도 같은 500행
        result = await _naver.get_market_cap_page("KOSPI", 100, 1)

        self.assertEqual(result["total_count"], 500)
        self.assertFalse(result["complete"])
        self.assertEqual(self._market_starts(), [0, 1])

    async def test_legacy_ranking_function_keeps_returning_the_first_page_list(self):
        self.market = _market_rows(30)
        ranked = await _naver.get_market_cap_ranking("KOSPI", 5)
        self.assertIsInstance(ranked, list)
        self.assertEqual([r["rank"] for r in ranked], [1, 2, 3, 4, 5])


# ---------------------------------------------------------------------------
# 2. 업종·테마 구성종목 — naver 계층
# ---------------------------------------------------------------------------

class GroupMemberPageTests(_FakeNaver):
    async def test_sector_reports_its_total_and_window(self):
        self._group("industry", "제약", 175)
        result = await _naver.get_sector_stocks("제약", count=50, page=1)

        self.assertEqual(len(result["stocks"]), 50)
        self.assertEqual(result["total_count"], 175)
        self.assertEqual(result["fetched_count"], 175)
        self.assertEqual(result["start_index"], 1)
        self.assertTrue(result["complete"])

    async def test_members_past_1000_are_not_cut(self):
        """예전엔 10쪽(1,000행)에서 멈춰 '기타' 1,537종목의 뒤 537개가 빠졌다."""
        self._group("industry", "기타", 1537)
        last = await _naver.get_sector_stocks("기타", count=500, page=4)

        self.assertEqual(len(last["stocks"]), 37)
        self.assertEqual(last["stocks"][-1]["code"], "001537")
        self.assertEqual(last["start_index"], 1501)

    async def test_member_pages_join_into_the_whole_sector_once(self):
        self._group("industry", "제약", 175)
        codes = []
        for page in (1, 2, 3, 4):
            chunk = await _naver.get_sector_stocks("제약", count=50, page=page)
            codes += [s["code"] for s in chunk["stocks"]]
        self.assertEqual(codes, [f"{i:06d}" for i in range(1, 176)])

    async def test_duplicate_across_pages_is_counted_once_and_flagged(self):
        """쪽 사이에 순서가 바뀌어 같은 종목이 두 번 오면 한 번만 세고, 모자라면 불완전으로 적는다."""
        self._group("industry", "제약", 150)
        page2 = _member_rows(50, start=101)
        page2[0] = dict(self.groups["industry"]["rows"][99])   # 1쪽 마지막 종목이 2쪽에 또
        self.member_pages = {("industry", 2): page2}
        result = await _naver.get_sector_stocks("제약", count=500, page=1)

        codes = [s["code"] for s in result["stocks"]]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(result["fetched_count"], 149)
        self.assertEqual(result["total_count"], 150)
        self.assertFalse(result["complete"])

    async def test_understated_total_count_does_not_stop_early(self):
        """totalCount 가 실제보다 작아도 모자란 쪽이 올 때까지 받는다."""
        self._group("industry", "제약", 250, total=100)
        result = await _naver.get_sector_stocks("제약", count=500, page=1)

        self.assertEqual(len(result["stocks"]), 250)
        self.assertEqual(result["total_count"], 250)
        self.assertTrue(result["complete"])

    async def test_theme_shares_the_same_paging(self):
        self._group("theme", "2차전지", 143)
        result = await _naver.get_theme_stocks("2차전지", count=100, include_reason=False, page=2)

        self.assertEqual(len(result["stocks"]), 43)
        self.assertEqual(result["total_count"], 143)
        self.assertEqual(result["start_index"], 101)


# ---------------------------------------------------------------------------
# 3. 도구 출력 — 잘렸는데 전체처럼 보이지 않는다
# ---------------------------------------------------------------------------

class _ToolOutput(_FakeNaver):
    def setUp(self):
        super().setUp()
        for target, value in (("is_licensed", lambda: True),
                              ("build_market_clock", lambda: _CLOSED)):
            p = patch.object(server, target, value)
            p.start()
            self.addCleanup(p.stop)


class SectorStocksToolTests(_ToolOutput):
    async def test_truncated_page_names_the_total_not_just_its_rows(self):
        self._group("industry", "제약", 175)
        text = await server.get_sector_stocks("제약", count=50)
        body = _body(text)

        self.assertIn("전체 175개 중 1~50번째", body)
        self.assertNotIn("(50개 종목)", body)
        self.assertIn("`page=2`", body)
        self.assertIn("`count=175`", body)
        cov = _meta(text)["coverage"]
        self.assertEqual((cov["total_count"], cov["returned_count"]), (175, 50))
        self.assertEqual(cov["next_page"], 2)
        self.assertFalse(cov["coverage_complete"])
        self.assertEqual(cov["reason"], "pagination")
        self.assertEqual(_meta(text)["data_completeness"], rmeta.PARTIAL)

    async def test_count_above_50_is_no_longer_silently_cut(self):
        """예전엔 count=175 를 줘도 50개만 나갔다 — 같은 도구 설명이 그렇게 하라고 했는데."""
        self._group("industry", "제약", 175)
        text = await server.get_sector_stocks("제약", count=175)
        body = _body(text)

        self.assertEqual(_row_count(body), 175)
        self.assertIn("전체 175개 종목", body)
        self.assertNotIn("page=", body)
        meta = _meta(text)
        self.assertNotIn("coverage", meta)
        self.assertEqual(meta["data_completeness"], rmeta.COMPLETE)

    async def test_the_most_fallen_stocks_are_on_the_last_page(self):
        self._group("industry", "제약", 175)
        text = await server.get_sector_stocks("제약", count=50, page=4)
        body = _body(text)

        self.assertIn("전체 175개 중 151~175번째", body)
        self.assertIn("| 종목175 |", body)
        self.assertIn("마지막 쪽", body)
        self.assertIsNone(_meta(text)["coverage"]["next_page"])

    async def test_count_over_the_cap_says_it_was_capped(self):
        self._group("industry", "기타", 1537)
        text = await server.get_sector_stocks("기타", count=2000)
        body = _body(text)

        self.assertIn("전체 1,537개 중 1~500번째", body)
        self.assertIn("요청 2000 → 500", body)
        self.assertEqual(_meta(text)["coverage"]["reason"], "server_cap")

    async def test_page_past_the_end_points_to_the_last_page(self):
        self._group("industry", "제약", 175)
        text = await server.get_sector_stocks("제약", count=30, page=9)
        self.assertIn("마지막 쪽은 page=6", text)

    async def test_short_fetch_is_warned_in_body_and_meta(self):
        self._group("industry", "제약", 120, total=175)
        text = await server.get_sector_stocks("제약", count=500)

        self.assertIn("175개인데 120개만 받았습니다", _body(text))
        cov = _meta(text)["coverage"]
        self.assertFalse(cov["coverage_complete"])
        self.assertEqual(cov["reason"], "unknown")


class ThemeStocksToolTests(_ToolOutput):
    async def test_theme_is_not_cut_at_50_and_names_its_total(self):
        self._group("theme", "2차전지", 143)
        whole = _body(await server.get_theme_stocks("2차전지", count=143, include_reason=False))
        self.assertEqual(_row_count(whole), 143)
        self.assertIn("전체 143개 종목", whole)

        part = await server.get_theme_stocks("2차전지", count=10, include_reason=False)
        self.assertIn("전체 143개 중 1~10번째", _body(part))
        self.assertEqual(_meta(part)["coverage"]["next_page"], 2)


class MarketCapToolTests(_ToolOutput):
    async def test_header_names_total_and_rank_window(self):
        self.market = _market_rows(1820)
        text = await server.get_market_cap_ranking("KOSDAQ", count=500, page=2)
        body = _body(text)

        self.assertIn("전체 1,820개 중 501~1,000위 (2/4쪽)", body)
        self.assertIn("`page=3`", body)
        self.assertIn("\n501 | 000501 |", body)
        cov = _meta(text)["coverage"]
        self.assertEqual((cov["total_count"], cov["page"], cov["next_page"]), (1820, 2, 3))
        self.assertTrue(cov["coverage_complete"])   # 요청한 순위 구간은 다 줬다

    async def test_last_page_says_it_is_the_last(self):
        self.market = _market_rows(945)
        body = _body(await server.get_market_cap_ranking("KOSPI", count=500, page=2))
        self.assertIn("501~945위 (2/2쪽)", body)
        self.assertIn("마지막 쪽", body)
        self.assertNotIn("다음 쪽", body)

    async def test_count_over_500_is_reported_as_capped(self):
        self.market = _market_rows(945)
        text = await server.get_market_cap_ranking("KOSPI", count=1000)

        self.assertIn("요청 1000 → 500", _body(text))
        cov = _meta(text)["coverage"]
        self.assertTrue(cov["truncated"])
        self.assertFalse(cov["coverage_complete"])
        self.assertEqual(cov["reason"], "server_cap")
        self.assertEqual(_meta(text)["data_completeness"], rmeta.PARTIAL)

    async def test_security_kind_column_and_composition(self):
        self.market = _market_rows(5, types_by_index={1: "RT"}, halted={3})
        body = _body(await server.get_market_cap_ranking("KOSPI", count=5))

        self.assertIn("| 구분", body)
        self.assertIn("| 종목2 | 1,000 | -1.00% | 4 | 리츠", body)
        self.assertIn("| 종목4 | 1,000 | -1.00% | 2 | 거래정지", body)
        self.assertIn("KOSPI 전체 5개 구분: 주식 4 · 리츠 1. 이 가운데 거래정지 1개.", body)

    async def test_unconfirmed_end_is_warned_and_total_left_out_of_meta(self):
        self.market = _market_rows(500)
        self._route = lambda url, params: self.market
        text = await server.get_market_cap_ranking("KOSPI", count=100)

        self.assertIn("끝을 확인하지 못했습니다", _body(text))
        cov = _meta(text)["coverage"]
        self.assertIsNone(cov["total_count"])
        self.assertFalse(cov["coverage_complete"])
        self.assertEqual(_meta(text)["data_completeness"], rmeta.PARTIAL)

    async def test_plain_stock_page_has_no_kind_column(self):
        self.market = _market_rows(5)
        body = _body(await server.get_market_cap_ranking("KOSPI", count=5))
        self.assertNotIn("| 구분", body)

    async def test_all_market_label_is_not_kept_on_kospi_data(self):
        """예전 머리말은 'ALL'이라고 적고 KOSPI 숫자를 냈다."""
        self.market = _market_rows(5)
        body = _body(await server.get_market_cap_ranking("ALL", count=5))
        self.assertIn("시가총액 상위 (KOSPI)", body)
        self.assertIn("'ALL' 대신 KOSPI", body)


class SectorValuationUniverseTests(_ToolOutput):
    async def test_big_sector_is_not_called_whole_when_only_300_were_read(self):
        """'기타'(1,537종목)에서 300개만 받고 '업종 전체 300개 · 중앙값'이라고 적지 않는다."""
        stocks = [{"code": f"{i:06d}", "name": f"종목{i}"} for i in range(1, 301)]
        listed = {"sector_name": "기타", "sector_id": "25", "stocks": stocks,
                  "total_count": 1537}

        async def scan(codes, days=60, include_financial=True):
            return [{"code": c, "name": c, "per": 10.0 + int(c) % 7, "pbr": 1.0, "roe": 5.0,
                     "시가총액": "100억원", "fin_period": "2026.06"} for c in codes]

        with patch.object(server, "naver_get_sector_stocks", return_value=listed), \
             patch.object(server, "naver_scan_snapshot", side_effect=scan):
            text = await server.get_sector_valuation(sector_name="기타")

        vs = _meta(text)["valuation_sample"]
        self.assertEqual(vs["universe"], 1537)
        self.assertEqual(vs["scope"], "sample")
        self.assertNotIn("업종 전체 **300개**", _body(text))


if __name__ == "__main__":
    unittest.main()
