"""US market session helpers (NYSE calendar).

Lets the collector stay open permanently and only record during the cash
session (skipping weekends/holidays), capturing from the first tick at the open.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")
_CAL = mcal.get_calendar("NYSE")


def _now_et(now=None):
    if now is None:
        return datetime.now(ET)
    return now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)


def session_bounds(day=None):
    """(market_open, market_close) as tz-aware ET datetimes, or None on a non-trading day."""
    day = day or _now_et().date()
    sched = _CAL.schedule(start_date=str(day), end_date=str(day))
    if sched.empty:
        return None
    row = sched.iloc[0]
    return (
        row["market_open"].tz_convert(ET).to_pydatetime(),
        row["market_close"].tz_convert(ET).to_pydatetime(),
    )


def is_trading_day(day=None):
    return session_bounds(day) is not None


def is_market_open(now=None):
    now = _now_et(now)
    bounds = session_bounds(now.date())
    return bool(bounds and bounds[0] <= now <= bounds[1])


def next_open(now=None):
    """Next market open (tz-aware ET), scanning ~2 weeks ahead."""
    now = _now_et(now)
    for i in range(0, 14):
        bounds = session_bounds((now + pd.Timedelta(days=i)).date())
        if bounds and bounds[1] > now:
            return bounds[0]
    return None


def status(now=None):
    now = _now_et(now)
    if is_market_open(now):
        return f"OPEN ({now:%Y-%m-%d %H:%M:%S} ET)"
    nxt = next_open(now)
    return (f"CLOSED ({now:%H:%M:%S} ET); proxima apertura {nxt:%Y-%m-%d %H:%M} ET"
            if nxt else "CLOSED")


if __name__ == "__main__":
    print(status())
