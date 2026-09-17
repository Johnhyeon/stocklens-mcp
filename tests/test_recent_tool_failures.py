"""RECENT_TOOL_FAILURES 판정 테스트 — 세 Lens 가 같은 규칙으로 판정한다.

기록 없음 / 전부 성공 / 실패 후 같은 도구 성공 / 마지막이 실패(warn) / 취소만.
metrics 파일을 실제로 읽지 않도록 기록을 직접 넘기거나 load_metrics 를 바꿔 끼운다.
"""

from __future__ import annotations

import json

from stock_mcp_server import diagnostics


def _rec(tool: str, ts: str, error: str | None = None, detail: str | None = None) -> dict:
    return {"timestamp": ts, "tool": tool, "error": error, "error_detail": detail}


def test_no_records_is_ok():
    check = diagnostics._check_recent_tool_failures([])
    assert check.id == "RECENT_TOOL_FAILURES"
    assert check.status == "ok"
    assert check.critical is False
    assert check.summary == "최근 이틀 동안 AI 앱이 StockLens를 쓴 기록이 없어요."


def test_all_success_is_ok():
    records = [_rec("get_price", "2026-09-17T10:00:00"), _rec("get_chart", "2026-09-17T10:01:00")]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "ok"
    assert check.summary == "최근 이틀 동안 조회 2번이 모두 정상이었어요."
    assert check.detail == []


def test_failure_then_same_tool_success_is_resolved():
    records = [
        _rec("get_flow", "2026-09-17T09:00:00", "ReadTimeout", "timed out"),
        _rec("get_flow", "2026-09-17T09:05:00"),
        _rec("get_price", "2026-09-17T09:06:00"),
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "ok"
    assert check.summary == "최근 이틀 동안 조회 3번 중 1번이 실패했지만, 계속 실패하고 있지는 않아요."
    assert check.error_code is None
    assert check.detail and check.detail[0].startswith("get_flow: 실패 1번, 마지막 09:00, 분류 timeout, ReadTimeout")


def test_last_call_failed_is_warn_with_category_action():
    records = [
        _rec("get_flow", "2026-09-17T09:00:00"),
        _rec("get_flow", "2026-09-17T11:00:00", "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        _rec("get_price", "2026-09-17T11:01:00", "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED]"),
        _rec("get_chart", "2026-09-17T11:02:00"),
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "warn"
    assert check.critical is False
    assert check.summary == "최근 조회 중 아직 실패로 남아 있는 것이 2가지 있어요."
    assert check.error_code == "RECENT_TOOL_FAILURES_TLS"
    assert "[업데이트]" in check.fix and "[지원 문의]" in check.fix
    d = check.to_dict()
    assert d["details"]["impact"]
    assert len(d["details"]["lines"]) == 2


def test_single_unclear_failure_is_not_warn_but_repeat_is():
    """AI 앱이 인자를 한 번 잘못 넣은 호출로 카드가 이틀 내내 '주의'가 되면 안 된다.
    다시 불러도 또 실패하면 그때는 진짜 결함으로 본다."""
    once = [_rec("list_themes", "2026-09-17T09:00:00", "ValueError", "page는 1 이상이어야 해요")]
    check = diagnostics._check_recent_tool_failures(once)
    assert check.status == "ok"
    assert check.detail and check.detail[0].startswith("list_themes: 실패 1번")

    twice = once + [_rec("list_themes", "2026-09-17T09:00:20", "ValueError", "page는 1 이상이어야 해요")]
    check = diagnostics._check_recent_tool_failures(twice)
    assert check.status == "warn"
    assert check.error_code == "RECENT_TOOL_FAILURES_OTHER"


def test_other_tool_success_does_not_resolve_a_failure():
    records = [
        _rec("get_flow", "2026-09-17T09:00:00", "NaverParseError", "수급 표: 구조 변경 가능성"),
        _rec("get_price", "2026-09-17T09:05:00"),
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "warn"
    assert check.error_code == "RECENT_TOOL_FAILURES_SCHEMA"


def test_cancelled_only_is_not_a_failure_but_kept_in_details():
    records = [
        _rec("get_financial_batch", "2026-09-17T09:00:00", "CancelledError"),
        _rec("get_financial_batch", "2026-09-17T09:01:00", "CancelledError"),
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "ok"
    assert check.summary == "최근 이틀 동안 조회 2번이 모두 정상이었어요."
    assert any("cancelled" in line for line in check.detail)


def test_cancel_after_failure_does_not_hide_the_failure():
    records = [
        _rec("get_flow", "2026-09-17T09:00:00", "ConnectError", "Connection refused"),
        _rec("get_flow", "2026-09-17T09:00:30", "ConnectError", "Connection refused"),
        _rec("get_flow", "2026-09-17T09:01:00", "CancelledError"),
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert check.status == "warn"
    assert check.error_code == "RECENT_TOOL_FAILURES_CONNECT"


def test_details_are_capped_and_hide_secrets():
    secret = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    records = [
        _rec(f"tool_{i}", f"2026-09-17T10:{i:02d}:00", "RuntimeError",
             f"bad https://api.example.com/x?appkey={secret} token {secret}")
        for i in range(12)
    ]
    check = diagnostics._check_recent_tool_failures(records)
    assert len(check.detail) == 8
    blob = json.dumps(check.to_dict(), ensure_ascii=False)
    assert secret not in blob
    assert "appkey" not in blob
    # 가장 최근 실패가 먼저 온다.
    assert check.detail[0].startswith("tool_11:")


def test_offline_report_includes_the_check(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKLENS_HOME", str(tmp_path))
    monkeypatch.setattr(
        "stock_mcp_server._metrics.load_metrics",
        lambda days=1: [
            _rec("get_price", "2026-09-17T10:00:00", "ReadTimeout", "timed out"),
            _rec("get_price", "2026-09-17T10:00:30", "ReadTimeout", "timed out"),
        ],
    )
    report = diagnostics.run_diagnostics(online=False)
    check = next(c for c in report.checks if c.id == "RECENT_TOOL_FAILURES")
    assert check.status == "warn"
    assert check.error_code == "RECENT_TOOL_FAILURES_TIMEOUT"


def test_crash_while_reading_metrics_does_not_break_report(monkeypatch):
    def boom(days=1):
        raise OSError("disk")

    monkeypatch.setattr("stock_mcp_server._metrics.load_metrics", boom)
    report = diagnostics.run_diagnostics(online=False)
    check = next(c for c in report.checks if c.id == "RECENT_TOOL_FAILURES")
    assert check.status == "fail"
    assert check.critical is False
