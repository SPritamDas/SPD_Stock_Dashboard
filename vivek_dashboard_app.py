"""
vivek_dashboard_app.py
======================
Interactive dashboard for the 9 "Trading with Vivek" strategies.

    Strategy selector + Ticker selector  ->  TradingView-style chart
    (historical opportunities marked win/fail) + live KPIs + trade log.

RUN (local):
    cd "SPD_Stock_Dashboard"
    pip install -r requirements.txt          # streamlit>=1.42 (native Google login) + the rest
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in (see README)
    streamlit run vivek_dashboard_app.py

First time: click "Build / refresh data cache" in the sidebar (fetches all
tickers once, ~a few minutes) -> after that every selection is instant.
Without a cache it still works: click any ticker to fetch it live on demand.

Note: the green/red ticker boxes use a CSS hook that needs Streamlit >= 1.39
(older versions still show the 🟢/🔴/🟡 dot in each label as a fallback).
"""

import hashlib
import json
import os
import pickle
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import vivek_strategies as vs
import vivek_dashboard_core as core

# ============================================================================
# CONFIG  (credentials loaded from st.secrets / env — NO keys are stored in this repo)
# ============================================================================
def _load_service_account():
    """Service-account creds for reading the Google Sheets, in priority order:
       1. Streamlit secrets  [gcp_service_account]  (Streamlit Cloud + local secrets.toml)
       2. env var GCP_SERVICE_ACCOUNT  (full JSON; used by the GitHub Actions refresh)
    No private key is ever stored in this repo."""
    info = None
    try:
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
    except Exception:
        info = None
    if info is None:
        _raw = os.environ.get("GCP_SERVICE_ACCOUNT")
        if _raw:
            info = json.loads(_raw)
    if not info:
        raise RuntimeError(
            "No Google service-account credentials found. Add a [gcp_service_account] block to "
            ".streamlit/secrets.toml (see secrets.toml.example), or set the GCP_SERVICE_ACCOUNT env var.")
    _missing = [k for k in ("private_key", "client_email", "token_uri") if not info.get(k)]
    if _missing:
        raise RuntimeError("Service-account JSON looks incomplete (missing: "
                           + ", ".join(_missing) + "). Re-paste the full key JSON into [gcp_service_account].")
    return info

SERVICE_ACCOUNT_INFO = _load_service_account()
SHEET_KEY      = "1qzj_Va1Xle6Pnz7HDUsO1iPaUeGEv_VLJFXE4-zZYNw"
WORKSHEET_NAME = "stock_classifications"
GROUP_COLUMNS  = ["v_40", "v_40_next", "v_200"]
# FII/DII superstar-investor workbook (written by fii_dii_investment_pattern.ipynb — same service account)
FII_SHEET_KEY  = "1rIFmhm37XEJsfXV2Nn1QPfakLG9xvMmEsjJ7YYule8g"
FII_SUMMARY_TAB = "fii_dii_indian_investment_summary"
YEARS          = 20
OUTPUT_DIR     = "vivek_output"
CACHE_PKL      = os.path.join(OUTPUT_DIR, "dashboard_cache.pkl")
os.makedirs(OUTPUT_DIR, exist_ok=True)

st.set_page_config(page_title="SPritamDas Strategy Dashboard", layout="wide",
                   initial_sidebar_state="expanded")

# ---- access control: Google sign-in + allow-list + session timeout ----
import auth
auth.require_login()

# ============================================================================
# DATA LOADERS
# ============================================================================
@st.cache_resource(show_spinner=False)
def _gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=scopes)
    return gspread.authorize(creds)


def _try_download_cache_from_release():
    """On Streamlit Cloud the cache pkl is not in the repo (git-ignored); fetch the
    nightly-built copy from the GitHub Release asset. Configured via [github] secrets.
    Silent no-op locally / when unconfigured (the app then builds the cache on demand)."""
    try:
        gh = dict(st.secrets.get("github", {}))
    except Exception:
        gh = {}
    repo, token = gh.get("repo"), gh.get("token")
    if not (repo and token):
        return
    tag   = gh.get("release_tag", "data-latest")
    asset = gh.get("cache_asset", "dashboard_cache.pkl")
    try:
        import requests
        h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=h, timeout=30)
        if r.status_code != 200:
            return
        a = next((x for x in r.json().get("assets", []) if x.get("name") == asset), None)
        if not a:
            return
        dl = requests.get(a["url"], headers={"Authorization": f"Bearer {token}",
                                             "Accept": "application/octet-stream"}, timeout=180)
        if dl.status_code == 200 and dl.content:
            with open(CACHE_PKL, "wb") as f:
                f.write(dl.content)
    except Exception:
        return


def load_cache():
    if not os.path.exists(CACHE_PKL):
        _try_download_cache_from_release()      # cloud: pull the nightly-built cache if absent
    if os.path.exists(CACHE_PKL):
        try:
            with open(CACHE_PKL, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    return None


@st.cache_data(show_spinner="Reading group lists from Google Sheet…")
def read_groups_from_sheet():
    from gspread_dataframe import get_as_dataframe
    ws = _gspread_client().open_by_key(SHEET_KEY).worksheet(WORKSHEET_NAME)
    df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
    groups = {}
    for col in GROUP_COLUMNS:
        if col in df.columns:
            groups[col] = sorted({str(x).strip().upper() for x in df[col].dropna()
                                  if str(x).strip() and str(x).strip().lower() != "nan"})
    return groups


def get_groups(cache):
    if cache and cache.get("groups"):
        return cache["groups"]
    return read_groups_from_sheet()


def _fetch_one_raw(ticker, years=YEARS):
    """Plain fetch — safe to call from worker threads (no Streamlit cache). Tries NSE (.NS)
    first, then falls back to BSE (.BO) for names that are BSE-only or transiently missing on NSE."""
    import yfinance as yf
    today = datetime.now().date()
    end = today + timedelta(days=1)              # yfinance `end` is EXCLUSIVE -> +1 to include today's candle
    start = today - timedelta(days=years * 365)
    tk = df = None
    for suffix in (".NS", ".BO"):                # NSE first, then BSE (BSE-only / NSE-missing names)
        _tk = yf.Ticker(f"{ticker}{suffix}")
        try:
            _df = _tk.history(start=start, end=end, interval="1d")
        except Exception:
            _df = None
        if _df is not None and not _df.empty:
            tk, df = _tk, _df
            break
    if df is None or df.empty:
        return None
    df = df.reset_index()
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    # yfinance's DAILY history lags the live quote by ~1 day: it returns the most recent trading day
    # as an ALL-NaN bar even though fast_info already has that day's price. The dropna below would
    # otherwise leave the dashboard a day stale — so backfill that trailing NaN bar from the live quote.
    if cols and len(df) and df[cols].iloc[-1].isna().all():
        _dt = df.iloc[-1].get("Date")
        _d = _dt.date() if hasattr(_dt, "date") else None
        if _d is None or _d <= today:            # never fabricate a future-dated bar
            try:
                _fi = tk.fast_info
                _lp = float(_fi.last_price)
                _o = float(getattr(_fi, "open", None) or _lp)
                _hi = float(getattr(_fi, "day_high", None) or _lp)
                _lo = float(getattr(_fi, "day_low", None) or _lp)
            except Exception:
                _lp = float("nan")
            if _lp == _lp and _lp > 0:           # got a real live price -> fill the trailing bar
                fill = {"Open": _o, "High": max(_hi, _lp), "Low": min(_lo, _lp), "Close": _lp}
                for c in cols:
                    df.loc[df.index[-1], c] = fill.get(c, _lp)
                if "Volume" in df.columns and pd.isna(df.loc[df.index[-1], "Volume"]):
                    df.loc[df.index[-1], "Volume"] = 0
    if cols:                                     # drop any remaining NaN rows (older gaps / holidays)
        df = df.dropna(subset=cols)
    return df.reset_index(drop=True) if not df.empty else None


@st.cache_data(show_spinner="Fetching price history…")
def fetch_one(ticker, years=YEARS):
    return _fetch_one_raw(ticker, years)


@st.cache_data(show_spinner=False, ttl=3600)
def _cur_price(ticker):
    """Latest close for a bulk/block ticker (tries NSE .NS then BSE .BO). Returns None for
    BSE-numeric scrip codes and any ticker yfinance can't resolve — used for '% since buy'."""
    t = str(ticker).strip().upper()
    if not t or t.isdigit():                     # numeric = BSE scrip code, not yfinance-able
        return None
    df = _fetch_one_raw(t, years=1)
    if df is None or df.empty or "Close" not in df.columns:
        return None
    try:
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def fetch_fund(ticker):
    return vs.fetch_fundamentals(ticker)


@st.cache_data(show_spinner="Loading superstar investors…", ttl=21600)
def fetch_superstar_summary():
    """Read the pre-computed FII/DII Indian investor summary (list + portfolio metrics +
    Trendlyne links) written by the fii_dii notebook. Returns an empty df if unavailable."""
    try:
        from gspread_dataframe import get_as_dataframe
        ws = _gspread_client().open_by_key(FII_SHEET_KEY).worksheet(FII_SUMMARY_TAB)
        df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
        df = df.dropna(axis=1, how="all")
        if "name" in df.columns:
            df = df[df["name"].astype(str).str.strip() != ""].reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def _read_fii_tab(tab):
    """Read any tab from the FII/DII sheet → clean df (empty df if missing/unreadable)."""
    try:
        from gspread_dataframe import get_as_dataframe
        ws = _gspread_client().open_by_key(FII_SHEET_KEY).worksheet(tab)
        df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all").dropna(axis=1, how="all")
        if "name" in df.columns:
            df = df[df["name"].astype(str).str.strip() != ""].reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner="Loading alert history…", ttl=21600)
def fetch_superstar_history():
    """Monthly snapshots of investor metrics (run_date + signal/sharpe/maxdd/…), appended by the
    notebook on the 1st of each month → powers the 'who entered/left the alert list' timeline."""
    return _read_fii_tab("fii_dii_indian_investment_history")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_master_stock():
    """The daily superstar universe (master_stock tab) — used here only for the freshness alarm."""
    return _read_fii_tab("master_stock")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_superstar_moves():
    """Per-investor moves this quarter (investor · ticker · move NEW/ADD/TRIM/EXIT · stake · value),
    written by the notebook's master_stock build → powers the Alerts 'new buys' feed."""
    return _read_fii_tab("superstar_moves")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_quarter_updates():
    """Per-investor quarter advances (detected_on · name · prev_quarter · new_quarter) — the notebook
    logs an investor here whenever their disclosed quarter (data_to) advances run-over-run."""
    return _read_fii_tab("quarter_updates")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_superstar_sast():
    """SEBI SAST Reg 29 disclosures matched to superstars (investor · stock · Acquisition/Sale ·
    Δ stake % · resulting stake % · Reg29(1)/(2)), written by the notebook's SAST build → the
    near-real-time (T+2) 'moves' feed that catches accumulation the quarterly holdings miss."""
    return _read_fii_tab("superstar_sast_deals")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_superstar_bulkblock():
    """NSE bulk & block deals matched to superstars (investor · stock · buy/sell · qty · price ·
    bulk/block), written by the notebook → same-day large-trade feed that complements SAST (a
    different trigger, so it catches individual investors SAST's threshold-crossings miss)."""
    return _read_fii_tab("superstar_bulkblock_deals")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_superstar_insider():
    """Insider (PIT) + SAST disclosures matched to superstars (report date · stock · person ·
    Promoter/Other · Acquisition/Disposal · holding-after % · regulation), from the Trendlyne
    per-investor insider page → the promoter/insider signal (complements bulk/block + SAST)."""
    return _read_fii_tab("superstar_insider_deals")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_market_bulkblock():
    """Market-wide (all-market) recent NSE + BSE bulk & block deals, written by the notebook →
    the '📅 Today's deals' browser (not filtered to superstars)."""
    return _read_fii_tab("market_bulkblock_deals")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_fii_dii_flow():
    """Daily net FII/FPI & DII cash-market flow (accumulated by the notebook) → Markets → FII/DII."""
    return _read_fii_tab("fii_dii_flow")


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_ipo_dashboard():
    """IPO list — investorgain GMP + NSE enrich (notebook build_ipo_feed) → Markets → IPOs."""
    return _read_fii_tab("ipo_dashboard")


# raw column name -> the SAME friendly label shown in each table's header, so the filter dropdowns
# read like the columns the user actually sees (not snake_case internals like 'score_vs_benchmark').
_FILTER_LABELS = {
    "_d": "Date", "date": "Date", "conf": "Match", "source": "Via", "detail": "Details",
    "investor": "Investor", "name": "Investor", "signal": "Signal", "type": "Type",
    "side": "Side", "action": "Side", "move": "Move", "stock": "Stock", "company": "Company",
    "ticker": "Ticker", "symbol": "Symbol", "client": "Client", "entity": "Via (account/entity)",
    "exchange": "Exchange", "deal_type": "Deal type", "qty": "Qty", "price": "Price ₹",
    "pct_traded": "% of company", "pct_after": "Holding after %", "holding_after": "Holding after %",
    "traded_pct": "% traded", "reg_type": "Regulation", "regulation": "Regulation", "person": "Person",
    "category": "Category", "confidence_score": "Conf", "sharpe_ratio": "Sharpe",
    "alpha_ann_pct": "Alpha %", "ann_return_pct": "Ann ret %", "max_drawdown_pct": "Max DD %",
    "score_vs_benchmark": "Rank score", "quarters_tracked": "Quarters", "current_net_worth_cr": "Net worth ₹Cr",
    "prev_quarter": "Was", "new_quarter": "Now", "investors": "# investors", "who": "Who",
    "latest": "Latest buy", "avg_stake": "Avg stake %", "value_cr": "Value ₹Cr", "total": "Total",
    "trade_dates": "Trade dates", "filed": "Filed", "report_date": "Reported",
}


def _flabel(c):
    """Friendly display name for a raw column (falls back to the column name itself)."""
    return _FILTER_LABELS.get(str(c), str(c))


def filter_ui(df, key, *, label="🔎 Filter columns"):
    """Reusable, collapsed-by-default per-column filter for ANY table. Renders an expander;
    returns the filtered DataFrame (call st.dataframe on the result). Adds column filtering everywhere
    WITHOUT cluttering the default view — the widgets live inside the collapsed expander. Column names
    are shown with their friendly labels (see _FILTER_LABELS) so they match the table headers.
      · numeric column (mostly numbers, many distinct)  -> min/max range slider
      · categorical (≤ 40 distinct values)              -> multiselect
      · anything else (free text / high-cardinality)    -> 'contains' text box
    Each table must pass a UNIQUE `key` (seeds the widget keys)."""
    if df is None or getattr(df, "empty", True) or len(df.columns) == 0:
        return df
    # inline expander (not st.popover): renders full-width so the value dropdowns have room and never
    # clip the way a narrow floating popover does; still collapsed by default = no clutter.
    with st.expander(label, expanded=False):
        pick = st.multiselect("Pick column(s) to filter", list(df.columns), key=f"{key}__cols",
                              format_func=_flabel,
                              help="Choose one or more columns, then set the value/range to filter on.")
        out = df
        for c in pick:
            s = df[c]
            if isinstance(s, pd.DataFrame):        # guard against duplicate column labels
                s = s.iloc[:, 0]
            wkey, clabel = f"{key}__{c}", _flabel(c)
            snum = pd.to_numeric(s, errors="coerce")
            nunq = int(s.nunique(dropna=True))
            if snum.notna().mean() >= 0.8 and nunq >= 5:                       # numeric (%, price, days…) → range slider
                lo, hi = float(snum.min()), float(snum.max())
                if lo < hi:
                    a, b = st.slider(clabel, lo, hi, (lo, hi), key=wkey)
                    out = out[pd.to_numeric(out[c], errors="coerce").between(a, b)]
            elif 0 < nunq <= 40:                                               # categorical / few-valued → multiselect
                opts = sorted(map(str, s.dropna().unique()))
                sel = st.multiselect(clabel, opts, key=wkey)
                if sel:
                    out = out[out[c].astype(str).isin(sel)]
            else:                                                              # text → contains
                q = st.text_input(f"{clabel} contains", key=wkey).strip()
                if q:
                    out = out[out[c].astype(str).str.contains(q, case=False, na=False, regex=False)]
    if len(out) != len(df):
        st.caption(f"🔎 Column filters active — showing **{len(out)}** of {len(df)} rows. "
                   "(Clear them in the Filter columns popover.)")
    return out


def _quality_meta(summ):
    """{investor_lower: {signal, alpha, sharpe, maxdd}} for QUALITY superstars ONLY — the exact bar
    superstar_stock_scores() uses: signal BUY/STRONG BUY · Sharpe >= 0.4 · Max DD >= -40."""
    if summ is None or summ.empty or "name" not in summ.columns:
        return {}
    sh = pd.to_numeric(summ.get("sharpe_ratio"), errors="coerce")
    dd = pd.to_numeric(summ.get("max_drawdown_pct"), errors="coerce")
    al = pd.to_numeric(summ.get("alpha_ann_pct"), errors="coerce")
    sig = summ.get("signal", pd.Series(index=summ.index, dtype=object)).astype(str)
    qual = sig.isin(["STRONG BUY", "BUY"]) & (sh >= 0.4) & (dd >= -40)
    out = {}
    has_type = "type" in summ.columns
    for i in summ.index[qual.fillna(False)]:
        out[str(summ.at[i, "name"]).strip().lower()] = {
            "signal": sig.at[i], "alpha": al.at[i], "sharpe": sh.at[i], "maxdd": dd.at[i],
            "type": str(summ.at[i, "type"]) if has_type else ""}
    return out


@st.cache_data(show_spinner="Merging quality-superstar disclosures…", ttl=21600)
def build_quality_moves(days=None):
    """ONE chronological feed of every SAST + bulk + block + insider disclosure made by a QUALITY
    superstar, across NSE **and** BSE — so the user never has to open each portfolio. Filters the
    three per-investor feeds to the qualifying set and normalizes them to a common schema.
    Returns (df, meta). df columns: date/_dt · investor · signal · source(SAST/Bulk/Block/Insider) ·
    side(BUY/SELL) · stock · ticker · exchange · detail · conf. Newest first."""
    summ = fetch_superstar_summary()
    meta = _quality_meta(summ)
    if not meta:
        return pd.DataFrame(), {}
    qnames = set(meta)

    def _num(x):
        return pd.to_numeric(x, errors="coerce")

    def _qty(v):
        n = pd.to_numeric(v, errors="coerce")
        return f"{int(n):,}" if pd.notna(n) else str(v)

    def _mine(df):
        if df is None or df.empty or "investor" not in df.columns:
            return pd.DataFrame()
        return df[df["investor"].astype(str).str.strip().str.lower().isin(qnames)].copy()

    rows = []
    # --- bulk & block (already spans NSE + BSE via the exchange column) ---
    b = _mine(fetch_superstar_bulkblock())
    for _, r in (b.iterrows() if not b.empty else []):
        p = _num(r.get("pct_traded"))
        det = f"{_qty(r.get('qty'))} sh @ ₹{r.get('price')}" + (f" · {p:.2f}% of co" if pd.notna(p) else "")
        rows.append({"date": r.get("date"), "investor": r.get("investor"),
                     "source": (str(r.get("deal_type", "")).strip().title() or "Bulk"),
                     "side": str(r.get("action", "")).upper().strip(),
                     "stock": r.get("company"), "ticker": r.get("ticker"),
                     "exchange": str(r.get("exchange", "")).upper().strip(),
                     "detail": det, "conf": r.get("confidence")})
    # --- SAST Reg 29 (NSE) ---
    s = _mine(fetch_superstar_sast())
    for _, r in (s.iterrows() if not s.empty else []):
        act = str(r.get("action", "")).strip().lower()
        side = "BUY" if act.startswith("acq") else ("SELL" if act.startswith("sal") else act.upper())
        pt, pa = _num(r.get("pct_traded")), _num(r.get("pct_after"))
        det = f"Δ{pt:.2f}% → {pa:.2f}% held" if pd.notna(pt) and pd.notna(pa) else ""
        rt = str(r.get("reg_type", "")).strip()
        det = ((det + " " if det else "") + f"[{rt}]") if rt else (det or "—")
        rows.append({"date": r.get("filed"), "investor": r.get("investor"), "source": "SAST",
                     "side": side, "stock": r.get("company"), "ticker": r.get("symbol"),
                     "exchange": "NSE", "detail": det, "conf": r.get("confidence")})
    # --- insider / PIT (Trendlyne per-investor; can include SAST-family disclosures too) ---
    n = _mine(fetch_superstar_insider())
    for _, r in (n.iterrows() if not n.empty else []):
        act = str(r.get("action", "")).strip().lower()
        side = "BUY" if act.startswith("acq") else ("SELL" if act.startswith("dis") else act.upper())
        ha = _num(r.get("holding_after"))
        det = f"{_qty(r.get('qty'))} sh" + (f" → {ha:.2f}% held" if pd.notna(ha) else "")
        reg = str(r.get("regulation", "")).strip()
        det = (det + f" [{reg}]") if reg else det
        rows.append({"date": r.get("report_date"), "investor": r.get("investor"), "source": "Insider",
                     "side": side, "stock": r.get("company"), "ticker": r.get("ticker"),
                     "exchange": "", "detail": det, "conf": r.get("confidence")})

    if not rows:
        return pd.DataFrame(), meta
    d = pd.DataFrame(rows)
    # feeds mix formats (full month "08-July-2026", abbrev "6-Jul-2026", ISO "2026-07-07"); all are
    # unambiguous (month-name or year-first) so format="mixed" infers each row WITHOUT dayfirst (which
    # would mangle ISO). A bare to_datetime would lock the first row's format and NaT most of the rest.
    d["_dt"] = pd.to_datetime(d["date"].astype(str).str.strip(), format="mixed", errors="coerce")
    d["signal"] = d["investor"].map(lambda x: meta.get(str(x).strip().lower(), {}).get("signal", ""))
    d["alpha"] = d["investor"].map(lambda x: meta.get(str(x).strip().lower(), {}).get("alpha"))
    d["type"] = d["investor"].map(lambda x: meta.get(str(x).strip().lower(), {}).get("type", ""))
    d = d.drop_duplicates(subset=["investor", "ticker", "_dt", "side", "source", "detail"])
    if days:
        _cut = pd.Timestamp.now().normalize() - pd.Timedelta(days=int(days))
        d = d[d["_dt"].isna() | (d["_dt"] >= _cut)]
    d = d.sort_values("_dt", ascending=False, na_position="last").reset_index(drop=True)
    return d, meta


@st.cache_data(show_spinner=False, ttl=21600)
def superstar_stock_scores():
    """Per-stock conviction from ONLY the QUALITY superstars — signal BUY/STRONG BUY, Sharpe >= 0.4,
    Max DD >= -40 — each weighted by their alpha %. Consensus = alpha-weighted count of qualifying
    holders (full current-holder set from superstar_holdings); flow = those investors' recent
    quarterly moves + last-180d bulk/block. Non-qualifying superstars get zero weight."""
    summ = fetch_superstar_summary(); hold = _read_fii_tab("superstar_holdings")
    moves, bb, ms = fetch_superstar_moves(), fetch_superstar_bulkblock(), fetch_master_stock()
    if summ.empty or "name" not in summ.columns or hold.empty or "ticker" not in hold.columns:
        return pd.DataFrame()
    _sh = pd.to_numeric(summ.get("sharpe_ratio"), errors="coerce")
    _dd = pd.to_numeric(summ.get("max_drawdown_pct"), errors="coerce")
    _al = pd.to_numeric(summ.get("alpha_ann_pct"), errors="coerce")
    _qual = (summ.get("signal", pd.Series(index=summ.index, dtype=object)).isin(["STRONG BUY", "BUY"])
             & (_sh >= 0.4) & (_dd >= -40))
    summ = summ.copy(); summ["qw"] = _al.clip(lower=1, upper=60).where(_qual, 0).fillna(0)
    qw = dict(zip(summ["name"].astype(str).str.lower().str.strip(), summ["qw"]))
    _q = lambda n: qw.get(str(n).strip().lower(), 0.0)

    h = hold.copy(); h["ticker"] = h["ticker"].astype(str)
    if "move" in h.columns:
        h = h[~h["move"].astype(str).isin(["EXIT", "past", "nan", "None"])]
    h["qw"] = h["investor"].map(_q)
    hq = h[h["qw"] > 0]
    if hq.empty:
        return pd.DataFrame()
    s = hq.groupby("ticker").agg(
        n_holders=("investor", "nunique"), qcons=("qw", "sum"),
        held_by=("investor", lambda x: ", ".join(sorted(set(x))[:4]))).reset_index()

    _mvw = {"NEW": 3, "ADD": 2, "HOLD": 0, "TRIM": -2, "EXIT": -3}
    fm = pd.Series(dtype=float)
    if not moves.empty and "ticker" in moves.columns:
        moves = moves.copy(); moves["ticker"] = moves["ticker"].astype(str)
        moves["w"] = moves["move"].map(_mvw).fillna(0) * moves["investor"].map(_q)
        fm = moves.groupby("ticker")["w"].sum()
    fb = pd.Series(dtype=float); lastbuy = pd.DataFrame()
    if not bb.empty and "ticker" in bb.columns:
        bb = bb.copy(); bb["ticker"] = bb["ticker"].astype(str)
        bb["_dt"] = pd.to_datetime(bb["date"], format="mixed", dayfirst=True, errors="coerce")
        bb["priceN"] = pd.to_numeric(bb["price"], errors="coerce")
        bb["qwv"] = bb["investor"].map(_q)
        _b = bb[bb["_dt"] >= pd.Timestamp.now() - pd.Timedelta(days=180)].copy()
        _b["w"] = _b["action"].map({"BUY": 2, "SELL": -2}).fillna(0) * _b["qwv"]
        fb = _b.groupby("ticker")["w"].sum()
        _bu = bb[(bb["action"].astype(str).str.upper() == "BUY") & (bb["qwv"] > 0)].dropna(subset=["_dt", "priceN"]).sort_values("_dt")
        if _bu.empty:
            _bu = bb[bb["action"].astype(str).str.upper() == "BUY"].dropna(subset=["_dt", "priceN"]).sort_values("_dt")
        if not _bu.empty:
            lastbuy = _bu.groupby("ticker").tail(1).set_index("ticker")[["priceN", "investor"]]

    s["flow_raw"] = s["ticker"].map(fm).fillna(0) + s["ticker"].map(fb).fillna(0)
    s["consensus"] = (s["qcons"] / s["qcons"].max() * 40) if s["qcons"].max() > 0 else 0
    _f = s["flow_raw"]
    s["flow"] = ((_f - _f.min()) / (_f.max() - _f.min()) * 30) if _f.max() > _f.min() else 0
    if not ms.empty and "ticker" in ms.columns:
        ms = ms.copy(); ms["ticker"] = ms["ticker"].astype(str)
        s = s.merge(ms[[c for c in ["ticker", "company", "recent_action", "total_value_cr"] if c in ms.columns]],
                    on="ticker", how="left")
    if "company" not in s.columns:
        s["company"] = s["ticker"]
    s["company"] = s["company"].fillna(s["ticker"])
    if not lastbuy.empty:
        s["last_buy_price"] = s["ticker"].map(lastbuy["priceN"]); s["last_buy_by"] = s["ticker"].map(lastbuy["investor"])
    s["base_score"] = s["consensus"] + s["flow"]
    return s.sort_values("base_score", ascending=False).reset_index(drop=True)


@st.cache_data(show_spinner="Loading holdings journey…", ttl=21600)
def _fetch_holdings_tab():
    """Full per-investor HOLDINGS JOURNEY (investor · ticker · company · move · delta · value · qty +
    one %-stake column per quarter), written nightly by the notebook's master_stock build → lets the
    superstar detail page show the journey WITHOUT live-scraping Trendlyne (405-blocks Cloud servers)."""
    return _read_fii_tab("superstar_holdings")


def superstar_holdings_journey(investor_name):
    """Reshape ONE investor's rows from the superstar_holdings tab into the DataFrame the page renders
    (Stock · Ticker · Move · Δ stake · <quarter cols newest-first> · Holding Value · Qty Held).
    Returns (df, quarters, error). No network — reads the pre-scraped sheet."""
    df = _fetch_holdings_tab()
    if df.empty or "investor" not in df.columns:
        return pd.DataFrame(), [], ("holdings not built yet — run the notebook (its master_stock build "
                                    "now also writes a `superstar_holdings` tab).")
    sub = df[df["investor"].astype(str).str.strip().str.lower()
             == str(investor_name).strip().lower()].copy()
    if sub.empty:
        return pd.DataFrame(), [], "no holdings stored for this investor yet — re-run the notebook."

    _MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    def _is_q(c):
        p = str(c).split()
        return len(p) == 2 and p[0] in _MON and p[1].isdigit() and len(p[1]) == 4

    def _qk(c):
        try:
            return pd.Timestamp(str(c))
        except Exception:
            return pd.Timestamp("1900-01-01")

    def _col(name, default=""):
        return sub[name] if name in sub.columns else pd.Series([default] * len(sub), index=sub.index)

    qcols = [c for c in sub.columns if _is_q(c) and pd.to_numeric(sub[c], errors="coerce").notna().any()]
    qcols = sorted(qcols, key=_qk, reverse=True)
    out = pd.DataFrame({
        "Stock":   _col("company"),
        "Ticker":  _col("ticker"),
        "Move":    _col("move"),
        "Δ stake": pd.to_numeric(_col("delta", None), errors="coerce"),
    })
    for q in qcols:
        out[q] = pd.to_numeric(sub[q], errors="coerce")
    out["Holding Value"] = pd.to_numeric(_col("value_cr", None), errors="coerce")
    out["Qty Held"] = _col("qty")
    return out.reset_index(drop=True), qcols, ""


_TL_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
               "Accept-Language": "en-US,en;q=0.9"}


@st.cache_data(show_spinner="Fetching holdings journey from Trendlyne…", ttl=21600)
def fetch_superstar_holdings(url):
    """Scrape ONE investor's Trendlyne portfolio page → tidy holdings-journey DataFrame:
    one row per stock with the disclosed %-stake for each of the last ~9 quarters (newest
    first), the current Holding Value / Qty, and a computed Move (NEW/ADD/TRIM/EXIT/HOLD)
    + Δ vs the prior quarter. Returns (df, quarters, error)."""
    import re
    import requests
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return pd.DataFrame(), [], "BeautifulSoup not installed — run: pip install beautifulsoup4"
    try:
        r = requests.get(url, headers=_TL_HEADERS, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return pd.DataFrame(), [], f"couldn't fetch Trendlyne page ({e})"
    soup = BeautifulSoup(r.text, "html.parser")
    tb = soup.find("table")
    if tb is None:
        return pd.DataFrame(), [], "no holdings table on the page (login-gated or layout changed)"

    # quarter labels: read ONLY the table's own header row (not sub-table headers), collapse the
    # consecutive 'Mon YYYY' duplicate (latest quarter has Change% + Holding% twins), stop on any
    # repeat, and cap the count — so a stray/extra header block can't inflate the column width.
    _head_row = next((tr for tr in tb.find_all("tr") if tr.find("th")), None)
    _head_ths = _head_row.find_all("th") if _head_row is not None else tb.find_all("th")
    quarters = []
    for th in _head_ths:
        m = re.match(r"([A-Za-z]{3})\s+(\d{4})", th.get_text(" ", strip=True))
        if not m:
            continue
        lab = f"{m.group(1)} {m.group(2)}"
        if quarters and quarters[-1] == lab:
            continue
        if lab in quarters or len(quarters) >= 16:
            break
        quarters.append(lab)
    if not quarters:
        return pd.DataFrame(), [], "couldn't read quarter columns"

    def _pct(x):
        x = (x or "").strip()
        if x in ("", "-", "—", "–"):
            return None
        mm = re.search(r"-?\d+\.?\d*", x.replace(",", ""))
        return float(mm.group()) if mm else None

    from urllib.parse import unquote

    def _symbol(td):
        """NSE symbol from the stock cell's link: …/equity/share-holding/<id>/<SYMBOL>/…"""
        a = td.find("a", href=True)
        if not a:
            return ""
        mm = re.search(r"/equity/(?:share-holding|share|stock)[^/]*/\d+/([^/]+)/", a["href"])
        return unquote(mm.group(1)).strip().upper() if mm else ""

    body = tb.find("tbody") or tb
    rows = []
    for tr in body.find_all("tr"):
        if tr.find_parent("table") is not tb:          # skip nested expand/sub-tables
            continue
        tds = tr.find_all("td", recursive=False)
        cells = [td.get_text(" ", strip=True) for td in tds]
        if len(cells) < 5 + len(quarters) or not cells[1].strip():
            continue
        stock = cells[1].strip()
        ticker = _symbol(tds[1])                       # NSE symbol → opens in Stock Analysis
        # Verified Trendlyne layout: [expand, stock, value, qty, latest-Change%, then N Holding% cols
        # (newest-first), then optional history/details]. Only the latest quarter is twinned (its own
        # Change% at cells[4]); older quarters are single — so the N stake columns start at index 5.
        series = [_pct(v) for v in cells[5:5 + len(quarters)]]   # newest-first
        latest = series[0] if series else None
        prev = series[1] if len(series) > 1 else None
        if latest is not None and prev is None:
            move, delta = "NEW", latest
        elif latest is None and prev is not None:
            move, delta = "EXIT", -prev
        elif latest is not None and prev is not None:
            d = round(latest - prev, 2)
            move, delta = ("ADD" if d > 0 else "TRIM" if d < 0 else "HOLD"), d
        elif any(v is not None for v in series):
            move, delta = "past", None
        else:
            continue                                    # never disclosed -> skip
        rec = {"Stock": stock, "Ticker": ticker, "Move": move, "Δ stake": delta,
               "Holding Value": cells[2].strip(), "Qty Held": cells[3].strip()}
        for q, v in zip(quarters, series):
            rec[q] = v
        rows.append(rec)
    if not rows:
        return pd.DataFrame(), quarters, "holdings table found but no rows parsed"
    return pd.DataFrame(rows), quarters, ""


@st.cache_data(show_spinner="Fetching index history…", ttl=21600)   # auto-refresh every ~6h
def fetch_index(ticker):
    """Fetch a market INDEX's full daily history (ticker used as-is — no .NS append)."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="max", interval="1d")
    if df is None or df.empty:
        return None
    df = df.reset_index()
    if "Date" not in df.columns and "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "Date"})
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    if cols:
        df = df.dropna(subset=cols)
    return df.reset_index(drop=True) if not df.empty else None


def fetch_index_any(candidates):
    """Try the primary ticker, then each fallback; return (df, ticker_that_worked)."""
    for tk in candidates:
        try:
            df = fetch_index(tk)
        except Exception:
            df = None
        if df is not None and len(df) >= 30:
            return df, tk
    return None, (candidates[0] if candidates else None)


@st.cache_data(show_spinner="Fetching constituents…", ttl=86400)   # daily
def fetch_constituents(csv_name):
    """List an index's constituent NSE symbols from the NSE archive CSV. Returns [] on failure."""
    import requests, io
    url = f"https://archives.nseindia.com/content/indices/{csv_name}"
    headers = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        df = pd.read_csv(io.StringIO(r.text))
        col = next((c for c in df.columns if str(c).strip().lower() == "symbol"), None)
        if col is None:
            return []
        return sorted({str(s).strip() for s in df[col].dropna() if str(s).strip()})
    except Exception:
        return []


def get_df(ticker, cache):
    if cache and ticker in cache.get("data", {}):
        return cache["data"][ticker]
    return fetch_one(ticker)


def get_fund(ticker, cache, needed):
    if not needed:
        return None
    # V-universe tickers (those in the price cache) HONOUR the cache so the main panel matches the
    # sidebar scanner (which reads fundamentals only from the cache). Off-universe tickers (e.g. a
    # superstar's holding, not in the cache) are live-fetched so their fundamentals still show.
    if cache and ticker in cache.get("data", {}):
        return cache.get("fund", {}).get(ticker)
    return fetch_fund(ticker)


@st.cache_data(show_spinner=False)
def multi_strategy_status(ticker, token):
    """For one ticker: which V40/V40-N/V200 groups it's in, and the READY status + key
    metrics of every APPLICABLE strategy. Cached by (ticker, code/cache-version=token)."""
    df = get_df(ticker, cache)
    if df is None or len(df) < 30:
        return {"groups": [], "rows": []}
    mem = [g for g in ("v_40", "v_40_next", "v_200") if ticker in set(groups.get(g, []))]

    def _designed(c):
        return ("ALL_NSE" in c["groups"]) or bool(set(c["groups"]) & set(mem))

    # In a V-group → show the strategies DESIGNED for it. NOT in any V-group (superstar / off-universe
    # picks) → the group rule can't gate it, so CHECK EVERY strategy and let the signal decide; only the
    # broad 3×-in-3yr is "designed" for an all-NSE stock (the UI ⭐-marks it).
    appl = (list(vs.STRATEGY_CONFIG.keys()) if not mem
            else [s for s, c in vs.STRATEGY_CONFIG.items() if _designed(c)])
    rows = []
    for s in appl:
        need = s in ("lifetime_high", "three_x_three")
        try:
            kk = core.kpi_block(core.analyze(s, ticker, df, fundamentals=get_fund(ticker, cache, need)))
            rows.append({"key": s, "Strategy": core.STRATEGY_LABELS.get(s, s),
                         "designed": _designed(vs.STRATEGY_CONFIG[s]), "Status": kk["ready"],
                         "Exp profit %": kk["exp_profit_pct"],
                         "Exp. days": kk["exp_duration_days"] or None,        # time-to-target (backtest)
                         "Median days": kk["median_days"] or None,
                         "Success %": kk["success_rate"], "Opportunities": kk["total_ops"]})
        except Exception:
            pass
    return {"groups": mem, "rows": rows}


GROUP_LABELS = {"v_40": "V40", "v_40_next": "V40-N", "v_200": "V200"}


def _groups_of(ticker):
    """Which V-universe groups a ticker belongs to (from the cached groups lists)."""
    return [g for g in ("v_40", "v_40_next", "v_200") if ticker in set(groups.get(g, []))]


def _applicable_strats(mem):
    """Strategy keys designed for a stock given its group membership `mem`
    (the broad All-NSE 3×3 always applies)."""
    return [s for s, c in vs.STRATEGY_CONFIG.items()
            if ("ALL_NSE" in c["groups"]) or (set(c["groups"]) & set(mem))]


def _nav_to_stocks():
    """Point the 2-level nav at 📊 Analyze → 📈 Stocks. Every jump-to-stock click-through calls this
    (from a callback or before the nav renders), so setting the nav widget keys is always safe."""
    st.session_state.nav_section = "📊 Analyze"
    st.session_state.nav_analyze = "📈 Stocks"


def _open_in_strategy(ticker, skey, from_index):
    """Jump from the index page straight into Stock Analysis for `ticker`, preselecting `skey`
    (the Strategy dropdown reads st.session_state.strat_sel)."""
    st.session_state.sel_ticker = ticker
    st.session_state.ticker_pick = ticker            # keep the sidebar picker in sync
    st.session_state.user_picked = True
    _nav_to_stocks()
    st.session_state.setdefault("extra_tickers", set()).add(ticker)
    st.session_state.jumped_from = from_index
    st.session_state.strat_sel = skey


def _open_stock(ticker, from_label):
    """Open `ticker` in Stock Analysis (keeps the current strategy) — e.g. from a superstar's
    holding. Runs as a button on_click, so app_mode is set before any widget is instantiated."""
    st.session_state.sel_ticker = ticker
    st.session_state.ticker_pick = ticker            # keep the sidebar picker in sync
    st.session_state.user_picked = True
    _nav_to_stocks()
    st.session_state.setdefault("extra_tickers", set()).add(ticker)
    st.session_state.jumped_from = from_label


def _set_strategy(skey):
    """Switch the Strategy dropdown (button on_click → runs before the selectbox re-instantiates)."""
    st.session_state.strat_sel = skey


@st.cache_data(show_spinner="Building constituent value-screen…", ttl=21600)
def constituents_table(csv_name, key):
    """Value-screen table for an index's constituents: price, % below 200 DMA, % above
    52-week low, % below all-time high, and YoY revenue growth. Uses cached OHLCV/fund
    where available, live-fetches the rest (parallel). `key` (cache built ts) busts the
    cache when the data cache is rebuilt."""
    import concurrent.futures
    syms = fetch_constituents(csv_name)
    if not syms:
        return pd.DataFrame()
    datac = (cache or {}).get("data", {})
    fundc = (cache or {}).get("fund", {})

    def _row(sym):
        df = datac.get(sym)
        if df is None:
            try:
                df = _fetch_one_raw(sym)            # thread-safe (non-cached) live fetch
            except Exception:
                df = None
        if df is None or len(df) < 30:
            return None
        try:                                            # wrap EVERYTHING — one bad ticker must not
            di = vs.add_base_indicators(core._clean_ohlc(df))   # crash the whole parallel build
            cur = float(di["Close"].iloc[-1])
            d200, lo52 = di["dma_200"].iloc[-1], di["low_52w"].iloc[-1]
            ath = float(di["lifetime_high"].iloc[-1])
            f, rg = fundc.get(sym), None
            if f:
                ar = f.get("annual_revenue_hist") or []
                if (len(ar) >= 2 and len(ar[-1]) > 1 and len(ar[-2]) > 1 and ar[-2][1]):
                    rg = (ar[-1][1] / ar[-2][1] - 1) * 100
                elif f.get("sales_growth_pct") is not None:
                    rg = f["sales_growth_pct"]
            return {
                "Ticker": sym, "Price": round(cur, 2),
                "% below 200DMA": round((d200 - cur) / d200 * 100, 1) if (pd.notna(d200) and d200) else None,
                "% above 52w low": round((cur - lo52) / lo52 * 100, 1) if (pd.notna(lo52) and lo52) else None,
                "% below ATH": round((ath - cur) / ath * 100, 1) if ath else None,
                "Rev growth % YoY": round(rg, 1) if rg is not None else None,
            }
        except Exception:
            return None
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(_row, syms):
            if r:
                rows.append(r)
    return pd.DataFrame(rows)


def build_full_cache(groups, do_prices=True, do_fund=True, prev=None):
    """Refresh the cache pickle. `do_prices` re-fetches all OHLCV; `do_fund` re-fetches
    fundamentals. Whichever you DON'T refresh is carried over from `prev` (the existing
    cache) — so you can refresh prices daily and fundamentals only when you want, each
    keeping the other intact. Separate timestamps track when each was last refreshed."""
    import concurrent.futures
    prev = prev or {}
    all_t = sorted(set().union(*[set(v) for v in groups.values()])) if groups else []
    # ALSO refresh any off-universe tickers the nightly build cached (e.g. quality-superstar picks) —
    # otherwise a manual in-app price refresh would silently drop every non-V-universe series.
    all_t = sorted(set(all_t) | set(prev.get("data") or {}) | set(prev.get("fund") or {}))
    data = dict(prev.get("data") or {})           # default: keep existing
    fund = dict(prev.get("fund") or {})
    now = datetime.now().isoformat()
    ts_prices, ts_fund = prev.get("built_prices"), prev.get("built_fund")

    def _parallel(fn, label):
        out, prog = {}, st.progress(0.0, text=f"Fetching {label}…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fn, t): t for t in all_t}
            for i, fut in enumerate(concurrent.futures.as_completed(futs)):
                t = futs[fut]
                try:
                    out[t] = fut.result()
                except Exception:
                    out[t] = None
                prog.progress((i + 1) / max(1, len(all_t)), text=f"{label} {i+1}/{len(all_t)}")
        prog.empty()
        return out

    if do_prices:                                  # merge: keep a prior series for any ticker that fails to refetch
        got = _parallel(_fetch_one_raw, "prices")
        data.update({t: df for t, df in got.items() if df is not None})
        ts_prices = now
    if do_fund:                                    # merge: keep prior fundamentals for any that come back empty
        got = _parallel(vs.fetch_fundamentals, "fundamentals")
        fund.update({t: f for t, f in got.items() if f})
        ts_fund = now

    payload = {"groups": groups, "data": data, "fund": fund,
               "built": now, "built_prices": ts_prices, "built_fund": ts_fund}
    with open(CACHE_PKL, "wb") as f:
        pickle.dump(payload, f)
    return payload


# ============================================================================
# SCANNER — run the selected strategy across ALL allowed tickers (uses cache)
# ============================================================================
@st.cache_data(show_spinner="Scanning tickers…")
def scan_strategy(skey, token):
    cache = load_cache()
    groups = get_groups(cache)
    cfg = vs.STRATEGY_CONFIG[skey]
    grp_of, allowed = {}, []
    for g in cfg["groups"]:
        src = list(groups.keys()) if g == "ALL_NSE" else [g]
        for gg in src:
            for t in groups.get(gg, []):
                if t not in grp_of:
                    grp_of[t] = gg
                    allowed.append(t)
    data = (cache or {}).get("data", {})
    fundc = (cache or {}).get("fund", {})
    rows = []
    for t in allowed:
        df = data.get(t)
        if df is None:
            continue
        fund = fundc.get(t) if skey in ("lifetime_high", "three_x_three") else None
        try:
            a = core.analyze(skey, t, df, fundamentals=fund)
            k = core.kpi_block(a)
        except Exception:
            continue
        status = {"YES": "🟢 READY", "REVIEW": "🟡 REVIEW"}.get(k["ready"], "⚪ —")
        rows.append({
            "Ticker": t, "Group": grp_of.get(t, ""), "Status": status,
            "Current": k["current_price"], "Exp Profit %": k["exp_profit_pct"],
            "Success %": k["success_rate"], "Avg Win %": k["avg_win_profit"],
            "Ops": k["total_ops"],
            "Last Opp": (pd.to_datetime(k["last_opp_date"]).date()
                         if k["last_opp_date"] is not None else None),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        order = {"🟢 READY": 0, "🟡 REVIEW": 1, "⚪ —": 2}
        out["_o"] = out["Status"].map(order).fillna(3)
        out = out.sort_values(["_o", "Exp Profit %"], ascending=[True, False]).drop(columns="_o")
    return out


@st.cache_data(show_spinner="Building the investable list…", ttl=1800)
def build_investable_table(token, strategies=("v20", "lifetime_high", "fifty_two_low")):
    """One row per (ticker, strategy) currently READY/REVIEW, for the chosen `strategies` only,
    restricted to the V-UNIVERSE (V40 / V40 Next / V200) — the names these strategies are built for.
    Reads ONLY cached data — never live-fetches — so it's fast. Columns mirror the KPI block.
    Keyed by `token` (cache version). Uses module globals `cache`/`groups`."""
    datac = (cache or {}).get("data", {})
    fundc = (cache or {}).get("fund", {})
    strategies = tuple(s for s in strategies if s in vs.STRATEGY_CONFIG)
    v_universe = set().union(*[set(groups.get(g, [])) for g in ("v_40", "v_40_next", "v_200")]) \
        if groups else set()

    rows = []
    for t in sorted(datac.keys()):
        if t not in v_universe:                        # Investable now = V-universe only (skip off-universe names)
            continue
        df = datac.get(t)
        if df is None or len(df) < 30:
            continue
        mem = [g for g in ("v_40", "v_40_next", "v_200") if t in set(groups.get(g, []))]
        glabel = " / ".join(GROUP_LABELS.get(g, g) for g in mem) or "All-NSE"
        for skey in strategies:
            fund = fundc.get(t) if skey in ("lifetime_high", "three_x_three") else None
            try:
                kk = core.kpi_block(core.analyze(skey, t, df, fundamentals=fund))
            except Exception:
                continue
            if kk.get("ready") not in ("YES", "REVIEW"):       # keep only actionable rows
                continue
            rows.append({
                "Reviewed": False,
                "Ticker": t,
                "Group": glabel,
                "Strategy": core.STRATEGY_LABELS.get(skey, skey),
                "Status": {"YES": "🟢 READY", "REVIEW": "🟡 REVIEW"}.get(kk["ready"], kk["ready"]),
                "Entry": kk["entry"],
                "Target": kk["target"],
                "Exp Profit %": kk["exp_profit_pct"],
                "Success %": kk["success_rate"],
                "Exp. days": kk["exp_duration_days"] or None,
                "Median days": kk["median_days"] or None,
                "Opportunities": kk["total_ops"],
                "Avg Win %": kk["avg_win_profit"],
                "Last Opp": (pd.to_datetime(kk["last_opp_date"]).date()
                             if kk["last_opp_date"] is not None else None),
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        order = {"🟢 READY": 0, "🟡 REVIEW": 1}
        out["_o"] = out["Status"].map(order).fillna(2)
        out = out.sort_values(["_o", "Exp Profit %"], ascending=[True, False]).drop(columns="_o").reset_index(drop=True)
    return out


def _fmt_num(x):
    """Pretty-print a calc result: thousands separators, up to 6 dp, no trailing zeros."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            return str(x)
        if x == int(x) and abs(x) < 1e15:
            return f"{int(x):,}"
        return f"{x:,.6f}".rstrip("0").rstrip(".")
    return f"{x:,}"


_FLOAT_CALC_HTML = r"""
<script>
(function () {
  try {
    var W = window.parent;                      // the MAIN page (persists across Streamlit reruns)
    var doc = W.document;
    var S = W.__spdCalcState || (W.__spdCalcState = {});   // calc state kept on the parent window

    // Lifecycle: _FLOAT_CALC_HTML is a constant string at a fixed tree position, so Streamlit
    // normally REUSES the same iframe across reruns — this IIFE runs ONCE, and the panel + its
    // listeners simply keep serving (that is why state survives a normal rerun). This remove-then-
    // rebuild path only fires on a genuine remount (full reload / position shift); it is the safety
    // net that prevents a duplicate panel or dead listeners, restoring position/expression/open-state
    // from S (kept on the parent window) so a remount is seamless too.
    ['spd-float-calc', 'spd-calc-reopen', 'spd-float-calc-style'].forEach(function (id) {
      var el = doc.getElementById(id); if (el) el.remove();
    });

    var css = ''
    + '#spd-float-calc{position:fixed;bottom:22px;right:80px;z-index:1000;width:272px;'
    +   'background:#1b2027;border:1px solid #3a4048;border-radius:11px;'
    +   'box-shadow:0 10px 34px rgba(0,0,0,.55);font-family:ui-monospace,Menlo,Consolas,monospace;'
    +   'color:#e6e9ee;}'
    + '#spd-float-calc .cf-head{display:flex;align-items:center;justify-content:space-between;'
    +   'padding:7px 11px;cursor:move;background:#11151a;border-radius:11px 11px 0 0;'
    +   'border-bottom:1px solid #2a2f36;user-select:none;}'
    + '#spd-float-calc .cf-title{font-size:.80rem;font-weight:600;letter-spacing:.3px;}'
    + '#spd-float-calc .cf-head button{background:none;border:none;color:#9aa3ad;font-size:.95rem;'
    +   'cursor:pointer;padding:0 5px;line-height:1;}'
    + '#spd-float-calc .cf-head button:hover{color:#fff;}'
    + '#spd-float-calc .cf-body{padding:9px;}'
    + '#spd-float-calc .cf-disp{width:100%;box-sizing:border-box;background:#0d1014;'
    +   'border:1px solid #2a2f36;border-radius:7px;color:#e6e9ee;font-size:1.05rem;text-align:right;'
    +   'padding:9px 11px;font-family:inherit;outline:none;}'
    + '#spd-float-calc .cf-disp:focus{border-color:#3d6fb0;}'
    + '#spd-float-calc .cf-res{text-align:right;font-size:.98rem;color:#5ad19a;min-height:1.25em;'
    +   'padding:4px 5px 7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}'
    + '#spd-float-calc .cf-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;}'
    + '#spd-float-calc .cf-grid button{padding:10px 0;border-radius:7px;border:1px solid #2f353d;'
    +   'background:#262b31;color:#dfe3e8;font-size:.88rem;cursor:pointer;font-family:inherit;}'
    + '#spd-float-calc .cf-grid button:hover{background:#333a42;}'
    + '#spd-float-calc .cf-grid button:active{transform:translateY(1px);}'
    + '#spd-float-calc .op{color:#7cc5ff;}'
    + '#spd-float-calc .fn{background:#21262c;color:#d9b24a;font-size:.76rem;}'
    + '#spd-float-calc .eq{background:#1b8f4d;color:#fff;border-color:#14633a;grid-column:span 2;}'
    + '#spd-float-calc .clr{background:#b03a2e;color:#fff;border-color:#7d2820;}'
    + '#spd-float-calc.min .cf-body{display:none;}'
    + '#spd-calc-reopen{position:fixed;bottom:22px;right:22px;z-index:1000;width:46px;height:46px;'
    +   'border-radius:50%;background:#1b8f4d;color:#fff;border:1px solid #14633a;font-size:1.25rem;'
    +   'cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.5);display:none;}';
    var st = doc.createElement('style'); st.id = 'spd-float-calc-style';
    st.textContent = css; doc.head.appendChild(st);

    var wrap = doc.createElement('div'); wrap.id = 'spd-float-calc';
    wrap.innerHTML = ''
      + '<div class="cf-head" id="cf-head"><span class="cf-title">🧮 Calculator</span>'
      +   '<span><button data-act="min" title="minimize">_</button>'
      +   '<button data-act="close" title="close">✕</button></span></div>'
      + '<div class="cf-body">'
      +   '<input class="cf-disp" id="cf-disp" placeholder="type or tap — e.g. (26848/22543-1)*100" />'
      +   '<div class="cf-res" id="cf-res"></div>'
      +   '<div class="cf-grid">'
      +     '<button class="fn" data-k="sqrt(">√</button><button class="fn" data-act="square">x²</button>'
      +     '<button class="fn" data-k="**">xʸ</button><button class="fn" data-k="log(">log</button>'
      +     '<button class="fn" data-k="ln(">ln</button>'
      +     '<button class="fn" data-k="sin(">sin</button><button class="fn" data-k="cos(">cos</button>'
      +     '<button class="fn" data-k="tan(">tan</button><button class="fn" data-k="pi">π</button>'
      +     '<button class="fn" data-k="e">e</button>'
      +     '<button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button>'
      +     '<button class="op" data-k="/">÷</button><button class="clr" data-act="clear">C</button>'
      +     '<button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button>'
      +     '<button class="op" data-k="*">×</button><button class="op" data-act="back">⌫</button>'
      +     '<button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button>'
      +     '<button class="op" data-k="-">−</button><button class="op" data-k="%">%</button>'
      +     '<button data-k="0">0</button><button data-k=".">.</button>'
      +     '<button data-k="(">(</button><button data-k=")">)</button><button class="op" data-k="+">+</button>'
      +     '<button class="eq" data-act="eq">=</button>'
      +     '<button class="fn" data-k="*1.">×1.</button><button class="fn" data-act="ans" title="last answer">ANS</button>'
      +   '</div></div>';
    doc.body.appendChild(wrap);

    var reopen = doc.createElement('button'); reopen.id = 'spd-calc-reopen';
    reopen.textContent = '🧮'; reopen.title = 'Open calculator'; doc.body.appendChild(reopen);

    var disp = doc.getElementById('cf-disp');
    var resEl = doc.getElementById('cf-res');
    var lastAns = (typeof S.ans === 'number') ? S.ans : null;

    // --- restore saved state (position / expression / minimized / closed) ---
    if (typeof S.value === 'string') disp.value = S.value;
    if (S.left != null && S.top != null) {
      wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
      var _cl = Math.max(0, Math.min(S.left, W.innerWidth - 60));
      var _ct = Math.max(54, Math.min(S.top, W.innerHeight - 40));   // keep the header on-screen (min/close reachable)
      wrap.style.left = _cl + 'px'; wrap.style.top = _ct + 'px';
      S.left = _cl; S.top = _ct;
    }
    if (S.min) wrap.classList.add('min');
    if (S.closed) { wrap.style.display = 'none'; reopen.style.display = 'block'; }
    function save() { S.value = disp.value; S.ans = lastAns; }

    function fmt(v) {
      if (typeof v !== 'number' || Number.isNaN(v)) return '—';
      if (v === Infinity) return '∞';
      if (v === -Infinity) return '-∞';
      if (v === 0) return '0';                                   // also collapses -0
      var a = Math.abs(v);
      if (a >= 1e15 || a < 1e-4) return v.toExponential(6).replace(/\.?0+e/, 'e');   // out-of-range -> sci
      if (Number.isInteger(v)) return v.toLocaleString('en-US');
      var r = Math.round(v * 1e6) / 1e6;
      if (r === 0) return v.toExponential(6).replace(/\.?0+e/, 'e');  // tiny but nonzero -> keep precision
      return r.toLocaleString('en-US', { maximumFractionDigits: 6 });
    }
    function compute(raw) {
      if (!raw || !raw.trim()) return '';
      var s = raw
        .replace(/×/g, '*').replace(/÷/g, '/').replace(/−/g, '-')
        .replace(/\bsqrt\s*\(/g, 'Math.sqrt(').replace(/\bcbrt\s*\(/g, 'Math.cbrt(')
        .replace(/\blog10\s*\(/g, 'Math.log10(').replace(/\blog2\s*\(/g, 'Math.log2(')
        .replace(/\blog\s*\(/g, 'Math.log10(').replace(/\bln\s*\(/g, 'Math.log(')
        .replace(/\bexp\s*\(/g, 'Math.exp(').replace(/\babs\s*\(/g, 'Math.abs(')
        .replace(/\bsin\s*\(/g, 'Math.sin(').replace(/\bcos\s*\(/g, 'Math.cos(')
        .replace(/\btan\s*\(/g, 'Math.tan(').replace(/\bround\s*\(/g, 'Math.round(')
        .replace(/\bmin\s*\(/g, 'Math.min(').replace(/\bmax\s*\(/g, 'Math.max(')
        .replace(/\bpow\s*\(/g, 'Math.pow(').replace(/\bfloor\s*\(/g, 'Math.floor(')
        .replace(/\bceil\s*\(/g, 'Math.ceil(')
        .replace(/\bpi\b/g, '(Math.PI)').replace(/\btau\b/g, '(2*Math.PI)').replace(/\be\b/g, '(Math.E)')
        .replace(/\^/g, '**');
      // whitelist: strip ONLY the closed set of Math members we generate + allowed chars; if
      // anything remains, reject. (Closed allowlist so Math.constructor / Math.name etc. can't slip
      // through — no injection surface even though this is Function()-eval'd client-side.)
      var probe = s
        .replace(/Math\.(?:sqrt|cbrt|log10|log2|log|exp|abs|sin|cos|tan|round|min|max|pow|floor|ceil|PI|E)\b/g, '')
        .replace(/[0-9eE+\-*/%(). ,]/g, '');
      if (probe.length) throw new Error('bad chars');
      var v = Function('"use strict";return (' + s + ')')();
      if (typeof v !== 'number') throw new Error('not a number');
      return v;
    }
    function refresh() {
      save();
      var t = disp.value.trim();
      if (!t) { resEl.textContent = ''; return; }
      try { var v = compute(t); resEl.style.color = '#5ad19a'; resEl.textContent = '= ' + fmt(v); }
      catch (err) { resEl.style.color = '#c98b8b'; resEl.textContent = '…'; }
    }
    function insert(txt) {
      var s = disp.selectionStart, e = disp.selectionEnd, v = disp.value;
      disp.value = v.slice(0, s) + txt + v.slice(e);
      disp.selectionStart = disp.selectionEnd = s + txt.length;
      disp.focus(); refresh();
    }
    function doEquals() {
      try { var v = compute(disp.value); lastAns = v; disp.value = String(v);
            disp.selectionStart = disp.selectionEnd = disp.value.length;
            resEl.style.color = '#5ad19a'; resEl.textContent = '= ' + fmt(v); save(); }
      catch (err) { resEl.style.color = '#c98b8b'; resEl.textContent = 'invalid expression'; }
    }

    disp.addEventListener('input', refresh);
    disp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); doEquals(); }
      ev.stopPropagation();          // keep keystrokes out of Streamlit shortcuts
    });

    wrap.addEventListener('click', function (ev) {
      var b = ev.target.closest('button'); if (!b) return;
      var act = b.getAttribute('data-act'), k = b.getAttribute('data-k');
      if (k != null) { insert(k); return; }
      if (act === 'eq') doEquals();
      else if (act === 'clear') { disp.value = ''; resEl.textContent = ''; disp.focus(); save(); }
      else if (act === 'back') { var s = disp.selectionStart; if (s > 0) {
          disp.value = disp.value.slice(0, s - 1) + disp.value.slice(disp.selectionEnd);
          disp.selectionStart = disp.selectionEnd = s - 1; } disp.focus(); refresh(); }
      else if (act === 'square') {                 // wrap whole expr so (-3)² = 9, not -3**2 (a syntax error)
        var t = disp.value.trim(); if (t) { disp.value = '(' + t + ')**2';
          disp.selectionStart = disp.selectionEnd = disp.value.length; disp.focus(); refresh(); } }
      else if (act === 'ans') { if (lastAns != null) {
          insert(lastAns < 0 ? '(' + lastAns + ')' : String(lastAns)); } }   // parenthesize negatives
      else if (act === 'min') { wrap.classList.toggle('min'); S.min = wrap.classList.contains('min'); }
      else if (act === 'close') { wrap.style.display = 'none'; reopen.style.display = 'block'; S.closed = true; }
    });
    reopen.addEventListener('click', function () {
      wrap.style.display = ''; wrap.classList.remove('min');
      reopen.style.display = 'none'; S.closed = false; S.min = false; disp.focus();
    });

    // --- drag by the header. Pointer Events + setPointerCapture keep move/up routed to the header
    //     even when the cursor passes over OTHER iframes (Plotly chart, the 0x0 calc iframe, etc.) —
    //     plain document mouse listeners would lose the event there and the panel would stick. The
    //     move/up listeners are added on pointerdown and removed on pointerup, so they never pile up.
    var head = doc.getElementById('cf-head');
    head.addEventListener('pointerdown', function (ev) {
      if (ev.target.tagName === 'BUTTON') return;
      var r = wrap.getBoundingClientRect();
      var ox = ev.clientX - r.left, oy = ev.clientY - r.top;
      wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
      try { head.setPointerCapture(ev.pointerId); } catch (_) {}
      function mv(e) {
        var x = Math.max(0, Math.min(e.clientX - ox, W.innerWidth - 60));
        var y = Math.max(54, Math.min(e.clientY - oy, W.innerHeight - 30));   // keep header below the top toolbar
        wrap.style.left = x + 'px'; wrap.style.top = y + 'px';
      }
      function up() {
        head.removeEventListener('pointermove', mv);
        head.removeEventListener('pointerup', up);
        head.removeEventListener('pointercancel', up);
        try { head.releasePointerCapture(ev.pointerId); } catch (_) {}
        S.left = parseFloat(wrap.style.left); S.top = parseFloat(wrap.style.top);
      }
      head.addEventListener('pointermove', mv);
      head.addEventListener('pointerup', up);
      head.addEventListener('pointercancel', up);
      ev.preventDefault();
    });

    if (disp.value) refresh();        // re-show the result after a rerun-rebuild
  } catch (e) { /* cross-origin / sandboxed host: silently skip the floating panel */ }
})();
</script>
"""


def render_floating_calculator():
    """Inject a draggable, always-on-top scientific calculator that hovers over the whole
    app (pure client-side JS — instant, no Streamlit reruns). Injected once; survives reruns."""
    components.html(_FLOAT_CALC_HTML, height=0, width=0)


_FLOAT_NOTES_HTML = r"""
<script>
(function () {
  try {
    var W = window.parent, doc = W.document;
    var UI = W.__spdNotesUI || (W.__spdNotesUI = {});   // panel position / open-state (per session)
    ['spd-notes', 'spd-notes-reopen', 'spd-notes-style'].forEach(function (id) {
      var el = doc.getElementById(id); if (el) el.remove();   // rebuild fresh each run (see calculator note)
    });

    var css = ''
    + '#spd-notes{position:fixed;bottom:22px;right:80px;z-index:1000;width:322px;max-height:74vh;'
    +   'display:flex;flex-direction:column;background:#1b2027;border:1px solid #3a4048;border-radius:11px;'
    +   'box-shadow:0 10px 34px rgba(0,0,0,.55);font-family:ui-monospace,Menlo,Consolas,monospace;color:#e6e9ee;}'
    + '#spd-notes .nf-head{display:flex;align-items:center;justify-content:space-between;padding:7px 11px;'
    +   'cursor:move;background:#11151a;border-radius:11px 11px 0 0;border-bottom:1px solid #2a2f36;user-select:none;}'
    + '#spd-notes .nf-title{font-size:.80rem;font-weight:600;letter-spacing:.3px;}'
    + '#spd-notes .nf-head button{background:none;border:none;color:#9aa3ad;font-size:.95rem;cursor:pointer;padding:0 5px;line-height:1;}'
    + '#spd-notes .nf-head button:hover{color:#fff;}'
    + '#spd-notes .nf-body{padding:9px;display:flex;flex-direction:column;gap:7px;overflow:hidden;}'
    + '#spd-notes .nf-ctx{font-size:.70rem;color:#8a9aa9;background:#0d1014;border:1px solid #2a2f36;'
    +   'border-radius:6px;padding:5px 7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}'
    + '#spd-notes textarea.nf-ta{width:100%;box-sizing:border-box;min-height:46px;max-height:120px;resize:vertical;'
    +   'background:#0d1014;border:1px solid #2a2f36;border-radius:7px;color:#e6e9ee;padding:7px;font-family:inherit;font-size:.84rem;outline:none;}'
    + '#spd-notes textarea.nf-ta:focus{border-color:#3d6fb0;}'
    + '#spd-notes .nf-row{display:flex;gap:6px;align-items:center;}'
    + '#spd-notes .nf-row label{font-size:.70rem;color:#9aa3ad;display:flex;align-items:center;gap:3px;}'
    + '#spd-notes input.nf-filter{flex:1;box-sizing:border-box;background:#0d1014;border:1px solid #2a2f36;'
    +   'border-radius:6px;color:#e6e9ee;padding:5px 7px;font-family:inherit;font-size:.78rem;outline:none;}'
    + '#spd-notes button.nf-btn{background:#262b31;color:#dfe3e8;border:1px solid #2f353d;border-radius:7px;'
    +   'padding:6px 10px;font-size:.78rem;cursor:pointer;font-family:inherit;}'
    + '#spd-notes button.nf-btn:hover{background:#333a42;}'
    + '#spd-notes button.nf-add{background:#1b8f4d;color:#fff;border-color:#14633a;flex:1;}'
    + '#spd-notes .nf-list{overflow-y:auto;max-height:40vh;display:flex;flex-direction:column;gap:6px;padding-right:2px;}'
    + '#spd-notes .nf-note{background:#21262c;border:1px solid #2f353d;border-radius:8px;padding:6px 8px;font-size:.80rem;}'
    + '#spd-notes .nf-note.star{border-color:#caa10a;background:#252316;}'
    + '#spd-notes .nf-meta{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:3px;}'
    + '#spd-notes .nf-chip{background:#2d333b;color:#7cc5ff;border-radius:5px;padding:1px 6px;font-size:.66rem;'
    +   'cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px;}'
    + '#spd-notes .nf-chip:hover{background:#36506e;color:#cfe6ff;}'
    + '#spd-notes .nf-time{font-size:.64rem;color:#7a828b;margin-bottom:3px;}'
    + '#spd-notes .nf-actions button{background:none;border:none;color:#9aa3ad;cursor:pointer;font-size:.82rem;padding:0 2px;}'
    + '#spd-notes .nf-actions button:hover{color:#fff;}'
    + '#spd-notes .nf-txt{white-space:pre-wrap;word-break:break-word;outline:none;line-height:1.35;}'
    + '#spd-notes .nf-txt:focus{background:#0d1014;border-radius:4px;}'
    + '#spd-notes .nf-foot{display:flex;align-items:center;justify-content:space-between;}'
    + '#spd-notes.min .nf-body{display:none;}'
    + '#spd-notes-reopen{position:fixed;bottom:78px;right:22px;z-index:1000;width:46px;height:46px;border-radius:50%;'
    +   'background:#2d333b;color:#fff;border:1px solid #3a4048;font-size:1.25rem;cursor:pointer;'
    +   'box-shadow:0 6px 18px rgba(0,0,0,.5);display:none;}'      /* stacked just above the 🧮 calculator FAB */
    + '#spd-notes button.nf-clear{color:#e7a39a;border-color:#5a3030;}'
    + '#spd-notes button.nf-clear:hover{background:#3a2424;color:#fff;}'
    + '#spd-notes .nf-saved{font-size:.64rem;color:#6f8a76;}';
    var st = doc.createElement('style'); st.id = 'spd-notes-style'; st.textContent = css; doc.head.appendChild(st);

    var wrap = doc.createElement('div'); wrap.id = 'spd-notes';
    wrap.innerHTML = ''
      + '<div class="nf-head" id="nf-head"><span class="nf-title">📒 Notes</span>'
      +   '<span><button data-act="min" title="minimize">_</button>'
      +   '<button data-act="close" title="close">✕</button></span></div>'
      + '<div class="nf-body">'
      +   '<div class="nf-ctx" id="nf-ctx"></div>'
      +   '<textarea class="nf-ta" id="nf-ta" placeholder="Jot a note — e.g. \'watch for breakout above 1020\', \'check promoter pledge\'. Ctrl/⌘+Enter to add."></textarea>'
      +   '<div class="nf-row"><label><input type="checkbox" id="nf-attach" checked> attach current view</label>'
      +     '<button class="nf-btn nf-add" id="nf-add">＋ Add note</button></div>'
      +   '<div class="nf-row"><input class="nf-filter" id="nf-filter" placeholder="filter notes…"/>'
      +     '<label><input type="checkbox" id="nf-staronly"> ⭐ only</label></div>'
      +   '<div class="nf-list" id="nf-list"></div>'
      +   '<div class="nf-saved" id="nf-saved"></div>'
      +   '<div class="nf-row nf-foot"><span class="nf-time" id="nf-count"></span>'
      +     '<span><button class="nf-btn" id="nf-export" title="copy all notes as markdown">⧉ Copy</button>'
      +     '<button class="nf-btn nf-clear" id="nf-clear" title="delete ALL notes from the cache">🗑 Clear</button></span></div>'
      + '</div>';
    doc.body.appendChild(wrap);

    var reopen = doc.createElement('button'); reopen.id = 'spd-notes-reopen';
    reopen.textContent = '📒'; reopen.title = 'Open notes'; doc.body.appendChild(reopen);

    var KEY = 'spd-notes-v1';
    function load() { try { return JSON.parse(W.localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
    function save() { try { W.localStorage.setItem(KEY, JSON.stringify(notes)); } catch (e) {} }
    var notes = load();

    var listEl = doc.getElementById('nf-list'), countEl = doc.getElementById('nf-count'),
        ctxEl = doc.getElementById('nf-ctx'), taEl = doc.getElementById('nf-ta'),
        filterEl = doc.getElementById('nf-filter'), starOnlyEl = doc.getElementById('nf-staronly'),
        attachEl = doc.getElementById('nf-attach'), savedEl = doc.getElementById('nf-saved');

    function curCtx() { try { return W.__spdContext || null; } catch (e) { return null; } }
    function ctxLabel(c) {
      if (!c) return '';
      if (c.kind === 'superstar') return '⭐ ' + (c.investor || 'investor');
      if (c.kind === 'index') return '📈 ' + (c.index || 'index');
      var dot = { YES: '🟢', REVIEW: '🟡', NO: '🔴' }[c.status] || '';
      return (c.ticker || '') + (c.strategy ? ' · ' + c.strategy : '')
           + (c.price ? ' · ₹' + c.price : '') + (dot ? ' ' + dot : '');
    }
    function showCtx() {
      var c = curCtx();
      ctxEl.textContent = c ? ('Now viewing: ' + ctxLabel(c)) : 'Now viewing: — (open a stock or index)';
    }
    function fmtTime(ts) {
      var d = new Date(ts);
      return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    function openTicker(tk) { try { W.location.search = '?open=' + encodeURIComponent(tk); } catch (e) {} }

    function render() {
      var f = (filterEl.value || '').toLowerCase(), so = starOnlyEl.checked;
      listEl.innerHTML = '';
      var shown = notes.filter(function (z) {
        if (so && !z.star) return false;
        if (f) { var hay = ((z.text || '') + ' ' + ctxLabel(z.ctx)).toLowerCase(); if (hay.indexOf(f) < 0) return false; }
        return true;
      });
      shown.forEach(function (z) {
        var card = doc.createElement('div'); card.className = 'nf-note' + (z.star ? ' star' : '');
        var meta = doc.createElement('div'); meta.className = 'nf-meta';
        var left = doc.createElement('div');
        if (z.ctx) {
          var chip = doc.createElement('span'); chip.className = 'nf-chip'; chip.textContent = ctxLabel(z.ctx);
          if (z.ctx.ticker) { chip.title = 'Open ' + z.ctx.ticker + ' in Stock Analysis';
            chip.addEventListener('click', function () { openTicker(z.ctx.ticker); }); }
          else { chip.style.cursor = 'default'; }
          left.appendChild(chip);
        }
        var actions = doc.createElement('div'); actions.className = 'nf-actions';
        var starb = doc.createElement('button'); starb.textContent = z.star ? '⭐' : '☆'; starb.title = 'mark to revisit';
        starb.addEventListener('click', function () { z.star = !z.star; save(); render(); });
        var delb = doc.createElement('button'); delb.textContent = '✕'; delb.title = 'delete';
        delb.addEventListener('click', function () { notes = notes.filter(function (x) { return x.id !== z.id; }); save(); render(); });
        actions.appendChild(starb); actions.appendChild(delb);
        meta.appendChild(left); meta.appendChild(actions);
        var time = doc.createElement('div'); time.className = 'nf-time'; time.textContent = fmtTime(z.ts);
        var txt = doc.createElement('div'); txt.className = 'nf-txt'; txt.contentEditable = 'true'; txt.textContent = z.text || '';
        txt.title = 'click to edit';
        txt.addEventListener('blur', function () { z.text = txt.innerText; save(); });   // edit-in-place, no re-render
        txt.addEventListener('input', function () {        // persist as typed (debounced) so a remount can't lose it
          z.text = txt.innerText; clearTimeout(z._sv); z._sv = setTimeout(save, 400);
        });
        txt.addEventListener('keydown', function (ev) { ev.stopPropagation(); });
        card.appendChild(meta); card.appendChild(time); card.appendChild(txt);
        listEl.appendChild(card);
      });
      countEl.textContent = notes.length + ' note' + (notes.length === 1 ? '' : 's')
        + (shown.length !== notes.length ? ' (' + shown.length + ' shown)' : '');
      if (notes.length) {
        var oldest = notes.reduce(function (m, z) { return Math.min(m, z.ts); }, notes[0].ts);
        savedEl.textContent = '💾 cached in this browser — kept until you Clear · since '
          + new Date(oldest).toLocaleDateString();
      } else {
        savedEl.textContent = '💾 cached in this browser — survives app restarts';
      }
    }

    function addNote() {
      var t = (taEl.value || '').trim();
      var c = attachEl.checked ? curCtx() : null;
      if (!t && !c) return;
      notes.unshift({ id: (W.__spdNoteSeq = (W.__spdNoteSeq || 0) + 1) + '-' + new Date().getTime(),
                      ts: new Date().getTime(), text: t,
                      ctx: c ? JSON.parse(JSON.stringify(c)) : null, star: false });
      save(); taEl.value = ''; render(); taEl.focus();
    }
    function exportAll() {
      if (!notes.length) return;
      var md = notes.map(function (z) {
        return '- [' + fmtTime(z.ts) + '] ' + (z.star ? '⭐ ' : '')
             + (z.ctx ? '(' + ctxLabel(z.ctx) + ') ' : '') + (z.text || '');
      }).join('\n');
      try {
        navigator.clipboard.writeText(md).then(
          function () { countEl.textContent = 'copied ' + notes.length + ' notes ✓'; },
          function () { countEl.textContent = 'copy blocked — select & copy manually'; });
      } catch (e) { countEl.textContent = 'copy not available here'; }
    }
    function clearAll() {
      if (!notes.length) return;
      if (W.confirm('Delete ALL ' + notes.length + ' note(s) from the cache? This cannot be undone.')) {
        notes = []; save(); render();
      }
    }

    doc.getElementById('nf-add').addEventListener('click', addNote);
    doc.getElementById('nf-export').addEventListener('click', exportAll);
    doc.getElementById('nf-clear').addEventListener('click', clearAll);
    filterEl.addEventListener('input', render);
    filterEl.addEventListener('keydown', function (ev) { ev.stopPropagation(); });
    starOnlyEl.addEventListener('change', render);
    taEl.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); addNote(); }
      ev.stopPropagation();
    });
    doc.getElementById('nf-head').addEventListener('click', function (ev) {
      var b = ev.target.closest('button'); if (!b) return; var act = b.getAttribute('data-act');
      if (act === 'min') { wrap.classList.toggle('min'); UI.min = wrap.classList.contains('min'); }
      else if (act === 'close') { wrap.style.display = 'none'; reopen.style.display = 'block'; UI.closed = true; }
    });
    reopen.addEventListener('click', function () {
      wrap.style.display = ''; wrap.classList.remove('min'); reopen.style.display = 'none'; UI.closed = false; UI.min = false;
    });

    if (UI.left != null && UI.top != null) {
      // clamp into the viewport so the drag-head (minimize/close) is ALWAYS reachable,
      // even if a prior session saved an off-screen position.
      var _cl = Math.max(0, Math.min(UI.left, W.innerWidth - 60));
      var _ct = Math.max(54, Math.min(UI.top, W.innerHeight - 60));
      wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
      wrap.style.left = _cl + 'px'; wrap.style.top = _ct + 'px';
    }
    if (UI.min) wrap.classList.add('min');
    if (UI.closed) { wrap.style.display = 'none'; reopen.style.display = 'block'; }

    var head = doc.getElementById('nf-head');
    head.addEventListener('pointerdown', function (ev) {
      if (ev.target.tagName === 'BUTTON') return;
      var r = wrap.getBoundingClientRect(); var ox = ev.clientX - r.left, oy = ev.clientY - r.top;
      wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
      try { head.setPointerCapture(ev.pointerId); } catch (_) {}
      function mv(e) {
        var x = Math.max(0, Math.min(e.clientX - ox, W.innerWidth - 60));
        var y = Math.max(54, Math.min(e.clientY - oy, W.innerHeight - 30));   // keep header below the top toolbar
        wrap.style.left = x + 'px'; wrap.style.top = y + 'px';
      }
      function up() {
        head.removeEventListener('pointermove', mv); head.removeEventListener('pointerup', up);
        head.removeEventListener('pointercancel', up); try { head.releasePointerCapture(ev.pointerId); } catch (_) {}
        UI.left = parseFloat(wrap.style.left); UI.top = parseFloat(wrap.style.top);
      }
      head.addEventListener('pointermove', mv); head.addEventListener('pointerup', up);
      head.addEventListener('pointercancel', up); ev.preventDefault();
    });

    showCtx(); render();
    // The iframe is reused across normal reruns (script runs once), but publish_context updates
    // __spdContext every rerun — poll so the "Now viewing" line tracks navigation. Clear any prior
    // timer (from a previous mount) so it never points at a detached element.
    try { if (W.__spdNotesCtxTimer) W.clearInterval(W.__spdNotesCtxTimer); } catch (_) {}
    W.__spdNotesCtxTimer = W.setInterval(showCtx, 800);
  } catch (e) { /* cross-origin / sandboxed host: skip */ }
})();
</script>
"""


def render_floating_notes():
    """Inject a draggable, always-on-top 📒 Notes panel. Notes persist in the browser
    (localStorage → survive reruns AND app restarts), capture the current view's context,
    can be starred to revisit, filtered, exported, and a note's ticker chip deep-links back
    into Stock Analysis. Client-side JS, same robust rebuild/drag model as the calculator."""
    components.html(_FLOAT_NOTES_HTML, height=0, width=0)


_STRAT_SHORT = {"sma": "SMA", "knoxville": "KD", "v20": "V20", "rhs": "RHS", "cup_handle": "CWH",
                "v10": "V10", "lifetime_high": "LTH", "fifty_two_low": "52wL", "three_x_three": "3x3"}


def publish_context(ctx):
    """Expose the current view (ticker/strategy/price/status, or index) to the floating
    notes panel via window.parent.__spdContext, so 'Add note' can attach it."""
    payload = json.dumps(ctx).replace("</", "<\\/")        # never let a value close the <script> early
    components.html("<script>try{window.parent.__spdContext=" + payload
                    + ";}catch(e){}</script>", height=0, width=0)


def render_chart_block(a, key, extend_proj=False, macro=None):
    """Measure tool + trendline-split control + candlestick chart (all indicators) +
    baseline/overlay captions. Shared by the stock AND index analysis pages so they
    look identical. Strategy-only captions (patterns, V20 range) auto-skip when the
    summary is empty (e.g. for an index). `extend_proj=True` (index view) shows the full
    history + the ~2yr trend projection by default."""
    with st.container(border=True):
        _dser = a["df"]["Date"]
        _dmin, _dmax = _dser.iloc[0].date(), _dser.iloc[-1].date()
        _ago = (datetime.now().date() - _dmax).days
        _fresh = ("✅ up to date" if _ago <= 0 else
                  ("🟢 last trading day" if _ago <= 3 else f"⚠️ {_ago} days old — consider refreshing"))
        st.caption(f"🕒 **Data through {_dmax}** ({_fresh}). Last close shown is this date's.")
        measure = None
        with st.expander("📏 Measure — % change & duration between two dates (like TradingView)"):
            _mc = st.columns([1, 1, 2])
            _def_from = _dser.iloc[-min(len(_dser) - 1, 22)].date()
            d_from = _mc[0].date_input("From", value=_def_from, min_value=_dmin, max_value=_dmax, key=f"mf_{key}")
            d_to = _mc[1].date_input("To", value=_dmax, min_value=_dmin, max_value=_dmax, key=f"mt_{key}")
            if d_from and d_to:
                i0 = (_dser - pd.Timestamp(d_from)).abs().idxmin()
                i1 = (_dser - pd.Timestamp(d_to)).abs().idxmin()
                if i1 < i0:
                    i0, i1 = i1, i0
                if i0 != i1:
                    r0, r1 = a["df"].loc[i0], a["df"].loc[i1]
                    p0, p1 = float(r0["Close"]), float(r1["Close"])
                    if not (pd.notna(p0) and pd.notna(p1)):
                        _mc[2].metric("Δ change", "—")
                        st.caption("One of the selected dates has no price data — pick a trading day.")
                    elif p0 == 0:
                        _mc[2].metric("Δ change", "—")
                    else:
                        pct = (p1 - p0) / p0 * 100
                        cal = (r1["Date"] - r0["Date"]).days
                        td = int(i1 - i0)
                        measure = ((r0["Date"], p0), (r1["Date"], p1), pct, td)
                        _mc[2].metric("Δ change", f"{pct:+.2f}%   ({p1 - p0:+.2f})")
                        st.caption(f"**{r0['Date'].date()}** ({p0:.2f}) → **{r1['Date'].date()}** ({p1:.2f}) "
                                   f"= **{pct:+.2f}%** over **{cal} days** ({td} trading days). "
                                   "Pink dashed line marks it on the chart.")
        _dser2 = a["df"]["Date"]
        _dmn, _dmx = _dser2.iloc[0].date(), _dser2.iloc[-1].date()
        _default_bp = min(max(pd.Timestamp("2017-03-01").date(), _dmn), _dmx)
        with st.expander("⚙️ Trendline split date"):
            _bp = st.date_input("1st segment ends on (everything up to this date drives the "
                                "**trend: early phase** line)",
                                value=_default_bp, min_value=_dmn, max_value=_dmx, key=f"bp_{key}")
            st.caption("Default = **2017-03-01**. The **trend: early phase** line (toggle it in the "
                       "legend) is fit to all data UP TO this date, then projected forward. "
                       "Move it to fit the period you consider 'normal' growth.")
        try:
            gb = core.growth_baselines(a["df"], breakpoint_date=str(_bp))
        except Exception:
            gb = None
        st.plotly_chart(core.build_chart(a, measure=measure, baselines=gb,
                                         extend_to_projection=extend_proj, macro=macro),
                        use_container_width=True,
                        config={"scrollZoom": True, "displaylogo": False, "displayModeBar": True,
                                "modeBarButtonsToRemove": ["lasso2d", "select2d"]})
        st.caption("🔍 **Zoom:** ➕/➖ & autoscale in the top-right toolbar · **scroll** = zoom · "
                   "**drag** = pan · drag the **slider** under the chart to set the window · "
                   "range buttons (1m…All) = quick spans · **double-click** = fit all.")
        if gb:
            def _bl(tag, bl):
                if not bl:
                    return None
                v = bl["pct_vs_baseline"]
                rel = (f"price **{abs(v):.0f}% {'above' if v >= 0 else 'below'}** it" if v is not None else "")
                return f"**{tag}** {bl['cagr_pct']}%/yr → {bl['price_today']:,.0f} today ({rel})"
            _bls = [x for x in (_bl("Early phase", gb.get("early")), _bl("All data", gb.get("full"))) if x]
            if _bls:
                _e = gb.get("early")
                _split = ""
                if _e and _e.get("breakpoint_date"):
                    _split = (f" 1st segment ends **{_e['breakpoint_date']}** "
                              + ("(your date)" if _e.get("user_set") else "(auto-detected knee)") + ".")
                _acc = ("  _(early & all-data trends are close — fairly steady growth.)_"
                        if (_e and _e.get("accelerated") is False) else "")
                st.caption("📈 **Growth baselines** (log/CAGR — toggle **trend: early phase** / "
                           "**trend: all data** in the legend; lines project forward, **double-click** "
                           "to see it): " + "  ·  ".join(_bls)
                           + ". A *descriptive* reference for 'minimum growth', **not** a target."
                           + _split + _acc)
        sm = a.get("summary", {})
        pshapes = sm.get("pattern_shapes") or []
        if pshapes:
            n_act = sum(1 for p in pshapes if p.get("active"))
            n_past = len(pshapes) - n_act
            st.caption(
                f"**{n_act} currently-forming** pattern(s) → drawn **bright/solid** (this is the buy "
                f"signal). **{n_past} past** pattern(s) → drawn **faded/dashed** for **reference only** "
                "(already played out — *not* a recommendation). "
                "🟣 RHS joins L-shoulder → Head → R-shoulder · 🟢 Cup&Handle joins Cup → Handle; "
                "the dotted **neckline / rim** is the level the breakout cleared. "
                "Hover any marker for that pattern's buy → target. "
                + ("**No pattern is forming right now**, so the entry/target lines are hidden — "
                   "the faded ones are just history to study." if n_act == 0 else ""))
        if sm.get("range_from"):
            st.caption(f"🟩 **V20 range = {sm.get('range_green_candles', '?')} consecutive green "
                       f"candles**, {sm['range_from']} → {sm['range_to']} "
                       f"(lower line **{sm.get('Entry_Price')}**, upper line **{sm.get('Target_Price')}**). "
                       "Zoom into the shaded band to verify the run.")
        st.caption("**Overlays (off by default — click them in the legend to show):** "
                   "20 / 50 / 100 / 200 / 300 DMA · 52w High · 52w Low · trend lines.")

        # ---- 🧮 Chart-aware quick calcs (the general calculator floats — top-right 🧮) ----
        _last = float(a["df"]["Close"].iloc[-1]) if len(a["df"]) else 0.0
        with st.expander("🧮 Quick calcs — % change · target · CAGR (pre-filled with this chart's last close)"):
            st.caption(f"Last close = **{_last:,.2f}**. For free-form math use the floating **🧮 Calculator** "
                       "(top-right — drag it anywhere over the chart).")
            _q = st.columns(4)
            _entry = _q[0].number_input("Entry / From", value=round(_last, 2), step=1.0, key=f"qe_{key}")
            _other = _q[1].number_input("Target / To", value=round(_last * 1.1, 2), step=1.0, key=f"qo_{key}")
            _pct = _q[2].number_input("Apply %", value=10.0, step=1.0, key=f"qp_{key}")
            _yrs = _q[3].number_input("Years (for CAGR)", value=1.0, min_value=0.1, step=0.5, key=f"qy_{key}")
            _r = st.columns(3)
            if _entry:
                _r[0].metric("% change Entry→Target", f"{(_other - _entry) / _entry * 100:+.2f}%",
                             help="((Target − Entry) / Entry) × 100")
            _r[1].metric(f"Entry {'+' if _pct >= 0 else ''}{_pct:g}%", _fmt_num(_entry * (1 + _pct / 100)),
                         help="Entry × (1 + %/100) — a quick target / stop level.")
            if _entry > 0 and _other > 0 and _yrs > 0:
                _cagr = ((_other / _entry) ** (1 / _yrs) - 1) * 100
                _r[2].metric("CAGR Entry→Target", f"{_cagr:+.2f}%/yr",
                             help="((Target / Entry) ^ (1 / Years) − 1) × 100")


# ============================================================================
# UI
# ============================================================================
st.markdown("""<style>
  /* main-area strategy buttons (constituent table + ticker profile) — coloured by READY status */
  div[class*="st-key-sgrdy_"] button{background:#1b8f4d;color:#fff;border:1px solid #14633a;}
  div[class*="st-key-sgrev_"] button{background:#caa10a;color:#111;border:1px solid #8a6d00;}
  div[class*="st-key-sgnot_"] button{background:#b03a2e;color:#fff;border:1px solid #7d2820;}
  div[class*="st-key-sgneu_"] button{background:#3a3f44;color:#eee;border:1px solid #555;}
  .cwrap{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px;}
  .cchip{background:#262b31;color:#dfe3e8;border:1px solid #3a4048;border-radius:6px;
         padding:2px 8px;font-size:0.70rem;font-family:ui-monospace,Menlo,monospace;white-space:nowrap;}
</style>""", unsafe_allow_html=True)

# ── "Aurum Terminal" premium polish — paint-only, stable selectors; injected AFTER the status-button
#    block above so the semantic green/amber/red status colours always win on any tie. ──
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@600;700;800&display=swap');

:root{
  color-scheme: dark;
  --spd-bg:#0C0D10; --spd-elev:#14161B; --spd-elev-2:#191C22;
  --spd-gold:#E3B341; --spd-gold-hi:#F6D98A;
  --spd-gold-soft:rgba(227,179,65,0.12); --spd-gold-line:rgba(227,179,65,0.38);
  --spd-line:#23262D; --spd-text:#ECEEF2; --spd-text-strong:#F5F6F8; --spd-muted:#8B929C;
}

/* typography + crisp tabular numerics */
html, body,
[data-testid="stAppViewContainer"],
[data-testid="stSidebar"]{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
[data-testid="stAppViewContainer"] h1{
  font-family:'Plus Jakarta Sans','Inter',sans-serif;
  font-weight:800; letter-spacing:-.025em; line-height:1.12; color:var(--spd-text-strong);
}
[data-testid="stAppViewContainer"] h2,
[data-testid="stAppViewContainer"] h3{
  font-family:'Plus Jakarta Sans','Inter',sans-serif;
  font-weight:700; letter-spacing:-.015em; color:var(--spd-text-strong);
}
[data-testid="stAppViewContainer"] h4,
[data-testid="stAppViewContainer"] h5,
[data-testid="stAppViewContainer"] h6{ font-weight:600; letter-spacing:-.008em; }
[data-testid="stCaptionContainer"]{ color:var(--spd-muted); }
::selection{ background:rgba(227,179,65,.28); color:#fff; }

/* app canvas: barely-there gold/blue depth (paint only) */
[data-testid="stAppViewContainer"]{
  background-image:
    radial-gradient(1200px 480px at 80% -10%, rgba(227,179,65,.06), transparent 60%),
    radial-gradient(900px 520px at 0% 0%, rgba(88,116,168,.05), transparent 55%);
}
[data-testid="stMainBlockContainer"]{ padding-bottom:4.5rem; }

/* top header: translucent, blurred */
[data-testid="stHeader"]{
  background:rgba(12,13,16,.72);
  backdrop-filter:blur(10px) saturate(120%); -webkit-backdrop-filter:blur(10px) saturate(120%);
  border-bottom:1px solid var(--spd-line);
}

/* cards: st.container(border=True) */
[data-testid="stVerticalBlockBorderWrapper"]{
  background:linear-gradient(180deg, rgba(255,255,255,.022), rgba(255,255,255,0) 42%), var(--spd-elev);
  border:1px solid var(--spd-line); border-radius:.7rem;
  box-shadow:0 1px 0 rgba(255,255,255,.03) inset, 0 10px 28px -22px rgba(0,0,0,.95);
}

/* metric tiles */
[data-testid="stMetric"]{
  position:relative; overflow:hidden;
  background:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,0) 55%), var(--spd-elev-2);
  border:1px solid var(--spd-line); border-radius:.65rem; padding:.85rem 1rem .9rem 1.05rem;
  transition:border-color .18s ease, box-shadow .18s ease;
}
[data-testid="stMetric"]::before{
  content:""; position:absolute; left:0; top:0; bottom:0; width:2px;
  background:linear-gradient(180deg,var(--spd-gold),rgba(227,179,65,0)); opacity:.55;
}
[data-testid="stMetric"]:hover{ border-color:var(--spd-gold-line); box-shadow:0 12px 30px -22px rgba(0,0,0,.95); }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p{
  color:var(--spd-muted); font-size:.72rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase;
}
[data-testid="stMetricValue"]{ color:var(--spd-text-strong); font-weight:700; letter-spacing:-.01em; font-variant-numeric:tabular-nums; }
[data-testid="stMetricDelta"]{ font-weight:600; font-variant-numeric:tabular-nums; }

/* sidebar shell + radio navigation → nav pills */
[data-testid="stSidebar"]{ border-right:1px solid var(--spd-line); }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] label{
  color:var(--spd-muted); font-weight:700; font-size:.7rem; letter-spacing:.10em; text-transform:uppercase;
}
[data-testid="stSidebar"] [role="radiogroup"]{ gap:.15rem; }
[data-testid="stSidebar"] [role="radiogroup"] label{
  border:1px solid transparent; border-radius:.55rem; padding:.4rem .6rem; margin:2px 0;
  transition:background .15s ease, border-color .15s ease; cursor:pointer;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover{ background:rgba(255,255,255,.045); }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){ background:var(--spd-gold-soft); border-color:var(--spd-gold-line); }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) div{ color:#F4E7C2; }

/* expanders */
[data-testid="stExpander"]{
  border:1px solid var(--spd-line); border-radius:.6rem; overflow:hidden;
  background:var(--spd-elev); margin-bottom:.4rem; transition:border-color .18s ease;
}
[data-testid="stExpander"]:hover{ border-color:var(--spd-gold-line); }
[data-testid="stExpander"] summary{ padding:.7rem .9rem; font-weight:600; color:#E7E9ED; transition:background .15s ease, color .15s ease; }
[data-testid="stExpander"] summary:hover{ background:rgba(255,255,255,.035); color:var(--spd-gold-hi); }
[data-testid="stExpander"] summary svg{ color:var(--spd-muted); transition:color .15s ease; }
[data-testid="stExpander"] summary:hover svg{ color:var(--spd-gold); }

/* buttons — generic rules never set bg/color (status colours win); hover via brightness; only primary→gold */
[data-testid="stButton"] button,
[data-testid="stDownloadButton"] button,
[data-testid="stFormSubmitButton"] button{
  border-radius:.55rem; font-weight:600; letter-spacing:.01em;
  transition:transform .06s ease, filter .18s ease, box-shadow .18s ease;
}
[data-testid="stButton"] button:hover,
[data-testid="stDownloadButton"] button:hover,
[data-testid="stFormSubmitButton"] button:hover{ filter:brightness(1.08); box-shadow:0 8px 20px -14px rgba(0,0,0,.85); }
[data-testid="stButton"] button:active{ transform:translateY(1px); }
[data-testid="stButton"] button[kind="primary"],
[data-testid="stFormSubmitButton"] button[kind="primary"],
[data-testid="stBaseButton-primary"]{
  background:linear-gradient(180deg,#EAC45C,#DCA935); color:#191408; border:1px solid #B98F1F;
  box-shadow:0 8px 20px -12px rgba(227,179,65,.55);
}

/* tabs */
[data-testid="stTabs"] [data-baseweb="tab-list"]{ gap:.3rem; border-bottom:1px solid var(--spd-line); }
[data-testid="stTabs"] [data-baseweb="tab"]{ padding:.5rem .9rem; border-radius:.5rem .5rem 0 0; color:var(--spd-muted); font-weight:600; transition:color .15s ease, background .15s ease; }
[data-testid="stTabs"] [data-baseweb="tab"]:hover{ color:var(--spd-text); background:rgba(255,255,255,.03); }
[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"]{ color:var(--spd-gold-hi); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"]{ background:var(--spd-gold) !important; height:2px; }

/* dataframes */
[data-testid="stDataFrame"]{ border:1px solid var(--spd-line); border-radius:.6rem; overflow:hidden; box-shadow:0 10px 28px -24px rgba(0,0,0,.95); }

/* inputs: gold focus ring + gold multiselect chips */
div[data-baseweb="input"]:focus-within,
div[data-baseweb="base-input"]:focus-within,
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within{ border-color:var(--spd-gold-line) !important; box-shadow:0 0 0 3px var(--spd-gold-soft) !important; }
[data-testid="stMultiSelect"] span[data-baseweb="tag"]{ background:var(--spd-gold-soft); border:1px solid var(--spd-gold-line); color:#F4E7C2; }

/* dividers */
[data-testid="stAppViewContainer"] hr{ border:none; height:1px; margin:1.1rem 0; background:linear-gradient(90deg, transparent, var(--spd-line) 18%, var(--spd-line) 82%, transparent); }

/* links */
[data-testid="stMarkdownContainer"] a{ text-decoration-thickness:1px; text-underline-offset:2px; }

/* premium thin scrollbars */
*::-webkit-scrollbar{ width:10px; height:10px; }
*::-webkit-scrollbar-track{ background:transparent; }
*::-webkit-scrollbar-thumb{ background:#2A2E36; border-radius:8px; border:2px solid var(--spd-bg); }
*::-webkit-scrollbar-thumb:hover{ background:#3A3F49; }
</style>""", unsafe_allow_html=True)

cache = load_cache()
groups = get_groups(cache)

# ---- deep-link from a 📒 note: ?open=TICKER → jump into that stock's analysis ----
_goto = st.query_params.get("open")
if _goto:
    st.session_state.sel_ticker = _goto
    st.session_state.ticker_pick = _goto             # keep the sidebar picker in sync
    st.session_state.user_picked = True
    _nav_to_stocks()
    st.session_state.setdefault("extra_tickers", set()).add(_goto)
    st.session_state.jumped_from = "📒 a note"
    try:
        del st.query_params["open"]            # consume it so a later rerun doesn't re-trigger
    except Exception:
        pass

# ---- navigation: 3 intent-named SECTIONS, each with a couple of sub-views ----
# The (section, sub-view) choice maps to the SAME internal `_mode` strings the page blocks below
# already use, so nothing downstream changes and jump-to-stock click-throughs (which set the nav
# keys via _nav_to_stocks) keep working.
_SUBVIEWS = {
    "📊 Analyze":  [("📈 Stocks", "📊 Stocks"), ("🌐 Indices", "🌐 Indices")],
    "💡 Ideas":    [("🎯 Buy setups", "💎 Investable now"), ("💰 Size a budget", "💡 Allocate ₹")],
    "⭐ Investors": [("📋 Investor list", "⭐ Superstars"), ("🔔 Recent moves", "🔔 Alerts")],
    "🌍 Markets":  [("📅 Today", "🌍 Today"), ("📉 FII/DII flow", "🌍 FII/DII"), ("🚀 IPOs", "🌍 IPOs")],
}
_SUBKEY = {"📊 Analyze": "nav_analyze", "💡 Ideas": "nav_ideas", "⭐ Investors": "nav_investors", "🌍 Markets": "nav_markets"}
with st.sidebar:
    _section = st.radio("Section", list(_SUBVIEWS), key="nav_section",
                        captions=["Look up any stock or index", "What to buy",
                                  "What the big money is doing", "The macro tape"])
    _subs = _SUBVIEWS[_section]
    _sub = st.radio("View", [lbl for lbl, _m in _subs], horizontal=True, key=_SUBKEY[_section])
    _mode = dict(_subs).get(_sub, _subs[0][1])
st.session_state["app_mode"] = _mode      # keep the legacy key in sync for any residual reader

# single heading per mode (no duplicate title)
st.title("📈 SPritamDas — " + {"🌐 Indices": "Index Analysis", "⭐ Superstars": "Superstar Analysis",
                                "🔔 Alerts": "FII/DII Alerts", "💎 Investable now": "Investable Now",
                                "💡 Allocate ₹": "Suggest & Allocate", "🌍 Today": "Markets Today",
                                "🌍 FII/DII": "FII / DII Flow", "🌍 IPOs": "IPO Dashboard"}.get(_mode, "Stock Analysis"))
render_floating_calculator()   # 🧮 draggable, always-on-top calculator (hovers over the chart)
render_floating_notes()        # 📒 draggable, always-on-top notes (context-aware, persists in the browser)

# ============================================================================
# 💡 ALLOCATE — score & size a budget across superstar-backed stocks
# ============================================================================
if _mode == "💡 Allocate ₹":
    st.markdown("## 💡 Suggest & allocate — best names for your budget (superstars × your strategies)")
    st.caption("Ranks stocks by **quality-guru conviction** (BUY/STRONG-BUY · Sharpe ≥ 0.4 · Max DD ≥ −40, "
               "alpha-weighted) × **V-class** × **live strategy setup** × entry.  **Not advice — verify before buying.**")
    _sc = superstar_stock_scores()

    # ---- strategy layer: best live setup per ticker (V-universe backtest engine) ----
    try:
        _invt = build_investable_table((cache.get("built", "none") if cache else "none"))
    except Exception:
        _invt = pd.DataFrame()
    _setup = pd.DataFrame()
    if not _invt.empty and "Ticker" in _invt.columns:
        _iv = _invt.copy()
        _iv["_ep"] = pd.to_numeric(_iv.get("Exp Profit %"), errors="coerce").fillna(0)
        _iv["_sr"] = pd.to_numeric(_iv.get("Success %"), errors="coerce").fillna(0)
        _iv["_ev"] = _iv["_ep"] * _iv["_sr"] / 100.0
        _iv = _iv.sort_values("_ev", ascending=False).groupby("Ticker", as_index=False).first()
        _setup = _iv.rename(columns={"Ticker": "ticker", "Status": "setup", "Strategy": "strategy",
                                     "Entry": "entry", "Target": "target", "_ep": "exp_profit",
                                     "_sr": "success", "_ev": "setup_ev"})
        _setup["ticker"] = _setup["ticker"].astype(str)
        _setup = _setup[["ticker", "setup", "strategy", "entry", "target", "exp_profit", "success", "setup_ev"]]

    # ---- V-class tier bonus per ticker ----
    _vpts = {"v_40": 15, "v_40_next": 10, "v_200": 5}
    _vtier, _vlabel = {}, {}
    for _g in ("v_40", "v_40_next", "v_200"):
        for _t in (groups.get(_g, []) if groups else []):
            _t = str(_t)
            if _t not in _vtier:
                _vtier[_t] = _vpts[_g]; _vlabel[_t] = GROUP_LABELS.get(_g, _g)

    if _sc.empty and _setup.empty:
        st.warning("No data yet — run the notebook (superstar feeds) and build the price cache from **📊 Stocks**.")
        st.stop()

    _c = st.columns([2, 2])
    _budget = _c[0].number_input("Budget ₹", value=50000, min_value=1000, step=5000)
    _profile = _c[1].selectbox("Risk profile", ["Balanced", "Conservative", "Aggressive"],
                               help="Balanced ~8 · Conservative liquid/diversified ~12 · Aggressive concentrated ~5")
    with st.expander("⚙️ Advanced filters — how many · universe · only-setups · only-bargains", expanded=False):
        _n = st.number_input("How many stocks",
                             value={"Balanced": 8, "Conservative": 12, "Aggressive": 5}[_profile],
                             min_value=3, max_value=20, step=1)
        _univ = st.selectbox("Universe", ["All superstar-held", "V-universe only"],
                             help="All = every stock the quality gurus hold (wider; non-V names ride on the "
                                  "superstar signal alone, no fundamental/strategy vetting). V-universe only = "
                                  "restrict to your vetted V40 / V40-N / V200 names.")
        _need_setup = st.checkbox("Only names with a live strategy setup", value=False,
                                  help="On → every pick has a defined entry/target/expected-profit from your backtests.")
        _value_only = st.checkbox("💎 Only bargains — profits rising but price has fallen (~6 months)", value=False,
                                  help="Keep only names whose revenue & profit are rising while the price has FALLEN "
                                       "over ~6 months — fundamentally improving but out of favour.")

    # ---- build the candidate universe: superstar stocks ∪ live-setup stocks ----
    if not _sc.empty:
        base = _sc.copy()
    else:
        base = pd.DataFrame(columns=["ticker", "company", "n_holders", "consensus", "flow",
                                     "recent_action", "held_by", "last_buy_price", "last_buy_by"])
    base["ticker"] = base["ticker"].astype(str)
    if not _setup.empty:
        _miss = sorted(set(_setup["ticker"]) - set(base["ticker"]))
        if _miss:
            base = pd.concat([base, pd.DataFrame({"ticker": _miss})], ignore_index=True)
        base = base.merge(_setup, on="ticker", how="left")
    for _cc in ("consensus", "flow", "n_holders"):
        base[_cc] = pd.to_numeric(base.get(_cc), errors="coerce").fillna(0)
    if "company" not in base.columns:
        base["company"] = ""
    base["company"] = base["company"].fillna("").replace("", pd.NA).fillna(base["ticker"])
    base["vbonus"] = base["ticker"].map(_vtier).fillna(0)
    base["vclass"] = base["ticker"].map(_vlabel).fillna("")
    if str(_univ).startswith("V"):                          # restrict to the vetted V-universe if chosen
        base = base[base["vbonus"] > 0]
    _ev = pd.to_numeric(base.get("setup_ev"), errors="coerce").fillna(0) if "setup_ev" in base.columns else pd.Series(0, index=base.index)
    base["setup_bonus"] = (_ev / (_ev.max() or 1) * 22) + (base.get("setup", pd.Series("", index=base.index))
                                                            .astype(str).str.contains("READY").astype(float) * 6)
    if _need_setup:
        base = base[base.get("setup", pd.Series("", index=base.index)).astype(str).str.len() > 0]
    # profile pre-filter
    if _profile == "Conservative":
        _tv = pd.to_numeric(base.get("total_value_cr"), errors="coerce").fillna(0)
        base = base[(base["n_holders"] >= 3) | (base["vbonus"] >= 10) | (base["setup_bonus"] > 0)]
    if base.empty:
        st.info("No candidates matched this profile / filter."); st.stop()

    base["_pre"] = base["consensus"] + base["flow"] + base["vbonus"] + base["setup_bonus"]
    base = base.sort_values("_pre", ascending=False).head(40).copy()

    with st.spinner("Fetching live prices for the top candidates…"):
        base["price"] = base["ticker"].map(lambda t: _cur_price(t))
    base = base[base["price"].notna() & (base["price"] > 0)].copy()
    if base.empty:
        st.info("No priced candidates (many small/BSE-only names lack an NSE quote)."); st.stop()

    # --- value signal: revenue & profit rising while price is down (~6mo) — the guru accumulation pattern ---
    def _fund_for(t):
        _cf = (cache or {}).get("fund", {}).get(t)          # V-universe fundamentals are pre-cached (free)
        if _cf:
            return _cf
        try:
            return fetch_fund(t) or {}
        except Exception:
            return {}
    def _mom6(t):
        _df = (cache or {}).get("data", {}).get(t)          # reuse cached price history when available
        if _df is None or "Close" not in getattr(_df, "columns", []):
            try:
                _df = fetch_one(t, 1)
            except Exception:
                _df = None
        if _df is None or "Close" not in getattr(_df, "columns", []):
            return None
        _cc = _df["Close"].dropna()
        if len(_cc) < 40:
            return None
        _k = 126 if len(_cc) >= 126 else len(_cc) - 1       # ~6 trading months
        return (float(_cc.iloc[-1]) / float(_cc.iloc[-_k]) - 1) * 100
    with st.spinner("Scanning fundamentals vs price (value setups)…"):
        _vmap = {}
        for _t in base["ticker"]:
            _fd = _fund_for(_t); _m6 = _mom6(_t)
            _rev_up = ((_fd.get("sales_growth_pct") or 0) > 0) or bool(_fd.get("ttm_revenue_is_highest"))
            _pft_up = ((_fd.get("profit_growth_pct") or 0) > 0) or bool(_fd.get("ttm_netprofit_is_highest"))
            _vmap[_t] = (bool(_rev_up and _pft_up and _m6 is not None and _m6 < 0), _m6)
    base["mom6"] = base["ticker"].map(lambda t: _vmap.get(t, (False, None))[1])
    base["value_setup"] = base["ticker"].map(lambda t: _vmap.get(t, (False, None))[0])
    base["value_bonus"] = base["value_setup"].astype(float) * 14.0
    if _value_only:
        base = base[base["value_setup"]].copy()
        if base.empty:
            st.info("No 'earnings-up, price-down' setups among the candidates right now — loosen the filters."); st.stop()

    _lbp = pd.to_numeric(base.get("last_buy_price"), errors="coerce") if "last_buy_price" in base.columns else pd.Series(pd.NA, index=base.index)
    base["runup_pct"] = [((p - b) / b * 100) if (pd.notna(b) and b) else None for p, b in zip(base["price"], _lbp)]
    _pen = {"Conservative": 2.0, "Balanced": 1.0, "Aggressive": 0.6}[_profile]
    base["entry_score"] = [12.0 if ru is None or pd.isna(ru) else float(max(0, 20 - max(0, ru) * 0.2 * _pen))
                           for ru in base["runup_pct"]]
    _fmult = {"Aggressive": 1.6, "Balanced": 1.0, "Conservative": 0.8}[_profile]
    base["score"] = (base["consensus"] + base["flow"] * _fmult + base["vbonus"] + base["setup_bonus"]
                     + base["entry_score"] + base["value_bonus"])
    _top = base.sort_values("score", ascending=False).head(int(_n)).copy()

    _w = _top["score"].clip(lower=0.1); _w = _w / _w.sum(); _w = _w.clip(upper=0.25); _w = _w / _w.sum()
    _top["target_inr"] = _w.values * _budget
    _top["shares"] = (_top["target_inr"] // _top["price"]).astype(int)
    _top["invest_inr"] = (_top["shares"] * _top["price"]).round(0)
    _top["weight_pct"] = (_top["invest_inr"] / _budget * 100).round(1)
    _dep = _top["invest_inr"].sum(); _left = _budget - _dep

    def _why(r):
        bits = []
        _su = str(r.get("setup", "") or "").strip()
        if _su and _su.lower() != "nan":
            _stg = str(r.get("strategy", "") or "").strip()
            _t = f"{_su} — {_stg} strategy" if _stg else _su
            _ep, _sr = r.get("exp_profit"), r.get("success")
            if pd.notna(_ep) and pd.notna(_sr):
                _t += f" (target +{_ep:.0f}% · {_sr:.0f}% win-rate)"
            elif pd.notna(_ep):
                _t += f" (target +{_ep:.0f}%)"
            bits.append(_t)
        else:
            bits.append("no live strategy setup — quality/superstar signal only")
        if isinstance(r.get("vclass"), str) and r["vclass"]:
            bits.append(f"{r['vclass']} class")
        if r.get("n_holders", 0) >= 1:
            _g = f"{int(r['n_holders'])} quality gurus"
            if isinstance(r.get("held_by"), str) and r["held_by"].strip():
                _g += " (" + ", ".join([x.strip() for x in str(r["held_by"]).split(",")[:2]]) + ")"
            bits.append(_g)
        if isinstance(r.get("recent_action"), str) and r["recent_action"].strip():
            bits.append(r["recent_action"])
        ru = r.get("runup_pct")
        if pd.notna(ru):
            bits.append(f"{'+' if ru >= 0 else ''}{ru:.0f}% since {str(r.get('last_buy_by', 'guru')).title()}'s buy")
        return " · ".join(bits)
    _top["why"] = _top.apply(_why, axis=1)

    st.markdown(f"### 📋 {_profile} plan for ₹{_budget:,.0f} — {len(_top)} stocks · "
                f"₹{_dep:,.0f} deployed · ₹{_left:,.0f} cash left (rounding)")
    _cols = [c for c in ["company", "ticker", "vclass", "price", "invest_inr", "shares", "weight_pct",
                         "strategy", "setup", "exp_profit", "success", "score"] if c in _top.columns]
    st.dataframe(filter_ui(_top[_cols], "top_movers"), hide_index=True, use_container_width=True, column_config={
        "company": st.column_config.TextColumn("Stock"), "ticker": st.column_config.TextColumn("Ticker"),
        "vclass": st.column_config.TextColumn("Class"),
        "price": st.column_config.NumberColumn("Price ₹", format="%.1f"),
        "invest_inr": st.column_config.NumberColumn("Invest ₹", format="%.0f"),
        "shares": st.column_config.NumberColumn("Shares"),
        "weight_pct": st.column_config.NumberColumn("Weight", format="%.1f%%"),
        "strategy": st.column_config.TextColumn("Strategy", help="The backtested strategy with a live setup on this name (blank = none)."),
        "setup": st.column_config.TextColumn("Setup"),
        "exp_profit": st.column_config.NumberColumn("Exp +%", format="%.0f"),
        "success": st.column_config.NumberColumn("Win %", format="%.0f"),
        "score": st.column_config.NumberColumn("Score", format="%.0f"),
        "why": st.column_config.TextColumn("Why", width="large")})
    # ---- detailed per-pick briefing: holders · who added/trimmed · fundamentals · watch-outs ----
    _sd = fetch_superstar_summary().reset_index(drop=True)
    _qm = {}                                            # qualifying investor -> (signal, alpha)
    if not _sd.empty and "name" in _sd.columns:
        _shq = pd.to_numeric(_sd.get("sharpe_ratio"), errors="coerce")
        _ddq = pd.to_numeric(_sd.get("max_drawdown_pct"), errors="coerce")
        _alq = pd.to_numeric(_sd.get("alpha_ann_pct"), errors="coerce")
        for _i in range(len(_sd)):
            if (_sd.loc[_i, "signal"] in ("STRONG BUY", "BUY")
                    and pd.notna(_shq[_i]) and _shq[_i] >= 0.4 and pd.notna(_ddq[_i]) and _ddq[_i] >= -40):
                _qm[str(_sd.loc[_i, "name"]).strip().lower()] = (_sd.loc[_i, "signal"], _alq[_i])
    _mv_all = fetch_superstar_moves()
    if not _mv_all.empty and "ticker" in _mv_all.columns:
        _mv_all = _mv_all.copy(); _mv_all["ticker"] = _mv_all["ticker"].astype(str)
    else:
        _mv_all = pd.DataFrame(columns=["ticker", "investor", "move", "delta"])

    st.markdown("### 💬 Why these picks — full briefing (click to expand)")
    with st.spinner("Building briefings (holders · fundamentals)…"):
        for _, r in _top.iterrows():
            _tk = str(r["ticker"]); _cl = f" · {r['vclass']}" if isinstance(r.get("vclass"), str) and r["vclass"] else ""
            _su = str(r.get("setup", "") or "").strip()
            _hdr = (f"{r.get('company', _tk)}  ({_tk}{_cl})  ·  ₹{r.get('invest_inr', 0):,.0f} · "
                    f"{int(r.get('shares', 0))} sh · {r.get('weight_pct', 0):.1f}%  ·  "
                    f"{_su if _su and _su.lower() != 'nan' else 'no setup'}  ·  score {r.get('score', 0):.0f}")
            with st.expander(_hdr):
                # strategy
                if _su and _su.lower() != "nan":
                    _l = f"**📈 Strategy — {r.get('strategy','')}**: {_su}"
                    if pd.notna(r.get("exp_profit")) and pd.notna(r.get("success")):
                        _l += f" · target **+{r.get('exp_profit'):.0f}%** at **{r.get('success'):.0f}%** win-rate"
                    st.markdown(_l)
                else:
                    st.markdown("**📈 Strategy**: no live setup — on the superstar/quality signal only.")
                # who holds / added / trimmed (qualifying gurus only)
                _mt = _mv_all[_mv_all["ticker"] == _tk] if "ticker" in _mv_all.columns else _mv_all.iloc[0:0]
                _adds, _trims = [], []
                for _, m in _mt.iterrows():
                    _inv = str(m.get("investor", "")).strip().lower()
                    if _inv not in _qm:
                        continue
                    _d = pd.to_numeric(m.get("delta"), errors="coerce")
                    _tag = str(m.get("investor", "")).title() + (f" ({'+' if pd.notna(_d) and _d >= 0 else ''}{_d:.1f}%)" if pd.notna(_d) else "")
                    (_adds if str(m.get("move", "")).upper() in ("NEW", "ADD") else _trims).append((_qm[_inv][1], _tag)) \
                        if str(m.get("move", "")).upper() in ("NEW", "ADD", "TRIM", "EXIT") else None
                st.markdown(f"**⭐ Quality gurus holding: {int(r.get('n_holders', 0))}**")
                if _adds:
                    st.markdown("- 🟢 **Adding / new:** " + ", ".join(t for _, t in sorted(_adds, reverse=True)))
                if _trims:
                    st.markdown("- 🔴 **Trimming / exiting:** " + ", ".join(t for _, t in sorted(_trims, reverse=True)))
                if not _adds and not _trims and isinstance(r.get("held_by"), str) and r["held_by"].strip():
                    st.markdown(f"- holding steady: {r['held_by']}")
                _ru = r.get("runup_pct")
                if pd.notna(_ru):
                    st.markdown(f"- price is **{'+' if _ru >= 0 else ''}{_ru:.0f}%** vs the gurus' recent buy")
                # fundamentals (best-effort)
                try:
                    _fd = fetch_fund(_tk) or {}
                except Exception:
                    _fd = {}
                if _fd:
                    _fb = []
                    for _k, _lab, _sfx in [("pe_trailing", "P/E", ""), ("roe_pct", "ROE", "%"),
                                           ("debt_to_equity", "D/E", ""), ("sales_growth_pct", "Rev gr", "%"),
                                           ("profit_growth_pct", "Profit gr", "%")]:
                        _v = _fd.get(_k)
                        if isinstance(_v, (int, float)) and pd.notna(_v):
                            _fb.append(f"{_lab} {_v:.1f}{_sfx}")
                    _fl = []
                    if _fd.get("ttm_revenue_is_highest"): _fl.append("revenue at all-time high")
                    if _fd.get("ttm_netprofit_is_highest"): _fl.append("profit at all-time high")
                    if _fd.get("good_track_record"): _fl.append("consistent profit growth")
                    _fstr = " · ".join(_fb) + ((" · " + ", ".join(_fl)) if _fl else "")
                    if _fstr.strip(" ·"):
                        st.markdown(f"**📊 Fundamentals:** {_fstr}")
                if r.get("value_setup"):
                    _m6r = r.get("mom6")
                    _m6s = f"{_m6r:.0f}% over ~6 months" if isinstance(_m6r, (int, float)) and pd.notna(_m6r) else "down"
                    st.markdown(f"**💎 Value setup:** revenue & profit rising but price **{_m6s}** — earnings up, "
                                "price down (the pattern the pro gurus quietly accumulate).")
                # watch-outs
                _w = []
                if pd.notna(_ru) and _ru > 40:
                    _w.append(f"already up {_ru:.0f}% since the gurus bought — entry less attractive")
                if _trims and len(_trims) >= len(_adds):
                    _w.append("as many quality gurus trimming as adding")
                if _su and pd.notna(r.get("success")) and r.get("success") < 40:
                    _w.append(f"low backtest win-rate ({r.get('success'):.0f}%)")
                if not (isinstance(r.get("vclass"), str) and r["vclass"]):
                    _w.append("not in your V-universe — unvetted; check fundamentals yourself")
                if int(r.get("shares", 0)) == 0:
                    _w.append("too pricey for this budget slice (0 shares)")
                _de = _fd.get("debt_to_equity")
                if isinstance(_de, (int, float)) and pd.notna(_de) and _de > 1.5:
                    _w.append(f"elevated debt (D/E {_de:.1f})")
                _pe = _fd.get("pe_trailing")
                if isinstance(_pe, (int, float)) and pd.notna(_pe) and _pe > 80:
                    _w.append(f"rich valuation (P/E {_pe:.0f})")
                _pg = _fd.get("profit_growth_pct")
                if isinstance(_pg, (int, float)) and pd.notna(_pg) and _pg < 0:
                    _w.append("profit declining (latest)")
                if not _fd:
                    _w.append("no fundamentals available (thin / BSE-only name)")
                if _w:
                    st.markdown("**⚠️ Watch-outs:** " + " · ".join(_w))
    # open any pick in Stock Analysis (button on_click sets the mode BEFORE widgets instantiate — safe)
    _oc = st.columns([4, 2])
    _opts = ["—"] + [f"{r['ticker']} · {r['company']}" for _, r in _top.iterrows()]
    _pick_open = _oc[0].selectbox("Open a pick in 📊 Stock Analysis (chart · backtest · fundamentals)",
                                  _opts, key="alloc_open_pick")
    _oc[1].markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
    _oc[1].button("📈 Open", use_container_width=True, disabled=(_pick_open == "—"),
                  on_click=_open_stock,
                  args=((_pick_open.split(" · ")[0] if _pick_open != "—" else ""), "💡 Allocate ₹"))
    st.caption("**Score** = superstar conviction (quality-weighted consensus + recent buying) + **V-class** "
               "(V40>V40-N>V200) + **live setup** (expected profit × success, READY>REVIEW) + **entry** "
               "(penalises run-ups since the gurus' buys). Weights ∝ score, capped 25%/stock, whole shares "
               "at live price. **Verify each name in 📊 Stocks before buying — this is a screen, not advice.**")
    st.stop()

# ============================================================================
# 💎 INVESTABLE NOW MODE  — master table of every actionable (READY/REVIEW) setup
# ============================================================================
if _mode == "💎 Investable now":
    st.markdown("## 💎 Investable now — every actionable setup (one row per stock · strategy)")
    if not cache or not cache.get("data"):
        st.warning("No price cache loaded yet — the nightly data build hasn't run, or it's still "
                   "downloading. Try again in a moment, or build it from the **📊 Stocks** sidebar.")
        st.stop()
    _inv = build_investable_table((cache.get("built", "none") if cache else "none"))
    if _inv.empty:
        st.info("Nothing is **READY** or **REVIEW** across the cached universe right now "
                "(strategies: **V20 · Lifetime High · 52-Week Low**). Check back after the next 5 PM refresh.")
        st.stop()
    _ready_n = int((_inv["Status"] == "🟢 READY").sum())
    _rev_n = int((_inv["Status"] == "🟡 REVIEW").sum())
    st.caption(f"**{len(_inv)}** setups — 🟢 **{_ready_n}** READY · 🟡 **{_rev_n}** REVIEW · across "
               f"**{_inv['Ticker'].nunique()}** stocks · strategies: **V20 · Lifetime High · 52-Week Low**. "
               "Tick **✓** as you review each · click any column header to sort.")
    _icols = ["Reviewed", "Ticker", "Group", "Strategy", "Status", "Entry", "Target", "Exp Profit %",
              "Success %", "Exp. days", "Median days", "Opportunities", "Avg Win %", "Last Opp"]
    _inv = filter_ui(_inv, "investable")     # per-column filter (Group/Strategy/Status/Entry/Success%/…)
    _iedit = st.data_editor(
        _inv, hide_index=True, use_container_width=True, height=620, column_order=_icols,
        disabled=[c for c in _icols if c != "Reviewed"],
        key="investable_editor_" + hashlib.md5("|".join((_inv["Ticker"] + "·" + _inv["Strategy"]).astype(str)).encode()).hexdigest()[:8],
        column_config={
            "Reviewed": st.column_config.CheckboxColumn("✓", width="small", help="Tick the ones you've reviewed."),
            "Ticker": st.column_config.TextColumn("Ticker", width="small"),
            "Group": st.column_config.TextColumn("Group", width="small"),
            "Strategy": st.column_config.TextColumn("Strategy"),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Entry": st.column_config.NumberColumn("Entry", format="%.2f"),
            "Target": st.column_config.NumberColumn("Target", format="%.2f"),
            "Exp Profit %": st.column_config.NumberColumn("Exp Profit %", format="%.1f"),
            "Success %": st.column_config.NumberColumn("Success %", format="%.1f"),
            "Exp. days": st.column_config.NumberColumn("Exp. days", format="%d"),
            "Median days": st.column_config.NumberColumn("Median days", format="%d"),
            "Opportunities": st.column_config.NumberColumn("Opps", format="%d", width="small"),
            "Avg Win %": st.column_config.NumberColumn("Avg Win %", format="%.1f"),
            "Last Opp": st.column_config.DateColumn("Last Opp"),
        })
    try:
        st.caption(f"✓ Reviewed **{int(_iedit['Reviewed'].sum())}/{len(_iedit)}** (resets when the app restarts).")
    except Exception:
        pass
    st.markdown("---")
    _ijc = st.columns([3, 1])
    _ipick = _ijc[0].selectbox("Open a stock in 📊 Stock Analysis →",
                               options=sorted(_inv["Ticker"].unique()), key="inv_jump")
    _ijc[1].markdown("&nbsp;")
    _ijc[1].button("📊 Analyze", use_container_width=True, on_click=_open_stock,
                   args=(_ipick, "💎 Investable now"))
    st.caption("Every **V-universe** company (V40 · V40 Next · V200) is checked against "
               "**V20 · Lifetime High · 52-Week Low** — the strategies these names are built for. Read entirely "
               "from the **nightly cache** (rebuilt 5 PM) — no live fetching, so it loads in seconds. "
               "**Exp./Median days** now include still-open positions' elapsed time, so long-running setups aren't hidden.")
    st.stop()

# ============================================================================
# INDICES ANALYSIS MODE  (separate page; reuses the same chart engine/indicators)
# ============================================================================
if _mode == "🌐 Indices":
    with st.sidebar:
        st.markdown("---")
        _idx = st.selectbox(
            "Index", options=core.INDICES, key="idx_sel",
            format_func=lambda i: f"{'🇮🇳' if i['region'] == 'Indian' else '🇺🇸'} {i['name']}")
        if st.button("🔄  Refresh index data", use_container_width=True,
                     help="Re-pull indices live from Yahoo right now."):
            fetch_index.clear()
            fetch_constituents.clear()
            constituents_table.clear()
            st.rerun()
        st.caption("Indices are pulled **live** from Yahoo. Auto-refresh ~6 h; **Refresh** = now.")

        # ---- constituents: count + jump-to-analysis (the full table is in the main area) ----
        _csv = getattr(core, "INDEX_CONSTITUENT_CSV", {}).get(_idx["name"])
        if _csv:
            _syms = fetch_constituents(_csv)
            if _syms:
                st.caption(f"📋 **{len(_syms)} constituents** — value-screen table below the chart →")

                def _jump_to():                          # pick one → jump to its FULL stock analysis
                    _s = st.session_state.get("idx_jump")
                    if _s and _s != "— pick a company —":
                        st.session_state.sel_ticker = _s
                        st.session_state.ticker_pick = _s        # keep the sidebar picker in sync
                        st.session_state.user_picked = True
                        _nav_to_stocks()
                        st.session_state.setdefault("extra_tickers", set()).add(_s)
                        st.session_state.jumped_from = _idx["name"]
                st.selectbox("🔎 Analyze a constituent →", ["— pick a company —"] + _syms,
                             key="idx_jump", on_change=_jump_to,
                             help="Jumps to Stocks mode and shows that company's groups, "
                                  "applicable strategies, status, backtest, chart & fundamentals.")
            else:
                st.caption("Couldn't fetch the constituent list from NSE — click **Refresh** to retry.")
        else:
            _syms = []
            st.caption("ℹ️ Constituent list available only for the NIFTY indices (not VIX / US).")

    _cands = [_idx["ticker"]] + list(_idx.get("alts") or [])
    _idf, _used = fetch_index_any(_cands)
    if _idf is None or len(_idf) < 30:
        st.error(f"Couldn't fetch **{_idx['name']}** from Yahoo — tried: "
                 + ", ".join(f"`{t}`" for t in _cands)
                 + ". None are carried by yfinance. Tell me and I'll find another source / ETF proxy.")
        st.stop()
    _idf_ind = vs.add_base_indicators(core._clean_ohlc(_idf))
    _flag = "🇮🇳" if _idx["region"] == "Indian" else "🇺🇸"
    _ia = {"skey": "index", "ticker": f"{_idx['name']} · {_used}",
           "cfg": {}, "summary": {}, "opps": pd.DataFrame(), "df": _idf_ind}
    try:                                             # detect Cup&Handle / RHS patterns on the index
        _rhs, _ = vs.strategy_rhs(_idf_ind.copy())
        _cwh, _ = vs.strategy_cup_handle(_idf_ind.copy())
        _ia["summary"]["pattern_shapes"] = ((_rhs.get("pattern_shapes") or [])
                                            + (_cwh.get("pattern_shapes") or []))
    except Exception:
        pass

    with st.container(border=True):
        st.markdown(f"### {_flag} {_idx['name']}  ·  `{_used}`  ·  {_idx['region']} index")
        st.markdown(f"**What it tracks:** {_idx['meaning']}")
        if _used != _idx["ticker"]:
            st.caption(f"_(primary `{_idx['ticker']}` unavailable on Yahoo — using fallback `{_used}`.)_")

    _cur = float(_idf_ind["Close"].iloc[-1])
    publish_context({"kind": "index", "index": _idx["name"]})   # for the 📒 notes "attach current view"
    _hi = _idf_ind["high_52w"].iloc[-1]
    _lo = _idf_ind["low_52w"].iloc[-1]
    _ath = float(_idf_ind["lifetime_high"].iloc[-1])
    _start = _idf_ind["Date"].iloc[0].date()
    with st.container(border=True):
        _r = st.columns(6)
        _r[0].metric("Level", f"{_cur:,.2f}")
        _r[1].metric("52w High", f"{_hi:,.0f}" if pd.notna(_hi) else "—")
        _r[2].metric("52w Low", f"{_lo:,.0f}" if pd.notna(_lo) else "—")
        _r[3].metric("% from 52w high", f"{(_cur - _hi) / _hi * 100:+.1f}%" if pd.notna(_hi) and _hi else "—")
        _r[4].metric("From all-time high", f"{(_cur - _ath) / _ath * 100:+.1f}%" if _ath else "—")
        _r[5].metric("History since", str(_start))
        st.caption("**All-time high** is over the fetched history (yfinance `max`). For **VIX / "
                   "India VIX** read it inverted: a *high* level = market fear → often a value zone "
                   "for the broad market, not the VIX itself.")

    # ---- Macro signals & safe zone ----
    _macro = core.index_macro(_idf_ind)
    if _macro:
        with st.container(border=True):
            st.markdown("**🧭 Macro signals & safe zone**")
            # Row 1 — the three signals, with real text values + supporting numbers
            _m = st.columns(3)
            _m[0].metric("Bottoming cross", "🟢 Active" if _macro["bottom_cross_now"] else "⚪ Inactive",
                         help="Bottom signal = 50 DMA below BOTH the 200 & 300 DMA.")
            _m[0].caption(f"50DMA **{_macro['dma50']:,.0f}** vs 200 **{_macro['dma200']:,.0f}** "
                          f"/ 300 **{_macro['dma300']:,.0f}**" if _macro.get("dma50") else "—")
            _exh = _macro.get("exhaustion_top")
            _m[1].metric("Top exhaustion", "⚠️ Yes" if _exh else "🟢 No",
                         help="Top risk = index hasn't touched its 300 DMA for ~1 year.")
            _dst = _macro.get("days_since_300dma_touch")
            _m[1].caption("at/below its 300 DMA right now → **not** exhausted" if _dst == 0
                          else (f"{_dst} trading days since last 300-DMA touch" if _dst is not None
                                else "300 DMA not established"))
            _ss = _macro.get("safe_status")
            _m[2].metric("Safe-zone ceiling",
                         f"{_macro['safe_limit']:,.0f}" if _macro.get("safe_limit") else "—",
                         delta=(f"{_macro['pct_to_safe']:+.1f}% room" if _macro.get("safe_limit") else None),
                         help="Current price vs the safe-zone ceiling. Positive room = below it = SAFE.")
            _m[2].caption({"SAFE": "🟢 SAFE — below ceiling", "UNSAFE": "🔴 UNSAFE — above ceiling"}
                          .get(_ss, "—"))
            # Row 2 — the actual levels behind the safe-zone calc
            if _macro.get("safe_limit"):
                _bm, _hh = _macro['bottom_most'], _macro['last_high']
                _recov = (_hh - _bm) / _bm * 100 if _bm else 0
                _r2 = st.columns(4)
                _r2[0].metric("Current level", f"{_macro['current']:,.0f}")
                _r2[1].metric("Bottoming-cross low", f"{_bm:,.0f}", help=_macro['bottom_date'])
                _r2[2].metric("Last high (cup rim)", f"{_hh:,.0f}", help=_macro['last_high_date'])
                _r2[3].metric("Safe-zone ceiling", f"{_macro['safe_limit']:,.0f}",
                              delta=f"+{_recov:.0f}% over last high")
                st.caption(f"**Ceiling {_macro['safe_limit']:,.0f}** = last high **{_hh:,.0f}** "
                           f"({_macro['last_high_date']}) **+ {_recov:.0f}%** — the *same % recovery* "
                           f"as bottoming-cross low **{_bm:,.0f}** ({_macro['bottom_date']}) → last high. "
                           "Below it → growth still OK; above it → unsafe / overextended.")
            else:
                st.caption("No completed bottoming cross in the history → safe-zone ceiling not "
                           "computable yet.")

    render_chart_block(_ia, "idx_" + "".join(c for c in _idx["ticker"] if c.isalnum()),
                       extend_proj=True, macro=_macro)
    st.caption("**On the chart:** the **current cycle** is the cyan line ▲ bottoming-cross low → "
               "▼ last high (the cup); the bold 🎯 dashed line is the **safe-zone ceiling**. Past "
               "cycles are faded (small ▲ + dotted ceilings). Faint **red bands** = 1-yr 300-DMA "
               "**exhaustion (top-risk)** zones. 50/200/300 DMA shown; toggle the log-CAGR **trend** "
               "lines (projected ~2 yrs) and any RHS/Cup-&-Handle patterns in the legend.")

    # ---- CONSTITUENT VALUE-SCREEN TABLE (lowest zone of the page) ----
    if _csv and _syms:
        st.divider()
        with st.container(border=True):
            st.markdown(f"### 📋 {_idx['name']} — constituent value screen ({len(_syms)} companies)")
            _ctab = constituents_table(_csv, (cache.get("built", "none") if cache else "live"))
            if _ctab.empty:
                st.caption(f"Couldn't fetch data for the {len(_syms)} constituents (network / Yahoo). "
                           "Try **🔄 Refresh index data**.")
            else:
                _ctab = _ctab.copy()                       # add a Type column right after Ticker
                _ctab.insert(1, "Type", _ctab["Ticker"].map(
                    lambda t: " · ".join(GROUP_LABELS[g] for g in _groups_of(t)) or "—"))
                # Key the table on the index + the EXACT ticker list: if the constituent set/order
                # ever changes (e.g. after a refresh), the key changes → the row selection resets,
                # so a stale row position can never silently open the wrong company.
                _ttok = hashlib.md5("|".join(map(str, _ctab["Ticker"])).encode()).hexdigest()[:10]
                _event = st.dataframe(
                    _ctab, hide_index=True, use_container_width=True, height=460,
                    on_select="rerun", selection_mode="single-row",
                    key="idx_ctab_" + "".join(c for c in _idx["name"] if c.isalnum()) + "_" + _ttok,
                    column_config={
                        "Type": st.column_config.TextColumn(
                            "Type", help="V-universe membership → which strategies are designed for it."),
                        "Price": st.column_config.NumberColumn(format="%.2f"),
                        "% below 200DMA": st.column_config.NumberColumn(
                            "% below 200DMA", format="%.1f%%",
                            help="Positive = trading BELOW its 200 DMA (value)."),
                        "% above 52w low": st.column_config.NumberColumn(
                            "% above 52w low", format="%.1f%%",
                            help="Low value = close to its 52-week low."),
                        "% below ATH": st.column_config.NumberColumn(
                            "% below ATH", format="%.1f%%",
                            help="High value = deep discount from its lifetime high."),
                        "Rev growth % YoY": st.column_config.NumberColumn(
                            "Rev growth % YoY", format="%.1f%%",
                            help="Latest annual revenue vs the prior year (from cached fundamentals)."),
                    })
                st.caption(f"Showing **{len(_ctab)}/{len(_syms)}** companies. **Type** → applicable "
                           "strategies: **V40** → all 9 · **V40-N** → all except SMA & Knoxville · "
                           "**V200** → V20 & 3×3 · **—** (not in a V-list) → 3×3 only. Click a column "
                           "header to **sort** (e.g. **% below ATH** ↓ → deepest-discount names). "
                           "**Rev growth** needs **Fundamentals in cache** ('—' = not cached).")

                # selecting a row reveals that company's applicable strategies as clickable buttons
                _sel = _event.selection["rows"] if _event and _event.selection else []
                if _sel and _sel[0] < len(_ctab):
                    _pick = str(_ctab.iloc[_sel[0]]["Ticker"])
                    _mem = _groups_of(_pick)
                    _appl = _applicable_strats(_mem)
                    _typ = " · ".join(GROUP_LABELS[g] for g in _mem) if _mem else "— (not in a V-list)"
                    # current READY status of each applicable strategy (same engine/cache as Stocks)
                    try:
                        _esig = f"{os.path.getmtime(vs.__file__):.0f}.{os.path.getmtime(core.__file__):.0f}"
                    except Exception:
                        _esig = "0"
                    _itok = (cache.get("built", "none") if cache else "none") + "|" + _esig
                    with st.spinner(f"Checking each strategy's current signal for {_pick}…"):
                        _iprof = multi_strategy_status(_pick, _itok)
                    _l2k = {v: k for k, v in core.STRATEGY_LABELS.items()}
                    _stat = {_l2k.get(r["Strategy"], r["Strategy"]): r["Status"]
                             for r in (_iprof.get("rows") or [])}
                    _TAG = {"YES": "sgrdy", "REVIEW": "sgrev", "NO": "sgnot"}
                    _DOT = {"sgrdy": "🟢", "sgrev": "🟡", "sgnot": "🔴", "sgneu": "⚪"}
                    with st.container(border=True):
                        st.markdown(f"#### 🎯 {_pick}  ·  type **{_typ}**")
                        st.markdown("**Open in a strategy** → click to jump straight to it in "
                                    "**Stock Analysis**. Colour = its **current signal**:")
                        _bc = st.columns(3)
                        for _i, _s in enumerate(_appl):
                            _tag = _TAG.get(_stat.get(_s), "sgneu")     # green/yellow/red, gray if unknown
                            _bc[_i % 3].button(
                                f"{_DOT[_tag]} " + core.STRATEGY_LABELS.get(_s, _s),
                                key=f"{_tag}_openstrat_{_pick}_{_s}", use_container_width=True,
                                on_click=_open_in_strategy, args=(_pick, _s, _idx["name"]))
                        st.caption("🟢 **ready to invest now** · 🟡 **review** (needs a manual look — e.g. "
                                   "fundamentals) · 🔴 **not now** · ⚪ couldn't evaluate (insufficient "
                                   "history/data). Each button opens **this stock in that strategy** "
                                   "(chart · backtest · fundamentals); the sidebar **🔎 Analyze a "
                                   "constituent** shows the full profile.")
                else:
                    st.info("👆 **Click a company's row** above to see its applicable strategies — each "
                            "button is **🟢 ready / 🟡 review / 🔴 not now** and opens that stock directly "
                            "in that strategy.")

    st.stop()

# ============================================================================
# SUPERSTAR ANALYSIS MODE — investor list (+ metrics) → portfolio holdings journey
# ============================================================================
if _mode == "⭐ Superstars":
    _sdf = fetch_superstar_summary()
    with st.sidebar:
        st.markdown("---")
        if st.button("🔄  Refresh superstar data", use_container_width=True,
                     help="Re-read the investor summary + re-scrape holdings."):
            for _f in (fetch_superstar_summary, fetch_superstar_holdings, fetch_superstar_bulkblock,
                       fetch_superstar_sast, fetch_superstar_insider, fetch_market_bulkblock):
                try: _f.clear()
                except Exception: pass
            st.rerun()
        st.caption("List, metrics & deal feeds from the FII/DII sheet (run the notebook to refresh). "
                   "Click above to reload all tabs after a refresh.")

    if _sdf.empty or "name" not in _sdf.columns:
        st.warning("No superstar data found. Run the **fii_dii_investment_pattern.ipynb** notebook "
                   "(India: scrape → metrics) to populate `fii_dii_indian_investment_summary`, then "
                   "click **🔄 Refresh superstar data**.")
        st.stop()

    with st.container(border=True):
        st.markdown(f"### ⭐ Indian superstar investors — {len(_sdf)} tracked · vs **Nifty 50**")
        st.markdown("**Click an investor's row** to open their **portfolio holdings journey** — every "
                    "disclosed position and how the **% stake** moved each quarter "
                    "(🟢 new / added · 🔴 trimmed / exited).")
        # ---- Nifty 50 (NSE) benchmark reference — set your filter thresholds against these ----
        def _bmed(col):
            return pd.to_numeric(_sdf[col], errors="coerce").median() if col in _sdf.columns else None
        _bm = {k: _bmed(f"nifty_{k}") for k in
               ("sharpe_ratio", "ann_return_pct", "max_drawdown_pct", "volatility_ann_pct")}
        if _bm["sharpe_ratio"] is not None and pd.notna(_bm["sharpe_ratio"]):
            _bc = st.columns(4)
            _bc[0].metric("Nifty 50 · Sharpe", f"{_bm['sharpe_ratio']:.2f}")
            _bc[1].metric("Nifty 50 · Ann ret", f"{_bm['ann_return_pct']:.1f}%" if _bm['ann_return_pct'] is not None else "—")
            _bc[2].metric("Nifty 50 · Max DD", f"{_bm['max_drawdown_pct']:.1f}%" if _bm['max_drawdown_pct'] is not None else "—")
            _bc[3].metric("Nifty 50 · Volatility", f"{_bm['volatility_ann_pct']:.1f}%" if _bm['volatility_ann_pct'] is not None else "—")
            st.caption("📊 **NSE Nifty 50 benchmark** (median over the tracked windows) — the bar to beat. "
                       "Set **Min Sharpe** above its Sharpe, **Max DD ≥** above its drawdown, etc. Each investor "
                       "row also shows its own window-matched **Nifty Sharpe / Ann / Max DD** for a direct compare.")

    # ---- 📅 market-wide bulk & block browser (all-market, not just superstars) ----
    _mkt = fetch_market_bulkblock()
    if not _mkt.empty and "symbol" in _mkt.columns:
        _mkt = _mkt.copy()
        if "exchange" not in _mkt.columns:          # legacy rows (pre-BSE) were all NSE
            _mkt["exchange"] = "NSE"
        _mkt["exchange"] = _mkt["exchange"].astype(str).str.upper().replace({"": "NSE", "NAN": "NSE"})
        # the notebook writes canonical ISO ("YYYY-MM-DD") for both exchanges → parse WITHOUT dayfirst
        # (dayfirst=True mangles ISO: "2026-07-08" → 2026-08-07).
        _mkt["_dt"] = pd.to_datetime(_mkt["date"].astype(str).str.strip(), errors="coerce")
        _latest_d = _mkt["_dt"].max()
        _exs = sorted(_mkt["exchange"].dropna().unique())
        with st.expander(f"📅  Today's / recent bulk & block deals — market-wide (NSE + BSE)  ·  {len(_mkt)} deals "
                         f"(latest {_latest_d.date() if pd.notna(_latest_d) else '—'})"):
            _mc1 = st.columns([3, 2, 2, 2])
            _mq = _mc1[0].text_input("🔎 Search stock / client", key="mkt_q").strip().lower()
            _days = [str(pd.Timestamp(d).date()) for d in sorted(_mkt["_dt"].dropna().unique(), reverse=True)[:10]]
            _mdt = _mc1[1].selectbox("Day", ["All"] + _days, key="mkt_day")
            _mexch = _mc1[2].multiselect("Exchange", _exs, key="mkt_exch")
            _mside = _mc1[3].multiselect("Side", ["BUY", "SELL"], key="mkt_side")
            _mv = _mkt
            if _mq:
                _mv = _mv[_mv.apply(lambda r: _mq in str(r.get("symbol", "")).lower()
                                    or _mq in str(r.get("company", "")).lower()
                                    or _mq in str(r.get("client", "")).lower(), axis=1)]
            if _mdt != "All":
                _mv = _mv[_mv["_dt"].astype(str).str.startswith(_mdt)]
            if _mexch:
                _mv = _mv[_mv["exchange"].isin(_mexch)]
            if _mside:
                _mv = _mv[_mv["action"].astype(str).str.upper().isin(_mside)]
            _mv = _mv.sort_values("_dt", ascending=False)
            _nn, _nb = int((_mv["exchange"] == "NSE").sum()), int((_mv["exchange"] == "BSE").sum())
            st.caption(f"Showing **{len(_mv)}** of {len(_mkt)} market-wide deals (NSE {_nn} · BSE {_nb}). "
                       "Refreshed nightly by the notebook. BSE stocks show their BSE ticker/short name.")
            st.dataframe(
                filter_ui(_mv[[c for c in ["date", "exchange", "symbol", "company", "client", "action", "qty", "price", "deal_type"]
                     if c in _mv.columns]], "mkt_deals"),
                hide_index=True, use_container_width=True, height=360, column_config={
                    "date": st.column_config.TextColumn("Date"), "exchange": st.column_config.TextColumn("Exch"),
                    "symbol": st.column_config.TextColumn("Symbol"),
                    "company": st.column_config.TextColumn("Stock"), "client": st.column_config.TextColumn("Client"),
                    "action": st.column_config.TextColumn("Side"), "qty": st.column_config.TextColumn("Qty"),
                    "price": st.column_config.TextColumn("Price ₹"), "deal_type": st.column_config.TextColumn("Type")})

    # ---- filters (combine with AND; numeric ones apply only when you enter a value) ----
    def _ncol(df, c):
        return pd.to_numeric(df[c], errors="coerce") if c in df.columns else pd.Series(float("nan"), index=df.index)
    _f1 = st.columns([3, 3, 2])
    _q = _f1[0].text_input("🔎 Search investor", key="sstar_q").strip().lower()
    _sig_present = [s for s in ["STRONG BUY", "BUY", "WATCH", "HOLD", "AVOID"]
                    if "signal" in _sdf.columns and s in set(_sdf["signal"].astype(str))]
    _sigs = _f1[1].multiselect("Signal (any of)", _sig_present, key="sstar_sig")
    _type_present = sorted(_sdf["type"].dropna().astype(str).unique()) if "type" in _sdf.columns else []
    _types = _f1[2].multiselect("Type", _type_present, key="sstar_type",
                                help="Individual investor (guru) vs institutional (fund/AMC).")
    with st.expander("⚙️ Advanced filters — Sharpe · Alpha · Return · Max drawdown", expanded=False):
        _f2 = st.columns(4)
        _minsh = _f2[0].number_input("Min Sharpe", value=None, step=0.1, format="%.2f", key="sstar_minsh",
                                     placeholder="—")
        _minal = _f2[1].number_input("Min Alpha %", value=None, step=1.0, key="sstar_minal", placeholder="—")
        _minann = _f2[2].number_input("Min Ann ret %", value=None, step=1.0, key="sstar_minann", placeholder="—")
        _mindd = _f2[3].number_input("Max DD ≥ %", value=None, step=5.0, key="sstar_mindd", placeholder="—",
                                     help="Drawdown not worse than this (e.g. -40 hides names that fell past -40%).")
    view = _sdf.copy()
    if _q:
        view = view[view["name"].astype(str).str.lower().str.contains(_q, na=False)]
    if _sigs and "signal" in view.columns:
        view = view[view["signal"].astype(str).isin(_sigs)]
    if _types and "type" in view.columns:
        view = view[view["type"].astype(str).isin(_types)]
    if _minsh is not None:
        view = view[_ncol(view, "sharpe_ratio") >= _minsh]
    if _minal is not None:
        view = view[_ncol(view, "alpha_ann_pct") >= _minal]
    if _minann is not None:
        view = view[_ncol(view, "ann_return_pct") >= _minann]
    if _mindd is not None:
        view = view[_ncol(view, "max_drawdown_pct") >= _mindd]
    st.caption(f"Showing **{len(view)}/{len(_sdf)}** investors. Filters combine with **AND**; numeric "
               "filters apply only when you enter a value. (e.g. Signal = BUY + STRONG BUY · Min Sharpe 0.5 "
               "· Max DD ≥ -40 → only strong, risk-efficient names that never crashed past -40%.)")

    # ---- benchmark-relative readability (Option B): Sharpe as a MULTIPLE of the Nifty's Sharpe,
    #      plus the (already-computed) Information Ratio — both read correctly without the misleading
    #      absolute 0–3 Sharpe scale. (Raw Sharpe stays in the table too.)
    if "sharpe_ratio" in view.columns and "nifty_sharpe_ratio" in view.columns:
        _bsh = pd.to_numeric(view["nifty_sharpe_ratio"], errors="coerce")
        _ish = pd.to_numeric(view["sharpe_ratio"], errors="coerce")
        view["sharpe_x_nifty"] = (_ish / _bsh).where(_bsh > 1e-9).round(1)   # e.g. 0.84/0.12 ≈ 7.0×

    # ---- investor list table (sortable, row-selectable) ----
    _adv_cols = st.checkbox("Show advanced columns (Nifty comparisons · raw Sharpe · Info ratio · rank · quarters)",
                            value=False, key="sstar_advcols")
    _core_cols = ["name", "signal", "type", "sharpe_x_nifty", "alpha_ann_pct",
                  "ann_return_pct", "max_drawdown_pct", "current_net_worth_cr"]
    _adv_extra = ["confidence_score", "score_vs_benchmark", "sharpe_ratio", "nifty_sharpe_ratio",
                  "information_ratio", "nifty_ann_return_pct", "rolling_1y_pct",
                  "nifty_max_drawdown_pct", "quarters_tracked"]
    _wanted = _core_cols + (_adv_extra if _adv_cols else [])
    _disp = view[[c for c in _wanted if c in view.columns]].reset_index(drop=True)
    _ltok = hashlib.md5("|".join(_disp["name"].astype(str)).encode()).hexdigest()[:10]   # reset selection if list changes
    _ev = st.dataframe(
        _disp, hide_index=True, use_container_width=True, height=420,
        on_select="rerun", selection_mode="single-row", key="sstar_list_" + _ltok,
        column_config={
            "name": st.column_config.TextColumn("Investor"),
            "type": st.column_config.TextColumn("Type"),
            "signal": st.column_config.TextColumn("Signal"),
            "score_vs_benchmark": st.column_config.TextColumn("Rank score"),
            "quarters_tracked": st.column_config.NumberColumn("Quarters", format="%d"),
            "confidence_score": st.column_config.NumberColumn("Conf", format="%d"),
            "alpha_ann_pct": st.column_config.NumberColumn("Alpha %", format="%.1f"),
            "sharpe_ratio": st.column_config.NumberColumn("Sharpe", format="%.3f"),
            "nifty_sharpe_ratio": st.column_config.NumberColumn(
                "Nifty Sharpe", format="%.3f", help="Nifty 50's Sharpe over THIS investor's window."),
            "nifty_ann_return_pct": st.column_config.NumberColumn(
                "Nifty Ann %", format="%.1f", help="Nifty 50's annualised return over this investor's window."),
            "nifty_max_drawdown_pct": st.column_config.NumberColumn(
                "Nifty Max DD %", format="%.1f", help="Nifty 50's worst drawdown over this investor's window."),
            "sharpe_x_nifty": st.column_config.NumberColumn(
                "Sharpe ×Nifty", format="%.1f×",
                help="Investor Sharpe ÷ Nifty's Sharpe — reward-per-risk relative to the index "
                     "(the meaningful read; >1× beats the index)."),
            "information_ratio": st.column_config.NumberColumn(
                "Info Ratio", format="%.2f",
                help="Active return ÷ tracking error — the purest skill metric (consistency of beating "
                     "the benchmark). >0.5 good · >1.0 top-tier."),
            "ann_return_pct": st.column_config.NumberColumn("Ann ret %", format="%.1f"),
            "rolling_1y_pct": st.column_config.NumberColumn("1Y %", format="%.1f"),
            "max_drawdown_pct": st.column_config.NumberColumn("Max DD %", format="%.1f"),
            "current_net_worth_cr": st.column_config.NumberColumn("Net worth ₹Cr", format="%.0f"),
        })
    with st.expander("ℹ️ How to read this table"):
        st.caption("**Sharpe ×Nifty** = how many times the index's reward-per-risk (Nifty Sharpe ≈ 0.12) — "
                   "read this, not the raw Sharpe's 0–3 scale. **Info Ratio** = consistency of beating the "
                   "benchmark (>0.5 good). Sorted best-first (signal → confidence → alpha). **Reminder:** alpha/Sharpe come from "
                   "*net-worth* changes (contaminated by capital flows) — treat as **directional**, and verify "
                   "any name in **Stock Analysis** before acting.")

    # ---- 🏆 leaderboard — top investors by alpha (visual, from the current filter) ----
    _lb = view.copy()
    _lb["_a"] = pd.to_numeric(_lb.get("alpha_ann_pct"), errors="coerce")
    _lb = _lb.dropna(subset=["_a"]).sort_values("_a", ascending=False).head(15)
    if len(_lb) >= 2:
        st.markdown("#### 🏆 Leaderboard — alpha vs Nifty")
        import plotly.graph_objects as _go
        _lbr = _lb.iloc[::-1]                                  # highest on top in a horizontal bar
        _fig = _go.Figure(_go.Bar(
            x=_lbr["_a"], y=_lbr["name"].astype(str).str.title(), orientation="h",
            marker=dict(color=_lbr["_a"], colorscale=[[0, "#5C6679"], [1, "#E3B341"]]),
            text=[f"{v:+.0f}%" for v in _lbr["_a"]], textposition="outside", cliponaxis=False))
        _fig.update_layout(template="plotly_dark", height=min(520, 70 + 28 * len(_lbr)),
                           margin=dict(l=8, r=36, t=6, b=8), paper_bgcolor="rgba(0,0,0,0)",
                           plot_bgcolor="rgba(0,0,0,0)", xaxis_title="Alpha % (annualised, vs Nifty)",
                           coloraxis_showscale=False)
        st.plotly_chart(_fig, use_container_width=True, config={"displaylogo": False})
        st.caption("Alpha = annualised return above Nifty (from net-worth changes — directional). "
                   "Reflects the filters above.")

    _sel = _ev.selection["rows"] if _ev and _ev.selection else []
    if _sel and _sel[0] < len(_disp):
        _row = view.iloc[_sel[0]]                       # positional → exact clicked row (immune to duplicate names)
        _pick = str(_row["name"])
        publish_context({"kind": "superstar", "investor": _pick})

        def _u(key):                                    # NaN-safe cell → '' (so an `or` fallback works)
            v = _row.get(key, "")
            return "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()

        def _val(key, suf=""):
            v = _row.get(key, "")
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().lower() in ("", "nan"):
                return "—"
            if isinstance(v, float) and not isinstance(v, bool):    # match the table's rounding
                return f"{v:.2f}{suf}"
            return f"{v}{suf}"
        with st.container(border=True):
            st.markdown(f"## ⭐ {_pick.title()}  ·  {_u('type')}")
            _mc = st.columns(6)
            _mc[0].metric("Signal", _val("signal"))
            _mc[1].metric("Alpha", _val("alpha_ann_pct", "%"))
            _mc[2].metric("Sharpe", _val("sharpe_ratio"))
            _mc[3].metric("Ann return", _val("ann_return_pct", "%"))
            _mc[4].metric("Max DD", _val("max_drawdown_pct", "%"))
            _mc[5].metric("Quarters", _val("quarters_tracked"))
            if _u("interpretation"):
                st.caption(_u("interpretation"))

        # ---- 🗂️ holdings treemap (current holdings sized by ₹ value) ----
        _hold_all = _read_fii_tab("superstar_holdings")
        if not _hold_all.empty and "investor" in _hold_all.columns:
            _hh = _hold_all[_hold_all["investor"].astype(str).str.lower() == _pick.lower()].copy()
            _hh["_v"] = pd.to_numeric(_hh.get("value_cr"), errors="coerce")
            if "move" in _hh.columns:
                _hh = _hh[~_hh["move"].astype(str).isin(["EXIT", "past", "nan", "None"])]
            _hh = _hh.dropna(subset=["_v"])
            _hh = _hh[_hh["_v"] > 0]
            if len(_hh) >= 2:
                _hh = _hh.sort_values("_v", ascending=False).head(25)
                import plotly.express as _px
                st.markdown("#### 🗂️ Holdings by value")
                _tf = _px.treemap(_hh, path=[_px.Constant("Portfolio"), "company"], values="_v",
                                  color="_v", color_continuous_scale=["#2A2F3A", "#8A6D2E", "#E3B341"])
                _tf.update_layout(template="plotly_dark", height=380, margin=dict(l=4, r=4, t=26, b=4),
                                  paper_bgcolor="rgba(0,0,0,0)", coloraxis_showscale=False,
                                  font=dict(family="Inter, sans-serif"))
                _tf.update_traces(texttemplate="%{label}<br>₹%{value:,.0f} cr", textfont_size=13,
                                  marker=dict(cornerradius=4), root_color="rgba(0,0,0,0)")
                st.plotly_chart(_tf, use_container_width=True, config={"displaylogo": False})
                st.caption(f"Top {len(_hh)} disclosed positions by ₹ value (latest reported quarter). "
                           "Bigger tile = larger holding.")

        # ==== 📊 Deals & performance zone (bulk/block · performance · SAST · insider) ====
        _bb = fetch_superstar_bulkblock()
        _bb = (_bb[_bb["investor"].astype(str).str.lower() == _pick.lower()]
               if (not _bb.empty and "investor" in _bb.columns) else pd.DataFrame())
        if not _bb.empty:
            _bb = _bb.copy()
            for _c in ("date", "ticker", "company", "entity", "exchange", "action", "qty", "price", "pct_traded", "deal_type"):
                if _c not in _bb.columns:                # tolerate an older cached schema (e.g. pre-ticker)
                    _bb[_c] = ""
            _bb["_dt"] = pd.to_datetime(_bb["date"], format="mixed", dayfirst=True, errors="coerce")
            _bb["qtyN"] = pd.to_numeric(_bb["qty"], errors="coerce")
            _bb["priceN"] = pd.to_numeric(_bb["price"], errors="coerce")
            _bb["move"] = _bb["action"].astype(str).str.upper().map(
                lambda a: "🟢 Buy" if a == "BUY" else ("🔴 Sell" if a == "SELL" else "—"))
            _bb["pct_traded"] = pd.to_numeric(_bb["pct_traded"], errors="coerce")
        _isast = fetch_superstar_sast()
        _sast = (_isast[_isast["investor"].astype(str).str.lower() == _pick.lower()]
                 if (not _isast.empty and "investor" in _isast.columns) else pd.DataFrame())
        if not _sast.empty and "confidence" in _sast.columns:
            _sast = _sast[_sast["confidence"].astype(str).str.upper() != "REVIEW"]
        _iins = fetch_superstar_insider()
        _ins = (_iins[_iins["investor"].astype(str).str.lower() == _pick.lower()]
                if (not _iins.empty and "investor" in _iins.columns) else pd.DataFrame())
        if not _ins.empty:
            _ins = _ins.drop_duplicates(
                subset=[c for c in ["report_date", "company", "action", "qty", "traded_pct"] if c in _ins.columns])

        st.markdown("### 📊 Deals & performance")
        _tabR, _tabP, _tabA, _tabS, _tabI = st.tabs(
            ["🕒 Recent (3M)", "📈 Performance", "📜 All deals", "🔴 SAST", "🕵️ Insider (1M)"])
        _bbshow = ["date", "company", "entity", "exchange", "move", "qty", "price", "pct_traded", "deal_type"]
        _bbcfg = {
            "date": st.column_config.TextColumn("Date"), "company": st.column_config.TextColumn("Stock"),
            "entity": st.column_config.TextColumn("Via (account/entity)"), "exchange": st.column_config.TextColumn("Exch"),
            "move": st.column_config.TextColumn("Move"), "qty": st.column_config.TextColumn("Qty"),
            "price": st.column_config.TextColumn("Avg price ₹"), "pct_traded": st.column_config.NumberColumn("% traded", format="%.2f%%"),
            "deal_type": st.column_config.TextColumn("Type", help="Bulk = >0.5% of company in a day · Block = block-window trade")}

        with _tabR:
            if _bb.empty:
                st.caption(f"No bulk/block deals on record for **{_pick.title()}**.")
            else:
                _r = _bb[_bb["_dt"] >= pd.Timestamp.now() - pd.Timedelta(days=90)].sort_values("_dt", ascending=False)
                st.caption(f"**{len(_r)}** bulk/block trade(s) in the last 3 months — BSE + NSE, all entities.")
                st.dataframe(filter_ui(_r[[c for c in _bbshow if c in _r.columns]], "ss_bb_recent"), hide_index=True,
                             use_container_width=True, column_config=_bbcfg)

        with _tabP:
            if _bb.empty:
                st.caption("No bulk/block deals to analyse.")
            else:
                st.markdown("**% move since each buy** — did it rise after they bought? "
                            "_(now vs the deal's avg price · buys in the last 12 months)_")
                _buys = _bb[(_bb["move"] == "🟢 Buy") &
                            (_bb["_dt"] >= pd.Timestamp.now() - pd.Timedelta(days=365))].copy()
                if _buys.empty:
                    st.caption("No buys in the last 12 months.")
                else:
                    _px = {t: _cur_price(t) for t in _buys["ticker"].dropna().unique()}
                    _buys["current"] = _buys["ticker"].map(_px)
                    _buys["pct_since"] = ((_buys["current"] - _buys["priceN"]) / _buys["priceN"] * 100).round(1)
                    _buys["days"] = (pd.Timestamp.now().normalize() - _buys["_dt"]).dt.days
                    _pv = _buys.sort_values("_dt", ascending=False)[
                        [c for c in ["date", "company", "ticker", "priceN", "current", "pct_since", "days", "exchange"]
                         if c in _buys.columns]]
                    st.dataframe(filter_ui(_pv, "ss_perf_since"), hide_index=True, use_container_width=True, column_config={
                        "date": st.column_config.TextColumn("Bought"), "company": st.column_config.TextColumn("Stock"),
                        "ticker": st.column_config.TextColumn("Ticker"),
                        "priceN": st.column_config.NumberColumn("Buy ₹", format="%.1f"),
                        "current": st.column_config.NumberColumn("Now ₹", format="%.1f"),
                        "pct_since": st.column_config.NumberColumn("% since", format="%.1f%%"),
                        "days": st.column_config.NumberColumn("Days"), "exchange": st.column_config.TextColumn("Exch")})
                    st.caption("Blank Now/% = BSE-only scrip code or price unavailable. Prices ~1-day cached.")
                st.markdown("**Realized round-trips** — bought *and later* sold "
                            "_(same-day/price inter-entity transfers excluded; both legs must be disclosed)_")
                _rt = []
                for _tk, _g in _bb.dropna(subset=["_dt", "qtyN", "priceN"]).groupby("ticker"):
                    _key = (_g["_dt"].astype(str) + "|" + _g["priceN"].astype(str) + "|" + _g["qtyN"].astype(str))
                    _xfer = set(_key[_g["move"] == "🟢 Buy"]) & set(_key[_g["move"] == "🔴 Sell"])
                    _real = _g[~_key.isin(_xfer)]
                    _b = _real[_real["move"] == "🟢 Buy"]; _s = _real[_real["move"] == "🔴 Sell"]
                    _bq, _sq = _b["qtyN"].sum(), _s["qtyN"].sum()
                    if not (_bq > 0 and _sq > 0):
                        continue
                    _ab = (_b["qtyN"] * _b["priceN"]).sum() / _bq
                    _as = (_s["qtyN"] * _s["priceN"]).sum() / _sq
                    _rt.append({"company": _g["company"].iloc[0], "ticker": _tk,
                                "avg_buy": round(_ab, 1), "avg_sell": round(_as, 1),
                                "realized_pct": round((_as - _ab) / _ab * 100, 1) if _ab else None,
                                "days": (_s["_dt"].max() - _b["_dt"].min()).days})
                if _rt:
                    st.dataframe(filter_ui(pd.DataFrame(_rt).sort_values("realized_pct", ascending=False), "ss_roundtrips"),
                                 hide_index=True, use_container_width=True, column_config={
                        "company": st.column_config.TextColumn("Stock"), "ticker": st.column_config.TextColumn("Ticker"),
                        "avg_buy": st.column_config.NumberColumn("Avg buy ₹", format="%.1f"),
                        "avg_sell": st.column_config.NumberColumn("Avg sell ₹", format="%.1f"),
                        "realized_pct": st.column_config.NumberColumn("Realized %", format="%.1f%%"),
                        "days": st.column_config.NumberColumn("Hold days")})
                else:
                    st.caption("No clean disclosed round-trips — bulk/block usually shows only one side "
                               "(the other leg is accumulated/exited quietly below the threshold).")

        with _tabA:
            if _bb.empty:
                st.caption("No bulk/block deals on record.")
            else:
                _a = _bb.sort_values("_dt", ascending=False)
                st.caption(f"**{len(_a)}** total disclosed bulk/block trades · **Via** = the actual account/entity.")
                st.dataframe(filter_ui(_a[[c for c in _bbshow if c in _a.columns]], "ss_bb_all"), hide_index=True,
                             use_container_width=True, column_config=_bbcfg)

        with _tabS:
            st.caption("SEBI **SAST Reg 29** — crossed 5% (Reg29(1)) or moved ±2% (Reg29(2)); filed ~T+2.")
            if _sast.empty:
                st.caption(f"No SAST Reg 29 disclosures for **{_pick.title()}** in the tracked window.")
            else:
                _sv = _sast.copy()
                _sv["move"] = _sv["action"].astype(str).str.lower().map(
                    lambda a: "🟢 Buy" if a.startswith("acq") else ("🔴 Sell" if a else "—"))
                _sc = [c for c in ["trade_dates", "symbol", "company", "move", "pct_traded", "pct_after", "reg_type"]
                       if c in _sv.columns]
                st.dataframe(filter_ui(_sv[_sc], "ss_sast"), hide_index=True, use_container_width=True, column_config={
                    "trade_dates": st.column_config.TextColumn("Trade date(s)"),
                    "symbol": st.column_config.TextColumn("NSE symbol"), "company": st.column_config.TextColumn("Company"),
                    "move": st.column_config.TextColumn("Move"),
                    "pct_traded": st.column_config.NumberColumn("Δ stake %", format="%.2f"),
                    "pct_after": st.column_config.NumberColumn("Stake after %", format="%.2f"),
                    "reg_type": st.column_config.TextColumn("Reg")})

        with _tabI:
            st.caption("Insider (PIT) + SAST — promoter / designated-person trades, clustered across entities.")
            if _ins.empty:
                st.caption(f"No insider/SAST disclosures on record for **{_pick.title()}**.")
            else:
                _iv = _ins.copy()
                for _pc in ("holding_after", "traded_pct"):
                    if _pc in _iv.columns:
                        _iv[_pc] = pd.to_numeric(_iv[_pc], errors="coerce")
                _iv["_rd"] = pd.to_datetime(_iv.get("report_date"), format="mixed", dayfirst=True, errors="coerce")
                _all_i = st.checkbox("Show all (not just last 30 days)", value=False, key=f"ins_all_{_pick}")
                if not _all_i:
                    _iv = _iv[_iv["_rd"] >= pd.Timestamp.now() - pd.Timedelta(days=30)]
                _iv = _iv.sort_values("_rd", ascending=False)
                st.caption(f"**{len(_iv)}** disclosure(s){'' if _all_i else ' in the last 30 days'}.")
                _ic = [c for c in ["report_date", "company", "person", "category", "action",
                                   "holding_after", "traded_pct", "regulation"] if c in _iv.columns]
                st.dataframe(filter_ui(_iv[_ic], "ss_insider"), hide_index=True, use_container_width=True, column_config={
                    "report_date": st.column_config.TextColumn("Reported"), "company": st.column_config.TextColumn("Stock"),
                    "person": st.column_config.TextColumn("Person / entity"), "category": st.column_config.TextColumn("Type"),
                    "action": st.column_config.TextColumn("Action"),
                    "holding_after": st.column_config.NumberColumn("Holding after", format="%.2f%%"),
                    "traded_pct": st.column_config.NumberColumn("Traded %", format="%.2f%%"),
                    "regulation": st.column_config.TextColumn("Reg")})

        _link = _u("links") or _u("portfolio_url")
        _hold, _quarters, _err = superstar_holdings_journey(_pick)
        if _hold.empty:
            st.warning(f"Couldn't load the holdings journey: {_err}")
            if _link:
                st.markdown(f"🔗 [Open {_pick.title()}'s portfolio on Trendlyne →]({_link})")
        else:
            _latest = _quarters[0] if _quarters else ""
            _vc = _hold["Move"].value_counts().to_dict()
            st.markdown(f"#### 📜 Holdings journey — **{len(_hold)}** disclosed positions · "
                        f"latest **{_latest}**: 🟢 {_vc.get('NEW', 0)} new · 🟢 {_vc.get('ADD', 0)} added · "
                        f"🔴 {_vc.get('TRIM', 0)} trimmed · 🔴 {_vc.get('EXIT', 0)} exited")
            # ---- holdings filters (Move + min Δ stake) ----
            _hf = st.columns([4, 2, 4])
            _mvs = _hf[0].multiselect("Move (any of)", ["NEW", "ADD", "TRIM", "EXIT", "HOLD", "past"],
                                      key=f"hmv_{_pick}")
            _mind = _hf[1].number_input("Min Δ stake %", value=None, step=0.1, format="%.2f",
                                        key=f"hdl_{_pick}", placeholder="—",
                                        help="e.g. 0.5 → only this-quarter moves of at least +0.5% stake.")
            _holdf = _hold.copy()
            if _mvs:
                _holdf = _holdf[_holdf["Move"].isin(_mvs)]
            if _mind is not None:
                _holdf = _holdf[pd.to_numeric(_holdf["Δ stake"], errors="coerce").fillna(-1e9) >= _mind]
            _holdf = _holdf.reset_index(drop=True)
            if _holdf.empty:
                st.info("No holdings match these filters — clear Move / Min Δ stake.")
            else:
                _EMO = {"NEW": "🟢 NEW", "ADD": "🟢 ADD", "TRIM": "🔴 TRIM",
                        "EXIT": "🔴 EXIT", "HOLD": "⚪ HOLD", "past": "· past"}
                _dh = _holdf.copy()
                _dh["Move"] = _dh["Move"].map(lambda m: _EMO.get(m, m))
                _order = ["Stock", "Ticker", "Move", "Δ stake"] + _quarters + ["Holding Value", "Qty Held"]
                _dh = _dh[[c for c in _order if c in _dh.columns]]
                _cfg = {q: st.column_config.NumberColumn(q, format="%.1f%%") for q in _quarters}
                _cfg["Δ stake"] = st.column_config.NumberColumn(
                    "Δ stake", format="%+.2f%%", help="Latest-quarter change in % stake (price-independent).")
                _htok = hashlib.md5(("|".join(_holdf["Stock"].astype(str)) + "|" + _pick).encode()).hexdigest()[:10]
                _hev = st.dataframe(_dh, hide_index=True, use_container_width=True, height=540,
                                    on_select="rerun", selection_mode="single-row",
                                    key="sstar_hold_" + _htok, column_config=_cfg)
                st.caption(f"Showing **{len(_holdf)}/{len(_hold)}** positions. Each quarter column = the "
                           "investor's disclosed **% stake** (only ≥1% reported; '–' = not held / below "
                           "threshold). **% stake moves only when they buy or sell** — price doesn't affect it. "
                           "(e.g. Move = NEW + ADD · Min Δ stake 0.5 → only this quarter's real accumulations.)")

                # click a holding row → open that company in full Stock Analysis
                _hsel = _hev.selection["rows"] if _hev and _hev.selection else []
                if _hsel and _hsel[0] < len(_holdf):
                    _hrow = _holdf.iloc[_hsel[0]]
                    _htk = str(_hrow.get("Ticker", "") or "").strip()
                    _hstock = str(_hrow.get("Stock", ""))
                    if _htk:
                        st.button(f"📊  Open {_hstock} ({_htk}) in Stock Analysis  →  fundamentals · chart · indicators",
                                  key=f"openstk_{_htok}_{_htk}", use_container_width=True,
                                  on_click=_open_stock, args=(_htk, f"⭐ {_pick.title()}'s portfolio"))
                        st.caption("Opens the full stock page (price position, fundamentals & every strategy's "
                                   "signal) so you can vet this superstar pick before acting.")
                    else:
                        st.info(f"Couldn't read an NSE symbol for **{_hstock}** — it may not be NSE-listed "
                                "(open it manually in Stock Analysis).")
                else:
                    st.caption("👆 Click any holding row to open that company in **Stock Analysis**.")
    else:
        st.info("👆 **Click an investor's row** above to open their holdings journey.")

    st.stop()

# ============================================================================
# FII/DII ALERTS MODE — quality-investor watchlist + quarterly in/out + pipeline health
# ============================================================================
if _mode == "🔔 Alerts":
    _sdf = fetch_superstar_summary()
    with st.sidebar:
        st.markdown("---")
        if st.button("🔄  Refresh alerts", use_container_width=True,
                     help="Re-read the FII/DII summary, history & master_stock."):
            fetch_superstar_summary.clear(); fetch_superstar_history.clear()
            fetch_master_stock.clear(); fetch_superstar_moves.clear()
            fetch_superstar_sast.clear(); fetch_superstar_bulkblock.clear()
            fetch_superstar_insider.clear(); build_quality_moves.clear()
            st.rerun()
        st.caption("Alerts read the FII/DII sheet (run the notebook to refresh the underlying data).")

    st.markdown("## 🔔 FII/DII alerts — quality superstar buys (vs **Nifty 50**)")

    # ---- ⭐ QUALITY-SUPERSTAR LIVE MOVES — every SAST/bulk/block/insider deal by a quality name ----
    # The one place to glance: any disclosure (NSE + BSE) by an investor who passes the quality bar,
    # newest first, so you never open portfolios one by one.
    _qm, _qmeta = build_quality_moves(days=None)
    st.markdown("### ⭐ Quality-superstar moves — live disclosures (NSE + BSE)")
    if not _qmeta:
        st.info("No superstar currently passes the quality bar (signal **BUY/STRONG BUY** · Sharpe **≥ 0.4** · "
                "Max DD **≥ −40**) — nothing to surface here. Loosen the bar on the ⭐ Superstars page to inspect names.")
    elif _qm.empty:
        st.info(f"The **{len(_qmeta)}** quality superstars have **no** disclosed SAST / bulk / block / insider deal on "
                "record yet. Re-run the notebook (or press 🔄 Refresh alerts) after the next NSE/BSE posting.")
    else:
        _qnames_disp = ", ".join(sorted({str(n).title() for n in _qmeta})[:8]) + \
                       ("…" if len(_qmeta) > 8 else "")
        st.caption(f"Every SAST · bulk · block · insider disclosure by the **{len(_qmeta)}** investors who pass your "
                   "quality bar (**signal BUY/STRONG BUY · Sharpe ≥ 0.4 · Max DD ≥ −40**), newest first — so you never "
                   "open each portfolio. Bulk/block covers **both exchanges**; SAST is NSE Reg 29; insider is PIT/SAST "
                   f"(a Reg-29 crossing can appear under both **SAST** and **Insider**). Quality names: {_qnames_disp}")
        _fc = st.columns([2, 2, 2, 2, 2, 3])
        _win = _fc[0].selectbox("Window", ["30d", "60d", "90d", "180d", "All"], index=0, key="qm_win")
        _srcs = _fc[1].multiselect("Source", sorted(_qm["source"].dropna().unique()), key="qm_src")
        _typopts = sorted(x for x in _qm["type"].dropna().astype(str).unique() if x.strip())
        _typs = _fc[2].multiselect("Type", _typopts, key="qm_type",
                                   help="Individual investor (guru) vs institutional (fund/AMC).")
        _sides = _fc[3].multiselect("Side", ["BUY", "SELL"], key="qm_side")
        _exopts = [e for e in ["NSE", "BSE"] if e in set(_qm["exchange"].astype(str))]
        _exs = _fc[4].multiselect("Exchange", _exopts, key="qm_exch")
        _qq = _fc[5].text_input("🔎 Search investor / stock", key="qm_q").strip().lower()
        v = _qm.copy()
        if _win != "All":
            _cut = pd.Timestamp.now().normalize() - pd.Timedelta(days={"30d": 30, "60d": 60, "90d": 90, "180d": 180}[_win])
            v = v[v["_dt"].isna() | (v["_dt"] >= _cut)]
        if _srcs:
            v = v[v["source"].isin(_srcs)]
        if _typs:
            v = v[v["type"].isin(_typs)]
        if _sides:
            v = v[v["side"].isin(_sides)]
        if _exs:
            v = v[v["exchange"].isin(_exs)]
        if _qq:
            v = v[v.apply(lambda r: _qq in str(r.get("investor", "")).lower()
                          or _qq in str(r.get("stock", "")).lower()
                          or _qq in str(r.get("ticker", "")).lower(), axis=1)]
        _nb, _ns = int((v["side"] == "BUY").sum()), int((v["side"] == "SELL").sum())
        _mc = st.columns(4)
        _mc[0].metric("Moves shown", len(v))
        _mc[1].metric("Investors", int(v["investor"].nunique()))
        _mc[2].metric("Stocks", int(v["stock"].nunique()))
        _mc[3].metric("Buys / Sells", f"{_nb} / {_ns}")
        _disp = v.copy()
        _disp["investor"] = _disp["investor"].astype(str).str.title()
        _disp["_d"] = _disp["_dt"].dt.date.astype(str).where(_disp["_dt"].notna(), _disp["date"].astype(str))
        _show = ["_d", "investor", "signal", "source", "side", "stock", "ticker", "exchange", "detail", "conf"]
        st.dataframe(filter_ui(_disp[[c for c in _show if c in _disp.columns]], "qmoves"), hide_index=True,
                     use_container_width=True, height=460, column_config={
                         "_d": st.column_config.TextColumn("Date"),
                         "investor": st.column_config.TextColumn("Investor"),
                         "signal": st.column_config.TextColumn("Signal"),
                         "source": st.column_config.TextColumn("Via"),
                         "side": st.column_config.TextColumn("Side"),
                         "stock": st.column_config.TextColumn("Stock"),
                         "ticker": st.column_config.TextColumn("Ticker"),
                         "exchange": st.column_config.TextColumn("Exch"),
                         "detail": st.column_config.TextColumn("Details", width="large"),
                         "conf": st.column_config.TextColumn("Match")})
        # roll-up: stocks BOUGHT by ≥2 quality superstars in the current view — the strongest signal
        _roll = (v[v["side"] == "BUY"].groupby(["stock", "ticker"], dropna=False).agg(
                    investors=("investor", "nunique"),
                    who=("investor", lambda x: ", ".join(sorted({str(i).title() for i in x})[:6])),
                    latest=("_dt", "max")).reset_index())
        _roll = _roll[_roll["investors"] >= 2].sort_values(["investors", "latest"], ascending=False)
        if not _roll.empty:
            with st.expander(f"🔥 Stocks BOUGHT by ≥2 quality superstars in this window ({len(_roll)})", expanded=True):
                _roll["latest"] = pd.to_datetime(_roll["latest"], errors="coerce").dt.date.astype(str)
                st.dataframe(filter_ui(_roll[["stock", "ticker", "investors", "who", "latest"]], "qm_rollup"), hide_index=True,
                             use_container_width=True, column_config={
                                 "stock": st.column_config.TextColumn("Stock"),
                                 "ticker": st.column_config.TextColumn("Ticker"),
                                 "investors": st.column_config.NumberColumn("# Quality buyers"),
                                 "who": st.column_config.TextColumn("Who"),
                                 "latest": st.column_config.TextColumn("Latest buy")})
    st.markdown("---")

    # ---- 📢 PORTFOLIO QUARTER UPDATES — WHICH investors just advanced to a newer disclosed quarter ----
    # Per-investor change detection: the notebook diffs each investor's data_to run-over-run and logs
    # the advances to `quarter_updates`. This replaces the old single-global-quarter banner (which
    # mislabeled an in-progress quarter as "disclosed").
    _latest_q = None
    if not _sdf.empty and "data_to" in _sdf.columns:
        _lq = pd.to_datetime(_sdf["data_to"], errors="coerce").max()
        if pd.notna(_lq):
            _latest_q = _lq.date().isoformat()
    _qu = fetch_quarter_updates()
    _recent = pd.DataFrame()
    if not _qu.empty and {"detected_on", "name"}.issubset(_qu.columns):
        _last_run = _qu["detected_on"].astype(str).max()
        _recent = _qu[_qu["detected_on"].astype(str) == _last_run].copy()
    if not _recent.empty:
        st.success(f"📢 **{len(_recent)} portfolio(s) just published a newer quarter** — detected {_last_run}. "
                   "See exactly who & what changed below. 👇")
        with st.expander(f"📢 Which portfolios updated their quarter ({len(_recent)})", expanded=True):
            _qcols = [c for c in ["name", "prev_quarter", "new_quarter"] if c in _recent.columns]
            st.dataframe(filter_ui(_recent[_qcols].reset_index(drop=True), "quarter_updates"), hide_index=True, use_container_width=True,
                         height=min(380, 70 + 35 * len(_recent)),
                         column_config={
                             "name": st.column_config.TextColumn("Investor"),
                             "prev_quarter": st.column_config.TextColumn("Was"),
                             "new_quarter": st.column_config.TextColumn("Now"),
                         })
            st.caption("These investors' Trendlyne data advanced to a newer disclosed quarter since the previous "
                       "run (tracked daily by the notebook in the `quarter_updates` tab).")
    else:
        st.caption("📭 No portfolio quarter-updates recorded yet — once the notebook has run twice, any investor "
                   "advancing to a newer quarter is listed here." +
                   (f"  ·  Latest quarter present in our data: **{_latest_q}**." if _latest_q else ""))

    # ---- 🔎 WHAT CHANGED — which stocks/portfolios the superstars actually moved on ----
    _mv0 = fetch_superstar_moves()
    if not _mv0.empty and {"move", "ticker"}.issubset(_mv0.columns):
        _mc0 = _mv0["move"].astype(str).str.upper()
        _cnt = {k: int((_mc0 == k).sum()) for k in ("NEW", "ADD", "TRIM", "EXIT")}
        st.markdown(f"**What changed:** 🟢 **{_cnt['NEW']}** new buys · 🔵 **{_cnt['ADD']}** added · "
                    f"🟠 **{_cnt['TRIM']}** trimmed · 🔴 **{_cnt['EXIT']}** exited "
                    "— across all tracked superstars this quarter.")
        if "as_of" in _mv0.columns:                # surface freshness: moves persist from the last scrape that found any
            _mvd = pd.to_datetime(_mv0["as_of"], errors="coerce").max()
            if pd.notna(_mvd):
                st.caption(f"🕒 Moves as of **{_mvd.date()}** (last notebook run). A quarter with no new moves "
                           "keeps the prior scrape — re-run the notebook to refresh.")
        with st.expander("🔎 See which stocks superstars moved on"):
            _topnew = (_mv0[_mc0 == "NEW"].groupby(["ticker", "company"], dropna=False).agg(
                          investors=("investor", "nunique"),
                          avg_stake=("latest_stake", lambda s: round(float(pd.to_numeric(s, errors="coerce").mean()), 2)),
                          value_cr=("value_cr", lambda s: round(float(pd.to_numeric(s, errors="coerce").fillna(0).sum()), 1)),
                       ).reset_index().sort_values(["investors", "value_cr"], ascending=False).head(20)
                       ) if _cnt["NEW"] else pd.DataFrame()
            if not _topnew.empty:
                st.markdown("**🟢 Most-bought NEW positions** (ranked by how many superstars bought in):")
                st.dataframe(filter_ui(_topnew, "most_bought_new"), hide_index=True, use_container_width=True,
                             column_config={
                                 "ticker": st.column_config.TextColumn("Ticker"),
                                 "company": st.column_config.TextColumn("Company"),
                                 "investors": st.column_config.NumberColumn("# superstars", format="%d"),
                                 "avg_stake": st.column_config.NumberColumn("Avg stake %", format="%.2f%%"),
                                 "value_cr": st.column_config.NumberColumn("Σ value ₹Cr", format="%.1f"),
                             })
            else:
                st.caption("No brand-new positions this quarter — superstars mostly held / adjusted existing names.")
            # ---- 👤 whose results are in for this quarter + what each of them did ----
            if not _sdf.empty and "data_to" in _sdf.columns:
                _dt_str = pd.to_datetime(_sdf["data_to"], errors="coerce").dt.date.astype(str)
                _n_fresh = int((_dt_str == _latest_q).sum())
                st.markdown(f"**📅 {_n_fresh} of {len(_sdf)}** tracked superstars have published **{_latest_q}** data.")
            if "investor" in _mv0.columns:
                _mU = _mv0["move"].astype(str).str.upper()
                _byinv = (_mv0.assign(_m=_mU).groupby("investor").agg(
                              NEW=("_m", lambda s: int((s == "NEW").sum())),
                              ADD=("_m", lambda s: int((s == "ADD").sum())),
                              TRIM=("_m", lambda s: int((s == "TRIM").sum())),
                              EXIT=("_m", lambda s: int((s == "EXIT").sum())),
                              total=("_m", "count"),
                          ).reset_index().sort_values("total", ascending=False).reset_index(drop=True))
                st.markdown(f"**👤 Which superstars changed** — {len(_byinv)} investors disclosed moves this quarter "
                            "(click a column header to sort):")
                st.dataframe(filter_ui(_byinv, "by_investor"), hide_index=True, use_container_width=True, height=320,
                             column_config={
                                 "investor": st.column_config.TextColumn("Investor"),
                                 "NEW": st.column_config.NumberColumn("🟢 New", format="%d"),
                                 "ADD": st.column_config.NumberColumn("🔵 Added", format="%d"),
                                 "TRIM": st.column_config.NumberColumn("🟠 Trimmed", format="%d"),
                                 "EXIT": st.column_config.NumberColumn("🔴 Exited", format="%d"),
                                 "total": st.column_config.NumberColumn("Σ moves", format="%d"),
                             })
            st.caption("This counts **all** tracked superstars. The criteria-filtered 'best investor' new buys are "
                       "in the **🆕 New buys** section below.")

    # ---- 🔧 PIPELINE HEALTH — only shows when something actually needs attention (else silent) ----
    alarms = []   # (level_emoji, message)
    if _sdf.empty:
        alarms.append(("🔴", "Investor **data is missing/empty** — the nightly refresh hasn't run yet."))
    else:
        if "data_to" in _sdf.columns:
            _dt = pd.to_datetime(_sdf["data_to"], errors="coerce").max()
            if pd.notna(_dt) and (datetime.now() - _dt).days > 130:
                alarms.append(("🟡", f"Investor data looks **stale** — latest disclosed quarter is "
                                     f"{_dt.date()} ({(datetime.now() - _dt).days} days ago)."))
        if "ann_return_pct" in _sdf.columns and len(_sdf):
            _full = int(pd.to_numeric(_sdf["ann_return_pct"], errors="coerce").notna().sum())
            if _full / len(_sdf) < 0.7:
                alarms.append(("🟡", f"**Low metric coverage** — only {_full}/{len(_sdf)} investors have full "
                                     "metrics (a refresh may have been throttled)."))
    _ms = fetch_master_stock()
    if _ms.empty:
        alarms.append(("🟡", "**master_stock not built** — the daily refresh needs to run."))
    elif "as_of" in _ms.columns:
        _msd = pd.to_datetime(_ms["as_of"], errors="coerce").max()
        if pd.notna(_msd) and (datetime.now() - _msd).days > 8:
            alarms.append(("🟡", f"**master_stock is stale** — last built {_msd.date()} "
                                 f"({(datetime.now() - _msd).days} days ago)."))
    try:
        import glob
        _here = os.path.dirname(os.path.abspath(__file__))
        _bases = {_here, os.path.dirname(_here), os.getcwd(), os.path.dirname(os.getcwd())}
        _names = ("master_stock_checkpoint.csv",                       # only OUR pipeline's files
                  "fii_dii_indian_investment_summary_checkpoint.csv",
                  "fii_dii_us_investment_summary_checkpoint.csv")
        _ck = sorted({os.path.basename(p) for b in _bases for nm in _names
                      for p in glob.glob(os.path.join(b, nm))})
        if _ck:
            alarms.append(("🟡", "A refresh is **in progress or was interrupted** "
                                 f"(checkpoint present: `{', '.join(_ck)}`)."))
    except Exception:
        pass
    if alarms:      # stay silent when healthy; surface (collapsed unless critical) only when not
        _hard = any(_lvl == "🔴" for _lvl, _ in alarms)
        with st.expander(("🔴 Data needs attention" if _hard else "🟡 Data — minor issues"), expanded=_hard):
            for _lvl, _msg in alarms:
                (st.error if _lvl == "🔴" else st.warning)(f"{_lvl} {_msg}")
            st.caption("For the maintainer: re-run the nightly refresh (`run_daily.sh`) on a residential IP.")

    if _sdf.empty:
        st.stop()

    # ---- ✅ ALERT CRITERIA (defaults = your spec; tweakable) ----
    st.markdown("### ✅ Alert criteria")
    _af1 = st.columns([3, 3])
    _aq = _af1[0].text_input("🔎 Search investor", key="alert_q").strip().lower()
    _asig = _af1[1].multiselect("Signal (any of)", ["STRONG BUY", "BUY", "WATCH", "HOLD", "AVOID"],
                                default=["STRONG BUY", "BUY"], key="alert_sig")
    with st.expander("⚙️ Advanced filters — Sharpe · Alpha · Return · Max drawdown", expanded=False):
        _af2 = st.columns(4)
        _ash = _af2[0].number_input("Min Sharpe", value=0.5, step=0.1, format="%.2f", key="alert_sh")
        _aal = _af2[1].number_input("Min Alpha %", value=None, step=1.0, key="alert_al", placeholder="—")
        _aann = _af2[2].number_input("Min Ann ret %", value=None, step=1.0, key="alert_minann", placeholder="—")
        _add = _af2[3].number_input("Max DD ≥ %", value=-40.0, step=5.0, key="alert_dd",
                                    help="Drawdown not worse than this.")

    def _qualify(df):
        if df.empty:
            return df
        m = pd.Series(True, index=df.index)
        if _aq and "name" in df.columns:
            m &= df["name"].astype(str).str.lower().str.contains(_aq, na=False)
        if _asig and "signal" in df.columns:
            m &= df["signal"].astype(str).isin(_asig)
        if _ash is not None and "sharpe_ratio" in df.columns:
            m &= pd.to_numeric(df["sharpe_ratio"], errors="coerce") >= _ash
        if _aal is not None and "alpha_ann_pct" in df.columns:
            m &= pd.to_numeric(df["alpha_ann_pct"], errors="coerce") >= _aal
        if _aann is not None and "ann_return_pct" in df.columns:
            m &= pd.to_numeric(df["ann_return_pct"], errors="coerce") >= _aann
        if _add is not None and "max_drawdown_pct" in df.columns:
            m &= pd.to_numeric(df["max_drawdown_pct"], errors="coerce") >= _add
        return df[m]

    _qual = _qualify(_sdf)
    st.caption(f"Showing **{len(_qual)}/{len(_sdf)}** investors. Filters combine with **AND**; numeric "
               "filters apply only when you enter a value.")

    # ---- 📋 CURRENT ALERT LIST ----
    with st.container(border=True):
        st.markdown(f"### 📋 Current alerts — **{len(_qual)}** investors meet the criteria")
        if _qual.empty:
            st.info("No investors currently meet the criteria — loosen the thresholds above.")
        else:
            _cols = [c for c in ["name", "type", "signal", "confidence_score", "sharpe_ratio",
                                 "alpha_ann_pct", "ann_return_pct", "max_drawdown_pct",
                                 "score_vs_benchmark", "quarters_tracked"] if c in _qual.columns]
            st.dataframe(
                filter_ui(_qual[_cols].reset_index(drop=True), "alert_qual_list"), hide_index=True, use_container_width=True, height=380,
                column_config={
                    "name": st.column_config.TextColumn("Investor"),
                    "confidence_score": st.column_config.NumberColumn("Conf", format="%d"),
                    "sharpe_ratio": st.column_config.NumberColumn("Sharpe", format="%.3f"),
                    "alpha_ann_pct": st.column_config.NumberColumn("Alpha %", format="%.1f"),
                    "ann_return_pct": st.column_config.NumberColumn("Ann ret %", format="%.1f"),
                    "max_drawdown_pct": st.column_config.NumberColumn("Max DD %", format="%.1f"),
                })
            st.caption("These are the alpha investors worth following right now. Open **⭐ Superstars** and "
                       "click a name to see their holdings journey (what they're buying/trimming).")

    # ---- 🆕 NEW BUYS by the qualifying (best) investors this quarter ----
    with st.container(border=True):
        st.markdown("### 🆕 New buys by your best investors")
        _moves = fetch_superstar_moves()
        _qnames = set(_qual["name"].astype(str).str.strip().str.lower()) if not _qual.empty else set()
        if _moves.empty or "investor" not in _moves.columns or "move" not in _moves.columns:
            st.info("No moves data yet — run the notebook's **master_stock** build (Cell 14). It now also "
                    "writes a `superstar_moves` tab that powers this.")
        elif not _qnames:
            st.info("No investors meet your criteria above, so there are no 'best investor' new buys to show.")
        else:
            _mv = _moves.copy()
            _mv["_inv"] = _mv["investor"].astype(str).str.strip().str.lower()
            _nb = _mv[(_mv["_inv"].isin(_qnames)) & (_mv["move"].astype(str) == "NEW")]
            if _nb.empty:
                st.info("Your best investors made **no NEW purchases** in the latest disclosed quarter "
                        "(they're holding / trimming). Loosen the criteria to widen the investor set.")
            else:
                _agg = (_nb.groupby(["ticker", "company"]).agg(
                            buyers=("investor", "nunique"),
                            bought_by=("investor", lambda s: ", ".join(sorted(set(s.astype(str))))),
                            avg_new_stake=("latest_stake", lambda s: round(float(pd.to_numeric(s, errors="coerce").mean()), 2)),
                            total_value_cr=("value_cr", lambda s: round(float(pd.to_numeric(s, errors="coerce").fillna(0).sum()), 1)),
                        ).reset_index()
                        .sort_values(["buyers", "total_value_cr"], ascending=False).reset_index(drop=True))
                st.caption(f"Stocks **newly bought** in the latest quarter by the **{len(_qnames)}** investors "
                           "meeting your criteria — ranked by **how many of them** bought it (consensus = stronger "
                           "signal). **Click a row** to vet it in Stock Analysis.")
                _mev = st.dataframe(
                    _agg, hide_index=True, use_container_width=True, height=360,
                    on_select="rerun", selection_mode="single-row",
                    key="alert_newbuys_" + hashlib.md5("|".join(_agg["ticker"].astype(str)).encode()).hexdigest()[:8],
                    column_config={
                        "ticker": st.column_config.TextColumn("Ticker"),
                        "company": st.column_config.TextColumn("Company"),
                        "buyers": st.column_config.NumberColumn("# best buyers", format="%d",
                                                                help="How many of your qualifying investors newly bought it."),
                        "bought_by": st.column_config.TextColumn("Bought by"),
                        "avg_new_stake": st.column_config.NumberColumn("Avg new stake %", format="%.2f%%"),
                        "total_value_cr": st.column_config.NumberColumn("Σ value ₹Cr", format="%.1f"),
                    })
                _ms = _mev.selection["rows"] if _mev and _mev.selection else []
                if _ms and _ms[0] < len(_agg):
                    _rr = _agg.iloc[_ms[0]]
                    _tk = str(_rr["ticker"]).strip()
                    if _tk:
                        st.button(f"📊  Open {_rr['company']} ({_tk}) in Stock Analysis  →  fundamentals · chart · indicators",
                                  key=f"alert_open_{_tk}", use_container_width=True,
                                  on_click=_open_stock, args=(_tk, "🔔 best-investor new buy"))

    # ---- 📆 QUARTERLY IN / OUT (from monthly history snapshots) ----
    with st.container(border=True):
        st.markdown("### 📆 Movers — who **entered** / **left** the alert list over time")
        _hist = fetch_superstar_history()
        if _hist.empty or "run_date" not in _hist.columns or "name" not in _hist.columns:
            st.info("No history yet. Snapshots are appended to `fii_dii_indian_investment_history` when you "
                    "run the notebook on the **1st of a month** — run it monthly and this timeline fills in.")
        else:
            _qh = _qualify(_hist)
            _nm = lambda g: set(g["name"].astype(str).str.strip().str.lower())   # normalise to avoid case/space churn
            _by = {str(d): _nm(g) for d, g in _qh.groupby("run_date")}
            # date axis from ALL snapshots (so an all-disqualified month still shows, not silently merged)
            _dates = sorted({str(d) for d in _hist["run_date"]})
            if len(_dates) < 2:
                _only = _dates[0] if _dates else "none"
                st.caption(f"Only **{len(_dates)}** snapshot so far ({_only}). Need ≥2 to show entries/exits — "
                           "it builds up as you run the notebook each month.")
            else:
                st.caption(f"Comparing **{len(_dates)}** snapshots ({_dates[0]} → {_dates[-1]}). "
                           "🟢 entered = newly meets the criteria · 🔴 left = no longer does.")
                for _prev, _cur in reversed(list(zip(_dates, _dates[1:]))):     # newest first
                    _pc, _cc = _by.get(_prev, set()), _by.get(_cur, set())
                    _ent = sorted(_cc - _pc)
                    _ext = sorted(_pc - _cc)
                    with st.expander(f"**{_cur}**  ·  🟢 +{len(_ent)} entered · 🔴 −{len(_ext)} left  "
                                     f"(list size {len(_cc)})", expanded=(_cur == _dates[-1])):
                        _c1, _c2 = st.columns(2)
                        _c1.markdown("**🟢 Entered the list**\n\n"
                                     + ("\n".join(f"- {n}" for n in _ent) if _ent else "_none_"))
                        _c2.markdown("**🔴 Left the list**\n\n"
                                     + ("\n".join(f"- {n}" for n in _ext) if _ext else "_none_"))

    st.stop()

# ============================================================================
# 🌍 MARKETS — the macro tape: today's overview · FII/DII flow · IPOs
# ============================================================================
if _mode == "🌍 FII/DII":
    st.markdown("## 📉 FII / DII — daily cash-market flow")
    _flow = fetch_fii_dii_flow()
    if _flow.empty or "net_cr" not in _flow.columns:
        st.info("No FII/DII data yet — run the notebook (`build_fii_dii_flow`); it reads NSE's daily "
                "FII/DII activity and builds a rolling series (one session per refresh).")
        st.stop()
    _flow = _flow.copy()
    _flow["_dt"] = pd.to_datetime(_flow["date"].astype(str).str.strip(), errors="coerce")
    for _c in ("buy_cr", "sell_cr", "net_cr"):
        _flow[_c] = pd.to_numeric(_flow[_c], errors="coerce")
    _flow = _flow.dropna(subset=["_dt"]).sort_values("_dt")
    _latest = _flow["_dt"].max()
    _cur = _flow[_flow["_dt"] == _latest]
    _fii = float(_cur[_cur["category"] == "FII"]["net_cr"].sum())
    _dii = float(_cur[_cur["category"] == "DII"]["net_cr"].sum())
    st.caption(f"Latest session **{_latest.date()}** · cash market · ₹ crore · **positive = net buying**. "
               "NSE posts one session at a time, so the history fills in one trading day per refresh.")
    _mc = st.columns(3)
    _mc[0].metric("FII / FPI net", f"₹{_fii:,.0f} cr", delta=f"{_fii:+,.0f}")
    _mc[1].metric("DII net", f"₹{_dii:,.0f} cr", delta=f"{_dii:+,.0f}")
    _mc[2].metric("Combined net", f"₹{_fii + _dii:,.0f} cr", delta=f"{_fii + _dii:+,.0f}")
    _win = st.selectbox("Window", ["Last 15 sessions", "Last 30", "Last 60", "All"], key="fd_win")
    _piv = _flow.pivot_table(index="_dt", columns="category", values="net_cr", aggfunc="sum").sort_index()
    _n = {"Last 15 sessions": 15, "Last 30": 30, "Last 60": 60}.get(_win)
    if _n:
        _piv = _piv.tail(_n)
    import plotly.graph_objects as _go
    _fig = _go.Figure()
    if "FII" in _piv.columns:
        _fig.add_bar(x=_piv.index, y=_piv["FII"], name="FII / FPI", marker_color="#6AA6FF")
    if "DII" in _piv.columns:
        _fig.add_bar(x=_piv.index, y=_piv["DII"], name="DII", marker_color="#E3B341")
    _fig.update_layout(barmode="group", template="plotly_dark", height=400,
                       margin=dict(l=8, r=8, t=28, b=8), paper_bgcolor="rgba(0,0,0,0)",
                       plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", y=1.12, x=0),
                       yaxis_title="Net ₹ crore", bargap=0.25, bargroupgap=0.1)
    _fig.add_hline(y=0, line_color="#3A3F49", line_width=1)
    st.plotly_chart(_fig, use_container_width=True, config={"displaylogo": False})
    with st.expander("📄 Raw daily figures"):
        st.dataframe(filter_ui(_flow[["date", "category", "buy_cr", "sell_cr", "net_cr"]]
                               .sort_values("date", ascending=False), "fiidii_raw"),
                     hide_index=True, use_container_width=True, height=320, column_config={
                         "date": st.column_config.TextColumn("Date"), "category": st.column_config.TextColumn("Who"),
                         "buy_cr": st.column_config.NumberColumn("Buy ₹cr", format="%.0f"),
                         "sell_cr": st.column_config.NumberColumn("Sell ₹cr", format="%.0f"),
                         "net_cr": st.column_config.NumberColumn("Net ₹cr", format="%+.0f")})
    st.stop()

if _mode == "🌍 IPOs":
    st.markdown("## 🚀 IPO dashboard")
    _ipo = fetch_ipo_dashboard()
    if _ipo.empty or "name" not in _ipo.columns:
        st.info("No IPO data yet — run the notebook (`build_ipo_feed`).")
        st.stop()
    _ipo = _ipo.copy()
    _ipo["gmp_expected_pct"] = pd.to_numeric(_ipo["gmp_expected_pct"], errors="coerce")
    st.caption("Open & upcoming IPOs with **GMP / expected listing gain %** from investorgain "
               "(**unofficial grey-market estimate — indicative only**), enriched with NSE symbol, live "
               "subscription & status where the names match.")
    _mrow = st.columns(3)
    _mrow[0].metric("IPOs tracked", len(_ipo))
    if _ipo["gmp_expected_pct"].notna().any():
        _best = _ipo.loc[_ipo["gmp_expected_pct"].idxmax()]
        _mrow[1].metric("Highest GMP", f"{_best['gmp_expected_pct']:.0f}%", delta=str(_best['name'])[:22])
    _mrow[2].metric("NSE-matched", int((_ipo["nse_matched"] == "yes").sum()))
    _fc = st.columns([2, 2, 4])
    _cats = sorted(x for x in _ipo["category"].dropna().astype(str).unique() if x.strip())
    _cat = _fc[0].multiselect("Board", _cats, key="ipo_cat")
    _q = _fc[2].text_input("🔎 Search IPO", key="ipo_q").strip().lower()
    v = _ipo
    if _cat:
        v = v[v["category"].astype(str).isin(_cat)]
    if _q:
        v = v[v["name"].astype(str).str.lower().str.contains(_q, na=False)]
    _show = ["name", "symbol", "category", "status", "price_band", "gmp_expected_pct",
             "subscription_x", "est_listing_date", "open_date", "close_date", "lot", "rating"]
    st.dataframe(filter_ui(v[[c for c in _show if c in v.columns]], "ipo_tbl"),
                 hide_index=True, use_container_width=True, height=560, column_config={
                     "name": st.column_config.TextColumn("IPO", width="large"),
                     "symbol": st.column_config.TextColumn("NSE"),
                     "category": st.column_config.TextColumn("Board"),
                     "status": st.column_config.TextColumn("Status"),
                     "price_band": st.column_config.TextColumn("Price band"),
                     "gmp_expected_pct": st.column_config.NumberColumn("GMP / exp gain", format="%.1f%%",
                         help="Grey-market premium as % of issue price ≈ expected listing gain. UNOFFICIAL."),
                     "subscription_x": st.column_config.TextColumn("Subscribed"),
                     "est_listing_date": st.column_config.TextColumn("Est. listing"),
                     "open_date": st.column_config.TextColumn("Opens"),
                     "close_date": st.column_config.TextColumn("Closes"),
                     "lot": st.column_config.TextColumn("Lot"),
                     "rating": st.column_config.TextColumn("Rating")})
    st.caption("⚠️ GMP is a **grey-market estimate** (investorgain) — not official, not a guarantee; it "
               "moves daily and can vanish at listing. Use only as a sentiment gauge.")
    st.stop()

if _mode == "🌍 Today":
    st.markdown("## 📅 Markets today — the tape at a glance")
    _flow = fetch_fii_dii_flow()
    if not _flow.empty and "net_cr" in _flow.columns:
        _flow = _flow.copy()
        _flow["_dt"] = pd.to_datetime(_flow["date"].astype(str).str.strip(), errors="coerce")
        _flow["net_cr"] = pd.to_numeric(_flow["net_cr"], errors="coerce")
        _ld = _flow["_dt"].max()
        _cur = _flow[_flow["_dt"] == _ld]
        _fii = float(_cur[_cur["category"] == "FII"]["net_cr"].sum())
        _dii = float(_cur[_cur["category"] == "DII"]["net_cr"].sum())
        st.markdown(f"#### 💵 Institutional flow · {_ld.date() if pd.notna(_ld) else '—'}")
        _fc = st.columns(3)
        _fc[0].metric("FII / FPI net", f"₹{_fii:,.0f} cr", delta=f"{_fii:+,.0f}")
        _fc[1].metric("DII net", f"₹{_dii:,.0f} cr", delta=f"{_dii:+,.0f}")
        _fc[2].metric("Combined", f"₹{_fii + _dii:,.0f} cr", delta=f"{_fii + _dii:+,.0f}")
    st.markdown("#### ⭐ Quality-superstar recent moves")
    _qm, _qmeta = build_quality_moves(days=45)
    if _qmeta and not _qm.empty:
        _qv = _qm.copy()
        _qv["investor"] = _qv["investor"].astype(str).str.title()
        _qv["_d"] = _qv["_dt"].dt.date.astype(str).where(_qv["_dt"].notna(), _qv["date"].astype(str))
        _qcols = ["_d", "investor", "source", "side", "stock", "exchange", "detail"]
        st.caption(f"Every SAST · bulk · block · insider disclosure by the {len(_qmeta)} quality investors, last 45 days.")
        st.dataframe(filter_ui(_qv[[c for c in _qcols if c in _qv.columns]], "today_qm"),
                     hide_index=True, use_container_width=True, height=340, column_config={
                         "_d": st.column_config.TextColumn("Date"), "investor": st.column_config.TextColumn("Investor"),
                         "source": st.column_config.TextColumn("Via"), "side": st.column_config.TextColumn("Side"),
                         "stock": st.column_config.TextColumn("Stock"), "exchange": st.column_config.TextColumn("Exch"),
                         "detail": st.column_config.TextColumn("Details", width="large")})
    else:
        st.caption("No quality-superstar moves on record yet.")
    _mkt = fetch_market_bulkblock()
    if not _mkt.empty and "symbol" in _mkt.columns:
        _mkt = _mkt.copy()
        _mkt["_dt"] = pd.to_datetime(_mkt["date"].astype(str).str.strip(), errors="coerce")
        _mq = pd.to_numeric(_mkt.get("qty").astype(str).str.replace(",", "", regex=False), errors="coerce")
        _mp = pd.to_numeric(_mkt.get("price").astype(str).str.replace(",", "", regex=False), errors="coerce")
        _mkt["_val"] = _mq * _mp
        _ldm = _mkt["_dt"].max()
        _big = _mkt[_mkt["_dt"] == _ldm].sort_values("_val", ascending=False).head(15)
        st.markdown(f"#### 🔥 Biggest bulk / block deals · {_ldm.date() if pd.notna(_ldm) else '—'}")
        st.dataframe(_big[[c for c in ["exchange", "symbol", "company", "client", "action", "qty", "price", "deal_type"] if c in _big.columns]],
                     hide_index=True, use_container_width=True, height=300, column_config={
                         "exchange": st.column_config.TextColumn("Exch"), "symbol": st.column_config.TextColumn("Symbol"),
                         "company": st.column_config.TextColumn("Stock"), "client": st.column_config.TextColumn("Client"),
                         "action": st.column_config.TextColumn("Side"), "qty": st.column_config.TextColumn("Qty"),
                         "price": st.column_config.TextColumn("Price ₹"), "deal_type": st.column_config.TextColumn("Type")})
    st.stop()

# ---- sidebar: strategy + cache controls ----
with st.sidebar:
    skey = st.selectbox("Strategy", options=list(vs.STRATEGY_CONFIG.keys()),
                        format_func=lambda k: core.STRATEGY_LABELS.get(k, k), key="strat_sel")
    cfg = vs.STRATEGY_CONFIG[skey]

    # freshness tag stays visible (one glance = "is my data current?"); the rebuild controls are advanced
    if cache:
        try:                                         # newest candle date across cached tickers
            _through = max(df["Date"].iloc[-1] for df in (cache.get("data") or {}).values()
                           if df is not None and len(df))
            _through = pd.Timestamp(_through).date()
            _ago = (datetime.now().date() - _through).days
            _tag = "✅" if _ago <= 0 else ("🟢" if _ago <= 3 else "⚠️")
            st.caption(f"🕒 **Data through {_through}** {_tag}")
        except Exception:
            pass

    with st.expander("⚙️ Data & cache (advanced)", expanded=False):
        st.caption("The nightly job rebuilds this automatically — you rarely need these. "
                   "Use only if the data looks stale.")
        if st.button("📈  Refresh prices  ·  daily", use_container_width=True,
                     help="Re-fetch all OHLCV (run DAILY). Keeps fundamentals as-is."):
            cache = build_full_cache(groups, do_prices=True, do_fund=False, prev=cache)
            st.success(f"Prices refreshed — {len(cache['data'])} tickers.")
            st.rerun()
        if st.button("📊  Refresh fundamentals  ·  quarterly", use_container_width=True,
                     help="Re-fetch fundamentals (only when needed). Keeps prices as-is."):
            cache = build_full_cache(groups, do_prices=False, do_fund=True, prev=cache)
            st.success(f"Fundamentals refreshed — {len(cache.get('fund', {}))} tickers.")
            st.rerun()
        if st.button("🔄  Build all  ·  first time", use_container_width=True,
                     help="Fetch BOTH prices and fundamentals from scratch."):
            cache = build_full_cache(groups, do_prices=True, do_fund=True)
            st.success(f"Built — {len(cache['data'])} tickers.")
            st.rerun()
        if cache:
            _p = (cache.get("built_prices") or cache.get("built") or "")[:16].replace("T", " ")
            _f = (cache.get("built_fund") or "")[:16].replace("T", " ")
            st.caption(f"refreshed — prices: {_p or '—'} · fundamentals: {_f or '—'}")
        else:
            st.caption("No cache yet — click **Build all** first.")

# group columns shown as tabs (V40 / V40-N / V200)
real_cols = [c for c in GROUP_COLUMNS if c in groups]
tab_cols = real_cols if "ALL_NSE" in cfg["groups"] else [c for c in cfg["groups"] if c in groups]
GLABEL = {"v_40": "V40", "v_40_next": "V40-N", "v_200": "V200"}
# include any constituents the user jumped to from an index (may be outside V40/V40-N/V200)
allowed = sorted({t for c in tab_cols for t in groups.get(c, [])}
                 | st.session_state.get("extra_tickers", set()))
if not allowed:
    st.warning("No tickers in the sheet for this strategy "
               "(check the `stock_classifications` tab: v_40 / v_40_next / v_200).")
    st.stop()

# scan -> status per ticker (needs cache); used for colour + sorting.
# Fold the engine/core file mtimes into the cache key so the scan (and thus the
# sidebar colours) ALWAYS recompute when the strategy code changes. Otherwise a
# Streamlit hot-reload can keep a STALE scan whose green/red disagrees with the
# freshly-recomputed main panel.
try:
    _eng_sig = f"{os.path.getmtime(vs.__file__):.0f}.{os.path.getmtime(core.__file__):.0f}"
except Exception:
    _eng_sig = "0"
token = (cache.get("built", "none") if cache else "none") + "|" + _eng_sig
scan_df = scan_strategy(skey, token) if cache else pd.DataFrame()
status_map, exp_map = {}, {}
if not scan_df.empty:
    for _, r in scan_df.iterrows():
        status_map[r["Ticker"]] = r["Status"]
        exp_map[r["Ticker"]] = r["Exp Profit %"] if pd.notna(r["Exp Profit %"]) else -1e9

def _rank(t):
    s = status_map.get(t)
    if s is None:
        return 3                       # grey: unknown / not in cache
    if s.startswith("🟢"):
        return 0                       # green: investable now
    if s.startswith("🟡"):
        return 1                       # yellow: review
    return 2                           # red: scanned, no signal

def _pfx(t):
    s = status_map.get(t)
    if s is None:
        return "neu_"
    return {"🟢 READY": "rdy_", "🟡 REVIEW": "rev_"}.get(s, "not_")

def _dot(t):
    return {"rdy_": "🟢", "rev_": "🟡", "not_": "🔴", "neu_": "⚪"}[_pfx(t)]

# default / validate the selected ticker
if "sel_ticker" not in st.session_state or st.session_state.sel_ticker not in allowed:
    st.session_state.sel_ticker = sorted(
        allowed, key=lambda t: (_rank(t), -exp_map.get(t, -1e9), t))[0]

# ---- sidebar: searchable ticker picker (🟢 buy-now first) — replaces the old ~200-button grid ----
def _pick_ticker():
    _p = st.session_state.get("ticker_pick")
    if _p and _p in allowed:
        st.session_state.sel_ticker = _p
        st.session_state.user_picked = True
        st.session_state.pop("jumped_from", None)     # an explicit pick clears the "from index" banner

def _tier_str(t):
    return " · ".join(GLABEL.get(g, g) for g in _groups_of(t)) or "All-NSE"

with st.sidebar:
    _buynow = st.toggle("🟢 Ready-to-buy only", value=True, key="buynow_only",
                        help="Show only the stocks this strategy rates 🟢 READY right now. "
                             "Turn off to search the whole list.")
    _tier_opts = [GLABEL.get(c, c) for c in tab_cols]
    _tier_sel = st.multiselect("Limit to tier", _tier_opts, default=[], key="tier_filter",
                               help="V40 / V40-N / V200. Leave empty for all.") if len(_tier_opts) > 1 else []
    _opts = sorted(allowed, key=lambda t: (_rank(t), -exp_map.get(t, -1e9), t))
    if _tier_sel:
        _opts = [t for t in _opts if set(_tier_str(t).split(" · ")) & set(_tier_sel)]
    if _buynow:
        _ready = [t for t in _opts if _pfx(t) == "rdy_"]
        if _ready:
            _opts = _ready
        else:
            st.caption("_No 🟢 READY names match — showing all (toggle off to keep browsing)._")
    _cur = st.session_state.get("sel_ticker")
    if _cur in allowed and _cur not in _opts:            # keep the current pick selectable even if filtered out
        _opts = [_cur] + _opts
    if not _opts:
        _opts = sorted(allowed)
    st.selectbox("🔎 Pick a stock (type to search)", _opts,
                 index=(_opts.index(_cur) if _cur in _opts else 0),
                 format_func=lambda t: f"{_dot(t)} {t}  ·  {_tier_str(t)}",
                 key="ticker_pick", on_change=_pick_ticker)
    st.caption(f"{len(_opts)} shown · 🟢 buy now · 🟡 review · 🔴 not now"
               + ("" if cache else "  _(build cache to colour)_"))

ticker = st.session_state.sel_ticker

# don't auto-fetch from the network on first load with no cache — wait for the
# user to build the cache or explicitly click a ticker
if not cache and not st.session_state.get("user_picked"):
    st.info("👈 **Build the data cache** (sidebar) to colour & rank every ticker, "
            "or **click any ticker** to load it live on demand.")
    st.stop()

# ============================================================================
# MAIN  —  Meaning · Metrics · Plot · Back-testing
# ============================================================================
df_raw = get_df(ticker, cache)
if df_raw is None or len(df_raw) < 30:
    st.error(f"Not enough price data for {ticker}.")
    st.stop()
needs_fund = skey in ("lifetime_high", "three_x_three")
fund = get_fund(ticker, cache, needs_fund)
a = core.analyze(skey, ticker, df_raw, fundamentals=fund)
if not a.get("summary"):                 # safety net — should not happen (price always carried)
    st.error(f"**{ticker}** — no price data available.")
    st.stop()
k = core.kpi_block(a)
ready = k["ready"]
badge = {"YES": "🟢 READY", "REVIEW": "🟡 REVIEW", "NO": "🔴 NOT NOW"}.get(ready, ready)
publish_context({"kind": "stock", "ticker": ticker, "strategy": _STRAT_SHORT.get(skey, skey),
                 "price": (f"{k['current_price']:,.2f}" if k.get("current_price") else None),
                 "status": ready})   # for the 📒 notes "attach current view"

# ---- MEANING ----
with st.container(border=True):
    st.markdown(f"### {ticker}  ·  {core.STRATEGY_LABELS.get(skey, skey)}  ·  {badge}")
    st.markdown(core.STRATEGY_BLURB.get(skey, ""))
    if a["summary"].get("insufficient_history"):
        st.warning(f"⚠️ **{ticker}** has limited price history for "
                   f"**{core.STRATEGY_LABELS.get(skey, skey)}** (some strategies need ~1–1.5 yrs). "
                   "Current price is shown, but there's no reliable signal or backtest yet.")

# ---- TICKER PROFILE — groups + applicable strategies & their status ----
_prof = multi_strategy_status(ticker, token)
with st.container(border=True):
    _jf = st.session_state.get("jumped_from")
    if _jf:
        st.markdown(f"📍 **{ticker}** opened from **{_jf}**.")
    _GL2 = {"v_40": "V40", "v_40_next": "V40-N", "v_200": "V200"}
    _mem = _prof.get("groups") or []
    if _mem:
        st.markdown("**In groups:** " + " · ".join(f"`{_GL2[g]}`" for g in _mem)
                    + " — eligible for those strategies.")
    else:
        st.markdown("**In groups:** _not in V40 / V40-N / V200_ (e.g. a superstar pick) — so the group "
                    "rule can't gate it. **⭐ 3× in 3 yrs (All-NSE)** is the one *designed* for it, but "
                    "**every** strategy is checked below (off-label) so you don't miss a signal.")
    _rows = _prof.get("rows") or []
    if _rows:
        _TAG = {"YES": "sgrdy", "REVIEW": "sgrev", "NO": "sgnot"}
        _DOT = {"sgrdy": "🟢", "sgrev": "🟡", "sgnot": "🔴", "sgneu": "⚪"}
        st.markdown("**Strategies — click one to load its full chart · backtest · fundamentals below** "
                    "(🟢 ready · 🟡 review · 🔴 not now"
                    + (" · ⭐ = designed for this stock):" if not _mem else "):"))
        _bc = st.columns(3)
        for _i, _r in enumerate(_rows):
            _tag = _TAG.get(_r.get("Status"), "sgneu")
            _star = "⭐ " if ((not _mem) and _r.get("designed")) else ""   # ⭐ the All-NSE 3×3 for off-universe
            _bc[_i % 3].button(f"{_DOT[_tag]} {_star}{_r['Strategy']}",
                               key=f"{_tag}_profstrat_{ticker}_{_r['key']}", use_container_width=True,
                               on_click=_set_strategy, args=(_r["key"],))
        with st.expander(f"📊 Details — exp profit · time-to-target · success · opportunities ({len(_rows)})",
                         expanded=bool(_jf)):
            _tbl = pd.DataFrame(_rows).drop(columns=["key", "designed"], errors="ignore")
            st.dataframe(
                filter_ui(_tbl, "stock_strategies"), hide_index=True, use_container_width=True,
                column_config={
                    "Exp profit %": st.column_config.NumberColumn("Exp profit %", format="%.2f%%"),
                    "Exp. days": st.column_config.NumberColumn(
                        "Exp. days", format="%d d",
                        help="Expected time to reach target (PERT-weighted from the backtest's closed "
                             "trades). Pair with Exp profit % for a return-per-time view."),
                    "Median days": st.column_config.NumberColumn(
                        "Median days", format="%d d", help="Median time-to-target of closed trades."),
                    "Success %": st.column_config.NumberColumn("Success %", format="%.2f%%"),
                })
            st.caption("Every strategy's current status with **expected profit, time-to-target "
                       "(Exp. days / Median days), success rate & opportunities** — score them side by "
                       "side, then click a button above (or the sidebar **Strategy** dropdown) to load it.")

# ---- FUNDAMENTALS (graphical) — for ANY ticker ----
# Plots revenue & net-profit PER QUARTER and PER YEAR so you can SEE how the metrics
# changed over time (not just a pass/fail label). Available on every strategy.
_fund_any = (cache or {}).get("fund", {}).get(ticker) if cache else None
if not _fund_any and (not cache or ticker not in cache.get("data", {})):
    _fund_any = fetch_fund(ticker)           # off-universe ticker (e.g. superstar pick) → live-fetch
_ffig, _finfo = core.build_fundamentals_chart(_fund_any)
if _ffig is not None:
    with st.expander("📊 Fundamentals — revenue, net-profit & key ratios", expanded=False):
        st.markdown("**Revenue & net-profit over time** "
                    "(gold = period-best · ★ + green outline = latest)")
        st.plotly_chart(_ffig, use_container_width=True, config={"displaylogo": False})

        # --- Key ratios (yfinance) ---
        def _mv(v, suf="", pct=False):
            if v is None:
                return "—"
            return f"{v:,.2f}{'%' if pct else suf}"
        st.markdown("**Key ratios** — _from yfinance (best-effort)_")
        _r = st.columns(7)
        _r[0].metric("PE (TTM)", _mv(_fund_any.get("pe_trailing")))
        _r[1].metric("Debt/Equity", _mv(_fund_any.get("debt_to_equity")))
        _r[2].metric("ROE", _mv(_fund_any.get("roe_pct"), pct=True))
        _r[3].metric("ROCE", _mv(_fund_any.get("roce_pct"), pct=True))
        _r[4].metric("Sales growth", _mv(_fund_any.get("sales_growth_pct"), pct=True))
        _r[5].metric("Profit growth", _mv(_fund_any.get("profit_growth_pct"), pct=True))
        _r[6].metric("Intrinsic (Graham)", _mv(_fund_any.get("intrinsic_graham")))
        _ib = _fund_any.get("intrinsic_basis")
        if _ib:
            _mpe, _mpbv = _fund_any.get("graham_max_pe"), _fund_any.get("graham_max_pbv")
            _detail = (f" — maxPE **{_mpe}** (5yr sales-growth {_fund_any.get('sales_growth_5yr_pct','?')}%×1.5), "
                       f"maxPBV **{_mpbv}** (5yr ROCE {_fund_any.get('roce_5yr_pct','?')}%÷8)"
                       if (_mpe is not None and _mpbv is not None) else "")
            st.caption(f"💡 **Intrinsic value** uses *{_ib}*{_detail}. Highly subjective — a rough "
                       "value guide, not a target. (Screener's number will differ — different "
                       "underlying data.)")

        _tags = []
        if _finfo["rev_highest"] is not None:
            _tags.append(("✅" if _finfo["rev_highest"] else "❌") + " TTM revenue highest-ever")
        if _finfo["np_highest"] is not None:
            _tags.append(("✅" if _finfo["np_highest"] else "❌") + " TTM net-profit highest-ever")
        if _finfo["quarter_improved"] is not None:
            _tags.append(("✅" if _finfo["quarter_improved"] else "❌") + " latest qtr improved (QoQ)")
        if _finfo["track_record"] is not None:
            _tags.append(("✅" if _finfo["track_record"] else "❌") + " good track record")
        st.caption(" · ".join(_tags) + f"  ·  {_finfo['quarters']} quarters, "
                   f"{_finfo['years']} years available (yfinance free tier ≈ 4–5 quarters / 4 years; "
                   "“highest-ever” = highest within that window).")
        st.caption("⚠️ **Source: Yahoo (yfinance).** Indian fundamentals here often "
                   "**differ from Screener/Groww** (standalone vs consolidated, restated quarters, "
                   "different line items; net-profit & ROE/ROCE especially shaky for NSE). Treat as "
                   "*indicative only* and verify before acting — which is why fundamental "
                   "strategies show **REVIEW**, not an auto-buy. _(Shareholding — promoter/FII/DII/"
                   "pledge — isn't in yfinance and is currently disabled.)_")
elif skey in ("lifetime_high", "three_x_three"):
    with st.container(border=True):
        st.caption("📊 No fundamentals cached for this ticker — tick **Fundamentals in cache**, "
                   "then **Build / refresh cache** to see the revenue & net-profit history here.")

# ---- METRICS ----
with st.container(border=True):
    # row 1 — price levels + durations
    r1 = st.columns(6)
    r1[0].metric("Current", f"{k['current_price']:.2f}" if k['current_price'] is not None else "—")
    r1[1].metric("Entry", f"{k['entry']:.2f}" if k['entry'] else "—")
    _tgt = ("~" if k.get("target_estimated") else "") + f"{k['target']:.2f}" if k['target'] else "—"
    r1[2].metric("Target", _tgt)
    r1[3].metric("Exp. profit", f"{k['exp_profit_pct']:.1f}%" if k['exp_profit_pct'] is not None else "—")
    r1[4].metric("Approx. time", f"{k['exp_duration_days']} d" if k['exp_duration_days'] else "—")
    r1[5].metric("Median days", k["median_days"] if k["median_days"] else "—")
    # success rate stays visible as the one headline outcome; the rest is one click away
    st.metric("Success rate (of closed trades)",
              f"{k['success_rate']:.1f}%" if k['success_rate'] is not None else "—")
    with st.expander("📉 More trade stats — counts, averages, recency", expanded=False):
        # row 2 — outcome rates + counts (all out of CLOSED trades)
        r2 = st.columns(6)
        r2[0].metric("Success (of closed)", f"{k['success_rate']:.1f}%" if k['success_rate'] is not None else "—")
        r2[1].metric("Non-loss (of closed)", f"{k['nonloss_rate']:.1f}%" if k['nonloss_rate'] is not None else "—")
        r2[2].metric("Avg win", f"{k['avg_win_profit']:.1f}%" if k['avg_win_profit'] is not None else "—")
        r2[3].metric("Opportunities", k["total_ops"] if k["total_ops"] is not None else "—")
        r2[4].metric("Succeeded / Closed",
                     f"{k['total_succ']} / {k['total_closed']}"
                     if (k['total_succ'] is not None and k['total_closed'] is not None) else "—")
        r2[5].metric("Non-loss / Closed",
                     f"{k['total_nonloss']} / {k['total_closed']}"
                     if (k['total_nonloss'] is not None and k['total_closed'] is not None) else "—")
        # row 3 — recency
        r3 = st.columns(6)
        r3[0].metric("Last opp.", str(pd.to_datetime(k["last_opp_date"]).date())
                     if k["last_opp_date"] is not None else "—")
        r3[1].metric("Last result", (k["last_opp_result"] or "—") if k["last_opp_date"] is not None else "—")
        st.caption("All rates are **out of CLOSED trades** (Open trades — still held / target not yet "
                   "reached — are excluded). **Success** = target reached **on-pace** (3×-in-3yr: "
                   "+20%≈6mo · +44%≈1y · +100%≈23mo · +200%=3y). **Non-loss** = reached target at all "
                   "(on-pace *or* off-pace); for SMA/Knoxville it excludes trades sold below entry.")
    if k.get("target_estimated"):
        st.caption("〜 **Target/Exp. profit are estimated** from the historical median winning "
                   "move — this strategy exits on a signal, not a fixed price target.")
    if ready == "REVIEW":
        st.info("**REVIEW** = price conditions met, but this strategy needs fundamentals / "
                "human judgment (e.g. reason-of-fall) before acting — by design.")

# ---- PLOT (shared chart block: candles + all indicators + trendlines + measure) ----
render_chart_block(a, ticker)

# ---- BACK-TESTING ----
with st.expander("🔬 Back-testing — how this strategy did on past signals", expanded=False):
    st.caption("**Result:** **Success** = target/exit hit on-pace · **Slow (off-pace)** = hit, "
               "but too slowly to count · **Loss** = sold below entry (SMA/Knoxville) · "
               "**Open** = still held / target not reached.")
    st.caption("ℹ️ **One position per price level:** while a trade toward a target is open, a repeat "
               "entry toward the **same target** is counted only if it's at a **lower** price "
               "(genuine averaging-down). Same-or-higher re-entries while holding are dropped, so a "
               "stretch of overlapping signals counts as **one** opportunity — not many.")
    opps = a["opps"]
    if opps.empty:
        st.caption("No historical signals for this ticker / strategy.")
    else:
        show = opps.sort_values("Entry_Date", ascending=False) if "Entry_Date" in opps else opps
        st.dataframe(filter_ui(show, "indices_summary"), use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download trade log (CSV)", show.to_csv(index=False).encode(),
                           file_name=f"{ticker}_{skey}_trades.csv", mime="text/csv")
