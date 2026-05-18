#!/usr/bin/env python3
"""Phase 3.5a (cont.) — Backfill `lab_recs` and `dx_exclusions` rows for
SYNTH-002 through SYNTH-010 by firing the existing n8n Lab Recs Sync and
Dx Exclusions Sync webhooks.

Sister script to `backfill_patterns_questions.py`. Phase 2 / 3 only ran
Lab Recs + Dx Exclusions for SYNTH-001 + SYNTH-005; this script populates
the rest so the dashboard's Lab Recs and Dx Exclusions panels render data
when those patients are clicked.

Strategy:
1. Fire Lab Recs Sync sequentially for all 8 patients (each ~30-90s).
2. Fire Dx Exclusions Sync sequentially for all 8 patients.
   Both flows read the latest summary row for the patient when they run.

Usage:
    python3 scripts/backfill_labs_dx.py
    python3 scripts/backfill_labs_dx.py --skip-labs   # dx only
    python3 scripts/backfill_labs_dx.py --skip-dx     # labs only

Logs to /tmp/helix_labs_dx_backfill.log so the parent can poll Supabase via
MCP rather than waiting on bash timeouts.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

LAB_RECS_SYNC_URL = os.environ.get(
    "N8N_LAB_RECS_SYNC_URL",
    "http://localhost:5678/webhook/helix-lab-recs",
)
DX_SYNC_URL = os.environ.get(
    "N8N_DX_SYNC_URL",
    "http://localhost:5678/webhook/helix-dx",
)
PER_REQUEST_TIMEOUT_S = 300  # Tool Agent + Pinecone retrieval can be slow
LOG_PATH = Path("/tmp/helix_labs_dx_backfill.log")

PATIENTS = [
    "SYNTH-002",
    "SYNTH-003",
    "SYNTH-004",
    "SYNTH-006",
    "SYNTH-007",
    "SYNTH-008",
    "SYNTH-009",
    "SYNTH-010",
]


def log(msg: str) -> None:
    line = "[{}] {}\n".format(
        time.strftime("%H:%M:%S", time.localtime()), msg
    )
    sys.stdout.write(line)
    sys.stdout.flush()
    with LOG_PATH.open("a") as f:
        f.write(line)


def fire(url: str, patient_id: str, label: str) -> bool:
    log("{} | {} | firing {}...".format(patient_id, label, url))
    started = time.monotonic()

    try:
        resp = requests.post(
            url,
            json={"patient_id": patient_id},
            timeout=PER_REQUEST_TIMEOUT_S,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        log("{} | {} | request error: {}: {}".format(
            patient_id, label, type(exc).__name__, exc
        ))
        return False

    elapsed = time.monotonic() - started

    if resp.status_code != 200:
        log("{} | {} | HTTP {} after {:.1f}s; body[:200]={}".format(
            patient_id, label, resp.status_code, elapsed, resp.text[:200]
        ))
        return False

    log("{} | {} | OK in {:.1f}s".format(patient_id, label, elapsed))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-labs",
        action="store_true",
        help="Skip Lab Recs Sync; only fire Dx Exclusions Sync.",
    )
    parser.add_argument(
        "--skip-dx",
        action="store_true",
        help="Skip Dx Exclusions Sync; only fire Lab Recs Sync.",
    )
    args = parser.parse_args()

    LOG_PATH.write_text("")  # truncate
    log("Backfill starting — {} patients".format(len(PATIENTS)))
    log("Lab Recs Sync URL: {}".format(LAB_RECS_SYNC_URL))
    log("Dx Sync URL: {}".format(DX_SYNC_URL))

    lab_results = []
    if not args.skip_labs:
        log("--- Phase 1: Lab Recs Sync ---")
        for pid in PATIENTS:
            ok = fire(LAB_RECS_SYNC_URL, pid, "LabRecs")
            lab_results.append((pid, ok))
            time.sleep(2)
        log("Lab Recs Sync complete: {} / {} succeeded".format(
            sum(1 for _, ok in lab_results if ok), len(lab_results)
        ))
    else:
        log("--- Phase 1: Lab Recs Sync SKIPPED ---")

    dx_results = []
    if not args.skip_dx:
        log("--- Phase 2: Dx Exclusions Sync ---")
        for pid in PATIENTS:
            ok = fire(DX_SYNC_URL, pid, "DxExcl")
            dx_results.append((pid, ok))
            time.sleep(2)
        log("Dx Exclusions Sync complete: {} / {} succeeded".format(
            sum(1 for _, ok in dx_results if ok), len(dx_results)
        ))
    else:
        log("--- Phase 2: Dx Exclusions Sync SKIPPED ---")

    log("--- Backfill complete ---")
    if lab_results:
        log("Lab Recs Sync results:")
        for pid, ok in lab_results:
            log("  {} {}".format("OK  " if ok else "FAIL", pid))
    if dx_results:
        log("Dx Exclusions Sync results:")
        for pid, ok in dx_results:
            log("  {} {}".format("OK  " if ok else "FAIL", pid))

    lab_failed = sum(1 for _, ok in lab_results if not ok)
    dx_failed = sum(1 for _, ok in dx_results if not ok)
    return 0 if (lab_failed + dx_failed) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
