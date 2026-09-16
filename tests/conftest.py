"""테스트가 진짜 사용자 폴더를 건드리지 않게 막는다.

체험 시작일은 두 곳에 적힌다 — Lens 폴더(`~/.stocklens`)와 브랜드 공용
폴더(`~/.leetkit`). 앞엣것은 테스트가 `_home` 을 tmp_path 로 돌려놓으면 피해가지만,
뒤엣것은 `Path.home()` 에서 바로 계산되므로 그것만으로는 못 막는다.

실제로 그렇게 당했다 — 라이선스 테스트를 돌렸더니 개발자 홈의 `trial_started.json`
안에 가짜 license_id 수십 개가 쌓였다. 파일 단위 fixture 로는 새 테스트 파일이 생길
때마다 같은 실수가 반복되므로, 여기서 전체에 건다.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_stocklens_home(tmp_path_factory, monkeypatch):
    """모든 테스트에 tmp STOCKLENS_HOME 을 강제한다.

    실제로 당했다 (2026-08-27). STOCKLENS_HOME 미설정으로 돌던 분봉
    테스트들이 가짜 봉을 개발자 실사용 캐시(~/.stocklens/cache)에
    '완전한 하루'로 저장했고, 실서비스 조회가 그 가짜 캐시를 맞았다.
    개별 테스트가 자기 home 을 다시 설정하는 것은 그대로 동작한다.

    라이선스 파일만 실사용 홈에서 복사해 온다. 수많은 도구 테스트가
    유료 게이트 뒤에 있어서, 격리 홈이 비면 전부 라이선스 오류로
    무너진다 (읽기 전용 복사라 실사용 파일은 건드리지 않는다).
    """
    import shutil
    from pathlib import Path

    root = tmp_path_factory.mktemp("stocklens_home")
    real_home = Path.home() / ".stocklens"
    for name in ("license.key", "revoked_cache.json", "clock_seen"):
        src = real_home / name
        if src.exists():
            try:
                shutil.copy2(src, root / name)
            except OSError:
                pass
    monkeypatch.setenv("STOCKLENS_HOME", str(root))


@pytest.fixture(autouse=True)
def _isolate_license_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("license_state")
    try:
        from stock_mcp_server import licensing
    except Exception:  # 라이선스 모듈을 안 쓰는 테스트도 있다
        return
    monkeypatch.setattr(
        licensing,
        "_trial_mark_paths",
        lambda: [root / "lens" / "trial_started.json", root / "shared" / "trial_started.json"],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _default_krx_sosok(monkeypatch):
    """일봉 도구가 애프터마켓 이름표를 붙이기 전에 시장 구분(ETF·ETN 여부)을 네이버에 묻는다.

    테스트가 그 한 번 때문에 실제 네트워크에 나가지 않게 기본값을 일반 주식으로 둔다.
    ETF·ETN 경로를 보는 테스트는 자기 안에서 다시 patch 한다.
    """
    try:
        from unittest.mock import AsyncMock

        from stock_mcp_server import server
    except Exception:
        return
    monkeypatch.setattr(server, "naver_get_krx_sosok", AsyncMock(return_value="KOSPI"), raising=False)
