"""KRX 애프터마켓(2026-09-14~) 이후 네이버 일봉 계열 도구의 이름표.

2026-09-14 실측(80종목): 네이버 일봉 종가 = 19:59 애프터마켓 마지막 체결가,
정규장 종가(15:30 체결가) = 다음 날 기준가. 005930 은 일봉 종가 248,500 /
정규장 종가 249,000 이었다. 값은 원천대로 두고, '정규장 종가'로 읽히지 않게 한다.
"""

import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server
from stock_mcp_server.market_clock import get_market_clock

KST = ZoneInfo("Asia/Seoul")


def _meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        return json.loads(text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0])
    return json.loads(text)["_meta"]


def _bar(date: str, close: int) -> dict:
    return {"date": date, "open": close, "high": close + 500, "low": close - 500,
            "close": close, "volume": 1000}


BARS = [_bar("20260911", 259500), _bar("20260914", 248500), _bar("20260915", 250750)]
OLD_BARS = [_bar("20260910", 269000), _bar("20260911", 259500)]


def _clock(*hm):
    return lambda: get_market_clock(datetime(2026, 9, 15, *hm, tzinfo=KST))


class ChartAfterMarketTests(unittest.IsolatedAsyncioTestCase):
    async def _chart(self, bars, hm, regular=None):
        regular = regular or {}
        lookup = AsyncMock(side_effect=lambda code, day: regular.get(day))
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "build_market_clock", _clock(*hm)), \
             patch.object(server, "_now_kst", return_value=datetime(2026, 9, 15, *hm, tzinfo=KST)), \
             patch.object(server, "get_regular_session_close", lookup):
            text = await server.get_chart(code="005930", count=3)
        return text, _meta(text), lookup

    async def test_after_market_bars_are_not_called_regular_close(self) -> None:
        text, meta, _ = await self._chart(BARS, (17, 0), {"2026-09-14": 249000})

        self.assertEqual(meta["price_session"], "regular_and_after")
        self.assertEqual(meta["session_mix"]["bars"], 2)
        self.assertEqual(meta["session_mix"]["first_bar"], "2026-09-14")
        self.assertIn(rmeta.EXTENDED_SESSION_WARNING, meta["warnings"])
        self.assertIn("정규장 종가(15:30)가 아니므로", text)

    async def test_regular_close_is_shown_beside_not_swapped_in(self) -> None:
        text, meta, _ = await self._chart(BARS, (17, 0), {"2026-09-14": 249000})

        # OHLCV 표의 종가는 원천 그대로다.
        self.assertIn("2026-09-14|248500|249000|248000|248500|1000", text)
        self.assertIn("2026-09-14|248500|249000|-500", text)
        self.assertIn("2026-09-15|250750|확인 불가|-", text)
        checks = {c["date"]: c for c in meta["regular_close_check"]}
        self.assertEqual(checks["2026-09-14"]["status"], "ok")
        self.assertEqual(checks["2026-09-15"]["status"], "no_1530_bar")

    async def test_bar_still_moving_during_after_market(self) -> None:
        _, meta, _ = await self._chart(BARS, (17, 0))

        self.assertEqual(meta["data_basis"], "in_progress_bar")
        self.assertFalse(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["bar_state"]["last_completed_bar_date"], "2026-09-14")
        self.assertEqual(meta["session"], "after_hours")
        self.assertTrue(any("애프터마켓(20:00까지)" in w for w in meta["warnings"]))

    async def test_bar_final_after_20(self) -> None:
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=BARS)), \
             patch.object(server, "build_market_clock",
                          lambda: get_market_clock(datetime(2026, 9, 15, 20, 30, tzinfo=KST))), \
             patch.object(server, "_now_kst", return_value=datetime(2026, 9, 15, 20, 30, tzinfo=KST)), \
             patch.object(server, "get_regular_session_close", AsyncMock(return_value=None)):
            meta = _meta(await server.get_chart(code="005930", count=3))

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertTrue(meta["bar_state"]["last_bar_complete"])
        self.assertEqual(meta["price_session"], "regular_and_after")

    async def test_today_is_not_checked_before_the_closing_auction(self) -> None:
        _, meta, lookup = await self._chart(BARS, (10, 0), {"2026-09-14": 249000})

        self.assertEqual([c["date"] for c in meta["regular_close_check"]], ["2026-09-14"])
        self.assertEqual(lookup.await_count, 1)

    async def test_bars_before_after_market_start_stay_regular(self) -> None:
        text, meta, lookup = await self._chart(OLD_BARS, (17, 0))

        self.assertEqual(meta["price_session"], "regular")
        self.assertNotIn("session_mix", meta)
        self.assertNotIn(rmeta.EXTENDED_SESSION_WARNING, meta["warnings"])
        self.assertNotIn("정규장 종가 대조", text)
        lookup.assert_not_called()

    async def test_lookup_failure_is_named(self) -> None:
        lookup = AsyncMock(side_effect=RuntimeError("down"))
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=BARS)), \
             patch.object(server, "build_market_clock", _clock(17, 0)), \
             patch.object(server, "_now_kst", return_value=datetime(2026, 9, 15, 17, 0, tzinfo=KST)), \
             patch.object(server, "get_regular_session_close", lookup):
            meta = _meta(await server.get_chart(code="005930", count=3))

        self.assertEqual({c["status"] for c in meta["regular_close_check"]}, {"lookup_failed"})


class WeeklyBarTests(unittest.TestCase):
    def test_week_that_ends_after_start_is_mixed(self) -> None:
        # 2026-09-08(화) 주봉은 9/13(일)에 끝나 애프터마켓 전, 9/14 주는 섞인다.
        self.assertIsNone(server._extended_bar_info([{"date": "20260908"}], "week"))
        self.assertEqual(server._extended_bar_info([{"date": "20260914"}], "week")["bars"], 1)
        self.assertEqual(server._extended_bar_info([{"date": "20260901"}], "month")["bars"], 1)
        self.assertIsNone(server._extended_bar_info([{"date": "20260831"}], "month"))


class FlowAndStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_reference_close_is_named(self) -> None:
        rows = [
            {"date": "2026.09.14", "close": 248500, "volume": 1, "institutional": 1,
             "foreign": 1, "individual": 1},
            {"date": "2026.09.11", "close": 259500, "volume": 1, "institutional": 1,
             "foreign": 1, "individual": 1},
        ]
        with patch.object(server, "get_investor_flow", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", _clock(21, 0)):
            text = await server.get_flow(code="005930", days=2)
        meta = _meta(text)

        self.assertIn("[참고] 종가: 2026-09-14부터는 20:00 애프터마켓 마지막 체결가", text)
        self.assertEqual(meta["price_session"], "regular_and_after")
        self.assertEqual(meta["session_mix"]["bars"], 1)

    async def test_multi_chart_stats_note(self) -> None:
        stats = [{"code": "005930", "bars_count": 260, "current_price": 250750,
                  "current_date": "20260915", "high": 380000, "high_date": "20260801",
                  "low": 76300, "low_date": "20250915", "drawdown_pct": -34.0,
                  "recovery_pct": 228.0, "period_return_pct": 200.0, "avg_volume": 1,
                  "excluded_bars": 0}]
        with patch.object(server, "naver_get_multi_chart_stats", AsyncMock(return_value=stats)), \
             patch.object(server, "build_market_clock", _clock(21, 0)):
            text = await server.get_multi_chart_stats(codes=["005930"], days=260)
        meta = _meta(text)

        self.assertIn("정규장 종가 기준 수치가 아닙니다", text)
        self.assertEqual(meta["price_session"], "regular_and_after")

    async def test_indicators_json_carries_price_session(self) -> None:
        bars = [_bar(f"202608{d:02d}", 250000) for d in range(1, 29)] + BARS
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "build_market_clock", _clock(21, 0)):
            payload = json.loads(await server.get_indicators(code="005930", days=60, include=["candle"]))

        self.assertEqual(payload["_meta"]["price_session"], "regular_and_after")
        self.assertEqual(payload["_meta"]["session_mix"]["bars"], 2)


class MarketNoteTests(unittest.TestCase):
    def test_gap_before_after_market_says_quotes_will_move(self) -> None:
        krx = get_market_clock(datetime(2026, 9, 15, 15, 45, tzinfo=KST))["krx"]
        note = server._kr_market_note(krx)[0]
        self.assertIn("16:00에 애프터마켓이 열리면", note)

    def test_after_market_note_depends_on_price_session(self) -> None:
        krx = get_market_clock(datetime(2026, 9, 15, 17, 0, tzinfo=KST))["krx"]
        self.assertIn("정규장 기준이며 애프터마켓 체결은 들어 있지 않습니다",
                      server._kr_market_note(krx, "regular")[0])
        self.assertIn("20:00 전까지 바뀝니다", server._kr_market_note(krx, "after_market")[0])

    def test_after_20_is_plain_closed(self) -> None:
        krx = get_market_clock(datetime(2026, 9, 15, 20, 30, tzinfo=KST))["krx"]
        self.assertIn("KRX 장마감 상태", server._kr_market_note(krx)[0])


if __name__ == "__main__":
    unittest.main()
