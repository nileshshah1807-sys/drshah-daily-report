"""
US stock market session calculator — NYSE/NASDAQ regular hours in Eastern Time.

Computes whether the market is open, the session phase (open / pre-open /
closed / weekend / holiday) and the next event (open or close) with a
ticking countdown, including NYSE holidays (computed per year).
"""
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 — Windows without tzdata: fall back to EST
    import datetime as _dt
    ET = _dt.timezone(_dt.timedelta(hours=-5))


OPEN_MIN = 9 * 60 + 30        # 9:30 AM ET
CLOSE_MIN = 16 * 60           # 4:00 PM ET
PREOPEN_MIN = 4 * 60          # pre-market starts 4:00 AM ET

_CACHE = {"ts": 0.0, "result": None}


# ---------------------------------------------------------------------------
# NYSE holiday calendar (computed)
# ---------------------------------------------------------------------------

def _easter(year: int) -> date:
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
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon..6=Sun) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """NYSE observation rule: Sat→Fri, Sun→Mon."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _holidays(year: int) -> set[date]:
    return {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                    # MLK Day
        _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
        _easter(year) - timedelta(days=2),              # Good Friday
        _last_weekday(year, 5, 0),                      # Memorial Day
        _observed(date(year, 6, 19)),                   # Juneteenth
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(date(year, 12, 25)),                  # Christmas
    }


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _holidays(d.year)


def _fmt_duration(sec: float) -> str:
    sec = max(0, int(round(sec)))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m"
    return "<1m"


def _fmt_wd(d: date) -> str:
    return d.strftime("%a")


def market_status(now: datetime | None = None) -> dict:
    """Compute the market session state. `now` should be ET-aware (tests can
    inject a synthetic time); default = current Eastern time."""
    if now is None:
        now = datetime.now(ET)
    # small TTL cache (cheap but avoids recompute on every page request)
    ts = now.timestamp()
    # serve cache only for the same or later moment (never a "future" result)
    if 0 <= ts - _CACHE["ts"] < 30 and _CACHE["result"]:
        return _CACHE["result"]

    today = now.date()
    trading = is_trading_day(today)
    hm = now.hour * 60 + now.minute

    if trading and OPEN_MIN <= hm < CLOSE_MIN:
        phase = "open"
    elif trading and PREOPEN_MIN <= hm < OPEN_MIN:
        phase = "preopen"
    elif trading:
        phase = "closed"
    elif today.weekday() >= 5:
        phase = "weekend"
    else:
        phase = "holiday"

    # ---- next event ---------------------------------------------------------
    if phase == "open":
        event_dt = datetime(today.year, today.month, today.day, 16, 0, tzinfo=ET)
        prefix = "MARKET OPEN"
        next_label = "closes 4:00 PM"
        verb = "closes in"
    else:
        # next session open: today (pre-open, or a trading day before 4:00 AM
        # ET — that used to skip ahead to the next day) or the next trading day
        if trading and hm < OPEN_MIN:
            open_day = today
        else:
            open_day = today + timedelta(days=1)
            while not is_trading_day(open_day):
                open_day += timedelta(days=1)
        event_dt = datetime(open_day.year, open_day.month, open_day.day, 9, 30, tzinfo=ET)
        if phase == "preopen":
            prefix, next_label = "PRE-MARKET", "opens today 9:30 AM"
        elif phase == "weekend":
            prefix, next_label = "MARKET CLOSED · WEEKEND", f"opens {_fmt_wd(open_day)} 9:30 AM"
        elif phase == "holiday":
            prefix, next_label = "MARKET CLOSED · HOLIDAY", f"opens {_fmt_wd(open_day)} 9:30 AM"
        else:
            day_txt = "today" if open_day == today else _fmt_wd(open_day)
            prefix, next_label = "MARKET CLOSED", f"opens {day_txt} 9:30 AM"
        verb = "opens in"

    in_seconds = max(0, int((event_dt - now).total_seconds()))
    result = {
        "open": phase == "open",
        "phase": phase,
        "prefix": prefix,
        "next_label": next_label,
        "verb": verb,
        "in_seconds": in_seconds,
        "countdown": _fmt_duration(in_seconds),
        "next_at_epoch": int(event_dt.timestamp()),
        "now_et": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "trading_day": today.isoformat(),
    }
    _CACHE["ts"] = ts
    _CACHE["result"] = result
    return result
