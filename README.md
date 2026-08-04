# SPD Stock Dashboard

A personal NSE stock screener / backtester + "superstar" FII/DII investor tracker,
built with Streamlit. Deployed on **Streamlit Community Cloud**, with a nightly
**GitHub Actions** job that refreshes all data so the live app stays fast.

> ⚠️ This repo is **private** and contains **no credentials**. All secrets live in
> GitHub Actions Secrets and Streamlit Cloud Secrets — never in the code.

---

## How it works

```
 ┌─ GitHub Actions  (.github/workflows/daily.yml, nightly cron) ──────────┐
 │  1. pip install requirements.txt + requirements-ci.txt                 │
 │  2. run the FII/DII notebook headless (creds from GCP_SERVICE_ACCOUNT) │
 │       → refreshes the superstar Google Sheets                          │
 │  3. run refresh.py → builds dashboard_cache.pkl (Yahoo prices+fundas)  │
 │  4. upload cache as the single 'data-latest' Release asset (overwrite) │
 └──────────────────────────────┬─────────────────────────────────────────┘
                                │ (Release asset updated)
                                ▼
   Streamlit Community Cloud  ── reads Sheets live + downloads the latest
   (the app)                     cache asset at startup → fast dashboard
                                ▲
                                │  one shared password ([app] password in
                            your friends    Streamlit Secrets) — see auth.py
```

* **Cache is shipped as a Release asset, not committed** → only the latest copy
  exists, so git history never bloats.
* **Auth = a single shared password** (`auth.py`), read from `[app] password` in
  Streamlit Secrets (or the `APP_PASSWORD` env var). You give that password to the
  people you want in; to revoke access you change it and redeploy.
  *(Google sign-in via `st.login` was tried and removed — it looped on Streamlit
  Community Cloud with "Missing provider for OAuth callback". There is no
  `ALLOWED_EMAILS` list and no OAuth client to configure.)*
* **The V-universe is read LIVE from the sheet on every refresh run**, so editing
  `stock_classifications` takes effect on the next `run_daily.sh` — no code change.
  The app also re-reads it directly (15-min cache) so list edits appear without a rebuild.

---

## One-time setup (do these once, in order)

### 1. Rotate the Google service-account key  ⚠️ important
The old key was kept inline for local use and must be considered compromised.
- Google Cloud Console → **IAM & Admin → Service Accounts** →
  `spritamdas@sheet-operations-spritamdas.iam.gserviceaccount.com` → **Keys**.
- **Create a new JSON key**, download it, then **disable/delete the old key**.
- You'll paste the *new* JSON into two places (steps 4 and 6). Never into a file in this repo.

### 2. Choose the access password
No OAuth client is needed. Pick a strong shared password and put it in
`[app] password` (secrets template = `.streamlit/secrets.toml.example`) — locally and
in Streamlit Cloud Secrets. That's the whole gate.

### 3. Decide who can access the app
Give that password to the people you want in. To revoke access, **change the password
and redeploy** — everyone re-enters the new one. Sessions also expire after
`[app] session_timeout_min` (default 240) on the user's next interaction, and there's a
**Log out** button in the sidebar. *(No email allow-list and no `ALLOWED_EMAILS` set exist.)*

### 4. Create the private GitHub repo and push
```bash
cd SPD_Stock_Dashboard
git init && git add . && git commit -m "initial deploy scaffold"
git branch -M main
git remote add origin https://github.com/<you>/SPD_Stock_Dashboard.git
git push -u origin main
```
(GitHub secret-scanning will block the push if any key slips in — that's a feature.)

Then in the repo:
- **Settings → Secrets and variables → Actions → New repository secret**:
  `GCP_SERVICE_ACCOUNT` = the **entire new** service-account JSON.
  (The workflow's built-in `GITHUB_TOKEN` already has the `contents:write` it needs.)
- **Settings → Developer settings → Fine-grained tokens → Generate**: repo access =
  *only this repo*, permission = **Contents → Read-only**. Copy it → `[github] token`
  in secrets. *(Note: fine-grained PATs expire — set a reminder to rotate it.)*

### 5. Deploy on Streamlit Community Cloud
- https://share.streamlit.io → **New app** → this private repo, branch `main`,
  main file `vivek_dashboard_app.py`.
- Open the app's **Settings → Secrets** and paste your filled-in `secrets.toml`
  (template = `secrets.toml.example`): `[gcp_service_account]`, `[app]`, `[github]`.
  - ⚠️ Make sure `[app] dev_no_auth` is **false** (or absent) on the cloud. *(As a safety
    net the dev bypass is hard-disabled on Streamlit Cloud anyway, but don't rely on that.)*
  - ⚠️ On the cloud, `[github] token` only needs **Contents: Read-only** — the app just
    downloads the cache asset. Do **not** put the read-write `upload_token` there.

### 6. Build the data once (first deploy will be EMPTY until you do this)
The cache pkl isn't in the repo, so on first load the dashboard has no data yet.
- GitHub → **Actions → nightly-refresh → Run workflow** → wait for it to finish
  (the Trendlyne scrape can take a while), then reload the app.
- After that it refreshes automatically every night.

---

## Local testing
```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# In secrets.toml: fill [gcp_service_account] with the new key, and either
#   set [app] password = "…"        # then type it at the gate, or
#   set [app] dev_no_auth = true    # skip the gate entirely (LOCAL ONLY)
pip install -r requirements.txt
streamlit run vivek_dashboard_app.py
```
`dev_no_auth = true` skips the password screen for local work. Leave it **false** for
production — and it is hard-disabled on Streamlit Cloud regardless (the check looks for the
`/mount/...` path Cloud apps run under), so a stray secret can't open the deployed app.
`.streamlit/secrets.toml` is git-ignored, so your local secrets never get committed.

---

## Repo layout
```
SPD_Stock_Dashboard/
├── .github/workflows/daily.yml     # nightly cron: notebook → cache → publish Release asset
├── .streamlit/secrets.toml.example # template (real secrets.toml is git-ignored)
├── vivek_dashboard_app.py          # the Streamlit app  (auth gate + cache-from-Release)
├── vivek_dashboard_core.py         # charts / indicators engine
├── vivek_strategies.py             # strategy + backtest engine
├── auth.py                         # shared-password gate + session timeout
├── publish_cache.py                # gh-free Release-asset upload (used by run_daily.sh step 3)
├── nb_summary.py                   # surfaces the executed notebook's per-cell log lines
├── run_daily.sh                    # one-command local refresh: notebook → cache → publish
├── refresh.py                      # headless cache builder (run nightly)
├── fii_dii_investment_pattern.ipynb# FII/DII pipeline (creds via env; run nightly, scrubbed of keys)
├── requirements.txt                # app deps
├── requirements-ci.txt             # extra deps the Action needs to run the notebook
├── .gitignore
└── README.md
```

## Security & session notes
- No credentials are in this repo; secrets live only in GitHub Actions Secrets and
  Streamlit Cloud Secrets. (Verified: the only `BEGIN PRIVATE KEY` anywhere in git history
  is the placeholder in `secrets.toml.example`.)
- Access = **one shared password** (`[app] password`), compared in constant time
  (`hmac.compare_digest`) in `auth.py`. Anyone with the password is in — so treat it like a
  door key: don't post it anywhere, and rotate it when you want to cut someone off.
- **Sessions** live in Streamlit session state, so they end when the browser session ends.
  The controls are the **Log out** button, the **timeout** (`[app] session_timeout_min`,
  applied on the user's next interaction), and **changing the password + redeploy**.
- There is **no brute-force throttling** on the gate. Use a long, random password.
- The nightly Trendlyne scrape may be rate-limited from GitHub's IPs; the pipeline
  skips-on-failure and keeps the previous day's data (no corruption). The notebook also
  refreshes US investors + a glossary, so a cold run can be long — fine within the job's
  180-min timeout.

## Manual refresh
GitHub → **Actions → nightly-refresh → Run workflow** to refresh on demand, or locally:
```bash
caffeinate -is ./run_daily.sh        # keeps the Mac awake for the whole run
```

> NSE constituent lists are fetched live from NSE archives; the V40 classifications and
> FII/DII tabs are read live from Google Sheets — so no data files are stored in this repo.

## Editing the stock lists
Edit `v_40` / `v_40_next` / `v_200` in the **`stock_classifications`** tab of the
[SPritamDas Stock Analaysis](https://docs.google.com/spreadsheets/d/1qzj_Va1Xle6Pnz7HDUsO1iPaUeGEv_VLJFXE4-zZYNw/edit)
workbook. `refresh.py` re-reads that tab **live at the start of every run** and logs what
changed (`➕ added` / `➖ removed`), so additions *and* removals apply on the next run with no
code change. The app re-reads it too (15-min cache), so list edits show up there immediately.

**Keep the cells clean.** Values are sanitized before use — blanks, `nan`, bare BSE scrip
codes and **spreadsheet error text** (`#N/A`, `#REF!`, …) are dropped and reported in the log.
A broken `XLOOKUP` used to be handed to yfinance as if it were a ticker; wrap lookups in
`IFERROR` so a miss leaves the cell empty instead.

**Renamed symbols** live in `SYMBOL_ALIASES` in `vivek_strategies.py` — the fetch is
redirected while the sheet's symbol stays the cache/display key. Currently:
`EIH → EIHOTEL`, `TATAMOTORS → TMPV` (post-demerger; both carry the full history).
If the log prints `🚨 … in your V-universe and will stay unusable`, either fix the symbol in
the sheet or add an alias there.
