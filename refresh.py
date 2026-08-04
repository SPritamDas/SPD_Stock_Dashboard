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
import time
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
MIN_FUND_FRACTION  = 0.50       # merged (fresh + carried-forward) fundamentals coverage floor for the V-universe.
                                # Was 0.25 — too loose to catch the mid-run Yahoo throttle that left 39% blank.
RETRY_PASSES       = 2          # extra price passes for the stragglers (transient Yahoo throttling, not real delisting)
RETRY_COOLDOWN_S   = 30         # wait between passes to let Yahoo's rate-limit window reset
RETRY_WORKERS      = 4          # gentler concurrency on retries so we don't re-trigger the throttle
# ---- fundamentals: quarterly data, so DON'T refetch all 660 every night --------------------
# Each fetch_fundamentals() call makes ~5 HTTP requests (quarterly_financials · financials · .info ·
# balance_sheet · income_stmt). 660 x 5 on top of the price phase overran Yahoo's rate-limit window
# mid-run: coverage fell off a cliff at ticker ~346/660 (98% success before, 2% after) and 315 came
# back empty. Fundamentals only move once a quarter, so refresh them on a slow cycle and carry the
# rest forward — that keeps nightly request volume well under the throttle.
FUND_MAX_AGE_DAYS  = 7          # refresh a ticker's fundamentals only if the cached copy is older than this
FUND_RETRY_PASSES  = 2          # re-fetch tickers that came back EMPTY (throttle looks identical to "no data")
FUND_RETRY_COOLDOWN_S = 60      # longer than the price cooldown — the .info endpoint throttles harder
FUND_WORKERS       = 6          # gentler than prices (10): 5 requests per ticker, not 1
PHASE_COOLDOWN_S   = 45         # pause between the price and fundamentals phases so the window resets

# ---- symbol hygiene ------------------------------------------------------------------------
# Single source of truth lives in vivek_strategies (no Streamlit dependency), so refresh.py and the
# app agree on what counts as a ticker and on which renamed symbols to redirect.
SYMBOL_ALIASES = vs.SYMBOL_ALIASES
_clean_symbol = vs.clean_symbol


def _sanitize(values, label):
    """Clean a column of sheet values into a sorted symbol list; report what was dropped so a
    broken sheet cell is VISIBLE in the log instead of silently costing fetches every night."""
    kept, dropped = set(), []
    for v in values:
        s = _clean_symbol(v)
        if s:
            kept.add(s)
        else:
            raw = str(v).strip()
            if raw and raw.lower() != "nan":
                dropped.append(raw)
    if dropped:
        _show = [d[:60] + ("…" if len(d) > 60 else "") for d in dropped[:5]]
        print(f"  ⚠️  {label}: dropped {len(dropped)} unusable cell(s) — fix these in the sheet: "
              f"{_show}", flush=True)
    return sorted(kept)
# FII/DII superstar workbook — so the cache ALSO covers off-universe / superstar-held stocks.
# The app's "Investable now" page then reads those from cache instead of live-fetching them → fast.
FII_SHEET_KEY  = "1rIFmhm37XEJsfXV2Nn1QPfakLG9xvMmEsjJ7YYule8g"
SUPERSTAR_TAB  = "superstar_moves"
SUMMARY_TAB    = "fii_dii_indian_investment_summary"   # per-investor portfolio metrics (signal/return/drawdown)
MAX_DRAWDOWN_FLOOR = -50.0          # only cache picks from investors whose worst drawdown is no worse than this (%)
GOOD_SIGNALS   = ("BUY", "STRONG BUY")


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
    """Read the V-universe LIVE from the `stock_classifications` tab on EVERY run — no caching,
    no memoisation, fresh process each time — so adding/removing a stock in the sheet takes effect
    on the very next run. Values are sanitized (see _clean_symbol) so a broken formula cell can't
    become a 'ticker'."""
    from gspread_dataframe import get_as_dataframe
    ws = _gspread_client().open_by_key(SHEET_KEY).worksheet(WORKSHEET_NAME)
    df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
    groups = {}
    for col in GROUP_COLUMNS:
        if col in df.columns:
            groups[col] = _sanitize(df[col].dropna(), f"{WORKSHEET_NAME}.{col}")
    return groups


def _qualifying_superstars(client):
    """Names of superstars whose PORTFOLIO clears the quality bar — signal in BUY/STRONG BUY,
    annualised return above Nifty's, and max drawdown no worse than -50% (mirrors the dashboard's
    own Superstar/alert filters). Only THEIR new buys are worth caching. Returns a set of lowercased
    names (matches superstar_moves.investor); empty set on any failure -> caller caches V-universe only."""
    from gspread_dataframe import get_as_dataframe
    try:
        ws = client.open_by_key(FII_SHEET_KEY).worksheet(SUMMARY_TAB)
        df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
    except Exception as e:
        print(f"  (superstar summary unavailable — {e}; caching V-universe only)", flush=True)
        return set()
    need = {"name", "signal", "ann_return_pct", "nifty_ann_return_pct", "max_drawdown_pct"}
    missing = need - set(df.columns)
    if missing:
        print(f"  (summary missing columns {sorted(missing)}; caching V-universe only)", flush=True)
        return set()
    sig = df["signal"].astype(str).str.strip().str.upper()
    arr = pd.to_numeric(df["ann_return_pct"],       errors="coerce")     # investor annualised return
    bm  = pd.to_numeric(df["nifty_ann_return_pct"], errors="coerce")     # Nifty over the same window
    mdd = pd.to_numeric(df["max_drawdown_pct"],     errors="coerce")     # negative (worst peak-to-trough)
    keep = sig.isin(GOOD_SIGNALS) & (arr > bm) & (mdd >= MAX_DRAWDOWN_FLOOR)
    names = {str(n).strip().lower() for n in df.loc[keep, "name"].dropna() if str(n).strip()}
    print(f"  {len(names)}/{len(df)} superstars clear the quality bar "
          f"(BUY/STRONG BUY · ann ret > Nifty · drawdown >= {MAX_DRAWDOWN_FLOOR:.0f}%)", flush=True)
    return names


def read_superstar_tickers():
    """NSE tickers NEWLY BOUGHT or ADDED (move NEW/ADD) by QUALITY superstars only (see
    _qualifying_superstars). This keeps the nightly build fast and the cache small while still
    covering the off-universe picks the 'Investable now' page cares about. Best-effort: returns []
    if the data is missing, so the critical V-universe build is never blocked by it."""
    from gspread_dataframe import get_as_dataframe
    client = _gspread_client()
    good = _qualifying_superstars(client)
    if not good:
        return []
    try:
        ws = client.open_by_key(FII_SHEET_KEY).worksheet(SUPERSTAR_TAB)
        df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how="all")
    except Exception as e:
        print(f"  (superstar moves unavailable — {e}; caching V-universe only)", flush=True)
        return []
    if "ticker" not in df.columns or "investor" not in df.columns:
        # 'investor' is the join key for the quality contract; without it we can't tell whose picks
        # these are, so cache nothing extra rather than caching every (incl. low-quality) buy.
        return []
    if "move" in df.columns:                       # only NEW buys + ADDs (what they're accumulating)
        df = df[df["move"].astype(str).str.upper().isin(("NEW", "ADD"))]
    df = df[df["investor"].astype(str).str.strip().str.lower().isin(good)]   # ...only the quality superstars
    # same sanitizer as the V-universe: drops blanks, 'nan', bare BSE scrip codes, stray ".0" floats
    # and any spreadsheet error text that leaked into the tab.
    return _sanitize(df["ticker"].dropna(), f"{SUPERSTAR_TAB}.ticker")


CACHE_KEEP_COLS = ("Date", "Open", "High", "Low", "Close", "Volume")
# yfinance also returns 'Dividends' and 'Stock Splits'. Nothing in the app, the strategy engine or
# the notebook reads them (verified by grep), and they cost ~25% of the pickle. Drop them at build
# time: 125.8 MB -> ~94 MB, losslessly for every consumer.


def fetch_one(ticker, years=YEARS):
    """Faithful copy of the app's _fetch_one_raw: daily OHLCV via yfinance — tries NSE (.NS)
    first, then falls back to BSE (.BO) for names that are BSE-only or transiently missing on
    NSE — with a fast_info backfill of the trailing all-NaN bar so prices aren't a day stale.

    `ticker` is the SHEET's symbol; SYMBOL_ALIASES redirects the fetch for names Yahoo renamed,
    while the caller still caches the result under the sheet's symbol."""
    import yfinance as yf
    symbol = vs.resolve_symbol(ticker)
    today = datetime.now().date()
    end = today + timedelta(days=1)              # yfinance `end` is EXCLUSIVE -> +1 to include today
    start = today - timedelta(days=years * 365)
    tk = df = None
    for suffix in (".NS", ".BO"):                # NSE first, then BSE (BSE-only / NSE-missing names)
        _tk = yf.Ticker(f"{symbol}{suffix}")
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
    if df.empty:
        return None
    df = df[[c for c in CACHE_KEEP_COLS if c in df.columns]]   # drop unread Dividends / Stock Splits
    return df.reset_index(drop=True)


def fetch_fund_one(ticker):
    """fetch_fundamentals for the SHEET's symbol (it applies SYMBOL_ALIASES internally, so renamed
    names like EIH -> EIHOTEL resolve). Returns {} when Yahoo has nothing — NOTE: fetch_fundamentals
    swallows every exception into {}, so a 429 rate-limit is indistinguishable from 'no statements
    for this company'. That is exactly why the caller retries every EMPTY result instead of
    trusting it."""
    return vs.fetch_fundamentals(ticker)


def _parallel(fn, tickers, label, workers=10, per_ticker=5):
    out = {}
    # yfinance calls have no explicit socket timeout, so a single stuck connection could otherwise
    # wedge as_completed (and the executor's blocking shutdown) FOREVER. Cap the whole phase with a
    # generous wall-clock backstop and cancel/abandon the rest on timeout — a hung worker then
    # degrades to a handful of "misses" (recovered by the retry passes) instead of hanging the build.
    overall = max(300, per_ticker * len(tickers))
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(fn, t): t for t in tickers}
    done = 0
    try:
        for fut in concurrent.futures.as_completed(futs, timeout=overall):
            t = futs[fut]
            try:
                out[t] = fut.result()
            except Exception:
                out[t] = None
            done += 1
            if done % 25 == 0 or done == len(tickers):
                print(f"  {label}: {done}/{len(tickers)}", flush=True)
    except concurrent.futures.TimeoutError:
        print(f"  {label}: wall-clock timeout after {overall}s — {len(tickers) - done} ticker(s) "
              "unfinished, treated as misses (a hung Yahoo worker won't block the build).", flush=True)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


def _load_prev_cache():
    """The existing cache (or {}) — lets this run CARRY FORWARD fundamentals instead of refetching
    all 660 every night. Never fatal: a missing/corrupt pkl just means a full rebuild."""
    if not os.path.exists(CACHE_PKL):
        return {}
    try:
        with open(CACHE_PKL, "rb") as f:
            return pickle.load(f) or {}
    except Exception as e:
        print(f"  (previous cache unreadable — {e}; building fundamentals from scratch)", flush=True)
        return {}


def _fund_age_days(prev):
    """How old the carried-forward fundamentals are, in days (inf if unknown)."""
    ts = prev.get("built_fund") or prev.get("built")
    if not ts:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds() / 86400.0
    except Exception:
        return float("inf")


def build_fundamentals(all_t, v_t, prev):
    """Fundamentals for `all_t`, refreshed on a SLOW cycle and carried forward otherwise.

    Why: fundamentals move once a quarter, but each fetch costs ~5 HTTP requests. Refetching all
    660 nightly overran Yahoo's rate-limit window mid-run — coverage collapsed at ticker ~346/660
    (98% success before, 2% after) and 315 came back empty, silently blinding the Lifetime-High and
    3x-in-3yr gates on 39% of the V-universe. So:
      · carry forward every non-empty cached entry,
      · fetch only what is MISSING (new tickers, or previously-failed ones),
      · force a full refresh only when the cached copy is older than FUND_MAX_AGE_DAYS,
      · retry the EMPTIES (a throttled response is indistinguishable from 'no data'),
      · never let an empty result overwrite a good carried-forward value.
    Returns (fund, stats)."""
    carried = {t: f for t, f in (prev.get("fund") or {}).items() if f}     # non-empty only
    age = _fund_age_days(prev)
    stale = age >= FUND_MAX_AGE_DAYS

    _age_txt = "no previous cache" if age == float("inf") else f"cached copy is {age:.1f}d old ≥ {FUND_MAX_AGE_DAYS}d"
    if stale:
        todo = list(all_t)
        print(f"Fetching fundamentals — FULL refresh ({len(todo)} tickers; {_age_txt})…", flush=True)
    else:
        todo = [t for t in all_t if t not in carried]
        print(f"Fetching fundamentals — INCREMENTAL: {len(todo)} missing of {len(all_t)} "
              f"({len(carried)} carried forward, {age:.1f}d old < {FUND_MAX_AGE_DAYS}d)…", flush=True)

    fetched = {}
    if todo:
        fetched = {t: (f or {}) for t, f in
                   _parallel(fetch_fund_one, todo, "fundamentals",
                             workers=FUND_WORKERS, per_ticker=8).items()}
        # Retry the empties. Yahoo's .info/.financials endpoints throttle harder than price history,
        # and fetch_fundamentals() reports a 429 as {} — so an empty result is NOT trustworthy.
        for attempt in range(1, FUND_RETRY_PASSES + 1):
            empties = [t for t in todo if not fetched.get(t)]
            if not empties:
                break
            print(f"  {len(empties)} empty after pass {attempt} — cooling {FUND_RETRY_COOLDOWN_S}s "
                  f"then retrying at {RETRY_WORKERS} workers…", flush=True)
            time.sleep(FUND_RETRY_COOLDOWN_S)
            got = {t: f for t, f in
                   _parallel(fetch_fund_one, empties, f"fundamentals-retry{attempt}",
                             workers=RETRY_WORKERS, per_ticker=10).items() if f}
            fetched.update(got)
            print(f"  recovered {len(got)}/{len(empties)} on retry {attempt}", flush=True)
            if not got:                    # rescued nothing -> the rest genuinely have no statements
                break

    # merge: a fresh non-empty result wins; otherwise keep the carried-forward value.
    fund = dict(carried)
    n_new = n_kept = 0
    for t in all_t:
        f = fetched.get(t)
        if f:
            fund[t] = f
            n_new += 1
        elif t in carried:
            n_kept += 1
        else:
            fund.setdefault(t, {})        # genuinely nothing anywhere — keep the key, value {}
    fund = {t: fund.get(t, {}) for t in all_t}      # drop entries for tickers no longer in the sheet
    v_ok = sum(1 for t in v_t if fund.get(t))
    # Only treat a full refresh as DONE if it substantially succeeded. A full pass that got throttled
    # still returns good data (carry-forward covers the gaps) — but it must NOT advance built_fund,
    # or the staleness clock would reset and we'd wait another FUND_MAX_AGE_DAYS before retrying.
    refreshed = bool(stale and todo and n_new >= 0.6 * len(todo))
    stats = {"fresh": n_new, "carried": n_kept, "empty": sum(1 for t in all_t if not fund.get(t)),
             "v_ok": v_ok, "full_refresh": refreshed}
    if stale and not refreshed:
        print(f"  ⚠️  full refresh only got {n_new}/{len(todo)} fresh — keeping the previous "
              "`built_fund` timestamp so the next run tries a full refresh again.", flush=True)
    print(f"  fundamentals: {n_new} fresh · {n_kept} carried forward · {stats['empty']} still empty "
          f"· V-universe coverage {v_ok}/{len(v_t)} = {v_ok / max(1, len(v_t)):.0%}", flush=True)
    return fund, stats


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    prev = _load_prev_cache()

    # ALWAYS read the sheet live, first thing, every run — the V-universe is whatever the
    # `stock_classifications` tab says RIGHT NOW (additions and removals both take effect).
    print("Reading group lists from Google Sheet (live, every run)…", flush=True)
    groups = read_groups()
    v_t = sorted(set().union(*[set(v) for v in groups.values()])) if groups else []
    print(f"{len(v_t)} unique V-universe tickers across groups "
          + ", ".join(f"{g}={len(v)}" for g, v in groups.items()), flush=True)
    if not v_t:
        sys.exit("ERROR: no tickers in stock_classifications — refusing to overwrite the cache.")
    _prev_v = set().union(*[set(v) for v in (prev.get("groups") or {}).values()]) if prev.get("groups") else set()
    if _prev_v:
        _added, _removed = sorted(set(v_t) - _prev_v), sorted(_prev_v - set(v_t))
        if _added:
            print(f"  ➕ added since last build ({len(_added)}): {', '.join(_added[:15])}"
                  f"{' …' if len(_added) > 15 else ''}", flush=True)
        if _removed:
            print(f"  ➖ removed since last build ({len(_removed)}): {', '.join(_removed[:15])}"
                  f"{' …' if len(_removed) > 15 else ''}", flush=True)
        if not _added and not _removed:
            print("  (V-universe unchanged since the last build)", flush=True)

    print("Reading QUALITY superstar buys (NEW/ADD) to also cache…", flush=True)
    extra = [t for t in read_superstar_tickers() if t not in set(v_t)]
    all_t = sorted(set(v_t) | set(extra))
    print(f"+ {len(extra)} quality-superstar NEW/ADD tickers → {len(all_t)} total to cache", flush=True)

    now = datetime.now().isoformat()

    print("Fetching prices…", flush=True)
    data = {t: df for t, df in _parallel(fetch_one, all_t, "prices").items() if df is not None}

    # Retry stragglers: most "possibly delisted / no timezone found" misses are transient Yahoo
    # rate-limiting, not real delistings. Re-fetch ONLY the misses after a cooldown, with gentler
    # concurrency — recovers the bulk of them without slowing the common path.
    for attempt in range(1, RETRY_PASSES + 1):
        missing = [t for t in all_t if t not in data]
        if not missing:
            break
        print(f"  {len(missing)} still missing after pass {attempt} — cooling {RETRY_COOLDOWN_S}s then retrying…",
              flush=True)
        time.sleep(RETRY_COOLDOWN_S)
        recovered = {t: df for t, df in _parallel(fetch_one, missing, f"prices-retry{attempt}",
                                                  workers=RETRY_WORKERS).items() if df is not None}
        data.update(recovered)
        print(f"  recovered {len(recovered)}/{len(missing)} on retry {attempt}", flush=True)
        if not recovered:                          # this pass rescued nothing -> the rest are truly dead; stop early
            break                                  # (avoids a needless RETRY_COOLDOWN_S sleep + refetch of dead symbols)
    _still_missing = [t for t in all_t if t not in data]
    if _still_missing:
        print(f"  {len(_still_missing)} unrecovered after {RETRY_PASSES} retries (likely truly delisted / "
              f"not on NSE or BSE): {', '.join(_still_missing[:15])}{' …' if len(_still_missing) > 15 else ''}",
              flush=True)
        # A V-universe name that never prices is a SHEET problem (wrong/renamed symbol), not a blip.
        # Call it out explicitly with the alias hint so it gets fixed instead of failing forever.
        _dead_v = [t for t in _still_missing if t in set(v_t)]
        if _dead_v:
            print(f"  🚨 {len(_dead_v)} of these are in your V-universe and will stay unusable until the "
                  f"sheet is corrected: {', '.join(_dead_v)}", flush=True)
            print(f"      → fix the symbol in `{WORKSHEET_NAME}`, or add it to SYMBOL_ALIASES in refresh.py "
                  f"(current aliases: {SYMBOL_ALIASES}).", flush=True)

    # Carry-forward + incremental refresh; retries every empty (a 429 looks exactly like "no data").
    if data:
        print(f"Cooling {PHASE_COOLDOWN_S}s before fundamentals so Yahoo's rate-limit window resets…",
              flush=True)
        time.sleep(PHASE_COOLDOWN_S)
    fund, fstats = build_fundamentals(all_t, v_t, prev)

    # Safety: a broken/blocked fetch shouldn't clobber a good cache. Gate on the V-UNIVERSE
    # coverage (the critical set) — superstar/off-universe tickers are best-effort and some may
    # legitimately fail to price (delisted / odd symbols), which must not abort the whole build.
    _v_priced = sum(1 for t in v_t if t in data)
    if _v_priced < max(1, int(MIN_PRICE_FRACTION * len(v_t))):
        sys.exit(f"ERROR: only {_v_priced}/{len(v_t)} V-universe price series fetched — looks broken; "
                 "NOT overwriting the cache.")
    # Same idea for fundamentals: .info/.financials hit different Yahoo endpoints than price history, so
    # they can be throttled run-wide while prices succeed. The coverage below is POST-carry-forward, so
    # a throttled night keeps yesterday's good values and still clears the bar; only a genuine, sustained
    # outage (nothing cached AND nothing fetchable) trips it.
    # ...but ONLY when there is an existing cache worth protecting. On a first/cold build there is
    # nothing to clobber, and prices (the critical part — 7 of the 9 strategies need no fundamentals)
    # are already in hand; aborting there would throw away a good price fetch and leave the dashboard
    # with NO data at all. Fundamentals simply degrade Lifetime-High / 3x3 to REVIEW.
    _v_fund = fstats["v_ok"]
    _floor = max(1, int(MIN_FUND_FRACTION * len(v_t)))
    if _v_fund < _floor:
        if prev.get("fund"):
            sys.exit(f"ERROR: only {_v_fund}/{len(v_t)} V-universe fundamentals available even after "
                     f"carry-forward + {FUND_RETRY_PASSES} retries — fundamentals look blocked "
                     "run-wide; NOT overwriting (keeps the previous cache intact).")
        print(f"  ⚠️  only {_v_fund}/{len(v_t)} V-universe fundamentals on this COLD build "
              f"(< {_floor}) — writing anyway because there is no previous cache to protect. "
              "Lifetime-High / 3x-in-3yr will show REVIEW for the uncovered names until the next "
              "run tops them up.", flush=True)

    # built_fund only advances on a FULL refresh — that's what drives the next run's staleness check,
    # so an incremental night must not reset the clock (else it would never do a full refresh again).
    _ts_fund = now if fstats["full_refresh"] else (prev.get("built_fund") or now)
    payload = {"groups": groups, "data": data, "fund": fund,
               "built": now, "built_prices": now, "built_fund": _ts_fund}
    # atomic write: dump to a temp file then os.replace() — a crash/kill mid-pickle (this is a big
    # payload) must NOT leave a truncated pkl that the deployed dashboard would fail to unpickle.
    _tmp = CACHE_PKL + ".tmp"
    with open(_tmp, "wb") as f:
        pickle.dump(payload, f)
    os.replace(_tmp, CACHE_PKL)
    size_mb = os.path.getsize(CACHE_PKL) / 1e6
    # Count NON-EMPTY fundamentals. The old line printed len(fund), which counted the empty {}
    # placeholders too — that is how a run with 315 blank entries still reported "660 fundamentals".
    _n_fund = sum(1 for f in fund.values() if f)
    print(f"✅ cache written: {len(data)}/{len(all_t)} priced "
          f"(V-universe {_v_priced}/{len(v_t)}) · {_n_fund}/{len(all_t)} fundamentals "
          f"(V-universe {_v_fund}/{len(v_t)} = {_v_fund / max(1, len(v_t)):.0%}) · "
          f"{size_mb:.1f} MB -> {CACHE_PKL}", flush=True)


if __name__ == "__main__":
    main()
