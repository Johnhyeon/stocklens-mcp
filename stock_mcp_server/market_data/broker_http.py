"""공용 증권사 HTTP transport (1.0 설계 11.1 최소 권한).

- 호출자는 URL 을 만들 수 없다. descriptor 의 endpoint_id 로만 요청한다.
- host 는 프로필별 hosts_by_profile, path 는 endpoint 정의에서만 나온다.
  둘 다 descriptor 의 allowed_hosts / allowed_paths 안에 있어야 한다.
- https 강제, TLS 검증, redirect 미추종, 응답 크기 제한.
- 오류 문자열에 query, header, 응답 본문(비밀 반사 포함)을 싣지 않는다.
  어댑터가 오류 코드를 분류할 수 있게 파싱된 본문은 payload 속성으로만
  전달한다 (str/repr 미포함).
"""

from __future__ import annotations

from typing import Mapping

import httpx

_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# 명시적 접근(get_header)에서도 절대 내주지 않는 header.
_BLOCKED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "appkey", "appsecret", "app_key", "app_secret",
})


class EndpointNotAllowedError(Exception):
    """endpoint_id·host·path 가 허용 목록 밖이다. 요청 자체를 만들지 않는다."""


class BrokerHttpError(Exception):
    """비밀 없는 HTTP 오류. 어댑터 분류용 본문은 payload 속성으로만."""

    def __init__(self, provider_status: str, message: str,
                 status_code: int | None = None,
                 payload: dict | None = None,
                 retry_after: int | None = None):
        self.provider_status = provider_status
        self.status_code = status_code
        self.payload = payload
        self.retry_after = retry_after
        super().__init__(message)

    def __repr__(self) -> str:
        return (f"BrokerHttpError(provider_status={self.provider_status!r}, "
                f"status_code={self.status_code!r})")


class SafeHttpResponse:
    """header·본문을 repr 에 드러내지 않는 응답."""

    def __init__(self, status_code: int, payload: object,
                 headers: Mapping[str, str]):
        self.status_code = status_code
        self._payload = payload
        self._headers = {key.lower(): value for key, value in headers.items()}

    def json(self) -> object:
        return self._payload

    def get_header(self, name: str) -> str | None:
        lowered = name.lower()
        if lowered in _BLOCKED_HEADERS:
            return None
        return self._headers.get(lowered)

    def __repr__(self) -> str:
        return f"SafeHttpResponse(status={self.status_code})"

    __str__ = __repr__


def _status_for(code: int) -> str:
    if code == 429:
        return "rate_limited"
    if code in (401, 403):
        return "authentication_failed"
    return "provider_unavailable"


class BrokerHttpTransport:
    def __init__(self, transport: httpx.BaseTransport | None = None,
                 timeout: float = _TIMEOUT_SECONDS):
        self._transport = transport
        self._timeout = timeout

    async def request(
        self,
        descriptor,
        endpoint_id: str,
        *,
        profile: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Mapping | None = None,
        form_body: Mapping | None = None,
    ) -> SafeHttpResponse:
        spec = descriptor.endpoint(endpoint_id)
        if spec is None:
            raise EndpointNotAllowedError(
                f"{descriptor.provider_id}에 허용되지 않은 endpoint: "
                f"{endpoint_id}")
        host = descriptor.host_for_profile(profile)
        if host is None:
            raise EndpointNotAllowedError(
                f"{descriptor.provider_id}에 정의되지 않은 프로필 host: "
                f"{profile}")
        # 방어적 재검증: descriptor 가 손으로 조작돼도 allowlist 가 잡는다.
        if host not in descriptor.allowed_hosts:
            raise EndpointNotAllowedError(
                f"{descriptor.provider_id} 허용 host 밖입니다")
        if spec.path not in descriptor.allowed_paths:
            raise EndpointNotAllowedError(
                f"{descriptor.provider_id} 허용 path 밖입니다")

        url = f"https://{host}{spec.path}"
        label = f"{descriptor.provider_id}:{endpoint_id}"
        try:
            async with httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    timeout=self._timeout,
                    verify=True) as client:
                response = await client.request(
                    spec.method, url,
                    headers=dict(headers or {}),
                    params=dict(params or {}),
                    json=json_body,
                    data=dict(form_body) if form_body else None)
        except httpx.HTTPError as exc:
            # 원문 메시지에 URL·비밀이 섞일 수 있다. 형식명만 보고한다.
            raise BrokerHttpError(
                "provider_unavailable",
                f"{label} 요청 실패: {type(exc).__name__}") from None

        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise BrokerHttpError(
                "provider_unavailable",
                f"{label} 응답이 허용 크기를 초과했습니다")

        if 300 <= response.status_code < 400:
            # redirect 는 따라가지 않는다 (cross-host 탈취 방지).
            raise BrokerHttpError(
                "provider_unavailable",
                f"{label} 응답이 redirect({response.status_code})입니다. "
                "따라가지 않습니다", status_code=response.status_code)

        payload: object = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            retry_after = None
            raw_retry = response.headers.get("retry-after")
            if raw_retry and str(raw_retry).isdigit():
                retry_after = int(raw_retry)
            raise BrokerHttpError(
                _status_for(response.status_code),
                f"{label} 오류 응답: HTTP {response.status_code}",
                status_code=response.status_code,
                payload=payload if isinstance(payload, dict) else None,
                retry_after=retry_after)

        return SafeHttpResponse(
            response.status_code, payload, response.headers)
