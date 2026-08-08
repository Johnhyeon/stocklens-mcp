"""event_date 기준 주가·수급 반응 계산.

DartLens의 공시 접수일을 StockLens가 그대로 받아 주가/수급 시간축으로
해석하도록 만드는 얇은 계산 모듈이다.

**fail-closed 원칙**: 분석할 수 없는 사건창에서는 숫자를 만들지 않는다.
보유 데이터 구간 밖의 사건, 거래가 없는 구간, 수급 결측은 각각 다른 코드로
구분해 `validation`에 담고, 해당 수치는 `null`로 남긴다. 데이터 없음을
0%·기관 0·외국인 0 같은 정상 관측치로 바꿔 내보내지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# ── 검증 코드 ────────────────────────────────────────────────────────────
EVENT_BEFORE_PRICE_HISTORY = "EVENT_BEFORE_PRICE_HISTORY"
EVENT_AFTER_PRICE_HISTORY = "EVENT_AFTER_PRICE_HISTORY"
NO_PRICE_HISTORY = "NO_PRICE_HISTORY"
NO_TRADING_ACTIVITY = "NO_TRADING_ACTIVITY"
INSUFFICIENT_POST_EVENT_HISTORY = "INSUFFICIENT_POST_EVENT_HISTORY"
FLOW_UNAVAILABLE_FOR_EVENT_WINDOW = "FLOW_UNAVAILABLE_FOR_EVENT_WINDOW"
PROVIDER_ERROR = "PROVIDER_ERROR"

CODE_MESSAGES: dict[str, str] = {
    EVENT_BEFORE_PRICE_HISTORY: (
        "사건일이 보유 가격 데이터의 시작일보다 이릅니다. "
        "보유 구간의 첫 거래일을 기준일로 대체하지 않고 계산을 중단했습니다."
    ),
    EVENT_AFTER_PRICE_HISTORY: (
        "사건일 이후의 거래일이 보유 가격 데이터에 없습니다. "
        "미래 날짜이거나 아직 반응 구간이 형성되지 않았습니다."
    ),
    NO_PRICE_HISTORY: "가격 데이터가 비어 있어 사건창을 구성할 수 없습니다.",
    NO_TRADING_ACTIVITY: (
        "사건일 이후 요청한 사건창 안에 거래량이 양수인 거래일이 없습니다. "
        "거래정지·유동성 부재 가능성이며 시장 무반응과 다릅니다."
    ),
    INSUFFICIENT_POST_EVENT_HISTORY: (
        "사건 이후 거래 가능한 관측치가 부족해 일부 시점의 수익률을 계산하지 못했습니다."
    ),
    FLOW_UNAVAILABLE_FOR_EVENT_WINDOW: (
        "사건창과 겹치는 투자자 수급 행이 없습니다. 순매매 0이 아니라 데이터 없음입니다."
    ),
    PROVIDER_ERROR: "데이터 공급자 조회에 실패했습니다. 데이터 부재와 구분해 취급하세요.",
}

# 이 코드가 붙으면 수익률·수급 숫자를 일절 생성하지 않는다.
FATAL_CODES = frozenset(
    {
        EVENT_BEFORE_PRICE_HISTORY,
        EVENT_AFTER_PRICE_HISTORY,
        NO_PRICE_HISTORY,
        NO_TRADING_ACTIVITY,
    }
)

ALIGN_EXACT = "exact"
ALIGN_NEXT_SESSION = "next_trading_session"
ALIGN_FIRST_TRADABLE = "first_tradable_session_after_event"

_MISSING = "—"


def normalize_date(value: Any) -> str:
    """YYYYMMDD / YYYY.MM.DD / YYYY-MM-DD를 YYYY-MM-DD로 통일."""
    s = str(value or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        raw = digits[:8]
        try:
            datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return s[:10]
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return s[:10]


def _parse_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _calendar_gap(later: str | None, earlier: str | None) -> int | None:
    """later - earlier 달력일 차이. 어느 쪽이든 파싱 실패면 None."""
    a, b = _parse_day(later or ""), _parse_day(earlier or "")
    if a is None or b is None:
        return None
    return (a - b).days


def _return_pct(close: float, base: float) -> float | None:
    if not base:
        return None
    return round((close - base) / base * 100, 2)


def _avg(values: list[int | float]) -> int | None:
    return int(sum(values) / len(values)) if values else None


def _point(label: str, row: dict, base_close: float) -> dict:
    close = float(row["close"])
    return {
        "label": label,
        "date": row["date"],
        "close": int(close) if close.is_integer() else close,
        "volume": int(row.get("volume") or 0),
        "return_pct": _return_pct(close, base_close),
        "status": "available",
    }


def _null_point(label: str, code: str) -> dict:
    return {
        "label": label,
        "date": None,
        "close": None,
        "volume": None,
        "return_pct": None,
        "status": "unavailable",
        "code": code,
    }


def _normalize_bars(ohlcv: list[dict]) -> list[dict]:
    """날짜·종가·거래량이 성립하는 봉만 남기고 날짜 오름차순으로 정렬."""
    bars: list[dict] = []
    for row in ohlcv or []:
        d = normalize_date(row.get("date"))
        if not d or _parse_day(d) is None:
            continue
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        item = dict(row)
        item["date"] = d
        item["close"] = close
        try:
            item["volume"] = int(row.get("volume") or 0)
        except (TypeError, ValueError):
            item["volume"] = 0
        bars.append(item)
    bars.sort(key=lambda r: r["date"])
    return bars


def _normalize_flows(flows: list[dict] | None) -> list[dict]:
    rows: list[dict] = []
    for row in flows or []:
        d = normalize_date(row.get("date"))
        if not d or _parse_day(d) is None:
            continue
        item = dict(row)
        item["date"] = d
        rows.append(item)
    rows.sort(key=lambda r: r["date"])
    return rows


def _flow_segment(rows: list[dict], *, expected: bool, flow_error: str | None) -> dict:
    """수급 결측(데이터 없음)과 실제 순매매 0을 구분해 반환."""
    if rows:
        return {
            "status": "available",
            "days": len(rows),
            "institutional": sum(int(r.get("institutional") or 0) for r in rows),
            "foreign": sum(int(r.get("foreign") or 0) for r in rows),
        }
    if not expected:
        # 구간 자체가 없음(before=0 등) — 결측이 아니다.
        return {
            "status": "not_applicable",
            "days": 0,
            "institutional": None,
            "foreign": None,
            "reason": "empty_window",
        }
    return {
        "status": "unavailable",
        "days": 0,
        "institutional": None,
        "foreign": None,
        "reason": "provider_error" if flow_error else "no_rows_in_window",
    }


def _validation(
    *,
    status: str,
    codes: list[str],
    event_date: str,
    earliest: str | None,
    latest: str | None,
    basis_date: str | None,
    alignment_gap: int | None,
    alignment_reason: str | None,
    history_gap: int | None = None,
    next_tradable_date: str | None = None,
) -> dict:
    return {
        "status": status,
        "codes": codes,
        "messages": [{"code": c, "message": CODE_MESSAGES.get(c, c)} for c in codes],
        "event_date": event_date,
        "earliest_available_date": earliest,
        "latest_available_date": latest,
        "basis_trading_date": basis_date,
        "alignment_gap_calendar_days": alignment_gap,
        "alignment_reason": alignment_reason,
        "history_gap_calendar_days": history_gap,
        # 사건창 밖에서 거래가 재개된 경우의 첫 거래일 — 기준일로 쓰지 않는다.
        "next_tradable_date_outside_window": next_tradable_date,
    }


def _unavailable(
    *,
    code: str,
    event_date: str,
    codes: list[str],
    earliest: str | None,
    latest: str | None,
    before: int,
    after: int,
    history_gap: int | None = None,
    next_tradable_date: str | None = None,
) -> dict:
    """수익률·수급 숫자를 만들지 않는 응답 골격."""
    blocked_flow = {
        "status": "unavailable",
        "days": 0,
        "institutional": None,
        "foreign": None,
        "reason": "event_window_unavailable",
    }
    return {
        "code": code,
        "event_date": event_date,
        "basis_trading_date": None,
        "window": {"before": before, "after": after, "start_date": None, "end_date": None},
        "points": {},
        "volume": {"pre_avg": None, "basis": None, "post_avg": None},
        "flow": {"pre": dict(blocked_flow), "post": dict(blocked_flow)},
        "validation": _validation(
            status="unavailable",
            codes=codes,
            event_date=event_date,
            earliest=earliest,
            latest=latest,
            basis_date=None,
            alignment_gap=None,
            alignment_reason=None,
            history_gap=history_gap,
            next_tradable_date=next_tradable_date,
        ),
    }


def build_event_reaction(
    *,
    code: str,
    event_date: str,
    ohlcv: list[dict],
    flows: list[dict] | None = None,
    before: int = 5,
    after: int = 20,
    flow_error: str | None = None,
) -> dict:
    """OHLCV/수급 원자료를 event_date 기준 반응 요약 dict로 변환.

    분석 가능성을 먼저 판정하고, 통과하지 못하면 수익률·수급 숫자를 만들지 않는다.
    `flow_error`는 수급 공급자 조회 실패 사유(문자열)로, 데이터 부재와 구분된다.
    """
    before = max(0, int(before))
    after = max(0, int(after))
    target_date = normalize_date(event_date)
    if _parse_day(target_date) is None:
        raise ValueError(f"event_date({event_date})를 날짜로 해석할 수 없습니다.")

    bars = _normalize_bars(ohlcv)
    if not bars:
        return _unavailable(
            code=code,
            event_date=target_date,
            codes=[NO_PRICE_HISTORY],
            earliest=None,
            latest=None,
            before=before,
            after=after,
        )

    earliest = bars[0]["date"]
    latest = bars[-1]["date"]

    # R1 — 보유 구간보다 이른 사건은 첫 보유 거래일로 대체하지 않고 거절한다.
    if target_date < earliest:
        return _unavailable(
            code=code,
            event_date=target_date,
            codes=[EVENT_BEFORE_PRICE_HISTORY],
            earliest=earliest,
            latest=latest,
            before=before,
            after=after,
            history_gap=_calendar_gap(earliest, target_date),
        )

    candidates = [row for row in bars if row["date"] >= target_date]
    if not candidates:
        return _unavailable(
            code=code,
            event_date=target_date,
            codes=[EVENT_AFTER_PRICE_HISTORY],
            earliest=earliest,
            latest=latest,
            before=before,
            after=after,
            history_gap=_calendar_gap(target_date, latest),
        )

    # R2 — 가격 발견이 실제로 일어난 봉(거래량 > 0)만 기준일·오프셋 계산에 쓴다.
    # 거래 재개는 **요청한 사건창(D0~D+after 세션) 안**에서만 인정한다. 2년 뒤 재개된
    # 거래를 그 사건의 반응으로 정렬하면 무거래 0.00%와 똑같은 종류의 허위 분석이 된다.
    tradable = [row for row in bars if row["volume"] > 0]
    window_sessions = candidates[: max(1, after + 1)]
    basis = next((row for row in window_sessions if row["volume"] > 0), None)
    if basis is None:
        resumed = next((row["date"] for row in tradable if row["date"] >= target_date), None)
        return _unavailable(
            code=code,
            event_date=target_date,
            codes=[NO_TRADING_ACTIVITY],
            earliest=earliest,
            latest=latest,
            before=before,
            after=after,
            next_tradable_date=resumed,
        )

    basis_date = basis["date"]
    base_close = float(basis["close"])
    basis_idx = tradable.index(basis)

    if basis_date == target_date:
        alignment_reason = ALIGN_EXACT
    elif candidates[0]["date"] == basis_date:
        alignment_reason = ALIGN_NEXT_SESSION
    else:
        # 사건일 당일(또는 이월된 첫 세션)에 거래가 없어 재개일을 기준으로 삼았다.
        alignment_reason = ALIGN_FIRST_TRADABLE

    codes: list[str] = []

    offsets = [-before, 0]
    offsets.extend(v for v in (1, 5, 20) if v <= after)
    if after not in offsets:
        offsets.append(after)

    points: dict[str, dict] = {}
    seen_offsets: set[int] = set()
    for offset in offsets:
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        label = "D0" if offset == 0 else f"D{offset:+d}"
        idx = basis_idx + offset
        if 0 <= idx < len(tradable):
            points[label] = _point(label, tradable[idx], base_close)
            continue
        if offset > 0:
            # 사건 이후 거래 가능한 관측치 부족 — 0%가 아니라 null.
            points[label] = _null_point(label, INSUFFICIENT_POST_EVENT_HISTORY)
            if INSUFFICIENT_POST_EVENT_HISTORY not in codes:
                codes.append(INSUFFICIENT_POST_EVENT_HISTORY)
        # 사건 이전 구간 부족은 기존과 동일하게 지점 자체를 생략한다.

    start_idx = max(0, basis_idx - before)
    end_idx = min(len(tradable) - 1, basis_idx + after)
    pre_bars = tradable[start_idx:basis_idx]
    post_bars = tradable[basis_idx : end_idx + 1]
    start_date = tradable[start_idx]["date"]
    end_date = tradable[end_idx]["date"]

    flow_rows = _normalize_flows(flows)
    pre_flow_rows = [r for r in flow_rows if start_date <= r["date"] < basis_date]
    post_flow_rows = [r for r in flow_rows if basis_date <= r["date"] <= end_date]

    flow = {
        "pre": _flow_segment(pre_flow_rows, expected=bool(pre_bars), flow_error=flow_error),
        "post": _flow_segment(post_flow_rows, expected=True, flow_error=flow_error),
    }
    if any(seg["status"] == "unavailable" for seg in flow.values()):
        codes.append(FLOW_UNAVAILABLE_FOR_EVENT_WINDOW)
    if flow_error and PROVIDER_ERROR not in codes:
        codes.append(PROVIDER_ERROR)

    status = "partial" if codes else "usable"

    return {
        "code": code,
        "event_date": target_date,
        "basis_trading_date": basis_date,
        "window": {
            "before": before,
            "after": after,
            "start_date": start_date,
            "end_date": end_date,
        },
        "points": points,
        "volume": {
            "pre_avg": _avg([row["volume"] for row in pre_bars]),
            "basis": int(basis["volume"]),
            "post_avg": _avg([row["volume"] for row in post_bars]),
        },
        "flow": flow,
        "validation": _validation(
            status=status,
            codes=codes,
            event_date=target_date,
            earliest=earliest,
            latest=latest,
            basis_date=basis_date,
            alignment_gap=_calendar_gap(basis_date, target_date),
            alignment_reason=alignment_reason,
        ),
    }


def _fmt_int(value: int | float | None) -> str:
    if value is None:
        return _MISSING
    return f"{int(value):,}"


def _fmt_signed(value: int | float | None) -> str:
    if value is None:
        return _MISSING
    return f"{int(value):+,}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return _MISSING
    return f"{value:+.2f}%"


_ALIGNMENT_LABELS = {
    ALIGN_EXACT: "사건일 당일 거래(exact)",
    ALIGN_NEXT_SESSION: "휴장 이월 — 다음 거래일(next_trading_session)",
    ALIGN_FIRST_TRADABLE: "사건일 무거래 — 거래 재개일(first_tradable_session_after_event)",
}


def _history_line(validation: dict) -> str:
    earliest = validation.get("earliest_available_date")
    latest = validation.get("latest_available_date")
    if not earliest or not latest:
        return "보유 가격 데이터 없음"
    return f"{earliest} ~ {latest}"


def _format_unavailable(reaction: dict) -> str:
    validation = reaction["validation"]
    lines = [
        f"# 이벤트 반응 — {reaction['code']} (event_date={reaction['event_date']})",
        "",
        "**분석 불가 — 수익률·수급 숫자를 생성하지 않았습니다.**",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        "| 검증 상태 | `unavailable` |",
        f"| 사건일 | {reaction['event_date']} |",
        f"| 보유 가격 데이터 구간 | {_history_line(validation)} |",
    ]
    gap = validation.get("history_gap_calendar_days")
    if gap is not None:
        lines.append(f"| 사건일-데이터 경계 차이 | {gap:,}일 |")
    resumed = validation.get("next_tradable_date_outside_window")
    if resumed:
        resumed_gap = _calendar_gap(resumed, validation.get("event_date"))
        suffix = f" (사건일 +{resumed_gap:,}일)" if resumed_gap is not None else ""
        lines.append(f"| 사건창 밖 거래 재개일 | {resumed}{suffix} |")
    lines.extend(["", "## 사유", ""])
    for item in validation["messages"]:
        lines.append(f"- `{item['code']}` — {item['message']}")
    lines.extend(
        [
            "",
            "_이 응답에는 D0/D+ 수익률, 기관·외국인 순매매 값이 없습니다. "
            "숫자가 없는 것은 반응이 0이라는 뜻이 아니라 측정 자체가 불가능하다는 뜻입니다._",
        ]
    )
    if resumed:
        lines.append(
            "_거래가 재개된 날은 사건창 밖입니다. 그 날의 등락은 이 사건의 반응이 아니라 "
            "거래재개 자체의 가격 조정이므로 기준일로 쓰지 않습니다._"
        )
    else:
        lines.append(
            "_보유 구간 밖 사건은 첫 보유 거래일로 대체하지 않습니다. "
            "더 이른 사건은 현재 조회 범위(일봉 500개)로 측정할 수 없습니다._"
        )
    return "\n".join(lines)


def format_event_reaction(reaction: dict) -> str:
    validation = reaction.get("validation") or {}
    if validation.get("status") == "unavailable":
        return _format_unavailable(reaction)

    code = reaction["code"]
    event_date = reaction["event_date"]
    basis_date = reaction["basis_trading_date"]
    window = reaction["window"]

    lines = [
        f"# 이벤트 반응 — {code} (event_date={event_date}, 기준 거래일={basis_date})",
        "",
        f"분석 구간: {window['start_date']} ~ {window['end_date']} "
        f"(D-{window['before']} ~ D+{window['after']} 거래일)",
        "",
        "| 지점 | 거래일 | 종가 | 기준일 대비 | 거래량 |",
        "|---|---|---:|---:|---:|",
    ]
    for label, point in reaction["points"].items():
        if point.get("status") == "unavailable":
            cells = " | ".join([_MISSING] * 4)
            lines.append(f"| {label} | {cells} |")
            continue
        lines.append(
            f"| {label} | {point['date']} | {_fmt_int(point['close'])} | "
            f"{_fmt_pct(point['return_pct'])} | {_fmt_int(point['volume'])} |"
        )

    vol = reaction["volume"]
    lines.extend(
        [
            "",
            "## 거래량",
            "",
            "| 구간 | 평균/당일 거래량 |",
            "|---|---:|",
            f"| D-{window['before']}~D-1 평균 | {_fmt_int(vol['pre_avg'])} |",
            f"| D0 당일 | {_fmt_int(vol['basis'])} |",
            f"| D0~D+{window['after']} 평균 | {_fmt_int(vol['post_avg'])} |",
            "",
            "## 수급",
            "",
            "| 구간 | 일수 | 기관 순매매 | 외국인 순매매 |",
            "|---|---:|---:|---:|",
        ]
    )
    flow_notes: list[str] = []
    for label, key in ((f"D-{window['before']}~D-1", "pre"), (f"D0~D+{window['after']}", "post")):
        row = reaction["flow"][key]
        if row["status"] == "available":
            lines.append(
                f"| {label} | {row['days']} | {_fmt_signed(row['institutional'])} | "
                f"{_fmt_signed(row['foreign'])} |"
            )
            continue
        lines.append(f"| {label} | {_MISSING} | {_MISSING} | {_MISSING} |")
        if row["status"] == "unavailable":
            flow_notes.append(
                f"- {label}: 해당 사건창 수급 데이터 없음 (`{FLOW_UNAVAILABLE_FOR_EVENT_WINDOW}`)"
            )

    if flow_notes:
        lines.append("")
        lines.extend(flow_notes)
        lines.append("- 위 구간의 기관·외국인 값은 순매매 0이 아니라 데이터 없음입니다.")

    lines.extend(
        [
            "",
            "## 검증",
            "",
            "| 항목 | 값 |",
            "|---|---|",
            f"| 검증 상태 | `{validation.get('status')}` |",
            f"| 기준일 정렬 | {_ALIGNMENT_LABELS.get(validation.get('alignment_reason'), '-')} |",
            f"| 사건일→기준 거래일 차이 | "
            f"{_fmt_int(validation.get('alignment_gap_calendar_days'))}일 |",
            f"| 보유 가격 데이터 구간 | {_history_line(validation)} |",
        ]
    )
    if validation.get("messages"):
        lines.extend(["", "### 경고", ""])
        for item in validation["messages"]:
            lines.append(f"- `{item['code']}` — {item['message']}")

    lines.append("")
    lines.append(
        "_시간축: event_date는 DartLens의 공시 접수일을 그대로 넘기는 필드입니다. "
        "event_date가 휴장일이면 다음 거래일, 사건일에 거래가 없으면 거래가 재개된 "
        "첫 날을 기준 거래일로 사용합니다._"
    )
    lines.append(
        f"_`{_MISSING}` 표기는 데이터 없음이며 0이 아닙니다. D+ 지점은 거래량이 양수인 "
        "거래일만 세어 산정합니다._"
    )
    lines.append(
        "_반대 흐름: 주가·수급 이상 움직임에서 출발했다면 DartLens로 해당 기간 공시를 "
        "확인하세요. 이 도구는 원인 단정이나 매수/매도 판단이 아니라 시간축 정렬용입니다._"
    )
    return "\n".join(lines)
