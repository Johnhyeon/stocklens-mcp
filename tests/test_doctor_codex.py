"""Codex(TOML 기반 MCP 클라이언트) 등록 상태를 diagnostics._registered_targets()가
실제로 감지하는지 검증 — StockLens.

실사용 중 발견된 문제 재현: setup_claude.py는 Phase C에서 --target codex를 지원하게
됐지만, diagnostics.py의 _registered_targets()는 여전히 claude-desktop/claude-code
(JSON)만 확인하고 있어서 실제로 Codex에 정상 등록해도 Manager 대시보드의
targets/체크박스에는 전혀 반영되지 않았다.
"""

from __future__ import annotations

from unittest.mock import patch

import tomlkit

from stock_mcp_server import diagnostics, setup_claude


def _empty_json_paths(tmp_path):
    return {
        "get_claude_desktop_config_path": tmp_path / "desktop.json",
        "get_claude_code_config_path": tmp_path / "code.json",
    }


def test_codex_target_included_when_registered(tmp_path):
    codex_path = tmp_path / "config.toml"
    fake_exe = tmp_path / "stocklens.exe"
    fake_exe.write_text("", encoding="utf-8")
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table()
    server = tomlkit.table()
    server["command"] = str(fake_exe)
    doc["mcp_servers"][setup_claude.SERVER_KEY] = server
    codex_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    with patch.object(setup_claude, "get_claude_desktop_config_path", return_value=tmp_path / "no-desktop.json"), \
         patch.object(setup_claude, "get_claude_code_config_path", return_value=tmp_path / "no-code.json"), \
         patch.object(setup_claude, "get_codex_config_path", return_value=codex_path):
        targets = diagnostics._registered_targets()

    assert targets == ["codex"]


def test_codex_absent_when_config_file_missing(tmp_path):
    with patch.object(setup_claude, "get_claude_desktop_config_path", return_value=tmp_path / "no-desktop.json"), \
         patch.object(setup_claude, "get_claude_code_config_path", return_value=tmp_path / "no-code.json"), \
         patch.object(setup_claude, "get_codex_config_path", return_value=tmp_path / "no-codex.toml"):
        targets = diagnostics._registered_targets()

    assert targets == []


def test_codex_absent_when_entry_missing_but_other_servers_present(tmp_path):
    """다른 MCP 서버가 codex에 등록돼 있어도, stocklens 자기 항목이 없으면 등록 안 된
    것으로 판정해야 한다(다른 서버 존재 여부에 낚이면 안 됨)."""
    codex_path = tmp_path / "config.toml"
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table()
    other = tomlkit.table()
    other["command"] = "npx"
    doc["mcp_servers"]["github"] = other
    codex_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    with patch.object(setup_claude, "get_claude_desktop_config_path", return_value=tmp_path / "no-desktop.json"), \
         patch.object(setup_claude, "get_claude_code_config_path", return_value=tmp_path / "no-code.json"), \
         patch.object(setup_claude, "get_codex_config_path", return_value=codex_path):
        targets = diagnostics._registered_targets()

    assert targets == []


def test_check_mcp_config_valid_labels_codex():
    result = diagnostics._check_mcp_config_valid(["codex"])
    assert result.status == "ok"
    assert "Codex CLI" in result.summary
