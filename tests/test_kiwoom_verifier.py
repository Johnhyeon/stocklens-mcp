"""키움 연결 시험 테스트 (1.0 Task 12).

판정 값은 KIS 검증기와 동일 어휘를 쓴다:
- available / unavailable / unverified, auth: ok / credential_invalid
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.kiwoom_verifier import KiwoomVerifier
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
_TOKEN_SUCCESS = json.loads(
    (_FIXTURES / "token_success.json").read_text("utf-8"))
_TOKEN_FAILURE = json.loads(
    (_FIXTURES / "token_failure.json").read_text("utf-8"))


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": "verify-key", "secret_key": "verify-secret"})


class Scenario:
    def __init__(self, token=None, kr=None, us=None):
        self.token = token
        self.kr = kr
        self.us = us

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return self.token or httpx.Response(200, json=_TOKEN_SUCCESS)
        if request.url.path == "/api/dostk/chart":
            return self.kr or httpx.Response(200, json={
                "return_code": 0,
                "stk_cd": "005930",
                "stk_min_pole_chart_qry": [{"cur_prc": "+70000"}]})
        if request.url.path == "/api/us/chart":
            return self.us or httpx.Response(200, json={
                "return_code": 0,
                "result_list": [{"cur_prc": "225.1000"}]})
        raise AssertionError(f"unexpected path {request.url.path}")


def _verify(scenario: Scenario) -> dict:
    verifier = KiwoomVerifier(
        transport=httpx.MockTransport(scenario.handler))
    return asyncio.run(verifier.verify(_payload(), "real"))


class KiwoomVerifierTests(unittest.TestCase):
    def test_kr_and_us_both_probed_available(self):
        # US 도 probe 한다 (2026-08-28 ET 라벨 계약 확정으로 차단 해제).
        result = _verify(Scenario())
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "available")
        self.assertEqual(result["us_intraday"], "available")

    def test_us_body_error_is_unavailable(self):
        result = _verify(Scenario(
            us=httpx.Response(200, json={"return_code": 7,
                                         "return_msg": "no data"})))
        self.assertEqual(result["kr_intraday"], "available")
        self.assertEqual(result["us_intraday"], "unavailable")

    def test_invalid_credentials_short_circuits(self):
        result = _verify(Scenario(
            token=httpx.Response(200, json=_TOKEN_FAILURE)))
        self.assertEqual(result["auth"], "credential_invalid")
        self.assertEqual(result["kr_intraday"], "unverified")
        self.assertEqual(result["us_intraday"], "unverified")

    def test_rate_limited_probe_is_unverified(self):
        result = _verify(Scenario(
            kr=httpx.Response(429, json={})))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "unverified")

    def test_permission_denied_is_unavailable(self):
        result = _verify(Scenario(
            kr=httpx.Response(403, json={})))
        self.assertEqual(result["kr_intraday"], "unavailable")

    def test_body_error_code_is_unavailable(self):
        result = _verify(Scenario(
            kr=httpx.Response(200, json={"return_code": 1505,
                                         "return_msg": "no api"})))
        self.assertEqual(result["kr_intraday"], "unavailable")


if __name__ == "__main__":
    unittest.main()
