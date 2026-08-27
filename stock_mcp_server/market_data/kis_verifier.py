"""KIS 연결 시험. 후보 자격 증명은 메모리에서만 쓰고 저장하지 않는다.

국내·미국 능력을 개별 probe 로 확인한다. 판정 값:
- "available"   probe 성공
- "unavailable" 권한·인증 계열 실패 (이 프로필로는 못 쓴다)
- "unverified"  일시 오류(호출 제한 등)라 판정 불가. 추측하지 않는다.
"""

from __future__ import annotations

import httpx

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials
from stock_mcp_server.market_data.kis_client import KisApiError, KisClient

_DOMESTIC_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
_DOMESTIC_TR = "FHKST03010230"
_OVERSEAS_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
_OVERSEAS_TR = "HHDFS76950200"

# 인증·권한 계열: 이 프로필의 능력이 없다고 판정해도 되는 상태.
_CAPABILITY_DENIED = {
    "credential_invalid", "authentication_failed", "permission_denied",
}

_AUTH_FAILURES = {"credential_invalid", "authentication_failed"}


class KisVerifier:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def verify(self, credentials: BrokerCredentials,
                     profile: str) -> dict:
        client = KisClient(credentials, profile, transport=self._transport)
        result = {
            "auth": "unverified",
            "kr_intraday": "unverified",
            "us_intraday": "unverified",
        }

        # 국내 probe. 토큰 발급이 여기서 함께 일어난다.
        kr = await self._probe(client, "GET", _DOMESTIC_PATH, _DOMESTIC_TR, {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": "005930",
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_HOUR_1": "100000",
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_FAKE_TICK_INCU_YN": "N",
        })
        if kr in _AUTH_FAILURES:
            # 인증 자체가 실패했다. 시장 능력을 추측하지 않는다.
            result["auth"] = "credential_invalid"
            return result

        result["auth"] = "ok"
        result["kr_intraday"] = kr

        us = await self._probe(client, "GET", _OVERSEAS_PATH, _OVERSEAS_TR, {
            "AUTH": "", "EXCD": "NAS", "SYMB": "AAPL", "NMIN": "1",
            "PINC": "1", "NEXT": "", "NREC": "2", "FILL": "", "KEYB": "",
        })
        result["us_intraday"] = us
        return result

    async def _probe(self, client: KisClient, method: str, path: str,
                     tr_id: str, params: dict) -> str:
        try:
            payload = await client.request(
                method, path, tr_id=tr_id, params=params)
        except KisApiError as exc:
            if exc.provider_status in _AUTH_FAILURES:
                return exc.provider_status
            if exc.provider_status in _CAPABILITY_DENIED:
                return "unavailable"
            return "unverified"
        if str(payload.get("rt_cd", "")) != "0":
            return "unavailable"
        return "available"
