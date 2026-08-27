"""공급원 라우팅 결정표 테스트 (Task 11).

- 모드·시장·간격·연결 상태별 공급자 선택
- 자동 전환(fallback) 없음: 요청 시작 전 능력 기반 초기 선택만 있고,
  실패해도 다른 공급자나 Yahoo 로 자동 전환하지 않는다 (1.0 정책)
- 공급자 전환은 사용자의 직접 선택(source 명시·모드 변경)으로만
- 일부 반환을 다른 공급원으로 메우지 않음
- 미검증 KIS 일봉 능력은 선택되지 않음
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.kis_client import KisApiError
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
)
from stock_mcp_server.market_data.router import (
    RouterError,
    fetch_with_failover,
    resolve_source,
)

KST = ZoneInfo("Asia/Seoul")


def _caps(connected=True, kr_intraday=True, us_intraday=True):
    return {
        "connected": connected,
        "kr_intraday": kr_intraday and connected,
        "us_intraday": us_intraday and connected,
        # 일·주·월봉은 수정주가 검증 전까지 unverified 다.
        "kr_daily": False,
        "us_daily": False,
    }


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="005930", market="KR", interval="5m",
        start=None, end=None, trading_date=date(2026, 8, 27),
        row_limit=120, venue="KRX", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _bar(minute=0) -> NormalizedBar:
    start = datetime(2026, 8, 27, 9, minute, tzinfo=KST)
    return NormalizedBar(
        start_at=start, end_at=start.replace(minute=minute + 1),
        open=Decimal(100), high=Decimal(101), low=Decimal(99),
        close=Decimal(100), volume=10, interval="1m", session="regular",
        complete=True, session_tail=False, expected_minutes=1,
        actual_minutes=1, data_integrity="complete", source_gap_status="none",
    )


def _dataset(provider="kis", bars=(), complete=True) -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market="KR", symbol="005930", provider=provider,
        profile="real" if provider == "kis" else None, venue="KRX",
        timezone="Asia/Seoul", session="regular", requested_interval="1m",
        source_interval="1m", aggregation_method="provider_native",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={"complete": complete}, warnings=(),
    )


class FakeProvider:
    def __init__(self, provider_id, result=None, error=None):
        self.provider_id = provider_id
        self._result = result
        self._error = error
        self.calls = 0

    async def fetch_bars(self, request):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def _run(coro):
    return asyncio.run(coro)


class DecisionTableTests(unittest.TestCase):
    def _route(self, mode, market, interval, connected, source="auto"):
        return resolve_source(
            mode=mode, market=market, interval=interval,
            requested_source=source,
            capabilities={"kis": _caps(connected=connected)},
            primary_provider="kis")

    def test_route_table(self):
        cases = [
            ("auto", "KR", "5m", True, "kis"),
            ("auto", "US", "5m", True, "kis"),
            ("auto", "US", "5m", False, "yahoo"),
            ("auto", "KR", "1d", True, "naver"),
            ("auto", "US", "1d", True, "yahoo"),
            ("legacy", "US", "5m", True, "yahoo"),
            ("legacy", "KR", "1d", True, "naver"),
            ("legacy", "US", "1d", True, "yahoo"),
            ("broker_first", "KR", "5m", True, "kis"),
            ("broker_first", "US", "5m", True, "kis"),
        ]
        for mode, market, interval, connected, expected in cases:
            res = self._route(mode, market, interval, connected)
            self.assertEqual(
                res.selected_provider, expected,
                f"{mode}/{market}/{interval}/connected={connected}")
            self.assertEqual(res.mode, mode)
            self.assertTrue(res.selection_reason)

    def test_unverified_daily_broker_capability_not_selected(self):
        # broker_first 여도 일봉은 검증 전이라 KIS 를 선택하지 않는다.
        res = self._route("broker_first", "KR", "1d", True)
        self.assertEqual(res.selected_provider, "naver")
        res = self._route("broker_first", "US", "1wk", True)
        self.assertEqual(res.selected_provider, "yahoo")

    def test_legacy_mode_never_selects_kis(self):
        for market, interval in (("KR", "5m"), ("US", "1m"), ("US", "60m")):
            if market == "KR":
                with self.assertRaises(RouterError):
                    self._route("legacy", market, interval, True)
            else:
                res = self._route("legacy", market, interval, True)
                self.assertNotEqual(res.selected_provider, "kis")

    def test_kr_intraday_without_kis_is_structured_error(self):
        with self.assertRaises(RouterError) as ctx:
            self._route("auto", "KR", "5m", False)
        self.assertEqual(ctx.exception.provider_status, "not_configured")

    def test_strict_kis_has_no_fallback_flag(self):
        res = self._route("auto", "US", "5m", True, source="kis")
        self.assertEqual(res.selected_provider, "kis")
        self.assertFalse(res.fallback_allowed_before_first_bar)

    def test_strict_kis_unavailable_raises(self):
        with self.assertRaises(RouterError) as ctx:
            self._route("auto", "US", "5m", False, source="kis")
        self.assertEqual(ctx.exception.provider_status, "not_configured")

    def test_explicit_naver_yahoo(self):
        res = self._route("auto", "KR", "1d", True, source="naver")
        self.assertEqual(res.selected_provider, "naver")
        res = self._route("auto", "US", "5m", True, source="yahoo")
        self.assertEqual(res.selected_provider, "yahoo")

    def test_no_fallback_in_any_mode(self):
        # 1.0 정책: 자동 전환 없음. 연결·능력 확인 시 KIS 고정.
        for mode in ("auto", "broker_first"):
            res = self._route(mode, "US", "5m", True)
            self.assertEqual(res.selected_provider, "kis")
            self.assertFalse(res.fallback_allowed_before_first_bar)
            self.assertIsNone(res.fallback_provider)


class NoAutoSwitchTests(unittest.TestCase):
    """1.0 정책: 장애가 나도 다른 공급자로 자동 전환하지 않는다."""

    def test_kis_error_propagates_without_touching_yahoo(self):
        kis = FakeProvider("kis", error=KisApiError("provider_unavailable"))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities={"kis": _caps()},
            primary_provider="kis")
        with self.assertRaises(KisApiError):
            _run(fetch_with_failover(
                res, {"kis": kis, "yahoo": yahoo},
                _request(market="US", venue="NAS")))
        self.assertEqual(yahoo.calls, 0)

    def test_empty_dataset_returned_as_is(self):
        # 빈 결과도 다른 공급자로 메우지 않는다. 빈 것은 빈 것이다.
        kis = FakeProvider("kis", result=_dataset("kis", []))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities={"kis": _caps()},
            primary_provider="kis")
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(ds.provider, "kis")
        self.assertEqual(ds.bars, ())
        self.assertFalse(meta["fallback_used"])
        self.assertEqual(yahoo.calls, 0)

    def test_partial_result_is_not_filled_by_other_provider(self):
        kis = FakeProvider(
            "kis", result=_dataset("kis", [_bar(0)], complete=False))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities={"kis": _caps()},
            primary_provider="kis")
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(ds.provider, "kis")
        self.assertFalse(meta["fallback_used"])
        self.assertEqual(yahoo.calls, 0)
        self.assertFalse(ds.coverage["complete"])

    def test_explicit_yahoo_is_user_choice_not_fallback(self):
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="yahoo", capabilities={"kis": _caps()},
            primary_provider="kis")
        ds, meta = _run(fetch_with_failover(
            res, {"yahoo": yahoo},
            _request(market="US", venue="NAS", source="yahoo")))
        self.assertEqual(ds.provider, "yahoo")
        self.assertFalse(meta["fallback_used"])


if __name__ == "__main__":
    unittest.main()
