"""Per-patient fixture assertions for the Lab Recs test harness.

Each fixture maps a synthetic patient_id to a list of FixtureCheck objects.
A FixtureCheck looks at the recommendations array (and optionally the
uncertainty_notes) and returns (passed, detail).

Schema-only patients (no domain-specific clinical assertions yet) are listed
in SCHEMA_ONLY_PATIENTS so the harness still records that it tested them.

Add fixtures over time — start with SYNTH-002 and SYNTH-005 per spec 3.2f.cc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple


# ---------------------------------------------------------------------------
# FixtureCheck primitive
# ---------------------------------------------------------------------------

@dataclass
class FixtureCheck:
    """A single named clinical assertion against a Lab Recs payload."""

    name: str
    predicate: Callable[[dict], bool]
    detail: str  # human-readable description of what it checks

    def run(self, payload: dict) -> Tuple[bool, str]:
        try:
            ok = bool(self.predicate(payload))
        except Exception as exc:  # noqa: BLE001 — fixtures should never crash the harness
            return False, "{}: predicate raised {}: {}".format(
                self.name, type(exc).__name__, exc
            )
        return ok, self.detail


# ---------------------------------------------------------------------------
# Helpers used by predicates
# ---------------------------------------------------------------------------

def _recs(payload: dict) -> List[dict]:
    return payload.get("recommendations") or []


def _any_rec_matches(payload: dict, pattern: str, fields: Tuple[str, ...] = ("lab", "rationale")) -> bool:
    """Case-insensitive regex search across the named fields of every rec."""
    rx = re.compile(pattern, re.IGNORECASE)
    for rec in _recs(payload):
        for field in fields:
            value = rec.get(field) or ""
            if rx.search(value):
                return True
    return False


def _any_guideline_matches(payload: dict, pattern: str) -> bool:
    rx = re.compile(pattern, re.IGNORECASE)
    for rec in _recs(payload):
        if rx.search(rec.get("guideline_source") or ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Patient fixtures
# ---------------------------------------------------------------------------

# SYNTH-002 — Robert Chen, T2DM follow-up.
_SYNTH_002_CHECKS = [
    FixtureCheck(
        name="hba1c_present",
        predicate=lambda p: _any_rec_matches(p, r"\bHbA1c\b|\bA1C\b"),
        detail="At least one recommendation references HbA1c.",
    ),
    FixtureCheck(
        name="renal_function_present",
        predicate=lambda p: _any_rec_matches(
            p, r"\beGFR\b|\bcreatinine\b|\bACR\b|albumin[\s/-]*creatinine"
        ),
        detail="At least one recommendation references renal function (eGFR / creatinine / ACR).",
    ),
    FixtureCheck(
        name="diabetes_guideline_cited",
        predicate=lambda p: _any_guideline_matches(p, r"NG28|USPSTF.*(diabet|prediabet)"),
        detail="At least one guideline_source cites NICE NG28 or USPSTF diabetes guidance.",
    ),
]

# SYNTH-005 — Eleanor Hayes, geriatric multi-condition (HFpEF + AFib + T2DM + CKD + anemia).
_SYNTH_005_CHECKS = [
    FixtureCheck(
        name="renal_function_present",
        predicate=lambda p: _any_rec_matches(
            p, r"\beGFR\b|\bcreatinine\b|\bACR\b|albumin[\s/-]*creatinine"
        ),
        detail="At least one recommendation covers renal function (CKD monitoring).",
    ),
    FixtureCheck(
        name="ntprobnp_present",
        predicate=lambda p: _any_rec_matches(p, r"NT[\s-]*pro[\s-]*BNP|\bBNP\b"),
        detail="At least one recommendation covers NT-proBNP / BNP (HFpEF monitoring).",
    ),
    FixtureCheck(
        name="iron_studies_present",
        predicate=lambda p: _any_rec_matches(
            p, r"iron studies|ferritin|transferrin|TSAT|iron saturation"
        ),
        detail="At least one recommendation covers iron studies (anemia + HFpEF).",
    ),
    FixtureCheck(
        name="tsh_present",
        predicate=lambda p: _any_rec_matches(p, r"\bTSH\b|thyroid[\s-]*stimulating"),
        detail="At least one recommendation covers TSH (thyroid screening).",
    ),
    FixtureCheck(
        name="vitamin_d_present",
        predicate=lambda p: _any_rec_matches(p, r"vitamin[\s-]*d|25[\s-]*OH"),
        detail="At least one recommendation covers Vitamin D.",
    ),
    FixtureCheck(
        name="hf_guideline_cited",
        predicate=lambda p: _any_guideline_matches(p, r"NG106"),
        detail="At least one guideline_source cites NICE NG106 (chronic heart failure).",
    ),
]


# Map of patient_id -> list of FixtureCheck. Add more over time.
PATIENT_FIXTURES: Dict[str, List[FixtureCheck]] = {
    "SYNTH-002": _SYNTH_002_CHECKS,
    "SYNTH-005": _SYNTH_005_CHECKS,
}


# Patients exercised by the harness with schema-only validation (no clinical fixtures yet).
# SYNTH-001 is the healthy baseline — empty uncertainty_notes is acceptable for it.
SCHEMA_ONLY_PATIENTS: List[str] = [
    "SYNTH-001",
    "SYNTH-003",
    "SYNTH-004",
    "SYNTH-006",
    "SYNTH-007",
    "SYNTH-008",
    "SYNTH-009",
    "SYNTH-010",
]


# Patients allowed to have an empty uncertainty_notes string.
# Per spec: SYNTH-001 is healthy baseline; everyone else must document complexity.
PATIENTS_ALLOWED_EMPTY_UNCERTAINTY: List[str] = ["SYNTH-001"]


def all_test_patients() -> List[str]:
    """Ordered list of every patient the harness should hit."""
    seen: List[str] = []
    for pid in SCHEMA_ONLY_PATIENTS + list(PATIENT_FIXTURES.keys()):
        if pid not in seen:
            seen.append(pid)
    seen.sort()  # SYNTH-001 .. SYNTH-010
    return seen
