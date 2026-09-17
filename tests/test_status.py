"""stocklens_status(status.py) 테스트.

경량 스냅샷이라 네트워크를 새로 타지 않는다는 게 핵심 계약이므로, metrics 로그와
업데이트 캐시를 mock으로 주입해서 market 상태 판정(ok/degraded/down/unknown)과
최근 성공/실패 추출 로직만 검증한다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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


# ---------- 라이선스: "활성화됨"이면 도구가 정말 열려 있어야 한다 ----------
#
# 여기는 verify_key 를 mock 하지 않고 임시 키쌍으로 진짜 서명한 키를 쓴다. 예전 버그가
# 바로 "서명이 맞으면 active"였고, verify_key 를 mock 한 테스트는 그걸 통과시켰다.

_EPOCH = date(1970, 1, 1)


def _utc_today() -> date:
    # 제품 코드가 UTC 로 판단한다 — 로컬 날짜를 쓰면 KST 00~09시에 하루 어긋난다.
    return datetime.now(timezone.utc).date()


@pytest.fixture
def use_key(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        licensing, "_PUBLIC_KEY_B64", base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    )
    monkeypatch.setattr(licensing, "_home", lambda: tmp_path)
    monkeypatch.setattr(licensing, "_licensed_cache", False)
    monkeypatch.setattr(licensing, "_fetch_revoked", lambda: pytest.fail("상태 조회 테스트가 네트워크를 탔다"))
    monkeypatch.setattr(status, "load_metrics", lambda *a, **k: [])
    monkeypatch.setattr("stock_mcp_server._update_check._load_cache", lambda: None)

    def use(expires_on: date | None = None, revoked: bool = False) -> None:
        payload = licensing.PRODUCT + secrets.token_bytes(6)
        if expires_on is not None:
            payload += (expires_on - _EPOCH).days.to_bytes(4, "big")
        key = base64.b32encode(payload + priv.sign(payload)).decode().rstrip("=")
        monkeypatch.setattr(licensing, "stored_key", lambda: key)
        # 방금 받은 목록으로 둔다 — 하루가 안 지났으니 다시 받지 않는다.
        (tmp_path / "revoked_cache.json").write_text(
            json.dumps({"revoked": [payload[4:10].hex()] if revoked else [], "fetched_at": time.time()}),
            encoding="utf-8",
        )

    return use


def _assert_matches_gate(snap: status.StatusSnapshot) -> None:
    assert (snap.license_status == "active") is licensing.is_licensed()


def test_live_trial_key_is_active(use_key):
    use_key(_utc_today() + timedelta(days=3))
    snap = status.build_status()
    assert snap.license_status == "active"
    _assert_matches_gate(snap)
    assert "- 라이선스: 활성화됨" in status.format_status(snap)


def test_expired_trial_key_is_not_active(use_key):
    use_key(_utc_today() - timedelta(days=1))
    snap = status.build_status()
    assert snap.license_status == "expired"
    _assert_matches_gate(snap)
    text = status.format_status(snap)
    assert "- 라이선스: 사용 기간 끝남" in text
    assert "활성화됨" not in text
    assert "[구매]" in text and "[활성화]" in text


def test_revoked_key_is_not_active(use_key):
    use_key(None, revoked=True)
    snap = status.build_status()
    assert snap.license_status == "revoked"
    _assert_matches_gate(snap)
    text = status.format_status(snap)
    assert "- 라이선스: 사용 중지됨" in text
    assert "활성화됨" not in text
    assert "[지원 문의]" in text


def test_clock_turned_back_is_not_active(use_key, tmp_path):
    use_key(_utc_today() + timedelta(days=5))
    (tmp_path / "clock_seen").write_text((_utc_today() + timedelta(days=10)).isoformat(), encoding="utf-8")
    snap = status.build_status()
    assert snap.license_status == "clock"
    _assert_matches_gate(snap)
    assert "- 라이선스: 컴퓨터 날짜 확인 필요" in status.format_status(snap)


def test_every_blocked_state_has_label_and_next_step():
    """새 상태가 생겼는데 이름표나 안내를 빠뜨리면 영문 코드가 그대로 나간다."""
    assert set(status._LICENSE_NEXT_STEP) == set(status._LICENSE_LABEL) - {"active"}


@pytest.mark.parametrize("state", sorted(status._LICENSE_LABEL))
def test_license_guidance_never_asks_for_terminal(state):
    snap = status.StatusSnapshot(
        package_version="0.0.0",
        license_status=state,
        kr_market_status="unknown",
        us_market_status="unknown",
        last_success_at=None,
        last_failure_at=None,
        last_failure_error_code=None,
        cache_writable=True,
        update_available=False,
        latest_version=None,
    )
    text = status.format_status(snap)
    for banned in ("stocklens-activate", "stocklens-doctor", "터미널", "명령"):
        assert banned not in text


class StocklensStatusToolTests(unittest.TestCase):
    def test_tool_works_without_license(self) -> None:
        """stocklens_status는 안내 목적상 라이선스 게이트를 걸지 않는다."""
        from stock_mcp_server import server

        with patch.object(licensing, "is_licensed", return_value=False), patch.object(
            licensing, "stored_key", return_value=None
        ):
            text = asyncio.run(server.stocklens_status())

        self.assertIn("STOCKLENS_STATUS_JSON_START", text)
        self.assertNotEqual(text, licensing.LOCKED_MESSAGE)  # safe_tool의 잠금 안내가 아님을 확인
        self.assertNotIn("라이선스 키가 필요해요", text)


if __name__ == "__main__":
    unittest.main()
