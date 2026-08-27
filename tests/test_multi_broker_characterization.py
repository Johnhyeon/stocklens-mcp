"""1.0 멀티 증권사 착수 전 KIS 기준 동작 고정 (1.0 Task 2 특성화 테스트).

이 파일은 새 기능을 검증하지 않는다. 레지스트리 도입 리팩터링(Task 3+)이
기존 KIS·네이버·야후 계약을 바꾸지 않았음을 증명하는 안전망이다.
여기 테스트가 깨지면 리팩터링이 기존 계약을 바꾼 것이므로, 테스트가 아니라
리팩터링을 고친다. (테스트 수정이 필요하면 사유를 커밋 메시지에 남긴다.)
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server._result_meta import (
    PROVIDER_EXTENSION_FIELDS,
    PROVIDER_STATUSES,
    provider_extension,
)
from stock_mcp_server.market_data.kis_client import KisApiError
from stock_mcp_server.market_data.models import (
    BarDataset,
    BarRequest,
    NormalizedBar,
)
from stock_mcp_server.market_data.naver_provider import NaverBarProvider
from stock_mcp_server.market_data.router import (
    RouterError,
    SourceResolution,
    fetch_with_failover,
    resolve_source,
)
from stock_mcp_server.market_data.yahoo_provider import YahooBarProvider

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def _caps(connected=True):
    return {
        "connected": connected,
        "kr_intraday": connected,
        "us_intraday": connected,
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


def _dataset(provider="kis", bars=()) -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market="KR", symbol="005930", provider=provider,
        profile="real" if provider == "kis" else None, venue="KRX",
        timezone="Asia/Seoul", session="regular", requested_interval="1m",
        source_interval="1m", aggregation_method="provider_native",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={"complete": True}, warnings=(),
    )


class _CountingProvider:
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


class LegacyModeCharacterization(unittest.TestCase):
    def test_legacy_mode_makes_zero_broker_calls(self):
        # legacy 모드는 KIS 연결·능력이 있어도 절대 KIS 를 고르지 않는다.
        cases = [
            ("KR", "1d", "naver"),
            ("US", "1d", "yahoo"),
            ("US", "5m", "yahoo"),
        ]
        for market, interval, expected in cases:
            res = resolve_source(
                mode="legacy", market=market, interval=interval,
                requested_source="auto", capabilities=_caps())
            self.assertEqual(res.selected_provider, expected)
            self.assertEqual(res.selection_reason,
                             "legacy_mode_default_source")

        # 실제 조회 경로에서도 KIS 호출은 0회다.
        kis = _CountingProvider("kis", result=_dataset("kis", [_bar(0)]))
        yahoo = _CountingProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="legacy", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(kis.calls, 0)
        self.assertEqual(yahoo.calls, 1)

        # KR 분봉은 legacy 공급자가 없으므로 KIS 로 대신하지 않고 오류다.
        with self.assertRaises(RouterError) as ctx:
            resolve_source(
                mode="legacy", market="KR", interval="5m",
                requested_source="auto", capabilities=_caps())
        self.assertEqual(ctx.exception.provider_status, "not_configured")


class KisRoutingCharacterization(unittest.TestCase):
    def test_kis_auto_intraday_selects_kis_when_verified(self):
        # SourceResolution 전체 필드를 정확히 고정한다.
        for market in ("KR", "US"):
            res = resolve_source(
                mode="auto", market=market, interval="5m",
                requested_source="auto", capabilities=_caps())
            self.assertEqual(res, SourceResolution(
                requested_source="auto",
                selected_provider="kis",
                selection_reason="broker_connected_and_intraday_supported",
                fallback_allowed_before_first_bar=False,
                mode="auto",
                capability_version=1,
                fallback_provider=None,
            ))

    def test_explicit_kis_is_strict(self):
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="kis", capabilities=_caps())
        self.assertEqual(res.selected_provider, "kis")
        self.assertEqual(res.selection_reason, "explicit_source_kis_strict")
        self.assertFalse(res.fallback_allowed_before_first_bar)
        self.assertIsNone(res.fallback_provider)

        # 미연결이면 다른 공급자로 대신하지 않고 not_configured 오류다.
        with self.assertRaises(RouterError) as ctx:
            resolve_source(
                mode="auto", market="US", interval="5m",
                requested_source="kis", capabilities=_caps(connected=False))
        self.assertEqual(ctx.exception.provider_status, "not_configured")
        self.assertIn("strict", str(ctx.exception))

    def test_kis_request_never_mixes_pages_with_yahoo(self):
        # 장애 전파: KIS 오류는 그대로 올라가고 Yahoo 는 호출되지 않는다.
        kis = _CountingProvider(
            "kis", error=KisApiError("provider_unavailable"))
        yahoo = _CountingProvider("yahoo", result=_dataset("yahoo", [_bar(0)]))
        res = resolve_source(
            mode="auto", market="US", interval="5m",
            requested_source="auto", capabilities=_caps())
        with self.assertRaises(KisApiError):
            _run(fetch_with_failover(
                res, {"kis": kis, "yahoo": yahoo},
                _request(market="US", venue="NAS")))
        self.assertEqual(yahoo.calls, 0)

        # 빈 결과도 다른 공급자로 메우지 않고, meta 는 이 형태 그대로다.
        kis = _CountingProvider("kis", result=_dataset("kis", []))
        ds, meta = _run(fetch_with_failover(
            res, {"kis": kis, "yahoo": yahoo},
            _request(market="US", venue="NAS")))
        self.assertEqual(ds.provider, "kis")
        self.assertEqual(ds.bars, ())
        self.assertEqual(yahoo.calls, 0)
        self.assertEqual(meta, {
            "requested_source": "auto",
            "selected_provider": "kis",
            "selection_reason": "broker_connected_and_intraday_supported",
            "mode": "auto",
            "fallback_used": False,
            "fallback_from": None,
        })


_NAVER_ROWS = [
    {"date": "20260825", "open": 100, "high": 110, "low": 95,
     "close": 105, "volume": 1000},
    {"date": "20260826", "open": 105, "high": 112, "low": 104,
     "close": 111, "volume": 2000},
]

_YAHOO_DAILY_ROWS = [
    {"date": datetime(2026, 8, 25), "open": 200.0, "high": 205.0,
     "low": 199.0, "close": 204.0, "volume": 1_000_000.0},
    {"date": datetime(2026, 8, 26), "open": 204.0, "high": 206.0,
     "low": 202.0, "close": 205.0, "volume": 1_100_000.0},
]


class LegacyProviderContractCharacterization(unittest.IsolatedAsyncioTestCase):
    async def test_existing_naver_daily_contract_is_unchanged(self):
        mock = AsyncMock(return_value=copy.deepcopy(_NAVER_ROWS))
        with patch(
                "stock_mcp_server.market_data.naver_provider.get_ohlcv", mock):
            ds = await NaverBarProvider().fetch_bars(
                _request(interval="1d", trading_date=None, row_limit=2))

        mock.assert_awaited_once_with("005930", "day", 2)
        self.assertEqual(ds.provider, "naver")
        self.assertIsNone(ds.profile)
        self.assertEqual(ds.venue, "KRX")
        self.assertEqual(ds.timezone, "Asia/Seoul")
        self.assertEqual(ds.session, "regular")
        self.assertEqual(ds.requested_interval, "1d")
        self.assertEqual(ds.source_interval, "1d")
        self.assertEqual(ds.aggregation_method, "provider_native")
        self.assertEqual(ds.adjustment_basis, "unknown")
        self.assertEqual(ds.source_endpoint, "naver_fchart")
        self.assertEqual(ds.coverage,
                         {"requested_rows": 2, "returned_rows": 2})
        self.assertEqual(ds.warnings, ())

        first = ds.bars[0]
        self.assertEqual(first.start_at,
                         datetime(2026, 8, 25, 9, 0, tzinfo=KST))
        self.assertEqual(first.end_at,
                         datetime(2026, 8, 25, 15, 30, tzinfo=KST))
        self.assertEqual(first.open, Decimal("100"))
        self.assertEqual(first.volume, 1000)
        self.assertTrue(first.complete)
        self.assertIsNone(ds.bars[-1].complete)

    async def test_existing_yahoo_daily_contract_is_unchanged(self):
        mock = AsyncMock(return_value=copy.deepcopy(_YAHOO_DAILY_ROWS))
        with patch(
                "stock_mcp_server.market_data.yahoo_provider.get_history",
                mock):
            ds = await YahooBarProvider().fetch_bars(_request(
                symbol="AAPL", market="US", interval="1d",
                trading_date=None, venue="NAS", row_limit=2))

        self.assertEqual(mock.await_args.args, ("AAPL",))
        self.assertEqual(mock.await_args.kwargs["interval"], "1d")
        self.assertIs(mock.await_args.kwargs["prepost"], False)
        self.assertEqual(ds.provider, "yahoo")
        self.assertIsNone(ds.profile)
        self.assertEqual(ds.venue, "NAS")
        self.assertEqual(ds.timezone, "America/New_York")
        self.assertEqual(ds.session, "regular")
        self.assertEqual(ds.requested_interval, "1d")
        self.assertEqual(ds.source_interval, "1d")
        self.assertEqual(ds.aggregation_method, "provider_native")
        self.assertEqual(ds.adjustment_basis, "unadjusted")
        self.assertEqual(ds.source_endpoint, "yfinance_history")
        self.assertEqual(set(ds.coverage), {
            "requested_rows", "returned_rows", "source_rows", "period"})
        self.assertEqual(ds.coverage["returned_rows"], 2)

        first = ds.bars[0]
        self.assertEqual(first.start_at,
                         datetime(2026, 8, 25, 9, 30, tzinfo=NY))
        self.assertEqual(first.end_at,
                         datetime(2026, 8, 25, 16, 0, tzinfo=NY))
        self.assertIsNone(ds.bars[-1].complete)


class MetaContractCharacterization(unittest.TestCase):
    def test_kis_meta_fields_are_unchanged(self):
        # meta 확장 필드 집합과 상태 어휘를 그대로 고정한다. 필드를 더하는
        # 변경은 허용되지만 이름 변경·삭제·의미 변경은 계약 파괴다.
        self.assertEqual(PROVIDER_EXTENSION_FIELDS, (
            "provider", "provider_status", "provider_profile",
            "requested_source", "selection_reason", "fallback_used",
            "fallback_from", "venue", "timezone", "requested_interval",
            "source_interval", "aggregation_method", "adjustment_basis",
            "data_as_of_timestamp",
        ))
        self.assertEqual(PROVIDER_STATUSES, (
            "ok", "not_configured", "credential_invalid",
            "authentication_failed", "permission_denied", "rate_limited",
            "provider_unavailable", "source_parse_error", "entity_not_found",
            "no_session", "partial",
        ))

        ext = provider_extension(
            provider="kis",
            provider_status="ok",
            provider_profile="real",
            requested_source="auto",
            selection_reason="broker_connected_and_intraday_supported",
            venue="KRX",
            timezone="Asia/Seoul",
            requested_interval="5m",
            source_interval="1m",
            aggregation_method="session_anchored_resample",
            adjustment_basis="unadjusted",
            data_as_of_timestamp="2026-08-27T15:30:00+09:00",
        )
        self.assertEqual(ext, {
            "provider": "kis",
            "provider_status": "ok",
            "provider_profile": "real",
            "requested_source": "auto",
            "selection_reason": "broker_connected_and_intraday_supported",
            "fallback_used": False,
            "fallback_from": None,
            "venue": "KRX",
            "timezone": "Asia/Seoul",
            "requested_interval": "5m",
            "source_interval": "1m",
            "aggregation_method": "session_anchored_resample",
            "adjustment_basis": "unadjusted",
            "data_as_of_timestamp": "2026-08-27T15:30:00+09:00",
        })


if __name__ == "__main__":
    unittest.main()
