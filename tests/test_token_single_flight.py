"""동시 요청이 토큰을 중복 발급하지 않는다 (1.1 리뷰 차단 1).

`investor_flow_batch` 는 최대 30종목을 `asyncio.gather` 로 동시에 부른다.
저장된 토큰이 없거나 막 만료된 순간 첫 배치가 들어오면, single-flight 가
없는 클라이언트는 발급 요청을 **종목 수만큼** 보낸다.

KIS 는 토큰 발급이 1분 1회 제한이다(실측 2026-08-27, 403 + EGW00133).
그러니 이건 낭비가 아니라 **첫 배치가 그대로 실패하는 결함**이다.
토스는 재발급이 이전 토큰을 즉시 무효화해서 서로의 토큰을 죽인다.

세 공급자를 같은 테스트로 묶는 이유: 키움·토스에는 lock 이 있었고 KIS
에만 없었다. 하나씩 따로 두면 다음에도 하나만 빠진다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.kis_client import KisClient
from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.toss_client import TossClient

CONCURRENT = 10


def _payload(provider_id: str) -> SecretPayload:
    schema = registry.require(provider_id).credential_schema
    return SecretPayload.from_schema(
        schema, {f.name: "value-for-" + f.name for f in schema})


class _Counter:
    """토큰 발급 횟수를 센다. 데이터 요청은 그냥 통과시킨다."""

    def __init__(self):
        self.token_calls = 0
        self.data_calls = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/oauth2/tokenP", "/oauth2/token")):
            self.token_calls += 1
            # 실제 발급은 즉시 끝나지 않는다. 경합 창을 열어 둔다.
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={
                "access_token": "T", "expires_in": 86400,
                "token": "T", "token_type": "Bearer",
                "expires_dt": "20991231235959",
                "result": {"accessToken": "T", "expiresIn": 86400},
            })
        self.data_calls += 1
        return httpx.Response(200, json={
            "rt_cd": "0", "return_code": 0, "output": [], "output2": [],
            "stk_invsr_orgn": [], "candles": [], "result": {"candles": []},
        })


def _transport(counter: _Counter) -> httpx.MockTransport:
    return httpx.MockTransport(counter.handler)


class SingleFlightTests(unittest.TestCase):
    def _run_concurrent(self, make_client, call):
        counter = _Counter()
        client = make_client(counter)

        async def main():
            await asyncio.gather(*[call(client) for _ in range(CONCURRENT)],
                                 return_exceptions=True)

        asyncio.run(main())
        return counter

    def test_kis_issues_one_token_for_concurrent_requests(self):
        counter = self._run_concurrent(
            lambda c: KisClient(_payload("kis"), "real",
                                transport=_transport(c)),
            lambda client: client.request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                tr_id="FHKST01010900",
                params={"FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": "005930"}))
        self.assertEqual(counter.token_calls, 1,
                         f"동시 {CONCURRENT}건에 발급 "
                         f"{counter.token_calls}회. KIS 는 1분 1회 제한이라 "
                         "첫 배치가 그대로 실패한다.")
        self.assertEqual(counter.data_calls, CONCURRENT)

    def test_kiwoom_issues_one_token_for_concurrent_requests(self):
        counter = self._run_concurrent(
            lambda c: KiwoomClient(_payload("kiwoom"), "real",
                                   transport=_transport(c)),
            lambda client: client.request(
                "kr_investor_daily", api_id="ka10059",
                body={"stk_cd": "005930", "dt": "20260828",
                      "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1"}))
        self.assertEqual(counter.token_calls, 1)

    def test_toss_issues_one_token_for_concurrent_requests(self):
        # 토스는 재발급이 이전 토큰을 무효화한다. 중복 발급은 서로를
        # 죽이는 경합이 된다.
        counter = self._run_concurrent(
            lambda c: TossClient(_payload("toss"), "real",
                                 transport=_transport(c)),
            lambda client: client.request(
                "candles", params={"symbol": "005930"}))
        self.assertEqual(counter.token_calls, 1)


class ReissueAfterInvalidationTests(unittest.TestCase):
    def test_a_second_wave_after_invalidation_issues_exactly_once_more(self):
        """폐기 후에도 한 번만 다시 받는다.

        lock 을 잡고 나서 조건을 다시 보지 않으면, 대기하던 요청들이
        차례로 깨어나며 각자 재발급한다 (double-checked locking).
        """
        counter = _Counter()
        client = KisClient(_payload("kis"), "real",
                           transport=_transport(counter))

        async def call():
            return await client.request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                tr_id="FHKST01010900",
                params={"FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": "005930"})

        async def main():
            await asyncio.gather(*[call() for _ in range(CONCURRENT)])
            client._invalidate_token()
            await asyncio.gather(*[call() for _ in range(CONCURRENT)])

        asyncio.run(main())
        self.assertEqual(counter.token_calls, 2)


if __name__ == "__main__":
    unittest.main()
