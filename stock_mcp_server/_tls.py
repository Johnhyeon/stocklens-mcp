"""TLS 신뢰 기준을 OS 인증서 저장소에 맞춘다 — 프로세스당 한 번.

파이썬 HTTP 클라이언트는 기본적으로 `certifi` 번들만 믿는다. OS 인증서 저장소를
보지 않는다. 그래서 백신이나 회사망 장비가 TLS를 가로채 자기 루트로 재서명하는
PC에서는, 브라우저는 멀쩡한데 우리만 이렇게 죽는다:

    ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer
    certificate

2026-08-13 문의에서 실제로 이 원인으로 조회가 100% 실패했다. 사용자는 "인터넷은
되는데 이것만 안 된다"고 말한다 — 그 말이 정확했다.

## 왜 두 갈래인가

1. **파이썬 ssl 을 쓰는 것** (httpx, requests, urllib3) → `truststore.inject_into_ssl()`
   OS 저장소로 실제 체인 검증을 한다. 브라우저와 같은 판단이다.

2. **libcurl 을 쓰는 것** (`curl_cffi` → yfinance, 즉 미국 주식 전 도구)
   파이썬 ssl 을 아예 안 타므로 1번이 안 먹는다. OS 루트를 PEM 으로 떠서
   `CURL_CA_BUNDLE`/`REQUESTS_CA_BUNDLE` 로 물린다(libcurl 이 이 변수를 본다).

1번만 하면 한국 시세는 되는데 미국 주식만 안 되는, 더 설명하기 어려운 상태가 된다.

## 규칙: 더해주기만 하고 빼앗지 않는다

2번의 PEM 은 반드시 **certifi 번들과 합쳐서** 만든다. Windows 루트 저장소는 필요할
때 받아오는 방식이라 갓 설치한 PC 에서는 몇 개밖에 안 들어 있을 수 있다. 그것만
물리면 지금 잘 되던 연결까지 깨진다. 합친 결과가 최소 개수에 못 미치면 아무것도
하지 않고 기존 동작으로 둔다.

검증을 끄는 선택지(`verify=False`)는 어디에도 없다 — 연결되는 것보다 상대가 진짜인
게 먼저다.
"""

from __future__ import annotations

import os
import ssl
import sys
import time
from pathlib import Path

# 서버 인증(serverAuth) 용도 OID. Windows 저장소에는 코드서명·이메일 전용 인증서도
# 섞여 있어서, 그것까지 담으면 번들만 커지고 얻는 게 없다.
_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"

_BUNDLE_NAME = "os-ca-bundle.pem"
# 합친 번들이 이보다 적으면 만들다 만 것으로 보고 쓰지 않는다. certifi 만으로도
# 100장을 훌쩍 넘으므로, 이 선에 걸린다는 건 certifi 를 못 읽었다는 뜻이다.
_MIN_CERTS = 50
_MAX_AGE_SEC = 24 * 3600  # 하루 지나면 다시 뜬다 — 루트가 새로 깔릴 수 있다

_applied = False


def apply() -> None:
    """프로세스당 한 번 적용. 어떤 경우에도 예외를 올리지 않는다.

    신뢰 범위를 넓히려다 서버가 안 뜨면 본말전도다. 실패하면 조용히 기존
    동작(certifi)으로 남는다.
    """
    global _applied
    if _applied:
        return
    _applied = True

    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass

    try:
        _bridge_native_tls()
    except Exception:
        pass


def _home() -> Path:
    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def _bridge_native_tls() -> None:
    """libcurl 계열이 OS 루트를 보게 한다."""
    # 사용자가 직접 지정했으면 존중한다. 회사에서 내려준 번들을 덮어쓰면 안 된다.
    if os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    path = _ensure_bundle()
    if path is None:
        return
    os.environ["CURL_CA_BUNDLE"] = str(path)
    os.environ["REQUESTS_CA_BUNDLE"] = str(path)


def _ensure_bundle() -> Path | None:
    dest = _home() / _BUNDLE_NAME
    try:
        if dest.is_file() and (time.time() - dest.stat().st_mtime) < _MAX_AGE_SEC:
            return dest
    except OSError:
        pass

    pem = _build_bundle()
    if pem is None:
        # 새로 못 만들었어도 쓸 만한 옛 번들이 있으면 그것을 쓴다.
        return dest if dest.is_file() else None

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(pem, encoding="ascii")
        # 원자적 교체 — 반쯤 쓰인 번들을 다른 프로세스가 읽으면 그 PC의 TLS가 통째로 깨진다.
        tmp.replace(dest)
        return dest
    except OSError:
        return dest if dest.is_file() else None


def _build_bundle() -> str | None:
    """certifi + OS 저장소. 하나라도 빠지면 기존보다 나빠질 수 있으므로 합쳐서 낸다."""
    blocks: list[str] = []
    try:
        import certifi

        blocks.append(Path(certifi.where()).read_text(encoding="ascii", errors="ignore"))
    except Exception:
        pass

    blocks.extend(_os_root_pems())

    text = "\n".join(b.strip() for b in blocks if b.strip()) + "\n"
    if text.count("-----BEGIN CERTIFICATE-----") < _MIN_CERTS:
        return None
    return text


def _os_root_pems() -> list[str]:
    """OS 인증서 저장소의 서버 인증용 인증서들.

    ROOT(루트)와 CA(중간) 둘 다 담는다 — 가로채기 제품이 중간 인증서를 쓰는 경우가
    있고, "unable to get local issuer certificate"는 중간이 없을 때도 난다.

    Windows 외 플랫폼은 빈 목록을 낸다. `ssl.enum_certificates` 가 Windows 전용이고,
    macOS/Linux 는 truststore 만으로 충분하다(libcurl 이 OS 기본 경로를 이미 본다).
    """
    if sys.platform != "win32":
        return []

    out: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            entries = ssl.enum_certificates(store)
        except Exception:
            continue
        for der, encoding, trust in entries:
            if encoding != "x509_asn":
                continue
            # trust 가 True 면 모든 용도, 집합이면 허용 OID 목록이다.
            if trust is not True and _SERVER_AUTH_OID not in (trust or ()):
                continue
            try:
                out.append(ssl.DER_cert_to_PEM_cert(der))
            except Exception:
                continue
    return out
