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
# timeout=2400 (40 min/cell): generous for the slow-but-legit holdings scrape, but bounds a
#   hung request so the run can't stall FOREVER (was -1 = infinite → the whole run hung silently).
# interrupt_on_timeout + --allow-errors: a single blocked/slow cell is interrupted and RECORDED,
#   then the notebook CONTINUES — so one bad source no longer prevents the other ~20 tabs from
#   refreshing, and steps [2/3]/[3/3] always run afterwards. (Cells are block-safe: on failure
#   they preserve last-good data, so --allow-errors is safe here.)
"$JUP" nbconvert --to notebook --execute fii_dii_investment_pattern.ipynb \
    --output /tmp/spd_executed.ipynb \
    --ExecutePreprocessor.timeout=2400 \
    --ExecutePreprocessor.interrupt_on_timeout=True \
    --allow-errors 2>&1 | tee -a "$LOG"

# nbconvert writes each cell's print() output INTO the .ipynb, not to stdout, so the tee'd log
# above only shows nbconvert's own lines. Surface a concise per-cell summary (what wrote, what
# ALERTed/aborted, what errored) so a stale tab or a block is VISIBLE in the log, not silent.
"$PY" nb_summary.py /tmp/spd_executed.ipynb 2>&1 | tee -a "$LOG"

# --- 2) V-universe price / fundamentals / strategy cache ---
log ""
log "[2/3] Building V-universe cache (refresh.py)…"
"$PY" refresh.py 2>&1 | tee -a "$LOG"

# --- 3) publish the cache to the GitHub release (for the deployed dashboard) ---
log ""
log "[3/3] Publishing cache to the GitHub release…"
CACHE="vivek_output/dashboard_cache.pkl"
if [ ! -f "$CACHE" ]; then
  log "    (skipped — $CACHE not built; step [2/3] must have failed. Deployed app keeps its last cache.)"
elif command -v gh >/dev/null 2>&1; then
  if gh release upload data-latest "$CACHE" --clobber 2>&1 | tee -a "$LOG"; then
    log "    cache published (via gh)."
  else
    log "    (gh upload failed — deployed app keeps its last cache. Try: gh auth login)"
  fi
elif [ -n "${GH_UPLOAD_TOKEN:-}" ]; then
  # No gh CLI on this machine → publish via the GitHub REST API (publish_cache.py).
  # Needs a Contents:Read-WRITE PAT in $GH_UPLOAD_TOKEN (the [github] token in secrets.toml is
  # Read-only — enough for the app to DOWNLOAD, not to upload).
  "$PY" publish_cache.py 2>&1 | tee -a "$LOG"
else
  log "    (skipped — no 'gh' CLI and no \$GH_UPLOAD_TOKEN set, so the freshest cache is NOT reaching"
  log "     the deployed app. To publish from this machine without gh:"
  log "        export GH_UPLOAD_TOKEN=<fine-grained PAT · Contents:Read-write · this repo only>"
  log "     then re-run. Or run the dashboard locally — it reads the local cache directly.)"
fi

log ""
log "===== done — $(date) · full log: $LOG ====="

