"""온라인 진단 보강 테스트 — 국내 데이터 종류별 확인, 미국 시세 분류, 증권사 시세 확인.

네트워크를 타지 않는다. 네이버·야후 함수는 가짜로 바꾸고, 증권사는 가짜 공급자로만
확인한다(실 증권사 호출 금지). 주문·계좌 API 는 가짜 공급자에 아예 없다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import diagnostics
from stock_mcp_server.market_data.kis_client import KisApiError
from stock_mcp_server.naver import NaverParseError


def _run(coro):
    return asyncio.run(coro)


def _probe(name, result=None, exc=None, judge=diagnostics._judge_rows):
    async def factory():
        if exc is not None:
            raise exc
        return result

    return (name, factory, judge)


def _six(**overrides):
    base = {
        "현재가": _probe("현재가", {"name": "삼성전자", "price": 72300}, judge=diagnostics._judge_price),
        "일봉": _probe("일봉", [{"close": 1}]),
        "투자자 수급": _probe("투자자 수급", [{"foreign": 1}]),
        "시가총액 순위": _probe("시가총액 순위", [{"code": "005930"}]),
        "테마 목록": _probe("테마 목록", [{"name": "반도체"}]),
        "재무": _probe("재무", {"name": "삼성전자", "_periods": {}}, judge=diagnostics._judge_financials),
    }
    base.update(overrides)
    return tuple(base.values())


# ---------- KR_DATA_REACHABLE ----------


def test_kr_all_ok():
    check = _run(diagnostics._check_kr_data_reachable(_six()))
    assert check.status == "ok"
    assert check.summary == "국내 데이터 6가지가 모두 정상이에요."
    assert check.detail[0] == "현재가: 정상 (삼성전자 72,300원)"
    assert check.fix is None


def test_kr_one_kind_fails_is_warn_with_customer_readable_line():
    probes = _six(**{"투자자 수급": _probe("투자자 수급", exc=httpx.ReadTimeout("timed out"))})
    check = _run(diagnostics._check_kr_data_reachable(probes))
    assert check.status == "warn"
    assert check.summary == "국내 데이터 6가지 중 5가지는 정상이고, 투자자 수급 조회가 실패했어요."
    assert check.error_code == "KR_DATA_UNREACHABLE"
    assert "연결이 느려서" in check.fix and "[진단]" in check.fix
    line = next(x for x in check.detail if x.startswith("투자자 수급"))
    assert line.startswith("투자자 수급: 실패 (연결 시간 초과")


def test_kr_price_failure_is_fail():
    probes = _six(**{"현재가": _probe("현재가", exc=NaverParseError("구조 변경"), judge=diagnostics._judge_price)})
    check = _run(diagnostics._check_kr_data_reachable(probes))
    assert check.status == "fail"
    assert "[업데이트]" in check.fix  # schema


def test_kr_all_fail_is_fail():
    err = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    probes = tuple(_probe(name, exc=err, judge=judge) for name, _f, judge in _six())
    check = _run(diagnostics._check_kr_data_reachable(probes))
    assert check.status == "fail"
    assert check.summary == "국내 데이터 6가지를 모두 가져오지 못했어요."
    assert "보안 프로그램" in check.fix


def test_kr_empty_answer_is_schema_failure():
    probes = _six(**{"시가총액 순위": _probe("시가총액 순위", [])})
    check = _run(diagnostics._check_kr_data_reachable(probes))
    assert check.status == "warn"
    assert "시가총액 순위: 실패 (응답 모양이 달라짐, 빈 응답)" in check.detail


def test_kr_detail_hides_urls_and_keeps_raw_short():
    long_url = "https://m.stock.naver.com/api/stock/005930/integration?secret=" + "x" * 50
    probes = _six(**{"재무": _probe("재무", exc=RuntimeError(f"failed {long_url} " + "y" * 200))})
    check = _run(diagnostics._check_kr_data_reachable(probes))
    line = next(x for x in check.detail if x.startswith("재무"))
    assert "http" not in line and "?" not in line and "secret" not in line
    raw = line.split("(", 1)[1]
    assert len(raw) <= 120  # 분류 이름 + 원문 80자


def test_kr_default_probes_call_the_right_naver_functions():
    """장 시작 전에도 비지 않는 시가총액 순위를 쓴다(등락률·거래량 순위 아님)."""
    mocks = {
        "get_current_price": AsyncMock(return_value={"name": "삼성전자", "price": 1}),
        "get_ohlcv": AsyncMock(return_value=[{}]),
        "get_investor_flow": AsyncMock(return_value=[{}]),
        "get_market_cap_ranking": AsyncMock(return_value=[{}]),
        "list_themes": AsyncMock(return_value=[{}]),
        "get_financials": AsyncMock(return_value={"name": "삼성전자"}),
        "get_change_ranking": AsyncMock(side_effect=AssertionError("장 전엔 빈 순위")),
        "get_volume_ranking": AsyncMock(side_effect=AssertionError("장 전엔 빈 순위")),
    }
    with patch.multiple("stock_mcp_server.naver", **mocks):
        check = _run(diagnostics._check_kr_data_reachable())
    assert check.status == "ok", check.detail
    mocks["get_current_price"].assert_awaited_once_with("005930")
    mocks["get_investor_flow"].assert_awaited_once()
    mocks["get_market_cap_ranking"].assert_awaited_once()
    mocks["list_themes"].assert_awaited_once_with(1)
    mocks["get_financials"].assert_awaited_once_with("005930")


def test_kr_probes_run_concurrently():
    async def slow():
        await asyncio.sleep(0.3)
        return [{}]

    probes = tuple((f"종류{i}", slow, diagnostics._judge_rows) for i in range(6))
    t0 = time.monotonic()
    check = _run(diagnostics._check_kr_data_reachable(probes))
    assert check.status == "ok"
    assert time.monotonic() - t0 < 1.2  # 하나씩이면 1.8초


# ---------- US_DATA_REACHABLE ----------


def test_us_timeout_is_classified():
    with patch("stock_mcp_server.yfinance_source.get_price", AsyncMock(side_effect=httpx.ConnectTimeout("x"))):
        check = _run(diagnostics._check_us_data_reachable())
    assert check.status == "fail"
    assert check.summary == "미국 시세를 가져오지 못했어요."
    assert check.error_code == "US_DATA_UNREACHABLE"
    assert "연결이 느려서" in check.fix
    assert check.detail[0].startswith("미국 시세(AAPL): 실패 (연결 시간 초과")


def test_us_none_and_missing_fields():
    with patch("stock_mcp_server.yfinance_source.get_price", AsyncMock(return_value=None)):
        assert _run(diagnostics._check_us_data_reachable()).status == "fail"
    with patch("stock_mcp_server.yfinance_source.get_price", AsyncMock(return_value={"name": "Apple", "price": None})):
        check = _run(diagnostics._check_us_data_reachable())
    assert check.status == "fail"
    assert "[업데이트]" in check.fix


def test_us_ok():
    with patch("stock_mcp_server.yfinance_source.get_price", AsyncMock(return_value={"name": "Apple Inc.", "price": 210.5})):
        check = _run(diagnostics._check_us_data_reachable())
    assert check.status == "ok"
    assert check.detail == ["미국 시세: 정상 (Apple Inc. 210.5달러)"]


def test_exception_chain_classification_sees_wrapped_cause():
    """증권사 클라이언트는 httpx 오류를 provider_unavailable 로 바꿔 from None 으로 올린다."""
    try:
        try:
            raise httpx.ConnectTimeout("connect timed out")
        except httpx.TimeoutException:
            raise KisApiError("provider_unavailable") from None
    except KisApiError as wrapped:
        assert diagnostics._classify_exception(wrapped) == "timeout"
    assert diagnostics._classify_exception(KisApiError("credential_invalid", 401)) == "auth"


# ---------- BROKER_DATA_REACHABLE ----------

SENTINEL = "PSSENTINELAPPKEY0123456789abcdefghijklmnopqrstu"


class FakeAdapter:
    """분봉 조회만 있다. 주문·계좌 메서드가 없으므로 부르면 AttributeError 로 드러난다."""

    def __init__(self, provider, exc=None, bars=1):
        self.provider_id = provider
        self.exc = exc
        self.bars = bars
        self.requests = []

    async def fetch_bars(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(bars=[object()] * self.bars)


class FakeSnapshot:
    def __init__(self, caps):
        self._caps = caps

    def capabilities(self, provider):
        return self._caps.get(provider, {"connected": False})


class FakeRuntime:
    def __init__(self, caps, adapters):
        self._snapshot = FakeSnapshot(caps)
        self.adapters = adapters
        self.calls = []

    def snapshot(self):
        return self._snapshot

    def providers_for(self, market, source="auto", snapshot=None):
        self.calls.append((market, source))
        providers = {"naver": object(), "yahoo": object()}
        adapter = self.adapters.get((source, market))
        if adapter is not None:
            providers[source] = adapter
        return providers


_KR_ONLY = {"connected": True, "kr_intraday": True, "us_intraday": False}
_NOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)  # 목요일 11:00 KST, 장중


def test_broker_no_connection_returns_none():
    runtime = FakeRuntime({}, {})
    assert _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW)) is None


def test_broker_connected_but_nothing_verified_returns_none():
    runtime = FakeRuntime({"kis": {"connected": True, "kr_intraday": False, "us_intraday": False}}, {})
    assert _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW)) is None


def test_broker_ok_uses_read_only_bar_request():
    adapter = FakeAdapter("kis")
    runtime = FakeRuntime({"kis": _KR_ONLY}, {("kis", "KR"): adapter})
    check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))
    assert check.id == "BROKER_DATA_REACHABLE"
    assert check.status == "ok"
    assert check.critical is False
    assert check.summary == "연결한 증권사 1곳의 시세 조회가 모두 정상이에요."
    request = adapter.requests[0]
    assert (request.symbol, request.market, request.interval, request.row_limit) == ("005930", "KR", "1m", 1)
    assert request.trading_date == date(2026, 9, 17)
    assert request.source == "kis"
    assert runtime.calls == [("KR", "kis")]


def test_broker_partial_auth_failure_is_warn_with_broker_button():
    runtime = FakeRuntime(
        {"kis": _KR_ONLY, "kiwoom": _KR_ONLY},
        {
            ("kis", "KR"): FakeAdapter("kis", exc=KisApiError("credential_invalid", 401)),
            ("kiwoom", "KR"): FakeAdapter("kiwoom"),
        },
    )
    check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))
    assert check.status == "warn"
    assert check.summary == "연결한 증권사 2곳 중 한국투자증권 국내 시세가 실패했어요."
    assert check.fix == "StockLens 카드의 [증권사 연결]에서 연결을 다시 확인해 주세요."
    assert check.error_code == "BROKER_DATA_UNREACHABLE"


def test_broker_all_fail_is_fail_but_not_critical():
    runtime = FakeRuntime(
        {"kis": {"connected": True, "kr_intraday": True, "us_intraday": True}},
        {
            ("kis", "KR"): FakeAdapter("kis", exc=httpx.ConnectError("Connection refused")),
            ("kis", "US"): FakeAdapter("kis", exc=httpx.ConnectError("Connection refused")),
        },
    )
    check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))
    assert check.status == "fail"
    assert check.critical is False
    assert check.summary == "연결한 증권사 1곳의 시세를 모두 가져오지 못했어요."
    assert "데이터 서버에 연결하지 못했어요" in check.fix
    us_request = runtime.adapters[("kis", "US")].requests[0]
    assert (us_request.symbol, us_request.venue) == ("AAPL", "NAS")
    assert us_request.trading_date == date(2026, 9, 16)  # 뉴욕 시각으로는 아직 16일 밤


def test_broker_missing_stored_key_is_auth():
    runtime = FakeRuntime({"kiwoom": _KR_ONLY}, {})
    check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))
    assert check.status == "fail"
    assert "[증권사 연결]" in check.fix


def test_broker_experimental_provider_is_hidden(monkeypatch):
    monkeypatch.delenv("LEETKIT_ENABLE_EXPERIMENTAL_BROKERS", raising=False)
    runtime = FakeRuntime({"toss": _KR_ONLY}, {("toss", "KR"): FakeAdapter("toss")})
    assert _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW)) is None


def test_broker_details_never_carry_secrets():
    inner = RuntimeError(f"appkey={SENTINEL} token=Bearer {SENTINEL}")

    async def leak():
        try:
            raise inner
        except RuntimeError:
            raise RuntimeError(f"wrapped {SENTINEL}")

    class LeakyAdapter(FakeAdapter):
        async def fetch_bars(self, request):
            await leak()

    runtime = FakeRuntime({"kis": _KR_ONLY}, {("kis", "KR"): LeakyAdapter("kis")})
    check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))
    blob = json.dumps(check.to_dict(), ensure_ascii=False)
    assert SENTINEL not in blob
    assert "appkey" not in blob.lower() and "bearer" not in blob.lower()


def test_trading_day_before_open_uses_previous_day():
    before_open = datetime(2026, 9, 16, 23, 30, tzinfo=timezone.utc)  # 17일 08:30 KST
    assert diagnostics._latest_opened_trading_day("KR", before_open) == date(2026, 9, 16)
    saturday = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    assert diagnostics._latest_opened_trading_day("KR", saturday) == date(2026, 9, 18)


class RealRuntimeBrokerTests(unittest.TestCase):
    """실제 ProviderRuntime(가짜 keyring) 경로로 조립해도 비밀이 새지 않는다. 증권사 호출은 가짜 어댑터."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"STOCKLENS_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_real_runtime_with_fake_adapter(self):
        from stock_mcp_server.market_data.credential_store import CredentialStore
        from stock_mcp_server.market_data.provider_registry import registry
        from stock_mcp_server.market_data.runtime import ProviderRuntime
        from stock_mcp_server.market_data.secrets import SecretPayload

        class FakeKeyring:
            def __init__(self):
                self.entries = {}

            def set_password(self, service, username, password):
                self.entries[(service, username)] = password

            def get_password(self, service, username):
                return self.entries.get((service, username))

            def delete_password(self, service, username):
                self.entries.pop((service, username), None)

        keyring = FakeKeyring()
        store = CredentialStore(keyring_module=keyring, home=self.home)
        names = [f.name for f in registry.require("kis").credential_schema]
        payload = SecretPayload.from_schema(
            registry.require("kis").credential_schema, {names[0]: SENTINEL, names[1]: SENTINEL + "S"}
        )
        store.commit(store.stage("kis", "real", payload), {"auth": "ok", "kr_intraday": "available"})
        runtime = ProviderRuntime(keyring_module=keyring, home=self.home)

        built = []

        def fake_build_adapter(provider, client, profile, market):
            built.append((provider, profile, market))
            return FakeAdapter(provider, exc=KisApiError("authentication_failed", 401))

        with patch.object(runtime, "_build_adapter", side_effect=fake_build_adapter):
            check = _run(diagnostics._check_broker_data_reachable(runtime, now=_NOW))

        self.assertEqual(built, [("kis", "real", "KR")])
        self.assertEqual(check.status, "fail")
        self.assertIn("[증권사 연결]", check.fix)
        blob = json.dumps(check.to_dict(), ensure_ascii=False)
        self.assertNotIn(SENTINEL, blob)


# ---------- run_diagnostics 조립 ----------


def _patch_online(broker_result):
    return patch.multiple(
        diagnostics,
        _check_kr_data_reachable=AsyncMock(return_value=diagnostics.DiagnosticCheck(
            id="KR_DATA_REACHABLE", status="ok", critical=False, summary="ok")),
        _check_us_data_reachable=AsyncMock(return_value=diagnostics.DiagnosticCheck(
            id="US_DATA_REACHABLE", status="ok", critical=False, summary="ok")),
        _check_update_check_reachable=AsyncMock(return_value=(diagnostics.DiagnosticCheck(
            id="UPDATE_CHECK_REACHABLE", status="ok", critical=False, summary="ok"), None)),
        _check_broker_data_reachable=AsyncMock(return_value=broker_result)
        if not isinstance(broker_result, Exception) else AsyncMock(side_effect=broker_result),
    )


def test_online_report_without_broker_has_no_broker_check():
    patcher = _patch_online(None)
    with patcher:
        report = diagnostics.run_diagnostics(online=True)
    assert "BROKER_DATA_REACHABLE" not in {c.id for c in report.checks}


def test_online_report_appends_broker_check_last():
    broker = diagnostics.DiagnosticCheck(id="BROKER_DATA_REACHABLE", status="warn", critical=False, summary="s")
    patcher = _patch_online(broker)
    with patcher:
        report = diagnostics.run_diagnostics(online=True)
    assert report.checks[-1].id == "BROKER_DATA_REACHABLE"


def test_online_report_survives_broker_crash():
    patcher = _patch_online(RuntimeError("keychain exploded"))
    with patcher:
        report = diagnostics.run_diagnostics(online=True)
    crashed = next(c for c in report.checks if c.id == "BROKER_DATA_REACHABLE")
    assert crashed.status == "fail" and crashed.critical is False
