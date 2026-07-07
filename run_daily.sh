#!/usr/bin/env bash
# ============================================================================
# SPD Stock Dashboard — one-command daily refresh
# ----------------------------------------------------------------------------
# WHEN: run once in the evening on a TRADING DAY (Mon–Fri), ~7:00 PM IST — after
#       NSE has posted the day's bulk/block + SAST files. (The GitHub Actions
#       yml runs 5 PM IST; 7 PM catches late-posted deals more reliably.)
#
# WHAT it does, in order:
#   1) fii_dii_investment_pattern.ipynb  -> refreshes ALL Google-Sheet tabs
#      (superstar summary · holdings · bulk/block · SAST · insider · market-wide)
#   2) refresh.py                         -> builds the V-universe price /
#      fundamentals / strategy-setup cache (dashboard_cache.pkl)
#   3) gh release upload                  -> publishes that cache so the DEPLOYED
#      dashboard on Streamlit Cloud picks it up (sheet writes in step 1 are
#      already live and need no publish)
#
# PREREQS on this machine (see also: copy service_account.json + .streamlit/
# secrets.toml when moving to a new laptop; recreate .venv, don't copy it):
#   python3 -m venv .venv
#   .venv/bin/pip install -r requirements.txt -r requirements-ci.txt
#   gh auth login          # only needed for step 3 (publishing the cache)
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")" || { echo "cannot cd to script dir"; exit 1; }

PY=".venv/bin/python"
JUP=".venv/bin/jupyter"
LOG="run_daily_$(date +%Y%m%d_%H%M).log"

log() { echo "$@" | tee -a "$LOG"; }

log "===== SPD daily refresh — $(date) ====="

# --- credentials: Cell 2 falls back to the file, but export it so refresh.py sees it too ---
if [ -f service_account.json ]; then
  export GCP_SERVICE_ACCOUNT="$(cat service_account.json)"
else
  log "!! service_account.json NOT found — Google Sheet reads/writes will fail. Aborting."
  exit 1
fi

# --- 1) investor + NSE tabs (the notebook) ---
log ""
log "[1/3] Refreshing investor + NSE sheet tabs (notebook)…"
"$JUP" nbconvert --to notebook --execute fii_dii_investment_pattern.ipynb \
    --output /tmp/spd_executed.ipynb --ExecutePreprocessor.timeout=-1 2>&1 | tee -a "$LOG"

# --- 2) V-universe price / fundamentals / strategy cache ---
log ""
log "[2/3] Building V-universe cache (refresh.py)…"
"$PY" refresh.py 2>&1 | tee -a "$LOG"

# --- 3) publish the cache to the GitHub release (for the deployed dashboard) ---
log ""
log "[3/3] Publishing cache to the GitHub release…"
if command -v gh >/dev/null 2>&1 && [ -f vivek_output/dashboard_cache.pkl ]; then
  if gh release upload data-latest vivek_output/dashboard_cache.pkl --clobber 2>&1 | tee -a "$LOG"; then
    log "    cache published."
  else
    log "    (upload failed — deployed app keeps its last cache. Try: gh auth login)"
  fi
else
  log "    (skipped — 'gh' not installed or cache not built. Install gh + 'gh auth login' to publish;"
  log "     or run the dashboard locally: .venv/bin/streamlit run vivek_dashboard_app.py — it reads the local cache.)"
fi

log ""
log "===== done — $(date) · full log: $LOG ====="
