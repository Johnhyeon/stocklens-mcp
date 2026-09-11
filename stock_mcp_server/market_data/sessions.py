"""거래소 세션 창. 기존 market_clock 캘린더(휴장·조기 폐장 포함)를 재사용한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from stock_mcp_server.market_clock import krx_calendar, us_calendar


@dataclass(frozen=True)
class SessionWindow:
    market: str
    day: date
    open_at: datetime
    close_at: datetime

    @property
    def minutes(self) -> int:
        return int((self.close_at - self.open_at).total_seconds() // 60)


def session_window(market: str, day: date) -> SessionWindow | None:
    """해당 거래일의 정규장 창. 휴장·주말이면 None."""
    if market == "KR":
        cal = krx_calendar()
    elif market == "US":
        cal = us_calendar()
    else:
        raise ValueError(f"지원하지 않는 market: {market}")

    if not cal.is_trading_day(day):
        return None

    hours = cal.hours_func(day) if cal.hours_func else cal.hours
    open_at = datetime.combine(day, hours.regular_open, tzinfo=cal.tz)
    close_at = datetime.combine(day, hours.regular_close, tzinfo=cal.tz)
    return SessionWindow(
        market=market, day=day, open_at=open_at, close_at=close_at)
