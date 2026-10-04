"""
Dr. SHAH'S US STOCKS ANALYSIS — Technical Analysis & Scoring Engine
=====================================================================
DELIVERY / INVESTMENT-FOCUSED ANALYSIS ON 2-YEAR DAILY CHARTS
--------------------------------------------------------------
The scoring model is identical to the investment model (same weights, point
values, thresholds and labels) — only the timeframe is DAILY:
  • 3 years of daily bars (≈ 756 sessions)
  • Long-term trend structure on daily SMAs (50/150/260 ≈ 10/30/52-week)
  • Golden-cross alignment (150/260-day, 50/200-day)
  • Daily RSI(14), MACD(12,26,9), ATR(14)
  • 3-month (63d) & 6-month (126d) momentum
  • Stability & drawdown risk, accumulation volume, 52-week position,
    daily candlestick patterns

Each stock gets a transparent 0-100 score mapped to
Strong Buy / Buy / Hold / Sell / Strong Sell plus a plain-language verdict.
"""
import math
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Indicator math (all return pandas Series aligned to input index)
# ----------------------------------------------------------------------------

def sma(s: pd.Series, n: int, min_periods: int | None = None) -> pd.Series:
    return s.rolling(n, min_periods=min_periods or n).mean()

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(100.0).where(avg_loss != 0, 100.0)

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist

def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    std = close.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([(high - low), (high - prev_close).abs(),
                      (low - prev_close).abs()], axis=1).max(axis=1)

def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1.0 / n, adjust=False).mean()

def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               n: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    rng = (hh - ll).replace(0, np.nan)
    k = (100.0 * (close - ll) / rng).rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d

def roc(close: pd.Series, n: int = 10) -> pd.Series:
    return close.pct_change(n) * 100.0

def swing_points(high: pd.Series, low: pd.Series, lookback: int = 5, n_points: int = 4):
    highs, lows = list(high.dropna()), list(low.dropna())
    n = len(highs)
    res, sup = [], []
    if n < lookback * 2 + 1:
        return res, sup
    for i in range(lookback, n - lookback):
        win_h = highs[i - lookback: i + lookback + 1]
        if highs[i] == max(win_h) and win_h.count(highs[i]) == 1:
            res.append(highs[i])
        win_l = lows[i - lookback: i + lookback + 1]
        if lows[i] == min(win_l) and win_l.count(lows[i]) == 1:
            sup.append(lows[i])
    return _dedupe(res[-n_points:]), _dedupe(sup[-n_points:])

def _dedupe(vals, tol_pct: float = 1.5):
    out = []
    for v in vals:
        if not any(abs(v - o) / o * 100 < tol_pct for o in out):
            out.append(v)
    return out

def detect_pattern(high, low, open_, close, body_avg_tol: float = 0.30) -> tuple[str, float]:
    """Candlestick pattern on the most recent daily candle(s). impact ∈ [-6, +6]
    (patterns are short-term signals, secondary for delivery scoring)."""
    h, l, o, c = [list(x.dropna()) for x in (high, low, open_, close)]
    if len(c) < 4:
        return "No pattern", 0.0
    c1, c2, c3 = c[-1], c[-2], c[-3]
    o1, o2, o3 = o[-1], o[-2], o[-3]
    h1, l1 = h[-1], l[-1]
    body = abs(c1 - o1)
    rng = max(h1 - l1, 1e-9)
    avg_body = np.mean([abs(c[i] - o[i]) for i in range(-5, 0)]) or 1e-9
    hammer = (body / rng < body_avg_tol and min(o1, c1) - l1 > 2 * body and h1 - max(o1, c1) < body)
    shooting = (body / rng < body_avg_tol and h1 - max(o1, c1) > 2 * body and min(o1, c1) - l1 < body)
    doji = body / rng < 0.08
    bull_eng = (c1 > o1 and c2 < o2 and o1 <= c2 + 0.05 * avg_body and
                c1 >= o2 - 0.05 * avg_body and c1 - o1 > 1.2 * avg_body)
    bear_eng = (c1 < o1 and c2 > o2 and o1 >= c2 - 0.05 * avg_body and
                c1 <= o2 + 0.05 * avg_body and o1 - c1 > 1.2 * avg_body)
    tws = all(c[i] > o[i] and (c[i] - o[i]) > avg_body and
              c[i] > c[i - 1] and o[i] > o[i - 1] for i in (-1, -2)) and \
          c3 > o3 and (c3 - o3) > avg_body and c2 > c3 and o2 > o3
    tbc = all(c[i] < o[i] and (o[i] - c[i]) > avg_body and
              c[i] < c[i - 1] and o[i] < o[i - 1] for i in (-1, -2)) and \
          c3 < o3 and (o3 - c3) > avg_body and c2 < c3 and o2 < o3
    if tws:
        return "Three White Soldiers (bullish)", 5.0
    if tbc:
        return "Three Black Crows (bearish)", -5.0
    if bull_eng:
        return "Bullish Engulfing", 4.5
    if bear_eng:
        return "Bearish Engulfing", -4.5
    if hammer and c1 > o1:
        return "Hammer (bullish)", 3.5
    if shooting and c1 < o1:
        return "Shooting Star (bearish)", -3.5
    if doji:
        return "Doji (indecision)", 0.0
    if c1 > o1 and c2 > o2:
        return "Two Bullish Days", 1.5
    if c1 < o1 and c2 < o2:
        return "Two Bearish Days", -1.5
    return "No clear pattern", 0.0

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))

def last(v):
    s = pd.Series(v).dropna()
    return float(s.iloc[-1]) if len(s) else None

def prev(v):
    s = pd.Series(v).dropna()
    return float(s.iloc[-2]) if len(s) >= 2 else None

def slope_pct(s: pd.Series, lookback: int = 5) -> float | None:
    s = pd.Series(s).dropna()
    if len(s) < lookback + 1:
        return None
    return float((s.iloc[-1] / s.iloc[-1 - lookback] - 1.0) * 100.0)

# ----------------------------------------------------------------------------
# Sub-scores — SAME scoring model (weights/points/thresholds), DAILY data
# ----------------------------------------------------------------------------

def score_longterm_trend(price, sma200, close, sma50, sma150, sma260, high) -> tuple[float, str, str, dict]:
    """25% — long-term trend structure on 2-year daily charts."""
    p = float(price)
    a200 = last(sma200)
    s50, s150, s260 = last(sma50), last(sma150), last(sma260)
    sl50, sl150 = slope_pct(sma50, 25), slope_pct(sma150, 75)
    pts, checks = 0, []

    def add(cond, w, msg):
        nonlocal pts
        checks.append(msg)
        if cond:
            pts += w

    add(a200 is not None and p > a200, 14, "Price above 200-day SMA (long-term trend up)")
    add(s260 is not None and p > s260, 14, "Price above 260-day SMA (1-year trend up)")
    add(s150 is not None and p > s150, 12, "Price above 150-day SMA (~7 months)")
    add(s150 is not None and s260 is not None and s150 > s260, 12, "150-day SMA above 260-day SMA")
    add(s50 is not None and s150 is not None and s50 > s150, 10, "50-day SMA above 150-day SMA")
    add(sl50 is not None and sl50 > 0, 10, "50-day SMA rising")
    add(sl150 is not None and sl150 > 0, 10, "150-day SMA rising")
    # near 52-week high = institutional strength (for delivery, NOT a sell)
    if len(high) >= 252:
        y_high = float(high.iloc[-252:].max())
        dist = (p / y_high - 1) * 100 if y_high else 0
        add(dist >= -15, 10, "Within 15% of 52-week high (strength)")
    # % of last 200 sessions above SMA200
    if a200 is not None:
        d = pd.Series(close).dropna().iloc[-200:]
        m = pd.Series(sma200).dropna().iloc[-200:]
        n = min(len(d), len(m))
        if n >= 120:
            pct = float((d.iloc[-n:] > m.iloc[-n:]).mean())
            add(pct >= 0.80, 6, f"Above 200-day SMA {pct*100:.0f}% of the last year")
    # higher highs / higher lows: last 6 months vs prior 6 months (130-day halves)
    seg = pd.Series(close).iloc[-260:]
    if len(seg) >= 200:
        h1, h2 = float(seg[:130].max()), float(seg[130:].max())
        l1, l2 = float(seg[:130].min()), float(seg[130:].min())
        add(h2 > h1, 6, "Higher highs (last 6 months vs prior 6)")
        add(l2 > l1, 6, "Higher lows (last 6 months vs prior 6)")

    score = clamp(pts)
    # the checklist adds up to 110 points; show the capped score, not "110/100"
    if pts >= 70:
        label, det = "Strong Uptrend", f"{score:.0f}/100 — long-term trend firmly up"
    elif pts >= 45:
        label, det = "Uptrend", f"{score:.0f}/100 — bullish long-term structure"
    elif pts >= 30:
        label, det = "Neutral", f"{score:.0f}/100 — mixed long-term signals"
    elif pts >= 15:
        label, det = "Downtrend", f"{score:.0f}/100 — long-term structure weak"
    else:
        label, det = "Strong Downtrend", f"{score:.0f}/100 — long-term trend damaged"
    return score, label, det, {"checks": checks}


def score_investment_momentum(rsi, macd_line, macd_sig, macd_hist, roc63, roc126) -> tuple[float, str, str, dict]:
    """20% — momentum on the daily chart (investor's momentum)."""
    r = last(rsi)
    hv, hpv = last(macd_hist), prev(macd_hist)
    macd_v, macd_sig_v = last(macd_line), last(macd_sig)
    r3, r6 = last(roc63), last(roc126)

    pts = 50.0
    notes = []
    if r is not None:
        if r <= 35:
            pts += 14; notes.append(f"RSI {r:.1f} — oversold, long-term opportunity zone")
        elif r < 45:
            pts += 6; notes.append(f"RSI {r:.1f} — recovering from weakness")
        elif r <= 65:
            pts += 16; notes.append(f"RSI {r:.1f} — healthy investment zone")
        elif r <= 80:
            pts -= 8; notes.append(f"RSI {r:.1f} — elevated, wait for a pullback")
        else:
            pts -= 18; notes.append(f"RSI {r:.1f} — overheated, poor entry point")
    if hv is not None and hpv is not None:
        if hv > 0 and hv >= hpv:
            pts += 16; notes.append("MACD histogram positive & expanding")
        elif hv > 0:
            pts += 8; notes.append("MACD histogram positive but cooling")
        elif hv >= hpv:
            pts -= 6; notes.append("MACD histogram negative but improving")
        else:
            pts -= 16; notes.append("MACD histogram negative & weakening")
    if macd_v is not None and macd_sig_v is not None:
        pts += 6 if macd_v > macd_sig_v else -6
        notes.append("MACD " + ("above" if macd_v > macd_sig_v else "below") + " signal line")
    # 3-month and 6-month momentum alignment (63 / 126 trading days)
    if r3 is not None and r6 is not None:
        if r3 > 0 and r6 > 0:
            pts += 12; notes.append(f"3M {r3:+.1f}% & 6M {r6:+.1f}% — both positive (aligned)")
        elif r3 > 0 or r6 > 0:
            pts += 4; notes.append("Mixed 3M/6M momentum")
        else:
            pts -= 12; notes.append(f"3M {r3:+.1f}% & 6M {r6:+.1f}% — both negative")

    score = clamp(pts)
    if score >= 70:
        label, det = "Strong Momentum", "Bullish daily momentum across RSI / MACD / 3-6M returns"
    elif score >= 55:
        label, det = "Bullish Momentum", "Daily momentum leaning positive"
    elif score >= 45:
        label, det = "Neutral Momentum", "Mixed daily momentum"
    elif score >= 30:
        label, det = "Bearish Momentum", "Daily momentum leaning negative"
    else:
        label, det = "Strong Bearish", "Negative daily momentum — avoid for delivery"
    return score, label, det, {"notes": notes}


def score_golden_cross(p, sma50, sma150, sma260, sma200) -> tuple[float, str, str, dict]:
    """20% — moving-average alignment / golden-cross structure (daily)."""
    s50, s150, s260, a200 = last(sma50), last(sma150), last(sma260), last(sma200)
    sl150, sl260 = slope_pct(sma150, 75), slope_pct(sma260, 75)
    conds = [
        ("Price > 150-day SMA", s150 is not None and p > s150),
        ("Price > 260-day SMA", s260 is not None and p > s260),
        ("Golden cross: 150-day SMA > 260-day SMA", s150 is not None and s260 is not None and s150 > s260),
        ("50-day SMA > 150-day SMA", s50 is not None and s150 is not None and s50 > s150),
        ("50-day SMA > 200-day SMA (daily golden cross)", s50 is not None and a200 is not None and s50 > a200),
        ("Price > 200-day SMA", a200 is not None and p > a200),
        ("150-day SMA rising", (sl150 or 0) > 0),
        ("260-day SMA rising", (sl260 or 0) > 0),
    ]
    ok = sum(1 for _, c in conds if c)
    score = clamp(ok / len(conds) * 100)
    if score >= 80:
        label = "Golden-Cross Bullish"
    elif score >= 60:
        label = "Constructive"
    elif score >= 40:
        label = "Mixed"
    elif score >= 20:
        label = "Bearish Alignment"
    else:
        label = "Death-Cross Bearish"
    det = f"{ok}/{len(conds)} bullish conditions"
    return score, label, det, {"conditions": conds, "ok": ok}


def score_stability(p, close, high, atr) -> tuple[float, str, str, dict]:
    """12% — volatility, drawdown risk and return consistency (investor risk)."""
    atr_pct = last(atr / close * 100.0)
    pts = 50.0
    notes = []
    if atr_pct is not None:
        if atr_pct < 3:
            pts += 18; notes.append(f"ATR {atr_pct:.1f}% — low volatility, stable holding")
        elif atr_pct < 5:
            pts += 12; notes.append(f"ATR {atr_pct:.1f}% — moderate volatility")
        elif atr_pct < 8:
            pts += 2; notes.append(f"ATR {atr_pct:.1f}% — elevated volatility")
        else:
            pts -= 12; notes.append(f"ATR {atr_pct:.1f}% — very volatile, risky to hold")
    if len(high) >= 252:
        y_high = float(high.iloc[-252:].max())
        dd = (p / y_high - 1) * 100 if y_high else 0
        if dd >= -10:
            pts += 14; notes.append(f"Only {abs(dd):.1f}% below 52-week high — shallow drawdown")
        elif dd >= -25:
            pts += 8; notes.append(f"{abs(dd):.1f}% below 52-week high — normal correction")
        elif dd >= -40:
            pts += 0; notes.append(f"{abs(dd):.1f}% below 52-week high — deep correction")
        else:
            pts -= 10; notes.append(f"{abs(dd):.1f}% below 52-week high — severely damaged")
    # consistency: fraction of up-days over last 6 months (126 sessions)
    rets = pd.Series(close).pct_change().iloc[-126:]
    if len(rets) >= 90:
        up = float((rets > 0).mean())
        if up >= 0.60:
            pts += 16; notes.append(f"{up*100:.0f}% of the last 6 months closed green — consistent")
        elif up >= 0.50:
            pts += 8; notes.append(f"{up*100:.0f}% of the last 6 months closed green")
        else:
            pts -= 12; notes.append(f"Only {up*100:.0f}% of the last 6 months closed green")
        ann = float(rets.std() * math.sqrt(252) * 100) if rets.std() else 0
        if ann < 25:
            pts += 8; notes.append(f"~{ann:.0f}% annualized volatility — low")
        elif ann > 45:
            pts -= 8; notes.append(f"~{ann:.0f}% annualized volatility — high")

    score = clamp(pts)
    if score >= 65:
        label, det = "Stable", "Good holding characteristics (low risk)"
    elif score >= 45:
        label, det = "Moderate", "Acceptable risk for delivery"
    else:
        label, det = "Risky", "High volatility / deep drawdowns"
    return score, label, det, {"notes": notes}


def score_accumulation_full(volume, close) -> tuple[float, str, str, dict]:
    """13% — accumulation with up/down-day volume split (investment-grade)."""
    v = pd.Series(volume).dropna()
    c = pd.Series(close).dropna()
    n = min(len(v), len(c))
    if n < 60:
        return 50.0, "Neutral", "Insufficient volume history", {"notes": []}
    v, c = v.iloc[-n:], c.iloc[-n:]
    v50 = float(v.iloc[-50:].mean()) or 1.0
    v200 = float(v.iloc[-200:].mean()) or 1.0
    trend = v50 / v200
    rets = c.pct_change().iloc[-90:]
    up_v = float(v.iloc[-90:][rets > 0].mean()) if (rets > 0).any() else 0.0
    dn_v = float(v.iloc[-90:][rets < 0].mean()) if (rets < 0).any() else 0.0
    ud = (up_v / dn_v) if dn_v > 0 else 1.0
    pts = 50.0
    notes = []
    if trend >= 1.2:
        pts += 14; notes.append(f"50-day volume {trend:.2f}× the 1-year average — rising participation")
    elif trend >= 1.0:
        pts += 6; notes.append(f"50-day volume {trend:.2f}× the 1-year average")
    elif trend <= 0.8:
        pts -= 8; notes.append(f"50-day volume {trend:.2f}× the 1-year average — fading interest")
    if ud >= 1.3:
        pts += 16; notes.append(f"Up-days carry {ud:.2f}× more volume than down-days (90d) — accumulation")
    elif ud <= 0.75:
        pts -= 14; notes.append(f"Down-days carry {ud:.2f}× more volume — distribution")
    v10 = float(v.iloc[-10:].mean()) or 1.0
    v40 = float(v.iloc[-50:-10].mean()) or 1.0
    r10 = v10 / v40
    if r10 >= 1.15:
        pts += 10; notes.append(f"Recent 10-day volume {r10:.2f}× prior — accumulation days")
    elif r10 <= 0.8:
        pts -= 6; notes.append("Recent volume quiet — no institutional interest")
    score = clamp(pts)
    if score >= 65:
        label, det = "Accumulation", "Institutions building positions"
    elif score >= 45:
        label, det = "Neutral", "No strong institutional signal"
    else:
        label, det = "Distribution", "Institutions reducing positions"
    return score, label, det, {"notes": notes}


def score_position_52w(price, high, low) -> tuple[float, str, str]:
    """10% — position in the 52-week range (investment interpretation)."""
    c = pd.Series(price).dropna()
    h = pd.Series(high).dropna()
    l = pd.Series(low).dropna()
    if len(c) < 60 or len(h) < 60:
        return 50.0, "Neutral", "Insufficient history"
    y_high = float(h.iloc[-252:].max())
    y_low = float(l.iloc[-252:].min())
    p = float(c.iloc[-1])
    if y_high == y_low:
        return 50.0, "Neutral", "Flat range"
    pos = (p - y_low) / (y_high - y_low) * 100.0
    if pos >= 85:
        score, label, det = 92, "Breakout Zone", f"Top {100-pos:.0f}% of 52-week range — institutional strength"
    elif pos >= 60:
        score, label, det = 75, "Upper Range", f"{pos:.0f}% above 52-week low — healthy trend"
    elif pos >= 40:
        score, label, det = 55, "Mid Range", f"{pos:.0f}% above 52-week low — neutral"
    elif pos >= 15:
        score, label, det = 35, "Lower Range", f"{pos:.0f}% above 52-week low — recovery watch"
    else:
        score, label, det = 20, "Near 52W Low", f"Bottom {pos:.0f}% of range — high risk for delivery"
    return score, label, det


def relative_strength(close: pd.Series, benchmark: pd.Series | None) -> dict:
    """Stock return minus S&P 500 (SPY) return over 3 / 6 / 12 months, in
    percentage points, plus a small score adjustment.

    Beating the index over both 6 and 12 months is one of the most reliable
    signs of a long-term leader: +2 points (+4 when ahead by more than 10 pts
    on both); lagging on both costs the same. With no benchmark data the
    block is empty and the score is unchanged.
    """
    out = {"benchmark": "S&P 500 (SPY)", "rs_3m": None, "rs_6m": None,
           "rs_12m": None, "label": "Not available", "score_adj": 0.0}
    if benchmark is None or len(benchmark) == 0:
        return out
    c = pd.Series(close).dropna()
    b = pd.Series(benchmark).dropna()
    b.index = pd.DatetimeIndex(b.index).normalize()
    b = b[~b.index.duplicated(keep="last")].sort_index()
    # benchmark close on each of the stock's session dates (SPY trades the
    # same days; ffill only bridges a missing bar)
    b = b.reindex(pd.DatetimeIndex(c.index).normalize(), method="ffill")
    b.index = c.index

    def diff(n):
        if len(c) <= n or pd.isna(b.iloc[-1]) or pd.isna(b.iloc[-1 - n]) or not b.iloc[-1 - n]:
            return None
        stock = c.iloc[-1] / c.iloc[-1 - n] - 1.0
        bench = b.iloc[-1] / b.iloc[-1 - n] - 1.0
        return round(float((stock - bench) * 100.0), 2)

    rs3, rs6, rs12 = diff(63), diff(126), diff(252)
    out.update(rs_3m=rs3, rs_6m=rs6, rs_12m=rs12)
    if rs6 is None:
        return out
    long_ = rs12 if rs12 is not None else rs6          # young stock: 6M only
    if rs6 > 10 and long_ > 10:
        adj, label = 4.0, "Strongly outperforming"
    elif rs6 > 0 and long_ > 0:
        adj, label = 2.0, "Outperforming"
    elif rs6 < -10 and long_ < -10:
        adj, label = -4.0, "Strongly underperforming"
    elif rs6 < 0 and long_ < 0:
        adj, label = -2.0, "Underperforming"
    else:
        adj, label = 0.0, "Mixed vs the index"
    out.update(label=label, score_adj=adj)
    return out


def support_resistance(close, high, low) -> dict:
    res, sup = swing_points(high, low)
    p = float(close.iloc[-1])
    nearest_res = min([r for r in res if r > p], default=None)
    nearest_sup = max([s for s in sup if s < p], default=None)
    return {
        "resistances": [round(r, 2) for r in res if r > p][:3],
        "supports": [round(s, 2) for s in sup if s < p][:3],
        "nearest_resistance": round(nearest_res, 2) if nearest_res else None,
        "nearest_support": round(nearest_sup, 2) if nearest_sup else None,
        "dist_to_resistance_pct": round((nearest_res / p - 1) * 100, 2) if nearest_res else None,
        "dist_to_support_pct": round((1 - nearest_sup / p) * 100, 2) if nearest_sup else None,
    }

# ----------------------------------------------------------------------------
# Master analysis — 2 YEARS of DAILY bars
# ----------------------------------------------------------------------------

ENGINE_VERSION = 4   # bump when the result schema changes (4: relative strength)

WEIGHTS = {
    "Long-Term Trend": 0.25,
    "Investment Momentum": 0.20,
    "Golden Cross": 0.20,
    "Stability": 0.12,
    "Volume": 0.13,
    "52W Position": 0.10,
}

VERDICTS = {
    "Strong Buy": "STRONG BUY — excellent long-term structure. Accumulate on minor dips and hold for delivery.",
    "Buy": "BUY — good investment candidate. Build the position in tranches on pullbacks.",
    "Hold": "HOLD — no compelling entry right now. Keep existing positions and wait for clearer trend confirmation.",
    "Sell": "SELL / REDUCE — the long-term trend is weakening. Trim exposure on bounces.",
    "Strong Sell": "STRONG SELL — long-term trend damaged. Avoid fresh buying; exit remaining positions.",
}

def rating_for(score: float) -> str:
    if score >= 80:
        return "Strong Buy"
    if score >= 65:
        return "Buy"
    if score >= 50:
        return "Hold"
    if score >= 35:
        return "Sell"
    return "Strong Sell"

def rating_rank(r: str) -> int:
    return {"Strong Buy": 5, "Buy": 4, "Hold": 3, "Sell": 2, "Strong Sell": 1}[r]


def analyze(df: pd.DataFrame, benchmark: pd.Series | None = None) -> dict:
    """
    DELIVERY / INVESTMENT analysis on 2-YEAR DAILY charts.
    df: daily OHLCV indexed by date (ascending); only the last ~3 years
        (756 trading days) are used for the analysis.
    benchmark: S&P 500 (SPY) daily closes for the relative-strength check
        (optional — without it that adjustment is simply skipped).
    """
    df = df.dropna(subset=["Close"]).copy()
    if len(df) > 756:
        df = df.iloc[-756:]                      # exactly 3 years of daily bars
    if len(df) < 120:
        raise ValueError(
            f"Insufficient price history for reliable analysis — "
            f"need at least 120 trading days (≈6 months), only {len(df)} found.")

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"].fillna(0)
    price = float(close.iloc[-1])

    # ---- daily indicators (2-year window) -----------------------------------
    sma20 = sma(close, 20)
    sma50 = sma(close, 50, min_periods=30)
    sma150 = sma(close, 150, min_periods=120)
    sma200 = sma(close, 200, min_periods=120)
    sma260 = sma(close, 260, min_periods=200)
    rsi = rsi_wilder(close, 14)
    b_up, b_mid, b_lo = bollinger(close, 20, 2.0)
    m_line, m_sig, m_hist = macd(close)
    atr = atr_wilder(high, low, close, 14)
    k, d = stochastic(high, low, close)
    roc63 = roc(close, 63)
    roc126 = roc(close, 126)

    # ---- sub-scores (same scoring model, daily data) -------------------------
    lt_score, lt_label, lt_det, lt_info = score_longterm_trend(
        price, sma200, close, sma50, sma150, sma260, high)
    im_score, im_label, im_det, im_info = score_investment_momentum(
        rsi, m_line, m_sig, m_hist, roc63, roc126)
    gc_score, gc_label, gc_det, gc_info = score_golden_cross(
        price, sma50, sma150, sma260, sma200)
    st_score, st_label, st_det, st_info = score_stability(price, close, high, atr)
    vol_score, vol_label, vol_det, vol_info = score_accumulation_full(volume, close)
    pos_score, pos_label, pos_det = score_position_52w(close, high, low)

    pattern, pattern_impact = detect_pattern(high, low, df["Open"], close)
    sr = support_resistance(close, high, low)

    # ---- final weighted score (SAME weights as before) -----------------------
    raw = (WEIGHTS["Long-Term Trend"] * lt_score
           + WEIGHTS["Investment Momentum"] * im_score
           + WEIGHTS["Golden Cross"] * gc_score
           + WEIGHTS["Stability"] * st_score
           + WEIGHTS["Volume"] * vol_score
           + WEIGHTS["52W Position"] * pos_score)
    score = clamp(raw + pattern_impact)
    if sr["dist_to_support_pct"] is not None and sr["dist_to_support_pct"] <= 1.5:
        score = clamp(score + 2.0)
    if sr["dist_to_resistance_pct"] is not None and sr["dist_to_resistance_pct"] <= 1.5:
        score = clamp(score - 2.0)
    rs = relative_strength(close, benchmark)
    score = clamp(score + rs["score_adj"])
    rating = rating_for(score)

    signals = [
        {"name": "Long-Term Trend", "score": round(lt_score, 1), "label": lt_label,
         "detail": lt_det, "weight": WEIGHTS["Long-Term Trend"]},
        {"name": "Investment Momentum", "score": round(im_score, 1), "label": im_label,
         "detail": im_det, "weight": WEIGHTS["Investment Momentum"]},
        {"name": "Golden Cross", "score": round(gc_score, 1), "label": gc_label,
         "detail": gc_det, "weight": WEIGHTS["Golden Cross"]},
        {"name": "Stability", "score": round(st_score, 1), "label": st_label,
         "detail": st_det, "weight": WEIGHTS["Stability"]},
        {"name": "Volume", "score": round(vol_score, 1), "label": vol_label,
         "detail": vol_det, "weight": WEIGHTS["Volume"]},
        {"name": "52W Position", "score": round(pos_score, 1), "label": pos_label,
         "detail": pos_det, "weight": WEIGHTS["52W Position"]},
    ]

    change_pct = (price / float(close.iloc[-2]) - 1.0) * 100.0 if len(close) > 1 else 0.0

    return {
        "ticker": "",
        "name": "",
        "engine_v": ENGINE_VERSION,
        "horizon": "Daily Charts · 3 Years — Delivery / Investment",
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "score": round(score, 1),
        "rating": rating,
        "verdict": VERDICTS[rating],
        "pattern": pattern,
        "signals": signals,
        "indicators": {
            "rsi": round(last(rsi), 1) if last(rsi) is not None else None,
            "macd_line": round(last(m_line), 3) if last(m_line) is not None else None,
            "macd_signal": round(last(m_sig), 3) if last(m_sig) is not None else None,
            "macd_hist": round(last(m_hist), 3) if last(m_hist) is not None else None,
            "sma20": round(last(sma20), 2) if last(sma20) is not None else None,
            "sma50": round(last(sma50), 2) if last(sma50) is not None else None,
            "sma150": round(last(sma150), 2) if last(sma150) is not None else None,
            "sma200": round(last(sma200), 2) if last(sma200) is not None else None,
            "sma260": round(last(sma260), 2) if last(sma260) is not None else None,
            "bb_upper": round(last(b_up), 2) if last(b_up) is not None else None,
            "bb_mid": round(last(b_mid), 2) if last(b_mid) is not None else None,
            "bb_lower": round(last(b_lo), 2) if last(b_lo) is not None else None,
            "atr_pct": round(last(atr / close * 100.0), 2) if last(atr) is not None else None,
            "stoch_k": round(last(k), 1) if last(k) is not None else None,
            "stoch_d": round(last(d), 1) if last(d) is not None else None,
            "roc63": round(last(roc63), 2) if last(roc63) is not None else None,
            "roc126": round(last(roc126), 2) if last(roc126) is not None else None,
            "vol_today": int(volume.iloc[-1]) if len(volume) else None,
            "vol_avg50": int(volume.iloc[-50:].mean()) if len(volume) >= 50 else None,
            "vol_avg200": int(volume.iloc[-200:].mean()) if len(volume) >= 200 else None,
            "w52_high": round(float(high.iloc[-252:].max()), 2),
            "w52_low": round(float(low.iloc[-252:].min()), 2),
        },
        "support_resistance": sr,
        "relative_strength": rs,
        "spark": [round(float(x), 4) for x in close.iloc[-60:].tolist()],
        "week52": {
            "high": round(float(high.iloc[-252:].max()), 2),
            "low": round(float(low.iloc[-252:].min()), 2),
        },
        # Reference levels for breakout / breakdown ALERTS only — the score above
        # is untouched. "previous N sessions" excludes the latest bar, so closing
        # above/below these really is a new 1-month / 52-week extreme.
        "levels": {
            "high_20d": round(float(high.iloc[-21:-1].max()), 2) if len(high) >= 21 else None,
            "low_20d": round(float(low.iloc[-21:-1].min()), 2) if len(low) >= 21 else None,
            "high_252d": round(float(high.iloc[-253:-1].max()), 2) if len(high) >= 253 else None,
            "low_252d": round(float(low.iloc[-253:-1].min()), 2) if len(low) >= 253 else None,
        },
        "last_updated": str(getattr(df.index[-1], "date", lambda: df.index[-1])()),
    }


def build_detail_history(df: pd.DataFrame) -> dict:
    """Chart data for the UI: 3 YEARS of DAILY bars with daily indicators."""
    df = df.dropna(subset=["Close"])
    if len(df) > 756:
        df = df.iloc[-756:]
    d = df
    close = d["Close"]
    return {
        "dates": [str(x.date()) for x in d.index],
        "open": [round(float(x), 2) for x in d["Open"]],
        "high": [round(float(x), 2) for x in d["High"]],
        "low": [round(float(x), 2) for x in d["Low"]],
        "close": [round(float(x), 2) for x in d["Close"]],
        "volume": [int(x) for x in d["Volume"].fillna(0)],
        "sma50": [round(float(x), 2) if pd.notna(x) else None for x in sma(close, 50, min_periods=30)],
        "sma150": [round(float(x), 2) if pd.notna(x) else None for x in sma(close, 150, min_periods=120)],
        "sma260": [round(float(x), 2) if pd.notna(x) else None for x in sma(close, 260, min_periods=200)],
        "bb_upper": [round(float(x), 2) if pd.notna(x) else None for x in bollinger(close, 20, 2.0)[0]],
        "bb_lower": [round(float(x), 2) if pd.notna(x) else None for x in bollinger(close, 20, 2.0)[2]],
        "rsi": [round(float(x), 1) if pd.notna(x) else None for x in rsi_wilder(close)],
        "macd": [round(float(x), 3) if pd.notna(x) else None for x in macd(close)[0]],
        "macd_signal": [round(float(x), 3) if pd.notna(x) else None for x in macd(close)[1]],
        "macd_hist": [round(float(x), 3) if pd.notna(x) else None for x in macd(close)[2]],
        "stoch_k": [round(float(x), 1) if pd.notna(x) else None for x in stochastic(d["High"], d["Low"], close)[0]],
        "stoch_d": [round(float(x), 1) if pd.notna(x) else None for x in stochastic(d["High"], d["Low"], close)[1]],
        "volume_sma": [round(float(x), 0) if pd.notna(x) else None for x in d["Volume"].fillna(0).rolling(20).mean()],
    }
