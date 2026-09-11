"""키움 REST 클라이언트 테스트 (1.0 Task 12). 네트워크 없이 MockTransport.

공식 스펙(au10001): POST /oauth2/token
  body {grant_type: client_credentials, appkey, secretkey}
  200 응답 {expires_dt: YYYYMMDDHHmmss(KST), token_type, token}
차트 호출은 api-id 헤더 + Bearer 토큰. 연속조회는 cont-yn/next-key 헤더.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_http import EndpointNotAllowedError
from stock_mcp_server.market_data.kiwoom_client import (
    KiwoomApiError,
    KiwoomClient,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

KST = ZoneInfo("Asia/Seoul")

SENTINEL_KEY = "PSA-KIWOOM-APP-KEY-333"
SENTINEL_SECRET = "PSA-KIWOOM-SECRET-KEY-333"

_FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
_TOKEN_SUCCESS = json.loads(
    (_FIXTURES / "token_success.json").read_text("utf-8"))
_TOKEN_FAILURE = json.loads(
    (_FIXTURES / "token_failure.json").read_text("utf-8"))
SENTINEL_TOKEN = _TOKEN_SUCCESS["token"]


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": SENTINEL_KEY, "secret_key": SENTINEL_SECRET})


class Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.api_calls = 0
        self.token_response = None
        self.api_responses: list = []
        # 실제 시계로 도는 테스트가 시한폭탄이 되지 않게 원미래로 둔다.
        # (만료 동작 테스트는 가짜 시계와 함께 값을 직접 지정한다.)
        self.token_expires_dt = "20991231235959"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            self.token_calls += 1
            if self.token_response is not None:
                return self.token_response
            payload = dict(_TOKEN_SUCCESS)
            payload["token"] = f"{SENTINEL_TOKEN}-{self.token_calls}"
            payload["expires_dt"] = self.token_expires_dt
            return httpx.Response(200, json=payload)
        self.api_calls += 1
        if self.api_responses:
            item = self.api_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={
            "return_code": 0, "return_msg": "정상적으로 처리되었습니다",
            "stk_cd": "005930", "stk_min_pole_chart_qry": []})


def _client(rec: Recorder, profile="real", **kwargs) -> KiwoomClient:
    return KiwoomClient(
        _payload(), profile,
        transport=httpx.MockTransport(rec.handler), **kwargs)


def _run(coro):
    return asyncio.run(coro)


class TokenTests(unittest.TestCase):
    def test_token_issued_with_official_fields_and_reused(self):
        rec = Recorder()
        client = _client(rec)

        async def go():
            await client.request("kr_chart", api_id="ka10080",
                                 body={"stk_cd": "005930"})
            await client.request("kr_chart", api_id="ka10080",
                                 body={"stk_cd": "005930"})

        _run(go())
        self.assertEqual(rec.token_calls, 1)
        self.assertEqual(rec.api_calls, 2)
        token_req = json.loads(rec.requests[0].content)
        self.assertEqual(token_req, {
            "grant_type": "client_credentials",
            "appkey": SENTINEL_KEY,
            "secretkey": SENTINEL_SECRET,
        })
        api_req = rec.requests[1]
        self.assertTrue(
            api_req.headers["authorization"].startswith("Bearer "))
        self.assertEqual(api_req.headers["api-id"], "ka10080")

    def test_expires_dt_refresh_near_expiry(self):
        rec = Recorder()
        rec.token_expires_dt = "20260827120000"  # KST 절대 만료 시각
        now = [datetime(2026, 8, 27, 10, 0, tzinfo=KST)]
        client = _client(rec, clock=lambda: now[0])

        async def go():
            await client.request("kr_chart", api_id="ka10080", body={})
            # 만료 30초 전: 여유 마진(60초) 안쪽이라 재발급해야 한다.
            now[0] = datetime(2026, 8, 27, 11, 59, 30, tzinfo=KST)
            await client.request("kr_chart", api_id="ka10080", body={})

        _run(go())
        self.assertEqual(rec.token_calls, 2)

    def test_invalid_credentials_maps_to_credential_invalid(self):
        rec = Recorder()
        rec.token_response = httpx.Response(200, json=_TOKEN_FAILURE)
        client = _client(rec)
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(client.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(ctx.exception.provider_status, "credential_invalid")
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn(SENTINEL_SECRET, text)

    def test_token_http_401_is_credential_invalid(self):
        rec = Recorder()
        rec.token_response = httpx.Response(401, json={"return_code": 8005})
        client = _client(rec)
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(client.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(ctx.exception.provider_status, "credential_invalid")

    def test_token_429_is_rate_limited(self):
        rec = Recorder()
        rec.token_response = httpx.Response(429, json={})
        client = _client(rec)
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(client.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")

    def test_single_flight_refresh(self):
        rec = Recorder()
        client = _client(rec)

        async def go():
            await asyncio.gather(
                client.request("kr_chart", api_id="ka10080", body={}),
                client.request("kr_chart", api_id="ka10080", body={}),
                client.request("kr_chart", api_id="ka10080", body={}),
            )

        _run(go())
        self.assertEqual(rec.token_calls, 1)
        self.assertEqual(rec.api_calls, 3)

    def test_generation_change_clears_token(self):
        rec = Recorder()
        gen = [1]
        client = _client(rec, generation_provider=lambda: gen[0])

        async def go():
            await client.request("kr_chart", api_id="ka10080", body={})
            gen[0] = 2
            await client.request("kr_chart", api_id="ka10080", body={})

        _run(go())
        self.assertEqual(rec.token_calls, 2)


class RequestTests(unittest.TestCase):
    def test_real_and_mock_hosts(self):
        for profile, host in (("real", "api.kiwoom.com"),
                              ("demo", "mockapi.kiwoom.com")):
            rec = Recorder()
            client = _client(rec, profile=profile)
            _run(client.request("kr_chart", api_id="ka10080", body={}))
            self.assertTrue(
                all(r.url.host == host for r in rec.requests), profile)

    def test_401_invalidates_and_retries_once(self):
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(401, json={}),
            httpx.Response(200, json={"return_code": 0,
                                      "stk_min_pole_chart_qry": []}),
        ]
        client = _client(rec)
        resp = _run(client.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(resp.payload["return_code"], 0)
        self.assertEqual(rec.token_calls, 2)

        rec2 = Recorder()
        rec2.api_responses = [httpx.Response(401, json={}),
                              httpx.Response(401, json={})]
        client2 = _client(rec2)
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(client2.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(ctx.exception.provider_status,
                         "authentication_failed")

    def test_pagination_headers_roundtrip(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(200, json={
            "return_code": 0, "stk_min_pole_chart_qry": []},
            headers={"cont-yn": "Y", "next-key": "abc123"})]
        client = _client(rec)
        resp = _run(client.request(
            "kr_chart", api_id="ka10080", body={"stk_cd": "005930"},
            cont_yn="Y", next_key="prev-key"))
        self.assertEqual(resp.cont_yn, "Y")
        self.assertEqual(resp.next_key, "abc123")
        api_req = rec.requests[-1]
        self.assertEqual(api_req.headers["cont-yn"], "Y")
        self.assertEqual(api_req.headers["next-key"], "prev-key")

    def test_body_error_payload_returned_without_repr_leak(self):
        # KIS 선례: 본문 오류코드(return_code)는 파서·검증기가 분류한다.
        # 클라이언트는 payload 를 그대로 주되 repr 로 새지 않게 한다.
        rec = Recorder()
        rec.api_responses = [httpx.Response(200, json={
            "return_code": 1513,
            "return_msg": f"leak? {SENTINEL_SECRET}"})]
        client = _client(rec)
        resp = _run(client.request("kr_chart", api_id="ka10080", body={}))
        self.assertEqual(resp.payload["return_code"], 1513)
        self.assertNotIn(SENTINEL_SECRET, repr(resp) + str(resp))

    def test_secret_reflection_never_leaks(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(500, json={
            "echo_key": SENTINEL_KEY, "echo_secret": SENTINEL_SECRET,
            "echo_token": SENTINEL_TOKEN})]
        client = _client(rec)
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(client.request("kr_chart", api_id="ka10080", body={}))
        text = str(ctx.exception) + repr(ctx.exception) + repr(client)
        for banned in (SENTINEL_KEY, SENTINEL_SECRET, SENTINEL_TOKEN):
            self.assertNotIn(banned, text)

    def test_unknown_endpoint_rejected_before_network(self):
        rec = Recorder()
        client = _client(rec)
        with self.assertRaises(EndpointNotAllowedError):
            _run(client.request("order", api_id="kt10000", body={}))
        self.assertEqual(rec.requests, [])


if __name__ == "__main__":
    unittest.main()
