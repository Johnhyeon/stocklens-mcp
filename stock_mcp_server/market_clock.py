"""Market session calendar helpers for StockLens."""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# 서머타임 전환을 며칠 전부터 미리 알릴지. 전환 당일에 알면 이미 늦다 —
# 한국 사용자가 미국장 보려고 22:30에 켰는데 안 열려 있는 식이 된다.
DST_NOTICE_DAYS = 14


def viewer_now() -> datetime:
    """사용자 PC의 로컬 시각(타임존 포함).

    해외 구매자가 있다. 개장 시각 자체는 KST로 고정이지만, "다음 개장 2026-08-18"만
    보여주면 뉴욕 사용자에게는 현지 8/17 저녁이라 **날짜가 하루 어긋난다.**
    그래서 시장 시각과 사용자 현지 시각을 같이 낸다.
    """
    return datetime.now().astimezone()


def next_offset_change(tz, after: datetime, horizon_days: int = 400) -> dict | None:
    """`after` 이후 그 타임존의 UTC 오프셋이 바뀌는 첫 시점 (= 서머타임 전환).

    **DST 규칙을 코드에 박지 않는다.** 미국은 2007년에 전환일을 옮겼고 EU는 폐지를
    계속 논의 중이다. 규칙을 하드코딩하면 그날 조용히 1시간 틀린다. 대신 tzinfo를
    직접 탐침해서 IANA 데이터베이스가 갱신되면 자동으로 따라가게 한다.

    하루 단위로 훑어 바뀌는 구간을 찾고 그 안을 이분 탐색으로 좁힌다.
    한국(Asia/Seoul)은 현재 서머타임이 없어 None이 나온다.
    """
    base_utc = after.astimezone(UTC)
    current = base_utc.astimezone(tz).utcoffset()

    lo = base_utc
    hi = None
    for step in range(1, horizon_days + 1):
        probe = base_utc + timedelta(days=step)
        if probe.astimezone(tz).utcoffset() != current:
            hi = probe
            break
        lo = probe
    if hi is None:
        return None

    while (hi - lo) > timedelta(minutes=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == current:
            lo = mid
        else:
            hi = mid

    at = hi.astimezone(tz)
    shift = int((at.utcoffset() - current).total_seconds() // 60)
    return {
        "at": at.isoformat(timespec="minutes"),
        "days_until": max(0, (hi - base_utc).days),
        "shift_minutes": shift,
        "from_abbr": base_utc.astimezone(tz).tzname() or "",
        "to_abbr": at.tzname() or "",
        # 서머타임 '시작'이면 오프셋이 커지고(+60), '해제'면 작아진다(-60).
        "kind": "start" if shift > 0 else "end",
    }


def _fmt_duration(seconds: float | None) -> str | None:
    """초 → '2일 3시간' / '5시간 20분' / '12분'. 남은 시간은 타임존과 무관해서
    어디서 보든 그대로 읽힌다."""
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}일 {hours}시간" if hours else f"{days}일"
    if hours:
        return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"
    return f"{minutes}분"


@dataclass(frozen=True)
class SessionHours:
    pre_open: time | None
    regular_open: time
    regular_close: time
    after_close: time | None


KRX_HOURS = SessionHours(
    pre_open=time(8, 30),
    regular_open=time(9, 0),
    regular_close=time(15, 30),
    after_close=None,
)
US_HOURS = SessionHours(
    pre_open=time(4, 0),
    regular_open=time(9, 30),
    regular_close=time(16, 0),
    after_close=time(20, 0),
)


KRX_YEAR_SPECIFIC_HOLIDAYS = {
    2025: {
        date(2025, 1, 28): "Lunar New Year",
        date(2025, 1, 29): "Lunar New Year",
        date(2025, 1, 30): "Lunar New Year",
        date(2025, 3, 3): "Substitute holiday",
        date(2025, 5, 6): "Substitute holiday",
        date(2025, 6, 3): "Presidential Election Day",
        date(2025, 10, 6): "Chuseok",
        date(2025, 10, 7): "Chuseok",
        date(2025, 10, 8): "Substitute holiday",
    },
    2026: {
        date(2026, 2, 16): "Lunar New Year",
        date(2026, 2, 17): "Lunar New Year",
        date(2026, 2, 18): "Lunar New Year",
        date(2026, 3, 2): "Substitute holiday for Independence Movement Day",
        date(2026, 5, 25): "Buddha's Birthday",
        date(2026, 6, 3): "Election Day",
        date(2026, 8, 17): "Substitute holiday for Liberation Day",
        date(2026, 9, 24): "Chuseok",
        date(2026, 9, 25): "Chuseok",
        date(2026, 9, 28): "Substitute holiday for Chuseok",
    },
    2027: {
        date(2027, 2, 8): "Lunar New Year",
        date(2027, 2, 9): "Lunar New Year",
        date(2027, 2, 10): "Lunar New Year",
        date(2027, 5, 13): "Buddha's Birthday",
        date(2027, 8, 16): "Substitute holiday for Liberation Day",
        date(2027, 9, 14): "Chuseok",
        date(2027, 9, 15): "Chuseok",
        date(2027, 9, 16): "Chuseok",
        date(2027, 10, 4): "Substitute holiday for National Foundation Day",
    },
}


def get_market_clock(now: datetime | None = None) -> dict:
    """Return KRX and US equity market session state for a point in time."""
    base = _coerce_datetime(now)
    now_kst = base.astimezone(KST)
    now_et = base.astimezone(ET)
    krx = _build_market_state(
        market="KRX",
        timezone="Asia/Seoul",
        now_local=now_kst,
        hours=KRX_HOURS,
        holiday_name_func=_krx_holiday_name,
    )
    us = _build_market_state(
        market="NYSE/NASDAQ",
        timezone="America/New_York",
        now_local=now_et,
        hours=_us_hours(now_et.date()),
        holiday_name_func=_us_holiday_name,
    )
    viewer = base.astimezone(viewer_now().tzinfo)
    dst = _dst_outlook(base, viewer, {"krx": (KST, krx), "us": (ET, us)})
    return {
        "dst": dst,
        "source": "stocklens.market_clock",
        "now_kst": now_kst.isoformat(timespec="seconds"),
        "now_et": now_et.isoformat(timespec="seconds"),
        # 사용자가 어디서 보든 자기 시각을 기준으로 읽을 수 있게. 한국 사용자면
        "viewer": {                       # KST와 같아서 표시할 때만 걸러낸다.
            "now": viewer.isoformat(timespec="seconds"),
            "tz": viewer.tzname() or "",
            "utc_offset": viewer.strftime("%z"),
            "same_as_kst": viewer.utcoffset() == now_kst.utcoffset(),
        },
        "krx": krx,
        "us": us,
        "warnings": [
            "Holiday calendars include regular exchange holidays and known 2025-2027 KRX date-specific closures.",
            "Unexpected exchange closures are not knowable until the exchange announces them.",
        ],
    }


def _open_time_in(tz, market_tz, on_day: date, open_time: time) -> str:
    """특정 날짜의 개장 시각을 다른 타임존으로 환산해 'HH:MM' 로."""
    dt = datetime.combine(on_day, open_time, tzinfo=market_tz)
    return dt.astimezone(tz).strftime("%H:%M")


def _dst_outlook(now: datetime, viewer: datetime, markets: dict) -> dict:
    """서머타임 전환 예보.

    전환 자체는 ZoneInfo가 알아서 처리하므로 계산은 이미 맞다. 문제는 **예고가
    없다는 것**이다. 10/30에 "미국장 22:30 개장"이라 답하고 사흘 뒤 23:30으로
    바뀌는데 아무도 모른다.

    중요한 건 **사용자가 보는 개장 시각은 양쪽 전환에 다 흔들린다**는 점이다.
    시드니 사용자는 한국장(서머타임 없음)을 보는데도 자기 지역 서머타임이
    시작되면 개장이 현지 10:00 → 11:00으로 바뀐다. 그래서 시장 쪽 전환과
    사용자 쪽 전환 중 **먼저 오는 쪽**을 기준으로 예보한다.
    """
    viewer_change = next_offset_change(viewer.tzinfo, now)
    out: dict = {"viewer": dict(viewer_change, has_dst=True) if viewer_change
                 else {"has_dst": False}}

    for key, (tz, state) in markets.items():
        market_change = next_offset_change(tz, now)
        candidates = [(c, src) for c, src in
                      ((market_change, "market"), (viewer_change, "viewer")) if c]
        if not candidates:
            # 양쪽 다 서머타임이 없다(예: 한국 사용자 + 한국장).
            # '없다'는 사실도 명시해야 읽는 쪽에서 가정이 안 생긴다.
            out[key] = {"has_dst": False, "market_has_dst": False}
            continue

        change, cause = min(candidates, key=lambda cs: cs[0]["days_until"])
        after_day = datetime.fromisoformat(change["at"]).date() + timedelta(days=1)
        open_t = time.fromisoformat(state["regular_open"])
        before = _open_time_in(viewer.tzinfo, tz, now.astimezone(tz).date(), open_t)
        after = _open_time_in(viewer.tzinfo, tz, after_day, open_t)
        out[key] = dict(
            change,
            has_dst=True,
            market_has_dst=market_change is not None,
            cause=cause,                      # 시장 쪽 전환인지 사용자 쪽 전환인지
            imminent=change["days_until"] <= DST_NOTICE_DAYS,
            open_in_viewer_now=before,
            open_in_viewer_after=after,
            open_time_shifts=before != after,
        )
    return out


def dst_warnings(clock: dict) -> list[str]:
    """임박한 전환만 문장으로. 평소에는 아무 말도 하지 않는다."""
    msgs = []
    labels = {"krx": "한국장", "us": "미국장"}
    for key, label in labels.items():
        d = clock["dst"].get(key) or {}
        if not d.get("has_dst") or not d.get("imminent"):
            continue
        kind = "서머타임 시작" if d["kind"] == "start" else "서머타임 해제"
        who = "현지" if d.get("cause") == "viewer" else label
        line = (
            f"{who} {kind}까지 {d['days_until']}일 "
            f"({d['at'][:10]}, {d['from_abbr']}→{d['to_abbr']})"
        )
        if d.get("open_time_shifts"):
            line += (f" — 이후 {label} 개장이 사용자 기준 "
                     f"{d['open_in_viewer_now']} → {d['open_in_viewer_after']} 로 바뀝니다")
        else:
            line += f" — {label} 개장 시각은 그대로입니다"
        msgs.append(line)
    return msgs


def _market_lines(label: str, state: dict, viewer: dict) -> list[str]:
    out = [f"- {label}: {_status_label(state['status'])} ({state['reason']})"]
    if state["is_open"] and state.get("closes_in"):
        out.append(f"  마감까지 {state['closes_in']} 남음")
    elif state.get("opens_in"):
        out.append(f"  개장까지 {state['opens_in']} 남음")
    line = f"  최근 거래일: {state['last_trading_day']} / 다음 개장: {state['next_open_local']} (현지 시장시각)"
    out.append(line)
    # 사용자 타임존이 시장과 다를 때만 환산을 덧붙인다. 같은 값을 두 번 쓰면 노이즈다.
    if not viewer["same_as_kst"] or state["timezone"] != "Asia/Seoul":
        if state["next_open_viewer"][:16] != state["next_open_local"][:16]:
            out.append(f"  → 사용자 현지({viewer['tz'] or viewer['utc_offset']}) 기준 개장: {state['next_open_viewer']}")
    return out


def format_market_clock(clock: dict) -> str:
    krx, us, viewer = clock["krx"], clock["us"], clock["viewer"]
    lines = [
        "시장 캘린더",
        f"- 기준: {clock['now_kst']} KST / {clock['now_et']} ET",
    ]
    if not viewer["same_as_kst"]:
        lines.append(
            f"- 사용자 현지: {viewer['now']} ({viewer['tz'] or viewer['utc_offset']})"
            " — 아래 '개장까지 남은 시간'은 어디서 보든 동일합니다"
        )
    lines += _market_lines("한국장", krx, viewer)
    lines += _market_lines("미국장", us, viewer)
    for msg in dst_warnings(clock):
        lines.append(f"- ⏰ {msg}")
    lines += [
        "",
        "MARKET_CLOCK_JSON_START",
        json.dumps(clock, ensure_ascii=False, sort_keys=True),
        "MARKET_CLOCK_JSON_END",
    ]
    return "\n".join(lines)


def _build_market_state(
    *,
    market: str,
    timezone: str,
    now_local: datetime,
    hours: SessionHours,
    holiday_name_func,
) -> dict:
    current_date = now_local.date()
    holiday_name = holiday_name_func(current_date)
    is_weekend = current_date.weekday() >= 5
    is_trading_day = not is_weekend and holiday_name is None
    status, reason = _session_status(now_local.time(), hours, is_weekend, holiday_name)
    if is_trading_day and now_local.time() >= hours.regular_open:
        last_trading_day = current_date
    else:
        last_trading_day = _shift_trading_day(current_date, -1, holiday_name_func)
    if is_trading_day and now_local.time() < hours.regular_open:
        next_trading_day = current_date
    else:
        next_trading_day = _shift_trading_day(current_date, 1, holiday_name_func)

    is_open = status in {"regular"}
    tz = now_local.tzinfo
    next_open_dt = datetime.combine(next_trading_day, hours.regular_open, tzinfo=tz)
    close_dt = datetime.combine(current_date, hours.regular_close, tzinfo=tz)
    seconds_to_open = None if is_open else max(0.0, (next_open_dt - now_local).total_seconds())
    seconds_to_close = max(0.0, (close_dt - now_local).total_seconds()) if is_open else None

    # 사용자 현지 시각으로 환산한 개장 시점. 날짜가 하루 밀리는 경우가 흔하다.
    viewer = viewer_now()
    next_open_viewer = next_open_dt.astimezone(viewer.tzinfo)

    return {
        "market": market,
        "timezone": timezone,
        "status": status,
        "is_open": is_open,
        "is_trading_day": is_trading_day,
        "is_weekend": is_weekend,
        "is_holiday": holiday_name is not None,
        "reason": reason,
        "last_trading_day": last_trading_day.isoformat(),
        "next_trading_day": next_trading_day.isoformat(),
        "regular_open": hours.regular_open.strftime("%H:%M"),
        "regular_close": hours.regular_close.strftime("%H:%M"),
        # 아래 4개가 해외 사용자용. 남은 시간은 타임존 무관, 개장 시점은 양쪽 표기.
        "next_open_local": next_open_dt.isoformat(timespec="minutes"),
        "next_open_viewer": next_open_viewer.isoformat(timespec="minutes"),
        "opens_in": _fmt_duration(seconds_to_open),
        "closes_in": _fmt_duration(seconds_to_close),
    }


def _session_status(local_time: time, hours: SessionHours, is_weekend: bool, holiday_name: str | None) -> tuple[str, str]:
    if is_weekend:
        return "closed_weekend", "Weekend"
    if holiday_name:
        return "closed_holiday", holiday_name
    if hours.pre_open and hours.pre_open <= local_time < hours.regular_open:
        return "pre_market", "Before regular session"
    if local_time < hours.regular_open:
        return "closed_before_open", "Before pre-market or opening session"
    if hours.regular_open <= local_time < hours.regular_close:
        return "regular", "Regular session"
    if hours.after_close and hours.regular_close <= local_time < hours.after_close:
        return "after_hours", "After-hours session"
    return "closed_after_hours", "Regular session ended"


def _shift_trading_day(start: date, direction: int, holiday_name_func) -> date:
    cursor = start + timedelta(days=direction)
    while cursor.weekday() >= 5 or holiday_name_func(cursor) is not None:
        cursor += timedelta(days=direction)
    return cursor


def _coerce_datetime(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now


def _krx_holiday_name(day: date) -> str | None:
    fixed = {
        (1, 1): "New Year's Day",
        (3, 1): "Independence Movement Day",
        (5, 1): "Labor Day",
        (5, 5): "Children's Day",
        (6, 6): "Memorial Day",
        (8, 15): "Liberation Day",
        (10, 3): "National Foundation Day",
        (10, 9): "Hangul Day",
        (12, 25): "Christmas Day",
        (12, 31): "Year-end market closure",
    }
    if (day.month, day.day) in fixed:
        return fixed[(day.month, day.day)]
    return KRX_YEAR_SPECIFIC_HOLIDAYS.get(day.year, {}).get(day)


def _us_holiday_name(day: date) -> str | None:
    holidays = {
        _observed_fixed(day.year, 1, 1): "New Year's Day",
        _nth_weekday(day.year, 1, calendar.MONDAY, 3): "Martin Luther King Jr. Day",
        _nth_weekday(day.year, 2, calendar.MONDAY, 3): "Washington's Birthday",
        _good_friday(day.year): "Good Friday",
        _last_weekday(day.year, 5, calendar.MONDAY): "Memorial Day",
        _observed_fixed(day.year, 6, 19): "Juneteenth National Independence Day",
        _observed_fixed(day.year, 7, 4): "Independence Day",
        _nth_weekday(day.year, 9, calendar.MONDAY, 1): "Labor Day",
        _nth_weekday(day.year, 11, calendar.THURSDAY, 4): "Thanksgiving Day",
        _observed_fixed(day.year, 12, 25): "Christmas Day",
    }
    return holidays.get(day)


def _us_hours(day: date) -> SessionHours:
    if day in _us_early_close_days(day.year):
        return SessionHours(
            pre_open=US_HOURS.pre_open,
            regular_open=US_HOURS.regular_open,
            regular_close=time(13, 0),
            after_close=US_HOURS.after_close,
        )
    return US_HOURS


def _us_early_close_days(year: int) -> set[date]:
    days = {
        _nth_weekday(year, 11, calendar.THURSDAY, 4) + timedelta(days=1),
        date(year, 12, 24),
    }
    july_4 = date(year, 7, 4)
    if july_4.weekday() == calendar.SATURDAY:
        days.add(july_4 - timedelta(days=2))
    elif july_4.weekday() == calendar.SUNDAY:
        days.add(july_4 - timedelta(days=2))
    elif july_4.weekday() not in {calendar.MONDAY, calendar.FRIDAY}:
        days.add(july_4 - timedelta(days=1))
    return {day for day in days if day.weekday() < 5 and _us_holiday_name(day) is None}


def _observed_fixed(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == calendar.SATURDAY:
        return actual - timedelta(days=1)
    if actual.weekday() == calendar.SUNDAY:
        return actual + timedelta(days=1)
    return actual


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _good_friday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _status_label(status: str) -> str:
    return {
        "closed_weekend": "주말 휴장",
        "closed_holiday": "휴장",
        "closed_before_open": "개장 전",
        "pre_market": "장전",
        "regular": "정규장",
        "after_hours": "시간외",
        "closed_after_hours": "장마감",
    }.get(status, status)
