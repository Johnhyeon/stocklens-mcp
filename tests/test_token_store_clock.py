"""저장되는 만료 시각은 반드시 벽시계다 (2026-08-28 실사고).

KIS 와 토스 클라이언트는 프로세스 안에서 `time.monotonic` 으로 만료를
잰다. monotonic 은 프로세스마다 기준이 다르고 부팅 이후 초 단위라
`time.time()` 과 자릿수부터 다르다. 그 값을 그대로 저장하면 다른
프로세스에서 항상 "이미 만료"로 읽혀 토큰을 다시 발급받고, KIS 에서는
1분 1회 제한에 걸린다.

실제로 그렇게 당했다: 저장 블록이 두 번 들어가 두 번째가 monotonic 값으로
덮어썼고, 재사용이 조용히 죽어 있었다.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.token_store import TokenStore


class FakeKeyring:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        del self.entries[(service, username)]


class StoredExpiryIsWallClockTests(unittest.TestCase):
    def _stored(self, keyring) -> dict:
        self.assertTrue(keyring.entries, "토큰이 저장되지 않았다")
        raw = next(iter(keyring.entries.values()))
        return json.loads(raw)

    def test_kis_stores_wall_clock_expiry(self):
        import asyncio

        from stock_mcp_server.market_data.kis_client import KisClient

        kr = FakeKeyring()

        def handler(request):
            if request.url.path.endswith("/oauth2/tokenP"):
                return httpx.Response(200, json={"access_token": "T",
                                                 "expires_in": 86400})
            return httpx.Response(200, json={"rt_cd": "0", "output": []})

        client = KisClient(
            SecretPayload.from_schema(
                registry.require("kis").credential_schema,
                {"app_key": "k", "app_secret": "s"}),
            "real", transport=httpx.MockTransport(handler),
            token_store=TokenStore(keyring_module=kr))

        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as http:
                await client._ensure_token(http)  # noqa: SLF001

        asyncio.run(go())
        record = self._stored(kr)
        now = time.time()
        # 벽시계라면 지금보다 크고 하루 안쪽이다. monotonic 이면 자릿수부터
        # 다르다 (부팅 이후 초).
        self.assertGreater(record["expires_at"], now,
                           "만료가 과거로 저장됐다 (monotonic 값 의심)")
        self.assertLess(record["expires_at"], now + 86400 + 60)

    def test_toss_stores_wall_clock_expiry(self):
        import asyncio

        from stock_mcp_server.market_data.toss_client import TossClient

        kr = FakeKeyring()

        def handler(request):
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "T",
                                                 "token_type": "Bearer",
                                                 "expires_in": 86399})
            return httpx.Response(200, json={"result": {"candles": []}})

        client = TossClient(
            SecretPayload.from_schema(
                registry.require("toss").credential_schema,
                {"client_id": "c", "client_secret": "s"}),
            "real", transport=httpx.MockTransport(handler),
            token_store=TokenStore(keyring_module=kr))
        asyncio.run(client._ensure_token())  # noqa: SLF001
        record = self._stored(kr)
        now = time.time()
        self.assertGreater(record["expires_at"], now)
        self.assertLess(record["expires_at"], now + 86400 + 60)

    def test_reissue_writes_only_once(self):
        # 저장 블록이 두 번 들어가면 뒤엣것이 앞엣것을 덮는다. 한 번만
        # 쓰는지 확인해 그 사고를 다시 만들지 않는다.
        import asyncio

        from stock_mcp_server.market_data.kis_client import KisClient

        writes = []

        class CountingStore(TokenStore):
            def save(self, provider, profile, fingerprint, token,
                     expires_at):
                writes.append(expires_at)

        def handler(request):
            if request.url.path.endswith("/oauth2/tokenP"):
                return httpx.Response(200, json={"access_token": "T",
                                                 "expires_in": 86400})
            return httpx.Response(200, json={"rt_cd": "0", "output": []})

        client = KisClient(
            SecretPayload.from_schema(
                registry.require("kis").credential_schema,
                {"app_key": "k", "app_secret": "s"}),
            "real", transport=httpx.MockTransport(handler),
            token_store=CountingStore(keyring_module=FakeKeyring()))

        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as http:
                await client._ensure_token(http)  # noqa: SLF001

        asyncio.run(go())
        self.assertEqual(len(writes), 1, f"저장이 {len(writes)}번 일어났다")


if __name__ == "__main__":
    unittest.main()
