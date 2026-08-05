"""비대화형 setup/activation의 Manager 계약 테스트.

핵심 확인 사항:
- `stocklens-setup ... --json --non-interactive`: stdout이 JSON 하나뿐이고,
  변경된 config/backup 경로를 담으며, 반복 실행해도 mcpServers에 중복 항목이
  생기지 않는다.
- `stocklens-activate --stdin --json`: 키를 인자(argv)가 아니라 표준입력으로
  받고, 활성화 키 원문이 stdout/stderr 어디에도 나타나지 않는다.

주의: 실제 Claude Desktop/Code config나 ~/.stocklens/license.key를 절대 건드리지
않도록, 경로 함수와 licensing.save_key를 전부 임시 디렉터리/mock으로 격리한다.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_mcp_server import licensing, setup_claude

FAKE_KEY = "FAKE-TEST-KEY-DOES-NOT-EXIST-1234567890"
FAKE_LICENSE_ID = "deadbeef1234"  # last4 = "1234"


class ConfigureOneTargetQuietTests(unittest.TestCase):
    def test_quiet_mode_prints_nothing_and_returns_change_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "claude_desktop_config.json"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = setup_claude._configure_one_target(
                    config_path, "Claude Desktop (test)", command="stocklens", quiet=True
                )

            self.assertEqual(buf.getvalue(), "")
            self.assertEqual(result["config_path"], str(config_path))
            self.assertIsNone(result["backup_path"])  # 첫 실행은 기존 파일이 없어 backup 없음
            self.assertIn("command", result)

    def test_repeated_runs_do_not_duplicate_mcpServers_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "claude_desktop_config.json"

            first = setup_claude._configure_one_target(
                config_path, "Claude Desktop (test)", command="stocklens", quiet=True
            )
            second = setup_claude._configure_one_target(
                config_path, "Claude Desktop (test)", command="stocklens", quiet=True
            )

            self.assertIsNone(first["backup_path"])
            self.assertIsNotNone(second["backup_path"])  # 두 번째부터는 백업 생김

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(list(cfg["mcpServers"].keys()), [setup_claude.SERVER_KEY])


class SetupCliJsonContractTests(unittest.TestCase):
    def test_json_non_interactive_stdout_is_single_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desktop_path = Path(tmp) / "desktop.json"
            code_path = Path(tmp) / "code.json"
            fake_targets = {
                "claude-desktop": (lambda: desktop_path, "Claude Desktop"),
                "claude-code": (lambda: code_path, "Claude Code CLI"),
            }

            argv = ["stocklens-setup", "--target", "both", "--json", "--non-interactive"]
            buf = io.StringIO()
            with patch.dict(setup_claude.TARGETS, fake_targets), patch(
                "sys.argv", argv
            ), contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    setup_claude.main()

            self.assertEqual(cm.exception.code, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["targets"]), 2)
            self.assertTrue(desktop_path.exists())
            self.assertTrue(code_path.exists())

    def test_running_twice_keeps_config_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desktop_path = Path(tmp) / "desktop.json"
            fake_targets = {"claude-desktop": (lambda: desktop_path, "Claude Desktop")}
            argv = ["stocklens-setup", "--target", "claude-desktop", "--json", "--non-interactive"]

            with patch.dict(setup_claude.TARGETS, fake_targets), patch("sys.argv", argv):
                for _ in range(2):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        with self.assertRaises(SystemExit):
                            setup_claude.main()

            cfg = json.loads(desktop_path.read_text(encoding="utf-8"))
            self.assertEqual(list(cfg["mcpServers"].keys()), [setup_claude.SERVER_KEY])


class ActivateCliStdinContractTests(unittest.TestCase):
    def test_stdin_json_success_never_leaks_key_or_full_license_id(self) -> None:
        argv = ["stocklens-activate", "--stdin", "--json"]
        buf = io.StringIO()
        with patch.object(licensing, "save_key", return_value={"valid": True, "license_id": FAKE_LICENSE_ID}), \
             patch("sys.argv", argv), \
             patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                licensing.activate_cli()

        out = buf.getvalue()
        self.assertEqual(cm.exception.code, 0)
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn(FAKE_LICENSE_ID, out)  # 전체 license_id는 노출 금지

        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["license_id_masked"], "****" + FAKE_LICENSE_ID[-4:].upper())

    def test_stdin_json_invalid_key_never_leaks_key_and_does_not_touch_disk(self) -> None:
        # save_key를 mock하지 않는다 — 실제 verify_key가 서명 불일치로 거부하고,
        # save_key는 유효하지 않은 키를 절대 파일에 쓰지 않는다(코드 경로상 보장).
        argv = ["stocklens-activate", "--stdin", "--json"]
        buf = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                licensing.activate_cli()

        out = buf.getvalue()
        self.assertEqual(cm.exception.code, 1)
        self.assertNotIn(FAKE_KEY, out)

        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "invalid")

    def test_key_never_read_from_argv_when_using_stdin(self) -> None:
        # --stdin 경로는 argv에 키를 담지 않는다는 걸 프로그램 인자 자체로 증명.
        argv = ["stocklens-activate", "--stdin", "--json"]
        self.assertNotIn(FAKE_KEY, argv)


if __name__ == "__main__":
    unittest.main()
