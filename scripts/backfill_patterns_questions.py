#!/usr/bin/env python3
"""Phase 3.5a — Backfill `patterns` and `questions` rows for SYNTH-002 through
SYNTH-010 by firing the existing n8n Pattern Sync and Question Sync webhooks.

Phase 2 (Pattern Recognition + Guided Questions) was originally only run for
SYNTH-001. This script populates the other 8 patients so the dashboard's
Patterns and Questions panels render data when those patients are clicked.

Strategy:
1. Fire Pattern Sync sequentially for all 8 patients (each ~30-60s).
2. After all patterns are written, fire Question Sync sequentially for all 8.
   Question Sync reads the latest pattern row for the patient when it runs,
   so this ordering ensures questions reference the just-generated patterns.

Usage:
    python3 scripts/backfill_patterns_questions.py
    python3 scripts/backfill_patterns_questions.py --skip-patterns   # if patterns already exist
    python3 scripts/backfill_patterns_questions.py --skip-questions  # patterns only

Logs to /tmp/helix_pq_backfill.log so the parent can poll Supabase via MCP
rather than waiting on bash timeouts.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

PATTERN_SYNC_URL = os.environ.get(
    "N8N_PATTERN_SYNC_URL",
    "http://localhost:5678/webhook/helix-patterns",
)
QUESTION_SYNC_URL = os.environ.get(
    "N8N_QUESTION_SYNC_URL",
    "http://localhost:5678/webhook/helix-questions",
)
PER_REQUEST_TIMEOUT_S = 240
LOG_PATH = Path("/tmp/helix_pq_backfill.log")

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
        "--skip-patterns",
        action="store_true",
        help="Skip Pattern Sync; only fire Question Sync.",
    )
    parser.add_argument(
        "--skip-questions",
        action="store_true",
        help="Skip Question Sync; only fire Pattern Sync.",
    )
    args = parser.parse_args()

    LOG_PATH.write_text("")  # truncate
    log("Backfill starting — {} patients".format(len(PATIENTS)))
    log("Pattern Sync URL: {}".format(PATTERN_SYNC_URL))
    log("Question Sync URL: {}".format(QUESTION_SYNC_URL))

    pattern_results = []
    if not args.skip_patterns:
        log("--- Phase 1: Pattern Sync ---")
        for pid in PATIENTS:
            ok = fire(PATTERN_SYNC_URL, pid, "Pattern")
            pattern_results.append((pid, ok))
            time.sleep(2)
        log("Pattern Sync complete: {} / {} succeeded".format(
            sum(1 for _, ok in pattern_results if ok), len(pattern_results)
        ))
    else:
        log("--- Phase 1: Pattern Sync SKIPPED ---")

    question_results = []
    if not args.skip_questions:
        log("--- Phase 2: Question Sync ---")
        for pid in PATIENTS:
            ok = fire(QUESTION_SYNC_URL, pid, "Question")
            question_results.append((pid, ok))
            time.sleep(2)
        log("Question Sync complete: {} / {} succeeded".format(
            sum(1 for _, ok in question_results if ok), len(question_results)
        ))
    else:
        log("--- Phase 2: Question Sync SKIPPED ---")

    log("--- Backfill complete ---")
    if pattern_results:
        log("Pattern Sync results:")
        for pid, ok in pattern_results:
            log("  {} {}".format("OK  " if ok else "FAIL", pid))
    if question_results:
        log("Question Sync results:")
        for pid, ok in question_results:
            log("  {} {}".format("OK  " if ok else "FAIL", pid))

    pattern_failed = sum(1 for _, ok in pattern_results if not ok)
    question_failed = sum(1 for _, ok in question_results if not ok)
    return 0 if (pattern_failed + question_failed) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
