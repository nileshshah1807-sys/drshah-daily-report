"""
"Does the score work?" — replays the scoring engine over the watchlist's own
3 years of daily history and checks what happened NEXT.

Every STEP sessions (≈ 2 weeks) each stock is scored exactly as the app would
have scored it on that day (only data up to that day is used), then its
return over the following 1 and 3 months is compared with the S&P 500 (SPY).
Signals are grouped by rating: if the model is useful, "Strong Buy" / "Buy"
days should have been followed by better returns than "Sell" / "Strong Sell".

Caveats shown with the result: today's watchlist (stocks often picked BECAUSE
they did well), overlapping periods, no costs/taxes, past ≠ future.
"""
import json
import logging
import threading
import time

import pandas as pd

import analysis
import data as data_layer

STEP = 10                     # score every 10th session (~2 weeks)
WARMUP = 260                  # the long moving averages need ~1 year of bars first
HORIZONS = (("1M", 21), ("3M", 63))
RATINGS = ["Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"]
MIN_GROUP = 10                # signals per side needed before judging

log = logging.getLogger("backtest")
_state = {"running": False, "done": 0, "total": 0, "error": None, "started_at": None}
_state_lock = threading.Lock()

CAVEATS = [
    "Uses today's watchlist — often stocks chosen because they already did well.",
    "Signals every 2 weeks overlap (a 3-month window spans several signals), so "
    "they are not independent.",
    "No trading costs, taxes or currency effects; past results do not guarantee "
    "future returns.",
]


# ---------------------------------------------------------------------------
# Storage (one saved result in data_cache.db)
# ---------------------------------------------------------------------------

def _init() -> None:
    try:
        with analysis._snap_conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS backtest_result(
                           id INTEGER PRIMARY KEY CHECK (id = 1),
                           payload TEXT NOT NULL, computed_at REAL NOT NULL)""")
    except Exception:  # noqa: BLE001
        pass


def _save(result: dict) -> None:
    _init()
    with analysis._snap_conn() as c:
        c.execute("INSERT OR REPLACE INTO backtest_result (id, payload, computed_at) "
                  "VALUES (1, ?, ?)", (json.dumps(result), result["computed_at"]))


def load() -> dict | None:
    _init()
    try:
        with analysis._snap_conn() as c:
            row = c.execute("SELECT payload FROM backtest_result WHERE id = 1").fetchone()
        result = json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None
    # a result worked out by an earlier scoring engine says nothing about
    # the ratings shown today — the test has to be run again
    if result and result.get("engine_v") != analysis.ENGINE_VERSION:
        return None
    return result


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def _aligned(bench, index) -> pd.Series | None:
    if bench is None or len(bench) == 0:
        return None
    b = pd.Series(bench).dropna()
    b.index = pd.DatetimeIndex(b.index).normalize()
    b = b[~b.index.duplicated(keep="last")].sort_index()
    return b.reindex(pd.DatetimeIndex(index).normalize(), method="ffill").to_numpy()


def signals_for(ticker: str, df, bench) -> list[dict]:
    """Every STEP-th session of `df`: the score then, and the return after."""
    df = df.dropna(subset=["Close"])
    close = df["Close"].to_numpy()
    b = _aligned(bench, df.index)
    stored = analysis._history(ticker)
    scores = analysis.closing_scores(df, bench)     # every session, one pass
    new_rows, out = [], []
    shortest = min(n for _, n in HORIZONS)
    for pos in range(WARMUP, len(df) - shortest, STEP):
        day = analysis._day(df.index[pos])
        h = stored.get(day)
        if h and h["final"]:
            score, rating = h["score"], h["rating"]
        else:
            row = scores.get(day) or analysis._score_at(df, pos, bench)
            new_rows.append(row)
            score, rating = row[1], row[2]
        sig = {"ticker": ticker, "date": day, "score": score, "rating": rating}
        for name, n in HORIZONS:
            if pos + n >= len(df):
                continue
            ret = close[pos + n] / close[pos] - 1.0
            sig[name] = ret * 100.0
            if b is not None and b[pos] and not pd.isna(b[pos]) and not pd.isna(b[pos + n]):
                sig[name + "_x"] = (ret - (b[pos + n] / b[pos] - 1.0)) * 100.0
        out.append(sig)
    analysis._store_scores(ticker, new_rows)      # reused next time (and by the chart)
    return out


def _mean(vals):
    return round(sum(vals) / len(vals), 2) if vals else None


def _group(name: str, sigs: list[dict]) -> dict:
    row = {"rating": name, "n": len(sigs)}
    for h, _ in HORIZONS:
        rets = [s[h] for s in sigs if h in s]
        xs = [s[h + "_x"] for s in sigs if h + "_x" in s]
        row[f"avg_{h}"] = _mean(rets)
        row[f"excess_{h}"] = _mean(xs)
        row[f"beat_{h}"] = round(100.0 * sum(x > 0 for x in xs) / len(xs), 1) if xs else None
        row[f"n_{h}"] = len(rets)
    return row


def summarize(signals: list[dict]) -> dict:
    rows = [_group(r, [s for s in signals if s["rating"] == r]) for r in RATINGS]
    good = _group("Buy + Strong Buy", [s for s in signals if s["rating"] in ("Strong Buy", "Buy")])
    bad = _group("Sell + Strong Sell", [s for s in signals if s["rating"] in ("Sell", "Strong Sell")])
    spread = None
    if good["excess_3M"] is not None and bad["excess_3M"] is not None:
        spread = round(good["excess_3M"] - bad["excess_3M"], 2)

    if spread is None or good["n_3M"] < MIN_GROUP or bad["n_3M"] < MIN_GROUP:
        verdict, tone = ("Not enough signals yet to judge — the test needs at least "
                         f"{MIN_GROUP} Buy and {MIN_GROUP} Sell signals with 3 months "
                         "of results. Add more stocks and run it again."), "neutral"
    elif spread >= 2:
        verdict, tone = (f"Useful on your stocks: after a Buy / Strong Buy rating, the stock "
                         f"did {spread:.1f} percentage points better over the next 3 months "
                         f"(vs the S&P 500) than after a Sell / Strong Sell rating."), "good"
    elif spread > -2:
        verdict, tone = (f"No clear edge on your stocks: Buy and Sell ratings were followed "
                         f"by similar results (difference {spread:+.1f} points over 3 "
                         f"months). Treat the score as one input, not a signal on its own."), "neutral"
    else:
        verdict, tone = (f"Worked backwards on your stocks: Sell ratings were followed by "
                         f"{-spread:.1f} points BETTER 3-month results than Buy ratings. "
                         f"Be careful relying on the score for these stocks."), "bad"

    days = sorted(s["date"] for s in signals)
    return {
        "rows": rows, "good": good, "bad": bad, "all": _group("All signals", signals),
        "spread_3m": spread, "verdict": verdict, "tone": tone,
        "signals": len(signals), "tickers": len({s["ticker"] for s in signals}),
        "first": days[0] if days else None, "last": days[-1] if days else None,
        "step": STEP, "caveats": CAVEATS, "computed_at": time.time(),
        "engine_v": analysis.ENGINE_VERSION,
    }


def _run(tickers: list[str]) -> None:
    try:
        bench = analysis.benchmark_close()
        signals, skipped = [], {}
        for i, t in enumerate(tickers):
            try:
                signals += signals_for(t, data_layer.fetch_history(t), bench)
            except Exception as e:  # noqa: BLE001 — one stock must not stop the test
                skipped[t] = str(e)[:120]
            with _state_lock:
                _state["done"] = i + 1
        result = summarize(signals)
        result["skipped"] = skipped
        _save(result)
        log.info("backtest done: %d signals over %d stocks", result["signals"], result["tickers"])
    except Exception as e:  # noqa: BLE001
        log.error("backtest failed: %s", e)
        with _state_lock:
            _state["error"] = str(e)[:200]
    finally:
        with _state_lock:
            _state["running"] = False


def start(tickers: list[str]) -> bool:
    """Run in the background; False if a test is already running."""
    with _state_lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=len(tickers), error=None,
                      started_at=time.time())
    threading.Thread(target=_run, args=(list(tickers),), name="backtest", daemon=True).start()
    return True


def status() -> dict:
    with _state_lock:
        st = dict(_state)
    st["result"] = None if st["running"] else load()
    return st
