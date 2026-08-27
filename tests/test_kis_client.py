"""KIS HTTP·토큰 클라이언트 테스트 (Task 6). 네트워크 없이 MockTransport 사용.

비밀 sentinel 이 예외, repr, 응답 어디에도 새지 않는지 함께 검증한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials
from stock_mcp_server.market_data.kis_client import (
    DEMO_BASE_URL,
    REAL_BASE_URL,
    KisApiError,
    KisClient,
)

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-222"
SENTINEL_SECRET = "PSA-SENTINEL-APP-SECRET-222"
SENTINEL_TOKEN = "PSA-SENTINEL-ACCESS-TOKEN-222"


def _creds() -> BrokerCredentials:
    return BrokerCredentials(app_key=SENTINEL_KEY, app_secret=SENTINEL_SECRET)


class Recorder:
    """MockTransport 핸들러. 요청을 기록하고 시나리오대로 응답한다."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.api_calls = 0
        self.api_responses: list = []
        self.token_response = None
        self.expires_in = 86400

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/oauth2/tokenP"):
            self.token_calls += 1
            if self.token_response is not None:
                return self.token_response
            return httpx.Response(200, json={
                "access_token": f"{SENTINEL_TOKEN}-{self.token_calls}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
            })
        self.api_calls += 1
        if self.api_responses:
            item = self.api_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={"rt_cd": "0", "output": []})


def _client(recorder: Recorder, profile: str = "real", **kwargs) -> KisClient:
    return KisClient(
        credentials=_creds(),
        profile=profile,
        transport=httpx.MockTransport(recorder.handler),
        **kwargs,
    )


def _run(coro):
    return asyncio.run(coro)


class BaseUrlTests(unittest.TestCase):
    def test_real_and_demo_base_urls(self):
        rec = Recorder()
        real = _client(rec, "real")
        demo = _client(rec, "demo")
        self.assertEqual(real.base_url, REAL_BASE_URL)
        self.assertEqual(demo.base_url, DEMO_BASE_URL)
        self.assertIn("openapivts", DEMO_BASE_URL)


class TokenLifecycleTests(unittest.TestCase):
    def test_token_issued_and_reused_before_expiry(self):
        rec = Recorder()
        now = [1000.0]
        client = _client(rec, clock=lambda: now[0])

        async def go():
            await client.request("GET", "/x", tr_id="T1")
            now[0] += 100
            await client.request("GET", "/x", tr_id="T1")

        _run(go())
        self.assertEqual(rec.token_calls, 1)
        self.assertEqual(rec.api_calls, 2)

    def test_token_refreshed_near_expiry(self):
        rec = Recorder()
        rec.expires_in = 600
        now = [1000.0]
        client = _client(rec, clock=lambda: now[0])

        async def go():
            await client.request("GET", "/x", tr_id="T1")
            # 만료 직전(여유 마진 안쪽)으로 이동하면 재발급해야 한다.
            now[0] += 600 - 10
            await client.request("GET", "/x", tr_id="T1")

        _run(go())
        self.assertEqual(rec.token_calls, 2)

    def test_generation_change_clears_token(self):
        rec = Recorder()
        gen = [1]
        client = _client(rec, generation_provider=lambda: gen[0])

        async def go():
            await client.request("GET", "/x", tr_id="T1")
            gen[0] = 2
            await client.request("GET", "/x", tr_id="T1")

        _run(go())
        self.assertEqual(rec.token_calls, 2)

    def test_auth_header_uses_bearer_token(self):
        rec = Recorder()
        client = _client(rec)
        _run(client.request("GET", "/x", tr_id="T1"))
        api_req = rec.requests[-1]
        self.assertTrue(
            api_req.headers["authorization"].startswith("Bearer "))
        self.assertEqual(api_req.headers["tr_id"], "T1")


class RetryAndErrorTests(unittest.TestCase):
    def test_401_refreshes_once_and_retries_once(self):
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(401, json={"msg_cd": "EGW00123"}),
            httpx.Response(200, json={"rt_cd": "0", "output": ["ok"]}),
        ]
        client = _client(rec)
        result = _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(result["output"], ["ok"])
        self.assertEqual(rec.token_calls, 2)
        self.assertEqual(rec.api_calls, 2)

    def test_second_401_raises_authentication_failed(self):
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(401, json={}),
            httpx.Response(401, json={}),
        ]
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "authentication_failed")
        self.assertEqual(rec.api_calls, 2)

    def test_403_permission_denied(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(403, json={})]
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "permission_denied")

    def test_429_rate_limited(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(429, json={})]
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "rate_limited")

    def test_malformed_json_source_parse_error(self):
        rec = Recorder()
        rec.api_responses = [httpx.Response(200, text="<html>oops</html>")]
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "source_parse_error")

    def test_timeout_provider_unavailable(self):
        rec = Recorder()
        rec.api_responses = [httpx.ConnectTimeout("slow")]
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "provider_unavailable")

    def test_token_endpoint_401_credential_invalid(self):
        rec = Recorder()
        rec.token_response = httpx.Response(401, json={"msg1": "invalid"})
        client = _client(rec)
        with self.assertRaises(KisApiError) as ctx:
            _run(client.request("GET", "/x", tr_id="T1"))
        self.assertEqual(ctx.exception.provider_status, "credential_invalid")


class SecretLeakTests(unittest.TestCase):
    def _assert_clean(self, text: str):
        for banned in (SENTINEL_KEY, SENTINEL_SECRET, SENTINEL_TOKEN,
                       SENTINEL_KEY[:10], SENTINEL_SECRET[:10]):
            self.assertNotIn(banned, text)

    def test_no_secret_in_error_or_repr(self):
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(403, json={"detail": "denied"}),
        ]
        client = _client(rec)
        try:
            _run(client.request("GET", "/x", tr_id="T1"))
            self.fail("expected KisApiError")
        except KisApiError as exc:
            self._assert_clean(str(exc))
            self._assert_clean(repr(exc))
        self._assert_clean(repr(client))

    def test_no_secret_even_when_provider_echoes_it(self):
        # 공급자가 오류 본문에 키를 그대로 되돌려줘도 예외에 싣지 않는다.
        rec = Recorder()
        rec.api_responses = [
            httpx.Response(403, json={"echo": SENTINEL_KEY}),
        ]
        client = _client(rec)
        try:
            _run(client.request("GET", "/x", tr_id="T1"))
            self.fail("expected KisApiError")
        except KisApiError as exc:
            self._assert_clean(str(exc) + repr(exc))


if __name__ == "__main__":
    unittest.main()
