"""공급자별 로컬·메모리 분봉 캐시.

디렉터리: <STOCKLENS_HOME>/cache/market_data/<provider>/<profile>/
기존 Naver·Yahoo 캐시(_cache.py)와 완전히 분리된다.

정책:
- 캐시는 공급자 연결 능력을 대신하지 않는다. 미연결 프로필은
  남은 캐시로도 결과를 만들 수 없다 (connected=False -> miss).
- 불완전 응답을 완전 캐시로 승격하지 않는다.
- 손상·비호환 entry 는 개별 제거한다.
- 총량 제한과 결정적 LRU(오래된 entry 부터) 정리.
- key·경로에 비밀값을 넣지 않는다. 경로 탈출을 검증한다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1
# 2: kis_domestic 이 요청 거래일 밖 행(전일 오후 페이지)을 혼입하던 버그
#    수정(2026-08-27 실측). 혼입 행이 저장된 v1 entry 를 무효화한다.
PARSE_VERSION = 2

# 기본 총량 상한. 구현 상수로 분리한다 (설계 21절).
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024

_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]*$")

_KEY_FIELDS = (
    "provider", "profile", "market", "symbol", "venue", "session",
    "source_interval", "trading_date", "cursor", "adjustment",
)


def _home(home: Path | str | None) -> Path:
    if home is not None:
        return Path(home)
    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def _safe(part: str, name: str) -> str:
    part = str(part)
    if not _SAFE_PART.match(part) or ".." in part:
        raise ValueError(f"캐시 key 필드 {name}에 허용되지 않는 문자: 경로 요소가 될 수 없습니다")
    return part


class ProviderCache:
    def __init__(
        self,
        home: Path | str | None = None,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self._root = _home(home) / "cache" / "market_data"
        self._max_total_bytes = max_total_bytes

    # --- 경로 ---

    def _entry_path(self, key: dict) -> Path:
        parts = {name: _safe(key.get(name, ""), name) for name in _KEY_FIELDS}
        directory = self._root / parts["provider"] / parts["profile"]
        filename = "__".join((
            parts["market"], parts["symbol"], parts["venue"],
            parts["session"], parts["source_interval"],
            parts["trading_date"], parts["cursor"] or "none",
            parts["adjustment"], f"p{PARSE_VERSION}",
        )) + ".json"
        path = (directory / filename).resolve()
        root = self._root.resolve()
        if not str(path).startswith(str(root) + os.sep):
            raise ValueError("캐시 경로가 market_data 루트를 벗어납니다")
        return path

    # --- 조회·저장 ---

    def get(self, key: dict, *, connected: bool) -> dict | None:
        if not connected:
            # 연결이 없으면 남은 캐시로 결과를 만들지 않는다.
            return None
        path = self._entry_path(key)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self._remove_entry(path)
            return None
        if not isinstance(doc, dict) or \
                doc.get("schema_version") != SCHEMA_VERSION or \
                doc.get("parse_version") != PARSE_VERSION:
            self._remove_entry(path)
            return None
        try:
            os.utime(path, None)  # LRU 갱신
        except OSError:
            pass
        return {"payload": doc.get("payload"),
                "complete": bool(doc.get("complete"))}

    def put(self, key: dict, payload: dict, *, complete: bool) -> None:
        path = self._entry_path(key)
        # 불완전 응답은 기존 entry 의 완전 여부와 무관하게 complete=False 로
        # 기록된다. 어떤 경로로도 완전 캐시로 승격되지 않는다.
        doc = {
            "schema_version": SCHEMA_VERSION,
            "parse_version": PARSE_VERSION,
            "complete": bool(complete),
            "payload": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".cache_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._evict_if_needed()

    # --- 정리 ---

    def total_bytes(self) -> int:
        total = 0
        if not self._root.exists():
            return 0
        for path in self._root.rglob("*.json"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _entries_by_age(self) -> list[Path]:
        entries = []
        for path in self._root.rglob("*.json"):
            try:
                entries.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
        entries.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in entries]

    def _evict_if_needed(self) -> None:
        total = self.total_bytes()
        if total <= self._max_total_bytes:
            return
        for path in self._entries_by_age():
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue
            if total <= self._max_total_bytes:
                break

    def _remove_entry(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def remove_provider(self, provider: str) -> None:
        """공급자 전체 해제 시 해당 provider 캐시 디렉터리만 삭제한다.

        - 레지스트리에 등록된 provider ID 만 허용한다 (사용자 문자열 금지)
        - symlink 와 루트 이탈 경로를 거부한다
        - 다른 provider 디렉터리는 절대 건드리지 않는다
        """
        from stock_mcp_server.market_data.provider_registry import (
            UnknownProviderError,
            registry,
        )

        provider = _safe(provider, "provider")
        try:
            registry.require(provider)
        except UnknownProviderError:
            raise ValueError(
                f"등록되지 않은 provider의 캐시는 삭제하지 않습니다: "
                f"{provider}") from None
        target = self._root / provider
        if target.is_symlink():
            raise ValueError("캐시 삭제 대상이 symlink 입니다. 거부합니다")
        resolved = target.resolve()
        root = self._root.resolve()
        if resolved != root / provider or \
                not str(resolved).startswith(str(root) + os.sep):
            raise ValueError("삭제 대상이 market_data 루트를 벗어납니다")
        if not target.exists():
            return
        import shutil
        shutil.rmtree(target, ignore_errors=False)


class MemoryBarCache:
    """프로세스 메모리 캐시. generation 이 바뀌면 전체를 폐기한다."""

    def __init__(self, generation_provider) -> None:
        self._generation_provider = generation_provider
        self._generation = generation_provider()
        self._entries: dict[str, object] = {}

    def _check_generation(self) -> None:
        current = self._generation_provider()
        if current != self._generation:
            self._entries.clear()
            self._generation = current

    def get(self, key: str):
        self._check_generation()
        return self._entries.get(key)

    def put(self, key: str, value) -> None:
        self._check_generation()
        self._entries[key] = value

    def clear(self) -> None:
        self._entries.clear()
