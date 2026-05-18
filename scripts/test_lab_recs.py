#!/usr/bin/env python3
"""Lab Recs regression harness — fires the n8n webhook against synthetic patients,
validates the response and the resulting Supabase row against the locked
Lab Recs schema (helix-tasks.md §3.2c), and prints a pass/fail report.

Usage:
    python3 scripts/test_lab_recs.py                   # all patients
    python3 scripts/test_lab_recs.py SYNTH-005         # one patient
    python3 scripts/test_lab_recs.py SYNTH-002 SYNTH-005

Env (loaded from project-root .env via python-dotenv):
    N8N_WEBHOOK_URL        e.g. http://localhost:5678/webhook/helix-lab-recs
    SUPABASE_URL           e.g. https://<project>.supabase.co
    SUPABASE_SERVICE_KEY   service_role key — NEVER printed or committed.

Exit code: 0 if every test passes, 1 if any fail.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- third-party imports (with helpful error messages) ----------------------
try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 'requests' is not installed.\n"
        "Install with: python3 -m pip install requests python-dotenv\n"
    )
    sys.exit(2)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 'python-dotenv' is not installed.\n"
        "Install with: python3 -m pip install requests python-dotenv\n"
    )
    sys.exit(2)

# Local fixtures module — same dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_recs_fixtures import (  # noqa: E402
    PATIENT_FIXTURES,
    PATIENTS_ALLOWED_EMPTY_UNCERTAINTY,
    all_test_patients,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "test-results"

WEBHOOK_TIMEOUT_S = 120  # Tool Agent makes ~10 LLM tool calls
SUPABASE_TIMEOUT_S = 30

REQUIRED_REC_KEYS = (
    "lab",
    "rationale",
    "normal_range",
    "monitoring_interval",
    "priority",
    "guideline_source",
)
ALLOWED_PRIORITIES = {"high", "medium", "low"}
GUIDELINE_SANITY_RX = re.compile(r"NICE|USPSTF", re.IGNORECASE)
MIN_RECS = 5
MAX_RECS = 10

# Console formatting (ANSI; harmless if terminal doesn't render).
GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CHECK = GREEN + "OK " + RESET
CROSS = RED + "FAIL" + RESET


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class PatientResult:
    patient_id: str
    passed: bool
    duration_s: float
    lab_recs_id: Optional[str] = None
    webhook_status: Optional[int] = None
    failures: List[str] = field(default_factory=list)
    schema_checks: List[CheckResult] = field(default_factory=list)
    fixture_checks: List[CheckResult] = field(default_factory=list)
    raw_webhook_body: Any = None
    raw_supabase_row: Any = None


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def load_env() -> Dict[str, str]:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    required = ("N8N_WEBHOOK_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.stderr.write(
            "ERROR: missing required env var(s): {}\n"
            "Copy .env.example to .env and fill in real values.\n".format(
                ", ".join(missing)
            )
        )
        sys.exit(2)

    # Normalize SUPABASE_URL — no trailing slash.
    return {
        "N8N_WEBHOOK_URL": os.environ["N8N_WEBHOOK_URL"].rstrip("/"),
        "SUPABASE_URL": os.environ["SUPABASE_URL"].rstrip("/"),
        "SUPABASE_SERVICE_KEY": os.environ["SUPABASE_SERVICE_KEY"],
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_schema(
    recommendations_payload: Any,
    uncertainty_notes: Any,
    patient_id: str,
) -> List[CheckResult]:
    """Return one CheckResult per schema rule, in the order they're checked."""
    checks: List[CheckResult] = []

    # Top-level must be a dict-like payload {recommendations, uncertainty_notes}
    payload_ok = isinstance(recommendations_payload, list)
    checks.append(CheckResult(
        name="recommendations_is_array",
        passed=payload_ok,
        detail="recommendations must be a JSON array (got {})".format(
            type(recommendations_payload).__name__
        ),
    ))
    if not payload_ok:
        return checks  # everything downstream depends on this

    recs: List[Any] = recommendations_payload

    # length 5-10
    length_ok = MIN_RECS <= len(recs) <= MAX_RECS
    checks.append(CheckResult(
        name="recommendations_length_5_to_10",
        passed=length_ok,
        detail="recommendations length must be {}-{} (got {})".format(
            MIN_RECS, MAX_RECS, len(recs)
        ),
    ))

    # Per-rec checks. Aggregate into one CheckResult per rule, listing offenders.
    missing_keys_offenders: List[str] = []
    empty_value_offenders: List[str] = []
    bad_priority_offenders: List[str] = []
    bad_guideline_offenders: List[str] = []

    for idx, rec in enumerate(recs):
        if not isinstance(rec, dict):
            missing_keys_offenders.append("rec[{}] is not an object".format(idx))
            continue

        missing = [k for k in REQUIRED_REC_KEYS if k not in rec]
        if missing:
            missing_keys_offenders.append("rec[{}] missing {}".format(idx, missing))

        for k in REQUIRED_REC_KEYS:
            if k in rec and not _is_nonempty_str(rec.get(k)):
                empty_value_offenders.append("rec[{}].{}".format(idx, k))

        priority = rec.get("priority")
        if priority not in ALLOWED_PRIORITIES:
            bad_priority_offenders.append(
                "rec[{}].priority={!r}".format(idx, priority)
            )

        guideline = rec.get("guideline_source") or ""
        if not GUIDELINE_SANITY_RX.search(guideline):
            bad_guideline_offenders.append(
                "rec[{}].guideline_source={!r}".format(idx, guideline)
            )

    checks.append(CheckResult(
        name="all_required_keys_present",
        passed=not missing_keys_offenders,
        detail="; ".join(missing_keys_offenders) or "every rec has all six required keys",
    ))
    checks.append(CheckResult(
        name="all_values_non_empty_strings",
        passed=not empty_value_offenders,
        detail="empty/non-string fields: {}".format(empty_value_offenders)
            if empty_value_offenders else "every rec field is a non-empty string",
    ))
    checks.append(CheckResult(
        name="priority_in_allowed_set",
        passed=not bad_priority_offenders,
        detail="; ".join(bad_priority_offenders)
            or "every priority is one of high|medium|low",
    ))
    checks.append(CheckResult(
        name="guideline_source_cites_NICE_or_USPSTF",
        passed=not bad_guideline_offenders,
        detail="; ".join(bad_guideline_offenders)
            or "every guideline_source mentions NICE or USPSTF",
    ))

    # Uncertainty notes
    notes_required = patient_id not in PATIENTS_ALLOWED_EMPTY_UNCERTAINTY
    notes_ok = (not notes_required) or _is_nonempty_str(uncertainty_notes)
    checks.append(CheckResult(
        name="uncertainty_notes_present",
        passed=notes_ok,
        detail=(
            "uncertainty_notes must be non-empty for {} (got {!r})".format(
                patient_id, uncertainty_notes
            ) if not notes_ok else
            "uncertainty_notes acceptable ({} chars)".format(
                len(uncertainty_notes) if isinstance(uncertainty_notes, str) else 0
            )
        ),
    ))

    return checks


# ---------------------------------------------------------------------------
# Network calls
# ---------------------------------------------------------------------------

def call_webhook(env: Dict[str, str], patient_id: str) -> Tuple[int, Any, Optional[str]]:
    """POST to n8n webhook. Returns (status, parsed_body_or_text, error_message)."""
    try:
        resp = requests.post(
            env["N8N_WEBHOOK_URL"],
            json={"patient_id": patient_id},
            timeout=WEBHOOK_TIMEOUT_S,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        return 0, None, "webhook request failed: {}: {}".format(
            type(exc).__name__, exc
        )

    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    return resp.status_code, body, None


def fetch_lab_recs_row(env: Dict[str, str], lab_recs_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch the lab_recs row from Supabase. Returns (row_or_None, error_message)."""
    url = "{}/rest/v1/lab_recs".format(env["SUPABASE_URL"])
    params = {"id": "eq.{}".format(lab_recs_id), "select": "*"}
    headers = {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": "Bearer {}".format(env["SUPABASE_SERVICE_KEY"]),
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=SUPABASE_TIMEOUT_S)
    except requests.RequestException as exc:
        return None, "Supabase request failed: {}: {}".format(
            type(exc).__name__, exc
        )

    if resp.status_code != 200:
        return None, "Supabase returned HTTP {} for lab_recs id {}".format(
            resp.status_code, lab_recs_id
        )

    try:
        rows = resp.json()
    except ValueError:
        return None, "Supabase returned non-JSON body"

    if not isinstance(rows, list) or not rows:
        return None, "Supabase returned no row for lab_recs id {}".format(lab_recs_id)

    return rows[0], None


# ---------------------------------------------------------------------------
# Per-patient flow
# ---------------------------------------------------------------------------

def _extract_lab_recs_id(body: Any) -> Optional[str]:
    """The webhook returns success=true plus the inserted row's id under
    one of a few plausible keys. Try them in order."""
    if not isinstance(body, dict):
        return None
    for key in ("lab_recs_id", "id"):
        if isinstance(body.get(key), str):
            return body[key]
    # Sometimes the upserted row is nested.
    row = body.get("row") if isinstance(body.get("row"), dict) else None
    if row and isinstance(row.get("id"), str):
        return row["id"]
    return None


def run_one(env: Dict[str, str], patient_id: str) -> PatientResult:
    started = time.monotonic()
    result = PatientResult(patient_id=patient_id, passed=False, duration_s=0.0)

    status, body, err = call_webhook(env, patient_id)
    result.webhook_status = status
    result.raw_webhook_body = body
    if err:
        result.failures.append(err)
        result.duration_s = time.monotonic() - started
        return result

    if status != 200:
        result.failures.append("webhook returned HTTP {}".format(status))
        result.duration_s = time.monotonic() - started
        return result

    # The n8n workflow returns the full inserted Supabase row (no success wrapper).
    # Treat any 200 + JSON-object body as success; downstream id extraction confirms.
    if not isinstance(body, dict):
        result.failures.append(
            "webhook body is not a JSON object (got: {})".format(
                type(body).__name__ if not isinstance(body, str) else body[:300]
            )
        )
        result.duration_s = time.monotonic() - started
        return result

    lab_recs_id = _extract_lab_recs_id(body)
    if not lab_recs_id:
        result.failures.append(
            "could not find lab_recs_id in webhook body (keys: {})".format(
                list(body.keys()) if isinstance(body, dict) else type(body).__name__
            )
        )
        result.duration_s = time.monotonic() - started
        return result
    result.lab_recs_id = lab_recs_id

    row, err = fetch_lab_recs_row(env, lab_recs_id)
    if err or row is None:
        result.failures.append(err or "Supabase row missing")
        result.duration_s = time.monotonic() - started
        return result
    result.raw_supabase_row = row

    recommendations = row.get("recommendations")
    uncertainty_notes = row.get("uncertainty_notes")

    # Supabase jsonb columns come back already-decoded, but tolerate string form.
    if isinstance(recommendations, str):
        try:
            recommendations = json.loads(recommendations)
        except ValueError:
            result.failures.append("recommendations column is non-JSON string")
            result.duration_s = time.monotonic() - started
            return result

    schema_checks = validate_schema(recommendations, uncertainty_notes, patient_id)
    result.schema_checks = schema_checks
    for c in schema_checks:
        if not c.passed:
            result.failures.append("schema/{}: {}".format(c.name, c.detail))

    # Patient fixtures (predicates run against {recommendations, uncertainty_notes} dict).
    payload_for_fixtures = {
        "recommendations": recommendations if isinstance(recommendations, list) else [],
        "uncertainty_notes": uncertainty_notes,
    }
    for fixture in PATIENT_FIXTURES.get(patient_id, []):
        passed, detail = fixture.run(payload_for_fixtures)
        result.fixture_checks.append(CheckResult(
            name=fixture.name, passed=passed, detail=detail,
        ))
        if not passed:
            result.failures.append("fixture/{}: {}".format(fixture.name, detail))

    result.passed = not result.failures
    result.duration_s = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_console_report(results: List[PatientResult]) -> None:
    print()
    print(BOLD + "Lab Recs regression — {} patient(s)".format(len(results)) + RESET)
    print("-" * 78)
    header = "{:<11} {:<6} {:>7}  {:<36}  {}".format(
        "patient", "result", "dur(s)", "lab_recs_id", "summary"
    )
    print(header)
    print("-" * 78)

    for r in results:
        mark = CHECK if r.passed else CROSS
        lab_id = r.lab_recs_id or "-"
        if r.passed:
            n_schema = sum(1 for c in r.schema_checks if c.passed)
            n_fixture = sum(1 for c in r.fixture_checks if c.passed)
            summary = "{} schema, {} fixture checks".format(n_schema, n_fixture)
        else:
            summary = "{} failure(s)".format(len(r.failures))
        print("{:<11} {}   {:>7.1f}  {:<36}  {}".format(
            r.patient_id, mark, r.duration_s, lab_id, summary
        ))

        if not r.passed:
            for f in r.failures:
                print("              {}- {}{}".format(DIM, f, RESET))

    print("-" * 78)
    n_pass = sum(1 for r in results if r.passed)
    overall = (CHECK if n_pass == len(results) else CROSS)
    print("{}  {} / {} patients passed".format(overall, n_pass, len(results)))
    print()


def write_json_report(results: List[PatientResult]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / "lab_recs_{}.json".format(ts)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    env = load_env()

    patients = argv[1:] if len(argv) > 1 else all_test_patients()

    print("Webhook: {}".format(env["N8N_WEBHOOK_URL"]))
    print("Supabase: {}".format(env["SUPABASE_URL"]))
    print("Patients: {}".format(", ".join(patients)))
    print()

    results: List[PatientResult] = []
    for pid in patients:
        print("[{}] firing webhook...".format(pid), flush=True)
        r = run_one(env, pid)
        mark = "OK" if r.passed else "FAIL"
        print("[{}] {} in {:.1f}s".format(pid, mark, r.duration_s), flush=True)
        results.append(r)

    print_console_report(results)
    out_path = write_json_report(results)
    rel = out_path.relative_to(PROJECT_ROOT) if out_path.is_relative_to(PROJECT_ROOT) else out_path
    print("Wrote: {}".format(rel))

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
