"""비밀 없는 연결 상태 파일 (broker_state.json).

Manager CLI 는 실행 중인 MCP 프로세스의 메모리 토큰을 직접 지울 수 없다.
대신 이 파일의 connection_generation 을 올리고, MCP 프로세스는 각 KIS 호출
전에 generation 을 확인해 달라졌으면 토큰·메모리 캐시를 폐기하고 다시 읽는다.

이 파일에는 어떤 비밀값도 쓰지 않는다. 손상되면 legacy 모드로 안전하게
동작한다 (KIS 호출 없음).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_FILENAME = "broker_state.json"

DATA_SOURCE_MODES = ("auto", "broker_first", "legacy")

DEFAULT_STATE: dict = {
    "connection_generation": 0,
    "active_provider": None,
    "active_profile": None,
    "data_source_mode": "legacy",
}


def _home(home: Path | str | None = None) -> Path:
    if home is not None:
        return Path(home)
    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def state_path(home: Path | str | None = None) -> Path:
    return _home(home) / STATE_FILENAME


def _sanitize(raw: object) -> dict:
    """형식이 깨진 상태 파일은 legacy 기본값으로 안전하게 내린다."""
    if not isinstance(raw, dict):
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    gen = raw.get("connection_generation")
    mode = raw.get("data_source_mode")
    provider = raw.get("active_provider")
    profile = raw.get("active_profile")
    if not isinstance(gen, int) or gen < 0:
        return dict(DEFAULT_STATE)
    if mode not in DATA_SOURCE_MODES:
        return dict(DEFAULT_STATE)
    if provider is not None and not isinstance(provider, str):
        return dict(DEFAULT_STATE)
    if profile is not None and not isinstance(profile, str):
        return dict(DEFAULT_STATE)
    state.update({
        "connection_generation": gen,
        "active_provider": provider,
        "active_profile": profile,
        "data_source_mode": mode,
    })
    # 검증 결과 등 비밀 아닌 부가 키는 보존한다.
    for key, value in raw.items():
        if key not in state:
            state[key] = value
    return state


def load_state(home: Path | str | None = None) -> dict:
    path = state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)
    return _sanitize(raw)


def save_state(state: dict, home: Path | str | None = None) -> None:
    """임시 파일에 쓴 뒤 os.replace 로 원자 교체한다."""
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".broker_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def bump_generation(state: dict) -> dict:
    updated = dict(state)
    updated["connection_generation"] = int(state.get(
        "connection_generation", 0)) + 1
    return updated
