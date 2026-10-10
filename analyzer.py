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

Since engine 5 (Oct 2026) the score is built to stay steady:
  • every chart check is counted in one factor only
  • the score is the average of the last 5 sessions' readings
  • a rating changes only once the score is 2 points past a line
  • candlestick patterns and support / resistance are shown, but no longer
    move the score
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
# Daily bars as arrays — every factor can be worked out for ANY session
# ----------------------------------------------------------------------------

class Bars:
    """One stock's daily bars and indicators as plain arrays.

    Each factor below is scored "as it stood at the close of session i" from
    these arrays, using nothing after i. The same code therefore gives today's
    score and the score of any earlier day — which is what the 5-session
    average, the score history and the back-test are built from.
    """

    def __init__(self, df: pd.DataFrame, benchmark: pd.Series | None = None):
        close, high, low = df["Close"], df["High"], df["Low"]
        volume = df["Volume"].fillna(0)
        self.n = len(df)
        self.index = df.index
        # indicator series (analyze() reads their latest values for the UI)
        self.s_sma50 = sma(close, 50, min_periods=30)
        self.s_sma150 = sma(close, 150, min_periods=120)
        self.s_sma200 = sma(close, 200, min_periods=120)
        self.s_sma260 = sma(close, 260, min_periods=200)
        self.s_rsi = rsi_wilder(close, 14)
        self.s_macd, self.s_macd_sig, self.s_macd_hist = macd(close)
        self.s_atr = atr_wilder(high, low, close, 14)
        self.s_roc63 = roc(close, 63)
        self.s_roc126 = roc(close, 126)

        def arr(s):
            return s.to_numpy(dtype=float)

        self.close, self.high, self.low = arr(close), arr(high), arr(low)
        self.volume = arr(volume)
        self.sma50, self.sma150 = arr(self.s_sma50), arr(self.s_sma150)
        self.sma200, self.sma260 = arr(self.s_sma200), arr(self.s_sma260)
        self.rsi = arr(self.s_rsi)
        self.macd, self.macd_sig = arr(self.s_macd), arr(self.s_macd_sig)
        self.macd_hist = arr(self.s_macd_hist)
        self.atr_pct = arr(self.s_atr / close * 100.0)
        self.roc63, self.roc126 = arr(self.s_roc63), arr(self.s_roc126)
        self.ret = arr(close.pct_change())           # day-on-day change, first is NaN
        ok200 = np.flatnonzero(~np.isnan(self.sma200))
        self.sma200_from = int(ok200[0]) if len(ok200) else self.n
        self.bench = _aligned_benchmark(close, benchmark)


def _val(a, i: int) -> float | None:
    """The newest value at or before session i (None while there is none)."""
    while i >= 0 and a[i] != a[i]:           # NaN: the indicator had no value yet
        i -= 1
    return float(a[i]) if i >= 0 else None


def _prev(a, i: int) -> float | None:
    """The value one session before the newest one at or before i."""
    while i >= 0 and a[i] != a[i]:
        i -= 1
    return _val(a, i - 1) if i >= 1 else None


def _slope(a, i: int, lookback: int) -> float | None:
    """% change of a moving average over `lookback` sessions, as at session i."""
    j = i - lookback
    if j < 0 or a[i] != a[i] or a[j] != a[j]:
        return None
    return float((a[i] / a[j] - 1.0) * 100.0)


# ----------------------------------------------------------------------------
# Sub-scores, each "as at session i" — every check belongs to ONE factor:
#   Long-Term Trend  price against its long averages, their direction, structure
#   Golden Cross     how the averages stand against each other
#   52W Position     where the price sits in its 52-week range
# (Until engine 4 the trend and golden-cross lists shared six checks, and the
#  52-week high was also a trend check — the same thing was counted up to
#  three times.)
# ----------------------------------------------------------------------------

def score_longterm_trend(b: Bars, i: int) -> tuple[float, str, str, dict]:
    """25% — long-term trend: price against its averages, their direction, structure."""
    p = float(b.close[i])
    a200, s150, s260 = _val(b.sma200, i), _val(b.sma150, i), _val(b.sma260, i)
    sl50, sl150 = _slope(b.sma50, i, 25), _slope(b.sma150, i, 75)
    pts, checks = 0, []

    def add(cond, w, msg):
        nonlocal pts
        checks.append(msg)
        if cond:
            pts += w

    add(a200 is not None and p > a200, 16, "Price above 200-day SMA (long-term trend up)")
    add(s260 is not None and p > s260, 16, "Price above 260-day SMA (1-year trend up)")
    add(s150 is not None and p > s150, 14, "Price above 150-day SMA (~7 months)")
    add(sl50 is not None and sl50 > 0, 12, "50-day SMA rising")
    add(sl150 is not None and sl150 > 0, 12, "150-day SMA rising")
    # % of the last 200 sessions above SMA200
    if a200 is not None:
        n = min(200, i - b.sma200_from + 1)
        if n >= 120:
            pct = float((b.close[i - n + 1: i + 1] > b.sma200[i - n + 1: i + 1]).mean())
            add(pct >= 0.80, 10, f"Above 200-day SMA {pct*100:.0f}% of the last year")
    # higher highs / higher lows: last 6 months vs prior 6 months (130-day halves)
    seg = b.close[max(0, i - 259): i + 1]
    if len(seg) >= 200:
        add(float(seg[130:].max()) > float(seg[:130].max()), 10,
            "Higher highs (last 6 months vs prior 6)")
        add(float(seg[130:].min()) > float(seg[:130].min()), 10,
            "Higher lows (last 6 months vs prior 6)")

    score = clamp(pts)                       # the checklist adds up to 100 points
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


def score_investment_momentum(b: Bars, i: int) -> tuple[float, str, str, dict]:
    """20% — momentum on the daily chart (investor's momentum)."""
    r = _val(b.rsi, i)
    hv, hpv = _val(b.macd_hist, i), _prev(b.macd_hist, i)
    macd_v, macd_sig_v = _val(b.macd, i), _val(b.macd_sig, i)
    r3, r6 = _val(b.roc63, i), _val(b.roc126, i)

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
        label, det = "Strong Bearish", "Daily momentum clearly negative"
    return score, label, det, {"notes": notes}


def score_golden_cross(b: Bars, i: int) -> tuple[float, str, str, dict]:
    """15% — how the moving averages stand against each other (golden-cross
    structure). Price-against-average checks belong to the trend factor."""
    s50, s150 = _val(b.sma50, i), _val(b.sma150, i)
    s260, a200 = _val(b.sma260, i), _val(b.sma200, i)
    sl260 = _slope(b.sma260, i, 75)
    conds = [
        ("Golden cross: 150-day SMA > 260-day SMA", 35,
         s150 is not None and s260 is not None and s150 > s260),
        ("50-day SMA > 150-day SMA", 25, s50 is not None and s150 is not None and s50 > s150),
        ("50-day SMA > 200-day SMA (daily golden cross)", 20,
         s50 is not None and a200 is not None and s50 > a200),
        ("260-day SMA rising", 20, (sl260 or 0) > 0),
    ]
    ok = sum(1 for _, _, c in conds if c)
    score = clamp(sum(w for _, w, c in conds if c))
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
    return score, label, det, {"conditions": [(name, c) for name, _, c in conds], "ok": ok}


def score_stability(b: Bars, i: int) -> tuple[float, str, str, dict]:
    """15% — volatility, drawdown risk and return consistency (investor risk)."""
    p = float(b.close[i])
    atr_pct = _val(b.atr_pct, i)
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
    if i + 1 >= 252:
        y_high = float(np.nanmax(b.high[i - 251: i + 1]))
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
    rets = b.ret[max(0, i - 125): i + 1]
    if len(rets) >= 90:
        up = float((rets > 0).mean())
        if up >= 0.60:
            pts += 16; notes.append(f"{up*100:.0f}% of the last 6 months closed green — consistent")
        elif up >= 0.50:
            pts += 8; notes.append(f"{up*100:.0f}% of the last 6 months closed green")
        else:
            pts -= 12; notes.append(f"Only {up*100:.0f}% of the last 6 months closed green")
        known = rets[~np.isnan(rets)]
        std = float(known.std(ddof=1)) if len(known) > 1 else 0.0
        ann = std * math.sqrt(252) * 100 if std else 0
        if ann < 25:
            pts += 8; notes.append(f"~{ann:.0f}% annualized volatility — low")
        elif ann > 45:
            pts -= 8; notes.append(f"~{ann:.0f}% annualized volatility — high")

    score = clamp(pts)
    if score >= 65:
        label, det = "Stable", "Smaller swings and shallower drawdowns than most"
    elif score >= 45:
        label, det = "Moderate", "Moderate swings and drawdowns"
    else:
        label, det = "Risky", "High volatility / deep drawdowns"
    return score, label, det, {"notes": notes}


def score_accumulation_full(b: Bars, i: int) -> tuple[float, str, str, dict]:
    """15% — volume: is it rising, and heavier on up-days or on down-days?

    Volume cannot show WHO is trading, so the wording describes the pattern
    only (it used to say "institutions building / reducing positions").
    """
    if i + 1 < 60:
        return 50.0, "Neutral", "Insufficient volume history", {"notes": []}
    v = b.volume
    v50 = float(v[max(0, i - 49): i + 1].mean()) or 1.0
    v200 = float(v[max(0, i - 199): i + 1].mean()) or 1.0
    trend = v50 / v200
    lo = max(0, i - 89)
    rets, v90 = b.ret[lo: i + 1], v[lo: i + 1]
    up, dn = rets > 0, rets < 0
    up_v = float(v90[up].mean()) if up.any() else 0.0
    dn_v = float(v90[dn].mean()) if dn.any() else 0.0
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
        pts += 16; notes.append(f"Up-days carry {ud:.2f}× more volume than down-days (90d)")
    elif ud <= 0.75:
        pts -= 14; notes.append(f"Down-days carry {1 / ud if ud else 0:.2f}× more volume than up-days (90d)")
    v10 = float(v[max(0, i - 9): i + 1].mean()) or 1.0
    older = v[max(0, i - 49): i - 9]
    v40 = (float(older.mean()) if len(older) else 0.0) or 1.0
    r10 = v10 / v40
    if r10 >= 1.15:
        pts += 10; notes.append(f"Recent 10-day volume {r10:.2f}× the weeks before — activity picking up")
    elif r10 <= 0.8:
        pts -= 6; notes.append("Recent volume quiet")
    score = clamp(pts)
    if score >= 65:
        label, det = "Accumulation", "Volume is supportive — heavier on up-days or picking up"
    elif score >= 45:
        label, det = "Neutral", "No clear signal from volume"
    else:
        label, det = "Distribution", "Volume is unsupportive — heavier on down-days or fading"
    return score, label, det, {"notes": notes}


# 52-week position → factor score. The five levels are the ones the model has
# always used; between them the score now moves in a straight line instead of
# jumping 15-20 points when the price crosses the edge of a band.
POSITION_AT = (7.5, 27.5, 50.0, 72.5, 92.5)      # % of the 52-week range
POSITION_SCORE = (20.0, 35.0, 55.0, 75.0, 92.0)


def score_position_52w(b: Bars, i: int) -> tuple[float, str, str]:
    """10% — position in the 52-week range (investment interpretation)."""
    if i + 1 < 60:
        return 50.0, "Neutral", "Insufficient history"
    lo = max(0, i - 251)
    y_high = float(np.nanmax(b.high[lo: i + 1]))
    y_low = float(np.nanmin(b.low[lo: i + 1]))
    p = float(b.close[i])
    if y_high == y_low:
        return 50.0, "Neutral", "Flat range"
    pos = (p - y_low) / (y_high - y_low) * 100.0
    score = float(np.interp(pos, POSITION_AT, POSITION_SCORE))
    if pos >= 85:
        label, det = "Breakout Zone", f"Top {100-pos:.0f}% of 52-week range — price strength"
    elif pos >= 60:
        label, det = "Upper Range", f"{pos:.0f}% above 52-week low — healthy trend"
    elif pos >= 40:
        label, det = "Mid Range", f"{pos:.0f}% above 52-week low — neutral"
    elif pos >= 15:
        label, det = "Lower Range", f"{pos:.0f}% above 52-week low — recovery watch"
    else:
        label, det = "Near 52W Low", f"Bottom {pos:.0f}% of range — weak position"
    return score, label, det


def _aligned_benchmark(close: pd.Series, benchmark: pd.Series | None):
    """The benchmark's close on each of the stock's session dates, as an array
    (None without benchmark data)."""
    if benchmark is None or len(benchmark) == 0:
        return None
    b = pd.Series(benchmark).dropna()
    b.index = pd.DatetimeIndex(b.index).normalize()
    b = b[~b.index.duplicated(keep="last")].sort_index()
    # SPY trades the same days; ffill only bridges a missing bar
    b = b.reindex(pd.DatetimeIndex(close.index).normalize(), method="ffill")
    return b.to_numpy(dtype=float)


def _relative_strength_at(c, bench, i: int) -> dict:
    out = {"benchmark": "S&P 500 (SPY)", "rs_3m": None, "rs_6m": None,
           "rs_12m": None, "label": "Not available", "score_adj": 0.0}
    if bench is None:
        return out

    def diff(n):
        if i < n or bench[i] != bench[i] or bench[i - n] != bench[i - n] or not bench[i - n]:
            return None
        stock = c[i] / c[i - n] - 1.0
        index = bench[i] / bench[i - n] - 1.0
        return round(float((stock - index) * 100.0), 2)

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


def relative_strength(close: pd.Series, benchmark: pd.Series | None) -> dict:
    """Stock return minus S&P 500 (SPY) return over 3 / 6 / 12 months, in
    percentage points, plus a small score adjustment.

    Beating the index over both 6 and 12 months is one of the most reliable
    signs of a long-term leader: +2 points (+4 when ahead by more than 10 pts
    on both); lagging on both costs the same. With no benchmark data the
    block is empty and the score is unchanged.
    """
    c = pd.Series(close).dropna()
    return _relative_strength_at(c.to_numpy(dtype=float), _aligned_benchmark(c, benchmark),
                                 len(c) - 1)


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

# bump when the result schema or the scoring changes
#   4: relative strength
#   5: each check counted once, new weights, 5-session average, rating margin;
#      candle patterns and support/resistance no longer move the score
ENGINE_VERSION = 5

MIN_BARS = 120          # sessions of history needed before a stock is scored

WEIGHTS = {
    "Long-Term Trend": 0.25,
    "Investment Momentum": 0.20,
    "Golden Cross": 0.15,
    "Stability": 0.15,
    "Volume": 0.15,
    "52W Position": 0.10,
}

FACTORS = (
    ("Long-Term Trend", score_longterm_trend),
    ("Investment Momentum", score_investment_momentum),
    ("Golden Cross", score_golden_cross),
    ("Stability", score_stability),
    ("Volume", score_accumulation_full),
    ("52W Position", score_position_52w),
)

# ---- a steadier score and rating ---------------------------------------------
# One session's reading swings several points on a single MACD or RSI tick, and
# a score sitting near a line used to flip the rating back and forth (a typical
# stock changed rating about 26 times in six months, two times in three
# reversed within a week). So:
SCORE_DAYS = 5          # the score is the average of the last 5 sessions' readings
RATING_MARGIN = 2.0     # a rating moves only once the score is this far past a line
RATING_REPLAY = 60      # sessions the margin rule is replayed over (≈ 3 months)

RATING_LINES = (35.0, 50.0, 65.0, 80.0)
RATING_NAMES = ("Strong Sell", "Sell", "Hold", "Buy", "Strong Buy")

DISCLAIMER = "A chart-based score for study — not investment advice."

VERDICTS = {
    "Strong Buy": "STRONG BUY — the long-term chart signals are strong across the board. "
                  "The score reads this as a favourable setup for long-term buying, ideally in stages.",
    "Buy": "BUY — most long-term chart signals are positive. The score reads this as a "
           "reasonable candidate to buy in stages, preferably on pullbacks.",
    "Hold": "HOLD — the signals are mixed. The score sees no clear case for adding or for "
            "selling; waiting for a clearer trend is reasonable.",
    "Sell": "SELL / REDUCE — the long-term trend is weakening on the chart. The score "
            "suggests caution with new buying and a review of existing holdings.",
    "Strong Sell": "STRONG SELL — the long-term trend is weak on the chart. The score "
                   "suggests avoiding fresh buying and reviewing whether to keep holding.",
}

def rating_for(score: float) -> str:
    """The rating a score maps to on its own (no margin)."""
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


def steady_rating(scores) -> str:
    """The rating at the end of a run of daily scores (oldest first).

    It starts as the plain rating of the first score and then moves only when
    a score is RATING_MARGIN points past a line: up to Buy at 67, back down to
    Hold below 63. A score hovering around 65 therefore keeps its rating.
    """
    level = RATING_NAMES.index(rating_for(scores[0]))
    for s in scores[1:]:
        while level < 4 and s >= RATING_LINES[level] + RATING_MARGIN:
            level += 1
        while level > 0 and s < RATING_LINES[level - 1] - RATING_MARGIN:
            level -= 1
    return RATING_NAMES[level]


def rating_note(score: float, rating: str) -> str | None:
    """One line for the UI when the margin is holding a rating in place."""
    plain = rating_for(score)
    if plain == rating:
        return None
    level = RATING_NAMES.index(rating)
    line = RATING_LINES[level - 1] if rating_rank(plain) < rating_rank(rating) else RATING_LINES[level]
    side = "below" if score < line else "above"
    return (f"Rating held at {rating}: the score is {side} {line:.0f} by less than "
            f"{RATING_MARGIN:g} points, and a rating changes only once the score is "
            f"{RATING_MARGIN:g} points past a line.")


def session_score(b: Bars, i: int) -> float:
    """ONE session's reading: the six weighted factors plus the relative-
    strength adjustment (the published score averages SCORE_DAYS of these)."""
    raw = sum(WEIGHTS[name] * fn(b, i)[0] for name, fn in FACTORS)
    return clamp(raw + _relative_strength_at(b.close, b.bench, i)["score_adj"])


def _averaged(readings: list[float], first: int, upto: int, count: int) -> list[float]:
    """Published scores for the `count` sessions ending at `upto`, oldest first.
    readings[k] is the reading of session first + k; a score is the average of
    up to SCORE_DAYS readings ending at its session, to one decimal."""
    out = []
    for j in range(upto - count + 1, upto + 1):
        seg = readings[max(0, j - first - SCORE_DAYS + 1): j - first + 1]
        out.append(round(sum(seg) / len(seg), 1))
    return out


def score_series(df: pd.DataFrame, benchmark: pd.Series | None = None) -> list[dict]:
    """Score and rating at the close of every session of `df` that has enough
    history, oldest first: [{"date", "score", "rating", "price"}, …].

    The same numbers analyze() gives for `df` cut off at that session, in one
    pass — the score history and the back-test use it instead of re-running
    the whole analysis for every past day.
    """
    df = df.dropna(subset=["Close"])
    if len(df) < MIN_BARS:
        return []
    b = Bars(df, benchmark)
    first = MIN_BARS - 1
    readings = [session_score(b, i) for i in range(first, b.n)]
    scores = _averaged(readings, first, b.n - 1, len(readings))
    out = []
    for k, score in enumerate(scores):
        i = first + k
        out.append({"date": str(getattr(df.index[i], "date", lambda: df.index[i])()),
                    "score": score,
                    "rating": steady_rating(scores[max(0, k - RATING_REPLAY): k + 1]),
                    "price": round(float(b.close[i]), 2)})
    return out


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
    if len(df) < MIN_BARS:
        raise ValueError(
            f"Insufficient price history for reliable analysis — "
            f"need at least {MIN_BARS} trading days (≈6 months), only {len(df)} found.")

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"].fillna(0)
    price = float(close.iloc[-1])

    # ---- daily indicators (2-year window) -----------------------------------
    b = Bars(df, benchmark)
    today = b.n - 1
    sma20 = sma(close, 20)
    sma50, sma150, sma200, sma260 = b.s_sma50, b.s_sma150, b.s_sma200, b.s_sma260
    rsi = b.s_rsi
    b_up, b_mid, b_lo = bollinger(close, 20, 2.0)
    m_line, m_sig, m_hist = b.s_macd, b.s_macd_sig, b.s_macd_hist
    atr = b.s_atr
    k, d = stochastic(high, low, close)
    roc63, roc126 = b.s_roc63, b.s_roc126

    # ---- sub-scores as they stand today ---------------------------------------
    factor_rows = [(name, fn(b, today)) for name, fn in FACTORS]

    # shown for information only — one candle or a nearby support / resistance
    # level no longer moves a long-term score
    pattern, _pattern_impact = detect_pattern(high, low, df["Open"], close)
    sr = support_resistance(close, high, low)
    rs = _relative_strength_at(b.close, b.bench, today)

    # ---- the score: average of the last SCORE_DAYS sessions' readings ---------
    first = max(MIN_BARS - 1, today - RATING_REPLAY - (SCORE_DAYS - 1))
    readings = [session_score(b, i) for i in range(first, today)]
    readings.append(clamp(sum(WEIGHTS[name] * row[0] for name, row in factor_rows)
                          + rs["score_adj"]))
    replay_from = max(MIN_BARS - 1, today - RATING_REPLAY)
    scores = _averaged(readings, first, today, today - replay_from + 1)
    score = scores[-1]
    rating = steady_rating(scores)

    signals = [{"name": name, "score": round(row[0], 1), "label": row[1],
                "detail": row[2], "weight": WEIGHTS[name]} for name, row in factor_rows]

    change_pct = (price / float(close.iloc[-2]) - 1.0) * 100.0 if len(close) > 1 else 0.0

    return {
        "ticker": "",
        "name": "",
        "engine_v": ENGINE_VERSION,
        "horizon": "Daily Charts · 3 Years — Delivery / Investment",
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "score": score,
        "rating": rating,
        "verdict": VERDICTS[rating],
        "disclaimer": DISCLAIMER,
        # how the score was steadied (the six factor rows describe TODAY only)
        "score_today": round(readings[-1], 1),
        "score_days": min(SCORE_DAYS, len(readings)),
        "rating_margin": RATING_MARGIN,
        "rating_note": rating_note(score, rating),
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
