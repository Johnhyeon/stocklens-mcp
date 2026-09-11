"""증권사 접근 토큰의 프로세스 간 재사용 (2026-08-28 실측 기반).

## 왜 필요한가

토큰은 지금까지 프로세스 메모리에만 있었다. 그래서 StockLens 프로세스가
새로 뜰 때마다 (Claude Desktop 과 Claude Code 를 함께 쓰는 경우처럼)
같은 앱키로 토큰을 다시 발급받았다. 실측한 공급자별 결과:

| 공급자 | 수명 | 재발급 요청 시 |
|---|---|---|
| KIS  | 86,400초(24h) | **같은 토큰**을 돌려준다. 단 발급 엔드포인트가
|      |               | 1분 1회 제한이라 두 번째 프로세스가 403
|      |               | (EGW00133) 을 받는다 |
| 키움 | 절대 만료시각(약 24h) | **같은 토큰**을 돌려준다 |
| 토스 | 86,399초(24h) | **새 토큰을 주고 이전 토큰을 즉시 무효화**한다.
|      |               | 두 프로세스가 서로의 토큰을 죽인다 (401) |

즉 토큰은 24시간짜리 안정된 자격이고, 매 프로세스가 새로 받는 것은
불필요할 뿐 아니라 토스에서는 **다른 프로세스를 망가뜨린다**.

## 어디에 두는가

운영체제 보안 저장소(keyring)에만 둔다. API 키가 이미 있는 그곳이다.
파일·상태 파일·로그·지원 번들에는 어떤 형태로도 남기지 않는다.

## 키 구성

`profile` + 자격 증명 지문(fingerprint)으로 슬롯을 나눈다. 키를 바꾸면
지문이 바뀌어 옛 토큰을 자동으로 못 쓰게 된다. 지문은 자격 증명에서
파생한 단방향 값이고 원문을 복원할 수 없다 (같은 keyring 안에 원문이
이미 있으므로 노출이 늘지 않는다).

## 실패는 조용히 메모리 전용으로 되돌아간다

keyring 을 못 쓰는 환경에서도 동작이 멈추면 안 된다. 저장·조회 실패는
삼키고 예전처럼 프로세스 메모리만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import time

_SERVICE_PREFIX = "stocklens-broker-token"
# 만료 직전 토큰을 재사용하지 않는다 (요청 도중 만료 방지).
_SAFETY_MARGIN_SECONDS = 120


def credential_fingerprint(values: dict[str, str]) -> str:
    """자격 증명에서 파생한 단방향 지문. 키가 바뀌면 값이 바뀐다."""
    parts = [f"{k}={values[k]}" for k in sorted(values) if values.get(k)]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class TokenStore:
    """keyring 기반 토큰 캐시. 실패하면 없는 것처럼 동작한다."""

    def __init__(self, keyring_module=None, clock=None) -> None:
        if keyring_module is None:
            import keyring as keyring_module  # noqa: PLC0415
        self._keyring = keyring_module
        self._clock = clock or time.time

    @staticmethod
    def _service(provider: str) -> str:
        return f"{_SERVICE_PREFIX}-{provider}"

    @staticmethod
    def _username(profile: str, fingerprint: str) -> str:
        return f"{profile}:{fingerprint}"

    def load(self, provider: str, profile: str,
             fingerprint: str) -> tuple[str, float] | None:
        """유효한 토큰과 만료 시각. 없거나 만료가 가까우면 None."""
        try:
            raw = self._keyring.get_password(
                self._service(provider),
                self._username(profile, fingerprint))
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
            token = record["token"]
            expires_at = float(record["expires_at"])
        except (ValueError, KeyError, TypeError):
            return None
        if not token:
            return None
        if expires_at - self._clock() <= _SAFETY_MARGIN_SECONDS:
            return None
        return token, expires_at

    def save(self, provider: str, profile: str, fingerprint: str,
             token: str, expires_at: float) -> None:
        try:
            self._keyring.set_password(
                self._service(provider),
                self._username(profile, fingerprint),
                json.dumps({"token": token, "expires_at": expires_at,
                            "issued_at": self._clock()}))
        except Exception:  # noqa: BLE001
            # 저장 실패가 조회 실패로 이어지면 안 된다. 메모리로 계속 간다.
            return

    def delete(self, provider: str, profile: str,
               fingerprint: str) -> bool:
        """연결 해제·키 교체 때 토큰도 함께 지운다."""
        try:
            self._keyring.delete_password(
                self._service(provider),
                self._username(profile, fingerprint))
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete_provider(self, provider: str,
                        entries: "list[tuple[str, str]]") -> list[str]:
        """(profile, fingerprint) 목록을 한 번에 정리한다."""
        removed = []
        for profile, fingerprint in entries:
            if self.delete(provider, profile, fingerprint):
                removed.append(f"{provider}:{profile}")
        return removed

    def __repr__(self) -> str:  # 토큰이 로그에 새지 않게 고정 문자열
        return "TokenStore(<redacted>)"
