"""키움 연결 시험 (1.0 Task 12). 후보 자격 증명은 메모리에서만 쓴다.

판정 어휘는 KIS 검증기와 동일하다:
- "available"   probe 성공 (return_code 0)
- "unavailable" 권한 거부 또는 본문 오류코드 (이 프로필로는 못 쓴다)
- "unverified"  일시 오류(호출 제한 등)라 판정 불가. 추측하지 않는다.
"""

from __future__ import annotations

import httpx

from stock_mcp_server.market_data.kiwoom_client import (
    KiwoomApiError,
    KiwoomClient,
)

_AUTH_FAILURES = {"credential_invalid", "authentication_failed"}
_CAPABILITY_DENIED = {"permission_denied"}


class KiwoomVerifier:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def verify(self, credentials, profile: str) -> dict:
        client = KiwoomClient(credentials, profile,
                              transport=self._transport)
        result = {
            "auth": "unverified",
            "kr_intraday": "unverified",
            "us_intraday": "unverified",
        }

        # 국내 probe. 토큰 발급이 여기서 함께 일어난다.
        kr = await self._probe(client, "kr_chart", "ka10080", {
            "stk_cd": "005930",
            "tic_scope": "1",
            "upd_stkpc_tp": "0",
        })
        if kr in _AUTH_FAILURES:
            result["auth"] = "credential_invalid"
            return result

        result["auth"] = "ok"
        result["kr_intraday"] = kr
        # US 는 실측 계약 불일치(가격·거래량·커버리지, 2026-08-27)로
        # 코드 차단 상태다. probe 없이 unavailable 로 고정한다.
        result["us_intraday"] = "unavailable"
        return result

    async def _probe(self, client: KiwoomClient, endpoint_id: str,
                     api_id: str, body: dict) -> str:
        try:
            response = await client.request(
                endpoint_id, api_id=api_id, body=body)
        except KiwoomApiError as exc:
            if exc.provider_status in _AUTH_FAILURES:
                return exc.provider_status
            if exc.provider_status in _CAPABILITY_DENIED:
                return "unavailable"
            return "unverified"
        code = response.payload.get("return_code")
        if code not in (0, "0", None):
            return "unavailable"
        return "available"
