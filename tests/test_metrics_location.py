"""로그 저장 위치 — 2026-08 에 Downloads 밖으로 옮겼다.

옮긴 이유: 예전엔 엑셀 저장 폴더(~/Downloads/kstock/)에 진단 로그까지 얹어 썼다.
그 폴더가 Downloads 에 있는 건 '사용자가 내보낸 엑셀을 찾을 수 있게'라서 맞지만,
사용자가 열 일 없는 로그까지 거기 두면 Downloads 정리 한 번에 지원 문의용 기록이
통째로 날아간다(실측 42MB·81일치). DartLens 는 처음부터 ~/.dartlens/logs 를 썼다.

기존 사용자 것은 옮기지 않는다 — 새 자리에 쌓고, 읽을 때만 양쪽을 본다.
"""

from __future__ import annotations

import json

import pytest

from stock_mcp_server import _metrics


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """새 위치와 옛 위치를 둘 다 임시 폴더로 돌린다."""
    monkeypatch.setenv("STOCKLENS_HOME", str(tmp_path / ".stocklens"))
    legacy = tmp_path / "Downloads" / "kstock" / "logs"
    monkeypatch.setattr(_metrics, "_legacy_metrics_dir", lambda: legacy)
    return tmp_path / ".stocklens" / "logs", legacy


def _write(folder, day: str, count: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"metrics_{day}.jsonl").write_text(
        "".join(json.dumps({"timestamp": f"{day}T10:00:00", "tool": "get_chart"}) + "\n" for _ in range(count)),
        encoding="utf-8",
    )


class TestWriteLocation:
    def test_writes_under_the_app_home_not_downloads(self, homes):
        new, legacy = homes
        assert _metrics.get_metrics_dir() == new
        assert "Downloads" not in str(_metrics.get_metrics_dir())

    def test_respects_stocklens_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCKLENS_HOME", str(tmp_path / "custom"))
        assert _metrics.get_metrics_dir() == tmp_path / "custom" / "logs"

    def test_read_dirs_put_the_new_place_first(self, homes):
        new, legacy = homes
        assert _metrics.get_metrics_dirs() == [new, legacy]


class TestReadingBothPlaces:
    """기존 사용자는 옛 자리에만 기록이 있다. 안 읽으면 그 사람 기록이 사라진 것처럼 된다."""

    def _today(self):
        from datetime import datetime

        return datetime.now().strftime("%Y%m%d")

    def test_legacy_only_is_still_read(self, homes):
        new, legacy = homes
        _write(legacy, self._today(), 5)
        assert len(_metrics.load_metrics(days=1)) == 5

    def test_new_location_is_read(self, homes):
        new, legacy = homes
        _write(new, self._today(), 3)
        assert len(_metrics.load_metrics(days=1)) == 3

    def test_same_day_in_both_is_not_double_counted(self, homes):
        """옮기던 날 하루가 양쪽에 걸칠 수 있다 — 두 번 세면 사용량이 부풀려진다."""
        new, legacy = homes
        today = self._today()
        _write(legacy, today, 9)
        _write(new, today, 2)
        assert len(_metrics.load_metrics(days=1)) == 2  # 새 위치가 이긴다

    def test_unreadable_legacy_folder_is_not_fatal(self, homes, monkeypatch):
        """Downloads 는 macOS 에서 권한으로 막힐 수 있다 — 그것 때문에 전체가 죽으면 안 된다."""
        new, legacy = homes
        _write(new, self._today(), 4)
        monkeypatch.setattr(_metrics, "_legacy_metrics_dir", lambda: legacy / "없는곳")
        assert len(_metrics.load_metrics(days=1)) == 4
