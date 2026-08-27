"""공급원 라우팅 결정표 테스트 (Task 11).

- 모드·시장·간격·연결 상태별 공급자 선택
- strict source=kis 는 fallback 없음
- 봉 0개 채택 시에만 요청 전체 재시작 가능
- 봉 1개라도 채택하면 공급원 변경 금지
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
            requested_source=source, capabilities=_caps(connected=connected))

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

    def test_auto_us_fallback_flag_set(self):
        res = self._route("auto", "US", "5m", True)
        self.assertTrue(res.fallback_allowed_before_first_bar)
        self.assertEqual(res.fallback_provider, "yahoo")


class FailoverTests(unittest.TestCase):
    def test_zero_bars_error_restarts_whole_request_on_fallback(self):
        kis = FakeProvider("kis", error=KisApiError("provider_unavailable"))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo}, _request(market="US",
                                                        venue="NAS")))
        self.assertEqual(ds.provider, "yahoo")
        self.assertTrue(meta["fallback_used"])
        self.assertEqual(meta["fallback_from"], "kis")
        self.assertEqual(kis.calls, 1)
        self.assertEqual(yahoo.calls, 1)

    def test_partial_result_is_not_filled_by_fallback(self):
        # 봉을 하나라도 채택했으면 fallback 호출 자체가 없어야 한다.
        kis = FakeProvider(
            "kis", result=_dataset("kis", [_bar(0)], complete=False))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(ds.provider, "kis")
        self.assertFalse(meta["fallback_used"])
        self.assertEqual(yahoo.calls, 0)
        self.assertFalse(ds.coverage["complete"])

    def test_strict_kis_failure_propagates_without_fallback(self):
        kis = FakeProvider("kis", error=KisApiError("rate_limited"))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="kis", capabilities=_caps())
        with self.assertRaises(KisApiError):
            _run(fetch_with_failover(
                res, {"kis": kis, "yahoo": yahoo},
                _request(market="US", venue="NAS", source="kis")))
        self.assertEqual(yahoo.calls, 0)

    def test_empty_dataset_restarts_on_fallback(self):
        # 예외가 아니어도 봉 0개면 "유효한 봉을 하나도 채택하지 않은" 상태다.
        # 설계 규칙 1: 이때만 요청 전체 재시작이 허용된다 (2026-08-27 실측:
        # 미국 야간에 KIS 가 세션 밖 행만 돌려줘 빈 결과가 나왔다).
        kis = FakeProvider("kis", result=_dataset("kis", []))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(ds.provider, "yahoo")
        self.assertTrue(meta["fallback_used"])

    def test_strict_kis_empty_dataset_returned_as_is(self):
        kis = FakeProvider("kis", result=_dataset("kis", []))
        yahoo = FakeProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="kis", capabilities=_caps())
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS", source="kis")))
        self.assertEqual(ds.provider, "kis")
        self.assertEqual(ds.bars, ())
        self.assertEqual(yahoo.calls, 0)

    def test_fallback_failure_also_propagates(self):
        kis = FakeProvider("kis", error=KisApiError("provider_unavailable"))
        yahoo = FakeProvider("yahoo", error=RuntimeError("yahoo down"))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        with self.assertRaises(RuntimeError):
            _run(fetch_with_failover(
                res, {"kis": kis, "yahoo": yahoo},
                _request(market="US", venue="NAS")))


if __name__ == "__main__":
    unittest.main()
