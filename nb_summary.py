#!/usr/bin/env python3
"""nb_summary.py — print a concise per-cell summary of an EXECUTED notebook.

nbconvert writes each cell's print() output INTO the .ipynb (not to stdout), so the refresh
log / GitHub Actions output would otherwise show none of the 'written / ALERT / SKIPPED / ABORT'
progress lines. This surfaces just those + any cell that errored, so a stale tab or a blocked
source is VISIBLE at a glance instead of silent.

Usage: python nb_summary.py [/path/to/executed.ipynb]   (default: /tmp/executed.ipynb)
Always exits 0 — it is a reporter, not a gate.
"""
import json
import sys

KEEP = ("ALERT", "ABORT", "written", "SKIPPED", "BROKEN", "reference:", "Checkpoint")

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/executed.ipynb"
try:
    nb = json.load(open(path))
except Exception as e:
    print(f"  (could not read executed notebook {path}: {e})")
    sys.exit(0)

errs = 0
for i, c in enumerate(nb.get("cells", [])):
    if c.get("cell_type") != "code":
        continue
    for o in c.get("outputs", []):
        if o.get("output_type") == "error":
            errs += 1
            print(f"  [cell {i}] ERROR {o.get('ename')}: {o.get('evalue', '')[:200]}")
        elif o.get("output_type") == "stream":
            for ln in "".join(o.get("text", [])).splitlines():
                if any(k in ln for k in KEEP):
                    print(f"  [cell {i}] {ln.strip()[:200]}")
print(f"  === notebook summary: {errs} cell(s) errored ===")
