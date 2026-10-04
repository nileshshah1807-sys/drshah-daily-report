"""
Analysis orchestration — fetch + score a watchlist with a shared TTL cache
and an on-disk SNAPSHOT of the last results (stale-while-revalidate):
  • the dashboard can render the saved snapshot instantly on load
  • a full refresh then runs in the background and replaces it

Also adds to every result:
  • relative strength vs the S&P 500 (SPY is the benchmark)
  • SCORE HISTORY — one score per stock per trading session, stored in
    data_cache.db. The last 6 months are rebuilt from price history in the
    background, so trends ("score 71, up from 58 a month ago") and the
    "rating changed" alert work from day one.
  • the next earnings date, with a warning when it is less than a week away
"""
import json
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import analyzer
import data as data_layer
import market

ENGINE_VERSION = getattr(analyzer, "ENGINE_VERSION", 3)

import paths
BASE = paths.DATA_DIR
SNAP_DB = paths.CACHE_DB_PATH

ANALYSIS_CACHE: dict[str, tuple[float, dict]] = {}
ANALYSIS_TTL = 300          # seconds — in-memory freshness
SNAPSHOT_MAX_AGE_S = 7 * 86400   # keep saved results up to 7 days
_lock = threading.Lock()

BENCHMARK = "SPY"           # S&P 500 ETF — the relative-strength yardstick
HISTORY_SESSIONS = 126      # score history kept/rebuilt: ~6 months of sessions
EARNINGS_SOON_DAYS = 7      # warn when results are due within a week
BACKFILL_ENABLED = True     # background rebuild of past scores (off in tests)


# ---------------------------------------------------------------------------
# On-disk snapshot of the last analysis results + score history
# ---------------------------------------------------------------------------

def _snap_conn():
    conn = sqlite3.connect(SNAP_DB, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _snap_init():
    try:
        with _snap_conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS analysis_results(
                  ticker TEXT PRIMARY KEY,
                  payload TEXT NOT NULL,
                  computed_at REAL NOT NULL)""")
            # final = 1 → computed from that session's closing bar;
            # final = 0 → the current session (may still change intraday)
            c.execute("""
                CREATE TABLE IF NOT EXISTS score_history(
                  ticker TEXT NOT NULL,
                  date TEXT NOT NULL,
                  score REAL NOT NULL,
                  rating TEXT NOT NULL,
                  price REAL,
                  final INTEGER NOT NULL DEFAULT 0,
                  engine_v INTEGER NOT NULL,
                  PRIMARY KEY (ticker, date))""")
    except Exception:  # noqa: BLE001
        pass


_snap_init()


def _save_result_snapshot(ticker: str, result: dict) -> None:
    try:
        with _lock:
            with _snap_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO analysis_results (ticker, payload, computed_at) "
                    "VALUES (?,?,?)",
                    (ticker, json.dumps({**result, "computed_at": time.time()}, default=str),
                     time.time()))
    except Exception:  # noqa: BLE001
        pass


def _drop_result_snapshot(ticker: str) -> None:
    try:
        with _lock:
            with _snap_conn() as c:
                c.execute("DELETE FROM analysis_results WHERE ticker = ?", (ticker,))
    except Exception:  # noqa: BLE001
        pass


def _load_result_snapshot() -> dict[str, dict]:
    """Read persisted analysis results (stale-while-revalidate source)."""
    out = {}
    try:
        with _snap_conn() as c:
            rows = c.execute(
                "SELECT ticker, payload, computed_at FROM analysis_results").fetchall()
            c.execute("DELETE FROM analysis_results WHERE computed_at < ?",
                      (time.time() - SNAPSHOT_MAX_AGE_S,))
        now = time.time()
        for ticker, payload, computed_at in rows:
            try:
                r = json.loads(payload)
                if r.get("engine_v") != ENGINE_VERSION:
                    continue            # stale-format snapshot — ignore & refetch
                r["computed_at"] = r.get("computed_at") or computed_at
                r["computed_ago_s"] = int(now - (r.get("computed_at") or now))
                out[ticker] = r
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def load_saved_snapshot() -> dict[str, dict]:
    """Read the snapshot AND seed the in-memory cache with it (old timestamps
    mean the next full analysis refetches fresh data automatically)."""
    snap = _load_result_snapshot()
    with _lock:
        for tk, r in snap.items():
            if tk not in ANALYSIS_CACHE:
                ANALYSIS_CACHE[tk] = (r.get("computed_at") or time.time(), r)
    return snap


# ---------------------------------------------------------------------------
# Benchmark (S&P 500) for relative strength
# ---------------------------------------------------------------------------

_bench_lock = threading.Lock()
_bench_forced_at = 0.0


def benchmark_close(force: bool = False):
    """SPY daily closes, or None if they cannot be loaded (the relative-
    strength adjustment is then skipped). One fetch at a time: the other
    analysis threads wait and then read the cache instead of all calling Yahoo."""
    global _bench_forced_at
    with _bench_lock:
        refetch = force and time.time() - _bench_forced_at > 120
        try:
            df = data_layer.fetch_history(BENCHMARK, force=refetch)
        except Exception:  # noqa: BLE001
            return None
        if refetch:
            _bench_forced_at = time.time()
    return df["Close"] if df is not None and len(df) else None


# ---------------------------------------------------------------------------
# Score history
# ---------------------------------------------------------------------------

def _day(ts) -> str:
    return str(getattr(ts, "date", lambda: ts)())


def _store_scores(ticker: str, rows: list[tuple]) -> None:
    """rows: (date, score, rating, price, final)."""
    if not rows:
        return
    try:
        with _lock:
            with _snap_conn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO score_history "
                    "(ticker, date, score, rating, price, final, engine_v) VALUES (?,?,?,?,?,?,?)",
                    [(ticker, d, float(s), r, p, int(f), ENGINE_VERSION)
                     for d, s, r, p, f in rows])
    except Exception:  # noqa: BLE001
        pass


def _history(ticker: str, since: str | None = None) -> dict[str, dict]:
    """Stored scores of the current engine version, keyed by session date."""
    try:
        with _snap_conn() as c:
            q = ("SELECT date, score, rating, price, final FROM score_history "
                 "WHERE ticker = ? AND engine_v = ?")
            args = [ticker, ENGINE_VERSION]
            if since:
                q += " AND date >= ?"
                args.append(since)
            rows = c.execute(q + " ORDER BY date", args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {d: {"date": d, "score": s, "rating": r, "price": p, "final": bool(f)}
            for d, s, r, p, f in rows}


def _score_at(df, pos: int, bench) -> tuple:
    """Score the stock as it stood at the close of session `pos` of df."""
    sub = df.iloc[: pos + 1]
    r = analyzer.analyze(sub, benchmark=bench)
    return (_day(df.index[pos]), r["score"], r["rating"], r["price"], 1)


_ticker_locks: dict[str, threading.Lock] = {}


def _ticker_lock(ticker: str) -> threading.Lock:
    with _lock:
        return _ticker_locks.setdefault(ticker, threading.Lock())


def history_offsets(sessions: int | None = None) -> list[int]:
    """Which past sessions (counted back from the newest bar) get a rebuilt
    closing score: every session of the last month — the 1-day / 1-week /
    1-month comparisons — then every 3rd one, plus exactly 3 months ago.
    ~57 scores per stock instead of 126 keeps the rebuild quick."""
    sessions = sessions or HISTORY_SESSIONS
    offs = set(range(1, 23)) | set(range(24, sessions + 1, 3)) | {63}
    return sorted(o for o in offs if o <= sessions)


def backfill_history(ticker: str, df, bench, sessions: int | None = None) -> int:
    """Rebuild the missing closing scores of the sampled past sessions (the
    newest bar is the live session and is not touched). Returns how many
    were computed."""
    df = df.dropna(subset=["Close"])
    if len(df) < 121:
        return 0
    with _ticker_lock(ticker):
        positions = [len(df) - 1 - o for o in history_offsets(sessions)]
        positions = [p for p in positions if p >= 119]     # analyze() needs 120 bars
        if not positions:
            return 0
        have = _history(ticker, since=_day(df.index[min(positions)]))
        rows = [_score_at(df, pos, bench) for pos in sorted(positions)
                if not have.get(_day(df.index[pos]), {}).get("final")]
        _store_scores(ticker, rows)
        return len(rows)


def record_score(ticker: str, result: dict, df, bench) -> None:
    """Save today's (live) score and make sure the previous session holds
    its CLOSING score — its row may have been saved intraday."""
    df = df.dropna(subset=["Close"])
    rows = [(_day(df.index[-1]), result["score"], result["rating"], result["price"], 0)]
    if len(df) >= 122:
        prev = _history(ticker, since=_day(df.index[-2])).get(_day(df.index[-2]))
        if not (prev and prev["final"]):
            rows.append(_score_at(df, len(df) - 2, bench))
    _store_scores(ticker, rows)


def score_trend(ticker: str, df) -> dict:
    """Score / rating at the previous session and ~1 week / 1 month /
    3 months ago (5 / 21 / 63 sessions), from the stored history."""
    df = df.dropna(subset=["Close"])
    out = {"prev_date": None, "prev_score": None, "prev_rating": None,
           "w1": None, "m1": None, "m3": None}
    if len(df) < 2:
        return out
    hist = _history(ticker, since=_day(df.index[max(0, len(df) - 64)]))
    prev = hist.get(_day(df.index[-2]))
    if prev:
        out.update(prev_date=prev["date"], prev_score=prev["score"],
                   prev_rating=prev["rating"])
    for key, n in (("w1", 5), ("m1", 21), ("m3", 63)):
        if len(df) > n and _day(df.index[-1 - n]) in hist:
            out[key] = hist[_day(df.index[-1 - n])]["score"]
    return out


def score_history(ticker: str, df, bench) -> list[dict]:
    """The last ~6 months of daily scores for the detail chart (rebuilt on
    the spot if the background job has not reached this stock yet)."""
    try:
        backfill_history(ticker, df, bench)
    except Exception:  # noqa: BLE001 — the chart is optional
        pass
    df = df.dropna(subset=["Close"])
    since = _day(df.index[max(0, len(df) - 1 - HISTORY_SESSIONS)])
    return [{"date": h["date"], "score": h["score"], "rating": h["rating"]}
            for h in _history(ticker, since=since).values()]


# --- background rebuild ------------------------------------------------------

_backfill_q: "queue.Queue[str]" = queue.Queue()
_backfill_pending: set[str] = set()
_backfill_done_at: dict[str, float] = {}
_backfill_thread: threading.Thread | None = None


def _backfill_one(t: str) -> None:
    df = data_layer.fetch_history(t)
    backfill_history(t, df, benchmark_close())
    # the cached result was scored before this history existed — fill in
    # its trend so the dashboard shows it on the next load
    with _lock:
        hit = ANALYSIS_CACHE.get(t)
    if hit:
        hit[1]["score_trend"] = score_trend(t, df)
        _save_result_snapshot(t, hit[1])


def _backfill_worker() -> None:
    while True:
        t = _backfill_q.get()
        try:
            _backfill_one(t)
        except Exception:  # noqa: BLE001 — try again on a later analysis
            pass
        finally:
            with _lock:
                _backfill_pending.discard(t)
                _backfill_done_at[t] = time.time()
        time.sleep(0.2)             # stay light on the CPU next to live requests


def request_backfill(ticker: str) -> None:
    """Queue a background rebuild unless this stock already has (almost) a
    full history, is queued, or was rebuilt in the last 6 hours."""
    global _backfill_thread
    if not BACKFILL_ENABLED:
        return
    with _lock:
        if ticker in _backfill_pending or time.time() - _backfill_done_at.get(ticker, 0) < 6 * 3600:
            return
    try:
        with _snap_conn() as c:
            n = c.execute("SELECT COUNT(*) FROM score_history WHERE ticker = ? AND final = 1 "
                          "AND engine_v = ?", (ticker, ENGINE_VERSION)).fetchone()[0]
    except Exception:  # noqa: BLE001
        n = 0
    if n >= len(history_offsets()) - 3:
        return
    with _lock:
        _backfill_pending.add(ticker)
        if _backfill_thread is None or not _backfill_thread.is_alive():
            _backfill_thread = threading.Thread(target=_backfill_worker,
                                                name="score-history", daemon=True)
            _backfill_thread.start()
    _backfill_q.put(ticker)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _earnings_block(ticker: str) -> dict | None:
    try:
        iso = data_layer.get_next_earnings(ticker)
    except Exception:  # noqa: BLE001
        return None
    if not iso:
        return None
    days = (date.fromisoformat(iso) - datetime.now(market.ET).date()).days
    return {"date": iso, "days": days, "soon": 0 <= days <= EARNINGS_SOON_DAYS}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and v == v else None


def fundamentals_block(info: dict, price) -> dict | None:
    """Business-quality figures shown NEXT TO the technical score (they do
    not change it), plus a simple 6-point health check.

    P/E is worked out from today's price and the per-share earnings, so it
    stays current between the weekly profile refreshes.
    """
    f = (info or {}).get("fundamentals")
    if not f:
        return None
    kind = str(f.get("quote_type") or "EQUITY").upper()
    dy = _num(f.get("dividend_yield"))              # Yahoo already gives a percent
    if kind != "EQUITY":
        return {"applicable": False, "type": kind, "dividend_yield": dy}

    def pct(key):
        v = _num(f.get(key))
        return round(v * 100.0, 1) if v is not None else None

    eps, feps, px = _num(f.get("eps_ttm")), _num(f.get("eps_fwd")), _num(price)
    pe = round(px / eps, 1) if px and eps and eps > 0 else None
    fpe = round(px / feps, 1) if px and feps and feps > 0 else None
    de = _num(f.get("debt_to_equity"))
    de = round(de / 100.0, 2) if de is not None else None   # Yahoo: 78.4 → 0.78×
    rg, eg, pm, roe = pct("revenue_growth"), pct("earnings_growth"), \
        pct("profit_margin"), pct("roe")

    checks = [(name, ok) for name, ok, have in (
        ("Revenue growing", rg is not None and rg > 0, rg is not None),
        ("Earnings growing", eg is not None and eg > 0, eg is not None),
        ("Profit margin above 10%", pm is not None and pm > 10, pm is not None),
        ("Return on equity above 15%", roe is not None and roe > 15, roe is not None),
        ("Debt below 1.5× equity", de is not None and de < 1.5, de is not None),
        ("P/E between 0 and 35", pe is not None and pe < 35,
         pe is not None or (eps is not None and eps <= 0)),
    ) if have]
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    ratio = passed / total if total else 0
    label = ("Not enough data" if total < 3 else
             "Strong" if ratio >= 0.75 else "Fair" if ratio >= 0.5 else "Weak")
    return {"applicable": True, "pe": pe, "forward_pe": fpe,
            "revenue_growth": rg, "earnings_growth": eg, "profit_margin": pm,
            "roe": roe, "debt_to_equity": de, "dividend_yield": dy,
            "checks": [{"name": n, "ok": ok} for n, ok in checks],
            "passed": passed, "total": total, "label": label}


def decorate(t: str, df, result: dict, bench, record: bool = True,
             backfill_now: bool = False) -> dict:
    """Add name / sector / freshness / earnings / score trend to a result.
    `backfill_now` rebuilds missing past scores first (detail view), so the
    trend is complete even before the background job reaches this stock."""
    result["ticker"] = t
    info = data_layer.get_company_info(t)
    result["name"] = info.get("name") or t
    result["sector"] = info.get("sector")
    result["market_cap"] = data_layer.format_market_cap(info.get("market_cap"))
    result["fundamentals"] = fundamentals_block(info, result.get("price"))
    result["last_updated"] = str(getattr(df.index[-1], "date", lambda: df.index[-1])())
    # how current is this price data? (newest bar vs. newest expected session)
    try:
        result["data_status"] = data_layer.freshness(t, df)
        result["data_as_of"] = result["data_status"].get("as_of")
    except Exception:  # noqa: BLE001 — freshness is informative only
        result["data_status"] = None
        result["data_as_of"] = result["last_updated"]

    earn = _earnings_block(t)
    result["earnings"] = earn
    if earn and earn["soon"]:
        when = "today" if earn["days"] == 0 else f"in {earn['days']} day(s)"
        result["earnings_note"] = (f"Quarterly results are due {when} ({earn['date']}) — "
                                   f"prices can jump either way; consider waiting for the "
                                   f"results before starting a new position.")

    try:
        if backfill_now:
            backfill_history(t, df, bench)
        if record:
            record_score(t, result, df, bench)
        result["score_trend"] = score_trend(t, df)
    except Exception:  # noqa: BLE001 — history is informative only
        result["score_trend"] = None
    request_backfill(t)
    return result


def analyze_one(ticker: str, force: bool = False) -> dict:
    t = ticker.upper()
    with _lock:
        hit = ANALYSIS_CACHE.get(t)
        if hit and not force and time.time() - hit[0] < ANALYSIS_TTL:
            return hit[1]
    df = data_layer.fetch_history(t, force=force)
    bench = benchmark_close(force=force)
    result = analyzer.analyze(df, benchmark=bench)
    decorate(t, df, result, bench)
    with _lock:
        ANALYSIS_CACHE[t] = (time.time(), result)
    _save_result_snapshot(t, result)     # persist for instant dashboard loads
    return result


def summarize_freshness(results: dict) -> dict:
    """Header-level summary: which session the prices represent and whether
    every symbol already has the newest session (weekend/holiday aware)."""
    expected = data_layer.latest_expected_session().isoformat()
    as_of, stale, checked, unknown = None, [], 0, 0
    for r in (results or {}).values():
        if not isinstance(r, dict):
            continue
        st = r.get("data_status") if isinstance(r.get("data_status"), dict) else None
        day = (st or {}).get("as_of") or r.get("last_updated")
        checked += 1
        if day and (as_of is None or str(day) > str(as_of)):
            as_of = str(day)
        if st is None or st.get("current") is None:
            unknown += 1
            # no freshness block (e.g. an older saved snapshot) — compare the
            # date we do have against the expected session (ISO sorts as dates)
            if day and str(day) < expected:
                stale.append(r.get("ticker"))
        elif not st.get("current"):
            stale.append(st.get("ticker") or r.get("ticker"))
    return {
        "expected": expected,
        "as_of": as_of,
        "current": (not stale) if checked else None,
        "stale": sorted(x for x in stale if x),
        "checked": checked,
        "unknown": unknown,
    }


def analyze_watchlist(tickers: list[str], force: bool = False, progress=None) -> dict:
    results, errors = {}, {}
    done = 0
    total = len(tickers)
    if total == 0:
        return {"results": results, "errors": errors}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(analyze_one, t, force): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                results[t] = fut.result()
            except Exception as e:  # noqa: BLE001
                errors[t] = str(e)[:200]
                data_layer.invalidate(t)   # clear any bad cached frame
            done += 1
            if progress:
                progress(done, total, t)
    return {"results": results, "errors": errors}
