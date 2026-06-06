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
                                │  Google sign-in; email must be in the
                            your friends    ALLOWED_EMAILS list in auth.py
```

* **Cache is shipped as a Release asset, not committed** → only the latest copy
  exists, so git history never bloats.
* **Auth** = Google sign-in (`st.login`) + an allow-list. **The allow-list is the
  `ALLOWED_EMAILS` set hard-coded in `auth.py`** (3 emails preloaded). To add/remove
  someone you edit that set, commit, and let Streamlit redeploy. *(A Sheet-driven
  list is a planned upgrade; not active yet.)*

---

## One-time setup (do these once, in order)

### 1. Rotate the Google service-account key  ⚠️ important
The old key was kept inline for local use and must be considered compromised.
- Google Cloud Console → **IAM & Admin → Service Accounts** →
  `spritamdas@sheet-operations-spritamdas.iam.gserviceaccount.com` → **Keys**.
- **Create a new JSON key**, download it, then **disable/delete the old key**.
- You'll paste the *new* JSON into two places (steps 4 and 6). Never into a file in this repo.

### 2. Create a Google OAuth client (for friend sign-in)
- Google Cloud Console → **APIs & Services → Credentials → Create credentials →
  OAuth client ID → Web application**.
- Add **Authorized redirect URIs** (you can add the cloud one now or after step 5):
  - `http://localhost:8501/oauth2callback`  (local testing)
  - `https://<your-app-name>.streamlit.app/oauth2callback`  (after you know the deployed URL)
- Copy the **Client ID** and **Client secret** → they go into `[auth]` in secrets.
- Generate a random `cookie_secret`:  `python -c "import secrets; print(secrets.token_hex(32))"`

### 3. Decide who can access the app
Open `auth.py` and edit the **`ALLOWED_EMAILS`** set (currently your 3 emails). Each
person signs in with that exact Google account. To revoke someone, remove their email
here and redeploy. *(There is no "allowed_users" Sheet — access is controlled here.)*

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
  (template = `secrets.toml.example`): `[auth]`, `[gcp_service_account]`, `[app]`, `[github]`.
  - ⚠️ The secrets are **NOT identical to local**: set
    `[auth] redirect_uri = "https://<your-app>.streamlit.app/oauth2callback"` (the cloud URL),
    and add that exact URL to the Google OAuth client (step 2) — otherwise sign-in fails
    with `redirect_uri_mismatch`.
  - ⚠️ Make sure `[app] dev_no_auth` is **false** (or absent) on the cloud. *(As a safety
    net the dev bypass is hard-disabled on Streamlit Cloud anyway, but don't rely on that.)*

### 6. Build the data once (first deploy will be EMPTY until you do this)
The cache pkl isn't in the repo, so on first load the dashboard has no data yet.
- GitHub → **Actions → nightly-refresh → Run workflow** → wait for it to finish
  (the Trendlyne scrape can take a while), then reload the app.
- After that it refreshes automatically every night.

---

## Local testing (the fast way — no OAuth needed)
```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# In secrets.toml: fill [gcp_service_account] with the new key, and set:
#     [app] dev_no_auth = true        # skips Google sign-in for local testing only
pip install -r requirements.txt
streamlit run vivek_dashboard_app.py
```
`dev_no_auth = true` lets you run the dashboard locally **without** setting up the OAuth
client. Leave it **false** for production (and it's ignored on Streamlit Cloud regardless).
To test the *real* Google login locally, set `dev_no_auth = false` and fill the `[auth]`
block + register `http://localhost:8501/oauth2callback` in the OAuth client.
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
├── auth.py                         # Google login + ALLOWED_EMAILS allow-list + session timeout
├── refresh.py                      # headless cache builder (run nightly)
├── fii_dii_investment_pattern.ipynb# FII/DII pipeline (creds via env; run nightly, scrubbed of keys)
├── requirements.txt                # app deps
├── requirements-ci.txt             # extra deps the Action needs to run the notebook
├── .gitignore
└── README.md
```

## Security & session notes
- No credentials are in this repo; secrets live only in GitHub Actions Secrets and
  Streamlit Cloud Secrets.
- Access = Google-verified identity + the `ALLOWED_EMAILS` allow-list in `auth.py`.
- **Sessions:** Streamlit's sign-in cookie lasts ~30 days and is **not** idle-aware, so
  **closing the tab does NOT log a user out.** The real controls are: the **Log out**
  button, the in-app **idle/absolute timeout** (`[app] session_timeout_min`, which kicks in
  on the user's next interaction), and **removing their email from `ALLOWED_EMAILS` +
  redeploy** (takes effect on their next page load).
- The nightly Trendlyne scrape may be rate-limited from GitHub's IPs; the pipeline
  skips-on-failure and keeps the previous day's data (no corruption). The notebook also
  refreshes US investors + a glossary, so a cold run can be long — fine within the job's
  180-min timeout.

## Manual refresh
GitHub → **Actions → nightly-refresh → Run workflow** to refresh on demand.

> NSE constituent lists are fetched live from NSE archives; the V40 classifications and
> FII/DII tabs are read live from Google Sheets — so no data files are stored in this repo.
