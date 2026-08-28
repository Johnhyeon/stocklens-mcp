"""토스증권 Open API 클라이언트 (1.0 Task 15).

공식 스펙:
- POST /oauth2/token (application/x-www-form-urlencoded)
  {grant_type=client_credentials, client_id, client_secret}
  200 {access_token, token_type: Bearer, expires_in(초)}
- client 당 유효 토큰 1개. 재발급 시 이전 토큰 즉시 무효화.
- 403 {error: access_denied, "IP address not allowed"} = 허용 IP 밖.
  공식 코드로 확인된 경우에만 ip_not_allowed 로 분류한다 (추측 금지).
- 429 는 Retry-After 헤더를 싣는다.

계좌·주문 금지: X-Tossinvest-Account 헤더를 만들지 않고, 주문·계좌
경로는 레지스트리 허용 목록에 없어 요청 자체가 만들어지지 않는다.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from stock_mcp_server.market_data.broker_http import (
    BrokerHttpError,
    BrokerHttpTransport,
)
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.token_store import (
    credential_fingerprint as _fingerprint,
)

_DESCRIPTOR = registry.require("toss")

_REFRESH_MARGIN_SECONDS = 60


class TossApiError(Exception):
    """비밀 없는 토스 오류. error_code 는 공식 코드 기반 세부 분류다."""

    def __init__(self, provider_status: str, http_status: int | None = None,
                 error_code: str | None = None,
                 retry_after: int | None = None):
        self.provider_status = provider_status
        self.http_status = http_status
        self.error_code = error_code
        self.retry_after = retry_after
        detail = f"http={http_status}" if http_status is not None else "no-http"
        if error_code:
            detail += f", code={error_code}"
        super().__init__(f"Toss 오류: {provider_status} ({detail})")


def _oauth_error_code(payload: dict | None) -> str | None:
    if isinstance(payload, dict):
        code = payload.get("error")
        if isinstance(code, str):
            return code
    return None


def _api_error_code(payload: dict | None) -> str | None:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str):
                return code
    return None


class TossClient:
    def __init__(
        self,
        credentials,
        profile: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        generation_provider=None,
        clock=None,
        token_store=None,
    ) -> None:
        if profile not in _DESCRIPTOR.supported_profiles:
            raise ValueError(f"지원하지 않는 프로필: {profile}")
        self._client_id = credentials.get("client_id")
        self._client_secret = credentials.get("client_secret")
        self.profile = profile
        self._transport = BrokerHttpTransport(transport=transport)
        self._generation_provider = generation_provider or (lambda: 0)
        self._clock = clock or time.monotonic
        self._token_store = token_store
        self._fingerprint = _fingerprint({"client_id": self._client_id,
                                          "client_secret": self._client_secret})
        self._lock = asyncio.Lock()

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_generation: int | None = None

    def __repr__(self) -> str:  # noqa: D105
        return f"TossClient(profile={self.profile})"

    # --- 토큰 ---

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0
        self._token_generation = None

    def _token_valid(self) -> bool:
        if self._token is None:
            return False
        if self._token_generation != self._generation_provider():
            self._invalidate_token()
            return False
        remaining = self._token_expires_at - self._clock()
        return remaining > _REFRESH_MARGIN_SECONDS

    async def _issue_token(self) -> None:
        try:
            response = await self._transport.request(
                _DESCRIPTOR, "token", profile=self.profile,
                form_body={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                })
        except BrokerHttpError as exc:
            code = _oauth_error_code(exc.payload)
            if exc.provider_status == "rate_limited":
                raise TossApiError("rate_limited", exc.status_code,
                                   retry_after=exc.retry_after) from None
            if exc.status_code == 403 and code == "access_denied":
                # 공식 코드로 확인된 허용 IP 차단이다.
                raise TossApiError(
                    "permission_denied", 403,
                    error_code="ip_not_allowed") from None
            if exc.status_code in (400, 401, 403):
                raise TossApiError("credential_invalid", exc.status_code,
                                   error_code=code) from None
            raise TossApiError("provider_unavailable",
                               exc.status_code) from None

        payload = response.json()
        if not isinstance(payload, dict):
            raise TossApiError("source_parse_error", response.status_code)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 0.0
        if not token or expires_in <= 0:
            raise TossApiError("source_parse_error", response.status_code)

        self._token = token
        self._token_expires_at = self._clock() + expires_in
        if self._token_store is not None:
            self._token_store.save(
                "toss", self.profile, self._fingerprint,
                token, time.time() + expires_in)
        self._token_generation = self._generation_provider()

    def _load_shared_token(self) -> bool:
        """다른 프로세스가 받아 둔 유효한 토큰을 쓴다.

        실측(2026-08-28): 토스는 재발급하면 새 토큰을 주고 **이전 토큰을
        즉시 무효화**한다(401). 프로세스마다 새로 받으면 서로의 토큰을
        죽이므로, 공유 토큰이 있으면 반드시 그것을 쓴다. 저장된 만료는
        벽시계라 이 프로세스의 monotonic 기준으로 환산한다.
        """
        if self._token_store is None:
            return False
        got = self._token_store.load("toss", self.profile,
                                     self._fingerprint)
        if got is None:
            return False
        token, wall_expires_at = got
        self._token = token
        self._token_expires_at = self._clock() + (
            wall_expires_at - time.time())
        self._token_generation = self._generation_provider()
        return True

    async def _ensure_token(self) -> str:
        async with self._lock:
            if not self._token_valid() and \
                    not self._load_shared_token():
                await self._issue_token()
            assert self._token is not None
            return self._token

    # --- API 요청 ---

    async def request(self, endpoint_id: str, *,
                      params: dict | None = None) -> dict:
        """endpoint_id 로만 요청한다. 401이면 한 번만 재발급·재시도한다."""
        if _DESCRIPTOR.endpoint(endpoint_id) is None:
            from stock_mcp_server.market_data.broker_http import (
                EndpointNotAllowedError,
            )
            raise EndpointNotAllowedError(
                f"toss에 허용되지 않은 endpoint: {endpoint_id}")

        response = await self._send_once(endpoint_id, params)
        if response is None:  # 401
            self._invalidate_token()
            response = await self._send_once(endpoint_id, params)
            if response is None:
                raise TossApiError("authentication_failed", 401)
        payload = response.json()
        if not isinstance(payload, dict):
            raise TossApiError("source_parse_error", response.status_code)
        return payload

    async def _send_once(self, endpoint_id: str, params: dict | None):
        token = await self._ensure_token()
        headers = {"authorization": f"Bearer {token}"}
        try:
            return await self._transport.request(
                _DESCRIPTOR, endpoint_id, profile=self.profile,
                headers=headers, params=params or {})
        except BrokerHttpError as exc:
            if exc.status_code == 401:
                return None
            code = _api_error_code(exc.payload)
            if exc.provider_status == "rate_limited":
                raise TossApiError("rate_limited", exc.status_code,
                                   error_code=code,
                                   retry_after=exc.retry_after) from None
            if exc.status_code == 404:
                raise TossApiError("entity_not_found", 404,
                                   error_code=code) from None
            if exc.status_code == 403:
                raise TossApiError("permission_denied", 403,
                                   error_code=code) from None
            raise TossApiError("provider_unavailable", exc.status_code,
                               error_code=code) from None
