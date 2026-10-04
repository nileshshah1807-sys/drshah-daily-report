"""
Global market indices & commodities snapshot —
NASDAQ Composite, Dow Jones, Gold and Crude Oil (WTI).

Fetched from Yahoo Finance with a short TTL cache so the dashboard strip
stays fast and never hammers the provider.
"""
import threading
import time

import yfinance as yf

import data as data_layer

CACHE_TTL = 90  # seconds

SYMBOLS = [
    {"key": "nasdaq", "name": "NASDAQ Composite", "symbol": "^IXIC",
     "unit": "points", "icon": "📈"},
    {"key": "dow", "name": "Dow Jones Industrial", "symbol": "^DJI",
     "unit": "points", "icon": "🏛️"},
    {"key": "gold", "name": "Gold", "symbol": "GC=F",
     "unit": "USD / troy oz", "icon": "🥇"},
    {"key": "crude", "name": "Crude Oil (WTI)", "symbol": "CL=F",
     "unit": "USD / barrel", "icon": "🛢️"},
]

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _fetch_one(spec: dict) -> dict:
    df = yf.Ticker(spec["symbol"]).history(period="5d", interval="1d",
                                           auto_adjust=True)
    if df is None or df.empty:
        raise ValueError("no data returned")
    # Yahoo often ships the newest candle with NULL prices (volume only) —
    # rebuild it from intraday so the strip shows the latest level, not yesterday's.
    df = data_layer.repair_daily_bars(spec["symbol"], df, cap=None)
    closes = df["Close"].dropna()
    if len(closes) < 2:
        raise ValueError("insufficient history")
    price = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    change = price - prev
    pct = (change / prev * 100.0) if prev else 0.0
    item = dict(spec)
    item.update({"price": round(price, 2), "change": round(change, 2),
                 "change_pct": round(pct, 2), "ok": True, "error": None,
                 "as_of": str(closes.index[-1].date())})
    return item


def fetch_indices(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        hit = _cache.get("all")
        if hit and not force and now - hit[0] < CACHE_TTL:
            return hit[1]

    items = []
    for spec in SYMBOLS:
        try:
            items.append(_fetch_one(spec))
        except Exception as e:  # noqa: BLE001
            item = dict(spec)
            item.update({"price": None, "change": None, "change_pct": None,
                         "ok": False, "error": str(e)[:100]})
            items.append(item)

    result = {
        "items": items,
        "ok": any(i["ok"] for i in items),
        "as_of": time.time(),
    }
    with _lock:
        _cache["all"] = (now, result)
    return result
