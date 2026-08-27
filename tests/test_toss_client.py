"""토스증권 클라이언트 테스트 (1.0 Task 15). 네트워크 없이 MockTransport.

공식 스펙:
- POST /oauth2/token, application/x-www-form-urlencoded,
  {grant_type=client_credentials, client_id, client_secret}
- 200 {access_token, token_type: Bearer, expires_in(초)}
- 401 {error: invalid_client} = 키 오류
- 403 {error: access_denied, error_description: IP address not allowed}
  = 허용 IP 밖 (공식 확인). WTS 설정에서 등록해야 한다.
- 429 는 X-RateLimit-* / Retry-After 헤더와 함께 온다.
- 계좌·주문 API 는 X-Tossinvest-Account 헤더가 필요하다. 이 클라이언트는
  그 헤더를 절대 만들지 않고, 주문 경로는 허용 목록에 없다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_http import EndpointNotAllowedError
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.toss_client import (
    TossApiError,
    TossClient,
)

SENTINEL_ID = "c_PSA-TOSS-CLIENT-ID-555"
SENTINEL_SECRET = "s_PSA-TOSS-CLIENT-SECRET-555"

_FIXTURES = Path(__file__).parent / "fixtures" / "toss"
_TOKEN_SUCCESS = json.loads(
    (_FIXTURES / "token_success.json").read_text("utf-8"))
_TOKEN_FAILURE = json.loads(
    (_FIXTURES / "token_failure.json").read_text("utf-8"))
SENTINEL_TOKEN = _TOKEN_SUCCESS["access_token"]

_IP_DENIED = {"error": "access_denied",
              "error_description": "IP address not allowed"}

_CANDLES_OK = {"result": {"candles": [], "nextBefore": None}}


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("toss").credential_schema,
        {"client_id": SENTINEL_ID, "client_secret": SENTINEL_SECRET})


class Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.api_calls = 0
        self.token_response = None
        self.api_responses: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            self.token_calls += 1
            if self.token_response is not None:
                return self.token_response
            payload = dict(_TOKEN_SUCCESS)
            payload["access_token"] = f"{SENTINEL_TOKEN}-{self.token_calls}"
            return httpx.Response(200, json=payload)
        self.api_calls += 1
        if self.api_responses:
            item = self.api_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json=_CANDLES_OK)


def _client(rec: Recorder, **kwargs) -> TossClient:
    return TossClient(
        _payload(), "real",
        transport=httpx.MockTransport(rec.handler), **kwargs)


def _run(coro):
    return asyncio.run(coro)


class TokenTests(unittest.TestCase):
    def test_token_form_encoded_and_reused(self):
        rec = Recorder()
        client = _client(rec)

        async def go():
            await client.request("candles", params={"symbol": "005930"})
            await client.request("candles", params={"symbol": "000660"})

        _run(go())
        self.assertEqual(rec.token_calls, 1)
        self.assertEqual(rec.api_calls, 2)
        token_req = rec.requests[0]
        self.assertIn("application/x-www-form-urlencoded",
                      token_req.headers["content-type"])
        form = parse_qs(token_req.content.decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["client_id"], [SENTINEL_ID])
        self.assertEqual(form["client_secret"], [SENTINEL_SECRET])
        api_req = rec.requests[1]
        self.assertTrue(
            api_req.headers["authorization"].startswith("Bearer "))

    def test_expiry_refresh(self):
        rec = Recorder()
        rec.token_response = None
        now = [1000.0]
        client = _client(rec, clock=lambda: now[0])

        async def go():
            await client.request("candles", params={})
            now[0] += 86400 - 30  # 만료 30초 전 (마진 60초 안쪽)
            await client.request("candles", params={})

        _run(go())
        self.assertEqual(rec.token_calls, 2)

    def test_invalid_client_is_credential_invalid(self):
        rec = Recorder()
        rec.token_response = httpx.Response(401, json=_TOKEN_FAILURE)
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        self.assertEqual(ctx.exception.provider_status, "credential_invalid")
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(SENTINEL_ID, text)
        self.assertNotIn(SENTINEL_SECRET, text)

    def test_ip_not_allowed_classified_from_official_code(self):
        rec = Recorder()
        rec.token_response = httpx.Response(403, json=_IP_DENIED)
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        self.assertEqual(ctx.exception.provider_status, "permission_denied")
        self.assertEqual(ctx.exception.error_code, "ip_not_allowed")

    def test_plain_403_without_official_code_is_not_ip_guess(self):
        rec = Recorder()
        rec.token_response = httpx.Response(403, json={"error": "other"})
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        self.assertNotEqual(ctx.exception.error_code, "ip_not_allowed")

    def test_token_429_is_rate_limited(self):
        rec = Recorder()
        rec.token_response = httpx.Response(
            429, json={"error": "rate"}, headers={"Retry-After": "3"})
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")
        self.assertEqual(ctx.exception.retry_after, 3)


class RequestTests(unittest.TestCase):
    def test_no_account_header_ever(self):
        rec = Recorder()
        client = _client(rec)
        _run(client.request("candles", params={
            "symbol": "005930", "interval": "1m", "adjusted": "false"}))
        for req in rec.requests:
            for name in req.headers:
                self.assertNotIn("tossinvest-account", name.lower())

    def test_order_paths_are_impossible(self):
        rec = Recorder()
        client = _client(rec)
        for endpoint in ("orders", "accounts", "holdings", "buying-power"):
            with self.assertRaises(EndpointNotAllowedError):
                _run(client.request(endpoint, params={}))
        self.assertEqual(rec.requests, [])

    def test_api_401_retries_once_then_fails(self):
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(401, json={"error": {"code": "unauthorized"}}),
            httpx.Response(200, json=_CANDLES_OK),
        ]
        client = _client(rec)
        payload = _run(client.request("candles", params={}))
        self.assertIn("result", payload)
        self.assertEqual(rec.token_calls, 2)

        rec2 = Recorder()
        rec2.api_responses = [
            httpx.Response(401, json={}), httpx.Response(401, json={})]
        client2 = _client(rec2)
        with self.assertRaises(TossApiError) as ctx:
            _run(client2.request("candles", params={}))
        self.assertEqual(ctx.exception.provider_status,
                         "authentication_failed")

    def test_api_429_carries_retry_after(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(
            429, json={"error": {"code": "rate-limit-exceeded"}},
            headers={"Retry-After": "2", "X-RateLimit-Remaining": "0"})]
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")
        self.assertEqual(ctx.exception.retry_after, 2)

    def test_404_is_entity_not_found(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(404, json={
            "error": {"code": "not-found", "message": "unknown symbol"}})]
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={"symbol": "XXXX"}))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")

    def test_secret_reflection_never_leaks(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(500, json={
            "echo_id": SENTINEL_ID, "echo_secret": SENTINEL_SECRET,
            "echo_token": SENTINEL_TOKEN})]
        client = _client(rec)
        with self.assertRaises(TossApiError) as ctx:
            _run(client.request("candles", params={}))
        text = str(ctx.exception) + repr(ctx.exception) + repr(client)
        for banned in (SENTINEL_ID, SENTINEL_SECRET, SENTINEL_TOKEN):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
