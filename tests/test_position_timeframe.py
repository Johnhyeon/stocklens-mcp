"""52주 위치 창은 봉 주기를 따라야 한다.

실측(UAT): 주봉 요청에서도 252개 봉을 되돌아봐, 약 5년치 고저가 "52주
고가/저가" 라벨로 나갔다. 라벨이 52주라고 말하면 창도 52주여야 한다 -
일봉 252개, 주봉 52개, 월봉 12개.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server
from stock_mcp_server._indicators import _to_df, compute_indicators, compute_position


def _weekly_bars(n=260, spike_at=200):
    """주봉 n개(오름차순). 끝에서 spike_at 번째 봉에 고점 스파이크.

    spike_at=200 이면 약 3.8년 전 - 52주 창 밖이어야 한다.
    """
    out = []
    start = date(2021, 1, 4)
    for i in range(n):
        d = start + timedelta(weeks=i)
        high = 9_000 if i == n - spike_at else 1_100
        low = 400 if i == n - spike_at else 900
        out.append({"date": d.strftime("%Y%m%d"), "open": 1000, "high": high,
                    "low": low, "close": 1000, "volume": 10_000})
    return out


class PositionWindowTests(unittest.TestCase):
    def test_weekly_window_is_52_bars(self):
        df = _to_df(_weekly_bars())
        pos = compute_position(df, bars_per_year=52)
        self.assertEqual(pos["high_52w"], 1_100)   # 3.8년 전 9,000 스파이크 제외
        self.assertEqual(pos["low_52w"], 900)
        self.assertEqual(pos["lookback_bars"], 52)

    def test_daily_default_stays_252(self):
        df = _to_df(_weekly_bars())               # 데이터 모양은 무관 - 창 수만 본다
        pos = compute_position(df)
        self.assertEqual(pos["high_52w"], 9_000)  # 252봉 창에는 스파이크 포함
        self.assertEqual(pos["lookback_bars"], 252)

    def test_compute_indicators_passes_timeframe(self):
        bars = _weekly_bars()
        week = compute_indicators(bars, ["position"], timeframe="week")
        self.assertEqual(week["position"]["high_52w"], 1_100)
        day = compute_indicators(bars, ["position"])
        self.assertEqual(day["position"]["high_52w"], 9_000)
        month = compute_indicators(bars, ["position"], timeframe="month")
        self.assertEqual(month["position"]["lookback_bars"], 12)


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
}
_EVENING = datetime(2026, 8, 26, 21, 0, tzinfo=rmeta.KST)


class ToolLevelTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekly_indicators_use_52_bar_window(self):
        bars = _weekly_bars()
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_EVENING):
            payload = json.loads(await server.get_indicators(
                code="005930", days=260, include=["position"], timeframe="week"))
        pos = payload["indicators"]["position"]
        self.assertEqual(pos["high_52w"], 1_100)
        self.assertEqual(pos["lookback_bars"], 52)


if __name__ == "__main__":
    unittest.main(verbosity=2)
