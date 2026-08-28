"""상세 수급 캐시 (1.1 Task 6).

디렉터리: <STOCKLENS_HOME>/cache/market_evidence/<provider>/<profile>/
분봉 캐시(market_data)와 완전히 분리된 루트다. 연결 해제 때 둘을 따로
지우고 따로 보고하기 위해서다 - 하나가 실패했는데 둘 다 지웠다고
말하면 안 된다.

이 캐시가 최적화가 아닌 이유:

- **잠정값을 담지 않는다.** 정산 전 값은 나중에 확정치로 바뀐다. 캐시에
  남으면 확정된 뒤에도 옛 숫자를 계속 돌려준다. 그래서 `data_state` 가
  final 인 행만 저장하고, 확정 행이 하나도 없으면 아예 저장하지 않는다.
  같은 날짜가 두 번 들어오면 나중 것으로 덮되 중복 행을 만들지 않는다.
- **연결을 대신하지 않는다.** 미연결 프로필은 남은 캐시로도 결과를
  만들지 못한다. generation 이 바뀌어도 마찬가지다.
- **허용 목록만 저장한다.** 헤더·토큰·계좌 식별자·원본 오류 본문은
  들어올 자리가 없다. 모르는 필드는 조용히 버린다.

경로에 능력 이름을 그대로 쓰지 않는다. 능력 이름은 점을 담고 앞으로
슬래시가 들어올 수도 있어서 경로 조각이 되면 안 된다. 해시로 줄인다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_SCHEMA_VERSION = 1

DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024

_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")

# 디스크에 남겨도 되는 필드. 여기 없는 것은 버린다. 새 필드를 담고
# 싶으면 여기 적어야 하고, 그때 "이게 남아도 되는 값인가"를 한 번 본다.
_ALLOWED_PAYLOAD_FIELDS = frozenset({
    "rows", "coverage", "measure", "unit", "market", "symbol",
    "data_state", "source_endpoint", "warnings", "granularity", "kind",
})
# 행 안에서 허용하는 필드.
_ALLOWED_ROW_FIELDS = frozenset({
    "date", "data_state", "values", "unsettled", "measures",
    "raw_categories", "raw_fields", "close", "volume", "balance_ok",
    "principal_sum",
})


def _home(home: Path | str | None) -> Path:
    if home is not None:
        return Path(home)
    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def _safe(part: str, name: str) -> str:
    text = str(part)
    if not _SAFE_PART.match(text) or ".." in text:
        raise ValueError(
            f"캐시 key 필드 {name} 에 허용되지 않는 문자: 경로 요소가 "
            "될 수 없습니다")
    return text


@dataclass(frozen=True)
class EvidenceCacheKey:
    provider: str
    profile: str
    schema_version: int
    capability: str
    symbol: str
    parts: tuple[str, ...]


class EvidenceCache:
    def __init__(self, home: Path | str | None = None, *,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> None:
        self._root = _home(home) / "cache" / "market_evidence"
        self._max_total_bytes = max_total_bytes

    # --- key ---

    def key(self, *, provider: str, profile: str, schema_version: int,
            capability: str, symbol: str,
            variant: str = "") -> EvidenceCacheKey:
        safe_provider = _safe(provider, "provider")
        safe_profile = _safe(profile, "profile")
        safe_symbol = _safe(symbol, "symbol")
        # 능력 이름은 경로에 넣지 않는다. 고정 알파벳으로 줄인다.
        digest = hashlib.sha256(
            f"{capability}\x1f{variant}".encode("utf-8")).hexdigest()[:16]
        filename = f"{safe_symbol}__{digest}__v{int(schema_version)}.json"
        return EvidenceCacheKey(
            provider=safe_provider, profile=safe_profile,
            schema_version=int(schema_version), capability=capability,
            symbol=safe_symbol,
            parts=(safe_provider, safe_profile, filename))

    def path_for(self, key: EvidenceCacheKey) -> Path:
        path = (self._root.joinpath(*key.parts)).resolve()
        root = self._root.resolve()
        if not str(path).startswith(str(root) + os.sep):
            raise ValueError("캐시 경로가 market_evidence 루트를 벗어납니다")
        return path

    # --- 저장 형태 ---

    @staticmethod
    def _final_rows(payload: dict) -> list[dict]:
        """확정 행만, 날짜 중복 없이, 최신 순으로."""
        by_date: dict[str, dict] = {}
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            if row.get("data_state") != "final":
                continue
            day = str(row.get("date") or "")
            if not day:
                continue
            cleaned = {k: v for k, v in row.items()
                       if k in _ALLOWED_ROW_FIELDS}
            # 같은 날짜가 다시 오면 나중 것이 이긴다. 두 줄로 남기지
            # 않는다 - 호출자가 어느 쪽이 맞는지 알 수 없게 된다.
            by_date[day] = cleaned
        return [by_date[d] for d in sorted(by_date, reverse=True)]

    @staticmethod
    def _clean_payload(payload: dict, rows: list[dict]) -> dict:
        out = {k: v for k, v in payload.items()
               if k in _ALLOWED_PAYLOAD_FIELDS and k != "rows"}
        out["rows"] = rows
        return out

    # --- 조회·저장 ---

    def get(self, key: EvidenceCacheKey, *, connected: bool,
            generation: int) -> dict | None:
        if not connected:
            # 캐시가 연결을 대신하지 않는다 (1.0 분봉 캐시와 같은 계약).
            return None
        path = self.path_for(key)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self._remove(path)
            return None
        if not isinstance(doc, dict) or \
                doc.get("schema_version") != key.schema_version or \
                doc.get("generation") != generation:
            self._remove(path)
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return {"payload": doc.get("payload") or {},
                "final_through": doc.get("final_through"),
                "generation": doc.get("generation")}

    def put(self, key: EvidenceCacheKey, payload: dict, *,
            generation: int, final_through: str | None) -> None:
        rows = self._final_rows(payload)
        path = self.path_for(key)
        if not rows:
            # 확정 행이 하나도 없으면 남길 것이 없다. 잠정값을 담으면
            # 확정된 뒤에도 옛 숫자를 계속 돌려주게 된다.
            self._remove(path)
            return

        previous = None
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        if isinstance(previous, dict) and \
                previous.get("schema_version") == key.schema_version and \
                previous.get("generation") == generation:
            merged: dict[str, dict] = {
                str(r.get("date")): r
                for r in (previous.get("payload") or {}).get("rows") or []
                if isinstance(r, dict) and r.get("date")}
            for row in rows:
                merged[str(row["date"])] = row
            rows = [merged[d] for d in sorted(merged, reverse=True)]

        newest = rows[0]["date"] if rows else None
        # 늦게 도착한 짧은 응답이 이미 가진 확정 구간을 줄이지 않는다.
        stamp = max(filter(None, (final_through, newest)), default=None)

        doc = {
            "schema_version": key.schema_version,
            "generation": int(generation),
            "final_through": stamp,
            "payload": self._clean_payload(payload, rows),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".ev_", suffix=".tmp")
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
        if not self._root.exists():
            return 0
        total = 0
        for path in self._root.rglob("*.json"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _evict_if_needed(self) -> None:
        total = self.total_bytes()
        if total <= self._max_total_bytes:
            return
        entries = []
        for path in self._root.rglob("*.json"):
            try:
                entries.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
        entries.sort(key=lambda item: (item[0], item[1]))
        for _, _, path in entries:
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue
            if total <= self._max_total_bytes:
                break

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def remove_provider(self, provider: str) -> None:
        """공급자 해제 시 그 공급자의 증거 캐시만 지운다.

        분봉 캐시와 같은 방어: 등록된 provider ID 만, symlink 거부,
        루트 이탈 거부, 다른 provider 디렉터리는 건드리지 않는다.
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
                "등록되지 않은 provider 의 캐시는 삭제하지 않습니다: "
                f"{provider}") from None
        target = self._root / provider
        if target.is_symlink():
            raise ValueError("캐시 삭제 대상이 symlink 입니다. 거부합니다")
        if not target.exists():
            return
        resolved = target.resolve()
        root = self._root.resolve()
        if resolved != root / provider or \
                not str(resolved).startswith(str(root) + os.sep):
            raise ValueError("삭제 대상이 market_evidence 루트를 벗어납니다")
        shutil.rmtree(target, ignore_errors=False)
