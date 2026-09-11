"""분봉 파이프라인 캐시 연결 테스트.

정책 (설계 21절):
- 완료된 거래일의 KIS 1분봉만 디스크 캐시에 저장한다 (장기 보존).
- 진행 중인 거래일은 캐시하지 않는다 (항상 실조회 - 낡은 장중 데이터 금지).
- partial 응답은 완전 캐시로 저장하지 않는다.
- 같은 완료 거래일의 재조회는 API 호출 0회.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server
from stock_mcp_server.market_data.models import (
    BarDataset,
    NormalizedBar,
    bar_from_dict,
    bar_to_dict,
)

KST = ZoneInfo("Asia/Seoul")

_CAPS = {"connected": True, "kr_intraday": True, "us_intraday": True,
         "kr_daily": False, "us_daily": False}
_STATE = {"data_source_mode": "auto", "active_profile": "real",
          "active_provider": "kis", "connection_generation": 1}


def _minute_bars(open_at: datetime, count: int) -> list[NormalizedBar]:
    bars = []
    for i in range(count):
        start = open_at + timedelta(minutes=i)
        o = 1000 + i
        bars.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=1),
            open=Decimal(o), high=Decimal(o + 2), low=Decimal(o - 2),
            close=Decimal(o + 1), volume=100 + i,
            interval="1m", session="regular", complete=True,
            session_tail=False, expected_minutes=1, actual_minutes=1,
            data_integrity="complete", source_gap_status="none",
        ))
    return bars


class DayProvider:
    """trading_date 별 1분봉을 돌려주고 호출을 기록하는 fake KIS."""

    provider_id = "kis"

    def __init__(self, day_bars: dict, complete=True):
        self.day_bars = day_bars
        self.calls: list = []
        self.complete = complete

    async def fetch_bars(self, request):
        self.calls.append(request.trading_date)
        bars = self.day_bars.get(request.trading_date, [])
        return BarDataset(
            bars=tuple(bars), market="KR", symbol=request.symbol,
            provider="kis", profile="real", venue="KRX",
            timezone="Asia/Seoul", session="regular",
            requested_interval="1m", source_interval="1m",
            aggregation_method="provider_native",
            adjustment_basis="unadjusted",
            source_endpoint="domestic_minute",
            coverage={"complete": self.complete,
                      "returned_rows": len(bars)},
            warnings=(),
        )


class SerializationTests(unittest.TestCase):
    def test_bar_roundtrip(self):
        bar = _minute_bars(datetime(2026, 8, 26, 9, 0, tzinfo=KST), 1)[0]
        restored = bar_from_dict(bar_to_dict(bar))
        self.assertEqual(restored, bar)

    def test_bar_from_corrupt_dict_raises(self):
        with self.assertRaises((KeyError, ValueError, TypeError)):
            bar_from_dict({"start_at": "not-a-date"})


class CacheWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self._env = patch.dict(os.environ, {"STOCKLENS_HOME": self.home})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _fetch(self, provider, *, day, now, interval="5m",
                     row_limit=30):
        with patch.object(server, "_intraday_providers",
                          return_value={"kis": provider}), \
             patch.object(server, "_broker_capabilities",
                          return_value=dict(_CAPS)), \
             patch.object(server, "_broker_state",
                          return_value=dict(_STATE)):
            return await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval=interval,
                trading_date=day, row_limit=row_limit, venue=None,
                session="regular", completed_only=True, source="auto",
                now=now)

    async def test_completed_day_cached_second_call_zero_api(self):
        day = date(2026, 8, 26)
        bars = _minute_bars(datetime(2026, 8, 26, 9, 0, tzinfo=KST), 391)
        now = datetime(2026, 8, 26, 16, 0, tzinfo=KST)

        p1 = DayProvider({day: bars})
        ds1, meta1 = await self._fetch(p1, day=day, now=now, row_limit=10)
        self.assertGreater(len(p1.calls), 0)
        self.assertEqual(ds1.provider, "kis")

        p2 = DayProvider({day: bars})
        ds2, meta2 = await self._fetch(p2, day=day, now=now, row_limit=10)
        # 완료 거래일 재조회는 API 0회, 결과는 동일해야 한다.
        self.assertEqual(p2.calls, [])
        self.assertTrue(meta2.get("cache_hit"))
        self.assertEqual(
            [(b.start_at, b.close, b.volume) for b in ds2.bars],
            [(b.start_at, b.close, b.volume) for b in ds1.bars])

    async def test_in_progress_day_not_cached(self):
        day = date(2026, 8, 27)
        bars = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 60)
        now = datetime(2026, 8, 27, 10, 5, tzinfo=KST)  # 장중

        p1 = DayProvider({day: bars})
        await self._fetch(p1, day=day, now=now, row_limit=5)
        p2 = DayProvider({day: bars})
        await self._fetch(p2, day=day, now=now, row_limit=5)
        # 진행 중 거래일은 캐시 서빙 금지. 매번 실조회.
        self.assertGreater(len(p2.calls), 0)

    async def test_partial_day_not_cached(self):
        day = date(2026, 8, 26)
        bars = _minute_bars(datetime(2026, 8, 26, 9, 0, tzinfo=KST), 30)
        now = datetime(2026, 8, 26, 16, 0, tzinfo=KST)

        p1 = DayProvider({day: bars}, complete=False)  # partial 응답
        await self._fetch(p1, day=day, now=now, row_limit=5)
        p2 = DayProvider({day: bars}, complete=False)
        await self._fetch(p2, day=day, now=now, row_limit=5)
        self.assertGreater(len(p2.calls), 0)

    async def test_multiday_loop_uses_cache_for_past_days(self):
        # 8/27 조회가 8/26 이력을 끌어온다. 8/26 은 완료일이라 캐시된다.
        d26, d27 = date(2026, 8, 26), date(2026, 8, 27)
        bars26 = _minute_bars(datetime(2026, 8, 26, 9, 0, tzinfo=KST), 391)
        bars27 = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 391)
        now = datetime(2026, 8, 27, 16, 0, tzinfo=KST)

        p1 = DayProvider({d26: bars26, d27: bars27})
        ds1, _ = await self._fetch(p1, day=d27, now=now, interval="60m",
                                   row_limit=12)
        self.assertIn(d26, p1.calls)

        p2 = DayProvider({d26: bars26, d27: bars27})
        ds2, _ = await self._fetch(p2, day=d27, now=now, interval="60m",
                                   row_limit=12)
        # 두 날 모두 완료일이므로 두 번째 요청은 API 0회.
        self.assertEqual(p2.calls, [])
        self.assertEqual(
            [(b.start_at, b.close) for b in ds2.bars],
            [(b.start_at, b.close) for b in ds1.bars])


if __name__ == "__main__":
    unittest.main()
