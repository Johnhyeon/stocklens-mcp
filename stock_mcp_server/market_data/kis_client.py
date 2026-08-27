"""KIS(한국투자증권) Open API HTTP·토큰 클라이언트.

- 접근 토큰은 프로세스 메모리에만 둔다. 파일·상태·캐시에 저장하지 않는다.
- connection generation 이 바뀌면 토큰을 폐기하고 새로 발급받는다.
- 오류에는 provider_status 코드와 HTTP 상태만 싣는다. App Key·App Secret·
  토큰·응답 본문 원문을 예외, repr, 로그에 싣지 않는다.
"""

from __future__ import annotations

import time

import httpx

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
DEMO_BASE_URL = "https://openapivts.koreainvestment.com:29443"

_TOKEN_PATH = "/oauth2/tokenP"

# 만료 전 여유 마진(초). 남은 수명이 이보다 짧으면 재발급한다.
_REFRESH_MARGIN_SECONDS = 60

_TIMEOUT_SECONDS = 10.0


class KisApiError(Exception):
    """비밀 없는 KIS 오류. provider_status 는 설계 27절 값만 사용한다."""

    def __init__(self, provider_status: str, http_status: int | None = None):
        self.provider_status = provider_status
        self.http_status = http_status
        detail = f"http={http_status}" if http_status is not None else "no-http"
        super().__init__(f"KIS 오류: {provider_status} ({detail})")


class KisClient:
    def __init__(
        self,
        credentials: BrokerCredentials,
        profile: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        generation_provider=None,
        clock=None,
    ) -> None:
        if profile not in ("real", "demo"):
            raise ValueError(f"지원하지 않는 프로필: {profile}")
        self._credentials = credentials
        self.profile = profile
        self.base_url = REAL_BASE_URL if profile == "real" else DEMO_BASE_URL
        self._transport = transport
        self._generation_provider = generation_provider or (lambda: 0)
        self._clock = clock or time.monotonic

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_generation: int | None = None

    def __repr__(self) -> str:  # noqa: D105
        return f"KisClient(profile={self.profile}, base_url={self.base_url})"

    # --- 토큰 ---

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0
        self._token_generation = None

    def _token_valid(self) -> bool:
        if self._token is None:
            return False
        current_gen = self._generation_provider()
        if self._token_generation != current_gen:
            # Manager 가 연결을 바꿨다. 메모리 토큰을 폐기한다.
            self._invalidate_token()
            return False
        remaining = self._token_expires_at - self._clock()
        return remaining > _REFRESH_MARGIN_SECONDS

    async def _issue_token(self, http: httpx.AsyncClient) -> None:
        try:
            resp = await http.post(
                self.base_url + _TOKEN_PATH,
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._credentials.app_key,
                    "appsecret": self._credentials.app_secret,
                },
            )
        except httpx.TimeoutException:
            raise KisApiError("provider_unavailable") from None
        except httpx.HTTPError:
            raise KisApiError("provider_unavailable") from None

        if resp.status_code in (400, 401, 403):
            # KIS 는 토큰 발급을 1분당 1회로 제한하고 403 + EGW00133 을
            # 돌려준다 (2026-08-27 실측). 이 경우는 키 문제가 아니다.
            if self._token_error_code(resp) == "EGW00133":
                raise KisApiError("rate_limited", resp.status_code)
            raise KisApiError("credential_invalid", resp.status_code)
        if resp.status_code == 429:
            raise KisApiError("rate_limited", resp.status_code)
        if resp.status_code != 200:
            raise KisApiError("provider_unavailable", resp.status_code)
        try:
            payload = resp.json()
            token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 0))
        except (ValueError, KeyError, TypeError):
            raise KisApiError("source_parse_error", resp.status_code) from None
        if not token or expires_in <= 0:
            raise KisApiError("source_parse_error", resp.status_code)

        self._token = token
        self._token_expires_at = self._clock() + expires_in
        self._token_generation = self._generation_provider()

    @staticmethod
    def _token_error_code(resp: httpx.Response) -> str | None:
        """토큰 오류 응답의 코드만 뽑는다. 본문 원문은 어디에도 싣지 않는다."""
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        code = payload.get("error_code") or payload.get("msg_cd")
        return code if isinstance(code, str) else None

    async def _ensure_token(self, http: httpx.AsyncClient) -> str:
        if not self._token_valid():
            await self._issue_token(http)
        assert self._token is not None
        return self._token

    # --- API 요청 ---

    async def request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        """토큰을 붙여 요청 하나를 보낸다. 401이면 한 번만 재발급·재시도한다."""
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT_SECONDS
        ) as http:
            resp = await self._send_once(http, method, path, tr_id,
                                         params, headers)
            if resp.status_code == 401:
                self._invalidate_token()
                resp = await self._send_once(http, method, path, tr_id,
                                             params, headers)
                if resp.status_code == 401:
                    raise KisApiError("authentication_failed", 401)
            return self._parse_response(resp)

    async def _send_once(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        tr_id: str,
        params: dict | None,
        headers: dict | None,
    ) -> httpx.Response:
        token = await self._ensure_token(http)
        request_headers = {
            "authorization": f"Bearer {token}",
            "appkey": self._credentials.app_key,
            "appsecret": self._credentials.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if headers:
            request_headers.update(headers)
        try:
            return await http.request(
                method, self.base_url + path,
                params=params, headers=request_headers)
        except httpx.TimeoutException:
            raise KisApiError("provider_unavailable") from None
        except httpx.HTTPError:
            raise KisApiError("provider_unavailable") from None

    def _parse_response(self, resp: httpx.Response) -> dict:
        if resp.status_code == 403:
            raise KisApiError("permission_denied", 403)
        if resp.status_code == 429:
            raise KisApiError("rate_limited", 429)
        if resp.status_code != 200:
            raise KisApiError("provider_unavailable", resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            raise KisApiError("source_parse_error", resp.status_code) from None
        if not isinstance(payload, dict):
            raise KisApiError("source_parse_error", resp.status_code)
        return payload
