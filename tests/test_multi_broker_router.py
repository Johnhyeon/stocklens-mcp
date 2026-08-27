"""멀티 증권사 라우팅 테스트 (1.0 Task 10).

- auto 분봉은 주 사용 증권사(primary) 하나에 고정된다
- primary 장애 시 다른 증권사·Yahoo 로 자동 전환하지 않는다
- 명시 source 는 primary 가 아니어도 연결·검증된 공급자를 허용하되 strict
- 일·주·월봉 auto 는 기존 Naver·Yahoo 를 유지한다
- 추가 연결은 라우팅 결과를 바꾸지 않는다
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


def _caps(connected=True, kr=True, us=True):
    return {
        "connected": connected,
        "kr_intraday": kr and connected,
        "us_intraday": us and connected,
        "kr_daily": False,
        "us_daily": False,
    }


def _off():
    return _caps(connected=False)


def _resolve(source="auto", market="KR", interval="5m", mode="auto",
             primary=None, capabilities=None):
    return resolve_source(
        mode=mode, market=market, interval=interval,
        requested_source=source,
        capabilities=capabilities or {},
        primary_provider=primary)


def _bar():
    start = datetime(2026, 8, 27, 9, 0, tzinfo=KST)
    return NormalizedBar(
        start_at=start, end_at=start.replace(minute=1),
        open=Decimal(100), high=Decimal(101), low=Decimal(99),
        close=Decimal(100), volume=10, interval="1m", session="regular",
        complete=True, session_tail=False, expected_minutes=1,
        actual_minutes=1, data_integrity="complete", source_gap_status="none")


def _dataset(provider):
    return BarDataset(
        bars=(_bar(),), market="KR", symbol="005930", provider=provider,
        profile="real", venue="KRX", timezone="Asia/Seoul",
        session="regular", requested_interval="1m", source_interval="1m",
        aggregation_method="provider_native", adjustment_basis="unadjusted",
        source_endpoint="test", coverage={"complete": True}, warnings=())


class CountingProvider:
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


def _request():
    return BarRequest(
        symbol="005930", market="KR", interval="5m", start=None, end=None,
        trading_date=date(2026, 8, 27), row_limit=120, venue="KRX",
        session="regular", adjustment="unadjusted", completed_only=True,
        source="auto")


class PrimaryRoutingTests(unittest.TestCase):
    def test_auto_intraday_uses_primary_provider(self):
        for primary in ("kis", "kiwoom", "toss"):
            res = _resolve(primary=primary,
                           capabilities={primary: _caps()})
            self.assertEqual(res.selected_provider, primary)
            self.assertEqual(res.selection_reason,
                             "broker_connected_and_intraday_supported")
            self.assertEqual(res.primary_provider, primary)
            self.assertFalse(res.fallback_allowed_before_first_bar)
            self.assertIsNone(res.fallback_provider)

    def test_additional_connection_does_not_affect_resolution(self):
        # kiwoom·toss 를 추가 연결해도 primary(kis)가 계속 선택된다.
        caps = {"kis": _caps(), "kiwoom": _caps(), "toss": _caps()}
        res = _resolve(primary="kis", capabilities=caps)
        self.assertEqual(res.selected_provider, "kis")
        res = _resolve(primary="kis", capabilities=caps,
                       market="US")
        self.assertEqual(res.selected_provider, "kis")

    def test_primary_failure_has_no_automatic_broker_fallback(self):
        kiwoom = CountingProvider("kiwoom", error=RuntimeError("down"))
        kis = CountingProvider("kis", result=_dataset("kis"))
        res = _resolve(primary="kiwoom",
                       capabilities={"kiwoom": _caps(), "kis": _caps()})
        with self.assertRaises(RuntimeError):
            asyncio.run(fetch_with_failover(
                res, {"kiwoom": kiwoom, "kis": kis}, _request()))
        self.assertEqual(kis.calls, 0)

    def test_primary_failure_has_no_automatic_yahoo_fallback(self):
        toss = CountingProvider("toss", error=RuntimeError("down"))
        yahoo = CountingProvider("yahoo", result=_dataset("yahoo"))
        res = _resolve(primary="toss", market="US",
                       capabilities={"toss": _caps()})
        with self.assertRaises(RuntimeError):
            asyncio.run(fetch_with_failover(
                res, {"toss": toss, "yahoo": yahoo}, _request()))
        self.assertEqual(yahoo.calls, 0)

    def test_explicit_connected_non_primary_provider_is_allowed(self):
        caps = {"kis": _caps(), "toss": _caps()}
        res = _resolve(source="toss", primary="kis", capabilities=caps)
        self.assertEqual(res.selected_provider, "toss")
        self.assertEqual(res.selection_reason, "explicit_source_toss_strict")
        self.assertFalse(res.fallback_allowed_before_first_bar)

    def test_explicit_provider_is_strict(self):
        # 미연결 공급자를 명시하면 다른 공급자로 대신하지 않는다.
        for source in ("kis", "kiwoom", "toss"):
            with self.assertRaises(RouterError) as ctx:
                _resolve(source=source, primary="kis",
                         capabilities={"kis": _off(), "kiwoom": _off(),
                                       "toss": _off()})
            self.assertEqual(ctx.exception.provider_status, "not_configured")
            self.assertIn("strict", str(ctx.exception))

    def test_daily_auto_still_uses_naver_or_yahoo(self):
        caps = {"kiwoom": _caps()}
        res = _resolve(primary="kiwoom", capabilities=caps, interval="1d")
        self.assertEqual(res.selected_provider, "naver")
        res = _resolve(primary="kiwoom", capabilities=caps,
                       market="US", interval="1d")
        self.assertEqual(res.selected_provider, "yahoo")

    def test_no_primary_intraday_kr_is_structured_error(self):
        with self.assertRaises(RouterError) as ctx:
            _resolve(primary=None, capabilities={})
        self.assertEqual(ctx.exception.provider_status, "not_configured")

    def test_no_primary_us_intraday_uses_yahoo_initial_selection(self):
        res = _resolve(primary=None, capabilities={}, market="US")
        self.assertEqual(res.selected_provider, "yahoo")


if __name__ == "__main__":
    unittest.main()
