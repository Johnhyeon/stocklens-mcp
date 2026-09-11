"""KIS 연결 시험(verifier) 테스트 (Task 14). MockTransport 기반, 네트워크 없음."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials
from stock_mcp_server.market_data.kis_verifier import KisVerifier

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-444"
SENTINEL_SECRET = "PSA-SENTINEL-APP-SECRET-444"


def _creds():
    return BrokerCredentials(app_key=SENTINEL_KEY, app_secret=SENTINEL_SECRET)


class Scenario:
    """token / 국내 / 해외 probe 응답을 시나리오로 정의한다."""

    def __init__(self, token=200, domestic=200, overseas=200):
        self.token = token
        self.domestic = domestic
        self.overseas = overseas
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/tokenP"):
            self.calls.append("token")
            if self.token != 200:
                return httpx.Response(self.token, json={})
            return httpx.Response(200, json={
                "access_token": "fixture-token", "expires_in": 86400})
        if "domestic-stock" in path:
            self.calls.append("domestic")
            if self.domestic != 200:
                return httpx.Response(self.domestic, json={})
            return httpx.Response(200, json={
                "rt_cd": "0",
                "output2": [{"stck_bsop_date": "20260827",
                             "stck_cntg_hour": "100000",
                             "stck_oprc": "1", "stck_hgpr": "1",
                             "stck_lwpr": "1", "stck_prpr": "1",
                             "cntg_vol": "1"}]})
        if "overseas-price" in path:
            self.calls.append("overseas")
            if self.overseas != 200:
                return httpx.Response(self.overseas, json={})
            return httpx.Response(200, json={
                "rt_cd": "0",
                "output2": [{"xymd": "20260826", "xhms": "100000",
                             "open": "1", "high": "1", "low": "1",
                             "last": "1", "evol": "1", "eamt": "1"}]})
        return httpx.Response(404, json={})


def _verify(scenario: Scenario, profile="real") -> dict:
    verifier = KisVerifier(transport=httpx.MockTransport(scenario))
    return asyncio.run(verifier.verify(_creds(), profile))


class VerifierTests(unittest.TestCase):
    def test_success_both_markets(self):
        result = _verify(Scenario())
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "available")
        self.assertEqual(result["us_intraday"], "available")

    def test_authentication_failure_stops_early(self):
        scenario = Scenario(token=401)
        result = _verify(scenario)
        self.assertEqual(result["auth"], "credential_invalid")
        # 인증 실패면 시장 능력을 추측하지 않는다.
        self.assertEqual(result["kr_intraday"], "unverified")
        self.assertEqual(result["us_intraday"], "unverified")

    def test_one_market_failure(self):
        result = _verify(Scenario(overseas=403))
        self.assertEqual(result["auth"], "ok")
        self.assertEqual(result["kr_intraday"], "available")
        self.assertEqual(result["us_intraday"], "unavailable")

    def test_transient_error_is_unverified_not_unavailable(self):
        result = _verify(Scenario(overseas=429))
        self.assertEqual(result["kr_intraday"], "available")
        # 호출 제한은 능력 판정이 아니다. 추측하지 않는다.
        self.assertEqual(result["us_intraday"], "unverified")

    def test_no_secret_in_result(self):
        import json as _json
        result = _verify(Scenario())
        text = _json.dumps(result)
        self.assertNotIn(SENTINEL_KEY, text)
        self.assertNotIn(SENTINEL_SECRET, text)


if __name__ == "__main__":
    unittest.main()
