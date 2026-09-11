"""토스 연결 시험 테스트 (1.0 Task 15).

auth 판정: ok / credential_invalid / ip_not_allowed.
ip_not_allowed 는 공식 오류(403 access_denied)로 확인된 경우에만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.toss_verifier import TossVerifier

_FIXTURES = Path(__file__).parent / "fixtures" / "toss"
_TOKEN_SUCCESS = json.loads(
    (_FIXTURES / "token_success.json").read_text("utf-8"))
_TOKEN_FAILURE = json.loads(
    (_FIXTURES / "token_failure.json").read_text("utf-8"))

_KR_CANDLES = {"result": {"candles": [
    {"timestamp": "2026-08-27T15:00:00+09:00", "openPrice": "71000",
     "highPrice": "71100", "lowPrice": "70900", "closePrice": "71050",
     "volume": "12000", "currency": "KRW"}], "nextBefore": None}}
_US_CANDLES = {"result": {"candles": [
    {"timestamp": "2026-08-27T10:30:00-04:00", "openPrice": "225.10",
     "highPrice": "225.30", "lowPrice": "225.00", "closePrice": "225.25",
     "volume": "52000", "currency": "USD"}], "nextBefore": None}}


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("toss").credential_schema,
        {"client_id": "cid", "client_secret": "csec"})


class Scenario:
    def __init__(self, token=None, kr=None, us=None):
        self.token = token
        self.kr = kr
        self.us = us
        self.candle_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return self.token or httpx.Response(200, json=_TOKEN_SUCCESS)
        assert request.url.path == "/api/v1/candles"
        self.candle_requests.append(request)
        symbol = request.url.params.get("symbol")
        if symbol == "005930":
            return self.kr or httpx.Response(200, json=_KR_CANDLES)
        return self.us or httpx.Response(200, json=_US_CANDLES)


def _verify(scenario: Scenario) -> dict:
    verifier = TossVerifier(transport=httpx.MockTransport(scenario.handler))
    return asyncio.run(verifier.verify(_payload(), "real"))


class TossVerifierTests(unittest.TestCase):
    def test_both_markets_pinned_unavailable(self):
        # KR 은 실측 계약 불일치(2026-08-27), US 는 대표 결정(2026-08-28,
        # 자체 테이프 = 1.0 시세 계약 미지원)으로 능력을 unavailable 로
        # 고정한다. probe 는 인증 확인용으로만 쓴다.
        scenario = Scenario()
        result = _verify(scenario)
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "unavailable")
        self.assertEqual(result["us_intraday"], "unavailable")
        symbols = {req.url.params.get("symbol")
                   for req in scenario.candle_requests}
        self.assertNotIn("005930", symbols)
        for req in scenario.candle_requests:
            self.assertEqual(req.url.params.get("interval"), "1m")
            self.assertEqual(req.url.params.get("adjusted"), "false")

    def test_invalid_client(self):
        result = _verify(Scenario(
            token=httpx.Response(401, json=_TOKEN_FAILURE)))
        self.assertEqual(result["auth"], "credential_invalid")
        self.assertEqual(result["us_intraday"], "unverified")

    def test_ip_not_allowed(self):
        result = _verify(Scenario(token=httpx.Response(403, json={
            "error": "access_denied",
            "error_description": "IP address not allowed"})))
        self.assertEqual(result["auth"], "ip_not_allowed")
        self.assertEqual(result["kr_intraday"], "unverified")
        self.assertEqual(result["us_intraday"], "unverified")

    def test_rate_limited_probe_stays_pinned(self):
        # 토큰 발급은 성공했으므로 auth 는 ok. 능력은 정책 고정이라
        # probe 결과와 무관하게 unavailable 이다.
        result = _verify(Scenario(us=httpx.Response(
            429, json={"error": {"code": "rate-limit-exceeded"}})))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["us_intraday"], "unavailable")

    def test_not_found_probe_is_unavailable(self):
        result = _verify(Scenario(us=httpx.Response(404, json={
            "error": {"code": "not-found"}})))
        self.assertEqual(result["us_intraday"], "unavailable")

    def test_empty_candles_auth_ok_capability_pinned(self):
        # 휴장 시간대의 빈 결과도 "권한 있음"의 증거다 (auth ok).
        result = _verify(Scenario(us=httpx.Response(200, json={
            "result": {"candles": [], "nextBefore": None}})))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["us_intraday"], "unavailable")


if __name__ == "__main__":
    unittest.main()
