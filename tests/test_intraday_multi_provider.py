"""분봉 도구의 멀티 증권사 배선 테스트 (1.0 Task 18).

- source=kiwoom / source=toss 허용, auto 는 primary 하나
- meta 에 실제 provider 와 primary_provider 가 함께 실린다
- 공급자별 캐시 키 분리 (교차 재사용 금지)
- 명시 수동 대안 안내만 있고 자동 전환은 없다
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server
from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE_V2,
    connect_profile_v2,
)
from stock_mcp_server.market_data.models import (
    BarDataset,
    NormalizedBar,
)

KST = ZoneInfo("Asia/Seoul")


def _minute_bars(start, count):
    bars = []
    for i in range(count):
        s = start.replace(minute=start.minute) + \
            __import__("datetime").timedelta(minutes=i)
        bars.append(NormalizedBar(
            start_at=s, end_at=s + __import__("datetime").timedelta(
                minutes=1),
            open=Decimal(100), high=Decimal(101), low=Decimal(99),
            close=Decimal(100), volume=10, interval="1m",
            session="regular", complete=True, session_tail=False,
            expected_minutes=1, actual_minutes=1,
            data_integrity="complete", source_gap_status="none"))
    return bars


def _dataset(provider, bars, market="KR"):
    return BarDataset(
        bars=tuple(bars), market=market,
        symbol="005930" if market == "KR" else "AAPL",
        provider=provider, profile="real",
        venue="KRX" if market == "KR" else "NAS",
        timezone="Asia/Seoul" if market == "KR" else "America/New_York",
        session="regular", requested_interval="1m", source_interval="1m",
        aggregation_method="provider_native",
        adjustment_basis="unadjusted",
        source_endpoint=f"{provider}_test",
        coverage={"complete": True}, warnings=())


class Fake:
    def __init__(self, provider_id, result):
        self.provider_id = provider_id
        self._result = result
        self.calls = 0

    async def fetch_bars(self, request):
        self.calls += 1
        return self._result


def _v2_state(*providers, primary=None):
    state = json.loads(json.dumps(DEFAULT_STATE_V2))
    for provider in providers:
        state = connect_profile_v2(
            state, provider, "real",
            credential_ref=f"ref-{provider}", verified=True,
            verified_at="2026-08-27T18:00:00+09:00",
            capabilities={"auth": "ok", "kr_intraday": "available",
                          "us_intraday": "available"})
    if primary:
        state["primary_provider"] = primary
    state["data_source_mode"] = "auto"
    return state


def _compat_state(v2):
    primary = v2["primary_provider"]
    record = v2["providers"].get(primary) if primary else None
    return {
        "data_source_mode": v2["data_source_mode"],
        "active_provider": primary,
        "active_profile": (record or {}).get("active_profile"),
        "connection_generation": v2["routing_generation"],
        "state_v2": v2,
    }


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class SourceValidationTests(unittest.TestCase):
    def test_kiwoom_and_toss_sources_accepted(self):
        for source in ("kiwoom", "toss", "kis", "auto", "naver", "yahoo"):
            err, _ = server._validate_intraday_args(
                "KR", "5m", source, "2026-08-27")
            self.assertIsNone(err, source)

    def test_unknown_source_rejected_with_full_list(self):
        err, _ = server._validate_intraday_args(
            "KR", "5m", "binance", "2026-08-27")
        self.assertIsNotNone(err)
        for name in ("kis", "kiwoom", "toss"):
            self.assertIn(name, err)


class MultiProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    """라우팅 배선을 검사한다. 출시 검증 게이트는 별도 테스트가 지키므로
    여기서는 열어 둔다 (test_release_verification 참조)."""

    def setUp(self):
        from stock_mcp_server.market_data import provider_registry
        self._gate = patch.dict(provider_registry._RELEASE_VERIFIED, {
            (pid, cap): True
            for pid in ("kis", "kiwoom", "toss")
            for cap in ("kr_intraday", "us_intraday")
        })
        self._gate.start()
        self.addCleanup(self._gate.stop)

    async def _fetch(self, state_v2, providers, source="auto",
                     market="KR"):
        with patch.object(server, "_broker_state",
                          return_value=_compat_state(state_v2)), \
             patch.object(server, "_intraday_providers",
                          return_value=providers):
            return await server._fetch_intraday_dataset(
                symbol="005930" if market == "KR" else "AAPL",
                market=market, interval="5m",
                trading_date=date(2026, 8, 27), row_limit=10,
                venue=None if market == "KR" else "NAS",
                session="regular", completed_only=False, source=source,
                now=datetime(2026, 8, 27, 16, 0, tzinfo=KST))

    async def test_auto_uses_primary_kiwoom_with_meta(self):
        state = _v2_state("kiwoom")
        bars = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 10)
        kiwoom = Fake("kiwoom", _dataset("kiwoom", bars))
        ds, meta = await self._fetch(state, {"kiwoom": kiwoom})
        self.assertEqual(ds.provider, "kiwoom")
        self.assertEqual(meta["selected_provider"], "kiwoom")
        self.assertEqual(meta["primary_provider"], "kiwoom")
        extra = server._intraday_meta_extra(ds, meta)
        self.assertEqual(extra["provider"], "kiwoom")
        self.assertEqual(extra["primary_provider"], "kiwoom")

    async def test_explicit_toss_when_primary_is_kis(self):
        state = _v2_state("kis", "toss", primary="kis")
        bars = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 10)
        toss = Fake("toss", _dataset("toss", bars))
        kis = Fake("kis", _dataset("kis", bars))
        ds, meta = await self._fetch(
            state, {"toss": toss, "kis": kis}, source="toss")
        self.assertEqual(ds.provider, "toss")
        self.assertEqual(meta["primary_provider"], "kis")
        self.assertEqual(kis.calls, 0)

    async def test_additional_connection_keeps_primary_routing(self):
        state = _v2_state("kis", "kiwoom", "toss", primary="kis")
        bars = _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 10)
        kis = Fake("kis", _dataset("kis", bars))
        kiwoom = Fake("kiwoom", _dataset("kiwoom", bars))
        ds, meta = await self._fetch(state, {"kis": kis, "kiwoom": kiwoom})
        self.assertEqual(ds.provider, "kis")
        self.assertEqual(kiwoom.calls, 0)


class ProviderCacheKeyTests(unittest.TestCase):
    def test_cache_keys_are_provider_scoped(self):
        kis_key = server._broker_day_cache_key(
            "kis", "real", "KR", "005930", "KRX", "regular",
            date(2026, 8, 27))
        kiwoom_key = server._broker_day_cache_key(
            "kiwoom", "real", "KR", "005930", "KRX", "regular",
            date(2026, 8, 27))
        self.assertEqual(kis_key["provider"], "kis")
        self.assertEqual(kiwoom_key["provider"], "kiwoom")
        self.assertNotEqual(kis_key, kiwoom_key)

    def test_no_cross_provider_cache_reuse(self):
        from stock_mcp_server.market_data.provider_cache import ProviderCache
        with tempfile.TemporaryDirectory() as home:
            cache = ProviderCache(home=Path(home))
            kiwoom_key = server._broker_day_cache_key(
                "kiwoom", "real", "KR", "005930", "KRX", "regular",
                date(2026, 8, 27))
            cache.put(kiwoom_key, {"bars": []}, complete=True)
            kis_key = server._broker_day_cache_key(
                "kis", "real", "KR", "005930", "KRX", "regular",
                date(2026, 8, 27))
            self.assertIsNone(cache.get(kis_key, connected=True))
            self.assertIsNotNone(cache.get(kiwoom_key, connected=True))


class ToolDocContractTests(unittest.TestCase):
    def test_ai_facing_docs_mention_all_brokers(self):
        # AI 는 도구 설명으로 능력을 파악한다. 키움·토스가 설명에 없으면
        # 존재를 모른다 (리뷰 보완 항목).
        doc = server.get_intraday_chart.__doc__ or ""
        for name in ("키움", "토스", "kiwoom", "toss"):
            self.assertIn(name, doc, name)
        self.assertNotIn("auto|kis|naver|yahoo.", doc)

    def test_unsupported_error_says_not_a_key_problem(self):
        text = server._intraday_error_result(
            "005930", "KR", "증권사 데이터 조회 실패: unsupported",
            "unsupported")
        self.assertIn("키 문제가 아닙니다", text)


class BrokerErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_kiwoom_and_toss_errors_are_reported_like_kis(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError
        from stock_mcp_server.market_data.toss_client import TossApiError

        for exc in (KiwoomApiError("rate_limited", 429),
                    TossApiError("provider_unavailable", 500)):
            async def _raise(**kwargs):
                raise exc

            with patch.object(server, "_fetch_intraday_dataset", _raise):
                result = await server.get_intraday_chart(
                    symbol="005930", market="KR", interval="5m",
                    date="2026-08-27")
            self.assertIn("증권사 데이터 조회 실패", result)
            self.assertIn(exc.provider_status, result)


if __name__ == "__main__":
    unittest.main()
