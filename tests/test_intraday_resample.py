"""세션 안전 집계(resample) 테스트 (Task 9).

산식: 시가=첫 행, 고가=최대, 저가=최소, 종가=마지막 행, 거래량=합.
forward fill 금지, 날짜·세션 경계 초과 금지, 세션 꼬리 분리.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.models import BarDataset, NormalizedBar
from stock_mcp_server.market_data.resample import resample_intraday

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def _one_minute_bars(open_at: datetime, count: int,
                     base: int = 1000) -> list[NormalizedBar]:
    """open_at 부터 1분봉 count 개. 값은 결정적(인덱스 기반)."""
    bars = []
    for i in range(count):
        start = open_at + timedelta(minutes=i)
        o = base + i
        bars.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=1),
            open=Decimal(o), high=Decimal(o + 2), low=Decimal(o - 2),
            close=Decimal(o + 1), volume=10 + i,
            interval="1m", session="regular", complete=True,
            session_tail=False, expected_minutes=1, actual_minutes=1,
            data_integrity="complete", source_gap_status="none",
        ))
    return bars


def _dataset(bars, market="KR", tz="Asia/Seoul") -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market=market, symbol="005930", provider="kis",
        profile="real", venue="KRX" if market == "KR" else "NAS",
        timezone=tz, session="regular", requested_interval="1m",
        source_interval="1m", aggregation_method="provider_native",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={}, warnings=(),
    )


def _kr_full_session() -> BarDataset:
    return _dataset(_one_minute_bars(
        datetime(2026, 8, 27, 9, 0, tzinfo=KST), 390))


def _us_full_session() -> BarDataset:
    return _dataset(_one_minute_bars(
        datetime(2026, 8, 26, 9, 30, tzinfo=NY), 390),
        market="US", tz="America/New_York")


_KR_AFTER_CLOSE = datetime(2026, 8, 27, 16, 0, tzinfo=KST)
_US_AFTER_CLOSE = datetime(2026, 8, 26, 16, 30, tzinfo=NY)


class ArithmeticTests(unittest.TestCase):
    def test_5m_ohlcv_arithmetic(self):
        ds = resample_intraday(_kr_full_session(), "5m", now=_KR_AFTER_CLOSE)
        first = ds.bars[0]
        # 시가=첫 행 open, 고가=max(high), 저가=min(low), 종가=마지막 close
        self.assertEqual(first.open, Decimal(1000))
        self.assertEqual(first.high, Decimal(1004 + 2))
        self.assertEqual(first.low, Decimal(1000 - 2))
        self.assertEqual(first.close, Decimal(1004 + 1))
        self.assertEqual(first.volume, sum(10 + i for i in range(5)))
        self.assertEqual(len(ds.bars), 78)
        self.assertEqual(ds.aggregation_method, "stocklens_session_resample")
        self.assertEqual(ds.source_interval, "1m")
        self.assertEqual(ds.requested_interval, "5m")

    def test_60m_bucket_boundaries_anchor_at_session_open(self):
        ds = resample_intraday(_kr_full_session(), "60m", now=_KR_AFTER_CLOSE)
        starts = [b.start_at.strftime("%H%M") for b in ds.bars]
        self.assertEqual(
            starts, ["0900", "1000", "1100", "1200", "1300", "1400", "1500"])
        # 마지막 봉은 15:00~15:30 세션 꼬리다.
        self.assertEqual(ds.bars[-1].actual_minutes, 30)
        self.assertTrue(ds.bars[-1].session_tail)


class TailTests(unittest.TestCase):
    def test_kr_240m_regular_session_has_full_and_tail(self):
        ds = resample_intraday(_kr_full_session(), "240m", now=_KR_AFTER_CLOSE)
        self.assertEqual(
            [(b.actual_minutes, b.session_tail, b.complete) for b in ds.bars],
            [(240, False, True), (150, True, True)])
        self.assertEqual(ds.bars[0].start_at.strftime("%H%M"), "0900")
        self.assertEqual(ds.bars[1].start_at.strftime("%H%M"), "1300")
        self.assertEqual(ds.bars[1].end_at.strftime("%H%M"), "1530")

    def test_us_240m_regular_session_has_full_and_tail(self):
        ds = resample_intraday(_us_full_session(), "240m", now=_US_AFTER_CLOSE)
        self.assertEqual(
            [(b.actual_minutes, b.session_tail, b.complete) for b in ds.bars],
            [(240, False, True), (150, True, True)])
        self.assertEqual(ds.bars[1].start_at.strftime("%H%M"), "1330")

    def test_us_120m_tail(self):
        ds = resample_intraday(_us_full_session(), "120m", now=_US_AFTER_CLOSE)
        self.assertEqual(
            [(b.actual_minutes, b.session_tail) for b in ds.bars],
            [(120, False), (120, False), (120, False), (30, True)])

    def test_us_early_close_240m_single_tail(self):
        # 2026-11-27 은 13:00 조기 폐장. 09:30~13:00 = 210분 하나.
        bars = _one_minute_bars(
            datetime(2026, 11, 27, 9, 30, tzinfo=NY), 210)
        ds = resample_intraday(
            _dataset(bars, market="US", tz="America/New_York"), "240m",
            now=datetime(2026, 11, 27, 14, 0, tzinfo=NY))
        self.assertEqual(
            [(b.actual_minutes, b.session_tail, b.complete) for b in ds.bars],
            [(210, True, True)])


class CompletenessTests(unittest.TestCase):
    def test_incomplete_current_bucket(self):
        # 10:07 장중: 두 번째 5분봉(10:05~10:10)은 미완성이다.
        bars = _one_minute_bars(
            datetime(2026, 8, 27, 10, 0, tzinfo=KST), 8)
        ds = resample_intraday(
            _dataset(bars), "5m",
            now=datetime(2026, 8, 27, 10, 7, 30, tzinfo=KST))
        self.assertTrue(ds.bars[0].complete)
        self.assertFalse(ds.bars[-1].complete)

    def test_missing_minutes_marked_partial_gap(self):
        bars = _one_minute_bars(
            datetime(2026, 8, 27, 9, 0, tzinfo=KST), 5)
        # 9:02 행 제거 → 5분 버킷에 4행만 존재.
        bars = [b for b in bars if b.start_at.minute != 2]
        ds = resample_intraday(
            _dataset(bars), "5m", now=_KR_AFTER_CLOSE)
        self.assertEqual(ds.bars[0].data_integrity, "partial")
        self.assertEqual(ds.bars[0].source_gap_status, "unknown_gap")
        # forward fill 로 5행처럼 만들지 않는다. 거래량은 실제 합이다.
        self.assertEqual(ds.bars[0].volume, sum(
            10 + i for i in range(5) if i != 2))

    def test_no_cross_date_aggregation(self):
        day1 = _one_minute_bars(
            datetime(2026, 8, 26, 15, 0, tzinfo=KST), 30)
        day2 = _one_minute_bars(
            datetime(2026, 8, 27, 9, 0, tzinfo=KST), 30)
        ds = resample_intraday(
            _dataset(day1 + day2), "60m", now=_KR_AFTER_CLOSE)
        for bar in ds.bars:
            self.assertEqual(bar.start_at.date(), bar.end_at.date())
        dates = {b.start_at.date() for b in ds.bars}
        self.assertEqual(len(dates), 2)

    def test_out_of_session_rows_dropped_with_warning(self):
        bars = _one_minute_bars(
            datetime(2026, 8, 27, 8, 30, tzinfo=KST), 10)  # 08:30~08:40 장전
        bars += _one_minute_bars(
            datetime(2026, 8, 27, 9, 0, tzinfo=KST), 10)
        ds = resample_intraday(_dataset(bars), "5m", now=_KR_AFTER_CLOSE)
        self.assertTrue(all(b.start_at.hour >= 9 for b in ds.bars))
        self.assertTrue(any("세션 밖" in w for w in ds.warnings))

    def test_non_trading_day_rows_dropped(self):
        bars = _one_minute_bars(
            datetime(2026, 8, 29, 9, 0, tzinfo=KST), 10)  # 토요일
        ds = resample_intraday(_dataset(bars), "5m", now=_KR_AFTER_CLOSE)
        self.assertEqual(ds.bars, ())


class InvalidInputTests(unittest.TestCase):
    def test_target_must_be_multiple_of_source(self):
        with self.assertRaises(ValueError):
            resample_intraday(_kr_full_session(), "7m", now=_KR_AFTER_CLOSE)

    def test_same_interval_passthrough_still_session_checked(self):
        ds = resample_intraday(_kr_full_session(), "1m", now=_KR_AFTER_CLOSE)
        self.assertEqual(len(ds.bars), 390)

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            resample_intraday(_kr_full_session(), "5m",
                              now=datetime(2026, 8, 27, 16, 0))


if __name__ == "__main__":
    unittest.main()
