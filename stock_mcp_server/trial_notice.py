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


# ── 세션 시작 시 모델에게 알리는 상태 ────────────────────────────────────────
#
# 위의 append_notice 는 7·4·1일에만 한 줄을 붙인다. 그 사이 기간에는 모델이 이 설치가
# 체험판인지조차 모른다. 그래서 "체험 언제 끝나?" 같은 질문에 아는 게 없어 답을 못
# 하거나, 없는 날짜를 지어낸다.
#
# MCP 서버 instructions 는 세션이 열릴 때 한 번 모델에게 전달된다. 여기에 사실 몇 줄과
# **말하지 않을 규칙**을 같이 넣는다. 규칙이 없으면 반대 문제가 생긴다 — 알게 된 모델이
# 매 답변마다 체험 이야기를 꺼내고, 그건 광고로 읽힌다. 우리가 파는 게 데이터 신뢰라서
# 그 손해가 더 크다.
#
# 구매자(기간 없는 키)에게는 아무것도 붙이지 않는다.
#
# 세션이 열릴 때 한 번 계산되므로, 자정을 넘겨 계속 켜둔 세션에서는 남은 날짜가 하루
# 지날 수 있다. 정확한 시점 안내는 도구 응답에 붙는 append_notice 쪽이 담당한다.

_TRIAL_DAYS = 14


def _trial_day(expiry: date) -> int | None:
    """체험 며칠차인가. 가이드의 14일 코스와 같은 번호여야 한다.

    키를 넣은 날이 1일차다. 그날은 남은 날짜가 14라서, 빼기만 하면 0이 나와 첫날에는
    아무 말도 못 하고 그 뒤로는 계속 하루씩 낮게 센다. 가이드는 "1일차 — 오늘 조건에
    맞는 종목 찾기"부터 시작하는데 모델이 다른 번호를 부르면 그날 할 일이 어긋난다.
    """
    day = _TRIAL_DAYS - _days_left(expiry) + 1
    return day if 1 <= day <= _TRIAL_DAYS else None


def session_context() -> str | None:
    """세션 instructions 뒤에 붙일 체험 상태. 체험이 아니거나 알 수 없으면 None."""
    try:
        expiry = licensing.expires_on()
        if expiry is None:
            return None  # 기간 없는 키 = 구매자

        left = _days_left(expiry)
        when = f"{expiry.year}년 {expiry.month}월 {expiry.day}일"

        lines = ["", "", "## 이 설치에 대해"]
        if left < 0:
            lines.append(f"이 설치는 LeetKit 체험판이고 {when}로 기간이 끝났다.")
        else:
            lines.append(
                f"이 설치는 LeetKit 14일 체험판이다. {when}까지 쓸 수 있고 {left}일 남았다."
            )
            day = _trial_day(expiry)
            if day:
                lines.append(f"오늘은 체험 {day}일차다.")

        lines += [
            "",
            "이 사실을 다룰 때 지킬 것:",
            "- 사용자가 묻지 않으면 체험이라는 말을 먼저 꺼내지 않는다. 매 답변에 붙이지 않는다.",
            "- 남은 기간·만료일·체험 며칠차인지 물으면 위 값으로 답한다. 날짜를 지어내지 않는다.",
            "- 구매를 권하지 않는다. 도구 응답 끝에 안내가 붙어 나오면 그것만 그대로 전달한다.",
            "- 무엇을 물어야 할지 모르겠다고 하면, 가이드의 14일 코스에서 그날 질문을 안내해도 된다.",
        ]
        return "\n".join(lines)
    except Exception:
        return None
