#!/usr/bin/env python3
"""
refresh.py — headless nightly cache builder (run by .github/workflows/daily.yml).

Builds the V-universe dashboard cache (prices + fundamentals) into
vivek_output/dashboard_cache.pkl, which the workflow then publishes as the
'data-latest' GitHub Release asset. The Streamlit app downloads that at boot.

Produces the SAME payload shape the app's build_full_cache() writes:
    {groups, data, fund, built, built_prices, built_fund}

Credentials come ONLY from env GCP_SERVICE_ACCOUNT (full service-account JSON).
The FII/DII "superstar" Google Sheets are refreshed separately by executing the
notebook (see daily.yml) — this script handles the V-universe price/fundamentals
cache that makes the dashboard fast.
"""
import os
import sys
import json
import pickle
import concurrent.futures
from datetime import datetime, timedelta

import pandas as pd
import vivek_strategies as vs   # for fetch_fundamentals (Streamlit-free engine)

# ── config (mirrors the app's CONFIG block) ──────────────────────────────────
SHEET_KEY      = "1qzj_Va1Xle6Pnz7HDUsO1iPaUeGEv_VLJFXE4-zZYNw"
WORKSHEET_NAME = "stock_classifications"
GROUP_COLUMNS  = ["v_40", "v_40_next", "v_200"]
YEARS          = 20
OUTPUT_DIR     = "vivek_output"
CACHE_PKL      = os.path.join(OUTPUT_DIR, "dashboard_cache.pkl")
MIN_PRICE_FRACTION = 0.5        # if fewer than this fraction of tickers priced -> looks broken, don't overwrite


def _service_account_info():
    raw = os.environ.get("GCP_SERVICE_ACCOUNT")
    if not raw:
        sys.exit("ERROR: GCP_SERVICE_ACCOUNT env var not set — required for the headless cache build.")
    try:
        return json.loads(raw)
    except Exception as e:
        sys.exit(f"ERROR: GCP_SERVICE_ACCOUNT is not valid JSON: {e}")


def _gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(_service_account_info(), scopes=scopes)
    return gspread.authorize(creds)


def read_groups():
    from gspread_dataframe import get_as_dataframe
    ws = _gspread_client().open_by_key(SHEET_KEY).worksheet(WORKSHEET_NAME)
    df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
    groups = {}
    for col in GROUP_COLUMNS:
        if col in df.columns:
            groups[col] = sorted({str(x).strip().upper() for x in df[col].dropna()
                                  if str(x).strip() and str(x).strip().lower() != "nan"})
    return groups


def fetch_one(ticker, years=YEARS):
    """Faithful copy of the app's _fetch_one_raw: daily OHLCV via yfinance (.NS) with a
    fast_info backfill of the trailing all-NaN bar so prices aren't a day stale."""
    import yfinance as yf
    today = datetime.now().date()
    end = today + timedelta(days=1)              # yfinance `end` is EXCLUSIVE -> +1 to include today
    start = today - timedelta(days=years * 365)
    tk = yf.Ticker(f"{ticker}.NS")
    df = tk.history(start=start, end=end, interval="1d")
    if df.empty:
        return None
    df = df.reset_index()
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    if cols and len(df) and df[cols].iloc[-1].isna().all():
        _dt = df.iloc[-1].get("Date")
        _d = _dt.date() if hasattr(_dt, "date") else None
        if _d is None or _d <= today:
            try:
                _fi = tk.fast_info
                _lp = float(_fi.last_price)
                _o = float(getattr(_fi, "open", None) or _lp)
                _hi = float(getattr(_fi, "day_high", None) or _lp)
                _lo = float(getattr(_fi, "day_low", None) or _lp)
            except Exception:
                _lp = float("nan")
            if _lp == _lp and _lp > 0:
                fill = {"Open": _o, "High": max(_hi, _lp), "Low": min(_lo, _lp), "Close": _lp}
                for c in cols:
                    df.loc[df.index[-1], c] = fill.get(c, _lp)
                if "Volume" in df.columns and pd.isna(df.loc[df.index[-1], "Volume"]):
                    df.loc[df.index[-1], "Volume"] = 0
    if cols:
        df = df.dropna(subset=cols)
    return df.reset_index(drop=True) if not df.empty else None


def _parallel(fn, tickers, label, workers=10):
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, t): t for t in tickers}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            t = futs[fut]
            try:
                out[t] = fut.result()
            except Exception:
                out[t] = None
            done += 1
            if done % 25 == 0 or done == len(tickers):
                print(f"  {label}: {done}/{len(tickers)}", flush=True)
    return out


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Reading group lists from Google Sheet…", flush=True)
    groups = read_groups()
    all_t = sorted(set().union(*[set(v) for v in groups.values()])) if groups else []
    print(f"{len(all_t)} unique tickers across groups {list(groups)}", flush=True)
    if not all_t:
        sys.exit("ERROR: no tickers in stock_classifications — refusing to overwrite the cache.")

    now = datetime.now().isoformat()

    print("Fetching prices…", flush=True)
    data = {t: df for t, df in _parallel(fetch_one, all_t, "prices").items() if df is not None}

    print("Fetching fundamentals…", flush=True)
    fund = {t: (f or {}) for t, f in _parallel(vs.fetch_fundamentals, all_t, "fundamentals").items()}

    # Safety: a broken/blocked fetch shouldn't clobber a good cache with mostly-empty data.
    if len(data) < max(1, int(MIN_PRICE_FRACTION * len(all_t))):
        sys.exit(f"ERROR: only {len(data)}/{len(all_t)} price series fetched — looks broken; "
                 "NOT overwriting the cache.")

    payload = {"groups": groups, "data": data, "fund": fund,
               "built": now, "built_prices": now, "built_fund": now}
    with open(CACHE_PKL, "wb") as f:
        pickle.dump(payload, f)
    size_mb = os.path.getsize(CACHE_PKL) / 1e6
    print(f"✅ cache written: {len(data)} priced, {len(fund)} fundamentals, "
          f"{size_mb:.1f} MB -> {CACHE_PKL}", flush=True)


if __name__ == "__main__":
    main()
