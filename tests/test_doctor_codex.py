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


# ── 텍스트 doctor 경로 + auto 타겟 ──────────────────────────────────────────
# 위 테스트들은 JSON(diagnostics) 경로를 지킨다. 텍스트 doctor 와 `--target auto` 는
# 같은 사실을 따로 판정하고 있었고, 둘 다 codex 를 몰랐다 — ChatGPT 만 쓰는 사람이
# "등록 완료"를 보고도 도구가 없거나, 잘 되는데 "어디에도 등록 안 됨" FAIL 을 봤다.


def test_text_doctor_sees_codex_registration(tmp_path):
    from stock_mcp_server import doctor

    codex_path = tmp_path / "config.toml"
    fake_exe = tmp_path / "stocklens.exe"
    fake_exe.write_text("", encoding="utf-8")
    doc = tomlkit.document()
    servers = tomlkit.table()
    entry = tomlkit.table()
    entry["command"] = str(fake_exe)
    servers[setup_claude.SERVER_KEY] = entry
    doc["mcp_servers"] = servers
    codex_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    with patch.object(doctor, "get_codex_config_path", return_value=codex_path):
        check = doctor.check_config_codex()
    assert check.status == "ok"

    # Claude 쪽이 둘 다 없어도 codex 하나로 종합 판정이 통과해야 한다.
    missing = doctor.Check("Config — Claude Desktop")
    missing.status = "info-skip"
    summary = doctor.check_at_least_one_config(missing, missing, check)
    assert summary.status == "ok"


def test_text_doctor_fails_only_when_no_target_at_all():
    from stock_mcp_server import doctor

    missing = doctor.Check("Config — x")
    missing.status = "info-skip"
    summary = doctor.check_at_least_one_config(missing, missing, missing)
    assert summary.status == "fail"
    # 고칠 방법에 codex 가 빠져 있으면, ChatGPT 만 쓰는 사람은 갈 곳이 없다.
    assert "codex" in (summary.fix or "")


def test_auto_target_picks_codex_when_no_claude_present(tmp_path):
    """ChatGPT 만 쓰는 PC. 예전에는 무조건 claude-desktop 으로 떨어져서 없는 앱의
    설정 파일을 만들고 "완료"라고 말했다."""
    def which(name):
        return None if name == "claude" else str(tmp_path / "codex.exe")

    with patch.object(setup_claude.shutil, "which", side_effect=which), \
         patch.object(
             setup_claude, "get_claude_desktop_config_path",
             return_value=tmp_path / "nope" / "claude_desktop_config.json",
         ):
        assert setup_claude._resolve_targets("auto") == ["codex"]


def test_auto_target_keeps_claude_desktop_fallback_when_nothing_found(tmp_path):
    with patch.object(setup_claude.shutil, "which", return_value=None), \
         patch.object(
             setup_claude, "get_claude_desktop_config_path",
             return_value=tmp_path / "nope" / "claude_desktop_config.json",
         ), \
         patch.object(
             setup_claude, "get_codex_config_path",
             return_value=tmp_path / "nope" / ".codex" / "config.toml",
         ):
        assert setup_claude._resolve_targets("auto") == ["claude-desktop"]


def test_registered_command_prefers_uv_managed_binary(tmp_path):
    """설정 파일에 적히는 실행 파일은 uv 관리본이어야 한다.

    실기기에서 확인한 사고: 옛 `pip install` 잔재가 시스템 Python 의 Scripts 에 남아
    있으면 PATH 순서상 그게 먼저 잡혀서, 설정 파일에 옛 실행 파일 경로가 박힌다. 그러면
    Manager 로 최신 버전을 올려도 호스트 앱은 계속 옛 버전을 띄운다 — 실제로 이 PC 의
    Claude Desktop·Claude Code·ChatGPT 세 곳 모두 0.5.0 을 가리키고 있었다.
    """
    uv_bin = tmp_path / "uv" / "bin"
    uv_bin.mkdir(parents=True)
    uv_exe = uv_bin / "stocklens.exe"
    uv_exe.write_text("", encoding="utf-8")
    pip_exe = tmp_path / "Scripts" / "stocklens.exe"
    pip_exe.parent.mkdir(parents=True)
    pip_exe.write_text("", encoding="utf-8")

    with patch.object(setup_claude, "_uv_tool_bin_dirs", return_value=[uv_bin]), \
         patch.object(setup_claude.shutil, "which", return_value=str(pip_exe)):
        entry = setup_claude.resolve_server_entry("stocklens")
    assert entry["command"] == str(uv_exe)


def test_registered_command_falls_back_to_path_without_uv(tmp_path):
    """uv 없이 pip 로만 설치한 환경은 예전 그대로 동작해야 한다."""
    pip_exe = tmp_path / "Scripts" / "stocklens.exe"
    pip_exe.parent.mkdir(parents=True)
    pip_exe.write_text("", encoding="utf-8")
    with patch.object(setup_claude, "_uv_tool_bin_dirs", return_value=[]), \
         patch.object(setup_claude.shutil, "which", return_value=str(pip_exe)):
        entry = setup_claude.resolve_server_entry("stocklens")
    assert entry["command"] == str(pip_exe)
