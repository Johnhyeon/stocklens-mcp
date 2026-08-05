"""diagnostics.run_diagnostics()의 구조 계약 + stocklens-doctor --json CLI 계약 테스트.

Manager가 파싱하는 표면이므로: 체크 ID 9종이 항상 모두 존재하는지, 기본(오프라인)
진단이 네트워크 없이 5초 이내 끝나는지, JSON이 항상 json.loads 가능한지,
라이선스 미활성 상태를 'ok'로 잘못 표시하지 않는지를 확인한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_mcp_server import diagnostics

ALL_CHECK_IDS = {
    "PACKAGE_IMPORTABLE",
    "COMMAND_AVAILABLE",
    "PYTHON_SUPPORTED",
    "MCP_CONFIG_VALID",
    "LICENSE_ACTIVE",
    "CACHE_WRITABLE",
    "KR_DATA_REACHABLE",
    "US_DATA_REACHABLE",
    "UPDATE_CHECK_REACHABLE",
}
ONLINE_ONLY_IDS = {"KR_DATA_REACHABLE", "US_DATA_REACHABLE", "UPDATE_CHECK_REACHABLE"}


class RunDiagnosticsOfflineTests(unittest.TestCase):
    def test_all_check_ids_present_and_online_checks_skipped(self) -> None:
        start = time.monotonic()
        report = diagnostics.run_diagnostics(online=False)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 5.0, "offline diagnostics must finish within 5s")
        self.assertFalse(report.online)

        ids = {c.id for c in report.checks}
        self.assertEqual(ids, ALL_CHECK_IDS)

        for c in report.checks:
            if c.id in ONLINE_ONLY_IDS:
                self.assertEqual(c.status, "skip")

    def test_report_is_json_serializable(self) -> None:
        report = diagnostics.run_diagnostics(online=False)
        data = json.loads(report.to_json())

        self.assertIn("schema_version", data)
        self.assertIn("overall", data)
        self.assertIn("license", data)
        self.assertIn("targets", data)
        self.assertIn("checks", data)
        self.assertEqual(len(data["checks"]), len(ALL_CHECK_IDS))

    def test_never_raises_even_when_home_dir_is_unwritable_like(self) -> None:
        # offline 진단은 traceback으로 죽지 않아야 한다(acceptance criterion).
        try:
            diagnostics.run_diagnostics(online=False)
        except Exception as e:  # pragma: no cover - 실패하면 즉시 표면화
            self.fail(f"run_diagnostics(online=False) raised: {e!r}")


class UnlicensedReportTests(unittest.TestCase):
    def test_missing_license_marks_report_as_fail_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"STOCKLENS_HOME": tmp, "STOCKLENS_LICENSE_KEY": ""}):
                report = diagnostics.run_diagnostics(online=False)

        license_check = next(c for c in report.checks if c.id == "LICENSE_ACTIVE")
        self.assertEqual(license_check.status, "fail")
        self.assertEqual(license_check.error_code, "STOCKLENS_LICENSE_MISSING")
        self.assertEqual(report.license.status, "missing")
        # 활성화되지 않은 설치를 report.overall == 'ok'로 표시하면 안 된다.
        self.assertNotEqual(report.overall, "ok")
        self.assertFalse(report.ok)


class DoctorJsonCliTests(unittest.TestCase):
    def _run_doctor(self, *extra_args: str, home: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["STOCKLENS_HOME"] = home
        env["STOCKLENS_LICENSE_KEY"] = ""
        return subprocess.run(
            [sys.executable, "-m", "stock_mcp_server.doctor", "--json", *extra_args],
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env=env,
        )

    def test_json_flag_stdout_is_parseable_and_offline_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            proc = self._run_doctor(home=tmp)
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, 8.0)
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

        data = json.loads(proc.stdout)
        self.assertFalse(data["online"])
        self.assertEqual({c["id"] for c in data["checks"]}, ALL_CHECK_IDS)
        # exit code는 report.overall == 'fail'일 때만 1이어야 한다.
        self.assertEqual(proc.returncode, 0 if data["overall"] != "fail" else 1)


if __name__ == "__main__":
    unittest.main()
