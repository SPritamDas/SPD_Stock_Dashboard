"""
vivek_strategies.py
===================
Faithful, "as-taught" implementation of every strategy from the
"Trading with Vivek" 7-session course, plus the company-group rules.

Each strategy:
  - takes a raw OHLCV DataFrame (columns: Date, Open, High, Low, Close, Volume)
  - returns (summary: dict, opportunities: list[dict])
  - is self-contained (adds its own base indicators if missing)
so it drops straight into the existing run_strategy_on_batch(data_cache, func, **params).

Position sizing / group applicability is encoded in STRATEGY_CONFIG exactly as taught.

DATA-LIMITATION NOTES (read these):
  * Knoxville Divergence is codified from a public Pine v5 version of the indicator
    (bullish/bearish divergence via pivots + momentum + RSI). Settings follow Vivek's
    teaching: momentum=20, RSI=14, OB/OS=70/30, bars_back=200 (used as the max
    pivot-to-pivot distance; a small pivot strength keeps signals timely).
  * Lifetime-High and 3x-in-3-years need FUNDAMENTALS (highest-ever TTM profit/revenue,
    quarterly improvement). Live signal uses yfinance fundamentals when available; a
    fundamental BACKTEST is not feasible on free data, so those backtests are price-proxy
    and flagged as such.
  * 3x-in-3-years conditions 2/3/5 (reason-of-fall / reason-gone / future-prospect) are
    HUMAN JUDGMENT and surfaced as manual-review flags.
"""

import math

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# FUND-GROWTH PACE  ->  defines the backtest SUCCESS criterion
#   Plan (from the PDF): grow the fund GROWTH_MULTIPLE x in GROWTH_YEARS years
#   (default 3x in 3yr, ~44%/yr). A trade "succeeds" only if its target gain
#   arrives fast enough to stay on that pace: a Y% gain must be reached within
#       GROWTH_YEARS * ln(1 + Y/100) / ln(GROWTH_MULTIPLE)  years.
#   e.g.  +20% -> ~6 months,  +44% -> ~1yr,  +100% -> ~23 months,  +200% -> 3yr.
#   (Change to a pure CAGR, e.g. 40%: set GROWTH_MULTIPLE=1.40, GROWTH_YEARS=1.)
# ---------------------------------------------------------------------------
GROWTH_MULTIPLE = 3.0
GROWTH_YEARS = 3.0
TRADING_DAYS = 252


def pace_window_days(entry, target):
    """Trading days allowed to reach `target` from `entry` while staying on the
    GROWTH_MULTIPLE-in-GROWTH_YEARS pace. Returns 0 if there is no upside."""
    if entry <= 0 or target <= entry:
        return 0
    yrs = GROWTH_YEARS * math.log(target / entry) / math.log(GROWTH_MULTIPLE)
    return max(1, int(math.ceil(yrs * TRADING_DAYS)))


def _roundtrip_result(entry, exit_px, days):
    """Classify a signal-exit round-trip (SMA / Knoxville) with the SAME taxonomy
    as _backtest_entries: Success = profitable AND on-pace; Slow (off-pace) =
    profitable but too slow to count; Loss = sold at/below entry."""
    if exit_px <= entry:
        return "Loss"
    return "Success" if days <= pace_window_days(entry, exit_px) else "Slow (off-pace)"


# ---------------------------------------------------------------------------
# STRATEGY CONFIG  (groups + sizing exactly as taught)
#   groups: which sheet columns the strategy is allowed to run on
#   max_pct: max % of total portfolio in ONE stock for this strategy
#   per_trade_pct / max_trades / avg_gap_pct: sizing & averaging rules
# ---------------------------------------------------------------------------
STRATEGY_CONFIG = {
    "sma":            {"func": "strategy_sma",            "groups": ["v_40"],
                       "per_trade_pct": 3, "max_trades": 1, "avg_gap_pct": None, "max_pct": 3},
    "knoxville":      {"func": "strategy_knoxville",      "groups": ["v_40"],
                       "per_trade_pct": 3, "max_trades": 2, "avg_gap_pct": 5,    "max_pct": 6},
    "v20":            {"func": "strategy_v20",            "groups": ["v_40", "v_40_next", "v_200"],
                       "per_trade_pct": 3, "max_trades": 3, "avg_gap_pct": 10,   "max_pct": 9},
    "rhs":            {"func": "strategy_rhs",            "groups": ["v_40", "v_40_next"],
                       "per_trade_pct": 3, "max_trades": 1, "avg_gap_pct": None, "max_pct": 3},
    "cup_handle":     {"func": "strategy_cup_handle",     "groups": ["v_40", "v_40_next"],
                       "per_trade_pct": 3, "max_trades": 1, "avg_gap_pct": None, "max_pct": 3},
    "v10":            {"func": "strategy_v10",            "groups": ["v_40", "v_40_next"],
                       "per_trade_pct": 3, "max_trades": 2, "avg_gap_pct": 5,    "max_pct": 6},
    "lifetime_high":  {"func": "strategy_lifetime_high",  "groups": ["v_40", "v_40_next"],
                       "per_trade_pct": 3, "max_trades": 3, "avg_gap_pct": 10,   "max_pct": 10},
    "fifty_two_low":  {"func": "strategy_52w_low",        "groups": ["v_40", "v_40_next"],
                       "per_trade_pct": 4, "max_trades": 1, "avg_gap_pct": None, "max_pct": 4},
    "three_x_three":  {"func": "strategy_3x3y",           "groups": ["ALL_NSE"],
                       "per_trade_pct": 3, "max_trades": 1, "avg_gap_pct": None, "max_pct": 3},
}

# ===========================================================================
# SHARED HELPERS
# ===========================================================================

def add_base_indicators(df):
    """Idempotent: add MAs, lifetime high, 52w low/high, RSI, green-flag."""
    df = df.copy().reset_index(drop=True)
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"])
        # yfinance returns tz-aware dates; strip tz (Excel can't store tz-aware datetimes)
        if isinstance(df["Date"].dtype, pd.DatetimeTZDtype):
            df["Date"] = df["Date"].dt.tz_localize(None)
    for w in (20, 50, 100, 200, 300):
        col = f"dma_{w}"
        if col not in df.columns:
            df[col] = df["Close"].rolling(w).mean()
    if "lifetime_high" not in df.columns:
        df["lifetime_high"] = df["High"].expanding().max()
    if "low_52w" not in df.columns:
        df["low_52w"] = df["Low"].rolling(252, min_periods=20).min()
    if "high_52w" not in df.columns:
        df["high_52w"] = df["High"].rolling(252, min_periods=20).max()
    if "rsi_14" not in df.columns:
        df["rsi_14"] = _rsi(df["Close"], 14)
    if "is_green" not in df.columns:
        df["is_green"] = df["Close"] >= df["Open"]
    return df


def _ensure(df):
    return add_base_indicators(df) if "dma_200" not in df.columns else df


def _rsi(series, period=14):
    # Wilder's RSI (RMA / exponential smoothing, alpha=1/period) to match Pine's
    # ta.rsi — the Knoxville source. A zero-loss window is RSI 100 (not NaN).
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.where(loss != 0, 100.0)


def calc_stats(durations, total_ops):
    """Vivek's PERT-style expected duration: (p25 + p75 + 4*median)/6."""
    n = len(durations)
    rate = (n / total_ops * 100) if total_ops else 0
    if n:
        avg = int(round(np.mean(durations)))
        p25 = int(round(np.percentile(durations, 25)))
        med = int(round(np.median(durations)))
        p75 = int(round(np.percentile(durations, 75)))
        exp = int(round((p25 + p75 + 4 * med) / 6))
    else:
        avg = p25 = med = p75 = exp = 0
    return {
        "Total_Opportunities": total_ops, "Total_Successes": n,
        "Success_Rate_%": round(rate, 2), "Avg_Exit_Duration_Days": avg,
        "25th_Pct_Duration": p25, "Median_Duration": med,
        "75th_Pct_Duration": p75, "Exp_Duration": exp,
    }


def summarize_trades(total_ops, details):
    """Build the stat block from a list of trade `details`.
    SUCCESS = a valid opportunity whose target was reached on-pace.
    Success_Rate_% = successes / CLOSED trades — 'Open' trades (still held / target
    not yet reached) are EXCLUDED from the denominator so they don't drag the rate
    down. Closed = Success + Slow (off-pace) + Loss.
    Duration stats (Avg/Median/Pct/Exp) are over ALL CLOSED trades (how long a trade
    typically takes to resolve), not just successes. Win % stays success-only."""
    succ = [d for d in details if d.get("Result") == "Success"]
    n = len(succ)
    closed = [d for d in details if d.get("Result") != "Open"]   # resolved trades only
    n_closed = len(closed)
    n_nonloss = sum(1 for d in closed if d.get("Result") != "Loss")   # reached target (on/off-pace)
    rate = (n / n_closed * 100) if n_closed else 0
    nonloss_rate = (n_nonloss / n_closed * 100) if n_closed else 0
    durs = [d["Days_to_Exit"] for d in closed if d.get("Days_to_Exit") is not None]
    profs = [d["Profit_%"] for d in succ if d.get("Profit_%") is not None]
    if durs:
        avg = int(round(np.mean(durs)))
        p25 = int(round(np.percentile(durs, 25)))
        med = int(round(np.median(durs)))
        p75 = int(round(np.percentile(durs, 75)))
        exp = int(round((p25 + p75 + 4 * med) / 6))
    else:
        avg = p25 = med = p75 = exp = 0
    return {
        "Total_Opportunities": total_ops, "Total_Successes": n, "Closed_Trades": n_closed,
        "NonLoss_Trades": n_nonloss, "NonLoss_Rate_%": round(nonloss_rate, 2),
        "Success_Rate_%": round(rate, 2), "Avg_Exit_Duration_Days": avg,
        "25th_Pct_Duration": p25, "Median_Duration": med, "75th_Pct_Duration": p75,
        "Exp_Duration": exp,
        "Avg_Win_Profit_%": round(float(np.mean(profs)), 2) if profs else 0.0,
        "Median_Win_Profit_%": round(float(np.median(profs)), 2) if profs else 0.0,
    }


def _insufficient(df):
    """Minimal summary when there isn't enough history to evaluate the strategy.
    We STILL surface the current price (and an empty stat block) so the dashboard
    renders the panel with 'NOT NOW' instead of dropping the stock entirely."""
    cur = float(df["Close"].iloc[-1]) if len(df) else float("nan")
    return {"Ready_to_Invest": "NO",
            "current_price": round(cur, 2) if cur == cur else np.nan,
            "insufficient_history": True,
            **summarize_trades(0, [])}, []


def _dedupe_open_trades(details):
    """Drop pointless repeat entries so the backtest counts one position at a time per
    price level. Rule: while >=1 position toward a given Target_Price is still OPEN, a
    new entry toward that SAME target is kept ONLY if its Entry_Price is STRICTLY BELOW
    the lowest still-open entry toward that target — i.e. a genuine averaging-DOWN at a
    better price. A re-entry at the same-or-higher price while already holding is removed
    (no point buying again at 205 when you already hold 200 toward 400). Once every
    position toward a target has CLOSED, re-entry is allowed again. Different targets are
    independent (so different entry/target combinations can be open together).

    Causally sound (no look-ahead): the decision for an entry on date D only asks whether
    a PRIOR same-target position is still held on D (its exit is after D) — something you
    genuinely know on D; it never inspects the new entry's own future."""
    kept = []
    # Chronological; ties on the SAME day broken by LOWEST price first, so same-date /
    # same-target entries keep only the cheapest (a same-day higher re-entry is pointless)
    # and the result is order-independent (not dependent on how the strategy emitted them).
    for c in sorted(details, key=lambda d: (d["Entry_Date"], d["Entry_Price"])):
        tk, cdate, cpx = c["Target_Price"], c["Entry_Date"], c["Entry_Price"]
        open_entries = [k["Entry_Price"] for k in kept
                        if k["Target_Price"] == tk
                        and (k["Exit_Date"] is None or k["Exit_Date"] > cdate)]
        if open_entries and min(open_entries) <= cpx:
            continue                       # already holding at this price or better → skip the repeat
        kept.append(c)
    return kept


def _backtest_entries(df, entries):
    """entries: list of {idx, entry_price, target_price}. Counts only VALID long
    opportunities (target > entry). Classifies each:
      * Success         = target reached within the fund-growth-pace window.
      * Slow (off-pace) = target reached, but only AFTER that window (too slow to
                          stay on the 3x-in-3yr pace) -> does NOT count as success.
      * Open            = target never reached, even by the end of the data.
    Each hit (on- or off-pace) carries its realized Profit_%. Overlapping repeat entries
    at the same target & same-or-higher price (while still holding) are removed by
    _dedupe_open_trades, so Total_Opportunities counts one position per price level.
    Returns (total, durations, details); durations holds on-pace successes only."""
    details = []
    n = len(df)
    for e in entries:
        i = e["idx"]
        entry, target = e["entry_price"], e["target_price"]
        if not (entry > 0 and target > entry):   # skip degenerate / non-positive entries
            continue
        gain = round((target - entry) / entry * 100, 2)
        end = min(i + 1 + pace_window_days(entry, target), n)   # last on-pace bar
        base = {"Entry_Date": df["Date"].iloc[i], "Entry_Price": round(entry, 2),
                "Target_Price": round(target, 2)}
        hit_j = None
        for j in range(i + 1, n):             # scan to the end: tells slow from open
            if df["High"].iloc[j] >= target:
                hit_j = j
                break
        if hit_j is not None and hit_j < end:                 # on-pace win
            details.append({**base, "Result": "Success", "Exit_Date": df["Date"].iloc[hit_j],
                            "Days_to_Exit": hit_j - i, "Profit_%": gain})
        elif hit_j is not None:                               # hit, but too slow
            details.append({**base, "Result": "Slow (off-pace)", "Exit_Date": df["Date"].iloc[hit_j],
                            "Days_to_Exit": hit_j - i, "Profit_%": gain})
        else:                                                 # never reached target
            details.append({**base, "Result": "Open", "Exit_Date": None,
                            "Days_to_Exit": None, "Profit_%": None})
    details = _dedupe_open_trades(details)                    # one open position per price level
    total = len(details)
    durations = [d["Days_to_Exit"] for d in details if d.get("Result") == "Success"]
    return total, durations, details


def _pivot_lows(high, low, order=5):
    lo = low.values
    out = []
    for i in range(order, len(lo) - order):
        if lo[i] == lo[i - order:i + order + 1].min():
            out.append(i)
    return out


def _pivot_highs(high, low, order=5):
    hi = high.values
    out = []
    for i in range(order, len(hi) - order):
        if hi[i] == hi[i - order:i + order + 1].max():
            out.append(i)
    return out


def _green_streaks(df):
    """Runs of consecutive green candles. Returns list of (start_idx, end_idx)."""
    streaks, i, n = [], 0, len(df)
    g = df["is_green"].values
    while i < n:
        if g[i]:
            j = i
            while j < n and g[j]:
                j += 1
            if (j - 1) > i:           # need >1 green candle to form a streak
                streaks.append((i, j - 1))
            i = j
        else:
            i += 1
    return streaks


# ===========================================================================
# 1. SIMPLE MOVING AVERAGE  (inverse of golden cross)  --  V40 only
#    BUY  when 200dma > 50dma > 20dma > Close   (max pessimism)
#    SELL when Close > 20dma > 50dma > 200dma   (max optimism)
# ===========================================================================
def strategy_sma(df, **kw):
    df = _ensure(df)
    if len(df) < 320:
        return _insufficient(df)
    d20, d50, d200 = df["dma_20"], df["dma_50"], df["dma_200"]
    close = df["Close"]
    buy = (d200 > d50) & (d50 > d20) & (d20 > close)
    sell = (close > d20) & (d20 > d50) & (d50 > d200)

    # backtest: enter on first bar buy turns True (flat -> enter next open), exit on
    # first sell True after entry (next open). Classified like the pattern engine:
    # Success / Slow (off-pace) / Loss, plus a trailing Open if still held at the end.
    details, total = [], 0
    n, in_pos, entry_i, entry_px = len(df), False, None, None
    bv, sv = buy.values, sell.values
    for i in range(n - 1):
        if not in_pos and bv[i]:
            in_pos, entry_i = True, i + 1
            entry_px = float(df["Open"].iloc[i + 1])
        elif in_pos and sv[i]:
            exit_i = i + 1
            exit_px = float(df["Open"].iloc[i + 1])
            total += 1
            d = exit_i - entry_i
            details.append({
                "Entry_Date": df["Date"].iloc[entry_i], "Entry_Price": round(entry_px, 2),
                "Target_Price": round(exit_px, 2), "Result": _roundtrip_result(entry_px, exit_px, d),
                "Exit_Date": df["Date"].iloc[exit_i], "Days_to_Exit": d,
                "Profit_%": round((exit_px - entry_px) / entry_px * 100, 2)})
            in_pos = False
    if in_pos:                                       # still holding at end-of-data
        total += 1
        details.append({"Entry_Date": df["Date"].iloc[entry_i], "Entry_Price": round(entry_px, 2),
                        "Target_Price": np.nan, "Result": "Open",
                        "Exit_Date": None, "Days_to_Exit": None, "Profit_%": None})

    stats = summarize_trades(total, details)
    cur = float(close.iloc[-1])
    ready = "YES" if bool(buy.iloc[-1]) else "NO"
    summary = {
        "Ready_to_Invest": ready, "current_price": round(cur, 2),
        "Entry_Price": round(cur, 2) if ready == "YES" else np.nan,
        "Target_Price": np.nan,
        "current_200_dma": round(float(d200.iloc[-1]), 2),
        "Last_Signal_Date": str(df["Date"].iloc[-1].date()) if ready == "YES" else None,
    }
    return {**summary, **stats}, details


# ===========================================================================
# 2. KNOXVILLE DIVERGENCE  --  V40 only
#    Codified from the public Pine v5 Knoxville script. Settings per Vivek's
#    teaching: momentum=20, RSI=14, OB/OS=70/30, bars_back=200.
#    BUY  = bullish KD: at a pivot low, price lower-low vs the PREVIOUS pivot low,
#           momentum higher-low, and RSI < oversold.
#    SELL = bearish KD: at a pivot high, price higher-high vs the previous pivot
#           high, momentum lower-high, and RSI > overbought.
#    Backtest = buy -> next-sell state machine; "success" = profitable round-trip.
#    (The public script uses one `lookback` as pivot strength; the genuine
#    indicator's "bars back" is a scan window, so bars_back = max pivot-to-pivot
#    distance, and a small pivot_lookback keeps signals timely.)
# ===========================================================================
def strategy_knoxville(df, momentum=20, rsi_len=14, overbought=70, oversold=30,
                       bars_back=200, pivot_lookback=5, **kw):
    df = _ensure(df)
    if len(df) < max(bars_back, pivot_lookback * 2) + 30:
        return _insufficient(df)
    mom = df["Close"] - df["Close"].shift(momentum)          # ta.momentum(close, len)
    rsi = _rsi(df["Close"], rsi_len)
    plo = _pivot_lows(df["High"], df["Low"], order=pivot_lookback)
    phi = _pivot_highs(df["High"], df["Low"], order=pivot_lookback)

    # bullish KD bars (buy): current pivot low vs the previous pivot low
    bull = set()
    for k in range(1, len(plo)):
        cur, prev = plo[k], plo[k - 1]
        if cur - prev > bars_back:
            continue
        if (df["Low"].iloc[cur] < df["Low"].iloc[prev]       # price lower-low
                and mom.iloc[cur] > mom.iloc[prev]           # momentum higher-low
                and rsi.iloc[cur] < oversold):               # RSI oversold
            bull.add(cur)

    # bearish KD bars (sell): current pivot high vs the previous pivot high
    bear = set()
    for k in range(1, len(phi)):
        cur, prev = phi[k], phi[k - 1]
        if cur - prev > bars_back:
            continue
        if (df["High"].iloc[cur] > df["High"].iloc[prev]     # price higher-high
                and mom.iloc[cur] < mom.iloc[prev]           # momentum lower-high
                and rsi.iloc[cur] > overbought):             # RSI overbought
            bear.add(cur)

    # backtest: a pivot at index `cur` is only CONFIRMABLE pivot_lookback bars later,
    # so act on the confirmation bar (cur + pivot_lookback), entering at the next
    # open — this removes look-ahead bias. Classified like the pattern engine.
    n = len(df)
    bull_act = {c + pivot_lookback for c in bull}
    bear_act = {c + pivot_lookback for c in bear}
    details, total = [], 0
    in_pos, entry_i, entry_px = False, None, None
    for i in range(n - 1):
        if not in_pos and i in bull_act:
            in_pos, entry_i = True, i + 1
            entry_px = float(df["Open"].iloc[i + 1])
        elif in_pos and i in bear_act:
            exit_i = i + 1
            exit_px = float(df["Open"].iloc[i + 1])
            total += 1
            d = exit_i - entry_i
            details.append({
                "Entry_Date": df["Date"].iloc[entry_i], "Entry_Price": round(entry_px, 2),
                "Target_Price": round(exit_px, 2), "Result": _roundtrip_result(entry_px, exit_px, d),
                "Exit_Date": df["Date"].iloc[exit_i], "Days_to_Exit": d,
                "Profit_%": round((exit_px - entry_px) / entry_px * 100, 2)})
            in_pos = False
    if in_pos:                                       # still holding at end-of-data
        total += 1
        details.append({"Entry_Date": df["Date"].iloc[entry_i], "Entry_Price": round(entry_px, 2),
                        "Target_Price": np.nan, "Result": "Open",
                        "Exit_Date": None, "Days_to_Exit": None, "Profit_%": None})

    stats = summarize_trades(total, details)

    cur_px = float(df["Close"].iloc[-1])
    win_lo = n - (pivot_lookback + 3)                        # latest detectable pivot window
    recent_bull = [b for b in bull if b >= win_lo]
    recent_bear = [b for b in bear if b >= win_lo]
    ready = "YES" if (recent_bull and (not recent_bear or max(recent_bull) > max(recent_bear))) else "NO"
    summary = {
        "Ready_to_Invest": ready, "current_price": round(cur_px, 2),
        "Entry_Price": round(cur_px, 2) if ready == "YES" else np.nan,
        "current_RSI": round(float(rsi.iloc[-1]), 2) if pd.notna(rsi.iloc[-1]) else None,
        "current_momentum": round(float(mom.iloc[-1]), 2) if pd.notna(mom.iloc[-1]) else None,
        "Last_Signal_Date": str(df["Date"].iloc[max(recent_bull)].date()) if recent_bull else None,
        "note": "codified from public Pine v5 Knoxville; settings per Vivek (mom=20, RSI=14, OB/OS=70/30, bars_back=200)",
    }
    return {**summary, **stats}, details


# ===========================================================================
# 3. V20  --  V40, V40 Next, V200    (script.py strategy_v20 logic)
#    Streak labelling, Signal_Condition (Streak_Profit > 20% AND Open < 200 DMA),
#    drop_duplicates dedup, ready/entry/target and the backtest entry scan are as in
#    script.py. TWO intentional tweaks: (1) a green run of just ONE candle counts if
#    that single candle moved > 20% (script.py required >= 2); (2) the SUCCESS window
#    uses the global 3x-in-3yr pace (your later choice) not script.py's exit_days.
#    Entry/target are surfaced even when not ready (chart watch lines) and the
#    green-run span is exposed for the highlight.
# ===========================================================================
def strategy_v20(df, profit_target_pct=20, dma_period=200, **kw):
    df = _ensure(df).reset_index(drop=True)
    n = len(df)
    if n < 30:
        return _insufficient(df)
    dma_col = f"dma_{dma_period}"
    if dma_col not in df.columns:
        df[dma_col] = df["Close"].rolling(dma_period).mean()

    # --- streak labelling: identical to script.py add_technical_indicators ---
    candel = np.where(df["Close"].values >= df["Open"].values, "GREEN", "RED")
    s_open = np.full(n, np.nan); s_close = np.full(n, np.nan); s_prof = np.full(n, np.nan)
    s_beg = np.full(n, -1); s_end = np.full(n, -1)

    def _label(a, b):                                  # inclusive green run [a, b]
        o = float(df["Open"].iloc[a])
        if o <= 0:
            return
        c = float(df["Close"].iloc[b])
        s_open[a:b + 1] = o; s_close[a:b + 1] = c
        s_prof[a:b + 1] = (c - o) / o * 100
        s_beg[a:b + 1] = a; s_end[a:b + 1] = b

    streak_start = None
    for i in range(n):
        if candel[i] == "GREEN":
            if streak_start is None:
                streak_start = i
        else:
            if streak_start is not None and i - streak_start >= 1:   # >= 1 green candle
                _label(streak_start, i - 1)
            streak_start = None
    if streak_start is not None and n - streak_start >= 1:
        _label(streak_start, n - 1)

    df["Streak_Open"] = s_open
    df["Streak_Close"] = s_close
    df["Streak_Profit"] = s_prof

    # --- signals: 20%+ streak formed (partly) below the 200 DMA, one per unique range ---
    sig_cond = (df["Streak_Profit"] > profit_target_pct) & (df["Open"] < df[dma_col])
    unique_signals = df[sig_cond].drop_duplicates(subset=["Streak_Open", "Streak_Close"])

    cur = float(df["Close"].iloc[-1])
    if unique_signals.empty:
        return {"Ready_to_Invest": "NO", "current_price": round(cur, 2),
                "Entry_Price": np.nan, "Target_Price": np.nan, "exp_profit_pct": np.nan,
                **summarize_trades(0, [])}, []

    # --- live signal: most recent qualifying range; ready when price < lower line ---
    last = unique_signals.sort_values(by="Date").iloc[-1]
    entry, target = float(last["Streak_Open"]), float(last["Streak_Close"])
    ready = "YES" if cur < entry else "NO"
    exp_profit = round((target - cur) / cur * 100, 2) if (ready == "YES" and cur > 0) else np.nan

    # --- backtest: after the run ends, enter when Close drops back below Streak_Open ---
    entries = []
    for si in unique_signals.index:
        h_entry = float(df.loc[si, "Streak_Open"]); h_target = float(df.loc[si, "Streak_Close"])
        end_idx = si + 1
        while end_idx < n and df.loc[end_idx, "Streak_Open"] == h_entry:
            end_idx += 1
        ent = None
        for j in range(end_idx, n):
            if df.loc[j, "Close"] < h_entry:
                ent = j
                break
        if ent is not None:
            entries.append({"idx": ent, "entry_price": h_entry, "target_price": h_target})
    total, durs, details = _backtest_entries(df, entries)     # success = target hit on the 3x-in-3yr pace
    stats = summarize_trades(total, details)

    summary = {"Ready_to_Invest": ready, "current_price": round(cur, 2),
               "Entry_Price": round(entry, 2), "Target_Price": round(target, 2),
               "exp_profit_pct": exp_profit,
               f"current_{dma_period}_dma": round(float(df[dma_col].iloc[-1]), 2)
               if pd.notna(df[dma_col].iloc[-1]) else None}
    a, b = int(s_beg[last.name]), int(s_end[last.name])       # green run behind the active range
    if a >= 0:
        summary["range_from"] = str(df["Date"].iloc[a].date())
        summary["range_to"] = str(df["Date"].iloc[b].date())
        summary["range_green_candles"] = int(b - a + 1)
    return {**summary, **stats}, details


# ===========================================================================
# 4. REVERSE HEAD & SHOULDER  --  V40, V40 Next
#    3 troughs: left-shoulder, head (lowest), right-shoulder; neckline ~horizontal.
#    BUY at break above right-shoulder base with green candle (closing basis).
#    TARGET = max(technical target = head-depth above neckline, lifetime high).
# ===========================================================================
def strategy_rhs(df, neckline_tol=0.04, min_gain_pct=40, **kw):
    df = _ensure(df)
    if len(df) < 60:
        return _insufficient(df)
    order = 5
    plows = _pivot_lows(df["High"], df["Low"], order=order)
    phighs = _pivot_highs(df["High"], df["Low"], order=order)
    if len(plows) < 3:
        return _empty_pattern(df)

    entries, patterns = [], []
    for a in range(len(plows) - 2):
        ls, hd, rs = plows[a], plows[a + 1], plows[a + 2]
        ls_p, hd_p, rs_p = float(df["Low"].iloc[ls]), float(df["Low"].iloc[hd]), float(df["Low"].iloc[rs])
        if not (hd_p < ls_p and hd_p < rs_p):           # head must be the lowest
            continue
        # ---- shape-quality gates (reject lop-sided / shallow = fake inverse H&S) ----
        if abs(ls_p - rs_p) / min(ls_p, rs_p) > 0.15:   # shoulders roughly level (within 15%)
            continue
        if (min(ls_p, rs_p) - hd_p) / hd_p < 0.04:      # head a clear low: >=4% below the shoulders
            continue
        if (rs - ls) < 20:                              # pattern must span real time, not a few bars
            continue
        necks = [h for h in phighs if ls < h < rs]
        if len(necks) < 2:
            continue
        neck = float(np.mean([df["High"].iloc[h] for h in necks]))
        nvals = [df["High"].iloc[h] for h in necks]
        if (max(nvals) - min(nvals)) / neck > neckline_tol:   # neckline must be ~horizontal
            continue
        # taught rule: neckline must NOT cut through the BODY of a green candle
        # (wicks and red-candle bodies may be crossed)
        crossed = False
        for j in range(ls, rs + 1):
            o, c = df["Open"].iloc[j], df["Close"].iloc[j]
            if c >= o and min(o, c) < neck < max(o, c):
                crossed = True
                break
        if crossed:
            continue
        depth = neck - hd_p
        tech_target = neck + depth
        # buy = first green candle closing above the right-shoulder high. The right
        # shoulder is a pivot only CONFIRMABLE `order` bars later, so the breakout scan
        # must start at rs+order+1 — acting before that = look-ahead on a pivot that
        # wasn't yet knowable. (rs_high's window is then entirely in the past at entry.)
        rs_high = float(df["High"].iloc[max(0, rs - order):rs + order + 1].max())
        buy_i = None
        for j in range(rs + order + 1, len(df)):
            if df["is_green"].iloc[j] and df["Close"].iloc[j] > rs_high:
                buy_i = j
                break
        if buy_i is None:
            continue
        lth = float(df["lifetime_high"].iloc[buy_i])   # ATH as of the breakout, not the shoulder
        target = max(tech_target, lth)                 # higher of the two (RHS rule)
        entry_px = float(df["Close"].iloc[buy_i])
        if (target - entry_px) / entry_px * 100 < min_gain_pct:   # only >=40% potential
            continue
        entries.append({"idx": buy_i, "entry_price": entry_px, "target_price": target})
        patterns.append({"buy_i": buy_i, "entry": entry_px, "target": target,
                         "ls": ls, "hd": hd, "rs": rs, "level": neck})  # geometry for the plot

    total, durs, details = _backtest_entries(df, entries)   # success = target hit on the 3x-in-3y pace
    stats = summarize_trades(total, details)
    return _pattern_summary(df, patterns, details, stats, "RHS")


# ===========================================================================
# 5. CUP WITH HANDLE  --  V40, V40 Next
#    = RHS without left shoulder. Cup (deep U) then handle (shallower).
#    BUY at handle-base breakout (green candle). TARGET = technical target ONLY.
# ===========================================================================
def strategy_cup_handle(df, neckline_tol=0.05, **kw):
    df = _ensure(df)
    if len(df) < 60:
        return _insufficient(df)
    order = 5
    plows = _pivot_lows(df["High"], df["Low"], order=order)
    phighs = _pivot_highs(df["High"], df["Low"], order=order)
    if len(plows) < 2:
        return _empty_pattern(df)

    entries, patterns = [], []
    for a in range(len(plows) - 1):
        cup, handle = plows[a], plows[a + 1]
        cup_p, hnd_p = float(df["Low"].iloc[cup]), float(df["Low"].iloc[handle])
        if hnd_p <= cup_p:                              # handle low must sit ABOVE the cup low
            continue
        rims = [h for h in phighs if cup < h < handle]
        if not rims:
            continue
        rim = float(max(df["High"].iloc[h] for h in rims))   # the cup's right lip = resistance
        depth = rim - cup_p
        if depth <= 0:
            continue
        # ---- shape-quality gates (reject shallow / lop-sided / micro "cups" = fakes) ----
        cup_depth_pct = depth / rim
        if not (0.12 <= cup_depth_pct <= 0.60):         # a real cup is ~12-60% deep
            continue
        if hnd_p < cup_p + 0.5 * depth:                 # handle must stay in the UPPER HALF of the cup
            continue
        if (handle - cup) < 20:                         # cup must span real time (~1 month+), not a few bars
            continue
        # breakout = green candle CLOSING above the rim (the cup's resistance lip). The
        # handle is a pivot only CONFIRMABLE `order` bars later, so scan from handle+order+1.
        buy_i = None
        for j in range(handle + order + 1, len(df)):
            if df["is_green"].iloc[j] and df["Close"].iloc[j] > rim:
                buy_i = j
                break
        if buy_i is None:
            continue
        entry_px = float(df["Close"].iloc[buy_i])
        target = entry_px + depth                       # measured move: cup depth above the breakout
        entries.append({"idx": buy_i, "entry_price": entry_px, "target_price": target})
        patterns.append({"buy_i": buy_i, "entry": entry_px, "target": target,
                         "cup": cup, "handle": handle, "level": rim})  # geometry for the plot

    total, durs, details = _backtest_entries(df, entries)   # success = target hit on the 3x-in-3y pace
    stats = summarize_trades(total, details)
    return _pattern_summary(df, patterns, details, stats, "CWH")


def _empty_pattern(df):
    cur = float(df["Close"].iloc[-1])
    return {"Ready_to_Invest": "NO", "current_price": round(cur, 2),
            **summarize_trades(0, [])}, []


PATTERN_RECENT_BARS = 10        # a breakout this many bars old (or newer) is still "forming"


def _pattern_summary(df, patterns, details, stats, label):
    cur = float(df["Close"].iloc[-1])
    shapes = _pattern_shapes(df, patterns, label, cur)
    # READY only when a pattern is CURRENTLY forming (fresh breakout, not yet at
    # target, price still valid). Past/completed patterns are reference-only.
    active = [s for s in shapes if s["active"]]
    ready, entry, target = "NO", np.nan, np.nan
    if active:
        last = active[-1]
        ready, entry, target = "YES", last["buy_y"], last["target"]
    exp = round((target - cur) / cur * 100, 2) if (target == target) else np.nan
    summary = {"Ready_to_Invest": ready, "current_price": round(cur, 2),
               "Entry_Price": round(entry, 2) if entry == entry else np.nan,
               "Target_Price": round(target, 2) if target == target else np.nan,
               "exp_profit_pct": exp, "pattern": label,
               "active_patterns": len(active), "past_patterns": len(shapes) - len(active),
               "pattern_shapes": shapes}
    return {**summary, **stats}, details


def _pattern_shapes(df, patterns, label, cur=None):
    """Convert every detected pattern (current AND historical) into chart geometry:
       the trough outline (RHS: L-shoulder/Head/R-shoulder; CWH: cup/handle), the
       neckline/rim level, and the breakout (buy) point. Each shape is tagged
       active=True only if it is CURRENTLY forming — breakout within the last
       PATTERN_RECENT_BARS bars, price has NOT yet reached target (i.e. the trade
       hasn't already succeeded), and price still HOLDS above the neckline/rim it
       broke (a close back below that level = failed breakout, so not actionable).
       A normal pull-back/throwback that retests the neckline still counts as active.
       Past patterns are active=False and shown for reference only. Dates are ISO
       strings so the chart plots them on the date axis without index alignment."""
    n = len(df)
    if cur is None:
        cur = float(df["Close"].iloc[-1]) if n else float("nan")

    def _d(i):
        return str(df["Date"].iloc[int(i)].date())

    def _lo(i):
        return round(float(df["Low"].iloc[int(i)]), 2)

    shapes = []
    for p in patterns:
        bi = int(p["buy_i"])
        bd = _d(bi)
        if label == "RHS":
            pts_i = [p["ls"], p["hd"], p["rs"]]
            tags = ["L-shoulder", "Head", "R-shoulder"]
        else:                                            # CWH
            pts_i = [p["cup"], p["handle"]]
            tags = ["Cup", "Handle"]
        entry, target = float(p["entry"]), float(p["target"])
        level = float(p["level"])                       # neckline (RHS) / rim (CWH)
        recent = bi >= n - PATTERN_RECENT_BARS
        high_since = float(df["High"].iloc[bi:].max()) if n else target
        not_succeeded = high_since < target             # target never hit since breakout
        held = (cur == cur) and cur >= level            # breakout level still holds (throwback ok)
        active = bool(recent and not_succeeded and held)
        shapes.append({
            "type": label, "active": active,
            "x": [_d(i) for i in pts_i],
            "y": [_lo(i) for i in pts_i],
            "tags": tags,
            "level": round(float(p["level"]), 2),        # neckline (RHS) / rim (CWH)
            "lvl_x0": _d(pts_i[0]), "lvl_x1": bd,
            "buy_x": bd, "buy_y": round(entry, 2),
            "target": round(target, 2)})
    return shapes


# ===========================================================================
# 6. V10  --  V40, V40 Next  (rides inside an open RHS/CWH trade)
#    Within the buy->target window of an RHS/CWH signal, any >=10% fall from a
#    peak = buy; sell when price returns to that peak. Max 2 V10 trades (5% apart).
# ===========================================================================
def strategy_v10(df, drop_pct=10, **kw):
    df = _ensure(df)
    # derive the active RHS/CWH window from the pattern detectors
    rhs_sum, _ = strategy_rhs(df)
    cwh_sum, _ = strategy_cup_handle(df)
    parent_ready = (rhs_sum.get("Ready_to_Invest") == "YES") or (cwh_sum.get("Ready_to_Invest") == "YES")

    # backtest V10 over the whole series as a proxy: every 10% pullback from a
    # running local peak, target = that peak.
    entries, n = [], len(df)
    peak = float(df["High"].iloc[0])
    last_entry_px = None
    for i in range(1, n):
        peak = max(peak, float(df["High"].iloc[i]))
        if peak > 0 and (peak - df["Low"].iloc[i]) / peak * 100 >= drop_pct:
            entry_px = peak * (1 - drop_pct / 100)
            # enforce taught rule: each new V10 entry must be >= 5% below the previous
            if last_entry_px is None or entry_px <= last_entry_px * 0.95:
                entries.append({"idx": i, "entry_price": entry_px, "target_price": peak})
                last_entry_px = entry_px
                peak = float(df["High"].iloc[i])       # reset peak ONLY after an accepted entry
    total, durs, details = _backtest_entries(df, entries)   # success = peak reclaimed on the 3x-in-3y pace
    stats = summarize_trades(total, details)

    cur = float(df["Close"].iloc[-1])
    recent_peak = float(df["High"].iloc[-60:].max())
    dropped = (recent_peak - cur) / recent_peak * 100 if recent_peak else 0
    ready = "YES" if (parent_ready and dropped >= drop_pct) else "NO"
    summary = {"Ready_to_Invest": ready, "current_price": round(cur, 2),
               "parent_pattern_active": parent_ready,
               "pct_below_recent_peak": round(dropped, 2),
               "Entry_Price": round(cur, 2) if ready == "YES" else np.nan,
               "Target_Price": round(recent_peak, 2) if ready == "YES" else np.nan,
               # surface the parent RHS + CWH patterns (current + historical) for the plot
               "pattern_shapes": (rhs_sum.get("pattern_shapes") or [])
                                 + (cwh_sum.get("pattern_shapes") or [])}
    return {**summary, **stats}, details


# ===========================================================================
# 7. LIFETIME HIGH  --  V40, V40 Next
#    Highest-ever TTM revenue AND net profit, AND >=30% below lifetime high.
#    BUY, TARGET = lifetime high. Average every further 10% down (30/40/50%).
#    Fundamentals via optional `fundamentals` dict (see fetch_fundamentals()).
# ===========================================================================
def strategy_lifetime_high(df, below_pct=30, fundamentals=None, **kw):
    df = _ensure(df)
    if len(df) < 60:
        return _insufficient(df)
    cur = float(df["Close"].iloc[-1])
    lth = float(df["lifetime_high"].iloc[-1])
    pct_below = (lth - cur) / lth * 100 if lth else 0

    # PRICE-PROXY backtest: each time price first drops >=30% below the running
    # lifetime high, target = lifetime high at that point.
    entries, n = [], len(df)
    triggered = False
    for i in range(20, n):
        lh = float(df["lifetime_high"].iloc[i])
        below = (lh - df["Close"].iloc[i]) / lh * 100 if lh else 0
        if below >= below_pct and not triggered:
            entries.append({"idx": i, "entry_price": float(df["Close"].iloc[i]),
                            "target_price": lh})
            triggered = True
        if below < below_pct * 0.5:                    # re-arm once it recovers
            triggered = False
    total, durs, details = _backtest_entries(df, entries)   # success = reclaim ATH on the 3x-in-3y pace
    stats = summarize_trades(total, details)

    # FUNDAMENTAL gate (live signal only)
    ttm_high = None
    if fundamentals:
        ttm_high = fundamentals.get("ttm_revenue_is_highest") and \
                   fundamentals.get("ttm_netprofit_is_highest")
    price_ok = pct_below >= below_pct
    if fundamentals is None:
        ready = "REVIEW" if price_ok else "NO"          # need fundamentals to confirm
    else:
        ready = "YES" if (price_ok and ttm_high) else "NO"

    actionable = ready in ("YES", "REVIEW")
    summary = {"Ready_to_Invest": ready, "current_price": round(cur, 2),
               "lifetime_high": round(lth, 2), "pct_below_lifetime_high": round(pct_below, 2),
               "Entry_Price": round(cur, 2) if actionable else np.nan,       # buy at current price
               "Target_Price": round(lth, 2) if actionable else np.nan,      # target = lifetime high
               "exp_profit_pct": round((lth - cur) / cur * 100, 2) if (actionable and cur > 0) else np.nan,
               "cond_price_30pct_below": price_ok,
               "cond_ttm_revenue_highest": fundamentals.get("ttm_revenue_is_highest") if fundamentals else None,
               "cond_ttm_netprofit_highest": fundamentals.get("ttm_netprofit_is_highest") if fundamentals else None,
               "ttm_highest_ever": ttm_high,
               "note": "fundamental backtest not feasible on free data (price-proxy used)"}
    return {**summary, **stats}, details


# ===========================================================================
# 8. 52-WEEK LOW  --  V40, V40 Next
#    BUY when price is AT / within threshold_pct of the 52-week low; TARGET =
#    lifetime high (prior ATH). threshold_pct is a small band (default 5%): with
#    0% the rule "Close <= rolling-min-Low" is near-impossible (the min includes
#    today), so it almost never triggers and the backtest comes up empty.
# ===========================================================================
def strategy_52w_low(df, threshold_pct=5, gap_days=5, **kw):
    df = _ensure(df)
    if len(df) < 260:
        return _insufficient(df)
    cur = float(df["Close"].iloc[-1])
    low52 = float(df["low_52w"].iloc[-1])
    dist = (cur - low52) / low52 * 100 if low52 else 0
    ready = "YES" if dist <= threshold_pct else "NO"

    # backtest: each time Close touches the 52w low (within threshold), enter;
    # target = lifetime high (prior ATH) at that bar.
    entries, last_i, n = [], -10 ** 9, len(df)
    for i in range(252, n):
        line = df["low_52w"].iloc[i] * (1 + threshold_pct / 100)
        if df["Close"].iloc[i] <= line and (i - last_i) > gap_days:
            entries.append({"idx": i, "entry_price": float(df["Close"].iloc[i]),
                            "target_price": float(df["lifetime_high"].iloc[i])})
            last_i = i
    total, durs, details = _backtest_entries(df, entries)   # success = reclaim ATH on the 3x-in-3y pace
    stats = summarize_trades(total, details)

    lth = float(df["lifetime_high"].iloc[-1])
    summary = {"Ready_to_Invest": ready, "current_price": round(cur, 2),
               "52W_Low": round(low52, 2), "Dist_From_52W_Low_%": round(dist, 2),
               "Target_Price": round(lth, 2) if ready == "YES" else np.nan,
               "exp_profit_pct": round((lth - cur) / cur * 100, 2) if ready == "YES" else np.nan}
    return {**summary, **stats}, details


# ===========================================================================
# 9. THREE TIMES IN THREE YEARS  --  ALL NSE-listed
#    10 conditions. Codifiable: (1) >=67% below LTH, (7) still >=50% below LTH,
#    (4) past track record, (6) latest-quarter improvement [fundamentals].
#    Conditions 2,3,5 (reason-of-fall / reason-gone / future-prospect) = MANUAL.
#    BUY, +100% within 12m -> exit, else hold till lifetime high.
# ===========================================================================
def strategy_3x3y(df, fall_pct=67, still_below_pct=50, fundamentals=None, **kw):
    df = _ensure(df)
    if len(df) < 60:
        return _insufficient(df)
    cur = float(df["Close"].iloc[-1])
    lth = float(df["lifetime_high"].iloc[-1])
    pct_below = (lth - cur) / lth * 100 if lth else 0

    cond1_67 = pct_below >= fall_pct
    cond7_50 = pct_below >= still_below_pct

    # PRICE-PROXY backtest: when price has fallen >=67% from running LTH, did it
    # then double (+100%) on the global 3x-in-3yr pace (~477 trading days / ~1.9 yr)?
    entries, n, triggered = [], len(df), False
    for i in range(20, n):
        lh = float(df["lifetime_high"].iloc[i])
        below = (lh - df["Close"].iloc[i]) / lh * 100 if lh else 0
        if below >= fall_pct and not triggered:
            ep = float(df["Close"].iloc[i])
            entries.append({"idx": i, "entry_price": ep, "target_price": ep * 2.0})
            triggered = True
        if below < fall_pct * 0.6:
            triggered = False
    total, durs, details = _backtest_entries(df, entries)  # success = +100% on the 3x-in-3y pace (~23 months)
    stats = summarize_trades(total, details)

    # fundamentals (live)
    qtr_improved = fundamentals.get("latest_quarter_improved") if fundamentals else None
    track_record = fundamentals.get("good_track_record") if fundamentals else None

    codifiable_ok = cond1_67 and cond7_50
    if fundamentals:
        codifiable_ok = codifiable_ok and bool(qtr_improved) and bool(track_record)
    ready = "REVIEW" if codifiable_ok else "NO"          # always REVIEW: 2/3/5 are human calls

    summary = {
        "Ready_to_Invest": ready, "current_price": round(cur, 2),
        "lifetime_high": round(lth, 2), "pct_below_lifetime_high": round(pct_below, 2),
        "cond1_fall_67pct": cond1_67, "cond7_still_50pct_down": cond7_50,
        "cond4_track_record": track_record, "cond6_quarter_improved": qtr_improved,
        "MANUAL_cond2_reason_of_fall": "REVIEW", "MANUAL_cond3_reason_gone": "REVIEW",
        "MANUAL_cond5_future_prospect": "REVIEW",
        "Target_Price": round(cur * 2, 2) if ready == "REVIEW" else np.nan,
    }
    return {**summary, **stats}, details


# ===========================================================================
# OPTIONAL: fundamentals fetch for LTH & 3x3y (yfinance, best-effort)
# ===========================================================================
def fetch_fundamentals(ticker):
    """Return dict with raw per-quarter & per-year revenue/net-profit history (for the
    trend graph) + TTM-highest flags / quarter-improvement (for the signal gate), or
    {} on failure. yfinance gives ~4-5 quarters and ~4 years for free."""
    try:
        import yfinance as yf
        tk = q = None
        for suffix in (".NS", ".BO"):             # NSE first, then BSE (BSE-only / NSE-missing names)
            _tk = yf.Ticker(f"{ticker}{suffix}")
            try:
                _q = _tk.quarterly_financials   # rows = line items, cols = quarters (newest first)
            except Exception:
                _q = None
            if _q is not None and not getattr(_q, "empty", True):
                tk, q = _tk, _q
                break
        if tk is None or q is None or getattr(q, "empty", True):
            return {}
        try:
            af = tk.financials        # annual income statement (newest first)
        except Exception:
            af = None

        def raw_series(frame, names):
            """Pick the first matching row, drop NaN, order oldest -> newest."""
            if frame is None or getattr(frame, "empty", True):
                return pd.Series(dtype=float)
            for r in names:
                if r in frame.index:
                    return frame.loc[r].dropna().astype(float)[::-1]
            return pd.Series(dtype=float)

        def _hist(s):                                   # [[period-end ISO date, value], ...]
            return [[str(pd.Timestamp(d).date()), float(v)] for d, v in s.items()]

        # try common yfinance row aliases (quarterly & annual sheets can differ slightly)
        REV = ["Total Revenue", "TotalRevenue", "Operating Revenue", "OperatingRevenue"]
        NI = ["Net Income", "NetIncome", "Net Income Common Stockholders",
              "NetIncomeCommonStockholders"]
        q_rev, q_ni = raw_series(q, REV), raw_series(q, NI)      # quarterly (raw)
        a_rev, a_ni = raw_series(af, REV), raw_series(af, NI)    # annual (raw)
        rev_ttm = q_rev.rolling(4).sum().dropna()               # TTM for the highest-ever test
        ni_ttm = q_ni.rolling(4).sum().dropna()

        out = {}
        if len(q_rev):
            out["quarterly_revenue_hist"] = _hist(q_rev)
        if len(q_ni):
            out["quarterly_netprofit_hist"] = _hist(q_ni)
            out["good_track_record"] = bool((q_ni.diff().dropna() > 0).mean() >= 0.5)
            if len(q_ni) >= 2:
                out["latest_quarter_improved"] = bool(q_ni.iloc[-1] > q_ni.iloc[-2])  # QoQ
        if len(a_rev):
            out["annual_revenue_hist"] = _hist(a_rev)
        if len(a_ni):
            out["annual_netprofit_hist"] = _hist(a_ni)
        if len(rev_ttm):
            out["ttm_revenue_is_highest"] = bool(rev_ttm.iloc[-1] >= rev_ttm.max() - 1e-6)
        if len(ni_ttm):
            out["ttm_netprofit_is_highest"] = bool(ni_ttm.iloc[-1] >= ni_ttm.max() - 1e-6)

        # ---- ratios from .info (best-effort; often None/shaky for NSE) ----
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}

        def _f(key, scale=1.0):
            v = info.get(key)
            try:
                return round(float(v) * scale, 2) if v is not None else None
            except (TypeError, ValueError):
                return None

        out["pe_trailing"] = _f("trailingPE")
        out["pe_forward"] = _f("forwardPE")
        out["debt_to_equity"] = _f("debtToEquity", 0.01)      # Yahoo gives % -> ratio
        out["roe_pct"] = _f("returnOnEquity", 100.0)          # fraction -> %
        out["roa_pct"] = _f("returnOnAssets", 100.0)
        out["sales_growth_pct"] = _f("revenueGrowth", 100.0)
        out["profit_growth_pct"] = _f("earningsGrowth", 100.0)

        # balance sheet + income statement (for ROCE and ratio fallbacks)
        bs = ist = None
        try:
            bs, ist = tk.balance_sheet, tk.income_stmt
        except Exception:
            pass

        def _row(frame, names):
            if frame is None or getattr(frame, "empty", True):
                return None
            for nm in names:
                if nm in frame.index:
                    s = frame.loc[nm].dropna()
                    if len(s):
                        return float(s.iloc[0])               # newest column
            return None

        ebit = _row(ist, ["EBIT", "Ebit", "Operating Income", "OperatingIncome"])
        ta = _row(bs, ["Total Assets", "TotalAssets"])
        cl = _row(bs, ["Current Liabilities", "Total Current Liabilities", "CurrentLiabilities"])
        equity = _row(bs, ["Stockholders Equity", "Total Stockholder Equity",
                           "Common Stock Equity", "Total Equity Gross Minority Interest"])
        debt = _row(bs, ["Total Debt", "TotalDebt", "Net Debt"])
        net_inc = _row(ist, ["Net Income", "NetIncome", "Net Income Common Stockholders"])

        # ROCE = EBIT / (Total Assets - Current Liabilities)
        if ebit is not None and ta is not None and cl is not None and (ta - cl):
            out["roce_pct"] = round(ebit / (ta - cl) * 100, 2)

        # ---- inputs for Screener's Graham number: median 5yr ROCE & sales growth ----
        def _row_series(frame, names):
            if frame is None or getattr(frame, "empty", True):
                return None
            for nm in names:
                if nm in frame.index:
                    return frame.loc[nm].dropna().astype(float)
            return None

        roce_5yr = None
        ebit_s = _row_series(ist, ["EBIT", "Ebit", "Operating Income", "OperatingIncome"])
        ta_s = _row_series(bs, ["Total Assets", "TotalAssets"])
        cl_s = _row_series(bs, ["Current Liabilities", "Total Current Liabilities", "CurrentLiabilities"])
        if ebit_s is not None and ta_s is not None and cl_s is not None:
            common = ebit_s.index.intersection(ta_s.index).intersection(cl_s.index)
            roce_yrs = [float(ebit_s[c] / (ta_s[c] - cl_s[c]) * 100)
                        for c in common if (ta_s[c] - cl_s[c])]
            if roce_yrs:                                      # median over the available (<=5) years
                roce_5yr = float(np.median(roce_yrs))
                out["roce_5yr_pct"] = round(roce_5yr, 2)

        sales_growth_5yr = None
        if len(a_rev) >= 2:
            g = a_rev.pct_change().dropna()                   # YoY revenue growth (fraction)
            if len(g):
                sales_growth_5yr = float(np.median(g.tail(5))) * 100   # median of last <=5 yrs, %
                out["sales_growth_5yr_pct"] = round(sales_growth_5yr, 2)

        # ---- fallbacks so the cards don't go blank when .info omits a ratio ----
        eps, bvps = info.get("trailingEps"), info.get("bookValue")
        cp = info.get("currentPrice") or info.get("regularMarketPrice")
        if out.get("pe_trailing") is None and cp and eps:
            try:
                if float(eps) > 0:
                    out["pe_trailing"] = round(float(cp) / float(eps), 2)
            except (TypeError, ValueError):
                pass
        if out.get("debt_to_equity") is None and debt is not None and equity not in (None, 0):
            out["debt_to_equity"] = round(debt / equity, 2)
        if out.get("roe_pct") is None and net_inc is not None and equity not in (None, 0):
            out["roe_pct"] = round(net_inc / equity * 100, 2)
        if out.get("sales_growth_pct") is None and len(a_rev) >= 2 and a_rev.iloc[-2]:
            out["sales_growth_pct"] = round((a_rev.iloc[-1] / a_rev.iloc[-2] - 1) * 100, 2)
        if out.get("profit_growth_pct") is None and len(a_ni) >= 2 and a_ni.iloc[-2]:
            out["profit_growth_pct"] = round((a_ni.iloc[-1] / a_ni.iloc[-2] - 1) * 100, 2)
        # ---- Intrinsic value = Screener's Graham number ----
        #   sqrt(EPS * BookValue * maxPE * maxPBV)
        #   maxPE  = median 5yr sales growth * 1.5   (clamped to [8, 100])
        #   maxPBV = 5yr ROCE / 8                     (clamped to [1, 10])
        # Falls back to the CLASSIC Graham (fixed 22.5 = 15 * 1.5) when 5yr growth/ROCE
        # are unavailable, so the card isn't blank. Requires EPS>0 and BookValue>0.
        try:
            if eps and bvps and float(eps) > 0 and float(bvps) > 0:
                eps_f, bv_f = float(eps), float(bvps)
                if sales_growth_5yr is not None and roce_5yr is not None:
                    max_pe = min(100.0, max(8.0, sales_growth_5yr * 1.5))
                    max_pbv = min(10.0, max(1.0, roce_5yr / 8.0))
                    out["graham_max_pe"] = round(max_pe, 2)
                    out["graham_max_pbv"] = round(max_pbv, 2)
                    out["intrinsic_graham"] = round((eps_f * bv_f * max_pe * max_pbv) ** 0.5, 2)
                    out["intrinsic_basis"] = "Screener (maxPE = 5yr sales-growth×1.5, maxPBV = 5yr ROCE/8)"
                else:
                    out["intrinsic_graham"] = round((22.5 * eps_f * bv_f) ** 0.5, 2)
                    out["intrinsic_basis"] = "classic Graham (22.5) — 5yr growth/ROCE unavailable"
        except (TypeError, ValueError):
            pass

        return out
    except Exception:
        return {}


def fetch_shareholding(ticker):
    """CURRENTLY DISABLED — not called by fetch_fundamentals (Screener returned no
    data in testing). Kept here so it can be re-enabled later by restoring the
    `out.update(fetch_shareholding(ticker))` line in fetch_fundamentals.

    Best-effort scrape of the quarterly shareholding pattern from Screener.in:
    promoter / FII / DII / public / pledge % for the LATEST quarter. yfinance carries
    NO Indian shareholding data, so this is a separate (fragile, may break if the page
    changes / IP is rate-limited) source. Returns {} on any failure.

    NOTE: hits the network — only runs during the cache build / live fundamentals fetch."""
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0 Safari/537.36")}
        html = None
        for path in (f"{ticker}/consolidated/", f"{ticker}/"):
            try:
                r = requests.get(f"https://www.screener.in/company/{path}",
                                 headers=headers, timeout=15)
            except Exception:
                continue
            if r.status_code == 200 and "shareholding" in r.text.lower():
                html = r.text
                break
        if not html:
            return {}
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")   # stdlib fallback if lxml absent
        sec = soup.find("section", id="shareholding")
        table = sec.find("table") if sec else None
        if table is None:
            return {}
        thead = table.find("thead")
        heads = [th.get_text(strip=True) for th in thead.find_all("th")] if thead else []
        # latest quarter = last header carrying a digit (skips a trailing 'Trend'/blank column)
        qheads = [h for h in heads if any(ch.isdigit() for ch in h)]
        latest_q = qheads[-1] if qheads else (heads[-1] if heads else None)
        # row-label PREFIXES (lower-cased) -> output key. startswith avoids false matches
        # (e.g. "public" can't grab a "No. of Shareholders" row); aliases cover label variants.
        wanted = [(["promoter"], "promoter_pct"),
                  (["fii", "foreign"], "fii_pct"),
                  (["dii", "domestic"], "dii_pct"),
                  (["government", "govt"], "govt_pct"),
                  (["public"], "public_pct"),
                  (["pledge"], "pledge_pct")]
        body = table.find("tbody")
        out = {}
        for tr in (body.find_all("tr") if body else []):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            # last NUMERIC data cell = latest quarter (robust to a trailing trend/chart column)
            val = None
            for c in cells[1:]:
                t = c.get_text(strip=True).replace("%", "").replace(",", "")
                try:
                    val = float(t)
                except ValueError:
                    continue
            if val is None:
                continue
            for keys, okey in wanted:
                if okey not in out and any(label.startswith(k) for k in keys):
                    out[okey] = round(val, 2)
                    break
        if out:
            out["shareholding_quarter"] = latest_q
            out["shareholding_source"] = "screener.in"
        return out
    except Exception:
        return {}


FUNCTION_MAP = {
    "strategy_sma": strategy_sma,
    "strategy_knoxville": strategy_knoxville,
    "strategy_v20": strategy_v20,
    "strategy_rhs": strategy_rhs,
    "strategy_cup_handle": strategy_cup_handle,
    "strategy_v10": strategy_v10,
    "strategy_lifetime_high": strategy_lifetime_high,
    "strategy_52w_low": strategy_52w_low,
    "strategy_3x3y": strategy_3x3y,
}
