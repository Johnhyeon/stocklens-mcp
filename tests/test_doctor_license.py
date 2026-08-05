"""라이선스 진단 테스트 — missing/invalid/active 3가지 분기.

diagnostics.py는 `_license_summary()`(licensing 조회 → LicenseSummary)와
`_check_license_active(summary)`(LicenseSummary → DiagnosticCheck)로 나뉘어
있다(Manager 공통 계약의 top-level `license` 필드와 `checks[]`의 LICENSE_ACTIVE
항목이 같은 판정을 공유하기 위함). 두 함수를 각각 검증하고, 어느 경로로도
raw key 원문이 새지 않는지, license_id는 마스킹된 형태로만 노출되는지 확인한다.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from stock_mcp_server import diagnostics, doctor, licensing

FAKE_RAW_KEY = "STKL-DO-NOT-LEAK-THIS-RAW-KEY-VALUE"
FAKE_LICENSE_ID = "abcd1234ef56"  # last4 = "ef56" -> masked "****EF56"


def _flatten_check_text(check: diagnostics.DiagnosticCheck) -> str:
    d = check.to_dict()
    details = d.get("details") or {}
    return " ".join(
        str(v)
        for v in (
            d.get("summary", ""),
            d.get("action", ""),
            *details.get("lines", []),
            details.get("error_code", ""),
        )
    )


class LicenseSummaryTests(unittest.TestCase):
    def test_missing_key(self) -> None:
        with patch.object(licensing, "stored_key", return_value=None):
            summary = diagnostics._license_summary()
        self.assertEqual(summary.status, "missing")
        self.assertIsNone(summary.license_id_masked)

    def test_invalid_key_never_leaks_raw_key(self) -> None:
        with patch.object(licensing, "stored_key", return_value=FAKE_RAW_KEY), patch.object(
            licensing, "verify_key", return_value={"valid": False, "reason": "서명 불일치(위조/변조)"}
        ):
            summary = diagnostics._license_summary()
        self.assertEqual(summary.status, "invalid")
        self.assertNotIn(FAKE_RAW_KEY, str(summary.to_dict()))

    def test_active_key_masks_license_id(self) -> None:
        with patch.object(licensing, "stored_key", return_value=FAKE_RAW_KEY), patch.object(
            licensing, "verify_key", return_value={"valid": True, "license_id": FAKE_LICENSE_ID}
        ):
            summary = diagnostics._license_summary()
        self.assertEqual(summary.status, "active")
        blob = str(summary.to_dict())
        self.assertNotIn(FAKE_RAW_KEY, blob)
        self.assertNotIn(FAKE_LICENSE_ID, blob)  # 전체 license_id는 노출 금지
        self.assertEqual(summary.license_id_masked, "****" + FAKE_LICENSE_ID[-4:].upper())


class CheckLicenseActiveTests(unittest.TestCase):
    def test_missing_is_critical_fail(self) -> None:
        check = diagnostics._check_license_active(diagnostics.LicenseSummary(status="missing"))
        self.assertEqual(check.id, "LICENSE_ACTIVE")
        self.assertEqual(check.status, "fail")
        self.assertTrue(check.critical)
        self.assertEqual(check.error_code, "STOCKLENS_LICENSE_MISSING")

    def test_invalid_is_critical_fail(self) -> None:
        check = diagnostics._check_license_active(diagnostics.LicenseSummary(status="invalid"))
        self.assertEqual(check.status, "fail")
        self.assertTrue(check.critical)
        self.assertEqual(check.error_code, "STOCKLENS_LICENSE_INVALID")

    def test_active_exposes_only_masked_license_id(self) -> None:
        summary = diagnostics.LicenseSummary(status="active", license_id_masked="****EF56")
        check = diagnostics._check_license_active(summary)

        self.assertEqual(check.status, "ok")
        text = _flatten_check_text(check)
        self.assertNotIn(FAKE_LICENSE_ID, text)
        self.assertIn("****EF56", text)


class DoctorTextModeLicenseCheckTests(unittest.TestCase):
    """사람용 텍스트 CLI(doctor.check_license)도 동일한 규칙을 지키는지 확인."""

    def test_missing_key_fails_with_fix_hint(self) -> None:
        with patch.object(licensing, "stored_key", return_value=None):
            c = doctor.check_license()

        self.assertEqual(c.status, "fail")
        self.assertIsNotNone(c.fix)

    def test_active_license_never_prints_raw_key(self) -> None:
        with patch.object(licensing, "stored_key", return_value=FAKE_RAW_KEY), patch.object(
            licensing, "verify_key", return_value={"valid": True, "license_id": FAKE_LICENSE_ID}
        ):
            c = doctor.check_license()

        self.assertEqual(c.status, "ok")
        blob = " ".join(c.lines) + " " + (c.fix or "")
        self.assertNotIn(FAKE_RAW_KEY, blob)
        self.assertNotIn(FAKE_LICENSE_ID, blob)
        self.assertIn(FAKE_LICENSE_ID[-4:].upper(), blob)


if __name__ == "__main__":
    unittest.main()
