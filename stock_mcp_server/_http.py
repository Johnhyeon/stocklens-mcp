"""공용 httpx.AsyncClient 싱글톤 + 동시 요청 제한 + 재시도.

매 요청마다 새 연결을 만드는 대신 keep-alive로 재사용한다.
TCP/TLS 핸드셰이크 비용을 제거해서 응답 속도를 2~3배 향상시킨다.

추가 안전장치:
- Semaphore로 동시 요청 수를 15개로 제한 (네이버 Rate Limit 회피)
- 타임아웃 8초 (빠른 실패, 이벤트 루프 블로킹 방지)
- 429/5xx 에러 시 지수 백오프 재시도 (최대 2회)
"""

import asyncio
import random
import ssl

import httpx

_ssl_ctx: "ssl.SSLContext | bool | None" = None


def _verify() -> "ssl.SSLContext | bool":
    """TLS 검증에 OS 인증서 저장소를 쓴다(브라우저·curl과 같은 기준).

    httpx 기본값은 certifi 번들만 믿는다. 그런데 백신이나 회사망 프록시가 TLS를
    가로채 자기 루트로 재서명하는 환경에서는, 그 루트가 Windows 저장소에는 있어도
    certifi 에는 없어서 전부 실패한다:

        ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local
        issuer certificate

    실제 문의(2026-08-13, 뉴질랜드 사용자)에서 이 원인으로 모든 조회가 죽었다.
    같은 PC의 브라우저는 멀쩡했고, 같은 파이썬을 터미널에서 직접 부르면 성공해서
    원인을 좁히는 데 한참 걸렸다. 사용자가 "인터넷은 되는데 이것만 안 된다"고
    말하는 전형이 이 모양이다.

    truststore 를 못 쓰면 기존 동작(certifi)으로 조용히 돌아간다. 검증을 끄는
    선택지는 두지 않는다 — 연결이 되는 것보다 상대가 진짜인 게 먼저다.
    """
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import truststore

            _ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _ssl_ctx = True
    return _ssl_ctx

_TIMEOUT = 8.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 동시 요청 상한 (네이버 봇 탐지 회피 + 이벤트 루프 보호)
_MAX_CONCURRENT = 15
_semaphore: asyncio.Semaphore | None = None

_client: httpx.AsyncClient | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def get_client() -> httpx.AsyncClient:
    """싱글톤 AsyncClient를 반환한다. 첫 호출 시 지연 초기화.

    주의: 이 클라이언트를 직접 `await client.get(...)` 하는 대신
    `fetch(url, ...)` 래퍼를 쓰면 Semaphore + 재시도가 적용된다.
    기존 코드 호환성을 위해 get_client()는 그대로 유지.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            verify=_verify(),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=30,  # Semaphore(15)보다 여유있게
                keepalive_expiry=30.0,
            ),
        )
    return _client


async def fetch(
    url: str,
    *,
    params: dict | None = None,
    max_retries: int = 2,
) -> httpx.Response:
    """안전한 GET 요청 래퍼.

    - Semaphore로 동시 요청 15개 제한
    - 429/5xx 시 지수 백오프 재시도 (최대 2회)
    - 타임아웃 8초
    """
    client = get_client()
    sem = _get_semaphore()

    last_exc: Exception | None = None

    async with sem:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(url, params=params)
                # 429 / 5xx 는 재시도 대상
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                        await asyncio.sleep(backoff)
                        continue
                return resp
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                if attempt < max_retries:
                    backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                    await asyncio.sleep(backoff)
                    continue
                raise

    if last_exc:
        raise last_exc
    raise RuntimeError("fetch failed without exception")


async def close_client() -> None:
    """프로세스 종료 시 호출. 현재는 server.py가 SIGTERM 시 호출."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
