"""토스 연결 시험 (1.0 Task 15). 후보 자격 증명은 메모리에서만 쓴다.

auth 판정: ok / credential_invalid / ip_not_allowed.
ip_not_allowed 는 공식 오류 코드(403 access_denied)로 확인된 경우에만
쓴다. 시장 능력은 available / unavailable / unverified 어휘를 쓴다.
"""

from __future__ import annotations

import httpx

from stock_mcp_server.market_data.toss_client import (
    TossApiError,
    TossClient,
)

_AUTH_FAILURES = {"credential_invalid", "authentication_failed"}


class TossVerifier:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def verify(self, credentials, profile: str) -> dict:
        client = TossClient(credentials, profile, transport=self._transport)
        result = {
            "auth": "unverified",
            "kr_intraday": "unverified",
            "us_intraday": "unverified",
        }

        # 인증 probe 는 미국 종목으로만 한다 (KR 캔들은 차단 상태라
        # probe 수단이 US 뿐이다). 능력 판정은 probe 결과와 무관하게
        # 정책으로 고정한다:
        # - KR: 실측 계약 불일치 (봉 라벨 시프트·통합 거래량·마감
        #   동시호가 부재, 2026-08-27)
        # - US: 대표 결정 (2026-08-28) - 토스 캔들은 KIS·키움과 다른
        #   자체 테이프(완결일 391분 전부 OHLC 상이, 거래량 10~40%,
        #   과거일은 금요일만 보존)라 1.0 시세 계약 미지원. 연결·인증은
        #   유지하고, 어댑터 파이프라인은 후속 "토스 자체 시세" 기능용
        #   으로 보존한다 (evidence/toss/us-tape-mismatch-20260828.json)
        us = await self._probe(client, "AAPL")
        if us in _AUTH_FAILURES:
            result["auth"] = "credential_invalid"
            return result
        if us == "ip_not_allowed":
            result["auth"] = "ip_not_allowed"
            return result

        result["auth"] = "ok"
        result["kr_intraday"] = "unavailable"
        result["us_intraday"] = "unavailable"
        return result

    async def _probe(self, client: TossClient, symbol: str) -> str:
        try:
            payload = await client.request("candles", params={
                "symbol": symbol,
                "interval": "1m",
                "count": "2",
                # 수정주가 미적용 원천만 검증한다 (봉 계약과 동일).
                "adjusted": "false",
            })
        except TossApiError as exc:
            if exc.error_code == "ip_not_allowed":
                return "ip_not_allowed"
            if exc.provider_status in _AUTH_FAILURES:
                return exc.provider_status
            if exc.provider_status in ("permission_denied",
                                       "entity_not_found"):
                return "unavailable"
            return "unverified"
        result = payload.get("result")
        if not isinstance(result, dict) or "candles" not in result:
            return "unavailable"
        return "available"
