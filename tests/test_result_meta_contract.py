"""결과 메타 봉투 규약 v1 테스트 — 시간축·판정 확정도·Lens 간 이어달리기.

핵심은 as_of(조회 시각)와 data_as_of(데이터 기준일)의 분리다. 토요일에 시세를
조회하면 두 값이 다르고, 사용자에게 말해야 하는 날짜는 data_as_of다. 예전에는
as_of 하나뿐이라 금요일 종가가 토요일 시세로 서술됐다.
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


def _meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
        return json.loads(payload)
    return json.loads(text)["_meta"]


_OPEN = {"krx": {"is_open": True, "status": "regular", "last_trading_day": "2026-08-06"},
         "us": {"is_open": True, "status": "regular", "last_trading_day": "2026-08-06"}}
_CLOSED = {"krx": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-05"},
           "us": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-05"}}


def _bar(date: str) -> dict:
    return {"date": date, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}


# ---------------------------------------------------------------------------
# 1. 봉투 자체
# ---------------------------------------------------------------------------


class EnvelopeTests(unittest.TestCase):
    def test_as_of_and_data_as_of_are_separate(self):
        m = rmeta.build_meta(
            lens="stocklens", data_basis=rmeta.BASIS_LAST_CLOSE,
            data_as_of="20260814",
            now=datetime(2026, 8, 16, 9, 38, tzinfo=rmeta.KST),
        )
        self.assertTrue(m["as_of"].startswith("2026-08-16"))   # 물어본 날
        self.assertEqual(m["data_as_of"], "2026-08-14")        # 숫자의 날

    def test_day_normalization_across_lenses(self):
        """DART는 20260813, 네이버는 2026.08.14로 준다. 메타에선 한 형태로 모은다."""
        for raw in ("20260813", "2026.08.13", "2026-08-13", "2026/08/13(연결)"):
            self.assertEqual(rmeta.normalize_day(raw), "2026-08-13", raw)

    def test_unparseable_day_is_none_not_guessed(self):
        for raw in ("", None, "미상", "2026", "2026.08"):
            self.assertIsNone(rmeta.normalize_day(raw))

    def test_in_progress_bar_always_carries_warning(self):
        m = rmeta.build_meta(lens="stocklens", data_basis=rmeta.BASIS_IN_PROGRESS_BAR)
        self.assertTrue(any("마감되지 않" in w for w in m["warnings"]))

    def test_unknown_basis_rejected(self):
        with self.assertRaises(ValueError):
            rmeta.build_meta(lens="stocklens", data_basis="realtime_ish")

    def test_entity_omits_empty_fields(self):
        self.assertIsNone(rmeta.entity())
        self.assertEqual(rmeta.entity(stock_code="039840"), {"stock_code": "039840"})
        self.assertEqual(
            rmeta.entity(stock_code="039840", corp_code="00115931", name="디오"),
            {"stock_code": "039840", "corp_code": "00115931", "name": "디오"},
        )

    def test_meta_version_is_pinned(self):
        """세 Lens가 같은 규약을 쓰는지 확인하는 유일한 표식이다."""
        m = rmeta.build_meta(lens="stocklens", data_basis=rmeta.BASIS_FILING)
        self.assertEqual(m["meta_v"], 1)


# ---------------------------------------------------------------------------
# 2. 장중 미완성 봉 — 판정이 뒤집힐 수 있음을 알린다
# ---------------------------------------------------------------------------


class InProgressBarTests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_during_open_market_is_in_progress(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=[_bar("20260806")] * 5)), \
             patch.object(server, "build_market_clock", return_value=_OPEN):
            m = _meta(await server.get_chart(code="005930", count=5))
        self.assertEqual(m["data_basis"], "in_progress_bar")
        self.assertTrue(any("마감되지 않" in w for w in m["warnings"]))

    async def test_chart_after_close_is_confirmed(self):
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=[_bar("20260805")] * 5)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_chart(code="005930", count=5))
        self.assertEqual(m["data_basis"], "last_close")
        self.assertFalse(any("마감되지 않" in w for w in m["warnings"]))

    async def test_indicators_inherit_bar_basis(self):
        """RSI·골든크로스가 미완성 봉으로 계산됐는지가 여기 드러나야 한다."""
        bars = [_bar(f"2026{m:02d}{d:02d}") for m in (6, 7) for d in range(1, 29)]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), \
             patch.object(server, "build_market_clock", return_value=_OPEN):
            m = _meta(await server.get_indicators(code="005930", days=60, include=["ma"]))
        self.assertEqual(m["data_basis"], "in_progress_bar")

    async def test_price_snapshot_is_realtime_not_in_progress(self):
        """현재가는 '봉'이 아니라 스냅샷이라 in_progress_bar가 아니다."""
        price = {"name": "삼성전자", "price": 1, "open": 1, "high": 1, "low": 1, "volume": 1}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), \
             patch.object(server, "build_market_clock", return_value=_OPEN):
            m = _meta(await server.get_price(code="005930"))
        self.assertEqual(m["data_basis"], "realtime")


# ---------------------------------------------------------------------------
# 3. data_as_of는 캘린더 역산이 아니라 실제 데이터에서
# ---------------------------------------------------------------------------


class DataAsOfSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_uses_last_bar_date_not_calendar(self):
        """거래정지 등으로 마지막 봉이 캘린더상 최근 거래일보다 과거일 수 있다."""
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=[_bar("20260710")])), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_chart(code="005930", count=1))
        self.assertEqual(m["data_as_of"], "2026-07-10")       # 실제 봉
        self.assertNotEqual(m["data_as_of"], "2026-08-05")    # 캘린더 역산이 아님

    async def test_flow_uses_newest_row_date(self):
        rows = [{"date": "2026.08.05", "institutional": 1, "foreign": 2, "close": 10, "volume": 5}]
        with patch.object(server, "get_investor_flow", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_flow(code="005930", days=1))
        self.assertEqual(m["data_as_of"], "2026-08-05")

    async def test_calendar_used_only_as_fallback(self):
        price = {"name": "삼성전자", "price": 1, "quote_date": None}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_price(code="005930"))
        self.assertEqual(m["data_as_of"], "2026-08-05")

    async def test_source_stamp_wins_over_calendar(self):
        """네이버가 페이지에 찍어주는 기준일이 우리 캘린더보다 정확하다."""
        price = {"name": "삼성전자", "price": 1, "quote_date": "2026-08-04"}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_price(code="005930"))
        self.assertEqual(m["data_as_of"], "2026-08-04")


# ---------------------------------------------------------------------------
# 4. Lens 간 이어달리기 — entity / 기간 기준
# ---------------------------------------------------------------------------


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_price_carries_code_and_name_for_next_call(self):
        price = {"name": "디오", "price": 1, "quote_date": "2026-08-14"}
        with patch.object(server, "get_current_price", AsyncMock(return_value=price)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_price(code="039840"))
        self.assertEqual(m["entity"]["stock_code"], "039840")
        self.assertEqual(m["entity"]["name"], "디오")

    async def test_financial_reports_period_not_a_fake_date(self):
        """재무는 기간이 기준이다. '2026.03'을 날짜로 지어내면 안 된다."""
        fin = {
            "name": "디오",
            "_periods": {"annual": ["2025.12", "2026.12(E)"], "quarterly": ["2026.03"]},
            "매출액": ["1641", "1967", "413"],
        }
        with patch.object(server, "get_financials", AsyncMock(return_value=fin)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED):
            m = _meta(await server.get_financial(code="039840"))
        self.assertEqual(m["data_basis"], "filing")
        self.assertIsNone(m["data_as_of"])
        self.assertIn("2026.03", m["data_period"])

    def test_us_and_kr_share_one_envelope_shape(self):
        kr = server._kr_meta(kind="snapshot", code="005930")
        us = server._us_meta(kind="snapshot", ticker="AAPL")
        self.assertEqual(kr.keys() - {"entity"}, us.keys() - {"entity"})
        self.assertEqual(kr["market"], "KR")
        self.assertEqual(us["market"], "US")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# 5. 키 이름이 곧 정의 (2단계)
# ---------------------------------------------------------------------------


class KeyNamesStateTheirDefinitionTests(unittest.TestCase):
    """값이 아니라 **이름**이 틀려서 오독을 유발하던 키들의 회귀 방지.

    예전 이름은 정의를 다 담지 못했다:
      today            마지막 봉인데 '오늘'로 읽힘 (토요일 조회 시 금요일 값)
      days_since_high  봉 개수인데 '일'로 읽힘 (123봉 = 달력 184일)
      trade_value_krw  종가×거래량 추산인데 실거래대금으로 읽힘
      rank_52w         1이 최다인지 최소인지, 모집단이 뭔지 이름에 없음
    """

    def _df(self, n=300):
        import pandas as pd
        return pd.DataFrame({
            "date": [f"2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
            "open": [100] * n, "high": [110] * n, "low": [90] * n,
            "close": [100] * n, "volume": list(range(1, n + 1)),
        })

    def test_volume_keys_renamed(self):
        from stock_mcp_server._indicators import compute_volume
        v = compute_volume(self._df())
        self.assertIn("latest", v)
        self.assertIn("latest_date", v)          # 어느 봉인지 이름 옆에 날짜로
        self.assertIn("ratio_vs_avg_20b", v)
        self.assertIn("trade_value_est_krw", v)  # 추산임을 이름에 명시
        self.assertIn("volume_rank_252b", v)
        for gone in ("today", "avg_20d", "ratio_vs_20d", "trade_value_krw", "rank_52w"):
            self.assertNotIn(gone, v, f"옛 키 {gone}가 남아 있음")

    def test_latest_is_the_last_bar_with_its_date(self):
        from stock_mcp_server._indicators import compute_volume
        df = self._df()
        v = compute_volume(df)
        self.assertEqual(v["latest"], int(df["volume"].iloc[-1]))
        self.assertEqual(v["latest_date"], df["date"].iloc[-1])

    def test_volume_rank_one_means_highest(self):
        from stock_mcp_server._indicators import compute_volume
        # 마지막 봉이 252봉 중 최대 거래량 → 1위
        df = self._df()
        self.assertEqual(compute_volume(df)["volume_rank_252b"], 1)

    def test_trade_value_is_close_times_volume(self):
        from stock_mcp_server._indicators import compute_volume
        df = self._df()
        v = compute_volume(df)
        self.assertEqual(
            v["trade_value_est_krw"], int(df["close"].iloc[-1]) * int(df["volume"].iloc[-1])
        )

    def test_position_uses_bars_not_days(self):
        from stock_mcp_server._indicators import compute_position
        p = compute_position(self._df())
        self.assertIn("bars_since_high", p)
        self.assertIn("bars_since_low", p)
        self.assertNotIn("days_since_high", p)
        self.assertNotIn("days_since_low", p)
        # 달력일을 원하면 직접 계산하라 — 날짜는 그대로 제공한다
        self.assertIn("high_date", p)

    def test_short_history_returns_named_nulls_not_missing_keys(self):
        """20봉 미만이어도 키는 있어야 한다 — 없으면 '0'으로 오해할 여지가 생긴다."""
        from stock_mcp_server._indicators import compute_volume
        v = compute_volume(self._df(5))
        self.assertEqual(set(v), {
            "latest", "latest_date", "avg_20b",
            "ratio_vs_avg_20b", "trade_value_est_krw", "volume_rank_252b",
        })
        self.assertTrue(all(val is None for val in v.values()))
