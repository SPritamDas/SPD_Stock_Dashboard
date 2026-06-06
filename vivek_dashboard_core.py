"""
vivek_dashboard_core.py
=======================
Pure analysis + chart builders for the interactive dashboard.
No Streamlit, no network here -> unit-testable in isolation.

  analyze(skey, ticker, df_raw, fundamentals) -> dict with summary, opps, indicator df
  build_chart(analysis)                        -> a TradingView-style Plotly figure
  kpi_block(analysis)                          -> dict of headline numbers for the KPI row
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import vivek_strategies as vs

# Pretty labels + one-line entry/exit blurbs (for the dashboard header)
STRATEGY_LABELS = {
    "sma":           "SMA — inverse golden cross",
    "knoxville":     "Knoxville Divergence",
    "v20":           "V20 — consecutive green-candle range",
    "rhs":           "Reverse Head & Shoulder",
    "cup_handle":    "Cup with Handle",
    "v10":           "V10 — 10% pullback rider",
    "lifetime_high": "Lifetime High",
    "fifty_two_low": "52-Week Low",
    "three_x_three": "3× in 3 Years (turnaround)",
}

STRATEGY_BLURB = {
    "sma":           "**Buy** when `200DMA > 50DMA > 20DMA > Close` (max pessimism); **sell** when fully inverted. The deliberate *opposite* of the golden cross. **V40 only.**",
    "knoxville":     "**Buy** on bullish KD (price lower-low + momentum higher-low + RSI<30); **sell** on bearish KD (price higher-high + momentum lower-high + RSI>70). Codified from the public Pine v5 script; settings mom=20, RSI=14, bars_back=200. **V40 only.**",
    "v20":           "A V20 range = a run of **consecutive green candles** (even a **single candle** that moved >20%) whose move from the **first candle's open** to the **last candle's close** is **>20%**, formed **below the 200 DMA**. **Buy** when price falls back below that lower line (first open); **target** = the upper line (last close). **V40 / V40 Next / V200.**",
    "rhs":           "**Buy** the green-close breakout from the right-shoulder base; **target** = higher of (head-depth projection, lifetime high). Only if ≥40% upside. **V40 / V40 Next.**",
    "cup_handle":    "**Buy** the green-close breakout from the handle base; **target** = cup-depth projection (NOT raised to lifetime high). **V40 / V40 Next.**",
    "v10":           "Rides an open RHS/CWH: **buy** any 10% pullback from a peak, **sell** back at the peak (~+11%). **V40 / V40 Next.**",
    "lifetime_high": "**Buy** when TTM revenue & net-profit are highest-ever AND price ≥30% below lifetime high; **target** = lifetime high. Needs fundamentals → may show `REVIEW`. **V40 / V40 Next.**",
    "fifty_two_low": "**Buy** at the 52-week low; **target** = lifetime high (prior all-time high). **V40 / V40 Next.**",
    "three_x_three": "**Buy** a turnaround ≥67% below lifetime high (still ≥50% down) with improving quarter; **target** = +100%. Reason-of-fall is human-judgment → shows `REVIEW`. **All NSE.**",
}

# Market indices for the Indices-analysis mode. yfinance tickers used AS-IS (no .NS append).
INDICES = [
    # ---- Indian (NSE) ----
    {"region": "Indian", "name": "NIFTY 50",              "ticker": "^NSEI",                "meaning": "Top 50 large-cap companies in India"},
    {"region": "Indian", "name": "NIFTY NEXT 50",         "ticker": "^NSMIDCP",             "meaning": "Next 50 companies after NIFTY 50"},
    {"region": "Indian", "name": "NIFTY 100",             "ticker": "^CNX100",              "meaning": "Top 100 Indian companies"},
    {"region": "Indian", "name": "NIFTY 200",             "ticker": "^CNX200",              "meaning": "Top 200 companies"},
    {"region": "Indian", "name": "NIFTY 500",             "ticker": "^CRSLDX",              "alts": ["^CNX500"], "meaning": "Broad Indian market (500 stocks)"},
    {"region": "Indian", "name": "NIFTY MIDCAP 50",       "ticker": "^NSEMDCP50",           "meaning": "Mid-sized Indian companies"},
    {"region": "Indian", "name": "NIFTY MIDCAP 100",      "ticker": "NIFTY_MIDCAP_100.NS",  "meaning": "Top Indian midcaps"},
    {"region": "Indian", "name": "NIFTY SMALLCAP 100",    "ticker": "^CNXSC",               "alts": ["NIFTY_SMLCAP_100.NS", "^CNXSMCP", "^NIFTYSMLCAP100"], "meaning": "Small-cap companies"},
    {"region": "Indian", "name": "NIFTY BANK (Bank Nifty)", "ticker": "^NSEBANK",           "meaning": "Major Indian banking stocks"},
    {"region": "Indian", "name": "NIFTY FINANCIAL SERVICES", "ticker": "NIFTY_FIN_SERVICE.NS", "alts": ["^CNXFINSERVICE", "^CNXFIN"], "meaning": "Financial sector companies"},
    {"region": "Indian", "name": "NIFTY IT",              "ticker": "^CNXIT",               "meaning": "Indian IT companies"},
    {"region": "Indian", "name": "NIFTY FMCG",            "ticker": "^CNXFMCG",             "meaning": "FMCG sector"},
    {"region": "Indian", "name": "NIFTY AUTO",            "ticker": "^CNXAUTO",             "meaning": "Automobile sector"},
    {"region": "Indian", "name": "NIFTY PHARMA",          "ticker": "^CNXPHARMA",           "meaning": "Pharmaceutical companies"},
    {"region": "Indian", "name": "NIFTY METAL",           "ticker": "^CNXMETAL",            "meaning": "Metals and mining"},
    {"region": "Indian", "name": "NIFTY REALTY",          "ticker": "^CNXREALTY",           "meaning": "Real estate companies"},
    {"region": "Indian", "name": "NIFTY ENERGY",          "ticker": "^CNXENERGY",           "meaning": "Energy sector"},
    {"region": "Indian", "name": "NIFTY PSU BANK",        "ticker": "^CNXPSUBANK",          "meaning": "Public sector banks"},
    {"region": "Indian", "name": "NIFTY INFRASTRUCTURE",  "ticker": "^CNXINFRA",            "meaning": "Infrastructure companies"},
    {"region": "Indian", "name": "INDIA VIX",             "ticker": "^INDIAVIX",            "meaning": "India market volatility (fear) index"},
    # ---- US ----
    {"region": "US", "name": "S&P 500",                   "ticker": "^GSPC",   "meaning": "Largest 500 US companies"},
    {"region": "US", "name": "Dow Jones Industrial Avg",  "ticker": "^DJI",    "meaning": "30 major US companies"},
    {"region": "US", "name": "NASDAQ Composite",          "ticker": "^IXIC",   "meaning": "Broad Nasdaq-listed stocks"},
    {"region": "US", "name": "NASDAQ 100",                "ticker": "^NDX",    "meaning": "Largest non-financial Nasdaq stocks"},
    {"region": "US", "name": "Russell 2000",              "ticker": "^RUT",    "meaning": "Small-cap US stocks"},
    {"region": "US", "name": "Russell 1000",              "ticker": "^RUI",    "meaning": "Large + mid-cap US stocks"},
    {"region": "US", "name": "S&P 100",                   "ticker": "^OEX",    "meaning": "Top 100 US companies"},
    {"region": "US", "name": "S&P 400 MidCap",            "ticker": "^MID",    "meaning": "Mid-cap US stocks"},
    {"region": "US", "name": "S&P 600 SmallCap",          "ticker": "^SP600",  "meaning": "Small-cap S&P stocks"},
    {"region": "US", "name": "CBOE Volatility Index (VIX)", "ticker": "^VIX",  "meaning": "US market fear/volatility index"},
    {"region": "US", "name": "Dow Jones Transportation",  "ticker": "^DJT",    "meaning": "Transportation companies"},
    {"region": "US", "name": "NYSE Composite",            "ticker": "^NYA",    "meaning": "Broad NYSE market"},
    {"region": "US", "name": "Wilshire 5000",             "ticker": "^W5000",  "alts": ["^FTW5000", "^W5000T"], "meaning": "Very broad US market"},
    {"region": "US", "name": "Philadelphia Semiconductor (SOX)", "ticker": "^SOX", "meaning": "Semiconductor sector"},
]

# NSE archive CSV (https://archives.nseindia.com/content/indices/<file>) per index name —
# used to list each index's constituent company symbols. Indian NIFTY indices only
# (India VIX has no constituents; US indices aren't on NSE). Filenames are best-effort.
INDEX_CONSTITUENT_CSV = {
    "NIFTY 50": "ind_nifty50list.csv",
    "NIFTY NEXT 50": "ind_niftynext50list.csv",
    "NIFTY 100": "ind_nifty100list.csv",
    "NIFTY 200": "ind_nifty200list.csv",
    "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY MIDCAP 50": "ind_niftymidcap50list.csv",
    "NIFTY MIDCAP 100": "ind_niftymidcap100list.csv",
    "NIFTY SMALLCAP 100": "ind_niftysmallcap100list.csv",
    "NIFTY BANK (Bank Nifty)": "ind_niftybanklist.csv",
    "NIFTY FINANCIAL SERVICES": "ind_niftyfinancelist.csv",
    "NIFTY IT": "ind_niftyitlist.csv",
    "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv",
    "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY PSU BANK": "ind_niftypsubanklist.csv",
    "NIFTY INFRASTRUCTURE": "ind_niftyinfralist.csv",
}

_GREEN, _RED = "#26a69a", "#ef5350"
_WIN, _LOSS = "#00c853", "#d50000"


def _clean_ohlc(df):
    """Clean yfinance OHLCV:
      (a) drop trailing/holiday rows with NaN OHLC, and
      (b) trim *pre-listing data-splice artifacts*: a single-bar move > 55% is never a
          real move for our large-cap / index universe — it's an unadjusted split or a
          DEMERGER splice (e.g. BAJAJFINSV carrying old Bajaj Auto prices pre-2008). We
          cut to AFTER the last such bar so lifetime-high / trends use only real history.
    Returns a fresh 0..n-1 indexed frame (works even on an already-built cache)."""
    if df is None:
        return df
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    if cols:
        df = df.dropna(subset=cols)
    df = df.reset_index(drop=True)
    if "Close" in df.columns and len(df) > 30:
        jump = df["Close"].pct_change().abs().values            # single-bar % move
        bad = np.where(jump > 0.55)[0]                          # >55% in one bar = artifact
        if len(bad):
            df = df.iloc[int(bad[-1]):].reset_index(drop=True)  # keep only post-artifact history
    return df.reset_index(drop=True)


def analyze(skey, ticker, df_raw, fundamentals=None):
    """Run one strategy on one ticker; return everything the UI needs."""
    cfg = vs.STRATEGY_CONFIG[skey]
    fn = vs.FUNCTION_MAP[cfg["func"]]
    df_raw = _clean_ohlc(df_raw)                 # strip NaN price rows before anything reads them
    summary, opps = fn(df_raw.copy(), fundamentals=fundamentals)
    df_ind = vs.add_base_indicators(df_raw)
    opp_df = pd.DataFrame(opps)
    if not opp_df.empty and "Entry_Date" in opp_df:
        opp_df["Entry_Date"] = pd.to_datetime(opp_df["Entry_Date"])
        if "Exit_Date" in opp_df:
            opp_df["Exit_Date"] = pd.to_datetime(opp_df["Exit_Date"], errors="coerce")
    return {"skey": skey, "ticker": ticker, "cfg": cfg,
            "summary": summary or {}, "opps": opp_df, "df": df_ind}


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def kpi_block(a):
    """Headline numbers for the KPI row."""
    s, opps = a["summary"], a["opps"]
    last_opp_date, last_opp_result = None, None
    if not opps.empty and "Entry_Date" in opps:
        last = opps.sort_values("Entry_Date").iloc[-1]
        last_opp_date = last["Entry_Date"]
        last_opp_result = last.get("Result")
    cur = _num(s.get("current_price"))
    entry = _num(s.get("Entry_Price"))
    target = _num(s.get("Target_Price"))
    exp_profit = _num(s.get("exp_profit_pct"))
    # SMA / Knoxville exit on a SIGNAL, not a fixed price target -> Target_Price is
    # blank. When such a signal is live, fall back to the historical median winning
    # move so the user still gets an expected target/return (consistent with how the
    # "approx. time" is already backtest-derived). Flagged so the UI can show a "~".
    target_estimated = False
    if target is None and entry and s.get("Ready_to_Invest") in ("YES", "REVIEW"):
        mw = _num(s.get("Median_Win_Profit_%")) or _num(s.get("Avg_Win_Profit_%"))
        if mw and mw > 0:
            target = round(entry * (1 + mw / 100), 2)
            target_estimated = True
            if exp_profit is None:
                exp_profit = round(mw, 2)
    if exp_profit is None and entry and target and cur:
        exp_profit = round((target - cur) / cur * 100, 2)
    return {
        "ready": s.get("Ready_to_Invest", "NO"),
        "current_price": cur,
        "entry": entry,
        "target": target,
        "target_estimated": target_estimated,
        "exp_profit_pct": exp_profit,
        "exp_duration_days": s.get("Exp_Duration"),
        "success_rate": s.get("Success_Rate_%"),
        "nonloss_rate": s.get("NonLoss_Rate_%"),
        "total_ops": s.get("Total_Opportunities"),
        "total_closed": s.get("Closed_Trades"),
        "total_succ": s.get("Total_Successes"),
        "total_nonloss": s.get("NonLoss_Trades"),
        "median_days": s.get("Median_Duration"),
        "avg_win_profit": s.get("Avg_Win_Profit_%"),
        "median_win_profit": s.get("Median_Win_Profit_%"),
        "last_signal_date": s.get("Last_Signal_Date"),
        "last_opp_date": last_opp_date,
        "last_opp_result": last_opp_result,
    }


def growth_baselines(df, future_bdays=504, breakpoint_date=None):   # ~2 years projected forward
    """Log-space (constant-CAGR) growth baselines projected forward — a 'minimum growth'
    reference. Two lines:
      - 'full' : one log-linear regression over ALL history (overall CAGR).
      - 'early': the EARLY-segment CAGR. The split ('1st segment ends') is auto-detected
                 (best 2-piece log fit) UNLESS `breakpoint_date` is given, in which case
                 all bars up to that date form the 1st segment.
    Straight line in log space == constant % growth, which is what 'extend the early
    slope' actually means (a linear-price extension of a 100->3700 stock is meaningless).
    Each sub-dict carries the projected (x,y) line, cagr_pct, price_today and
    pct_vs_baseline (current price vs the line today). Returns None if too little data."""
    if df is None or len(df) < 120:
        return None
    close = df["Close"].values.astype(float)
    dates = pd.to_datetime(df["Date"]).reset_index(drop=True)
    if (close <= 0).any():
        return None
    n = len(close)
    t = np.arange(n, dtype=float)
    y = np.log(close)
    span_days = (dates.iloc[-1] - dates.iloc[0]).days or 1
    bpy = (n - 1) / (span_days / 365.25)                 # bars per year, from real dates
    try:
        fut = pd.bdate_range(dates.iloc[-1] + pd.Timedelta(days=1), periods=future_bdays)
    except Exception:
        fut = pd.DatetimeIndex([])
    all_x = [str(pd.Timestamp(d).date()) for d in list(dates) + list(fut)]
    all_t = np.arange(n + len(fut), dtype=float)
    cur = float(close[-1])

    def _line(m, b):
        yv = np.exp(b + m * all_t)
        today = float(np.exp(b + m * (n - 1)))
        return {"x": all_x, "y": [round(float(v), 2) for v in yv],
                "cagr_pct": round((float(np.exp(m * bpy)) - 1) * 100, 2),
                "price_today": round(today, 2), "current_price": round(cur, 2),
                "pct_vs_baseline": round((cur - today) / today * 100, 2) if today else None}

    m_full, b_full = np.polyfit(t, y, 1)
    full = _line(m_full, b_full)

    early = None
    k_user = None
    if breakpoint_date is not None:                       # user-chosen split: bars up to this date = 1st segment
        try:
            kd = pd.Timestamp(breakpoint_date)
            k_user = int((dates <= kd).sum())
            k_user = max(5, min(n - 5, k_user))           # keep >=5 bars on each side
        except Exception:
            k_user = None

    if k_user is not None:                                # MANUAL breakpoint
        m1, b1 = np.polyfit(t[:k_user], y[:k_user], 1)
        m2, b2 = np.polyfit(t[k_user:], y[k_user:], 1)
        early = _line(m1, b1)
        early["breakpoint_date"] = str(pd.Timestamp(dates.iloc[k_user - 1]).date())
        early["accelerated"] = bool(m2 > m1)
        early["user_set"] = True
    else:                                                 # AUTO breakpoint (best 2-piece log fit)
        lo, hi = int(n * 0.2), int(n * 0.8)
        if hi - lo > 10:
            step = max(1, (hi - lo) // 150)
            best = None
            for k in range(lo, hi, step):
                m1, b1 = np.polyfit(t[:k], y[:k], 1)
                m2, b2 = np.polyfit(t[k:], y[k:], 1)
                sse = (float(np.sum((y[:k] - (m1 * t[:k] + b1)) ** 2))
                       + float(np.sum((y[k:] - (m2 * t[k:] + b2)) ** 2)))
                if best is None or sse < best[0]:
                    best = (sse, k, m1, b1, m2, b2)
            _, k, m1, b1, m2, b2 = best
            early = _line(m1, b1)                          # 1st-phase fit — ALWAYS drawn
            early["breakpoint_date"] = str(pd.Timestamp(dates.iloc[k]).date())
            early["accelerated"] = bool(m2 > m1)           # did the 2nd phase actually steepen?
            early["user_set"] = False
    return {"full": full, "early": early, "bars_per_year": round(float(bpy), 1)}


def index_macro(df):
    """Macro top/bottom signals + 'safe-zone' for an INDEX (needs dma_50/200/300):
      - bottom signal  : 50 DMA < 200 DMA AND 50 DMA < 300 DMA (the bottoming cross).
      - top signal     : price didn't touch the 300 DMA for a whole year (~252 bars) -> exhaustion.
      - safe-zone ceil : last_high + ((last_high-bottom)/bottom)*last_high  (the SAME % recovery
                         applied again on top of the last high; = last_high^2/bottom). bottom = the
                         trough at a bottoming cross, last_high = the recovery peak after it.
                         Below the ceiling = growth still 'safe'; above = unsafe/overextended.
    Computes this for EVERY historical bottoming-cross episode + all 1-yr exhaustion zones.
    Returns a dict, or None if too little history."""
    need = ("dma_50", "dma_200", "dma_300")
    if df is None or len(df) < 360 or not all(c in df.columns for c in need):
        return None
    d = df.reset_index(drop=True)
    n = len(d)
    dates = pd.to_datetime(d["Date"])
    close, low, high = d["Close"].values, d["Low"].values, d["High"].values
    d50, d200, d300 = d["dma_50"].values, d["dma_200"].values, d["dma_300"].values
    cur = float(close[-1])

    # ---- bottoming cross: 50 < 200 AND 50 < 300 — find ALL contiguous episodes ----
    cross = (d50 < d200) & (d50 < d300)                    # NaN comparisons -> False (early bars)
    cross_now = bool(cross[-1])
    runs, i = [], 0
    while i < n:
        if cross[i]:
            j = i
            while j < n and cross[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    # each episode: trough within the cross-run + recovery high up to the NEXT episode (or end)
    episodes = []
    for ri, (s, e) in enumerate(runs):
        b_rel = s + int(np.argmin(low[s:e + 1]))
        nxt = runs[ri + 1][0] if ri + 1 < len(runs) else n
        h_end = max(nxt, b_rel + 1)
        h_rel = b_rel + int(np.argmax(high[b_rel:h_end]))
        bm, hh = float(low[b_rel]), float(high[h_rel])
        # safe-zone ceiling = last_high + ((last_high - bottom)/bottom) * last_high
        #                   = apply the SAME % recovery again on top of last_high  (= hh^2/bm)
        ceil_ = (hh + (hh - bm) / bm * hh) if (hh > bm and bm > 0) else None
        episodes.append({
            "bottom_date": str(dates.iloc[b_rel].date()), "bottom_most": round(bm, 2),
            "high_date": str(dates.iloc[h_rel].date()), "last_high": round(hh, 2),
            "ceiling": round(ceil_, 2) if ceil_ else None,
            "end_date": str(dates.iloc[min(h_end - 1, n - 1)].date())})

    # ---- latest-episode summary (drives the panel + the prominent ceiling line) ----
    bottom_most = bottom_date = last_high = last_high_date = None
    safe_limit = safe_status = pct_to_safe = None
    if episodes:
        le = episodes[-1]
        bottom_most, bottom_date = le["bottom_most"], le["bottom_date"]
        last_high, last_high_date = le["last_high"], le["high_date"]
        safe_limit = le["ceiling"]
        if safe_limit:
            safe_status = "SAFE" if cur < safe_limit else "UNSAFE"
            pct_to_safe = round((safe_limit - cur) / cur * 100, 2)

    # ---- top: 1-year 300-DMA exhaustion — current flag + ALL historical zones ----
    valid = ~np.isnan(d300)
    vi = np.where(valid)[0]
    exhaustion = None
    if len(vi) >= 252:
        win = vi[-252:]
        exhaustion = bool(not np.any(low[win] <= d300[win]))
    touch = np.where(valid & (low <= d300))[0]
    days_since_touch = int(n - 1 - touch[-1]) if len(touch) else None
    # zones = contiguous spans where the trailing 1yr never touched the 300 DMA
    rmin = pd.Series(low - d300).rolling(252).min().values   # NaN where <252 history
    exhausted = np.where(np.isnan(rmin), False, rmin > 0)
    zones, i = [], 0
    while i < n:
        if exhausted[i]:
            j = i
            while j < n and exhausted[j]:
                j += 1
            zones.append([str(dates.iloc[i].date()), str(dates.iloc[j - 1].date())])
            i = j
        else:
            i += 1

    return {
        "current": round(cur, 2),
        "dma50": round(float(d50[-1]), 2) if not np.isnan(d50[-1]) else None,
        "dma200": round(float(d200[-1]), 2) if not np.isnan(d200[-1]) else None,
        "dma300": round(float(d300[-1]), 2) if not np.isnan(d300[-1]) else None,
        "bottom_cross_now": cross_now,
        "bottom_most": bottom_most, "bottom_date": bottom_date,
        "last_high": last_high, "last_high_date": last_high_date,
        "safe_limit": safe_limit, "safe_status": safe_status, "pct_to_safe": pct_to_safe,
        "exhaustion_top": exhaustion, "days_since_300dma_touch": days_since_touch,
        "episodes": episodes,                              # ALL bottoming-cross episodes (past + latest)
        "exhaustion_zones": zones,                         # ALL 1-yr-no-300DMA-touch spans
    }


def build_chart(a, measure=None, baselines=None, extend_to_projection=False, macro=None):
    """TradingView-style candlestick: price + MAs + per-trade entry/exit markers,
    always-on entry/target lines, optional measure line, growth-baseline projections,
    volume subplot, pan/scroll zoom + range buttons.
    `measure` = ((d0,p0),(d1,p1),pct,trading_days) or None; `baselines` = growth_baselines().
    `extend_to_projection` (index view): default to FULL history + the forward projection,
    with the y-axis fitted to include the projected trend lines."""
    df, opps, s = a["df"], a["opps"], a["summary"]
    skey, ticker = a["skey"], a["ticker"]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.78, 0.22])

    fig.add_trace(go.Candlestick(
        x=df["Date"], open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        name="Price", increasing_line_color=_GREEN, decreasing_line_color=_RED), 1, 1)

    # Overlays — HIDDEN by default; click a legend entry to toggle each on/off. For an INDEX
    # (macro present) the 50/200/300 DMAs ARE the macro indicators, so show them by default.
    for w, c in [(20, "#2962ff"), (50, "#ff9800"), (100, "#26c6da"),
                 (200, "#9c27b0"), (300, "#8d6e63")]:
        col = f"dma_{w}"
        if col in df:
            _vis = True if (macro and w in (50, 200, 300)) else "legendonly"
            fig.add_trace(go.Scatter(x=df["Date"], y=df[col], name=f"{w} DMA",
                          line=dict(width=1, color=c), opacity=0.7, visible=_vis), 1, 1)
    for col, nm, c in [("high_52w", "52w High", "#66bb6a"), ("low_52w", "52w Low", "#ef5350")]:
        if col in df:
            fig.add_trace(go.Scatter(x=df["Date"], y=df[col], name=nm,
                          line=dict(width=1.2, color=c, dash="dot"), opacity=0.85,
                          visible="legendonly"), 1, 1)

    # Growth baselines (log-space CAGR projected forward) — HIDDEN by default, toggle in legend
    if baselines:
        for key, nm, c in [("early", "trend: early phase", "#29b6f6"),
                           ("full", "trend: all data", "#ffb300")]:
            bl = baselines.get(key)
            if bl:
                fig.add_trace(go.Scatter(
                    x=[pd.to_datetime(x) for x in bl["x"]], y=bl["y"], mode="lines",
                    name=f"{nm} · {bl['cagr_pct']}%/yr",
                    line=dict(width=1.6, color=c, dash="dash"), opacity=0.9,
                    visible="legendonly", hoverinfo="skip"), 1, 1)

    # Index macro / safe-zone. CURRENT cycle drawn prominently (bottom ▲ → last-high ▼ = the cup,
    # + the safe-zone ceiling line); PAST cycles faded; 1-yr 300-DMA exhaustion (top-risk) zones
    # shaded faintly with a legend label so the red is explained.
    if macro:
        eps = macro.get("episodes") or []
        zones = macro.get("exhaustion_zones") or []
        for z in zones:                                    # faint top-risk bands
            fig.add_vrect(x0=z[0], x1=z[1], fillcolor="rgba(239,83,80,0.06)", line_width=0, row=1, col=1)
        if zones:                                          # legend proxy so the red is labeled
            fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                name="top-risk band (1yr no 300-DMA touch)",
                marker=dict(size=11, color="rgba(239,83,80,0.45)", symbol="square")), 1, 1)
        # PAST cycles — faint
        past = eps[:-1]
        if past:
            fig.add_trace(go.Scatter(
                x=[pd.to_datetime(e["bottom_date"]) for e in past], y=[e["bottom_most"] for e in past],
                mode="markers", name="past bottoms", opacity=0.4,
                marker=dict(symbol="triangle-up", size=8, color="#00c853"), hoverinfo="skip"), 1, 1)
            seg_x, seg_y = [], []
            for e in past:
                if e.get("ceiling"):
                    seg_x += [pd.to_datetime(e["high_date"]), pd.to_datetime(e["end_date"]), None]
                    seg_y += [e["ceiling"], e["ceiling"], None]
            if seg_x:
                fig.add_trace(go.Scatter(x=seg_x, y=seg_y, mode="lines", name="past safe ceilings",
                    line=dict(color="#ff7043", width=1, dash="dot"), opacity=0.35, hoverinfo="skip"), 1, 1)
        # CURRENT cycle — the cup: bottoming-cross low ▲ → last high ▼ (prominent, labeled)
        le = eps[-1] if eps else None
        if le and le.get("bottom_most") is not None and le.get("last_high") is not None:
            bd, bm = pd.to_datetime(le["bottom_date"]), le["bottom_most"]
            hd, hh = pd.to_datetime(le["high_date"]), le["last_high"]
            _cd, _cp = df["Date"].iloc[-1], float(df["Close"].iloc[-1])   # the "handle" = high → now
            fig.add_trace(go.Scatter(x=[bd, hd, _cd], y=[bm, hh, _cp], mode="lines+markers",
                name="current cycle: bottom ▲ → last-high ▼ → now ●", line=dict(color="#26c6da", width=2.5),
                marker=dict(size=[15, 15, 11], color=["#00c853", "#ab47bc", "#26c6da"],
                            symbol=["triangle-up", "triangle-down", "circle"], line=dict(width=1.5, color="#fff")),
                text=[f"<b>bottoming-cross low</b><br>{bm:,.0f} · {le['bottom_date']}",
                      f"<b>last high (cup rim)</b><br>{hh:,.0f} · {le['high_date']}",
                      f"<b>now</b><br>{_cp:,.0f}"], hoverinfo="text"), 1, 1)
        sl = macro.get("safe_limit")
        if sl:                                             # safe-zone ceiling — bold, clearly labeled
            _ok = macro.get("safe_status") == "SAFE"
            _pct = macro.get("pct_to_safe")
            _room = (f" · {_pct:+.0f}% room — SAFE" if _ok else " · price ABOVE — UNSAFE")
            fig.add_hline(y=sl, line=dict(color="#ff7043", dash="dash", width=2.5),
                          annotation_text=f"🎯 safe-zone ceiling {sl:,.0f}{_room}",
                          annotation_position="top left",
                          annotation_font=dict(color="#ff7043", size=13), row=1, col=1)

    # Per-trade markers — entry ▲ + exit ▼ + connector, matching the backtest table
    if not opps.empty and "Entry_Date" in opps and "Entry_Price" in opps:
        res = opps["Result"] if "Result" in opps.columns else pd.Series("?", index=opps.index)

        def _ht(r, lbl):
            d = r.get("Days_to_Exit")
            dd = f" · {int(d)} d" if pd.notna(d) else ""
            p = r.get("Profit_%")
            pp = f" · {p:+.1f}%" if pd.notna(p) else ""
            xd = r.get("Exit_Date")
            xs = (f"<br>Exit {pd.to_datetime(xd).date()}{dd}{pp}" if pd.notna(xd) else "<br>not exited")
            tp = r.get("Target_Price")
            tps = f"{tp:.2f}" if pd.notna(tp) else "—"
            return (f"<b>{lbl}</b><br>Entry {r['Entry_Price']:.2f} "
                    f"({pd.to_datetime(r['Entry_Date']).date()})"
                    f"<br>Target {tps}{xs}")

        for grp, color, edge, name in [
                (opps[res == "Success"],                          _WIN,      "#003d00", "Win"),
                (opps[res == "Slow (off-pace)"],                  "#ff9800", "#7a4f00", "Slow (off-pace)"),
                (opps[~res.isin(["Success", "Slow (off-pace)"])], _LOSS,     "#3d0000", "Open / loss")]:
            if grp.empty:
                continue
            fig.add_trace(go.Scatter(
                x=grp["Entry_Date"], y=grp["Entry_Price"], mode="markers", name=f"{name} entry",
                marker=dict(symbol="triangle-up", size=11, color=color, line=dict(width=1, color=edge)),
                text=[_ht(r, name) for _, r in grp.iterrows()], hoverinfo="text"), 1, 1)
            ex = grp[grp["Exit_Date"].notna()] if "Exit_Date" in grp.columns else grp.iloc[0:0]
            if not ex.empty and len(ex) <= 250:
                fig.add_trace(go.Scatter(
                    x=ex["Exit_Date"], y=ex["Target_Price"], mode="markers", showlegend=False,
                    marker=dict(symbol="triangle-down", size=11, color=color, line=dict(width=1, color=edge)),
                    text=[f"<b>{name} exit</b><br>{r['Target_Price']:.2f} "
                          f"({pd.to_datetime(r['Exit_Date']).date()})" for _, r in ex.iterrows()],
                    hoverinfo="text"), 1, 1)
                cx, cy = [], []
                for _, r in ex.iterrows():
                    cx += [r["Entry_Date"], r["Exit_Date"], None]
                    cy += [r["Entry_Price"], r["Target_Price"], None]
                fig.add_trace(go.Scatter(x=cx, y=cy, mode="lines", showlegend=False,
                    line=dict(width=1, color=color, dash="dot"), opacity=0.55, hoverinfo="skip"), 1, 1)

    # V20: highlight the consecutive-green-candle run that formed the range
    rf, rt = s.get("range_from"), s.get("range_to")
    if rf and rt:
        ngc = s.get("range_green_candles")
        try:
            _rlab = pd.to_datetime(rf).strftime("%b %Y")
        except Exception:
            _rlab = str(rf)
        fig.add_vrect(x0=rf, x1=rt, fillcolor="rgba(0,200,83,0.18)", line_width=0,
                      annotation_text=(f"V20 range · {_rlab}" + (f" ({ngc} candles)" if ngc else "")),
                      annotation_position="top left", row=1, col=1)

    # RHS / Cup-&-Handle patterns (V10, RHS, CWH). A pattern is ACTIVE only if it is
    # currently forming (fresh breakout, target not yet hit) — drawn BRIGHT + solid,
    # and it is what drives the READY signal. PAST patterns (already played out) are
    # drawn FADED + dashed for REFERENCE only — never a buy recommendation.
    shapes = s.get("pattern_shapes") or []
    if shapes:
        # buckets: (type, active) -> outline xs/ys/txt ; plus neckline/rim levels
        buckets = {("RHS", True): [[], [], []], ("RHS", False): [[], [], []],
                   ("CWH", True): [[], [], []], ("CWH", False): [[], [], []]}
        lvl_a_x, lvl_a_y, lvl_p_x, lvl_p_y = [], [], [], []
        for p in shapes:
            act = bool(p.get("active"))
            xs = [pd.to_datetime(x) for x in p["x"]]
            tag_note = "" if act else " · PAST (reference)"
            label = (f"{p['type']}{tag_note} · buy {p['buy_y']:.2f} → target {p['target']:.2f}"
                     f" (broke {p['level']:.2f})")
            tags = p.get("tags") or [""] * len(xs)
            bx, by, bt = buckets[(p["type"], act)]
            bx += xs + [None]; by += list(p["y"]) + [None]
            bt += [f"<b>{tg}</b><br>{label}" for tg in tags] + [""]
            if act:
                lvl_a_x += [pd.to_datetime(p["lvl_x0"]), pd.to_datetime(p["lvl_x1"]), None]
                lvl_a_y += [p["level"], p["level"], None]
            else:
                lvl_p_x += [pd.to_datetime(p["lvl_x0"]), pd.to_datetime(p["lvl_x1"]), None]
                lvl_p_y += [p["level"], p["level"], None]
        # past levels first (under), then past outlines, then active on top
        if lvl_p_x:
            fig.add_trace(go.Scatter(x=lvl_p_x, y=lvl_p_y, mode="lines", name="neckline/rim (past)",
                line=dict(color="#607d8b", width=1, dash="dot"), opacity=0.4, hoverinfo="skip"), 1, 1)
        if lvl_a_x:
            fig.add_trace(go.Scatter(x=lvl_a_x, y=lvl_a_y, mode="lines", name="neckline / rim",
                line=dict(color="#455a64", width=1.5, dash="dot"), hoverinfo="skip"), 1, 1)
        style = {  # (color, active_label, past_label)
            "RHS": ("#ab47bc", "RHS pattern (forming)", "RHS pattern (past)"),
            "CWH": ("#00897b", "Cup & Handle (forming)", "Cup & Handle (past)")}
        for typ in ("RHS", "CWH"):
            color, nm_a, nm_p = style[typ]
            px, py, pt = buckets[(typ, False)]
            if px:                                         # past — faded, dashed, small markers
                fig.add_trace(go.Scatter(x=px, y=py, mode="lines+markers", name=nm_p,
                    line=dict(color=color, width=1.2, dash="dash"), opacity=0.4,
                    marker=dict(size=6, color=color), text=pt, hoverinfo="text"), 1, 1)
            ax, ay, at = buckets[(typ, True)]
            if ax:                                         # active — bright, solid, big markers
                fig.add_trace(go.Scatter(x=ax, y=ay, mode="lines+markers", name=nm_a,
                    line=dict(color=color, width=3),
                    marker=dict(size=11, color=color, line=dict(width=1.5, color="#fff")),
                    text=at, hoverinfo="text"), 1, 1)

    # Potential entry / target lines — ALWAYS shown when the strategy has levels.
    # Mirror kpi_block: SMA/Knoxville have no fixed target, so estimate it from the
    # historical median win so the chart agrees with the KPI row.
    e, t = _num(s.get("Entry_Price")), _num(s.get("Target_Price"))
    live = s.get("Ready_to_Invest") in ("YES", "REVIEW")
    t_est = False
    if t is None and e and live:
        mw = _num(s.get("Median_Win_Profit_%")) or _num(s.get("Avg_Win_Profit_%"))
        if mw and mw > 0:
            t, t_est = round(e * (1 + mw / 100), 2), True
    # Entry label BELOW its line, Target label ABOVE its line → they separate vertically and never
    # overlap even when the two levels sit close together (e.g. a V20 range like 117 → 155).
    if e:
        fig.add_hline(y=e, line=dict(color="#2962ff", dash="dot", width=2 if live else 1),
                      annotation_text=f"Entry {e:.2f}" + ("" if live else " (watch)"),
                      annotation_position="bottom left", row=1, col=1)
    if t:
        fig.add_hline(y=t, line=dict(color=_WIN, dash="dash", width=2 if live else 1),
                      annotation_text=f"Target {'~' if t_est else ''}{t:.2f}" + (" (est.)" if t_est else ""),
                      annotation_position="top left", row=1, col=1)

    # Volume
    if "Volume" in df:
        vcol = np.where(df["Close"] >= df["Open"], "rgba(38,166,154,0.5)", "rgba(239,83,80,0.5)")
        fig.add_trace(go.Bar(x=df["Date"], y=df["Volume"], marker_color=vcol,
                             name="Volume", showlegend=False), 2, 1)

    # Measure tool: dashed segment + %/duration annotation between two chosen points
    if measure:
        (mx0, mp0), (mx1, mp1), mpct, mdays = measure
        fig.add_trace(go.Scatter(x=[mx0, mx1], y=[mp0, mp1], mode="lines+markers",
            line=dict(color="#e91e63", width=2, dash="dash"),
            marker=dict(size=8, color="#e91e63"), name="measure", hoverinfo="skip"), 1, 1)
        fig.add_annotation(x=mx1, y=mp1, text=f"<b>{mpct:+.1f}%</b> · {mdays} td",
            showarrow=True, arrowhead=2, ax=0, ay=-30,
            bgcolor="#e91e63", font=dict(color="#fff", size=12), row=1, col=1)

    # default view: last ~12 months (extended to the V20 green run). Pan + scroll to
    # zoom; range buttons for quick spans; DOUBLE-CLICK to auto-fit the full history.
    win = min(252, max(2, len(df) - 1))
    x0, x1 = df["Date"].iloc[-win], df["Date"].iloc[-1]
    if rf:                                                   # V20: include the green run
        x0 = min(x0, pd.to_datetime(rf))
    if shapes:                                               # V10/RHS/CWH: include every pattern
        try:
            x0 = min([x0] + [pd.to_datetime(p["x"][0]) for p in shapes if p.get("x")])
        except Exception:
            pass
    inwin = df["Date"] >= x0
    y_lo = float(df.loc[inwin, "Low"].min())
    y_hi = float(df.loc[inwin, "High"].max())
    for lv in (e, t):                                        # keep the always-on lines in view
        if lv:
            y_lo, y_hi = min(y_lo, lv), max(y_hi, lv)
    # NOTE (default/stock view): baselines are NOT folded into the y-range — the early-slope
    # line projects to very high values and would squash the candles. Toggle it on and
    # double-click to fit it.
    if extend_to_projection and baselines:                   # index view: full history + projection
        x0 = df["Date"].iloc[0]
        try:
            _ends = [pd.to_datetime(bl["x"][-1])
                     for bl in (baselines.get("full"), baselines.get("early")) if bl]
            if _ends:
                x1 = max([x1] + _ends)                        # extend out to the ~2yr projection
        except Exception:
            pass
        _inw = df["Date"] >= x0                               # recompute over full history
        y_lo = float(df.loc[_inw, "Low"].min())
        y_hi = float(df.loc[_inw, "High"].max())
        for bl in (baselines.get("full"), baselines.get("early")):   # fit the projected trend lines in
            if bl:
                for xv, yv in zip(bl["x"], bl["y"]):
                    if x0 <= pd.to_datetime(xv) <= x1:
                        y_lo, y_hi = min(y_lo, yv), max(y_hi, yv)
    if macro and macro.get("safe_limit"):                    # ALWAYS show the safe-zone target.
        # The gap between price and the ceiling is the "room to grow". For a normal/shallow
        # recovery the ceiling sits just above price (no squash); only a DEEP (>~2.5x) single-cycle
        # recovery pushes it far up and compresses recent candles — use the range buttons to zoom.
        y_hi = max(y_hi, macro["safe_limit"])
    pad = (y_hi - y_lo) * 0.06 or 1.0

    fig.update_layout(
        template="plotly_white", height=780, hovermode="x unified", dragmode="pan",
        # title + range buttons own the TOP; the legend is a FOOTER below the chart so
        # it never collides with the 1m/3m/… buttons and has room to wrap with spacing.
        title=dict(text=(f"{ticker} — {STRATEGY_LABELS[skey]}" if skey in STRATEGY_LABELS else ticker),
                   x=0.004, xref="paper", xanchor="left",
                   y=0.985, yref="container", yanchor="top", font=dict(size=14)),
        # legend pushed well BELOW the range-slider (which now sits at the bottom) so the
        # two don't overlap; generous bottom margin gives the wrapped legend room.
        legend=dict(orientation="h", x=0, xanchor="left", y=-0.34, yanchor="top",
                    font=dict(size=10), tracegroupgap=6),
        margin=dict(l=10, r=10, t=66, b=215), bargap=0)
    fig.update_xaxes(
        type="date", rangeslider_visible=False, row=1, col=1, range=[x0, x1],
        rangeselector=dict(x=0, xanchor="left", y=1.0, yanchor="bottom", font=dict(size=11),
                           buttons=[
            dict(count=1, label="1m", step="month", stepmode="backward"),
            dict(count=3, label="3m", step="month", stepmode="backward"),
            dict(count=6, label="6m", step="month", stepmode="backward"),
            dict(count=1, label="1y", step="year", stepmode="backward"),
            dict(count=3, label="3y", step="year", stepmode="backward"),
            dict(count=5, label="5y", step="year", stepmode="backward"),
            dict(step="all", label="All")]))
    # TradingView-style draggable range-slider (mini-map) under the chart for horizontal
    # zoom + slide; drag its handles to set the window, drag the middle to pan.
    fig.update_xaxes(type="date", row=2, col=1, range=[x0, x1],
                     rangeslider=dict(visible=True, thickness=0.045, bgcolor="rgba(255,255,255,0.04)"))
    fig.update_yaxes(title_text="Price", row=1, col=1, range=[y_lo - pad, y_hi + pad])
    fig.update_yaxes(title_text="Vol", row=2, col=1)
    return fig


def build_fundamentals_chart(fund):
    """Bars of revenue & net-profit PER QUARTER (left) and PER YEAR (right) so you can
    SEE how the metrics changed over time. Tallest bar = gold (period-best); latest
    bar gets a green outline + ★. Values in ₹ crore. Returns (fig, info) or (None, None)."""
    if not fund:
        return None, None
    q_rev = fund.get("quarterly_revenue_hist") or []
    q_ni = fund.get("quarterly_netprofit_hist") or []
    a_rev = fund.get("annual_revenue_hist") or []
    a_ni = fund.get("annual_netprofit_hist") or []
    if not any([q_rev, q_ni, a_rev, a_ni]):
        return None, None

    has_annual = bool(a_rev or a_ni)
    ncols = 2 if has_annual else 1
    titles = (["Revenue — by Quarter (₹ Cr)"]
              + (["Revenue — by Year (₹ Cr)"] if has_annual else [])
              + ["Net Profit — by Quarter (₹ Cr)"]
              + (["Net Profit — by Year (₹ Cr)"] if has_annual else []))
    fig = make_subplots(rows=2, cols=ncols, shared_xaxes=False,
                        vertical_spacing=0.18, horizontal_spacing=0.08,
                        subplot_titles=titles)

    # Indian fiscal year = Apr->Mar, so quarter-ends are Jun(Q1)/Sep(Q2)/Dec(Q3)/Mar(Q4)
    QMAP = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}

    def _qlabel(iso):                                  # "2026-03-31" -> "Mar '26 Q4"
        dt = pd.Timestamp(iso)
        tag = QMAP.get(dt.month)
        base = dt.strftime("%b '%y")
        return f"{base} {tag}" if tag else base

    def _ylabel(iso):                                  # "2026-03-31" -> "FY26" (else cal. year)
        dt = pd.Timestamp(iso)
        return f"FY{dt.year % 100:02d}" if dt.month == 3 else str(dt.year)

    def _add(series, row, col, base_color, period):
        if not series:
            return
        labels = [(_qlabel(d) if period == "Q" else _ylabel(d)) for d, _ in series]
        vals = [v / 1e7 for _, v in series]            # rupees -> ₹ crore
        mx = max(vals)
        colors, lines = [], []
        for k, v in enumerate(vals):
            is_max = v >= mx - 1e-6
            is_last = (k == len(vals) - 1)
            colors.append("#ffc107" if is_max else (base_color if v >= 0 else "#ef5350"))
            lines.append("#00c853" if is_last else "rgba(0,0,0,0)")
        text = [("★ " if k == len(vals) - 1 else "") + f"{v:,.0f}" for k, v in enumerate(vals)]
        fig.add_trace(go.Bar(x=labels, y=vals, marker_color=colors,
                             marker_line=dict(color=lines, width=2),
                             text=text, textposition="outside", cliponaxis=False,
                             textfont=dict(size=13, color="#f5f5f5"), showlegend=False,
                             hovertemplate="%{x}<br>₹%{y:,.0f} Cr<extra></extra>"), row, col)

    _add(q_rev, 1, 1, "#42a5f5", "Q")
    _add(q_ni, 2, 1, "#26a69a", "Q")
    if has_annual:
        _add(a_rev, 1, 2, "#42a5f5", "Y")
        _add(a_ni, 2, 2, "#26a69a", "Y")
    # dark-theme styling + headroom so the value labels are never clipped/invisible
    fig.update_layout(height=480, bargap=0.28, margin=dict(l=10, r=10, t=46, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#e8e8e8"),
                      uniformtext_minsize=10, uniformtext_mode="show")
    fig.update_xaxes(showgrid=False, color="#e8e8e8")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.12)", color="#e8e8e8",
                     rangemode="tozero", automargin=True)
    fig.update_annotations(font_color="#e8e8e8")     # subplot titles
    info = {
        "rev_highest": fund.get("ttm_revenue_is_highest"),
        "np_highest": fund.get("ttm_netprofit_is_highest"),
        "track_record": fund.get("good_track_record"),
        "quarter_improved": fund.get("latest_quarter_improved"),
        "quarters": max(len(q_rev), len(q_ni)),
        "years": max(len(a_rev), len(a_ni)),
    }
    return fig, info
