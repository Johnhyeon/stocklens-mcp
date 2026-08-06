"""Codex(TOML 기반 MCP 클라이언트) 등록 경로 단위 테스트.

`_configure_toml_target`이 tomlkit으로 round-trip 편집하는지(다른 mcp_servers.* 항목·
주석 보존, 우리 섹션만 갱신, 백업 생성)를 검증한다. 실제 uv/PATH 탐색에 의존하지 않게
`resolve_server_entry`를 monkeypatch한다.
"""

from __future__ import annotations

from unittest.mock import patch

import tomlkit

from stock_mcp_server import setup_claude


def test_get_codex_config_path_is_under_home_dot_codex():
    assert setup_claude.get_codex_config_path().parts[-2:] == (".codex", "config.toml")


def test_codex_added_to_targets_dict():
    assert "codex" in setup_claude.TARGETS
    path_func, label = setup_claude.TARGETS["codex"]
    assert path_func() == setup_claude.get_codex_config_path()
    assert label == "Codex CLI"


def test_configure_toml_target_preserves_other_entries_and_comments(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# a comment that must survive\n"
        "[mcp_servers.github]\n"
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n',
        encoding="utf-8",
    )

    with patch.object(setup_claude, "resolve_server_entry", return_value={"command": "/fake/stocklens"}):
        result = setup_claude._configure_toml_target(config_path, "Codex CLI", command="stocklens")

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert "# a comment that must survive" in config_path.read_text(encoding="utf-8")
    assert doc["mcp_servers"]["github"]["command"] == "npx"
    assert doc["mcp_servers"]["stocklens"]["command"] == "/fake/stocklens"
    assert result["config_path"] == str(config_path)
    assert result["backup_path"] == str(config_path.with_suffix(".toml.backup"))
    assert (tmp_path / "config.toml.backup").exists()


def test_configure_toml_target_creates_fresh_doc_when_file_missing(tmp_path):
    config_path = tmp_path / "codex" / "config.toml"
    with patch.object(setup_claude, "resolve_server_entry", return_value={"command": "/fake/stocklens"}):
        result = setup_claude._configure_toml_target(config_path, "Codex CLI", command="stocklens")

    assert result["backup_path"] is None
    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["stocklens"]["command"] == "/fake/stocklens"


def test_configure_one_target_dispatches_toml_by_suffix(tmp_path):
    config_path = tmp_path / "config.toml"
    with patch.object(setup_claude, "_configure_toml_target", return_value={"ok": "toml"}) as mock_toml:
        result = setup_claude._configure_one_target(config_path, "Codex CLI", command="stocklens")
    mock_toml.assert_called_once()
    assert result == {"ok": "toml"}


def test_target_choices_include_codex():
    parser = setup_claude._build_parser()
    target_action = next(a for a in parser._actions if a.dest == "target")
    assert "codex" in target_action.choices
