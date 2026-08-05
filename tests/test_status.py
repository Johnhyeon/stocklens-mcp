"""stocklens_status(status.py) 테스트.

경량 스냅샷이라 네트워크를 새로 타지 않는다는 게 핵심 계약이므로, metrics 로그와
업데이트 캐시를 mock으로 주입해서 market 상태 판정(ok/degraded/down/unknown)과
최근 성공/실패 추출 로직만 검증한다.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from stock_mcp_server import licensing, status


def _rec(tool: str, ts: str, error: str | None = None) -> dict:
    return {"tool": tool, "timestamp": ts, "error": error}


class MarketStatusTests(unittest.TestCase):
    def test_no_records_is_unknown(self) -> None:
        self.assertEqual(status._market_status([], "kr"), "unknown")

    def test_all_success_is_ok(self) -> None:
        records = [_rec("get_price", "2026-08-06T10:00:00"), _rec("get_chart", "2026-08-06T10:01:00")]
        self.assertEqual(status._market_status(records, "kr"), "ok")

    def test_all_failed_is_down(self) -> None:
        records = [_rec("get_us_price", "2026-08-06T10:00:00", error="TimeoutException")]
        self.assertEqual(status._market_status(records, "us"), "down")

    def test_mixed_is_degraded(self) -> None:
        records = [
            _rec("get_price", "2026-08-06T10:00:00"),
            _rec("get_chart", "2026-08-06T10:01:00", error="ConnectError"),
        ]
        self.assertEqual(status._market_status(records, "kr"), "degraded")

    def test_non_market_tools_are_excluded_from_bucketing(self) -> None:
        records = [_rec("get_metrics_summary", "2026-08-06T10:00:00", error="Boom")]
        self.assertEqual(status._market_status(records, "kr"), "unknown")
        self.assertEqual(status._market_status(records, "us"), "unknown")

    def test_kr_and_us_buckets_stay_independent(self) -> None:
        records = [
            _rec("get_price", "2026-08-06T10:00:00"),  # kr ok
            _rec("get_us_price", "2026-08-06T10:00:00", error="Boom"),  # us down
        ]
        self.assertEqual(status._market_status(records, "kr"), "ok")
        self.assertEqual(status._market_status(records, "us"), "down")


class RecentSuccessFailureTests(unittest.TestCase):
    def test_picks_latest_of_each(self) -> None:
        records = [
            _rec("get_price", "2026-08-06T09:00:00"),
            _rec("get_chart", "2026-08-06T09:05:00", error="TimeoutException"),
            _rec("get_flow", "2026-08-06T10:00:00"),  # latest success
            _rec("get_us_price", "2026-08-06T09:30:00", error="ConnectError"),  # latest failure
        ]
        last_success, last_failure, last_failure_error = status._recent_success_and_failure(records)
        self.assertEqual(last_success, "2026-08-06T10:00:00")
        self.assertEqual(last_failure, "2026-08-06T09:30:00")
        self.assertEqual(last_failure_error, "ConnectError")

    def test_no_records_returns_all_none(self) -> None:
        self.assertEqual(status._recent_success_and_failure([]), (None, None, None))


class UpdateInfoTests(unittest.TestCase):
    def test_no_cache_means_no_update_info(self) -> None:
        with patch("stock_mcp_server._update_check._load_cache", return_value=None):
            available, latest = status._update_info()
        self.assertFalse(available)
        self.assertIsNone(latest)

    def test_cached_newer_version_reports_available(self) -> None:
        with patch(
            "stock_mcp_server._update_check._load_cache",
            return_value={"latest_version": "99.0.0", "release_notes": ""},
        ):
            available, latest = status._update_info()
        self.assertTrue(available)
        self.assertEqual(latest, "99.0.0")


class BuildStatusTests(unittest.TestCase):
    def test_reflects_missing_license_and_json_roundtrip(self) -> None:
        with patch.object(status, "load_metrics", return_value=[]), patch.object(
            licensing, "stored_key", return_value=None
        ), patch("stock_mcp_server._update_check._load_cache", return_value=None):
            snap = status.build_status()

        self.assertEqual(snap.license_status, "missing")
        self.assertEqual(snap.kr_market_status, "unknown")
        self.assertEqual(snap.us_market_status, "unknown")

        text = status.format_status(snap)
        self.assertIn("STOCKLENS_STATUS_JSON_START", text)
        payload = text.split("STOCKLENS_STATUS_JSON_START", 1)[1].split("STOCKLENS_STATUS_JSON_END", 1)[0].strip()
        data = json.loads(payload)
        self.assertEqual(data, snap.to_dict())

    def test_active_license_reflected(self) -> None:
        with patch.object(status, "load_metrics", return_value=[]), patch.object(
            licensing, "stored_key", return_value="FAKE"
        ), patch.object(licensing, "verify_key", return_value={"valid": True, "license_id": "abcd1234"}), patch(
            "stock_mcp_server._update_check._load_cache", return_value=None
        ):
            snap = status.build_status()

        self.assertEqual(snap.license_status, "active")


class StocklensStatusToolTests(unittest.TestCase):
    def test_tool_works_without_license(self) -> None:
        """stocklens_status는 안내 목적상 라이선스 게이트를 걸지 않는다."""
        from stock_mcp_server import server

        with patch.object(licensing, "is_licensed", return_value=False), patch.object(
            licensing, "stored_key", return_value=None
        ):
            text = asyncio.run(server.stocklens_status())

        self.assertIn("STOCKLENS_STATUS_JSON_START", text)
        self.assertNotIn("유료 라이선스가 필요", text)  # safe_tool의 LOCKED_MESSAGE가 아님을 확인


if __name__ == "__main__":
    unittest.main()
