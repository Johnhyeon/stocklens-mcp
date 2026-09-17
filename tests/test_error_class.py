"""_error_class 분류 테스트 — 세 Lens 에 같은 사본이 있다(표의 근거 문자열마다 1개 이상)."""

from __future__ import annotations

import pytest

from stock_mcp_server._error_class import CATEGORIES, action_for, classify_error

CASES = [
    # cancelled
    ("CancelledError", None, "cancelled"),
    # tls
    ("SSLError", "whatever", "tls"),
    ("SSLCertVerificationError", None, "tls"),
    ("ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "tls"),
    ("ConnectError", "certificate verify failed: unable to get local issuer", "tls"),
    ("ConnectError", "self signed certificate in certificate chain", "tls"),
    ("RuntimeError", "self-signed certificate", "tls"),
    # dns
    ("ConnectError", "[Errno 11001] getaddrinfo failed", "dns"),
    ("ConnectError", "[Errno -2] Name or service not known", "dns"),
    ("ConnectError", "nodename nor servname provided, or not known", "dns"),
    ("OSError", "[WinError 11001] 호스트를 찾을 수 없음", "dns"),
    ("ConnectError", "[Errno -5] No address associated with hostname", "dns"),
    # timeout
    ("TimeoutException", None, "timeout"),
    ("ReadTimeout", "", "timeout"),
    ("ConnectTimeout", None, "timeout"),
    ("PoolTimeout", None, "timeout"),
    ("TimeoutError", None, "timeout"),
    ("OSError", "The read operation timed out", "timeout"),
    # blocked
    ("HTTPStatusError", "Client error '403 Forbidden' for url 'https://x'", "blocked"),
    ("HTTPStatusError", "Client error '429 Too Many Requests' for url 'https://x'", "blocked"),
    ("HTTPStatusError", "Client error '451 Unavailable For Legal Reasons' for url", "blocked"),
    ("BrokerHttpError", "키움 오류 응답: HTTP 451 ", "blocked"),
    ("DartApiError", "[012] 접근할 수 없는 IP입니다.", "blocked"),
    ("DartApiError", "[020] 요청 제한을 초과하였습니다.", "blocked"),
    ("FloodWaitError", "A wait of 30 seconds is required", "blocked"),
    ("KisApiError", "KIS 오류: rate_limited (http=403)", "blocked"),
    # auth
    ("HTTPStatusError", "Client error '401 Unauthorized' for url", "auth"),
    ("AuthKeyUnregisteredError", None, "auth"),
    ("SessionRevokedError", None, "auth"),
    ("AuthKeyError", None, "auth"),
    ("DartApiError", "[010] 등록되지 않은 키입니다.", "auth"),
    ("DartApiError", "[011] 사용할 수 없는 키입니다.", "auth"),
    ("KisApiError", "KIS 오류: credential_invalid (http=401)", "auth"),
    ("KiwoomApiError", "Kiwoom 오류: authentication_failed (no-http)", "auth"),
    ("KisApiError", "KIS 오류: permission_denied (http=403)", "auth"),
    # connect
    ("ConnectError", "All connection attempts failed", "connect"),
    ("ConnectionRefusedError", None, "connect"),
    ("ConnectionResetError", None, "connect"),
    ("RemoteProtocolError", "Server disconnected without sending a response.", "connect"),
    ("OSError", "[WinError 10061] 대상 컴퓨터에서 연결을 거부했으므로", "connect"),
    ("OSError", "[WinError 10054] 현재 연결은 원격 호스트에 의해 강제로 끊겼습니다", "connect"),
    ("OSError", "Connection refused", "connect"),
    ("OSError", "Connection reset by peer", "connect"),
    # schema
    ("KeyError", "'price'", "schema"),
    ("IndexError", "list index out of range", "schema"),
    ("JSONDecodeError", "Expecting value: line 1 column 1 (char 0)", "schema"),
    ("NaverParseError", "시세: 네이버가 JSON 대신 다른 응답을 줬습니다", "schema"),
    ("RuntimeError", "페이지 구조가 바뀌었을 수 있어요", "schema"),
    ("ValueError", "Expecting value", "schema"),
    ("KisApiError", "KIS 오류: source_parse_error (http=200)", "schema"),
    # other
    ("RuntimeError", "boom", "other"),
    (None, None, "other"),
    ("DartApiError", "[013] 조회된 데이터가 없습니다.", "other"),
]


@pytest.mark.parametrize("error_type,detail,expected", CASES)
def test_classify(error_type, detail, expected):
    assert classify_error(error_type, detail) == expected


def test_case_insensitive():
    assert classify_error("sslerror", None) == "tls"
    assert classify_error("x", "CONNECTION REFUSED") == "connect"


def test_order_tls_before_connect_and_dns_before_timeout():
    assert classify_error("ConnectError", "SSL: CERTIFICATE_VERIFY_FAILED") == "tls"
    assert classify_error("ConnectTimeout", "getaddrinfo failed") == "dns"


def test_numbers_inside_codes_do_not_trigger():
    """종목코드·가격 안의 숫자로 dns·connect·blocked 가 잡히면 안 된다."""
    assert classify_error("RuntimeError", "code=110010 price=451,000") == "other"
    assert classify_error("RuntimeError", "1006100 rows") == "other"


def test_categories_list_is_the_shared_contract():
    assert CATEGORIES == (
        "cancelled", "tls", "dns", "timeout", "blocked", "auth", "connect", "schema", "other",
    )


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_category_has_an_action(category):
    text = action_for(category, "StockLens")
    assert text and "{lens}" not in text
    assert "[" in text


def test_auth_action_is_per_lens():
    assert "[증권사 연결]" in action_for("auth", "StockLens")
    assert "[활성화]" in action_for("auth", "DartLens")
    assert "[텔레그램 로그인]" in action_for("auth", "TelegramLens")
