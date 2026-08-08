"""event_date 기준 주가·수급 반응 계산 테스트.

정상 계산뿐 아니라 **분석 불가 판정(fail-closed)** 을 함께 검증한다.
데이터 없음이 0%·순매매 0으로 새어 나가면 실패해야 한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from stock_mcp_server.event_reaction import (
    UNAVAILABLE_HEADLINES,
    EVENT_AFTER_PRICE_HISTORY,
    EVENT_BEFORE_PRICE_HISTORY,
    FLOW_UNAVAILABLE_FOR_EVENT_WINDOW,
    INSUFFICIENT_POST_EVENT_HISTORY,
    NO_PRICE_HISTORY,
    NO_TRADING_ACTIVITY,
    build_event_reaction,
    format_event_reaction,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_reaction_cases.json"


def _bar(date, close, volume=1000):
    return {
        "date": date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _flow(date, inst, foreign):
    return {
        "date": date,
        "close": 0,
        "volume": 0,
        "institutional": inst,
        "foreign": foreign,
    }


def _normal_bars():
    return [
        _bar("20260508", 100, 1000),
        _bar("20260511", 105, 1100),
        _bar("20260512", 110, 1200),
        _bar("20260513", 120, 1300),
        _bar("20260514", 130, 1400),
        _bar("20260515", 150, 3000),
        _bar("20260518", 165, 2500),
        _bar("20260519", 160, 2000),
        _bar("20260520", 180, 2200),
        _bar("20260521", 210, 2400),
        _bar("20260522", 200, 2600),
    ]


def _normal_flows():
    return [
        _flow("2026.05.14", -100, 200),
        _flow("2026.05.15", 500, 700),
        _flow("2026.05.18", 600, -100),
        _flow("2026.05.19", -200, 300),
        _flow("2026.05.20", 100, 400),
        _flow("2026.05.21", 0, 500),
        _flow("2026.05.22", 200, 600),
    ]


class EventReactionTests(unittest.TestCase):
    def test_uses_exact_event_trading_day(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=_normal_flows(),
            before=5,
            after=5,
        )

        self.assertEqual(reaction["event_date"], "2026-05-15")
        self.assertEqual(reaction["basis_trading_date"], "2026-05-15")
        self.assertEqual(reaction["points"]["D-5"]["date"], "2026-05-08")
        self.assertAlmostEqual(reaction["points"]["D-5"]["return_pct"], -33.33)
        self.assertEqual(reaction["points"]["D+5"]["date"], "2026-05-22")
        self.assertAlmostEqual(reaction["points"]["D+5"]["return_pct"], 33.33)
        self.assertEqual(reaction["flow"]["post"]["institutional"], 1200)
        self.assertEqual(reaction["flow"]["post"]["foreign"], 2400)
        self.assertEqual(reaction["validation"]["status"], "usable")
        self.assertEqual(reaction["validation"]["alignment_reason"], "exact")
        self.assertEqual(reaction["validation"]["alignment_gap_calendar_days"], 0)
        self.assertEqual(reaction["validation"]["codes"], [])

    def test_weekend_event_uses_next_trading_day(self):
        bars = [
            _bar("20260515", 150),
            _bar("20260518", 165),
            _bar("20260519", 160),
        ]

        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-16",
            ohlcv=bars,
            flows=[],
            before=1,
            after=1,
        )

        self.assertEqual(reaction["event_date"], "2026-05-16")
        self.assertEqual(reaction["basis_trading_date"], "2026-05-18")
        self.assertEqual(reaction["points"]["D0"]["close"], 165)
        self.assertEqual(reaction["validation"]["alignment_reason"], "next_trading_session")
        self.assertEqual(reaction["validation"]["alignment_gap_calendar_days"], 2)

    def test_event_before_price_history_blocks_all_returns(self):
        bars = [_bar("20240717", 1000), _bar("20240718", 1100), _bar("20240719", 900)]

        reaction = build_event_reaction(
            code="101670",
            event_date="2022-11-28",
            ohlcv=bars,
            flows=[],
            before=5,
            after=10,
        )
        validation = reaction["validation"]

        self.assertEqual(validation["status"], "unavailable")
        self.assertIn(EVENT_BEFORE_PRICE_HISTORY, validation["codes"])
        self.assertIsNone(reaction["basis_trading_date"])
        self.assertEqual(reaction["points"], {})
        self.assertEqual(validation["earliest_available_date"], "2024-07-17")
        self.assertEqual(validation["latest_available_date"], "2024-07-19")
        self.assertEqual(validation["history_gap_calendar_days"], 597)
        self.assertNotIn("%", format_event_reaction(reaction))

    def test_event_after_price_history_is_distinct_code(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-09-01",
            ohlcv=[_bar("20260515", 150), _bar("20260518", 165)],
            flows=[],
        )

        self.assertEqual(reaction["validation"]["status"], "unavailable")
        self.assertIn(EVENT_AFTER_PRICE_HISTORY, reaction["validation"]["codes"])
        self.assertEqual(reaction["points"], {})

    def test_empty_ohlcv_is_no_price_history_not_zero_reaction(self):
        reaction = build_event_reaction(
            code="005930", event_date="2026-05-15", ohlcv=[], flows=[]
        )

        self.assertEqual(reaction["validation"]["status"], "unavailable")
        self.assertIn(NO_PRICE_HISTORY, reaction["validation"]["codes"])
        self.assertIsNone(reaction["volume"]["basis"])

    def test_zero_volume_event_day_uses_first_tradable_session(self):
        bars = [
            _bar("20241104", 1000, 5000),
            _bar("20241105", 1000, 4000),
            _bar("20241106", 1000, 0),  # 사건일 무거래
            _bar("20241107", 900, 7000),  # 거래 재개
            _bar("20241108", 950, 6000),
        ]

        reaction = build_event_reaction(
            code="348370",
            event_date="2024-11-06",
            ohlcv=bars,
            flows=[],
            before=2,
            after=1,
        )
        validation = reaction["validation"]

        self.assertEqual(reaction["basis_trading_date"], "2024-11-07")
        self.assertEqual(validation["alignment_reason"], "first_tradable_session_after_event")
        self.assertEqual(validation["alignment_gap_calendar_days"], 1)
        self.assertEqual(reaction["points"]["D0"]["close"], 900)
        self.assertEqual(reaction["points"]["D0"]["volume"], 7000)
        # 무거래 봉은 D-n 산정에서 제외된다.
        self.assertEqual(reaction["points"]["D-2"]["date"], "2024-11-04")

    def test_all_zero_volume_window_is_no_trading_activity(self):
        bars = [_bar(f"2024062{i}", 3140, 0) for i in range(4, 9)]

        reaction = build_event_reaction(
            code="222160",
            event_date="2024-06-28",
            ohlcv=bars,
            flows=[],
            before=5,
            after=10,
        )
        text = format_event_reaction(reaction)

        self.assertEqual(reaction["validation"]["status"], "unavailable")
        self.assertIn(NO_TRADING_ACTIVITY, reaction["validation"]["codes"])
        self.assertEqual(reaction["points"], {})
        self.assertNotIn("0.00%", text)
        self.assertIn("NO_TRADING_ACTIVITY", text)

    def test_trading_resumed_outside_window_is_not_used_as_basis(self):
        """사건창을 한참 지나 재개된 거래를 그 사건의 반응으로 정렬하지 않는다.

        실데이터 회귀: 222160은 2024-06-28 사건 후 2026-06-26에 거래가 재개됐고,
        수정 전에는 재개일(-49.52%)이 D+1로 표시됐다.
        """
        halted = [_bar(f"202406{d:02d}", 8040, 0) for d in range(24, 29)]
        halted += [_bar(f"202407{d:02d}", 8040, 0) for d in range(1, 13)]
        resumed = [_bar("20260626", 208, 6_361_145), _bar("20260629", 105, 4_340_924)]

        reaction = build_event_reaction(
            code="222160",
            event_date="2024-06-28",
            ohlcv=halted + resumed,
            flows=[],
            before=5,
            after=10,
        )
        validation = reaction["validation"]
        text = format_event_reaction(reaction)

        self.assertEqual(validation["status"], "unavailable")
        self.assertIn(NO_TRADING_ACTIVITY, validation["codes"])
        self.assertIsNone(reaction["basis_trading_date"])
        self.assertEqual(reaction["points"], {})
        self.assertEqual(validation["next_tradable_date_outside_window"], "2026-06-26")
        self.assertIn("사건창 밖 거래 재개일", text)
        self.assertNotIn("-49.52%", text)
        self.assertNotIn("기준 거래일=2026-06-26", text)

    def test_short_halt_inside_window_still_computes(self):
        """창 안에서 거래가 재개되면 정상적으로 기준일을 잡는다(과잉 차단 방지)."""
        bars = [
            _bar("20260511", 1000, 5000),
            _bar("20260512", 1000, 0),  # 사건일 — 무거래
            _bar("20260513", 1000, 0),
            _bar("20260514", 1000, 0),
            _bar("20260515", 940, 8000),  # 3세션 뒤 재개, 창(after=10) 안
            _bar("20260518", 900, 7000),
        ]

        reaction = build_event_reaction(
            code="005930", event_date="2026-05-12", ohlcv=bars, flows=[], before=1, after=10
        )

        self.assertEqual(reaction["basis_trading_date"], "2026-05-15")
        self.assertEqual(
            reaction["validation"]["alignment_reason"], "first_tradable_session_after_event"
        )
        self.assertAlmostEqual(reaction["points"]["D+1"]["return_pct"], -4.26)

    def test_insufficient_post_history_nulls_only_that_point(self):
        bars = _normal_bars()  # 2026-05-15 이후 거래일 5개뿐

        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=bars,
            flows=_normal_flows(),
            before=5,
            after=10,
        )
        validation = reaction["validation"]

        self.assertEqual(validation["status"], "partial")
        self.assertIn(INSUFFICIENT_POST_EVENT_HISTORY, validation["codes"])
        self.assertAlmostEqual(reaction["points"]["D+5"]["return_pct"], 33.33)
        self.assertIsNone(reaction["points"]["D+10"]["return_pct"])
        self.assertIsNone(reaction["points"]["D+10"]["date"])
        self.assertEqual(reaction["points"]["D+10"]["code"], INSUFFICIENT_POST_EVENT_HISTORY)
        self.assertNotIn("| D+10 | 2026", format_event_reaction(reaction))

    def test_missing_flow_rows_return_null_not_zero(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=[_flow("2025.01.02", 900, 900)],  # 사건창과 겹치지 않는 행
            before=5,
            after=5,
        )
        text = format_event_reaction(reaction)

        self.assertIn(FLOW_UNAVAILABLE_FOR_EVENT_WINDOW, reaction["validation"]["codes"])
        self.assertEqual(reaction["validation"]["status"], "partial")
        for key in ("pre", "post"):
            self.assertEqual(reaction["flow"][key]["status"], "unavailable")
            self.assertIsNone(reaction["flow"][key]["institutional"])
            self.assertIsNone(reaction["flow"][key]["foreign"])
            self.assertEqual(reaction["flow"][key]["days"], 0)
        self.assertIn("해당 사건창 수급 데이터 없음", text)
        self.assertNotIn("+0 |", text)

    def test_real_zero_flow_sum_stays_numeric_zero(self):
        flows = [
            _flow("2026.05.15", 300, -50),
            _flow("2026.05.18", -300, 50),
        ]

        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=flows,
            before=0,
            after=1,
        )

        self.assertEqual(reaction["flow"]["post"]["status"], "available")
        self.assertEqual(reaction["flow"]["post"]["institutional"], 0)
        self.assertEqual(reaction["flow"]["post"]["foreign"], 0)
        self.assertEqual(reaction["flow"]["post"]["days"], 2)
        self.assertNotIn(FLOW_UNAVAILABLE_FOR_EVENT_WINDOW, reaction["validation"]["codes"])

    def test_flow_provider_error_is_separate_from_missing_data(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=[],
            before=5,
            after=5,
            flow_error="ConnectError",
        )

        self.assertIn("PROVIDER_ERROR", reaction["validation"]["codes"])
        self.assertEqual(reaction["flow"]["post"]["reason"], "provider_error")
        self.assertIsNone(reaction["flow"]["post"]["institutional"])

    def test_shuffled_bar_order_yields_same_result(self):
        ordered = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=_normal_flows(),
            before=5,
            after=5,
        )
        shuffled_bars = [_normal_bars()[i] for i in (7, 2, 10, 0, 5, 9, 1, 4, 8, 3, 6)]
        shuffled = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=shuffled_bars,
            flows=list(reversed(_normal_flows())),
            before=5,
            after=5,
        )

        self.assertEqual(ordered, shuffled)

    def test_markdown_states_dartlens_timeline_contract(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=[_bar("20260514", 130), _bar("20260515", 150), _bar("20260518", 165)],
            flows=[],
            before=1,
            after=1,
        )
        text = format_event_reaction(reaction)

        self.assertIn("event_date=2026-05-15", text)
        self.assertIn("기준 거래일=2026-05-15", text)
        self.assertIn("DartLens", text)
        self.assertIn("공시 접수일", text)
        self.assertIn("해당 기간 공시", text)


class EventReactionRegressionTests(unittest.TestCase):
    """정상 사건의 기존 계산값이 회귀하지 않는지 고정."""

    def test_normal_event_point_values_unchanged(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=_normal_bars(),
            flows=_normal_flows(),
            before=5,
            after=5,
        )
        actual = {
            label: (point["date"], point["close"], point["return_pct"], point["volume"])
            for label, point in reaction["points"].items()
        }

        self.assertEqual(
            actual,
            {
                "D-5": ("2026-05-08", 100, -33.33, 1000),
                "D0": ("2026-05-15", 150, 0.0, 3000),
                "D+1": ("2026-05-18", 165, 10.0, 2500),
                "D+5": ("2026-05-22", 200, 33.33, 2600),
            },
        )
        self.assertEqual(reaction["volume"], {"pre_avg": 1200, "basis": 3000, "post_avg": 2450})
        self.assertEqual(reaction["window"]["start_date"], "2026-05-08")
        self.assertEqual(reaction["window"]["end_date"], "2026-05-22")


class TwelveCaseFixtureTests(unittest.TestCase):
    """research 017의 실제 12건을 비식별한 회귀 fixture — usable 4 / 차단 8."""

    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_reproduces_four_usable_eight_blocked(self):
        usable, blocked = [], []
        for case in self.payload["cases"]:
            reaction = build_event_reaction(
                code=case["code"],
                event_date=case["event_date"],
                ohlcv=case["ohlcv"],
                flows=case["flows"],
                before=case["before"],
                after=case["after"],
            )
            expect = case["expect"]
            validation = reaction["validation"]

            with self.subTest(case=case["case_id"]):
                self.assertEqual(validation["status"], expect["status"])
                self.assertEqual(validation["codes"], expect["codes"])
                self.assertEqual(reaction["basis_trading_date"], expect["basis_trading_date"])
                self.assertEqual(validation["alignment_reason"], expect["alignment_reason"])

                if expect["valid_for_interpretation"]:
                    usable.append(case["case_id"])
                    self.assertAlmostEqual(reaction["points"]["D0"]["return_pct"], 0.0)
                    for label, value in expect["returns"].items():
                        self.assertAlmostEqual(
                            reaction["points"][label]["return_pct"], value, places=2
                        )
                    self.assertEqual(reaction["flow"]["post"]["status"], "available")
                else:
                    blocked.append(case["case_id"])
                    self.assertEqual(reaction["points"], {})
                    self.assertIsNone(reaction["volume"]["basis"])
                    self.assertIsNone(reaction["flow"]["post"]["institutional"])
                    self.assertEqual(
                        validation["next_tradable_date_outside_window"],
                        expect.get("next_tradable_date_outside_window"),
                    )
                    if "history_gap_calendar_days" in expect:
                        self.assertEqual(
                            validation["earliest_available_date"],
                            expect["earliest_available_date"],
                        )
                        self.assertEqual(
                            validation["history_gap_calendar_days"],
                            expect["history_gap_calendar_days"],
                        )

        self.assertEqual(len(usable), self.payload["meta"]["expected_usable"], usable)
        self.assertEqual(len(blocked), self.payload["meta"]["expected_blocked"], blocked)

    def test_blocked_cases_never_render_zero_reaction_markdown(self):
        for case in self.payload["cases"]:
            if case["expect"]["valid_for_interpretation"]:
                continue
            reaction = build_event_reaction(
                code=case["code"],
                event_date=case["event_date"],
                ohlcv=case["ohlcv"],
                flows=case["flows"],
                before=case["before"],
                after=case["after"],
            )
            text = format_event_reaction(reaction)

            with self.subTest(case=case["case_id"]):
                # 사유별로 고객이 겪은 상황을 그대로 말한다 — 뭉뚱그린 "분석 불가" 금지.
                headline = UNAVAILABLE_HEADLINES[case["expect"]["codes"][0]]
                self.assertIn(headline, text)
                self.assertIn("등락률·수급 숫자를 만들지 않았습니다", text)
                self.assertNotIn("0.00%", text)
                self.assertNotIn("+0.00", text)
                self.assertNotIn("기관 순매매", text)
                self.assertNotIn("| D0 |", text)


class EventReactionToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_formats_event_reaction_without_network(self):
        from stock_mcp_server import server

        bars = [_bar("20260514", 130), _bar("20260515", 150), _bar("20260518", 165)]
        flows = [_flow("2026.05.15", 500, 700)]

        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), patch.object(
            server, "get_investor_flow", AsyncMock(return_value=flows)
        ):
            text = await server.get_event_reaction(
                code="005930",
                event_date="2026-05-15",
                before=1,
                after=1,
            )

        self.assertIn("event_date=2026-05-15", text)
        self.assertIn("기준 거래일=2026-05-15", text)
        self.assertIn("DartLens", text)

    async def test_tool_rejects_event_older_than_retained_bars(self):
        """500봉 응답이라도 오래된 사건을 첫 보유 거래일로 정렬하지 않는다."""
        from stock_mcp_server import server

        bars = [_bar(f"2024071{i}", 1000 + i * 10, 400_000) for i in range(7, 10)]

        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), patch.object(
            server, "get_investor_flow", AsyncMock(return_value=[])
        ):
            text = await server.get_event_reaction(
                code="101670",
                event_date="2022-11-28",
                before=5,
                after=10,
            )

        self.assertIn("이 날짜의 주가를 가지고 있지 않습니다", text)
        self.assertIn(EVENT_BEFORE_PRICE_HISTORY, text)
        self.assertIn("2024-07-17", text)
        self.assertNotIn("기준 거래일=2024-07-17", text)
        self.assertNotIn("%", text)

    async def test_tool_reports_provider_failure_not_flat_reaction(self):
        from stock_mcp_server import server

        with patch.object(
            server, "get_ohlcv", AsyncMock(side_effect=RuntimeError("boom"))
        ), patch.object(server, "get_investor_flow", AsyncMock(return_value=[])):
            text = await server.get_event_reaction(code="005930", event_date="2026-05-15")

        self.assertIn("PROVIDER_ERROR", text)
        self.assertNotIn("0.00%", text)

    async def test_tool_marks_flow_provider_failure_as_missing(self):
        from stock_mcp_server import server

        with patch.object(
            server, "get_ohlcv", AsyncMock(return_value=_normal_bars())
        ), patch.object(
            server, "get_investor_flow", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            text = await server.get_event_reaction(
                code="005930", event_date="2026-05-15", before=5, after=5
            )

        self.assertIn("해당 사건창 수급 데이터 없음", text)
        self.assertIn("PROVIDER_ERROR", text)

    async def test_tool_rejects_malformed_event_date(self):
        from stock_mcp_server import server

        text = await server.get_event_reaction(code="005930", event_date="어제")

        self.assertIn("YYYY-MM-DD", text)


if __name__ == "__main__":
    unittest.main()
