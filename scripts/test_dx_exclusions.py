#!/usr/bin/env python3
"""Dx Exclusion regression harness — fires the n8n webhook against synthetic patients,
validates the response and the resulting Supabase row against the locked
Dx Exclusion schema (helix-tasks.md §3.3b), and prints a pass/fail report.

Mirrors `scripts/test_lab_recs.py` — same env, same auth, same JSON-fence
extraction. Differences: webhook path (`helix-dx`), Supabase table
(`dx_exclusions`), jsonb column (`exclusions`), schema rules.

Usage:
    python3 scripts/test_dx_exclusions.py                   # all patients
    python3 scripts/test_dx_exclusions.py SYNTH-005         # one patient
    python3 scripts/test_dx_exclusions.py SYNTH-001 SYNTH-005

Env (loaded from project-root .env via python-dotenv):
    N8N_DX_WEBHOOK_URL     e.g. http://localhost:5678/webhook/helix-dx
                            (falls back to N8N_WEBHOOK_URL with /helix-lab-recs
                             swapped to /helix-dx if not set)
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
from dx_exclusions_fixtures import (  # noqa: E402
    PATIENT_FIXTURES,
    PATIENTS_ALLOWED_EMPTY_UNCERTAINTY,
    PATIENTS_ALLOWED_SPARSE_EXCLUSIONS,
    all_test_patients,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "test-results"

WEBHOOK_TIMEOUT_S = 600  # Tool Agent runs vary 60s-5min; 10-min ceiling per observed worst case
SUPABASE_TIMEOUT_S = 30

REQUIRED_EX_KEYS = (
    "diagnosis",
    "considered_because",
    "exclusion_rationale",
    "confidence",
    "guideline_source",
    "residual_risk_notes",
)
ALLOWED_CONFIDENCES = {"high", "medium", "low"}
GUIDELINE_SANITY_RX = re.compile(r"NICE|USPSTF", re.IGNORECASE)
MIN_EXCL = 3
MAX_EXCL = 8

# Console formatting
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
    dx_exclusions_id: Optional[str] = None
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

    # Webhook URL — prefer dedicated var, else derive from lab_recs URL.
    webhook = os.environ.get("N8N_DX_WEBHOOK_URL")
    if not webhook:
        lab_url = os.environ.get("N8N_WEBHOOK_URL", "")
        if lab_url:
            webhook = lab_url.replace("/helix-lab-recs", "/helix-dx")
        if not webhook or "helix-dx" not in webhook:
            sys.stderr.write(
                "ERROR: missing N8N_DX_WEBHOOK_URL (or N8N_WEBHOOK_URL pointing at\n"
                "the lab-recs endpoint to derive from).\n"
            )
            sys.exit(2)

    required_remaining = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    missing = [k for k in required_remaining if not os.environ.get(k)]
    if missing:
        sys.stderr.write(
            "ERROR: missing required env var(s): {}\n"
            "Copy .env.example to .env and fill in real values.\n".format(
                ", ".join(missing)
            )
        )
        sys.exit(2)

    return {
        "N8N_WEBHOOK_URL": webhook.rstrip("/"),
        "SUPABASE_URL": os.environ["SUPABASE_URL"].rstrip("/"),
        "SUPABASE_SERVICE_KEY": os.environ["SUPABASE_SERVICE_KEY"],
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_schema(
    exclusions_payload: Any,
    uncertainty_notes: Any,
    patient_id: str,
) -> List[CheckResult]:
    """Return one CheckResult per schema rule, in the order they're checked."""
    checks: List[CheckResult] = []

    payload_ok = isinstance(exclusions_payload, list)
    checks.append(CheckResult(
        name="exclusions_is_array",
        passed=payload_ok,
        detail="exclusions must be a JSON array (got {})".format(
            type(exclusions_payload).__name__
        ),
    ))
    if not payload_ok:
        return checks

    excls: List[Any] = exclusions_payload

    # Length 3-8, OR sparse-allowed (e.g., SYNTH-001 healthy baseline).
    sparse_allowed = patient_id in PATIENTS_ALLOWED_SPARSE_EXCLUSIONS
    if sparse_allowed:
        length_ok = 0 <= len(excls) <= MAX_EXCL
        length_detail = "exclusions length must be 0-{} for {} (got {})".format(
            MAX_EXCL, patient_id, len(excls)
        )
    else:
        length_ok = MIN_EXCL <= len(excls) <= MAX_EXCL
        length_detail = "exclusions length must be {}-{} (got {})".format(
            MIN_EXCL, MAX_EXCL, len(excls)
        )
    checks.append(CheckResult(
        name="exclusions_length_within_bounds",
        passed=length_ok,
        detail=length_detail,
    ))

    # Per-exclusion checks. Aggregate offenders across all rules.
    missing_keys_offenders: List[str] = []
    empty_value_offenders: List[str] = []
    bad_confidence_offenders: List[str] = []
    bad_guideline_offenders: List[str] = []

    for idx, ex in enumerate(excls):
        if not isinstance(ex, dict):
            missing_keys_offenders.append("ex[{}] is not an object".format(idx))
            continue

        missing = [k for k in REQUIRED_EX_KEYS if k not in ex]
        if missing:
            missing_keys_offenders.append("ex[{}] missing {}".format(idx, missing))

        for k in REQUIRED_EX_KEYS:
            if k in ex and not _is_nonempty_str(ex.get(k)):
                empty_value_offenders.append("ex[{}].{}".format(idx, k))

        confidence = ex.get("confidence")
        if confidence not in ALLOWED_CONFIDENCES:
            bad_confidence_offenders.append(
                "ex[{}].confidence={!r}".format(idx, confidence)
            )

        guideline = ex.get("guideline_source") or ""
        if not GUIDELINE_SANITY_RX.search(guideline):
            bad_guideline_offenders.append(
                "ex[{}].guideline_source={!r}".format(idx, guideline)
            )

    checks.append(CheckResult(
        name="all_required_keys_present",
        passed=not missing_keys_offenders,
        detail="; ".join(missing_keys_offenders)
            or "every exclusion has all six required keys",
    ))
    checks.append(CheckResult(
        name="all_values_non_empty_strings",
        passed=not empty_value_offenders,
        detail="empty/non-string fields: {}".format(empty_value_offenders)
            if empty_value_offenders else "every exclusion field is a non-empty string",
    ))
    checks.append(CheckResult(
        name="confidence_in_allowed_set",
        passed=not bad_confidence_offenders,
        detail="; ".join(bad_confidence_offenders)
            or "every confidence is one of high|medium|low",
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


def fetch_dx_exclusions_row(env: Dict[str, str], dx_id: str) -> Tuple[Optional[dict], Optional[str]]:
    url = "{}/rest/v1/dx_exclusions".format(env["SUPABASE_URL"])
    params = {"id": "eq.{}".format(dx_id), "select": "*"}
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
        return None, "Supabase returned HTTP {} for dx_exclusions id {}".format(
            resp.status_code, dx_id
        )

    try:
        rows = resp.json()
    except ValueError:
        return None, "Supabase returned non-JSON body"

    if not isinstance(rows, list) or not rows:
        return None, "Supabase returned no row for dx_exclusions id {}".format(dx_id)

    return rows[0], None


# ---------------------------------------------------------------------------
# Per-patient flow
# ---------------------------------------------------------------------------

def _extract_dx_id(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    for key in ("dx_exclusions_id", "id"):
        if isinstance(body.get(key), str):
            return body[key]
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

    if not isinstance(body, dict):
        result.failures.append(
            "webhook body is not a JSON object (got: {})".format(
                type(body).__name__ if not isinstance(body, str) else body[:300]
            )
        )
        result.duration_s = time.monotonic() - started
        return result

    dx_id = _extract_dx_id(body)
    if not dx_id:
        result.failures.append(
            "could not find dx_exclusions_id in webhook body (keys: {})".format(
                list(body.keys()) if isinstance(body, dict) else type(body).__name__
            )
        )
        result.duration_s = time.monotonic() - started
        return result
    result.dx_exclusions_id = dx_id

    row, err = fetch_dx_exclusions_row(env, dx_id)
    if err or row is None:
        result.failures.append(err or "Supabase row missing")
        result.duration_s = time.monotonic() - started
        return result
    result.raw_supabase_row = row

    exclusions = row.get("exclusions")
    uncertainty_notes = row.get("uncertainty_notes")

    if isinstance(exclusions, str):
        try:
            exclusions = json.loads(exclusions)
        except ValueError:
            result.failures.append("exclusions column is non-JSON string")
            result.duration_s = time.monotonic() - started
            return result

    schema_checks = validate_schema(exclusions, uncertainty_notes, patient_id)
    result.schema_checks = schema_checks
    for c in schema_checks:
        if not c.passed:
            result.failures.append("schema/{}: {}".format(c.name, c.detail))

    payload_for_fixtures = {
        "exclusions": exclusions if isinstance(exclusions, list) else [],
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
    print(BOLD + "Dx Exclusion regression — {} patient(s)".format(len(results)) + RESET)
    print("-" * 78)
    header = "{:<11} {:<6} {:>7}  {:<36}  {}".format(
        "patient", "result", "dur(s)", "dx_exclusions_id", "summary"
    )
    print(header)
    print("-" * 78)

    for r in results:
        mark = CHECK if r.passed else CROSS
        dx_id = r.dx_exclusions_id or "-"
        if r.passed:
            n_schema = sum(1 for c in r.schema_checks if c.passed)
            n_fixture = sum(1 for c in r.fixture_checks if c.passed)
            summary = "{} schema, {} fixture checks".format(n_schema, n_fixture)
        else:
            summary = "{} failure(s)".format(len(r.failures))
        print("{:<11} {}   {:>7.1f}  {:<36}  {}".format(
            r.patient_id, mark, r.duration_s, dx_id, summary
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
    path = RESULTS_DIR / "dx_exclusions_{}.json".format(ts)
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
