"""체험 만료가 다가올 때, Claude 응답 끝에 한 줄로 알린다.

**왜 여기인가.** 사람들은 LeetKit Manager를 잘 안 연다. 설치할 때 한 번 열고 그 뒤로는
Claude 안에서만 산다. 그래서 "기간이 끝나간다"를 Manager 화면에만 두면 정작 알아야 할
사람이 못 본다 — 기간이 끝나고 도구가 잠긴 다음에야 알게 된다. 도구 응답은 그 사람이
지금 보고 있는 유일한 화면이다.

**지켜야 할 선.** 우리가 파는 건 "믿을 수 있는 데이터"다. 그 응답에 광고를 섞으면
제품 자체의 신뢰를 깎는다. 그래서:
  - 정해진 시점에 딱 세 번(7·4·1일 남음). 매번이 아니다.
  - 한 줄. 사실만 — 남은 날짜와 가는 곳.
  - 권유 문구·과장 없음. "지금 안 사면" 같은 말은 쓰지 않는다.
  - 기간 없는 키(구매자)는 이 코드를 아예 안 탄다.

세 Lens 중 StockLens에만 둔다. 셋 다 넣으면 하루에 같은 알림을 세 번 보게 되는데,
실제 사용 로그상 StockLens 호출이 나머지 둘의 수십 배라 여기 하나로 사실상 전원에게
닿는다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from stock_mcp_server import licensing

# 남은 날짜가 이 값 이하로 처음 떨어졌을 때 한 번씩. 하루를 건너뛰어도 다음 호출에서
# 잡히도록 "그 날짜에 정확히"가 아니라 "이하로 내려왔을 때"로 판단한다.
_MILESTONES = (7, 4, 1)


def _state_path() -> Path:
    return licensing._home() / "trial_notice.json"


def _shown() -> set[int]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return {int(x) for x in data.get("shown", [])}
    except Exception:
        return set()


def _mark(milestones: set[int]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"shown": sorted(_shown() | milestones)}), encoding="utf-8")
    except OSError:
        pass  # 못 적으면 다음에 또 뜰 뿐이다 — 도구 응답을 막을 이유는 없다


def _days_left(expiry: date) -> int:
    return (expiry - datetime.now(timezone.utc).date()).days


def pending_notice() -> str | None:
    """지금 붙일 안내 한 줄. 붙일 게 없으면 None.

    실패하면 언제나 None — 안내 하나 때문에 도구 응답이 깨지면 안 된다.
    """
    try:
        expiry = licensing.expires_on()
        if expiry is None:
            return None  # 기간 없는 키(구매자) — 여기 올 일이 없다
        left = _days_left(expiry)
        if left < 0:
            return None  # 이미 끝났으면 잠금 안내가 따로 나간다

        # 지금 지나쳐 있는 시점들을 한꺼번에 처리한다. 하나씩 소진하면, 7일을 건너뛰고
        # 3일째에 처음 쓴 사람에게 같은 안내가 연달아 두 번 나간다(7이 먼저 걸려 뜨고,
        # 바로 다음 호출에서 4가 또 걸린다). 지난 시점은 지난 것이므로 한 번만 말하고
        # 전부 닫는다 — 남은 안내는 그 아래(1일)뿐이다.
        shown = _shown()
        due = {m for m in _MILESTONES if left <= m} - shown
        if not due:
            return None
        _mark(due)
        return _format(left, expiry)
    except Exception:
        return None


def _format(left: int, expiry: date) -> str:
    when = f"{expiry.month}월 {expiry.day}일"
    head = "오늘까지입니다" if left <= 0 else f"{left}일 남았습니다"
    line = f"ℹ️ StockLens 체험 기간이 {head} ({when}까지)."
    if licensing.PURCHASE_URL:
        line += f"\n   계속 쓰시려면 → {licensing.PURCHASE_URL}"
    return line


def append_notice(result):
    """도구 결과 뒤에 안내를 붙인다. 붙일 게 없거나 문자열이 아니면 그대로 돌려준다."""
    if not isinstance(result, str):
        return result
    notice = pending_notice()
    return f"{result}\n\n{notice}" if notice else result
