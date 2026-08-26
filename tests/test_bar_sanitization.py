"""거래정지 placeholder 봉 분리 - SL-01 회귀.

실측(2026-08-27): 한화에어로스페이스 주봉 2024-09-06/13/20 세 개가
고가 0·저가 0·거래량 0으로 오고(종가만 이월), 이월드 2026-08-20 일봉은
시가·고가·저가 0·종가 500이다. 이 봉이 그대로 계산에 들어가서

  - position 의 52주 저가가 0 -> ZeroDivisionError
  - 그런데도 배치 응답은 data_completeness=complete, 경고 없음

이 파일은 세 층을 각각 고정한다: 봉 검증, 계산 가드, 그리고
"지표가 오류인데 complete" 를 막는 완전성 판정.
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
from stock_mcp_server._indicators import compute_indicators, split_valid_bars


def extract_meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
        return json.loads(payload)
    return json.loads(text)["_meta"]


def _bar(date, o=100, h=101, lo=99, c=100, v=1000):
    return {"date": date, "open": o, "high": h, "low": lo, "close": c, "volume": v}


# 한화에어로 실측 패턴: 종가만 이월되고 고가·저가·거래량이 0
def _halt_placeholder(date, carried_close=295411):
    return {"date": date, "open": carried_close, "high": 0, "low": 0,
            "close": carried_close, "volume": 0}


# 이월드 실측 패턴: 시가·고가·저가 0, 종가만 있음
def _zero_open_placeholder(date, close=500):
    return {"date": date, "open": 0, "high": 0, "low": 0, "close": close, "volume": 0}


_CLOSED = {
    "krx": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_weekend", "last_trading_day": "2026-08-26"},
}
_EVENING = datetime(2026, 8, 26, 21, tzinfo=rmeta.KST)


class SplitValidBarsTests(unittest.TestCase):
    """봉 검증 - 가격이 유한한 양수이고 고가·저가 관계가 맞아야 실제 봉이다."""

    def test_halt_placeholder_is_excluded_with_reason(self):
        rows = [_bar("20240830"), _halt_placeholder("20240906"), _bar("20240927")]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual([r["date"] for r in valid], ["20240830", "20240927"])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["date"], "20240906")
        self.assertIn("0", excluded[0]["reason"])

    def test_zero_open_placeholder_is_excluded(self):
        rows = [_bar("20260819"), _zero_open_placeholder("20260820"), _bar("20260821")]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual(len(valid), 2)
        self.assertEqual(excluded[0]["date"], "20260820")

    def test_inverted_high_low_is_excluded(self):
        rows = [_bar("20260819"), _bar("20260820", o=100, h=95, lo=99, c=100)]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual(len(valid), 1)
        self.assertEqual(excluded[0]["date"], "20260820")

    def test_suspension_bar_with_carried_price_is_kept(self):
        """o=h=l=c 이고 거래량 0인 봉은 기하가 멀쩡하다. 실제 기준가 표시라 남긴다."""
        rows = [_bar("20260819"),
                _bar("20260820", o=500, h=500, lo=500, c=500, v=0)]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual(len(valid), 2)
        self.assertEqual(excluded, [])

    def test_non_numeric_and_negative_are_excluded(self):
        rows = [_bar("20260819"),
                _bar("20260820", o=None), _bar("20260821", lo=-5)]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(excluded), 2)

    def test_all_valid_returns_everything(self):
        rows = [_bar(f"202608{d:02d}") for d in range(1, 11)]
        valid, excluded = split_valid_bars(rows)
        self.assertEqual(len(valid), 10)
        self.assertEqual(excluded, [])


class PositionGuardTests(unittest.TestCase):
    """계산 가드 - placeholder 가 빠졌어도 남을 수 있는 극단 입력."""

    HANWHA = ([_bar(f"2024{m:02d}{d:02d}", o=290000, h=300000, lo=280000, c=295000,
                    v=10000) for m in (7, 8) for d in (5, 12, 19, 26)]
              + [_halt_placeholder("20240906"), _halt_placeholder("20240913"),
                 _halt_placeholder("20240920")]
              + [_bar(f"2024{m:02d}{d:02d}", o=290000, h=305000, lo=285000, c=300000,
                      v=12000) for m in (10, 11) for d in (4, 11, 18, 25)])

    def test_position_survives_halt_placeholders(self):
        out = compute_indicators(self.HANWHA, ["position"])
        pos = out["position"]
        self.assertNotIn("error", pos)
        self.assertGreater(pos["low_52w"], 0)

    def test_flat_series_does_not_divide_by_zero(self):
        rows = [_bar(f"202608{d:02d}", o=500, h=500, lo=500, c=500, v=0)
                for d in range(1, 11)]
        out = compute_indicators(rows, ["position"])
        self.assertNotIn("error", out["position"])

    def test_ma_and_volume_use_only_valid_bars(self):
        """placeholder 를 뺀 손계산과 일치해야 한다."""
        rows = ([_bar(f"202607{d:02d}", o=100 + d, h=120 + d, lo=90, c=100 + d, v=1000)
                 for d in range(1, 6)]
                + [_zero_open_placeholder("20260706")])
        out = compute_indicators(rows, ["ma"])
        # placeholder(종가 500)가 섞이면 ma5 는 500 쪽으로 튄다
        expected_ma5 = sum(100 + d for d in range(1, 6)) / 5
        self.assertEqual(out["ma"]["ma5"], int(expected_ma5))


class IndicatorErrorCompletenessTests(unittest.IsolatedAsyncioTestCase):
    """지표가 오류인데 complete 로 나가면 안 된다 (요구 5)."""

    ROWS = [_bar(f"202607{d:02d}") for d in range(1, 29)]

    def _patches(self):
        return (
            patch.object(server, "build_market_clock", return_value=_CLOSED),
            patch.object(server, "_now_kst", return_value=_EVENING),
        )

    async def test_single_indicator_error_marks_partial(self):
        broken = {"position": {"error": "ZeroDivisionError: float division by zero"},
                  "ma": {"ma5": 100}}
        a, b = self._patches()
        with a, b, \
             patch.object(server, "get_ohlcv", AsyncMock(return_value=self.ROWS)), \
             patch.object(server, "compute_indicators", return_value=broken):
            payload = json.loads(await server.get_indicators(
                code="084680", days=28, include=["position", "ma"]))
        meta = payload["_meta"]
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(meta["coverage"]["reason"], "indicator_error")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        self.assertEqual(meta["indicator_errors"],
                         [{"indicator": "position",
                           "error": "ZeroDivisionError: float division by zero"}])
        self.assertTrue(any("position" in w for w in meta["warnings"]))

    async def test_bulk_reports_per_code_indicator_errors(self):
        def broken(ohlcv, include, params=None):
            return {"position": {"error": "ZeroDivisionError: float division by zero"},
                    "ma": {"ma5": 100}}

        a, b = self._patches()
        with a, b, \
             patch.object(server, "get_ohlcv", AsyncMock(return_value=self.ROWS)), \
             patch.object(server, "compute_indicators", side_effect=broken):
            payload = json.loads(await server.get_indicators_bulk(
                codes=["084680", "005930"], days=28, include=["position", "ma"]))
        meta = payload["_meta"]
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertEqual(meta["coverage"]["reason"], "indicator_error")
        self.assertEqual(
            {e["code"] for e in meta["indicator_errors"]}, {"084680", "005930"})

    async def test_clean_indicators_stay_complete(self):
        a, b = self._patches()
        # ma 는 28봉으로는 ma240 부족이라 원래(SL-06) partial 이 맞다.
        # 여기서는 지표 오류 경로만 보므로 이력이 충분한 candle 만 요청한다.
        with a, b, patch.object(server, "get_ohlcv", AsyncMock(return_value=self.ROWS)):
            payload = json.loads(await server.get_indicators(
                code="005930", days=28, include=["candle"]))
        self.assertEqual(payload["_meta"]["data_completeness"], "complete")
        self.assertNotIn("indicator_errors", payload["_meta"])


class ExclusionMetaTests(unittest.IsolatedAsyncioTestCase):
    """제외된 봉이 있으면 그 사실이 메타·본문에 남아야 한다 (요구 2)."""

    ROWS = ([_bar(f"202607{d:02d}") for d in range(1, 27)]
            + [_zero_open_placeholder("20260820")]
            + [_bar("20260821"), _bar("20260826")])

    def _patches(self):
        return (
            patch.object(server, "get_ohlcv", AsyncMock(return_value=self.ROWS)),
            patch.object(server, "build_market_clock", return_value=_CLOSED),
            patch.object(server, "_now_kst", return_value=_EVENING),
        )

    async def test_chart_excludes_and_records_policy(self):
        a, b, c = self._patches()
        with a, b, c:
            text = await server.get_chart(code="084680", timeframe="day", count=29)
        meta = extract_meta(text)
        exc = meta["bar_exclusions"]
        self.assertEqual(exc["policy"], "exclude")
        self.assertEqual(exc["count"], 1)
        self.assertEqual(exc["bars"][0]["date"], "2026-08-20")
        self.assertNotIn("| 0 |", text)
        self.assertIn("거래정지", text)
        self.assertTrue(any("2026-08-20" in w for w in meta["warnings"]))

    async def test_indicators_record_exclusions(self):
        a, b, c = self._patches()
        with a, b, c:
            payload = json.loads(await server.get_indicators(
                code="084680", days=29, include=["position", "ma"]))
        meta = payload["_meta"]
        self.assertEqual(meta["bar_exclusions"]["count"], 1)
        self.assertNotIn("error", payload["indicators"]["position"])

    async def test_no_placeholder_no_exclusion_key(self):
        rows = [_bar(f"202607{d:02d}") for d in range(1, 11)]
        with patch.object(server, "get_ohlcv", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_CLOSED), \
             patch.object(server, "_now_kst", return_value=_EVENING):
            text = await server.get_chart(code="005930", timeframe="day", count=10)
        self.assertNotIn("bar_exclusions", extract_meta(text))


class MultiChartStatsSanitizationTests(unittest.IsolatedAsyncioTestCase):
    """기간 통계도 placeholder 에 오염되면 안 된다 - 저가 0이 52주 저가가 된다."""

    async def test_low_is_not_zero(self):
        import stock_mcp_server.naver as naver

        rows = ([_bar(f"202607{d:02d}", o=900, h=1000, lo=800, c=950, v=500)
                 for d in range(1, 27)]
                + [_zero_open_placeholder("20260820", close=500)])

        async def fake_ohlcv(code, timeframe, count):
            return rows

        with patch.object(naver, "get_ohlcv", AsyncMock(side_effect=fake_ohlcv)):
            stats = await naver.get_multi_chart_stats(["084680"], days=260)
        s = stats[0]
        self.assertGreater(s["low"], 0)
        self.assertEqual(s["excluded_bars"], 1)
        # 현재가는 placeholder 종가(500)가 아니라 마지막 유효 봉 종가여야 한다
        self.assertEqual(s["current_price"], 950)


if __name__ == "__main__":
    unittest.main(verbosity=2)
