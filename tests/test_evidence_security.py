"""상세 수급 경로 보안 (1.1 Task 15).

증거 조회는 **읽기 전용 시세·수급 endpoint 만** 쓴다. 계좌·잔고·주문
namespace 는 allowlist 에 없고, 있을 이유도 없다. 법적 라인(자동매매
연결 금지·계좌 정보 수집 금지)이 코드 층에서도 지켜지는지 본다.

allowlist 를 '지금 없다'로 확인하는 것으로는 부족하다. 나중에 누가
endpoint 를 추가할 때 계좌·주문 경로가 딸려 들어오는 것을 막아야 한다.
그래서 namespace 단위로 금지한다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_http import (
    BrokerHttpError,
    BrokerHttpTransport,
    EndpointNotAllowedError,
)
from stock_mcp_server.market_data.kis_client import KisClient
from stock_mcp_server.market_data.kis_evidence import KisEvidenceProvider
from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
from stock_mcp_server.market_data.kiwoom_evidence import (
    KiwoomEvidenceProvider,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

SECRET = "PSA-EVIDENCE-SECRET-909090"

# KIS 는 access_token + expires_in(초), 키움은 token + expires_dt(KST 절대
# 시각)를 준다. 하나로 합칠 수 없어 둘 다 담는다.
_TOKEN_RESPONSE = {
    "access_token": "T", "expires_in": 86400,
    "token": "T", "token_type": "Bearer", "expires_dt": "20991231235959",
}

# 종목 조회에 필요 없는 namespace. 어느 공급자에도 열려 있으면 안 된다.
FORBIDDEN_FRAGMENTS = (
    "account", "balance", "holding", "order", "trading/order",
    "inquire-balance", "inquire-psbl", "cash", "deposit", "asset",
    "credit-order", "sell", "buy",
)


def _run(coro):
    return asyncio.run(coro)


def _payload(provider_id: str) -> SecretPayload:
    schema = registry.require(provider_id).credential_schema
    return SecretPayload.from_schema(
        schema, {f.name: SECRET for f in schema})


class AllowlistTests(unittest.TestCase):
    def test_no_provider_allows_an_account_or_order_namespace(self):
        for provider_id in registry.ids():
            descriptor = registry.require(provider_id)
            for path in descriptor.allowed_paths:
                lowered = path.lower()
                for fragment in FORBIDDEN_FRAGMENTS:
                    self.assertNotIn(
                        fragment, lowered,
                        f"{provider_id} 가 {path} 를 허용한다 "
                        f"({fragment}). 계좌·주문 경로는 열지 않는다.")

    def test_every_declared_endpoint_is_inside_the_allowlist(self):
        for provider_id in registry.ids():
            descriptor = registry.require(provider_id)
            for endpoint in descriptor.endpoints:
                self.assertIn(endpoint.path, descriptor.allowed_paths,
                              f"{provider_id}:{endpoint.endpoint_id}")

    def test_unmeasured_evidence_paths_are_not_allowed(self):
        # 데이터를 확인하지 못한 endpoint 는 올리지 않는다 (2026-08-28).
        kis = registry.require("kis")
        for path in kis.allowed_paths:
            self.assertNotIn("daily-loan-trans", path)
            self.assertNotIn("daily-credit-balance", path)

    def test_a_caller_cannot_reach_an_unregistered_endpoint(self):
        transport = BrokerHttpTransport(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={})))
        for provider_id, endpoint_id in (("kis", "kr_balance"),
                                         ("kiwoom", "kr_order"),
                                         ("toss", "place_order")):
            with self.assertRaises(EndpointNotAllowedError):
                _run(transport.request(registry.require(provider_id),
                                       endpoint_id, profile="real"))


class _Recorder:
    """모든 요청의 header·body 를 붙잡아 두는 서버 대역."""

    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/oauth2/tokenP", "/oauth2/token")):
            return httpx.Response(200, json=_TOKEN_RESPONSE)
        self.requests.append(request)
        # 응답이 비밀을 되비추는 최악의 공급자를 가정한다.
        return httpx.Response(200, json={
            "rt_cd": "0", "return_code": 0, "output": [], "output2": [],
            "echo": SECRET})

    @property
    def headers(self) -> list[dict]:
        return [dict(r.headers) for r in self.requests]


def _kis_provider(recorder):
    client = KisClient(_payload("kis"), "real",
                       transport=httpx.MockTransport(recorder.handler))
    return KisEvidenceProvider(client, "real")


def _kiwoom_provider(recorder):
    client = KiwoomClient(_payload("kiwoom"), "real",
                          transport=httpx.MockTransport(recorder.handler))
    return KiwoomEvidenceProvider(client, "real")


class RequestHygieneTests(unittest.TestCase):
    def _exercise(self, provider_id):
        recorder = _Recorder(provider_id)
        provider = (_kis_provider(recorder) if provider_id == "kis"
                    else _kiwoom_provider(recorder))
        kinds = [k for k, v in provider.pressure_capabilities().items()
                 if v == "available"]
        try:
            _run(provider.fetch_investor_flow(
                "005930", base_date=date(2026, 8, 27)))
        except Exception:  # noqa: BLE001  빈 응답은 여기서 관심사가 아니다
            pass
        try:
            _run(provider.fetch_supply_pressure(
                "005930", kinds=kinds, base_date=date(2026, 8, 27)))
        except Exception:  # noqa: BLE001
            pass
        self.assertTrue(recorder.requests, provider_id)
        return recorder

    def test_no_account_identifier_header_is_sent(self):
        for provider_id in ("kis", "kiwoom"):
            recorder = self._exercise(provider_id)
            for headers in recorder.headers:
                for name in headers:
                    lowered = name.lower()
                    self.assertNotIn("account", lowered, provider_id)
                    self.assertNotIn("cano", lowered, provider_id)
                    self.assertNotIn("acnt", lowered, provider_id)

    def test_every_request_stays_on_an_allowed_path(self):
        for provider_id in ("kis", "kiwoom"):
            recorder = self._exercise(provider_id)
            allowed = set(registry.require(provider_id).allowed_paths)
            for request in recorder.requests:
                self.assertIn(request.url.path, allowed, provider_id)

    def test_the_symbol_is_the_only_entity_identifier_sent(self):
        # 계좌번호·상품코드 자리에 무언가를 넣지 않는다.
        for provider_id in ("kis", "kiwoom"):
            recorder = self._exercise(provider_id)
            for request in recorder.requests:
                blob = (str(request.url.query) + (
                    request.content.decode("utf-8", "replace")
                    if request.content else "")).upper()
                for banned in ("CANO", "ACNT_PRDT_CD", "ACCOUNT_NO"):
                    self.assertNotIn(banned, blob, provider_id)


class SecretLeakTests(unittest.TestCase):
    def _reflecting_error(self, provider_id, status):
        recorder = _Recorder(provider_id)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(
                    ("/oauth2/tokenP", "/oauth2/token")):
                return httpx.Response(200, json=_TOKEN_RESPONSE)
            return httpx.Response(status, json={
                "msg1": f"denied {SECRET}", "return_msg": SECRET,
                "echo_secret": SECRET})

        recorder.handler = handler  # type: ignore[method-assign]
        return recorder

    def test_a_provider_reflecting_the_secret_is_redacted(self):
        for provider_id, factory in (("kis", _kis_provider),
                                     ("kiwoom", _kiwoom_provider)):
            recorder = self._reflecting_error(provider_id, 403)
            provider = factory(recorder)
            try:
                _run(provider.fetch_investor_flow(
                    "005930", base_date=date(2026, 8, 27)))
            except Exception as exc:  # noqa: BLE001
                text = str(exc) + repr(exc)
                self.assertNotIn(SECRET, text, provider_id)

    def test_a_pressure_failure_block_carries_no_secret(self):
        for provider_id, factory in (("kis", _kis_provider),
                                     ("kiwoom", _kiwoom_provider)):
            recorder = self._reflecting_error(provider_id, 500)
            provider = factory(recorder)
            kinds = [k for k, v in provider.pressure_capabilities().items()
                     if v == "available"]
            blocks = _run(provider.fetch_supply_pressure(
                "005930", kinds=kinds, base_date=date(2026, 8, 27)))
            for kind, block in blocks.items():
                blob = repr(block)
                self.assertNotIn(SECRET, blob, f"{provider_id}:{kind}")

    def test_the_credential_payload_never_reprs_itself(self):
        for provider_id in registry.ids():
            payload = _payload(provider_id)
            self.assertNotIn(SECRET, repr(payload), provider_id)
            self.assertNotIn(SECRET, str(payload), provider_id)

    def test_transport_errors_on_evidence_paths_are_sanitized(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"dns failed for {SECRET}")

        transport = BrokerHttpTransport(
            transport=httpx.MockTransport(handler))
        with self.assertRaises(BrokerHttpError) as ctx:
            _run(transport.request(registry.require("kis"),
                                   "kr_investor_daily", profile="real"))
        self.assertNotIn(SECRET, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
