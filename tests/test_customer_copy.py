"""고객에게 보이는 문구에 터미널 명령·메일 주소·모르는 버튼이 다시 들어오지 않게 막는다.

고객이 보는 곳은 셋이다.
- Claude 답변 안: 도구 잠금 안내, 상태 도구 출력, 원인 불명·파싱 실패 안내
- LeetKit Manager 화면: 진단 JSON 의 checks[].summary / action
- Manager 활성화 창: 키를 넣다 실패했을 때의 reason

주 고객층(40~50대 비개발자)은 터미널 명령에서 멈춘다. 안내는 Manager 버튼 하나로 하고,
버튼 이름은 화면 글자 그대로 쓴다(DartLens·TelegramLens 도 같은 검사를 둔다).
details.lines 도 Manager 상세 창에 보인다. 온라인 확인(KR/US/증권사) 줄은 한국어로
시작하는지·URL 이 없는지 따로 검사한다. RECENT_TOOL_FAILURES 줄은 화면에 안 뜨고
[결과 복사]·지원 번들로만 가므로 지원용 형식 그대로 둔다.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest

from stock_mcp_server import diagnostics, licensing, server, status

FORBIDDEN = [
    re.compile(r"\b(stocklens|dartlens|telegramlens)-(activate|doctor|setup|login|broker)\b", re.I),
    re.compile(r"\buv (tool|pip|run)\b", re.I),
    re.compile(r"터미널"),
    re.compile(r"PowerShell", re.I),
    re.compile(r"@gmail\.com", re.I),
    re.compile(r"STOCKLENS_HOME|DARTLENS_HOME|TELEGRAMLENS_HOME", re.I),
]

# LeetKit Manager 화면에 지금 있는 버튼 이름 그대로(이름 변경은 대표 결정 대기 중).
ALLOWED_BUTTONS = {
    "활성화", "구매", "업데이트", "MCP 등록", "진단", "복구",
    "텔레그램 로그인", "증권사 연결", "지원 문의", "문제 해결",
}

_BUTTON_RE = re.compile(r"\[([^\[\]]+)\]")


def assert_customer_copy(text: str | None, where: str) -> None:
    if not text:
        return
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{where}: 금지 문구 {pattern.pattern!r}\n{text}"
    for name in _BUTTON_RE.findall(text):
        assert name in ALLOWED_BUTTONS, f"{where}: 화면에 없는 버튼 [{name}]\n{text}"


# ---------- 도구 잠금 안내 (Claude 답변 안) ----------


@pytest.mark.parametrize("reason", ["missing", "invalid", "expired", "revoked", "clock"])
def test_locked_message_for_every_reason(reason, monkeypatch):
    monkeypatch.setattr(licensing, "license_block_reason", lambda: reason)
    text = licensing.locked_message()
    assert_customer_copy(text, f"locked_message({reason})")
    assert "LeetKit Manager" in text


def test_lock_messages_point_to_the_right_button():
    assert "StockLens 카드에서 [활성화]" in licensing.LOCKED_MESSAGE
    assert "[구매]" in licensing.EXPIRED_MESSAGE and "[활성화]" in licensing.EXPIRED_MESSAGE
    assert "LeetKit Manager 상단 [지원 문의]" in licensing.REVOKED_MESSAGE
    assert "LeetKit Manager 상단 [지원 문의]" in licensing.CLOCK_MESSAGE


# ---------- Manager 활성화 창 reason ----------


def test_activation_reasons_are_customer_copy():
    for bad in ("abc", "STKL-NOT-A-REAL-KEY", ""):
        res = licensing.verify_key(bad)
        assert res["valid"] is False
        assert_customer_copy(res["reason"], f"verify_key({bad!r})")


# ---------- 상태 도구 (Claude 답변 안) ----------


@pytest.mark.parametrize("state", sorted(status._LICENSE_LABEL))
def test_status_output_for_every_license_state(state):
    snap = status.StatusSnapshot(
        package_version="0.0.0",
        license_status=state,
        kr_market_status="unknown",
        us_market_status="unknown",
        last_success_at=None,
        last_failure_at=None,
        last_failure_error_code=None,
        cache_writable=False,
        update_available=True,
        latest_version="9.9.9",
    )
    assert_customer_copy(status.format_status(snap), f"format_status({state})")


def test_status_tool_error_branch():
    with patch.object(server, "build_status", side_effect=RuntimeError("boom")):
        text = asyncio.run(server.stocklens_status())
    assert_customer_copy(text, "stocklens_status except")
    assert "[진단]" in text


def test_status_tool_docstring_does_not_ask_claude_for_terminal():
    # docstring 은 Claude 에게 주는 지시라 "터미널 명령은 안내하지 마세요"라는 말 자체는 있다.
    doc = server.stocklens_status.__doc__
    assert not FORBIDDEN[0].search(doc)
    assert "[진단]" in doc and "[지원 문의]" in doc


# ---------- 원인 불명·파싱 실패 (Claude 답변 안) ----------


def _run_wrapped(wrapper, exc):
    async def tool():
        raise exc

    with patch.object(server, "is_licensed", return_value=True):
        return asyncio.run(wrapper(tool)())


def test_unknown_error_does_not_blame_customer_input():
    text = _run_wrapped(server.safe_tool, RuntimeError("boom"))
    assert_customer_copy(text, "safe_tool 원인 불명")
    assert "종목코드가 올바른지" not in text
    assert "LeetKit Manager 상단 [지원 문의]" in text


def test_unknown_us_error_does_not_blame_ticker():
    text = _run_wrapped(server.safe_us_tool, RuntimeError("boom"))
    assert_customer_copy(text, "safe_us_tool 원인 불명")
    assert "티커가 올바른지" not in text


def test_parse_failure_has_update_then_support_lines():
    text = _run_wrapped(server.safe_tool, server.NaverParseError("구조 변경"))
    assert_customer_copy(text, "safe_tool 파싱 실패")
    assert "[업데이트]" in text and "[지원 문의]" in text
    assert "습니다" not in server._PARSE_FAIL_NEXT


def test_export_flow_parse_failure_has_next_steps():
    """수급 표 엑셀 내보내기만 두 번째 줄이 빠져 있었다."""
    with patch.object(server, "is_licensed", return_value=True), patch.object(
        server, "get_investor_flow", AsyncMock(side_effect=server.NaverParseError("구조 변경"))
    ), patch.object(server, "get_update_notice", AsyncMock(return_value="")):
        text = asyncio.run(server.export_to_excel("flow", "005930"))
    assert_customer_copy(text, "export_to_excel flow 파싱 실패")
    assert server._PARSE_FAIL_NEXT in text


# ---------- 진단 JSON (Manager 화면) ----------


def _assert_check(check: diagnostics.DiagnosticCheck, where: str) -> None:
    d = check.to_dict()
    assert_customer_copy(d["summary"], f"{where} summary")
    assert_customer_copy(d["action"], f"{where} action")
    assert_customer_copy(d["details"].get("impact"), f"{where} impact")


def test_error_catalog_entries():
    for code, entry in diagnostics.ERROR_CATALOG.items():
        for field in ("summary", "impact", "action"):
            assert_customer_copy(entry.get(field), f"ERROR_CATALOG[{code}].{field}")


@pytest.mark.parametrize("state", ["missing", "invalid", "expired", "revoked", "clock", "active"])
def test_license_check_for_every_state(state):
    check = diagnostics._check_license_active(diagnostics.LicenseSummary(status=state))
    _assert_check(check, f"LICENSE_ACTIVE({state})")
    if state != "active":
        assert check.fix, "막힌 상태에는 할 일이 있어야 한다"


def test_license_check_buttons():
    fix = {
        s: diagnostics._check_license_active(diagnostics.LicenseSummary(status=s)).fix
        for s in ("missing", "invalid", "expired", "revoked", "clock")
    }
    assert "[활성화]" in fix["missing"] and "[활성화]" in fix["invalid"]
    assert "[구매]" in fix["expired"]
    assert "[지원 문의]" in fix["revoked"]
    assert "[진단]" in fix["clock"]


def test_mcp_config_check():
    check = diagnostics._check_mcp_config_valid([])
    _assert_check(check, "MCP_CONFIG_VALID fail")
    assert "[MCP 등록]" in check.fix
    _assert_check(diagnostics._check_mcp_config_valid(["claude-desktop"]), "MCP_CONFIG_VALID ok")


def test_cache_check_failure():
    with patch("stock_mcp_server._excel.get_snapshot_dir", side_effect=PermissionError("denied")):
        check = diagnostics._check_cache_writable()
    assert check.status == "fail"
    _assert_check(check, "CACHE_WRITABLE fail")


def test_command_and_python_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)
    monkeypatch.setattr("stock_mcp_server.setup_claude._uv_tool_bin_dirs", lambda: [])
    monkeypatch.setattr(diagnostics.sysconfig, "get_paths", lambda: {"scripts": str(tmp_path)})
    _assert_check(diagnostics._check_command_available(), "COMMAND_AVAILABLE fail")

    (tmp_path / "stocklens.exe").write_text("", encoding="utf-8")
    warn = diagnostics._check_command_available()
    assert warn.status == "warn"
    _assert_check(warn, "COMMAND_AVAILABLE warn")

    monkeypatch.setattr(diagnostics, "MIN_PYTHON", (99, 0))
    _assert_check(diagnostics._check_python_supported(), "PYTHON_SUPPORTED fail")


def test_crash_and_skip_rows():
    def boom():
        raise RuntimeError("injected")

    crashed = diagnostics._safe_check(boom, fallback_id="X")
    _assert_check(crashed, "_safe_check")
    assert "RuntimeError" not in crashed.summary
    _assert_check(diagnostics._skip("KR_DATA_REACHABLE"), "_skip")


def test_update_check_failure():
    with patch("stock_mcp_server._update_check._fetch_latest", AsyncMock(side_effect=OSError("down"))):
        check, _latest = asyncio.run(diagnostics._check_update_check_reachable())
    assert check.status == "fail"
    _assert_check(check, "UPDATE_CHECK_REACHABLE fail")


def test_full_offline_report_without_license(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKLENS_HOME", str(tmp_path))
    monkeypatch.setenv("STOCKLENS_LICENSE_KEY", "")
    report = diagnostics.run_diagnostics(online=False).to_dict()
    for c in report["checks"]:
        assert_customer_copy(c["summary"], f"{c['id']} summary")
        assert_customer_copy(c["action"], f"{c['id']} action")


@pytest.mark.parametrize(
    "error_type,detail",
    [
        ("ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED]"),
        ("ReadTimeout", "timed out"),
        ("ConnectError", "[Errno 11001] getaddrinfo failed"),
        ("ConnectError", "Connection refused"),
        ("HTTPStatusError", "Client error '429 Too Many Requests'"),
        ("KisApiError", "KIS 오류: credential_invalid (http=401)"),
        ("NaverParseError", "구조 변경 가능성"),
        ("RuntimeError", "boom"),
    ],
)
def test_recent_tool_failures_warn_for_every_category(error_type, detail):
    # 원인 불명·시간 초과·연결 실패는 되풀이돼야 '주의'다 — 모든 분류의 주의 문구를 보려고 두 번씩.
    records = [
        {"timestamp": f"2026-09-17T10:00:0{i}", "tool": "get_flow", "error": error_type, "error_detail": detail}
        for i in range(2)
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "warn"
    _assert_check(check, f"RECENT_TOOL_FAILURES({error_type})")


# ---------- 온라인 진단 (Manager 화면) ----------
#
# KR/US(와 증권사) details 줄은 Manager 상세 창에 그대로 보인다. 줄 앞은 고객이 읽을
# 한국어, 예외 원문은 줄 끝 괄호 안에 짧게. URL·쿼리스트링은 들어가지 않는다.

_ONLINE_ERRORS = [
    ConnectionError("[SSL: CERTIFICATE_VERIFY_FAILED] https://api.stock.naver.com/x?y=1"),
    TimeoutError(),
    OSError("[Errno 11001] getaddrinfo failed"),
    ConnectionRefusedError("Connection refused"),
    RuntimeError("Client error '403 Forbidden' for url 'https://m.stock.naver.com/api'"),
    server.NaverParseError("구조 변경 가능성"),
    RuntimeError("boom"),
]


def _assert_readable_detail_lines(check: diagnostics.DiagnosticCheck, where: str) -> None:
    for line in check.detail:
        assert re.match(r"^[가-힣]", line), f"{where}: 한국어로 시작하지 않는 줄 {line!r}"
        assert "://" not in line and "?" not in line, f"{where}: URL 이 남은 줄 {line!r}"


@pytest.mark.parametrize("exc", _ONLINE_ERRORS, ids=lambda e: type(e).__name__)
def test_kr_and_us_failures_for_every_category(exc):
    async def fail():
        raise exc

    probes = tuple((name, fail, judge) for name, _f, judge in diagnostics._kr_probes())
    kr = asyncio.run(diagnostics._check_kr_data_reachable(probes))
    _assert_check(kr, f"KR_DATA_REACHABLE({type(exc).__name__})")
    _assert_readable_detail_lines(kr, "KR_DATA_REACHABLE")

    with patch("stock_mcp_server.yfinance_source.get_price", AsyncMock(side_effect=exc)):
        us = asyncio.run(diagnostics._check_us_data_reachable())
    _assert_check(us, f"US_DATA_REACHABLE({type(exc).__name__})")
    _assert_readable_detail_lines(us, "US_DATA_REACHABLE")


def test_broker_check_copy():
    from types import SimpleNamespace

    from stock_mcp_server.market_data.kis_client import KisApiError

    class Adapter:
        def __init__(self, exc=None):
            self.exc = exc

        async def fetch_bars(self, request):
            if self.exc:
                raise self.exc
            return SimpleNamespace(bars=[])

    class Runtime:
        def __init__(self, adapters):
            self.adapters = adapters

        def snapshot(self):
            caps = {p: {"connected": True, "kr_intraday": True, "us_intraday": False} for p in self.adapters}
            return SimpleNamespace(capabilities=lambda p: caps.get(p, {"connected": False}))

        def providers_for(self, market, source="auto", snapshot=None):
            return {source: self.adapters[source]} if source in self.adapters else {}

    for adapters in (
        {"kis": Adapter()},
        {"kis": Adapter(KisApiError("credential_invalid", 401)), "kiwoom": Adapter()},
        {"kis": Adapter(TimeoutError()), "kiwoom": Adapter(KisApiError("rate_limited", 429))},
    ):
        check = asyncio.run(diagnostics._check_broker_data_reachable(Runtime(adapters)))
        _assert_check(check, "BROKER_DATA_REACHABLE")
        _assert_readable_detail_lines(check, "BROKER_DATA_REACHABLE")
