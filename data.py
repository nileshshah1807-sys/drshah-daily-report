"""
Data layer — fetches OHLCV history & company metadata from Yahoo Finance
via yfinance, with a two-level cache:
  • in-memory TTL cache  (fast per-request, keyed by (ticker, period))
  • on-disk SQLite cache (survives restarts → instant startup and ~90% fewer
    Yahoo requests; data_cache.db)

Cache keys include the requested PERIOD so a short validation fetch (e.g. 6mo)
can never poison the 3-year analysis data. All history bars are upserted into
one per-(ticker,date) table, so partial fetches only ever ADD rows.

FRESHNESS RULES (fixed 2026-09-23)
----------------------------------
1. A cached frame (memory or disk) is reused ONLY if it already contains the
   most recent expected NYSE session (weekends/holidays aware). If the newest
   bar is older than that, the app goes back to Yahoo instead of serving
   yesterday's price. (Before this fix the disk cache was considered "fresh
   enough" for 10 calendar days, so prices could stay 1-2 sessions behind.)
2. Yahoo sometimes publishes the newest daily candle with NULL prices — volume
   only, no open/high/low/close. Such bars used to be dropped, leaving the app
   one session behind. They are now rebuilt from intraday (1-minute, then
   5-minute) data, so the latest close is never missing.
3. Live re-fetches for the same symbol are throttled (LIVE_RETRY_GAP seconds)
   so a slow/rate-limited provider is never hammered; while waiting, the app
   serves the best data it already has instead of erroring.
"""
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

import market

import paths
BASE = paths.DATA_DIR
DB_PATH = paths.CACHE_DB_PATH

CACHE_TTL = 600            # seconds — in-memory freshness window
MIN_CACHE_ROWS = 30        # never cache suspiciously short/partial frames
DISK_MAX_AGE_DAYS = 10     # absolute sanity limit for serving disk data
HISTORY_KEEP_DAYS = 1500   # prune history rows older than ~4 years
FULL_PERIODS = ("3y", "2y", "1y")   # periods that may be served from disk

LIVE_RETRY_GAP = 180       # seconds — min gap between live Yahoo attempts / symbol
FINAL_AFTER_S = 600        # the daily bar is final ~10 min after the 4 PM ET close
SESSION_OPEN_GRACE = 10    # min — after 9:40 ET today's bar is expected to exist
INTRADAY_REPAIR_DAYS = 8   # how far back intraday data can repair missing bars

_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_lock = threading.Lock()
_dblock = threading.Lock()
_attempt_lock = threading.Lock()
_last_live_attempt: dict[str, float] = {}
INFO_CACHE: dict[str, tuple[float, dict]] = {}   # ticker → (expires_at, info)
INFO_TTL = 3600 * 24
META_MAX_AGE = 7 * 86400   # re-check name / sector / market cap on disk weekly
META_RETRY = 3600          # an incomplete Yahoo answer is retried after an hour
EARNINGS_TTL = 86400       # next-earnings date is re-checked once a day
_earn_cache: dict[str, tuple[float, str | None]] = {}   # ticker → (expires_at, iso date)

STATS = {
    "yahoo_history_calls": 0,
    "disk_hits": 0,
    "yahoo_info_calls": 0,
    "repaired_bars": 0,     # daily candles rebuilt from intraday data
    "stale_serves": 0,      # requests answered from cache while refreshing
    "live_attempts": 0,
    "yahoo_calendar_calls": 0,
}


# ---------------------------------------------------------------------------#
# Session / freshness helpers
# ---------------------------------------------------------------------------#

def _prev_trading_day(d: date) -> date:
    """Most recent NYSE session strictly before `d`."""
    d -= timedelta(days=1)
    while not market.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def latest_expected_session(now: datetime | None = None) -> date:
    """The newest session date whose daily bar should already exist.

    • trading day, 09:40 ET or later  → today  (bar exists live / after close)
    • trading day before 09:40 ET     → previous session
    • weekend / holiday               → previous session
    """
    now_et = (now or datetime.now(market.ET)).astimezone(market.ET)
    d = now_et.date()
    mins = now_et.hour * 60 + now_et.minute
    if market.is_trading_day(d) and mins >= market.OPEN_MIN + SESSION_OPEN_GRACE:
        return d
    return _prev_trading_day(d)


def _bar_dates(index) -> pd.DatetimeIndex:
    """Normalise any index (tz-aware or not) to tz-naive session dates."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(market.ET).tz_localize(None)
    return idx.normalize()


_PERIOD_DAYS = {"1y": 252, "2y": 504, "3y": 756}


def _trim_to_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Disk holds up to ~4 years of bars; hand back only the requested span."""
    n = _PERIOD_DAYS.get(period)
    if n and len(df) > n:
        return df.iloc[-n:]
    return df


def _last_bar_date(df: pd.DataFrame | None) -> date | None:
    if df is None or len(df) == 0:
        return None
    try:
        return _bar_dates(df.index)[-1].date()
    except Exception:  # noqa: BLE001
        return None


def has_session(df: pd.DataFrame | None, expected: date) -> bool:
    """True when the frame already contains the expected (newest) session."""
    d = _last_bar_date(df)
    return bool(d and d >= expected)


def _throttled(t: str, now: float) -> bool:
    with _attempt_lock:
        return (now - _last_live_attempt.get(t, 0.0)) < LIVE_RETRY_GAP


def _mark_attempt(t: str, now: float) -> None:
    with _attempt_lock:
        _last_live_attempt[t] = now


def freshness(ticker: str, df: pd.DataFrame | None = None) -> dict:
    """Data-freshness block for the UI: what the newest bar is vs. what we expect."""
    t = ticker.strip().upper()
    expected = latest_expected_session()
    d = _last_bar_date(df) if df is not None else _disk_latest_date(t)
    with _attempt_lock:
        last_try = _last_live_attempt.get(t)
    return {
        "ticker": t,
        "as_of": d.isoformat() if d else None,
        "expected": expected.isoformat(),
        # None = nothing loaded yet (unknown), False = loaded but behind
        "current": (d >= expected) if d else None,
        "sessions_behind": _sessions_between(d, expected) if d else None,
        "last_live_attempt": last_try,
    }


def _sessions_between(d: date, expected: date) -> int:
    """How many trading sessions the data is behind (0 = up to date)."""
    if d >= expected:
        return 0
    n, cur = 0, expected
    while cur > d and n < 40:
        n += 1
        cur = _prev_trading_day(cur)
    return n


# ---------------------------------------------------------------------------#
# Disk helpers (SQLite)
# ---------------------------------------------------------------------------#

def _disk_conn():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _disk_init():
    try:
        with _dblock:
            with _disk_conn() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS ohlcv(
                      ticker TEXT NOT NULL,
                      date   TEXT NOT NULL,
                      open   REAL, high REAL, low REAL, close REAL, volume REAL,
                      PRIMARY KEY (ticker, date))""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker ON ohlcv(ticker)")
                c.execute("""
                    CREATE TABLE IF NOT EXISTS meta(
                      ticker TEXT PRIMARY KEY,
                      name TEXT, sector TEXT, market_cap REAL, currency TEXT,
                      updated_at REAL)""")
                c.execute("""
                    CREATE TABLE IF NOT EXISTS earnings(
                      ticker TEXT PRIMARY KEY,
                      next_date TEXT,
                      checked_at REAL NOT NULL)""")
                cols = {r[1] for r in c.execute("PRAGMA table_info(meta)")}
                if "fundamentals" not in cols:       # added 25 Sep 2026
                    c.execute("ALTER TABLE meta ADD COLUMN fundamentals TEXT")
                # when each symbol's bars were last downloaded (added 3 Oct 2026)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS fetch_log(
                      ticker TEXT PRIMARY KEY,
                      fetched_at REAL NOT NULL)""")
    except Exception:  # noqa: BLE001 — cache must never break the app
        pass


_disk_init()


def _save_history_to_disk(t: str, df: pd.DataFrame) -> None:
    rows = [
        (t, d.strftime("%Y-%m-%d"), float(r["Open"]), float(r["High"]),
         float(r["Low"]), float(r["Close"]), float(r["Volume"]))
        for d, r in df.iterrows()
    ]
    if not rows:
        return
    try:
        with _dblock:
            with _disk_conn() as c:
                c.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?)", rows)
                cutoff = (pd.Timestamp.now() - pd.Timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
                c.execute("DELETE FROM ohlcv WHERE ticker = ? AND date < ?", (t, cutoff))
                c.execute("INSERT OR REPLACE INTO fetch_log (ticker, fetched_at) VALUES (?,?)",
                          (t, time.time()))
    except Exception:  # noqa: BLE001
        pass


def _load_history_from_disk(t: str) -> pd.DataFrame | None:
    try:
        with _dblock:
            with _disk_conn() as c:
                rows = c.execute(
                    "SELECT date, open, high, low, close, volume FROM ohlcv "
                    "WHERE ticker = ? ORDER BY date", (t,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    df.index = pd.to_datetime(df["date"])
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _disk_latest_date(t: str) -> date | None:
    try:
        with _dblock:
            with _disk_conn() as c:
                row = c.execute("SELECT MAX(date) FROM ohlcv WHERE ticker = ?", (t,)).fetchone()
        if not row or not row[0]:
            return None
        return pd.Timestamp(row[0]).date()
    except Exception:  # noqa: BLE001
        return None


def _disk_fetched_at(t: str) -> float | None:
    try:
        with _dblock:
            with _disk_conn() as c:
                row = c.execute("SELECT fetched_at FROM fetch_log WHERE ticker = ?",
                                (t,)).fetchone()
        return float(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


def _session_final_ts(day: date) -> float:
    """When the daily bar of `day` is final: the 4:00 PM ET close + a few minutes."""
    close = datetime(day.year, day.month, day.day, market.CLOSE_MIN // 60,
                     market.CLOSE_MIN % 60, tzinfo=market.ET)
    return close.timestamp() + FINAL_AFTER_S


def _disk_bar_settled(t: str, bar_day: date, now_ts: float | None = None) -> bool:
    """False when the newest bar on disk was downloaded while its session was
    still running and that session has since ended — e.g. the app was closed at
    11 AM New York time. Its "close" is then a mid-session price, and showing
    it the next morning as yesterday's close (wrong change %, wrong score)
    is exactly what must not happen: such a bar has to be downloaded again."""
    now_ts = time.time() if now_ts is None else now_ts
    final_at = _session_final_ts(bar_day)
    if now_ts < final_at:
        return True                 # session still open: nothing more final exists yet
    fetched = _disk_fetched_at(t)
    return fetched is not None and fetched >= final_at


def _disk_has_session(t: str, expected: date) -> bool:
    """The disk frame already contains the newest expected session — and that
    bar is final (or its session is still running) → no Yahoo call."""
    d = _disk_latest_date(t)
    if not d:
        return False
    if (pd.Timestamp.now().normalize() - pd.Timestamp(d)).days > DISK_MAX_AGE_DAYS + 5:
        return False
    return d >= expected and _disk_bar_settled(t, d)


def _save_meta_to_disk(t: str, info: dict) -> None:
    try:
        with _dblock:
            with _disk_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO meta (ticker, name, sector, market_cap, currency, "
                    "updated_at, fundamentals) VALUES (?,?,?,?,?,?,?)",
                    (t, info.get("name"), info.get("sector"),
                     info.get("market_cap"), info.get("currency"), time.time(),
                     json.dumps(info.get("fundamentals") or {})))
    except Exception:  # noqa: BLE001
        pass


def _load_meta_from_disk(t: str) -> dict | None:
    try:
        with _dblock:
            with _disk_conn() as c:
                row = c.execute(
                    "SELECT name, sector, market_cap, currency, updated_at, fundamentals "
                    "FROM meta WHERE ticker = ?", (t,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    try:
        fundamentals = json.loads(row[5]) if row[5] is not None else None
    except ValueError:
        fundamentals = None
    return {"name": row[0] or t, "sector": row[1],
            "market_cap": row[2], "currency": row[3] or "USD",
            "updated_at": row[4] or 0.0,
            "fundamentals": fundamentals}      # None = saved before fundamentals existed


# Yahoo profile fields kept for the fundamentals panel (raw values; the
# percentages / ratios are worked out in analysis.fundamentals_block)
FUNDAMENTAL_FIELDS = {
    "quoteType": "quote_type", "trailingEps": "eps_ttm", "forwardEps": "eps_fwd",
    "revenueGrowth": "revenue_growth", "earningsGrowth": "earnings_growth",
    "profitMargins": "profit_margin", "returnOnEquity": "roe",
    "debtToEquity": "debt_to_equity", "dividendYield": "dividend_yield",
    "trailingPE": "pe_ttm",
}


# ---------------------------------------------------------------------------#
# Intraday → daily bar repair
# ---------------------------------------------------------------------------#

def _intraday_daily(symbol: str) -> pd.DataFrame | None:
    """Aggregate recent intraday bars (regular hours, ET) into daily OHLCV rows.

    Tries 1-minute bars first (closest to the official close), then 5-minute.
    Returns a frame indexed by tz-naive session date, or None.
    """
    for interval in ("1m", "5m"):
        raw = None
        try:
            raw = yf.Ticker(symbol).history(period="5d", interval=interval,
                                            auto_adjust=False)
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None or raw.empty:
            continue
        try:
            raw = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if raw.empty:
                continue
            idx = pd.DatetimeIndex(raw.index)
            if idx.tz is not None:
                idx = idx.tz_convert(market.ET)
            work = raw.copy()
            work["_day"] = [x.date() for x in idx]
            g = work.groupby("_day").agg(
                Open=("Open", "first"), High=("High", "max"),
                Low=("Low", "min"), Close=("Close", "last"),
                Volume=("Volume", "sum"))
            g.index = pd.to_datetime(list(g.index))
            if len(g):
                return g
        except Exception:  # noqa: BLE001
            continue
    return None


def repair_daily_bars(symbol: str, df: pd.DataFrame | None,
                      cap: date | None = None) -> pd.DataFrame | None:
    """Yahoo regularly publishes its newest daily candle with NULL prices
    (volume only). Rebuild any such bar — and append any session that is
    completely missing — from intraday data so the latest close is real.

    `cap` limits filled/appended sessions (e.g. the newest expected NYSE
    session for equities); None means "whatever intraday data provides"
    (used for indices / 24-hour futures).
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    out.index = _bar_dates(out.index)
    if out.index.has_duplicates:
        # Yahoo sometimes ships the same session twice (e.g. a live row next to
        # the daily row). Merge them — last non-empty value per column — or the
        # per-date lookups below get a Series and raise "truth value ambiguous"
        out = out.groupby(level=0).last()

    # trailing rows whose prices are empty
    tail = list(out.index[-INTRADAY_REPAIR_DAYS:])
    missing = [d for d in tail if pd.isna(out.at[d, "Close"])]

    # only pay for an intraday request when something actually needs repairing:
    #  • the newest candle came back with NULL prices, or
    #  • the newest session row is missing from the daily feed completely
    need = bool(missing)
    if not need and cap is not None and len(out) and out.index.max().date() < cap:
        need = True
    if not need:
        return out

    intra = _intraday_daily(symbol)
    if intra is None or intra.empty:
        # nothing we can do — caller drops the empty rows as before
        return out

    filled = 0
    for d in missing:
        # never fill a session newer than the one we expect to exist (e.g. a
        # pre-market empty row for today while yesterday is still the last close)
        if cap is not None and d.date() > cap:
            continue
        if d in intra.index:
            for col in ("Open", "High", "Low", "Close", "Volume"):
                out.at[d, col] = intra.at[d, col]
            filled += 1

    # sessions present in intraday but absent from the daily frame altogether
    last_known = out.index.max()
    extra_rows = {}
    for d in intra.index:
        if d <= last_known:
            continue
        if cap is not None and d.date() > cap:
            continue
        row = intra.loc[d]
        if pd.isna(row["Close"]):
            continue
        extra_rows[d] = row
    if extra_rows:
        extra = pd.DataFrame.from_dict(extra_rows, orient="index")
        out = pd.concat([out, extra])
        out = out[~out.index.duplicated(keep="last")].sort_index()
        filled += len(extra_rows)

    if filled:
        STATS["repaired_bars"] += filled
    return out


# ---------------------------------------------------------------------------#
# History
# ---------------------------------------------------------------------------#

def fetch_history(ticker: str, period: str = "3y", force: bool = False) -> pd.DataFrame:
    """Daily OHLCV history (default 3 years — needed for the investment analysis).

    Serves the cache only while it contains the newest expected session;
    otherwise re-fetches from Yahoo (throttled per symbol) and repairs any
    NULL-priced newest candle from intraday data.
    """
    t = ticker.strip().upper()
    key = (t, period)
    now = time.time()
    expected = latest_expected_session()
    full = period in FULL_PERIODS

    # 1) in-memory TTL cache — must be both young AND up to the newest session
    with _lock:
        hit = _cache.get(key)
    if hit and not force and len(hit[1]) >= MIN_CACHE_ROWS \
            and now - hit[0] < CACHE_TTL and has_session(hit[1], expected):
        return hit[1].copy()

    # 2) on-disk cache (restart-fast path) — same "must hold newest session" rule
    if not force and full and _disk_has_session(t, expected):
        df = _load_history_from_disk(t)
        if df is not None and len(df) >= MIN_CACHE_ROWS and has_session(df, expected):
            df = _trim_to_period(df, period)      # never hand back more than asked
            STATS["disk_hits"] += 1
            with _lock:
                _cache[key] = (now, df)
            return df.copy()

    # 3) serve what we have if we tried Yahoo very recently (anti-hammering)
    if not force and _throttled(t, now):
        stale = hit[1] if (hit and len(hit[1]) >= MIN_CACHE_ROWS) else \
            (_load_history_from_disk(t) if full else None)
        if stale is not None and len(stale) >= MIN_CACHE_ROWS:
            STATS["stale_serves"] += 1
            with _lock:
                _cache[key] = (now, stale)
            return stale.copy()

    # 4) live fetch from Yahoo (one retry for transient failures)
    _mark_attempt(t, now)
    STATS["live_attempts"] += 1
    df = None
    for _attempt in range(2):
        try:
            STATS["yahoo_history_calls"] += 1
            df = yf.Ticker(t).history(period=period, interval="1d", auto_adjust=True)
        except Exception:  # noqa: BLE001
            df = None
        if df is not None and not df.empty:
            break
        time.sleep(1.5)

    if df is None or df.empty:
        # last resort: stale disk data beats nothing
        if not force:
            stale = _load_history_from_disk(t)
            if stale is not None and len(stale) >= MIN_CACHE_ROWS:
                with _lock:
                    _cache[key] = (now, stale)
                return stale.copy()
        raise ValueError(f"No market data found for '{t}'")

    df = df[["Open", "High", "Low", "Close", "Volume"]]
    # NEW: rebuild the newest candle when Yahoo ships it without prices
    df = repair_daily_bars(t, df, cap=expected)
    df = df.dropna(subset=["Close"])
    df.index = _bar_dates(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    _save_history_to_disk(t, df)          # upsert every bar (adds, never replaces)
    if len(df) >= MIN_CACHE_ROWS:
        with _lock:
            _cache[key] = (now, df)
    return df.copy()


def invalidate(ticker: str) -> None:
    """Drop cached data for a ticker (memory + disk) so the next attempt
    refetches fresh instead of reusing a bad frame."""
    t = ticker.strip().upper()
    with _lock:
        for key in [k for k in _cache if k[0] == t]:
            _cache.pop(key, None)
        INFO_CACHE.pop(t, None)
    with _attempt_lock:
        _last_live_attempt.pop(t, None)
    try:
        with _dblock:
            with _disk_conn() as c:
                c.execute("DELETE FROM ohlcv WHERE ticker = ?", (t,))
                c.execute("DELETE FROM meta WHERE ticker = ?", (t,))
                c.execute("DELETE FROM fetch_log WHERE ticker = ?", (t,))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------#
# Company metadata
# ---------------------------------------------------------------------------#

def get_company_info(ticker: str) -> dict:
    t = ticker.strip().upper()
    now = time.time()
    with _lock:
        hit = INFO_CACHE.get(t)
        if hit and now < hit[0]:
            return hit[1]

    # disk rows used to be served forever: one failed Yahoo lookup left a stock
    # without its name/sector for good, and market caps never updated
    disk = _load_meta_from_disk(t)
    if disk is not None and now - disk.pop("updated_at") < META_MAX_AGE \
            and disk.get("fundamentals") is not None:
        with _lock:
            INFO_CACHE[t] = (now + INFO_TTL, disk)
        return disk

    info = {"name": t, "sector": None, "market_cap": None, "currency": "USD",
            "fundamentals": None}
    try:
        STATS["yahoo_info_calls"] += 1
        tk = yf.Ticker(t)
        fi = tk.fast_info
        if fi.last_price is not None:
            info["market_cap"] = float(fi.market_cap) if fi.market_cap else None
            info["currency"] = str(fi.currency) if fi.currency else "USD"
        try:
            full = tk.info
            nm = full.get("shortName") or full.get("longName")
            if nm:
                info["name"] = str(nm)
            info["sector"] = full.get("sector")
            if full.get("marketCap"):
                info["market_cap"] = float(full["marketCap"])
            if nm:
                info["fundamentals"] = {
                    ours: full.get(theirs) for theirs, ours in FUNDAMENTAL_FIELDS.items()
                    if isinstance(full.get(theirs), (int, float, str))}
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    complete = info["name"] != t          # the full company profile answered
    if disk is not None:                  # never replace good data with a partial answer
        if not complete:
            info["name"] = disk.get("name") or t
            info["fundamentals"] = disk.get("fundamentals")
        info["sector"] = info["sector"] or disk.get("sector")
        info["market_cap"] = info["market_cap"] or disk.get("market_cap")
    if complete:
        _save_meta_to_disk(t, info)
    with _lock:
        INFO_CACHE[t] = (now + (INFO_TTL if complete else META_RETRY), info)
    return info


# ---------------------------------------------------------------------------#
# Next earnings date
# ---------------------------------------------------------------------------#

def _load_earnings_from_disk(t: str):
    try:
        with _dblock:
            with _disk_conn() as c:
                return c.execute("SELECT next_date, checked_at FROM earnings WHERE ticker = ?",
                                 (t,)).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _save_earnings_to_disk(t: str, next_date: str | None, checked_at: float) -> None:
    try:
        with _dblock:
            with _disk_conn() as c:
                c.execute("INSERT OR REPLACE INTO earnings (ticker, next_date, checked_at) "
                          "VALUES (?,?,?)", (t, next_date, checked_at))
    except Exception:  # noqa: BLE001
        pass


class _NoFundamentalsFilter(logging.Filter):
    """ETFs have no earnings calendar; yfinance logs that 404 as an ERROR in
    the console window every day. It is expected — hide just that message."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "No fundamentals data found" not in record.getMessage()


logging.getLogger("yfinance").addFilter(_NoFundamentalsFilter())


def get_next_earnings(ticker: str, today: date | None = None) -> str | None:
    """ISO date of the next quarterly results, or None (ETFs, unknown).

    Checked with Yahoo at most once a day per symbol (memory + disk cache),
    and again as soon as the stored date has passed. If Yahoo cannot be
    reached, the last known date is kept and the lookup retried in an hour.
    """
    t = ticker.strip().upper()
    now = time.time()
    today = today or datetime.now(market.ET).date()

    def upcoming(iso):
        return iso is None or date.fromisoformat(iso) >= today

    with _lock:
        hit = _earn_cache.get(t)
    if hit and now < hit[0] and upcoming(hit[1]):
        return hit[1]

    row = _load_earnings_from_disk(t)
    if row and now - (row[1] or 0) < EARNINGS_TTL and upcoming(row[0]):
        with _lock:
            _earn_cache[t] = (row[1] + EARNINGS_TTL, row[0])
        return row[0]

    try:
        STATS["yahoo_calendar_calls"] += 1
        cal = yf.Ticker(t).calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        days = sorted(pd.Timestamp(d).date() for d in (raw or []) if d is not None)
        ahead = [d for d in days if d >= today]
        found = ahead[0].isoformat() if ahead else None
    except Exception:  # noqa: BLE001 — Yahoo unreachable / rate limited
        kept = row[0] if row and row[0] and upcoming(row[0]) else None
        with _lock:
            _earn_cache[t] = (now + META_RETRY, kept)
        return kept

    _save_earnings_to_disk(t, found, now)
    with _lock:
        _earn_cache[t] = (now + EARNINGS_TTL, found)
    return found


def format_market_cap(mc: float | None) -> str:
    if not mc:
        return "—"
    b = mc / 1e9
    if b >= 1000:
        return f"${b / 1000:.2f}T"
    if b >= 1:
        return f"${b:.1f}B"
    return f"${mc / 1e6:.0f}M"
