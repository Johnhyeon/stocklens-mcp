"""관심종목('내 종목') — LeetKit 세 Lens 공용 저장.

파일: ~/.leetkit/watchlist.json  (LEETKIT_HOME 으로 재정의)
형식: {"stocks": [{"code": "039840", "name": "디오"}]}

TelegramLens가 먼저 쓰던 형식을 그대로 따른다. **형식을 바꾸면 상대 Lens가
못 읽는다** — 이건 파일이 아니라 계약이다.

왜 StockLens에 관리 도구를 두는가:
LeetKit STOCK 단품 구매자가 다수인데(구매 로그상 StockLens는 수천 회, 나머지
둘은 100회 이하), 관리 수단이 텔레그램 명령뿐이면 그분들은 아예 못 쓴다.
관심종목은 시세 중심 개념이라 여기가 제자리다.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

MAX_STOCKS = 50  # 너무 많으면 '관심'이 아니게 된다. 눈으로 훑을 수 있는 선.


def leetkit_dir() -> Path:
    override = os.environ.get("LEETKIT_HOME")
    base = Path(override) if override else (Path.home() / ".leetkit")
    base.mkdir(parents=True, exist_ok=True)
    return base


def watchlist_path() -> Path:
    return leetkit_dir() / "watchlist.json"


def load() -> list[dict]:
    """[{code, name}]. 파일이 없거나 깨졌으면 빈 리스트 — 조회를 막지 않는다."""
    p = watchlist_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("stocks") if isinstance(data, dict) else data
    return [s for s in (items or []) if isinstance(s, dict) and s.get("code")]


def codes() -> set[str]:
    """★ 표시용. 조회 경로에서 매번 부르므로 가볍게 유지한다."""
    return {s["code"] for s in load()}


def save(stocks: list[dict]) -> None:
    """원자적 쓰기.

    TelegramLens 데몬과 StockLens가 동시에 건드릴 수 있다. 같은 파일에 직접
    쓰다가 중간에 죽으면 목록이 통째로 깨진다 — 사용자가 등록해둔 걸 잃는 건
    기능 하나 없는 것보다 나쁘다.
    """
    p = watchlist_path()
    payload = json.dumps({"stocks": stocks}, ensure_ascii=False, indent=2)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, p)
    except OSError:
        pass


def add(code: str, name: str) -> tuple[bool, str]:
    """반환: (추가됐나, 사유). 이미 있으면 조용히 넘어간다."""
    cur = load()
    if any(s["code"] == code for s in cur):
        return False, "이미 등록됨"
    if len(cur) >= MAX_STOCKS:
        return False, f"최대 {MAX_STOCKS}개까지"
    cur.append({"code": code, "name": name})
    save(cur)
    return True, ""


def remove(code_or_name: str) -> dict | None:
    """코드 우선, 안 맞으면 이름 부분일치. 반환: 제거된 종목 | None."""
    cur = load()
    target = next((s for s in cur if s["code"] == code_or_name), None)
    if target is None:
        target = next((s for s in cur if code_or_name in (s.get("name") or "")), None)
    if target is None:
        return None
    save([s for s in cur if s is not target])
    return target


def clear() -> int:
    n = len(load())
    save([])
    return n
