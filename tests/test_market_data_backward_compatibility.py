"""증권사 선로(broker market data routing) 도입 전 현행 동작 고정 테스트.

기존 사용자 계약을 characterization 테스트로 못박는다. 이 파일의 실패는
"새 기능이 기존 도구를 바꿨다"는 뜻이므로 테스트를 고치지 말고 코드를 되돌린다.

고정하는 계약:
- get_chart / get_indicators / get_indicators_bulk / get_us_chart 의 인자 순서와 기본값
- 국내 도구는 naver.get_ohlcv, 지표는 현행 compute_indicators 를 그대로 사용
- get_indicators_bulk 는 종목별 오류를 results 안에 보존
- get_us_chart 는 기존 interval 전부를 Yahoo(get_history)로 처리
- 가짜 broker 상태 파일이 있어도 기존 도구의 데이터 경로는 바뀌지 않는다
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server


def _extract_meta(text: str) -> dict:
    payload = text.split("RESULT_META_JSON_START", 1)[1].split(
        "RESULT_META_JSON_END", 1)[0].strip()
    return json.loads(payload)


_OPEN_CLOCK = {
    "krx": {"is_open": True, "last_trading_day": "2026-08-26"},
    "us": {"is_open": True, "last_trading_day": "2026-08-26"},
}
# 장 마감 후 조회. 진행 중 봉 경고가 붙지 않아 completeness 판정이 결정적이다.
_CLOSED_CLOCK = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours",
           "last_trading_day": "2026-08-26"},
}


def _kr_bar(date: str) -> dict:
    return {"date": date, "open": 100, "high": 101, "low": 99,
            "close": 100, "volume": 10}


def _kr_bars(n: int) -> list[dict]:
    out = []
    for i in range(n):
        y, rest = 2025 + (i // 250), i % 250
        m, d = (rest // 21) + 1, (rest % 21) + 1
        close = 100 + i * 0.3
        out.append({
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "open": close - 0.5, "high": close + 1, "low": close - 1,
            "close": round(close, 2), "volume": 1_000_000 + i,
        })
    return out


def _us_rows(n: int) -> list[dict]:
    out = []
    for i in range(n):
        close = 200 + i * 0.5
        out.append({
            "date": f"2026-07-{(i % 28) + 1:02d}",
            "open": close - 1, "high": close + 1, "low": close - 2,
            "close": round(close, 2), "volume": 5_000_000 + i,
        })
    return out


class PublicSignatureTests(unittest.TestCase):
    """공개 도구 시그니처 고정. 인자 추가는 반드시 끝에, 기존 순서·기본값 불변."""

    def test_get_chart_signature(self) -> None:
        sig = inspect.signature(server.get_chart)
        self.assertEqual(list(sig.parameters), ["code", "timeframe", "count"])
        self.assertEqual(sig.parameters["timeframe"].default, "day")
        self.assertEqual(sig.parameters["count"].default, 120)

    def test_get_indicators_signature(self) -> None:
        sig = inspect.signature(server.get_indicators)
        self.assertEqual(
            list(sig.parameters), ["code", "days", "include", "timeframe", "params"])
        self.assertEqual(sig.parameters["days"].default, 260)
        self.assertIsNone(sig.parameters["include"].default)
        self.assertEqual(sig.parameters["timeframe"].default, "day")
        self.assertIsNone(sig.parameters["params"].default)

    def test_get_indicators_bulk_signature(self) -> None:
        sig = inspect.signature(server.get_indicators_bulk)
        self.assertEqual(
            list(sig.parameters), ["codes", "days", "include", "timeframe", "params"])
        self.assertEqual(sig.parameters["days"].default, 260)
        self.assertEqual(sig.parameters["timeframe"].default, "day")

    def test_get_us_chart_signature_first_five(self) -> None:
        sig = inspect.signature(server.get_us_chart)
        self.assertEqual(
            list(sig.parameters)[:5],
            ["ticker", "period", "interval", "prepost", "limit"])
        self.assertEqual(sig.parameters["period"].default, "3mo")
        self.assertEqual(sig.parameters["interval"].default, "1d")
        self.assertIs(sig.parameters["prepost"].default, False)
        self.assertEqual(sig.parameters["limit"].default, 500)


class GetChartRouteTests(unittest.IsolatedAsyncioTestCase):
    """get_chart 는 timeframe 전부에서 현행 naver.get_ohlcv 를 호출한다."""

    async def test_day_week_month_call_naver_get_ohlcv(self) -> None:
        for timeframe, label in (("day", "일봉"), ("week", "주봉"), ("month", "월봉")):
            mock = AsyncMock(return_value=[_kr_bar("2026-08-25"), _kr_bar("2026-08-26")])
            with patch.object(server, "get_ohlcv", mock), patch.object(
                server, "build_market_clock", return_value=_CLOSED_CLOCK
            ):
                text = await server.get_chart(code="005930", timeframe=timeframe, count=2)

            mock.assert_awaited_once_with("005930", timeframe, 2)
            self.assertIn(label, text)
            self.assertIn("RESULT_META_JSON_START", text)
            meta = _extract_meta(text)
            # 주·월봉은 구간이 끝나기 전이면 partial 이 현행 동작이다.
            # 결정적으로 고정할 수 있는 것은 일봉 완결 케이스뿐이다.
            if timeframe == "day":
                self.assertEqual(meta["data_completeness"], "complete")
            else:
                self.assertIn(meta["data_completeness"], ("complete", "partial"))

    async def test_count_clamped_to_500(self) -> None:
        mock = AsyncMock(return_value=[_kr_bar("2026-08-26")])
        with patch.object(server, "get_ohlcv", mock), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            await server.get_chart(code="005930", count=9999)
        mock.assert_awaited_once_with("005930", "day", 500)


class GetIndicatorsRouteTests(unittest.IsolatedAsyncioTestCase):
    """get_indicators 는 naver.get_ohlcv + 현행 compute_indicators 조합을 유지한다."""

    async def test_calls_naver_and_current_compute_indicators(self) -> None:
        bars = _kr_bars(260)
        ohlcv_mock = AsyncMock(return_value=bars)
        seen: dict = {}

        real_compute = server.compute_indicators

        def spy_compute(ohlcv, include, params=None):
            seen["ohlcv_len"] = len(ohlcv)
            seen["include"] = list(include)
            return real_compute(ohlcv, include, params=params)

        with patch.object(server, "get_ohlcv", ohlcv_mock), patch.object(
            server, "compute_indicators", side_effect=spy_compute
        ), patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK):
            text = await server.get_indicators(code="005930", days=260)

        ohlcv_mock.assert_awaited_once_with("005930", timeframe="day", count=260)
        self.assertEqual(seen["ohlcv_len"], 260)
        self.assertEqual(seen["include"], ["ma", "ma_phase", "volume", "candle"])

        payload = json.loads(text)
        self.assertEqual(payload["code"], "005930")
        self.assertEqual(payload["timeframe"], "day")
        self.assertEqual(payload["days"], 260)
        self.assertIn("indicators", payload)
        self.assertIn("_meta", payload)
        self.assertIn("ma", payload["indicators"])

    async def test_days_clamped_between_30_and_500(self) -> None:
        mock = AsyncMock(return_value=_kr_bars(40))
        with patch.object(server, "get_ohlcv", mock), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            await server.get_indicators(code="005930", days=5)
        mock.assert_awaited_once_with("005930", timeframe="day", count=30)


class GetIndicatorsBulkErrorTests(unittest.IsolatedAsyncioTestCase):
    """bulk 는 한 종목이 죽어도 나머지를 살리고, 오류를 종목별로 보존한다."""

    async def test_per_code_errors_are_preserved(self) -> None:
        good = _kr_bars(260)

        async def fake_ohlcv(code, timeframe="day", count=260):
            if code == "000001":
                raise RuntimeError("boom")
            if code == "000002":
                return []
            return good

        with patch.object(server, "get_ohlcv", AsyncMock(side_effect=fake_ohlcv)), \
             patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK):
            text = await server.get_indicators_bulk(
                codes=["005930", "000001", "000002"])

        payload = json.loads(text)
        results = payload["results"]
        self.assertEqual(payload["count"], 3)
        self.assertNotIn("error", results["005930"])
        self.assertIn("error", results["000001"])
        self.assertIn("RuntimeError", results["000001"]["error"])
        self.assertIn("error", results["000002"])
        meta = payload["_meta"]
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertTrue(any("2개 종목 조회 실패" in w for w in meta["warnings"]))


class GetUsChartRouteTests(unittest.IsolatedAsyncioTestCase):
    """get_us_chart 는 기존 interval 전부를 yfinance_source.get_history 로 처리한다."""

    async def test_existing_intervals_call_yahoo(self) -> None:
        for interval in ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"):
            mock = AsyncMock(return_value=_us_rows(20))
            with patch.object(server.us, "get_history", mock), patch.object(
                server, "build_market_clock", return_value=_OPEN_CLOCK
            ):
                text = await server.get_us_chart(
                    ticker="AAPL", period="1mo", interval=interval)

            mock.assert_awaited_once_with(
                "AAPL", period="1mo", interval=interval, prepost=False)
            self.assertIn("AAPL", text)
            self.assertIn("20 bars", text)

    async def test_prepost_flag_passthrough(self) -> None:
        mock = AsyncMock(return_value=_us_rows(5))
        with patch.object(server.us, "get_history", mock), patch.object(
            server, "build_market_clock", return_value=_OPEN_CLOCK
        ):
            await server.get_us_chart(
                ticker="AAPL", period="1d", interval="5m", prepost=True)
        mock.assert_awaited_once_with(
            "AAPL", period="1d", interval="5m", prepost=True)

    async def test_limit_truncates_to_recent_rows(self) -> None:
        rows = _us_rows(30)
        with patch.object(server.us, "get_history", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK):
            text = await server.get_us_chart(
                ticker="AAPL", period="3mo", interval="1d", limit=10)
        self.assertIn("10 bars", text)
        self.assertIn("원본 30행 중 최근 10행", text)


class FakeBrokerStateIsolationTests(unittest.IsolatedAsyncioTestCase):
    """broker 상태 파일이 존재해도 기존 도구의 공급원 선택은 바뀌지 않는다.

    아직 broker 기능이 없는 시점에는 자명하게 통과한다. 기능이 들어온 뒤에도
    기존 도구가 이 파일을 읽고 경로를 바꾸면 안 된다는 회귀 방벽이다.
    """

    async def test_old_tools_ignore_broker_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            state_path = Path(home) / "broker_state.json"
            state_path.write_text(json.dumps({
                "connection_generation": 99,
                "active_provider": "kis",
                "active_profile": "real",
                "data_source_mode": "broker_first",
            }), encoding="utf-8")

            kr_mock = AsyncMock(return_value=[_kr_bar("2026-08-26")])
            us_mock = AsyncMock(return_value=_us_rows(5))
            with patch.dict(os.environ, {"STOCKLENS_HOME": home}), \
                 patch.object(server, "get_ohlcv", kr_mock), \
                 patch.object(server.us, "get_history", us_mock), \
                 patch.object(server, "build_market_clock", return_value=_OPEN_CLOCK):
                kr_text = await server.get_chart(code="005930", count=1)
                us_text = await server.get_us_chart(
                    ticker="AAPL", period="1d", interval="1d")

            kr_mock.assert_awaited_once_with("005930", "day", 1)
            us_mock.assert_awaited_once_with(
                "AAPL", period="1d", interval="1d", prepost=False)
            self.assertIn("일봉", kr_text)
            self.assertIn("AAPL", us_text)


if __name__ == "__main__":
    unittest.main()
