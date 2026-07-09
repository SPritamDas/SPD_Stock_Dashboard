#!/usr/bin/env python3
"""
publish_cache.py — upload vivek_output/dashboard_cache.pkl to the GitHub Release
asset 'data-latest' via the REST API, so LOCAL refreshes can publish the cache to
the deployed Streamlit app WITHOUT the `gh` CLI installed.

Why this exists: run_daily.sh step [3/3] used `gh release upload`, but `gh` is not
installed on the refresh machine, so the local (freshest) cache was never published —
the deployed app only ever got CI's copy. This is a drop-in, gh-free publisher.

Auth: needs a fine-grained PAT with **Contents: Read-WRITE** for this repo, in env
    GH_UPLOAD_TOKEN
(the [github] token in .streamlit/secrets.toml is Contents: Read-ONLY — enough for the
app to DOWNLOAD the asset, but NOT enough to upload it). Repo defaults to
SPritamDas/SPD_Stock_Dashboard; override with GH_REPO.

Exit 0 on success, non-zero on any failure (so run_daily.sh can report it).
"""
import os
import sys

import requests

REPO   = os.environ.get("GH_REPO", "SPritamDas/SPD_Stock_Dashboard")
TOKEN  = os.environ.get("GH_UPLOAD_TOKEN")
TAG    = os.environ.get("GH_RELEASE_TAG", "data-latest")
ASSET  = os.environ.get("GH_CACHE_ASSET", "dashboard_cache.pkl")
CACHE  = os.environ.get("GH_CACHE_PATH", "vivek_output/dashboard_cache.pkl")

API    = "https://api.github.com"


def _die(msg, code=1):
    print(f"    publish_cache: {msg}")
    sys.exit(code)


def main():
    if not TOKEN:
        _die("GH_UPLOAD_TOKEN not set (need a Contents:Read-write PAT) — skipping upload.", code=2)
    if not os.path.isfile(CACHE):
        _die(f"cache not found at {CACHE} — nothing to upload.", code=3)

    h = {"Authorization": f"Bearer {TOKEN}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}

    # 1) find (or create) the release for TAG
    r = requests.get(f"{API}/repos/{REPO}/releases/tags/{TAG}", headers=h, timeout=30)
    if r.status_code == 404:
        r = requests.post(f"{API}/repos/{REPO}/releases", headers=h, timeout=30,
                          json={"tag_name": TAG, "name": "Latest data cache",
                                "body": "auto-updated by run_daily.sh / publish_cache.py"})
        if r.status_code not in (200, 201):
            _die(f"could not create release '{TAG}': HTTP {r.status_code} {r.text[:200]}")
    elif r.status_code != 200:
        _die(f"could not read release '{TAG}': HTTP {r.status_code} {r.text[:200]} "
             "(check GH_UPLOAD_TOKEN has Contents:Read-write on this repo).")
    rel = r.json()
    release_id = rel["id"]

    # 2) delete any existing asset of the same name (REST has no --clobber)
    for a in rel.get("assets", []):
        if a.get("name") == ASSET:
            d = requests.delete(f"{API}/repos/{REPO}/releases/assets/{a['id']}", headers=h, timeout=30)
            if d.status_code not in (204, 200):
                _die(f"could not delete old asset (HTTP {d.status_code} {d.text[:200]}).")
            print(f"    publish_cache: removed old '{ASSET}'.")

    # 3) upload the new asset
    upload_url = rel["upload_url"].split("{", 1)[0]     # strip the {?name,label} template
    size_mb = os.path.getsize(CACHE) / 1e6
    print(f"    publish_cache: uploading {CACHE} ({size_mb:.1f} MB) → {REPO} release '{TAG}'…")
    with open(CACHE, "rb") as f:
        up = requests.post(f"{upload_url}?name={ASSET}",
                           headers={**h, "Content-Type": "application/octet-stream"},
                           data=f, timeout=600)
    if up.status_code not in (200, 201):
        _die(f"upload failed: HTTP {up.status_code} {up.text[:200]}")
    print(f"    publish_cache: ✅ published '{ASSET}' (release id {release_id}). "
          "Deployed app picks it up within ~30 min.")


if __name__ == "__main__":
    main()
