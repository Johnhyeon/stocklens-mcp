"""market_data 공통 모델 계약 테스트 (Task 2).

BarRequest / NormalizedBar / BarDataset / ProviderCapabilities 가 설계 문서
(2026-08-27-broker-market-data-routing-design.md 9~11절)의 검증 규칙을 지키는지
확인한다. 비밀값(App Key 등)은 어떤 모델에도 실리지 않는다.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.models import (
    SUPPORTED_INTERVALS,
    SUPPORTED_MARKETS,
    PROVIDER_STATUSES,
    BarDataset,
    BarRequest,
    NormalizedBar,
    ProviderCapabilities,
)

KST = ZoneInfo("Asia/Seoul")


def _bar(**overrides) -> NormalizedBar:
    base = dict(
        start_at=datetime(2026, 8, 27, 9, 0, tzinfo=KST),
        end_at=datetime(2026, 8, 27, 9, 5, tzinfo=KST),
        open=Decimal("100"), high=Decimal("110"),
        low=Decimal("90"), close=Decimal("105"),
        volume=1000, interval="5m", session="regular",
        complete=True, session_tail=False,
        expected_minutes=5, actual_minutes=5,
        data_integrity="complete", source_gap_status="none",
    )
    base.update(overrides)
    return NormalizedBar(**base)


def _request(**overrides) -> BarRequest:
    base = dict(
        symbol="005930", market="KR", interval="5m",
        start=None, end=None, trading_date=date(2026, 8, 27),
        row_limit=120, venue="KRX", session="regular",
        adjustment="unadjusted", completed_only=True, source="auto",
    )
    base.update(overrides)
    return BarRequest(**base)


def _dataset(bars=None, **overrides) -> BarDataset:
    base = dict(
        bars=tuple(bars) if bars is not None else (_bar(),),
        market="KR", symbol="005930", provider="kis", profile="real",
        venue="KRX", timezone="Asia/Seoul", session="regular",
        requested_interval="5m", source_interval="1m",
        aggregation_method="stocklens_session_resample",
        adjustment_basis="unadjusted",
        source_endpoint="domestic_minute",
        coverage={}, warnings=(),
    )
    base.update(overrides)
    return BarDataset(**base)


class SupportedValueTests(unittest.TestCase):
    def test_intraday_intervals_and_minutes(self) -> None:
        self.assertEqual(SUPPORTED_INTERVALS, {
            "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
            "30m": 30, "60m": 60, "120m": 120, "240m": 240,
        })

    def test_supported_markets(self) -> None:
        self.assertEqual(SUPPORTED_MARKETS, ("KR", "US"))

    def test_provider_statuses_match_design(self) -> None:
        self.assertEqual(PROVIDER_STATUSES, (
            "ok", "not_configured", "credential_invalid",
            "authentication_failed", "permission_denied", "rate_limited",
            "provider_unavailable", "source_parse_error", "entity_not_found",
            "no_session", "partial",
        ))


class BarRequestTests(unittest.TestCase):
    def test_valid_request(self) -> None:
        req = _request()
        self.assertEqual(req.symbol, "005930")
        self.assertEqual(req.interval, "5m")

    def test_rejects_unknown_market(self) -> None:
        with self.assertRaisesRegex(ValueError, "market"):
            _request(market="JP")

    def test_rejects_unknown_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "interval"):
            _request(interval="7m")

    def test_rejects_non_positive_row_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "row_limit"):
            _request(row_limit=0)

    def test_rejects_naive_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            _request(start=datetime(2026, 8, 27, 9, 0))

    def test_rejects_empty_symbol(self) -> None:
        with self.assertRaisesRegex(ValueError, "symbol"):
            _request(symbol="")

    def test_immutable(self) -> None:
        req = _request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            req.symbol = "000660"

    def test_no_credential_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(BarRequest)}
        for banned in ("app_key", "app_secret", "token", "authorization"):
            self.assertNotIn(banned, names)
        text = repr(_request())
        for banned in ("app_key", "app_secret", "authorization"):
            self.assertNotIn(banned, text)


class NormalizedBarTests(unittest.TestCase):
    def test_valid_bar(self) -> None:
        bar = _bar()
        self.assertEqual(bar.volume, 1000)
        self.assertTrue(bar.complete)

    def test_rejects_naive_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            _bar(start_at=datetime(2026, 8, 27, 9, 0))
        with self.assertRaisesRegex(ValueError, "timezone"):
            _bar(end_at=datetime(2026, 8, 27, 9, 5))

    def test_rejects_end_not_after_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "end_at"):
            _bar(end_at=datetime(2026, 8, 27, 9, 0, tzinfo=KST))

    def test_rejects_high_below_low(self) -> None:
        with self.assertRaisesRegex(ValueError, "OHLC"):
            _bar(high=Decimal("80"), low=Decimal("90"),
                 open=Decimal("85"), close=Decimal("85"))

    def test_rejects_high_below_open_or_close(self) -> None:
        with self.assertRaisesRegex(ValueError, "OHLC"):
            _bar(high=Decimal("104"))
        with self.assertRaisesRegex(ValueError, "OHLC"):
            _bar(low=Decimal("101"))

    def test_rejects_negative_volume(self) -> None:
        with self.assertRaisesRegex(ValueError, "volume"):
            _bar(volume=-1)

    def test_complete_may_be_unknown(self) -> None:
        bar = _bar(complete=None)
        self.assertIsNone(bar.complete)

    def test_rejects_unknown_data_integrity(self) -> None:
        with self.assertRaisesRegex(ValueError, "data_integrity"):
            _bar(data_integrity="great")

    def test_rejects_unknown_gap_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_gap_status"):
            _bar(source_gap_status="maybe")

    def test_immutable(self) -> None:
        bar = _bar()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bar.close = Decimal("1")


class BarDatasetTests(unittest.TestCase):
    def test_valid_dataset(self) -> None:
        ds = _dataset()
        self.assertEqual(ds.provider, "kis")
        self.assertEqual(len(ds.bars), 1)

    def test_fewer_bars_than_row_limit_is_valid(self) -> None:
        """row_limit 는 최대치다. 반환 행이 적어도 그 자체로는 오류가 아니다."""
        ds = _dataset(bars=[_bar()])
        self.assertEqual(len(ds.bars), 1)

    def test_empty_bars_allowed(self) -> None:
        ds = _dataset(bars=[])
        self.assertEqual(ds.bars, ())

    def test_rejects_unsorted_bars(self) -> None:
        b1 = _bar()
        b2 = _bar(
            start_at=datetime(2026, 8, 27, 9, 5, tzinfo=KST),
            end_at=datetime(2026, 8, 27, 9, 10, tzinfo=KST),
        )
        with self.assertRaisesRegex(ValueError, "ascending"):
            _dataset(bars=[b2, b1])

    def test_rejects_duplicate_start_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _dataset(bars=[_bar(), _bar()])

    def test_immutable(self) -> None:
        ds = _dataset()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ds.provider = "naver"

    def test_no_credential_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(BarDataset)}
        for banned in ("app_key", "app_secret", "token", "authorization"):
            self.assertNotIn(banned, names)


class ProviderCapabilitiesTests(unittest.TestCase):
    def test_fields(self) -> None:
        caps = ProviderCapabilities(
            provider="kis", contract_version=1,
            markets=("KR", "US"), venues=("KRX", "NYS", "NAS", "AMS"),
            native_intervals=("1m",), verified_intervals=("1m",),
            sessions=("regular",), adjustment_modes=("unadjusted",),
            max_rows_per_call=120, historical_limit=None,
        )
        self.assertEqual(caps.provider, "kis")
        self.assertEqual(caps.contract_version, 1)

    def test_immutable(self) -> None:
        caps = ProviderCapabilities(
            provider="kis", contract_version=1,
            markets=("KR",), venues=("KRX",),
            native_intervals=("1m",), verified_intervals=(),
            sessions=("regular",), adjustment_modes=("unadjusted",),
            max_rows_per_call=None, historical_limit=None,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            caps.provider = "x"


class ProviderProtocolTests(unittest.TestCase):
    def test_protocol_importable_and_has_members(self) -> None:
        from stock_mcp_server.market_data.provider import MarketDataProvider
        self.assertTrue(hasattr(MarketDataProvider, "capabilities"))
        self.assertTrue(hasattr(MarketDataProvider, "fetch_bars"))


if __name__ == "__main__":
    unittest.main()
