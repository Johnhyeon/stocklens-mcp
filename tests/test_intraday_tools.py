"""신규 분봉 도구(get_intraday_chart) 테스트 (Task 12)."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import server
from stock_mcp_server.market_data.kis_client import KisApiError
from stock_mcp_server.market_data.models import BarDataset, NormalizedBar
from stock_mcp_server.market_data.router import RouterError

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

_OPEN_CLOCK = {
    "krx": {"is_open": False, "status": "closed_after_hours",
            "last_trading_day": "2026-08-27"},
    "us": {"is_open": False, "status": "closed_after_hours",
           "last_trading_day": "2026-08-26"},
}


def _minute_bars(open_at: datetime, count: int, minutes: int = 1,
                 interval: str = "1m") -> list[NormalizedBar]:
    bars = []
    for i in range(count):
        start = open_at + timedelta(minutes=i * minutes)
        o = 1000 + i
        bars.append(NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=minutes),
            open=Decimal(o), high=Decimal(o + 2), low=Decimal(o - 2),
            close=Decimal(o + 1), volume=100 + i,
            interval=interval, session="regular", complete=True,
            session_tail=False, expected_minutes=minutes,
            actual_minutes=minutes, data_integrity="complete",
            source_gap_status="none",
        ))
    return bars


def _dataset(bars, market="KR", provider="kis", interval="5m",
             warnings=()) -> BarDataset:
    return BarDataset(
        bars=tuple(bars), market=market, symbol="005930" if market == "KR"
        else "AAPL", provider=provider,
        profile="real" if provider == "kis" else None,
        venue="KRX" if market == "KR" else "NAS",
        timezone="Asia/Seoul" if market == "KR" else "America/New_York",
        session="regular", requested_interval=interval,
        source_interval="1m",
        aggregation_method="stocklens_session_resample",
        adjustment_basis="unadjusted", source_endpoint="test",
        coverage={"complete": True}, warnings=tuple(warnings),
    )


def _route_meta(provider="kis", fallback=False):
    return {
        "requested_source": "auto",
        "selected_provider": provider,
        "selection_reason": "broker_connected_and_intraday_supported",
        "mode": "auto",
        "fallback_used": fallback,
        "fallback_from": "kis" if fallback else None,
    }


def _extract_meta(text: str) -> dict:
    payload = text.split("RESULT_META_JSON_START", 1)[1].split(
        "RESULT_META_JSON_END", 1)[0].strip()
    return json.loads(payload)


class IntradayChartTests(unittest.IsolatedAsyncioTestCase):
    async def test_kr_chart_output_and_meta(self):
        bars = _minute_bars(
            datetime(2026, 8, 27, 9, 0, tzinfo=KST), 5, minutes=5,
            interval="5m")
        fetch = AsyncMock(return_value=(_dataset(bars), _route_meta()))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="005930", market="KR", interval="5m")

        self.assertIn("005930", text)
        self.assertIn("5 bars", text)
        self.assertIn("날짜|시가|고가|저가|종가|거래량", text)
        meta = _extract_meta(text)
        self.assertEqual(meta["provider"], "kis")
        self.assertEqual(meta["provider_profile"], "real")
        self.assertEqual(meta["requested_source"], "auto")
        self.assertFalse(meta["fallback_used"])
        self.assertEqual(meta["requested_interval"], "5m")
        self.assertEqual(meta["source_interval"], "1m")
        self.assertEqual(meta["aggregation_method"],
                         "stocklens_session_resample")
        self.assertEqual(meta["timezone"], "Asia/Seoul")
        self.assertEqual(meta["data_as_of"], "2026-08-27")
        self.assertTrue(meta["data_as_of_timestamp"].startswith("2026-08-27T"))

    async def test_us_chart_with_fallback_metadata(self):
        bars = _minute_bars(
            datetime(2026, 8, 26, 9, 30, tzinfo=NY), 3, minutes=5,
            interval="5m")
        fetch = AsyncMock(return_value=(
            _dataset(bars, market="US", provider="yahoo"),
            _route_meta(provider="yahoo", fallback=True)))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="AAPL", market="US", interval="5m")
        meta = _extract_meta(text)
        self.assertEqual(meta["provider"], "yahoo")
        self.assertTrue(meta["fallback_used"])
        self.assertEqual(meta["fallback_from"], "kis")
        self.assertTrue(any("kis" in w.lower() for w in meta["warnings"]))

    async def test_invalid_inputs(self):
        with patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            self.assertIn("market", await server.get_intraday_chart(
                symbol="005930", market="JP"))
            self.assertIn("interval", await server.get_intraday_chart(
                symbol="005930", interval="7m"))
            self.assertIn("source", await server.get_intraday_chart(
                symbol="005930", source="bloomberg"))
            self.assertIn("날짜", await server.get_intraday_chart(
                symbol="005930", date="26-08-27"))

    async def test_unverified_sessions_rejected(self):
        # 리뷰 지적(결함 2): 미검증 세션은 데이터를 섞어 반환하지 말고
        # 도구 입구에서 거부한다. 검증 후에만 연다.
        with patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            for bad in ("pre", "after", "daytime"):
                text = await server.get_intraday_chart(
                    symbol="AAPL", market="US", session=bad, venue="NAS")
                self.assertIn("session", text)
                text2 = await server.get_intraday_indicators(
                    symbol="AAPL", market="US", session=bad, venue="NAS")
                self.assertIn("session", text2)

    async def test_all_intervals_accepted(self):
        bars = _minute_bars(
            datetime(2026, 8, 27, 9, 0, tzinfo=KST), 2, minutes=1)
        for interval in ("1m", "3m", "5m", "10m", "15m", "30m",
                         "60m", "120m", "240m"):
            fetch = AsyncMock(return_value=(
                _dataset(bars, interval=interval), _route_meta()))
            with patch.object(server, "_fetch_intraday_dataset", fetch), \
                 patch.object(server, "build_market_clock",
                              return_value=_OPEN_CLOCK):
                text = await server.get_intraday_chart(
                    symbol="005930", interval=interval)
            self.assertIn("RESULT_META_JSON_START", text)

    async def test_no_provider_returns_structured_message(self):
        fetch = AsyncMock(side_effect=RouterError(
            "not_configured", "KIS가 연결되어 있지 않습니다"))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="005930", market="KR", interval="5m")
        self.assertIn("연결", text)
        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertEqual(meta["provider_status"], "not_configured")

    async def test_provider_error_reported_without_secrets(self):
        fetch = AsyncMock(side_effect=KisApiError("rate_limited", 429))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="005930", market="KR", interval="5m")
        meta = _extract_meta(text)
        self.assertEqual(meta["provider_status"], "rate_limited")

    async def test_us_kis_error_suggests_manual_alternatives(self):
        # 자동 전환이 없으므로(1.0 정책) 장애 시 대안을 안내한다.
        # 전환은 어디까지나 사용자의 직접 선택이다.
        fetch = AsyncMock(side_effect=KisApiError("rate_limited", 429))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="AAPL", market="US", interval="5m", venue="NAS")
        self.assertIn("source", text)
        self.assertIn("yahoo", text)
        self.assertIn("거래량 기준", text)

    async def test_empty_dataset_reports_none(self):
        fetch = AsyncMock(return_value=(_dataset([]), _route_meta()))
        with patch.object(server, "_fetch_intraday_dataset", fetch), \
             patch.object(server, "build_market_clock",
                          return_value=_OPEN_CLOCK):
            text = await server.get_intraday_chart(
                symbol="005930", interval="5m")
        meta = _extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")


class FetchPipelineTests(unittest.IsolatedAsyncioTestCase):
    """_fetch_intraday_dataset 이 라우터·resample·completed_only 를 잇는지."""

    def _fake_providers(self, kis_result=None, kis_error=None,
                        yahoo_result=None):
        class Fake:
            def __init__(self, pid, result, error):
                self.provider_id = pid
                self._result = result
                self._error = error
                self.calls = 0

            async def fetch_bars(self, request):
                self.calls += 1
                if self._error:
                    raise self._error
                return self._result

        providers = {}
        if kis_result is not None or kis_error is not None:
            providers["kis"] = Fake("kis", kis_result, kis_error)
        if yahoo_result is not None:
            providers["yahoo"] = Fake("yahoo", yahoo_result, None)
        return providers

    async def test_kis_1m_source_resampled_to_target(self):
        raw = _dataset(
            _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 30),
            interval="1m")
        providers = self._fake_providers(kis_result=raw)
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value=providers), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "auto", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            ds, meta = await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval="10m",
                trading_date=date(2026, 8, 27), row_limit=100,
                venue=None, session="regular", completed_only=True,
                source="auto",
                now=datetime(2026, 8, 27, 16, 0, tzinfo=KST))

        self.assertEqual(meta["selected_provider"], "kis")
        self.assertEqual(ds.requested_interval, "10m")
        self.assertEqual(len(ds.bars), 3)
        self.assertTrue(all(b.complete for b in ds.bars))

    async def test_completed_only_drops_in_progress_bucket(self):
        raw = _dataset(
            _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 12),
            interval="1m")
        providers = self._fake_providers(kis_result=raw)
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value=providers), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "auto", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            # 09:12 장중 조회: 10분 버킷 09:10~09:20 은 미완성이라 제외된다.
            ds, _ = await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval="10m",
                trading_date=date(2026, 8, 27), row_limit=100,
                venue=None, session="regular", completed_only=True,
                source="auto",
                now=datetime(2026, 8, 27, 9, 12, tzinfo=KST))
            self.assertEqual(len(ds.bars), 1)

            ds2, _ = await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval="10m",
                trading_date=date(2026, 8, 27), row_limit=100,
                venue=None, session="regular", completed_only=False,
                source="auto",
                now=datetime(2026, 8, 27, 9, 12, tzinfo=KST))
            self.assertEqual(len(ds2.bars), 2)
            self.assertFalse(ds2.bars[-1].complete)

    async def test_row_limit_keeps_recent(self):
        raw = _dataset(
            _minute_bars(datetime(2026, 8, 27, 9, 0, tzinfo=KST), 30),
            interval="1m")
        providers = self._fake_providers(kis_result=raw)
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value=providers), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "auto", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            ds, _ = await server._fetch_intraday_dataset(
                symbol="005930", market="KR", interval="5m",
                trading_date=date(2026, 8, 27), row_limit=2,
                venue=None, session="regular", completed_only=True,
                source="auto",
                now=datetime(2026, 8, 27, 16, 0, tzinfo=KST))
        self.assertEqual(len(ds.bars), 2)
        self.assertEqual(ds.bars[-1].start_at.strftime("%H%M"), "0925")

    async def test_us_kis_provider_called_once_no_multiday_loop(self):
        # 해외 endpoint 는 날짜 인자 없이 KEYB 로만 페이지네이션한다.
        # 다일 루프를 돌리면 같은 호출만 반복된다 (2026-08-27 실측).
        raw = _dataset(
            _minute_bars(datetime(2026, 8, 26, 9, 30, tzinfo=NY), 10),
            market="US", interval="1m")
        providers = self._fake_providers(kis_result=raw)
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value=providers), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "auto", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            await server._fetch_intraday_dataset(
                symbol="AAPL", market="US", interval="60m",
                trading_date=date(2026, 8, 26), row_limit=100,
                venue="NAS", session="regular", completed_only=False,
                source="auto",
                now=datetime(2026, 8, 26, 17, 0, tzinfo=NY))
        self.assertEqual(providers["kis"].calls, 1)

    async def test_legacy_mode_makes_zero_kis_calls(self):
        yahoo_ds = _dataset(
            _minute_bars(datetime(2026, 8, 26, 9, 30, tzinfo=NY), 10),
            market="US", provider="yahoo", interval="1m")
        providers = self._fake_providers(
            kis_result=_dataset([], market="US"), yahoo_result=yahoo_ds)
        caps = {"connected": True, "kr_intraday": True, "us_intraday": True,
                "kr_daily": False, "us_daily": False}
        with patch.object(server, "_intraday_providers",
                          return_value=providers), \
             patch.object(server, "_broker_capabilities", return_value=caps), \
             patch.object(server, "_broker_state", return_value={
                 "data_source_mode": "legacy", "active_profile": "real",
                 "active_provider": "kis", "connection_generation": 1}):
            ds, meta = await server._fetch_intraday_dataset(
                symbol="AAPL", market="US", interval="5m",
                trading_date=date(2026, 8, 26), row_limit=100,
                venue="NAS", session="regular", completed_only=True,
                source="auto",
                now=datetime(2026, 8, 26, 17, 0, tzinfo=NY))
        self.assertEqual(providers["kis"].calls, 0)
        self.assertEqual(meta["selected_provider"], "yahoo")


class KisClientReuseTests(unittest.TestCase):
    """도구 호출마다 토큰을 재발급하면 KIS 1분 제한에 걸린다 (실측).
    같은 (provider, profile, generation) 이면 runtime 이 클라이언트를
    재사용하고, generation 이 바뀌면 그 공급자 것만 새로 만든다."""

    class _FakeKeyring:
        def __init__(self):
            self.entries = {}

        def set_password(self, service, username, password):
            self.entries[(service, username)] = password

        def get_password(self, service, username):
            return self.entries.get((service, username))

        def delete_password(self, service, username):
            if (service, username) not in self.entries:
                raise Exception("not found")
            del self.entries[(service, username)]

    def setUp(self):
        import tempfile
        from pathlib import Path
        from stock_mcp_server.market_data.credential_store import (
            CredentialStore,
        )
        from stock_mcp_server.market_data.runtime import ProviderRuntime

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        keyring = self._FakeKeyring()
        self.store = CredentialStore(keyring_module=keyring, home=self.home)
        self.runtime = ProviderRuntime(keyring_module=keyring,
                                       home=self.home)
        self._connect()

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self):
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.secrets import SecretPayload

        payload = SecretPayload.from_schema(
            registry.require("kis").credential_schema,
            {"app_key": "k", "app_secret": "s"})
        pending = self.store.stage("kis", "real", payload)
        self.store.commit(pending, {
            "auth": "ok", "kr_intraday": "available",
            "us_intraday": "available"})

    def test_same_generation_reuses_client(self):
        with patch.object(server, "_PROVIDER_RUNTIME", self.runtime):
            p1 = server._intraday_providers("KR")
            p2 = server._intraday_providers("US")
        self.assertIs(p1["kis"]._client, p2["kis"]._client)

    def test_generation_change_builds_new_client(self):
        with patch.object(server, "_PROVIDER_RUNTIME", self.runtime):
            p1 = server._intraday_providers("KR")
            self._connect()  # 재연결 -> kis generation 증가
            p2 = server._intraday_providers("KR")
        self.assertIsNot(p1["kis"]._client, p2["kis"]._client)


if __name__ == "__main__":
    unittest.main()
