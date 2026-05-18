#!/usr/bin/env python3
"""Phase 3.4a — Backfill `summaries` rows for SYNTH-002 through SYNTH-010
by firing Helix CRM Sync against each patient PDF.

Logs progress to /tmp/helix_backfill.log so the parent (Cowork session) can
poll status via Supabase MCP rather than waiting on the bash timeout.

Usage:
    python3 scripts/backfill_summaries.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pdfplumber
import requests

# n8n CRM Sync webhook — defaults to localhost so this runs from the user's
# Mac terminal. Override via N8N_CRM_SYNC_URL env var if running elsewhere.
WEBHOOK_URL = os.environ.get(
    "N8N_CRM_SYNC_URL",
    "http://localhost:5678/webhook/helix-sync",
)
PER_REQUEST_TIMEOUT_S = 240  # Helix Extractor + Supabase write
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT_ROOT / "test-data" / "synthetic-patients"
LOG_PATH = Path("/tmp/helix_backfill.log")

# (patient_id, pdf_filename) — order matters: keeps log grep-friendly.
PATIENTS = [
    ("SYNTH-002", "02_chen_diabetes_followup.pdf"),
    ("SYNTH-003", "03_williams_ed_chest_pain.pdf"),
    ("SYNTH-004", "04_parker_pediatric_otitis.pdf"),
    ("SYNTH-006", "06_patel_hypothyroid_consult.pdf"),
    ("SYNTH-007", "07_johnson_psych_followup.pdf"),
    ("SYNTH-008", "08_schmidt_postop_knee.pdf"),
    ("SYNTH-009", "09_walker_med_reconciliation.pdf"),
    ("SYNTH-010", "10_doe_ambiguous_urgent_care.pdf"),
]


def log(msg: str) -> None:
    line = "[{}] {}\n".format(
        time.strftime("%H:%M:%S", time.localtime()), msg
    )
    sys.stdout.write(line)
    sys.stdout.flush()
    with LOG_PATH.open("a") as f:
        f.write(line)


def extract_text(pdf_path: Path) -> str:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n\n".join(pages).strip()


def fire(patient_id: str, pdf_filename: str) -> bool:
    pdf_path = PDF_DIR / pdf_filename
    if not pdf_path.exists():
        log("{} | MISSING PDF: {}".format(patient_id, pdf_path))
        return False

    try:
        text = extract_text(pdf_path)
    except Exception as exc:  # noqa: BLE001
        log("{} | PDF extract failed: {}: {}".format(
            patient_id, type(exc).__name__, exc
        ))
        return False

    if not text:
        log("{} | empty extracted text".format(patient_id))
        return False

    log("{} | extracted {} chars; firing webhook...".format(
        patient_id, len(text)
    ))

    started = time.monotonic()
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"text": text},
            timeout=PER_REQUEST_TIMEOUT_S,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        log("{} | webhook error: {}: {}".format(
            patient_id, type(exc).__name__, exc
        ))
        return False

    elapsed = time.monotonic() - started

    if resp.status_code != 200:
        log("{} | HTTP {} after {:.1f}s; body[:200]={}".format(
            patient_id, resp.status_code, elapsed, resp.text[:200]
        ))
        return False

    try:
        body = resp.json()
        body_summary = "id={}".format(body.get("id", "<missing>"))
    except ValueError:
        body_summary = "non-JSON body"

    log("{} | OK in {:.1f}s ({})".format(patient_id, elapsed, body_summary))
    return True


def main() -> int:
    LOG_PATH.write_text("")  # truncate
    log("Backfill starting — {} patients".format(len(PATIENTS)))

    results = []
    for patient_id, pdf_filename in PATIENTS:
        ok = fire(patient_id, pdf_filename)
        results.append((patient_id, ok))
        # Small gap between requests to avoid n8n stampede.
        time.sleep(2)

    n_ok = sum(1 for _, ok in results if ok)
    log("Backfill complete: {} / {} succeeded".format(n_ok, len(results)))

    # Summary report.
    for pid, ok in results:
        log("  {} {}".format("OK  " if ok else "FAIL", pid))

    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
