"""실패 원인 분류 — 세 Lens 동일 사본 (StockLens·DartLens·TelegramLens).

`_result_meta.py` 처럼 세 저장소에 같은 내용으로 둔다. 한 곳을 고치면 나머지 둘도
같이 고친다. category 이름과 화면 문구가 Lens 마다 갈리면 LeetKit Manager 가 같은
원인을 Lens 마다 다른 말로 보여주게 된다.

입력은 예외 이름과 메시지 두 문자열뿐이다. metrics JSONL(`error`, `error_detail`)에는
예외 객체가 없고 이 둘만 남기 때문이다 — 진단 중에 잡은 예외도 같은 함수로 분류해야
"최근 조회 실패"와 "지금 연결 확인"이 같은 원인을 같은 이름으로 부른다.

판정 순서가 곧 우선순위다. TLS 가로채기는 연결 오류 안에 싸여 오는 경우가 많아서
connect 보다 먼저 본다(2026-08-13 문의: 원인은 인증서 가로채기, 겉으로는 ConnectError).
"""

from __future__ import annotations

import re

CATEGORIES: tuple[str, ...] = (
    "cancelled", "tls", "dns", "timeout", "blocked", "auth", "connect", "schema", "other",
)

_TLS_TYPES = ("sslerror", "sslcertverificationerror")
_TLS_TEXT = ("certificate_verify_failed", "certificate verify", "self signed", "self-signed")

_DNS_TEXT = ("getaddrinfo", "name or service not known", "nodename nor servname", "no address associated")
# Windows 소켓 오류 번호. 숫자만 따로 보면 종목코드·가격 안의 숫자와 헷갈리므로 앞뒤가 숫자가 아닐 때만.
_DNS_CODE = re.compile(r"(?<!\d)11001(?!\d)")

_TIMEOUT_TYPES = (
    "timeoutexception", "readtimeout", "connecttimeout", "pooltimeout", "writetimeout", "timeouterror",
)
_TIMEOUT_TEXT = ("timed out",)

_BLOCKED_TEXT = re.compile(
    r"403 forbidden|429 too many|http[ =]?429|451 unavailable|http[ =]?451| 451 |rate_limited",
)
_BLOCKED_TYPES = ("floodwaiterror",)

_AUTH_TEXT = re.compile(
    r"401 unauthorized|http[ =]?401|credential_invalid|authentication_failed|permission_denied",
)
_AUTH_TYPES = ("authkeyunregisterederror", "sessionrevokederror", "authkeyerror")

_CONNECT_TYPES = (
    "connecterror", "connectionrefusederror", "connectionreseterror", "connectionabortederror",
    "connectionerror", "remoteprotocolerror",
)
_CONNECT_TEXT = ("connection refused", "connection reset")
_CONNECT_CODE = re.compile(r"(?<!\d)(10061|10054)(?!\d)")

_SCHEMA_TYPES = ("keyerror", "indexerror", "jsondecodeerror")
_SCHEMA_TEXT = ("구조가 바뀌", "구조 변경", "expecting value", "source_parse_error")

# DART 응답 status. DartLens 의 DartApiError 는 "[012] 메시지" 모양으로 올라온다.
_DART_STATUS = re.compile(r"^\s*\[(\d{3})\]")
_DART_BLOCKED = ("012", "020")  # IP 차단, 요청 제한
_DART_AUTH = ("010", "011")  # 등록되지 않은 키, 사용할 수 없는 키


def classify_error(error_type: str | None, error_detail: str | None) -> str:
    """예외 이름과 메시지로 원인 category 하나를 고른다. 대소문자는 가리지 않는다."""
    t = (error_type or "").strip().lower()
    d = (error_detail or "").strip()
    dl = d.lower()

    if "cancellederror" in t:
        return "cancelled"
    if t in _TLS_TYPES or t.startswith("ssl") or any(k in dl for k in _TLS_TEXT):
        return "tls"
    if any(k in dl for k in _DNS_TEXT) or _DNS_CODE.search(dl) or t == "gaierror":
        return "dns"
    if t in _TIMEOUT_TYPES or "timeout" in t or any(k in dl for k in _TIMEOUT_TEXT):
        return "timeout"

    dart = _DART_STATUS.match(d) if t == "dartapierror" else None
    if t in _BLOCKED_TYPES or _BLOCKED_TEXT.search(f" {dl} ") or (dart and dart.group(1) in _DART_BLOCKED):
        return "blocked"
    if t in _AUTH_TYPES or _AUTH_TEXT.search(dl) or (dart and dart.group(1) in _DART_AUTH):
        return "auth"
    if t in _CONNECT_TYPES or any(k in dl for k in _CONNECT_TEXT) or _CONNECT_CODE.search(dl):
        return "connect"
    # 각 Lens 의 파싱 실패 전용 예외(NaverParseError 등)는 이름이 ParseError 로 끝난다.
    if t in _SCHEMA_TYPES or t.endswith("parseerror") or any(k in dl for k in _SCHEMA_TEXT):
        return "schema"
    return "other"


# ---------- LeetKit Manager 화면 문구 (진단 action) ----------
#
# 상황 한 문장 → 할 일 하나 → 그래도 같으면 [지원 문의]. 버튼 이름은 Manager 화면 글자
# 그대로다. {lens} 자리에 Lens 이름이 들어간다.
_ACTIONS: dict[str, str] = {
    "tls": (
        "백신이나 회사 보안 프로그램이 연결을 가로채고 있을 수 있어요. "
        "{lens} 카드의 [업데이트]를 확인하고, 그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "timeout": (
        "연결이 느려서 시간 안에 답을 못 받았어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "dns": (
        "인터넷 주소를 찾지 못했어요. 인터넷 연결을 확인한 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "connect": (
        "데이터 서버에 연결하지 못했어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "blocked": (
        "데이터 제공처가 요청을 막았어요. 잠시 뒤 다시 해보고, "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "schema": (
        "데이터 제공처 화면 구조가 바뀌었을 수 있어요. {lens} 카드의 [업데이트]를 확인하고, "
        "업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "other": (
        "{lens} 카드의 [업데이트]를 확인해 주세요. "
        "업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
}

# 인증 실패는 Lens 마다 다시 넣을 것이 다르다.
_AUTH_ACTIONS: dict[str, str] = {
    "StockLens": "StockLens 카드의 [증권사 연결]에서 연결을 다시 확인해 주세요.",
    "DartLens": "DartLens 카드의 [활성화]에서 DART 인증키를 다시 넣어주세요.",
    "TelegramLens": "TelegramLens 카드의 [텔레그램 로그인]을 다시 눌러주세요.",
}


def action_for(category: str, lens: str) -> str:
    """Manager 화면(진단 action)에 쓸 할 일 문장. 모르는 category 는 other 로 본다."""
    if category == "auth":
        return _AUTH_ACTIONS.get(lens) or _ACTIONS["other"].format(lens=lens)
    template = _ACTIONS.get(category) or _ACTIONS["other"]
    return template.format(lens=lens)
