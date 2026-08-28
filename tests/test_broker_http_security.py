"""공용 증권사 HTTP transport 보안 테스트 (1.0 Task 8, 설계 11.1).

- endpoint_id 로만 요청한다. 호출자가 URL 을 만들 수 없다
- 허용 host·path 밖은 거부, https 강제, redirect 미추종
- 오류에 query·header·응답 본문(비밀 반사 포함)을 싣지 않는다
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_http import (
    BrokerHttpError,
    BrokerHttpTransport,
    EndpointNotAllowedError,
)
from stock_mcp_server.market_data.provider_registry import (
    CredentialField,
    EndpointSpec,
    ProviderDescriptor,
    registry,
)

SECRET = "PSA-HTTP-SECRET-424242"


def _run(coro):
    return asyncio.run(coro)


def _transport_with(handler):
    return BrokerHttpTransport(transport=httpx.MockTransport(handler))


class EndpointMappingTests(unittest.TestCase):
    def test_registry_descriptors_expose_endpoints(self):
        kis = registry.require("kis")
        ids = {e.endpoint_id for e in kis.endpoints}
        self.assertEqual(ids, {"token", "kr_minute", "us_minute"})
        kiwoom = registry.require("kiwoom")
        # 1.1 상세 수급·수급 압력 endpoint (2026-08-28 실측 확정 경로).
        self.assertEqual({e.endpoint_id for e in kiwoom.endpoints},
                         {"token", "kr_chart", "us_chart",
                          "kr_investor_daily", "kr_investor_market",
                          "kr_program_trade", "kr_short_selling",
                          "kr_credit_trade", "kr_securities_lending",
                          "kr_foreign_holding"})
        toss = registry.require("toss")
        self.assertEqual({e.endpoint_id for e in toss.endpoints},
                         {"token", "candles"})
        # 모든 endpoint path 는 allowed_paths 안에 있다.
        for provider_id in registry.ids():
            desc = registry.require(provider_id)
            for endpoint in desc.endpoints:
                self.assertIn(endpoint.path, desc.allowed_paths)

    def test_unknown_endpoint_is_rejected(self):
        transport = _transport_with(
            lambda request: httpx.Response(200, json={}))
        with self.assertRaises(EndpointNotAllowedError):
            _run(transport.request(
                registry.require("toss"), "place_order", profile="real"))

    def test_unknown_profile_host_is_rejected(self):
        transport = _transport_with(
            lambda request: httpx.Response(200, json={}))
        with self.assertRaises(EndpointNotAllowedError):
            _run(transport.request(
                registry.require("toss"), "candles", profile="demo"))


class TransportSecurityTests(unittest.TestCase):
    def _descriptor(self, host="openapi.tossinvest.com",
                    path="/api/v1/candles"):
        return ProviderDescriptor(
            provider_id="toss",
            display_name="토스증권",
            allowed_hosts=("openapi.tossinvest.com",),
            allowed_paths=("/api/v1/candles",),
            credential_schema=(
                CredentialField(name="client_id", label="ID"),
                CredentialField(name="client_secret", label="Secret"),
            ),
            supported_profiles=("real",),
            signup_url="https://corp.tossinvest.com/ko/open-api",
            docs_url="https://developers.tossinvest.com/docs",
            hosts_by_profile=(("real", host),),
            endpoints=(EndpointSpec("candles", "GET", path),),
        )

    def test_host_and_path_outside_allowlist_are_rejected(self):
        transport = _transport_with(
            lambda request: httpx.Response(200, json={}))
        # descriptor 를 손으로 바꿔도 allowlist 검증이 다시 잡는다.
        bad_host = self._descriptor(host="evil.example.com")
        with self.assertRaises(EndpointNotAllowedError):
            _run(transport.request(bad_host, "candles", profile="real"))
        bad_path = self._descriptor(path="/api/v1/orders")
        with self.assertRaises(EndpointNotAllowedError):
            _run(transport.request(bad_path, "candles", profile="real"))

    def test_request_url_is_https_on_allowed_host(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["scheme"] = request.url.scheme
            seen["host"] = request.url.host
            seen["path"] = request.url.path
            return httpx.Response(200, json={"ok": True})

        transport = _transport_with(handler)
        response = _run(transport.request(
            registry.require("toss"), "candles", profile="real",
            params={"symbol": "005930"}))
        self.assertEqual(seen["scheme"], "https")
        self.assertEqual(seen["host"], "openapi.tossinvest.com")
        self.assertEqual(seen["path"], "/api/v1/candles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)

    def test_cross_host_redirect_is_not_followed(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302, headers={"location": "https://evil.example.com/steal"})

        transport = _transport_with(handler)
        with self.assertRaises(BrokerHttpError) as ctx:
            _run(transport.request(
                registry.require("toss"), "candles", profile="real"))
        self.assertEqual(len(calls), 1)  # redirect 를 따라가지 않았다
        self.assertNotIn("evil.example.com", str(ctx.exception))

    def test_query_and_headers_are_not_present_in_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = _transport_with(handler)
        with self.assertRaises(BrokerHttpError) as ctx:
            _run(transport.request(
                registry.require("toss"), "candles", profile="real",
                headers={"Authorization": f"Bearer {SECRET}"},
                params={"token": SECRET}))
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Authorization", text)

    def test_provider_echoing_secrets_is_redacted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={
                "echo_secret": SECRET, "msg": "denied"})

        transport = _transport_with(handler)
        with self.assertRaises(BrokerHttpError) as ctx:
            _run(transport.request(
                registry.require("toss"), "candles", profile="real"))
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(SECRET, text)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_response_repr_hides_headers_and_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"secret": SECRET},
                                  headers={"x-secret-echo": SECRET})

        transport = _transport_with(handler)
        response = _run(transport.request(
            registry.require("toss"), "candles", profile="real"))
        self.assertNotIn(SECRET, repr(response))
        self.assertNotIn(SECRET, str(response))
        # 비밀 아닌 페이지네이션 header 는 명시적 접근으로만 읽는다.
        self.assertIsNone(response.get_header("authorization"))
        self.assertIsNone(response.get_header("set-cookie"))

    def test_pagination_headers_are_accessible(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={}, headers={
                "cont-yn": "Y", "next-key": "abc123"})

        transport = _transport_with(handler)
        response = _run(transport.request(
            registry.require("kiwoom"), "kr_chart", profile="real",
            json_body={"stk_cd": "005930"}))
        self.assertEqual(response.get_header("cont-yn"), "Y")
        self.assertEqual(response.get_header("next-key"), "abc123")

    def test_oversized_response_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (6 * 1024 * 1024))

        transport = _transport_with(handler)
        with self.assertRaises(BrokerHttpError):
            _run(transport.request(
                registry.require("toss"), "candles", profile="real"))

    def test_network_errors_are_sanitized(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("getaddrinfo failed " + SECRET)

        transport = _transport_with(handler)
        with self.assertRaises(BrokerHttpError) as ctx:
            _run(transport.request(
                registry.require("toss"), "candles", profile="real"))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")
        self.assertNotIn(SECRET, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
