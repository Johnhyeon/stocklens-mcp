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
        "가지고 있는 첫 거래일로 옮겨 계산하면 엉뚱한 시기의 등락을 이 날짜의 반응처럼 "
        "보여주게 되어, 계산하지 않고 멈췄습니다."
    ),
    EVENT_AFTER_PRICE_HISTORY: "앞날짜이거나, 아직 반응을 볼 기간이 안 생겼습니다.",
    NO_PRICE_HISTORY: "주가를 한 건도 받지 못해 앞뒤 기간을 만들 수 없었습니다.",
    NO_TRADING_ACTIVITY: (
        "거래정지였거나 사려는 사람·팔려는 사람이 없던 기간입니다. "
        "주가가 안 움직인 것과는 다릅니다."
    ),
    INSUFFICIENT_POST_EVENT_HISTORY: (
        "기준 거래일 뒤로 거래된 날이 모자라 일부 시점은 등락률을 내지 못했습니다."
    ),
    FLOW_UNAVAILABLE_FOR_EVENT_WINDOW: (
        "이 기간의 기관·외국인 수급이 없습니다(수급은 최근 60거래일까지만 조회합니다). "
        "순매매 0이 아니라 데이터가 없는 것입니다."
    ),
    PROVIDER_ERROR: "데이터를 불러오지 못했습니다. 데이터가 없는 것과는 다르며 재시도 대상입니다.",
}

# 차단 사유별 첫 줄 — 고객이 겪은 상황을 그대로 말한다.
UNAVAILABLE_HEADLINES: dict[str, str] = {
    EVENT_BEFORE_PRICE_HISTORY: "이 날짜의 주가를 가지고 있지 않습니다",
    EVENT_AFTER_PRICE_HISTORY: "이 날짜 이후로 아직 거래된 날이 없습니다",
    NO_PRICE_HISTORY: "주가 데이터를 불러오지 못했습니다",
    NO_TRADING_ACTIVITY: "이 기간에 거래가 한 건도 없었습니다",
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


_KNOWN_ADJUSTMENTS = {"raw", "split_adjusted", "total_return_adjusted"}


def price_adjustment_meta(source_status: Any = None) -> dict:
    """이 종가가 무엇으로 조정된 값인지. 원천이 안 알려주면 "모른다"로 적는다.

    액면분할·유상증자 전후를 그냥 이어 붙이면 D-5 대비 D+1 수익률이 통째로
    허구가 된다. 그런데 지금까지 응답에는 이 가격이 수정주가인지 원주가인지
    적힌 곳이 없었다. 네이버는 조정 기준을 명시하지 않으므로 정답은 unknown이고,
    모른다고 적어야 6개월 전 반응과 지금을 나란히 놓기 전에 한 번 멈춘다.

    모르는 라벨을 그대로 실어 보내지 않는다. 확인된 값으로 오해된다.
    """
    status = source_status if source_status in _KNOWN_ADJUSTMENTS else "unknown"
    known = status != "unknown"
    return {
        "status": status,
        "corporate_actions_checked": known,
        "cross_event_comparison_safe": known,
    }


CORPORATE_ACTION_NOTE = (
    "기업행위(액면분할·유상증자·합병) 전후 구간은 이 수치로 이어 붙여 비교하면 "
    "안 됩니다. 원천이 조정 기준을 밝히지 않아 수정주가인지 확인되지 않았습니다."
)


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
    details: dict | None = None,
) -> dict:
    """수익률·수급 숫자를 만들지 않는 응답 골격."""
    blocked_flow = {
        "status": "unavailable",
        "days": 0,
        "institutional": None,
        "foreign": None,
        "reason": "event_window_unavailable",
    }
    validation = _validation(
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
    )
    validation.update(details or {})
    return {
        "code": code,
        "event_date": event_date,
        "basis_trading_date": None,
        "window": {"before": before, "after": after, "start_date": None, "end_date": None},
        "points": {},
        "volume": {"pre_avg": None, "basis": None, "post_avg": None},
        "flow": {"pre": dict(blocked_flow), "post": dict(blocked_flow)},
        "validation": validation,
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
    price_adjustment: str | None = None,
) -> dict:
    """OHLCV/수급 원자료를 event_date 기준 반응 요약 dict로 변환.

    분석 가능성을 먼저 판정하고, 통과하지 못하면 수익률·수급 숫자를 만들지 않는다.
    `flow_error`는 수급 공급자 조회 실패 사유(문자열)로, 데이터 부재와 구분된다.
    `price_adjustment`는 원천이 조정 기준을 밝히는 경우에만 넘긴다(기본 unknown).
    """
    reaction = _reaction_core(
        code=code,
        event_date=event_date,
        ohlcv=ohlcv,
        flows=flows,
        before=before,
        after=after,
        flow_error=flow_error,
    )
    # 분석 불가로 끝나도 '무슨 가격을 보고 그렇게 판정했는지'는 남아야 한다.
    reaction["price_adjustment"] = price_adjustment_meta(price_adjustment)
    return reaction


def _reaction_core(
    *,
    code: str,
    event_date: str,
    ohlcv: list[dict],
    flows: list[dict] | None = None,
    before: int = 5,
    after: int = 20,
    flow_error: str | None = None,
) -> dict:
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
        closes = {row["close"] for row in window_sessions}
        return _unavailable(
            code=code,
            event_date=target_date,
            codes=[NO_TRADING_ACTIVITY],
            earliest=earliest,
            latest=latest,
            before=before,
            after=after,
            next_tradable_date=resumed,
            # 거래정지인지 유동성 부재인지는 시세 데이터로 확정할 수 없다. 관측 사실만 남긴다.
            details={
                "no_trade_sessions": len(window_sessions),
                "no_trade_last_close": window_sessions[-1]["close"],
                "no_trade_close_unchanged": len(closes) == 1,
            },
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


_STATUS_LABELS = {
    "usable": "쓸 수 있음(usable)",
    "partial": "일부만 쓸 수 있음(partial)",
    "unavailable": "측정 불가(unavailable)",
}

_ALIGNMENT_LABELS = {
    ALIGN_EXACT: "입력한 날짜에 거래가 있어 그대로 씀(exact)",
    ALIGN_NEXT_SESSION: "입력한 날짜가 휴장일이라 다음 거래일로(next_trading_session)",
    ALIGN_FIRST_TRADABLE: "그날 거래가 없어 다시 거래된 날로(first_tradable_session_after_event)",
}


def _history_line(validation: dict) -> str:
    earliest = validation.get("earliest_available_date")
    latest = validation.get("latest_available_date")
    if not earliest or not latest:
        return "가지고 있는 주가 없음"
    return f"{earliest} ~ {latest}"


def _format_unavailable(reaction: dict) -> str:
    validation = reaction["validation"]
    codes = validation.get("codes") or []
    headline = next(
        (UNAVAILABLE_HEADLINES[c] for c in codes if c in UNAVAILABLE_HEADLINES),
        "이 기간은 계산할 수 없습니다",
    )
    lines = [
        f"# 이벤트 반응 — {reaction['code']} (event_date={reaction['event_date']})",
        "",
        f"**{headline}.**",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 판정 | {_STATUS_LABELS['unavailable']} |",
        f"| 입력한 날짜 | {reaction['event_date']} |",
        f"| 가지고 있는 주가 기간 | {_history_line(validation)} |",
    ]
    gap = validation.get("history_gap_calendar_days")
    if gap is not None:
        lines.append(f"| 얼마나 벗어났나 | {gap:,}일 |")
    sessions = validation.get("no_trade_sessions")
    if sessions:
        last_close = validation.get("no_trade_last_close")
        detail = f"{sessions}거래일 내내 거래량 0"
        if validation.get("no_trade_close_unchanged") and last_close is not None:
            detail += f", 종가 {_fmt_int(last_close)}원 그대로"
        lines.append(f"| 그 기간 거래 상황 | {detail} |")
    resumed = validation.get("next_tradable_date_outside_window")
    if resumed:
        resumed_gap = _calendar_gap(resumed, validation.get("event_date"))
        suffix = f" (입력한 날짜로부터 {resumed_gap:,}일 뒤)" if resumed_gap is not None else ""
        lines.append(f"| 거래가 다시 시작된 날 | {resumed}{suffix} |")
    lines.extend(["", "## 사유", ""])
    for item in validation["messages"]:
        lines.append(f"- `{item['code']}` — {item['message']}")
    lines.extend(
        [
            "",
            "_반응이 0이었다는 뜻이 아니라, 계산할 재료가 없다는 뜻입니다._",
        ]
    )
    if resumed:
        lines.append(
            "_거래가 다시 시작된 날은 보시려는 전후 기간을 한참 벗어나 있습니다. 그 날의 "
            "등락은 이 날짜에 대한 반응이 아니라 거래 재개 자체로 생긴 가격 조정이라 "
            "기준 거래일로 쓰지 않습니다._"
        )
    elif EVENT_BEFORE_PRICE_HISTORY in codes:
        lines.append(
            "_이 도구가 보는 주가는 일봉 500개(약 2년)까지입니다._"
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
            why = (
                "수급 조회에 실패했습니다"
                if row.get("reason") == "provider_error"
                else "수급은 최근 60거래일까지만 조회합니다"
            )
            flow_notes.append(
                f"- {label}: 이 기간 수급 데이터 없음 — {why} "
                f"(`{FLOW_UNAVAILABLE_FOR_EVENT_WINDOW}`)"
            )

    if flow_notes:
        lines.append("")
        lines.extend(flow_notes)
        lines.append("- 위 구간의 기관·외국인 값은 순매매 0이 아니라 데이터 없음입니다.")

    lines.extend(
        [
            "",
            "## 판정",
            "",
            "| 항목 | 값 |",
            "|---|---|",
            f"| 판정 | "
            f"{_STATUS_LABELS.get(validation.get('status'), validation.get('status'))} |",
            f"| 기준 거래일을 고른 방법 | "
            f"{_ALIGNMENT_LABELS.get(validation.get('alignment_reason'), '-')} |",
            f"| 입력한 날짜와 며칠 차이 | "
            f"{_fmt_int(validation.get('alignment_gap_calendar_days'))}일 |",
            f"| 가지고 있는 주가 기간 | {_history_line(validation)} |",
        ]
    )
    if validation.get("messages"):
        lines.extend(["", "### 같이 봐두실 점", ""])
        for item in validation["messages"]:
            lines.append(f"- `{item['code']}` — {item['message']}")

    lines.append("")
    lines.append(
        "_시간축: event_date는 DartLens의 공시 접수일을 그대로 넘기는 필드입니다. "
        "입력한 날짜가 휴장일이면 다음 거래일, 그날 거래가 없으면 거래가 다시 된 "
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
    lines.append(f"_{CORPORATE_ACTION_NOTE}_")
    return "\n".join(lines)
