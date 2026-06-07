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
    """Plain fetch — safe to call from worker threads (no Streamlit cache)."""
    import yfinance as yf
    today = datetime.now().date()
    end = today + timedelta(days=1)              # yfinance `end` is EXCLUSIVE -> +1 to include today's candle
    start = today - timedelta(days=years * 365)
    tk = yf.Ticker(f"{ticker}.NS")
    df = tk.history(start=start, end=end, interval="1d")
    if df.empty:
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


def _open_in_strategy(ticker, skey, from_index):
    """Jump from the index page straight into Stock Analysis for `ticker`, preselecting `skey`
    (the Strategy dropdown reads st.session_state.strat_sel)."""
    st.session_state.sel_ticker = ticker
    st.session_state.user_picked = True
    st.session_state.app_mode = "📊 Stocks"
    st.session_state.setdefault("extra_tickers", set()).add(ticker)
    st.session_state.jumped_from = from_index
    st.session_state.strat_sel = skey


def _open_stock(ticker, from_label):
    """Open `ticker` in Stock Analysis (keeps the current strategy) — e.g. from a superstar's
    holding. Runs as a button on_click, so app_mode is set before any widget is instantiated."""
    st.session_state.sel_ticker = ticker
    st.session_state.user_picked = True
    st.session_state.app_mode = "📊 Stocks"
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

    if do_prices:
        data = {t: df for t, df in _parallel(_fetch_one_raw, "prices").items() if df is not None}
        ts_prices = now
    if do_fund:
        fund = {t: (f or {}) for t, f in _parallel(vs.fetch_fundamentals, "fundamentals").items()}
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
    across EVERY ticker already in the cache (V-universe + any superstar stocks the nightly build
    cached). Reads ONLY cached data — never live-fetches — so it's fast regardless of universe size.
    The chosen strategies are applied to ALL companies (not gated by group design). Columns mirror
    the KPI block. Keyed by `token` (cache version). Uses module globals `cache`/`groups`."""
    datac = (cache or {}).get("data", {})
    fundc = (cache or {}).get("fund", {})
    strategies = tuple(s for s in strategies if s in vs.STRATEGY_CONFIG)

    rows = []
    for t in sorted(datac.keys()):
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
      wrap.style.left = S.left + 'px'; wrap.style.top = S.top + 'px';
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

    if (UI.left != null && UI.top != null) { wrap.style.right = 'auto'; wrap.style.bottom = 'auto'; wrap.style.left = UI.left + 'px'; wrap.style.top = UI.top + 'px'; }
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
  section[data-testid="stSidebar"] div[class*="st-key-rdy_"] button{background:#1b8f4d;color:#fff;border:1px solid #14633a;}
  section[data-testid="stSidebar"] div[class*="st-key-rev_"] button{background:#caa10a;color:#111;border:1px solid #8a6d00;}
  section[data-testid="stSidebar"] div[class*="st-key-not_"] button{background:#b03a2e;color:#fff;border:1px solid #7d2820;}
  section[data-testid="stSidebar"] div[class*="st-key-neu_"] button{background:#3a3f44;color:#eee;border:1px solid #555;}
  section[data-testid="stSidebar"] div[class*="st-key-"] button{padding:1px 5px;font-size:0.72rem;min-height:0;line-height:1.5;}
  /* main-area strategy buttons (constituent table) — coloured by current READY status */
  div[class*="st-key-sgrdy_"] button{background:#1b8f4d;color:#fff;border:1px solid #14633a;}
  div[class*="st-key-sgrev_"] button{background:#caa10a;color:#111;border:1px solid #8a6d00;}
  div[class*="st-key-sgnot_"] button{background:#b03a2e;color:#fff;border:1px solid #7d2820;}
  div[class*="st-key-sgneu_"] button{background:#3a3f44;color:#eee;border:1px solid #555;}
  .cwrap{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px;}
  .cchip{background:#262b31;color:#dfe3e8;border:1px solid #3a4048;border-radius:6px;
         padding:2px 8px;font-size:0.70rem;font-family:ui-monospace,Menlo,monospace;white-space:nowrap;}
</style>""", unsafe_allow_html=True)

cache = load_cache()
groups = get_groups(cache)

# ---- deep-link from a 📒 note: ?open=TICKER → jump into that stock's analysis ----
_goto = st.query_params.get("open")
if _goto:
    st.session_state.sel_ticker = _goto
    st.session_state.user_picked = True
    st.session_state.app_mode = "📊 Stocks"
    st.session_state.setdefault("extra_tickers", set()).add(_goto)
    st.session_state.jumped_from = "📒 a note"
    try:
        del st.query_params["open"]            # consume it so a later rerun doesn't re-trigger
    except Exception:
        pass

# ---- mode toggle (top of sidebar): Stocks  ⟷  Indices ----
with st.sidebar:
    _mode = st.radio("Mode", ["📊 Stocks", "🌐 Indices", "⭐ Superstars", "🔔 Alerts", "💎 Investable now"],
                     horizontal=True, key="app_mode")

# single heading per mode (no duplicate title)
st.title("📈 SPritamDas — " + {"🌐 Indices": "Index Analysis", "⭐ Superstars": "Superstar Analysis",
                                "🔔 Alerts": "FII/DII Alerts", "💎 Investable now": "Investable Now"}.get(_mode, "Stock Analysis"))
render_floating_calculator()   # 🧮 draggable, always-on-top calculator (hovers over the chart)
render_floating_notes()        # 📒 draggable, always-on-top notes (context-aware, persists in the browser)

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
    st.caption("Every company in the cache (V-universe + any superstar stocks the nightly build includes) is checked "
               "against **V20 · Lifetime High · 52-Week Low**, applied to all of them regardless of group. Read "
               "entirely from the **nightly cache** (rebuilt 5 PM) — no live fetching, so it loads in seconds. "
               "To include the superstar/off-universe stocks, run the Action once so they get added to the cache.")
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
                        st.session_state.user_picked = True
                        st.session_state.app_mode = "📊 Stocks"
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
            fetch_superstar_summary.clear(); fetch_superstar_holdings.clear(); st.rerun()
        st.caption("List + metrics from the FII/DII sheet (run the notebook to refresh); "
                   "holdings scraped **live** from Trendlyne, cached ~6 h.")

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

    # ---- filters (combine with AND; numeric ones apply only when you enter a value) ----
    def _ncol(df, c):
        return pd.to_numeric(df[c], errors="coerce") if c in df.columns else pd.Series(float("nan"), index=df.index)
    _f1 = st.columns([3, 3])
    _q = _f1[0].text_input("🔎 Search investor", key="sstar_q").strip().lower()
    _sig_present = [s for s in ["STRONG BUY", "BUY", "WATCH", "HOLD", "AVOID"]
                    if "signal" in _sdf.columns and s in set(_sdf["signal"].astype(str))]
    _sigs = _f1[1].multiselect("Signal (any of)", _sig_present, key="sstar_sig")
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
    _wanted = ["name", "type", "signal", "confidence_score", "score_vs_benchmark", "alpha_ann_pct",
               "sharpe_ratio", "nifty_sharpe_ratio", "sharpe_x_nifty", "information_ratio",
               "ann_return_pct", "nifty_ann_return_pct", "rolling_1y_pct",
               "max_drawdown_pct", "nifty_max_drawdown_pct", "quarters_tracked", "current_net_worth_cr"]
    _disp = view[[c for c in _wanted if c in view.columns]].reset_index(drop=True)
    _ltok = hashlib.md5("|".join(_disp["name"].astype(str)).encode()).hexdigest()[:10]   # reset selection if list changes
    _ev = st.dataframe(
        _disp, hide_index=True, use_container_width=True, height=420,
        on_select="rerun", selection_mode="single-row", key="sstar_list_" + _ltok,
        column_config={
            "name": st.column_config.TextColumn("Investor"),
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
    st.caption("**Sharpe ×Nifty** = how many times the index's reward-per-risk (Nifty Sharpe ≈ 0.12) — "
               "read this, not the raw Sharpe's 0–3 scale. **Info Ratio** = consistency of beating the "
               "benchmark (>0.5 good). Sorted best-first (signal → confidence → alpha). **Reminder:** alpha/Sharpe come from "
               "*net-worth* changes (contaminated by capital flows) — treat as **directional**, and verify "
               "any name in **Stock Analysis** before acting.")

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

        _link = _u("links") or _u("portfolio_url")
        if not _link:
            st.info("No Trendlyne portfolio link stored for this investor.")
        else:
            _hold, _quarters, _err = fetch_superstar_holdings(_link)
            if _hold.empty:
                st.warning(f"Couldn't load the holdings journey: {_err}")
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
            st.rerun()
        st.caption("Alerts read the FII/DII sheet (run the notebook to refresh the underlying data).")

    st.markdown("## 🔔 FII/DII alerts — quality superstar buys (vs **Nifty 50**)")

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
            st.dataframe(_recent[_qcols].reset_index(drop=True), hide_index=True, use_container_width=True,
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
        with st.expander("🔎 See which stocks superstars moved on"):
            _topnew = (_mv0[_mc0 == "NEW"].groupby(["ticker", "company"], dropna=False).agg(
                          investors=("investor", "nunique"),
                          avg_stake=("latest_stake", lambda s: round(float(pd.to_numeric(s, errors="coerce").mean()), 2)),
                          value_cr=("value_cr", lambda s: round(float(pd.to_numeric(s, errors="coerce").fillna(0).sum()), 1)),
                       ).reset_index().sort_values(["investors", "value_cr"], ascending=False).head(20)
                       ) if _cnt["NEW"] else pd.DataFrame()
            if not _topnew.empty:
                st.markdown("**🟢 Most-bought NEW positions** (ranked by how many superstars bought in):")
                st.dataframe(_topnew, hide_index=True, use_container_width=True,
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
                st.dataframe(_byinv, hide_index=True, use_container_width=True, height=320,
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

    # ---- 🚨 PIPELINE HEALTH (flags issues in the data/notebook pipeline) ----
    with st.container(border=True):
        st.markdown("### 🚨 Pipeline health")
        alarms = []   # (level_emoji, message)
        if _sdf.empty:
            alarms.append(("🔴", "Investor **summary missing/empty** — run the `fii_dii` notebook "
                                 "(India: Cell 6 scrape → Cell 9 metrics)."))
        else:
            if "data_to" in _sdf.columns:
                _dt = pd.to_datetime(_sdf["data_to"], errors="coerce").max()
                if pd.notna(_dt) and (datetime.now() - _dt).days > 130:
                    alarms.append(("🟡", f"Investor data looks **stale** — latest disclosed quarter is "
                                         f"{_dt.date()} ({(datetime.now() - _dt).days} days ago). Re-run the notebook."))
            if "ann_return_pct" in _sdf.columns and len(_sdf):
                _full = int(pd.to_numeric(_sdf["ann_return_pct"], errors="coerce").notna().sum())
                if _full / len(_sdf) < 0.7:
                    alarms.append(("🟡", f"**Low metric coverage** — only {_full}/{len(_sdf)} investors have full "
                                         "metrics (a scrape may have partially failed / been throttled)."))
        _ms = fetch_master_stock()
        if _ms.empty:
            alarms.append(("🟡", "**master_stock not built** — run notebook Cell 14 (`build_master_stock`)."))
        elif "as_of" in _ms.columns:
            _msd = pd.to_datetime(_ms["as_of"], errors="coerce").max()
            if pd.notna(_msd) and (datetime.now() - _msd).days > 8:
                alarms.append(("🟡", f"**master_stock is stale** — last built {_msd.date()} "
                                     f"({(datetime.now() - _msd).days} days ago). Re-run Cell 14 (it's meant to run daily)."))
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
                alarms.append(("🟡", "A scrape is **in progress or was interrupted** — checkpoint(s) present: "
                                     f"`{', '.join(_ck)}`. If no run is active, re-run the notebook to finish "
                                     "(it resumes & retries failures)."))
        except Exception:
            pass
        if not alarms:
            st.success("🟢 All clear — summary present & fresh · master_stock current · no interrupted runs.")
        else:
            for _lvl, _msg in alarms:
                (st.error if _lvl == "🔴" else st.warning)(f"{_lvl} {_msg}")

    if _sdf.empty:
        st.stop()

    # ---- ✅ ALERT CRITERIA (defaults = your spec; tweakable) ----
    st.markdown("### ✅ Alert criteria")
    _af1 = st.columns([3, 3])
    _aq = _af1[0].text_input("🔎 Search investor", key="alert_q").strip().lower()
    _asig = _af1[1].multiselect("Signal (any of)", ["STRONG BUY", "BUY", "WATCH", "HOLD", "AVOID"],
                                default=["STRONG BUY", "BUY"], key="alert_sig")
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
                _qual[_cols].reset_index(drop=True), hide_index=True, use_container_width=True, height=380,
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

# ---- sidebar: strategy + cache controls ----
with st.sidebar:
    skey = st.selectbox("Strategy", options=list(vs.STRATEGY_CONFIG.keys()),
                        format_func=lambda k: core.STRATEGY_LABELS.get(k, k), key="strat_sel")
    cfg = vs.STRATEGY_CONFIG[skey]

    st.markdown("**Cache** — refresh from Yahoo")
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
        _through = None
        try:                                         # newest candle date across cached tickers
            _through = max(df["Date"].iloc[-1] for df in (cache.get("data") or {}).values()
                           if df is not None and len(df))
            _through = pd.Timestamp(_through).date()
        except Exception:
            _through = None
        if _through is not None:
            _ago = (datetime.now().date() - _through).days
            _tag = "✅" if _ago <= 0 else ("🟢" if _ago <= 3 else "⚠️")
            st.caption(f"🕒 **data through {_through}** {_tag}")
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

# ---- sidebar: colour-coded ticker grid (green = buy now, red = not) ----
clicked = None
with st.sidebar:
    st.markdown("**Tickers** — 🟢 buy now · 🔴 not now"
                + (" · 🟡 review" if any(s.startswith("🟡") for s in status_map.values()) else "")
                + ("" if cache else "  _(build cache to colour)_"))
    for tab, col in zip(st.tabs([GLABEL.get(c, c) for c in tab_cols]), tab_cols):
        with tab:
            items = sorted(set(groups.get(col, [])),
                           key=lambda t: (_rank(t), -exp_map.get(t, -1e9), t))
            gc = st.columns(2)
            for i, t in enumerate(items):
                with gc[i % 2]:
                    mark = "▶ " if t == st.session_state.sel_ticker else ""
                    if st.button(f"{mark}{_dot(t)} {t}", key=f"{_pfx(t)}{col}__{t}",
                                 use_container_width=True):
                        clicked = t

# capture the click only after the whole grid is drawn, then rerun so the ▶
# marker and the main panel both render against one consistent selection
if clicked is not None:
    st.session_state.sel_ticker = clicked
    st.session_state.user_picked = True
    st.session_state.pop("jumped_from", None)        # a grid click clears the "from index" banner
    st.rerun()

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
                _tbl, hide_index=True, use_container_width=True,
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
    with st.container(border=True):
        st.markdown("**📊 Fundamentals — revenue & net-profit over time** "
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
with st.container(border=True):
    st.markdown("**Back-testing — historical opportunities**")
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
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download trade log (CSV)", show.to_csv(index=False).encode(),
                           file_name=f"{ticker}_{skey}_trades.csv", mime="text/csv")
