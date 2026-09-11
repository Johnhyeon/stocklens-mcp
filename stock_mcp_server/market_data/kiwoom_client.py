"""키움증권 REST HTTP·토큰 클라이언트 (1.0 Task 12).

공식 스펙(au10001):
- POST /oauth2/token, body {grant_type: client_credentials, appkey, secretkey}
- 200 응답 {expires_dt: YYYYMMDDHHmmss(KST 절대 시각), token_type, token}
차트 호출은 api-id 헤더로 TR 을 고르고 Bearer 토큰을 붙인다. 연속조회는
응답 header 의 cont-yn / next-key 를 요청 header 로 되돌린다.

- 토큰은 프로세스 메모리에만 둔다. 파일·상태·캐시 저장 금지.
- generation 이 바뀌면 토큰을 폐기한다.
- 본문 오류코드(return_code)는 KIS 선례대로 파서·검증기가 분류한다.
- 예외·repr 에 키·토큰·응답 원문을 싣지 않는다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from stock_mcp_server.market_data.broker_http import (
    BrokerHttpError,
    BrokerHttpTransport,
)
from stock_mcp_server.market_data.provider_registry import registry

_KST = ZoneInfo("Asia/Seoul")
_DESCRIPTOR = registry.require("kiwoom")

# 만료 전 여유 마진. 남은 수명이 이보다 짧으면 재발급한다.
_REFRESH_MARGIN = timedelta(seconds=60)


class KiwoomApiError(Exception):
    """비밀 없는 키움 오류. provider_status 는 설계 27절 어휘만 쓴다."""

    def __init__(self, provider_status: str, http_status: int | None = None):
        self.provider_status = provider_status
        self.http_status = http_status
        detail = f"http={http_status}" if http_status is not None else "no-http"
        super().__init__(f"Kiwoom 오류: {provider_status} ({detail})")


class KiwoomResponse:
    """payload 와 연속조회 헤더. repr 에 본문을 드러내지 않는다."""

    def __init__(self, payload: dict, cont_yn: str | None,
                 next_key: str | None):
        self.payload = payload
        self.cont_yn = cont_yn
        self.next_key = next_key

    def __repr__(self) -> str:
        return f"KiwoomResponse(cont_yn={self.cont_yn!r})"

    __str__ = __repr__


class KiwoomClient:
    def __init__(
        self,
        credentials,
        profile: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        generation_provider=None,
        clock=None,
    ) -> None:
        if profile not in _DESCRIPTOR.supported_profiles:
            raise ValueError(f"지원하지 않는 프로필: {profile}")
        self._app_key = credentials.get("app_key")
        self._secret_key = credentials.get("secret_key")
        self.profile = profile
        self._transport = BrokerHttpTransport(transport=transport)
        self._generation_provider = generation_provider or (lambda: 0)
        self._clock = clock or (lambda: datetime.now(_KST))
        self._lock = asyncio.Lock()

        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_generation: int | None = None

    def __repr__(self) -> str:  # noqa: D105
        return f"KiwoomClient(profile={self.profile})"

    # --- 토큰 ---

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = None
        self._token_generation = None

    def _token_valid(self) -> bool:
        if self._token is None or self._token_expires_at is None:
            return False
        if self._token_generation != self._generation_provider():
            self._invalidate_token()
            return False
        return (self._token_expires_at - self._clock()) > _REFRESH_MARGIN

    async def _issue_token(self) -> None:
        try:
            response = await self._transport.request(
                _DESCRIPTOR, "token", profile=self.profile,
                json_body={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._secret_key,
                })
        except BrokerHttpError as exc:
            if exc.provider_status == "rate_limited":
                raise KiwoomApiError("rate_limited",
                                     exc.status_code) from None
            if exc.status_code in (400, 401, 403):
                raise KiwoomApiError("credential_invalid",
                                     exc.status_code) from None
            raise KiwoomApiError("provider_unavailable",
                                 exc.status_code) from None

        payload = response.json()
        if not isinstance(payload, dict):
            raise KiwoomApiError("source_parse_error", response.status_code)
        token = payload.get("token")
        expires_dt = payload.get("expires_dt")
        if not token or not expires_dt:
            code = payload.get("return_code")
            if code not in (0, "0", None):
                # 200 + return_code 오류 = 키 문제 (공식 오류 응답 형태)
                raise KiwoomApiError(
                    "credential_invalid", response.status_code)
            raise KiwoomApiError("source_parse_error", response.status_code)
        try:
            expires_at = datetime.strptime(
                str(expires_dt), "%Y%m%d%H%M%S").replace(tzinfo=_KST)
        except ValueError:
            raise KiwoomApiError(
                "source_parse_error", response.status_code) from None

        self._token = token
        self._token_expires_at = expires_at
        self._token_generation = self._generation_provider()

    async def _ensure_token(self) -> str:
        # single-flight: 동시 요청이 토큰을 중복 발급하지 않게 한다.
        async with self._lock:
            if not self._token_valid():
                await self._issue_token()
            assert self._token is not None
            return self._token

    # --- API 요청 ---

    async def request(
        self,
        endpoint_id: str,
        *,
        api_id: str,
        body: dict,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> KiwoomResponse:
        """endpoint_id 로만 요청한다. 401이면 한 번만 재발급·재시도한다."""
        if _DESCRIPTOR.endpoint(endpoint_id) is None:
            # 허용 목록 밖이면 토큰 발급조차 하지 않는다.
            from stock_mcp_server.market_data.broker_http import (
                EndpointNotAllowedError,
            )
            raise EndpointNotAllowedError(
                f"kiwoom에 허용되지 않은 endpoint: {endpoint_id}")
        response = await self._send_once(
            endpoint_id, api_id, body, cont_yn, next_key)
        if response is None:  # 401
            self._invalidate_token()
            response = await self._send_once(
                endpoint_id, api_id, body, cont_yn, next_key)
            if response is None:
                raise KiwoomApiError("authentication_failed", 401)
        payload = response.json()
        if not isinstance(payload, dict):
            raise KiwoomApiError("source_parse_error", response.status_code)
        return KiwoomResponse(
            payload,
            response.get_header("cont-yn"),
            response.get_header("next-key"))

    async def _send_once(self, endpoint_id, api_id, body,
                         cont_yn, next_key):
        token = await self._ensure_token()
        headers = {
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        try:
            return await self._transport.request(
                _DESCRIPTOR, endpoint_id, profile=self.profile,
                headers=headers, json_body=body)
        except BrokerHttpError as exc:
            if exc.status_code == 401:
                return None
            if exc.status_code == 403:
                raise KiwoomApiError("permission_denied", 403) from None
            if exc.provider_status == "rate_limited":
                raise KiwoomApiError("rate_limited",
                                     exc.status_code) from None
            raise KiwoomApiError("provider_unavailable",
                                 exc.status_code) from None
