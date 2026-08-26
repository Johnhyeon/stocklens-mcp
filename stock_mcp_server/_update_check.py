"""업데이트 알림 — PyPI 최신 버전 + GitHub Release 노트 조회.

동작 원리:
- 프로세스당 1회만 체크 (수백 번 tool 호출되어도 한 번)
- 하루 1회 캐시 (~/.stocklens/update_check.json)
- 네트워크 실패 시 조용히 빈 문자열 반환 (tool 동작 방해 X)
- STOCKLENS_FORCE_UPDATE_NOTICE=1 로 강제 테스트 가능

알림 문구는 tool 응답 말미에 주입되어 LLM이 사용자에게 자연스럽게 전달.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from stock_mcp_server import __version__

CACHE_DIR = Path.home() / ".stocklens"
CACHE_FILE = CACHE_DIR / "update_check.json"
PYPI_URL = "https://pypi.org/pypi/stocklens-mcp/json"
GITHUB_URL = "https://api.github.com/repos/Johnhyeon/stocklens-mcp/releases/latest"
CACHE_TTL = timedelta(hours=24)
TIMEOUT = 3.0
MAX_NOTE_LINES = 8

_notice_issued: bool = False


def _load_cache() -> dict | None:
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(data.get("checked_at", ""))
        if datetime.now() - checked_at > CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def _save_cache(latest_version: str, release_notes: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(
                {
                    "checked_at": datetime.now().isoformat(),
                    "latest_version": latest_version,
                    "release_notes": release_notes,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


async def _fetch_latest() -> tuple[str, str] | None:
    """PyPI + GitHub 병렬 호출. 네트워크 실패 시 None."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            pypi_resp, gh_resp = await asyncio.gather(
                client.get(PYPI_URL),
                client.get(GITHUB_URL),
                return_exceptions=True,
            )
        latest_version = ""
        release_notes = ""
        if not isinstance(pypi_resp, Exception) and pypi_resp.status_code == 200:
            latest_version = pypi_resp.json().get("info", {}).get("version", "") or ""
        if not isinstance(gh_resp, Exception) and gh_resp.status_code == 200:
            release_notes = gh_resp.json().get("body", "") or ""
        if latest_version:
            return latest_version, release_notes
    except Exception:
        pass
    return None


def _version_key(v: str) -> tuple[int, ...] | None:
    """"0.8.2" / "v1.2" -> (0, 8, 2) / (1, 2). 그 밖의 형태는 None.

    숫자만 뽑아 비교하면 안 된다. "0.8.3rc1" 의 숫자는 (0,8,3,1)이라
    (0,8,3)보다 커져 프리릴리스가 정식판 위로 올라간다. 순수한
    v?숫자.숫자... 형태만 받고, rc·beta·post 같은 접미사가 붙으면
    비교 자체를 포기한다(None). 끝의 0은 지운다 - 0.8.2.0 과 0.8.2 는
    같은 버전인데 튜플 길이로 이기면 안 된다.
    """
    import re

    s = (v or "").strip()
    if not re.fullmatch(r"[vV]?\d+(?:\.\d+)*", s):
        return None
    nums = [int(x) for x in s.lstrip("vV").split(".")]
    while nums and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def _version_gt(latest: str, current: str) -> bool:
    """semver 비교. packaging 이 없으면 숫자 튜플로 비교한다.

    예전 fallback 은 "버전이 다르기만 하면 새 버전"이었다. 실측(2026-08-27):
    현재 0.8.2 인데 캐시된 최신이 0.8.0 이던 시점에 "새 버전 v0.8.0" 안내가
    만들어졌다. 낮은 버전으로 안내하면 사용자는 다운그레이드를 하게 된다.
    숫자를 못 뽑으면 새 버전이라고 주장하지 않는다 - 오탐 안내가 더 나쁘다.
    """
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        pass
    try:
        lk, ck = _version_key(latest), _version_key(current)
        # 빈 튜플은 버전 0 이지 실패가 아니다. 실패(None)만 걸러낸다.
        if lk is None or ck is None:
            return False
        return lk > ck
    except Exception:
        return False


def _format_notice(latest: str, current: str, notes: str, test_mode: bool = False) -> str:
    lines = [ln for ln in notes.strip().split("\n") if ln.strip()][:MAX_NOTE_LINES]
    notes_text = "\n".join(lines) if lines else "(릴리즈 노트 없음)"

    if test_mode:
        return (
            f"\n\n---\n"
            f"ℹ️ StockLens 업데이트 알림 테스트 (TEST MODE)\n"
            f"현재 설치: v{current} / PyPI 최신: v{latest}\n"
            f"개발 모드 강제 표시. 실제 릴리즈 시 아래와 같은 형태로 노출됩니다.\n\n"
            f"새 버전: v{latest}\n"
            f"업데이트: `uv tool upgrade stocklens-mcp`\n"
            f"주요 변경:\n{notes_text}"
        )

    # 안내는 LeetKit Manager 하나만 말한다. 예전엔 터미널 명령(`uv tool upgrade`)을
    # 적어뒀는데, 주 고객층은 거기서 막힌다 — Manager가 있는 이유가 그 명령을 안 치게
    # 하려는 것이다. 길이 둘이면 쉬운 쪽도 어려워 보인다.
    return (
        f"\n\n---\n"
        f"ℹ️ StockLens 업데이트 정보\n"
        f"새 버전: v{latest} (현재 v{current})\n"
        f"업데이트: LeetKit Manager를 열고 [지금 업데이트]를 눌러주세요.\n\n"
        f"주요 변경:\n{notes_text}"
    )


async def get_update_notice() -> str:
    """업데이트 알림 문자열을 반환 (없으면 빈 문자열).

    - 프로세스당 1회만 실제 작업 (이후 호출은 즉시 "" 리턴)
    - STOCKLENS_FORCE_UPDATE_NOTICE=1 이면 매번 강제 생성
    """
    global _notice_issued

    force = os.environ.get("STOCKLENS_FORCE_UPDATE_NOTICE") == "1"

    if _notice_issued and not force:
        return ""

    cached = _load_cache()
    if cached:
        latest = cached.get("latest_version", "")
        notes = cached.get("release_notes", "")
    else:
        result = await _fetch_latest()
        if not result:
            return ""
        latest, notes = result
        _save_cache(latest, notes)

    current = __version__
    if not force and not _version_gt(latest, current):
        _notice_issued = True  # 체크는 했으니 재실행 방지
        return ""

    _notice_issued = True
    return _format_notice(latest, current, notes, test_mode=force)
