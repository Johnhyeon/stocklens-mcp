"""분봉 응답의 이름표는 시계가 아니라 받은 봉에서 온다 (2026-09-17 실측 결함).

1. 장중에 지난 날짜(2025-09-10) 1분봉을 물으면 끝난 거래일인데도
   data_basis=in_progress_bar + "마지막 봉이 아직 마감되지 않았습니다"가 붙었다.
2. 봉이 없으면 data_as_of 가 요청일이 아니라 오늘 날짜였다.
3. 키움은 보관 기간 밖 날짜를 entity_not_found(종목 없음처럼 읽힘)로, KIS 는
   "분봉 데이터가 없습니다"로 답했다. 같은 경우는 같은 말이어야 한다.
4. 끝난 거래일에 봉이 0개여도 이전 거래일을 최대 10일 더 불렀다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server
from stock_mcp_server.market_data.kis_client import KisApiError
from stock_mcp_server.market_data.models import BarDataset, NormalizedBar

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

_TODAY = "2026-09-17"

# 목요일 13:00 KST - KRX 정규장 중. 미국은 닫혀 있다.
_KRX_OPEN = {
    "krx": {"is_open": True, "status": "regular", "current_session": "regular",
            "last_trading_day": _TODAY, "regular_close": "15:30"},
    "us": {"is_open": False, "status": "closed_overnight",
           "last_trading_day": "2026-09-16"},
}
# 목요일 21:00 KST - KRX 는 애프터마켓까지 끝났다.
_KRX_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "current_session": None, "next_session": None,
            "last_trading_day": _TODAY, "regular_close": "15:30"},
    "us": {"is_open": False, "status": "closed_pre_market",
           "last_trading_day": "2026-09-16"},
}
# 목요일 11:00 ET - 미국 정규장 중.
_US_OPEN = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "current_session": None, "next_session": None,
            "last_trading_day": _TODAY, "regular_close": "15:30"},
    "us": {"is_open": True, "status": "regular",
           "last_trading_day": _TODAY},
}
_US_CLOSED = {
    "krx": _KRX_OPEN["krx"],
    "us": {"is_open": False, "status": "closed_after_hours",
           "last_trading_day": _TODAY},
}


def _bars(open_at: datetime, count: int, minutes: int = 1,
          complete_last: bool = True) -> list[NormalizedBar]:
    out = []
    for i in range(count):
        start = open_at + timedelta(minutes=i * minutes)
        o = 1000 + i
        out.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=minutes),
            open=Decimal(o), high=Decimal(o + 2), low=Decimal(o - 2),
            close=Decimal(o + 1), volume=100 + i, interval=f"{minutes}m",
            session="regular",
            complete=complete_last or i < count - 1,
            session_tail=False, expected_minutes=minutes,
            actual_minutes=minutes, data_integrity="complete",
            source_gap_status="none"))
    return out


def _dataset(bars, *, market="KR", provider="kis") -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market=market,
        symbol="005930" if market == "KR" else "AAPL",
        provider=provider, profile="real",
        venue="KRX" if market == "KR" else "NAS",
        timezone="Asia/Seoul" if market == "KR" else "America/New_York",
        session="regular", requested_interval="1m", source_interval="1m",
        aggregation_method="provider_native", adjustment_basis="unadjusted",
        source_endpoint="test", coverage={"complete": True}, warnings=())


def _route(provider="kis"):
    return {"requested_source": "auto", "selected_provider": provider,
            "selection_reason": "broker_connected_and_intraday_supported",
            "mode": "auto", "fallback_used": False, "fallback_from": None}


def _meta(text: str) -> dict:
    body = text.split("RESULT_META_JSON_START", 1)[1]
    return json.loads(body.split("RESULT_META_JSON_END", 1)[0])


class _ToolCase(unittest.IsolatedAsyncioTestCase):
    async def _chart(self, dataset, *, clock, now, provider="kis", **kwargs):
        fetch = AsyncMock(return_value=(dataset, _route(provider)))
        args = dict(symbol="005930", market="KR", interval="1m", row_limit=5)
        args.update(kwargs)
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock", return_value=clock), \
             patch.object(server, "_intraday_now", return_value=now):
            text = await server.get_intraday_chart(**args)
        return text, _meta(text)


class PastDateLabelTests(_ToolCase):
    async def test_past_date_during_open_market_is_confirmed(self):
        # 보고된 재현: 장중에 2025-09-10 1분봉 1개.
        bars = _bars(datetime(2025, 9, 10, 15, 30, tzinfo=KST), 1)
        text, meta = await self._chart(
            _dataset(bars), clock=_KRX_OPEN,
            now=datetime(2026, 9, 17, 13, 0, tzinfo=KST),
            date="2025-09-10", row_limit=1)

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertEqual(meta["data_as_of"], "2025-09-10")
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])
        self.assertFalse(any("마감되지 않" in w for w in meta["warnings"]))
        self.assertFalse(any("진행 중" in w for w in meta["warnings"]))
        self.assertEqual(meta["price_session"], "regular")

    async def test_past_date_after_close_does_not_claim_latest_day(self):
        bars = _bars(datetime(2025, 9, 10, 9, 0, tzinfo=KST), 3)
        _, meta = await self._chart(
            _dataset(bars), clock=_KRX_CLOSED,
            now=datetime(2026, 9, 17, 21, 0, tzinfo=KST), date="2025-09-10")

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertFalse(any("최근 거래일" in w for w in meta["warnings"]),
                         meta["warnings"])

    async def test_latest_day_after_close_keeps_the_market_note(self):
        bars = _bars(datetime(2026, 9, 17, 15, 28, tzinfo=KST), 3)
        _, meta = await self._chart(
            _dataset(bars), clock=_KRX_CLOSED,
            now=datetime(2026, 9, 17, 21, 0, tzinfo=KST))

        self.assertEqual(meta["data_as_of"], _TODAY)
        self.assertTrue(any(f"최근 거래일({_TODAY})" in w
                            for w in meta["warnings"]), meta["warnings"])

    async def test_us_past_date_is_confirmed_and_not_delayed(self):
        bars = _bars(datetime(2026, 8, 20, 15, 57, tzinfo=NY), 3)
        _, meta = await self._chart(
            _dataset(bars, market="US"), clock=_US_OPEN,
            now=datetime(2026, 9, 17, 11, 0, tzinfo=NY),
            symbol="AAPL", market="US", venue="NAS", date="2026-08-20")

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertEqual(meta["data_as_of"], "2026-08-20")
        self.assertFalse(meta["is_delayed"])
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])

    async def test_us_past_date_while_closed_does_not_claim_latest_day(self):
        bars = _bars(datetime(2026, 8, 20, 15, 57, tzinfo=NY), 3)
        _, meta = await self._chart(
            _dataset(bars, market="US"), clock=_US_CLOSED,
            now=datetime(2026, 9, 17, 18, 0, tzinfo=NY),
            symbol="AAPL", market="US", venue="NAS", date="2026-08-20")

        self.assertFalse(any("최근 거래일" in w for w in meta["warnings"]),
                         meta["warnings"])


class TodayLabelTests(_ToolCase):
    async def test_completed_bars_today_are_not_called_unfinished(self):
        # completed_only=True(기본)면 마지막 봉은 이미 끝났다. 장이 진행 중이라는
        # 사실은 따로 알린다.
        bars = _bars(datetime(2026, 9, 17, 12, 55, tzinfo=KST), 5)
        _, meta = await self._chart(
            _dataset(bars), clock=_KRX_OPEN,
            now=datetime(2026, 9, 17, 13, 0, 20, tzinfo=KST))

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])
        self.assertTrue(any("정규장이 아직 진행 중" in w and "13:00" in w
                            for w in meta["warnings"]), meta["warnings"])

    async def test_forming_bar_is_in_progress(self):
        bars = _bars(datetime(2026, 9, 17, 12, 56, tzinfo=KST), 5,
                     complete_last=False)
        _, meta = await self._chart(
            _dataset(bars), clock=_KRX_OPEN,
            now=datetime(2026, 9, 17, 13, 0, 20, tzinfo=KST),
            completed_only=False)

        self.assertEqual(meta["data_basis"], "in_progress_bar")
        self.assertIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])

    def test_bars_from_an_earlier_day_say_so(self):
        # 휴장일을 기준일로 주면 이전 거래일 봉으로 답한다. 그 사실을 적는다.
        bars = _bars(datetime(2026, 9, 18, 15, 28, tzinfo=KST), 3)
        labels = server._intraday_bar_labels(
            "KR", bars, date(2026, 9, 19),
            now=datetime(2026, 9, 19, 12, 0, tzinfo=KST))
        self.assertEqual(labels["data_as_of"], "2026-09-18")
        self.assertFalse(labels["bar_forming"])
        self.assertTrue(any("2026-09-19" in w and "2026-09-18" in w
                            for w in labels["warnings"]))


class NoDataLabelTests(_ToolCase):
    async def test_empty_result_carries_the_requested_date(self):
        _, meta = await self._chart(
            _dataset([]), clock=_KRX_OPEN,
            now=datetime(2026, 9, 17, 13, 0, tzinfo=KST), date="2025-09-05")

        self.assertEqual(meta["data_as_of"], "2025-09-05")
        self.assertEqual(meta["data_completeness"], "none")
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])

    async def test_kis_and_kiwoom_say_the_same_thing(self):
        # KIS 는 행 0개, 키움은 빈 행 1개 -> 공급자에서 둘 다 빈 결과가 된다.
        texts = []
        for provider in ("kis", "kiwoom"):
            text, meta = await self._chart(
                _dataset([], provider=provider), provider=provider,
                clock=_KRX_OPEN, now=datetime(2026, 9, 17, 13, 0, tzinfo=KST),
                date="2025-08-29", source=provider)
            texts.append(text.split("RESULT_META_JSON_START", 1)[0])
            self.assertNotIn("entity_not_found", text)
            self.assertIn("2025-08-29", text)
            self.assertIn("보관 기간", text)
            self.assertIn("종목코드", text)
            self.assertEqual(meta["provider_status"], "ok")
        self.assertEqual(texts[0], texts[1])

    async def test_broker_error_carries_the_requested_date(self):
        fetch = AsyncMock(side_effect=KisApiError("rate_limited", 429))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            text = await server.get_intraday_chart(
                symbol="005930", interval="1m", date="2025-09-10")
        meta = _meta(text)
        self.assertEqual(meta["provider_status"], "rate_limited")
        self.assertEqual(meta["data_as_of"], "2025-09-10")
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])

    async def test_indicators_without_bars_do_not_claim_a_forming_bar(self):
        fetch = AsyncMock(return_value=(_dataset([]), _route()))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            text = await server.get_intraday_indicators(
                symbol="005930", interval="5m")
        meta = _meta(text)
        self.assertEqual(meta["provider_status"], "no_session")
        self.assertEqual(meta["data_as_of"], _TODAY)
        self.assertNotIn(rmeta.IN_PROGRESS_WARNING, meta["warnings"])


class MetaBuilderCompatibilityTests(unittest.TestCase):
    """봉 상태를 모르는 기존 도구는 예전처럼 장 상태로 판정한다."""

    def test_kr_bars_without_bar_state_follow_the_clock(self):
        with patch.object(server, "build_market_clock",
                          return_value=_KRX_OPEN):
            meta = server._kr_meta(kind="bars", data_as_of="2026-09-01")
        self.assertEqual(meta["data_basis"], "in_progress_bar")

    def test_us_bars_without_bar_state_follow_the_clock(self):
        with patch.object(server, "build_market_clock",
                          return_value=_US_OPEN):
            meta = server._us_meta(kind="bars", data_as_of="2026-09-01")
        self.assertEqual(meta["data_basis"], "in_progress_bar")
        self.assertTrue(meta["is_delayed"])


class _DayProvider:
    """거래일별 1분봉을 돌려주고 호출한 날짜를 기록한다."""

    provider_id = "kis"

    def __init__(self, by_day: dict):
        self.by_day = by_day
        self.calls: list[date] = []

    async def fetch_bars(self, request):
        self.calls.append(request.trading_date)
        return _dataset(self.by_day.get(request.trading_date, []))


class NoWalkBackOnEmptyDayTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, provider, *, day, now, row_limit=120):
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value={"kis": provider}), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "auto", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            return await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval="5m",
                trading_date=day, row_limit=row_limit, venue=None,
                session="regular", completed_only=True, source="auto",
                now=now)

    async def test_finished_day_without_bars_makes_one_call(self):
        # 이전 거래일에는 봉이 있어도 묻지 않은 날짜로 답하지 않는다.
        older = date(2025, 9, 8)
        provider = _DayProvider({
            older: _bars(datetime(2025, 9, 8, 9, 0, tzinfo=KST), 381)})
        ds, _ = await self._fetch(
            provider, day=date(2025, 9, 9),
            now=datetime(2026, 9, 17, 13, 0, tzinfo=KST))
        self.assertEqual(provider.calls, [date(2025, 9, 9)])
        self.assertEqual(len(ds.bars), 0)

    async def test_day_with_bars_still_pulls_earlier_history(self):
        d1, d2 = date(2025, 9, 9), date(2025, 9, 10)
        provider = _DayProvider({
            d1: _bars(datetime(2025, 9, 9, 9, 0, tzinfo=KST), 381),
            d2: _bars(datetime(2025, 9, 10, 9, 0, tzinfo=KST), 381)})
        ds, _ = await self._fetch(
            provider, day=d2, now=datetime(2026, 9, 17, 13, 0, tzinfo=KST))
        self.assertEqual(provider.calls[:2], [d2, d1])
        self.assertEqual(len(ds.bars), 120)

    async def test_today_with_no_bars_yet_uses_the_previous_day(self):
        # 09:00 직후 조회: 오늘 봉이 아직 없으면 직전 거래일로 이어 간다(기존 동작).
        today, prev = date(2026, 9, 17), date(2026, 9, 16)
        provider = _DayProvider({
            prev: _bars(datetime(2026, 9, 16, 9, 0, tzinfo=KST), 381)})
        ds, _ = await self._fetch(
            provider, day=today,
            now=datetime(2026, 9, 17, 9, 0, 5, tzinfo=KST), row_limit=10)
        self.assertEqual(provider.calls[:2], [today, prev])
        self.assertEqual(len(ds.bars), 10)


if __name__ == "__main__":
    unittest.main()
