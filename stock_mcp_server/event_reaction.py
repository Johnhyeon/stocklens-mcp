"""event_date 기준 주가·수급 반응 계산.

DartLens의 공시 접수일을 StockLens가 그대로 받아 주가/수급 시간축으로
해석하도록 만드는 얇은 계산 모듈이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


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


def _return_pct(close: float, base: float) -> float:
    if base == 0:
        return 0.0
    return round((close - base) / base * 100, 2)


def _avg(values: list[int | float]) -> int:
    return int(sum(values) / len(values)) if values else 0


def _point(label: str, row: dict, base_close: float) -> dict:
    close = float(row["close"])
    return {
        "label": label,
        "date": row["date"],
        "close": int(close) if close.is_integer() else close,
        "volume": int(row.get("volume") or 0),
        "return_pct": _return_pct(close, base_close),
    }


def _sum_flow(rows: list[dict]) -> dict:
    return {
        "days": len(rows),
        "institutional": sum(int(r.get("institutional") or 0) for r in rows),
        "foreign": sum(int(r.get("foreign") or 0) for r in rows),
    }


def build_event_reaction(
    *,
    code: str,
    event_date: str,
    ohlcv: list[dict],
    flows: list[dict] | None = None,
    before: int = 5,
    after: int = 20,
) -> dict:
    """OHLCV/수급 원자료를 event_date 기준 반응 요약 dict로 변환."""
    if not ohlcv:
        raise ValueError("OHLCV 데이터가 비어 있습니다.")

    before = max(0, int(before))
    after = max(0, int(after))
    target_date = normalize_date(event_date)
    bars = []
    for row in ohlcv:
        d = normalize_date(row.get("date"))
        if not d:
            continue
        item = dict(row)
        item["date"] = d
        bars.append(item)
    bars.sort(key=lambda r: r["date"])

    event_idx = next((i for i, row in enumerate(bars) if row["date"] >= target_date), None)
    if event_idx is None:
        raise ValueError(f"event_date({target_date}) 이후 거래일을 OHLCV에서 찾지 못했습니다.")

    basis = bars[event_idx]
    basis_date = basis["date"]
    base_close = float(basis["close"])

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
        idx = event_idx + offset
        if idx < 0 or idx >= len(bars):
            continue
        label = "D0" if offset == 0 else f"D{offset:+d}"
        points[label] = _point(label, bars[idx], base_close)

    start_idx = max(0, event_idx - before)
    end_idx = min(len(bars) - 1, event_idx + after)
    pre_bars = bars[start_idx:event_idx]
    post_bars = bars[event_idx : end_idx + 1]
    start_date = bars[start_idx]["date"]
    end_date = bars[end_idx]["date"]

    flow_rows = []
    for row in flows or []:
        d = normalize_date(row.get("date"))
        if not d:
            continue
        item = dict(row)
        item["date"] = d
        flow_rows.append(item)

    pre_flow = [r for r in flow_rows if start_date <= r["date"] < basis_date]
    post_flow = [r for r in flow_rows if basis_date <= r["date"] <= end_date]

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
            "pre_avg": _avg([int(r.get("volume") or 0) for r in pre_bars]),
            "basis": int(basis.get("volume") or 0),
            "post_avg": _avg([int(r.get("volume") or 0) for r in post_bars]),
        },
        "flow": {
            "pre": _sum_flow(pre_flow),
            "post": _sum_flow(post_flow),
        },
    }


def _fmt_int(value: int | float) -> str:
    return f"{int(value):,}"


def _fmt_signed(value: int | float) -> str:
    return f"{int(value):+,}"


def format_event_reaction(reaction: dict) -> str:
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
        lines.append(
            f"| {label} | {point['date']} | {_fmt_int(point['close'])} | "
            f"{point['return_pct']:+.2f}% | {_fmt_int(point['volume'])} |"
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
    for label, key in ((f"D-{window['before']}~D-1", "pre"), (f"D0~D+{window['after']}", "post")):
        row = reaction["flow"][key]
        lines.append(
            f"| {label} | {row['days']} | {_fmt_signed(row['institutional'])} | "
            f"{_fmt_signed(row['foreign'])} |"
        )

    lines.append("")
    lines.append(
        "_시간축: event_date는 DartLens의 공시 접수일을 그대로 넘기는 필드입니다. "
        "event_date가 휴장일이면 다음 거래일을 기준 거래일로 사용합니다._"
    )
    lines.append(
        "_반대 흐름: 주가·수급 이상 움직임에서 출발했다면 DartLens로 해당 기간 공시를 "
        "확인하세요. 이 도구는 원인 단정이나 매수/매도 판단이 아니라 시간축 정렬용입니다._"
    )
    return "\n".join(lines)
