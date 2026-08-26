"""메타 규약 v3 회귀 - 요청한 범위와 실제로 돌려준 범위가 갈라져 보이는가.

이 파일이 막는 것은 하나다: **도구가 조용히 잘라놓고 아무 말도 안 하는 것.**

get_flow_batch(days=60)은 서버 상한 20일에 걸려 20일치만 돌려준다. 그런데
지금까지 응답에는 "최근 20일"이라고만 적혀 있었다. 60일을 물어본 사람은 그게
자기가 요청한 범위인 줄 알고 20일치를 60일 흐름으로 읽는다. 요청값과 적용값을
나란히 싣고 data_completeness=partial 로 내려야 그 오독이 구조적으로 막힌다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
        return json.loads(payload)
    return json.loads(text)["_meta"]


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
}


def _flow_rows(n: int) -> list[dict]:
    return [
        {"date": f"2026.08.{day:02d}", "institutional": 1, "foreign": 2}
        for day in range(1, n + 1)
    ]


class FlowBatchCoverageTests(unittest.IsolatedAsyncioTestCase):
    """SL-01. 수급 배치의 days 상한(20)이 사용자에게 보이는가."""

    async def _run(self, codes, days, flow=None):
        flow = flow if flow is not None else AsyncMock(return_value=_flow_rows(20))
        with patch.object(server, "get_investor_flow", flow), \
             patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_flow_batch(codes, days=days)

    async def test_sixty_day_request_exposes_twenty_day_cap(self):
        text = await self._run(["005930"], 60)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "day", "value": 60})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 20})
        self.assertTrue(meta["coverage"]["truncated"])
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "server_cap")
        self.assertEqual(meta["data_completeness"], "partial")
        # 본문만 읽는 사용자에게도 보여야 한다. 메타는 AI만 본다.
        self.assertIn("60일", text)
        self.assertIn("20일", text)

    async def test_twenty_day_request_is_not_truncated(self):
        text = await self._run(["005930"], 20)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "day", "value": 20})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "day", "value": 20})
        self.assertFalse(meta["coverage"]["truncated"])
        self.assertTrue(meta["coverage"]["coverage_complete"])
        self.assertIsNone(meta["coverage"]["reason"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_default_five_days_stays_five(self):
        """기본값은 건드리지 않는다. 5일 요청은 5일 그대로가 정답이다."""
        text = await self._run(["005930"], 5, AsyncMock(return_value=_flow_rows(5)))
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["effective"]["value"], 5)
        self.assertFalse(meta["coverage"]["truncated"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_returned_count_is_per_entity(self):
        """상한 안이어도 종목마다 실제로 몇 행 왔는지는 다르다(신규 상장 등)."""
        async def uneven(code, days):
            return _flow_rows(20 if code == "005930" else 3)

        text = await self._run(["005930", "000660"], 20, AsyncMock(side_effect=uneven))
        meta = extract_meta(text)
        self.assertEqual(
            meta["coverage"]["per_entity_returned_count"],
            {"005930": 20, "000660": 3},
        )
        self.assertEqual(meta["coverage"]["returned_count"], 23)

    async def test_partial_failure_is_partial(self):
        """일부 종목이 실패했으면 전체를 complete 라고 말할 수 없다."""
        async def half_broken(code, days):
            if code == "000660":
                raise RuntimeError("네이버 응답 없음")
            return _flow_rows(20)

        text = await self._run(["005930", "000660"], 20, AsyncMock(side_effect=half_broken))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["per_entity_returned_count"]["000660"], 0)

    async def test_truncated_and_failed_together_still_partial(self):
        async def half_broken(code, days):
            if code == "000660":
                raise RuntimeError("네이버 응답 없음")
            return _flow_rows(20)

        text = await self._run(["005930", "000660"], 60, AsyncMock(side_effect=half_broken))
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(meta["coverage"]["truncated"])
        self.assertEqual(meta["coverage"]["reason"], "server_cap")

    async def test_body_table_shape_is_unchanged(self):
        """기존 본문 형식은 유지한다. 경고 한 줄만 늘어난다."""
        text = await self._run(["005930"], 5, AsyncMock(return_value=_flow_rows(5)))
        self.assertIn("각 행: 날짜 | 기관 순매매(주) | 외국인 순매매(주)", text)
        self.assertIn("※ 양수=순매수", text)
        self.assertIn("합계(5일)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# SL-02. 마지막 봉이 아직 안 끝났다는 사실
# ---------------------------------------------------------------------------


def _bar(date: str) -> dict:
    return {"date": date, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}


def _kst(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=rmeta.KST)


class BarStateTests(unittest.TestCase):
    """장이 닫혔다고 봉이 끝난 것은 아니다.

    수요일 저녁이면 `data_basis`는 last_close 라서 "확정치"로 읽힌다. 그런데
    그 시점의 **주봉**은 금요일까지 두 거래일이 더 남아 있어 아직 안 끝났다.
    같은 주봉으로 계산한 이평·RSI는 금요일에 값이 바뀐다. 세션 상태만으로는
    이 구분이 안 나와서, 봉 구간이 닫혔는지를 따로 계산해 싣는다.
    """

    def setUp(self):
        from stock_mcp_server import market_clock as mc
        self.cal = mc.krx_calendar()

    def test_current_week_bar_is_incomplete_on_wednesday(self):
        state = server._bar_state(
            timeframe="week",
            rows=[_bar("20260821"), _bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=self.cal,
        )
        self.assertEqual(state, {
            "timeframe": "week",
            "last_bar_date": "2026-08-26",
            "last_bar_complete": False,
            "last_completed_bar_date": "2026-08-21",
            "calculation_includes_incomplete": True,
        })

    def test_daily_bar_during_session_is_incomplete(self):
        state = server._bar_state(
            timeframe="day",
            rows=[_bar("20260825"), _bar("20260826")],
            now=_kst(2026, 8, 26, 11),
            market_calendar=self.cal,
        )
        self.assertFalse(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-08-25")
        self.assertTrue(state["calculation_includes_incomplete"])

    def test_daily_bar_after_close_is_complete(self):
        state = server._bar_state(
            timeframe="day",
            rows=[_bar("20260825"), _bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=self.cal,
        )
        self.assertTrue(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-08-26")
        self.assertFalse(state["calculation_includes_incomplete"])

    def test_month_bar_in_progress(self):
        state = server._bar_state(
            timeframe="month",
            rows=[_bar("20260731"), _bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=self.cal,
        )
        self.assertFalse(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-07-31")

    def test_month_bar_on_last_trading_day_after_close(self):
        state = server._bar_state(
            timeframe="month",
            rows=[_bar("20260731"), _bar("20260831")],
            now=_kst(2026, 8, 31, 21),
            market_calendar=self.cal,
        )
        self.assertTrue(state["last_bar_complete"])

    def test_holiday_shortened_week_closes_early(self):
        """추석으로 목·금이 쉬는 주. 수요일 마감이면 그 주봉은 끝난 것이다.

        달력을 안 보고 '금요일이어야 끝'이라고 두면 여기서 틀린다.
        """
        state = server._bar_state(
            timeframe="week",
            rows=[_bar("20260918"), _bar("20260923")],
            now=_kst(2026, 9, 23, 21),
            market_calendar=self.cal,
        )
        self.assertTrue(state["last_bar_complete"], "2026-09-24/25는 추석 휴장")

    def test_holiday_shortened_week_still_open_midsession(self):
        state = server._bar_state(
            timeframe="week",
            rows=[_bar("20260918"), _bar("20260923")],
            now=_kst(2026, 9, 23, 11),
            market_calendar=self.cal,
        )
        self.assertFalse(state["last_bar_complete"])

    def test_unknown_calendar_does_not_guess_completion(self):
        """달력을 못 구했으면 모른다고 한다. 추측한 값이 확정치로 읽힌다."""
        state = server._bar_state(
            timeframe="week",
            rows=[_bar("20260821"), _bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=None,
        )
        self.assertIsNone(state["last_bar_complete"])
        self.assertIsNone(state["calculation_includes_incomplete"])
        self.assertEqual(state["last_bar_date"], "2026-08-26")

    def test_broken_calendar_does_not_raise(self):
        class Boom:
            def is_period_closed(self, *a, **k):
                raise RuntimeError("달력 조회 실패")

        state = server._bar_state(
            timeframe="day",
            rows=[_bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=Boom(),
        )
        self.assertIsNone(state["last_bar_complete"])

    def test_no_rows_yields_no_state(self):
        self.assertIsNone(
            server._bar_state(timeframe="day", rows=[], market_calendar=self.cal)
        )


_WED_EVENING = _kst(2026, 8, 26, 21)      # 수요일 장 마감 후. 일봉은 끝났고 주봉은 남았다
_WED_SESSION = _kst(2026, 8, 26, 11)      # 수요일 장중


class BarStateInToolsTests(unittest.IsolatedAsyncioTestCase):
    """봉 상태가 실제 도구 응답까지 실려 나가는가."""

    def _patches(self, ohlcv, now):
        return (
            patch.object(server, "get_ohlcv", AsyncMock(return_value=ohlcv)),
            patch.object(server, "build_market_clock", return_value=_CLOSED),
            patch.object(server, "_now_kst", return_value=now),
        )

    async def _chart(self, timeframe, rows, now, count=None):
        a, b, c = self._patches(rows, now)
        with a, b, c:
            return await server.get_chart(
                code="005930", timeframe=timeframe, count=count or len(rows)
            )

    async def test_weekly_chart_after_close_still_flags_open_bar(self):
        """장은 닫혔다. 그래도 이번 주 주봉은 금요일까지 안 끝났다."""
        text = await self._chart("week", [_bar("20260821"), _bar("20260826")], _WED_EVENING)
        meta = extract_meta(text)
        self.assertEqual(meta["bar_state"]["timeframe"], "week")
        self.assertFalse(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["bar_state"]["last_completed_bar_date"], "2026-08-21")
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(meta["coverage"]["reason"], "incomplete_tail")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertIn("2026-08-21", text)

    async def test_daily_chart_after_close_is_complete(self):
        text = await self._chart("day", [_bar("20260825"), _bar("20260826")], _WED_EVENING)
        meta = extract_meta(text)
        self.assertTrue(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["data_completeness"], "complete")
        self.assertNotIn("coverage", meta)

    async def test_daily_chart_during_session_is_partial(self):
        text = await self._chart("day", [_bar("20260825"), _bar("20260826")], _WED_SESSION)
        meta = extract_meta(text)
        self.assertFalse(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["coverage"]["reason"], "incomplete_tail")
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_indicators_carry_bar_state(self):
        rows = [_bar(f"2026{m:02d}{d:02d}") for m in (6, 7) for d in range(1, 29)]
        rows.append(_bar("20260826"))
        a, b, c = self._patches(rows, _WED_EVENING)
        with a, b, c:
            payload = json.loads(
                await server.get_indicators(code="005930", days=60, timeframe="week")
            )
        state = payload["_meta"]["bar_state"]
        self.assertEqual(state["timeframe"], "week")
        self.assertTrue(state["calculation_includes_incomplete"])
        self.assertEqual(payload["_meta"]["data_completeness"], "partial")

    async def test_indicators_bulk_carries_bar_state(self):
        rows = [_bar(f"202607{d:02d}") for d in range(1, 29)] + [_bar("20260826")]
        a, b, c = self._patches(rows, _WED_EVENING)
        with a, b, c:
            payload = json.loads(
                await server.get_indicators_bulk(
                    codes=["005930", "000660"], days=60, timeframe="week"
                )
            )
        state = payload["_meta"]["bar_state"]
        self.assertEqual(state["last_bar_date"], "2026-08-26")
        self.assertTrue(state["calculation_includes_incomplete"])
        self.assertEqual(payload["_meta"]["data_completeness"], "partial")

    async def test_multi_chart_stats_carries_bar_state(self):
        stats = [{
            "code": "005930", "bars_count": 260, "current_price": 70000,
            "current_date": "20260826", "high": 90000, "high_date": "20260101",
            "low": 60000, "low_date": "20260601", "drawdown_pct": -22.2,
            "recovery_pct": 16.6, "period_return_pct": 5.0, "avg_volume": 100,
        }]
        with patch.object(server, "naver_get_multi_chart_stats", AsyncMock(return_value=stats)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_SESSION):
            text = await server.get_multi_chart_stats(codes=["005930"], days=260)
        meta = extract_meta(text)
        self.assertEqual(meta["bar_state"]["timeframe"], "day")
        self.assertFalse(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_bar_values_are_untouched(self):
        """메타만 늘어난다. 봉 값과 본문 표는 그대로다."""
        rows = [_bar("20260825"), _bar("20260826")]
        text = await self._chart("week", rows, _WED_EVENING)
        self.assertIn("종목 005930 주봉 OHLCV (2개 봉)", text)


# ---------------------------------------------------------------------------
# SL-03. 이 가격이 무엇으로 조정된 값인지
# ---------------------------------------------------------------------------

_UNKNOWN_ADJ = {
    "status": "unknown",
    "corporate_actions_checked": False,
    "cross_event_comparison_safe": False,
}


class PriceAdjustmentTests(unittest.IsolatedAsyncioTestCase):
    """액면분할·유상증자 전후를 그냥 이어 붙이면 안 되는데, 지금까지 응답에는
    이 가격이 수정주가인지 원주가인지 적힌 곳이 없었다.

    원천이 알려주지 않으므로 정답은 "모른다"다. 모른다고 적어야 6개월 전 공시
    반응과 지금을 나란히 놓기 전에 한 번 멈춘다.
    """

    async def _chart(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=[_bar("20260825"), _bar("20260826")])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            return await server.get_chart(code="005930", timeframe="day", count=2)

    async def test_chart_reports_unknown_adjustment(self):
        meta = extract_meta(await self._chart())
        self.assertEqual(meta["price_adjustment"], _UNKNOWN_ADJ)

    async def test_indicators_report_unknown_adjustment(self):
        rows = [_bar(f"202607{d:02d}") for d in range(1, 29)]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators(code="005930", days=60))
        self.assertEqual(payload["_meta"]["price_adjustment"], _UNKNOWN_ADJ)

    async def test_indicators_bulk_reports_unknown_adjustment(self):
        rows = [_bar(f"202607{d:02d}") for d in range(1, 29)]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators_bulk(codes=["005930"], days=60))
        self.assertEqual(payload["_meta"]["price_adjustment"], _UNKNOWN_ADJ)

    async def test_period_stats_warn_about_corporate_actions(self):
        """기간수익률은 구간 양 끝을 이어 붙인 값이라 특히 위험하다."""
        stats = [{
            "code": "005930", "bars_count": 260, "current_price": 70000,
            "current_date": "20260826", "high": 90000, "high_date": "20260101",
            "low": 60000, "low_date": "20260601", "drawdown_pct": -22.2,
            "recovery_pct": 16.6, "period_return_pct": 5.0, "avg_volume": 100,
        }]
        with patch.object(server, "naver_get_multi_chart_stats", AsyncMock(return_value=stats)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            text = await server.get_multi_chart_stats(codes=["005930"], days=260)
        meta = extract_meta(text)
        self.assertEqual(meta["price_adjustment"], _UNKNOWN_ADJ)
        self.assertIn("기업행위", text)

    async def test_event_reactions_report_unknown_adjustment(self):
        items = [{"date": "2026-08-20", "title": "단일판매·공급계약 체결"}]
        rows = [_bar(f"202608{d:02d}") for d in range(1, 27)]
        flows = [{"date": f"2026.08.{d:02d}", "institutional": 1, "foreign": 2}
                 for d in range(1, 27)]
        with patch.object(server, "naver_get_disclosure_list", AsyncMock(return_value=items)), \
             patch.object(server, "get_ohlcv", AsyncMock(return_value=rows)), \
             patch.object(server, "get_investor_flow", AsyncMock(return_value=flows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            text = await server.get_event_reactions(code="005930", max_events=1, after=4)
        meta = extract_meta(text)
        self.assertEqual(meta["price_adjustment"], _UNKNOWN_ADJ)
        self.assertIn("기업행위", text)

    def test_known_source_status_is_passed_through(self):
        """원천이 조정 기준을 알려주면 그대로 전달하고, 비교 가능으로 표시한다."""
        from stock_mcp_server.event_reaction import price_adjustment_meta

        self.assertEqual(price_adjustment_meta("split_adjusted"), {
            "status": "split_adjusted",
            "corporate_actions_checked": True,
            "cross_event_comparison_safe": True,
        })
        self.assertEqual(price_adjustment_meta("total_return_adjusted")["status"],
                         "total_return_adjusted")
        self.assertEqual(price_adjustment_meta("raw")["status"], "raw")

    def test_unrecognized_source_status_falls_back_to_unknown(self):
        """모르는 라벨을 그대로 실어 보내면 읽는 쪽이 확인된 값으로 오해한다."""
        from stock_mcp_server.event_reaction import price_adjustment_meta

        for bogus in (None, "", "adjusted", "수정주가", 3):
            self.assertEqual(price_adjustment_meta(bogus), _UNKNOWN_ADJ, bogus)


# ---------------------------------------------------------------------------
# SL-04/05/06. 재무 기간 혼재 · 공시 표본 · 지표 이력 부족
# ---------------------------------------------------------------------------


def _fin(name, periods_annual, periods_quarterly, per):
    """get_financials 반환 흉내. 값 리스트는 라벨 수와 길이가 맞아야 한다."""
    labels = list(periods_annual) + list(periods_quarterly)
    return {
        "name": name,
        "_periods": {"annual": list(periods_annual), "quarterly": list(periods_quarterly)},
        # 키는 _FIN_COMPARE_ROWS 의 첫 원소다(라벨이 아니라).
        "PER(배)": [str(per)] * len(labels),
        "PBR(배)": ["1.0"] * len(labels),
        "ROE(지배주주)": ["10.0"] * len(labels),
        "영업이익률": ["5.0"] * len(labels),
        "부채비율": ["50.0"] * len(labels),
        "시가배당률(%)": ["2.0"] * len(labels),
    }


class FinancialPeriodCoverageTests(unittest.IsolatedAsyncioTestCase):
    """종목마다 기준 분기가 다르면 그 표는 비교표가 아니다.

    A는 2026.06 확정, B는 아직 2026.03 이면 PER 을 나란히 놓는 순간 서로 다른
    시점을 비교하게 된다. 표에는 '기준' 열이 있지만 열을 안 보면 그만이고,
    메타에는 그 사실이 아예 없었다.
    """

    async def _run(self, mapping):
        async def fake(code):
            return mapping[code]

        with patch.object(server, "get_financials", AsyncMock(side_effect=fake)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_financial_batch(list(mapping))

    async def test_mixed_periods_are_partial(self):
        text = await self._run({
            "005930": _fin("삼성전자", ["2024.12", "2025.12"], ["2026.03", "2026.06"], 12),
            "096770": _fin("SK이노베이션", ["2024.12", "2025.12"], ["2025.12", "2026.03"], 8),
        })
        meta = extract_meta(text)
        pc = meta["period_coverage"]
        self.assertEqual(pc["consistency"], "mixed")
        self.assertEqual(pc["periods"], {"005930": "2026.06", "096770": "2026.03"})
        self.assertEqual(pc["oldest"], "2026.03")
        self.assertEqual(pc["newest"], "2026.06")
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertIn("기준 기간", text)

    async def test_uniform_periods_stay_complete(self):
        text = await self._run({
            "005930": _fin("삼성전자", ["2024.12", "2025.12"], ["2026.03", "2026.06"], 12),
            "000660": _fin("SK하이닉스", ["2024.12", "2025.12"], ["2026.03", "2026.06"], 20),
        })
        meta = extract_meta(text)
        self.assertEqual(meta["period_coverage"]["consistency"], "uniform")
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_no_period_is_unknown_not_uniform(self):
        """기준을 못 읽었으면 '전부 같다'가 아니라 '모른다'다."""
        text = await self._run({"005930": {"name": "삼성전자", "_periods": {}}})
        meta = extract_meta(text)
        self.assertEqual(meta["period_coverage"]["consistency"], "unknown")


class DisclosureSampleTests(unittest.IsolatedAsyncioTestCase):
    """이 목록은 '최근 몇 건'이지 '전체'가 아니다."""

    async def _run(self, items):
        with patch.object(server, "naver_get_disclosure_list", AsyncMock(return_value=items)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            return await server.get_disclosure(code="005930")

    async def test_disclosure_is_explicitly_a_recent_sample(self):
        items = [{"date": f"2026.08.{d:02d}", "title": f"공시 {d}", "source": "코스콤"}
                 for d in range(1, 21)]
        text = await self._run(items)
        meta = extract_meta(text)
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertIsNone(meta["coverage"]["total_count"])
        self.assertEqual(meta["coverage"]["returned_count"], 20)
        self.assertEqual(meta["coverage"]["reason"], "source_limit")
        self.assertTrue(meta["title_may_be_truncated"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertIn("전체 공시가 아니라", text)

    async def test_single_item_is_still_a_sample(self):
        """한 건만 왔다고 '이 종목 공시는 하나뿐'이 아니다."""
        text = await self._run([{"date": "2026.08.26", "title": "단일판매", "source": "코스콤"}])
        meta = extract_meta(text)
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "partial")


class IndicatorCoverageTests(unittest.IsolatedAsyncioTestCase):
    """봉이 모자라면 지표는 조용히 없어진다. 없는 것과 안 나온 것은 다르다."""

    def _rows(self, n):
        return [_bar(f"2026{(i // 28) % 12 + 1:02d}{i % 28 + 1:02d}") for i in range(n)]

    async def test_ma120_with_104_weekly_bars_exposes_shortfall(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=self._rows(104))), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators(
                code="005930", days=104, include=["ma"], timeframe="week"
            ))
        coverage = payload["_meta"]["indicator_coverage"]
        self.assertEqual(coverage["available_bars"], 104)
        self.assertEqual(coverage["required_bars"]["ma120"], 120)
        self.assertIn("ma120", coverage["insufficient"])
        self.assertNotIn("ma60", coverage["insufficient"])
        self.assertEqual(payload["_meta"]["data_completeness"], "partial")

    async def test_enough_bars_is_complete(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=self._rows(300))), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators(
                code="005930", days=300, include=["ma", "rsi"]
            ))
        coverage = payload["_meta"]["indicator_coverage"]
        self.assertEqual(coverage["insufficient"], [])
        self.assertEqual(coverage["required_bars"]["rsi"], 15)

    async def test_params_override_changes_requirement(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=self._rows(40))), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators(
                code="005930", days=40, include=["rsi"], params={"rsi": {"period": 60}}
            ))
        coverage = payload["_meta"]["indicator_coverage"]
        self.assertEqual(coverage["required_bars"]["rsi"], 61)
        self.assertIn("rsi", coverage["insufficient"])

    async def test_bulk_uses_worst_case_bars(self):
        """배치에서는 가장 짧은 종목을 기준으로 잡는다. 과대 주장을 하지 않는다."""
        async def uneven(code, timeframe="day", count=260):
            return self._rows(300 if code == "005930" else 40)

        with patch.object(server, "get_ohlcv", AsyncMock(side_effect=uneven)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators_bulk(
                codes=["005930", "000660"], days=300, include=["ma"]
            ))
        coverage = payload["_meta"]["indicator_coverage"]
        self.assertEqual(coverage["available_bars"], 40)
        self.assertIn("ma60", coverage["insufficient"])
# ---------------------------------------------------------------------------
# 회귀 - 리뷰에서 나온 허위 complete 5건
# ---------------------------------------------------------------------------


class BatchBarStateTests(unittest.IsolatedAsyncioTestCase):
    """배치는 종목별로 봉 상태를 계산한 뒤 합쳐야 한다.

    종목들의 "마지막 날짜"만 모아 하나의 시계열인 척 넘기면, 두 종목이 같은
    미완성 봉을 가질 때 직전 원소가 자기 자신이 되어 확정 봉 날짜가 미완성 봉
    날짜로 나온다.
    """

    WEEK_ROWS = [_bar("20260814"), _bar("20260821"), _bar("20260826")]

    async def test_bulk_reports_the_real_previous_completed_bar(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=self.WEEK_ROWS * 12)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators_bulk(
                codes=["005930", "000660"], days=60, timeframe="week"
            ))
        state = payload["_meta"]["bar_state"]
        self.assertEqual(state["last_bar_date"], "2026-08-26")
        self.assertFalse(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-08-21")

    async def test_bulk_takes_the_newest_entity_as_the_batch_state(self):
        """한 종목만 최신 봉을 갖고 있어도 배치에는 미완성 봉이 섞인 것이다."""
        async def uneven(code, timeframe="day", count=260):
            if code == "005930":
                return self.WEEK_ROWS * 12
            return [_bar("20260814"), _bar("20260821")] * 18

        with patch.object(server, "get_ohlcv", AsyncMock(side_effect=uneven)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            payload = json.loads(await server.get_indicators_bulk(
                codes=["005930", "000660"], days=60, timeframe="week"
            ))
        state = payload["_meta"]["bar_state"]
        self.assertEqual(state["last_bar_date"], "2026-08-26")
        self.assertFalse(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-08-21")

    async def test_multi_chart_stats_names_the_previous_trading_day(self):
        stats = [{
            "code": c, "bars_count": 260, "current_price": 70000,
            "current_date": "20260826", "high": 90000, "high_date": "20260101",
            "low": 60000, "low_date": "20260601", "drawdown_pct": -22.2,
            "recovery_pct": 16.6, "period_return_pct": 5.0, "avg_volume": 100,
        } for c in ("005930", "000660")]
        with patch.object(server, "naver_get_multi_chart_stats", AsyncMock(return_value=stats)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_WED_SESSION):
            text = await server.get_multi_chart_stats(codes=["005930", "000660"], days=260)
        state = extract_meta(text)["bar_state"]
        self.assertFalse(state["last_bar_complete"])
        self.assertEqual(state["last_completed_bar_date"], "2026-08-25")

    def test_repeated_dates_never_point_at_themselves(self):
        """같은 날짜가 겹쳐 들어와도 확정 봉이 자기 자신이 되면 안 된다."""
        from stock_mcp_server import market_clock as mc

        state = server._bar_state(
            timeframe="week",
            rows=[_bar("20260826"), _bar("20260826")],
            now=_kst(2026, 8, 26, 21),
            market_calendar=mc.krx_calendar(),
        )
        self.assertFalse(state["last_bar_complete"])
        self.assertIsNone(state["last_completed_bar_date"])


class FalseCompleteRegressionTests(unittest.IsolatedAsyncioTestCase):
    """모른다와 일부만 왔다를 complete 로 적던 자리들."""

    async def test_short_rows_for_one_stock_make_the_batch_partial(self):
        """20일을 요청해 3일만 온 종목이 섞였는데 전체가 complete 일 수 없다."""
        async def uneven(code, days):
            return _flow_rows(20 if code == "005930" else 3)

        with patch.object(server, "get_investor_flow", AsyncMock(side_effect=uneven)), \
             patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.get_flow_batch(["005930", "000660"], days=20)
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["per_entity_returned_count"],
                         {"005930": 20, "000660": 3})
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "source_limit")
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_full_rows_everywhere_stay_complete(self):
        with patch.object(server, "get_investor_flow",
                          AsyncMock(return_value=_flow_rows(20))), \
             patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=[])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.get_flow_batch(["005930", "000660"], days=20)
        meta = extract_meta(text)
        self.assertTrue(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_unknown_bar_completion_is_partial(self):
        """봉 마감 여부를 판별 못 했으면 확정치라고 말할 수 없다."""
        with patch.object(server, "get_ohlcv",
                          AsyncMock(return_value=[_bar("20260825"), _bar("20260826")])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "krx_calendar", lambda: None), \
             patch.object(server, "_now_kst", return_value=_WED_EVENING):
            text = await server.get_chart(code="005930", timeframe="week", count=2)
        meta = extract_meta(text)
        self.assertIsNone(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "unknown")

    async def test_unknown_financial_period_is_partial(self):
        """기준 기간을 하나도 못 읽었으면 전부 같다가 아니라 모른다다."""
        async def fake(code):
            return {"name": "삼성전자", "_periods": {}}

        with patch.object(server, "get_financials", AsyncMock(side_effect=fake)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.get_financial_batch(["005930"])
        meta = extract_meta(text)
        self.assertEqual(meta["period_coverage"]["consistency"], "unknown")
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["coverage"]["reason"], "unknown")

    async def test_mixed_financial_period_reason_is_mixed_periods(self):
        mapping = {
            "005930": _fin("삼성전자", ["2024.12", "2025.12"], ["2026.03", "2026.06"], 12),
            "096770": _fin("SK이노베이션", ["2024.12", "2025.12"], ["2025.12", "2026.03"], 8),
        }

        async def one(code):
            return mapping[code]

        with patch.object(server, "get_financials", AsyncMock(side_effect=one)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            text = await server.get_financial_batch(list(mapping))
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["reason"], "mixed_periods")
        self.assertEqual(meta["data_completeness"], "partial")
